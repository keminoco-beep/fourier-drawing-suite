"""Deterministic rule-based prompt → FDSL v2.1 planner (WP-N).

``plan_prompt("画一只黑色的猫，线稿风格，居中")`` returns ``(plan, warnings)``.
The planner is offline, stdlib-only and fully deterministic: every random
choice comes from ``random.Random(seed)``, never from the clock.

Contract
--------
* Unknown subject → generic geometric composition + ``FP_W_SUBJECT_UNKNOWN``
  warning (never a silent failure).
* Empty / unparseable prompt → :class:`PlannerError` with a stable
  ``FP_E_PROMPT_*`` code.
* Output is canonical FDSL v2.1 (``palette`` / ``layers`` / ``elements`` /
  ``texts``); text entries use ``as: "outline"`` and are validated with
  :func:`fourier_prompt.schema.validate_plan_detailed` before returning.

Geometry is composed from :mod:`fourier_prompt.generators` plus explicit
polylines; no bitmap tracing and no learned model is involved.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import random
import re

from . import generators, prompts
from .schema import FDSL_SCHEMA, validate_plan_detailed

__all__ = [
    "PlannerError",
    "plan_prompt",
    "plan_hash",
    "plan_summary",
    "MAX_PROMPT_CHARS",
    "MAX_TEXTS",
]

MAX_PROMPT_CHARS = 2000
MAX_TEXTS = 2

_TAU = 2.0 * math.pi


class PlannerError(ValueError):
    """Structured planner failure carrying a stable ``code``."""

    def __init__(self, code: str, message: str, *, path: str = "$.prompt"):
        self.code = str(code)
        self.message = str(message)
        self.path = str(path)
        super().__init__(f"{self.code} at {self.path}: {self.message}")

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "path": self.path}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def plan_prompt(prompt, *, style=None, seed=0):
    """Turn a natural-language prompt into ``(plan, warnings)``.

    ``style`` overrides the style detected in the prompt (unknown names fall
    back to the default pack with a warning).  ``seed`` drives the only random
    choices (generic-composition decorative dots).
    """
    if not isinstance(prompt, str):
        raise PlannerError("FP_E_PROMPT_TYPE", f"prompt must be a string (got {type(prompt).__name__})")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise PlannerError("FP_E_SEED_TYPE", f"seed must be an integer (got {seed!r})", path="$.seed")
    if style is not None and not isinstance(style, str):
        raise PlannerError("FP_E_STYLE_TYPE", f"style must be a string or None (got {style!r})", path="$.style")

    text = prompts.normalize_prompt(prompt)
    if not text:
        raise PlannerError("FP_E_PROMPT_EMPTY", "prompt is empty or whitespace-only")
    if len(text) > MAX_PROMPT_CHARS:
        raise PlannerError(
            "FP_E_PROMPT_TOO_LONG",
            f"prompt is {len(text)} characters; the deterministic planner accepts <= {MAX_PROMPT_CHARS}",
        )
    if not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", text):
        raise PlannerError(
            "FP_E_PROMPT_UNPARSEABLE",
            "prompt has no letters, digits or CJK characters; nothing can be parsed",
        )

    warnings = []
    rng = random.Random(seed)

    # 1. Text extraction (quoted strings / 文字: ...), removed from the
    #    parsing surface so text content cannot be mistaken for subjects.
    clean, texts = _extract_texts(text, warnings)

    # 2. Negations (no text / no background / unknown negations).
    clean, negated = _apply_negations(clean, warnings)

    # 3. Style: explicit argument > prompt detection > default.
    detected_style, _detected_alias = prompts.detect_style_name(clean)
    style_key = None
    if style is not None:
        style_key, _pack = prompts.resolve_style(style)
        if style_key is None:
            warnings.append(
                _warn("FP_W_STYLE_UNKNOWN", f"unknown style {style!r}; falling back to the prompt/default style")
            )
    if style_key is None:
        style_key = detected_style or prompts.DEFAULT_STYLE
    pack = prompts.STYLE_PACKS[style_key]
    # Style words describe the treatment, not the subject (``黑白`` must not
    # become "black + white object colours"): mask them before slot parsing.
    clean = _mask_spans(clean, prompts.find_terms(clean, prompts.STYLES))

    # 4. Colours, ratio, subjects, layout.
    colors = _detect_colors(clean, negated)
    ratio = _detect_ratio(clean)
    width, height = prompts.CANVAS_PRESETS[ratio]
    background_color = _detect_background(clean, colors)
    subject_matches = prompts.find_terms(clean, prompts.SUBJECTS)
    subject_keys = []
    for match in subject_matches:
        if match["key"] not in subject_keys:
            subject_keys.append(match["key"])
    if len(subject_keys) > 3:
        warnings.append(
            _warn(
                "FP_W_SUBJECT_TRUNCATED",
                f"prompt names {len(subject_keys)} subjects; only the first 3 are composed",
            )
        )
        subject_keys = subject_keys[:3]

    if not subject_keys:
        warnings.append(
            _warn(
                "FP_W_SUBJECT_UNKNOWN",
                "no known subject in the prompt; using a generic geometric composition",
            )
        )

    count = None
    if subject_matches:
        count = prompts.parse_count_before(clean, subject_matches[0]["start"])
        if count is None:
            after = clean[subject_matches[0]["end"]:subject_matches[0]["end"] + 6]
            after_match = re.search(r"^\s*(\d{1,3})\s*(?:个|颗|只|朵|座|棵|片|轮|枚|条|张)?", after)
            if after_match:
                count = int(after_match.group(1))
    if count is None:
        # Fallback: an explicit measure phrase anywhere in the prompt
        # (e.g. "紫色的星星，网格排列，9个").
        measure = _find_measure_count(clean)
        count = measure if measure is not None else 1
    if count < 1:
        count = 1
    if count > prompts.MAX_COUNT:
        warnings.append(
            _warn(
                "FP_W_COUNT_CLAMPED",
                f"requested {count} copies; clamped to {prompts.MAX_COUNT}",
            )
        )
        count = prompts.MAX_COUNT

    layout = _detect_layout(clean)
    position = _detect_position(clean)

    # 5. Palette: style pack + prompt colours.  Explicit colours win even in
    #    mono_ink (the user asked for them); the rest of the pack stays.
    palette = dict(pack["palette"])
    palette.setdefault("detail", palette["ink"])
    _apply_colors(palette, colors, fill_alpha=float(pack["fill_alpha"]))
    if background_color:
        palette["paper"] = background_color

    # 6. Build geometry.
    builder = _Builder(width, height, pack, palette, rng)
    text_band = _text_band_height(height, len(texts))
    _compose_subjects(
        builder,
        subject_keys or ["generic"],
        count=count,
        layout=layout,
        position=position,
        text_band=text_band,
    )
    elements = builder.elements

    # 7. Text entries (outline mode, baked by the compiler).
    text_entries = _build_texts(width, height, len(texts), texts, pack)

    plan = {
        "schema": FDSL_SCHEMA,
        "name": _plan_name(subject_keys or ["generic"], style_key, seed),
        "description": _describe(prompt, subject_keys or ["generic"], style_key),
        "canvas": {
            "width": width,
            "height": height,
            "background": pack.get("background", "paper"),
        },
        "palette": {key: palette[key] for key in sorted(palette)},
        "layers": [
            {"id": 0, "name": "fills", "order": 0},
            {"id": 1, "name": "ink", "order": 1},
            {"id": 2, "name": "text", "order": 2},
        ],
        "elements": elements,
        "texts": text_entries,
        "constraints": {"max_elements": 400},
        "seed": seed,
        "meta": {
            "generator": "rule_planner_v1",
            "seed": seed,
            "style": style_key,
            "subjects": subject_keys or ["generic"],
            "colors": [key for key, _hex in colors],
            "ratio": ratio,
            "layout": layout,
            "prompt": prompt[:500],
        },
    }
    if not text_entries:
        del plan["texts"]

    report = validate_plan_detailed(plan)
    if not report["ok"]:
        first = report["errors"][0]
        raise PlannerError(
            "FP_E_INTERNAL",
            f"planner produced an invalid FDSL plan: {first.get('code')} at {first.get('path')}: {first.get('message')}",
            path="$",
        )
    return plan, warnings


def plan_hash(plan) -> str:
    """Stable SHA-256 of the canonical JSON form (same plan → same hash)."""
    payload = json.dumps(plan, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def plan_summary(plan) -> dict:
    """Small, deterministic summary used by the CLI / benchmark."""
    meta = plan.get("meta") or {}
    kinds: dict[str, int] = {}
    for element in plan.get("elements") or []:
        kind = str(element.get("kind", "?"))
        kinds[kind] = kinds.get(kind, 0) + 1
    return {
        "name": plan.get("name"),
        "style": meta.get("style"),
        "subjects": meta.get("subjects"),
        "colors": meta.get("colors"),
        "ratio": meta.get("ratio"),
        "layout": meta.get("layout"),
        "canvas": [plan["canvas"]["width"], plan["canvas"]["height"]],
        "elements": len(plan.get("elements") or []),
        "texts": len(plan.get("texts") or []),
        "kinds": kinds,
    }


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_QUOTE_RE = re.compile(r'["“”\'‘’「」『』]([^"“”\'‘’「」『』\n]{1,60})["“”\'‘’「」『』]')
_TEXT_PREFIX_RE = re.compile(
    r"(?:文字|文本|写上|写着|标题|text|write|caption)\s*[:：]\s*([^,，。;；\n]{1,60})",
    re.IGNORECASE,
)
_NO_TEXT_RE = re.compile(
    r"(?:不要|没有|不带|无|去掉|去除|删除|排除|no|without)\s*(?:任何)?\s*"
    r"(?:文字|文本|字|text|words|caption)",
    re.IGNORECASE,
)
_NO_BG_RE = re.compile(
    r"(?:不要|没有|无|去掉|去除|no|without)\s*(?:任何)?\s*(?:背景|background)",
    re.IGNORECASE,
)
_GENERIC_NEG_RE = re.compile(
    r"(?:不要|没有|不带|去掉|去除|删除|排除)\s*([\u4e00-\u9fffA-Za-z]{1,6})"
    r"|(?<![A-Za-z])(?:without|no)\s+([\u4e00-\u9fffA-Za-z]{1,6})",
    re.IGNORECASE,
)


def _extract_texts(text: str, warnings: list):
    """Return ``(clean_text, [str])`` with up to :data:`MAX_TEXTS` texts."""
    spans = []
    for match in _QUOTE_RE.finditer(text):
        spans.append((match.start(), match.end(), match.group(1)))
    for match in _TEXT_PREFIX_RE.finditer(text):
        spans.append((match.start(), match.end(), match.group(1)))
    spans.sort(key=lambda item: item[0])
    chosen = []
    last_end = -1
    for start, end, content in spans:
        if start < last_end:
            continue
        content = re.sub(r"\s+", " ", content).strip()
        content = content.strip('"“”\'‘’「」『』 ')
        if not content:
            continue
        chosen.append((start, end, content))
        last_end = end
        if len(chosen) >= MAX_TEXTS:
            break
    if len(chosen) > MAX_TEXTS:
        chosen = chosen[:MAX_TEXTS]
    texts = []
    for _start, _end, content in chosen:
        if len(content) > 40:
            warnings.append(
                _warn("FP_W_TEXT_TRUNCATED", f"text {content[:12]!r}... truncated to 40 characters")
            )
            content = content[:40]
        texts.append(content)
    clean = text
    for start, end, _content in reversed(chosen):
        clean = clean[:start] + " " + clean[end:]
    return re.sub(r"\s+", " ", clean).strip(), texts


def _apply_negations(text: str, warnings: list):
    """Handle no-text/no-background and record unsupported negations.

    Returns ``(clean_text, negated_tokens)``; negated tokens are colour keys
    that must not be applied.
    """
    clean = text
    negated = set()
    if _NO_TEXT_RE.search(clean):
        warnings.append(_warn("FP_W_NEGATION_APPLIED", "negative 'no text' request honoured"))
        clean = _NO_TEXT_RE.sub(" ", clean)
    if _NO_BG_RE.search(clean):
        warnings.append(_warn("FP_W_NEGATION_APPLIED", "negative 'no background' request honoured"))
        clean = _NO_BG_RE.sub(" ", clean)
    # Colour negations: drop the colour instead of applying it.  Spans are
    # collected first and removed back-to-front so offsets stay valid.
    spans = []
    for match in _GENERIC_NEG_RE.finditer(clean):
        token = match.group(1) or match.group(2)
        color_matches = prompts.find_terms(token, prompts.COLORS)
        if color_matches and color_matches[0]["start"] == 0:
            negated.add(color_matches[0]["key"])
            warnings.append(
                _warn("FP_W_NEGATION_APPLIED", f"colour {token!r} was negated and is not applied")
            )
        else:
            warnings.append(
                _warn(
                    "FP_W_NEGATION_UNSUPPORTED",
                    f"negation {match.group(0).strip()!r} is not supported; the phrase was ignored",
                )
            )
        spans.append((match.start(), match.end()))
    for start, end in sorted(spans, reverse=True):
        clean = clean[:start] + " " + clean[end:]
    return re.sub(r"\s+", " ", clean).strip(), negated


def _mask_spans(text: str, matches) -> str:
    """Blank out matched spans (style words) and squeeze whitespace."""
    if not matches:
        return text
    chars = list(text)
    for match in matches:
        for index in range(match["start"], match["end"]):
            chars[index] = " "
    return re.sub(r"\s+", " ", "".join(chars)).strip()


def _detect_colors(text: str, negated: set):
    """Return ordered ``[(color_key, hex)]`` from words and explicit hex codes."""
    found = []
    words = prompts.find_terms(text, prompts.COLORS)
    hexes = prompts.find_hex_colors(text)
    events = [(m["start"], "word", m["key"]) for m in words] + [(text.lower().find(h), "hex", h) for h in hexes]
    events.sort(key=lambda item: item[0])
    for _pos, kind, value in events:
        if kind == "word":
            if value in negated:
                continue
            found.append((value, prompts.COLOR_HEX[value]))
        else:
            found.append(("custom", value))
    # Deduplicate by hex, keep first occurrence.
    seen = set()
    unique = []
    for key, hex_value in found:
        if hex_value in seen:
            continue
        seen.add(hex_value)
        unique.append((key, hex_value))
    return unique[:2]


def _detect_ratio(text: str) -> str:
    matches = prompts.find_terms(text, prompts.RATIO_WORDS)
    if not matches:
        return "square"
    return matches[0]["key"]


def _detect_position(text: str):
    matches = prompts.find_terms(text, prompts.POSITIONS)
    if not matches:
        return None
    # ``center`` must win only when it actually appears first; find_terms
    # already returns prompt order.
    return matches[0]["key"]


def _detect_layout(text: str):
    matches = prompts.find_terms(text, prompts.LAYOUT_WORDS)
    if not matches:
        return None
    return matches[0]["key"]


def _find_measure_count(text: str):
    """Find ``9个`` / ``三颗`` anywhere in the prompt (count fallback)."""
    match = re.search(
        r"(\d{1,3}|[一二两三四五六七八九十])\s*(?:个|颗|只|朵|座|棵|片|轮|枚|条|张|位)",
        text.lower(),
    )
    if not match:
        return None
    token = match.group(1)
    if token.isdigit():
        return int(token)
    return prompts.NUMBER_WORDS.get(token)


def _detect_background(text: str, colors):
    """Return an explicit background hex when the prompt says ``X色背景``."""
    lowered = text.lower()
    for key, hex_value in colors:
        if key == "custom":
            continue
        for alias in prompts.COLORS[key]:
            for match in re.finditer(re.escape(alias.lower()), lowered):
                before = lowered[max(0, match.start() - 6):match.start()]
                after = lowered[match.end():match.end() + 6]
                before_ok = re.search(
                    r"(?:背景|background)\s*(?:是|为|:|：)?\s*$", before
                )
                after_ok = re.match(r"^[\s，,、]*(?:的)?\s*(?:背景|background)", after)
                if before_ok or after_ok:
                    return hex_value
    return None


def _apply_colors(palette: dict, colors, *, fill_alpha: float = 1.0):
    if not colors:
        palette["detail"] = _contrast_detail(
            palette["accent"], palette["paper"], palette["ink"], fill_alpha=fill_alpha
        )
        return
    palette["accent"] = colors[0][1]
    palette["accent2"] = colors[1][1] if len(colors) > 1 else _shade(colors[0][1], -0.28)
    palette["muted"] = _shade(colors[0][1], 0.45)
    if colors[0][0] == "white":
        palette["accent"] = "#ececec"
        palette["ink"] = "#2a2a2a"
    # Facial/interior details need contrast against the *painted* fill, which is
    # alpha-blended with the paper for translucent styles.
    palette["detail"] = _contrast_detail(
        palette["accent"], palette["paper"], palette["ink"], fill_alpha=fill_alpha
    )


def _luma(hex_color: str) -> float:
    body = hex_color.lstrip("#")
    red, green, blue = (int(body[index:index + 2], 16) / 255.0 for index in (0, 2, 4))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast_detail(accent: str, paper: str, ink: str, fill_alpha: float = 1.0) -> str:
    """Lighten details on dark painted fills; keep ink otherwise."""
    painted = float(fill_alpha) * _luma(accent) + (1.0 - float(fill_alpha)) * _luma(paper)
    if painted < 0.30 and _luma(paper) > 0.55:
        return "#f2f2f2"
    return ink


def _shade(hex_color: str, amount: float) -> str:
    body = hex_color.lstrip("#")
    rgb = [int(body[index:index + 2], 16) for index in (0, 2, 4)]
    if amount >= 0:
        rgb = [int(round(value + (255 - value) * amount)) for value in rgb]
    else:
        rgb = [int(round(value * (1.0 + amount))) for value in rgb]
    return "#{:02x}{:02x}{:02x}".format(*[max(0, min(255, value)) for value in rgb])


def _warn(code: str, message: str) -> dict:
    return {"severity": "warning", "code": code, "message": message}


def _text_band_height(height: int, text_count: int) -> float:
    if text_count <= 0:
        return height * 0.04
    return height * (0.16 + 0.07 * (text_count - 1))


def _plan_name(subjects, style_key, seed) -> str:
    safe = "_".join(subjects)[:60]
    return f"planner_{style_key}_{safe}_s{seed}"[:200]


def _describe(prompt: str, subjects, style_key) -> str:
    snippet = re.sub(r"\s+", " ", prompt).strip()[:120]
    return f"rule planner v1: {', '.join(subjects)} in {style_key} style; prompt={snippet!r}"[:200]


# ---------------------------------------------------------------------------
# Geometry builder
# ---------------------------------------------------------------------------


class _Builder:
    """Accumulates canonical FDSL elements with sequential ids."""

    def __init__(self, width, height, pack, palette, rng):
        self.width = float(width)
        self.height = float(height)
        self.long_side = float(max(width, height))
        self.pack = pack
        self.palette = palette
        self.rng = rng
        self.elements: list[dict] = []
        self._counter = 0
        self.line_width = max(1.6, float(pack["line_width"]) * self.long_side / 768.0)
        self.detail_width = max(1.0, float(pack["detail_width"]) * self.long_side / 768.0)
        self.fill_alpha = float(pack["fill_alpha"])
        self.line_alpha = float(pack["line_alpha"])

    # -- ids ----------------------------------------------------------------
    def _next_id(self) -> str:
        self._counter += 1
        return f"e{self._counter}"

    # -- brush records ------------------------------------------------------
    def _fill_brush(self):
        return {"preset": self.pack["fill_brush"], "alpha": round(self.fill_alpha, 3)}

    def _line_brush(self, color, width, alpha=None):
        return {
            "preset": self.pack["line_brush"],
            "width": round(float(width), 2),
            "color": color,
            "alpha": round(self.line_alpha if alpha is None else float(alpha), 3),
        }

    def _detail_brush(self, color, width=None, alpha=None):
        return {
            "preset": self.pack["detail_brush"],
            "width": round(float(self.detail_width if width is None else width), 2),
            "color": color,
            "alpha": round(self.line_alpha if alpha is None else float(alpha), 3),
        }

    # -- primitives ---------------------------------------------------------
    def fill_gen(self, gen, params, color="accent", alpha=None):
        element = {
            "kind": "fourier_gen",
            "id": self._next_id(),
            "layer": 0,
            "gen": gen,
            "params": _round_params(params),
            "color": color,
            "brush": self._fill_brush(),
        }
        if alpha is not None:
            element["brush"]["alpha"] = round(float(alpha), 3)
        self.elements.append(element)
        return element

    def line_gen(self, gen, params, color="ink", width=None, alpha=None):
        self.elements.append(
            {
                "kind": "fourier_gen",
                "id": self._next_id(),
                "layer": 1,
                "gen": gen,
                "params": _round_params(params),
                "closed": False,
                "brush": self._line_brush(color, self.line_width if width is None else width, alpha),
            }
        )

    def fill_poly(self, points, color="accent", alpha=None):
        element = {
            "kind": "fill",
            "id": self._next_id(),
            "layer": 0,
            "poly": _round_points(points),
            "color": color,
            "brush": self._fill_brush(),
        }
        if alpha is not None:
            element["brush"]["alpha"] = round(float(alpha), 3)
        self.elements.append(element)
        return element

    def line(self, points, color="ink", width=None, closed=False, alpha=None, detail=False):
        points = _round_points(points)
        brush = (
            self._detail_brush(color, width, alpha)
            if detail
            else self._line_brush(color, self.line_width if width is None else width, alpha)
        )
        self.elements.append(
            {
                "kind": "stroke",
                "id": self._next_id(),
                "layer": 1,
                "points": points,
                "closed": bool(closed),
                "brush": brush,
            }
        )

    def outline(self, gen, params, color="ink", width=None, closed=True):
        samples = 192 if gen not in ("regular_polygon", "star") else 0
        points = generators.contour(gen, params, samples=samples or 192)
        self.line(points, color=color, width=width, closed=closed)

    def arc(self, cx, cy, r, a0, a1, color="ink", width=None, alpha=None):
        self.line_gen(
            "arc",
            {"at": [cx, cy], "r": r, "from": a0, "to": a1},
            color=color,
            width=width,
            alpha=alpha,
        )

    def dot(self, cx, cy, r, color="ink", alpha=None, layer=0):
        self.elements.append(
            {
                "kind": "fourier_gen",
                "id": self._next_id(),
                "layer": layer,
                "gen": "circle",
                "params": _round_params({"at": [cx, cy], "r": max(0.5, r)}),
                "color": color,
                "brush": dict(self._fill_brush(), alpha=round(self.fill_alpha if alpha is None else float(alpha), 3)),
            }
        )


def _round_params(params: dict) -> dict:
    output = {}
    for key, value in params.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            output[key] = round(float(value), 2)
        elif isinstance(value, (list, tuple)) and len(value) == 2 and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in value
        ):
            output[key] = [round(float(v), 2) for v in value]
        else:
            output[key] = value
    return output


def _round_points(points) -> list:
    return [[round(float(x), 2), round(float(y), 2)] for x, y in points]


def _fit(points, cx, cy, width, height, uniform=True):
    """Centre an arbitrary point cloud in a ``width`` × ``height`` box."""
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max(max_x - min_x, 1e-9)
    span_y = max(max_y - min_y, 1e-9)
    scale_x = width / span_x
    scale_y = height / span_y
    if uniform:
        scale_x = scale_y = min(scale_x, scale_y)
    mid_x = (min_x + max_x) / 2.0
    mid_y = (min_y + max_y) / 2.0
    return [
        [cx + (x - mid_x) * scale_x, cy + (y - mid_y) * scale_y]
        for x, y in points
    ]


def _heart_points(cx, cy, r):
    raw = []
    for index in range(96):
        t = _TAU * index / 96
        x = 16.0 * math.sin(t) ** 3
        y = -(13.0 * math.cos(t) - 5.0 * math.cos(2 * t) - 2.0 * math.cos(3 * t) - math.cos(4 * t))
        raw.append([x, y])
    return _fit(raw, cx, cy, r * 1.8, r * 1.65)


def _rect(x, y, w, h):
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]


# ---------------------------------------------------------------------------
# Subject recipes
# ---------------------------------------------------------------------------


def _recipe_circle(b, cx, cy, r):
    b.fill_gen("circle", {"at": [cx, cy], "r": r * 0.9}, "accent")
    b.outline("circle", {"at": [cx, cy], "r": r * 0.9})


def _recipe_star(b, cx, cy, r):
    params = {"at": [cx, cy], "r": r, "ri": r * 0.45, "n": 5, "rot": -90}
    b.fill_gen("star", params, "accent")
    b.outline("star", params)


def _recipe_heart(b, cx, cy, r):
    points = _heart_points(cx, cy, r)
    b.fill_poly(points, "accent")
    b.line(points, color="ink", closed=True)


def _recipe_cloud(b, cx, cy, r):
    blobs = [
        (cx - 0.45 * r, cy + 0.12 * r, 0.34 * r),
        (cx, cy - 0.02 * r, 0.46 * r),
        (cx + 0.45 * r, cy + 0.10 * r, 0.33 * r),
        (cx + 0.06 * r, cy + 0.22 * r, 0.38 * r),
    ]
    for bx, by, br in blobs:
        b.fill_gen("circle", {"at": [bx, by], "r": br}, "accent", alpha=min(0.65, b.fill_alpha + 0.15))
    for bx, by, br in blobs[:3]:
        b.arc(bx, by, br, 185, 355, color="ink", alpha=0.8)


def _recipe_mountain(b, cx, cy, r):
    base = cy + 0.62 * r
    back = [[cx - 0.95 * r, base], [cx - 0.12 * r, cy - 0.85 * r], [cx + 0.55 * r, base]]
    front = [[cx - 0.35 * r, base], [cx + 0.32 * r, cy - 0.62 * r], [cx + 0.98 * r, base]]
    b.fill_poly(back, "muted")
    b.fill_poly(front, "accent")
    b.fill_poly(
        [
            [cx + 0.32 * r, cy - 0.62 * r],
            [cx + 0.08 * r, cy - 0.35 * r],
            [cx + 0.20 * r, cy - 0.30 * r],
            [cx + 0.40 * r, cy - 0.42 * r],
            [cx + 0.52 * r, cy - 0.36 * r],
        ],
        "highlight",
        alpha=0.9,
    )
    b.line(back, color="ink", closed=True)
    b.line(front, color="ink", closed=True)
    b.line([[cx - 1.05 * r, base], [cx + 1.05 * r, base]], color="ink", alpha=0.6)


def _recipe_tree(b, cx, cy, r):
    b.line([[cx - 0.9 * r, cy + 0.95 * r], [cx + 0.9 * r, cy + 0.95 * r]], color="ink", alpha=0.55)
    b.fill_poly(_rect(cx - 0.11 * r, cy + 0.05 * r, 0.22 * r, 0.9 * r), "wood")
    for bx, by, br in [
        (cx, cy - 0.32 * r, 0.52 * r),
        (cx - 0.38 * r, cy - 0.02 * r, 0.37 * r),
        (cx + 0.38 * r, cy - 0.02 * r, 0.37 * r),
        (cx, cy + 0.02 * r, 0.42 * r),
    ]:
        b.fill_gen("circle", {"at": [bx, by], "r": br}, "accent")
    b.outline("circle", {"at": [cx, cy - 0.32 * r], "r": 0.52 * r}, color="ink", width=b.detail_width)


def _recipe_house(b, cx, cy, r):
    b.line([[cx - 1.0 * r, cy + 0.85 * r], [cx + 1.0 * r, cy + 0.85 * r]], color="ink", alpha=0.55)
    wall = _rect(cx - 0.58 * r, cy - 0.05 * r, 1.16 * r, 0.9 * r)
    roof = [[cx - 0.74 * r, cy - 0.02 * r], [cx, cy - 0.82 * r], [cx + 0.74 * r, cy - 0.02 * r]]
    b.fill_poly(wall, "muted")
    b.fill_poly(roof, "accent")
    b.fill_poly(_rect(cx - 0.12 * r, cy + 0.28 * r, 0.24 * r, 0.57 * r), "wood")
    for wx in (cx - 0.42 * r, cx + 0.18 * r):
        b.fill_poly(_rect(wx - 0.10 * r, cy + 0.22 * r, 0.20 * r, 0.20 * r), "accent2")
    b.line(wall, color="ink", closed=True)
    b.line(roof, color="ink", closed=True)


def _recipe_cat(b, cx, cy, r):
    # body
    b.fill_gen("ellipse", {"at": [cx, cy + 0.42 * r], "rx": 0.52 * r, "ry": 0.38 * r}, "accent")
    b.outline("ellipse", {"at": [cx, cy + 0.42 * r], "rx": 0.52 * r, "ry": 0.38 * r}, color="ink")
    # tail
    b.arc(cx + 0.52 * r, cy + 0.52 * r, 0.42 * r, -100, 120, color="ink", width=b.line_width * 0.8)
    # head
    head = {"at": [cx, cy - 0.18 * r], "r": 0.54 * r}
    b.fill_gen("circle", head, "accent")
    # ears
    for sign in (-1, 1):
        ear = [
            [cx + sign * 0.18 * r, cy - 0.52 * r],
            [cx + sign * 0.48 * r, cy - 0.78 * r],
            [cx + sign * 0.52 * r, cy - 0.38 * r],
        ]
        b.fill_poly(ear, "accent")
        b.line(ear, color="ink", closed=True)
        b.fill_poly(
            [
                [cx + sign * 0.28 * r, cy - 0.52 * r],
                [cx + sign * 0.45 * r, cy - 0.68 * r],
                [cx + sign * 0.45 * r, cy - 0.45 * r],
            ],
            "accent2",
            alpha=0.8,
        )
    b.outline("circle", head, color="ink")
    # eyes / nose / whiskers
    for sign in (-1, 1):
        b.dot(cx + sign * 0.21 * r, cy - 0.22 * r, 0.075 * r, "detail", alpha=1.0, layer=1)
    b.fill_poly(
        [[cx - 0.05 * r, cy - 0.06 * r], [cx + 0.05 * r, cy - 0.06 * r], [cx, cy + 0.01 * r]],
        "detail",
        alpha=0.9,
    )
    for sign in (-1, 1):
        for dy in (-0.04, 0.03, 0.10):
            b.line(
                [[cx + sign * 0.12 * r, cy - 0.05 * r], [cx + sign * 0.52 * r, cy + dy * r]],
                color="detail",
                width=b.detail_width,
                alpha=0.9,
                detail=True,
            )


def _recipe_flower(b, cx, cy, r):
    b.line([[cx, cy + 0.18 * r], [cx, cy + 1.12 * r]], color="stem", width=b.detail_width * 1.6)
    for sign in (-1, 1):
        b.fill_gen(
            "ellipse",
            {
                "at": [cx + sign * 0.26 * r, cy + 0.62 * r],
                "rx": 0.24 * r,
                "ry": 0.10 * r,
                "rot": sign * 28,
            },
            "stem",
        )
    for petal in range(6):
        angle = _TAU * petal / 6
        px = cx + 0.48 * r * math.cos(angle)
        py = cy + 0.48 * r * math.sin(angle)
        b.fill_gen(
            "ellipse",
            {"at": [px, py], "rx": 0.40 * r, "ry": 0.24 * r, "rot": math.degrees(angle)},
            "accent",
        )
    b.fill_gen("circle", {"at": [cx, cy], "r": 0.24 * r}, "accent2")
    b.outline("circle", {"at": [cx, cy], "r": 0.24 * r}, color="ink", width=b.detail_width)


def _recipe_sun(b, cx, cy, r):
    b.fill_gen("circle", {"at": [cx, cy], "r": r * 0.72}, "accent")
    b.outline("circle", {"at": [cx, cy], "r": r * 0.72}, color="ink")
    for ray in range(12):
        angle = _TAU * ray / 12
        b.line(
            [
                [cx + 0.86 * r * math.cos(angle), cy + 0.86 * r * math.sin(angle)],
                [cx + 1.08 * r * math.cos(angle), cy + 1.08 * r * math.sin(angle)],
            ],
            color="accent2",
            width=b.line_width * 0.7,
        )


def _recipe_moon(b, cx, cy, r):
    b.fill_gen("circle", {"at": [cx - 0.12 * r, cy + 0.02 * r], "r": r * 0.82}, "accent")
    b.fill_gen("circle", {"at": [cx + 0.38 * r, cy - 0.22 * r], "r": r * 0.72}, "paper", alpha=1.0)
    for dx, dy, dr in ((0.72, -0.62, 0.055), (0.88, -0.30, 0.04), (0.62, -0.92, 0.035)):
        b.dot(cx + dx * r, cy + dy * r, dr * r, "accent2", alpha=1.0, layer=1)


def _recipe_wave(b, cx, cy, r):
    specs = [
        (-0.42, "accent", 1.25, 2.0),
        (0.10, "accent2", 1.15, 2.4),
        (0.55, "ink", 1.05, 2.8),
    ]
    for offset, color, scale, cycles in specs:
        b.line_gen(
            "wave",
            {
                "at": [cx - 1.05 * r * scale, cy + offset * r],
                "w": 2.1 * r * scale,
                "amp": 0.14 * r,
                "cycles": cycles,
            },
            color=color,
        )


def _recipe_snow(b, cx, cy, r):
    for spoke in range(6):
        angle = _TAU * spoke / 6 + math.pi / 2
        dx, dy = math.cos(angle), math.sin(angle)
        b.line([[cx, cy], [cx + r * dx, cy + r * dy]], color="ink")
        for t in (0.48, 0.72):
            bx, by = cx + r * t * dx, cy + r * t * dy
            for side in (-1, 1):
                tick = angle + side * math.radians(35)
                b.line(
                    [[bx, by], [bx + r * 0.20 * math.cos(tick), by + r * 0.20 * math.sin(tick)]],
                    color="accent2",
                    width=b.detail_width,
                    detail=True,
                )
    b.outline("circle", {"at": [cx, cy], "r": r * 0.16}, color="ink", width=b.detail_width)


def _recipe_eye(b, cx, cy, r):
    upper = []
    lower = []
    for index in range(33):
        t = -1.0 + 2.0 * index / 32
        upper.append([cx + t * r, cy - 0.55 * r * (1 - t * t) ** 0.65])
        lower.append([cx + t * r, cy + 0.48 * r * (1 - t * t) ** 0.8])
    almond = upper + list(reversed(lower))
    b.fill_poly(almond, "highlight")
    b.fill_gen("circle", {"at": [cx, cy], "r": 0.36 * r}, "accent2")
    b.fill_gen("circle", {"at": [cx, cy], "r": 0.17 * r}, "ink", alpha=1.0)
    b.dot(cx - 0.10 * r, cy - 0.10 * r, 0.055 * r, "highlight", alpha=1.0, layer=1)
    b.line(upper, color="ink", width=b.line_width)
    b.line(lower, color="ink", width=b.detail_width, detail=True)
    for angle in (205, 230, 155):
        rad = math.radians(angle)
        b.line(
            [
                [cx + 0.92 * r * math.cos(rad), cy + 0.58 * r * math.sin(rad)],
                [cx + 1.18 * r * math.cos(rad), cy + 0.72 * r * math.sin(rad)],
            ],
            color="ink",
            width=b.detail_width,
            detail=True,
        )


def _recipe_geometry(b, cx, cy, r):
    b.fill_gen("circle", {"at": [cx - 0.42 * r, cy - 0.22 * r], "r": 0.40 * r}, "accent")
    b.fill_gen(
        "regular_polygon",
        {"at": [cx + 0.45 * r, cy - 0.18 * r], "r": 0.44 * r, "n": 3, "rot": -90},
        "accent2",
    )
    b.fill_poly(_rect(cx - 0.30 * r, cy + 0.28 * r, 0.58 * r, 0.58 * r), "muted")
    b.fill_gen("star", {"at": [cx + 0.50 * r, cy + 0.52 * r], "r": 0.32 * r, "ri": 0.14 * r, "n": 5}, "accent2")
    b.outline("circle", {"at": [cx - 0.42 * r, cy - 0.22 * r], "r": 0.40 * r}, color="ink", width=b.detail_width)
    b.outline(
        "regular_polygon",
        {"at": [cx + 0.45 * r, cy - 0.18 * r], "r": 0.44 * r, "n": 3, "rot": -90},
        color="ink",
        width=b.detail_width,
    )


def _recipe_generic(b, cx, cy, r):
    """Fallback composition for unknown subjects (deterministic via seed)."""
    b.outline("circle", {"at": [cx, cy], "r": r * 0.9}, color="ink")
    b.fill_gen("circle", {"at": [cx - 0.34 * r, cy - 0.18 * r], "r": 0.34 * r}, "accent")
    b.fill_gen(
        "regular_polygon",
        {"at": [cx + 0.38 * r, cy - 0.24 * r], "r": 0.36 * r, "n": 3, "rot": -90},
        "accent2",
    )
    b.fill_gen(
        "star",
        {"at": [cx + 0.30 * r, cy + 0.42 * r], "r": 0.26 * r, "ri": 0.11 * r, "n": 5},
        "muted",
    )
    for _ in range(3):
        angle = b.rng.uniform(0, _TAU)
        radius = b.rng.uniform(0.18, 0.55) * r
        b.dot(cx + radius * math.cos(angle), cy + radius * math.sin(angle), b.rng.uniform(1.5, 3.0), "ink")
    b.line_gen(
        "spiral",
        {"at": [cx - 0.42 * r, cy + 0.46 * r], "r0": 0.04 * r, "growth": 2.4, "turns": 1.35},
        color="accent2",
        width=b.detail_width,
    )


RECIPES = {
    "circle": _recipe_circle,
    "star": _recipe_star,
    "heart": _recipe_heart,
    "cloud": _recipe_cloud,
    "mountain": _recipe_mountain,
    "tree": _recipe_tree,
    "house": _recipe_house,
    "cat": _recipe_cat,
    "flower": _recipe_flower,
    "sun": _recipe_sun,
    "moon": _recipe_moon,
    "wave": _recipe_wave,
    "snow": _recipe_snow,
    "eye": _recipe_eye,
    "geometry": _recipe_geometry,
    "generic": _recipe_generic,
}

assert set(RECIPES) == set(prompts.SUBJECTS) | {"generic"}


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


def _compose_subjects(builder, subject_keys, *, count, layout, position, text_band):
    width = builder.width
    height = builder.height
    margin_x = width * 0.08
    top = height * 0.10
    bottom = height - text_band - height * 0.03
    area_w = width - 2 * margin_x
    area_h = max(height * 0.25, bottom - top)

    if len(subject_keys) > 1:
        slots = []
        each = area_w / len(subject_keys)
        for index, key in enumerate(subject_keys):
            slots.append((margin_x + each * (index + 0.5), top + area_h * 0.50, each, area_h, key))
        radius = min(each * 0.42, area_h * 0.34)
        for cx, cy, _cw, _ch, key in slots:
            RECIPES[key](builder, cx, cy, radius)
        return

    key = subject_keys[0]
    use_grid = layout == "grid" and count > 1
    use_row = layout in ("row", "repeat") or (count > 1 and not use_grid)
    if count > 1 and use_grid:
        columns = int(math.ceil(math.sqrt(count)))
        rows = int(math.ceil(count / float(columns)))
        cell_w = area_w / columns
        cell_h = area_h / rows
        radius = max(24.0, min(cell_w, cell_h) * 0.36)
        for index in range(count):
            row, col = divmod(index, columns)
            cx = margin_x + cell_w * (col + 0.5)
            cy = top + cell_h * (row + 0.5)
            RECIPES[key](builder, cx, cy, radius)
        return
    if count > 1 and use_row:
        cell_w = area_w / count
        radius = max(20.0, min(cell_w * 0.40, area_h * 0.28))
        for index in range(count):
            cx = margin_x + cell_w * (index + 0.5)
            cy = top + area_h * 0.50
            RECIPES[key](builder, cx, cy, radius)
        return

    cx = width * 0.50
    cy = top + area_h * 0.48
    if position == "left":
        cx = width * 0.30
    elif position == "right":
        cx = width * 0.70
    elif position == "top":
        cy = top + area_h * 0.30
    elif position == "bottom":
        cy = top + area_h * 0.72
    radius = min(area_w, area_h) * 0.30
    RECIPES[key](builder, cx, cy, radius)


def _build_texts(width, height, text_count, texts, pack):
    entries = []
    if not texts:
        return entries
    band = _text_band_height(height, len(texts))
    band_top = height - band
    row_h = band / max(1, len(texts))
    font_scale = min(width * 0.055, row_h * 0.55)
    size_px = int(max(14, min(64, round(font_scale))))
    for index, content in enumerate(texts):
        y = band_top + row_h * index + row_h * 0.10
        box_h = row_h * 0.80
        entries.append(
            {
                "id": f"t{index + 1}",
                "as": "outline",
                "layer": 2,
                "text": content,
                "font": {
                    "id": "source_han_sans_sc",
                    "size_px": size_px,
                    "color": "ink",
                    "align": "center",
                    "alpha": 1.0,
                    "line_gap": 1.15,
                },
                "box": [round(width * 0.08, 2), round(y, 2), round(width * 0.84, 2), round(box_h, 2)],
            }
        )
    return entries
