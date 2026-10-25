#!/usr/bin/env python
"""Uninstall ComfyUI-FDP-T2I from a ComfyUI custom_nodes directory.

Removes only the node package directory (and its generated fdp_config.json).
The release workspace and ComfyUI itself are untouched.

    python release/uninstall.py --comfyui "D:/ComfyUI" --dry-run
    python release/uninstall.py --comfyui "D:/ComfyUI" --yes
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

NODE_DIR_NAME = "ComfyUI-FDP-T2I"


def _safe_stdio() -> None:
    """Windows GBK 控制台下不可编码字符替换而不是崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def main() -> int:
    _safe_stdio()
    ap = argparse.ArgumentParser(description="卸载 ComfyUI-FDP-T2I 节点包")
    ap.add_argument("--comfyui", required=True, help="ComfyUI 根目录")
    ap.add_argument("--yes", action="store_true", help="确认删除（不加则只打印）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不删除")
    args = ap.parse_args()

    target = Path(args.comfyui).expanduser().resolve() / "custom_nodes" / NODE_DIR_NAME
    print(f"[uninstall] 目标：{target}")
    if not target.exists():
        print("[uninstall] 节点包不存在，无需卸载。")
        return 0
    if args.dry_run or not args.yes:
        print("[uninstall] 未删除（--dry-run 或缺少 --yes）。确认后加 --yes 重新运行。")
        return 0
    shutil.rmtree(target)
    print("[uninstall] 已删除节点包；重启 ComfyUI 生效。")
    print("[uninstall] 提示：如不再使用发布包，workspace/ 目录可自行删除。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
