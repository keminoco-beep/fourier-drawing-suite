"""Compare the rule planner with the local LLM planner on the 20-prompt set.

For every prompt we record: does the plan compile, did the LLM answer on the
first attempt / need retries / fall back to the rule planner, and how long it
took.  The script is CPU-safe: without torch the LLM column simply falls back
(or use ``--rule-only``).

Usage::

    python scripts/eval_planner.py --rule-only
    python scripts/eval_planner.py --model-dir models/Qwen2.5-1.5B-Instruct \
        --adapter-dir out/llm/qwen2.5-1.5b-lora-v1 --out out/llm/eval_planner.json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fourier_prompt.llm.dataset import DatasetError, call_planner, prepare_plan  # noqa: E402
from fourier_prompt.llm.infer import default_model_dir, generate_plan  # noqa: E402

DEFAULT_PROMPTS = (
    "画一只黑色的猫，线稿风格，居中，竖版",
    "生成一个金色的星星，居中，极简风格",
    "画一座山和太阳，水墨风格，横版",
    "画一朵花，红色，左下方，正方形",
    "Draw a blue bird in pencil sketch style, centered, portrait",
    "生成六个圆，网格排列，黑白线稿",
    "画一个心形，红色轮廓，居中，写上文字「LOVE」",
    "画出雨中的森林，灰色调，横版，线稿",
    "画一个机器人，蓝色，右侧，马克笔风格",
    "极简风格的城市天际线，黑色，横版",
    "画螺旋和波浪，金色，沿对角线，竖版",
    "生成一幅漫画风格的猫，白色背景，居中",
    "画三颗星星，重复排列，黄色，正方形",
    "一幅水墨月夜，竖版，灰色和黑色",
    "Draw a geometric fox, mono ink, on the left, square",
    "画一个咖啡杯，棕色，桌面上，写实一点但保持线稿",
    "画太阳和云，油画风格，暖色，横版",
    "生成一张海报：黑色背景，金色文字「2026」，极简",
    "画一只眼睛，特写，黑色线条，居中，正方形",
    "不要照片感，画一棵树和鸟，铅笔画，右侧，竖版",
)


def load_prompts(path=None):
    """Load prompts from a JSONL file, falling back to :data:`DEFAULT_PROMPTS`."""
    if not path or not os.path.isfile(path):
        return list(DEFAULT_PROMPTS), "builtin"
    prompts = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                payload = line
            if isinstance(payload, str):
                prompts.append(payload)
            elif isinstance(payload, dict):
                value = payload.get("prompt") or payload.get("text")
                if isinstance(value, str) and value.strip():
                    prompts.append(value)
                else:
                    raise ValueError(f"{path}:{line_number}: no 'prompt' field")
            else:
                raise ValueError(f"{path}:{line_number}: unsupported JSONL entry")
    if not prompts:
        raise ValueError(f"{path}: no prompts found")
    return prompts, os.path.abspath(path)


def _rule_run(prompt, *, planner_fn, seed, compile_enabled=True):
    started = time.perf_counter()
    try:
        if planner_fn is None:
            from fourier_prompt.llm.dataset import _default_planner

            planner_fn = _default_planner()
        plan, warnings = call_planner(planner_fn, prompt, seed=seed)
        prepared, plan_json, repair_warnings, error = prepare_plan(
            plan, compile_check_enabled=compile_enabled
        )
    except DatasetError as exc:
        return {
            "ok": False,
            "compile_ok": False,
            "available": False,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "error": f"planner unavailable: {exc}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "compile_ok": False,
            "available": True,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "error": f"{type(exc).__name__}: {exc}",
        }
    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    if error is not None:
        return {
            "ok": False,
            "compile_ok": False,
            "available": True,
            "latency_ms": latency_ms,
            "error": f"{error.get('code')}: {error.get('message')}",
        }
    return {
        "ok": True,
        "compile_ok": True,
        "available": True,
        "latency_ms": latency_ms,
        "plan_bytes": len(plan_json.encode("utf-8")),
        "warnings": len(warnings) + len(repair_warnings),
    }


def _llm_run(
    prompt,
    *,
    planner_fn,
    model_dir,
    adapter_dir,
    device,
    max_new_tokens,
    seed,
    rule_only,
    compile_enabled=True,
):
    result = generate_plan(
        prompt,
        model_dir=model_dir,
        adapter_dir=adapter_dir,
        max_new_tokens=max_new_tokens,
        device=device,
        seed=seed,
        planner_fn=planner_fn,
        rule_only=rule_only,
        compile_check_enabled=compile_enabled,
    )
    return {
        "ok": bool(result.get("ok")),
        "compile_ok": result.get("compile_ok"),
        "source": result.get("source"),
        "attempts": result.get("attempts"),
        "fallback": bool(result.get("fallback")),
        "latency_s": result.get("latency_s"),
        "plan_bytes": (
            len(json.dumps(result["plan"], ensure_ascii=False, separators=(",", ":")))
            if result.get("plan")
            else 0
        ),
        "errors": result.get("errors", []),
    }


def _rate(count, total):
    return round(count / total, 4) if total else 0.0


def summarise(rows, *, mode):
    total = len(rows)
    rule_ok = sum(1 for row in rows if row["rule"].get("compile_ok"))
    rule_available = sum(1 for row in rows if row["rule"].get("available", True))
    llm_ok = sum(1 for row in rows if row["llm"].get("compile_ok"))
    first_attempt = sum(
        1 for row in rows if row["llm"].get("source") == "llm" and row["llm"].get("attempts") == 1
    )
    retried = sum(1 for row in rows if (row["llm"].get("attempts") or 0) > 1)
    fallback = sum(1 for row in rows if row["llm"].get("source") == "rule")
    latencies = [row["llm"]["latency_s"] for row in rows if row["llm"].get("latency_s") is not None]
    rule_ms = [row["rule"]["latency_ms"] for row in rows if row["rule"].get("latency_ms") is not None]
    both = sum(1 for row in rows if row["rule"].get("compile_ok") and row["llm"].get("compile_ok"))
    rule_only_ok = sum(
        1 for row in rows if row["rule"].get("compile_ok") and not row["llm"].get("compile_ok")
    )
    llm_only_ok = sum(
        1 for row in rows if not row["rule"].get("compile_ok") and row["llm"].get("compile_ok")
    )
    return {
        "n": total,
        "mode": mode,
        "rule": {
            "available": rule_available,
            "compile_ok": rule_ok,
            "compile_rate": _rate(rule_ok, total),
            "mean_latency_ms": round(statistics.fmean(rule_ms), 1) if rule_ms else None,
        },
        "llm": {
            "compile_ok": llm_ok,
            "compile_rate": _rate(llm_ok, total),
            "first_attempt_ok": first_attempt,
            "first_attempt_rate": _rate(first_attempt, total),
            "retried": retried,
            "retry_rate": _rate(retried, total),
            "fallback": fallback,
            "fallback_rate": _rate(fallback, total),
            "attempts_histogram": _attempt_histogram(rows),
            "mean_latency_s": round(statistics.fmean(latencies), 3) if latencies else None,
            "p50_latency_s": round(statistics.median(latencies), 3) if latencies else None,
            "max_latency_s": round(max(latencies), 3) if latencies else None,
        },
        "comparison": {
            "both_compile": both,
            "rule_only_compile": rule_only_ok,
            "llm_only_compile": llm_only_ok,
            "neither": total - both - rule_only_ok - llm_only_ok,
        },
    }


def _attempt_histogram(rows):
    histogram = {}
    for row in rows:
        key = str(row["llm"].get("attempts"))
        histogram[key] = histogram.get(key, 0) + 1
    return dict(sorted(histogram.items()))


def run_eval(
    prompts,
    *,
    planner_fn=None,
    model_dir=None,
    adapter_dir=None,
    device="cuda",
    max_new_tokens=2048,
    seed=0,
    limit=None,
    rule_only=False,
    compile_enabled=True,
    source="builtin",
):
    """Run rule vs LLM on ``prompts`` and return a JSON-serialisable report."""
    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        prompts = prompts[:limit]
    rows = []
    for index, prompt in enumerate(prompts):
        rule = _rule_run(prompt, planner_fn=planner_fn, seed=seed + index)
        llm = _llm_run(
            prompt,
            planner_fn=planner_fn,
            model_dir=model_dir,
            adapter_dir=adapter_dir,
            device=device,
            max_new_tokens=max_new_tokens,
            seed=seed + index,
            rule_only=rule_only,
            compile_enabled=compile_enabled,
        )
        rows.append({"index": index, "prompt": prompt, "rule": rule, "llm": llm})
    mode = "rule_only" if rule_only else "llm"
    return {
        "prompts_source": source,
        "limit": limit,
        "seed": seed,
        "model_dir": model_dir or default_model_dir(),
        "adapter_dir": adapter_dir,
        "mode": mode,
        "aggregate": summarise(rows, mode=mode),
        "rows": rows,
    }


def print_table(report):
    aggregate = report["aggregate"]
    print(f"{'#':>2}  {'rule':^6} {'compile':^7} {'src':^4} {'try':^3} {'s':>6}  prompt")
    for row in report["rows"]:
        rule = row["rule"]
        llm = row["llm"]
        print(
            f"{row['index']:>2}  "
            f"{'ok' if rule.get('compile_ok') else 'FAIL':^6} "
            f"{'ok' if llm.get('compile_ok') else 'FAIL':^7} "
            f"{str(llm.get('source')):^4} {str(llm.get('attempts')):^3} "
            f"{llm.get('latency_s') or 0:>6.2f}  {row['prompt'][:48]}"
        )
    print()
    print(f"n={aggregate['n']} mode={aggregate['mode']}")
    print(
        f"rule: compile {aggregate['rule']['compile_ok']}/{aggregate['n']} "
        f"({aggregate['rule']['compile_rate']:.1%})"
    )
    llm = aggregate["llm"]
    print(
        f"llm : compile {llm['compile_ok']}/{aggregate['n']} ({llm['compile_rate']:.1%}), "
        f"first-attempt {llm['first_attempt_rate']:.1%}, retry {llm['retry_rate']:.1%}, "
        f"fallback {llm['fallback_rate']:.1%}, "
        f"mean {llm['mean_latency_s']}s"
    )


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python scripts/eval_planner.py",
        description="Rule planner vs local LLM planner on the 20-prompt eval set.",
    )
    parser.add_argument(
        "--prompts",
        default=str(_ROOT / "assets" / "eval" / "prompts_20.jsonl"),
        help="JSONL prompt set (falls back to the built-in 20 prompts)",
    )
    parser.add_argument(
        "--out",
        default=str(_ROOT / "out" / "llm" / "eval_planner.json"),
        help="report JSON output path",
    )
    parser.add_argument("--limit", type=int, default=None, help="evaluate only the first N")
    parser.add_argument("--model-dir", default=None)
    parser.add_argument("--adapter-dir", default=None)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--max-new-tokens", type=int, default=2048)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--rule-only",
        action="store_true",
        help="skip the LLM (useful before torch/model are installed)",
    )
    parser.add_argument(
        "--no-compile-check",
        action="store_true",
        help="validate schema only (compile check is on by default)",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")
    try:
        prompts, source = load_prompts(args.prompts)
    except Exception as exc:
        print(f"error: cannot load prompts: {exc}", file=sys.stderr)
        return 2
    try:
        report = run_eval(
            prompts,
            model_dir=args.model_dir,
            adapter_dir=args.adapter_dir,
            device=args.device,
            max_new_tokens=args.max_new_tokens,
            seed=args.seed,
            limit=args.limit,
            rule_only=args.rule_only,
            compile_enabled=not args.no_compile_check,
            source=source,
        )
    except Exception as exc:
        print(f"error: evaluation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print_table(report)
    output = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    with open(output, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    print(f"\nreport -> {output}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
