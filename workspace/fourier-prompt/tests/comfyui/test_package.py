"""Static contract tests for the ComfyUI-FDP package (WP-L).

Mocks ``folder_paths`` and ``comfy`` only; ``fourier_prompt`` is the real core.
"""

from __future__ import annotations

import ast
import inspect
import os
import sys
import unittest

from _fixtures import CUSTOM_NODES, INTERRUPT, NODES, PACKAGE, SAMPLE

WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN"}


class TestPackageContract(unittest.TestCase):
    def test_mapping_has_eight_nodes_and_display_names(self):
        self.assertEqual(len(NODES), 8, f"expected 8 nodes, got {sorted(NODES)}")
        self.assertEqual(set(PACKAGE.NODE_DISPLAY_NAME_MAPPINGS), set(NODES))
        for name, cls in NODES.items():
            self.assertTrue(name.startswith("FDP"), name)
            self.assertEqual(cls.__name__, name)
            self.assertTrue(PACKAGE.NODE_DISPLAY_NAME_MAPPINGS[name].strip())

    def test_custom_types_declared(self):
        self.assertEqual(PACKAGE.nodes.FDRAW, "FDRAW")
        self.assertEqual(PACKAGE.nodes.FDSL, "FDSL")
        self.assertIs(NODES["FDPLoadFdraw"].RETURN_TYPES[0], "FDRAW")
        compile_inputs = NODES["FDPCompileFDSL"].INPUT_TYPES()
        self.assertEqual(compile_inputs["optional"]["fdsl"][0], "FDSL")
        # every FDRAW producer/consumer uses the same literal
        for name in ("FDPLoadFdraw", "FDPCompileFDSL", "FDPBatchFdraw"):
            self.assertIn("FDRAW", NODES[name].RETURN_TYPES)

    def test_v1_api_fields(self):
        for name, cls in NODES.items():
            with self.subTest(node=name):
                inputs = cls.INPUT_TYPES()
                self.assertIsInstance(inputs, dict)
                self.assertIn("required", inputs)
                self.assertIsInstance(inputs["required"], dict)
                self.assertTrue(inputs["required"], "required inputs must not be empty")
                self.assertIsInstance(cls.RETURN_TYPES, tuple)
                self.assertTrue(all(isinstance(item, str) for item in cls.RETURN_TYPES))
                self.assertTrue(callable(getattr(cls, cls.FUNCTION)))
                self.assertIsInstance(cls.CATEGORY, str)
                self.assertTrue(cls.CATEGORY.startswith("FDP"))
                if hasattr(cls, "RETURN_NAMES"):
                    self.assertEqual(len(cls.RETURN_NAMES), len(cls.RETURN_TYPES))

    def test_combo_is_non_empty_and_lists_sample(self):
        options = NODES["FDPLoadFdraw"].INPUT_TYPES()["required"]["fdraw"][0]
        self.assertIsInstance(options, list)
        self.assertTrue(options)
        self.assertNotIn("<empty>", options)
        self.assertIn("mini.fdraw", options)
        for item in options:
            self.assertFalse(os.path.isabs(item), item)

    def test_heavy_nodes_are_coroutines(self):
        for name, method in (
            ("FDPLoadFdraw", "load"),
            ("FDPSaveFdraw", "save"),
            ("FDPCompileFDSL", "compile"),
            ("FDPRenderFdraw", "render"),
            ("FDPPreviewFdraw", "preview"),
            ("FDPVideoFdraw", "video"),
            ("FDPInspectFdraw", "inspect"),
            ("FDPBatchFdraw", "batch"),
        ):
            with self.subTest(node=name):
                fn = getattr(NODES[name], method)
                self.assertTrue(inspect.iscoroutinefunction(fn), f"{name}.{method} must be async")
                self.assertIs(fn, getattr(NODES[name], NODES[name].FUNCTION))

    def test_output_arity_matches_implementation(self):
        # every FUNCTION returns a tuple with len(RETURN_TYPES) items; save is an
        # output node and returns {"ui", "result"} instead (ComfyUI C14).
        expected = {
            "FDPLoadFdraw": 2,
            "FDPSaveFdraw": 1,
            "FDPCompileFDSL": 2,
            "FDPRenderFdraw": 1,
            "FDPPreviewFdraw": 1,
            "FDPVideoFdraw": 2,
            "FDPInspectFdraw": 1,
            "FDPBatchFdraw": 2,
        }
        for name, arity in expected.items():
            self.assertEqual(len(NODES[name].RETURN_TYPES), arity, name)
        self.assertTrue(NODES["FDPSaveFdraw"].OUTPUT_NODE)
        self.assertEqual(NODES["FDPBatchFdraw"].OUTPUT_IS_LIST, (True, True))

    def test_fingerprints_stable_and_mtime_sensitive(self):
        node = NODES["FDPLoadFdraw"]()
        first = node.IS_CHANGED("mini.fdraw", False, "")
        second = node.IS_CHANGED("mini.fdraw", False, "")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertEqual(node.fingerprint_inputs("mini.fdraw", False, ""), first)
        path = str(SAMPLE["fdraw"])
        stat = os.stat(path)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        try:
            touched = node.IS_CHANGED("mini.fdraw", False, "")
            touched_new_hook = node.fingerprint_inputs("mini.fdraw", False, "")
        finally:
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertNotEqual(first, touched)
        self.assertEqual(touched, touched_new_hook, "fingerprint_inputs must dispatch to the subclass")

    def test_fingerprint_reacts_to_parameters(self):
        render = NODES["FDPRenderFdraw"]()
        base = render.IS_CHANGED("fdraw-value", 0, "fast", "auto", 2, 0.0)
        self.assertNotEqual(base, render.IS_CHANGED("fdraw-value", 0, "exact", "auto", 2, 0.0))
        self.assertNotEqual(base, render.IS_CHANGED("fdraw-value", 0, "fast", "#000000", 2, 0.0))
        self.assertEqual(base, NODES["FDPRenderFdraw"]().IS_CHANGED("fdraw-value", 0, "fast", "auto", 2, 0.0))

    def test_folder_paths_registered_and_web_directory_none(self):
        import folder_paths

        self.assertIn("fdraw", folder_paths.folder_names_and_paths)
        directories, suffixes = folder_paths.folder_names_and_paths["fdraw"]
        self.assertIn(".fdraw", suffixes)
        joined = "|".join(directories)
        self.assertIn(os.path.join("input", "fdraw"), joined)
        self.assertIn(os.path.join("output", "fdraw"), joined)
        self.assertIsNone(PACKAGE.WEB_DIRECTORY)

    def test_import_whitelist(self):
        allowed = {
            "asyncio",
            "dataclasses",
            "hashlib",
            "json",
            "logging",
            "numpy",
            "os",
            "re",
            "sys",
            "tempfile",
            "time",
            "typing",
            "__future__",
            "folder_paths",
            "comfy",
            "fourier_prompt",
        }
        for filename in ("__init__.py", "nodes.py"):
            tree = ast.parse((CUSTOM_NODES / filename).read_text(encoding="utf-8"))
            for node in tree.body:  # module level only; torch is imported lazily
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertIn(alias.name.split(".")[0], allowed, f"{filename}: {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    self.assertIn((node.module or "").split(".")[0], allowed, f"{filename}: {node.module}")
        source = (CUSTOM_NODES / "nodes.py").read_text(encoding="utf-8")
        self.assertIn("asyncio.to_thread", source)
        self.assertNotIn("import torch\n", source.split("def _image_tensor", 1)[0])

    def test_button_state_does_not_leak_between_tests(self):
        # guards the fake comfy module used by the interruption test
        self.assertFalse(INTERRUPT["flag"])


if __name__ == "__main__":
    unittest.main()
