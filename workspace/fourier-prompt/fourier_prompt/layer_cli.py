"""Standalone layer export CLI (WP-I).

::

    python -m fourier_prompt.layer_cli export --fdraw X.fdraw --out-dir D \
        [--psd] [--kra] [--layers-png] [--resolution 256] [--quality fast] [--json]
    python -m fourier_prompt.layer_cli inspect-layers --fdraw X.fdraw [--json]

When none of ``--psd/--kra/--layers-png`` is given all three exports run.  The
input ``.fdraw`` is never modified; layer metadata is derived additively from
``render.json`` (or ``source.fdsl.json`` when present).

Exit codes: 0 ok, 1 export/validation failure, 2 usage, 3 I/O or internal.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import __version__
from .fdraw import FDrawError, unpack
from .kra import write_kra
from .layers import (
    MAX_RESOLUTION,
    MIN_RESOLUTION,
    LayerError,
    add_layer_metadata,
    doc_layers,
    render_layer_pngs,
    render_layers,
)
from .psd import write_psd

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_INTERNAL = 3


def _print_json(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))


def _load_document(path: str) -> tuple[dict, dict]:
    result = unpack(path)
    doc = result["render"]
    if not isinstance(doc.get("layers"), list) or not doc["layers"]:
        source = result.get("source_fdsl")
        add_layer_metadata(doc, source if isinstance(source, dict) else None)
    return doc, result


def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0] or "export"


def _check_resolution(value: int) -> int:
    if isinstance(value, bool) or not MIN_RESOLUTION <= int(value) <= MAX_RESOLUTION:
        raise LayerError(
            "FP_E_LAYER_RESOLUTION",
            f"resolution 必须在 [{MIN_RESOLUTION}, {MAX_RESOLUTION}]: {value}",
        )
    return int(value)


def _cmd_export(args: argparse.Namespace) -> int:
    resolution = _check_resolution(args.resolution)
    targets = {"psd": args.psd, "kra": args.kra, "layers_png": args.layers_png}
    if not any(targets.values()):
        targets = {"psd": True, "kra": True, "layers_png": True}
    doc, container = _load_document(args.fdraw)
    stem = os.path.basename(args.name) if args.name else _stem(args.fdraw)
    if not stem:
        raise LayerError("FP_E_LAYER_INPUT", "--name 不能为空")
    out_dir = os.fspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    started = time.perf_counter()
    images = render_layers(doc, resolution=resolution, quality=args.quality)
    result: dict = {
        "ok": True,
        "command": "export",
        "fdraw": os.path.abspath(args.fdraw),
        "out_dir": os.path.abspath(out_dir),
        "resolution": resolution,
        "quality": args.quality,
        "layer_count": len(images),
        "layers": [
            {
                "index": image["index"],
                "name": image["name"],
                "visible": image["visible"],
                "opacity": image["opacity"],
                "stroke_count": image["stroke_count"],
                "fill_count": image["fill_count"],
                "bbox": image["bbox"],
                "quality": image["quality"],
                "fallback_reason": image["fallback_reason"],
            }
            for image in images
        ],
        "outputs": {},
        "warnings": [warning for image in images for warning in image.get("warnings") or []],
        "fallbacks": [
            {"index": image["index"], "name": image["name"], "reasons": image["fallback_reason"]}
            for image in images
            if image["fallback_reason"]
        ],
    }
    if targets["layers_png"]:
        png_dir = os.path.join(out_dir, f"{stem}_layers")
        manifest = render_layer_pngs(
            doc,
            resolution=resolution,
            out_dir=png_dir,
            quality=args.quality,
            layer_images=images,
        )
        result["outputs"]["layers_png"] = {
            "out_dir": os.path.abspath(png_dir),
            "count": len(manifest),
            "files": [
                {
                    "index": entry["index"],
                    "name": entry["name"],
                    "path": entry["path"],
                    "bytes": entry["bytes"],
                    "sha256": entry["sha256"],
                }
                for entry in manifest
            ],
            "total_bytes": sum(entry["bytes"] for entry in manifest),
        }
    if targets["psd"]:
        psd_path = os.path.join(out_dir, f"{stem}.psd")
        result["outputs"]["psd"] = write_psd(
            doc,
            out=psd_path,
            resolution=resolution,
            quality=args.quality,
            layer_images=images,
        )
    if targets["kra"]:
        kra_path = os.path.join(out_dir, f"{stem}.kra")
        result["outputs"]["kra"] = write_kra(
            doc,
            out=kra_path,
            resolution=resolution,
            quality=args.quality,
            layer_images=images,
        )
    result["elapsed_s"] = round(time.perf_counter() - started, 4)
    result["container"] = {
        "schema": container.get("meta", {}).get("schema"),
        "root_sha256": container.get("root_sha256"),
        "elements": len(doc.get("strokes") or []),
    }
    if args.json:
        _print_json(result)
    else:
        print(f"fdraw: {args.fdraw}")
        print(f"canvas: {images[0]['width']}x{images[0]['height']} @ R{resolution}")
        print(f"layers: {len(images)}")
        for image in images:
            print(
                f"  L{image['index']} {image['name']!r} strokes={image['stroke_count']} "
                f"fills={image['fill_count']} visible={image['visible']} "
                f"opacity={image['opacity']:g} quality={image['quality']}"
                + (f" fallback={image['fallback_reason']}" if image["fallback_reason"] else "")
            )
        for kind, payload in result["outputs"].items():
            if kind == "layers_png":
                print(f"layers-png: {payload['count']} files -> {payload['out_dir']} ({payload['total_bytes']} bytes)")
            else:
                print(f"{kind}: {payload['out']} ({payload['bytes']} bytes, sha256={payload['sha256'][:16]}…)")
        print(f"elapsed: {result['elapsed_s']}s")
    return EXIT_OK


def _cmd_inspect_layers(args: argparse.Namespace) -> int:
    doc, container = _load_document(args.fdraw)
    layers = doc_layers(doc)
    payload = {
        "ok": True,
        "command": "inspect-layers",
        "fdraw": os.path.abspath(args.fdraw),
        "schema": container.get("meta", {}).get("schema"),
        "canvas": doc.get("canvas"),
        "resolution_contract": (doc.get("render_contract") or {}).get("target_resolution"),
        "layer_count": len(layers),
        "layers": layers,
    }
    if args.json:
        _print_json(payload)
        return EXIT_OK
    print(f"fdraw: {args.fdraw}")
    print(
        f"canvas: {payload['canvas']}  target_resolution={payload['resolution_contract']}  "
        f"layers={len(layers)}"
    )
    print(f"{'idx':>3} {'ord':>3} {'name':<18} {'vis':>3} {'op':>4} {'strk':>5} {'fill':>5} bbox")
    for layer in layers:
        bbox = layer.get("bbox")
        bbox_text = "-" if not bbox else "[" + ", ".join(f"{float(v):.3f}" for v in bbox) + "]"
        print(
            f"{layer['index']:>3} {layer['order']:>3} {str(layer['name'])[:18]:<18} "
            f"{'yes' if layer['visible'] else 'no':>3} {layer['opacity']:>4.2f} "
            f"{layer['stroke_count']:>5} {layer['fill_count']:>5} {bbox_text}"
        )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m fourier_prompt.layer_cli",
        description="Layer export for .fdraw documents (PSD / KRA / per-layer PNG).",
        epilog="Exit codes: 0 ok, 1 export failure, 2 usage, 3 I/O or internal.",
    )
    parser.add_argument("--version", action="version", version=f"fourier-prompt {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="export layered PSD/KRA/PNG files")
    export.add_argument("--fdraw", required=True, help="input .fdraw container")
    export.add_argument("--out-dir", required=True, help="output directory")
    export.add_argument("--psd", action="store_true", help="write <name>.psd")
    export.add_argument("--kra", action="store_true", help="write <name>.kra")
    export.add_argument("--layers-png", dest="layers_png", action="store_true", help="write per-layer RGBA PNGs")
    export.add_argument("--resolution", type=int, default=256, help="long-side resolution (16..512, default 256)")
    export.add_argument("--quality", choices=("fast", "exact"), default="fast", help="render quality (default fast)")
    export.add_argument("--name", help="output basename (default: input stem)")
    export.add_argument("--json", action="store_true", help="print machine-readable JSON")
    export.set_defaults(func=_cmd_export)

    inspect = sub.add_parser("inspect-layers", help="print the layer table without rendering")
    inspect.add_argument("--fdraw", required=True, help="input .fdraw container")
    inspect.add_argument("--json", action="store_true", help="print machine-readable JSON")
    inspect.set_defaults(func=_cmd_inspect_layers)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help / usage errors
        return int(exc.code or 0)
    try:
        return int(args.func(args))
    except (LayerError, FDrawError) as exc:
        code = getattr(exc, "code", "FP_E_LAYER")
        message = getattr(exc, "message", str(exc))
        print(f"error[{code}]: {message}", file=sys.stderr)
        if isinstance(getattr(exc, "evidence", None), dict) and exc.evidence.get("errors"):
            for item in exc.evidence["errors"]:
                print(f"  - {item}", file=sys.stderr)
        return EXIT_FAIL
    except FileNotFoundError as exc:
        print(f"error[FP_E_LAYER_IO]: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except OSError as exc:
        print(f"error[FP_E_LAYER_IO]: {exc}", file=sys.stderr)
        return EXIT_INTERNAL
    except Exception as exc:  # pragma: no cover - defensive
        print(f"error[FP_E_LAYER_INTERNAL]: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
