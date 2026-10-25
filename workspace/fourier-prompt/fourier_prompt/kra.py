"""Minimal Krita KRA writer for layered FDSL documents (WP-I).

A KRA file is a ZIP container (documented Krita native format, implemented
independently from the public format description):

``mimetype`` (first entry, STORED, ``application/x-krita``), ``maindoc.xml``,
``documentinfo.xml``, ``preview.png``, ``mergedimage.png`` and one tiled pixel
stream per layer under ``<docname>/layers/layerN``.

* ``maindoc.xml`` lists layers **TOP-first**; pixel streams are bottom-first
  (paper, then document layers sorted by ``(order, index)``);
* layer names are UTF-8 XML attributes;
* tiles are 64x64, PIXELSIZE 4, BGRA channel-planar, written **uncompressed**
  (``flag=0`` with the still-required ``LZF`` codec marker — the simplification
  explicitly allowed by the work package; no LZF implementation is included);
* ``visible="0"`` marks hidden layers; per-layer opacity is baked into the
  tile alpha (maindoc opacity stays 255 so Krita does not apply it twice);
* no ICC profile is embedded (keeps the file free of third-party ICC assets;
  Krita falls back to its default sRGB).

Determinism: fixed ZIP timestamps/attributes, fixed XML, no randomness; the
same document/resolution/quality produces identical bytes.
"""

from __future__ import annotations

import hashlib
import io
import os
import zipfile
from xml.sax.saxutils import escape

import numpy as np

from .layers import (
    LayerError,
    _base_rgb,
    _check_quality,
    _check_resolution,
    canvas_size,
    render_layers,
)

__all__ = ["write_kra", "build_kra", "TILE_SIZE", "PIXELSIZE", "KRA_MIME"]

KRA_MIME = b"application/x-krita"
TILE_SIZE = 64
PIXELSIZE = 4
DOC_NAME = "fourier-prompt"
ZIP_DATE = (1980, 1, 1, 0, 0, 0)
PAPER_LAYER_NAME = "paper"


# ---------------------------------------------------------------------------
# tiled layer stream
# ---------------------------------------------------------------------------
def _planar_bgra_tile(rgba: np.ndarray, width: int, height: int, tx: int, ty: int) -> bytes:
    tile = np.zeros((TILE_SIZE, TILE_SIZE, 4), dtype=np.uint8)
    sub = rgba[ty : ty + TILE_SIZE, tx : tx + TILE_SIZE, :4]
    tile[: sub.shape[0], : sub.shape[1], :] = sub
    # Krita's raw tile payload is channel-planar B, G, R, A.
    planes = tile[..., [2, 1, 0, 3]].transpose(2, 0, 1)
    return np.ascontiguousarray(planes).tobytes(order="C")


def _layer_stream(rgba: np.ndarray) -> bytes:
    height, width = rgba.shape[:2]
    cols = (width + TILE_SIZE - 1) // TILE_SIZE
    rows = (height + TILE_SIZE - 1) // TILE_SIZE
    count = cols * rows
    header = (
        f"VERSION 2\nTILEWIDTH {TILE_SIZE}\nTILEHEIGHT {TILE_SIZE}\n"
        f"PIXELSIZE {PIXELSIZE}\nDATA {count}\n"
    ).encode("ascii")
    body = bytearray()
    for ty in range(0, height, TILE_SIZE):
        for tx in range(0, width, TILE_SIZE):
            planar = _planar_bgra_tile(np.ascontiguousarray(rgba), width, height, tx, ty)
            # codec marker stays "LZF" (real Krita format) but flag 0 = raw;
            # csize includes the one-byte flag.
            body += f"{tx},{ty},LZF,{TILE_SIZE * TILE_SIZE * PIXELSIZE + 1}\n".encode("ascii")
            body.append(0)
            body += planar
    return header + bytes(body)


# ---------------------------------------------------------------------------
# XML
# ---------------------------------------------------------------------------
def _xml_name(text: str) -> str:
    return escape(str(text), {'"': "&quot;", "'": "&apos;"})


def _layer_xml(filename: str, name: str, *, visible: bool, selected: bool) -> str:
    return (
        f'   <layer filename="{filename}" name="{_xml_name(name)}" opacity="255" '
        f'visible="{1 if visible else 0}" nodetype="paintlayer" compositeop="normal" '
        f'x="0" y="0" locked="0" channelflags="" selected="{"true" if selected else "false"}"/>\n'
    )


def _maindoc_xml(width: int, height: int, names: list[str], visibles: list[bool]) -> bytes:
    """``names``/``visibles`` are bottom-first; XML records are TOP-first."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<!DOCTYPE DOC PUBLIC '-//KDE//DTD krita 2.0//EN' 'http://www.calligra.org/DTD/krita-2.0.dtd'>",
        '<DOC xmlns="http://www.calligra.org/DTD/krita" syntaxVersion="2.0" '
        'editor="fourier-prompt" kritaVersion="5.3.3">',
        f' <IMAGE name="{DOC_NAME}" width="{width}" height="{height}" colorspacename="RGBA" '
        f'mime="application/x-kra" description="" x-res="72" y-res="72">',
        "  <layers>",
    ]
    top = len(names) - 1
    for index in range(len(names) - 1, -1, -1):
        lines.append(
            _layer_xml(
                f"layer{index}",
                names[index],
                visible=visibles[index],
                selected=(index == top),
            ).rstrip("\n")
        )
    lines += ["  </layers>", " </IMAGE>", "</DOC>", ""]
    return "\n".join(lines).encode("utf-8")


def _documentinfo_xml() -> bytes:
    # Krita's KoDocumentInfo requires non-empty dates; fixed values keep bytes
    # deterministic (no wall clock in the file).
    text = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE document-info PUBLIC '-//KDE//DTD document-info 1.1//EN' 'http://www.calligra.org/DTD/document-info-1.1.dtd'>
<document-info xmlns="http://www.calligra.org/DTD/document-info">
 <about>
  <title>fourier-prompt</title>
  <description></description>
  <subject></subject>
  <abstract><![CDATA[]]></abstract>
  <keyword></keyword>
  <initial-creator>fourier-prompt</initial-creator>
  <editing-cycles>1</editing-cycles>
  <editing-time>0</editing-time>
  <date>2026-01-01T00:00:00</date>
  <creation-date>2026-01-01T00:00:00</creation-date>
  <language></language>
  <license></license>
 </about>
 <author>
  <full-name></full-name>
  <initial></initial>
 </author>
</document-info>
"""
    return text.encode("utf-8")


# ---------------------------------------------------------------------------
# PNG helpers
# ---------------------------------------------------------------------------
def _png_bytes(image) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(image)).save(buffer, format="PNG")
    return buffer.getvalue()


def _preview_bytes(composite: np.ndarray, max_side: int = 256) -> bytes:
    from PIL import Image

    image = Image.fromarray(np.ascontiguousarray(composite))
    image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# ZIP
# ---------------------------------------------------------------------------
def _zip_entries(entries: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_STORED if name == "mimetype" else zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data, compress_type=info.compress_type, compresslevel=9)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# public
# ---------------------------------------------------------------------------
def build_kra(
    *,
    width: int,
    height: int,
    composite_rgb: np.ndarray,
    layer_images: list[dict],
    paper_rgb: list[int],
) -> tuple[bytes, list[dict], list[dict], list[dict]]:
    """Assemble KRA bytes from a bottom-first layer stack + opaque composite."""
    if not isinstance(width, int) or not isinstance(height, int) or width < 1 or height < 1:
        raise LayerError("FP_E_KRA_LIMIT", f"KRA 尺寸非法: {width}x{height}")
    if tuple(composite_rgb.shape[:2]) != (height, width):
        raise LayerError(
            "FP_E_KRA_COMPOSITE",
            f"composite 尺寸异常: {composite_rgb.shape}（需要 {height}x{width}x3）",
        )
    paper = np.empty((height, width, 4), dtype=np.uint8)
    paper[..., 0] = int(paper_rgb[0])
    paper[..., 1] = int(paper_rgb[1])
    paper[..., 2] = int(paper_rgb[2])
    paper[..., 3] = 255
    full: list[dict] = [
        {
            "role": "paper",
            "name": PAPER_LAYER_NAME,
            "visible": True,
            "opacity": 1.0,
            "rgba": paper,
            "quality": "exact",
            "fallback_reason": [],
        }
    ]
    full.extend(layer_images)
    names = [str(item.get("name", f"layer{index}")) for index, item in enumerate(full)]
    for index, name in enumerate(names):
        try:
            name.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise LayerError(
                "FP_E_KRA_NAME",
                f"层 {index} 名称无法编码为 UTF-8（{name!r}）: {exc}",
            ) from exc
    visibles = [bool(item.get("visible", True)) for item in full]
    maindoc = _maindoc_xml(width, height, names, visibles)

    entries: list[tuple[str, bytes]] = [
        ("mimetype", KRA_MIME),
        ("maindoc.xml", maindoc),
        ("documentinfo.xml", _documentinfo_xml()),
        ("preview.png", _preview_bytes(composite_rgb)),
        ("mergedimage.png", _png_bytes(composite_rgb[..., :3])),
    ]
    layer_meta: list[dict] = []
    warnings: list[dict] = []
    for index, item in enumerate(full):
        rgba = item.get("rgba")
        if rgba is None or tuple(rgba.shape[:2]) != (height, width):
            raise LayerError(
                "FP_E_KRA_LAYER",
                f"层 {item.get('name')!r} 尺寸 {None if rgba is None else rgba.shape} != {width}x{height}",
            )
        data = _layer_stream(rgba)
        path = f"{DOC_NAME}/layers/layer{index}"
        entries.append((path, data))
        warnings.extend(item.get("warnings") or [])
        layer_meta.append(
            {
                "record": index,
                "role": item.get("role", "layer"),
                "index": item.get("index"),
                "name": names[index],
                "visible": visibles[index],
                "opacity": float(item.get("opacity", 1.0)),
                "opacity_baked": item.get("role") != "paper" and float(item.get("opacity", 1.0)) != 1.0,
                "width": width,
                "height": height,
                "path": path,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "tiles": ((width + TILE_SIZE - 1) // TILE_SIZE) * ((height + TILE_SIZE - 1) // TILE_SIZE),
                "quality": item.get("quality"),
                "fallback_reason": list(item.get("fallback_reason") or []),
            }
        )
    blob = _zip_entries(entries)
    entry_reports = [
        {"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in entries
    ]
    return blob, layer_meta, warnings, entry_reports


def write_kra(
    doc: dict,
    *,
    out: str | None,
    resolution: int,
    quality: str = "fast",
    layer_images: list[dict] | None = None,
    layers: list[dict] | None = None,
) -> dict:
    """Render each layer and write a minimal KRA (``out=None`` returns bytes)."""
    resolution = _check_resolution(resolution)
    quality = _check_quality(quality)
    if not isinstance(doc, dict):
        raise LayerError("FP_E_LAYER_INPUT", "doc 必须是 dict")
    width, height = canvas_size(doc, resolution)
    if layer_images is None:
        layer_images = render_layers(
            doc, resolution=resolution, quality=quality, layers=layers
        )
    if not layer_images:
        raise LayerError("FP_E_LAYER_EMPTY", "没有可写入 KRA 的层")
    from . import render_fast

    composite, composite_info = render_fast.render_auto(
        doc,
        resolution=resolution,
        quality=quality,
        background=_base_rgb(doc),
        return_info=True,
    )
    blob, layer_meta, warnings, entry_reports = build_kra(
        width=width,
        height=height,
        composite_rgb=composite,
        layer_images=layer_images,
        paper_rgb=_base_rgb(doc),
    )
    if out is not None:
        path = os.fspath(out)
        parent = os.path.dirname(os.path.abspath(path))
        try:
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as handle:
                handle.write(blob)
            os.replace(tmp, path)
        except OSError as exc:
            raise LayerError("FP_E_KRA_IO", f"写入 KRA 失败 {path}: {exc}") from exc
    fallbacks = [
        {"index": image["index"], "name": image["name"], "reasons": list(image.get("fallback_reason") or [])}
        for image in layer_images
        if image.get("fallback_reason")
    ]
    result = {
        "out": os.path.abspath(os.fspath(out)) if out is not None else None,
        "bytes": len(blob),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "width": width,
        "height": height,
        "layers": layer_meta,
        "layer_count": len(layer_meta),
        "quality": quality,
        "composite_quality": composite_info.get("quality"),
        "fallbacks": fallbacks,
        "warnings": warnings,
        "entries": entry_reports,
    }
    if out is None:
        result["blob"] = blob
    return result
