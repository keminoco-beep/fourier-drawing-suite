"""CLI tests for ``python -m fourier_prompt.fdraw_cli`` (subprocess end-to-end)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _fixtures import PKG_ROOT, SAMPLES, load_sample

from fourier_prompt import fdraw


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "fourier_prompt.fdraw_cli", *args],
        cwd=str(PKG_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


class CliCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_pack_inspect_validate_unpack_roundtrip(self):
        fdraw_path = self.tmp / "out.fdraw"
        source_path = self.tmp / "source.json"
        source = {"schema": "fdsl/2.1", "canvas": {"w": 512, "h": 512}, "elements": []}
        source_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")

        packed = run_cli(
            "pack",
            "--fbs",
            str(SAMPLES["signature"]),
            "--source-plan",
            str(source_path),
            "--out",
            str(fdraw_path),
        )
        self.assertEqual(packed.returncode, 0, packed.stderr)
        self.assertIn("packed", packed.stdout)
        self.assertTrue(fdraw_path.is_file())

        inspected = run_cli("inspect", "--fdraw", str(fdraw_path), "--json")
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        info = json.loads(inspected.stdout)
        self.assertEqual(info["schema"], fdraw.FDRAW_VERSION)
        self.assertTrue(info["root_sha256_ok"])
        self.assertEqual(info["render"]["element_count"], 1)
        self.assertTrue(info["has_source"])

        validated = run_cli("validate", "--fdraw", str(fdraw_path))
        self.assertEqual(validated.returncode, 0, validated.stderr)
        self.assertIn("OK", validated.stdout)

        validated_json = run_cli("validate", "--fdraw", str(fdraw_path), "--json")
        self.assertEqual(validated_json.returncode, 0, validated_json.stderr)
        self.assertEqual(json.loads(validated_json.stdout), {"ok": True, "errors": []})

        out_dir = self.tmp / "unpacked"
        unpacked = run_cli(
            "unpack", "--fdraw", str(fdraw_path), "--out-dir", str(out_dir)
        )
        self.assertEqual(unpacked.returncode, 0, unpacked.stderr)
        self.assertTrue((out_dir / "render.json").is_file())
        self.assertTrue((out_dir / "source.fdsl.json").is_file())
        self.assertTrue((out_dir / "meta.json").is_file())
        self.assertEqual(
            json.loads((out_dir / "render.json").read_text(encoding="utf-8")),
            load_sample("signature"),
        )
        self.assertEqual(
            json.loads((out_dir / "source.fdsl.json").read_text(encoding="utf-8")),
            source,
        )

    def test_validate_rejects_non_zip_with_exit_code(self):
        bad = self.tmp / "bad.fdraw"
        bad.write_bytes(b"this is not a zip file")
        result = run_cli("validate", "--fdraw", str(bad))
        self.assertEqual(result.returncode, 1)
        self.assertIn("FD_E_ZIP", result.stderr)

    def test_inspect_rejects_non_zip(self):
        bad = self.tmp / "bad.fdraw"
        bad.write_bytes(b"not a zip")
        result = run_cli("inspect", "--fdraw", str(bad))
        self.assertEqual(result.returncode, 1)
        self.assertIn("FD_E_ZIP", result.stderr)

    def test_inspect_human_output(self):
        fdraw_path = self.tmp / "out.fdraw"
        fdraw_path.write_bytes(fdraw.pack(load_sample("icon")))
        result = run_cli("inspect", "--fdraw", str(fdraw_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("elements: 7", result.stdout)
        self.assertIn("layers=[0, 1]", result.stdout)
        self.assertIn("root_sha256:", result.stdout)
        self.assertIn("capabilities.required", result.stdout)

    def test_pack_bad_fbs_returns_failure(self):
        bad = self.tmp / "bad.fbs.json"
        doc = load_sample("signature")
        doc["format"] = "nope"
        bad.write_text(json.dumps(doc), encoding="utf-8")
        out = self.tmp / "bad.fdraw"
        result = run_cli("pack", "--fbs", str(bad), "--out", str(out))
        self.assertEqual(result.returncode, 1)
        self.assertIn("FD_E_RENDER", result.stderr)
        self.assertFalse(out.exists())

    def test_pack_missing_input_returns_failure(self):
        result = run_cli(
            "pack",
            "--fbs",
            str(self.tmp / "missing.fbs.json"),
            "--out",
            str(self.tmp / "x.fdraw"),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("FD_E_INPUT", result.stderr)


if __name__ == "__main__":
    unittest.main()
