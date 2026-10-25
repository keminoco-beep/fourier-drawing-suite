"""Hand-written PSD writer for layered FDSL documents (WP-I).

The output is an Adobe Photoshop 8BPS v1 file: 8-bit RGB, straight alpha,
**bottom-first** layer records, a bottom "paper" coat filled with the document
base colour, and a merged composite image identical to the normal renderer.
No layer groups and no blend modes are written (``norm`` only, opacity 255:
per-layer opacity is baked into the alpha channel by :mod:`fourier_prompt.layers`).

Format notes (implemented independently from the public Adobe PSD file format
specification; the project does not copy any third-party PSD writer):

* layer channels are **per-channel records** (2-byte compression + payload);
* a layer channel length **includes** the 2-byte compression marker;
* flags bit 1 is read as *hidden* by real readers, so visible = 0 and
  hidden = 2 (the spec's "bit 1 = visible" wording is a known documentation
  bug);
* the layer-info block length and the Pascal layer name are padded to even /
  multiple-of-4 boundaries;
* layer names are written to ``luni`` as UTF-16BE and to the legacy Pascal
  field as ASCII (``?`` substitution + warning when the name is non-ASCII).

Determinism: no timestamps, no randomness, no compression choices — the same
document/resolution/quality produces identical bytes.
"""

from __future__ import annotations

import hashlib
import os
import struct

from .layers import (
    LayerError,
    _base_rgb,
    _check_quality,
    _check_resolution,
    canvas_size,
    render_layers,
)

__all__ = ["write_psd", "build_psd", "parse_psd_layers"]

PSD_SIGNATURE = b"8BPS"
PSD_VERSION = 1
PSD_MAX_DIMENSION = 30000
PAPER_LAYER_NAME = "paper"


# ---------------------------------------------------------------------------
# name encoding
# ---------------------------------------------------------------------------
def _pascal_pad4(name: str) -> bytes:
    raw = name.encode("ascii", "replace")[:255]
    if not raw:
        raw = b"layer"
    data = bytes([len(raw)]) + raw
    data += b"\x00" * ((-len(data)) % 4)
    return data


def _luni_block(name: str) -> bytes:
    data = name.encode("utf-16-be")
    # PSD stores the length in UTF-16 code units (not bytes); psd-tools and
    # Photoshop's readers multiply by two.  Keep the payload itself UTF-16BE.
    units = len(data) // 2
    block = b"8BIM" + b"luni" + struct.pack(">I", units) + data
    if len(block) % 2:  # UTF-16 payloads are always even; defensive
        block += b"\x00"
    return block


def _psd_layer_name(name: str) -> tuple[bytes, bytes, list[dict]]:
    warnings: list[dict] = []
    ascii_name = name.encode("ascii", "replace").decode("ascii")
    if ascii_name != name:
        warnings.append(
            {
                "code": "W_PSD_NAME_ASCII",
                "message": f"layer name {name!r} is not ASCII; Pascal field uses {ascii_name!r}, luni carries UTF-16",
            }
        )
    try:
        luni = _luni_block(name)
    except UnicodeEncodeError as exc:
        luni = b""
        warnings.append(
            {
                "code": "W_PSD_LUNI_FALLBACK",
                "message": f"layer name {name!r} cannot be encoded as UTF-16BE ({exc}); ASCII fallback only",
            }
        )
    return _pascal_pad4(ascii_name), luni, warnings


# ---------------------------------------------------------------------------
# bytes
# ---------------------------------------------------------------------------
def _layer_channel_data(rgba) -> bytes:
    """Compression-0 records for R, G, B and alpha (straight alpha)."""
    planes = [rgba[..., 0], rgba[..., 1], rgba[..., 2], rgba[..., 3]]
    return b"".join(b"\x00\x00" + plane.tobytes(order="C") for plane in planes)


def _layer_record(*, width: int, height: int, name: str, visible: bool, opacity: int) -> tuple[bytes, list[dict]]:
    pascal, luni, warnings = _psd_layer_name(name)
    channel_length = 2 + width * height  # compression marker included
    record = struct.pack(">iiii", 0, 0, height, width)
    record += struct.pack(">H", 4)
    for channel_id in (0, 1, 2, -1):
        record += struct.pack(">hI", channel_id, channel_length)
    record += b"8BIM" + b"norm"
    flags = 0 if visible else 2  # bit 1 = hidden (real-reader semantics)
    record += bytes([int(opacity) & 0xFF, 0, flags, 0])
    extra = struct.pack(">II", 0, 0) + pascal + luni
    record += struct.pack(">I", len(extra)) + extra
    return record, warnings


def build_psd(
    *,
    width: int,
    height: int,
    composite_rgb,
    layer_images: list[dict],
    paper_rgb,
) -> tuple[bytes, dict]:
    """Build PSD bytes from a bottom-first layer stack + opaque composite."""
    if not isinstance(width, int) or not isinstance(height, int) or width < 1 or height < 1:
        raise LayerError("FP_E_PSD_LIMIT", f"PSD 尺寸非法: {width}x{height}")
    if width > PSD_MAX_DIMENSION or height > PSD_MAX_DIMENSION:
        raise LayerError(
            "FP_E_PSD_LIMIT",
            f"PSD 单边上限 {PSD_MAX_DIMENSION}px（got {width}x{height}）",
        )
    full: list[dict] = [
        {
            "role": "paper",
            "name": PAPER_LAYER_NAME,
            "visible": True,
            "opacity": 1.0,
            "rgba": None,
            "paper_rgb": [int(v) for v in paper_rgb],
        }
    ]
    full.extend(layer_images)
    if len(full) > 32767:
        raise LayerError("FP_E_PSD_LIMIT", f"PSD 层数上限 32767（got {len(full)}）")

    header = PSD_SIGNATURE + struct.pack(">H", PSD_VERSION) + b"\x00" * 6
    header += struct.pack(">HIIHH", 3, height, width, 8, 3)
    color_mode = struct.pack(">I", 0)
    image_resources = struct.pack(">I", 0)

    records = b""
    channel_data = b""
    layer_meta: list[dict] = []
    warnings: list[dict] = []
    for position, item in enumerate(full):
        if item.get("role") == "paper":
            rgba_bytes = b""
            channel_data += (
                b"\x00\x00" + bytes(item["paper_rgb"][0:1]) * (width * height)
                + b"\x00\x00" + bytes(item["paper_rgb"][1:2]) * (width * height)
                + b"\x00\x00" + bytes(item["paper_rgb"][2:3]) * (width * height)
                + b"\x00\x00" + b"\xff" * (width * height)
            )
        else:
            rgba = item.get("rgba")
            if rgba is None or tuple(rgba.shape[:2]) != (height, width):
                raise LayerError(
                    "FP_E_PSD_LAYER",
                    f"层 {item.get('name')!r} 尺寸 {None if rgba is None else rgba.shape} != {width}x{height}",
                )
            channel_data += _layer_channel_data(rgba)
        record, name_warnings = _layer_record(
            width=width,
            height=height,
            name=str(item.get("name", f"layer{position}")),
            visible=bool(item.get("visible", True)),
            opacity=255,
        )
        records += record
        warnings.extend(name_warnings)
        layer_meta.append(
            {
                "record": position,
                "role": item.get("role", "layer"),
                "index": item.get("index"),
                "name": str(item.get("name", f"layer{position}")),
                "visible": bool(item.get("visible", True)),
                "opacity": float(item.get("opacity", 1.0)),
                "opacity_baked": item.get("role") != "paper" and float(item.get("opacity", 1.0)) != 1.0,
                "width": width,
                "height": height,
                "quality": item.get("quality"),
                "fallback_reason": list(item.get("fallback_reason") or []),
            }
        )
    layer_info = struct.pack(">h", len(full)) + records + channel_data
    if len(layer_info) % 2:
        layer_info += b"\x00"
    section = struct.pack(">I", len(layer_info)) + layer_info + struct.pack(">I", 0)

    composite = composite_rgb
    if tuple(composite.shape[:2]) != (height, width) or composite.shape[2] < 3:
        raise LayerError(
            "FP_E_PSD_COMPOSITE",
            f"composite 尺寸/通道异常: {composite.shape}（需要 {height}x{width}x3）",
        )
    rgb = composite[..., :3]
    image_data = b"\x00\x00" + b"".join(
        rgb[..., channel].tobytes(order="C") for channel in range(3)
    )
    blob = (
        header
        + color_mode
        + image_resources
        + struct.pack(">I", len(section))
        + section
        + image_data
    )
    return blob, {"layers": layer_meta, "warnings": warnings}


def write_psd(
    doc: dict,
    *,
    layers: list[dict] | None = None,
    out: str | None,
    resolution: int,
    quality: str = "fast",
    layer_images: list[dict] | None = None,
) -> dict:
    """Render each layer and write a bottom-first layered PSD.

    Returns ``{out,bytes,sha256,width,height,layers,quality,fallbacks,warnings}``.
    ``layer_images`` lets a caller reuse renders from
    :func:`fourier_prompt.layers.render_layers`; ``layers`` optionally overrides
    names/visibility/opacity/order.  ``out=None`` returns the bytes under
    ``blob`` instead of writing (used by tests).
    """
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
        raise LayerError("FP_E_LAYER_EMPTY", "没有可写入 PSD 的层")
    from . import render_fast

    composite, composite_info = render_fast.render_auto(
        doc,
        resolution=resolution,
        quality=quality,
        background=_base_rgb(doc),
        return_info=True,
    )
    blob, meta = build_psd(
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
            raise LayerError("FP_E_PSD_IO", f"写入 PSD 失败 {path}: {exc}") from exc
    fallbacks = [
        {"index": image["index"], "name": image["name"], "reasons": list(image.get("fallback_reason") or [])}
        for image in layer_images
        if image.get("fallback_reason")
    ]
    warnings = list(meta.get("warnings") or [])
    for image in layer_images:
        warnings.extend(image.get("warnings") or [])
    result = {
        "out": os.path.abspath(os.fspath(out)) if out is not None else None,
        "bytes": len(blob),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "width": width,
        "height": height,
        "layers": meta.get("layers") or [],
        "layer_count": len(meta.get("layers") or []),
        "quality": quality,
        "composite_quality": composite_info.get("quality"),
        "fallbacks": fallbacks,
        "warnings": warnings,
    }
    if out is None:
        result["blob"] = blob
    return result


# ---------------------------------------------------------------------------
# minimal independent parser (tests / diagnostics; not a full PSD reader)
# ---------------------------------------------------------------------------
def parse_psd_layers(blob: bytes) -> dict:
    """Parse header + layer records from PSD bytes written by this module.

    Returns ``{width,height,depth,color_mode,channels,layer_count,layers,
    composite_channels}`` where each layer has ``name,ascii_name,luni_name,
    bbox,flags,opacity,channel_lengths``.  It is intentionally strict: any
    structural inconsistency raises :class:`LayerError`.
    """
    if not isinstance(blob, (bytes, bytearray)) or len(blob) < 40:
        raise LayerError("FP_E_PSD_PARSE", "PSD 数据过短")
    data = bytes(blob)
    if data[:4] != PSD_SIGNATURE:
        raise LayerError("FP_E_PSD_PARSE", f"PSD 签名错误: {data[:4]!r}")
    version = struct.unpack(">H", data[4:6])[0]
    channels = struct.unpack(">H", data[12:14])[0]
    height = struct.unpack(">I", data[14:18])[0]
    width = struct.unpack(">I", data[18:22])[0]
    depth = struct.unpack(">H", data[22:24])[0]
    color_mode = struct.unpack(">H", data[24:26])[0]
    pos = 26
    color_len = struct.unpack(">I", data[pos : pos + 4])[0]
    pos += 4 + color_len
    resources_len = struct.unpack(">I", data[pos : pos + 4])[0]
    pos += 4 + resources_len
    section_len = struct.unpack(">I", data[pos : pos + 4])[0]
    pos += 4
    section_start = pos
    if section_len < 4 or pos + section_len > len(data):
        raise LayerError("FP_E_PSD_PARSE", f"Layer/Mask 段长度异常: {section_len}")
    inner_len = struct.unpack(">I", data[pos : pos + 4])[0]
    pos += 4
    inner_start = pos
    if inner_len < 2 or inner_start + inner_len > section_start + section_len:
        raise LayerError("FP_E_PSD_PARSE", f"Layer info 长度异常: {inner_len}")
    layer_count = struct.unpack(">h", data[pos : pos + 2])[0]
    pos += 2
    layers = []
    for _ in range(abs(layer_count)):
        top, left, bottom, right = struct.unpack(">iiii", data[pos : pos + 16])
        pos += 16
        n_channels = struct.unpack(">H", data[pos : pos + 2])[0]
        pos += 2
        channel_lengths = []
        for _channel in range(n_channels):
            channel_id = struct.unpack(">h", data[pos : pos + 2])[0]
            length = struct.unpack(">I", data[pos + 2 : pos + 6])[0]
            pos += 6
            channel_lengths.append({"id": channel_id, "length": length})
        blend_sig = data[pos : pos + 4]
        blend_key = data[pos + 4 : pos + 8]
        pos += 8
        opacity = data[pos]
        clipping = data[pos + 1]
        flags = data[pos + 2]
        pos += 4
        extra_len = struct.unpack(">I", data[pos : pos + 4])[0]
        pos += 4
        extra_end = pos + extra_len
        mask_len = struct.unpack(">I", data[pos : pos + 4])[0]
        pos += 4 + mask_len
        blend_ranges = struct.unpack(">I", data[pos : pos + 4])[0]
        pos += 4 + blend_ranges
        name_len = data[pos]
        pos += 1
        ascii_name = data[pos : pos + name_len].decode("latin-1", "replace")
        pos += name_len
        # Pascal name is padded to a multiple of four bytes overall
        consumed = 1 + name_len
        pad = (-consumed) % 4
        pos += pad
        luni_name = None
        while pos + 12 <= extra_end:
            sig = data[pos : pos + 4]
            key = data[pos + 4 : pos + 8]
            length = struct.unpack(">I", data[pos + 8 : pos + 12])[0]
            # ``luni`` length is in UTF-16 code units; every other additional
            # layer info block uses a byte length.
            payload_len = length * 2 if key == b"luni" else length
            payload = data[pos + 12 : pos + 12 + payload_len]
            if len(payload) != payload_len:
                raise LayerError("FP_E_PSD_PARSE", "luni 段越界")
            if key == b"luni":
                luni_name = payload.decode("utf-16-be", "replace")
            pos += 12 + payload_len + (payload_len % 2)
        pos = extra_end
        layers.append(
            {
                "name": luni_name if luni_name is not None else ascii_name,
                "ascii_name": ascii_name,
                "luni_name": luni_name,
                "bbox": [left, top, right, bottom],
                "flags": flags,
                "hidden": bool(flags & 2),
                "opacity": opacity,
                "clipping": clipping,
                "blend_sig": blend_sig.decode("latin-1"),
                "blend_key": blend_key.decode("latin-1"),
                "channel_lengths": channel_lengths,
            }
        )
    # channel data length check: each channel record must fit the layer-info block
    channel_pos = pos
    channel_start = pos
    for layer in layers:
        offsets = []
        for channel in layer["channel_lengths"]:
            if channel_pos + channel["length"] > inner_start + inner_len:
                raise LayerError("FP_E_PSD_PARSE", "层通道数据越界")
            offsets.append(
                {
                    "id": channel["id"],
                    "length": channel["length"],
                    "compression_offset": channel_pos,
                    "data_offset": channel_pos + 2,
                }
            )
            channel_pos += channel["length"]
        layer["channel_data"] = offsets
        if channel_pos % 2:
            channel_pos += 1
    return {
        "version": version,
        "channels": channels,
        "width": width,
        "height": height,
        "depth": depth,
        "color_mode": color_mode,
        "layer_count": len(layers),
        "layers": layers,
        "layer_info_length": inner_len,
        "section_length": section_len,
        "channel_data_start": channel_start,
        "channel_data_end": channel_pos,
        "file_size": len(data),
    }
