"""Font asset / license / subsetting tests (WP-K)."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from _fixtures import FONT_PATH, LICENSES_PATH, OFL_PATH, PACKAGE_ROOT

from fourier_prompt import fonts


class FontAssetCase(unittest.TestCase):
    def test_packaged_asset_exists_and_loads(self):
        self.assertTrue(FONT_PATH.is_file(), f"missing packaged font asset: {FONT_PATH}")
        handle = fonts.load_font()
        info = handle.info()
        self.assertEqual(info["path"], str(FONT_PATH.resolve()))
        self.assertGreater(info["size_bytes"], 100_000)
        self.assertEqual(info["sha256"], hashlib.sha256(FONT_PATH.read_bytes()).hexdigest())
        self.assertTrue(info["family"].startswith("FdrawSubset NotoSansSC "), info["family"])
        self.assertEqual(info["upem"], 1000)
        self.assertAlmostEqual(info["ascent_ratio"], 0.88, places=6)
        self.assertEqual(info["fonttools"].split(".")[0], "4")
        for table in ("head", "hhea", "hmtx", "cmap", "glyf", "loca", "maxp", "name", "OS/2"):
            self.assertIn(table, info["tables"])
        self.assertGreater(info["cmap_entries"], 3000)
        self.assertGreater(info["glyphs"], 3000)

    def test_license_is_shipped_and_recorded(self):
        self.assertTrue(OFL_PATH.is_file())
        license_text = OFL_PATH.read_text(encoding="utf-8")
        self.assertIn("SIL OPEN FONT LICENSE", license_text)
        self.assertIn("Version 1.1", license_text)
        self.assertIn("PERMISSION & CONDITIONS", license_text)

        self.assertTrue(LICENSES_PATH.is_file())
        record = LICENSES_PATH.read_text(encoding="utf-8")
        info = fonts.font_info()
        self.assertIn(info["sha256"], record)
        self.assertIn("OFL-1.1", record)
        self.assertIn("pyftsubset", record)
        self.assertIn("FdrawSubset NotoSansSC", record)
        self.assertIn("Reserved Font Name", record)
        self.assertIn("https://github.com/notofonts/noto-cjk", record)
        self.assertIn("763146584cf0710223441356b4395e279021b0806c196614377a7a0174ae074a", record)
        self.assertIn(hashlib.sha256(OFL_PATH.read_bytes()).hexdigest(), record)

    def test_font_name_table_keeps_license(self):
        handle = fonts.load_font()
        self.assertIn("SIL Open Font License", handle.license_name)
        self.assertIn("scripts.sil.org/OFL", handle.license_url)
        self.assertTrue(handle.postscript_name.startswith("FdrawSubsetNotoSansSC-"))

    def test_missing_font_fails_loud_without_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist.ttf"
            with self.assertRaises(fonts.FontError) as ctx:
                fonts.load_font(missing)
        self.assertEqual(ctx.exception.code, "FP_E_TEXT_FONT")
        self.assertIn("no system-font fallback", ctx.exception.message)
        self.assertEqual(ctx.exception.path, str(missing))

    def test_uncovered_character_fails_loud(self):
        handle = fonts.load_font()
        self.assertIsNone(handle.glyph_id("🦄"))
        with self.assertRaises(fonts.FontError) as ctx:
            fonts.require_glyph(handle, "🦄")
        self.assertEqual(ctx.exception.code, "FP_E_TEXT_GLYPH")
        self.assertIn("U+1F984", ctx.exception.message)

    def test_common_charset_covers_ascii_hanzi_and_sample(self):
        charset = fonts.common_charset()
        self.assertGreater(len(charset), 3000)
        for char in "A z 0 9 你好中文，。！？…":
            if char != " ":
                self.assertIn(char, charset)
        handle = fonts.load_font()
        missing = [char for char in charset if handle.glyph_id(char) is None]
        self.assertEqual(missing, [], f"asset does not cover charset: {missing[:10]}")


class SubsetCase(unittest.TestCase):
    def test_subset_is_reproducible_and_renamed(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "sub1.ttf"
            second = Path(tmp) / "sub2.ttf"
            meta1 = fonts.subset_font(FONT_PATH, first, text="A你o8，! 2026")
            meta2 = fonts.subset_font(FONT_PATH, second, text="A你o8，! 2026")
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(meta1["sha256"], meta2["sha256"])
            self.assertEqual(meta1["sha256"], hashlib.sha256(first.read_bytes()).hexdigest())
            self.assertEqual(meta1["source_sha256"], fonts.font_info()["sha256"])
            self.assertTrue(meta1["family"].startswith("FdrawSubset NotoSansSC "))
            self.assertTrue(meta1["renamed"])
            self.assertLess(meta1["bytes"], fonts.font_info()["size_bytes"])
            loaded = fonts.load_font(first)
            for char in "A你o8，! 2026":
                self.assertIsNotNone(loaded.glyph_id(char), char)

    def test_subset_reports_missing_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "bad.ttf"
            with self.assertRaises(fonts.FontError) as ctx:
                fonts.subset_font(FONT_PATH, out, text="A🦄")
        self.assertEqual(ctx.exception.code, "FP_E_FONT_SUBSET")
        self.assertIn("U+1F984", ctx.exception.message)

    def test_subset_rejects_empty_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(fonts.FontError) as ctx:
                fonts.subset_font(FONT_PATH, Path(tmp) / "empty.ttf", text="")
        self.assertEqual(ctx.exception.code, "FP_E_FONT_SUBSET")


if __name__ == "__main__":
    unittest.main()
