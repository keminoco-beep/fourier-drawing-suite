"""Layer semantics for compiled FDSL / ``.fdraw`` documents (WP-I).

The skill document model has only an integer ``stroke['layer']``; there is no
layer name, visibility or opacity.  This module adds an **additive** layer
table (``doc['layers']``) without changing any rendering field, and it renders
each layer to **straight-alpha RGBA** so that layered engineering files (PSD /
KRA) can be written by :mod:`fourier_prompt.psd` / :mod:`fourier_prompt.kra`.

Layer order
-----------
``doc['layers']`` entries carry ``{index,id,name,visible,opacity,order}``.
``index`` is the integer used by ``stroke['layer']`` (the bottom layer keeps
index 0 when the document uses the compiler's sequential indices).  Drawing
order is ``(order, index)``; all writers emit bottom-first records.

Straight alpha recovery
-----------------------
The skill renderer composites onto an opaque background and returns RGB only.
A layer is therefore rendered twice, over black and over white::

    C_black = C * a
    C_white = C * a + (1 - a) * 255
    a       = 1 - (C_white - C_black) / 255
    C       = C_black / a

This is exact up to 8-bit quantisation (documented in ``reports/WP-I.md``).
Layer ``opacity`` is baked into the recovered alpha (maindoc / PSD layer
opacity stays 255 so readers do not apply it twice); ``visible=false`` layers
are still rendered but written with the hidden flag.  Nothing fails silently:
every such decision produces a warning entry.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import time

import numpy as np

from . import render_fast

__all__ = [
    "LayerError",
    "MIN_RESOLUTION",
    "MAX_RESOLUTION",
    "MAX_LAYERS",
    "doc_layers",
    "add_layer_metadata",
    "filter_doc",
    "canvas_size",
    "render_layers",
    "render_layer_pngs",
    "composite_over",
    "LAYER_WARNING_CODES",
]

MIN_RESOLUTION = 16
MAX_RESOLUTION = 512
MAX_LAYERS = 32
MAX_LAYER_NAME = 64

LAYER_WARNING_CODES = frozenset(
    {
        "W_LAYER_HIDDEN",
        "W_LAYER_OPACITY_BAKED",
        "W_LAYER_NO_METADATA",
        "W_LAYER_EMPTY",
    }
)

_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class LayerError(Exception):
    """Structured layer/export failure (``FP_E_LAYER_*`` / ``FP_E_PSD_*``)."""

    def __init__(self, code: str, message: str, evidence: dict | None = None):
        super().__init__(f"{code}: {message}")
        self.code = str(code)
        self.message = str(message)
        self.evidence = dict(evidence or {})

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "evidence": dict(self.evidence)}


# ---------------------------------------------------------------------------
# validation helpers
# ---------------------------------------------------------------------------
def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _check_resolution(resolution) -> int:
    if isinstance(resolution, bool) or not isinstance(resolution, int):
        raise LayerError("FP_E_LAYER_RESOLUTION", f"resolution 必须是整数: {resolution!r}")
    if not MIN_RESOLUTION <= resolution <= MAX_RESOLUTION:
        raise LayerError(
            "FP_E_LAYER_RESOLUTION",
            f"resolution 必须在 [{MIN_RESOLUTION}, {MAX_RESOLUTION}]（R≤512）: {resolution}",
        )
    return int(resolution)


def _check_quality(quality) -> str:
    value = str(quality or "fast").lower()
    if value not in ("fast", "exact"):
        raise LayerError("FP_E_LAYER_QUALITY", f"quality 必须是 fast|exact: {quality!r}")
    return value


def _check_name(name, *, index: int) -> str:
    if not isinstance(name, str) or not name.strip():
        raise LayerError("FP_E_LAYER_NAME", f"layer[{index}] 名称必须是非空字符串: {name!r}")
    if len(name) > MAX_LAYER_NAME:
        raise LayerError(
            "FP_E_LAYER_NAME",
            f"layer[{index}] 名称超过 {MAX_LAYER_NAME} 字符: {len(name)}",
        )
    if any(ord(ch) < 0x20 for ch in name):
        raise LayerError("FP_E_LAYER_NAME", f"layer[{index}] 名称含控制字符")
    return name


def _check_opacity(value, *, index: int) -> float:
    if not _is_number(value):
        raise LayerError("FP_E_LAYER_OPACITY", f"layer[{index}].opacity 必须是数字: {value!r}")
    opacity = float(value)
    if not 0.0 <= opacity <= 1.0:
        raise LayerError("FP_E_LAYER_OPACITY", f"layer[{index}].opacity 必须在 [0,1]: {opacity}")
    return opacity


def _check_visible(value, *, index: int) -> bool:
    if not isinstance(value, bool):
        raise LayerError("FP_E_LAYER_VISIBLE", f"layer[{index}].visible 必须是 bool: {value!r}")
    return value


def _check_order(value, *, index: int) -> int:
    if not _is_int(value) or value < 0:
        raise LayerError("FP_E_LAYER_ORDER", f"layer[{index}].order 必须是整数 >= 0: {value!r}")
    return int(value)


# ---------------------------------------------------------------------------
# document / layer tables
# ---------------------------------------------------------------------------
def _base_rgb(doc: dict) -> list[int]:
    raw = doc.get("base_color")
    if raw is None:
        return [255, 255, 255]
    values = np.asarray(raw, dtype=np.float64).reshape(-1)
    if values.size < 3:
        return [255, 255, 255]
    values = values[:3]
    if not np.all(np.isfinite(values)):
        return [255, 255, 255]
    if float(np.max(np.abs(values))) > 1.5:
        values = values / 255.0
    return [int(round(float(v) * 255.0)) for v in np.clip(values, 0.0, 1.0)]


def canvas_size(doc: dict, resolution: int) -> tuple[int, int]:
    """Pixel size ``(W, H)`` the skill renderer produces for ``resolution``.

    Mirrors ``fbs_core.render._output_size`` (long side = resolution) without
    importing a private symbol from the read-only skill.
    """
    R = _check_resolution(resolution)
    if not isinstance(doc, dict):
        raise LayerError("FP_E_LAYER_INPUT", "doc 必须是 dict")
    canvas = doc.get("canvas") or {}
    try:
        w = float(canvas.get("width", 1.0))
        h = float(canvas.get("height", 1.0))
    except (TypeError, ValueError) as exc:
        raise LayerError("FP_E_LAYER_CANVAS", f"canvas 尺寸非法: {exc}") from exc
    long_side = max(w, h, 1e-12)
    return max(1, int(round(w / long_side * R))), max(1, int(round(h / long_side * R)))


def _stroke_bbox(stroke: dict):
    bbox = stroke.get("bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        try:
            values = [float(v) for v in bbox]
        except (TypeError, ValueError):
            values = []
        if len(values) == 4 and all(math.isfinite(v) for v in values):
            return values
    poly = stroke.get("poly")
    if isinstance(poly, (list, tuple)) and len(poly) >= 1:
        try:
            points = np.asarray(poly, dtype=np.float64).reshape(-1, 2)
        except (TypeError, ValueError):
            return None
        if len(points) and np.all(np.isfinite(points)):
            return [
                float(points[:, 0].min()),
                float(points[:, 1].min()),
                float(points[:, 0].max()),
                float(points[:, 1].max()),
            ]
    return None


def _stats_by_layer(doc: dict) -> dict[int, dict]:
    stats: dict[int, dict] = {}
    for stroke in doc.get("strokes") or []:
        if not isinstance(stroke, dict) or not _is_int(stroke.get("layer")):
            continue
        index = int(stroke["layer"])
        entry = stats.setdefault(
            index,
            {"stroke_count": 0, "fill_count": 0, "text_count": 0, "bbox": None},
        )
        if stroke.get("fill_role") == "region_fill":
            entry["fill_count"] += 1
        else:
            entry["stroke_count"] += 1
        bbox = _stroke_bbox(stroke)
        if bbox is not None:
            if entry["bbox"] is None:
                entry["bbox"] = list(bbox)
            else:
                old = entry["bbox"]
                entry["bbox"] = [
                    min(old[0], bbox[0]),
                    min(old[1], bbox[1]),
                    max(old[2], bbox[2]),
                    max(old[3], bbox[3]),
                ]
    for text in doc.get("texts") or []:
        if isinstance(text, dict) and _is_int(text.get("layer")):
            index = int(text["layer"])
            entry = stats.setdefault(
                index,
                {"stroke_count": 0, "fill_count": 0, "text_count": 0, "bbox": None},
            )
            entry["text_count"] += 1
    return stats


def _synthesised_layers(doc: dict) -> list[dict]:
    stats = _stats_by_layer(doc)
    layers = []
    for index in sorted(stats):
        layers.append(
            {
                "index": int(index),
                "id": int(index),
                "name": f"L{index}",
                "visible": True,
                "opacity": 1.0,
                "order": int(index),
                "source": "synthesized",
            }
        )
    return layers


def _metadata_layers(doc: dict) -> list[dict]:
    raw = doc.get("layers")
    if not isinstance(raw, list) or not raw:
        return []
    if len(raw) > MAX_LAYERS:
        raise LayerError("FP_E_LAYER_LIMIT", f"层数 {len(raw)} 超过上限 {MAX_LAYERS}")
    layers = []
    seen: set[int] = set()
    for position, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise LayerError("FP_E_LAYER_INPUT", f"layers[{position}] 必须是对象")
        index = entry.get("index", position)
        if not _is_int(index) or index < 0:
            raise LayerError("FP_E_LAYER_INDEX", f"layers[{position}].index 非法: {index!r}")
        index = int(index)
        if index in seen:
            raise LayerError("FP_E_LAYER_INDEX", f"layers[{position}].index 重复: {index}")
        seen.add(index)
        name = _check_name(entry.get("name", f"L{index}"), index=index)
        layers.append(
            {
                "index": index,
                "id": entry.get("id", index),
                "name": name,
                "visible": _check_visible(entry.get("visible", True), index=index),
                "opacity": _check_opacity(entry.get("opacity", 1.0), index=index),
                "order": _check_order(entry.get("order", position), index=index),
                "source": "metadata",
            }
        )
    return layers


def doc_layers(doc: dict, *, include_empty: bool = True) -> list[dict]:
    """Return the document layer table, bottom-first.

    Metadata in ``doc['layers']`` wins; without it the distinct integer
    ``stroke['layer']`` / ``text['layer']`` values are synthesised as
    ``L0..Ln``.  Every returned entry carries ``index,id,name,visible,
    opacity,order,stroke_count,fill_count,text_count,bbox``; ``bbox`` is in
    the document's normalised world coordinates (``None`` for empty layers).
    """
    if not isinstance(doc, dict):
        raise LayerError("FP_E_LAYER_INPUT", "doc 必须是 dict")
    if not isinstance(doc.get("strokes"), list):
        raise LayerError("FP_E_LAYER_INPUT", "doc.strokes 必须是列表")
    layers = _metadata_layers(doc)
    if not layers:
        layers = _synthesised_layers(doc)
    if len(layers) > MAX_LAYERS:
        raise LayerError("FP_E_LAYER_LIMIT", f"层数 {len(layers)} 超过上限 {MAX_LAYERS}")
    stats = _stats_by_layer(doc)
    for layer in layers:
        stat = stats.get(layer["index"]) or {
            "stroke_count": 0,
            "fill_count": 0,
            "text_count": 0,
            "bbox": None,
        }
        layer.update(stat)
    layers.sort(key=lambda item: (int(item["order"]), int(item["index"])))
    if not include_empty:
        layers = [
            layer
            for layer in layers
            if layer["stroke_count"] + layer["fill_count"] + layer["text_count"] > 0
        ]
    return layers


def add_layer_metadata(doc: dict, source_plan: dict | None) -> dict:
    """Write ``doc['layers']`` from a canonical FDSL plan (in place, additive).

    ``source_plan['layers']`` entries are mapped in list order to the integer
    layer indices the compiler uses (``compile._layer_index`` resolves both
    integer refs and string ids to the list position).  Without plan layers the
    table is synthesised as ``L0..Ln`` from the strokes present.  Only the
    ``layers`` key is touched; all rendering fields are left byte-identical.
    """
    if not isinstance(doc, dict):
        raise LayerError("FP_E_LAYER_INPUT", "doc 必须是 dict")
    plan_layers = None
    if isinstance(source_plan, dict):
        raw = source_plan.get("layers")
        if isinstance(raw, list) and raw:
            plan_layers = raw
    entries: list[dict] = []
    if plan_layers is not None:
        if len(plan_layers) > MAX_LAYERS:
            raise LayerError("FP_E_LAYER_LIMIT", f"层数 {len(plan_layers)} 超过上限 {MAX_LAYERS}")
        for position, layer in enumerate(plan_layers):
            if not isinstance(layer, dict):
                raise LayerError("FP_E_LAYER_INPUT", f"plan.layers[{position}] 必须是对象")
            name = _check_name(layer.get("name") or f"L{position}", index=position)
            entries.append(
                {
                    "index": position,
                    "id": layer.get("id", position),
                    "name": name,
                    "visible": _check_visible(layer.get("visible", True), index=position),
                    "opacity": _check_opacity(layer.get("opacity", 1.0), index=position),
                    "order": _check_order(layer.get("order", position), index=position),
                }
            )
    else:
        for layer in _synthesised_layers(doc):
            entries.append({key: layer[key] for key in ("index", "id", "name", "visible", "opacity", "order")})
    doc["layers"] = entries
    return doc


def filter_doc(doc: dict, layer_index: int, *, include_above: bool = False) -> dict:
    """Copy of ``doc`` containing only ``layer_index`` (or the cumulative stack).

    ``include_above=True`` keeps the target layer **and all layers below it**
    (``layer <= layer_index``), i.e. the cumulative picture after painting up
    to that layer — useful for export previews.  The input document is never
    mutated and stroke dicts are shared read-only (the renderer does not mutate
    them).
    """
    if not isinstance(doc, dict):
        raise LayerError("FP_E_LAYER_INPUT", "doc 必须是 dict")
    if not _is_int(layer_index) or layer_index < 0:
        raise LayerError("FP_E_LAYER_INDEX", f"layer_index 必须是非负整数: {layer_index!r}")
    known = {int(layer["index"]) for layer in doc_layers(doc)}
    if int(layer_index) not in known:
        raise LayerError(
            "FP_E_LAYER_INDEX",
            f"layer_index {layer_index} 不在文档层表 {sorted(known)} 中",
        )
    target = int(layer_index)

    def keep(stroke) -> bool:
        if not isinstance(stroke, dict) or not _is_int(stroke.get("layer")):
            return False
        value = int(stroke["layer"])
        return value == target or (include_above and value < target)

    out = copy.copy(doc)
    out["strokes"] = [stroke for stroke in doc.get("strokes") or [] if keep(stroke)]
    if isinstance(doc.get("texts"), list):
        out["texts"] = [text for text in doc["texts"] if keep(text)]
    if isinstance(doc.get("layers"), list):
        out["layers"] = [
            copy.deepcopy(entry)
            for entry in doc["layers"]
            if isinstance(entry, dict) and entry.get("index") in {int(s["layer"]) for s in out["strokes"]}
        ]
    return out


# ---------------------------------------------------------------------------
# per-layer straight-alpha rendering
# ---------------------------------------------------------------------------
def _unmix_straight_alpha(black: np.ndarray, white: np.ndarray) -> np.ndarray:
    """Recover straight-alpha RGBA from renders over black and over white."""
    black_i = black.astype(np.int16)
    white_i = white.astype(np.int16)
    diff = np.clip(white_i - black_i, 0, 255).astype(np.float32).mean(axis=-1)
    alpha = np.clip(np.rint(255.0 - diff), 0, 255).astype(np.uint8)
    a = alpha.astype(np.float32) / 255.0
    safe = np.maximum(a, 1e-6)[..., None]
    rgb = black.astype(np.float32) / safe
    rgb = np.where(a[..., None] > 0.0, rgb, 0.0)
    rgb = np.clip(np.rint(rgb), 0.0, 255.0).astype(np.uint8)
    return np.dstack([rgb, alpha])


def _render_one_layer(doc: dict, layer: dict, *, resolution: int, quality: str):
    started = time.perf_counter()
    width, height = canvas_size(doc, resolution)
    sub = filter_doc(doc, int(layer["index"]))
    has_content = bool(sub.get("strokes")) or bool(sub.get("texts"))
    if not has_content:
        rgba = np.zeros((height, width, 4), dtype=np.uint8)
        info = {"quality": quality, "fallback_reason": [], "approximations": []}
    else:
        black, black_info = render_fast.render_auto(
            sub,
            resolution=int(resolution),
            quality=quality,
            background=[0, 0, 0],
            return_info=True,
        )
        white, white_info = render_fast.render_auto(
            sub,
            resolution=int(resolution),
            quality=quality,
            background=[255, 255, 255],
            return_info=True,
        )
        if black.shape != (height, width, 3) or white.shape != black.shape:
            raise LayerError(
                "FP_E_LAYER_RENDER",
                f"层 {layer['name']!r} 渲染尺寸异常: black={black.shape} white={white.shape}",
            )
        rgba = _unmix_straight_alpha(black, white)
        info = dict(black_info)
        info["fallback_reason"] = list(black_info.get("fallback_reason") or [])
        if white_info.get("quality") != black_info.get("quality"):
            info["fallback_reason"].append("black/white render quality mismatch")
    opacity = float(layer.get("opacity", 1.0))
    warnings: list[dict] = []
    if opacity != 1.0:
        rgba[..., 3] = np.clip(
            np.rint(rgba[..., 3].astype(np.float32) * opacity), 0, 255
        ).astype(np.uint8)
        warnings.append(
            {
                "code": "W_LAYER_OPACITY_BAKED",
                "message": f"layer {layer['name']!r}: opacity {opacity:g} baked into the alpha channel",
            }
        )
    if not layer.get("visible", True):
        warnings.append(
            {
                "code": "W_LAYER_HIDDEN",
                "message": f"layer {layer['name']!r}: visible=false; pixels written with the hidden flag",
            }
        )
    if not has_content:
        warnings.append(
            {
                "code": "W_LAYER_EMPTY",
                "message": f"layer {layer['name']!r}: no strokes/texts; transparent pixels written",
            }
        )
    return {
        "index": int(layer["index"]),
        "id": layer.get("id", layer["index"]),
        "name": str(layer["name"]),
        "visible": bool(layer.get("visible", True)),
        "opacity": opacity,
        "order": int(layer["order"]),
        "stroke_count": int(layer.get("stroke_count", 0)),
        "fill_count": int(layer.get("fill_count", 0)),
        "text_count": int(layer.get("text_count", 0)),
        "bbox": layer.get("bbox"),
        "width": width,
        "height": height,
        "rgba": rgba,
        "quality": info.get("quality", quality),
        "fallback_reason": list(info.get("fallback_reason") or []),
        "approximations": list(info.get("approximations") or []),
        "render_seconds": round(time.perf_counter() - started, 4),
        "warnings": warnings,
    }


def _resolve_layer_table(doc: dict, layers) -> list[dict]:
    """Merge a caller-supplied layer table with the document's stats."""
    table = doc_layers(doc)
    if layers is None:
        return table
    if not isinstance(layers, list) or not layers:
        raise LayerError("FP_E_LAYER_INPUT", "layers 必须是非空列表")
    by_index = {int(layer["index"]): layer for layer in table}
    out: list[dict] = []
    seen: set[int] = set()
    for position, entry in enumerate(layers):
        if not isinstance(entry, dict):
            raise LayerError("FP_E_LAYER_INPUT", f"layers[{position}] 必须是对象")
        index = entry.get("index", position)
        if not _is_int(index) or index < 0:
            raise LayerError("FP_E_LAYER_INDEX", f"layers[{position}].index 非法: {index!r}")
        index = int(index)
        if index in seen:
            raise LayerError("FP_E_LAYER_INDEX", f"layers[{position}].index 重复: {index}")
        if index not in by_index:
            raise LayerError(
                "FP_E_LAYER_INDEX",
                f"layers[{position}].index {index} 不在文档层表 {sorted(by_index)} 中",
            )
        seen.add(index)
        merged = dict(by_index[index])
        merged["name"] = _check_name(entry.get("name", merged["name"]), index=index)
        merged["visible"] = _check_visible(entry.get("visible", merged["visible"]), index=index)
        merged["opacity"] = _check_opacity(entry.get("opacity", merged["opacity"]), index=index)
        merged["order"] = _check_order(entry.get("order", merged["order"]), index=index)
        if "id" in entry:
            merged["id"] = entry["id"]
        out.append(merged)
    out.sort(key=lambda item: (int(item["order"]), int(item["index"])))
    return out


def render_layers(
    doc: dict,
    *,
    resolution: int,
    quality: str = "fast",
    layers: list[dict] | None = None,
) -> list[dict]:
    """Render every layer to straight-alpha RGBA (bottom-first).

    Each item: ``{index,id,name,visible,opacity,order,stroke_count,bbox,
    width,height,rgba,quality,fallback_reason,warnings,...}``.  Rendering uses
    :func:`render_fast.render_auto`; unsupported shapes fall back to exact
    automatically and the reason is recorded per layer.
    """
    _check_resolution(resolution)
    quality = _check_quality(quality)
    table = _resolve_layer_table(doc, layers)
    if not table:
        raise LayerError("FP_E_LAYER_EMPTY", "文档没有可导出的层（strokes/texts 为空）")
    return [
        _render_one_layer(doc, layer, resolution=int(resolution), quality=quality)
        for layer in table
    ]


def composite_over(layer_images: list[dict], background) -> np.ndarray:
    """Alpha-over composite of straight-alpha layers (bottom-first) onto RGB.

    Returns an ``(H, W, 3)`` uint8 array.  Used by tests/reports as an
    independent reconstruction path; the export writers use the full renderer
    for the merged/composite images instead.
    """
    if not layer_images:
        raise LayerError("FP_E_LAYER_EMPTY", "composite_over 需要至少一层")
    h, w = layer_images[0]["rgba"].shape[:2]
    out = np.zeros((h, w, 3), dtype=np.float64)
    out[...] = np.asarray(background, dtype=np.float64).reshape(-1)[:3]
    for image in layer_images:
        rgba = image["rgba"]
        if rgba.shape[:2] != (h, w):
            raise LayerError("FP_E_LAYER_RENDER", "层尺寸不一致，无法合成")
        a = rgba[..., 3].astype(np.float64)[..., None] / 255.0
        out = rgba[..., :3].astype(np.float64) * a + out * (1.0 - a)
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)


# ---------------------------------------------------------------------------
# PNG export
# ---------------------------------------------------------------------------
def _slug(name: str, *, fallback: str) -> str:
    value = _NAME_RE.sub("_", str(name)).strip("._-")
    return (value or fallback)[:40]


def render_layer_pngs(
    doc: dict,
    *,
    resolution: int,
    out_dir: str,
    quality: str = "fast",
    layer_images: list[dict] | None = None,
    layers: list[dict] | None = None,
) -> list[dict]:
    """Write one straight-alpha RGBA PNG per layer plus ``layers.json``.

    Returns the manifest entries (paths, sizes, sha256, layer metadata).  The
    source document and the input ``layer_images`` are not modified.
    """
    from PIL import Image

    if not isinstance(out_dir, (str, os.PathLike)):
        raise LayerError("FP_E_LAYER_INPUT", "out_dir 必须是路径")
    images = layer_images if layer_images is not None else render_layers(
        doc, resolution=resolution, quality=quality, layers=layers
    )
    if not images:
        raise LayerError("FP_E_LAYER_EMPTY", "文档没有可导出的层")
    base = os.fspath(out_dir)
    try:
        os.makedirs(base, exist_ok=True)
    except OSError as exc:
        raise LayerError("FP_E_LAYER_IO", f"无法创建输出目录 {base}: {exc}") from exc
    manifest: list[dict] = []
    for image in images:
        rgba = image["rgba"]
        filename = f"layer_{int(image['index']):02d}_{_slug(image['name'], fallback='layer')}.png"
        path = os.path.join(base, filename)
        try:
            Image.fromarray(np.ascontiguousarray(rgba), mode="RGBA").save(path, format="PNG")
        except OSError as exc:
            raise LayerError("FP_E_LAYER_IO", f"写入层 PNG 失败 {path}: {exc}") from exc
        with open(path, "rb") as handle:
            data = handle.read()
        manifest.append(
            {
                "index": int(image["index"]),
                "id": image.get("id"),
                "name": str(image["name"]),
                "visible": bool(image["visible"]),
                "opacity": float(image["opacity"]),
                "order": int(image["order"]),
                "stroke_count": int(image.get("stroke_count", 0)),
                "fill_count": int(image.get("fill_count", 0)),
                "text_count": int(image.get("text_count", 0)),
                "bbox": image.get("bbox"),
                "width": int(image["width"]),
                "height": int(image["height"]),
                "quality": image.get("quality"),
                "fallback_reason": list(image.get("fallback_reason") or []),
                "warnings": list(image.get("warnings") or []),
                "path": path,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    index_path = os.path.join(base, "layers.json")
    payload = {
        "schema": "fdraw-layers/1",
        "width": int(images[0]["width"]),
        "height": int(images[0]["height"]),
        "layers": [
            {
                **{key: value for key, value in entry.items() if key != "path"},
                "file": os.path.basename(entry["path"]),
            }
            for entry in manifest
        ],
    }
    with open(index_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
    return manifest
