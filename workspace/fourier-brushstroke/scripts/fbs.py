#!/usr/bin/env python
"""fourier-brushstroke v2 唯一 CLI 入口。

子命令：
  build      位图 → .fbs + 重建 PNG + SVG + report.json（Stage 0–6）
  onestroke  逐笔画 .fbs → 一笔画 .fbs（单次 FFT，R2）
  video      一笔画 .fbs → 60s 作画视频（R3）
  render     任意分辨率渲染 .fbs → PNG
  eval       同分辨率四维度量（Stage 6）
  acceptance 验收套件（A1–A15 / T1–T13 / R2–R8）

用法示例见 SKILL.md；所有参数默认 None，只有显式给出才覆盖 FBSConfig（防止 CLI 默认值
无声覆盖 config —— 旧版 R4 类根因）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fbs_core import document, io_format, metrics as metrics_mod  # noqa: E402
from fbs_core import onestroke as onestroke_mod  # noqa: E402
from fbs_core import render as render_mod  # noqa: E402
from fbs_core import timeline as timeline_mod  # noqa: E402
from fbs_core import video as video_mod  # noqa: E402
from fbs_core.config import FBSConfig  # noqa: E402
from fbs_core.util import FBSError, WarningLog, stem_of  # noqa: E402


# ------------------------------------------------------------------ 工具
def _log(msg: str, quiet: bool = False) -> None:
    if not quiet:
        print(msg, flush=True)


def _save_png(rgb: np.ndarray, path: str) -> str:
    from PIL import Image

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    Image.fromarray(np.ascontiguousarray(rgb.astype(np.uint8))).save(path)
    return path


def _load_rgb_float(path: str) -> np.ndarray:
    from PIL import Image

    if not os.path.exists(path):
        raise FBSError("E_INPUT_MISSING", f"图片不存在: {path}")
    with Image.open(path) as im:
        return np.asarray(im.convert("RGB"), dtype=np.float32) / 255.0


def _resize_rgb(rgb: np.ndarray, width: int, height: int) -> np.ndarray:
    from PIL import Image

    if rgb.shape[1] == width and rgb.shape[0] == height:
        return rgb
    im = Image.fromarray(np.clip(rgb * 255.0 + 0.5, 0, 255).astype(np.uint8))
    return np.asarray(im.resize((int(width), int(height)), Image.LANCZOS), dtype=np.float32) / 255.0


def _cfg_from_args(args: argparse.Namespace) -> FBSConfig:
    overrides = {
        "target_resolution": getattr(args, "resolution", None),
        "epsilon_px": getattr(args, "epsilon_px", None),
        "denoise_sigma": getattr(args, "denoise_sigma", None),
        "colors": getattr(args, "colors", None),
        "min_region_area": getattr(args, "min_region_area", None),
        "N_mode": getattr(args, "N_mode", None),
        "N_max": getattr(args, "N_max", None),
        "hybrid": getattr(args, "hybrid", None),
        "width_mode": getattr(args, "width_mode", None),
        "width_scale": getattr(args, "width_scale", None),
        "subpixel_axis": getattr(args, "subpixel_axis", None),
        "fill_soft_px": getattr(args, "fill_soft", None),
        "color_field_grid": getattr(args, "color_field_grid", None),
        "pale_band_palette": (True if getattr(args, "pale_band_palette", False) else None),
        "texture_preserve": (True if getattr(args, "texture_preserve", False) else None),
        "allow_photo_experimental": (True if getattr(args, "allow_photo_experimental", False) else None),
        "mesh": (True if getattr(args, "mesh", False) else None),
        "mesh_tol": getattr(args, "mesh_tol", None),
        "mesh_min_side": getattr(args, "mesh_min_side", None),
        "mesh_max_cells": getattr(args, "mesh_max_cells", None),
        "painterly": (True if getattr(args, "painterly", False) else None),
        "brush_strokes": getattr(args, "brush_strokes", None),
        "brush_work_res": getattr(args, "brush_work_res", None),
        "prune_threshold": getattr(args, "prune_threshold", None),
        "min_branch_length_px": getattr(args, "min_branch_length", None),
        "merge_distance_px": getattr(args, "merge_distance", None),
        "max_curves": getattr(args, "max_curves", None),
        "supersample": getattr(args, "supersample", None),
        "render_mode": getattr(args, "mode", None),
        "shape_mode": getattr(args, "shape_mode", None),
        "curve_tol": getattr(args, "curve_tol", None),
        "harmonics": getattr(args, "harmonics", None),
        "max_fft": getattr(args, "max_fft", None),
        "video_duration": getattr(args, "duration", None),
        "video_fps": getattr(args, "fps", None),
        "video_circles": getattr(args, "circles", None),
    }
    return FBSConfig.merge_cli(**overrides)


def _cfg_from_doc(doc: dict, **overrides) -> FBSConfig:
    params = (doc.get("decomposition") or {}).get("params") or {}
    cfg = FBSConfig.from_dict(params)
    for k, v in overrides.items():
        if v is not None:
            setattr(cfg, k, v)
    cfg.validate()
    return cfg


def _print_fbs_summary(doc: dict, path: str) -> None:
    st = doc.get("stats", {})
    warn_n = len(doc.get("warnings", []))
    print(f"  .fbs          : {path} ({os.path.getsize(path) / 1024:.1f} KiB)")
    print(f"  strokes       : {st.get('stroke_count')}（填充 {st.get('fill_count')}，区域 {st.get('region_count')}）")
    print(f"  coefficients  : {st.get('coef_count')}")
    print(f"  N 中位/最大   : {_median_n(doc)} / {_max_n(doc)}")
    print(f"  warnings      : {warn_n}" + (f" -> {[w.get('code') for w in doc.get('warnings', [])[:6]]}" if warn_n else ""))


def _median_n(doc: dict) -> int:
    ns = sorted(int(s.get("N", 0)) for s in doc.get("strokes", []))
    if not ns:
        return 0
    return ns[len(ns) // 2]


def _max_n(doc: dict) -> int:
    return max((int(s.get("N", 0)) for s in doc.get("strokes", [])), default=0)


def _parse_size(text: str | None) -> tuple[int, int] | None:
    if not text:
        return None
    try:
        w, h = text.lower().split("x")
        return int(w), int(h)
    except Exception as exc:  # noqa: BLE001 - 参数格式错误直接报 FBSError
        raise FBSError("E_CONFIG", f"--size 格式应为 WxH: {text!r}", evidence={"error": str(exc)}) from exc


# ------------------------------------------------------------------ build
def cmd_build(args: argparse.Namespace) -> int:
    cfg = _cfg_from_args(args)
    outdir = os.path.abspath(args.out or ".")
    os.makedirs(outdir, exist_ok=True)
    paths = document.output_paths(args.input, outdir, cfg.target_resolution)
    warn = WarningLog()
    t0 = time.perf_counter()

    t = time.perf_counter()
    doc, info = document.build_from_image(args.input, cfg, warn, force_class=args.force_class)
    _log(f"[build] Stage0–4 {time.perf_counter() - t:.1f}s  class={info['klass']} "
         f"strokes={doc['stats']['stroke_count']}", args.quiet)

    t = time.perf_counter()
    render_rgb = render_mod.render(
        doc,
        resolution=cfg.target_resolution,
        supersample=cfg.supersample,
        mode=cfg.render_mode,
        warn=warn,
    )
    _log(f"[build] 渲染 {render_rgb.shape[1]}x{render_rgb.shape[0]} 用 {time.perf_counter() - t:.1f}s", args.quiet)

    ref = _resize_rgb(info["reference_rgb"], render_rgb.shape[1], render_rgb.shape[0])
    bg = np.asarray(doc.get("base_color", [255, 255, 255])[:3], dtype=np.float64) / 255.0
    metrics = metrics_mod.compare(render_rgb, ref, doc=doc, bg=bg)
    document.attach_quality(doc, metrics)

    io_format.write_fbs(doc, paths["fbs"])
    _save_png(render_rgb, paths["png"])
    files = {"fbs": paths["fbs"], "png": paths["png"]}
    if not args.no_svg:
        try:
            svg_info = render_mod.export_svg(doc, paths["svg"], resolution=cfg.target_resolution)
            files["svg"] = paths["svg"]
            _log(f"[build] SVG {os.path.getsize(paths['svg']) / 1024:.1f} KiB（互通用途，非保真路径）", args.quiet)
            if svg_info and svg_info.get("notes"):
                warn.add("W_SVG_APPROX", str(svg_info["notes"]))
        except FBSError as exc:
            warn.add("W_SVG_APPROX", f"SVG 导出失败: {exc.message}", severity="degrade")

    timings = dict(info.get("timings", {}))
    timings["render"] = time.perf_counter() - t0 - float(sum(v for k, v in info.get("timings", {}).items() if k.startswith("stage")))
    timings["total"] = time.perf_counter() - t0
    # 纯笔触绘画通道：额外导出"画笔属性时间轴"（傅里叶曲线 + 随时间 t 的属性曲线）
    timeline_path = None
    if (doc.get("decomposition") or {}).get("method") == "painterly_brush_strokes":
        base = os.path.basename(paths["fbs"])
        stem = base[: -len(".fbs.json")] if base.endswith(".fbs.json") else stem_of(paths["fbs"])
        timeline_path = os.path.join(os.path.dirname(paths["fbs"]), f"{stem}.brush_timeline.json")
        timeline_mod.write_timeline(doc, timeline_path, duration_s=cfg.video_duration)
        files["timeline"] = timeline_path
    document.write_report(paths["report"], doc=doc, info=info, metrics=metrics, files=files, timings=timings)

    print(f"\n[build] 完成（{time.perf_counter() - t0:.1f}s）")
    _print_fbs_summary(doc, paths["fbs"])
    print(f"  PSNR/SSIM     : {metrics.get('psnr', float('nan')):.3f} dB / {metrics.get('ssim', float('nan')):.4f}"
          f"  (R={cfg.target_resolution}, 参考图已重采样到同分辨率)")
    print(f"  PNG           : {paths['png']}")
    if timeline_path:
        print(f"  画笔时间轴    : {timeline_path}  ({os.path.getsize(timeline_path) / 1024:.1f} KiB)")
    if "svg" in files:
        print(f"  SVG           : {paths['svg']}")
    print(f"  report        : {paths['report']}")
    return 0


# ------------------------------------------------------------------ onestroke
def cmd_onestroke(args: argparse.Namespace) -> int:
    src = args.fbs
    doc = io_format.read_fbs(src)
    cfg = _cfg_from_doc(doc, harmonics=args.harmonics, max_fft=args.max_fft,
                        target_resolution=args.resolution or int(doc["render_contract"]["target_resolution"]),
                        fill_mode=args.fill_mode, path_order=args.order)
    warn = WarningLog()
    t0 = time.perf_counter()
    one = onestroke_mod.build_one_stroke(doc, cfg, warn, harmonics=cfg.harmonics, max_fft=cfg.max_fft)
    # build_one_stroke 内部已 finalize（含 fft_passes/path_deviation 诊断字段），此处不得再次
    # finalize，否则 stats 会被重算、诊断字段被冲掉（render 队友实测踩过）。
    base = os.path.basename(src)
    stem = base[: -len(".fbs.json")] if base.endswith(".fbs.json") else stem_of(src)
    out = args.out or os.path.join(os.path.dirname(os.path.abspath(src)), f"{stem}.onestroke.fbs.json")
    io_format.write_fbs(one, out)
    st = one.get("strokes", [{}])[0]
    print(f"[onestroke] 完成（{time.perf_counter() - t0:.1f}s）")
    print(f"  .fbs          : {out} ({os.path.getsize(out) / 1024:.1f} KiB)")
    print(f"  N / 系数      : {st.get('N')} / +1 单次 FFT（fft_passes={one.get('stats', {}).get('fft_passes')}）")
    print(f"  路径偏差 RMS  : {st.get('path_deviation_px'):.4f} px @R={cfg.target_resolution}"
          f"（max {st.get('path_deviation_max_px')} / p99.5 {st.get('path_deviation_p995_px')}）")
    print(f"  总弧长/抬笔   : {st.get('arc_length'):.2f} / {st.get('travel_length', 0.0):.2f} 世界单位")
    print(f"  填充区间      : {len(one.get('one_stroke_fills', []))}")
    for w in warn.to_list():
        print(f"  ! {w['code']}: {w['message']}")
    return 0


# ------------------------------------------------------------------ video
def cmd_video(args: argparse.Namespace) -> int:
    one = io_format.read_fbs(args.fbs)
    cfg = _cfg_from_doc(one)
    size = _parse_size(args.size)
    warn = WarningLog()
    t0 = time.perf_counter()
    info = video_mod.make_video(
        one,
        args.out,
        duration=float(args.duration if args.duration is not None else cfg.video_duration),
        fps=int(args.fps if args.fps is not None else cfg.video_fps),
        size=size,
        circles=int(args.circles if args.circles is not None else cfg.video_circles),
        mode=args.mode or "sweep",
        warn=warn,
        smooth=bool(getattr(args, "smooth", False)),
    )
    probe = video_mod.probe_video(args.out)
    print(f"[video] 完成（{time.perf_counter() - t0:.1f}s）")
    print(f"  文件          : {args.out} ({os.path.getsize(args.out) / 1024 / 1024:.1f} MiB)")
    print(f"  帧数/时长     : {probe.get('frames')} / {probe.get('duration_s'):.2f}s @ {probe.get('fps')}fps"
          f"（timeline={info.get('timeline')}）")
    print(f"  尺寸          : {probe.get('size')}  圆数上限 {info.get('circles')}")
    for w in warn.to_list():
        print(f"  ! {w['code']}: {w['message']}")
    return 0


# ------------------------------------------------------------------ render
def cmd_render(args: argparse.Namespace) -> int:
    doc = io_format.read_fbs(args.fbs)
    res = args.resolution or int(doc["render_contract"]["target_resolution"])
    mode = args.mode or str((doc.get("decomposition") or {}).get("params", {}).get("render_mode") or "sweep")
    device = getattr(args, "device", "cpu")
    # v1.38: optional OpenGL GPU rasteriser (same pixels as CPU exact within
    # ~40dB PSNR).  Default "cpu" keeps every existing render byte-stable;
    # "auto" tries GPU then falls back to CPU, "gl" requires GPU.
    if device in ("auto", "gl"):
        gpu_script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "skills", "fourier-multistroke-training", "scripts", "gpu_render.py",
        )
        if os.path.isfile(gpu_script):
            cmd = [sys.executable, gpu_script, "--fbs", os.path.abspath(args.fbs),
                   "--out", os.path.abspath(args.out), "--resolution", str(int(res)),
                   "--supersample", str(int(args.supersample or 2))]
            if args.width_scale is not None:
                cmd += ["--width-scale", str(args.width_scale)]
            base = doc.get("base_color")
            if isinstance(base, (list, tuple)) and len(base) >= 3:
                cmd += ["--background", f"{int(base[0])},{int(base[1])},{int(base[2])}"]
            t_gpu = time.perf_counter()
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
            if proc.returncode == 0 and os.path.isfile(args.out):
                print(f"[render] {args.out}  (GPU, {time.perf_counter() - t_gpu:.1f}s)")
                last = (proc.stdout or "").strip().splitlines()
                if last:
                    print(f"  {last[-1]}")
                return 0
            lines = (proc.stderr or proc.stdout or "").strip().splitlines()
            hint = lines[-1] if lines else f"exit={proc.returncode}"
            if device == "gl":
                print(f"[render] GPU 渲染失败: {hint}", file=sys.stderr)
                return proc.returncode or 1
            print(f"[render] GPU 不可用（{hint}），回退 CPU 渲染", file=sys.stderr)
        elif device == "gl":
            print("[render] GPU 渲染失败: 找不到 gpu_render.py", file=sys.stderr)
            return 1
    warn = WarningLog()
    t0 = time.perf_counter()
    rgb = render_mod.render(doc, resolution=res, supersample=args.supersample, mode=mode,
                            width_scale=args.width_scale, warn=warn)
    _save_png(rgb, args.out)
    print(f"[render] {args.out}  ({rgb.shape[1]}x{rgb.shape[0]}, {time.perf_counter() - t0:.1f}s)")
    for w in warn.to_list():
        print(f"  ! {w['code']}: {w['message']}")
    return 0


def cmd_timeline(args: argparse.Namespace) -> int:
    doc = io_format.read_fbs(args.fbs)
    cfg = _cfg_from_doc(doc)
    duration = float(args.duration if args.duration is not None else cfg.video_duration)
    overlap = float(args.overlap if args.overlap is not None else 2.5)
    base = os.path.basename(args.fbs)
    stem = base[: -len(".fbs.json")] if base.endswith(".fbs.json") else stem_of(args.fbs)
    out = args.out or os.path.join(os.path.dirname(os.path.abspath(args.fbs)), f"{stem}.brush_timeline.json")
    t0 = time.perf_counter()
    timeline_mod.write_timeline(doc, out, duration_s=duration, overlap=overlap)
    data = timeline_mod.build_timeline(doc, duration_s=duration, overlap=overlap)
    print(f"[timeline] {out}  ({os.path.getsize(out) / 1024:.1f} KiB, {time.perf_counter() - t0:.2f}s)")
    print(f"  笔触数        : {data['stroke_count']}  总时长 {data['duration_s']:.1f}s  重叠系数 {data['overlap']}")
    return 0


# ------------------------------------------------------------------ eval
def cmd_eval(args: argparse.Namespace) -> int:
    doc = io_format.read_fbs(args.fbs)
    ref = _load_rgb_float(args.reference)
    if args.render:
        rec = _load_rgb_float(args.render)
    else:
        native_long = int(max(ref.shape[0], ref.shape[1]))
        warn = WarningLog()
        rec = render_mod.render(doc, resolution=native_long, supersample=args.supersample,
                                mode=args.mode or "sweep", warn=warn)
        if args.save_render:
            _save_png(rec, args.save_render)
            print(f"[eval] 渲染已保存: {args.save_render}")
    bg = np.asarray(doc.get("base_color", [255, 255, 255])[:3], dtype=np.float64) / 255.0
    m = metrics_mod.compare(rec, ref, doc=doc, bg=bg)
    summary = {
        "psnr": round(float(m.get("psnr", float("nan"))), 4),
        "ssim": round(float(m.get("ssim", float("nan"))), 5),
        "rmse": round(float(m.get("rmse", float("nan"))), 4),
        "mae": round(float(m.get("mae", float("nan"))), 4),
        "resolution": m.get("resolution"),
        "structure": m.get("structure"),
        "tone": {k: v for k, v in (m.get("tone") or {}).items() if isinstance(v, (int, float))},
        "linewidth": m.get("linewidth"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(m, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        print(f"[eval] 报告: {args.out}")
    errs = m.get("contract", {}).get("errors")
    return 0 if not errs else 1


# ------------------------------------------------------------------ acceptance
def cmd_acceptance(args: argparse.Namespace) -> int:
    from run_acceptance import run as run_acceptance  # 延迟导入，避免无谓开销

    items = [s.strip() for s in args.items.split(",")] if args.items else None
    report = run_acceptance(
        quick=bool(args.quick),
        items=items,
        report_dir=args.report_dir,
        skip_video=bool(args.skip_video),
    )
    summary = report.get("summary", {})
    # 只有「无失败且无 BLOCKED」才算验收成功；SKIP 既不算通过也不算失败
    return 0 if summary.get("ok", report.get("ok", False)) else 1


# ------------------------------------------------------------------ argparse
def _add_cfg_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--resolution", type=int, default=None, help="目标渲染分辨率（长边像素数，默认 1024）")
    p.add_argument("--epsilon-px", dest="epsilon_px", type=float, default=None, help="亚像素契约（默认 0.5）")
    p.add_argument("--denoise-sigma", dest="denoise_sigma", type=float, default=None)
    p.add_argument("--colors", type=int, default=None, help="调色板色数；0=auto（默认）")
    p.add_argument("--N-mode", dest="N_mode", choices=["error_driven", "energy", "fixed"], default=None)
    p.add_argument("--N-max", dest="N_max", type=int, default=None)
    p.add_argument("--hybrid", choices=["auto", "on", "off"], default=None)
    p.add_argument("--width-mode", dest="width_mode", choices=["auto", "coverage", "dt", "hybrid"], default=None)
    p.add_argument("--width-scale", dest="width_scale", type=float, default=None, help="0=auto")
    p.add_argument("--subpixel-axis", dest="subpixel_axis", choices=["auto", "on", "off"], default=None)
    p.add_argument("--fill-soft", dest="fill_soft", type=float, default=None)
    p.add_argument("--color-field-grid", dest="color_field_grid", type=int, default=None, help="0=关闭区域颜色场")
    p.add_argument("--prune-threshold", dest="prune_threshold", type=float, default=None)
    p.add_argument("--min-branch-length", dest="min_branch_length", type=float, default=None)
    p.add_argument("--min-region-area", dest="min_region_area", type=int, default=None,
                   help="最小区域面积（源像素），小区域会被吸收；柔光插画可调大以抑制碎斑")
    p.add_argument("--merge-distance", dest="merge_distance", type=float, default=None)
    p.add_argument("--max-curves", dest="max_curves", type=int, default=None)
    p.add_argument("--supersample", type=int, default=None)
    p.add_argument("--shape-mode", dest="shape_mode", choices=["auto", "round", "soft", "marker"], default=None)
    p.add_argument("--curve-tol", dest="curve_tol", type=float, default=None)
    p.add_argument("--pale-band-palette", dest="pale_band_palette", action="store_true",
                   help="实验：近背景浅色内容补色（默认关；manga 实测掉分）")
    p.add_argument("--texture-preserve", dest="texture_preserve", action="store_true",
                   help="实验：线稿保留暗底薄纹理为独立笔画（默认关；manga PSNR −0.45 / SSIM +0.011）")
    p.add_argument("--allow-photo-experimental", dest="allow_photo_experimental", action="store_true",
                   help="实验：照片按自适应梯度面片处理（默认拒绝；合同仍延期照片，保真不保证）")
    p.add_argument("--mesh", dest="mesh", action="store_true",
                   help="连续色调通道：自适应双线性梯度面片（照片实验通道默认启用；也可对任意图强制使用）")
    p.add_argument("--mesh-tol", dest="mesh_tol", type=float, default=None,
                   help="面片残差 RMS 容差（默认 0.04；越小越准/面片越多）")
    p.add_argument("--mesh-min-side", dest="mesh_min_side", type=int, default=None,
                   help="最小面片边（像素，默认 4）")
    p.add_argument("--mesh-max-cells", dest="mesh_max_cells", type=int, default=None,
                   help="面片数安全阀（默认 12000）")
    p.add_argument("--painterly", dest="painterly", action="store_true",
                   help="纯笔触绘画：只输出 Fourier 曲线 + 画笔属性时间轴（无填充/面片；照片实验通道默认启用）")
    p.add_argument("--brush-strokes", dest="brush_strokes", type=int, default=None,
                   help="笔触预算（默认 3000；越多细节越多）")
    p.add_argument("--brush-work-res", dest="brush_work_res", type=int, default=None,
                   help="笔触拟合工作分辨率（长边像素，默认 512）")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="fbs.py",
        description="fourier-brushstroke v2：位图 → 傅里叶笔触（.fbs）→ 重建图 / 一笔画 / 60s 作画视频",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="位图 → .fbs + PNG + SVG + report.json")
    b.add_argument("input", help="输入图片路径（PNG/JPG/WEBP/BMP/GIF/TIFF）")
    b.add_argument("-o", "--out", default=None, help="输出目录（默认当前目录）")
    _add_cfg_args(b)
    b.add_argument("--mode", choices=["stamp", "sweep", "line"], default=None, help="渲染模式（默认 sweep）")
    b.add_argument("--force-class", dest="force_class",
                   choices=["lineart", "flat_art", "illustration", "gradient"], default=None,
                   help="强制图像分类（照片拒绝不可绕过）")
    b.add_argument("--no-svg", action="store_true", help="不导出 SVG")
    b.add_argument("--quiet", action="store_true")
    b.set_defaults(func=cmd_build)

    o = sub.add_parser("onestroke", help="逐笔画 .fbs → 一笔画 .fbs（单次 FFT）")
    o.add_argument("--fbs", required=True)
    o.add_argument("--out", default=None, help="默认 <stem>.onestroke.fbs.json")
    o.add_argument("--harmonics", type=int, default=None, help="目标谐波数（默认 65536）")
    o.add_argument("--max-fft", dest="max_fft", type=int, default=None, help="FFT 采样数上限（默认 1048576）")
    o.add_argument("--resolution", type=int, default=None, help="路径偏差复核用的分辨率（默认沿用 .fbs 契约）")
    o.add_argument("--fill-mode", dest="fill_mode", choices=["polygon", "brush"], default=None,
                   help="填充画法：polygon=整块多边形（默认）；brush=宽笔扫掠（画面随笔画出现，推荐做绘制过程视频）")
    o.add_argument("--order", dest="order", choices=["layer", "lines_first", "spatial", "reading"], default=None,
                   help="一笔画作画顺序：layer=先铺色后勾线（默认）；lines_first=先线稿后铺色；spatial=按区域逐块画；reading=从左到右（logo/文字推荐）")
    o.set_defaults(func=cmd_onestroke)

    v = sub.add_parser("video", help="一笔画 .fbs → 60s 作画视频")
    v.add_argument("--fbs", required=True)
    v.add_argument("--out", required=True)
    v.add_argument("--duration", type=float, default=None, help="默认取 FBSConfig.video_duration（60）")
    v.add_argument("--fps", type=int, default=None, help="默认取 FBSConfig.video_fps（30）")
    v.add_argument("--circles", type=int, default=None, help="默认取 FBSConfig.video_circles（600）")
    v.add_argument("--mode", choices=["sweep", "stamp"], default=None, help="默认 sweep")
    v.add_argument("--smooth", action="store_true",
                   help="平滑档：t 在时间上匀速推进（笔画数多/步骤>帧数时推荐；不保证每个微步骤独占一帧，但所有墨迹都会画出）")
    v.add_argument("--size", default=None, help="输出尺寸 WxH（默认竖版 720x1080 自适应）")
    v.set_defaults(func=cmd_video)

    r = sub.add_parser("render", help="任意分辨率渲染 .fbs → PNG")
    r.add_argument("--fbs", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--resolution", type=int, default=None)
    r.add_argument("--mode", choices=["stamp", "sweep", "line"], default=None)
    r.add_argument("--supersample", type=int, default=None)
    r.add_argument("--width-scale", dest="width_scale", type=float, default=None)
    r.add_argument("--device", choices=["auto", "gl", "cpu"], default="cpu",
                   help="渲染设备：cpu=原 CPU 光栅（默认，输出不变）；"
                        "gl=OpenGL GPU（需 PyOpenGL/glfw）；auto=GPU 失败自动回退 CPU")
    r.set_defaults(func=cmd_render)

    e = sub.add_parser("eval", help="同分辨率四维度量（Stage 6）")
    e.add_argument("--fbs", required=True)
    e.add_argument("--reference", required=True, help="参考图（原始输入图）")
    e.add_argument("--render", default=None, help="已渲染图；省略则按参考图原生分辨率现渲染")
    e.add_argument("--save-render", dest="save_render", default=None, help="现渲染时保存 PNG 路径")
    e.add_argument("--resolution", type=int, default=None)
    e.add_argument("--mode", choices=["stamp", "sweep", "line"], default=None)
    e.add_argument("--supersample", type=int, default=None)
    e.add_argument("--out", default=None, help="度量报告 JSON 输出路径")
    e.set_defaults(func=cmd_eval)

    tl = sub.add_parser("timeline", help="逐笔画 .fbs → 画笔属性时间轴 JSON（傅里叶曲线 + t 上属性）")
    tl.add_argument("--fbs", required=True)
    tl.add_argument("--out", default=None, help="默认 <stem>.brush_timeline.json")
    tl.add_argument("--duration", type=float, default=None, help="总时长（默认取 FBSConfig.video_duration=60）")
    tl.add_argument("--overlap", type=float, default=None, help="相邻笔触时间重叠系数（默认 2.5）")
    tl.set_defaults(func=cmd_timeline)

    a = sub.add_parser("acceptance", help="验收套件（A1–A15 / T1–T13 / R2–R8）")
    a.add_argument("--quick", action="store_true", help="快速模式（跳过视频与重负载项）")
    a.add_argument("--items", default=None, help="只跑指定项，逗号分隔，如 A1,R2")
    a.add_argument("--report-dir", dest="report_dir", default=None)
    a.add_argument("--skip-video", dest="skip_video", action="store_true")
    a.set_defaults(func=cmd_acceptance)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except FBSError as exc:
        print(f"\n[错误] {exc.code}: {exc.message}", file=sys.stderr)
        if exc.evidence:
            print(f"  证据: {json.dumps(exc.evidence, ensure_ascii=False)}", file=sys.stderr)
        for sug in exc.suggestions:
            print(f"  建议: {sug}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n[中断] 用户中止", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
