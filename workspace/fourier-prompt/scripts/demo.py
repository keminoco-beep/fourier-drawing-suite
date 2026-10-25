#!/usr/bin/env python3
"""One-click FDSL demo: plan -> .fbs -> PNG -> .fdraw -> (PSD/KRA) -> (video).

The demo is intentionally thin: it only *orchestrates* the public CLIs/APIs of
``fourier-prompt`` (schema validation, ``compile_plan``, the skill's renderer,
``fdraw.pack`` and the ``layer_cli`` / ``fdvideo_cli`` subprocesses).  It never
re-implements compilation, Fourier fitting, rendering, containers or exports.

Determinism
-----------
The skill writer stamps ``created`` with wall-clock time.  For byte-identical
repeat runs the demo removes that metadata-only field before writing the
``.fbs`` (``compile.normalized_fbs`` already excludes it from its own hash) and
uses the same source plan / fixed ZIP timestamp for the ``.fdraw``.  Rendering
and container packing are deterministic, so the same plan + resolution yields
identical ``.fbs`` / PNG / ``.fdraw`` bytes.

Usage::

    python scripts/demo.py --plan skill/examples/text_plan.json \
        --outdir out/demo [--psd] [--kra] [--video] [--resolution 256]

Exit codes: 0 ok, 1 validation/compile/render/export failure, 2 usage or input
error, 3 internal error, 130 interrupted.

This file is WP-M output; it must not modify the core package.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parents[1]
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_INTERNAL = 3
EXIT_INTERRUPT = 130

MIN_RESOLUTION = 16
MAX_RESOLUTION = 256
DEFAULT_RESOLUTION = 256


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024.0:.1f} KiB"
    return f"{num_bytes / (1024.0 * 1024.0):.2f} MiB"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rel(path: Path) -> str:
    """Prefer a workspace-relative path in the log; fall back to absolute."""
    try:
        return str(path.resolve().relative_to(PKG_ROOT))
    except ValueError:
        return str(path.resolve())


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="python scripts/demo.py",
        description=(
            "FDSL plan -> .fbs -> PNG -> .fdraw one-click demo "
            "(optional layered PSD/KRA and progressive video)."
        ),
        epilog="Exit codes: 0 ok, 1 step failure, 2 usage/input error, 3 internal.",
    )
    parser.add_argument("--plan", required=True, metavar="P.json", help="FDSL v2.1 plan file")
    parser.add_argument(
        "--outdir", required=True, metavar="DIR", help="output directory (created if missing)"
    )
    parser.add_argument(
        "--resolution",
        type=int,
        default=DEFAULT_RESOLUTION,
        metavar="R",
        help=f"long-side render resolution in [{MIN_RESOLUTION}, {MAX_RESOLUTION}] "
        f"(default {DEFAULT_RESOLUTION})",
    )
    parser.add_argument("--psd", action="store_true", help="export a layered PSD")
    parser.add_argument("--kra", action="store_true", help="export a layered KRA")
    parser.add_argument(
        "--video",
        action="store_true",
        help="encode a progressive reveal mp4 (requires imageio + imageio-ffmpeg)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=20.0,
        help="video duration in seconds when --video is given (default 20)",
    )
    parser.add_argument(
        "--fps", type=int, default=24, help="video frame rate when --video is given (default 24)"
    )
    return parser.parse_args(argv)


def _fail(step: str, code: str, message: str, exit_code: int, started: float) -> int:
    print(f"error[{code}]: {message}", file=sys.stderr)
    print(f"demo: FAILED at {step} after {time.perf_counter() - started:.2f}s", file=sys.stderr)
    return exit_code


def _child_env() -> dict:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    parts = [str(PKG_ROOT)] + ([existing] if existing else [])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def _run_child(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(PKG_ROOT),
        env=_child_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _child_error(result: subprocess.CompletedProcess) -> str:
    text = (result.stderr or "").strip() or (result.stdout or "").strip()
    if not text:
        return f"child exited with code {result.returncode}"
    if "error[" in text:
        return text.splitlines()[0]
    return " | ".join(text.splitlines()[:3])


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------


def _step_schema(plan_path: Path, root: float) -> tuple[int, dict | None]:
    from fourier_prompt.schema import FDSchemaError, load_plan, validate_plan_detailed

    began = time.perf_counter()
    try:
        plan = load_plan(plan_path)
        report = validate_plan_detailed(plan, strict=False)
    except FDSchemaError as exc:
        code = "FP_E_USAGE" if exc.code in ("FP_E_LOAD", "FP_E_JSON", "FP_E_PLAN_TYPE") else exc.code
        kind = EXIT_USAGE if code == "FP_E_USAGE" else EXIT_FAIL
        return _fail("schema", exc.code, f"{exc.path}: {exc.message}", kind, root), None
    elapsed = time.perf_counter() - began
    if not report["ok"]:
        first = report["errors"][0]
        for issue in report["errors"][:5]:
            print(
                f"  {issue['code']} at {issue['path']}: {issue['message']}", file=sys.stderr
            )
        return (
            _fail(
                "schema",
                first["code"],
                f"{first['path']}: {first['message']} "
                f"({len(report['errors'])} error(s))",
                EXIT_FAIL,
                root,
            ),
            None,
        )
    counts = {
        "elements": len(plan.get("elements") or []),
        "layers": len(plan.get("layers") or []),
        "texts": len(plan.get("texts") or []),
    }
    print(
        f"[1/6] schema       ok  elements={counts['elements']} layers={counts['layers']} "
        f"texts={counts['texts']} warnings={len(report['warnings'])} ({elapsed:.3f}s)"
    )
    return EXIT_OK, plan


def _step_plan2fbs(plan: dict, outdir: Path, stem: str, resolution: int, root: float):
    from fourier_prompt.compile import CompileError, compile_plan
    from fourier_prompt.compat import skill_api

    began = time.perf_counter()
    try:
        result = compile_plan(plan, resolution=resolution)
        doc = result["fbs"]
        # ``created`` is wall-clock metadata only; dropping it keeps repeat runs
        # byte-identical (same reason compile.normalized_fbs excludes it).
        doc.pop("created", None)
        fbs_path = outdir / f"{stem}.fbs"
        data = skill_api.write_fbs(doc, fbs_path)
        with open(fbs_path, "r", encoding="utf-8") as handle:
            fbs_doc = json.load(handle)
    except CompileError as exc:
        return _fail("plan2fbs", exc.code, f"{exc.path}: {exc.message}", EXIT_FAIL, root), None, None
    except Exception as exc:  # noqa: BLE001 - structured skill errors are opaque here
        if skill_api.is_skill_error(exc):
            return (
                _fail("plan2fbs", "FP_E_COMPILE_SKILL", f"{type(exc).__name__}: {exc}", EXIT_FAIL, root),
                None,
                None,
            )
        raise
    elapsed = time.perf_counter() - began
    stats = result["stats"]
    digests = ", ".join(sorted({item["code"] for item in result["warnings"]}))
    print(
        f"[2/6] plan2fbs     ok  {_rel(fbs_path)} {_size(len(data))} "
        f"sha256={hashlib.sha256(data).hexdigest()[:16]} "
        f"records={stats['records']} fills={stats['fill']} strokes={stats['stroke']} "
        f"text={stats['text']} ({elapsed:.3f}s)"
    )
    if digests:
        print(f"      warnings: {digests}")
    return EXIT_OK, fbs_doc, fbs_path


def _step_draw(fbs_doc: dict, outdir: Path, stem: str, resolution: int, root: float):
    from PIL import Image

    from fourier_prompt.compat import skill_api

    began = time.perf_counter()
    png_path = outdir / f"{stem}_R{resolution}.png"
    try:
        rgb = skill_api.render(fbs_doc, resolution=resolution, supersample=2)
        Image.fromarray(rgb, mode="RGB").save(png_path, format="PNG")
    except Exception as exc:  # noqa: BLE001 - renderer failures must fail the step
        if skill_api.is_skill_error(exc):
            return _fail("draw", "FP_E_RENDER", f"{type(exc).__name__}: {exc}", EXIT_FAIL, root), None
        if isinstance(exc, OSError):
            return _fail("draw", "FP_E_IO", str(exc), EXIT_FAIL, root), None
        raise
    elapsed = time.perf_counter() - began
    size = png_path.stat().st_size
    print(
        f"[3/6] draw         ok  {_rel(png_path)} {_size(size)} "
        f"{rgb.shape[1]}x{rgb.shape[0]} sha256={_sha256_file(png_path)[:16]} ({elapsed:.3f}s)"
    )
    return EXIT_OK, png_path


def _step_pack(fbs_doc: dict, plan: dict, outdir: Path, stem: str, root: float):
    from fourier_prompt import fdraw

    began = time.perf_counter()
    fdraw_path = outdir / f"{stem}.fdraw"
    try:
        blob = fdraw.pack(
            fbs_doc,
            source_fdsl=plan,
            meta={"title": str(plan.get("name") or stem)},
            out=str(fdraw_path),
        )
        info = fdraw.inspect(str(fdraw_path))
    except fdraw.FDrawError as exc:
        return _fail("fdraw pack", exc.code, exc.message, EXIT_FAIL, root), None
    elapsed = time.perf_counter() - began
    print(
        f"[4/6] fdraw pack   ok  {_rel(fdraw_path)} {_size(len(blob))} "
        f"root_sha256={info['root_sha256'][:16]} "
        f"elements={info['render']['element_count']} layers={info['render']['layers']} "
        f"({elapsed:.3f}s)"
    )
    return EXIT_OK, fdraw_path


def _step_layer_export(
    fdraw_path: Path, outdir: Path, resolution: int, psd: bool, kra: bool, root: float
):
    began = time.perf_counter()
    layers_dir = outdir / "layers"
    cmd = [
        sys.executable,
        "-m",
        "fourier_prompt.layer_cli",
        "export",
        "--fdraw",
        str(fdraw_path),
        "--out-dir",
        str(layers_dir),
        "--resolution",
        str(resolution),
        "--quality",
        "fast",
        "--json",
    ]
    if psd:
        cmd.append("--psd")
    if kra:
        cmd.append("--kra")
    result = _run_child(cmd)
    if result.returncode != 0:
        print(f"  child: {' '.join(cmd)}", file=sys.stderr)
        print(_child_error(result), file=sys.stderr)
        return (
            _fail(
                "layer export",
                "FP_E_LAYER_EXPORT",
                f"layer_cli exited with code {result.returncode}",
                result.returncode,
                root,
            ),
            None,
        )
    try:
        payload = json.loads(result.stdout)
        outputs = payload.get("outputs") or {}
        parts = []
        for kind in ("psd", "kra"):
            entry = outputs.get(kind)
            if isinstance(entry, dict):
                parts.append(f"{kind}={_rel(Path(entry['out']))} {_size(entry['bytes'])}")
        if not parts:  # pragma: no cover - layer_cli contract violation
            return (
                _fail(
                    "layer export",
                    "FP_E_LAYER_EXPORT",
                    "layer_cli returned no psd/kra output",
                    EXIT_FAIL,
                    root,
                ),
                None,
            )
        elapsed = time.perf_counter() - began
        print(
            f"[5/6] layer export ok  {'; '.join(parts)} "
            f"layers={payload.get('layer_count')} ({elapsed:.3f}s)"
        )
        return EXIT_OK, outputs
    except (json.JSONDecodeError, KeyError) as exc:
        return (
            _fail("layer export", "FP_E_LAYER_EXPORT", f"bad --json output: {exc}", EXIT_FAIL, root),
            None,
        )


def _step_video(
    fdraw_path: Path, outdir: Path, stem: str, resolution: int, duration: float, fps: int, root: float
):
    began = time.perf_counter()
    video_path = outdir / f"{stem}_reveal_R{resolution}.mp4"
    cmd = [
        sys.executable,
        "-m",
        "fourier_prompt.fdvideo_cli",
        "video",
        "--fdraw",
        str(fdraw_path),
        "--out",
        str(video_path),
        "--resolution",
        str(resolution),
        "--duration",
        str(duration),
        "--fps",
        str(fps),
        "--quality",
        "fast",
        "--json",
    ]
    result = _run_child(cmd)
    if result.returncode != 0:
        print(f"  child: {' '.join(cmd)}", file=sys.stderr)
        print(_child_error(result), file=sys.stderr)
        return (
            _fail(
                "video",
                "FP_E_VIDEO",
                f"fdvideo_cli exited with code {result.returncode}",
                result.returncode,
                root,
            ),
            None,
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return _fail("video", "FP_E_VIDEO", f"bad --json output: {exc}", EXIT_FAIL, root), None
    elapsed = time.perf_counter() - began
    print(
        f"[6/6] video        ok  {_rel(Path(payload['out']))} {_size(payload['bytes'])} "
        f"{payload['frames']} frames @{payload['fps']}fps "
        f"sha256={payload['sha256'][:16]} ({elapsed:.3f}s)"
    )
    return EXIT_OK, payload


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    args = _parse_args(argv)
    started = time.perf_counter()

    if not (MIN_RESOLUTION <= args.resolution <= MAX_RESOLUTION):
        return _fail(
            "arguments",
            "FP_E_USAGE",
            f"--resolution must be in [{MIN_RESOLUTION}, {MAX_RESOLUTION}] "
            f"(got {args.resolution})",
            EXIT_USAGE,
            started,
        )
    if args.duration <= 0:
        return _fail("arguments", "FP_E_USAGE", "--duration must be > 0", EXIT_USAGE, started)
    if not (1 <= args.fps <= 120):
        return _fail("arguments", "FP_E_USAGE", "--fps must be in [1, 120]", EXIT_USAGE, started)

    plan_path = Path(args.plan)
    if not plan_path.is_file():
        return _fail("schema", "FP_E_LOAD", f"plan not found: {plan_path}", EXIT_USAGE, started)
    # Resolve before any child process runs with PKG_ROOT as its working
    # directory; relative --plan/--outdir paths are otherwise misinterpreted.
    plan_path = plan_path.resolve()

    outdir = Path(args.outdir).resolve()
    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _fail("output", "FP_E_IO", f"cannot create --outdir: {exc}", EXIT_USAGE, started)
    stem = plan_path.stem or "plan"

    print("fourier-prompt demo")
    print(f"plan:       {plan_path.resolve()}")
    print(f"outdir:     {outdir.resolve()}")
    print(f"resolution: {args.resolution}")
    extras = [
        name
        for name, enabled in (("psd", args.psd), ("kra", args.kra), ("video", args.video))
        if enabled
    ]
    print(f"extras:     {', '.join(extras) if extras else '(none)'}")
    print("")

    try:
        rc, plan = _step_schema(plan_path, started)
        if rc != EXIT_OK:
            return rc
        rc, fbs_doc, _fbs_path = _step_plan2fbs(plan, outdir, stem, args.resolution, started)
        if rc != EXIT_OK:
            return rc
        rc, _png_path = _step_draw(fbs_doc, outdir, stem, args.resolution, started)
        if rc != EXIT_OK:
            return rc
        rc, fdraw_path = _step_pack(fbs_doc, plan, outdir, stem, started)
        if rc != EXIT_OK:
            return rc

        if args.psd or args.kra:
            rc, _outputs = _step_layer_export(
                fdraw_path, outdir, args.resolution, args.psd, args.kra, started
            )
            if rc != EXIT_OK:
                return rc
        else:
            print("[5/6] layer export skipped (pass --psd and/or --kra)")

        if args.video:
            rc, _video = _step_video(
                fdraw_path, outdir, stem, args.resolution, args.duration, args.fps, started
            )
            if rc != EXIT_OK:
                return rc
        else:
            print("[6/6] video        skipped (pass --video)")
    except KeyboardInterrupt:
        print("demo: interrupted", file=sys.stderr)
        return EXIT_INTERRUPT
    except Exception:  # noqa: BLE001 - last-resort internal error reporting
        traceback.print_exc()
        return _fail("internal", "FP_E_INTERNAL", "unexpected exception", EXIT_INTERNAL, started)

    total = time.perf_counter() - started
    print("")
    print(f"done: {_rel(outdir)} in {total:.2f}s")
    print(
        "determinism: repeated runs with the same plan/resolution produce identical "
        ".fbs / PNG / .fdraw bytes"
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
