"""在线笔触 RL —— **真渲染器版**（决策画布 = FloatCanvas + 真实笔形内核）。

上一版 RL（stroke_rl.py）用近似软边光栅化做决策，和最终 10 笔形渲染有偏差
（训练画布 16.36dB vs 真渲染 15.90dB）。本版把决策画布换成渲染器本身：
每落一笔 → `FloatCanvas.add_band` 真实合成（含 bristle/chalk/marker 等内核）→
在笔触局部比较目标误差 → 接受/撤回；每类结构用 UCB 在线学习"保守/标准/大胆"力度臂。

同时实现用户要求的**次评分标准**（低权重，防舍本逐末）：
  J = PSNR_train - w_strokes × (n_strokes/1000) - w_segments × (n_line_segments/100)
其中 n_strokes 为接受笔数、n_line_segments 为线条类笔数（连续线段数量的代理）。
默认 w_strokes=0.02、w_segments=0.01（相对 PSNR 量级很小）。

注意：工作分辨率 <= 1024 时 S=2 的决策画布约 2048²；R 更高时按 working_res 降采样决策，
避免 4096² 超采样画布的内存爆炸（训练决策在 1024 画布上做，最终出图仍按 R 渲染）。
"""
from __future__ import annotations

import json
import math
import os
from typing import Any

import numpy as np
from PIL import Image

from .render import FloatCanvas

ARMS: tuple[tuple[str, float, float], ...] = (
    ("保守", 0.85, 0.75),
    ("标准", 1.00, 1.00),
    ("大胆", 1.15, 1.00),
)
_DEFAULT_W_STROKES = 0.02
_DEFAULT_W_SEGMENTS = 0.01
_DEFAULT_MIN_IMPROVE = 2.0e-3
_LOG_DECIMALS = 6


def _r(x: float) -> float:
    return round(float(x), _LOG_DECIMALS)


def _ucb_pick(stats: dict[str, dict[str, float]], total: int, c: float = 0.4) -> str:
    best_k, best_v = None, -1e18
    for k, st in stats.items():
        mean = st["reward_sum"] / max(st["n"], 1)
        explore = c * math.sqrt(math.log(max(total, 2)) / max(st["n"], 1))
        v = mean + explore
        if v > best_v:
            best_v, best_k = v, k
    assert best_k is not None
    return best_k


def _target_ss(target: np.ndarray, S: int) -> np.ndarray:
    """目标图最近邻放大 S 倍（与 FloatCanvas 超采样坐标对齐；决策精度足够）。"""
    if S <= 1:
        return target.astype(np.float32)
    return np.repeat(np.repeat(target.astype(np.float32), S, axis=0), S, axis=1)


def run_stroke_rl_render(
    target_rgb: np.ndarray,
    strokes: list[dict],
    log_path: str | None = None,
    working_res: int = 1024,
    rng: np.random.Generator | None = None,
    min_improve: float = _DEFAULT_MIN_IMPROVE,
    w_strokes: float = _DEFAULT_W_STROKES,
    w_segments: float = _DEFAULT_W_SEGMENTS,
    S: int = 2,
) -> dict:
    """真渲染器在线 RL；返回 {accepted, stats}。strokes 的 points_px/widths_px 在目标分辨率。"""
    from .util import WarningLog

    rng = rng or np.random.default_rng(20261018)
    target = np.asarray(target_rgb, dtype=np.float64)
    Ht, Wt = target.shape[:2]
    # 决策分辨率（避免 SS=2 画布内存爆炸）
    scale = 1.0
    if max(Ht, Wt) > int(working_res):
        scale = float(working_res) / float(max(Ht, Wt))
        tw = max(8, int(round(Wt * scale)))
        th = max(8, int(round(Ht * scale)))
        t_img = Image.fromarray(np.clip(target * 255, 0, 255).astype(np.uint8)).resize(
            (tw, th), Image.LANCZOS
        )
        target_dec = np.asarray(t_img, dtype=np.float64) / 255.0
    else:
        target_dec = target.copy()
    H, W = target_dec.shape[:2]
    fc = FloatCanvas(W, H, S, (1.0, 1.0, 1.0))
    tgt_ss = _target_ss(target_dec, S)
    warn = WarningLog()

    stats: dict[str, dict[str, dict[str, float]]] = {}
    per_class: dict[str, dict[str, Any]] = {}
    accepted: list[dict] = []
    curve: list[dict] = []
    n = len(strokes)
    for idx in range(n):
        st = strokes[idx]
        klass = str(st.get("class", "flat"))
        if klass not in stats:
            stats[klass] = {name: {"reward_sum": 0.0, "n": 0, "acc": 0} for name, _w, _a in ARMS}
        arm = _ucb_pick(stats[klass], int(sum(v["n"] for v in stats[klass].values())))
        wm, am = next((w, a) for name, w, a in ARMS if name == arm)
        pts = np.asarray(st["points_px"], dtype=np.float64)
        if len(pts) < 2:
            continue
        widths = np.asarray(st["widths_px"], dtype=np.float64).reshape(-1) * wm
        if len(widths) == 1:
            widths = np.full(len(pts), widths[0])
        if len(widths) != len(pts):
            widths = np.interp(np.linspace(0, len(widths) - 1, len(pts)),
                               np.arange(len(widths)), widths)
        alpha = float(np.clip(float(st["alpha"]) * am, 0.03, 1.0))
        color = np.asarray(st["color"], dtype=np.float64).reshape(3)
        shape = str(st.get("shape") or "round")
        pts_d = pts * scale
        widths_d = np.maximum(1.0, widths * scale)
        radii = widths_d * 0.5
        # 局部 bbox：先算决策像素坐标，再换算到超采样画布坐标（add_band 内部会再乘 S，
        # 所以传给 add_band 的必须是决策像素坐标，不能预乘 S —— 上一版在这里重复缩放，
        # 导致笔触全画在错误位置、接受率暴跌）。
        bbox_margin = float(radii.max()) + 3.0
        x0 = max(0, int(math.floor(float(pts_d[:, 0].min() - bbox_margin)) * S))
        x1 = min(fc.Ws, int(math.ceil(float(pts_d[:, 0].max() + bbox_margin)) * S) + 1)
        y0 = max(0, int(math.floor(float(pts_d[:, 1].min() - bbox_margin)) * S))
        y1 = min(fc.Hs, int(math.ceil(float(pts_d[:, 1].max() + bbox_margin)) * S) + 1)
        if x1 <= x0 or y1 <= y0:
            continue
        before = fc.rgb[y0:y1, x0:x1].copy()
        old_err = float(np.abs(tgt_ss[y0:y1, x0:x1] - before).sum())
        fc.add_band(
            pts_d, radii,
            np.tile(color, (len(pts_d), 1)), np.full(len(pts_d), alpha),
            mode="sweep", shapes=[shape] * len(pts_d),
        )
        new_err = float(np.abs(tgt_ss[y0:y1, x0:x1] - fc.rgb[y0:y1, x0:x1]).sum())
        delta = (new_err - old_err) / max(old_err, 1e-9)
        accept = delta < -min_improve
        if not accept and abs(delta) <= min_improve and rng.random() < 0.25:
            accept = True
        if not accept:
            fc.rgb[y0:y1, x0:x1] = before
        reward = max(0.0, -delta)
        arm_st = stats[klass][arm]
        arm_st["reward_sum"] = float(arm_st["reward_sum"]) + reward
        arm_st["n"] = int(arm_st["n"]) + 1
        cls_st = per_class.setdefault(klass, {"n": 0, "accepted": 0, "delta_sum": 0.0, "segments": 0})
        cls_st["n"] = int(cls_st["n"]) + 1
        if accept:
            arm_st["acc"] = int(arm_st["acc"]) + 1
            cls_st["accepted"] = int(cls_st["accepted"]) + 1
            cls_st["delta_sum"] = float(cls_st["delta_sum"]) + delta
            if klass.startswith("line"):
                cls_st["segments"] = int(cls_st["segments"]) + 1
            out = dict(st)
            out["widths_px"] = widths
            out["alpha"] = alpha
            out["arm"] = arm
            accepted.append(out)
        if (idx + 1) % 250 == 0 or idx == n - 1:
            dec = fc.to_float()
            mae = float(np.abs(target_dec - dec).mean())
            curve.append({"stroke_index": int(idx + 1), "mean_abs_err": _r(mae),
                          "accepted_so_far": int(len(accepted))})

    # 最终决策画布指标（真渲染器在小画布上的结果）
    dec = fc.to_float()
    mse = float(np.mean((target_dec - dec) ** 2))
    psnr = 10.0 * math.log10(1.0 / max(mse, 1e-12))
    n_segments = sum(int(v["segments"]) for v in per_class.values())
    objective = float(psnr - w_strokes * (len(accepted) / 1000.0) - w_segments * (n_segments / 100.0))
    summary = {
        "strokes_total": int(n),
        "strokes_accepted": int(len(accepted)),
        "accept_rate": _r(len(accepted) / max(n, 1)),
        "segments_accepted": int(n_segments),
        "working_res": int(max(H, W)),
        "target_res": int(max(Ht, Wt)),
        "canvas_psnr_db": _r(psnr),
        "objective_J": _r(objective),
        "weights": {"w_strokes": w_strokes, "w_segments": w_segments, "min_improve": min_improve},
        "classes": per_class,
        "arms": {
            klass: {arm: {"n": int(st["n"]), "acc": int(st["acc"]),
                          "mean_reward": _r(st["reward_sum"] / max(st["n"], 1))}
                     for arm, st in arms.items()}
            for klass, arms in stats.items()
        },
        "curve": curve,
    }
    if log_path:
        os.makedirs(os.path.dirname(os.path.abspath(log_path)) or ".", exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=1)
        md = os.path.splitext(log_path)[0] + ".md"
        lines = [
            "# 在线笔触 RL（真渲染器版）训练日志\n",
            f"- 规划笔触 {n}，接受 **{len(accepted)}**（{summary['accept_rate'] * 100:.1f}%），"
            f"线条段 {n_segments}\n",
            f"- 决策画布 {summary['working_res']}px（真渲染器 SS=2），训练 PSNR **{psnr:.2f} dB**\n",
            f"- 目标函数 J = PSNR − {w_strokes}×(笔数/1000) − {w_segments}×(线段/100) = **{objective:.3f}**\n",
            "\n## 各结构类\\n\\n| 结构类 | 笔数 | 接受 | 平均ΔE | 接受线段 | 各臂(n/接受/平均奖励) |\\n|---|---|---|---|---|---|\\n",
        ]
        for klass, cs in per_class.items():
            arms_desc = "；".join(
                f"{arm}: n={v['n']} acc={v['acc']} R={v['mean_reward']:.4f}"
                for arm, v in summary["arms"].get(klass, {}).items()
            )
            lines.append(
                f"| {klass} | {cs['n']} | {cs['accepted']} | "
                f"{cs['delta_sum'] / max(cs['n'], 1):+.4f} | {cs['segments']} | {arms_desc} |\\n"
            )
        lines.append("\\n## 误差曲线（每 250 笔）\\n\\n| 笔数 | 平均绝对误差 | 已接受 |\\n|---|---|---|\\n")
        for c in curve:
            lines.append(f"| {c['stroke_index']} | {c['mean_abs_err']} | {c['accepted_so_far']} |\\n")
        with open(md, "w", encoding="utf-8", newline="\n") as fh:
            fh.writelines(lines)
        summary["md"] = md
    return {"accepted": accepted, "stats": summary}

def run_stroke_rl_search(
    target_rgb: np.ndarray,
    strokes: list[dict],
    log_path: str | None = None,
    working_res: int = 1024,
    rng: np.random.Generator | None = None,
    min_improve: float = 1.5e-3,
    n_candidates: int = 2,
    w_strokes: float = _DEFAULT_W_STROKES,
    w_segments: float = _DEFAULT_W_SEGMENTS,
    S: int = 2,
) -> dict:
    """真渲染器 + 连续动作搜索的在线 RL。

    每个笔触除力度臂（宽度/alpha 粗调）外，再采样 n_candidates 个连续动作：
      位置偏移 (dx,dy) ∈ [-4,4]px、宽度倍率 ∈ [0.8,1.2]、alpha 倍率 ∈ [0.85,1.15]、
      颜色偏移 ∈ ±0.03（世界单位 0..1）；
    逐个在真渲染器上局部评估，取局部误差最小的候选落笔（并记录每笔收益，供剪枝）。
    """
    from PIL import Image as _Image

    rng = rng or np.random.default_rng(20261018)
    target = np.asarray(target_rgb, dtype=np.float64)
    Ht, Wt = target.shape[:2]
    scale = 1.0
    if max(Ht, Wt) > int(working_res):
        scale = float(working_res) / float(max(Ht, Wt))
        tw = max(8, int(round(Wt * scale)))
        th = max(8, int(round(Ht * scale)))
        target_dec = np.asarray(
            _Image.fromarray(np.clip(target * 255, 0, 255).astype(np.uint8)).resize((tw, th), _Image.LANCZOS),
            dtype=np.float64) / 255.0
    else:
        target_dec = target.copy()
    H, W = target_dec.shape[:2]
    fc = FloatCanvas(W, H, S, (1.0, 1.0, 1.0))
    tgt_ss = _target_ss(target_dec, S)
    stats: dict[str, dict[str, dict[str, float]]] = {}
    per_class: dict[str, dict[str, Any]] = {}
    accepted: list[dict] = []
    actions_log: list[dict] = []
    n = len(strokes)
    for idx in range(n):
        st = strokes[idx]
        klass = str(st.get("class", "flat"))
        if klass not in stats:
            stats[klass] = {name: {"reward_sum": 0.0, "n": 0, "acc": 0} for name, _w, _a in ARMS}
        arm = _ucb_pick(stats[klass], int(sum(v["n"] for v in stats[klass].values())))
        wm, am = next((w, a) for name, w, a in ARMS if name == arm)
        pts = np.asarray(st["points_px"], dtype=np.float64)
        if len(pts) < 2:
            continue
        widths = np.asarray(st["widths_px"], dtype=np.float64).reshape(-1) * wm
        if len(widths) == 1:
            widths = np.full(len(pts), widths[0])
        if len(widths) != len(pts):
            widths = np.interp(np.linspace(0, len(widths) - 1, len(pts)), np.arange(len(widths)), widths)
        base_alpha = float(np.clip(float(st["alpha"]) * am, 0.03, 1.0))
        base_color = np.asarray(st["color"], dtype=np.float64).reshape(3)
        shape = str(st.get("shape") or "round")
        rmax = float(np.max(widths)) * 0.5 * 1.2 + 5.0
        x0d = max(0.0, float(pts[:, 0].min() - rmax))
        x1d = float(pts[:, 0].max() + rmax)
        y0d = max(0.0, float(pts[:, 1].min() - rmax))
        y1d = float(pts[:, 1].max() + rmax)
        x0s, x1s = max(0, int(math.floor(x0d * S))), min(fc.Ws, int(math.ceil(x1d * S)) + 1)
        y0s, y1s = max(0, int(math.floor(y0d * S))), min(fc.Hs, int(math.ceil(y1d * S)) + 1)
        if x1s <= x0s or y1s <= y0s:
            continue
        cands = [dict(dx=0.0, dy=0.0, wm=1.0, am=1.0, dc=np.zeros(3))]
        for _k in range(int(max(0, n_candidates))):
            cands.append(
                dict(
                    dx=float(rng.uniform(-4.0, 4.0)),
                    dy=float(rng.uniform(-4.0, 4.0)),
                    wm=float(rng.uniform(0.8, 1.2)),
                    am=float(rng.uniform(0.85, 1.15)),
                    dc=rng.uniform(-0.03, 0.03, 3),
                )
            )
        before = fc.rgb[y0s:y1s, x0s:x1s].copy()
        old_err = float(np.abs(tgt_ss[y0s:y1s, x0s:x1s] - before).sum())
        best = None
        for ci, cd in enumerate(cands):
            pts_c = (pts + np.array([cd["dx"], cd["dy"]])) * scale
            widths_c = np.maximum(1.0, widths * cd["wm"] * scale)
            radii_c = widths_c * 0.5
            alpha_c = float(np.clip(base_alpha * cd["am"], 0.02, 1.0))
            color_c = np.clip(base_color + cd["dc"], 0.0, 1.0)
            before_c = before.copy()
            fc.add_band(pts_c, radii_c, np.tile(color_c, (len(pts_c), 1)),
                        np.full(len(pts_c), alpha_c), mode="sweep", shapes=[shape] * len(pts_c))
            new_err = float(np.abs(tgt_ss[y0s:y1s, x0s:x1s] - fc.rgb[y0s:y1s, x0s:x1s]).sum())
            block = fc.rgb[y0s:y1s, x0s:x1s].copy()
            fc.rgb[y0s:y1s, x0s:x1s] = before_c
            if best is None or new_err < best["new_err"]:
                best = {"new_err": new_err, "block": block, "cd": cd, "alpha": alpha_c, "pts": pts_c, "widths": widths_c}
        delta = (best["new_err"] - old_err) / max(old_err, 1e-9)
        accept = delta < -min_improve
        if not accept and abs(delta) <= min_improve and rng.random() < 0.2:
            accept = True
        arm_st = stats[klass][arm]
        arm_st["reward_sum"] = float(arm_st["reward_sum"]) + max(0.0, -delta)
        arm_st["n"] = int(arm_st["n"]) + 1
        cls_st = per_class.setdefault(klass, {"n": 0, "accepted": 0, "delta_sum": 0.0, "segments": 0, "benefit_sum": 0.0})
        cls_st["n"] = int(cls_st["n"]) + 1
        if accept:
            fc.rgb[y0s:y1s, x0s:x1s] = best["block"]
            arm_st["acc"] = int(arm_st["acc"]) + 1
            cls_st["accepted"] = int(cls_st["accepted"]) + 1
            cls_st["delta_sum"] = float(cls_st["delta_sum"]) + delta
            benefit = float(old_err - best["new_err"])
            cls_st["benefit_sum"] = float(cls_st["benefit_sum"]) + benefit
            if klass.startswith("line"):
                cls_st["segments"] = int(cls_st["segments"]) + 1
            out = dict(st)
            out["points_px"] = best["pts"] / scale if scale != 1.0 else best["pts"]
            out["widths_px"] = best["widths"] / scale if scale != 1.0 else best["widths"]
            out["alpha"] = best["alpha"]
            out["color"] = np.clip(base_color + best["cd"]["dc"], 0.0, 1.0)
            out["arm"] = arm
            out["benefit"] = benefit
            out["action"] = {"dx": best["cd"]["dx"], "dy": best["cd"]["dy"], "wm": best["cd"]["wm"], "am": best["cd"]["am"]}
            accepted.append(out)
            actions_log.append({"class": klass, **out["action"]})
        if (idx + 1) % 250 == 0 or idx == n - 1:
            dec = fc.to_float()
            mae = float(np.abs(target_dec - dec).mean())
            actions_log.append({"progress": int(idx + 1), "mae": _r(mae), "accepted": len(accepted)})
    dec = fc.to_float()
    mse = float(np.mean((target_dec - dec) ** 2))
    psnr = 10.0 * math.log10(1.0 / max(mse, 1e-12))
    n_segments = sum(int(v["segments"]) for v in per_class.values())
    benefits = np.asarray([float(s.get("benefit", 0.0)) for s in accepted], dtype=np.float64)
    objective = float(psnr - w_strokes * (len(accepted) / 1000.0) - w_segments * (n_segments / 100.0))
    summary = {
        "strokes_total": int(n),
        "strokes_accepted": int(len(accepted)),
        "accept_rate": _r(len(accepted) / max(n, 1)),
        "segments_accepted": int(n_segments),
        "working_res": int(max(H, W)),
        "canvas_psnr_db": _r(psnr),
        "objective_J": _r(objective),
        "weights": {"w_strokes": w_strokes, "w_segments": w_segments,
                    "min_improve": min_improve, "n_candidates": int(n_candidates)},
        "classes": per_class,
        "arms": {
            klass: {arm: {"n": int(st["n"]), "acc": int(st["acc"]),
                          "mean_reward": _r(st["reward_sum"] / max(st["n"], 1))}
                     for arm, st in arms.items()}
            for klass, arms in stats.items()
        },
        "benefit": {
            "mean": _r(benefits.mean()) if benefits.size else 0.0,
            "p10": _r(np.percentile(benefits, 10)) if benefits.size else 0.0,
            "p50": _r(np.percentile(benefits, 50)) if benefits.size else 0.0,
            "p90": _r(np.percentile(benefits, 90)) if benefits.size else 0.0,
            "sum": _r(benefits.sum()) if benefits.size else 0.0,
        },
        "curve": [c for c in actions_log if "progress" in c],
    }
    if log_path:
        os.makedirs(os.path.dirname(os.path.abspath(log_path)) or ".", exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as fh:
            json.dump({**summary, "actions": [a for a in actions_log if "progress" not in a]}, fh, ensure_ascii=False, indent=1)
        md = os.path.splitext(log_path)[0] + ".md"
        lines = [
            "# 在线笔触 RL（真渲染器 + 连续动作搜索）训练日志\n",
            f"- 规划 {n} 笔，接受 **{len(accepted)}**（{summary['accept_rate'] * 100:.1f}%），线条段 {n_segments}\n",
            f"- 决策画布 PSNR **{psnr:.2f} dB**，目标函数 J = **{objective:.3f}**"
            f"（w_strokes={w_strokes}, w_segments={w_segments}）\n",
            f"- 每笔收益：p10={summary['benefit']['p10']} p50={summary['benefit']['p50']} p90={summary['benefit']['p90']}\n",
            "\n## 各结构类\n\n| 结构类 | 笔数 | 接受 | 平均ΔE | 线条段 |\n|---|---|---|---|---|\n",
        ]
        for klass, cs in per_class.items():
            lines.append(f"| {klass} | {cs['n']} | {cs['accepted']} | "
                         f"{cs['delta_sum'] / max(cs['n'], 1):+.4f} | {cs['segments']} |\n")
        lines.append("\n## 误差曲线（每 250 笔）\n\n| 笔数 | 平均绝对误差 | 已接受 |\n|---|---|---|\n")
        for c in summary["curve"]:
            lines.append(f"| {c['progress']} | {c['mae']} | {c['accepted']} |\n")
        with open(md, "w", encoding="utf-8", newline="\n") as fh:
            fh.writelines(lines)
        summary["md"] = md
    return {"accepted": accepted, "stats": summary}

