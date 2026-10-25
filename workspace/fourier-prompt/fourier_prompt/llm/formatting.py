"""Chat formatting, JSON extraction and label masking for the local LLM planner.

Everything in this module is **stdlib-only and torch-free**: it is used both by
the data builder (:mod:`fourier_prompt.llm.dataset`), by the trainer
(``scripts/train_lora.py``) and by the inference wrapper
(:mod:`fourier_prompt.llm.infer`).  Tokenizer objects are duck-typed; the
HuggingFace ``PreTrainedTokenizer`` interface and small fakes used by the tests
are both supported.
"""

from __future__ import annotations

import json
import re

#: System prompt shared by training and inference.  Keep it stable: changing it
#: invalidates existing adapters (the training chat format must match).
FDSL_SYSTEM_PROMPT = (
    "You are a deterministic FDSL v2.1 drawing-plan generator. "
    "Given a user prompt, reply with ONE JSON object and nothing else "
    "(no markdown fences, no prose, no comments).\n"
    "Required shape:\n"
    '{"schema":"fdsl/2.1","name":"short-name",'
    '"canvas":{"width":1024,"height":1024,"background":[255,255,255]},'
    '"palette":{"ink":"#12203a"},'
    '"layers":[{"id":0,"name":"base"}],'
    '"elements":[{"kind":"fourier_gen","layer":0,"gen":"circle",'
    '"transform":{"center":[512,512],"scale":0.5},"params":{"r":1.0},'
    '"brush":{"preset":"ink_fineliner","width":3.0,"color":"ink"}}],'
    '"texts":[],"constraints":{"max_elements":400}}\n'
    "Rules: schema must be \"fdsl/2.1\"; canvas width/height are integers in "
    "[16,8192]; colours are #rrggbb or a declared palette name; layer ids are "
    "unique and every element/text layer references a declared layer; element "
    "kind is one of fourier_gen|stroke|fill|text; fourier_gen.gen is one of "
    "circle|ellipse|arc|regular_polygon|star|rose|spiral|wave|lissajous|"
    "superellipse; stroke needs >=2 points, fill needs a poly of >=3 points; "
    "brush.preset is one of ink_fineliner|ink_gpen|ink_brush_rough|pencil_2b|"
    "pencil_hb|charcoal|marker_alcohol|marker_chisel|watercolor_wash|wet_oil|"
    "bristle_oil|chalk|dry_flat|sumi_e|spatter_brush|basic_round_hard|"
    "basic_round_soft|basic_airbrush|pastel|palette_knife. "
    "Use only these fields; coordinates are canvas pixels."
)

_FENCE_RE = re.compile(r"```[ \t]*(?:json|JSON)[ \t]*\r?\n?(.*?)```", re.DOTALL)
_FENCE_ANY_RE = re.compile(r"```[ \t]*\r?\n?(.*?)```", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")

#: Roles accepted by :func:`build_messages`.
ROLES = ("system", "user", "assistant")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ExtractJSONError(ValueError):
    """Raised when no usable JSON object can be recovered from model text."""

    def __init__(self, code, message, *, snippet=None):
        super().__init__(f"{code}: {message}")
        self.code = str(code)
        self.message = str(message)
        self.snippet = snippet

    def to_dict(self):
        return {"code": self.code, "message": self.message}


class FormattingError(ValueError):
    """Raised for invalid formatting inputs (empty prompt, empty plan, ...)."""


# ---------------------------------------------------------------------------
# Canonical plan JSON
# ---------------------------------------------------------------------------


def canonical_plan_json(plan, *, sort_keys=True):
    """Serialise ``plan`` as compact canonical JSON (no whitespace).

    ``sort_keys=True`` makes the output independent of dict insertion order so
    that the same plan always produces the same training target.
    """
    if not isinstance(plan, dict):
        raise FormattingError("plan must be a dict")
    try:
        return json.dumps(
            plan,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=bool(sort_keys),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise FormattingError(f"plan is not JSON-serialisable: {exc}") from exc


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def build_messages(prompt, *, feedback=None, system=None):
    """Return the chat messages (``system`` + ``user`` [+ feedback]) for ``prompt``.

    ``feedback`` is appended as an extra user turn describing why the previous
    attempt was rejected; :mod:`fourier_prompt.llm.infer` uses it for retries.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise FormattingError("prompt must be a non-empty string")
    messages = [
        {"role": "system", "content": system or FDSL_SYSTEM_PROMPT},
        {"role": "user", "content": prompt.strip()},
    ]
    if feedback:
        messages.append(
            {
                "role": "user",
                "content": (
                    "The previous answer was rejected. Fix it and reply with "
                    "ONE valid FDSL v2.1 JSON object only.\n"
                    f"{str(feedback).strip()}"
                ),
            }
        )
    return messages


def messages_to_text(messages):
    """Render messages with the simple fallback template (no tokenizer)."""
    parts = []
    for message in messages:
        role = message.get("role", "user")
        if role not in ROLES:  # pragma: no cover - internal misuse
            raise FormattingError(f"unknown role {role!r}")
        parts.append(f"<|{role}|>\n{message.get('content', '')}\n")
    return "".join(parts)


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------


def _common_prefix_len(left, right):
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


def _balanced_object(text, start):
    """Return the balanced ``{...}`` substring starting at ``start`` or None."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _iter_object_candidates(text):
    """Yield ``(candidate, source)`` JSON-object substrings in document order."""
    index = text.find("{")
    while index != -1:
        candidate = _balanced_object(text, index)
        if candidate is not None:
            yield candidate, "scan"
            index += len(candidate)
            continue
        index = text.find("{", index + 1)


def _maybe_unwrap(payload):
    """Unwrap ``{"plan": {...}}`` style envelopes emitted by some models."""
    if not isinstance(payload, dict):
        return payload
    if "schema" in payload:
        return payload
    if len(payload) == 1:
        only = next(iter(payload.values()))
        if isinstance(only, dict) and "schema" in only:
            return only
    return payload


def _try_parse(text):
    """Return ``(payload, None)`` or ``(None, error_string)``; never raises."""
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        first_error = f"{exc.msg} (line {exc.lineno} column {exc.colno})"
    repaired = _TRAILING_COMMA_RE.sub(r"\1", text)
    if repaired != text:
        try:
            return json.loads(repaired), None
        except json.JSONDecodeError as exc:
            return None, f"{exc.msg} (line {exc.lineno} column {exc.colno})"
    return None, first_error


def _candidate_texts(text):
    """Ordered candidate strings: fenced blocks first, then the raw text."""
    seen = set()
    ordered = []
    for pattern in (_FENCE_RE, _FENCE_ANY_RE):
        for match in pattern.finditer(text):
            body = match.group(1).strip()
            if body and body not in seen:
                seen.add(body)
                ordered.append(body)
    stripped = text.strip().lstrip("\ufeff").strip()
    if stripped and stripped not in seen:
        seen.add(stripped)
        ordered.append(stripped)
    return ordered


def extract_json(text):
    """Extract the first JSON object from ``text``.

    Tolerates `````json`` fences, prose before/after the object, BOM, trailing
    commas and a ``{"plan": {...}}`` envelope.  Raises
    :class:`ExtractJSONError` when no object can be recovered.
    """
    if not isinstance(text, str):
        raise ExtractJSONError("llm/json_type", f"expected str, got {type(text).__name__}")
    if not text.strip():
        raise ExtractJSONError("llm/json_empty", "model returned an empty string")

    last_error = None
    for candidate in _candidate_texts(text):
        if candidate.startswith("{"):
            payload, error = _try_parse(candidate)
            if payload is not None:
                payload = _maybe_unwrap(payload)
                if isinstance(payload, dict):
                    return payload
                last_error = f"top-level JSON is {type(payload).__name__}, expected object"
            else:
                last_error = error
        for object_text, _source in _iter_object_candidates(candidate):
            payload, error = _try_parse(object_text)
            if payload is not None:
                payload = _maybe_unwrap(payload)
                if isinstance(payload, dict):
                    return payload
                last_error = f"top-level JSON is {type(payload).__name__}, expected object"
            else:
                last_error = error

    snippet = text.strip()[:200]
    raise ExtractJSONError(
        "llm/json_invalid",
        f"no JSON object found in model output ({last_error or 'no candidate'})",
        snippet=snippet,
    )


# ---------------------------------------------------------------------------
# Tokenisation / label masking
# ---------------------------------------------------------------------------


def _apply_chat_template(tokenizer, messages, *, add_generation_prompt):
    renderer = getattr(tokenizer, "apply_chat_template", None)
    if renderer is None:
        return None
    kwargs = {"tokenize": False, "add_generation_prompt": bool(add_generation_prompt)}
    try:
        return renderer(messages, **kwargs)
    except TypeError:
        # Very old tokenizers do not accept add_generation_prompt.
        return renderer(messages, tokenize=False)


def _encode(tokenizer, text):
    """Encode ``text`` to a list of ints using a duck-typed tokenizer."""
    encoder = getattr(tokenizer, "encode", None)
    if callable(encoder):
        try:
            ids = encoder(text, add_special_tokens=False)
        except TypeError:
            ids = encoder(text)
        return [int(value) for value in ids]
    output = tokenizer(text, add_special_tokens=False)
    if isinstance(output, dict):
        ids = output["input_ids"]
    else:
        ids = getattr(output, "input_ids", None)
    return [int(value) for value in ids]


def _eos_text(tokenizer):
    eos = getattr(tokenizer, "eos_token", None)
    return eos if isinstance(eos, str) else ""


def tokenize_example(tokenizer, prompt, plan_json, *, max_len=4096):
    """Tokenise one training pair; prompt tokens get ``label = -100``.

    Returns ``{"input_ids", "attention_mask", "labels", "num_prompt_tokens",
    "num_assistant_tokens", "prompt_truncated", "assistant_truncated",
    "truncated"}``.  The assistant segment (canonical plan JSON + template EOS)
    is the only part that contributes to the loss.
    """
    if not isinstance(plan_json, str) or not plan_json.strip():
        raise FormattingError("plan_json must be a non-empty string")
    if isinstance(max_len, bool) or not isinstance(max_len, int) or max_len < 8:
        raise FormattingError("max_len must be an int >= 8")

    messages = build_messages(prompt)
    assistant = {"role": "assistant", "content": plan_json.strip()}

    prompt_text = _apply_chat_template(tokenizer, messages, add_generation_prompt=True)
    full_text = _apply_chat_template(
        tokenizer, messages + [assistant], add_generation_prompt=False
    )
    if prompt_text is None or full_text is None:
        prompt_text = messages_to_text(messages) + "<|assistant|>\n"
        full_text = prompt_text + plan_json.strip() + _eos_text(tokenizer)

    prompt_ids = _encode(tokenizer, prompt_text)
    full_ids = _encode(tokenizer, full_text)
    if not prompt_ids:
        raise FormattingError("tokenizer produced no prompt tokens")

    split = len(prompt_ids)
    if full_ids[:split] != prompt_ids:
        # Templates that are not a strict prefix (e.g. added BOS/EOS) degrade
        # gracefully to the longest common prefix.
        split = _common_prefix_len(prompt_ids, full_ids)
    if split >= len(full_ids):
        raise FormattingError("chat template produced no assistant tokens")

    assistant_ids = full_ids[split:]
    prompt_truncated = 0
    assistant_truncated = 0
    if len(prompt_ids) + len(assistant_ids) > max_len:
        room = max_len - len(assistant_ids)
        if room >= 1:
            prompt_truncated = len(prompt_ids) - room
            prompt_ids = prompt_ids[prompt_truncated:]
        else:
            keep = max_len - 1
            assistant_truncated = len(assistant_ids) - keep
            assistant_ids = assistant_ids[:keep]
            prompt_ids = prompt_ids[:1]

    input_ids = list(prompt_ids) + list(assistant_ids)
    labels = [-100] * len(prompt_ids) + list(assistant_ids)
    return {
        "input_ids": input_ids,
        "attention_mask": [1] * len(input_ids),
        "labels": labels,
        "num_prompt_tokens": len(prompt_ids),
        "num_assistant_tokens": len(assistant_ids),
        "prompt_truncated": prompt_truncated,
        "assistant_truncated": assistant_truncated,
        "truncated": bool(prompt_truncated or assistant_truncated),
    }
