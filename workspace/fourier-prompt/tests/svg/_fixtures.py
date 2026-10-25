"""Shared fixtures for WP-S SVG importer tests (stdlib unittest, no pytest)."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKAGE_ROOT = HERE.parents[1]  # <repo>/fourier-prompt
REPO_ROOT = PACKAGE_ROOT.parent

for _candidate in (str(HERE), str(PACKAGE_ROOT)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from fourier_prompt.svg_import import svg_to_fdsl  # noqa: E402

DEFAULT_CANVAS = (200, 200)


def svg(body, *, width=200, height=200, viewbox="0 0 200 200", head="", tail=""):
    """Build an SVG document around ``body``."""
    viewbox_attr = "" if viewbox is None else f' viewBox="{viewbox}"'
    size = ""
    if width is not None:
        size += f' width="{width}"'
    if height is not None:
        size += f' height="{height}"'
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg"{size}{viewbox_attr}>'
        f"{head}{body}{tail}</svg>"
    )


def convert(body="", *, head="", canvas=DEFAULT_CANVAS, **kwargs):
    """Convert ``body`` with a square 200x200 canvas unless overridden."""
    return svg_to_fdsl(svg(body, head=head), canvas=canvas, **kwargs)


def codes(warnings):
    return [warning["code"] for warning in warnings]


def elements(plan, kind=None):
    if kind is None:
        return list(plan["elements"])
    return [element for element in plan["elements"] if element["kind"] == kind]


def palette_rgbs(plan):
    """Palette name → original RGB tuple used by assertions."""
    return {
        name: tuple(int(value[index : index + 2], 16) for index in (1, 3, 5))
        for name, value in (plan.get("palette") or {}).items()
    }


def element_rgb(plan, element):
    name = element.get("color") or (element.get("brush") or {}).get("color")
    return palette_rgbs(plan)[name]


def bbox(points):
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (min(xs), min(ys), max(xs), max(ys))
