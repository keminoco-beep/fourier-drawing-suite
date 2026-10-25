"""Shared fixtures for WP-D integration tests (no pytest dependency)."""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKAGE_ROOT = HERE.parents[1]  # <repo>/fourier-prompt
REPO_ROOT = PACKAGE_ROOT.parent  # <repo>
EXAMPLES = REPO_ROOT / "poc" / "fdsl" / "examples"

for candidate in (str(HERE), str(PACKAGE_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)


def write_json(directory, name: str, payload) -> str:
    path = Path(directory) / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def canonical_plan_shapes():
    """Hand-written FDSL v2.1 plan: fill + filled gen + stroked gen + stroke + text."""
    return {
        "schema": "fdsl/2.1",
        "name": "wp_d_canonical_shapes",
        "canvas": {"width": 256, "height": 256, "background": [250, 248, 242]},
        "palette": {"ink": "#1e2430", "red": "#c04030", "gold": "#d8a83e"},
        "layers": [{"id": 0, "name": "base"}, {"id": 1, "name": "ink"}],
        "elements": [
            {
                "kind": "fill",
                "id": "e1",
                "layer": 0,
                "color": "red",
                "poly": [[20, 20], [236, 20], [236, 236], [20, 236]],
                "holes": [[[96, 96], [160, 96], [160, 160], [96, 160]]],
            },
            {
                "kind": "fourier_gen",
                "id": "e2",
                "layer": 1,
                "gen": "circle",
                "closed": True,
                "params": {"at": [128, 128], "r": 82},
                "brush": {"preset": "ink_fineliner", "width": 2.0, "color": "gold"},
            },
            {
                "kind": "fourier_gen",
                "id": "e3",
                "layer": 1,
                "gen": "star",
                "closed": False,
                "params": {"at": [128, 128], "r": 60, "ri": 24, "n": 6, "rot": -90},
                "transform": {"center": [128, 128], "rotate": 15},
                "brush": {"preset": "ink_gpen", "width": 1.6, "color": "ink", "alpha": 0.9},
            },
            {
                "kind": "stroke",
                "id": "e4",
                "layer": 1,
                "closed": True,
                "points": [[128, 42], [158, 72], [158, 112], [128, 142], [98, 112], [98, 72]],
                "brush": {"preset": "ink_fineliner", "width": 1.2, "color": "ink", "alpha": 0.8},
            },
            {
                "kind": "text",
                "id": "e5",
                "layer": 1,
                "text": "wp-d",
                "font": {"id": "inter", "size_px": 18, "color": "ink", "align": "center"},
            },
        ],
        "constraints": {"max_elements": 400},
    }


def canonical_plan_curves():
    """Second hand-written plan exercising open gens, superellipse and width curves."""
    return {
        "schema": "fdsl/2.1",
        "name": "wp_d_canonical_curves",
        "canvas": {"width": 320, "height": 240, "background": "#f6efe0"},
        "palette": {"ink": "#24242c", "teal": "#2f6f6a", "cream": "#fbf5e6"},
        "layers": [{"id": 0, "name": "base"}, {"id": 1, "name": "ink"}],
        "elements": [
            {
                "kind": "fourier_gen",
                "layer": 0,
                "gen": "superellipse",
                "params": {"at": [160, 120], "rx": 120, "ry": 80, "n": 3.2},
                "brush": {"preset": "basic_round_soft", "width": 3.0, "color": "cream"},
            },
            {
                "kind": "fourier_gen",
                "layer": 1,
                "gen": "arc",
                "params": {"at": [160, 120], "r": 70, "from": 200, "to": 340},
                "transform": {"center": [160, 120], "rotate": 10, "scale": 0.9},
                "brush": {"preset": "ink_gpen", "width": 2.4, "color": "ink"},
            },
            {
                "kind": "fourier_gen",
                "layer": 1,
                "gen": "wave",
                "params": {"at": [20, 200], "w": 280, "amp": 18, "cycles": 2.5, "phase": 30},
                "brush": {
                    "preset": "ink_fineliner",
                    "width": [[0.0, 1.0], [0.5, 4.0], [1.0, 1.0]],
                    "color": "teal",
                    "alpha": 0.85,
                },
            },
            {
                "kind": "fourier_gen",
                "layer": 1,
                "gen": "lissajous",
                "closed": True,
                "params": {"at": [160, 120], "A": 40, "B": 28, "a": 3, "b": 2, "delta": 30},
                "brush": {"preset": "basic_round_soft", "width": 1.0, "color": "teal", "alpha": 0.35},
            },
        ],
    }


def run_cli(*argv):
    """Run ``fourier_prompt.cli.main`` in-process and capture output."""
    from fourier_prompt.cli import main

    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(list(argv))
    return code, stdout.getvalue(), stderr.getvalue()
