"""SVG → FDSL → .fbs → PNG: compile/render integration and CLI smoke tests."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _fixtures import convert, elements, svg  # noqa: E402

from fourier_prompt.schema import validate_plan  # noqa: E402
from fourier_prompt.svg_import import main  # noqa: E402

RENDER_SVG = (
    '<rect x="0" y="0" width="200" height="200" fill="#ffffff"/>'
    '<path d="M 20 20 H 180 V 180 H 20 Z M 70 70 H 130 V 130 H 70 Z" '
    'fill="#3366cc" fill-rule="evenodd"/>'
    '<circle cx="100" cy="100" r="90" fill="none" stroke="#cc2222" stroke-width="1.2"/>'
)


class CompileRenderCase(unittest.TestCase):
    def test_plan_compiles_and_renders_non_blank(self):
        import numpy as np

        from fourier_prompt.compile import compile_plan
        from fourier_prompt.compat import skill_api

        plan, warnings, _stats = convert(RENDER_SVG)
        self.assertEqual(warnings, [])
        self.assertEqual(validate_plan(plan), [])
        result = compile_plan(plan, resolution=96)
        self.assertGreaterEqual(result["stats"]["records"], 3)
        image = skill_api.render(result["fbs"], resolution=96, supersample=2)
        self.assertEqual(image.shape, (96, 96, 3))
        # Hole is white, the donut body is blue, the ring is red.
        center = image[48, 48]
        self.assertTrue(np.all(center > 240), center)
        body = image[19, 48]
        self.assertGreater(int(body[2]), int(body[0]) + 60)  # blue channel dominates
        self.assertGreater(int(body[2]), 120)
        diff = np.abs(image.astype(int) - np.array([255, 255, 255])).sum(axis=2)
        self.assertGreater(int((diff > 8).sum()), 96 * 96 * 0.1)

    def test_minimal_stroke_only_plan_renders(self):
        from fourier_prompt.compile import compile_plan
        from fourier_prompt.compat import skill_api

        plan, _warnings, _stats = convert(
            '<path d="M 20 100 C 60 20 140 180 180 100" fill="none" '
            'stroke="black" stroke-width="3"/>'
        )
        result = compile_plan(plan, resolution=64)
        image = skill_api.render(result["fbs"], resolution=64, supersample=2)
        self.assertGreater(int(image.min()), -1)
        self.assertGreater(int((255 - image.astype(int)).sum()), 0)


class CliCase(unittest.TestCase):
    def test_cli_writes_plan_png_and_fdraw(self):
        document = svg(
            '<rect x="10" y="10" width="80" height="80" fill="#c04030"/>'
            '<circle cx="140" cy="140" r="40" fill="none" stroke="black" '
            'stroke-width="2"/>'
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svg_path = root / "input.svg"
            svg_path.write_text(document, encoding="utf-8")
            plan_path = root / "plan.json"
            png_path = root / "out" / "plan.png"
            fdraw_path = root / "out" / "plan.fdraw"
            code = main(
                [
                    "--svg", str(svg_path),
                    "--out", str(plan_path),
                    "--png", str(png_path),
                    "--fdraw", str(fdraw_path),
                    "--resolution", "96",
                    "--quiet",
                ]
            )
            self.assertEqual(code, 0)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(validate_plan(plan), [])
            self.assertGreater(png_path.stat().st_size, 800)
            self.assertTrue(fdraw_path.read_bytes().startswith(b"PK"))
            self.assertEqual(plan["meta"]["source"]["name"], "input.svg")

    def test_cli_missing_file_returns_1(self):
        self.assertEqual(main(["--svg", "does-not-exist.svg", "--out", "x.json"]), 1)

    def test_cli_invalid_svg_returns_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "bad.svg"
            path.write_text("<svg><rect></svg>", encoding="utf-8")
            code = main(["--svg", str(path), "--out", str(root / "plan.json")])
            self.assertEqual(code, 1)
            self.assertFalse((root / "plan.json").exists())


if __name__ == "__main__":
    unittest.main()
