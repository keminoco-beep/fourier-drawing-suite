"""Synthetic prompt -> FDSL v2.1 dataset builder for the local LLM planner.

Training pairs are generated *entirely* from the deterministic rule planner
(:mod:`fourier_prompt.planner`, WP-N); no third-party images or captions are
used.  The builder only needs the standard library plus
:mod:`fourier_prompt.schema` / :mod:`fourier_prompt.repair`; the rule planner is
imported lazily (and can be injected through ``planner_fn``) so the module is
usable while WP-N is still in flight.

Typical use::

    from fourier_prompt.llm.dataset import build_dataset
    result = build_dataset(out_dir="out/llm/data", n=2000, seed=0)
    # -> out/llm/data/{train,val}.jsonl + stats.json

CLI::

    python -m fourier_prompt.llm.dataset --out out/llm/data --n 200 --seed 0
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import random
import statistics
import sys

from ..repair import repair_plan
from ..schema import FDSchemaError, validate_plan
from .formatting import FormattingError, canonical_plan_json

DATASET_VERSION = 1

# ---------------------------------------------------------------------------
# Prompt bank (synthetic; zh/en, subjects/styles/colours/positions/ratios)
# ---------------------------------------------------------------------------

SUBJECTS = (
    ("猫", "cat"),
    ("狐狸", "fox"),
    ("鸟", "bird"),
    ("鱼", "fish"),
    ("花", "flower"),
    ("树", "tree"),
    ("山", "mountain"),
    ("云", "cloud"),
    ("太阳", "sun"),
    ("月亮", "moon"),
    ("星星", "star"),
    ("心形", "heart"),
    ("波浪", "wave"),
    ("眼睛", "eye"),
    ("房子", "house"),
    ("人脸", "face"),
    ("机器人", "robot"),
    ("火箭", "rocket"),
    ("咖啡杯", "coffee cup"),
    ("书", "book"),
    ("几何图形", "geometric shapes"),
    ("三角形", "triangle"),
    ("六边形", "hexagon"),
    ("螺旋", "spiral"),
    ("雨", "rain"),
    ("雪", "snow"),
    ("森林", "forest"),
    ("城市天际线", "city skyline"),
)

STYLES = (
    ("黑白线稿", "black-and-white ink line art", "mono_ink"),
    ("漫画", "manga style", "manga_color"),
    ("铅笔画", "pencil sketch", "pencil"),
    ("水墨", "ink wash", "ink_wash"),
    ("马克笔", "marker drawing", "marker"),
    ("油画", "oil painting", "oil"),
    ("极简", "minimalist", "minimal"),
)

COLORS = (
    ("黑色", "black", "#111111"),
    ("红色", "red", "#d94b3a"),
    ("蓝色", "blue", "#3a63d9"),
    ("绿色", "green", "#3aa46b"),
    ("金色", "gold", "#e8c37a"),
    ("紫色", "purple", "#8a5bd6"),
    ("灰色", "grey", "#8c8c8c"),
    ("白色", "white", "#f5f5f5"),
)

POSITIONS = (
    ("居中", "centered"),
    ("左侧", "on the left"),
    ("右侧", "on the right"),
    ("上方", "at the top"),
    ("下方", "near the bottom"),
    ("网格排列", "in a grid"),
    ("重复排列", "repeated"),
    ("沿对角线", "along the diagonal"),
)

RATIOS = (
    ("竖版", "portrait"),
    ("横版", "landscape"),
    ("正方形", "square"),
)

TEXT_WORDS = ("HELLO", "2026", "夜", "风", "OK", "Dream", "光", "墨")

#: ``{subject} {style} {color} {position} {ratio} {text} {seed}`` slot templates.
TEMPLATES = (
    ("zh", "画一只{color}{subject}，{style}，{position}，{ratio}"),
    ("zh", "生成一幅{style}的{subject}插画，主色{color}，{position}，{ratio}"),
    ("zh", "{style}风格的{subject}，{color}轮廓，{position}，{ratio}，seed {seed}"),
    ("zh", "请画{subject}，{color}，{style}，{position}"),
    ("zh", "用{style}画{subject}和几何装饰，{color}，{position}，{ratio}"),
    ("zh", "一幅{ratio}{style}作品：{subject}，{position}，配色{color}"),
    ("en", "Draw a {color} {subject} in {style} style, {position}, {ratio}"),
    ("en", "Create {style} art of a {subject} using {color}, {position}, {ratio}"),
    ("en", "A {ratio} {style} illustration of a {subject}, {color} palette, {position}"),
    ("en", "Please draw a {subject} with {color} lines, {style}, {position}, seed {seed}"),
    ("en", "Minimal {style} composition featuring a {subject}, {color}, {position}"),
    ("zh", "画{subject}，避免照片感，{style}，{color}，{position}，{ratio}"),
)

#: Templates that carry a short text element (``{text}`` slot).
TEXT_TEMPLATES = (
    ("zh", "画{style}的{subject}，{position}，写上文字「{text}」，{ratio}"),
    ("zh", "{subject}，{color}，{style}，标题文字“{text}”，{position}"),
    ("en", "Draw a {style} {subject}, {position}, with the caption \"{text}\", {ratio}"),
    ("en", "{style} poster of a {subject} in {color}, title text \"{text}\", {ratio}"),
)


class DatasetError(RuntimeError):
    """Raised when the dataset cannot be built at all."""


# ---------------------------------------------------------------------------
# Planner plumbing
# ---------------------------------------------------------------------------


def _default_planner():
    """Lazily import the WP-N rule planner; raise a helpful error if missing."""
    try:
        from ..planner import plan_prompt
    except Exception as exc:  # ImportError or a broken partial module
        raise DatasetError(
            "rule planner is unavailable (fourier_prompt.planner.plan_prompt): "
            f"{type(exc).__name__}: {exc}. Wait for WP-N or pass planner_fn=..."
        ) from exc
    return plan_prompt


def _accepts_seed(fn):
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):  # builtins / C callables
        return False
    for parameter in signature.parameters.values():
        if parameter.name == "seed":
            return True
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


def call_planner(planner_fn, prompt, *, seed):
    """Call a rule planner and normalise its result to ``(plan, warnings)``.

    Accepts ``plan_prompt(prompt, seed=...) -> (plan, warnings)`` (WP-N
    contract), ``-> plan`` or ``-> {"plan": ..., "warnings": [...]}``.
    """
    if not callable(planner_fn):
        raise TypeError("planner_fn must be callable")
    if _accepts_seed(planner_fn):
        result = planner_fn(prompt, seed=seed)
    else:
        result = planner_fn(prompt)
    warnings = []
    if isinstance(result, tuple):
        if len(result) == 2:
            plan, warnings = result
        else:
            plan = result[0] if result else None
    elif isinstance(result, dict) and "plan" in result:
        plan = result.get("plan")
        warnings = result.get("warnings") or []
    else:
        plan = result
    if not isinstance(plan, dict):
        raise DatasetError(
            f"planner returned {type(plan).__name__}, expected a dict plan"
        )
    if warnings is None:
        warnings = []
    elif not isinstance(warnings, (list, tuple)):
        warnings = [warnings]
    return plan, list(warnings)


# ---------------------------------------------------------------------------
# Prompt synthesis
# ---------------------------------------------------------------------------


def _slots(rng):
    subject_zh, subject_en = SUBJECTS[rng.randrange(len(SUBJECTS))]
    style_zh, style_en, style_code = STYLES[rng.randrange(len(STYLES))]
    color_zh, color_en, color_hex = COLORS[rng.randrange(len(COLORS))]
    position_zh, position_en = POSITIONS[rng.randrange(len(POSITIONS))]
    ratio_zh, ratio_en = RATIOS[rng.randrange(len(RATIOS))]
    return {
        "subject_zh": subject_zh,
        "subject_en": subject_en,
        "style_zh": style_zh,
        "style_en": style_en,
        "style": style_code,
        "color_zh": color_zh,
        "color_en": color_en,
        "color": color_hex,
        "position_zh": position_zh,
        "position_en": position_en,
        "ratio_zh": ratio_zh,
        "ratio_en": ratio_en,
    }


def _render(template, slots, *, lang, text=None, seed=0):
    suffix = "_zh" if lang == "zh" else "_en"
    values = dict(slots)
    values.update(
        {
            "subject": slots["subject" + suffix],
            "style": slots["style" + suffix],
            "color": slots["color" + suffix],
            "position": slots["position" + suffix],
            "ratio": slots["ratio" + suffix],
            "text": text or "",
            "seed": seed,
        }
    )
    return template.format(**values)


def _normalise_prompt(prompt):
    return " ".join(prompt.strip().lower().split())


def make_prompt(rng, index, *, seed=0):
    """Deterministically synthesise one prompt + its meta."""
    use_text = index % 5 == 4  # ~20% of prompts carry an explicit caption
    bank = TEXT_TEMPLATES if use_text else TEMPLATES
    lang, template = bank[index % len(bank)]
    slots = _slots(rng)
    text = TEXT_WORDS[rng.randrange(len(TEXT_WORDS))] if use_text else None
    prompt = _render(template, slots, lang=lang, text=text, seed=seed)
    meta = {
        "template": template,
        "language": lang,
        "subject": slots["subject_en"],
        "style": slots["style"],
        "color": slots["color"],
        "position": slots["position_en"],
        "ratio": slots["ratio_en"],
        "text": text,
        "seed": seed,
    }
    return prompt, meta


def synthesise_prompts(n, *, seed=0, max_tries_factor=50):
    """Return ``n`` unique prompts (deterministic for a given seed)."""
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise DatasetError("n must be a non-negative integer")
    rng = random.Random(seed)
    seen = set()
    prompts = []
    index = 0
    attempts = 0
    limit = max(n * max_tries_factor, 100)
    while len(prompts) < n and attempts < limit:
        prompt, meta = make_prompt(rng, index, seed=seed)
        attempts += 1
        index += 1
        key = _normalise_prompt(prompt)
        if key in seen:
            continue
        seen.add(key)
        prompts.append((prompt, meta))
    if len(prompts) < n:
        # Fallback: make the prompt unique deterministically rather than fail.
        while len(prompts) < n:
            prompt, meta = make_prompt(rng, index, seed=seed)
            index += 1
            suffix = f"，seed {index}" if meta["language"] == "zh" else f", seed {index}"
            prompt = prompt + suffix
            key = _normalise_prompt(prompt)
            if key in seen:
                continue
            seen.add(key)
            prompts.append((prompt, meta))
    return prompts


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def compile_check(plan, *, resolution=256):
    """Return ``(ok, error_dict|None)``; imports numpy-backed compile lazily."""
    try:
        from ..compile import compile_plan
    except Exception as exc:  # pragma: no cover - numpy should be present
        return False, {"code": "FP_E_COMPILE_IMPORT", "message": str(exc)}
    try:
        compile_plan(plan, resolution=resolution)
        return True, None
    except Exception as exc:
        return False, {
            "code": str(getattr(exc, "code", type(exc).__name__)),
            "message": str(exc)[:500],
        }


def prepare_plan(plan, *, compile_check_enabled=False):
    """Repair + validate a planner plan.

    Returns ``(prepared_plan, plan_json, warnings, error)``; ``error`` is a dict
    when the plan had to be dropped.
    """
    try:
        repaired, warnings = repair_plan(plan)
    except FDSchemaError as exc:
        return None, None, [], {
            "stage": "repair",
            "code": exc.code,
            "message": str(exc)[:500],
        }
    except Exception as exc:  # defensive: a broken planner must not kill the run
        return None, None, [], {
            "stage": "repair",
            "code": type(exc).__name__,
            "message": str(exc)[:500],
        }
    errors = validate_plan(repaired)
    if errors:
        return None, None, warnings, {
            "stage": "schema",
            "code": "FP_E_SCHEMA_INVALID",
            "message": "; ".join(errors[:3])[:500],
        }
    if compile_check_enabled:
        ok, error = compile_check(repaired)
        if not ok:
            return None, None, warnings, {
                "stage": "compile",
                "code": (error or {}).get("code", "FP_E_COMPILE"),
                "message": (error or {}).get("message", "compile failed")[:500],
            }
    try:
        plan_json = canonical_plan_json(repaired)
    except FormattingError as exc:
        return None, None, warnings, {
            "stage": "serialize",
            "code": "llm/json_invalid",
            "message": str(exc)[:500],
        }
    return repaired, plan_json, warnings, None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _byte_stats(values):
    if not values:
        return {"count": 0, "min": 0, "mean": 0.0, "p50": 0.0, "max": 0}
    return {
        "count": len(values),
        "min": int(min(values)),
        "mean": round(statistics.fmean(values), 1),
        "p50": float(statistics.median(values)),
        "max": int(max(values)),
    }


def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(
                json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False)
            )
            handle.write("\n")


def build_dataset(
    *,
    out_dir,
    n=2000,
    seed=0,
    planner_fn=None,
    validate_ratio=0.1,
    compile_check_enabled=False,
):
    """Generate a synthetic prompt -> FDSL-plan dataset.

    Writes ``out_dir/train.jsonl``, ``out_dir/val.jsonl`` and
    ``out_dir/stats.json``.  Each record is
    ``{"prompt": str, "plan": dict, "plan_json": str, "meta": dict}`` where
    ``plan_json`` is the compact canonical JSON used as the training target.

    ``planner_fn`` defaults to ``fourier_prompt.planner.plan_prompt`` (imported
    lazily, so this module works while WP-N is still in flight).  Plans are
    deterministically repaired and must pass ``validate_plan``; invalid pairs
    are dropped and counted in ``stats["drops"]``.
    """
    out_dir = os.path.abspath(os.fspath(out_dir))
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise DatasetError("n must be a positive integer")
    if isinstance(validate_ratio, bool) or not isinstance(validate_ratio, (int, float)):
        raise DatasetError("validate_ratio must be a number in [0, 1)")
    validate_ratio = float(validate_ratio)
    if not 0.0 <= validate_ratio < 1.0:
        raise DatasetError("validate_ratio must be in [0, 1)")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise DatasetError("seed must be an integer")

    if planner_fn is None:
        planner_fn = _default_planner()

    prompts = synthesise_prompts(n, seed=seed)
    records = []
    drops = []
    warnings_total = 0
    repaired_count = 0
    plan_bytes = []

    for index, (prompt, meta) in enumerate(prompts):
        try:
            plan, warnings = call_planner(planner_fn, prompt, seed=seed + index)
        except Exception as exc:
            drops.append(
                {
                    "index": index,
                    "stage": "planner",
                    "code": type(exc).__name__,
                    "message": str(exc)[:300],
                }
            )
            continue

        prepared, plan_json, plan_warnings, error = prepare_plan(
            plan, compile_check_enabled=compile_check_enabled
        )
        if error is not None:
            error["index"] = index
            drops.append(error)
            continue

        warnings_total += len(warnings) + len(plan_warnings)
        repaired = bool(plan_warnings)
        repaired_count += 1 if repaired else 0
        payload = plan_json.encode("utf-8")
        plan_bytes.append(len(payload))
        record_meta = dict(meta)
        record_meta.update(
            {
                "i": index,
                "index": index,
                "repaired": repaired,
                "planner": getattr(planner_fn, "__module__", None) or "custom",
                "warnings": len(warnings) + len(plan_warnings),
            }
        )
        records.append(
            {
                "prompt": prompt,
                "plan": prepared,
                "plan_json": plan_json,
                "meta": record_meta,
            }
        )

    if not records:
        first = drops[0] if drops else {}
        raise DatasetError(
            "no dataset records were generated; "
            f"planner={planner_fn!r} first_drop={first}"
        )

    rng = random.Random((seed * 1_000_003) ^ 0x5EED)
    order = list(range(len(records)))
    rng.shuffle(order)
    n_val = int(round(len(records) * validate_ratio))
    if validate_ratio > 0 and len(records) > 1:
        n_val = max(1, min(n_val, len(records) - 1))
    else:
        n_val = 0
    val_indices = set(order[:n_val])
    train = [records[i] for i in range(len(records)) if i not in val_indices]
    val = [records[i] for i in range(len(records)) if i in val_indices]

    os.makedirs(out_dir, exist_ok=True)
    train_path = os.path.join(out_dir, "train.jsonl")
    val_path = os.path.join(out_dir, "val.jsonl")
    stats_path = os.path.join(out_dir, "stats.json")
    _write_jsonl(train_path, train)
    _write_jsonl(val_path, val)

    stats = {
        "version": DATASET_VERSION,
        "seed": seed,
        "n_requested": n,
        "n_generated": len(records),
        "n_dropped": len(drops),
        "n_train": len(train),
        "n_val": len(val),
        "validate_ratio": validate_ratio,
        "n_unique_prompts": len({_normalise_prompt(record["prompt"]) for record in records}),
        "compile_check": bool(compile_check_enabled),
        "planner": getattr(planner_fn, "__module__", None) or "custom",
        "repaired_plans": repaired_count,
        "warnings_total": warnings_total,
        "plan_bytes": _byte_stats(plan_bytes),
        "drops": drops[:100],
        "drop_reasons": _drop_reason_counts(drops),
        "files": {"train": "train.jsonl", "val": "val.jsonl"},
    }
    with open(stats_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(stats, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    return {
        "out_dir": out_dir,
        "train_path": train_path,
        "val_path": val_path,
        "stats_path": stats_path,
        "n": len(records),
        "n_train": len(train),
        "n_val": len(val),
        "n_dropped": len(drops),
        "stats": stats,
    }


def _drop_reason_counts(drops):
    counts = {}
    for drop in drops:
        key = f"{drop.get('stage', '?')}:{drop.get('code', '?')}"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


# ---------------------------------------------------------------------------
# Reading back
# ---------------------------------------------------------------------------


def load_jsonl(path):
    """Load a JSONL split produced by :func:`build_dataset`."""
    records = []
    with open(os.fspath(path), "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DatasetError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(record, dict) or "prompt" not in record:
                raise DatasetError(f"{path}:{line_number}: record needs a 'prompt'")
            records.append(record)
    return records


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m fourier_prompt.llm.dataset",
        description="Build a synthetic prompt->FDSL dataset with the rule planner.",
    )
    parser.add_argument("--out", required=True, help="output directory (train/val/stats)")
    parser.add_argument("--n", type=int, default=2000, help="number of pairs (default 2000)")
    parser.add_argument("--seed", type=int, default=0, help="deterministic seed (default 0)")
    parser.add_argument(
        "--validate-ratio",
        type=float,
        default=0.1,
        help="validation split fraction in [0, 1) (default 0.1)",
    )
    parser.add_argument(
        "--compile-check",
        action="store_true",
        help="also compile every plan (slower; guarantees renderable targets)",
    )
    parser.add_argument("--quiet", action="store_true", help="only print the summary")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = build_dataset(
            out_dir=args.out,
            n=args.n,
            seed=args.seed,
            validate_ratio=args.validate_ratio,
            compile_check_enabled=args.compile_check,
        )
    except DatasetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: cannot write dataset: {exc}", file=sys.stderr)
        return 2
    print(
        f"wrote {result['n_train']} train / {result['n_val']} val "
        f"({result['n_dropped']} dropped) to {result['out_dir']}"
    )
    if not args.quiet:
        print(json.dumps(result["stats"], ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
