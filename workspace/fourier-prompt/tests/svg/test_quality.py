"""WP-V quality controls: fill dilation, 64-colour palette, stroke handling,
overflow strategies and their CLI wiring (stdlib unittest, no pytest)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _fixtures import (  # noqa: E402
    bbox,
    codes,
    convert,
    element_rgb,
    elements,
    svg,
)

from fourier_prompt.schema import validate_plan  # noqa: E402
from fourier_prompt.svg_import import SvgImportError, main  # noqa: E402


class FillDilateCase(unittest.TestCase):
    def test_dilation_expands_fill_and_is_recorded(self):
        body = '<rect x="20" y="30" width="40" height="20" fill="#336699"/>'
        exact, _warnings, _stats = convert(body)
        grown, warnings, stats = convert(body, fill_dilate_px=1.0)
        self.assertEqual(bbox(elements(exact, "fill")[0]["poly"]), (20.0, 30.0, 60.0, 50.0))
        self.assertEqual(bbox(elements(grown, "fill")[0]["poly"]), (19.0, 29.0, 61.0, 51.0))
        self.assertEqual(codes(warnings), [])
        self.assertEqual(stats["fill_dilate_px"], 1.0)
        self.assertEqual(stats["fills_dilated"], 1)
        self.assertEqual(grown["meta"]["svg_import"]["fill_dilate_px"], 1.0)

    def test_dilation_shrinks_holes(self):
        body = (
            '<path d="M 10 10 H 110 V 110 H 10 Z M 40 40 H 80 V 80 H 40 Z" '
            'fill="#224466" fill-rule="evenodd"/>'
        )
        plan, _warnings, _stats = convert(body, fill_dilate_px=0.5)
        fill = elements(plan, "fill")[0]
        self.assertEqual(bbox(fill["poly"]), (9.5, 9.5, 110.5, 110.5))
        self.assertEqual(len(fill["holes"]), 1)
        self.assertEqual(bbox(fill["holes"][0]), (40.5, 40.5, 79.5, 79.5))

    def test_dilation_config_validation(self):
        for kwargs in (
            {"fill_dilate_px": -1.0},
            {"fill_dilate_px": 4.5},
            {"fill_dilate_px": "wide"},
            {"overflow": "spread"},
            {"stroke_to_fill_max_width": 0.0},
        ):
            with self.subTest(config=kwargs):
                with self.assertRaises(SvgImportError) as caught:
                    convert('<rect width="10" height="10" fill="black"/>', **kwargs)
                self.assertEqual(caught.exception.code, "SI_E_CONFIG")


class PaletteCase(unittest.TestCase):
    def test_palette_ranks_by_ink_weight_not_document_order(self):
        body = (
            '<rect width="5" height="5" fill="#00ff00"/>'
            '<rect x="10" width="5" height="5" fill="#0000ff"/>'
            '<rect x="30" y="30" width="100" height="100" fill="#ff0000"/>'
        )
        plan, warnings, stats = convert(body, max_colors=1)
        self.assertIn("SI_W_COLOR_QUANTIZED", codes(warnings))
        self.assertEqual(plan["palette"], {"c0": "#ff0000"})
        fills = elements(plan, "fill")
        self.assertEqual([element_rgb(plan, element) for element in fills], [(255, 0, 0)] * 3)
        self.assertEqual(stats["quantized_colors"], 2)

    def test_max_colors_64_is_fully_supported(self):
        body = "".join(
            f'<rect x="{index * 3}" width="2" height="2" '
            f'fill="#{index * 4:02x}{index * 4:02x}{index * 4:02x}"/>'
            for index in range(64)
        )
        plan64, warnings64, stats64 = convert(body, max_colors=64)
        self.assertEqual(codes(warnings64), [])
        self.assertEqual(len(plan64["palette"]), 64)
        self.assertEqual(stats64["colors"], 64)
        self.assertEqual(stats64["quantized_colors"], 0)
        plan32, warnings32, stats32 = convert(body, max_colors=32)
        self.assertIn("SI_W_COLOR_QUANTIZED", codes(warnings32))
        self.assertEqual(len(plan32["palette"]), 32)
        self.assertEqual(stats32["quantized_colors"], 32)


class StrokeModeCase(unittest.TestCase):
    def test_stroke_to_fill_converts_only_thin_strokes(self):
        body = (
            '<path d="M 20 20 L 180 20" fill="none" stroke="#cc0000" stroke-width="1.5"/>'
            '<path d="M 20 100 L 180 100" fill="none" stroke="#0000cc" stroke-width="6"/>'
        )
        plan, _warnings, stats = convert(
            body, stroke_to_fill=True, stroke_to_fill_max_width=2.0
        )
        kinds = [element["kind"] for element in plan["elements"]]
        self.assertEqual(kinds, ["fill", "stroke"])
        self.assertEqual(stats["strokes_to_fill"], 1)
        ribbon = elements(plan, "fill")[0]["poly"]
        self.assertEqual(len(ribbon), 4)
        area = abs(
            sum(
                ribbon[index][0] * ribbon[(index + 1) % 4][1]
                - ribbon[(index + 1) % 4][0] * ribbon[index][1]
                for index in range(4)
            )
            / 2.0
        )
        self.assertAlmostEqual(area, 160.0 * 1.5, delta=8.0)

    def test_stroke_to_fill_closed_ring_uses_keyhole_polygon(self):
        body = (
            '<circle cx="100" cy="100" r="40" fill="none" '
            'stroke="#dd2222" stroke-width="2"/>'
        )
        plan, _warnings, stats = convert(body, stroke_to_fill=True)
        fill = elements(plan, "fill")[0]
        self.assertNotIn("holes", fill)
        self.assertGreater(len(fill["poly"]), 8)
        self.assertEqual(stats["strokes"], 0)
        self.assertEqual(stats["strokes_to_fill"], 1)
        self.assertEqual(validate_plan(plan), [])

    def test_no_stroke_switch_drops_all_strokes(self):
        body = (
            '<rect width="50" height="50" fill="red" stroke="black" stroke-width="2"/>'
        )
        plan, _warnings, stats = convert(body, include_stroke=False)
        self.assertEqual([element["kind"] for element in plan["elements"]], ["fill"])
        self.assertEqual(stats["strokes"], 0)


class OverflowCase(unittest.TestCase):
    BODY = (
        '<rect x="10" y="10" width="15" height="15" fill="#ff0000"/>'
        '<rect x="30" y="10" width="15" height="15" fill="#ff0000"/>'
        '<rect x="50" y="10" width="15" height="15" fill="#ff0000"/>'
        '<rect x="70" y="10" width="15" height="15" fill="#ff0000"/>'
        '<rect x="10" y="100" width="20" height="20" fill="#00ff00"/>'
    )

    def test_area_keeps_largest_individual_path(self):
        plan, warnings, stats = convert(self.BODY, max_paths=2, overflow="area")
        self.assertEqual(stats["paths_dropped"], 3)
        self.assertIn("SI_W_PATH_LIMIT", codes(warnings))
        colours = [element_rgb(plan, element) for element in elements(plan, "fill")]
        self.assertIn((0, 255, 0), colours)
        self.assertIn((255, 0, 0), colours)

    def test_region_keeps_whole_dominant_colour_group(self):
        plan, warnings, stats = convert(self.BODY, max_paths=2, overflow="region")
        self.assertEqual(stats["paths_dropped"], 3)
        message = [item["message"] for item in warnings if item["code"] == "SI_W_PATH_LIMIT"]
        self.assertTrue(message and "same-colour" in message[0])
        colours = [element_rgb(plan, element) for element in elements(plan, "fill")]
        self.assertEqual(colours, [(255, 0, 0), (255, 0, 0)])
        self.assertEqual(plan["meta"]["svg_import"]["overflow"], "region")


class CliQualityCase(unittest.TestCase):
    def test_cli_quality_flags_reach_plan_metadata(self):
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
            code = main(
                [
                    "--svg", str(svg_path),
                    "--out", str(plan_path),
                    "--no-stroke",
                    "--max-colors", "64",
                    "--fill-dilate", "0.5",
                    "--overflow", "region",
                    "--quiet",
                ]
            )
            self.assertEqual(code, 0)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        meta = plan["meta"]["svg_import"]
        self.assertEqual(meta["fill_dilate_px"], 0.5)
        self.assertEqual(meta["overflow"], "region")
        self.assertEqual(meta["max_colors"], 64)
        self.assertEqual(meta["stroke_to_fill"], False)
        self.assertEqual([element["kind"] for element in plan["elements"]], ["fill"])
        self.assertEqual(validate_plan(plan), [])


if __name__ == "__main__":
    unittest.main()
