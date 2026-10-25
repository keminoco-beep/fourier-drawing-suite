"""Command line interface for fourier-prompt.

Subcommands::

    python -m fourier_prompt.cli schema --plan P.json [--strict] [--json]
    python -m fourier_prompt.cli selftest
    python -m fourier_prompt.cli version
    python -m fourier_prompt.cli plan2fbs --plan P.json --out X.fbs [--resolution 256|512]
    python -m fourier_prompt.cli draw --plan P.json --out X.png [--resolution 256|512]
    python -m fourier_prompt.cli --help

``plan2fbs`` / ``draw`` auto-detect early ``fdsl/2`` drafts and convert them via
``fourier_prompt.legacy``; pass ``--legacy`` to force conversion.
``pack`` / ``eval`` remain reserved for later batches and fail loudly.

Exit codes: 0 success, 1 validation/compile failure, 2 usage/input error,
3 internal error, 130 interrupted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import traceback

from . import __version__
from .repair import repair_plan
from .schema import (
    FDSL_SCHEMA,
    FDSL_SCHEMA_ALL,
    FDSchemaError,
    load_plan,
    validate_plan,
    validate_plan_detailed,
)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_INTERNAL = 3
EXIT_INTERRUPT = 130

#: Errors which mean "the input could not be read/parsed" (usage class).
INPUT_ERROR_CODES = {"FP_E_LOAD", "FP_E_JSON", "FP_E_PLAN_TYPE", "FP_E_USAGE"}

RESERVED_COMMANDS = {
    "pack": "the .fdraw container is a separate WP-E CLI (fourier_prompt.fdraw_cli)",
    "eval": "the evaluation harness is planned for batch 3 (see KICKOFF_v2.md section 6)",
}

MAX_CLI_RESOLUTION = 512


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="fourier-prompt",
        description="FDSL v2.1 plan tooling (validate / repair / compile).",
        epilog="Exit codes: 0 ok, 1 validation failure, 2 usage/input error, "
        "3 internal error, 130 interrupted.",
    )
    parser.add_argument(
        "--version", action="version", version=f"fourier-prompt {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    schema = subparsers.add_parser(
        "schema", help="validate an FDSL plan (JSON)", description="Validate an FDSL v2.1 plan."
    )
    schema.add_argument("--plan", required=True, metavar="P.json", help="path to the FDSL plan")
    schema.add_argument(
        "--strict",
        action="store_true",
        help="treat warnings as errors (unknown fields, non-builtin fonts, ...)",
    )
    schema.add_argument("--json", action="store_true", help="emit a JSON report")

    subparsers.add_parser(
        "selftest", help="run built-in validation/repair checks", description="Run built-in checks."
    )
    subparsers.add_parser("version", help="print version and supported schema", description="Print version.")

    def _add_compile_arguments(command):
        command.add_argument("--plan", required=True, metavar="P.json", help="path to the FDSL plan")
        command.add_argument(
            "--resolution",
            type=int,
            default=256,
            metavar="R",
            help=f"render contract resolution in [16, {MAX_CLI_RESOLUTION}] (default: 256)",
        )
        command.add_argument(
            "--legacy",
            action="store_true",
            help="force early fdsl/2 → fdsl/2.1 conversion (auto-detected by default)",
        )
        command.add_argument("--json", action="store_true", help="emit a JSON report")
        command.add_argument("--quiet", action="store_true", help="suppress the human summary")

    plan2fbs = subparsers.add_parser(
        "plan2fbs",
        help="compile an FDSL plan to a .fbs document",
        description="Compile FDSL → skill document → validated .fbs JSON.",
    )
    _add_compile_arguments(plan2fbs)
    plan2fbs.add_argument("--out", required=True, metavar="X.fbs", help="output .fbs path")

    draw = subparsers.add_parser(
        "draw",
        help="compile an FDSL plan and render it to PNG",
        description="Compile FDSL → skill document → render PNG.",
    )
    _add_compile_arguments(draw)
    draw.add_argument("--out", required=True, metavar="X.png", help="output PNG path")
    draw.add_argument(
        "--supersample",
        type=int,
        default=2,
        metavar="S",
        help="render supersampling factor (default: 2)",
    )
    draw.add_argument(
        "--fbs-out", default=None, metavar="X.fbs", help="also write the intermediate .fbs document"
    )

    for name, hint in RESERVED_COMMANDS.items():
        reserved = subparsers.add_parser(
            name,
            help=f"(not implemented) {hint}",
            description=f"Not implemented: {hint}",
        )
        reserved.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
        reserved.set_defaults(reserved_hint=hint)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv=None):
    """Run the CLI and return an exit code (never raises)."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help / --version / usage errors
        return int(exc.code or 0)

    command = getattr(args, "command", None)
    if command is None:
        parser.print_help()
        return EXIT_USAGE

    try:
        if command == "schema":
            return _cmd_schema(args)
        if command == "selftest":
            return _cmd_selftest()
        if command == "version":
            return _cmd_version()
        if command == "plan2fbs":
            return _cmd_plan2fbs(args)
        if command == "draw":
            return _cmd_draw(args)
        if command in RESERVED_COMMANDS:
            raise NotImplementedError(
                f"subcommand {command!r} is not implemented in fourier-prompt "
                f"{__version__}: {RESERVED_COMMANDS[command]}"
            )
        parser.error(f"unknown command {command!r}")
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("fourier-prompt: interrupted", file=sys.stderr)
        return EXIT_INTERRUPT
    except NotImplementedError as exc:
        print(f"NotImplementedError: {exc}", file=sys.stderr)
        return EXIT_INTERNAL
    except FDSchemaError as exc:
        code = EXIT_USAGE if exc.code in INPUT_ERROR_CODES else EXIT_FAIL
        if command == "schema" and getattr(args, "json", False):
            _print_json(
                {
                    "ok": False,
                    "command": "schema",
                    "plan": getattr(args, "plan", None),
                    "schema": None,
                    "counts": {},
                    "errors": [exc.to_dict()],
                    "warnings": [],
                }
            )
        elif command in ("plan2fbs", "draw") and getattr(args, "json", False):
            _print_json(
                {
                    "ok": False,
                    "command": command,
                    "plan": getattr(args, "plan", None),
                    "errors": [exc.to_dict()],
                    "warnings": [],
                }
            )
        else:
            print(f"{exc.code}: {exc.path}: {exc.message}", file=sys.stderr)
        return code
    except OSError as exc:
        print(f"fourier-prompt: I/O error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Exception:  # pragma: no cover - defensive
        traceback.print_exc()
        return EXIT_INTERNAL


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _cmd_schema(args):
    plan = load_plan(args.plan)
    report = validate_plan_detailed(plan, strict=args.strict)
    counts = _plan_counts(plan)

    if args.json:
        _print_json(
            {
                "ok": report["ok"],
                "command": "schema",
                "plan": str(args.plan),
                "schema": report["schema"],
                "strict": bool(args.strict),
                "counts": counts,
                "errors": report["errors"],
                "warnings": report["warnings"],
            }
        )
    else:
        print(f"plan: {args.plan}")
        print(f"schema: {report['schema']}")
        print(
            "counts: "
            f"elements={counts['elements']} layers={counts['layers']} texts={counts['texts']}"
        )
        print(f"strict: {'yes' if args.strict else 'no'}")
        print(f"valid: {'yes' if report['ok'] else 'no'}")
        for issue in report["errors"]:
            promoted = " (promoted warning)" if issue.get("promoted") else ""
            print(
                f"  ERROR {issue['code']} at {issue['path']}: {issue['message']}{promoted}"
            )
        for issue in report["warnings"]:
            print(f"  WARN  {issue['code']} at {issue['path']}: {issue['message']}")
        print(
            f"result: {len(report['errors'])} error(s), {len(report['warnings'])} warning(s)"
        )

    return EXIT_OK if report["ok"] else EXIT_FAIL


def _cmd_version():
    print(f"fourier-prompt {__version__}")
    print(f"fdsl: {FDSL_SCHEMA}")
    print(f"accepted schemas: {', '.join(FDSL_SCHEMA_ALL)}")
    print(f"python: {sys.version.split()[0]}")
    return EXIT_OK


def _cmd_selftest():
    checks = []

    valid_plan = _selftest_plan()
    errors = validate_plan(valid_plan)
    checks.append(("valid plan passes", not errors, "; ".join(errors)))

    bad_schema = dict(valid_plan, schema="fdsl/9")
    errors = validate_plan(bad_schema)
    checks.append(
        (
            "unknown schema rejected",
            any(item.startswith("FP_E_SCHEMA_VERSION") for item in errors),
            "; ".join(errors),
        )
    )

    bad_gen = _selftest_plan()
    bad_gen["elements"][1]["gen"] = "unicorn"
    errors = validate_plan(bad_gen)
    checks.append(
        (
            "unknown gen rejected",
            any(item.startswith("FP_E_GEN_UNKNOWN") for item in errors),
            "; ".join(errors),
        )
    )

    repairable = _selftest_plan()
    repairable["elements"][2]["points"].append([9999, -5])
    repaired, warnings = repair_plan(repairable)
    checks.append(
        (
            "repair clips out-of-canvas points",
            bool(warnings)
            and all(p[0] <= repaired["canvas"]["width"] for p in repaired["elements"][2]["points"]),
            f"warnings={[w['code'] for w in warnings]}",
        )
    )

    repaired_again, warnings_again = repair_plan(repaired)
    checks.append(
        (
            "repair is deterministic/idempotent",
            repaired_again == repaired and warnings_again == [],
            f"second warnings={[w['code'] for w in warnings_again]}",
        )
    )

    warning_plan = _selftest_plan()
    warning_plan["future_field"] = True
    strict_report = validate_plan_detailed(warning_plan, strict=True)
    checks.append(
        (
            "strict mode promotes warnings to errors",
            not strict_report["ok"]
            and any(item["code"] == "FP_W_UNKNOWN_FIELD" for item in strict_report["errors"]),
            f"errors={[e['code'] for e in strict_report['errors']]}",
        )
    )

    failed = [name for name, ok, _detail in checks if not ok]
    for name, ok, detail in checks:
        print(f"[{'ok' if ok else 'FAIL'}] {name}" + (f" — {detail}" if not ok else ""))
    print(f"selftest: {len(checks) - len(failed)}/{len(checks)} checks passed")
    return EXIT_OK if not failed else EXIT_FAIL


# ---------------------------------------------------------------------------
# Compile / render commands (WP-D)
# ---------------------------------------------------------------------------


def _check_render_args(args):
    resolution = getattr(args, "resolution", 256)
    if isinstance(resolution, bool) or not 16 <= int(resolution) <= MAX_CLI_RESOLUTION:
        raise FDSchemaError(
            "FP_E_USAGE",
            f"resolution must be in [16, {MAX_CLI_RESOLUTION}] (got {resolution!r})",
            "--resolution",
        )
    supersample = getattr(args, "supersample", 2)
    if isinstance(supersample, bool) or not 1 <= int(supersample) <= 4:
        raise FDSchemaError(
            "FP_E_USAGE", f"supersample must be in [1, 4] (got {supersample!r})", "--supersample"
        )


def _run_compile(args):
    from .compile import compile_plan

    plan = load_plan(args.plan)
    force = True if getattr(args, "legacy", False) else None
    return compile_plan(plan, resolution=int(args.resolution), legacy=force)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def _write_png(rgb, path: str) -> int:
    from PIL import Image

    _ensure_parent(path)
    Image.fromarray(rgb, mode="RGB").save(path, format="PNG")
    return int(os.path.getsize(path))


def _summary_lines(command: str, args, result: dict, extra: dict) -> list[str]:
    stats = result["stats"]
    lines = [
        f"plan: {args.plan}",
        f"compile: elements={stats['elements']} records={stats['records']} "
        f"fill={stats['fill']} stroke={stats['stroke']} styles={stats['styles']} "
        f"legacy={'yes' if stats['legacy'] else 'no'}",
        f"warnings: {stats['warnings']}",
    ]
    codes = sorted({item["code"] for item in result["warnings"]})
    if codes:
        lines.append("  " + ", ".join(codes))
    for key, value in extra.items():
        lines.append(f"{key}: {value}")
    return lines


def _emit_compile_report(command: str, args, result: dict, extra: dict, exit_code: int) -> int:
    payload = {
        "ok": True,
        "command": command,
        "plan": os.path.abspath(args.plan),
        "resolution": int(args.resolution),
        "stats": result["stats"],
        "warnings": result["warnings"],
        "ledger": result["ledger"],
        "out": result["stats"].get("out"),
    }
    if getattr(args, "json", False):
        _print_json(payload)
    elif not getattr(args, "quiet", False):
        for line in _summary_lines(command, args, result, extra):
            print(line)
    return exit_code


def _cmd_plan2fbs(args):
    from .compat import skill_api
    from .compile import normalized_fbs

    _check_render_args(args)
    result = _run_compile(args)
    data = skill_api.write_fbs(result["fbs"], args.out)
    result["stats"]["fbs_bytes"] = len(data)
    result["stats"]["out"] = os.path.abspath(args.out)
    result["stats"]["fbs_sha256"] = _sha256(normalized_fbs(result["fbs"]).encode("utf-8"))
    extra = {
        "out": os.path.abspath(args.out),
        "fbs": f"{os.path.abspath(args.out)} ({len(data) / 1024:.1f} KiB)",
        "resolution": int(args.resolution),
        "compile_seconds": result["stats"]["compile_seconds"],
    }
    return _emit_compile_report("plan2fbs", args, result, extra, EXIT_OK)


def _cmd_draw(args):
    from .compat import skill_api
    from .compile import normalized_fbs

    _check_render_args(args)
    result = _run_compile(args)
    if getattr(args, "fbs_out", None):
        skill_api.write_fbs(result["fbs"], args.fbs_out)
    rgb = skill_api.render(
        result["fbs"],
        resolution=int(args.resolution),
        supersample=int(args.supersample),
    )
    png_bytes = _write_png(rgb, args.out)
    stats = result["stats"]
    stats["png_bytes"] = png_bytes
    stats["png_sha256"] = _sha256(rgb.tobytes())
    stats["out"] = os.path.abspath(args.out)
    stats["supersample"] = int(args.supersample)
    stats["fbs_sha256"] = _sha256(normalized_fbs(result["fbs"]).encode("utf-8"))
    extra = {
        "out": os.path.abspath(args.out),
        "png": f"{os.path.abspath(args.out)} ({png_bytes / 1024:.1f} KiB)",
        "pixels_sha256": stats["png_sha256"],
        "resolution": int(args.resolution),
        "compile_seconds": stats["compile_seconds"],
    }
    return _emit_compile_report("draw", args, result, extra, EXIT_OK)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _print_json(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))


def _plan_counts(plan):
    def _length(value):
        return len(value) if isinstance(value, list) else 0

    return {
        "elements": _length(plan.get("elements")),
        "layers": _length(plan.get("layers")),
        "texts": _length(plan.get("texts")),
    }


def _selftest_plan():
    """Small but complete FDSL v2.1 plan used by ``selftest``."""
    return {
        "schema": FDSL_SCHEMA,
        "name": "selftest",
        "canvas": {"width": 256, "height": 256, "background": [255, 255, 255]},
        "palette": {"ink": "#12203a", "gold": "#e8c37a"},
        "layers": [
            {"id": 0, "name": "base"},
            {"id": 1, "name": "ink"},
        ],
        "elements": [
            {
                "kind": "fill",
                "layer": 0,
                "color": "ink",
                "poly": [[8, 8], [248, 8], [248, 248], [8, 248]],
            },
            {
                "kind": "fourier_gen",
                "gen": "circle",
                "layer": 1,
                "id": "e1",
                "tags": ["selftest", "shape"],
                "transform": {"center": [128, 128], "scale": 0.5},
                "params": {"r": 1.0},
                "brush": {"preset": "ink_fineliner", "width": 2.0, "color": "gold", "alpha": 0.9},
            },
            {
                "kind": "stroke",
                "layer": 1,
                "points": [[16, 240], [240, 16]],
                "brush": {"preset": "ink_gpen", "width": 1.5, "alpha": 0.8},
            },
        ],
        "constraints": {"max_elements": 400, "single_line": False},
    }


if __name__ == "__main__":
    sys.exit(main())
