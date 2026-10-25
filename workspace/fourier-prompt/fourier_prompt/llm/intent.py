"""Hybrid prompt path: local LLM parses a small **intent JSON**, the rule planner draws.

The full-FDSL LLM planner (:mod:`fourier_prompt.llm.infer`) is not usable yet, so
this module splits the problem:

1. Qwen (base Instruct, no adapter needed) receives a bilingual few-shot prompt
   and replies with a compact intent object::

       {"subject":["cat"],"count":1,"colors":["black"],"style":"mono_ink",
        "text":[],"position":"center","canvas":"square",
        "negations":[],"confidence":0.9}

2. The intent is validated/normalised against the *rule planner's own
   vocabulary* (:mod:`fourier_prompt.prompts`); impossible values fall back to
   defaults with explicit warnings.
3. :func:`plan_from_intent` rebuilds a natural-language prompt from the intent
   and delegates to :func:`fourier_prompt.planner.plan_prompt` — the planner
   code is **never duplicated here**.
4. If the model or its JSON fails completely, the CLI runs the rule planner on
   the original prompt (``source="rule_fallback"``), so a usable plan is still
   produced.

API
---
``extract_intent(prompt, *, model_dir, device="cuda", max_new_tokens=256,
seed=0) -> (intent, meta)``
    ``intent`` is a normalised dict or ``None``; ``meta`` records JSON validity,
    errors, warnings, latency and whether the caller should fall back.
``plan_from_intent(intent, *, seed=0, planner_fn=None) -> (plan, warnings)``
``plan_from_prompt(prompt, *, seed=0, planner_fn=None) -> (plan, warnings)``
    Thin wrapper over the rule planner (used by ``--rule-only`` / fallback).
``normalize_intent(payload, *, prompt=None) -> (intent, warnings)``
``intent_to_prompt(intent) -> str``

CLI::

    python -m fourier_prompt.llm.intent --prompt "画一只黑猫，线稿，居中" \
        --out out/llm/intent/plan.json --render out/llm/intent/plan.png \
        [--model-dir models/Qwen2.5-1.5B-Instruct] [--device cuda|cpu] \
        [--rule-only] [--json] [--intent-out intent.json]

Exit codes: ``0`` plan produced (LLM intent or rule fallback), ``1`` compile /
render / I/O failure, ``2`` usage error (argparse, empty prompt, bad flags),
``3`` no plan at all (intent failed *and* the rule planner rejected the prompt).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time

from .. import prompts
from ..planner import PlannerError, plan_prompt, plan_summary
from .dataset import DatasetError, call_planner
from .formatting import ExtractJSONError, extract_json

DEFAULT_MAX_NEW_TOKENS = 256
MAX_NEW_TOKENS_HARD = 1024
MAX_NEW_TOKENS_MIN = 16
MAX_RESOLUTION = 512

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_PLANNER = 3

#: Intent fields accepted in the model answer (the contract from the task).
INTENT_FIELDS = (
    "subject",
    "count",
    "colors",
    "style",
    "text",
    "position",
    "canvas",
    "negations",
    "confidence",
)

#: Tolerated spellings the model may emit instead of the canonical field name.
FIELD_ALIASES = {
    "subject": "subject",
    "subjects": "subject",
    "object": "subject",
    "objects": "subject",
    "count": "count",
    "number": "count",
    "quantity": "count",
    "colors": "colors",
    "colours": "colors",
    "color": "colors",
    "colour": "colors",
    "palette": "colors",
    "style": "style",
    "styles": "style",
    "text": "text",
    "texts": "text",
    "caption": "text",
    "position": "position",
    "placement": "position",
    "canvas": "canvas",
    "ratio": "canvas",
    "aspect": "canvas",
    "aspect_ratio": "canvas",
    "negations": "negations",
    "negation": "negations",
    "negative": "negations",
    "negatives": "negations",
    "confidence": "confidence",
}

#: Human-readable (zh) alias used when rebuilding a planner prompt.
SUBJECT_TERMS = {
    "circle": "圆形",
    "star": "五角星",
    "heart": "爱心",
    "cloud": "云朵",
    "mountain": "山脉",
    "tree": "树木",
    "house": "房子",
    "cat": "小猫",
    "flower": "花朵",
    "sun": "太阳",
    "moon": "月亮",
    "wave": "波浪",
    "snow": "雪花",
    "eye": "眼睛",
    "geometry": "几何图形",
}

COLOR_TERMS = {
    "black": "黑色",
    "white": "白色",
    "gray": "灰色",
    "red": "红色",
    "blue": "蓝色",
    "green": "绿色",
    "gold": "金色",
    "purple": "紫色",
    "orange": "橙色",
    "pink": "粉色",
    "cyan": "青色",
    "brown": "棕色",
    "yellow": "黄色",
}

POSITION_TERMS = {
    "center": "居中",
    "left": "左边",
    "right": "右边",
    "top": "上方",
    "bottom": "下方",
}

CANVAS_TERMS = {"square": "方形", "portrait": "竖版", "landscape": "横版"}

#: Planner vocabulary keys (kept in sync by tests).
SUBJECT_KEYS = tuple(sorted(prompts.SUBJECTS))
STYLE_KEYS = tuple(sorted(prompts.STYLES))
COLOR_KEYS = tuple(sorted(prompts.COLORS))
POSITION_KEYS = tuple(sorted(prompts.POSITIONS))
CANVAS_KEYS = tuple(sorted(prompts.CANVAS_PRESETS))
NEGATION_TERMS = ("no_text", "no_background")

def _alias_index(table, *, include_keys=True):
    """Build ``{lowercased alias: canonical key}`` for a planner lexicon."""
    index = {}
    for key, aliases in table.items():
        if include_keys:
            index[str(key).lower()] = key
        for alias in aliases:
            index[str(alias).strip().lower()] = key
    return index


_SUBJECT_ALIASES = _alias_index(prompts.SUBJECTS)
_COLOR_ALIASES = _alias_index(prompts.COLORS)
_STYLE_ALIASES = _alias_index(prompts.STYLES)
_POSITION_ALIASES = _alias_index(prompts.POSITIONS)
_CANVAS_ALIASES = _alias_index(prompts.RATIO_WORDS)
_CANVAS_ALIASES["1:1"] = "square"
_CANVAS_ALIASES["1x1"] = "square"


# ---------------------------------------------------------------------------
# Errors / warnings
# ---------------------------------------------------------------------------


class IntentError(ValueError):
    """Structured failure of the intent layer (bad input or unusable intent)."""

    def __init__(self, code, message):
        self.code = str(code)
        self.message = str(message)
        super().__init__(f"{self.code}: {self.message}")

    def to_dict(self):
        return {"code": self.code, "message": self.message}


def _warn(code, message):
    return {"severity": "warning", "code": str(code), "message": str(message)}


def _error(stage, code, message):
    return {"stage": str(stage), "code": str(code), "message": str(message)[:800]}


# ---------------------------------------------------------------------------
# Few-shot intent prompt
# ---------------------------------------------------------------------------

INTENT_SYSTEM_PROMPT = (
    "You are an intent parser for a deterministic drawing planner. "
    "Read the user prompt (Chinese or English) and reply with ONE JSON object "
    "and nothing else: no markdown fences, no prose, no comments.\n"
    "Shape:\n"
    '{"subject":["cat"],"count":1,"colors":["black"],"style":"mono_ink",'
    '"text":[],"position":"center","canvas":"square","negations":[],'
    '"confidence":0.9}\n'
    "Allowed values (use the English key, never a Chinese word):\n"
    "subject: circle|star|heart|cloud|mountain|tree|house|cat|flower|sun|"
    "moon|wave|snow|eye|geometry (list in prompt order, at most 3; [] if none)\n"
    "count: integer 1..12 (default 1)\n"
    "colors: black|white|gray|red|blue|green|gold|purple|orange|pink|cyan|"
    "brown|yellow or \"#rrggbb\" (at most 2, prompt order; [] if none)\n"
    "style: mono_ink|manga_color|pencil|ink_wash|marker|oil|watercolor|"
    "minimal|cyber, or null\n"
    "text: at most 2 exact strings the user wants written (from quotes or "
    "after '文字:'/'text:'); [] if none\n"
    "position: center|left|right|top|bottom or null\n"
    "canvas: square|portrait|landscape or null\n"
    "negations: any of \"no_text\",\"no_background\"; [] if none\n"
    "confidence: number 0..1 (how sure you are)\n"
    "Examples:\n"
    "user: 画一只黑色的猫，线稿风格，居中\n"
    'assistant: {"subject":["cat"],"count":1,"colors":["black"],'
    '"style":"mono_ink","text":[],"position":"center","canvas":null,'
    '"negations":[],"confidence":0.92}\n'
    "user: a golden star, minimal style, centered, square\n"
    'assistant: {"subject":["star"],"count":1,"colors":["gold"],'
    '"style":"minimal","text":[],"position":"center","canvas":"square",'
    '"negations":[],"confidence":0.9}\n'
    "user: 画一朵粉色的花，水彩风格，文字：\"春天\"，竖版\n"
    'assistant: {"subject":["flower"],"count":1,"colors":["pink"],'
    '"style":"watercolor","text":["春天"],"position":null,'
    '"canvas":"portrait","negations":[],"confidence":0.9}\n'
    "user: 五颗紫色的星星，深色背景，赛博朋克，方形，不要文字\n"
    'assistant: {"subject":["star"],"count":5,"colors":["purple"],'
    '"style":"cyber","text":[],"position":null,"canvas":"square",'
    '"negations":["no_text","no_background"],"confidence":0.85}\n'
    "user: draw a blue wave and a sun, landscape, no text\n"
    'assistant: {"subject":["wave","sun"],"count":1,"colors":["blue"],'
    '"style":null,"text":[],"position":null,"canvas":"landscape",'
    '"negations":["no_text"],"confidence":0.8}'
)


def build_intent_messages(prompt):
    """Chat messages (system few-shot + user prompt) for intent extraction."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise IntentError("FP_E_PROMPT_EMPTY", "prompt must be a non-empty string")
    return [
        {"role": "system", "content": INTENT_SYSTEM_PROMPT},
        {"role": "user", "content": prompt.strip()},
    ]


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _resolve(value, table, aliases):
    """Resolve a canonical key, an alias, or a phrase containing one."""
    if not isinstance(value, str):
        return None
    token = value.strip().lower()
    if not token:
        return None
    if token in aliases:
        return aliases[token]
    matches = prompts.find_terms(value, table)
    if matches:
        return matches[0]["key"]
    return None


def _normalize_subjects(value, prompt, warnings):
    subjects = []
    for item in _as_list(value):
        key = _resolve(item, prompts.SUBJECTS, _SUBJECT_ALIASES)
        if key is None:
            warnings.append(
                _warn("FP_W_INTENT_SUBJECT_UNKNOWN", f"unknown subject {item!r}; dropped")
            )
            continue
        if key not in subjects:
            subjects.append(key)
    if len(subjects) > 3:
        warnings.append(
            _warn("FP_W_INTENT_SUBJECT_TRUNCATED", f"{len(subjects)} subjects; kept first 3")
        )
        subjects = subjects[:3]
    if not subjects and prompt:
        derived = []
        for match in prompts.find_terms(prompt, prompts.SUBJECTS):
            if match["key"] not in derived:
                derived.append(match["key"])
        if derived:
            subjects = derived[:3]
            warnings.append(
                _warn(
                    "FP_W_INTENT_FIELD_FALLBACK",
                    "subject recovered from the prompt text: " + ", ".join(subjects),
                )
            )
    return subjects


def _normalize_colors(value, prompt, warnings):
    colors = []
    for item in _as_list(value):
        if not isinstance(item, str):
            warnings.append(_warn("FP_W_INTENT_COLOR_UNKNOWN", f"colour {item!r} is not a string; dropped"))
            continue
        token = item.strip()
        hex_match = re.fullmatch(r"#?([0-9a-fA-F]{6})", token)
        if hex_match:
            hex_value = "#" + hex_match.group(1).lower()
            if hex_value not in colors:
                colors.append(hex_value)
            continue
        key = _resolve(token, prompts.COLORS, _COLOR_ALIASES)
        if key is None:
            warnings.append(_warn("FP_W_INTENT_COLOR_UNKNOWN", f"unknown colour {item!r}; dropped"))
            continue
        if key not in colors:
            colors.append(key)
    if not colors and prompt:
        derived = []
        for match in prompts.find_terms(prompt, prompts.COLORS):
            if match["key"] not in derived:
                derived.append(match["key"])
        if derived:
            colors = derived[:2]
            warnings.append(
                _warn(
                    "FP_W_INTENT_FIELD_FALLBACK",
                    "colors recovered from the prompt text: " + ", ".join(colors),
                )
            )
    if len(colors) > 2:
        warnings.append(_warn("FP_W_INTENT_COLOR_TRUNCATED", f"{len(colors)} colours; kept first 2"))
        colors = colors[:2]
    return colors


def _normalize_style(value, prompt, warnings):
    if value is not None:
        key = _resolve(value, prompts.STYLES, _STYLE_ALIASES)
        if key is not None:
            return key
        warnings.append(_warn("FP_W_INTENT_STYLE_UNKNOWN", f"unknown style {value!r}; dropped"))
    if prompt:
        key, alias = prompts.detect_style_name(prompt)
        if key is not None:
            warnings.append(
                _warn(
                    "FP_W_INTENT_FIELD_FALLBACK",
                    f"style recovered from the prompt text: {key} ({alias!r})",
                )
            )
            return key
    return None


def _normalize_position(value, warnings):
    if value is None:
        return None
    key = _resolve(value, prompts.POSITIONS, _POSITION_ALIASES)
    if key is None:
        warnings.append(_warn("FP_W_INTENT_POSITION_UNKNOWN", f"unknown position {value!r}; dropped"))
    return key


def _normalize_text(value, warnings):
    items = _as_list(value)
    texts = []
    for item in items:
        if not isinstance(item, str):
            warnings.append(_warn("FP_W_INTENT_TEXT_INVALID", f"text {item!r} is not a string; dropped"))
            continue
        content = item.strip().strip('"“”\'‘’「」『』 ').strip()
        content = re.sub(r"\s+", " ", content)
        if not content:
            continue
        if len(content) > 40:
            warnings.append(
                _warn("FP_W_INTENT_TEXT_TRUNCATED", f"text {content[:12]!r}... truncated to 40 characters")
            )
            content = content[:40]
        if content not in texts:
            texts.append(content)
    if len(texts) > 2:
        warnings.append(_warn("FP_W_INTENT_TEXT_TRUNCATED", f"{len(texts)} texts; kept first 2"))
        texts = texts[:2]
    return texts


def _normalize_negations(value, warnings):
    negations = []
    for item in _as_list(value):
        if isinstance(item, bool):
            warnings.append(_warn("FP_W_INTENT_NEGATION_UNKNOWN", f"negation {item!r}; ignored"))
            continue
        token = str(item).strip().lower()
        if not token:
            continue
        if "background" in token or "背景" in token:
            key = "no_background"
        elif "text" in token or "文字" in token or token in ("no words", "字", "no caption"):
            key = "no_text"
        else:
            warnings.append(
                _warn("FP_W_INTENT_NEGATION_UNKNOWN", f"unsupported negation {item!r}; ignored")
            )
            continue
        if key not in negations:
            negations.append(key)
    return negations


_CANVAS_SIZE_RE = re.compile(r"(\d{1,5})\s*[x×:]\s*(\d{1,5})")


def _closest_ratio(width, height):
    if width <= 0 or height <= 0:
        return None
    target = math.log(float(width) / float(height))
    best, best_delta = None, None
    for key, (preset_w, preset_h) in prompts.CANVAS_PRESETS.items():
        delta = abs(math.log(float(preset_w) / float(preset_h)) - target)
        if best_delta is None or delta < best_delta:
            best, best_delta = key, delta
    return best


def _normalize_canvas(value, warnings):
    if value is None:
        return None
    if isinstance(value, dict):
        try:
            width = int(value.get("width"))
            height = int(value.get("height"))
        except (TypeError, ValueError):
            warnings.append(_warn("FP_W_INTENT_CANVAS_UNKNOWN", f"invalid canvas {value!r}; dropped"))
            return None
        key = _closest_ratio(width, height)
        if key is not None:
            warnings.append(
                _warn(
                    "FP_W_INTENT_CANVAS_SNAPPED",
                    f"canvas {width}x{height} snapped to preset {key}",
                )
            )
        return key
    if not isinstance(value, str):
        warnings.append(_warn("FP_W_INTENT_CANVAS_UNKNOWN", f"canvas {value!r}; dropped"))
        return None
    token = value.strip().lower()
    if not token:
        return None
    if token in _CANVAS_ALIASES:
        return _CANVAS_ALIASES[token]
    size_match = _CANVAS_SIZE_RE.search(token)
    if size_match:
        key = _closest_ratio(int(size_match.group(1)), int(size_match.group(2)))
        if key is not None:
            warnings.append(
                _warn("FP_W_INTENT_CANVAS_SNAPPED", f"canvas {value!r} snapped to preset {key}")
            )
        return key
    key = _resolve(value, prompts.RATIO_WORDS, _CANVAS_ALIASES)
    if key is None:
        warnings.append(_warn("FP_W_INTENT_CANVAS_UNKNOWN", f"unknown canvas {value!r}; dropped"))
    return key


def _normalize_count(value, warnings):
    if value is None:
        return 1
    count = None
    if isinstance(value, bool):
        count = None
    elif isinstance(value, int):
        count = value
    elif isinstance(value, float):
        if value.is_integer():
            count = int(value)
    elif isinstance(value, str):
        token = value.strip().lower()
        if token.isdigit():
            count = int(token)
        else:
            count = prompts.NUMBER_WORDS.get(token)
    if count is None:
        warnings.append(_warn("FP_W_INTENT_COUNT_INVALID", f"count {value!r}; default 1"))
        return 1
    if count < 1:
        warnings.append(_warn("FP_W_INTENT_COUNT_CLAMPED", f"count {count}; clamped to 1"))
        return 1
    if count > prompts.MAX_COUNT:
        warnings.append(
            _warn("FP_W_INTENT_COUNT_CLAMPED", f"count {count}; clamped to {prompts.MAX_COUNT}")
        )
        return prompts.MAX_COUNT
    return count


def _normalize_confidence(value, warnings):
    confidence = None
    if isinstance(value, bool) or value is None:
        confidence = None
    elif isinstance(value, (int, float)):
        confidence = float(value)
    elif isinstance(value, str):
        try:
            confidence = float(value.strip())
        except ValueError:
            confidence = None
    if confidence is None:
        if value is not None:
            warnings.append(_warn("FP_W_INTENT_CONFIDENCE_INVALID", f"confidence {value!r}; set 0.0"))
        return 0.0
    if not math.isfinite(confidence):
        warnings.append(_warn("FP_W_INTENT_CONFIDENCE_INVALID", f"confidence {value!r}; set 0.0"))
        return 0.0
    clamped = min(1.0, max(0.0, confidence))
    if clamped != confidence:
        warnings.append(
            _warn("FP_W_INTENT_CONFIDENCE_CLAMPED", f"confidence {confidence}; clamped to {clamped}")
        )
    return round(clamped, 3)


# ---------------------------------------------------------------------------
# Public normalisation
# ---------------------------------------------------------------------------


def normalize_intent(payload, *, prompt=None):
    """Validate/normalise one model payload into the 9-field intent dict.

    Returns ``(intent, warnings)``.  Never raises for *field* problems: illegal
    values fall back to defaults and are reported as warnings.  Unknown fields
    are dropped with a warning.  ``prompt`` (the original user text) is used
    only to recover missing subjects/colors/style from the prompt lexicon.
    """
    if not isinstance(payload, dict):
        raise IntentError(
            "FP_E_INTENT_TYPE", f"intent must be a JSON object (got {type(payload).__name__})"
        )
    if isinstance(payload.get("intent"), dict):
        payload = payload["intent"]
    warnings = []
    merged = {}
    unknown = []
    for raw_key, value in payload.items():
        canonical = FIELD_ALIASES.get(str(raw_key).strip().lower())
        if canonical is None:
            unknown.append(str(raw_key))
            continue
        merged.setdefault(canonical, value)
    if unknown:
        warnings.append(
            _warn("FP_W_INTENT_UNKNOWN_FIELD", "unknown intent fields dropped: " + ", ".join(sorted(unknown)))
        )

    subjects = _normalize_subjects(merged.get("subject"), prompt, warnings)
    colors = _normalize_colors(merged.get("colors"), prompt, warnings)
    style = _normalize_style(merged.get("style"), prompt, warnings)
    texts = _normalize_text(merged.get("text"), warnings)
    position = _normalize_position(merged.get("position"), warnings)
    canvas = _normalize_canvas(merged.get("canvas"), warnings)
    negations = _normalize_negations(merged.get("negations"), warnings)
    count = _normalize_count(merged.get("count"), warnings)
    confidence = _normalize_confidence(merged.get("confidence"), warnings)

    if "no_text" in negations and texts:
        warnings.append(
            _warn(
                "FP_W_INTENT_CONFLICT",
                "intent asks for text and 'no_text'; the explicit text wins",
            )
        )
        negations = [item for item in negations if item != "no_text"]

    return {
        "subject": subjects,
        "count": count,
        "colors": colors,
        "style": style,
        "text": texts,
        "position": position,
        "canvas": canvas,
        "negations": negations,
        "confidence": confidence,
    }, warnings


def intent_to_prompt(intent):
    """Rebuild a natural-language prompt the rule planner can parse.

    Style is *not* encoded as text: :func:`plan_from_intent` passes it through
    ``plan_prompt(style=...)``.  Everything else (count, colours, position,
    ratio, texts, negations) is written with the planner's Chinese aliases.
    """
    if not isinstance(intent, dict):
        raise IntentError("FP_E_INTENT_TYPE", "intent must be a dict")
    parts = []
    subjects = [SUBJECT_TERMS[key] for key in intent.get("subject") or [] if key in SUBJECT_TERMS]
    if subjects:
        subject_text = "、".join(subjects)
        count = intent.get("count") or 1
        if int(count) > 1:
            subject_text = f"{int(count)}个{subject_text}"
        parts.append(subject_text)
    for key in intent.get("colors") or []:
        if isinstance(key, str) and key.startswith("#"):
            parts.append(key)
        elif key in COLOR_TERMS:
            parts.append(COLOR_TERMS[key])
    position = intent.get("position")
    if position in POSITION_TERMS:
        parts.append(POSITION_TERMS[position])
    canvas = intent.get("canvas")
    if canvas in CANVAS_TERMS:
        parts.append(CANVAS_TERMS[canvas])
    for text in intent.get("text") or []:
        parts.append(f'文字："{text}"')
    for negation in intent.get("negations") or []:
        if negation == "no_text":
            parts.append("不要文字")
        elif negation == "no_background":
            parts.append("不要背景")
    return "，".join(part for part in parts if part)


# ---------------------------------------------------------------------------
# Generation / extraction
# ---------------------------------------------------------------------------


def _load_model(model_dir, device):
    """Indirection so tests can patch model loading without torch."""
    from .infer import load_model

    return load_model(model_dir, device=device)


def _generate_raw(model, tokenizer, messages, *, max_new_tokens, device, seed):
    """Indirection over :func:`fourier_prompt.llm.infer._generate_raw`."""
    from .infer import _generate_raw as generate

    return generate(
        model,
        tokenizer,
        messages,
        max_new_tokens=max_new_tokens,
        device=device,
        seed=seed,
    )


def _normalise_device(device):
    token = str(device or "").strip().lower()
    if not token:
        raise IntentError("FP_E_DEVICE", "device must be 'cuda' or 'cpu'")
    if token != "cpu" and not token.startswith("cuda"):
        raise IntentError("FP_E_DEVICE", f"unsupported device {device!r} (use 'cuda' or 'cpu')")
    return token


def extract_intent(
    prompt,
    *,
    model_dir=None,
    device="cuda",
    max_new_tokens=DEFAULT_MAX_NEW_TOKENS,
    seed=0,
    generator=None,
    model=None,
    tokenizer=None,
):
    """Run the local LLM once and return ``(intent, meta)``.

    ``intent`` is ``None`` when the model/JSON failed; ``meta["fallback"]`` then
    tells the caller to use :func:`plan_from_prompt` with the original prompt.
    ``generator`` (``callable(messages, *, max_new_tokens, seed) -> str``), and
    pre-loaded ``model``/``tokenizer`` are test/offline hooks.
    """
    started = time.perf_counter()
    messages = build_intent_messages(prompt)
    device = _normalise_device(device)
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise IntentError("FP_E_SEED", f"seed must be an integer (got {seed!r})")
    max_new_tokens = int(max_new_tokens)
    if max_new_tokens < MAX_NEW_TOKENS_MIN:
        raise IntentError("FP_E_MAX_NEW_TOKENS", f"max_new_tokens must be >= {MAX_NEW_TOKENS_MIN}")

    meta = {
        "ok": False,
        "source": "llm",
        "json_ok": False,
        "fallback": True,
        "model_dir": model_dir,
        "device": device,
        "seed": seed,
        "attempts": 0,
        "max_new_tokens": max_new_tokens,
        "latency_s": 0.0,
        "raw_chars": 0,
        "raw": None,
        "errors": [],
        "warnings": [],
        "confidence": 0.0,
    }
    errors = meta["errors"]

    try:
        if generator is not None:
            raw = generator(messages, max_new_tokens=max_new_tokens, seed=seed)
        else:
            if model is None or tokenizer is None:
                try:
                    model, tokenizer = _load_model(
                        model_dir or _default_model_dir(), device
                    )
                except Exception as exc:  # missing model dir / torch stack
                    errors.append(
                        _error("load", "FP_E_MODEL_UNAVAILABLE", f"{type(exc).__name__}: {exc}")
                    )
                    meta["latency_s"] = round(time.perf_counter() - started, 3)
                    return None, meta
            try:
                raw = _generate_raw(
                    model,
                    tokenizer,
                    messages,
                    max_new_tokens=max_new_tokens,
                    device=device,
                    seed=seed,
                )
            except Exception as exc:
                errors.append(_error("generate", type(exc).__name__, str(exc)))
                meta["latency_s"] = round(time.perf_counter() - started, 3)
                return None, meta
    except IntentError:
        raise
    except Exception as exc:  # generator hook failed
        errors.append(_error("generate", type(exc).__name__, str(exc)))
        meta["latency_s"] = round(time.perf_counter() - started, 3)
        return None, meta

    meta["attempts"] = 1
    meta["raw_chars"] = len(raw) if isinstance(raw, str) else 0
    meta["raw"] = raw[:800] if isinstance(raw, str) else None
    try:
        payload = extract_json(raw)
    except ExtractJSONError as exc:
        errors.append(_error("extract", exc.code, f"{exc.message} | snippet={exc.snippet!r}"))
        meta["latency_s"] = round(time.perf_counter() - started, 3)
        return None, meta

    meta["json_ok"] = True
    try:
        intent, warnings = normalize_intent(payload, prompt=prompt)
    except IntentError as exc:
        errors.append(_error("normalize", exc.code, exc.message))
        meta["latency_s"] = round(time.perf_counter() - started, 3)
        return None, meta

    meta["warnings"] = list(warnings)
    meta["confidence"] = intent["confidence"]
    meta["ok"] = True
    meta["fallback"] = False
    meta["latency_s"] = round(time.perf_counter() - started, 3)
    return intent, meta


def _default_model_dir():
    from .infer import default_model_dir

    return default_model_dir()


# ---------------------------------------------------------------------------
# Rule planner plumbing (no geometry is duplicated here)
# ---------------------------------------------------------------------------


def _planner_with_style(planner, style):
    """Bind ``style=`` when the planner accepts it (WP-N does; fakes may not)."""
    if not style:
        return planner
    try:
        import inspect

        parameters = inspect.signature(planner).parameters
    except (TypeError, ValueError):
        return planner
    accepts = "style" in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    if not accepts:
        return planner
    from functools import partial

    return partial(planner, style=style)


def plan_from_intent(intent, *, seed=0, planner_fn=None):
    """Rebuild a prompt from ``intent`` and draw it with the rule planner.

    Returns ``(plan, warnings)`` where warnings are the planner warnings plus
    hybrid-layer warnings (e.g. count ignored for multi-subject plans).
    """
    if not isinstance(intent, dict):
        raise IntentError("FP_E_INTENT_TYPE", f"intent must be a dict (got {type(intent).__name__})")
    warnings = []
    style = intent.get("style")
    if style is not None and style not in prompts.STYLE_PACKS:
        warnings.append(_warn("FP_W_INTENT_STYLE_UNKNOWN", f"unknown style {style!r}; ignored"))
        style = None
    prompt = intent_to_prompt(intent)
    if not prompt:
        prompt = "抽象画"
        warnings.append(
            _warn("FP_W_INTENT_GENERIC_FALLBACK", "intent has no drawable slot; using a generic composition")
        )
    subjects = intent.get("subject") or []
    if len(subjects) > 1 and int(intent.get("count") or 1) > 1:
        warnings.append(
            _warn(
                "FP_W_INTENT_COUNT_IGNORED",
                "the rule planner ignores count for multi-subject prompts",
            )
        )
    planner = planner_fn if planner_fn is not None else plan_prompt
    plan, planner_warnings = call_planner(_planner_with_style(planner, style), prompt, seed=seed)
    return plan, list(planner_warnings) + warnings


def plan_from_prompt(prompt, *, seed=0, planner_fn=None):
    """Deterministic rule-planner path used by ``--rule-only`` and fallback."""
    planner = planner_fn if planner_fn is not None else plan_prompt
    return call_planner(planner, prompt, seed=seed)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m fourier_prompt.llm.intent",
        description=(
            "Hybrid prompt path: the local LLM extracts an intent JSON, the "
            "deterministic rule planner renders the FDSL plan."
        ),
        epilog=(
            "Exit codes: 0 plan produced, 1 compile/render/IO failure, "
            "2 usage error, 3 no plan (intent failed and the planner rejected the prompt)."
        ),
    )
    parser.add_argument("--prompt", "-p", metavar="TEXT", help="natural-language prompt (zh/en)")
    parser.add_argument("--out", metavar="PLAN.json", help="write the FDSL plan JSON")
    parser.add_argument("--render", metavar="OUT.png", help="render a PNG")
    parser.add_argument("--intent-out", metavar="INTENT.json", help="write the normalised intent JSON")
    parser.add_argument(
        "--report",
        metavar="REPORT.json",
        help="write the machine-readable run report (UTF-8, same payload as --json)",
    )
    parser.add_argument(
        "--model-dir",
        default=None,
        help="local Qwen directory (default: $FDP_LLM_MODEL or models/Qwen2.5-1.5B-Instruct)",
    )
    parser.add_argument("--device", default="cuda", help="cuda / cpu (default cuda)")
    parser.add_argument(
        "--max-new-tokens",
        type=int,
        default=DEFAULT_MAX_NEW_TOKENS,
        help=f"intent-generation budget (default {DEFAULT_MAX_NEW_TOKENS})",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--resolution",
        type=int,
        default=256,
        help=f"render long side in px (16..{MAX_RESOLUTION}, default 256)",
    )
    parser.add_argument(
        "--rule-only",
        action="store_true",
        help="skip the model and draw the original prompt with the rule planner",
    )
    parser.add_argument("--json", action="store_true", help="emit a machine-readable JSON report")
    parser.add_argument("--quiet", action="store_true", help="do not print the summary line")
    return parser


def _write_json(path, payload):
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _render_plan(plan, path, *, resolution=256):
    from PIL import Image

    from ..compile import compile_plan
    from ..render_fast import render

    document = compile_plan(plan, resolution=resolution)["fbs"]
    image = render(document, resolution=resolution)
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    Image.fromarray(image).save(path)


def _report(args, *, source, intent, intent_meta, plan, plan_warnings, compile_ok, render_ok, latency):
    return {
        "ok": True,
        "exit_code": EXIT_OK,
        "prompt": args.prompt,
        "source": source,
        "intent_ok": intent is not None,
        "fallback": bool(intent is None and source == "rule_fallback"),
        "intent": intent,
        "intent_warnings": list((intent_meta or {}).get("warnings") or []),
        "intent_meta": {
            key: (intent_meta or {}).get(key)
            for key in (
                "json_ok",
                "attempts",
                "latency_s",
                "raw_chars",
                "confidence",
                "model_dir",
                "device",
                "errors",
            )
        },
        "plan": plan_summary(plan) if plan else None,
        "plan_warnings": list(plan_warnings or []),
        "compile_ok": compile_ok,
        "render_ok": render_ok,
        "latency": latency,
        "outputs": {"plan": args.out, "png": args.render, "intent": args.intent_out},
    }


def main(argv=None):
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:  # argparse already printed help / usage
        return int(exc.code) if exc.code is not None else EXIT_OK

    if not args.prompt or not args.prompt.strip():
        parser.print_usage(sys.stderr)
        print("error: --prompt is required and must not be empty", file=sys.stderr)
        return EXIT_USAGE
    if not MAX_NEW_TOKENS_MIN <= args.max_new_tokens <= MAX_NEW_TOKENS_HARD:
        print(
            f"error: --max-new-tokens must be in [{MAX_NEW_TOKENS_MIN}, {MAX_NEW_TOKENS_HARD}]",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if not 16 <= args.resolution <= MAX_RESOLUTION:
        print(
            f"error: --resolution must be in [16, {MAX_RESOLUTION}] (got {args.resolution})",
            file=sys.stderr,
        )
        return EXIT_USAGE
    try:
        device = _normalise_device(args.device)
    except IntentError as exc:
        print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return EXIT_USAGE

    started = time.perf_counter()
    intent = None
    intent_meta = None
    source = "rule"
    plan = None
    warnings = []
    intent_s = 0.0
    plan_s = 0.0

    if not args.rule_only:
        try:
            intent_started = time.perf_counter()
            intent, intent_meta = extract_intent(
                args.prompt,
                model_dir=args.model_dir,
                device=device,
                max_new_tokens=args.max_new_tokens,
                seed=args.seed,
            )
            intent_s = round(time.perf_counter() - intent_started, 3)
        except PlannerError as exc:  # pragma: no cover - defensive
            intent, intent_meta = None, {"ok": False, "errors": [exc.to_dict()], "warnings": []}
        if intent is not None:
            plan_started = time.perf_counter()
            try:
                plan, warnings = plan_from_intent(intent, seed=args.seed)
                source = "llm"
            except (PlannerError, DatasetError):
                plan = None
            plan_s = round(time.perf_counter() - plan_started, 3)
        if plan is None:
            source = "rule_fallback"

    if plan is None:
        plan_started = time.perf_counter()
        try:
            plan, warnings = plan_from_prompt(args.prompt, seed=args.seed)
        except PlannerError as exc:
            payload = {
                "ok": False,
                "exit_code": EXIT_PLANNER,
                "prompt": args.prompt,
                "source": source,
                "intent": intent,
                "error": exc.to_dict(),
                "latency": {"intent_s": intent_s, "plan_s": plan_s},
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"planner error {exc.code}: {exc.message}", file=sys.stderr)
            if args.report:
                try:
                    _write_json(args.report, payload)
                except Exception as write_exc:  # pragma: no cover - IO edge
                    print(f"error: report write failed: {write_exc}", file=sys.stderr)
            return EXIT_PLANNER
        except DatasetError as exc:
            print(f"error: rule planner unavailable: {exc}", file=sys.stderr)
            return EXIT_FAILURE
        plan_s = round(time.perf_counter() - plan_started, 3)

    try:
        from ..compile import compile_plan

        compile_started = time.perf_counter()
        compile_plan(plan, resolution=args.resolution)
        compile_s = round(time.perf_counter() - compile_started, 3)
    except Exception as exc:
        print(f"error: compile failed: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    render_ok = None
    try:
        if args.intent_out:
            _write_json(
                args.intent_out,
                {"intent": intent, "meta": intent_meta, "source": source},
            )
        if args.out:
            _write_json(args.out, plan)
        if args.render:
            render_started = time.perf_counter()
            _render_plan(plan, args.render, resolution=args.resolution)
            render_s = round(time.perf_counter() - render_started, 3)
            render_ok = True
        else:
            render_s = 0.0
    except Exception as exc:
        print(f"error: write/render failed: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    latency = {
        "intent_s": intent_s,
        "plan_s": plan_s,
        "compile_s": compile_s,
        "render_s": render_s,
        "total_s": round(time.perf_counter() - started, 3),
    }
    report = _report(
        args,
        source=source,
        intent=intent,
        intent_meta=intent_meta,
        plan=plan,
        plan_warnings=warnings,
        compile_ok=True,
        render_ok=render_ok,
        latency=latency,
    )
    if args.report:
        try:
            _write_json(args.report, report)
        except Exception as write_exc:  # pragma: no cover - IO edge
            print(f"error: report write failed: {write_exc}", file=sys.stderr)
            return EXIT_FAILURE
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    elif not args.quiet:
        summary = {
            key: report[key]
            for key in ("source", "intent_ok", "fallback", "compile_ok", "render_ok")
        }
        summary["intent_warnings"] = len(report["intent_warnings"])
        summary["plan_warnings"] = len(report["plan_warnings"])
        summary["latency"] = latency
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    if args.out is None and not args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
