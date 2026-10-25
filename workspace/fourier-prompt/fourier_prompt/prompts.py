"""Prompt vocabulary for the deterministic rule planner (WP-N).

This module owns *words*, not parsing decisions: subject/colour/style/position
lexicons, number words, negation markers, canvas presets and the nine style
packs.  :mod:`fourier_prompt.planner` turns these tables into an FDSL v2.1
plan.

Design rules
------------
* Chinese and English aliases live in the same table; matching is greedy
  longest-first so ``雪花`` (snowflake) wins over ``花`` (flower) and
  ``ink wash`` over ``ink``.
* ASCII aliases are matched on word boundaries (``star`` does not match
  ``start``).
* Every brush preset referenced by a style pack must exist in the frozen
  :data:`fourier_prompt.schema.BRUSH_PRESETS` (the 20 active catalog presets);
  this is asserted at import time so a typo fails loudly.
* No I/O, no randomness, no clock: pure tables and regexes.
"""

from __future__ import annotations

import re

from .schema import BRUSH_PRESETS

__all__ = [
    "SUBJECTS",
    "COLORS",
    "STYLES",
    "STYLE_PACKS",
    "POSITIONS",
    "NUMBER_WORDS",
    "CANVAS_PRESETS",
    "MAX_COUNT",
    "normalize_prompt",
    "find_terms",
    "detect_style_name",
    "resolve_style",
    "parse_count_before",
    "find_hex_colors",
    "is_word_alias",
]

# ---------------------------------------------------------------------------
# Subject lexicon: canonical key -> aliases (zh + en)
# ---------------------------------------------------------------------------

SUBJECTS = {
    "circle": ("圆形", "圆圈", "圆", "circle", "ring", "dot"),
    "star": ("五角星", "星星", "星形", "星", "star"),
    "heart": ("爱心", "心形", "心", "heart"),
    "cloud": ("云朵", "白云", "云", "cloud"),
    "mountain": ("山脉", "山", "mountain", "hill"),
    "tree": ("树木", "树", "tree"),
    "house": ("房子", "小屋", "房屋", "家", "house", "home"),
    "cat": ("小猫", "猫咪", "猫", "cat", "kitten"),
    "flower": ("花朵", "花", "玫瑰", "flower", "rose"),
    "sun": ("太阳", "阳光", "日", "sun"),
    "moon": ("月亮", "弯月", "月", "moon", "crescent"),
    "wave": ("波浪", "海浪", "水波", "浪", "wave"),
    "snow": ("雪花", "雪", "snowflake", "snow"),
    "eye": ("眼睛", "眼眸", "眼", "eye"),
    "geometry": ("几何图形", "几何", "抽象画", "抽象", "geometry", "abstract", "shapes"),
}

# ---------------------------------------------------------------------------
# Colour lexicon (prompt colour -> hex)
# ---------------------------------------------------------------------------

COLORS = {
    "black": ("黑色", "黑", "black"),
    "white": ("白色", "白", "white"),
    "gray": ("灰色", "灰", "gray", "grey"),
    "red": ("红色", "红", "red"),
    "blue": ("蓝色", "蓝", "blue"),
    "green": ("绿色", "绿", "green"),
    "gold": ("金色", "金", "gold", "golden"),
    "purple": ("紫色", "紫", "purple", "violet"),
    "orange": ("橙色", "橙", "orange"),
    "pink": ("粉色", "粉", "pink"),
    "cyan": ("青色", "青", "cyan", "teal"),
    "brown": ("棕色", "棕", "brown"),
    "yellow": ("黄色", "黄", "yellow"),
}

COLOR_HEX = {
    "black": "#1a1a1a",
    "white": "#f7f7f7",
    "gray": "#8a8a8a",
    "red": "#c0392b",
    "blue": "#2f6fa8",
    "green": "#3e8e41",
    "gold": "#e2b65c",
    "purple": "#8e44ad",
    "orange": "#e67e22",
    "pink": "#e88aa0",
    "cyan": "#00bcd4",
    "brown": "#8d6e63",
    "yellow": "#f1c40f",
}

# ---------------------------------------------------------------------------
# Style lexicon + packs
# ---------------------------------------------------------------------------

#: Canonical pack key -> aliases (zh + en).  Greedy longest-first matching.
STYLES = {
    "mono_ink": (
        "黑白线稿", "黑白漫画", "黑白", "线稿", "墨线", "钢笔画",
        "mono", "lineart", "line art", "black and white", "ink line",
    ),
    "manga_color": ("彩色漫画", "漫画风", "漫画", "comic", "manga"),
    "pencil": ("铅笔画", "铅笔", "素描", "pencil", "sketch", "graphite"),
    "ink_wash": ("水墨画", "水墨", "国画", "写意", "ink wash", "inkwash", "sumi"),
    "marker": ("马克笔", "马克", "marker", "copic"),
    "oil": ("油画", "厚涂", "oil painting", "oil", "painterly"),
    "watercolor": ("水彩画", "水彩", "watercolor", "watercolour", "aquarelle"),
    "minimal": ("极简风", "极简", "简约", "扁平", "扁平风", "minimal", "flat", "flat design"),
    "cyber": ("赛博朋克", "赛博风", "赛博", "cyberpunk", "cyber", "neon"),
}

#: Style pack: catalog-active brushes + palette + background + line weights.
#: ``fill_alpha`` is used for closed shapes; ``line_width``/``detail_width``
#: are canvas pixels at long_side 768 and scale with the canvas.
STYLE_PACKS = {
    "mono_ink": {
        "name_zh": "黑白线稿",
        "name_en": "Mono ink",
        # Palette/brush targets aligned with WP-P's style_rubric.json mono_ink
        # block (paper/haze/mid/shadow/ink; contour + hairline + flat fill).
        "fill_brush": "basic_round_hard",
        "line_brush": "ink_gpen",
        "detail_brush": "ink_fineliner",
        "fill_alpha": 0.96,
        "line_alpha": 1.0,
        "line_width": 4.5,
        "detail_width": 2.0,
        "background": "paper",
        "palette": {
            "ink": "#060505",
            "paper": "#fefdfd",
            "accent": "#a4a2a2",
            "accent2": "#686566",
            "muted": "#d0cece",
            "highlight": "#ffffff",
            "wood": "#686566",
            "stem": "#686566",
        },
    },
    "manga_color": {
        "name_zh": "漫画",
        "name_en": "Manga",
        "fill_brush": "basic_round_hard",
        "line_brush": "ink_brush_rough",
        "detail_brush": "ink_gpen",
        "fill_alpha": 1.0,
        "line_alpha": 1.0,
        "line_width": 6.5,
        "detail_width": 2.8,
        "background": "paper",
        "palette": {
            "ink": "#101018",
            "paper": "#fffdf7",
            "accent": "#e5533d",
            "accent2": "#2f6fa8",
            "muted": "#f0c75e",
            "highlight": "#ffffff",
            "wood": "#8a5a2b",
            "stem": "#3e7d3a",
        },
    },
    "pencil": {
        "name_zh": "铅笔画",
        "name_en": "Pencil",
        "fill_brush": "pencil_hb",
        "line_brush": "pencil_2b",
        "detail_brush": "pencil_hb",
        "fill_alpha": 0.35,
        "line_alpha": 0.9,
        "line_width": 4.2,
        "detail_width": 2.4,
        "background": "paper",
        "palette": {
            "ink": "#3a3a3a",
            "paper": "#f4f1e8",
            "accent": "#8b8b86",
            "accent2": "#55534e",
            "muted": "#c4c0b4",
            "highlight": "#ffffff",
            "wood": "#7a6a55",
            "stem": "#6a7a5a",
        },
    },
    "ink_wash": {
        "name_zh": "水墨",
        "name_en": "Ink wash",
        "fill_brush": "watercolor_wash",
        "line_brush": "sumi_e",
        "detail_brush": "ink_gpen",
        "fill_alpha": 0.35,
        "line_alpha": 0.95,
        "line_width": 11.0,
        "detail_width": 3.0,
        "background": "paper",
        "palette": {
            "ink": "#101010",
            "paper": "#f6f2e6",
            "accent": "#6f6f6a",
            "accent2": "#2c2c2a",
            "muted": "#b9b4a6",
            "highlight": "#ffffff",
            "wood": "#4a4a45",
            "stem": "#4f5a48",
        },
    },
    "marker": {
        "name_zh": "马克笔",
        "name_en": "Marker",
        "fill_brush": "marker_chisel",
        "line_brush": "marker_chisel",
        "detail_brush": "ink_fineliner",
        "fill_alpha": 0.82,
        "line_alpha": 1.0,
        "line_width": 7.0,
        "detail_width": 2.6,
        "background": "paper",
        "palette": {
            "ink": "#222222",
            "paper": "#fffdf6",
            "accent": "#f2b23c",
            "accent2": "#3c7dc4",
            "muted": "#ef7f8f",
            "highlight": "#ffffff",
            "wood": "#a06a3a",
            "stem": "#4c9a54",
        },
    },
    "oil": {
        "name_zh": "油画",
        "name_en": "Oil",
        "fill_brush": "bristle_oil",
        "line_brush": "ink_brush_rough",
        "detail_brush": "ink_gpen",
        "fill_alpha": 0.95,
        "line_alpha": 1.0,
        "line_width": 9.0,
        "detail_width": 3.0,
        "background": "paper",
        "palette": {
            "ink": "#1a1a1a",
            "paper": "#f2ead8",
            "accent": "#d98b3a",
            "accent2": "#3d5f8a",
            "muted": "#8a9a5a",
            "highlight": "#fff8e8",
            "wood": "#7b4a2a",
            "stem": "#567a3a",
        },
    },
    "watercolor": {
        "name_zh": "水彩",
        "name_en": "Watercolor",
        "fill_brush": "watercolor_wash",
        "line_brush": "ink_gpen",
        "detail_brush": "ink_fineliner",
        "fill_alpha": 0.55,
        "line_alpha": 0.9,
        "line_width": 4.2,
        "detail_width": 2.4,
        "background": "paper",
        "palette": {
            "ink": "#2a2a2a",
            "paper": "#fffdf8",
            "accent": "#e88a9a",
            "accent2": "#7ab0c8",
            "muted": "#f2d27a",
            "highlight": "#ffffff",
            "wood": "#a87a5a",
            "stem": "#7aa06a",
        },
    },
    "minimal": {
        "name_zh": "极简",
        "name_en": "Minimal",
        "fill_brush": "basic_round_hard",
        "line_brush": "basic_round_hard",
        "detail_brush": "ink_fineliner",
        "fill_alpha": 1.0,
        "line_alpha": 1.0,
        "line_width": 5.0,
        "detail_width": 2.5,
        "background": "paper",
        "palette": {
            "ink": "#1b1b1b",
            "paper": "#ffffff",
            "accent": "#e63946",
            "accent2": "#457b9d",
            "muted": "#f1c453",
            "highlight": "#ffffff",
            "wood": "#8d6e63",
            "stem": "#52b788",
        },
    },
    "cyber": {
        "name_zh": "赛博",
        "name_en": "Cyber",
        "fill_brush": "marker_alcohol",
        "line_brush": "ink_fineliner",
        "detail_brush": "ink_fineliner",
        "fill_alpha": 0.9,
        "line_alpha": 1.0,
        "line_width": 4.0,
        "detail_width": 2.2,
        "background": "paper",
        "palette": {
            "ink": "#d8f7ff",
            "paper": "#0c0c16",
            "accent": "#ff2e88",
            "accent2": "#00e5ff",
            "muted": "#8a2be2",
            "highlight": "#ffffff",
            "wood": "#ffb703",
            "stem": "#00ff9c",
        },
    },
}

#: Default pack when the prompt names no style.
DEFAULT_STYLE = "minimal"

# ---------------------------------------------------------------------------
# Position / layout / number / canvas lexicons
# ---------------------------------------------------------------------------

POSITIONS = {
    "center": ("居中", "中间", "中心", "中央", "正中间", "center", "middle", "centered"),
    "left": ("左边", "左侧", "左", "left"),
    "right": ("右边", "右侧", "右", "right"),
    "top": ("上方", "上面", "顶部", "上", "top", "upper"),
    "bottom": ("下方", "下面", "底部", "下", "bottom", "lower"),
}
POSITION_ORDER = ("center", "left", "right", "top", "bottom")

LAYOUT_WORDS = {
    "grid": ("网格", "阵列", "矩阵", "九宫格", "grid", "tile", "pattern"),
    "row": ("一排", "横向排列", "排成一排", "row"),
    "repeat": ("重复", "平铺", "repeat", "repeated"),
}

CANVAS_PRESETS = {
    "portrait": (768, 1024),
    "landscape": (1024, 768),
    "square": (768, 768),
}

RATIO_WORDS = {
    "portrait": ("竖版", "竖图", "竖幅", "纵向", "portrait", "vertical"),
    "landscape": ("横版", "横图", "横幅", "横向", "landscape", "horizontal"),
    "square": ("方形", "正方形", "方图", "square"),
}

NUMBER_WORDS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "a": 1, "an": 1,
}

#: Hard cap on repeated subjects per prompt (keeps plans small and legible).
MAX_COUNT = 12

NEGATIONS = ("不要", "没有", "不带", "无", "去掉", "去除", "删除", "除了", "no ", "without ", "don't ", "dont ")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def is_word_alias(alias: str) -> bool:
    """True when ``alias`` is ASCII and must be matched on word boundaries."""
    return bool(re.search(r"[A-Za-z]", alias)) and not re.search(r"[\u4e00-\u9fff]", alias)


def normalize_prompt(prompt: str) -> str:
    """NFKC-normalise, collapse whitespace and strip."""
    import unicodedata

    text = unicodedata.normalize("NFKC", prompt)
    text = text.replace("\u3000", " ")
    return re.sub(r"[ \t\r\f\v]+", " ", text).strip()


def _alias_matches(text: str, alias: str):
    """Yield ``(start, end)`` for ``alias`` in ``text`` (case-insensitive)."""
    lowered = text.lower()
    needle = alias.lower()
    if is_word_alias(alias):
        pattern = re.compile(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])")
        for match in pattern.finditer(lowered):
            yield match.start(), match.end()
        return
    start = 0
    while True:
        index = lowered.find(needle, start)
        if index < 0:
            return
        yield index, index + len(needle)
        start = index + len(needle)


def find_terms(text: str, table: dict):
    """Greedy longest-first, non-overlapping term matches.

    ``table`` maps canonical key -> alias tuple.  Returns
    ``[{"key", "alias", "start", "end"}]`` sorted by position.
    """
    candidates = []
    for key, aliases in table.items():
        for alias in aliases:
            for start, end in _alias_matches(text, alias):
                candidates.append((-(end - start), start, key, alias, end))
    candidates.sort(key=lambda item: (item[0], item[1]))
    taken = [False] * len(text)
    found = []
    for _neg_len, start, key, alias, end in candidates:
        if any(taken[start:end]):
            continue
        for index in range(start, end):
            taken[index] = True
        found.append({"key": key, "alias": alias, "start": start, "end": end})
    found.sort(key=lambda item: item["start"])
    return found


def detect_style_name(text: str):
    """Return ``(canonical_pack_key, matched_alias)`` or ``(None, None)``."""
    matches = find_terms(text, STYLES)
    if not matches:
        return None, None
    return matches[0]["key"], matches[0]["alias"]


def resolve_style(name):
    """Resolve a pack key or an alias to ``(canonical_key, pack)``.

    Returns ``(None, None)`` when ``name`` is not a known style; the planner
    turns that into an explicit warning instead of silently picking one.
    """
    if not isinstance(name, str) or not name.strip():
        return None, None
    key = name.strip().lower()
    if key in STYLE_PACKS:
        return key, STYLE_PACKS[key]
    matches = find_terms(normalize_prompt(name), STYLES)
    if not matches:
        return None, None
    canonical = matches[0]["key"]
    return canonical, STYLE_PACKS[canonical]


def parse_count_before(text: str, start: int, window: int = 10):
    """Parse a count written just before ``text[start]``.

    Accepts ``3``, ``三``, ``三只``, ``three``, ``a``.  Returns ``None`` when no
    number precedes the subject within ``window`` characters.
    """
    prefix = text[max(0, start - window):start]
    match = re.search(r"(\d{1,3})\s*(?:个|颗|只|朵|座|棵|片|轮|枚|条|张|位)?\s*$", prefix)
    if match:
        return int(match.group(1))
    match = re.search(
        r"([一二两三四五六七八九十]|one|two|three|four|five|six|seven|eight|nine|ten|a|an)"
        r"\s*(?:个|颗|只|朵|座|棵|片|轮|枚|条|张|位)?\s*$",
        prefix.lower(),
    )
    if match:
        return NUMBER_WORDS.get(match.group(1))
    return None


def find_hex_colors(text: str):
    """Return explicit ``#rrggbb`` colours in prompt order (lower-cased)."""
    return [match.group(0).lower() for match in re.finditer(r"#[0-9a-fA-F]{6}\b", text)]


# ---------------------------------------------------------------------------
# Import-time guard: every pack brush must be an active catalog preset
# ---------------------------------------------------------------------------

for _pack_key, _pack in STYLE_PACKS.items():
    for _role in ("fill_brush", "line_brush", "detail_brush"):
        _preset = _pack[_role]
        if _preset not in BRUSH_PRESETS:
            raise AssertionError(
                f"style pack {_pack_key!r} references unknown brush preset {_preset!r}"
            )
    for _name, _hex in _pack["palette"].items():
        if not re.match(r"^#[0-9a-f]{6}$", _hex):
            raise AssertionError(f"style pack {_pack_key!r} palette {_name!r} is not #rrggbb")
