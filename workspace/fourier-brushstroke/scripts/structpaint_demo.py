#!/usr/bin/env python
"""结构感知笔触绘画：局部结构实验 → UCB 选笔 → 完整作画。

用法：
  python scripts/structpaint_demo.py --input assets/out/user_chibi/chibi_whale.png \
      --out assets/out/user_chibi/structpaint1 --resolution 1024

流程（对应"局部实验 → 学习 → 完整图"）：
1. 结构分解（调色板/中轴）→ 块面/线条按结构分级（flat/shade/detail/line_fine/line_thick）；
2. 每类取局部 patch，对候选笔刷策略（预设+宽度/长度）逐一生效并评分，写 experiments/*.json/.md；
3. UCB 多臂老虎机从记录中选每类最优策略；
4. 按"先铺大块 → 中景 → 线条/细节"的顺序生成纯笔触，构建 .fbs、渲染、出图。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
from PIL import Image

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fbs_core import brushlib, coverage, decompose, metrics as metrics_mod, preprocess  # noqa: E402
from fbs_core import render as render_mod  # noqa: E402
from fbs_core import stroke_rl, stroke_rl_render, structdoc, structpaint  # noqa: E402
from fbs_core.config import FBSConfig  # noqa: E402
from fbs_core.util import WarningLog  # noqa: E402

_CLASS_COLORS = {
    "flat": (80, 120, 255),
    "shade": (170, 90, 230),
    "detail": (255, 160, 40),
    "line_fine": (230, 40, 40),
    "line_thick": (40, 160, 90),
}


def _save_png(rgb: np.ndarray, path: str) -> None:
    """兼容 render() 返回的 uint8 与内部 float 0..1 两种口径。"""
    if rgb.dtype == np.uint8:
        Image.fromarray(rgb).save(path)
        return
    arr = np.clip(rgb * 255.0 + 0.5, 0, 255).astype(np.uint8)
    Image.fromarray(arr).save(path)


def _structure_map(structure: dict, out_path: str) -> None:
    rgb = structure["rgb"].copy()
    for s in structure["structures"]:
        col = np.asarray(_CLASS_COLORS.get(s["class"], (255, 255, 255)), dtype=np.float64) / 255.0
        if s["kind"] == "region":
            rgb[s["ys"], s["xs"]] = 0.55 * rgb[s["ys"], s["xs"]] + 0.45 * col
        else:
            ys = np.clip((np.asarray(s["points_world"])[:, 1] * structure["L"]).astype(int), 0, structure["H"] - 1)
            xs = np.clip((np.asarray(s["points_world"])[:, 0] * structure["L"]).astype(int), 0, structure["W"] - 1)
            rgb[ys, xs] = col
    _save_png(rgb, out_path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="结构感知笔触绘画（局部实验 + UCB + 完整作画）")
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--resolution", type=int, default=None, help="默认 1024")
    ap.add_argument("--patch-px", type=int, default=None, help="默认 110")
    ap.add_argument("--patches-per-class", type=int, default=None, help="默认 3")
    ap.add_argument("--no-experiments", action="store_true", help="跳过实验，直接用默认候选第一条")
    ap.add_argument("--segmentation", choices=["palette","contour"], default=None, help="分区方式：contour=线稿封闭轮廓（默认）| palette=调色板连通块")
    ap.add_argument("--rl-min-improve", dest="rl_min_improve", type=float, default=None,
                    help="RL 接受阈值：局部相对误差改进需超过该值（默认 2e-3；调大=更少但更有效的笔）")
    ap.add_argument("--w-strokes", dest="w_strokes", type=float, default=None,
                    help="次评分：笔触数量惩罚权重（默认 0.02，低权重）")
    ap.add_argument("--w-segments", dest="w_segments", type=float, default=None,
                    help="次评分：连续线段数量惩罚权重（默认 0.01，低权重）")
    args = ap.parse_args(argv)
    if args.resolution is None:
        args.resolution = 1024
    if args.patch_px is None:
        args.patch_px = 110
    if args.patches_per_class is None:
        args.patches_per_class = 3
    if args.segmentation is None:
        args.segmentation = "contour"

    outdir = os.path.abspath(args.out)
    os.makedirs(outdir, exist_ok=True)
    warn = WarningLog()
    cfg = FBSConfig(target_resolution=int(args.resolution))
    t_all = time.perf_counter()

    # 1) 载入 + 结构分解
    img = preprocess.load_and_prepare(args.input, cfg, warn)
    cls = preprocess.classify(img, cfg)
    analysis = coverage.analyze(img, cfg, warn, policy_klass=str(cls.get("klass") or ""))
    analysis["klass"] = str(cls.get("klass") or analysis.get("klass"))
    dec = decompose.decompose(img, analysis, cfg, warn)
    structure = structpaint.build_structure(img, analysis, dec, cfg, warn, segmentation=str(args.segmentation))
    _structure_map(structure, os.path.join(outdir, "structure_map.png"))
    print(f"[structpaint] 结构：{structure['counts']}  ({time.perf_counter() - t_all:.1f}s)")

    # 2) 局部策略实验（patch 级渲染评分）
    exp_dir = os.path.join(outdir, "experiments")
    os.makedirs(exp_dir, exist_ok=True)
    exp_res = int(args.resolution)  # patch 实验与成图同分辨率，便于直接对比
    scale = 1.0

    def render_patch(klass: str, policy: dict, patch: tuple[int, int, int, int]):
        """把 patch 内该结构类的元素用给定策略重绘，返回 (patch 像素 0..1, 该类覆盖 mask)。"""
        px0, py0, px1, py1 = patch
        strokes: list[dict] = []
        mask = np.zeros((py1 - py0, px1 - px0), dtype=bool)
        for s_ in structure["structures"]:
            if s_["class"] != klass:
                continue
            x0, y0, x1, y1 = s_["bbox_px"]
            if x1 < px0 - 8 or x0 > px1 + 8 or y1 < py0 - 8 or y0 > py1 + 8:
                continue
            if s_["kind"] == "region":
                strokes.extend(structpaint._fill_strokes_for_region(s_, structure["rgb"], structure["L"], policy))
                geom = s_.get("geom")
                if isinstance(geom, dict):
                    gx0, gy0 = geom["bbox"][0], geom["bbox"][1]
                    gm = np.asarray(geom["mask"], dtype=bool)
                    ys, xs = np.nonzero(gm)
                    gxs = xs + gx0 - px0
                    gys = ys + gy0 - py0
                    ok = (gxs >= 0) & (gxs < mask.shape[1]) & (gys >= 0) & (gys < mask.shape[0])
                    mask[gys[ok], gxs[ok]] = True
            else:
                strokes.extend(structpaint._line_strokes_for_struct(s_, structure["L"], policy))
                pts = np.asarray(s_["points_world"], dtype=np.float64) * structure["L"]
                r = int(max(2.0, float(np.median(s_["widths_world"]) * structure["L"]) + 1.5))
                for (xx, yy) in pts:
                    ix, iy = int(xx) - px0, int(yy) - py0
                    if -r <= ix < mask.shape[1] + r and -r <= iy < mask.shape[0] + r:
                        x_lo, x_hi = max(0, ix - r), min(mask.shape[1], ix + r + 1)
                        y_lo, y_hi = max(0, iy - r), min(mask.shape[0], iy + r + 1)
                        if x_hi > x_lo and y_hi > y_lo:
                            mask[y_lo:y_hi, x_lo:x_hi] = True
        if not strokes:
            return None
        for st in strokes:
            st["shape"] = brushlib.BRUSHES[str(st["preset"])]["shape"]
        doc = structdoc.build_doc_from_strokes(strokes, img, args.input, cfg, WarningLog(),
                                               method="structure_policy_patch")
        rgb_u8 = render_mod.render(doc, resolution=int(args.resolution), supersample=2, mode="sweep",
                                   warn=WarningLog())
        rgb = rgb_u8.astype(np.float64) / 255.0
        return rgb[py0:py1, px0:px1], mask

    if args.no_experiments:
        learned = {
            klass: {
                f"{POL[0]['preset']}|w{POL[0]['width_scale']}|l{POL[0]['length_scale']}":
                {"mean_psnr": 0.0, "mean_ssim": 0.0, "reward": 1.0, "n": 1}
            }
            for klass, POL in structpaint.POLICY_CANDIDATES.items()
        }
        exp = {"learned": learned, "experiments": {}}
        print("[structpaint] 跳过实验，使用默认策略")
    else:
        t0 = time.perf_counter()
        exp = structpaint.run_policy_experiments(
            structure, render_patch,
            log_path=os.path.join(exp_dir, "brush_policy_experiments.json"),
            patches_per_class=int(args.patches_per_class),
            patch_px=int(args.patch_px),
        )
        print(f"[structpaint] 局部实验完成 ({time.perf_counter() - t0:.1f}s)：{exp['md']}")

    policies = structpaint.policies_from_learned(exp["learned"])
    print("[structpaint] UCB 选定策略：")
    for klass, pol in policies.items():
        print(f"    {klass:10s} → {pol['key']}")

    # 3) 完整作画：结构化笔触 → 在线 stroke RL（逐笔接受/撤回 + 力度臂学习）→ 构建文档
    t0 = time.perf_counter()
    strokes = structpaint.paint_structure(structure, policies)
    for st in strokes:
        st["shape"] = brushlib.BRUSHES[str(st["preset"])]["shape"]
    rl = stroke_rl_render.run_stroke_rl_render(
        structure["rgb"], strokes,
        log_path=os.path.join(exp_dir, "stroke_rl_log.json"),
        working_res=min(int(args.resolution), 1024),
        rng=np.random.default_rng(20261018),
        min_improve=float(args.rl_min_improve if args.rl_min_improve is not None else 2.0e-3),
        w_strokes=float(args.w_strokes if args.w_strokes is not None else 0.02),
        w_segments=float(args.w_segments if args.w_segments is not None else 0.01),
    )
    accepted = rl["accepted"]
    print(f"[structpaint] 在线 RL（真渲染器）：{rl['stats']['strokes_total']} 笔 → 接受 {len(accepted)}"
          f"（接受率 {rl['stats']['accept_rate'] * 100:.1f}%），"
          f"决策画布 PSNR {rl['stats']['canvas_psnr_db']:.2f}dB，J={rl['stats']['objective_J']:.3f}")
    counts: dict[str, int] = {}
    for st in accepted:
        key = f"{st['class']}/{st['preset']}"
        counts[key] = counts.get(key, 0) + 1
    doc = structdoc.build_doc_from_strokes(accepted, img, args.input, cfg, warn)
    print(f"[structpaint] 接受 {len(accepted)} 笔，Fourier 拟合完成 ({time.perf_counter() - t0:.1f}s)")

    t0 = time.perf_counter()
    rgb_u8 = render_mod.render(doc, resolution=int(args.resolution), supersample=2, mode="sweep", warn=warn)
    print(f"[structpaint] 渲染 ({time.perf_counter() - t0:.1f}s)")
    stem = os.path.splitext(os.path.basename(args.input))[0]
    png = os.path.join(outdir, f"{stem}_R{int(args.resolution)}.png")
    _save_png(rgb_u8, png)
    rgb = rgb_u8.astype(np.float64) / 255.0
    # 参考图同分辨率比较
    ref = np.asarray(Image.open(args.input).convert("RGB").resize(
        (rgb.shape[1], rgb.shape[0]), Image.LANCZOS), dtype=np.float64) / 255.0
    met = metrics_mod.compare(rgb, ref, doc=doc, bg=np.array([1.0, 1.0, 1.0]))
    summary = {
        "input": args.input,
        "resolution": int(args.resolution),
        "strokes_planned": len(strokes),
        "strokes": len(accepted),
        "fills": sum(1 for s in doc["strokes"] if s.get("fill_role") == "region_fill"),
        "stroke_breakdown": counts,
        "policies": policies,
        "quality": {k: met.get(k) for k in ("psnr", "ssim", "mae", "rmse")},
        "experiments": {"json": exp.get("json"), "md": exp.get("md")},
        "stroke_rl": rl["stats"],
        "structure_counts": structure["counts"],
        "timings_s": {"total": round(time.perf_counter() - t_all, 2)},
    }
    with open(os.path.join(outdir, "structpaint_report.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)
    from fbs_core import io_format  # noqa: E402

    io_format.write_fbs(doc, os.path.join(outdir, f"{stem}.fbs.json"))
    print(f"[structpaint] 完成：{png}  PSNR {met.get('psnr', 0):.3f} / SSIM {met.get('ssim', 0):.4f}  "
          f"({time.perf_counter() - t_all:.1f}s)")
    for w in warn.to_list():
        print(f"  ! {w['code']}: {w['message']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
