#!/usr/bin/env python
"""Process video wrapper with a cleaner Fourier epicycle overlay.

The skill's stock `_draw_circles` draws the DC (k=0) coefficient as a huge
circle and its link, producing long stray lines across the frame. Here the DC
term is still used for the chain translation but is not drawn, and oversized
circles are culled. Everything else is the stock per-stroke process renderer.
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np
from PIL import ImageDraw

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
SKILL = os.path.join(WS, "fourier-brushstroke", "scripts")
if SKILL not in sys.path:
    sys.path.insert(0, SKILL)

import make_fourier_process_video as m  # noqa: E402


def _draw_circles(frame, coef, pen_xy, scale, k_circles=12):
    if not coef:
        return
    arr = np.asarray(coef, dtype=np.float64).reshape(-1, 3)
    if len(arr) == 0:
        return
    if int(k_circles) <= 0:
        order = np.arange(len(arr))
    else:
        order = np.argsort(-np.abs(arr[:, 1] + 1j * arr[:, 2]))[:k_circles]
    ks = arr[order, 0]
    cs = (arr[order, 1] + 1j * arr[order, 2]) * float(scale)
    tip = complex(pen_xy[0], pen_xy[1])
    z = complex(0.0, 0.0)
    for k, c in zip(ks, cs):
        z += c * np.exp(2j * math.pi * float(k) * 0.25)
    cur = tip - z
    draw = ImageDraw.Draw(frame)
    limit = frame.size[0] * 0.28
    for k, c in zip(ks, cs):
        r = float(abs(c))
        nxt = cur + c
        if int(k) != 0 and 0.8 <= r <= limit:
            cx, cy = float(cur.real), float(cur.imag)
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=m._CIRCLE_EDGE, width=1)
            draw.line([(cx, cy), (float(nxt.real), float(nxt.imag))],
                      fill=m._CIRCLE_LINE, width=1)
        cur = nxt
    draw.ellipse([tip.real - 4, tip.imag - 4, tip.real + 4, tip.imag + 4], fill=m._TIP)


def main() -> int:
    m._draw_circles = _draw_circles
    return int(m.main())


if __name__ == "__main__":
    sys.exit(main())
