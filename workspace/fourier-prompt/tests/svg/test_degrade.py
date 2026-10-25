"""Explicit degradation paths: gradients, patterns, filters, text, CSS, display."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _fixtures import codes, convert, element_rgb, elements  # noqa: E402

from fourier_prompt.svg_import import SvgImportError, svg_to_fdsl  # noqa: E402


class GradientPatternCase(unittest.TestCase):
    def test_linear_gradient_average_stop_colour(self):
        plan, warnings, stats = convert(
            head='<defs><linearGradient id="g">'
            '<stop offset="0" stop-color="#ff0000"/>'
            '<stop offset="1" stop-color="#0000ff"/>'
            "</linearGradient></defs>",
            body='<rect width="100" height="100" fill="url(#g)"/>',
        )
        self.assertIn("SI_W_GRADIENT_APPROX", codes(warnings))
        self.assertEqual(element_rgb(plan, elements(plan, "fill")[0]), (128, 0, 128))
        self.assertEqual(stats["warning_counts"]["SI_W_GRADIENT_APPROX"], 1)

    def test_gradient_href_inherits_stops(self):
        plan, _warnings, _stats = convert(
            head='<defs><linearGradient id="base">'
            '<stop offset="0" stop-color="#ffffff"/>'
            '<stop offset="1" stop-color="#000000"/>'
            '</linearGradient><linearGradient id="alias" href="#base"/></defs>',
            body='<rect width="10" height="10" fill="url(#alias)"/>',
        )
        self.assertEqual(element_rgb(plan, elements(plan, "fill")[0]), (128, 128, 128))

    def test_pattern_average_colour(self):
        plan, warnings, _stats = convert(
            head='<defs><pattern id="p" width="10" height="10" '
            'patternUnits="userSpaceOnUse"><rect width="10" height="10" '
            'fill="#00ff00"/></pattern></defs>',
            body='<rect width="100" height="100" fill="url(#p)"/>',
        )
        self.assertIn("SI_W_PATTERN_APPROX", codes(warnings))
        self.assertEqual(element_rgb(plan, elements(plan, "fill")[0]), (0, 255, 0))

    def test_unknown_paint_reference_warns(self):
        plan, warnings, _stats = convert(
            '<rect width="10" height="10" fill="url(#missing)"/>'
        )
        self.assertIn("SI_W_PAINT_FALLBACK", codes(warnings))
        self.assertEqual(element_rgb(plan, elements(plan, "fill")[0]), (0, 0, 0))


class EffectsSkippedCase(unittest.TestCase):
    def test_filter_clip_mask_and_dash_warn_but_keep_geometry(self):
        plan, warnings, _stats = convert(
            head="<defs><filter id=\"f\"/><clipPath id=\"c\"/><mask id=\"m\"/></defs>",
            body='<rect width="50" height="50" fill="black" filter="url(#f)" '
            'clip-path="url(#c)" mask="url(#m)" stroke="black" stroke-width="2"/>',
        )
        found = codes(warnings)
        for code in (
            "SI_W_FILTER_IGNORED",
            "SI_W_CLIP_IGNORED",
            "SI_W_MASK_IGNORED",
        ):
            self.assertIn(code, found)
        self.assertEqual(len(elements(plan)), 2)

    def test_dasharray_warns(self):
        _plan, warnings, _stats = convert(
            '<path d="M 10 10 L 90 90" stroke="black" stroke-width="2" '
            'fill="none" stroke-dasharray="4 2"/>'
        )
        self.assertIn("SI_W_DASH_IGNORED", codes(warnings))

    def test_text_image_use_and_foreignobject_are_skipped(self):
        plan, warnings, _stats = convert(
            '<text x="10" y="20">hello</text>'
            '<image x="0" y="0" width="10" height="10" href="x.png"/>'
            '<use href="#missing"/>'
            "<foreignObject></foreignObject>"
            '<rect width="20" height="20" fill="black"/>'
        )
        found = codes(warnings)
        for code in (
            "SI_W_TEXT_SKIPPED",
            "SI_W_IMAGE_SKIPPED",
            "SI_W_USE_SKIPPED",
            "SI_W_FOREIGNOBJECT_SKIPPED",
        ):
            self.assertIn(code, found)
        self.assertEqual(len(elements(plan)), 1)

    def test_display_none_and_visibility_hidden(self):
        plan, _warnings, _stats = convert(
            '<g display="none"><rect width="100" height="100" fill="red"/></g>'
            '<rect x="10" y="10" width="20" height="20" fill="blue" visibility="hidden"/>'
            '<rect x="40" y="40" width="20" height="20" fill="green"/>'
        )
        self.assertEqual(len(elements(plan)), 1)
        self.assertEqual(element_rgb(plan, elements(plan)[0]), (0, 128, 0))

    def test_unknown_element_warns(self):
        plan, warnings, _stats = convert(
            '<foobar width="10" height="10"/><rect width="10" height="10" fill="black"/>'
        )
        self.assertIn("SI_W_UNSUPPORTED_ELEMENT", codes(warnings))
        self.assertEqual(len(elements(plan)), 1)


class CssCase(unittest.TestCase):
    def test_class_rule_applies_and_beats_presentation_attribute(self):
        plan, _warnings, _stats = convert(
            head='<style>.hot { fill: #00ff00; }</style>',
            body='<rect class="hot" width="10" height="10" fill="#ff0000"/>',
        )
        self.assertEqual(element_rgb(plan, elements(plan, "fill")[0]), (0, 255, 0))

    def test_inline_style_beats_class(self):
        plan, _warnings, _stats = convert(
            head='<style>.hot { fill: #00ff00; }</style>',
            body='<rect class="hot" style="fill:#0000ff" width="10" height="10"/>',
        )
        self.assertEqual(element_rgb(plan, elements(plan, "fill")[0]), (0, 0, 255))

    def test_id_and_tag_selectors(self):
        plan, _warnings, _stats = convert(
            head="<style>rect { fill: #111111; } #special { fill: #222222; }</style>",
            body='<rect width="10" height="10"/>'
            '<rect id="special" x="20" width="10" height="10"/>',
        )
        fills = elements(plan, "fill")
        self.assertEqual(element_rgb(plan, fills[0]), (0x11, 0x11, 0x11))
        self.assertEqual(element_rgb(plan, fills[1]), (0x22, 0x22, 0x22))

    def test_unknown_class_does_not_break_parsing(self):
        body = '<rect class="nope" width="10" height="10" fill="black"/>'
        head = "<style>.other { fill: red; } @media screen { rect { fill: blue; } }</style>"
        plan, warnings, _stats = convert(head=head, body=body)
        self.assertEqual(len(elements(plan)), 1)
        self.assertEqual(codes(warnings), [])


class EmptyDocumentCase(unittest.TestCase):
    def test_empty_svg_raises(self):
        with self.assertRaises(SvgImportError) as caught:
            svg_to_fdsl(
                '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"></svg>',
                canvas=(100, 100),
            )
        self.assertEqual(caught.exception.code, "SI_E_EMPTY")

    def test_invalid_xml_raises(self):
        with self.assertRaises(SvgImportError) as caught:
            svg_to_fdsl("<svg><rect></svg>", canvas=(100, 100))
        self.assertEqual(caught.exception.code, "SI_E_PARSE")

    def test_non_svg_root_raises(self):
        with self.assertRaises(SvgImportError) as caught:
            svg_to_fdsl("<html></html>", canvas=(100, 100))
        self.assertEqual(caught.exception.code, "SI_E_NOT_SVG")


if __name__ == "__main__":
    unittest.main()
