#!/usr/bin/env python
"""Render a drawing-process MP4 directly from a `.fdraw` container.

No AI / no manual fbs step:
  .fdraw -> fdraw.unpack() (integrity verified) -> temp render.json ->
  make_video_circles.py (epicycle overlay) -> MP4.

Also accepts `.fbs.json` for convenience.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

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
for p in (HERE, os.path.join(WS, "fourier-brushstroke", "scripts"),
          os.path.join(WS, "fourier-prompt")):
    if p not in sys.path:
        sys.path.insert(0, p)

import make_video_circles as mvc  # noqa: E402


def _render_doc(src: str):
    """Return a render document plus a source label."""
    if src.lower().endswith(".fdraw"):
        from fourier_prompt import fdraw
        errors = fdraw.validate(src)
        if errors:
            raise SystemExit("invalid fdraw: " + "; ".join(errors))
        unpacked = fdraw.unpack(src)
        doc = unpacked.get("render")
        if not isinstance(doc, dict):
            raise SystemExit("fdraw has no render document")
        return doc, "fdraw"
    if src.lower().endswith(".json"):
        with open(src, encoding="utf-8") as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict) or not doc.get("strokes"):
            raise SystemExit("json is not an fbs render document")
        return doc, "fbs"
    raise SystemExit("expected .fdraw or .fbs.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fdraw", required=True, help=".fdraw container or .fbs.json")
    ap.add_argument("--out", required=True, help="output .mp4")
    ap.add_argument("--duration", type=float, default=240.0)
    ap.add_argument("--fps", type=int, default=24)
    ap.add_argument("--size", type=int, default=1080)
    ap.add_argument("--art", type=int, default=900)
    ap.add_argument("--circles", type=int, default=8)
    args = ap.parse_args()
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    t0 = time.time()
    doc, kind = _render_doc(args.fdraw)
    n_strokes = len(doc.get("strokes") or [])
    tmp_dir = tempfile.mkdtemp(prefix="fdraw_video_")
    tmp_fbs = os.path.join(tmp_dir, "render.fbs.json")
    with open(tmp_fbs, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False)
    print(f"[fdraw-video] source={args.fdraw} kind={kind} strokes={n_strokes} "
          f"temp={tmp_fbs}", flush=True)
    import make_fourier_process_video as base
    base._draw_circles = mvc._draw_circles
    rc = base.main([
        "--fbs", tmp_fbs,
        "--out", os.path.abspath(args.out),
        "--duration", str(args.duration),
        "--fps", str(args.fps),
        "--size", str(args.size),
        "--art", str(args.art),
        "--circles", str(args.circles),
    ])
    # keep the temp document if rendering failed for debugging
    if rc == 0:
        try:
            os.remove(tmp_fbs)
            os.rmdir(tmp_dir)
        except OSError:
            pass
    report = {
        "source": os.path.abspath(args.fdraw), "kind": kind,
        "strokes": n_strokes, "out": os.path.abspath(args.out),
        "bytes": os.path.getsize(args.out) if os.path.exists(args.out) else 0,
        "duration": args.duration, "fps": args.fps, "size": args.size,
        "seconds": round(time.time() - t0, 1), "rc": rc,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(rc)


if __name__ == "__main__":
    sys.exit(main())
