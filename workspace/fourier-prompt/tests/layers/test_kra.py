"""KRA ZIP / XML / tile / determinism tests (WP-I)."""

from __future__ import annotations

import hashlib
import io
import sys
import unittest
import zipfile
import xml.etree.ElementTree as ET

from _fixtures import (
    PACKAGE_ROOT,
    kra_layer_arrays,
    parse_kra_tiles,
    prepare_01,
    prepare_06,
    small_doc,
    tempdir,
)

if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from fourier_prompt import kra, layers  # noqa: E402
from fourier_prompt import render_fast  # noqa: E402

NS = {"k": "http://www.calligra.org/DTD/krita"}


def build(doc, resolution=32, quality="fast", layer_images=None):
    return kra.write_kra(
        doc,
        out=None,
        resolution=resolution,
        quality=quality,
        layer_images=layer_images,
    )


class KraLayoutCase(unittest.TestCase):
    def test_kra_zip_layout(self):
        doc = small_doc()
        result = build(doc)
        with zipfile.ZipFile(io.BytesIO(result["blob"])) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            self.assertEqual(names[0], "mimetype")
            self.assertEqual(infos[0].compress_type, zipfile.ZIP_STORED)
            self.assertEqual(archive.read("mimetype"), b"application/x-krita")
            for required in (
                "maindoc.xml",
                "documentinfo.xml",
                "preview.png",
                "mergedimage.png",
                f"{kra.DOC_NAME}/layers/layer0",
                f"{kra.DOC_NAME}/layers/layer1",
                f"{kra.DOC_NAME}/layers/layer2",
            ):
                self.assertIn(required, names)
            self.assertTrue(names[0] == "mimetype" and names.count("mimetype") == 1)
            # maindoc first *after* mimetype is not required, but all PNGs must decode
            from PIL import Image

            with Image.open(io.BytesIO(archive.read("mergedimage.png"))) as merged:
                self.assertEqual(merged.size, layers.canvas_size(doc, 32))
                self.assertEqual(merged.mode, "RGB")
            with Image.open(io.BytesIO(archive.read("preview.png"))) as preview:
                self.assertLessEqual(max(preview.size), 256)

    def test_kra_maindoc_top_first_and_utf8_names(self):
        doc = small_doc(
            layers_spec=[
                {"id": 0, "name": "底色"},
                {"id": 1, "name": "墨线", "visible": False},
            ]
        )
        result = build(doc)
        with zipfile.ZipFile(io.BytesIO(result["blob"])) as archive:
            root = ET.fromstring(archive.read("maindoc.xml"))
        image = root.find("k:IMAGE", NS)
        width, height = layers.canvas_size(doc, 32)
        self.assertEqual(image.get("width"), str(width))
        self.assertEqual(image.get("height"), str(height))
        records = root.findall(".//k:layer", NS)
        self.assertEqual([record.get("filename") for record in records],
                         ["layer2", "layer1", "layer0"])
        self.assertEqual([record.get("name") for record in records],
                         ["墨线", "底色", "paper"])
        self.assertEqual(records[0].get("visible"), "0")
        self.assertEqual(records[1].get("visible"), "1")
        self.assertEqual(records[0].get("opacity"), "255")
        # TOP-first: the last document layer is the first XML record
        self.assertEqual(records[0].get("selected"), "true")

    def test_kra_tile_stream_roundtrip(self):
        doc = small_doc()
        images = layers.render_layers(doc, resolution=32, quality="fast")
        result = kra.write_kra(doc, out=None, resolution=32, quality="fast", layer_images=images)
        width, height = layers.canvas_size(doc, 32)
        names, streams = kra_layer_arrays(result["blob"], width, height)
        self.assertEqual(sorted(streams), [f"layer{i}" for i in range(3)])
        paper = streams["layer0"]
        base = layers._base_rgb(doc)
        self.assertEqual(paper[..., 0].min(), base[0])
        self.assertEqual(paper[..., 3].min(), 255)
        for index, image in enumerate(images, start=1):
            got = streams[f"layer{index}"]
            import numpy as np

            diff = np.abs(got.astype(int) - image["rgba"].astype(int))
            self.assertEqual(int(diff.max()), 0, f"layer {image['name']}")
        # header fields inside the stream
        with zipfile.ZipFile(io.BytesIO(result["blob"])) as archive:
            data = archive.read(f"{kra.DOC_NAME}/layers/layer1")
        array, header = parse_kra_tiles(data, width, height)
        self.assertEqual(header["TILEWIDTH"], 64)
        self.assertEqual(header["PIXELSIZE"], 4)
        self.assertLessEqual(width, 64)
        self.assertLessEqual(height, 64)
        self.assertEqual(header["DATA"], 1)  # fits in one 64x64 tile

    def test_kra_multitile_edge_padding(self):
        doc = small_doc()
        images = layers.render_layers(doc, resolution=192, quality="fast")
        result = kra.write_kra(doc, out=None, resolution=192, quality="fast", layer_images=images)
        self.assertEqual((images[0]["width"], images[0]["height"]), (192, 128))
        with zipfile.ZipFile(io.BytesIO(result["blob"])) as archive:
            data = archive.read(f"{kra.DOC_NAME}/layers/layer1")
        array, header = parse_kra_tiles(data, 192, 128)
        self.assertEqual(header["DATA"], 6)  # 3 columns x 2 rows
        import numpy as np

        self.assertEqual(int(np.abs(array.astype(int) - images[0]["rgba"].astype(int)).max()), 0)

    def test_kra_merged_matches_renderer(self):
        from PIL import Image
        import numpy as np

        doc = small_doc()
        result = build(doc)
        expected = render_fast.render_auto(
            doc, resolution=32, quality="fast", background=layers._base_rgb(doc)
        )
        with zipfile.ZipFile(io.BytesIO(result["blob"])) as archive:
            got = np.asarray(Image.open(io.BytesIO(archive.read("mergedimage.png"))).convert("RGB"))
        self.assertEqual(int(np.abs(got.astype(int) - expected.astype(int)).max()), 0)


class KraDeterminismCase(unittest.TestCase):
    def test_kra_bytes_are_deterministic(self):
        doc = small_doc()
        with tempdir() as directory:
            first = kra.write_kra(doc, out=str(directory / "a.kra"), resolution=32, quality="fast")
            second = kra.write_kra(doc, out=str(directory / "b.kra"), resolution=32, quality="fast")
            self.assertEqual(first["sha256"], second["sha256"])
            self.assertEqual(
                hashlib.sha256((directory / "a.kra").read_bytes()).hexdigest(),
                hashlib.sha256((directory / "b.kra").read_bytes()).hexdigest(),
            )

    def test_kra_01_layer_count_and_names(self):
        doc = prepare_01()
        result = build(doc)
        doc_names = [layer["name"] for layer in layers.doc_layers(doc)]
        with zipfile.ZipFile(io.BytesIO(result["blob"])) as archive:
            root = ET.fromstring(archive.read("maindoc.xml"))
            names = [record.get("name") for record in root.findall(".//k:layer", NS)]
        self.assertEqual(names, list(reversed(["paper"] + doc_names)))
        self.assertEqual(result["layer_count"], len(doc_names) + 1)

    def test_kra_06_pencil_layer_falls_back_exact(self):
        doc = prepare_06()
        images = layers.render_layers(doc, resolution=32, quality="fast")
        pencil = next(image for image in images if image["index"] == 5)
        self.assertEqual(pencil["quality"], "exact")
        self.assertTrue(pencil["fallback_reason"])
        result = build(doc, resolution=32, quality="fast", layer_images=images)
        self.assertEqual(result["layer_count"], len(images) + 1)
        self.assertTrue(any(item["index"] == 5 for item in result["fallbacks"]))


if __name__ == "__main__":
    unittest.main()
