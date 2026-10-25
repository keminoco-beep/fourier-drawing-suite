"""Determinism: identical input -> byte-identical plan, stable palette/source meta."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _fixtures import convert, svg  # noqa: E402

from fourier_prompt.svg_import import svg_to_fdsl  # noqa: E402

BODY = (
    '<rect x="0" y="0" width="80" height="80" fill="#aa0000"/>'
    '<path d="M 100 10 C 130 40 170 40 190 10" fill="none" '
    'stroke="#000000" stroke-width="2"/>'
)


def _dump(plan):
    return json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class DeterminismCase(unittest.TestCase):
    def test_two_conversions_are_identical(self):
        first_plan, first_warnings, _ = convert(BODY)
        second_plan, second_warnings, _ = convert(BODY)
        self.assertEqual(_dump(first_plan), _dump(second_plan))
        self.assertEqual(
            [warning["code"] for warning in first_warnings],
            [warning["code"] for warning in second_warnings],
        )

    def test_source_metadata_uses_basename_and_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.svg"
            text = svg(BODY)
            path.write_text(text, encoding="utf-8")
            plan, _warnings, _stats = svg_to_fdsl(path, canvas=(200, 200))
        source = plan["meta"]["source"]
        self.assertEqual(source["name"], "sample.svg")
        self.assertEqual(source["format"], "svg")
        self.assertEqual(source["bytes"], len(text.encode("utf-8")))
        self.assertEqual(
            source["sha256"], hashlib.sha256(text.encode("utf-8")).hexdigest()
        )
        self.assertEqual(plan["name"], "sample")
        self.assertEqual(source["viewBox"], [0.0, 0.0, 200.0, 200.0])

    def test_palette_order_follows_document_order(self):
        plan, _warnings, _stats = convert(
            '<rect width="10" height="10" fill="#00ff00"/>'
            '<rect x="20" width="10" height="10" fill="#ff0000"/>'
        )
        self.assertEqual(
            [plan["palette"][name] for name in ("c0", "c1")],
            ["#00ff00", "#ff0000"],
        )


if __name__ == "__main__":
    unittest.main()
