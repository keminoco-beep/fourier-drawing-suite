"""Shared helpers for the ``tests/skill`` suite (WP-M)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = PKG_ROOT / "skill"
EXAMPLES = SKILL_DIR / "examples"
TEMPLATES = SKILL_DIR / "templates"
DEMO = PKG_ROOT / "scripts" / "demo.py"

if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))


def run_demo(*args, timeout=180, cwd=None) -> subprocess.CompletedProcess:
    """Run ``scripts/demo.py`` in a subprocess (default cwd: package root)."""
    cmd = [sys.executable, str(DEMO), *[str(arg) for arg in args]]
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join([str(PKG_ROOT)] + ([existing] if existing else []))
    return subprocess.run(
        cmd,
        cwd=str(cwd or PKG_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def load_example(name: str) -> dict:
    with open(EXAMPLES / name, "r", encoding="utf-8") as handle:
        return json.load(handle)


def sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_text(path) -> str:
    return Path(path).read_text(encoding="utf-8")
