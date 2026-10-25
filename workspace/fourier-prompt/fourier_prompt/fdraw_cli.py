"""Standalone CLI for the ``.fdraw`` container (``python -m fourier_prompt.fdraw_cli``).

Deliberately independent from :mod:`fourier_prompt.cli` (WP-D owns that file):

    pack     --fbs X [--source-plan P] --out Y.fdraw
    unpack   --fdraw Y [--out-dir D]
    inspect  --fdraw Y [--json]
    validate --fdraw Y [--json]

Exit codes: 0 ok, 1 container/validation failure, 2 usage (argparse), 3 IO/internal.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .fdraw import (
    FDRAW_VERSION,
    FDrawError,
    inspect as inspect_container,
    pack as pack_container,
    unpack as unpack_container,
    validate as validate_container,
)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_INTERNAL = 3


def _load_json(path: str, what: str):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except OSError as exc:
        raise FDrawError("FD_E_INPUT", f"无法读取{what}: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise FDrawError("FD_E_JSON", f"{what} JSON 解析失败: {path}: {exc}") from exc


def _cmd_pack(args: argparse.Namespace) -> int:
    fbs = _load_json(args.fbs, ".fbs")
    source = _load_json(args.source_plan, "source plan") if args.source_plan else None
    meta = _load_json(args.meta, "meta") if args.meta else None
    blob = pack_container(fbs, source_fdsl=source, meta=meta, out=args.out)
    info = inspect_container(blob)
    print(
        f"packed {args.out} ({len(blob)} bytes, "
        f"root_sha256={info['root_sha256']}, "
        f"elements={info['render']['element_count']}, "
        f"source={'yes' if info['has_source'] else 'no'})"
    )
    return EXIT_OK


def _cmd_unpack(args: argparse.Namespace) -> int:
    result = unpack_container(args.fdraw, out_dir=args.out_dir)
    render = result["render"]
    print(
        f"unpacked {args.fdraw}: schema={result['meta'].get('schema')} "
        f"elements={len(render.get('strokes') or [])} "
        f"fonts={len(result['fonts'])} "
        f"source={'yes' if result['source_fdsl'] is not None else 'no'}"
    )
    print(f"root_sha256={result['root_sha256']} (verified)")
    for path in result.get("written") or []:
        print(f"  wrote {path}")
    return EXIT_OK


def _cmd_inspect(args: argparse.Namespace) -> int:
    info = inspect_container(args.fdraw)
    if args.json:
        print(json.dumps(info, ensure_ascii=False, sort_keys=True, indent=2))
        return EXIT_OK
    render = info["render"]
    print(f"file: {args.fdraw}")
    print(f"schema: {info['schema']} (spec {info['spec_version']})")
    print(
        f"zip: {info['zip_bytes']} bytes / uncompressed {info['uncompressed_bytes']} "
        f"bytes / {info['entry_count']} entries"
    )
    print(
        f"render: {render['format']} v{render['version']} mode={render['mode']} "
        f"canvas={render['canvas']['width']}x{render['canvas']['height']} "
        f"R={render['target_resolution']}"
    )
    print(
        f"elements: {render['element_count']} "
        f"(strokes={render['stroke_count']}, fills={render['fill_count']}, "
        f"texts={render['text_count']}), layers={render['layers']}"
    )
    print(
        f"width_scale: {render['width_scale']} "
        f"({'pinned' if render['width_scale_pinned'] else 'not pinned'})"
    )
    print(
        f"source: {'yes (' + str(info['source_schema']) + ')' if info['has_source'] else 'no'}"
    )
    print(f"fonts: {info['font_count']} ({sum(f['bytes'] for f in info['fonts'])} bytes)")
    print(
        f"root_sha256: {info['root_sha256']} "
        f"({'ok' if info['root_sha256_ok'] else 'MISMATCH'})"
    )
    caps = info["capabilities"] or {}
    print(f"capabilities.required: {caps.get('required')}")
    print(f"capabilities.optional: {caps.get('optional')}")
    return EXIT_OK


def _cmd_validate(args: argparse.Namespace) -> int:
    errors = validate_container(args.fdraw)
    if args.json:
        print(
            json.dumps(
                {"ok": not errors, "errors": errors},
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
    elif errors:
        for message in errors:
            print(f"INVALID: {message}", file=sys.stderr)
    else:
        print(f"OK: {args.fdraw} is a valid {FDRAW_VERSION} container")
    return EXIT_OK if not errors else EXIT_FAIL


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m fourier_prompt.fdraw_cli",
        description=f"Pack/unpack/inspect/validate .fdraw containers ({FDRAW_VERSION}).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    pack = sub.add_parser("pack", help="pack a .fbs into a .fdraw")
    pack.add_argument("--fbs", required=True, help="input .fbs JSON file")
    pack.add_argument("--source-plan", help="optional source FDSL plan JSON file")
    pack.add_argument("--meta", help="optional meta JSON file with user metadata")
    pack.add_argument("--out", required=True, help="output .fdraw path")
    pack.set_defaults(func=_cmd_pack)

    unpack = sub.add_parser("unpack", help="unpack and verify a .fdraw")
    unpack.add_argument("--fdraw", required=True, help="input .fdraw file")
    unpack.add_argument("--out-dir", help="extract whitelisted entries into this directory")
    unpack.set_defaults(func=_cmd_unpack)

    inspect = sub.add_parser("inspect", help="print container metadata without rendering")
    inspect.add_argument("--fdraw", required=True, help="input .fdraw file")
    inspect.add_argument("--json", action="store_true", help="print machine-readable JSON")
    inspect.set_defaults(func=_cmd_inspect)

    validate = sub.add_parser("validate", help="validate a .fdraw container")
    validate.add_argument("--fdraw", required=True, help="input .fdraw file")
    validate.add_argument("--json", action="store_true", help="print machine-readable JSON")
    validate.set_defaults(func=_cmd_validate)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except FDrawError as exc:
        print(f"error[{exc.code}]: {exc.message}", file=sys.stderr)
        if exc.evidence.get("errors"):
            for message in exc.evidence["errors"]:
                print(f"  - {message}", file=sys.stderr)
        return EXIT_FAIL
    except FileNotFoundError as exc:
        print(f"error[FD_E_INPUT]: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except OSError as exc:
        print(f"error[FD_E_IO]: {exc}", file=sys.stderr)
        return EXIT_INTERNAL
    except Exception as exc:  # pragma: no cover - defensive
        print(f"error[FD_E_INTERNAL]: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
