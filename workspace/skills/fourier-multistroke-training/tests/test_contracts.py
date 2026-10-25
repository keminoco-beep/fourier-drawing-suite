#!/usr/bin/env python
"""Repository-level contract tests for the ``fourier-multistroke-training`` skill.

Covers the cross-cutting quality gates that do not need a training run:

* every ``scripts/*.py`` answers ``--help`` with exit code 0 within 60 s;
* every script byte-compiles;
* ``SKILL.md`` keeps its key sections and its script/reference/assets tree is
  consistent with what is actually on disk;
* ``layered_base.py`` keeps its frozen v1.33 CLI/default contract and the
  documented tile/palette numbers in ``SKILL.md`` do not drift from argparse;
* the v1.33 gradient-gate options are documented once the docs are updated;
* scripts contain no hard-coded drive-letter absolute paths;
* all Markdown / Python files decode as strict UTF-8;
* ``--detail`` skips old underpainting strokes (``base_pass*`` / ``fix_*`` /
  ``soft_*``), the pre-v1.33 bug this cycle fixes.

Run from the skill root::

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import py_compile
import re
import tempfile
import unittest

try:  # imported as a top-level module by ``unittest discover -s tests``
    import _support as S
except ImportError:  # imported as ``tests.test_contracts``
    from tests import _support as S

#: Frozen core defaults of layered_base.py (Lead-confirmed v1.33 + v1.34/v1.35
#: additions; changing one on purpose means updating SKILL.md and this table
#: together).  v1.35: default ground = toned and the formal layers start earlier
#: (err-pct 82/72/60 -> 60/45/45).
FROZEN_CORE_DEFAULTS = {
    "--work-res": 1536,
    "--coarse": 24.0,
    "--mid": 12.0,
    "--fine": 6.0,
    "--mid-err-pct": 60.0,
    "--fine-err-pct": 45.0,
    "--base-color-mode": "toned",
    "--base-k": 8,
    "--mid-k": 10,
    "--fine-k": 24,
    "--base-jitter": 0.006,
    "--mid-jitter": 0.010,
    "--fine-jitter": 0.015,
    "--upper-color-mode": "palette",
    "--upper-blur": 2.5,
    "--upper-gradient": False,
    "--pass2-tile": 64,
    "--pass2-k": 6,
    "--pass3-tile": 32,
    "--pass3-k": 8,
    "--pass4-tile": 16,
    "--pass4-k": 8,
    "--pass4-err-pct": 45.0,
    "--pass4-spacing": 4.0,
    "--max-curves": 100000,
    "--seed": 20261103,
    # --- v1.34 pass1 additions (frozen with fixer-core 2026-10-24) ---------
    "--base-blur": 4.0,
    "--base-gradient": False,
    "--base-width-factor": 1.5,
    "--base-length-factor": 2.0,
}

#: Frozen v1.35 human-like underpainting interface (Lead-confirmed 2026-10-24).
FROZEN_BASE_COAT_DEFAULTS = {
    "--base-color": "auto",
    "--base-coat-angle": 0.0,
    "--base-coat-spacing": 0.0,          # sentinel: 0 -> --coarse
    "--base-coat-width-factor": 1.04,
    "--base-coat-jitter": 0.5,
}

FROZEN_BASE_MODE_CHOICES = ["toned", "none", "single", "quantized", "source"]

#: Frozen v1.36 human-paint interface (Lead-confirmed 2026-10-24).
FROZEN_HUMAN_PAINT_DEFAULTS = {
    "--base-coat-pattern": "wash",
    "--wash-angle": -22.0,
    "--flow-strokes": "on",
    "--paint-order": "region",
    "--region-count": 12,
    "--thick-dabs": 0,
    "--clip-to-fg": "auto",
}

FROZEN_HUMAN_PAINT_CHOICES = {
    "--base-coat-pattern": ["wash", "rows"],
    "--flow-strokes": ["on", "off"],
    "--paint-order": ["region", "scan", "legacy"],
    "--clip-to-fg": ["auto", "on", "off"],
}

#: Frozen v1.34 texture-flat interface (Lead-confirmed with fixer-core).
FROZEN_TEXTURE_FLAT_DEFAULTS = {
    "--texture-flat": False,
    "--texture-flat-count": 8000,
    "--texture-flat-spacing": 2.5,
    "--texture-flat-len-min": 5.0,
    "--texture-flat-len-max": 9.0,
    "--texture-flat-width-min": 2.5,
    "--texture-flat-width-max": 3.5,
    "--texture-flat-lstd": 0.01,
    "--texture-flat-ref-std": 0.0,
    "--texture-flat-layer": 19,
    "--texture-flat-jitter": 0.01,
}

#: Frozen v1.38 GPU render interface (Lead-confirmed 2026-10-25).
FROZEN_GPU_RENDER_DEFAULTS = {
    "--resolution": 1024,
    "--supersample": 2,
    "--device": "gl",
    "--background": None,
    "--width-scale": None,
}

FROZEN_GPU_RENDER_CHOICES = {
    "--supersample": [1, 2, 3],
    "--device": ["gl", "cpu"],
}

#: Frozen v1.37 silhouette-aware stroke generation (Lead-confirmed).
FROZEN_SILHOUETTE_DEFAULTS = {
    "--silhouette": "auto",
    "--silhouette-shrink-px": 1.0,
    "--silhouette-margin-px": 0.25,
    "--silhouette-min-width-px": 0.6,
    "--silhouette-feather-px": 0.8,
}

FROZEN_SILHOUETTE_CHOICES = {
    "--silhouette": ["auto", "on", "off"],
}

#: Frozen v1.33 gradient safety gate interface (Lead-confirmed with fixer-core).
FROZEN_GRADIENT_DEFAULTS = {
    "--gradient-gate": "on",
    "--gradient-max-delta": 0.12,
    "--gradient-edge-delta": 0.10,
    "--gradient-edge-pct": 100.0,
}

REQUIRED_SKILL_SECTIONS = [
    "## 目录与依赖",
    "## 一键本地流水线（推荐）",
    "## 快速开始",
    "## 算法要点（完整见 `references/ALGORITHM.md`）",
    "## 产物",
    "## 参数上限与实测（交付口径）",
    "## 检查表",
    "## 参考",
]



def _script_files():
    return sorted(os.path.join(S.SCRIPTS_DIR, name) for name in os.listdir(S.SCRIPTS_DIR)
                  if name.endswith(".py"))


def _skill_md_and_refs():
    parts = [S.read_text(S.SKILL_MD)]
    for name in sorted(os.listdir(S.REFERENCES_DIR)):
        if name.endswith(".md"):
            parts.append(S.read_text(os.path.join(S.REFERENCES_DIR, name)))
    return "\n".join(parts)


class TestHelpAndCompile(unittest.TestCase):
    """CLI discoverability + syntax contract for every script."""

    def test_every_script_help_exits_zero(self):
        failures = []
        for script in _script_files():
            try:
                proc = S.run_cli([script, "--help"], timeout=60)
            except Exception as exc:  # timeout / OSError
                failures.append(f"{os.path.basename(script)}: {type(exc).__name__}: {exc}")
                continue
            if proc.returncode != 0:
                failures.append(
                    f"{os.path.basename(script)}: exit {proc.returncode}; "
                    f"stderr={proc.stderr.strip()[-400:]!r}")
        self.assertFalse(failures, "scripts failing `<script> --help`:\n" +
                         "\n".join(failures))

    def test_every_script_compiles(self):
        failures = []
        with tempfile.TemporaryDirectory(prefix="pycompile_") as tmp:
            for script in _script_files():
                cfile = os.path.join(tmp, os.path.basename(script) + "c")
                try:
                    py_compile.compile(script, cfile=cfile, doraise=True)
                except py_compile.PyCompileError as exc:
                    failures.append(f"{os.path.basename(script)}: {exc}")
        self.assertFalse(failures, "scripts failing py_compile:\n" + "\n".join(failures))


def _parse_skill_tree(skill_md_text):
    """Parse the SKILL.md directory-tree block into per-section file sets.

    Returns ``{section: {file names}}`` where ``section`` is ``"scripts"``,
    ``"references"``, ``"assets"``, ``"tests"`` (once fixer-docs adds it) or
    ``"__root__"`` for files directly under the skill directory (SKILL.md).
    Top-level entries start at column 0 (``├── scripts/``); child entries are
    indented (``│   ├── layered_base.py`` or ``    ├── preview.png``).
    """
    blocks = re.findall(r"```(.*?)```", skill_md_text, flags=re.S)
    tree = next((block for block in blocks
                 if "fourier-multistroke-training/" in block and "├──" in block), "")
    sections = {"__root__": set()}
    current = "__root__"
    for raw in tree.splitlines():
        if not raw.strip():
            continue
        # top-level entry: no leading tree glyph / indentation at all
        if raw.startswith(("├── ", "└── ")):
            name = raw[4:].strip()
            if name.endswith("/"):
                current = name.rstrip("/")
                sections.setdefault(current, set())
            else:
                current = "__root__"
                sections["__root__"].add(name)
            continue
        match = re.match(r"^[│ ]*[├└]──\s+([\w.\-]+\.(?:py|md|png))", raw)
        if match:
            sections.setdefault(current, set()).add(match.group(1))
    return sections


class TestDocumentation(unittest.TestCase):
    """SKILL.md sections and directory-tree declarations stay in sync."""

    @classmethod
    def setUpClass(cls):
        cls.skill_md = S.read_text(S.SKILL_MD)
        cls.sections = _parse_skill_tree(cls.skill_md)
        cls.tree_found = any(cls.sections.values())

    def test_required_sections_present(self):
        missing = [sec for sec in REQUIRED_SKILL_SECTIONS if sec not in self.skill_md]
        self.assertFalse(missing, "SKILL.md missing sections:\n" + "\n".join(missing))

    def test_scripts_declared_and_exist(self):
        self.assertTrue(self.tree_found, "SKILL.md directory tree not found")
        actual = {os.path.basename(p) for p in _script_files()}
        declared = set(self.sections.get("scripts", set()))
        missing_files = sorted(declared - actual)
        undocumented = sorted(actual - declared)
        self.assertFalse(missing_files,
                         f"SKILL.md declares scripts that do not exist: {missing_files}")
        self.assertFalse(undocumented,
                         f"scripts missing from the SKILL.md directory tree: {undocumented}")

    def test_references_declared_and_exist(self):
        actual = {name for name in os.listdir(S.REFERENCES_DIR) if name.endswith(".md")}
        declared = set(self.sections.get("references", set()))
        missing = sorted(declared - actual)
        undocumented = sorted(actual - declared)
        self.assertFalse(missing,
                         f"SKILL.md declares references that do not exist: {missing}")
        self.assertFalse(undocumented,
                         f"references missing from the SKILL.md directory tree: {undocumented}")
        for name in actual:
            self.assertTrue(os.path.isfile(os.path.join(S.REFERENCES_DIR, name)))

    def test_assets_declared_and_exist(self):
        assets_dir = os.path.join(S.SKILL_ROOT, "assets")
        actual = {name for name in os.listdir(assets_dir) if name.endswith(".png")}
        declared = set(self.sections.get("assets", set()))
        self.assertFalse(sorted(declared - actual),
                         f"declared assets missing: {sorted(declared - actual)}")
        self.assertFalse(sorted(actual - declared),
                         f"assets missing from the SKILL.md tree: {sorted(actual - declared)}")

    def test_skill_md_declared_and_exists(self):
        root_files = self.sections.get("__root__", set())
        if root_files:
            self.assertIn("SKILL.md", root_files)
        self.assertTrue(os.path.isfile(S.SKILL_MD))

    def test_tests_dir_declared_if_tree_updated(self):
        """Once fixer-docs adds ``tests/`` to the tree, declarations must be real."""
        declared = set(self.sections.get("tests", set()))
        if not declared:
            self.skipTest("SKILL.md directory tree does not list tests/ yet "
                          "(fixer-docs deliverable pending)")
        self.assertGreaterEqual(len(declared), 1)
        missing = sorted(name for name in declared
                         if not os.path.isfile(os.path.join(S.TESTS_DIR, name)))
        self.assertFalse(missing, f"SKILL.md tests/ tree lists missing files: {missing}")


class TestEncodingAndPaths(unittest.TestCase):
    """Strict UTF-8 + no machine-specific absolute paths in scripts."""

    def _text_files(self):
        files = [S.SKILL_MD]
        for directory, suffixes in (
            (S.REFERENCES_DIR, (".md",)),
            (S.SCRIPTS_DIR, (".py",)),
            (S.TESTS_DIR, (".py", ".md")),
        ):
            if not os.path.isdir(directory):
                continue
            for name in sorted(os.listdir(directory)):
                path = os.path.join(directory, name)
                if os.path.isfile(path) and name.endswith(suffixes):
                    files.append(path)
        return files

    def test_all_text_files_are_strict_utf8(self):
        failures = []
        for path in self._text_files():
            try:
                with open(path, "rb") as fh:
                    fh.read().decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                failures.append(f"{os.path.relpath(path, S.SKILL_ROOT)}: {exc}")
        self.assertFalse(failures, "non-UTF-8 files:\n" + "\n".join(failures))

    def test_scripts_have_no_hardcoded_absolute_paths(self):
        # Drive-letter absolute path (any <letter>:/... form); URLs like http:// do not
        # match because the drive letter must not be preceded by a word char.
        pattern = re.compile(r"(?<![\w])[A-Za-z]:[\\/]")
        failures = []
        for script in _script_files():
            for lineno, line in enumerate(S.read_text(script).splitlines(), 1):
                if pattern.search(line):
                    failures.append(f"{os.path.basename(script)}:{lineno}: {line.strip()[:160]}")
        self.assertFalse(failures, "hard-coded absolute paths in scripts:\n" +
                         "\n".join(failures))


class TestLayeredBaseInterface(unittest.TestCase):
    """Frozen v1.33 CLI/default contract of layered_base.py and doc consistency."""

    @classmethod
    def setUpClass(cls):
        # Always inspect the skill's own script (not a FOURIER_LAYERED_BASE
        # snapshot): this is the final-state interface contract.
        cls.defaults = S.argparse_defaults(S.DEFAULT_LAYERED)
        cls.choices = S.argparse_choices(S.DEFAULT_LAYERED)
        cls.help_text = S.script_help(S.DEFAULT_LAYERED)
        cls.skill_md = S.read_text(S.SKILL_MD)
        cls.docs = _skill_md_and_refs()

    # -- frozen argparse defaults -------------------------------------
    def test_core_defaults_frozen(self):
        problems = []
        for option, expected in FROZEN_CORE_DEFAULTS.items():
            if option not in self.defaults:
                problems.append(f"{option}: missing from argparse")
            elif self.defaults[option] != expected:
                problems.append(f"{option}: argparse default {self.defaults[option]!r} "
                                f"!= contract {expected!r}")
        self.assertFalse(problems, "layered_base.py core defaults drifted:\n" +
                         "\n".join(problems))

    def test_gradient_gate_interface_frozen(self):
        self.assertIn("--gradient-gate", self.help_text,
                      "v1.33 gradient safety gate options missing from --help")
        problems = []
        for option, expected in FROZEN_GRADIENT_DEFAULTS.items():
            if option not in self.defaults:
                problems.append(f"{option}: missing from argparse")
            elif self.defaults[option] != expected:
                problems.append(f"{option}: argparse default {self.defaults[option]!r} "
                                f"!= contract {expected!r}")
        if self.choices.get("--gradient-gate") != ["on", "off"]:
            problems.append(f"--gradient-gate choices: {self.choices.get('--gradient-gate')!r} "
                            "!= ['on', 'off']")
        if self.choices.get("--base-color-mode") != FROZEN_BASE_MODE_CHOICES:
            problems.append(f"--base-color-mode choices: "
                            f"{self.choices.get('--base-color-mode')!r} "
                            f"!= {FROZEN_BASE_MODE_CHOICES}")
        if self.choices.get("--upper-color-mode") != ["palette", "source"]:
            problems.append("--upper-color-mode choices drifted")
        self.assertFalse(problems, "\n".join(problems))

    def test_base_coat_interface_frozen(self):
        """v1.35 human-like underpainting: toned default, colour + coat geometry."""
        self.assertIn("--base-color-mode", self.help_text)
        problems = []
        if self.defaults.get("--base-color-mode") != "toned":
            problems.append(f"--base-color-mode default "
                            f"{self.defaults.get('--base-color-mode')!r} != 'toned'")
        for option, expected in FROZEN_BASE_COAT_DEFAULTS.items():
            if option not in self.defaults:
                problems.append(f"{option}: missing from argparse")
            elif self.defaults[option] != expected:
                problems.append(f"{option}: argparse default {self.defaults[option]!r} "
                                f"!= contract {expected!r}")
        for option, expected in (("--max-curves", 100000),
                                 ("--mid-err-pct", 60.0),
                                 ("--fine-err-pct", 45.0),
                                 ("--pass4-err-pct", 45.0)):
            if self.defaults.get(option) != expected:
                problems.append(f"{option}: default {self.defaults.get(option)!r} "
                                f"!= contract {expected!r}")
        self.assertFalse(problems, "v1.35 base-coat interface drifted:\n" +
                         "\n".join(problems))

    def test_texture_flat_interface_frozen(self):
        """v1.34 opt-in texture-flat flags + the v1.34 pass1 additions."""
        problems = []
        self.assertIn("--texture-flat", self.help_text,
                      "v1.34 texture-flat options missing from --help")
        for option, expected in FROZEN_TEXTURE_FLAT_DEFAULTS.items():
            if option not in self.defaults:
                problems.append(f"{option}: missing from argparse")
            elif self.defaults[option] != expected:
                problems.append(f"{option}: argparse default {self.defaults[option]!r} "
                                f"!= contract {expected!r}")
        for option, expected in (("--base-blur", 4.0),
                                 ("--base-gradient", False),
                                 ("--base-width-factor", 1.5),
                                 ("--base-length-factor", 2.0)):
            if self.defaults.get(option) != expected:
                problems.append(f"{option}: v1.34 pass1 default "
                                f"{self.defaults.get(option)!r} != contract {expected!r}")
        self.assertFalse(problems, "\n".join(problems))

    def test_human_paint_interface_frozen(self):
        """v1.36: wash ground default, form-following flow and region order."""
        problems = []
        for option, expected in FROZEN_HUMAN_PAINT_DEFAULTS.items():
            if option not in self.defaults:
                problems.append(f"{option}: missing from argparse")
            elif self.defaults[option] != expected:
                problems.append(f"{option}: argparse default "
                                f"{self.defaults[option]!r} != contract "
                                f"{expected!r}")
        for option, expected in FROZEN_HUMAN_PAINT_CHOICES.items():
            if self.choices.get(option) != expected:
                problems.append(f"{option}: choices {self.choices.get(option)!r} "
                                f"!= {expected}")
        self.assertIn("--base-coat-pattern", self.help_text)
        self.assertIn("--paint-order", self.help_text)
        self.assertIn("--clip-to-fg", self.help_text)
        self.assertEqual(self.defaults.get("--base-color-mode"), "toned")
        self.assertFalse(problems, "v1.36 human-paint interface drifted:\n" +
                         "\n".join(problems))

    def test_silhouette_interface_frozen(self):
        """v1.37: silhouette-aware generation interface."""
        problems = []
        for option, expected in FROZEN_SILHOUETTE_DEFAULTS.items():
            if option not in self.defaults:
                problems.append(f"{option}: missing from argparse")
            elif self.defaults[option] != expected:
                problems.append(f"{option}: argparse default "
                                f"{self.defaults[option]!r} != contract "
                                f"{expected!r}")
        for option, expected in FROZEN_SILHOUETTE_CHOICES.items():
            if self.choices.get(option) != expected:
                problems.append(f"{option}: choices {self.choices.get(option)!r} "
                                f"!= {expected}")
        self.assertIn("--silhouette", self.help_text)
        self.assertFalse(problems, "v1.37 silhouette interface drifted:\n" +
                         "\n".join(problems))

    def test_gpu_render_interface_frozen(self):
        """v1.38: gpu_render.py CLI contract (device/supersample/...)."""
        script = os.path.join(S.SCRIPTS_DIR, "gpu_render.py")
        if not os.path.isfile(script):
            self.skipTest("gpu_render.py not implemented")
        defaults = S.argparse_defaults(script)
        choices = S.argparse_choices(script)
        problems = []
        for option, expected in FROZEN_GPU_RENDER_DEFAULTS.items():
            if option not in defaults:
                problems.append(f"{option}: missing from argparse")
            elif defaults[option] != expected:
                problems.append(f"{option}: argparse default "
                                f"{defaults[option]!r} != contract "
                                f"{expected!r}")
        for option, expected in FROZEN_GPU_RENDER_CHOICES.items():
            if choices.get(option) != expected:
                problems.append(f"{option}: choices {choices.get(option)!r} "
                                f"!= {expected}")
        help_text = S.script_help(script)
        for option in ("--fdraw", "--fbs", "--strokes", "--out",
                       "--device", "--timing-json"):
            if option not in help_text:
                problems.append(f"{option}: missing from --help")
        self.assertFalse(problems,
                         "v1.38 GPU render interface drifted:\n" +
                         "\n".join(problems))

    # -- documentation does not drift from argparse -------------------
    def test_documented_tile_and_palette_numbers_match_defaults(self):
        """SKILL.md §16.991 declares 64/32/16px tiles and K=6/8/8 palettes."""
        tile_match = re.search(r"Pass2/3/4 用\s*(\d+)\s*/\s*(\d+)\s*/\s*(\d+)px",
                               self.skill_md)
        self.assertIsNotNone(
            tile_match,
            "SKILL.md no longer states 'Pass2/3/4 用 64/32/16px 局部 tile'; "
            "update this contract test together with the docs")
        documented = [int(tile_match.group(i)) for i in (1, 2, 3)]
        actual = [self.defaults.get("--pass2-tile"), self.defaults.get("--pass3-tile"),
                  self.defaults.get("--pass4-tile")]
        self.assertEqual(
            actual, documented,
            f"doc drift: SKILL.md documents pass2/3/4 tiles {documented}px, "
            f"argparse defaults are {actual}")
        k_match = re.search(r"K\s*=\s*(\d+)\s*/\s*(\d+)\s*/\s*(\d+)", self.skill_md)
        self.assertIsNotNone(
            k_match,
            "SKILL.md no longer states 'K=6/8/8 色 palette' for the local palettes")
        documented_k = [int(k_match.group(i)) for i in (1, 2, 3)]
        actual_k = [self.defaults.get("--pass2-k"), self.defaults.get("--pass3-k"),
                    self.defaults.get("--pass4-k")]
        self.assertEqual(
            actual_k, documented_k,
            f"doc drift: SKILL.md documents pass2/3/4 palettes K={documented_k}, "
            f"argparse defaults are {actual_k}")

    def test_documented_gate_numbers_match_defaults_if_mentioned(self):
        problems = []
        for option, expected in FROZEN_GRADIENT_DEFAULTS.items():
            if not isinstance(expected, float):
                continue
            match = re.search(re.escape(option) + r"[^\n]{0,140}?(\d+(?:\.\d+)?)",
                              self.docs)
            if match is None:
                continue  # docs may summarise without repeating the number
            documented = float(match.group(1))
            if abs(documented - expected) > 1e-9:
                problems.append(f"{option}: docs mention {documented} but argparse "
                                f"default is {expected}")
        self.assertFalse(problems, "documented gradient-gate defaults drifted:\n" +
                         "\n".join(problems))

    def test_new_gradient_options_documented(self):
        """v1.33 gate flags should reach SKILL.md / references (fixer-docs task)."""
        missing = [option for option in FROZEN_GRADIENT_DEFAULTS
                   if option not in self.docs]
        if missing:
            self.skipTest("SKILL.md/references do not document v1.33 options yet "
                          f"({', '.join(missing)}); fixer-docs deliverable pending")
        for option in ("--upper-gradient", "--base-color-mode", "--base-k",
                       "--mid-k", "--fine-k", "--base-jitter", "--mid-jitter",
                       "--fine-jitter"):
            self.assertIn(option, self.docs,
                          f"{option} documented nowhere in SKILL.md/references")

    def test_texture_flat_options_documented(self):
        """v1.34 opt-in flags should reach SKILL.md / references (fixer-docs v1.34)."""
        missing = [option for option in FROZEN_TEXTURE_FLAT_DEFAULTS
                   if option not in self.docs]
        if missing:
            self.skipTest("SKILL.md/references do not document v1.34 texture-flat "
                          "options yet "
                          f"({', '.join(missing)}); fixer-docs v1.34 pending")
        for option in ("--texture-flat", "--texture-flat-count",
                       "--texture-flat-ref-std", "--texture-flat-layer"):
            self.assertIn(option, self.docs,
                          f"{option} documented nowhere in SKILL.md/references")

    def test_base_coat_options_documented(self):
        """v1.35 underpainting flags should reach SKILL.md / references."""
        options = list(FROZEN_BASE_COAT_DEFAULTS) + ["--base-color-mode",
                                                     "--max-curves"]
        missing = [option for option in options if option not in self.docs]
        if missing:
            self.skipTest("SKILL.md/references do not document v1.35 underpainting "
                          "options yet "
                          f"({', '.join(missing)}); fixer-docs v1.35 pending")
        for option in ("--base-color", "--base-coat-angle", "--base-coat-spacing",
                       "--base-coat-width-factor", "--base-coat-jitter"):
            self.assertIn(option, self.docs,
                          f"{option} documented nowhere in SKILL.md/references")


    def test_human_paint_options_documented(self):
        """v1.36 flags should reach SKILL.md / references (fixer-docs v1.36)."""
        options = list(FROZEN_HUMAN_PAINT_DEFAULTS) + ["--paint-order"]
        missing = [option for option in options if option not in self.docs]
        if missing:
            self.skipTest("SKILL.md/references do not document v1.36 "
                          "human-paint options yet; fixer-docs v1.36 pending")
        for option in ("--base-coat-pattern", "--flow-strokes",
                       "--paint-order", "--region-count"):
            self.assertIn(option, self.docs,
                          f"{option} documented nowhere in SKILL.md/references")


    def test_silhouette_options_documented(self):
        """v1.37 flags should reach SKILL.md / references (fixer-docs v1.37)."""
        options = list(FROZEN_SILHOUETTE_DEFAULTS)
        missing = [option for option in options if option not in self.docs]
        if missing:
            self.skipTest("SKILL.md/references do not document v1.37 "
                          "silhouette options yet; fixer-docs v1.37 pending")
        for option in ("--silhouette", "--silhouette-shrink-px",
                       "--silhouette-margin-px",
                       "--silhouette-min-width-px",
                       "--silhouette-feather-px"):
            self.assertIn(option, self.docs,
                          f"{option} documented nowhere in SKILL.md/references")


    def test_gpu_render_options_documented(self):
        """v1.38 flags should reach SKILL.md / references (fixer-docs v1.38)."""
        options = ["--fdraw", "--fbs", "--strokes", "--resolution",
                   "--supersample", "--device", "--timing-json"]
        missing = [option for option in options if option not in self.docs]
        if missing:
            self.skipTest("SKILL.md/references do not document v1.38 "
                          "GPU render options yet; fixer-docs v1.38 pending")
        for option in options:
            self.assertIn(option, self.docs,
                          f"{option} documented nowhere in SKILL.md/references")


class TestDetailFilterContract(unittest.TestCase):
    """``--detail`` must drop old underpainting strokes (base_pass*/fix_*/soft_*).

    Pre-v1.33 ``DROP_DETAIL_KINDS`` used exact match only, so a layered_base
    strokes JSON passed back as ``--detail`` re-added every base_pass stroke.
    The fix is part of this release, so the test asserts the fixed behaviour.
    """

    ALL_KINDS = ["base_pass1", "base_pass2", "base_pass3", "base_pass4",
                 "base", "fix", "fix_extra", "soft_atmosphere", "soft_glaze",
                 "soft_extra", "detail", "tex", "hatch"]
    KEPT_KINDS = ["detail", "tex", "hatch"]
    WORK_RES = 96

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="detail_contract_")
        cls.addClassCleanup(cls._tmp.cleanup)
        root = cls._tmp.name
        cls.input_png = S.write_scene(os.path.join(root, "canvas.png"),
                                      S.scene_canvas(192, 144))
        cls.detail_json = os.path.join(root, "detail_strokes.json")
        payload = []
        for index, kind in enumerate(cls.ALL_KINDS):
            payload.append({
                "points": [[0.10 + 0.02 * index, 0.20], [0.20, 0.30], [0.30, 0.40]],
                "widths": [0.01, 0.01, 0.01],
                "color": [0.5, 0.5, 0.5],
                "alpha": 1.0, "layer": 5, "kind": kind, "closed": False,
            })
        with open(cls.detail_json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        cls.outdir = os.path.join(root, "out")
        S.run_layered_ok(
            cls, ["--input", cls.input_png, "--detail", cls.detail_json,
                  "--outdir", cls.outdir, "--work-res", str(cls.WORK_RES)],
            label="--detail contract")
        cls.report = S.load_json(os.path.join(cls.outdir, "layered_base_report.json"))
        cls.strokes = S.load_json(os.path.join(cls.outdir, "layered_base_strokes.json"))

    def test_detail_underpainting_strokes_are_dropped(self):
        kept = int(self.report["detail"])
        self.assertEqual(
            kept, len(self.KEPT_KINDS),
            f"--detail kept {kept} of {len(self.ALL_KINDS)} test strokes; expected "
            f"{len(self.KEPT_KINDS)} ({self.KEPT_KINDS}). base_pass*/fix_*/soft_* "
            "must be dropped (pre-v1.33 bug kept all of them)")
        kinds = [str(s.get("kind", "")) for s in self.strokes]
        for kind in self.KEPT_KINDS:
            self.assertIn(kind, kinds)
        pass_total = sum(int(self.report[key]) for key in
                         ("pass1_full", "pass2_local", "pass3_local", "pass4_local"))
        dropped = int(self.report.get("silhouette_dropped_strokes", 0))
        # v1.37 silhouette clipping removes halo strokes after generation.
        self.assertEqual(pass_total + kept - dropped,
                         int(self.report["strokes_out"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
