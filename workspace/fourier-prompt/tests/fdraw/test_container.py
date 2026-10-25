"""Container behaviour: roundtrip, determinism, inspect, metadata, fonts."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from _fixtures import (
    PKG_ROOT,
    ZIP_DATE,
    canonical,
    container_bytes,
    error_code,
    load_sample,
    meta_for,
    sha256,
)

from fourier_prompt import fdraw


class PackUnpackCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    # -- roundtrip ----------------------------------------------------------
    def test_roundtrip_signature_render_equals_original(self):
        doc = load_sample("signature")
        blob = fdraw.pack(doc)
        unpacked = fdraw.unpack(blob)
        self.assertEqual(canonical(unpacked["render"]), canonical(doc))
        self.assertEqual(unpacked["meta"]["schema"], fdraw.FDRAW_VERSION)
        self.assertEqual(unpacked["root_sha256"], sha256(canonical(doc)))
        self.assertTrue(unpacked["root_sha256_ok"])
        self.assertIsNone(unpacked["source_fdsl"])
        self.assertEqual(unpacked["capabilities"]["editing"], "restricted")

    def test_roundtrip_icon_with_fills(self):
        doc = load_sample("icon")
        blob = fdraw.pack(doc, source_fdsl={"schema": "fdsl/2.1", "elements": []})
        unpacked = fdraw.unpack(blob)
        self.assertEqual(canonical(unpacked["render"]), canonical(doc))
        meta = unpacked["meta"]
        self.assertEqual(meta["counts"]["fills"], 4)
        self.assertEqual(meta["counts"]["strokes"], 3)
        self.assertEqual(meta["counts"]["elements"], 7)
        self.assertIn("fill/polygon", meta["capabilities"]["required"])
        self.assertEqual(meta["capabilities"]["source"], "fdsl/2.1")
        self.assertEqual(meta["capabilities"]["editing"], "full")

    def test_empty_strokes_solid_color_still_packs(self):
        doc = load_sample("solid")
        blob = fdraw.pack(doc)
        info = fdraw.inspect(blob)
        self.assertEqual(info["render"]["element_count"], 0)
        self.assertEqual(fdraw.validate(blob), [])

    def test_pack_wpd_compiled_output_when_present(self):
        """DoD: at least one container built from a WP-D compiler product."""
        path = PKG_ROOT / "out" / "01_rose.fbs"
        if not path.is_file():
            self.skipTest("WP-D compile output not available (out/01_rose.fbs)")
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        blob = fdraw.pack(doc)
        unpacked = fdraw.unpack(blob)
        self.assertEqual(canonical(unpacked["render"]), canonical(doc))
        info = fdraw.inspect(blob)
        self.assertGreater(info["render"]["element_count"], 0)
        self.assertGreaterEqual(info["render"]["layer_count"], 1)
        self.assertEqual(fdraw.validate(blob), [])

    def test_pack_is_byte_identical_twice(self):
        doc = load_sample("signature")
        kwargs = {
            "source_fdsl": {"schema": "fdsl/2.1", "elements": [{"id": "e1"}]},
            "fonts": [("demo.ttf", b"\x00\x01\x00\x00font")],
            "meta": {"title": "deterministic", "created": "2026-10-20T00:00:00Z"},
        }
        first = fdraw.pack(doc, **kwargs)
        second = fdraw.pack(doc, **kwargs)
        self.assertEqual(first, second)

    def test_repack_after_unpack_is_byte_identical(self):
        doc = load_sample("signature")
        blob = fdraw.pack(
            doc,
            source_fdsl={"schema": "fdsl/2.1", "canvas": {"w": 512, "h": 512}},
            fonts=[("meta.txt", b"license placeholder\n")],
        )
        unpacked = fdraw.unpack(blob)
        repacked = fdraw.pack(
            unpacked["render"],
            source_fdsl=unpacked["source_fdsl"],
            meta=unpacked["meta"],
            fonts=list(unpacked["fonts"].items()),
        )
        self.assertEqual(blob, repacked)

    # -- ZIP / hashes -------------------------------------------------------
    def test_zip_layout_fixed_order_and_timestamp(self):
        blob = fdraw.pack(
            load_sample("signature"),
            source_fdsl={"schema": "fdsl/2.1"},
            fonts=[("z.ttf", b"zzz"), ("a.ttf", b"aaa")],
        )
        with zipfile.ZipFile(__import__("io").BytesIO(blob), "r") as zf:
            infos = zf.infolist()
            self.assertEqual(
                [info.filename for info in infos],
                [
                    "meta.json",
                    "render.json",
                    "source.fdsl.json",
                    "fonts/a.ttf",
                    "fonts/z.ttf",
                ],
            )
            for info in infos:
                self.assertEqual(info.date_time, ZIP_DATE)
                self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED)
                self.assertEqual(info.create_system, 3)
                self.assertEqual((info.external_attr >> 16) & 0xFFFF, 0o100644)
                self.assertEqual(info.flag_bits & 0x1, 0)

    def test_root_sha256_is_render_canonical_bytes(self):
        doc = load_sample("icon")
        blob = fdraw.pack(doc)
        with zipfile.ZipFile(__import__("io").BytesIO(blob), "r") as zf:
            meta = json.loads(zf.read("meta.json").decode("utf-8"))
        expected = sha256(canonical(doc))
        self.assertEqual(meta["root_sha256"], expected)
        self.assertEqual(meta["integrity"]["root_sha256"], expected)
        self.assertEqual(meta["integrity"]["root_scope"], "render.json")
        info = fdraw.inspect(blob)
        self.assertEqual(info["root_sha256"], expected)
        self.assertEqual(info["root_sha256_recorded"], expected)
        self.assertTrue(info["root_sha256_ok"])
        self.assertTrue(info["entries_root_sha256_ok"])
        self.assertTrue(info["manifest_sha256_ok"])

    # -- inspect ------------------------------------------------------------
    def test_inspect_reports_structure_and_sizes(self):
        blob = fdraw.pack(load_sample("icon"))
        info = fdraw.inspect(blob)
        self.assertEqual(info["schema"], fdraw.FDRAW_VERSION)
        self.assertEqual(info["spec_version"], fdraw.FDRAW_SPEC_VERSION)
        self.assertEqual(info["zip_bytes"], len(blob))
        self.assertEqual(info["entry_count"], 2)
        self.assertEqual(info["render"]["format"], "fourier-brushstroke")
        self.assertEqual(info["render"]["version"], "2.0")
        self.assertEqual(info["render"]["mode"], "per_stroke")
        self.assertEqual(info["render"]["element_count"], 7)
        self.assertEqual(info["render"]["stroke_count"], 3)
        self.assertEqual(info["render"]["fill_count"], 4)
        self.assertEqual(info["render"]["layer_count"], 2)
        self.assertEqual(info["render"]["layers"], [0, 1])
        self.assertEqual(info["render"]["brush_style_count"], 7)
        self.assertEqual(info["render"]["canvas"]["width"], 1.0)
        self.assertEqual(info["render"]["target_resolution"], 512)
        self.assertFalse(info["render"]["width_scale_pinned"])
        self.assertIsNone(info["render"]["width_scale"])
        self.assertEqual(info["font_count"], 0)
        self.assertFalse(info["has_source"])
        self.assertEqual(info["counts"]["elements"], 7)
        self.assertGreater(info["uncompressed_bytes"], info["zip_bytes"])
        self.assertEqual(
            [entry["path"] for entry in info["entries"]],
            ["meta.json", "render.json"],
        )

    # -- source / fonts / meta ---------------------------------------------
    def test_source_roundtrip_and_capabilities(self):
        source = {
            "schema": "fdsl/2.1",
            "canvas": {"w": 512, "h": 512},
            "elements": [{"id": "e1", "kind": "stroke"}],
        }
        blob = fdraw.pack(load_sample("signature"), source_fdsl=source)
        unpacked = fdraw.unpack(blob)
        self.assertEqual(unpacked["source_fdsl"], source)
        meta = unpacked["meta"]
        self.assertEqual(meta["capabilities"]["source"], "fdsl/2.1")
        self.assertEqual(meta["capabilities"]["editing"], "full")
        self.assertIn("source/fdsl-2", meta["capabilities"]["optional"])
        self.assertEqual(meta["sizes"]["source_bytes"], len(canonical(source)))
        info = fdraw.inspect(blob)
        self.assertTrue(info["has_source"])
        self.assertEqual(info["source_schema"], "fdsl/2.1")

    def test_fonts_are_passthrough(self):
        payload = b"\x00\x01\x00\x00fake-ttf-placeholder"
        blob = fdraw.pack(load_sample("signature"), fonts=[("demo.ttf", payload)])
        unpacked = fdraw.unpack(blob)
        self.assertEqual(unpacked["fonts"], {"fonts/demo.ttf": payload})
        meta = unpacked["meta"]
        self.assertEqual(meta["fonts"][0]["path"], "fonts/demo.ttf")
        self.assertEqual(meta["fonts"][0]["bytes"], len(payload))
        self.assertIn("fonts/passthrough", meta["capabilities"]["optional"])
        info = fdraw.inspect(blob)
        self.assertEqual(info["font_count"], 1)
        self.assertEqual(info["fonts"][0]["path"], "fonts/demo.ttf")
        self.assertEqual(info["sizes"]["fonts_bytes"], len(payload))

    def test_unpack_out_dir_writes_whitelisted_files(self):
        blob = fdraw.pack(
            load_sample("signature"),
            source_fdsl={"schema": "fdsl/2.1"},
            fonts=[("demo.ttf", b"font-bytes")],
        )
        out_dir = self.tmp / "extract"
        unpacked = fdraw.unpack(blob, out_dir=str(out_dir))
        names = sorted(path.name for path in out_dir.iterdir())
        self.assertEqual(names, ["fonts", "meta.json", "render.json", "source.fdsl.json"])
        self.assertEqual(
            json.loads((out_dir / "render.json").read_text(encoding="utf-8")),
            load_sample("signature"),
        )
        self.assertEqual((out_dir / "fonts" / "demo.ttf").read_bytes(), b"font-bytes")
        self.assertEqual(len(unpacked["written"]), 4)

    def test_created_and_user_meta_preserved(self):
        meta = {"created": "2026-10-20T00:00:00Z", "title": "示例作品", "id": "01TEST"}
        blob = fdraw.pack(load_sample("signature"), meta=meta)
        unpacked = fdraw.unpack(blob)
        self.assertEqual(unpacked["meta"]["created"], meta["created"])
        self.assertEqual(unpacked["meta"]["title"], meta["title"])
        self.assertEqual(unpacked["meta"]["id"], meta["id"])
        self.assertEqual(fdraw.pack(load_sample("signature"), meta=meta), blob)

    # -- rejection at pack time --------------------------------------------
    def test_pack_rejects_invalid_render(self):
        doc = copy.deepcopy(load_sample("signature"))
        doc["format"] = "not-fbs"
        with self.assertRaises(fdraw.FDrawError) as ctx:
            fdraw.pack(doc)
        self.assertEqual(error_code(ctx.exception), "FD_E_RENDER")
        self.assertTrue(
            any("format" in message for message in ctx.exception.evidence["errors"])
        )

    def test_pack_rejects_unsafe_font_name(self):
        for name in ("../evil.ttf", "/abs.ttf", "fonts/sub/dir.ttf", "evil.exe"):
            with self.subTest(name=name):
                with self.assertRaises(fdraw.FDrawError) as ctx:
                    fdraw.pack(load_sample("signature"), fonts=[(name, b"x")])
                self.assertIn(
                    error_code(ctx.exception),
                    {"FD_E_PATH", "FD_E_CONTENT_TYPE", "FD_E_UNKNOWN_ENTRY"},
                )

    def test_unknown_required_capability_rejected(self):
        render = load_sample("signature")
        render_bytes = canonical(render)
        files = {"render.json": render_bytes}
        meta = meta_for(
            files,
            root=sha256(render_bytes),
            required=["fourier/stroke", "quantum/hologram"],
        )
        blob = container_bytes(files, meta=meta)
        with self.assertRaises(fdraw.FDrawError) as ctx:
            fdraw.unpack(blob)
        self.assertEqual(error_code(ctx.exception), "FD_E_CAPABILITY")
        self.assertTrue(
            any("FD_E_CAPABILITY" in message for message in fdraw.validate(blob))
        )

    def test_validate_accepts_good_container(self):
        blob = fdraw.pack(load_sample("signature"))
        self.assertEqual(fdraw.validate(blob), [])

    def test_meta_entries_cover_every_non_meta_file(self):
        blob = fdraw.pack(
            load_sample("signature"),
            source_fdsl={"schema": "fdsl/2.1"},
            fonts=[("demo.ttf", b"font")],
        )
        unpacked = fdraw.unpack(blob)
        listed = {entry["path"] for entry in unpacked["meta"]["entries"]}
        self.assertEqual(
            listed, {"render.json", "source.fdsl.json", "fonts/demo.ttf"}
        )
        self.assertNotIn("meta.json", listed)


if __name__ == "__main__":
    unittest.main()
