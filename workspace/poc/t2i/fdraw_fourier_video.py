# -*- coding: utf-8 -*-
"""仅读取 `.fdraw` 容器，渲染「傅里叶一笔画作画过程」视频（旋转圆 epicycle + 逐帧增量绘制）。

输入只有 `.fdraw`：脚本通过 `fourier_prompt.fdraw.unpack` 读取容器，取 `render.json`
（= `.fbs` v2 文档，渲染权威）在**内存中**合成 one_stroke 文档，再调用
`fourier-brushstroke` 的 `fbs_core.video.make_video` 出 mp4。
不需要源 PNG、不需要中间 `.fbs` 文件；`render.json` 里的几何/笔触/填充就是全部依据。

用法（Windows）::

    $PY = "<path>"
    & $PY poc\\t2i\\fdraw_fourier_video.py `
        --fdraw poc\\t2i\\out_e2e_20261021\\complex_E_market_simple\\t2i_lineart_krea2_s42.fdraw `
        --out  poc\\t2i\\out_video\\E_market_fourier_30s.mp4 `
        --duration 30 --fps 30 --size 720x720 --circles 600

产物：mp4 + poster PNG + report.json（溯源/统计/校验）。
退出码：0 成功；1 渲染/编码失败；2 用法错误；3 IO/内部错误。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import traceback
from pathlib import Path

SCHEMA = "fdraw-fourier-video/1"


def _workspace() -> Path:
    # <workspace>/poc/t2i/fdraw_fourier_video.py
    return Path(__file__).resolve().parents[2]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_size(text: str) -> tuple[int, int]:
    try:
        w, h = text.lower().split("x")
        w, h = int(w), int(h)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"--size 格式应为 WxH: {text!r}") from exc
    if w < 64 or h < 64:
        raise ValueError(f"--size 过小: {text!r}")
    return w, h


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="只读 .fdraw → 傅里叶一笔画作画视频（旋转圆 + 逐帧增量绘制）"
    )
    ap.add_argument("--fdraw", required=True, help="输入 .fdraw 文件（唯一数据来源）")
    ap.add_argument("--out", required=True, help="输出 mp4 路径")
    ap.add_argument("--duration", type=float, default=30.0, help="视频时长秒（默认 30）")
    ap.add_argument("--fps", type=int, default=30, help="帧率（默认 30）")
    ap.add_argument("--size", default="1920x1920", help="画面尺寸 WxH（默认 1920x1920）")
    ap.add_argument("--title", default=None,
                    help="视频左上角标题（默认取 fdraw 源文件 stem；中文可用）")
    ap.add_argument("--circles", type=int, default=2000, help="参与计算的 top-K 谐波（默认 2000）")
    ap.add_argument("--circle-max-k", type=int, default=960,
                    help="可见旋转圆的最大谐波次数（默认 960；更少=更干净，更多=更密）")
    ap.add_argument("--circle-min-px", type=float, default=0.35,
                    help="可见圆的最小半径像素（默认 0.35；越小画出的圆越多）")
    ap.add_argument("--harmonics", type=int, default=65536, help="一笔画目标谐波数（默认 65536）")
    ap.add_argument("--max-fft", type=int, default=2_097_152, help="一笔画 FFT 采样上限（默认 2097152）")
    ap.add_argument("--fill-mode", choices=("polygon", "brush"), default="polygon",
                    help="填充方式：polygon=整块多边形（默认）；brush=宽笔扫掠填充")
    ap.add_argument("--order", choices=("layer", "lines_first", "spatial", "reading"),
                    default="layer", help="作画顺序（默认 layer：先铺色后勾线）")
    ap.add_argument("--no-smooth", action="store_true",
                    help="关闭平滑时间轴（默认开启，适合元素数多的复杂图）")
    ap.add_argument("--poster-resolution", type=int, default=2048, help="海报 PNG 长边像素（默认 2048）")
    ap.add_argument("--no-poster", action="store_true", help="不生成海报 PNG")
    ap.add_argument("--report", default=None, help="报告 JSON 路径（默认 <out>.report.json）")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    workspace = _workspace()
    prompt_dir = workspace / "fourier-prompt"
    skill_scripts = workspace / "fourier-brushstroke" / "scripts"
    for p in (str(prompt_dir), str(skill_scripts)):
        if p not in sys.path:
            sys.path.insert(0, p)

    try:
        from fourier_prompt import fdraw  # noqa: E402
        from fbs_core.config import FBSConfig  # noqa: E402
        from fbs_core.onestroke import build_one_stroke  # noqa: E402
        from fbs_core.render import render_to_image  # noqa: E402
        from fbs_core.util import FBSError, WarningLog  # noqa: E402
        from fbs_core.video import make_video, probe_video  # noqa: E402
    except Exception as exc:  # noqa: BLE001
        print(f"error[E_IMPORT]: 无法导入渲染核心: {exc}", file=sys.stderr)
        return 3

    try:
        size = _parse_size(args.size)
    except ValueError as exc:
        print(f"error[E_USAGE]: {exc}", file=sys.stderr)
        return 2

    in_path = os.path.abspath(args.fdraw)
    out_path = os.path.abspath(args.out)
    if not os.path.isfile(in_path):
        print(f"error[E_INPUT]: 输入 .fdraw 不存在: {in_path}", file=sys.stderr)
        return 2
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    t0 = time.time()
    warnings: list[dict] = []

    try:
        blob = Path(in_path).read_bytes()
        in_sha = _sha256_bytes(blob)
        container = fdraw.unpack(blob)  # 权威解析 + root hash 校验
        doc = container["render"]
        meta = container.get("meta") or {}
        info = fdraw.inspect(blob)

        mode = doc.get("mode")
        n_strokes = len(doc.get("strokes") or [])
        if mode not in ("per_stroke", "one_stroke"):
            raise FBSError("E_INPUT_MODE", f"不支持的 .fdraw mode: {mode!r}")

        if mode == "one_stroke":
            one = doc
            one_stats = {"reused": True}
        else:
            params = (doc.get("decomposition") or {}).get("params") or {}
            cfg = FBSConfig.from_dict(params)
            cfg.target_resolution = int((doc.get("render_contract") or {}).get("target_resolution") or 1024)
            cfg.harmonics = int(args.harmonics)
            cfg.max_fft = int(args.max_fft)
            cfg.fill_mode = args.fill_mode
            cfg.path_order = args.order
            cfg.validate()
            warn = WarningLog()
            t1 = time.time()
            one = build_one_stroke(doc, cfg, warn, harmonics=cfg.harmonics, max_fft=cfg.max_fft)
            one_build_s = time.time() - t1
            one_stroke = (one.get("strokes") or [{}])[0]
            one_stats = {
                "reused": False,
                "build_s": round(one_build_s, 3),
                "N": one_stroke.get("N"),
                "fft_passes": (one.get("stats") or {}).get("fft_passes"),
                "path_deviation_px": one_stroke.get("path_deviation_px"),
                "path_deviation_max_px": one_stroke.get("path_deviation_max_px"),
                "arc_length": one_stroke.get("arc_length"),
                "travel_length": one_stroke.get("travel_length"),
                "fill_spans": len(one.get("one_stroke_fills") or []),
                "warnings": warn.to_list(),
            }
            for w in warn.to_list():
                warnings.append({"stage": "onestroke", **w})

        # 海报：one-stroke 静态终图（只从 .fdraw 派生）
        poster_path = None
        poster_info = None
        if not args.no_poster:
            t2 = time.time()
            poster_res = int(args.poster_resolution)
            image = render_to_image(one, resolution=poster_res, width_scale=1.0)
            poster_path = os.path.splitext(out_path)[0] + "_poster.png"
            image.save(poster_path)
            poster_info = {
                "path": poster_path,
                "bytes": os.path.getsize(poster_path),
                "resolution": poster_res,
                "elapsed_s": round(time.time() - t2, 3),
            }

        # 视频：旋转圆链 + 逐帧增量绘制
        t3 = time.time()
        warn_v = WarningLog()
        # fbs_core.video 内部把可见圆的最大谐波限制在 240；这里按用户要求放宽
        # （高频圆在相邻帧间旋转角更大，太多会闪烁；由 --circle-max-k 控制）。
        try:
            import fbs_core.video as _video_mod  # noqa: E402
            _video_mod._CIRCLE_MAX_K = int(args.circle_max_k)
            _video_mod._MIN_CIRCLE_PX = float(args.circle_min_px)
        except Exception:
            pass
        one_for_video = one
        if args.title:
            one_for_video = dict(one)
            src = dict(one_for_video.get("source") or {})
            src["path"] = str(args.title)  # fbs_core.video._video_title 从 source.path 取标题
            one_for_video["source"] = src
        video = make_video(
            one_for_video,
            out_path,
            duration=float(args.duration),
            fps=int(args.fps),
            size=size,
            circles=int(args.circles),
            mode="sweep",
            warn=warn_v,
            smooth=not bool(args.no_smooth),
        )
        video_s = round(time.time() - t3, 3)
        for w in warn_v.to_list():
            warnings.append({"stage": "video", **w})
        probe = probe_video(out_path)

        out_sha = _sha256_file(out_path)
        report = {
            "schema": SCHEMA,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "input": {
                "fdraw": in_path,
                "bytes": len(blob),
                "sha256": in_sha,
                "root_sha256": container["root_sha256"],
                "root_sha256_ok": container["root_sha256_ok"],
                "schema": info.get("schema"),
                "mode": mode,
                "elements": n_strokes,
                "layers": info.get("render", {}).get("layers"),
                "width_scale": info.get("render", {}).get("width_scale"),
            },
            "provenance": {
                "prompt": meta.get("prompt"),
                "negative": meta.get("negative"),
                "engine": meta.get("engine"),
                "models": meta.get("models"),
                "seed": meta.get("seed"),
                "steps": meta.get("steps"),
                "cfg": meta.get("cfg"),
                "sampler": meta.get("sampler"),
                "comfy_prompt_id": meta.get("comfy_prompt_id"),
                "source_png": meta.get("source_png"),
                "build": (meta.get("build") or {}).get("fbs") if isinstance(meta.get("build"), dict) else None,
                "created": meta.get("created"),
            },
            "onestroke": one_stats,
            "video": {
                "path": out_path,
                "title": args.title,
                "bytes": os.path.getsize(out_path),
                "sha256": out_sha,
                "frames": probe.get("frames"),
                "fps": probe.get("fps"),
                "duration_s": probe.get("duration_s"),
                "size": probe.get("size"),
                "circles": int(args.circles),
                "circle_max_k": int(args.circle_max_k),
                "circle_min_px": float(args.circle_min_px),
                "smooth": not bool(args.no_smooth),
                "render_s": video_s,
                "info": video,
            },
            "poster": poster_info,
            "warnings": warnings,
            "elapsed_s": round(time.time() - t0, 3),
            "tool_versions": {
                "fdraw_read": "fourier_prompt.fdraw.unpack (root hash verified)",
                "stroke_core": "fourier-brushstroke fbs_core",
                "video": "fbs_core.video.make_video",
            },
        }
        report_path = args.report or (os.path.splitext(out_path)[0] + ".report.json")
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)

        print(f"[fdraw→fourier video] 完成 {time.time() - t0:.1f}s")
        print(f"  输入 .fdraw : {in_path}")
        print(f"    {len(blob)} B sha256={in_sha[:16]}… elements={n_strokes} mode={mode}")
        print(f"  一笔画      : N={one_stats.get('N')} fft_passes={one_stats.get('fft_passes')} "
              f"fills={one_stats.get('fill_spans')} build={one_stats.get('build_s')}s")
        print(f"  视频        : {out_path} ({os.path.getsize(out_path)/1024/1024:.1f} MiB, "
              f"{probe.get('frames')} 帧 / {probe.get('duration_s'):.2f}s / {probe.get('fps')}fps)")
        if poster_path:
            print(f"  海报        : {poster_path}")
        print(f"  报告        : {report_path}")
        if warnings:
            print(f"  warnings    : {[w.get('code') for w in warnings]}")
        return 0
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", "E_INTERNAL")
        print(f"error[{code}]: {exc}", file=sys.stderr)
        if os.environ.get("FDRAW_VIDEO_DEBUG"):
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
