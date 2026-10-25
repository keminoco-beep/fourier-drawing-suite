#!/usr/bin/env python
"""Multi-round training loop for pure-stroke image-to-image reproduction.

Each round:
  1. mutate generation parameters from the best config (or explore fresh),
  2. generate base hatching + texture hatching + drawn detail lines,
  3. add error-guided corrective strokes (artist touch-up),
  4. render, score, optionally prune the texture layer if J improves,
  5. save a per-round sample PNG + metrics + state (resumable).

No region fill is ever generated. The objective balances fidelity and stroke
count:  J = PSNR + 1.5*SSIM - 0.10*(strokes/1000).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import sys
import time
import traceback
from dataclasses import asdict

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage as ndi

HERE = os.path.dirname(os.path.abspath(__file__))
HERE = os.path.dirname(os.path.abspath(__file__))
def _find_ws():
    env = os.environ.get("FOURIER_WS")
    if env:
        return os.path.abspath(env)
    cur = HERE
    for _ in range(8):
        if os.path.exists(os.path.join(cur, "fourier-brushstroke", "scripts", "fbs.py")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.abspath(os.path.join(HERE, "..", ".."))

WS = _find_ws()
if HERE not in sys.path:
    sys.path.insert(0, HERE)
if os.path.join(WS, "fourier-brushstroke", "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(WS, "fourier-brushstroke", "scripts"))

import hybrid_gen as hg  # noqa: E402
import hatch_gen as hgr  # noqa: E402

STOP_FILE = "STOP"


def safe_name(x: float) -> str:
    return f"{x:.3f}".replace("-", "m").replace(".", "p")


def params_to_json(p: hg.HParams) -> dict:
    return {k: (float(v) if isinstance(v, (np.floating, float)) else
                int(v) if isinstance(v, (np.integer, int)) else v)
            for k, v in asdict(p).items()}


def mutate(base: hg.HParams, rng: np.random.Generator, strength: float = 1.0) -> hg.HParams:
    d = asdict(base)
    keys_num = [
        "base_blur", "base_k", "base_min_area", "base_width_min", "base_width_max",
        "base_spacing_mul", "tex_blur", "tex_k", "tex_min_area", "tex_width_min",
        "tex_width_max", "tex_spacing_mul", "tex_alpha", "max_tex_strokes",
        "edge_percentile", "edge_width", "edge_dark", "max_edge_strokes",
        "color_jitter",
    ]
    n = int(rng.integers(1, 4))
    for key in rng.choice(keys_num, size=n, replace=False):
        v = float(d[key])
        if key in ("base_k", "tex_k", "max_tex_strokes", "max_edge_strokes",
                   "base_min_area", "tex_min_area"):
            v = int(round(v * float(rng.uniform(0.72, 1.28) ** strength)))
        elif key in ("edge_percentile",):
            v = v + float(rng.normal(0, 1.5)) * strength
        elif key == "edge_dark":
            v = v * float(rng.uniform(0.85, 1.10) ** strength)
        elif key in ("tex_alpha",):
            v = v * float(rng.uniform(0.85, 1.15) ** strength)
        else:
            v = v * float(rng.uniform(0.82, 1.18) ** strength)
        d[key] = v
    # clamp ranges
    d["base_k"] = int(np.clip(d["base_k"], 6, 16))
    d["tex_k"] = int(np.clip(d["tex_k"], 16, 48))
    d["base_min_area"] = int(np.clip(d["base_min_area"], 60, 400))
    d["tex_min_area"] = int(np.clip(d["tex_min_area"], 40, 300))
    d["base_blur"] = float(np.clip(d["base_blur"], 3.0, 10.0))
    d["tex_blur"] = float(np.clip(d["tex_blur"], 1.0, 4.0))
    d["base_width_min"] = float(np.clip(d["base_width_min"], 3.0, 10.0))
    d["base_width_max"] = float(np.clip(d["base_width_max"], d["base_width_min"] + 1.0, 22.0))
    d["tex_width_min"] = float(np.clip(d["tex_width_min"], 1.5, 5.0))
    d["tex_width_max"] = float(np.clip(d["tex_width_max"], d["tex_width_min"] + 0.5, 10.0))
    d["base_spacing_mul"] = float(np.clip(d["base_spacing_mul"], 0.36, 0.70))
    d["tex_spacing_mul"] = float(np.clip(d["tex_spacing_mul"], 0.65, 1.30))
    d["tex_alpha"] = float(np.clip(d["tex_alpha"], 0.45, 1.0))
    d["max_tex_strokes"] = int(np.clip(d["max_tex_strokes"], 800, 6000))
    d["edge_percentile"] = float(np.clip(d["edge_percentile"], 90.0, 97.5))
    d["edge_width"] = float(np.clip(d["edge_width"], 0.9, 2.6))
    d["edge_dark"] = float(np.clip(d["edge_dark"], 0.55, 1.05))
    d["max_edge_strokes"] = int(np.clip(d["max_edge_strokes"], 500, 2600))
    d["color_jitter"] = float(np.clip(d["color_jitter"], 0.0, 0.04))
    d["seed"] = int(rng.integers(0, 2**31 - 1))
    return hg.HParams(**d)


def fresh_params(rng: np.random.Generator, work_res: int, preset: str = "default") -> hg.HParams:
    if preset == "soft":
        # Atmospheric / hazy paintings: broad translucent masses, suppressed edges.
        return hg.HParams(
            work_res=work_res,
            base_blur=float(rng.uniform(6.5, 10.0)),
            base_k=int(rng.integers(6, 10)),
            base_min_area=int(rng.integers(120, 380)),
            base_width_min=float(rng.uniform(8.0, 14.0)),
            base_width_max=float(rng.uniform(18.0, 30.0)),
            base_spacing_mul=float(rng.uniform(0.50, 0.72)),
            base_alpha=0.92,
            tex_blur=float(rng.uniform(2.2, 4.0)),
            tex_k=int(rng.integers(14, 28)),
            tex_min_area=int(rng.integers(80, 260)),
            tex_width_min=float(rng.uniform(3.5, 6.0)),
            tex_width_max=float(rng.uniform(7.0, 12.0)),
            tex_spacing_mul=float(rng.uniform(0.85, 1.25)),
            tex_alpha=float(rng.uniform(0.40, 0.65)),
            max_tex_strokes=int(rng.integers(1200, 3000)),
            edge_percentile=float(rng.uniform(95.5, 98.5)),
            edge_width=float(rng.uniform(1.0, 1.8)),
            edge_dark=float(rng.uniform(0.75, 1.0)),
            max_edge_strokes=int(rng.integers(300, 900)),
            color_jitter=float(rng.uniform(0.0, 0.02)),
            seed=int(rng.integers(0, 2**31 - 1)),
        )
    if preset == "impasto":
        return hg.HParams(
            work_res=work_res,
            base_blur=float(rng.uniform(3.0, 6.0)),
            base_k=int(rng.integers(10, 18)),
            base_min_area=int(rng.integers(60, 220)),
            base_width_min=float(rng.uniform(4.0, 8.0)),
            base_width_max=float(rng.uniform(9.0, 18.0)),
            base_spacing_mul=float(rng.uniform(0.38, 0.55)),
            tex_blur=float(rng.uniform(1.0, 2.4)),
            tex_k=int(rng.integers(24, 48)),
            tex_min_area=int(rng.integers(50, 180)),
            tex_width_min=float(rng.uniform(2.5, 5.0)),
            tex_width_max=float(rng.uniform(6.0, 12.0)),
            tex_spacing_mul=float(rng.uniform(0.62, 0.95)),
            tex_alpha=float(rng.uniform(0.65, 0.95)),
            max_tex_strokes=int(rng.integers(2500, 6000)),
            edge_percentile=float(rng.uniform(90.0, 95.0)),
            edge_width=float(rng.uniform(1.2, 2.2)),
            edge_dark=float(rng.uniform(0.60, 0.95)),
            max_edge_strokes=int(rng.integers(1000, 2400)),
            color_jitter=float(rng.uniform(0.0, 0.025)),
            seed=int(rng.integers(0, 2**31 - 1)),
        )
    return hg.HParams(
        work_res=work_res,
        base_blur=float(rng.uniform(4.0, 9.0)),
        base_k=int(rng.integers(7, 15)),
        base_min_area=int(rng.integers(80, 320)),
        base_width_min=float(rng.uniform(4.0, 9.0)),
        base_width_max=float(rng.uniform(10.0, 20.0)),
        base_spacing_mul=float(rng.uniform(0.40, 0.64)),
        tex_blur=float(rng.uniform(1.4, 3.2)),
        tex_k=int(rng.integers(20, 44)),
        tex_min_area=int(rng.integers(60, 220)),
        tex_width_min=float(rng.uniform(1.8, 3.5)),
        tex_width_max=float(rng.uniform(4.0, 8.0)),
        tex_spacing_mul=float(rng.uniform(0.72, 1.18)),
        tex_alpha=float(rng.uniform(0.55, 0.95)),
        max_tex_strokes=int(rng.integers(1500, 5000)),
        edge_percentile=float(rng.uniform(92.0, 96.5)),
        edge_width=float(rng.uniform(1.1, 2.2)),
        edge_dark=float(rng.uniform(0.65, 1.0)),
        max_edge_strokes=int(rng.integers(800, 2000)),
        color_jitter=float(rng.uniform(0.0, 0.03)),
        seed=int(rng.integers(0, 2**31 - 1)),
    )


def fix_strokes(rgb: np.ndarray, img: np.ndarray, strokes: list[dict], rng,
                passes=(95, 97), min_area=28, width=2.8, spacing=2.1,
                max_per_pass=260) -> list[dict]:
    H, W = rgb.shape[:2]
    L = float(max(H, W))
    jxx, jyy, jxy = hg.orientation_field(rgb)
    add = []
    cur = img.copy()
    for pc in passes:
        err = np.abs(cur - rgb).mean(axis=2)
        thr = float(np.percentile(err, pc))
        mask = err > thr
        mask = ndi.binary_opening(mask, iterations=1)
        mask = ndi.binary_closing(mask, iterations=2)
        cc, n = ndi.label(mask, structure=np.ones((3, 3), dtype=np.int32))
        sizes = np.bincount(cc.ravel())
        for cid in range(1, n + 1):
            if sizes[cid] < min_area:
                continue
            cm = cc == cid
            ys, xs = np.nonzero(cm)
            if len(ys) == 0:
                continue
            x0, y0 = int(xs.min()), int(ys.min())
            x1, y1 = int(xs.max()) + 1, int(ys.max()) + 1
            ang = hg.region_orientation(cm, jxx, jyy, jxy)
            out = hg.scanline_hatch(cm, (x0, y0, x1, y1), ang, width, spacing,
                                    np.array([0.5, 0.5, 0.5]), (rgb, 0.012, 1.0),
                                    rng, max_strokes=max_per_pass)
            for st in out:
                st["kind"] = "fix"
                st["layer"] = 6
                st["points"] = st["points"] / L
                st["widths"] = st["widths"] / L
                add.append(st)
                if len(add) >= max_per_pass * len(passes):
                    break
        # provisional update so the next pass sees improvement
        if add:
            cur = hgr._render_strokes(strokes + add, H, W, ss=1)
    return add


def score_of(strokes: list[dict], rendered: np.ndarray, ref: np.ndarray,
             detail_penalty: bool = True, stroke_weight: float = 0.10,
             ssim_weight: float = 1.5) -> dict:
    psnr = hgr._psnr(ref, rendered)
    ssim = hgr._ssim(ref, rendered)
    n = len(strokes)
    lines = sum(1 for s in strokes if s.get("kind") in ("detail", "boundary"))
    J = psnr + ssim_weight * ssim - stroke_weight * (n / 1000.0)
    if detail_penalty and lines < 400:
        J -= 0.4
    return {"psnr": round(psnr, 4), "ssim": round(ssim, 5), "J": round(J, 4),
            "strokes": n, "lines": lines}


def json_strokes(strokes: list[dict]) -> list[dict]:
    out = []
    for s in strokes:
        d = dict(s)
        d["points"] = np.round(np.asarray(d["points"], dtype=np.float64), 6).tolist()
        d["widths"] = np.round(np.asarray(d["widths"], dtype=np.float64), 6).tolist()
        d["color"] = np.round(np.asarray(d["color"], dtype=np.float64), 5).tolist()
        d["alpha"] = float(d.get("alpha", 1.0))
        d["closed"] = bool(d.get("closed", False))
        d["layer"] = int(d.get("layer", 0))
        d["kind"] = str(d.get("kind", ""))
        out.append(d)
    return out


def strokes_from_json(data: list[dict]) -> list[dict]:
    out = []
    for d in data:
        s = dict(d)
        s["points"] = np.asarray(s["points"], dtype=np.float64)
        s["widths"] = np.asarray(s["widths"], dtype=np.float64)
        s["color"] = np.asarray(s["color"], dtype=np.float64)
        out.append(s)
    return out


def save_round_image(path: str, rgb: np.ndarray, rendered: np.ndarray, title: str) -> None:
    # side-by-side reference / result for quick visual review
    H, W = rgb.shape[:2]
    gap = 8
    sheet = np.ones((H, W * 2 + gap, 3), dtype=np.float32)
    sheet[:, :W] = rgb
    sheet[:, W + gap:] = rendered
    im = Image.fromarray(np.clip(sheet * 255, 0, 255).astype(np.uint8))
    draw = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("arial.ttf", max(14, W // 40))
    except Exception:
        font = ImageFont.load_default()
    draw.text((12, 10), "reference", fill=(20, 20, 20), font=font)
    draw.text((W + gap + 12, 10), title, fill=(20, 20, 20), font=font)
    im.save(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--work-res", type=int, default=1024)
    ap.add_argument("--max-rounds", type=int, default=200)
    ap.add_argument("--minutes", type=float, default=0.0, help="0 = run until STOP/max-rounds")
    ap.add_argument("--seed", type=int, default=20261021)
    ap.add_argument("--fresh-every", type=int, default=7)
    ap.add_argument("--fix-passes", type=int, default=2)
    ap.add_argument("--save-every", type=int, default=1)
    ap.add_argument("--stroke-weight", type=float, default=0.10)
    ap.add_argument("--ssim-weight", type=float, default=1.5)
    ap.add_argument("--preset", choices=("default", "soft", "impasto"), default="default",
                    help="initial parameter family; training still mutates from it")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    rounds_dir = os.path.join(args.outdir, "rounds")
    os.makedirs(rounds_dir, exist_ok=True)
    state_path = os.path.join(args.outdir, "state.json")
    log_path = os.path.join(args.outdir, "training_log.jsonl")
    best_strokes_path = os.path.join(args.outdir, "best_strokes.json")
    best_png_path = os.path.join(args.outdir, "best_R%d.png" % args.work_res)

    rgb = hg.load_image(args.input, args.work_res)
    H, W = rgb.shape[:2]
    rng = np.random.default_rng(args.seed)
    state = {
        "round": 0, "best_J": -1e9, "best_psnr": -1e9, "best_ssim": 0.0,
        "best_strokes": 0, "best_params": None, "history": [],
        "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "work_res": args.work_res, "input": os.path.abspath(args.input),
        "preset": args.preset,
    }
    if os.path.exists(state_path):
        try:
            state = json.load(open(state_path, encoding="utf-8"))
            print(f"[train] resume round={state.get('round')} best_J={state.get('best_J'):.3f}")
        except Exception:
            print("[train] state unreadable, starting fresh")
    deadline = time.time() + args.minutes * 60.0 if args.minutes > 0 else None
    best_params = hg.HParams(**state["best_params"]) if state.get("best_params") else None

    while int(state["round"]) < args.max_rounds:
        if os.path.exists(os.path.join(args.outdir, STOP_FILE)):
            print("[train] STOP file detected")
            break
        if deadline is not None and time.time() >= deadline:
            print("[train] time budget reached")
            break
        r = int(state["round"]) + 1
        t0 = time.time()
        try:
            if best_params is None or (r % args.fresh_every == 0):
                params = fresh_params(rng, args.work_res, preset=args.preset)
                mode = "fresh"
            else:
                strength = 1.0 if r - int(state.get("best_round", 0)) < 8 else 1.5
                params = mutate(best_params, rng, strength=strength)
                mode = "mutate"
            strokes, gen_stats = hg.generate(rgb, params, rng)
            # no coarse region-boundary layer: actual ink lines are the detail layer
            strokes = [s for s in strokes if s.get("kind") in ("base", "tex", "detail")]
            base_img = hgr._render_strokes(strokes, H, W, ss=1)
            passes = (95, 97) if args.fix_passes >= 2 else (96,)
            if args.fix_passes <= 0:
                passes = ()
            add = fix_strokes(rgb, base_img, strokes, rng, passes=passes) if passes else []
            strokes = strokes + add
            rendered = hgr._render_strokes(strokes, H, W, ss=1)
            variants = [("all", strokes, rendered)]
            # pruning candidate: drop texture layer entirely
            no_tex = [s for s in strokes if s.get("kind") != "tex"]
            if len(no_tex) >= 500:
                r2 = hgr._render_strokes(no_tex, H, W, ss=1)
                variants.append(("no_tex", no_tex, r2))
            best_v = None
            for name, st, im in variants:
                sc = score_of(st, im, rgb, stroke_weight=args.stroke_weight,
                              ssim_weight=args.ssim_weight)
                sc["variant"] = name
                if best_v is None or sc["J"] > best_v[0]["J"]:
                    best_v = (sc, st, im)
            sc, strokes, rendered = best_v
            improved = sc["J"] > float(state["best_J"])
            if improved:
                state["best_J"] = sc["J"]
                state["best_psnr"] = sc["psnr"]
                state["best_ssim"] = sc["ssim"]
                state["best_strokes"] = sc["strokes"]
                state["best_round"] = r
                state["best_params"] = params_to_json(params)
                best_params = params
                with open(best_strokes_path, "w", encoding="utf-8") as fh:
                    json.dump(json_strokes(strokes), fh, ensure_ascii=False)
                Image.fromarray(np.clip(rendered * 255, 0, 255).astype(np.uint8)).save(best_png_path)
                save_round_image(os.path.join(rounds_dir, "best_R%d.png" % W), rgb, rendered,
                                 f"BEST round {r}  PSNR {sc['psnr']:.3f}")
            rec = {
                "round": r, "mode": mode, "variant": sc["variant"],
                "psnr": sc["psnr"], "ssim": sc["ssim"], "J": sc["J"],
                "strokes": sc["strokes"], "lines": sc["lines"],
                "gen": {k: gen_stats[k] for k in ("base", "tex", "detail", "stroke_count")},
                "fix": len(add), "seconds": round(time.time() - t0, 1),
                "params": params_to_json(params), "improved": bool(improved),
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            state["round"] = r
            state["history"].append(rec)
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            with open(state_path, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=1)
            if r % args.save_every == 0:
                image_path = os.path.join(rounds_dir, "round_%04d_R%d.png" % (r, W))
                save_round_image(image_path, rgb, rendered,
                                 f"round {r} {mode}/{sc['variant']}  PSNR {sc['psnr']:.3f}  strokes {sc['strokes']}")
                with open(os.path.join(rounds_dir, "round_%04d_report.json" % r), "w",
                          encoding="utf-8") as fh:
                    json.dump(rec, fh, ensure_ascii=False, indent=1)
            print(f"[round {r}] {mode}/{sc['variant']} PSNR={sc['psnr']:.3f} "
                  f"SSIM={sc['ssim']:.4f} J={sc['J']:.3f} strokes={sc['strokes']} "
                  f"({rec['seconds']}s){' *BEST*' if improved else ''}", flush=True)
        except Exception:
            with open(os.path.join(args.outdir, "errors.log"), "a", encoding="utf-8") as fh:
                fh.write(f"[round {r}] {traceback.format_exc()}\n")
            print(f"[round {r}] failed, continuing", flush=True)
            state["round"] = r
            with open(state_path, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=1)
    # summary
    with open(os.path.join(args.outdir, "summary.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("# Pure-stroke training summary\n\n")
        fh.write(f"- started: {state['started']}\n")
        fh.write(f"- rounds: {state['round']}\n")
        fh.write(f"- best J={state['best_J']:.4f} @ round {state.get('best_round')}, "
                 f"PSNR={state['best_psnr']:.4f}, SSIM={state['best_ssim']:.4f}, "
                 f"strokes={state['best_strokes']}\n\n")
        fh.write("| round | mode | variant | PSNR | SSIM | J | strokes | lines | fix | s |\n")
        fh.write("|---|---|---|---|---|---|---|---|---|---|\n")
        for r in state["history"]:
            fh.write(f"| {r['round']} | {r['mode']} | {r['variant']} | {r['psnr']} | {r['ssim']} | "
                     f"{r['J']} | {r['strokes']} | {r['lines']} | {r['fix']} | {r['seconds']} |\n")
    print("[train] done", state["round"], "best_J", state["best_J"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
