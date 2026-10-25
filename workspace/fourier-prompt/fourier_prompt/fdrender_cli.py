"""Standalone ``fd-render`` CLI (WP-F).

    python -m fourier_prompt.fdrender_cli render  --fdraw X.fdraw --out X.png [--resolution 512] [--quality fast|exact]
    python -m fourier_prompt.fdrender_cli info    --fdraw X.fdraw [--json]
    python -m fourier_prompt.fdrender_cli preview --fdraw X.fdraw --fraction 0.3 --out X.png
    python -m fourier_prompt.fdrender_cli frame   --fdraw X.fdraw --at 20 --out X.png
    python -m fourier_prompt.fdrender_cli compare --a A.png --b B.png [--json]
    python -m fourier_prompt.fdrender_cli bench   --fdraw X.fdraw --resolution 512 [--json]

``--fdraw`` is read through :func:`fourier_prompt.fdraw.unpack` (``render.json``
is the rendering authority); ``--fbs`` is an optional direct ``.fbs`` reader.

Exit codes: 0 ok, 1 input/render failure, 2 usage (argparse), 3 IO/internal.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
from PIL import Image

from .compat.skill_api import is_skill_error
from .fdraw import FDrawError, unpack as unpack_container
from .render_fast import (
    compare_images,
    fast_supported,
    load_image,
    render_auto,
    truncate_doc,
)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_INTERNAL = 3


class CLIError(Exception):
    """Input/render failure that maps to exit code 1."""

    def __init__(self, code: str, message: str, evidence: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.evidence = evidence or {}


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------
def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise CLIError("FD_E_INPUT", f"文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CLIError("FD_E_JSON", f"JSON 解析失败: {path}: {exc}") from exc
    except OSError as exc:
        raise CLIError("FD_E_IO", f"无法读取: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CLIError("FD_E_JSON", f"{path} 顶层不是 JSON 对象")
    return data


def load_document(args: argparse.Namespace) -> tuple[dict, str]:
    """Return ``(render_doc, source_label)`` from ``--fdraw`` or ``--fbs``."""
    if bool(args.fdraw) == bool(args.fbs):
        raise CLIError("FD_E_USAGE", "必须且只能给出 --fdraw 或 --fbs 之一")
    if args.fdraw:
        try:
            unpacked = unpack_container(args.fdraw)
        except FileNotFoundError as exc:
            raise CLIError("FD_E_INPUT", f"文件不存在: {args.fdraw}") from exc
        except FDrawError as exc:
            raise CLIError(exc.code, f"{args.fdraw}: {exc.message}", exc.evidence) from exc
        render = unpacked.get("render")
        if not isinstance(render, dict):
            raise CLIError("FD_E_RENDER", f"{args.fdraw} 缺少 render.json 文档")
        return render, f"fdraw:{args.fdraw}"
    doc = _load_json(args.fbs)
    return doc, f"fbs:{args.fbs}"


def _parse_background(text: str | None):
    if not text:
        return None
    parts = [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]
    if len(parts) not in (3, 4):
        raise CLIError("FD_E_USAGE", f"--background 需要 R,G,B，收到 {text!r}")
    try:
        return [int(p) for p in parts[:3]]
    except ValueError as exc:
        raise CLIError("FD_E_USAGE", f"--background 必须是整数: {text!r}") from exc


def _render(doc: dict, args: argparse.Namespace) -> tuple[np.ndarray, dict]:
    quality = getattr(args, "quality", "fast")
    try:
        img, info = render_auto(
            doc,
            resolution=int(args.resolution),
            quality=quality,
            return_info=True,
            width_scale=getattr(args, "width_scale", None),
            background=_parse_background(getattr(args, "background", None)),
        )
    except ValueError as exc:
        raise CLIError("FD_E_RENDER", str(exc)) from exc
    return img, info


def _save_png(img: np.ndarray, path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        raise CLIError("FD_E_IO", f"输出目录不存在: {parent}")
    try:
        Image.fromarray(img).save(path, format="PNG")
    except OSError as exc:
        raise CLIError("FD_E_IO", f"无法写入 {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def _cmd_render(args: argparse.Namespace) -> int:
    doc, source = load_document(args)
    started = time.perf_counter()
    img, info = _render(doc, args)
    _save_png(img, args.out)
    elapsed = time.perf_counter() - started
    payload = {
        "command": "render",
        "source": source,
        "out": args.out,
        "size": [int(img.shape[1]), int(img.shape[0])],
        "resolution": int(args.resolution),
        "quality": info.get("quality"),
        "fallback_reason": info.get("fallback_reason") or [],
        "approximations": info.get("approximations") or [],
        "elapsed_s": round(elapsed, 4),
        "png_bytes": os.path.getsize(args.out),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"rendered {args.out} {img.shape[1]}x{img.shape[0]} "
            f"quality={payload['quality']} {elapsed:.2f}s ({payload['png_bytes']} bytes)"
        )
        for reason in payload["fallback_reason"]:
            print(f"  fallback: {reason}")
        for shape in payload["approximations"]:
            print(f"  approximation: shape={shape}（纹理核未复制）")
    return EXIT_OK


def _cmd_info(args: argparse.Namespace) -> int:
    doc, source = load_document(args)
    ok, reasons = fast_supported(doc)
    strokes = doc.get("strokes") or []
    fills = sum(1 for st in strokes if isinstance(st, dict) and st.get("fill_role") == "region_fill")
    styles = doc.get("brush_styles") or []
    payload = {
        "command": "info",
        "source": source,
        "format": doc.get("format"),
        "version": doc.get("version"),
        "mode": doc.get("mode"),
        "canvas": doc.get("canvas"),
        "resolution": (doc.get("render_contract") or {}).get("target_resolution"),
        "width_scale": (doc.get("render_contract") or {}).get("width_scale"),
        "elements": len(strokes),
        "strokes": len(strokes) - fills,
        "fills": fills,
        "styles": len(styles),
        "decomposition_method": (doc.get("decomposition") or {}).get("method"),
        "fast_supported": bool(ok),
        "fast_reasons": list(reasons),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        return EXIT_OK
    print(f"source: {source}")
    print(f"format: {payload['format']} v{payload['version']} mode={payload['mode']}")
    print(f"canvas: {payload['canvas']}")
    print(
        f"elements: {payload['elements']} (strokes={payload['strokes']}, fills={payload['fills']}), "
        f"styles={payload['styles']}"
    )
    print(f"render_contract: resolution={payload['resolution']} width_scale={payload['width_scale']}")
    if ok:
        print("fast: supported")
    else:
        print("fast: NOT supported -> exact fallback")
        for reason in reasons:
            print(f"  - {reason}")
    return EXIT_OK


def _cmd_preview(args: argparse.Namespace) -> int:
    doc, source = load_document(args)
    try:
        truncated = truncate_doc(doc, fraction=float(args.fraction))
    except ValueError as exc:
        raise CLIError("FD_E_USAGE", str(exc)) from exc
    kept = len(truncated.get("strokes") or [])
    img, info = _render(truncated, args)
    _save_png(img, args.out)
    payload = {
        "command": "preview",
        "source": source,
        "out": args.out,
        "fraction": float(args.fraction),
        "elements_kept": kept,
        "elements_total": len(doc.get("strokes") or []),
        "size": [int(img.shape[1]), int(img.shape[0])],
        "quality": info.get("quality"),
        "fallback_reason": info.get("fallback_reason") or [],
        "elapsed_s": round(float(info.get("elapsed_s") or 0.0), 4),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"preview {args.out} {kept}/{payload['elements_total']} elements "
            f"({payload['fraction'] * 100:.1f}%) quality={payload['quality']}"
        )
    return EXIT_OK


def _cmd_frame(args: argparse.Namespace) -> int:
    doc, source = load_document(args)
    try:
        truncated = truncate_doc(doc, at=int(args.at))
    except ValueError as exc:
        raise CLIError("FD_E_USAGE", str(exc)) from exc
    kept = len(truncated.get("strokes") or [])
    img, info = _render(truncated, args)
    _save_png(img, args.out)
    payload = {
        "command": "frame",
        "source": source,
        "out": args.out,
        "at": int(args.at),
        "elements_kept": kept,
        "elements_total": len(doc.get("strokes") or []),
        "size": [int(img.shape[1]), int(img.shape[0])],
        "quality": info.get("quality"),
        "fallback_reason": info.get("fallback_reason") or [],
        "elapsed_s": round(float(info.get("elapsed_s") or 0.0), 4),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"frame {args.out} {kept}/{payload['elements_total']} elements (at={payload['at']})")
    return EXIT_OK


def _cmd_compare(args: argparse.Namespace) -> int:
    try:
        metrics = compare_images(args.a, args.b)
    except FileNotFoundError as exc:
        raise CLIError("FD_E_INPUT", f"文件不存在: {exc.filename or exc}") from exc
    except ValueError as exc:
        raise CLIError("FD_E_COMPARE", str(exc)) from exc
    if args.json:
        print(json.dumps(metrics, ensure_ascii=False, sort_keys=True))
    else:
        print(f"max_abs_diff={metrics['max_abs_diff']} mean_abs_diff={metrics['mean_abs_diff']:.6f}")
        print(
            f"psnr_db={metrics['psnr_db']:.3f} within1={metrics['within1_pct']:.3f}% "
            f"within2={metrics['within2_pct']:.3f}%"
        )
        print("parity: " + ("PASS" if metrics["parity_pass"] else "FAIL (max<=2 / psnr>=42)"))
    return EXIT_OK


def _cmd_bench(args: argparse.Namespace) -> int:
    from .render_fast import bench as bench_fn

    doc, source = load_document(args)
    try:
        result = bench_fn(
            doc,
            resolution=int(args.resolution),
            repeats=int(args.repeats),
            include_exact=bool(args.exact),
        )
    except ValueError as exc:
        raise CLIError("FD_E_RENDER", str(exc)) from exc
    result["command"] = "bench"
    result["source"] = source
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return EXIT_OK
    print(f"source: {source}  resolution={result['resolution']} repeats={result['repeats']}")
    print(f"quality: {result['quality']}  fallback: {result['fallback_reason'] or 'none'}")
    print(f"fast: {result['fast_s']}  P50={result['fast_p50_s']}s  peak={result['peak_mem_mb']} MB")
    if "exact_s" in result:
        print(f"exact: {result['exact_s']}s  peak={result['exact_peak_mem_mb']} MB")
        print(f"parity: {json.dumps(result['parity'], ensure_ascii=False, sort_keys=True)}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
def _add_source(parser: argparse.ArgumentParser, *, required: bool = True) -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--fdraw", help="input .fdraw container (render.json via unpack)")
    group.add_argument("--fbs", help="input .fbs JSON file")


def _add_render_opts(parser: argparse.ArgumentParser, *, quality: bool = True) -> None:
    parser.add_argument("--resolution", type=int, default=512, help="long-side pixel size")
    if quality:
        parser.add_argument("--quality", choices=("fast", "exact"), default="fast")
    parser.add_argument("--width-scale", type=float, default=None, help="override stroke width scale")
    parser.add_argument("--background", default=None, help="override background R,G,B")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m fourier_prompt.fdrender_cli",
        description="fd-render: fast/exact raster of .fdraw/.fbs + parity/bench tools.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    render = sub.add_parser("render", help="render a document to PNG")
    _add_source(render)
    _add_render_opts(render)
    render.add_argument("--out", required=True)
    render.add_argument("--json", action="store_true")
    render.set_defaults(func=_cmd_render)

    info = sub.add_parser("info", help="inspect document + fast coverage")
    _add_source(info)
    info.add_argument("--json", action="store_true")
    info.set_defaults(func=_cmd_info)

    preview = sub.add_parser("preview", help="render the first fraction of elements")
    _add_source(preview)
    _add_render_opts(preview)
    preview.add_argument("--fraction", type=float, default=0.3)
    preview.add_argument("--out", required=True)
    preview.add_argument("--json", action="store_true")
    preview.set_defaults(func=_cmd_preview)

    frame = sub.add_parser("frame", help="render the first N elements")
    _add_source(frame)
    _add_render_opts(frame)
    frame.add_argument("--at", type=int, default=20)
    frame.add_argument("--out", required=True)
    frame.add_argument("--json", action="store_true")
    frame.set_defaults(func=_cmd_frame)

    compare = sub.add_parser("compare", help="compare two PNGs (parity metrics)")
    compare.add_argument("--a", required=True)
    compare.add_argument("--b", required=True)
    compare.add_argument("--json", action="store_true")
    compare.set_defaults(func=_cmd_compare)

    bench = sub.add_parser("bench", help="time the fast backend (optional parity)")
    _add_source(bench)
    bench.add_argument("--resolution", type=int, default=512)
    bench.add_argument("--repeats", type=int, default=2)
    bench.add_argument("--exact", action="store_true", help="also time exact + parity")
    bench.add_argument("--json", action="store_true")
    bench.set_defaults(func=_cmd_bench)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except CLIError as exc:
        print(f"error[{exc.code}]: {exc.message}", file=sys.stderr)
        for message in exc.evidence.get("errors") or []:
            print(f"  - {message}", file=sys.stderr)
        return EXIT_FAIL
    except FDrawError as exc:
        print(f"error[{exc.code}]: {exc.message}", file=sys.stderr)
        return EXIT_FAIL
    except FileNotFoundError as exc:
        print(f"error[FD_E_INPUT]: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except OSError as exc:
        print(f"error[FD_E_IO]: {exc}", file=sys.stderr)
        return EXIT_INTERNAL
    except ValueError as exc:
        print(f"error[FD_E_RENDER]: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except Exception as exc:  # pragma: no cover - defensive
        if is_skill_error(exc):
            code = getattr(exc, "code", "FD_E_RENDER")
            message = getattr(exc, "message", str(exc))
            print(f"error[{code}]: {message}", file=sys.stderr)
            return EXIT_FAIL
        print(f"error[FD_E_INTERNAL]: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
