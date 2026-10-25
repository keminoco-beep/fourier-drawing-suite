"""PSD structure / Pillow interop / determinism tests (WP-I)."""

from __future__ import annotations

import copy
import hashlib
import io
import struct
import sys
import unittest

from _fixtures import (
    PACKAGE_ROOT,
    clone,
    layer_pixels,
    prepare_01,
    psd_plane,
    small_doc,
    tempdir,
)

if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from fourier_prompt import layers, psd  # noqa: E402


def write_blob(doc, resolution=32, quality="fast", layer_images=None):
    return psd.write_psd(
        doc,
        out=None,
        resolution=resolution,
        quality=quality,
        layer_images=layer_images,
    )


class PsdStructureCase(unittest.TestCase):
    def test_psd_opens_with_pillow(self):
        from PIL import Image

        doc = small_doc()
        size = layers.canvas_size(doc, 32)
        result = write_blob(doc)
        self.assertGreater(result["bytes"], 0)
        self.assertEqual(result["layer_count"], 3)  # paper + 2 doc layers
        with Image.open(io.BytesIO(result["blob"])) as image:
            self.assertEqual(image.format, "PSD")
            self.assertEqual(image.size, size)
            self.assertEqual(image.mode, "RGB")
            self.assertEqual(image.n_frames, 3)
            self.assertEqual([layer[0] for layer in image.layers], ["paper", "base", "ink"])
            composite = image.convert("RGB")
            self.assertEqual(composite.size, size)

    def test_psd_header_and_layer_records(self):
        doc = small_doc()
        width, height = layers.canvas_size(doc, 32)
        result = write_blob(doc)
        info = psd.parse_psd_layers(result["blob"])
        self.assertEqual(result["blob"][:4], b"8BPS")
        self.assertEqual(info["version"], 1)
        self.assertEqual(info["channels"], 3)
        self.assertEqual(info["depth"], 8)
        self.assertEqual(info["color_mode"], 3)
        self.assertEqual((info["width"], info["height"]), (width, height))
        self.assertEqual(info["layer_count"], 3)
        self.assertEqual([layer["name"] for layer in info["layers"]], ["paper", "base", "ink"])
        expected = 2 + info["width"] * info["height"]
        for layer in info["layers"]:
            self.assertEqual([channel["length"] for channel in layer["channel_lengths"]],
                             [expected] * 4)
            self.assertEqual(layer["blend_key"], "norm")
            self.assertEqual(layer["opacity"], 255)
            self.assertEqual(layer["hidden"], False)
        self.assertLess(info["channel_data_end"], info["file_size"])

    def test_psd_hidden_layer_flag(self):
        doc = small_doc(
            layers_spec=[
                {"id": 0, "name": "base"},
                {"id": 1, "name": "ink", "visible": False},
            ]
        )
        result = write_blob(doc)
        info = psd.parse_psd_layers(result["blob"])
        self.assertFalse(info["layers"][1]["hidden"])
        self.assertTrue(info["layers"][2]["hidden"])
        self.assertEqual(info["layers"][2]["flags"] & 2, 2)
        self.assertTrue(result["layers"][2]["opacity_baked"] is False)

    def test_psd_unicode_luni_name(self):
        doc = small_doc(
            layers_spec=[
                {"id": 0, "name": "底色"},
                {"id": 1, "name": "墨线"},
            ]
        )
        result = write_blob(doc)
        info = psd.parse_psd_layers(result["blob"])
        names = [layer["name"] for layer in info["layers"]]
        self.assertEqual(names, ["paper", "底色", "墨线"])
        self.assertEqual(info["layers"][2]["luni_name"], "墨线")
        self.assertTrue(info["layers"][2]["ascii_name"].isascii())
        codes = [warning["code"] for warning in result["warnings"]]
        self.assertIn("W_PSD_NAME_ASCII", codes)

    def test_psd_luni_failure_falls_back_to_ascii(self):
        doc = small_doc()
        doc = clone(doc)
        doc["layers"][1]["name"] = "\ud800broken"
        result = psd.write_psd(doc, out=None, resolution=32, quality="fast")
        info = psd.parse_psd_layers(result["blob"])
        self.assertIsNone(info["layers"][2]["luni_name"])
        self.assertTrue(info["layers"][2]["ascii_name"].isascii())
        codes = [warning["code"] for warning in result["warnings"]]
        self.assertIn("W_PSD_LUNI_FALLBACK", codes)

    def test_psd_channel_pixels_match_layer_renders(self):
        doc = small_doc()
        images = layers.render_layers(doc, resolution=32, quality="fast")
        result = psd.write_psd(doc, out=None, resolution=32, quality="fast", layer_images=images)
        blob = result["blob"]
        info = psd.parse_psd_layers(blob)
        width, height = info["width"], info["height"]
        for record, image in enumerate(images, start=1):
            for channel in range(4):
                plane = psd_plane(blob, info, record, channel)
                self.assertEqual(
                    plane,
                    image["rgba"][..., channel].tobytes(order="C"),
                    f"layer {image['name']} channel {channel}",
                )
        paper_info = info["layers"][0]
        for channel, rgb in enumerate(layers._base_rgb(doc)):
            plane = psd_plane(blob, info, 0, channel)
            self.assertEqual(plane, bytes([rgb]) * (width * height))
            self.assertEqual(paper_info["channel_data"][channel]["id"], channel)
        alpha = psd_plane(blob, info, 0, 3)
        self.assertEqual(alpha, b"\xff" * (width * height))

    def test_psd_composite_matches_renderer(self):
        from PIL import Image

        from fourier_prompt import render_fast

        doc = small_doc()
        result = write_blob(doc)
        expected = render_fast.render_auto(
            doc, resolution=32, quality="fast", background=layers._base_rgb(doc)
        )
        with Image.open(io.BytesIO(result["blob"])) as image:
            got = image.convert("RGB")
        import numpy as np

        diff = np.abs(np.asarray(got).astype(int) - expected.astype(int))
        self.assertEqual(int(diff.max()), 0)


class PsdDeterminismCase(unittest.TestCase):
    def test_psd_bytes_are_deterministic(self):
        doc = small_doc()
        with tempdir() as directory:
            first = psd.write_psd(doc, out=str(directory / "a.psd"), resolution=32, quality="fast")
            second = psd.write_psd(doc, out=str(directory / "b.psd"), resolution=32, quality="fast")
            self.assertEqual(first["sha256"], second["sha256"])
            self.assertEqual(
                hashlib.sha256((directory / "a.psd").read_bytes()).hexdigest(),
                hashlib.sha256((directory / "b.psd").read_bytes()).hexdigest(),
            )

    def test_psd_01_layers_match_document(self):
        doc = prepare_01()
        result = psd.write_psd(doc, out=None, resolution=32, quality="fast")
        info = psd.parse_psd_layers(result["blob"])
        doc_names = [layer["name"] for layer in layers.doc_layers(doc)]
        self.assertEqual(info["layer_count"], len(doc_names) + 1)
        self.assertEqual([layer["name"] for layer in info["layers"][1:]], doc_names)
        self.assertEqual([layer["hidden"] for layer in info["layers"][1:]],
                         [not layer["visible"] for layer in layers.doc_layers(doc)])


if __name__ == "__main__":
    unittest.main()
