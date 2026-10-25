"""Render smoke test: the unpacked ``render.json`` renders identical pixels.

Read-only use of the skill renderer (``fourier-brushstroke/scripts``), R=128.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from _fixtures import SKILL_DIR, load_sample

from fourier_prompt import fdraw

SCRIPTS = str(SKILL_DIR / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import numpy as np  # noqa: E402

from fbs_core import io_format  # noqa: E402
from fbs_core.render import render, render_to_image  # noqa: E402


class RenderCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_unpacked_render_json_matches_original_pixels(self):
        for name in ("signature", "icon"):
            with self.subTest(sample=name):
                doc = load_sample(name)
                original = render(doc, resolution=128, supersample=2, width_scale=1.0)
                blob = fdraw.pack(doc)
                unpacked = fdraw.unpack(blob)
                self.assertEqual(io_format.validate(unpacked["render"]), [])
                restored = render(
                    unpacked["render"], resolution=128, supersample=2, width_scale=1.0
                )
                np.testing.assert_array_equal(original, restored)

    def test_unpacked_document_renders_png(self):
        doc = load_sample("signature")
        unpacked = fdraw.unpack(fdraw.pack(doc))
        image = render_to_image(
            unpacked["render"], resolution=128, supersample=2, width_scale=1.0
        )
        path = self.tmp / "render.png"
        image.save(path)
        self.assertGreater(path.stat().st_size, 0)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(8), b"\x89PNG\r\n\x1a\n")


if __name__ == "__main__":
    unittest.main()
