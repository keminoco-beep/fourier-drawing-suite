#!/usr/bin/env python
"""给一笔画过程视频追加"静止收尾帧"：完整成图、不显示旋转圆。"""
from __future__ import annotations

import argparse
import os
import sys

import imageio
import numpy as np
from PIL import Image, ImageDraw

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fbs_core import io_format  # noqa: E402
from fbs_core import render as render_mod  # noqa: E402
from fbs_core import video as video_mod  # noqa: E402
from fbs_core.util import WarningLog  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="追加静止收尾帧（无旋转圆）")
    ap.add_argument("--draw-video", required=True)
    ap.add_argument("--fbs", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--static-seconds", type=float, default=None)
    args = ap.parse_args(argv)
    static_s = float(args.static_seconds if args.static_seconds is not None else 6.0)

    doc = io_format.read_fbs(args.fbs)
    rd = imageio.get_reader(args.draw_video)
    meta = rd.get_meta_data()
    W = int(meta["size"][0]); H = int(meta["size"][1])
    _fps = meta.get("fps")
    fps = float(_fps) if _fps is not None else 60.0
    canvas = (float(doc["canvas"]["width"]), float(doc["canvas"]["height"]))
    layout = video_mod._layout(canvas, W, H)
    art = render_mod.render(doc, resolution=max(2048, int(layout["R_art"])), supersample=1, mode="sweep",
                            warn=WarningLog())
    art_img = Image.fromarray(art).resize((layout["art_w"], layout["art_h"]), Image.LANCZOS)
    font, cjk = video_mod._load_font(max(10, int(H * 0.019)))
    small, _ = video_mod._load_font(max(8, int(H * 0.015)))
    title = "Fourier one-stroke - complete (static)"
    n_static = max(1, int(round(static_s * fps)))
    outdir = os.path.dirname(os.path.abspath(args.out))
    art_img.save(os.path.join(outdir, "onestroke_complete_static.png"))

    writer = video_mod._writer_or_raise(args.out, int(round(fps)))
    n = 0
    try:
        for frame in rd:
            writer.append_data(np.asarray(frame))
            n += 1
        for _ in range(n_static):
            frame = Image.new("RGB", (W, H), video_mod._UI_BG)
            frame.paste(art_img, (layout["x0"], layout["y0"]))
            dr = ImageDraw.Draw(frame)
            dr.text((layout["margin_x"], int(H * 0.012)), title,
                    fill=video_mod._UI_INK, font=font)
            bar_w = W - 2 * layout["margin_x"]
            by = int(H * 0.055)
            dr.rectangle([layout["margin_x"], by, layout["margin_x"] + bar_w, by + 8],
                         fill=video_mod._UI_ACCENT)
            dr.text((layout["margin_x"], by + 12), "100.0%   complete",
                    fill=video_mod._UI_INK, font=small)
            writer.append_data(np.asarray(frame))
            n += 1
    finally:
        rd.close()
        writer.close()
    probe = video_mod.probe_video(args.out)
    print(f"[append] frames={probe.get('frames')} fps={probe.get('fps')} "
          f"duration={probe.get('duration_s')} size={probe.get('size')} "
          f"(draw={n - n_static}, static={n_static})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
