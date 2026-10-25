"""Security / malformed-container tests (zip-slip, bombs, bad JSON, hashes)."""

from __future__ import annotations

import json
import unittest

from _fixtures import (
    canonical,
    container_bytes,
    error_code,
    load_sample,
    make_zip,
    meta_for,
    patched_limits,
    sha256,
    zip_infos,
)

from fourier_prompt import fdraw


class SecurityCase(unittest.TestCase):
    def setUp(self):
        self.render = load_sample("signature")
        self.render_bytes = canonical(self.render)
        self.root = sha256(self.render_bytes)

    def _container(self, files: dict[str, bytes], **meta_kwargs) -> bytes:
        return container_bytes(
            files, meta=meta_for(files, root=meta_kwargs.pop("root", self.root), **meta_kwargs)
        )

    def _assert_code(self, blob, code: str):
        with self.assertRaises(fdraw.FDrawError) as ctx:
            fdraw.unpack(blob)
        self.assertEqual(error_code(ctx.exception), code)
        self.assertTrue(
            any(code in message for message in fdraw.validate(blob)),
            f"validate() 未返回 {code}: {fdraw.validate(blob)}",
        )

    # -- path traversal -----------------------------------------------------
    def test_zip_slip_paths_rejected(self):
        bad_names = [
            "../evil.json",
            "/evil.json",
            "C:/evil.json",
            "..\\evil.json",
            "fonts/../../evil.ttf",
        ]
        meta = canonical(meta_for({"render.json": self.render_bytes}, root=self.root))
        for name in bad_names:
            with self.subTest(name=name):
                zip_name = name.replace("\\", "/")
                blob = make_zip(
                    [
                        ("meta.json", meta),
                        ("render.json", self.render_bytes),
                        (zip_name, b"x"),
                    ]
                )
                if "\\" in name:
                    # ``zipfile`` normalises backslashes on write/read, so the
                    # in-memory patch makes the reader see the raw name where
                    # possible; the path guard is also unit-tested below.
                    self.assertEqual(len(zip_name), len(name))
                    blob = blob.replace(zip_name.encode(), name.encode())
                self._assert_code(blob, "FD_E_PATH")

    def test_backslash_path_guard(self):
        # Python's ``zipfile`` normalises ``\`` to ``/`` on read, so exercise the
        # path guard directly (it also protects non-Python ZIP producers/readers).
        for name in ("fonts\\evil.ttf", "..\\evil.json", "a\\b\\c.json"):
            with self.subTest(name=name):
                with self.assertRaises(fdraw.FDrawError) as ctx:
                    fdraw._validate_rel_path(name)
                self.assertEqual(error_code(ctx.exception), "FD_E_PATH")

    def test_unknown_entry_rejected(self):
        meta = canonical(meta_for({"render.json": self.render_bytes}, root=self.root))
        blob = make_zip(
            [
                ("meta.json", meta),
                ("render.json", self.render_bytes),
                ("payload.exe", b"MZ"),
            ]
        )
        self._assert_code(blob, "FD_E_UNKNOWN_ENTRY")

    def test_symlink_entry_rejected(self):
        files = {"render.json": self.render_bytes, "fonts/a.ttf": b"not-a-font"}
        meta = canonical(meta_for(files, root=self.root))
        entries = [("meta.json", meta)] + list(files.items())
        blob = make_zip(entries, symlinks={"fonts/a.ttf"})
        self._assert_code(blob, "FD_E_ENTRY_TYPE")

    def test_duplicate_case_insensitive_entries_rejected(self):
        meta = canonical(meta_for({"render.json": self.render_bytes}, root=self.root))
        blob = make_zip(
            [
                ("meta.json", meta),
                ("render.json", self.render_bytes),
                ("fonts/A.ttf", b"a"),
                ("fonts/a.ttf", b"b"),
            ]
        )
        self._assert_code(blob, "FD_E_DUPLICATE")

    # -- required entries / JSON -------------------------------------------
    def test_missing_render_rejected(self):
        meta = canonical(meta_for({"source.fdsl.json": b"{}"}, root=self.root))
        blob = make_zip([("meta.json", meta), ("source.fdsl.json", b"{}")])
        self._assert_code(blob, "FD_E_RENDER")

    def test_missing_meta_rejected(self):
        blob = make_zip([("render.json", self.render_bytes)])
        self._assert_code(blob, "FD_E_MANIFEST")

    def test_bad_json_rejected(self):
        files = {"render.json": b"{not json"}
        blob = self._container(files, root="0" * 64)
        self._assert_code(blob, "FD_E_JSON")

    def test_nan_json_rejected(self):
        files = {"render.json": b'{"format":"fourier-brushstroke","x":NaN}'}
        blob = self._container(files, root="0" * 64)
        self._assert_code(blob, "FD_E_JSON")

    def test_non_object_meta_rejected(self):
        blob = make_zip(
            [("meta.json", b"[]"), ("render.json", self.render_bytes)]
        )
        self._assert_code(blob, "FD_E_MANIFEST")

    # -- budgets / bombs ----------------------------------------------------
    def test_oversized_entry_rejected(self):
        with patched_limits(max_font_entry_bytes=64, max_fonts_bytes=64):
            files = {"render.json": self.render_bytes, "fonts/a.ttf": b"x" * 256}
            blob = self._container(files)
            self._assert_code(blob, "FD_E_ENTRY_TOO_BIG")

    def test_too_many_entries_rejected(self):
        files = {"render.json": self.render_bytes, "source.fdsl.json": b"{}"}
        blob = container_bytes(files, meta=meta_for(files, root=self.root))
        self.assertEqual(len(zip_infos(blob)), 3)
        with patched_limits(max_entries=2):
            self._assert_code(blob, "FD_E_TOO_MANY_ENTRIES")

    def test_compression_bomb_rejected(self):
        with patched_limits(max_compression_ratio=2.0, compression_ratio_min_bytes=64):
            files = {
                "render.json": self.render_bytes,
                "source.fdsl.json": b"0" * 8192,
            }
            blob = self._container(files)
            self._assert_code(blob, "FD_E_BOMB")

    def test_total_uncompressed_budget_rejected(self):
        blob = fdraw.pack(self.render)
        with patched_limits(max_total_bytes=100):
            self._assert_code(blob, "FD_E_BOMB")

    # -- manifest / hash ----------------------------------------------------
    def test_tampered_render_hash_rejected(self):
        blob = fdraw.pack(self.render, source_fdsl={"schema": "fdsl/2.1"})
        entries = {info.filename: data for info, data in zip_infos(blob)}
        tampered = json.loads(self.render_bytes.decode("utf-8"))
        tampered["canvas"]["width"] = 2.0
        new_render = canonical(tampered)
        meta = json.loads(entries["meta.json"].decode("utf-8"))
        for entry in meta["entries"]:
            if entry["path"] == "render.json":
                entry["bytes"] = len(new_render)
                entry["sha256"] = sha256(new_render)
        # Keep the OLD root_sha256 -> hash mismatch must be detected.
        new_meta = canonical(meta)
        tampered_blob = make_zip(
            [
                ("meta.json", new_meta),
                ("render.json", new_render),
                ("source.fdsl.json", entries["source.fdsl.json"]),
            ]
        )
        self._assert_code(tampered_blob, "FD_E_HASH")
        info = fdraw.inspect(tampered_blob)
        self.assertFalse(info["root_sha256_ok"])
        # The manifest itself was rewritten, so its own checksum is now stale too.
        self.assertFalse(info["manifest_sha256_ok"])

    def test_unlisted_entry_rejected(self):
        meta = canonical(meta_for({"render.json": self.render_bytes}, root=self.root))
        blob = make_zip(
            [
                ("meta.json", meta),
                ("render.json", self.render_bytes),
                ("fonts/a.ttf", b"x"),
            ]
        )
        self._assert_code(blob, "FD_E_MANIFEST")

    def test_listed_entry_missing_rejected(self):
        files = {
            "render.json": self.render_bytes,
            "source.fdsl.json": b"{}",
        }
        meta = canonical(meta_for(files, root=self.root))
        blob = make_zip([("meta.json", meta), ("render.json", self.render_bytes)])
        self._assert_code(blob, "FD_E_MANIFEST")

    # -- pack-time budgets --------------------------------------------------
    def test_pack_too_many_entries_rejected(self):
        with patched_limits(max_entries=2):
            with self.assertRaises(fdraw.FDrawError) as ctx:
                fdraw.pack(self.render, source_fdsl={"schema": "fdsl/2.1"})
        self.assertEqual(error_code(ctx.exception), "FD_E_TOO_MANY_ENTRIES")

    def test_pack_oversized_source_rejected(self):
        with patched_limits(max_source_bytes=16):
            with self.assertRaises(fdraw.FDrawError) as ctx:
                fdraw.pack(
                    self.render,
                    source_fdsl={"schema": "fdsl/2.1", "elements": [{"id": "e1"}]},
                )
        self.assertEqual(error_code(ctx.exception), "FD_E_ENTRY_TOO_BIG")

    def test_pack_rejects_unknown_required_capability(self):
        with self.assertRaises(fdraw.FDrawError) as ctx:
            fdraw.pack(
                self.render,
                meta={"capabilities": {"required": ["quantum/hologram"]}},
            )
        self.assertEqual(error_code(ctx.exception), "FD_E_CAPABILITY")

    # -- raw ZIP flag guards (zipfile normalises some malicious fields) -----
    def test_encryption_flag_guard(self):
        import zipfile

        info = zipfile.ZipInfo("render.json")
        info.flag_bits |= 0x1
        with self.assertRaises(fdraw.FDrawError) as ctx:
            fdraw._check_zip_info(info)
        self.assertEqual(error_code(ctx.exception), "FD_E_ZIP_MODE")

    def test_unsupported_compression_guard(self):
        import zipfile

        info = zipfile.ZipInfo("render.json")
        info.compress_type = zipfile.ZIP_BZIP2
        with self.assertRaises(fdraw.FDrawError) as ctx:
            fdraw._check_zip_info(info)
        self.assertEqual(error_code(ctx.exception), "FD_E_ZIP_MODE")

    def test_container_bytes_size_limit(self):
        with patched_limits(max_container_bytes=16):
            with self.assertRaises(fdraw.FDrawError) as ctx:
                fdraw.inspect(b"x" * 64)
        self.assertEqual(error_code(ctx.exception), "FD_E_ENTRY_TOO_BIG")


if __name__ == "__main__":
    unittest.main()
