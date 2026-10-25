#!/usr/bin/env python
"""ComfyUI-FDP-T2I release installer.

Installs the node package into a ComfyUI ``custom_nodes`` directory and writes
the portable ``fdp_config.json`` (workspace / python) that the node uses to
locate the release package when it runs.  Cross-platform, no third-party deps.

Examples
--------
    python release/install.py --comfyui "D:/ComfyUI"
    python release/install.py --comfyui "D:/ComfyUI" --workspace "D:/FDP" --python "D:/ComfyUI/python/python.exe"
    python release/install.py --comfyui "D:/ComfyUI" --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

NODE_DIR_NAME = "ComfyUI-FDP-T2I"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".github", "*.log")


def _is_workspace(p: Path) -> bool:
    return (p / "skills" / "fourier-multistroke-training" / "scripts"
            / "layered_base.py").is_file()


def _resolve_workspace(release_dir: Path, cli_value: str | None) -> Path:
    cands: list[Path] = []
    if cli_value:
        cands.append(Path(cli_value).expanduser())
    env = os.environ.get("FDP_WORKSPACE")
    if env:
        cands.append(Path(env).expanduser())
    root = release_dir.parent
    cands.extend([root / "workspace", root])
    for c in cands:
        try:
            c = c.resolve()
        except Exception:
            continue
        if _is_workspace(c):
            return c
    raise SystemExit(
        "找不到发布包 workspace（需要 <root>/workspace/skills/"
        "fourier-multistroke-training/scripts/layered_base.py）。\n"
        "请用 --workspace 指向发布包根目录（或其中的 workspace 目录）。\n"
        f"已尝试：{[str(c) for c in cands]}"
    )


def _resolve_node_source(release_dir: Path) -> Path:
    root = release_dir.parent
    cands = [
        release_dir / "comfyui" / "custom_nodes" / NODE_DIR_NAME,
        root / "comfyui" / "custom_nodes" / NODE_DIR_NAME,
        root / "fourier-prompt" / "comfyui" / NODE_DIR_NAME,
    ]
    env = os.environ.get("FDP_NODE_SOURCE")
    if env:
        cands.insert(0, Path(env).expanduser())
    for c in cands:
        if (c / "nodes.py").is_file():
            return c.resolve()
    raise SystemExit(f"找不到节点包源目录（含 nodes.py）。已尝试：{[str(c) for c in cands]}")


def _python_deps(py: str) -> list[str]:
    code = ("import importlib.util as u;"
            "mods=['numpy','scipy','PIL'];"
            "print(','.join(m for m in mods if u.find_spec(m) is None))")
    try:
        proc = subprocess.run([py, "-c", code], capture_output=True, text=True,
                              timeout=60, encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return [f"无法运行 {py}: {exc}"]
    if proc.returncode != 0:
        return [f"{py} 运行失败: {(proc.stderr or '').strip()[-300:]}"]
    return [m.strip() for m in (proc.stdout or "").strip().split(",") if m.strip()]


def _guess_comfyui_python(comfyui: Path) -> str | None:
    cands = [
        comfyui.parent / "python" / "python.exe",       # ComfyUI-aki / 便携版
        comfyui.parent / "python_embeded" / "python.exe",  # ComfyUI portable zip
        comfyui / "python" / "python.exe",
        comfyui.parent / "python.exe",
    ]
    for c in cands:
        if c.is_file():
            return str(c.resolve())
    return None


def _resolve_python(comfyui: Path, cli_value: str | None, workspace: Path | None) -> str:
    cands: list[str] = []
    if cli_value:
        cands.append(cli_value)
    env = os.environ.get("FDP_PYTHON")
    if env:
        cands.append(env)
    guessed = _guess_comfyui_python(comfyui)
    if guessed:
        cands.append(guessed)
    cands.append(sys.executable)
    first_bad = None
    for py in cands:
        py = os.path.expanduser(str(py))
        if not os.path.isfile(py):
            first_bad = first_bad or (py, [f"python 不存在: {py}"])
            continue
        missing = _python_deps(py)
        if not missing:
            return os.path.abspath(py)
        first_bad = first_bad or (py, missing)
    py, missing = first_bad or (sys.executable, ["未知"])
    print(f"[install] WARNING: 没有找到依赖完整的 python（最后尝试 {py}: {missing}）")
    if workspace is not None:
        req = workspace.parent / "release" / "requirements-core.txt"
        if not req.is_file():
            req = Path(__file__).resolve().parent / "requirements-core.txt"
        print(f"[install] 请先安装核心依赖：\n  \"{py}\" -m pip install -r \"{req}\"")
    return os.path.abspath(py)


def _safe_stdio() -> None:
    """Windows GBK 控制台下不可编码字符替换而不是崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def main() -> int:
    _safe_stdio()
    ap = argparse.ArgumentParser(
        description="Install ComfyUI-FDP-T2I into a ComfyUI directory (portable config)")
    ap.add_argument("--comfyui", required=True,
                    help="ComfyUI 根目录（其下应有 custom_nodes/）")
    ap.add_argument("--workspace", default=None,
                    help="发布包根目录（含 skills/、fourier-brushstroke/、fourier-prompt/）")
    ap.add_argument("--python", default=None,
                    help="执行 skill 脚本的 python（默认 ComfyUI 自带 python / sys.executable）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的操作，不写文件")
    args = ap.parse_args()

    release_dir = Path(__file__).resolve().parent
    comfyui = Path(args.comfyui).expanduser().resolve()
    if not comfyui.is_dir() and not args.dry_run:
        raise SystemExit(f"ComfyUI 目录不存在：{comfyui}")

    workspace = _resolve_workspace(release_dir, args.workspace)
    node_src = _resolve_node_source(release_dir)
    target_dir = comfyui / "custom_nodes" / NODE_DIR_NAME
    if args.dry_run and not comfyui.is_dir() and not args.python:
        py = sys.executable  # dry-run 不强制目标已存在（仅展示计划）
    else:
        py = _resolve_python(comfyui, args.python, workspace)

    actions: list[str] = []
    actions.append(f"复制节点包：{node_src} -> {target_dir}")
    actions.append(f"写入配置：{target_dir / 'fdp_config.json'} = "
                   f'{{"workspace": "{workspace}", "python": "{py}"}}')
    print("[install] 安装计划：")
    for a in actions:
        print("  -", a)
    if args.dry_run:
        print("[install] --dry-run：未写入任何文件。")
        return 0

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    if target_dir.exists():
        shutil.rmtree(target_dir)
    shutil.copytree(node_src, target_dir, ignore=IGNORE)
    config = {"workspace": str(workspace), "python": py}
    (target_dir / "fdp_config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    req_core = release_dir / "requirements-core.txt"
    req_gpu = release_dir / "requirements-gpu.txt"
    print("[install] 完成。后续步骤：")
    print(f"  1) 安装核心依赖（如果 ComfyUI python 还没有）：\n"
          f"     \"{py}\" -m pip install -r \"{req_core}\"")
    print(f"  2) 可选 GPU 渲染依赖（PyOpenGL/glfw；缺失会自动回退 CPU）：\n"
          f"     \"{py}\" -m pip install -r \"{req_gpu}\"")
    print("  3) 重启 ComfyUI，导入 comfyui/workflows/ 下的 workflow JSON（或在 "
          "ComfyUI 的 workflow 列表导入发布包里的文件）。")
    print(f"  4) 自检：python \"{release_dir / 'check_env.py'}\" --comfyui \"{comfyui}\"")
    print("  环境变量覆盖：FDP_WORKSPACE / FDP_PYTHON / FDP_GPU_RENDER=0（强制 CPU 渲染）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
