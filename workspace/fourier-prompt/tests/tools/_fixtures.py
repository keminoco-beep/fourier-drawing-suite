"""Shared fixtures for the WP-H tool tests.

Everything is generated in a temporary directory from a small FDSL plan, so the
tests do not depend on ``out/`` artifacts and never write inside the repo.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parents[1]
for candidate in (str(HERE), str(PKG_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from fourier_prompt import fdraw, tools  # noqa: E402

PLAN_RESOLUTION = 128


def mini_plan() -> dict:
    """A small, valid FDSL v2.1 plan (4 elements: 1 fill + 3 strokes)."""
    return {
        "schema": "fdsl/2.1",
        "name": "wp-h-fixture",
        "canvas": {"width": 128, "height": 128, "background": [255, 255, 255]},
        "palette": {"ink": "#12203a", "gold": "#e8c37a", "red": "#b23b3b"},
        "layers": [{"id": 0, "name": "F1"}, {"id": 1, "name": "L1"}],
        "elements": [
            {
                "kind": "fill",
                "layer": 0,
                "color": "ink",
                "poly": [[8, 8], [120, 8], [120, 120], [8, 120]],
            },
            {
                "kind": "fourier_gen",
                "gen": "circle",
                "layer": 1,
                "id": "e1",
                "transform": {"center": [64, 64], "scale": 0.6},
                "params": {"r": 1.0},
                "brush": {"preset": "ink_gpen", "width": 2.0, "color": "gold", "alpha": 0.9},
            },
            {
                "kind": "stroke",
                "layer": 1,
                "points": [[16, 120], [120, 16]],
                "brush": {"preset": "ink_fineliner", "width": 1.5, "alpha": 0.8},
            },
            {
                "kind": "stroke",
                "layer": 1,
                "points": [[16, 16], [64, 112], [112, 16]],
                "brush": {"preset": "basic_round_soft", "width": 4.0, "color": "red", "alpha": 1.0},
            },
        ],
        "constraints": {"max_elements": 400, "single_line": False},
    }


class Fixture:
    """Compiled ``.fbs`` + packed ``.fdraw`` + the source plan, in a temp dir."""

    def __init__(self, prefix: str = "fpt-tools-"):
        from fourier_prompt.compile import compile_plan
        from fourier_prompt.compat import skill_api

        self.dir = Path(tempfile.mkdtemp(prefix=prefix))
        plan = mini_plan()
        self.plan = plan
        self.plan_path = self.dir / "mini.plan.json"
        self.plan_path.write_text(
            json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        self.compiled = compile_plan(plan, resolution=PLAN_RESOLUTION)
        self.fbs_path = self.dir / "mini.fbs"
        skill_api.write_fbs(self.compiled["fbs"], str(self.fbs_path))
        self.fbs_doc = json.loads(self.fbs_path.read_text(encoding="utf-8"))
        self.stroke_count = len(self.fbs_doc.get("strokes") or [])
        self.fdraw_path = self.dir / "mini.fdraw"
        fdraw.pack(self.fbs_doc, source_fdsl=plan, out=str(self.fdraw_path))
        self.not_fbs = self.dir / "notes.txt"
        self.not_fbs.write_text("not a drawing", encoding="utf-8")
        self.bad_plan_path = self.dir / "bad.plan.json"
        self.bad_plan_path.write_text(
            json.dumps({"schema": "fdsl/9", "canvas": {}, "elements": []}, indent=1),
            encoding="utf-8",
        )
        self.corrupt_fdraw_path = self.dir / "corrupt.fdraw"
        blob = bytearray(self.fdraw_path.read_bytes())
        self.corrupt_fdraw_path.write_bytes(bytes(blob[: max(16, len(blob) // 3)]))

    def out(self, name: str) -> str:
        return str(self.dir / name)

    def close(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def png_size(path) -> tuple[int, int]:
    from PIL import Image

    with Image.open(path) as image:
        return image.size


def png_magic(path) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(8)


__all__ = ["Fixture", "mini_plan", "png_size", "png_magic", "tools", "fdraw", "PKG_ROOT"]
