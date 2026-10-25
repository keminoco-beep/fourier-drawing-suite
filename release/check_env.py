#!/usr/bin/env python
"""Environment self-check for a ComfyUI-FDP-T2I release package.

Checks the publish layout, the ComfyUI node install, the execution python
(numpy/scipy/Pillow required; imageio/video and OpenGL optional), the portable
``fdp_config.json`` and the workflow JSON files.  Prints PASS / WARN / FAIL and
actionable fixes; exit code 0 = no FAIL, 1 = at least one FAIL.

Examples
--------
    python release/check_env.py --comfyui "D:/ComfyUI"
    python release/check_env.py            # packaged layout auto-detect
    python release/check_env.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

NODE_DIR_NAME = "ComfyUI-FDP-T2I"
CORE_MODULES = [("numpy", True), ("scipy", True), ("PIL", True),
                ("imageio", False), ("imageio_ffmpeg", False)]


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []

    def add(self, status: str, name: str, detail: str, fix: str = "") -> None:
        self.rows.append((status, name, detail, fix))

    def ok(self, name: str, detail: str = "") -> None:
        self.add("PASS", name, detail)

    def warn(self, name: str, detail: str, fix: str = "") -> None:
        self.add("WARN", name, detail, fix)

    def fail(self, name: str, detail: str, fix: str = "") -> None:
        self.add("FAIL", name, detail, fix)

    def counts(self) -> tuple[int, int, int]:
        return tuple(sum(1 for r in self.rows if r[0] == s) for s in
                     ("PASS", "WARN", "FAIL"))  # type: ignore[return-value]

    def print(self) -> int:
        p, w, f = self.counts()
        width = max((len(r[1]) for r in self.rows), default=10)
        print("=" * (width + 62))
        print(f"ComfyUI-FDP-T2I environment check  (PASS={p} WARN={w} FAIL={f})")
        print("=" * (width + 62))
        for status, name, detail, fix in self.rows:
            print(f"[{status:4}] {name:<{width}}  {detail}")
            if fix and status != "PASS":
                print(f"        -> {fix}")
        print("-" * (width + 62))
        if f:
            print("结果：FAIL —— 请按上面的 -> 修复后重跑 check_env.py")
        elif w:
            print("结果：PASS（有 WARN；GPU/视频等功能可能不可用，其它功能正常）")
        else:
            print("结果：PASS —— 发布包可运行")
        return 0 if f == 0 else 1

    def as_json(self) -> dict:
        p, w, f = self.counts()
        return {"summary": {"pass": p, "warn": w, "fail": f},
                "rows": [{"status": s, "name": n, "detail": d, "fix": x}
                         for s, n, d, x in self.rows]}


def _is_workspace(p: Path) -> bool:
    return (p / "skills" / "fourier-multistroke-training" / "scripts"
            / "layered_base.py").is_file()


def _resolve_workspace(args: argparse.Namespace) -> tuple[Path | None, str]:
    cands: list[tuple[Path, str]] = []
    if args.workspace:
        cands.append((Path(args.workspace).expanduser(), "--workspace"))
    env = os.environ.get("FDP_WORKSPACE")
    if env:
        cands.append((Path(env).expanduser(), "FDP_WORKSPACE"))
    release_dir = Path(__file__).resolve().parent
    root = release_dir.parent
    cands.extend([(root / "workspace", "release 布局 <root>/workspace"),
                  (root, "release 布局 <root>")])
    if args.comfyui:
        b = Path(args.comfyui).expanduser().resolve()
        cands.extend([(b.parent / "workspace", "ComfyUI 同级 workspace"),
                      (b.parent, "ComfyUI 上级根目录")])
    for c, how in cands:
        try:
            c = c.resolve()
        except Exception:
            continue
        if _is_workspace(c):
            return c, how
    return None, ""


def _resolve_node(args: argparse.Namespace) -> tuple[Path | None, str]:
    if args.node_dir:
        p = Path(args.node_dir).expanduser().resolve()
        return (p, "--node-dir") if (p / "nodes.py").is_file() else (None, "")
    release_dir = Path(__file__).resolve().parent
    root = release_dir.parent
    cands: list[tuple[Path, str]] = []
    if args.comfyui:
        c = Path(args.comfyui).expanduser().resolve()
        cands.append((c / "custom_nodes" / NODE_DIR_NAME, "--comfyui 安装目录"))
    cands.extend([(root / "comfyui" / "custom_nodes" / NODE_DIR_NAME, "release 布局"),
                  (root / "fourier-prompt" / "comfyui" / NODE_DIR_NAME, "开发布局")])
    for c, how in cands:
        if (c / "nodes.py").is_file():
            return c.resolve(), how
    return None, ""


def _resolve_python(args: argparse.Namespace, node: Path | None,
                    workspace: Path | None) -> tuple[str | None, str]:
    cands: list[tuple[str, str]] = []
    if args.python:
        cands.append((args.python, "--python"))
    env = os.environ.get("FDP_PYTHON")
    if env:
        cands.append((env, "FDP_PYTHON"))
    if node is not None:
        cfg = _load_node_config(node)
        if cfg.get("python"):
            cands.append((str(cfg["python"]), "节点 fdp_config.json"))
    if workspace is not None:
        pass
    if args.comfyui:
        c = Path(args.comfyui).expanduser().resolve()
        for guess in (c.parent / "python" / "python.exe",
                      c.parent / "python_embeded" / "python.exe",
                      c / "python" / "python.exe"):
            if guess.is_file():
                cands.append((str(guess), "ComfyUI 自带 python"))
                break
    cands.append((sys.executable, "当前解释器"))
    for py, how in cands:
        py = os.path.expanduser(str(py))
        if os.path.isfile(py):
            return py, how
    return None, ""


def _load_node_config(node: Path) -> dict:
    for cand in (Path(os.environ.get("FDP_CONFIG", "")) if os.environ.get("FDP_CONFIG") else None,
                 node / "fdp_config.json"):
        if cand and Path(cand).is_file():
            try:
                data = json.loads(Path(cand).read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                continue
    return {}


def _check_python_deps(py: str) -> tuple[list[str], list[str]]:
    code = ("import importlib.util as u\n"
            "mods=" + repr([m for m, _ in CORE_MODULES]) + "\n"
            "miss=[m for m in mods if u.find_spec(m) is None]\n"
            "print(','.join(miss))")
    try:
        proc = subprocess.run([py, "-c", code], capture_output=True, text=True,
                              timeout=90, encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return [f"无法运行 {py}: {exc}"], []
    if proc.returncode != 0:
        return [f"{py} 运行失败: {(proc.stderr or '').strip()[-300:]}"], []
    missing = [m.strip() for m in (proc.stdout or "").strip().split(",") if m.strip()]
    fatal = [m for m in missing if dict(CORE_MODULES).get(m, False)]
    optional = [m for m in missing if m not in fatal]
    return fatal, optional


def _check_gl(py: str) -> tuple[bool, str]:
    code = r"""
import os
os.environ.setdefault('PYOPENGL_PLATFORM', '')
try:
    import glfw
    from OpenGL import GL
except Exception as exc:
    print('IMPORT_FAIL:' + repr(exc))
    raise SystemExit(0)
if not glfw.init():
    print('GLFW_INIT_FAIL')
    raise SystemExit(0)
glfw.window_hint(glfw.VISIBLE, glfw.FALSE)
glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
win = glfw.create_window(32, 32, 'check', None, None)
if not win:
    print('WINDOW_FAIL')
    glfw.terminate()
    raise SystemExit(0)
glfw.make_context_current(win)
print('OK:' + GL.glGetString(GL.GL_RENDERER).decode(errors='replace'))
glfw.terminate()
"""
    try:
        proc = subprocess.run([py, "-c", code], capture_output=True, text=True,
                              timeout=90, encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return False, f"GL 检查无法运行: {exc}"
    out = (proc.stdout or "").strip()
    if out.startswith("OK:"):
        return True, out[3:].strip()
    return False, out or (proc.stderr or "").strip()[-300:]


def _scan_abs_paths(paths: list[Path], report: Report) -> None:
    bad: list[str] = []
    # 动态拼接，避免检查器源码自身包含被扫描的绝对路径字面量
    markers = ("G:" + "\\AI" + "\\", "C:" + "\\Users" + "\\",
               "G:" + "/AI/", "C:" + "/Users/")
    for root in paths:
        if not root.exists():
            continue
        files = [root] if root.is_file() else list(root.rglob("*"))
        for p in files:
            if not p.is_file() or p.suffix.lower() not in (
                    ".py", ".json", ".md", ".txt", ".yaml", ".yml", ".cfg", ".toml"):
                continue
            if any(part in ("__pycache__", ".git", "tests") for part in p.parts):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for marker in markers:
                if marker in text:
                    bad.append(str(p))
                    break
    if bad:
        report.fail("源码不含本机绝对路径",
                    f"{len(bad)} 个文件含本机绝对路径",
                    "发布前清理：" + "; ".join(bad[:5]))
    else:
        report.ok("源码不含本机绝对路径", "已扫描 skills / fourier-* / 节点包")


def _safe_stdio() -> None:
    """Windows GBK 控制台下不可编码字符替换而不是崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def main() -> int:
    _safe_stdio()
    ap = argparse.ArgumentParser(description="ComfyUI-FDP-T2I 发布包环境自检")
    ap.add_argument("--comfyui", default=None, help="ComfyUI 根目录（检查节点是否已安装）")
    ap.add_argument("--workspace", default=None, help="发布包根目录/workspace 目录")
    ap.add_argument("--python", default=None, help="执行 skill 脚本的 python（默认 ComfyUI 自带）")
    ap.add_argument("--node-dir", default=None, help="直接指定节点包目录（调试用）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    ap.add_argument("--skip-gl", action="store_true", help="跳过 OpenGL 上下文检查")
    args = ap.parse_args()

    rep = Report()
    workspace, ws_how = _resolve_workspace(args)
    if workspace is None:
        rep.fail("workspace 解析", "找不到发布包 workspace（skills/ 目录）",
                 "用 --workspace 指向发布包根目录，或设置 FDP_WORKSPACE")
    else:
        rep.ok("workspace 解析", f"{workspace}（{ws_how}）")
        layered = workspace / "skills" / "fourier-multistroke-training" / "scripts" / "layered_base.py"
        build = workspace / "skills" / "fourier-multistroke-training" / "scripts" / "build_strokes_fbs.py"
        fbs = workspace / "fourier-brushstroke" / "scripts" / "fbs.py"
        fbs_core = workspace / "fourier-brushstroke" / "scripts" / "fbs_core" / "render.py"
        fdraw = workspace / "fourier-prompt" / "fourier_prompt" / "fdraw_cli.py"
        fdrend = workspace / "fourier-prompt" / "fourier_prompt" / "fdrender_cli.py"
        checks = [
            ("v1.36 绘制脚本", [layered, build]),
            ("fourier-brushstroke", [fbs, fbs_core]),
            ("fourier-prompt", [fdraw, fdrend]),
        ]
        for name, paths in checks:
            missing = [str(p) for p in paths if not p.is_file()]
            if missing:
                rep.fail(name, "缺少：" + ", ".join(missing), "发布包不完整，请重新解压")
            else:
                rep.ok(name, f"{len(paths)} 个入口文件存在")

    node, node_how = _resolve_node(args)
    if node is None:
        rep.warn("ComfyUI 节点包", "未找到已安装的 ComfyUI-FDP-T2I 节点",
                 "先运行：python release/install.py --comfyui <ComfyUI目录>")
    else:
        rep.ok("ComfyUI 节点包", f"{node}（{node_how}）")
        nodes_py = node / "nodes.py"
        version = ""
        try:
            text = nodes_py.read_text(encoding="utf-8")
            for line in text.splitlines():
                if "PACKAGE_VERSION" in line and "\"" in line:
                    version = line.split("\"")[1]
                    break
        except Exception:
            pass
        if version:
            rep.ok("节点版本", f"PACKAGE_VERSION={version}")
        else:
            rep.warn("节点版本", "nodes.py 中未找到 PACKAGE_VERSION")
        cfg = _load_node_config(node)
        if cfg:
            cfg_ws = cfg.get("workspace")
            if cfg_ws and _is_workspace(Path(str(cfg_ws)).expanduser()):
                rep.ok("fdp_config.json workspace", str(cfg_ws))
            elif cfg_ws:
                rep.fail("fdp_config.json workspace", f"配置的 workspace 无效：{cfg_ws}",
                         "重跑 install.py 或修正节点目录下 fdp_config.json")
            else:
                rep.warn("fdp_config.json", "配置存在但没有 workspace 字段",
                         "重跑 install.py 生成")
        else:
            rep.warn("fdp_config.json", "节点目录没有 fdp_config.json",
                     "运行 python release/install.py 生成（或设置 FDP_WORKSPACE）")

    py, py_how = _resolve_python(args, node, workspace)
    if py is None:
        rep.fail("执行 python", "找不到可用的 python", "用 --python 指定 ComfyUI 自带 python")
    else:
        fatal, optional = _check_python_deps(py)
        if fatal:
            rep.fail("python 核心依赖", f"{py} 缺少 {fatal}", "运行 release/requirements-core.txt")
        else:
            rep.ok("python 核心依赖", f"{py}（{py_how}）numpy/scipy/Pillow 就绪")
        if optional:
            rep.warn("python 可选依赖", f"缺少 {optional}（视频/序列帧功能不可用）",
                     "pip install imageio imageio-ffmpeg")
        if not args.skip_gl:
            gl_ok, gl_detail = _check_gl(py)
            if gl_ok:
                rep.ok("OpenGL 上下文（GPU 渲染）", gl_detail)
            else:
                rep.warn("OpenGL 上下文（GPU 渲染）", gl_detail,
                         "GPU 渲染不可用，渲染将自动回退 CPU（或安装 requirements-gpu.txt）")

    wf_dirs = []
    release_dir = Path(__file__).resolve().parent
    root = release_dir.parent
    if args.comfyui:
        c = Path(args.comfyui).expanduser().resolve()
        wf_dirs.append(c / "user" / "default" / "workflows")
        wf_dirs.append(c / "workflows")
    wf_dirs.extend([root / "comfyui" / "workflows",
                    root / "fourier-prompt" / "comfyui" / "ComfyUI-FDP-T2I" / "workflows"])
    if node is not None:
        wf_dirs.append(node / "workflows")
    found = 0
    bad: list[str] = []
    for d in wf_dirs:
        if not d.is_dir():
            continue
        for p in d.glob("*.json"):
            found += 1
            try:
                json.loads(p.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                bad.append(f"{p.name}: {exc}")
    if bad:
        rep.fail("workflow JSON", "; ".join(bad[:3]), "workflow 文件损坏，请重新解压发布包")
    elif found:
        rep.ok("workflow JSON", f"{found} 个可解析的 workflow 文件")
    else:
        rep.warn("workflow JSON", "没有找到 workflow 文件（不影响节点使用）")

    _scan_abs_paths([p for p in (workspace, node) if p is not None], rep)

    if args.json:
        print(json.dumps(rep.as_json(), ensure_ascii=False, indent=2))
        return 0 if rep.counts()[2] == 0 else 1
    return rep.print()


if __name__ == "__main__":
    sys.exit(main())
