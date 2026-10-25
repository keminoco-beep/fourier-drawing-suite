"""``python -m fourier_prompt.generate_cli`` 鈥?prompt 鈫?FDSL plan / PNG / .fdraw.

Examples
--------
::

    python -m fourier_prompt.generate_cli --prompt "鐢讳竴鍙粦鑹茬殑鐚紝绾跨椋庢牸锛屽眳涓? ^
        --out out/planner/cat.json --png out/planner/cat.png --resolution 256

    python -m fourier_prompt.generate_cli --benchmark assets/eval/prompts_20.jsonl ^
        --out-dir out/planner --resolution 256

Exit codes
----------
``0`` success (planner warnings do not fail the run);
``1`` compile / render / I/O failure;
``2`` usage error (argparse, missing ``--prompt``, bad ``--resolution``);
``3`` :class:`fourier_prompt.planner.PlannerError` (empty or unparseable prompt).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .planner import PlannerError, plan_hash, plan_prompt, plan_summary

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_PLANNER = 3

MAX_RESOLUTION = 512


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m fourier_prompt.generate_cli",
        description="Deterministic rule planner: natural-language prompt -> FDSL v2.1 -> PNG/.fdraw.",
        epilog=(
            "Exit codes: 0 ok, 1 compile/render/IO failure, 2 usage error, "
            "3 planner error (empty/unparseable prompt)."
        ),
    )
    parser.add_argument("--prompt", "-p", metavar="TEXT", help="natural-language prompt (zh/en)")
    parser.add_argument("--style", "-s", metavar="NAME", help="style pack override (e.g. mono_ink, 姘村ⅷ)")
    parser.add_argument("--seed", type=int, default=0, help="deterministic seed (default 0)")
    parser.add_argument("--out", metavar="PLAN.json", help="write the FDSL plan JSON")
    parser.add_argument("--png", metavar="OUT.png", help="render a PNG")
    parser.add_argument("--fdraw", metavar="OUT.fdraw", help="pack a portable .fdraw")
    parser.add_argument(
        "--resolution",
        type=int,
        default=256,
        help=f"render long side in px (16..{MAX_RESOLUTION}, default 256)",
    )
    parser.add_argument(
        "--benchmark",
        metavar="PROMPTS.jsonl",
        help="run every prompt in a JSONL file and write benchmark.json + contact sheet",
    )
    parser.add_argument(
        "--out-dir",
        default="out/planner",
        metavar="DIR",
        help="benchmark output directory (default out/planner)",
    )
    parser.add_argument("--json", action="store_true", help="emit a machine-readable JSON report")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:  # argparse already printed help / usage
        return int(exc.code) if exc.code is not None else EXIT_OK

    if not 16 <= int(args.resolution) <= MAX_RESOLUTION:
        print(
            f"error: --resolution must be in [16, {MAX_RESOLUTION}] (got {args.resolution})",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if args.benchmark:
        return _run_benchmark(args)
    if not args.prompt:
        parser.print_usage(sys.stderr)
        print("error: --prompt is required unless --benchmark is used", file=sys.stderr)
        return EXIT_USAGE

    started = time.perf_counter()
    try:
        plan, warnings = plan_prompt(args.prompt, style=args.style, seed=args.seed)
    except PlannerError as exc:
        payload = {"ok": False, "exit_code": EXIT_PLANNER, "error": exc.to_dict()}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"planner error {exc.code}: {exc.message}", file=sys.stderr)
        return EXIT_PLANNER

    try:
        outputs = _write_outputs(plan, args)
    except Exception as exc:  # noqa: BLE001 - contract: any backend failure -> exit 1
        payload = {
            "ok": False,
            "exit_code": EXIT_FAILURE,
            "error": {"code": getattr(exc, "code", "FP_E_GENERATE"), "message": str(exc)},
            "plan": plan_summary(plan),
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"generate failed: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    report = {
        "ok": True,
        "exit_code": EXIT_OK,
        "prompt": args.prompt,
        "style": plan["meta"]["style"],
        "seed": args.seed,
        "plan_hash": plan_hash(plan),
        "summary": plan_summary(plan),
        "warnings": warnings,
        "outputs": outputs,
        "elapsed_s": round(time.perf_counter() - started, 4),
        **{key: value for key, value in outputs.items() if key != "outputs"},
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        _print_human(report)
    return EXIT_OK


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)


def _write_json(path: str, payload) -> None:
    _ensure_parent(path)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1, allow_nan=False)
        handle.write("\n")


def _compile_plan(plan, resolution: int = 256):
    from .compile import compile_plan

    return compile_plan(plan, resolution=int(resolution))


def _write_outputs(plan, args) -> dict:
    outputs = {}
    compiled = None
    if args.out:
        _write_json(args.out, plan)
        outputs["plan"] = os.path.abspath(args.out)
    if args.png or args.fdraw:
        compiled = _compile_plan(plan, args.resolution)
        outputs["elements"] = int(compiled["stats"].get("elements", 0))
        outputs["compile_seconds"] = round(float(compiled["stats"].get("compile_seconds", 0.0)), 4)
    if args.png:
        from PIL import Image

        from .render_fast import render

        rgb = render(compiled["fbs"], resolution=int(args.resolution))
        _ensure_parent(args.png)
        Image.fromarray(rgb, mode="RGB").save(args.png, format="PNG")
        outputs["png"] = os.path.abspath(args.png)
        outputs["png_size"] = [int(rgb.shape[1]), int(rgb.shape[0])]
    if args.fdraw:
        from .fdraw import pack

        data = pack(
            compiled["fbs"],
            source_fdsl=plan,
            meta={
                "title": plan["name"],
                "generator": "rule_planner_v1",
                "style": plan["meta"]["style"],
                "seed": int(plan.get("seed", 0)),
            },
            out=args.fdraw,
        )
        outputs["fdraw"] = os.path.abspath(args.fdraw)
        outputs["fdraw_bytes"] = len(data)
    return outputs


def _print_human(report: dict) -> None:
    summary = report["summary"]
    print(f"prompt: {report['prompt']}")
    print(
        "plan: {name}  style={style}  subjects={subjects}  canvas={canvas}  "
        "elements={elements}  texts={texts}".format(
            name=summary["name"],
            style=summary["style"],
            subjects=",".join(summary["subjects"] or []),
            canvas="x".join(str(v) for v in summary["canvas"]),
            elements=summary["elements"],
            texts=summary["texts"],
        )
    )
    print(f"plan_hash: {report['plan_hash']}")
    if report["warnings"]:
        for warning in report["warnings"]:
            print(f"warning {warning['code']}: {warning['message']}")
    outputs = report.get("outputs") or {}
    for key in ("plan", "png", "fdraw"):
        if key in outputs:
            print(f"{key}: {outputs[key]}")
    print(f"elapsed: {report['elapsed_s']}s")


# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------


def _load_jsonl(path: str):
    items = []
    with open(path, "r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON ({exc})") from exc
            if not isinstance(item, dict):
                raise ValueError(f"{path}:{line_number}: each line must be a JSON object")
            items.append(item)
    return items


def _run_benchmark(args) -> int:
    from PIL import Image, ImageDraw

    out_dir = os.path.abspath(args.out_dir)
    png_dir = os.path.join(out_dir, "png")
    os.makedirs(png_dir, exist_ok=True)
    try:
        items = _load_jsonl(args.benchmark)
    except (OSError, ValueError) as exc:
        print(f"benchmark input error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if not items:
        print("benchmark input has no prompts", file=sys.stderr)
        return EXIT_USAGE

    rows = []
    thumbs = []
    for index, item in enumerate(items):
        item_id = str(item.get("id") or f"p{index + 1:02d}")
        prompt = item.get("prompt")
        row = {
            "id": item_id,
            "prompt": prompt,
            "style_arg": item.get("style"),
            "seed": int(item.get("seed", 0)),
        }
        started = time.perf_counter()
        png_path = os.path.join(png_dir, f"{item_id}.png")
        compiled = rendered = plan_ok = deterministic = False
        warnings = []
        try:
            plan, warnings = plan_prompt(prompt, style=item.get("style"), seed=int(item.get("seed", 0)))
            plan_ok = True
            hash_a = plan_hash(plan)
            plan_b, _warnings_b = plan_prompt(prompt, style=item.get("style"), seed=int(item.get("seed", 0)))
            deterministic = hash_a == plan_hash(plan_b)
            summary = plan_summary(plan)
            row.update(
                {
                    "detected_style": summary["style"],
                    "subjects": summary["subjects"],
                    "colors": summary["colors"],
                    "ratio": summary["ratio"],
                    "layout": summary["layout"],
                    "elements": summary["elements"],
                    "texts": summary["texts"],
                    "plan_hash": hash_a,
                }
            )
            result = _compile_plan(plan, args.resolution)
            compiled = True
            row["compiled_elements"] = int(result["stats"].get("elements", 0))
            row["compile_seconds"] = round(float(result["stats"].get("compile_seconds", 0.0)), 4)
            from .render_fast import render

            rgb = render(result["fbs"], resolution=int(args.resolution))
            rendered = True
            Image.fromarray(rgb, mode="RGB").save(png_path, format="PNG")
            thumbs.append((index, item_id, Image.fromarray(rgb, mode="RGB").convert("RGB"), summary))
            row["png"] = png_path
        except PlannerError as exc:
            row["error"] = exc.to_dict()
        except Exception as exc:  # noqa: BLE001 - benchmark records every failure
            row["error"] = {"code": getattr(exc, "code", "FP_E_BENCHMARK"), "message": str(exc)}
        row["plan_ok"] = plan_ok
        row["compiled"] = compiled
        row["rendered"] = rendered
        row["deterministic"] = deterministic
        row["warnings"] = [warning.get("code") for warning in warnings]
        row["warning_messages"] = [warning.get("message") for warning in warnings]
        row["elapsed_s"] = round(time.perf_counter() - started, 4)
        rows.append(row)
        status = "ok" if rendered else "FAIL"
        print(f"[{index + 1:02d}/{len(items):02d}] {item_id}: {status} ({row['elapsed_s']}s)")

    summary = {
        "total": len(rows),
        "plan_ok": sum(1 for row in rows if row["plan_ok"]),
        "compiled": sum(1 for row in rows if row["compiled"]),
        "rendered": sum(1 for row in rows if row["rendered"]),
        "deterministic": sum(1 for row in rows if row["deterministic"]),
        "with_warnings": sum(1 for row in rows if row["warnings"]),
        "resolution": int(args.resolution),
        "total_elements": sum(int(row.get("elements", 0)) for row in rows),
        "elapsed_s": round(sum(row["elapsed_s"] for row in rows), 4),
    }
    contact_sheet = _contact_sheet(thumbs, os.path.join(out_dir, "contact_sheet.png"))
    payload = {
        "schema": "planner-benchmark/1",
        "planner": "rule_planner_v1",
        "prompts": os.path.abspath(args.benchmark),
        "resolution": int(args.resolution),
        "summary": summary,
        "contact_sheet": contact_sheet,
        "rows": rows,
    }
    benchmark_path = os.path.join(out_dir, "benchmark.json")
    _write_json(benchmark_path, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            "summary: {rendered}/{total} rendered, {compiled}/{total} compiled, "
            "{deterministic}/{total} deterministic, {with_warnings} with warnings".format(**summary)
        )
        print(f"benchmark: {benchmark_path}")
        print(f"contact sheet: {contact_sheet}")
    return EXIT_OK if summary["rendered"] == summary["total"] else EXIT_FAILURE


def _contact_sheet(thumbs, path: str, columns: int = 4, cell: int = 256, label: int = 18):
    """Pillow contact sheet (4 columns); returns the absolute path."""
    from PIL import Image, ImageDraw

    if not thumbs:
        return ""
    rows = (len(thumbs) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * cell, rows * (cell + label)), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    for slot, (index, item_id, image, summary) in enumerate(thumbs):
        row, col = divmod(slot, columns)
        x = col * cell
        y = row * (cell + label)
        thumb = image.copy()
        thumb.thumbnail((cell, cell), Image.Resampling.LANCZOS)
        sheet.paste(thumb, (x + (cell - thumb.width) // 2, y + (cell - thumb.height) // 2))
        caption = f"{index + 1:02d} {summary.get('style', '')} {summary.get('subjects', [''])[0]}"
        draw.rectangle([x, y + cell, x + cell - 1, y + cell + label - 1], fill=(245, 245, 245))
        draw.text((x + 4, y + cell + 4), caption[:40], fill=(20, 20, 20))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    sheet.save(path, format="PNG")
    return os.path.abspath(path)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
