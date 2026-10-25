"""Documentation contract: SKILL.md fields, templates and cross-references."""

from __future__ import annotations

import unittest

from _support import EXAMPLES, SKILL_DIR, TEMPLATES, read_text

SKILL = SKILL_DIR / "SKILL.md"
README = SKILL_DIR / "README.md"


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Minimal ``key: value`` parser (no PyYAML); mirrors the DSH skill format."""
    if not text.startswith("---\n"):
        raise AssertionError("SKILL.md must start with a YAML frontmatter fence")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise AssertionError("SKILL.md frontmatter is not closed")
    front = text[4:end]
    body = text[end + 5 :]
    meta: dict[str, str] = {}
    for line in front.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise AssertionError(f"frontmatter line is not key:value: {line!r}")
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip()
    return meta, body


class SkillDocsCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read_text(SKILL)
        cls.meta, cls.body = parse_frontmatter(cls.text)
        cls.readme = read_text(README)
        cls.text_template = read_text(TEMPLATES / "text_to_drawing.md")
        cls.edit_template = read_text(TEMPLATES / "edit_prompt.md")

    def test_frontmatter_name_description_and_triggers(self):
        self.assertEqual(self.meta.get("name"), "fourier-prompt")
        description = self.meta.get("description", "")
        self.assertGreater(len(description), 40, description)
        for trigger in ("傅里叶提示词", "文生图", "FDSL", ".fdraw", "plan2fbs"):
            self.assertIn(trigger, description)
        self.assertIn("# fourier-prompt", self.body)
        self.assertNotIn("TODO", self.text)

    def test_command_surface_is_documented(self):
        for command in (
            "python -m fourier_prompt.cli",
            "plan2fbs",
            "draw",
            "fdraw_cli pack",
            "fdraw_cli unpack",
            "fdraw_cli inspect",
            "fdrender_cli render",
            "fdrender_cli preview",
            "fdrender_cli frame",
            "fdrender_cli compare",
            "fdvideo_cli video",
            "layer_cli export",
            "mcp_server",
            "scripts/demo.py",
        ):
            with self.subTest(command=command):
                self.assertIn(command, self.body)

    def test_limitations_are_explicit_and_not_overclaimed(self):
        for token in (
            "无本地 LLM",
            "无字体回退",
            "fdsl-edit/1",
            "PSD/KRA 未真机验证",
            "PSNR ≥ 30",
            "within2 ≥ 80%",
            "exact",
            "as: \"glyph\"",
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.body)
        # The skill must not promise the capabilities it explicitly lacks.
        for forbidden in ("自动调用本地模型", "自动回退系统字体", "支持原地编辑"):
            self.assertNotIn(forbidden, self.body)

    def test_templates_exist_and_are_wired(self):
        for path in (TEMPLATES / "text_to_drawing.md", TEMPLATES / "edit_prompt.md"):
            self.assertTrue(path.is_file(), path)
        self.assertIn("templates/text_to_drawing.md", self.body)
        self.assertIn("templates/edit_prompt.md", self.body)
        self.assertIn("templates/text_to_drawing.md", self.readme)
        self.assertIn("templates/edit_prompt.md", self.readme)

    def test_text_to_drawing_template_is_actionable(self):
        for token in ("fdsl/2.1", "schema --strict", "plan2fbs", "skill/examples/text_plan.json",
                      "没有本地 LLM", "brush.preset", "font"):
            with self.subTest(token=token):
                self.assertIn(token, self.text_template)
        self.assertIn("```json", self.text_template)

    def test_edit_template_declares_not_implemented(self):
        for token in ("未实现", "fdsl-edit/1", "不能原地", "unpack", "重新编译"):
            with self.subTest(token=token):
                self.assertIn(token, self.edit_template)

    def test_readme_quickstart_and_layout(self):
        for token in (
            "demo.py",
            "skill/examples/text_plan.json",
            "skill/examples/simple_plan.json",
            "FBS_SKILL_DIR",
            "unittest discover -s tests/skill",
            "text_plan.fbs",
            "text_plan.fdraw",
            "skill/templates/text_to_drawing.md",
            "skill/templates/edit_prompt.md",
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.readme)
        self.assertTrue((EXAMPLES / "README.md").is_file())


if __name__ == "__main__":
    unittest.main()
