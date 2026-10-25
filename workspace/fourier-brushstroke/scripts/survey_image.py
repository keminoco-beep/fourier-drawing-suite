#!/usr/bin/env python
"""原图高精度测绘 CLI：训练前先量化"识图精度"。

用法：
  python scripts/survey_image.py --input assets/out/user_chibi/chibi_whale.png --resolution 2048 --out out_dir
产物：<out>/survey.json + <out>/survey.md（也可用 --resolution 1024/2048/4096 做收敛性对比）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fbs_core import survey  # noqa: E402
from fbs_core.config import FBSConfig  # noqa: E402
from fbs_core.util import WarningLog  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="原图高精度测绘（survey）")
    ap.add_argument("--input", required=True)
    ap.add_argument("--resolution", type=int, default=None, help="测量分辨率长边（默认 1024）")
    ap.add_argument("--out", default=None, help="输出目录（默认与输入同目录）")
    args = ap.parse_args(argv)
    cfg = FBSConfig(target_resolution=int(args.resolution if args.resolution is not None else 1024))
    warn = WarningLog()
    data = survey.survey_image(args.input, cfg, warn)
    stem = os.path.splitext(os.path.basename(args.input))[0]
    outdir = os.path.abspath(args.out or os.path.dirname(os.path.abspath(args.input)))
    os.makedirs(outdir, exist_ok=True)
    src_json = os.path.splitext(args.input)[0] + ".survey.json"
    src_md = os.path.splitext(args.input)[0] + ".survey.md"
    dst_json = os.path.join(outdir, f"survey_R{int(cfg.target_resolution)}.json")
    dst_md = os.path.join(outdir, f"survey_R{int(cfg.target_resolution)}.md")
    if os.path.abspath(src_json) != os.path.abspath(dst_json):
        shutil.move(src_json, dst_json)
    if os.path.abspath(src_md) != os.path.abspath(dst_md):
        shutil.move(src_md, dst_md)
    print(f"[survey] {dst_json}")
    im = data["image"]
    li = data["lines"]
    re = data["regions"]
    print(f"  源图 {im['source_width']}×{im['source_height']}，测量 R={im['long_side']}（upscale×{im['upscale_factor']}）")
    print(f"  线条 {li['line_count']} 条，线宽中位 {li['width_px']['median']}px；"
          f"AA 带占墨迹 {data['coverage'].get('aa_fraction_of_ink')}")
    print(f"  区域 {re['region_count']} 个，面积中位 {re['area_px']['median']}px")
    for w in warn.to_list():
        print(f"  ! {w['code']}: {w['message']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
