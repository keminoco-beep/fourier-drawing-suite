"""Shared fixtures for the WP-A core tests (no third-party dependencies)."""

from __future__ import annotations

import copy
import json
import os


def valid_plan():
    """A small but complete FDSL v2.1 plan that must validate cleanly."""
    return {
        "schema": "fdsl/2.1",
        "name": "fixture",
        "canvas": {"width": 256, "height": 256, "background": [255, 255, 255]},
        "palette": {"ink": "#12203a", "gold": "#e8c37a"},
        "layers": [{"id": 0, "name": "base"}, {"id": 1, "name": "ink"}],
        "elements": [
            {
                "kind": "fill",
                "id": "e1",
                "layer": 0,
                "color": "ink",
                "poly": [[8, 8], [248, 8], [248, 248], [8, 248]],
            },
            {
                "kind": "fourier_gen",
                "id": "e2",
                "gen": "circle",
                "layer": 1,
                "tags": ["fixture", "shape"],
                "transform": {"center": [128, 128], "scale": 0.5},
                "params": {"r": 1.0},
                "brush": {
                    "preset": "ink_fineliner",
                    "width": 2.0,
                    "color": "gold",
                    "alpha": 0.9,
                },
            },
            {
                "kind": "stroke",
                "id": "e3",
                "layer": 1,
                "points": [[16, 240], [240, 16]],
                "brush": {"preset": "ink_gpen", "width": 1.5, "alpha": 0.8},
            },
        ],
        "texts": [
            {
                "id": "t1",
                "as": "outline",
                "layer": 1,
                "text": "hi",
                "font": {"id": "inter", "size_px": 24, "color": "ink", "align": "center"},
            }
        ],
        "constraints": {"max_elements": 400, "single_line": False},
    }


def clone(plan):
    return copy.deepcopy(plan)


def write_json(directory, name, payload):
    path = os.path.join(str(directory), name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    return path


def write_text(directory, name, text):
    path = os.path.join(str(directory), name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path
