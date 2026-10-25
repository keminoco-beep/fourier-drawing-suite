#!/usr/bin/env python
"""变体评估脚本：统一口径计算改进方案指标（PSNR/SSIM/边缘锐度/近边误差/笔宽/alpha + 三联图）。

用法：
  python scripts/eval_variant.py --fbs <doc.fbs.json> --input <原图> --out <outdir> [--tag baseline]
可选 --strokes <best_strokes.json>（只有笔触、无 doc 时先用 trainloop 同款构建文档再渲染）。
产物：<outdir>/<tag>_R2048.png、metrics.json、report.md、crops.png
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fbs_core import config, io_format, metrics, preprocess, render, structdoc  # noqa: E402
from fbs_core.util import WarningLog  # noqa: E402


def _edge_metrics(cur: np.ndarray, ref: np.ndarray) -> dict:
    from scipy import ndimage as ndi

    g = ref.mean(axis=2)
    gy, gx = np.gradient(g)
    gmag = np.hypot(gx, gy)
    edge = gmag > np.percentile(gmag, 95)
    dist = ndi.distance_transform_edt(~edge)
    gg = cur.mean(axis=2)
    cy, cx = np.gradient(gg)
    cmag = np.hypot(cx, cy)
    err = np.abs(cur - ref).mean(axis=2)
    lap = np.abs(4 * gg[1:-1, 1:-1] - gg[:-2, 1:-1] - gg[2:, 1:-1] - gg[1:-1, :-2] - gg[1:-1, 2:])
    gr = ref.mean(axis=2)
    lap_ref = np.abs(4 * gr[1:-1, 1:-1] - gr[:-2, 1:-1] - gr[2:, 1:-1] - gr[1:-1, :-2] - gr[1:-1, 2:])
    return {
        "edge_sharp_ratio": float(cmag[edge].mean() / max(gmag[edge].mean(), 1e-9)),
        "err_near_edge_frac": float(err[dist <= 3].sum() / max(err.sum(), 1e-9)),
        "err_near_mean": float(err[dist <= 3].mean()),
        "err_far_mean": float(err[dist > 20].mean()),
        "lap_var_ratio": float(lap.var() / max(lap_ref.var(), 1e-9)),
    }


def _stroke_stats(doc: dict) -> dict:
    styles = doc.get("brush_styles") or []
    widths, alphas = [], []
    for st in doc.get("strokes") or []:
        br = st.get("brush") or {}
        wc = br.get("width") or {}
        keys = wc.get("keys") or []
        vals = [float(kv[1]) for kv in keys if isinstance(kv, (list, tuple)) and len(kv) >= 2]
        if vals:
            widths.append(float(np.median(vals)) * 2048.0)
        a = 1.0
        si = br.get("style", 0)
        if 0 <= si < len(styles):
            aa = (styles[si] or {}).get("alpha") or {}
            kk = aa.get("keys") or []
            if kk:
                a = float(kk[0][1])
        alphas.append(a)
    w = np.asarray(widths, dtype=np.float64)
    a = np.asarray(alphas, dtype=np.float64)
    if w.size == 0:
        return {}
    return {
        "width_median_px": float(np.median(w)),
        "width_p10_px": float(np.percentile(w, 10)),
        "width_p90_px": float(np.percentile(w, 90)),
        "frac_width_gt_30px": float(np.mean(w > 30.0)),
        "alpha_lt_099_frac": float(np.mean(a < 0.99)),
        "alpha_lt_08_frac": float(np.mean(a < 0.8)),
    }


def _crops(ref: np.ndarray, cur: np.ndarray, out_png: str) -> None:
    h, w = cur.shape[:2]
    spots = [(int(w * 0.22), int(h * 0.28), 520), (int(w * 0.38), int(h * 0.48), 520),
             (int(w * 0.50), int(h * 0.72), 520)]
    panels = []
    for (x, y, s) in spots:
        x0 = max(0, min(w - s, x - s // 2))
        y0 = max(0, min(h - s, y - s // 2))
        for img in (ref, cur):
            crop = img[y0:y0 + s, x0:x0 + s]
            panels.append(Image.fromarray(np.clip(crop * 255, 0, 255).astype(np.uint8)))
    cols = 2
    rows = len(panels) // cols
    cw, ch = panels[0].size
    fig = Image.new("RGB", (cols * cw + 10 * (cols + 1), rows * ch + 10 * (rows + 1)), "white")
    for i, p in enumerate(panels):
        r, c = divmod(i, cols)
        fig.paste(p, (10 + c * (cw + 10), 10 + r * (ch + 10)))
    fig.save(out_png)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="变体评估（统一口径）")
    ap.add_argument("--fbs", default=None)
    ap.add_argument("--strokes", default=None, help="没有 fbs 时：best_strokes.json（按 2048 契约构建文档）")
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--tag", default=None)
    ap.add_argument("--resolution", type=int, default=None)
    args = ap.parse_args(argv)
    args.tag = str(args.tag if args.tag is not None else "variant")
    args.resolution = int(args.resolution if args.resolution is not None else 2048)
    os.makedirs(args.out, exist_ok=True)
    warn = WarningLog()
    if args.fbs:
        doc = io_format.read_fbs(args.fbs)
    elif args.strokes:
        strokes = json.load(open(args.strokes, encoding="utf-8"))
        img = preprocess.load_and_prepare(args.input, config.FBSConfig(target_resolution=421), warn)
        doc = structdoc.build_doc_from_strokes(strokes, img, args.input,
                                               config.FBSConfig(target_resolution=args.resolution),
                                               warn, method="eval_variant")
    else:
        print("需要 --fbs 或 --strokes")
        return 2
    rgb = render.render(doc, resolution=args.resolution, supersample=1, mode="sweep", warn=warn)
    out_png = os.path.join(args.out, f"{args.tag}_R{args.resolution}.png")
    Image.fromarray(rgb).save(out_png)
    ref = np.asarray(Image.open(args.input).convert("RGB").resize(
        (rgb.shape[1], rgb.shape[0]), Image.LANCZOS), dtype=np.float64) / 255.0
    cur = rgb.astype(np.float64) / 255.0
    m = metrics.compare(cur, ref)
    out = {"tag": args.tag, "fbs": args.fbs, "strokes_file": args.strokes,
           "resolution": args.resolution, "png": out_png,
           "psnr": float(m["psnr"]), "ssim": float(m["ssim"]),
           "mae": float(m.get("mae", 0.0))}
    out.update(_edge_metrics(cur, ref))
    out.update(_stroke_stats(doc))
    out["doc_contract"] = doc.get("render_contract")
    out["class_counts"] = dict((k, 0) for k in ("flat", "shade", "detail", "line_fine", "line_thick"))
    for st in doc.get("strokes") or []:
        cls = str((st.get("brush") or {}).get("structure_class", ""))
        # structure_class 存在 meta 里（有的文档没有），退化用 shape 统计
        if cls in out["class_counts"]:
            out["class_counts"][cls] += 1
    with open(os.path.join(args.out, "metrics.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    _crops(ref, cur, os.path.join(args.out, "crops.png"))
    lines = [
        f"# 变体评估：{args.tag}\n",
        f"- 输入：{args.input}\n- 文档：{args.fbs or args.strokes}\n- 渲染：R={args.resolution} SS1\n",
        "\n## 指标\n\n| 指标 | 值 |\n|---|---|\n",
    ]
    for k in ("psnr", "ssim", "edge_sharp_ratio", "err_near_edge_frac", "err_near_mean",
              "err_far_mean", "lap_var_ratio", "width_median_px", "width_p10_px",
              "width_p90_px", "frac_width_gt_30px", "alpha_lt_099_frac"):
        if k in out:
            lines.append(f"| {k} | {out[k]:.4f} |\n")
    lines.append(f"\n- 文档契约：{out['doc_contract']}\n")
    with open(os.path.join(args.out, "report.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.writelines(lines)
    print(json.dumps({k: out[k] for k in out if k in (
        "psnr", "ssim", "edge_sharp_ratio", "err_near_edge_frac", "width_median_px",
        "frac_width_gt_30px", "alpha_lt_099_frac")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
