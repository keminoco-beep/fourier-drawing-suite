"""训练循环（training loop）：9h+，断点续训；本轮升级为**进化式 warm-start**。

问题：上一版每轮从零随机生成笔触（只做结构抖动），round 79 后 J 停在第 4 轮水平（17.44），
随机重启没有"累积学习"。本版：
- 每轮用 UCB 在两种模式间选择：
  * `fresh`   ：从零生成（探索，保持多样性）；
  * `mutate`  ：加载 best 笔触集作为父代，做变异（位置/宽度/alpha/颜色）、
                丢弃低收益笔、追加新笔触（进化，利用已有最优解）；
- 变异后的子代同样走 在线 RL（真渲染器 + 连续动作搜索）与次评分剪枝；
- 只有 J 更高才更新 best（含 best 笔触集存档），实现"训练越跑越好"；
- 每轮 state/日志/checkpoint 原子写，崩溃续训；DONE 标记正常结束。
"""
from __future__ import annotations

import json
import math
import os
import time
import traceback
from typing import Any

import numpy as np
from PIL import Image

from . import brushlib, coverage, decompose, finelines, io_format, parallel_eval, preprocess, structdoc, structpaint, stroke_rl_render
from . import render as render_mod
from .config import FBSConfig
from .util import WarningLog

_EVAL_CANDIDATES = (0.0, 0.10)  # 基础候选 + 周期剪枝候选（每 6 轮才多渲染一次）
_PSNR_DROP_TOL = 0.15
_MODES = ("fresh", "mutate")
_MUTATE_FRAC = 0.12       # 每轮变异父代笔触比例
_MUTATE_ADD = 320         # 每轮给变异子代追加的新笔触数
_DROP_FRAC = 0.08         # 变异时丢弃低收益笔比例
_KICK_AFTER = 25          # 连续多少轮不破 best 才考虑 kick
_KICK_INTERVAL = 12      # 两次 kick 的最小间隔（避免每轮都 kick 卡死）
_KICK_FRESH_ROUNDS = 3   # kick 后强制 fresh 重找精英的轮数
_KICK_STRENGTH = 2.2      # kick 时变异强度倍数


def _psnr_ssim(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    mse = float(np.mean((a - b) ** 2))
    psnr = 10.0 * math.log10(1.0 / max(mse, 1e-12))
    from .util import gaussian_blur

    ag, bg = a.mean(axis=2), b.mean(axis=2)
    mu_a = gaussian_blur(ag.astype(np.float32), 1.5)
    mu_b = gaussian_blur(bg.astype(np.float32), 1.5)
    va = gaussian_blur((ag * ag).astype(np.float32), 1.5) - mu_a ** 2
    vb = gaussian_blur((bg * bg).astype(np.float32), 1.5) - mu_b ** 2
    cov = gaussian_blur((ag * bg).astype(np.float32), 1.5) - mu_a * mu_b
    c1, c2 = 0.01 ** 2, 0.03 ** 2
    s = ((2 * mu_a * mu_b + c1) * (2 * cov + c2)) / ((mu_a ** 2 + mu_b ** 2 + c1) * (va + vb + c2))
    return psnr, float(s.mean())


def _stroke_to_json(st: dict) -> dict:
    out = {}
    for k, v in st.items():
        if isinstance(v, np.ndarray):
            out[k] = v.tolist()
        elif isinstance(v, np.generic):
            out[k] = v.item()
        else:
            out[k] = v
    return out


def _stroke_from_json(d: dict) -> dict:
    out = dict(d)
    for k in ("points_px", "widths_px", "color"):
        if k in out and out[k] is not None:
            out[k] = np.asarray(out[k], dtype=np.float64)
    return out


def _save_best_strokes(strokes: list[dict], path: str) -> None:
    payload = [_stroke_to_json(s) for s in strokes]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    os.replace(tmp, path)


def _load_best_strokes(path: str) -> list[dict] | None:
    if not os.path.exists(path):
        return None
    try:
        data = json.load(open(path, encoding="utf-8"))
        return [_stroke_from_json(d) for d in data]
    except Exception:
        return None


def _mutate_strokes(
    parent: list[dict],
    structure: dict,
    policies: dict,
    rng: np.random.Generator,
    strength: float = 1.0,
    width_cap_px: float | None = None,
    edge_strokes: bool = False,
    fresh_strokes: list[dict] | None = None,
) -> list[dict]:
    """进化变异：扰动一部分父代笔触 + 丢弃低收益 + 追加新笔触。

    strength>1 用于平台期 kick：抖动/删笔/加笔幅度按强度放大，帮助跳出局部最优。
    """
    st_m = float(strength)
    child = [dict(s) for s in parent]
    n = len(child)
    mut_frac = min(0.5, _MUTATE_FRAC * st_m)
    idxs = rng.choice(n, size=max(1, int(n * mut_frac)), replace=False) if n > 0 else []
    for i in idxs:
        s = dict(child[i])
        pts = np.asarray(s.get("points_px", []), dtype=np.float64)
        if pts.size:
            s["points_px"] = pts + rng.normal(0.0, 1.5 * st_m, pts.shape)
        w = np.asarray(s.get("widths_px", []), dtype=np.float64)
        if w.size:
            s["widths_px"] = np.maximum(
                0.5, w * float(rng.uniform(1.0 - 0.1 * st_m, 1.0 + 0.1 * st_m))
            )
        s["alpha"] = float(
            np.clip(
                float(s.get("alpha", 1.0))
                * float(rng.uniform(1.0 - 0.08 * st_m, 1.0 + 0.08 * st_m)),
                0.05,
                1.0,
            )
        )
        col = np.asarray(s.get("color", [0.5, 0.5, 0.5]), dtype=np.float64)
        s["color"] = np.clip(col + rng.normal(0.0, 0.02 * st_m, 3), 0.0, 1.0)
        child[i] = s
    if n > 0:
        benefits = np.asarray([float(s.get("benefit", 0.0)) for s in child], dtype=np.float64)
        drop_k = int(round(n * min(0.3, _DROP_FRAC * st_m)))
        if drop_k > 0:
            drop_idx = set(np.argsort(benefits)[:drop_k].tolist())
            child = [s for j, s in enumerate(child) if j not in drop_idx]
    if fresh_strokes is not None:
        fresh = [dict(s) for s in fresh_strokes]
    else:
        fresh = structpaint.paint_structure(structure, policies, width_cap_px=width_cap_px,
                                            edge_strokes=edge_strokes)
        for st in fresh:
            st["shape"] = brushlib.BRUSHES[str(st["preset"])]["shape"]
    if fresh:
        add_n = min(len(fresh), int(_MUTATE_ADD * st_m))
        add_idx = rng.choice(len(fresh), size=add_n, replace=False)
        child.extend(dict(fresh[j]) for j in add_idx)
    return child


def _jitter_structure(structure: dict, rng: np.random.Generator) -> dict:
    out = dict(structure)
    regs = []
    for st in structure["structures"]:
        ns = dict(st)
        if st["kind"] == "region":
            ns["orientation"] = float(st.get("orientation", 0.0)) + float(rng.normal(0.0, 0.03))
            c = np.asarray(st["color"], dtype=np.float64)
            ns["color"] = np.clip(c + rng.normal(0.0, 0.006, 3), 0.0, 1.0)
        else:
            w = np.asarray(st.get("widths_world", []), dtype=np.float64)
            if len(w):
                ns["widths_world"] = np.maximum(0.3 / float(structure["L"]), w * rng.uniform(0.95, 1.05))
            c = np.asarray(st.get("color", [0.1, 0.1, 0.12]), dtype=np.float64)
            ns["color"] = np.clip(c + rng.normal(0.0, 0.004, 3), 0.0, 1.0)
        regs.append(ns)
    out["structures"] = regs
    return out


def _eval_variant(structure: dict, strokes: list[dict], img: dict, image_path: str,
                  cfg: FBSConfig, eval_res: int, ref: np.ndarray):
    doc = structdoc.build_doc_from_strokes(strokes, img, image_path, cfg, WarningLog(),
                                           method="structure_brush_training")
    rgb_u8 = render_mod.render(doc, resolution=int(eval_res), supersample=2, mode="sweep",
                               warn=WarningLog())
    a = rgb_u8.astype(np.float64) / 255.0
    p, s = _psnr_ssim(a, ref)
    return doc, rgb_u8, p, s


def _pick_mode(state: dict, has_best: bool) -> str:
    stats = state.setdefault("mode_stats", {})
    for mode in _MODES:
        if mode == "mutate" and not has_best:
            continue
        if int((stats.get(mode) or {}).get("n", 0)) == 0:
            return mode
    total = sum(int(v.get("n", 0)) for v in stats.values()) or 0
    best, best_v = "fresh", -1e18
    for mode in _MODES:
        if mode == "mutate" and not has_best:
            continue
        st = stats.get(mode) or {"n": 0, "J_sum": 0.0}
        n = int(st.get("n", 0))
        mean = float(st.get("J_sum", 0.0)) / max(n, 1)
        explore = 0.3 * math.sqrt(math.log(max(total, 2)) / max(n, 1))
        v = mean + explore
        if v > best_v:
            best, best_v = mode, v
    return best


def _pick_seg(state: dict) -> str:
    stats = state.get("seg_stats") or {}
    for seg in ("palette", "contour"):
        if int((stats.get(seg) or {}).get("n", 0)) == 0:
            return seg
    total = sum(int(v.get("n", 0)) for v in stats.values()) or 0
    best, best_v = "palette", -1e18
    for seg in ("palette", "contour"):
        st = stats.get(seg) or {"n": 0, "J_sum": 0.0}
        n = int(st.get("n", 0))
        mean = float(st.get("J_sum", 0.0)) / max(n, 1)
        explore = 0.3 * math.sqrt(math.log(max(total, 2)) / max(n, 1))
        v = mean + explore
        if v > best_v:
            best, best_v = seg, v
    return best


def run_training_loop(
    input_path: str,
    outdir: str,
    hours: float = 9.0,
    eval_res: int = 1024,
    final_res: int = 2048,
    max_rounds: int | None = None,
    seed0: int = 20261018,
    n_candidates: int = 2,
    w_strokes: float = 0.02,
    w_segments: float = 0.01,
    safety_minutes: float = 8.0,
    eval_ss: int = 1,
    workers: int | None = None,
    policy_overrides: dict | None = None,
    width_cap_px: float | None = None,
    edge_strokes: bool = False,
    std_threshold: float | None = None,
    detail_area_frac: float | None = None,
    generator: str = "structpaint",
) -> dict:
    os.makedirs(outdir, exist_ok=True)
    ckpt_dir = os.path.join(outdir, "checkpoints")
    os.makedirs(ckpt_dir, exist_ok=True)
    state_path = os.path.join(outdir, "state.json")
    log_path = os.path.join(outdir, "training_log.jsonl")
    err_path = os.path.join(outdir, "errors.log")
    summary_path = os.path.join(outdir, "summary.md")
    best_strokes_path = os.path.join(ckpt_dir, "best_strokes.json")

    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
        state.setdefault("mode_stats", {})
        prev = state.get("eval_config")
        cur = {"res": int(eval_res), "ss": int(eval_ss), "doc": int(final_res), "gen": str(generator)}
        if prev and (int(prev.get("res", -1)) != cur["res"] or int(prev.get("ss", -1)) != cur["ss"]
                    or int(prev.get("doc", -1)) != cur["doc"] or str(prev.get("gen", "")) != cur["gen"]):
            legacy = f"legacy_R{prev.get('res')}_ss{prev.get('ss')}"
            for name in ("best_J.fbs.json", "best_J.json", "best_J_R%d.png" % int(prev.get("res", 0)),
                         "best_psnr.fbs.json", "best_psnr.json"):
                src_f = os.path.join(ckpt_dir, name)
                if os.path.exists(src_f):
                    os.replace(src_f, os.path.join(ckpt_dir, f"{legacy}_{name}"))
            print(f"[trainloop] 评估口径改变 {prev} → {cur}：旧 best 已归档为 {legacy}_*，"
                  f"best_strokes 保留作为进化种子；新口径 best 从本轮重新累计")
            state["best_J"] = -1e9
            state["best_psnr"] = -1e9
            state["best_ssim"] = 0.0
            state["best_round"] = -1
        state["eval_config"] = cur
        print(f"[trainloop] 断点续训：round={state.get('round')} deadline={state.get('deadline')}")
    else:
        now = time.time()
        state = {
            "round": 0, "started_at": now, "deadline": now + float(hours) * 3600.0,
            "best_J": -1e9, "best_psnr": -1e9, "best_ssim": 0.0, "best_round": -1,
            "best_strokes": 0, "best_segments": 0, "seg_stats": {}, "mode_stats": {},
            "eval_config": {"res": int(eval_res), "ss": int(eval_ss), "doc": int(final_res), "gen": str(generator)},
            "history": [],
        }
        with open(state_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=1)
    safety = min(float(safety_minutes), max(1.0, float(hours) * 60.0 * 0.2))
    deadline = float(state["deadline"]) - safety * 60.0
    print(f"[trainloop] 目标时长 {(state['deadline'] - state['started_at']) / 3600:.2f}h，"
          f"剩余 {(deadline - time.time()) / 3600:.2f}h")

    cfg = FBSConfig(target_resolution=int(eval_res))
    # F（训练-交付口径统一）：文档 Fourier 契约按最终渲染分辨率拟合，
    # 避免"768 拟合文档 → 2048 渲染"带来的形状/宽度精度损失。
    doc_cfg = FBSConfig(target_resolution=int(final_res))
    warn = WarningLog()
    t0 = time.perf_counter()
    img = preprocess.load_and_prepare(input_path, cfg, warn)
    cls = preprocess.classify(img, cfg)
    analysis = coverage.analyze(img, cfg, warn, policy_klass=str(cls.get("klass") or ""))
    analysis["klass"] = str(cls.get("klass") or analysis.get("klass"))
    dec = decompose.decompose(img, analysis, cfg, warn)
    structures: dict[str, dict] = {}
    if generator == "finelines":
        structures["finelines"] = {
            "rgb": np.asarray(img["rgb"], dtype=np.float64),
            "L": int(img["long_side"]), "W": int(img["width"]), "H": int(img["height"]),
            "structures": [], "counts": {"fine": 0},
        }
        print(f"[trainloop] 生成器=finelines（细线绘画），结构就绪（{time.perf_counter() - t0:.1f}s）")
    else:
        for seg in ("palette", "contour"):
            try:
                structures[seg] = structpaint.build_structure(
                    img, analysis, dec, cfg, warn, segmentation=seg,
                    std_threshold=std_threshold, detail_area_frac=detail_area_frac
                )
            except Exception:
                with open(err_path, "a", encoding="utf-8") as fh:
                    fh.write(f"[structure {seg}] {traceback.format_exc()}\n")
        if "palette" not in structures:
            raise RuntimeError("palette 结构构建失败，训练无法启动")
        print(f"[trainloop] 结构就绪（{time.perf_counter() - t0:.1f}s）："
              + "；".join(f"{k}={v['counts']}" for k, v in structures.items()))

    H = int(round(img["height"] * eval_res / max(img["width"], img["height"])))
    ref = np.asarray(Image.open(input_path).convert("RGB").resize((int(eval_res), H), Image.LANCZOS),
                     dtype=np.float64) / 255.0
    policies = {
        k: {"preset": v[0]["preset"], "width_scale": v[0]["width_scale"],
            "length_scale": v[0]["length_scale"], "alpha_scale": 1.0}
        for k, v in structpaint.POLICY_CANDIDATES.items()
    }
    if policy_overrides:
        for klass, ov in policy_overrides.items():
            if klass in policies:
                policies[klass].update({k: v for k, v in ov.items() if v is not None})
        print(f"[trainloop] 策略覆盖：{policy_overrides}")
    exp_json = os.path.join(os.path.dirname(os.path.abspath(outdir)), "experiments",
                            "brush_policy_experiments.json")
    if os.path.exists(exp_json):
        try:
            data = json.load(open(exp_json, encoding="utf-8"))
            for k, stats in (data.get("learned") or {}).items():
                if not stats:
                    continue
                best = max(stats.items(), key=lambda kv: kv[1].get("reward", 0.0))
                preset, w, l = best[0].split("|")
                policies[k] = {"preset": preset, "width_scale": float(w[1:]),
                               "length_scale": float(l[1:]), "alpha_scale": 1.0}
            print(f"[trainloop] 载入离线实验策略：{exp_json}")
        except Exception:
            pass  # noqa: 非关键路径（展示图/可选实验/最终图）失败不阻塞训练，主流程错误已写 errors.log

    rng = np.random.default_rng(int(seed0) + int(state["round"]))
    consecutive_errors = 0
    while time.time() < deadline:
        if max_rounds is not None and int(state["round"]) >= int(max_rounds):
            break
        if os.path.exists(os.path.join(outdir, "STOP")):
            print("[trainloop] 检测到 STOP 文件：停止训练并写 summary/DONE")
            break
        round_t0 = time.time()
        state["round"] = int(state["round"]) + 1
        try:
            seg = "finelines" if generator == "finelines" else _pick_seg(state)
            structure = structures[seg]
            structures_j = _jitter_structure(structure, rng)
            best_strokes = _load_best_strokes(best_strokes_path)
            stalled = (int(state["round"]) - int(state["best_round"])) if int(state.get("best_round", -1)) >= 0 else 0
            last_kick = int(state.get("last_kick_round", -10**9))
            force_fresh = int(state.get("force_fresh", 0))
            do_kick = (
                stalled >= _KICK_AFTER
                and int(state["round"]) - last_kick >= _KICK_INTERVAL
                and force_fresh <= 0
                and best_strokes is not None
                and generator != "finelines"
            )
            if generator == "finelines" and (stalled >= _KICK_AFTER or best_strokes is None):
                # 细线模式的"重新探索"：重新生成一批细线（参数随机）
                state["force_fresh"] = 0
                strokes = finelines.generate_strokes(img, rng, finelines.jitter_params(rng))
                mode = "fresh"
                src = f"finelines-fresh({len(strokes)})"
            elif do_kick:
                # 平台期 kick：放大变异 + 清空探索统计；之后强制若干轮 fresh 重找精英
                state["last_kick_round"] = int(state["round"])
                state["force_fresh"] = _KICK_FRESH_ROUNDS
                state["mode_stats"] = {}
                state["seg_stats"] = {}
                strokes = _mutate_strokes(best_strokes, structures_j, policies, rng,
                                          strength=_KICK_STRENGTH, width_cap_px=width_cap_px,
                                          edge_strokes=edge_strokes)
                mode = "kick"
                src = f"kick({len(best_strokes)}→{len(strokes)})"
            elif force_fresh > 0:
                state["force_fresh"] = force_fresh - 1
                mode = "fresh"
                if generator == "finelines":
                    strokes = finelines.generate_strokes(img, rng, finelines.jitter_params(rng))
                else:
                    strokes = structpaint.paint_structure(structures_j, policies,
                                                         width_cap_px=width_cap_px, edge_strokes=edge_strokes)
                    for st in strokes:
                        st["shape"] = brushlib.BRUSHES[str(st["preset"])]["shape"]
                src = f"fresh-reinit({len(strokes)})"
            else:
                mode = _pick_mode(state, has_best=best_strokes is not None)
                if mode == "mutate" and best_strokes is not None:
                    extra = (finelines.generate_extra_strokes(img, rng, finelines.jitter_params(rng), 0.18)
                             if generator == "finelines" else None)
                    strokes = _mutate_strokes(best_strokes, structures_j, policies, rng,
                                              width_cap_px=width_cap_px, edge_strokes=edge_strokes,
                                              fresh_strokes=extra)
                    src = f"mutate({len(best_strokes)}→{len(strokes)})"
                else:
                    mode = "fresh"
                    if generator == "finelines":
                        strokes = finelines.generate_strokes(img, rng, finelines.jitter_params(rng))
                    else:
                        strokes = structpaint.paint_structure(structures_j, policies,
                                                             width_cap_px=width_cap_px, edge_strokes=edge_strokes)
                        for st in strokes:
                            st["shape"] = brushlib.BRUSHES[str(st["preset"])]["shape"]
                    src = f"fresh({len(strokes)})"
            rl = stroke_rl_render.run_stroke_rl_search(
                structures_j["rgb"], strokes,
                log_path=os.path.join(outdir, "last_rl_log.json"),
                working_res=min(int(eval_res), 1024),
                rng=rng,
                min_improve=float(rng.uniform(0.0008, 0.0025)),
                n_candidates=int(n_candidates),
                w_strokes=w_strokes,
                w_segments=w_segments,
            )
            accepted = rl["accepted"]
            benefits = np.asarray([float(s.get("benefit", 0.0)) for s in accepted], dtype=np.float64)
            order = np.argsort(benefits)
            # 构建 3 个剪枝候选（0%/10%/20%），**并行**评估（瓶颈是渲染）
            import dataclasses as _dc

            cfg_dict = {f.name: getattr(doc_cfg, f.name) for f in _dc.fields(doc_cfg)}
            tmp_dir = os.path.join(outdir, "_tmp_eval")
            os.makedirs(tmp_dir, exist_ok=True)
            payloads = []
            kept_subs = []
            ref32 = np.asarray(ref, dtype=np.float32)
            for vi, frac in enumerate(_EVAL_CANDIDATES):
                k = int(round(len(accepted) * frac))
                keep_idx = sorted(order[k:].tolist()) if k > 0 else list(range(len(accepted)))
                sub = [accepted[i] for i in keep_idx]
                if not sub:
                    continue
                seg_c = sum(1 for st in sub if str(st.get("class", "")).startswith("line"))
                payloads.append({
                    "frac": frac, "strokes": sub, "segments": seg_c,
                    "cfg": cfg_dict, "img": {"rgb": img["rgb"], "rgba": img["rgba"],
                                                "alpha": img["alpha"], "width": img["width"],
                                                "height": img["height"], "long_side": img["long_side"]},
                    "input_path": input_path, "ref": ref32, "eval_res": int(eval_res),
                    "supersample": int(eval_ss),
                    "fbs_path": os.path.join(tmp_dir, f"v{vi}.fbs.json"),
                    "png_path": os.path.join(tmp_dir, f"v{vi}.png"),
                })
                kept_subs.append((frac, sub, seg_c))
            eval_t0 = time.time()
            results = parallel_eval.eval_variants_parallel(payloads, workers=workers)
            eval_s = round(time.time() - eval_t0, 1)
            variants = []
            for r in results:
                if not r.get("ok"):
                    continue
                variants.append({"frac": r["frac"], "psnr": r["psnr"], "ssim": r["ssim"],
                                 "segments": r["segments"], "strokes": r["strokes"],
                                 "fbs_path": r["fbs_path"], "png_path": r["png_path"]})
            if not variants:
                raise RuntimeError("所有并行评估候选都失败")
            for v in variants:
                v["J"] = (v["psnr"] - w_strokes * (int(v["strokes"]) / 1000.0)
                           - w_segments * (int(v["segments"]) / 100.0))
            base = variants[0]
            eligible = [v for v in variants if v["psnr"] >= base["psnr"] - _PSNR_DROP_TOL]
            best = max(eligible, key=lambda v: float(v["J"]))
            # 读回最佳候选的 doc / 渲染图（只读一个，体积小）
            best["doc"] = io_format.read_fbs(best["fbs_path"])
            best["rgb_u8"] = np.asarray(Image.open(best["png_path"]).convert("RGB"), dtype=np.uint8)
            # 保留对应的 stroke 列表（进化种子/checkpoint 需要）
            best["strokes"] = next(sub for frac, sub, _sc in kept_subs if frac == best["frac"])
            assert best is not None
            for key, val in (("seg_stats", seg), ("mode_stats", mode)):
                st = state[key].setdefault(val, {"n": 0, "J_sum": 0.0})
                st["n"] = int(st["n"]) + 1
                st["J_sum"] = float(st["J_sum"]) + float(best["J"])
            # 进化种子刷新：即使没破全局 best，只要本轮 J 比"种子自身 J"更好、且不差
            # 全局 best 太多（≤0.3dB），就替换种子——避免一直围着旧精英打转（平台期关键）。
            seed_J = float(state.get("seed_J", -1e9))
            if not os.path.exists(best_strokes_path) or (
                float(best["J"]) > seed_J and float(best["J"]) > float(state["best_J"]) - 0.3
            ):
                _save_best_strokes(best["strokes"], best_strokes_path)
                state["seed_J"] = float(best["J"])
            improved_J = best["J"] > float(state["best_J"])
            improved_psnr = best["psnr"] > float(state["best_psnr"])
            if improved_J:
                state["best_J"] = float(best["J"])
                state["best_round"] = int(state["round"])
                state["best_psnr"] = float(best["psnr"])
                state["best_ssim"] = float(best["ssim"])
                state["best_strokes"] = int(len(best["strokes"]))
                state["best_segments"] = int(best["segments"])
                _save_ckpt(best, state, ckpt_dir, input_path, eval_res, tag="best_J")
                _save_best_strokes(best["strokes"], best_strokes_path)
            elif improved_psnr:
                state["best_psnr"] = float(best["psnr"])
                state["best_ssim"] = float(best["ssim"])
                _save_ckpt(best, state, ckpt_dir, input_path, eval_res, tag="best_psnr")
            rec = {
                "round": int(state["round"]), "seg": seg, "mode": mode, "source": src,
                "planned": int(len(strokes)), "accepted": int(len(accepted)),
                "prune_frac": float(best["frac"]), "strokes": int(len(best["strokes"])),
                "segments": int(best["segments"]), "psnr": round(float(best["psnr"]), 4),
                "ssim": round(float(best["ssim"]), 5), "J": round(float(best["J"]), 4),
                "rl_canvas_psnr": float(rl["stats"]["canvas_psnr_db"]),
                "rl_s": round(float(rl.get("stats", {}).get("rl_seconds", 0.0)), 1),
                "eval_s": eval_s,
                "seconds": round(time.time() - round_t0, 1),
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            state["history"].append(rec)
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            _save_state(state_path, state)
            print(f"[round {rec['round']}] {mode}/{seg} {src} → accepted={rec['accepted']} "
                  f"strokes={rec['strokes']} segs={rec['segments']} PSNR={rec['psnr']:.3f} "
                  f"SSIM={rec['ssim']:.4f} J={rec['J']:.3f} ({rec['seconds']}s)", flush=True)
            consecutive_errors = 0
            if int(state["round"]) % 20 == 0 and time.time() < deadline - 600:
                try:
                    _save_final_res(best, state, ckpt_dir, input_path, final_res)
                except Exception:
                    pass  # noqa: 非关键路径（展示图/可选实验/最终图）失败不阻塞训练，主流程错误已写 errors.log
        except Exception:
            consecutive_errors += 1
            with open(err_path, "a", encoding="utf-8") as fh:
                fh.write(f"[round {state['round']}] {traceback.format_exc()}\n")
            print(f"[round {state['round']}] 失败（连续 {consecutive_errors} 次），继续", flush=True)
            if consecutive_errors >= 5:
                print("[trainloop] 连续失败过多，停止训练")
                break

    try:
        _save_final_res_best(state, ckpt_dir, input_path, final_res, summary_path, outdir)
    except Exception:
        with open(err_path, "a", encoding="utf-8") as fh:
            fh.write(f"[finalize] {traceback.format_exc()}\n")
    _save_state(state_path, state)
    with open(summary_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("# 训练循环总结\n\n")
        fh.write(f"- 开始：{time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(state['started_at']))}\n")
        fh.write(f"- 结束：{time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
        fh.write(f"- 轮数：{state['round']}\n")
        fh.write(f"- best J={state['best_J']:.4f} @round {state['best_round']}，"
                 f"PSNR={state['best_psnr']:.4f}，SSIM={state['best_ssim']:.4f}，"
                 f"笔数={state['best_strokes']}，线段={state['best_segments']}\n")
        fh.write("\n## 每轮记录\n\n| round | mode | seg | planned | accepted | prune | strokes | segs | PSNR | SSIM | J | s |\n"
                 "|---|---|---|---|---|---|---|---|---|---|---|---|\n")
        for r in state["history"]:
            fh.write(f"| {r['round']} | {r.get('mode','')} | {r['seg']} | {r['planned']} | {r['accepted']} | "
                     f"{r['prune_frac']} | {r['strokes']} | {r['segments']} | {r['psnr']} | {r['ssim']} | {r['J']} | {r['seconds']} |\n")
    try:
        with open(os.path.join(outdir, "DONE"), "w", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%dT%H:%M:%S\n"))
    except Exception:
        pass  # noqa: 非关键路径（展示图/可选实验/最终图）失败不阻塞训练，主流程错误已写 errors.log
    print(f"[trainloop] 训练结束：round={state['round']} best J={state['best_J']:.3f} "
          f"PSNR={state['best_psnr']:.3f} SSIM={state['best_ssim']:.4f}")
    return state


def _save_ckpt(best: dict, state: dict, ckpt_dir: str, input_path: str, eval_res: int, tag: str) -> None:
    from . import io_format

    io_format.write_fbs(best["doc"], os.path.join(ckpt_dir, f"{tag}.fbs.json"))
    Image.fromarray(best["rgb_u8"]).save(os.path.join(ckpt_dir, f"{tag}_R{eval_res}.png"))
    meta = {k: (float(v) if isinstance(v, (int, float)) else v) for k, v in best.items()
            if k not in ("strokes", "doc", "rgb_u8")}
    meta["strokes_count"] = len(best["strokes"])
    with open(os.path.join(ckpt_dir, f"{tag}.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=1)


def _save_final_res(best: dict, state: dict, ckpt_dir: str, input_path: str, final_res: int) -> None:
    stem = os.path.splitext(os.path.basename(input_path))[0]
    rgb = render_mod.render(best["doc"], resolution=int(final_res), supersample=1, mode="sweep",
                            warn=WarningLog())
    Image.fromarray(rgb).save(os.path.join(ckpt_dir, f"{stem}_R{final_res}.png"))


def _save_final_res_best(state: dict, ckpt_dir: str, input_path: str, final_res: int,
                         summary_path: str, outdir: str) -> None:
    from . import io_format

    ckpt = os.path.join(ckpt_dir, "best_J.fbs.json")
    if not os.path.exists(ckpt):
        return
    doc = io_format.read_fbs(ckpt)
    rgb = render_mod.render(doc, resolution=int(final_res), supersample=1, mode="sweep",
                            warn=WarningLog())
    stem = os.path.splitext(os.path.basename(input_path))[0]
    Image.fromarray(rgb).save(os.path.join(outdir, f"{stem}_best_R{final_res}.png"))
    # 若评估口径改变过，旧 best 被归档为 legacy_*：也渲染一份供最终对比
    import glob as _glob

    for legacy in _glob.glob(os.path.join(ckpt_dir, "legacy_*_best_J.fbs.json")):
        try:
            ldoc = io_format.read_fbs(legacy)
            lrgb = render_mod.render(ldoc, resolution=int(final_res), supersample=1,
                                     mode="sweep", warn=WarningLog())
            lname = os.path.basename(legacy).replace(".fbs.json", f"_R{final_res}.png")
            Image.fromarray(lrgb).save(os.path.join(outdir, lname))
        except Exception:
            pass  # noqa: 非关键路径（展示图/可选实验/最终图）失败不阻塞训练，主流程错误已写 errors.log


def _save_state(path: str, state: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
