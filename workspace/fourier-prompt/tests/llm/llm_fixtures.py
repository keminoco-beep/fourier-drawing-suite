"""Shared helpers for the WP-O LLM tests (no torch, no GPU, no network)."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for candidate in (str(HERE), str(ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)


def valid_plan():
    """A small FDSL v2.1 plan that validates *and* compiles (no text/fonts)."""
    return {
        "schema": "fdsl/2.1",
        "name": "llm-fixture",
        "canvas": {"width": 256, "height": 256, "background": [255, 255, 255]},
        "palette": {"ink": "#12203a", "gold": "#e8c37a"},
        "layers": [{"id": 0, "name": "base"}],
        "elements": [
            {
                "kind": "fourier_gen",
                "id": "e1",
                "layer": 0,
                "gen": "circle",
                "transform": {"center": [128, 128], "scale": 0.5},
                "params": {"r": 1.0},
                "brush": {"preset": "ink_fineliner", "width": 2.0, "color": "ink"},
            },
            {
                "kind": "stroke",
                "id": "e2",
                "layer": 0,
                "points": [[16, 240], [240, 16]],
                "brush": {"preset": "ink_gpen", "width": 1.5},
            },
        ],
        "texts": [],
        "constraints": {"max_elements": 400},
    }


def clone(plan):
    return copy.deepcopy(plan)


class FakeTokenizer:
    """Character-level tokenizer with a deterministic chat template."""

    eos_token = "<|endoftext|>"
    pad_token_id = 0
    eos_token_id = 1

    def __init__(self):
        self.chat_calls = 0

    def encode(self, text, add_special_tokens=False):
        return [2] + [3 + (ord(char) % 97) for char in text]

    def apply_chat_template(self, messages, *, tokenize=False, add_generation_prompt=False):
        self.chat_calls += 1
        parts = [f"<|{message['role']}|>\n{message['content']}\n" for message in messages]
        if add_generation_prompt:
            parts.append("<|assistant|>\n")
        return "".join(parts)


class PlainTokenizer:
    """Tokenizer without ``apply_chat_template`` (fallback rendering path)."""

    eos_token = "</s>"
    pad_token_id = 0
    eos_token_id = 1

    def encode(self, text, add_special_tokens=False):
        return [5] + [6 + (ord(char) % 89) for char in text]


class FakePlanner:
    """Rule-planner stand-in returning a valid plan (optionally failing)."""

    def __init__(self, plan=None, *, fail_prompts=(), fail_exc=None, return_tuple=True):
        self.plan = plan
        self.calls = []
        self.fail_prompts = set(fail_prompts)
        self.fail_exc = fail_exc
        self.return_tuple = return_tuple

    def __call__(self, prompt, seed=0):
        self.calls.append((prompt, seed))
        if prompt in self.fail_prompts:
            raise self.fail_exc or RuntimeError(f"planner exploded on {prompt!r}")
        plan = clone(self.plan) if self.plan is not None else valid_plan()
        plan["name"] = f"plan-{seed}"
        if self.return_tuple:
            return plan, []
        return plan


def bad_plan():
    """Missing canvas: repair_plan must reject it."""
    return {
        "schema": "fdsl/2.1",
        "name": "broken",
        "palette": {"ink": "#12203a"},
        "layers": [{"id": 0}],
        "elements": [{"kind": "stroke", "id": "e1", "layer": 0, "points": [[1, 2], [3, 4]]}],
    }


def write_jsonl(path, records):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def load_script(name):
    """Import ``scripts/<name>.py`` by path (scripts/ is not a package)."""
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"wp_o_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_jsonl(path):
    records = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def clear_llm_env(monkeypatch=None):
    """Remove ``FDP_LLM_MODEL`` for tests that rely on the default path."""
    os.environ.pop("FDP_LLM_MODEL", None)
