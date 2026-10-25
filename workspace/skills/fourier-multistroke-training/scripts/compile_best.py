#!/usr/bin/env python
"""Compile best pure-stroke set to a capped v2 `.fbs` and high-res renders.

Uses the new high-precision caps: target_resolution=1536, epsilon_px=0.08,
N_max=16384, curve_tol=0.002, max_curves=40000. No region fill is emitted.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
# Resolve junctions/symlinks first: registered skills are reached through
# .dsh/skills/<skill> -> skills/<skill>, and walking up the junction path lands
# in .dsh/skills (which has no real fourier_prompt/ package) instead of the
# workspace root.  FOURIER_WS still overrides the auto-detection.
_REAL_HERE = os.path.dirname(os.path.realpath(__file__))


def _find_ws():
    env = os.environ.get("FOURIER_WS")
    if env:
        return os.path.abspath(env)
    cur = _REAL_HERE
    for _ in range(8):
        if os.path.exists(os.path.join(cur, "fourier-brushstroke", "scripts", "fbs.py")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.abspath(os.path.join(_REAL_HERE, "..", ".."))

WS = _find_ws()
SKILL = os.path.join(WS, "fourier-brushstroke", "scripts")
FP = os.path.join(WS, "fourier-prompt")
for p in (HERE, SKILL, FP):
    if p not in sys.path:
        sys.path.insert(0, p)

from fbs_core import io_format, preprocess, structdoc  # noqa: E402
from fbs_core.config import FBSConfig  # noqa: E402
from fbs_core.util import WarningLog  # noqa: E402
from fourier_prompt import render_fast  # noqa: E402
import hatch_gen as hgr  # noqa: E402


LINE_BRUSH_KINDS = {"detail", "outline", "faceink", "boundary", "oil_edge",
                    "oil_dark_detail", "edge_detail", "line_detail",
                    "structure_detail", "contour_detail"}
SOFT_BRUSH_KINDS = {"soft_atmosphere", "soft_glaze", "gradient_swath",
                    "flat_fill", "flat_overlay", "ground", "highlight", "blush"}
HAIR_BRUSH_KINDS = {"hairflow", "oil_streamline", "precision_repair",
                    "ssim_repair"}


def brush_for_kind(kind: str, mixed: bool) -> tuple[str, str]:
    """Choose a plausible brush family per stroke role (mixed mode)."""
    if not mixed:
        return "basic_round_hard", "round"
    if kind in LINE_BRUSH_KINDS:
        return "ink_gpen", "ink"
    if kind in HAIR_BRUSH_KINDS:
        return "ink_fineliner", "round"
    if kind in SOFT_BRUSH_KINDS:
        return "basic_round_soft", "soft"
    return "basic_round_hard", "round"


def load_strokes(path: str, long_side: int, mixed_brushes: bool = False) -> list[dict]:
    data = json.load(open(path, encoding="utf-8"))
    out = []
    for s in data:
        pts = np.asarray(s["points"], dtype=np.float64)
        widths = np.asarray(s["widths"], dtype=np.float64)
        if pts.ndim != 2 or len(pts) < 2:
            continue
        closed = bool(s.get("closed", False))
        if closed:
            pts = np.vstack([pts, pts[:1]])
            widths = np.concatenate([widths, widths[:1]])
        # Two-color endpoint gradient -> per-point linear ramp.  fbs brush
        # color curves already support this and collapse a linear ramp to two
        # stops, so the document stays compact.
        grad = s.get("gradient") or s.get("color_gradient")
        if grad is None and "color_start" in s and "color_end" in s:
            grad = {"start": s["color_start"], "end": s["color_end"]}
        if grad is not None:
            c0 = np.clip(np.asarray(grad["start"], dtype=np.float64).reshape(3), 0, 1)
            c1 = np.clip(np.asarray(grad["end"], dtype=np.float64).reshape(3), 0, 1)
            colors = np.stack([np.linspace(c0[ch], c1[ch], len(pts)) for ch in range(3)], axis=1)
            color = c0
        else:
            color = np.clip(np.asarray(s["color"], dtype=np.float64).reshape(3), 0, 1)
            colors = np.tile(color, (len(pts), 1))
        kind = str(s.get("kind", "stroke"))
        preset, shape = brush_for_kind(kind, mixed_brushes)
        out.append({
            "points_px": pts * float(long_side),
            "widths_px": np.maximum(widths * float(long_side), 0.45),
            "color": color,
            "colors": colors,
            "gradient": grad,
            "alpha": float(s.get("alpha", 1.0)),
            "layer": int(s.get("layer", 1)),
            "preset": preset,
            "shape": shape,
            "class": kind,
        })
    # draw base first, then texture, then lines/touch-ups; stable within layer
    out.sort(key=lambda s: int(s["layer"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="training run dir containing best_strokes.json")
    ap.add_argument("--strokes", default=None, help="override strokes JSON path")
    ap.add_argument("--input", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--resolution", type=int, default=1536, help="Fourier contract R (cap)")
    ap.add_argument("--render", default="2048,4096", help="comma separated render resolutions")
    ap.add_argument("--nmax", type=int, default=16384, help="per-stroke N_max cap")
    ap.add_argument("--epsilon", type=float, default=0.08)
    ap.add_argument("--max-curves", type=int, default=100000, help="max stroke count (4GiB mode)")
    ap.add_argument("--mixed-brushes", action="store_true",
                    help="use ink/soft/round brush families per stroke kind")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    best_strokes = args.strokes or os.path.join(args.run, "best_strokes.json")
    if not os.path.exists(best_strokes):
        raise SystemExit("missing best_strokes.json (train at least one round)")
    state = json.load(open(os.path.join(args.run, "state.json"), encoding="utf-8"))
    cfg = FBSConfig(
        target_resolution=int(args.resolution),
        epsilon_px=float(args.epsilon),
        N_max=int(args.nmax),
        resample_points=4096,
        curve_tol=0.002,
        max_curves=int(args.max_curves),
        supersample=2,
    )
    warn = WarningLog()
    img = preprocess.load_and_prepare(args.input, cfg, warn)
    L = int(img["long_side"])
    strokes = load_strokes(best_strokes, L, mixed_brushes=args.mixed_brushes)
    print(f"[compile] strokes={len(strokes)} L={L} contract R={cfg.target_resolution} eps={cfg.epsilon_px}")
    t0 = time.time()
    doc = structdoc.build_doc_from_strokes(strokes, img, args.input, cfg, warn,
                                           method="pure_stroke_hatching_training")
    stem = os.path.splitext(os.path.basename(args.input))[0]
    fbs_path = os.path.join(args.outdir, f"{stem}_purestroke_R{cfg.target_resolution}.fbs.json")
    io_format.write_fbs(doc, fbs_path)
    build_s = time.time() - t0
    fills = int(doc.get("stats", {}).get("fill_count", -1))
    print(f"[compile] fbs={fbs_path} ({os.path.getsize(fbs_path)/1e6:.1f} MB) "
          f"strokes={len(doc.get('strokes', []))} fills={fills} build={build_s:.1f}s")
    if fills != 0:
        raise SystemExit(f"contract violation: fill_count={fills}")
    ref_full = Image.open(args.input).convert("RGB")
    renders = []
    for rs in [int(x) for x in str(args.render).split(",") if x.strip()]:
        t1 = time.time()
        rgb, info = render_fast.render_with_info(doc, resolution=rs, supersample=1, width_scale=1.0)
        dt = time.time() - t1
        out_png = os.path.join(args.outdir, f"{stem}_purestroke_R{rs}.png")
        Image.fromarray(rgb).save(out_png)
        # Match the actual rendered aspect.  Do not assume long side = width:
        # portrait canvases render as (H > W), and a width-derived reference
        # resize would be transposed relative to rgb.
        ref = np.asarray(ref_full.resize((int(rgb.shape[1]), int(rgb.shape[0])),
                                        Image.LANCZOS), dtype=np.float64) / 255.0
        rec = np.asarray(rgb, dtype=np.float64) / 255.0
        psnr = hgr._psnr(ref, rec)
        ssim = hgr._ssim(ref, rec)
        renders.append({"resolution": rs, "png": out_png, "psnr": round(psnr, 4),
                        "ssim": round(ssim, 5), "seconds": round(dt, 1),
                        "fast_info": {k: info.get(k) for k in
                                      ("quality", "fallback_reason", "strokes", "fills", "size")}})
        print(f"[compile] render R={rs}: PSNR={psnr:.3f} SSIM={ssim:.4f} ({dt:.1f}s)")
    report = {
        "schema": "pure-stroke-training/1",
        "input": os.path.abspath(args.input),
        "run": os.path.abspath(args.run),
        "contract": {"target_resolution": cfg.target_resolution, "epsilon_px": cfg.epsilon_px,
                     "N_max": cfg.N_max, "curve_tol": cfg.curve_tol, "max_curves": cfg.max_curves},
        "best": {k: state.get(k) for k in ("round", "best_J", "best_psnr", "best_ssim",
                                            "best_strokes", "best_params")},
        "fbs": {"path": fbs_path, "bytes": os.path.getsize(fbs_path),
                "stroke_count": len(doc.get("strokes", [])), "fill_count": fills,
                "coef_count": int(doc.get("stats", {}).get("coef_count", 0))},
        "renders": renders, "warnings": doc.get("warnings", [])[:40],
        "build_seconds": round(build_s, 1),
    }
    with open(os.path.join(args.outdir, "compile_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    print(json.dumps({"fbs_bytes": report["fbs"]["bytes"], "renders": renders},
                     ensure_ascii=False)[:1500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
