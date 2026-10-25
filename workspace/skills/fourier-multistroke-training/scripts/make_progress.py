#!/usr/bin/env python
"""Build a contact sheet + GIF from per-round training examples."""
from __future__ import annotations

import argparse
import glob
import json
import os
import re

import numpy as np
from PIL import Image, ImageDraw, ImageFont


def _font(size: int):
    try:
        return ImageFont.truetype("arial.ttf", size)
    except Exception:
        return ImageFont.load_default()


def _result_half(path: str) -> Image.Image:
    im = Image.open(path).convert("RGB")
    w, h = im.size
    # side-by-side sheets: reference is left, result is right
    if w > h * 1.1:
        im = im.crop((w // 2 + 4, 0, w, h))
    return im


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--cell", type=int, default=360)
    ap.add_argument("--cols", type=int, default=4)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    files = sorted(glob.glob(os.path.join(args.run, "rounds", "round_*_R*.png")))
    files = [f for f in files if re.search(r"round_\d+_R\d+\.png$", f)]
    if not files:
        raise SystemExit("no round images")
    logs = {}
    log_path = os.path.join(args.run, "training_log.jsonl")
    if os.path.exists(log_path):
        for line in open(log_path, encoding="utf-8"):
            try:
                r = json.loads(line)
                logs[int(r["round"])] = r
            except Exception:
                pass
    thumbs = []
    for f in files:
        m = re.search(r"round_(\d+)_R(\d+)\.png$", f)
        rnd = int(m.group(1))
        im = _result_half(f).resize((args.cell, args.cell), Image.LANCZOS)
        rec = logs.get(rnd, {})
        label = f"r{rnd}  PSNR {rec.get('psnr', '-')}"
        thumbs.append((im, label))
    cols = args.cols
    rows = (len(thumbs) + cols - 1) // cols
    pad = 8
    title_h = 34
    lab_h = 22
    W = cols * args.cell + (cols + 1) * pad
    H = title_h + rows * (args.cell + lab_h) + (rows + 1) * pad
    sheet = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(sheet)
    d.text((pad, 6), f"training rounds: {len(thumbs)}  ({os.path.basename(args.run)})",
           fill=(10, 10, 10), font=_font(18))
    for i, (im, label) in enumerate(thumbs):
        r, c = divmod(i, cols)
        x = pad + c * (args.cell + pad)
        y = title_h + pad + r * (args.cell + lab_h + pad)
        sheet.paste(im, (x, y))
        d.text((x + 4, y + args.cell + 2), label, fill=(20, 20, 20), font=_font(13))
    sheet_path = os.path.join(args.outdir, "training_progress_sheet.png")
    sheet.save(sheet_path)
    # animated GIF
    frames = [im.copy() for im, _ in thumbs]
    gif_path = os.path.join(args.outdir, "training_progress.gif")
    if frames:
        frames[0].save(gif_path, save_all=True, append_images=frames[1:],
                       duration=500, loop=0, optimize=True)
    print(sheet_path)
    print(gif_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
