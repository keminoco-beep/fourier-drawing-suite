"""Shared fixtures for WP-I layer / PSD / KRA tests (no pytest dependency)."""

from __future__ import annotations

import copy
import contextlib
import io
import json
import struct
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKAGE_ROOT = HERE.parents[1]  # <repo>/fourier-prompt
REPO_ROOT = PACKAGE_ROOT.parent
OUT = PACKAGE_ROOT / "out"

for candidate in (str(HERE), str(PACKAGE_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from fourier_prompt import compile as compile_mod  # noqa: E402
from fourier_prompt import kra, layers, psd  # noqa: E402
from fourier_prompt.fdraw import unpack  # noqa: E402

SAMPLE_FDRAW = OUT / "verify" / "01.fdraw"
SAMPLE_06_FBS = OUT / "wp-d" / "06_cat_tophat.fbs"
TILE = 64


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------
def small_plan(*, layers_spec=None):
    return {
        "schema": "fdsl/2.1",
        "name": "wp_i_small",
        "canvas": {"width": 96, "height": 64, "background": [250, 248, 242]},
        "palette": {"ink": "#202630", "gold": "#d8a83e", "cream": "#f6efe0"},
        "layers": layers_spec
        or [
            {"id": 0, "name": "base"},
            {"id": 1, "name": "ink"},
        ],
        "elements": [
            {
                "kind": "fill",
                "layer": 0,
                "color": "cream",
                "poly": [[4, 4], [92, 4], [92, 60], [4, 60]],
            },
            {
                "kind": "stroke",
                "layer": 0,
                "points": [[8, 56], [40, 8], [88, 52]],
                "brush": {"width": 3.0, "color": "gold"},
            },
            {
                "kind": "stroke",
                "layer": 1,
                "points": [[10, 10], [50, 30], [86, 12]],
                "brush": {"width": 2.0, "color": "ink"},
            },
            {
                "kind": "fill",
                "layer": 1,
                "color": "ink",
                "poly": [[36, 36], [60, 36], [60, 58], [36, 58]],
            },
        ],
    }


def small_doc(*, layers_spec=None):
    plan = small_plan(layers_spec=layers_spec)
    return compile_mod.compile_plan(plan, resolution=64)["fbs"]


def unicode_doc():
    return small_doc(
        layers_spec=[
            {"id": 0, "name": "底色"},
            {"id": 1, "name": "墨线"},
        ]
    )


def doc_without_layers():
    doc = small_doc()
    doc.pop("layers", None)
    return doc


def sample_01():
    return unpack(str(SAMPLE_FDRAW))["render"]


def sample_06():
    with open(SAMPLE_06_FBS, "r", encoding="utf-8") as handle:
        return json.load(handle)


def prepare_01():
    doc = copy.deepcopy(sample_01())
    layers.add_layer_metadata(doc, None)
    return doc


def prepare_06():
    doc = copy.deepcopy(sample_06())
    layers.add_layer_metadata(doc, None)
    return doc


# ---------------------------------------------------------------------------
# assertions / parsers
# ---------------------------------------------------------------------------
def clone(value):
    return copy.deepcopy(value)


def layer_pixels(doc, *, resolution, quality="fast", layer_index=None):
    images = layers.render_layers(doc, resolution=resolution, quality=quality)
    if layer_index is None:
        return images
    return next(image for image in images if image["index"] == layer_index)


def psd_plane(blob, info, record, channel):
    """Return one channel plane for a PSD layer record (0=R,1=G,2=B,3=A)."""
    entry = info["layers"][record]
    channel_info = entry["channel_data"][channel]
    size = info["width"] * info["height"]
    start = channel_info["data_offset"]
    return blob[start : start + size]


def parse_kra_tiles(data: bytes, width: int, height: int):
    """Decode one KRA tiled layer stream (raw flag 0) to straight RGBA."""
    import numpy as np

    lines = data.split(b"\n")
    header = {}
    position = 0
    while position < len(lines):
        line = lines[position].decode("ascii")
        position += 1
        if line.startswith("DATA "):
            header["DATA"] = int(line.split()[1])
            break
        key, value = line.split(" ", 1)
        header[key] = int(value)
    assert header["VERSION"] == 2
    assert header["TILEWIDTH"] == TILE and header["TILEHEIGHT"] == TILE
    assert header["PIXELSIZE"] == 4
    stream = b"\n".join(lines[position:])
    canvas = np.zeros((height, width, 4), dtype=np.uint8)
    tile_count = 0
    offset = 0
    while offset < len(stream) and tile_count < header["DATA"]:
        end = stream.index(b"\n", offset)
        record = stream[offset:end].decode("ascii")
        offset = end + 1
        tx, ty, codec, csize = record.split(",")
        tx, ty, csize = int(tx), int(ty), int(csize)
        assert codec == "LZF"
        flag = stream[offset]
        payload = stream[offset + 1 : offset + csize]
        offset += csize
        assert flag == 0, f"expected raw tile flag 0, got {flag}"
        assert len(payload) == TILE * TILE * 4
        planes = np.frombuffer(payload, dtype=np.uint8).reshape(4, TILE, TILE)
        tile = np.stack([planes[2], planes[1], planes[0], planes[3]], axis=-1)
        hh = min(TILE, height - ty)
        ww = min(TILE, width - tx)
        canvas[ty : ty + hh, tx : tx + ww] = tile[:hh, :ww]
        tile_count += 1
    assert tile_count == header["DATA"]
    return canvas, header


def kra_layer_arrays(blob: bytes, width: int, height: int):
    import numpy as np

    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = archive.namelist()
        streams = {}
        for name in names:
            if "/layers/" in name:
                arrays, _header = parse_kra_tiles(archive.read(name), width, height)
                streams[name.rsplit("/", 1)[-1]] = np.asarray(arrays, dtype=np.uint8)
        return names, streams


def run_cli(argv):
    from fourier_prompt.layer_cli import main

    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(list(argv))
    return code, stdout.getvalue(), stderr.getvalue()


@contextlib.contextmanager
def tempdir():
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        yield Path(directory)
