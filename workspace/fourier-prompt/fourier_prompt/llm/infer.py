"""Local LLM planner inference: base Qwen2.5 + LoRA adapter -> validated FDSL.

The wrapper is deliberately defensive: generation is untrusted, so every answer
goes through ``extract_json -> repair_plan -> validate_plan -> compile_plan``.
Failures are retried at most ``max_retries`` times with error feedback, then the
deterministic rule planner is used as fallback.  ``generate_plan`` never raises
for model/JSON/compile problems; it returns a result dict with ``ok=False`` when
even the fallback fails.

Torch / transformers / peft are imported lazily, so this module (and its tests)
work in CPU-only environments without those packages.

CLI::

    python -m fourier_prompt.llm.infer --prompt "画一只黑猫，线稿，居中" \
        --out out/llm/plan.json [--render out/llm/plan.png]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from ..repair import repair_plan
from ..schema import FDSchemaError, validate_plan
from .dataset import DatasetError, call_planner, prepare_plan
from .formatting import (
    ExtractJSONError,
    FDSL_SYSTEM_PROMPT,
    build_messages,
    canonical_plan_json,
    extract_json,
)

DEFAULT_MAX_NEW_TOKENS = 2048
DEFAULT_MAX_RETRIES = 2
MAX_RETRIES_HARD = 2


def default_model_dir():
    """Resolve the local Qwen2.5-Instruct directory (env override first)."""
    env = os.environ.get("FDP_LLM_MODEL")
    if env:
        return env
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)
    ))))
    return os.path.join(repo_root, "models", "Qwen2.5-1.5B-Instruct")


# ---------------------------------------------------------------------------
# Lazy dependency plumbing
# ---------------------------------------------------------------------------


def _require_llm_stack():
    """Import the inference stack or raise a descriptive RuntimeError."""
    try:
        import torch  # noqa: F401
        from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: F401
    except Exception as exc:
        raise RuntimeError(
            "torch/transformers are not available; cannot run the LLM planner "
            f"({type(exc).__name__}: {exc})"
        ) from exc
    return True


def _pick_dtype(device):
    import torch

    if str(device).startswith("cuda") and torch.cuda.is_available():
        if torch.cuda.is_bf16_supported():
            return torch.bfloat16
        return torch.float16
    return torch.float32


def load_model(model_dir=None, adapter_dir=None, device="cuda"):
    """Load the local base model and optional LoRA adapter (offline only)."""
    _require_llm_stack()
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    model_dir = model_dir or default_model_dir()
    if not os.path.isdir(model_dir):
        raise FileNotFoundError(f"local model directory not found: {model_dir}")
    tokenizer = AutoTokenizer.from_pretrained(
        model_dir, local_files_only=True, trust_remote_code=False
    )
    if tokenizer.pad_token_id is None and tokenizer.eos_token_id is not None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        model_dir,
        local_files_only=True,
        trust_remote_code=False,
        torch_dtype=_pick_dtype(device),
    )
    if adapter_dir:
        try:
            from peft import PeftModel
        except Exception as exc:
            raise RuntimeError(f"peft is required for --adapter-dir ({exc})") from exc
        if not os.path.isdir(adapter_dir):
            raise FileNotFoundError(f"adapter directory not found: {adapter_dir}")
        model = PeftModel.from_pretrained(model, adapter_dir, is_trainable=False)
    model.to(device)
    model.eval()
    return model, tokenizer


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _generate_raw(model, tokenizer, messages, *, max_new_tokens, device, seed):
    """Greedy chat generation; returns the decoded new tokens."""
    import torch

    torch.manual_seed(seed)
    renderer = getattr(tokenizer, "apply_chat_template", None)
    if renderer is not None:
        text = renderer(messages, tokenize=False, add_generation_prompt=True)
    else:  # pragma: no cover - all supported tokenizers have a chat template
        from .formatting import messages_to_text

        text = messages_to_text(messages) + "<|assistant|>\n"
    inputs = tokenizer(text, return_tensors="pt")
    inputs = {key: value.to(device) for key, value in inputs.items()}
    generate_kwargs = {
        "max_new_tokens": int(max_new_tokens),
        "do_sample": False,
        "pad_token_id": getattr(tokenizer, "pad_token_id", None),
    }
    eos_id = getattr(tokenizer, "eos_token_id", None)
    if eos_id is not None:
        generate_kwargs["eos_token_id"] = eos_id
    with torch.no_grad():
        output = model.generate(**inputs, **generate_kwargs)
    new_tokens = output[0][inputs["input_ids"].shape[-1] :]
    return tokenizer.decode(new_tokens, skip_special_tokens=True)


def _error(stage, code, message, *, attempt=None):
    payload = {"stage": stage, "code": str(code), "message": str(message)[:800]}
    if attempt is not None:
        payload["attempt"] = attempt
    return payload


# ---------------------------------------------------------------------------
# Validation pipeline
# ---------------------------------------------------------------------------


def validate_candidate(plan, *, compile_enabled=True, resolution=256):
    """Repair + schema + compile check one candidate plan.

    Returns ``(plan, errors, warnings)``; ``errors`` is empty on success.
    """
    try:
        repaired, warnings = repair_plan(plan)
    except FDSchemaError as exc:
        return None, [_error("repair", exc.code, str(exc))], []
    except Exception as exc:  # defensive: keep generation alive
        return None, [_error("repair", type(exc).__name__, str(exc))], []

    schema_errors = validate_plan(repaired)
    if schema_errors:
        return None, [
            _error("schema", "FP_E_SCHEMA_INVALID", " | ".join(schema_errors[:3]))
        ], warnings

    if compile_enabled:
        try:
            from ..compile import compile_plan

            compile_plan(repaired, resolution=resolution)
        except Exception as exc:
            return None, [
                _error(
                    "compile",
                    getattr(exc, "code", type(exc).__name__),
                    str(exc),
                )
            ], warnings
    return repaired, [], warnings


# ---------------------------------------------------------------------------
# Rule planner fallback
# ---------------------------------------------------------------------------


def _resolve_planner(planner_fn):
    if planner_fn is not None:
        return planner_fn
    from .dataset import _default_planner

    return _default_planner()


def _rule_fallback(
    prompt, *, planner_fn, seed, errors, started, compile_check_enabled=True, attempts=0
):
    """Run the rule planner as fallback; returns a complete result dict."""
    try:
        planner = _resolve_planner(planner_fn)
        plan, warnings = call_planner(planner, prompt, seed=seed)
    except (DatasetError, ImportError) as exc:
        errors.append(_error("rule", "FP_E_PLANNER_UNAVAILABLE", str(exc)))
        return _result(
            None, "rule", attempts, None, errors, started, warnings=[], ok=False
        )
    except Exception as exc:
        errors.append(_error("rule", type(exc).__name__, str(exc)))
        return _result(
            None, "rule", attempts, None, errors, started, warnings=[], ok=False
        )

    prepared, _plan_json, repair_warnings, error = prepare_plan(
        plan, compile_check_enabled=compile_check_enabled
    )
    all_warnings = list(warnings) + list(repair_warnings)
    if error is not None:
        errors.append(_error("rule", error.get("code", "FP_E_RULE_INVALID"), error.get("message", "")))
        return _result(
            None, "rule", attempts, False, errors, started, warnings=all_warnings, ok=False
        )
    return _result(
        prepared,
        "rule",
        attempts,
        True,
        errors,
        started,
        warnings=all_warnings,
        ok=True,
        fallback=True,
    )


def _result(
    plan,
    source,
    attempts,
    compile_ok,
    errors,
    started,
    *,
    warnings=None,
    ok=True,
    fallback=False,
    model_dir=None,
    adapter_dir=None,
    attempt_trace=None,
):
    return {
        "plan": plan,
        "source": source,
        "attempts": int(attempts),
        "compile_ok": compile_ok,
        "errors": list(errors),
        "latency_s": round(time.perf_counter() - started, 3),
        "ok": bool(ok),
        "fallback": bool(fallback),
        "warnings": list(warnings or []),
        "model_dir": model_dir,
        "adapter_dir": adapter_dir,
        "attempt_trace": list(attempt_trace or []),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_plan(
    prompt,
    *,
    model_dir=None,
    adapter_dir=None,
    max_new_tokens=DEFAULT_MAX_NEW_TOKENS,
    device="cuda",
    seed=0,
    max_retries=DEFAULT_MAX_RETRIES,
    planner_fn=None,
    model=None,
    tokenizer=None,
    rule_only=False,
    compile_check_enabled=True,
):
    """Generate one FDSL v2.1 plan for ``prompt``.

    Returns ``{"plan", "source": "llm"|"rule", "attempts", "compile_ok",
    "errors", "latency_s", "ok", "fallback", "warnings", ...}``.  ``attempts``
    counts LLM generations (0 when the model could not be loaded or
    ``rule_only=True``); retries are capped at 2 (``max_retries`` is clamped).
    """
    started = time.perf_counter()
    errors = []
    max_retries = max(0, min(int(max_retries), MAX_RETRIES_HARD))
    model_dir = model_dir or default_model_dir()

    if not isinstance(prompt, str) or not prompt.strip():
        errors.append(_error("input", "FP_E_PROMPT_EMPTY", "prompt must be a non-empty string"))
        return _result(
            None,
            "rule",
            0,
            None,
            errors,
            started,
            ok=False,
            model_dir=model_dir,
            adapter_dir=adapter_dir,
        )

    if rule_only:
        return _rule_fallback(
            prompt,
            planner_fn=planner_fn,
            seed=seed,
            errors=errors,
            started=started,
            compile_check_enabled=compile_check_enabled,
            attempts=0,
        )

    if not rule_only and (model is None or tokenizer is None):
        try:
            model, tokenizer = load_model(model_dir, adapter_dir, device)
        except Exception as exc:
            errors.append(_error("load", "llm/model_unavailable", str(exc)))
            return _rule_fallback(
                prompt,
                planner_fn=planner_fn,
                seed=seed,
                errors=errors,
                started=started,
                compile_check_enabled=compile_check_enabled,
            )

    trace = []
    for attempt in range(1, max_retries + 2):
        messages = build_messages(
            prompt,
            feedback=_feedback(errors) if attempt > 1 else None,
            system=FDSL_SYSTEM_PROMPT,
        )
        try:
            raw = _generate_raw(
                model,
                tokenizer,
                messages,
                max_new_tokens=max_new_tokens,
                device=device,
                seed=seed + attempt - 1,
            )
        except Exception as exc:
            errors.append(_error("generate", type(exc).__name__, str(exc), attempt=attempt))
            trace.append({"attempt": attempt, "ok": False, "stage": "generate"})
            break

        try:
            candidate = extract_json(raw)
        except ExtractJSONError as exc:
            errors.append(_error("extract", exc.code, str(exc), attempt=attempt))
            trace.append(
                {"attempt": attempt, "ok": False, "stage": "extract", "raw_chars": len(raw)}
            )
            continue

        prepared, attempt_errors, warnings = validate_candidate(
            candidate, compile_enabled=compile_check_enabled
        )
        if attempt_errors:
            for item in attempt_errors:
                item["attempt"] = attempt
            errors.extend(attempt_errors)
            trace.append(
                {
                    "attempt": attempt,
                    "ok": False,
                    "stage": attempt_errors[0]["stage"],
                    "raw_chars": len(raw),
                }
            )
            continue

        trace.append({"attempt": attempt, "ok": True, "stage": "compiled", "raw_chars": len(raw)})
        return _result(
            prepared,
            "llm",
            attempt,
            True,
            errors,
            started,
            warnings=warnings,
            ok=True,
            model_dir=model_dir,
            adapter_dir=adapter_dir,
            attempt_trace=trace,
        )

    # All LLM attempts failed: fall back to the deterministic rule planner.
    result = _rule_fallback(
        prompt,
        planner_fn=planner_fn,
        seed=seed,
        errors=errors,
        started=started,
        compile_check_enabled=compile_check_enabled,
        attempts=len(trace),
    )
    result["fallback_reason"] = errors[-1]["message"] if errors else "unknown"
    result["model_dir"] = model_dir
    result["adapter_dir"] = adapter_dir
    result["attempt_trace"] = trace
    return result


def _feedback(errors):
    lines = []
    for item in errors[-4:]:
        lines.append(f"- {item.get('stage')}/{item.get('code')}: {item.get('message')}")
    return "Issues to fix:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m fourier_prompt.llm.infer",
        description="Prompt -> FDSL v2.1 plan with the local Qwen2.5 LoRA planner.",
    )
    parser.add_argument("--prompt", required=True, help="natural-language drawing prompt")
    parser.add_argument("--out", default=None, help="write the plan JSON here")
    parser.add_argument("--render", default=None, help="also render a PNG here")
    parser.add_argument(
        "--model-dir",
        default=None,
        help="local base model directory (default: $FDP_LLM_MODEL or models/Qwen2.5-1.5B-Instruct)",
    )
    parser.add_argument("--adapter-dir", default=None, help="LoRA adapter directory")
    parser.add_argument("--max-new-tokens", type=int, default=DEFAULT_MAX_NEW_TOKENS)
    parser.add_argument("--device", default="cuda", help="cuda / cpu")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    parser.add_argument(
        "--rule-only",
        action="store_true",
        help="skip the model and use the deterministic rule planner",
    )
    parser.add_argument("--quiet", action="store_true", help="do not print the summary")
    parser.add_argument(
        "--no-compile-check",
        action="store_true",
        help="skip the compile_plan validation (not recommended)",
    )
    return parser


def _write_plan(path, plan):
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(plan, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _render_plan(plan, path, *, resolution=512):
    from PIL import Image

    from ..compile import compile_plan
    from ..render_fast import render

    doc = compile_plan(plan, resolution=resolution)["fbs"]
    image = render(doc, resolution=resolution)
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    Image.fromarray(image).save(path)


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_new_tokens < 16:
        parser.error("--max-new-tokens must be >= 16")
    if args.max_retries < 0:
        parser.error("--max-retries must be >= 0")

    result = generate_plan(
        args.prompt,
        model_dir=args.model_dir,
        adapter_dir=args.adapter_dir,
        max_new_tokens=args.max_new_tokens,
        device=args.device,
        seed=args.seed,
        max_retries=args.max_retries,
        rule_only=args.rule_only,
        compile_check_enabled=not args.no_compile_check,
    )

    if not args.quiet:
        summary = {key: result[key] for key in ("source", "attempts", "compile_ok", "latency_s", "ok")}
        summary["errors"] = len(result["errors"])
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))

    if result.get("plan") is None:
        for item in result["errors"]:
            print(
                f"error: {item.get('stage')}/{item.get('code')}: {item.get('message')}",
                file=sys.stderr,
            )
        return 1

    if args.out:
        _write_plan(args.out, result["plan"])
    else:
        print(canonical_plan_json(result["plan"]))

    if args.render:
        try:
            _render_plan(result["plan"], args.render)
        except Exception as exc:
            print(f"error: render failed: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
