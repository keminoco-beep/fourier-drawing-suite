"""Shared fixtures for WP-K text/font tests (stdlib unittest only)."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKAGE_ROOT = HERE.parents[1]  # <repo>/fourier-prompt
FIXTURES = HERE / "fixtures"
FONT_PATH = PACKAGE_ROOT / "assets" / "fonts" / "NotoSansSC-VF.ttf"
OFL_PATH = PACKAGE_ROOT / "assets" / "fonts" / "OFL.txt"
LICENSES_PATH = PACKAGE_ROOT / "assets" / "fonts" / "LICENSES.md"

for candidate in (str(HERE), str(PACKAGE_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)


def text_plan() -> dict:
    """Hand-written two-line plan: Chinese + English + digits + texts[] entry."""
    return json.loads((FIXTURES / "text_plan.json").read_text(encoding="utf-8"))


def text_element(text: str = "你好，Fourier! 2026", *, size_px=32, **overrides) -> dict:
    element = {
        "kind": "text",
        "id": "e1",
        "layer": 0,
        "as": "outline",
        "text": text,
        "font": {"id": "source_han_sans_sc", "size_px": size_px, "color": "ink", "align": "left"},
        "box": [16, 16, 320, 160],
    }
    element.update(overrides)
    return element


def single_text_plan(text: str = "你好，Fourier! 2026", *, size_px=32, canvas=(384, 192), **element_overrides) -> dict:
    plan = {
        "schema": "fdsl/2.1",
        "name": "wpk_single_text",
        "canvas": {"width": canvas[0], "height": canvas[1], "background": [250, 248, 242]},
        "palette": {"ink": "#1e2430"},
        "layers": [{"id": 0, "name": "type"}],
        "elements": [text_element(text, size_px=size_px, **element_overrides)],
    }
    return plan


def write_json(directory, name: str, payload) -> str:
    path = Path(directory) / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def run_cli(*argv):
    """Run ``fourier_prompt.cli.main`` in-process and capture stdout/stderr."""
    from fourier_prompt.cli import main

    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(list(argv))
    return code, stdout.getvalue(), stderr.getvalue()


def render_fbs(fbs: dict, resolution: int = 256):
    from fourier_prompt.compat import skill_api

    return skill_api.render(fbs, resolution=resolution, supersample=1)


def pixel_hash(rgb) -> str:
    return hashlib.sha256(rgb.tobytes()).hexdigest()


def save_png(rgb, path) -> None:
    from PIL import Image

    Image.fromarray(rgb).save(path)


__all__ = [
    "FONT_PATH",
    "FIXTURES",
    "LICENSES_PATH",
    "OFL_PATH",
    "PACKAGE_ROOT",
    "copy",
    "pixel_hash",
    "render_fbs",
    "run_cli",
    "save_png",
    "single_text_plan",
    "tempfile",
    "text_element",
    "text_plan",
    "write_json",
]
