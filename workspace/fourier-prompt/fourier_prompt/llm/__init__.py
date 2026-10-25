"""Local LLM planner package (WP-O): synthetic dataset, chat formatting, inference.

The package is import-safe without torch/transformers/peft: model loading and
generation import them lazily, so unit tests and dataset building run on a
plain CPU-only Python install.  Exports are resolved lazily (PEP 562) so that
``python -m fourier_prompt.llm.dataset`` does not double-import submodules.

Modules
-------
* :mod:`~fourier_prompt.llm.formatting` — canonical plan JSON, chat messages,
  tolerant JSON extraction and assistant-only label masking;
* :mod:`~fourier_prompt.llm.dataset` — synthetic prompt -> plan dataset builder
  (``build_dataset``) and JSONL readers;
* :mod:`~fourier_prompt.llm.infer` — ``generate_plan`` (base + LoRA, offline)
  with schema/compile validation, retries and rule-planner fallback.
"""

from __future__ import annotations

import importlib

__version__ = "0.1.0"

_EXPORTS = {
    # formatting
    "FDSL_SYSTEM_PROMPT": ".formatting",
    "ExtractJSONError": ".formatting",
    "FormattingError": ".formatting",
    "build_messages": ".formatting",
    "canonical_plan_json": ".formatting",
    "extract_json": ".formatting",
    "messages_to_text": ".formatting",
    "tokenize_example": ".formatting",
    # dataset
    "DATASET_VERSION": ".dataset",
    "DatasetError": ".dataset",
    "build_dataset": ".dataset",
    "call_planner": ".dataset",
    "dataset_parser": ".dataset",
    "load_jsonl": ".dataset",
    "synthesise_prompts": ".dataset",
    # inference
    "DEFAULT_MAX_NEW_TOKENS": ".infer",
    "DEFAULT_MAX_RETRIES": ".infer",
    "default_model_dir": ".infer",
    "generate_plan": ".infer",
    "load_model": ".infer",
    "validate_candidate": ".infer",
}

__all__ = ["__version__", *sorted(_EXPORTS)]


def __getattr__(name):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))
