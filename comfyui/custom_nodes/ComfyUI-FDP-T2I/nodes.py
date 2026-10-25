"""ComfyUI-FDP-T2I — 「Krea2 扩散出图 / 手动导图 → 傅里叶笔触 → .fdraw」全链路节点。

设计原则（2026-10 重构）：
- 入口只有一个「模式选择」节点：文生图（Krea2）/ 图生图（手动导入），
  同时创建本次任务的唯一输出目录，所有产物都落在这个目录里。
- lazy switch：选图生图时 Krea2 整条分支不会被加载/采样。
- 节点参数精简为高质量预设；路径、Python、采样器等全部内置。
- 图生图打包 .fdraw 时不写自定义 provenance meta（容器自带的最小 meta 仍保留）。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import numpy as np
from PIL import Image

try:  # ComfyUI 运行时一定存在；独立导入时允许缺失
    import folder_paths  # type: ignore
except Exception:  # pragma: no cover
    folder_paths = None  # type: ignore

# ---------------------------------------------------------------------------
# 可移植路径解析（发布包 v3.1.0）
# workspace: FDP_WORKSPACE env -> 节点目录 fdp_config.json -> 发布包布局自动探测
# python:    FDP_PYTHON env -> fdp_config.json python -> 当前解释器 sys.executable
# 二者都带可操作报错；发布源码中不允许出现本机绝对路径。
# ---------------------------------------------------------------------------
_CONFIG_NAME = "fdp_config.json"
_PY_DEP_CACHE: dict[str, list[str]] = {}


def _node_dir() -> Path:
    return Path(__file__).resolve().parent


def _load_config() -> dict:
    """读取 FDP_CONFIG 或节点目录下的 fdp_config.json（installer 生成）。"""
    candidates: list[Path] = []
    env_cfg = os.environ.get("FDP_CONFIG")
    if env_cfg:
        candidates.append(Path(env_cfg).expanduser())
    candidates.append(_node_dir() / _CONFIG_NAME)
    for path in candidates:
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            continue
    return {}


def _is_workspace(p: Path) -> bool:
    return (p / "skills" / "fourier-multistroke-training" / "scripts"
            / "layered_base.py").is_file()


def _workspace_candidates() -> list[Path]:
    cands: list[Path] = []
    env_ws = os.environ.get("FDP_WORKSPACE")
    if env_ws:
        cands.append(Path(env_ws).expanduser())
    cfg_ws = _load_config().get("workspace")
    if cfg_ws:
        cands.append(Path(str(cfg_ws)).expanduser())
    here = _node_dir()
    # <root>/comfyui/custom_nodes/<pkg> -> try <root>, <root>/workspace, ...
    for base in (here, here.parent, here.parent.parent, here.parent.parent.parent,
                 here.parent.parent.parent.parent):
        cands.extend([base, base / "workspace"])
    for env_name in ("COMFYUI_DIR", "COMFYUI_PATH", "FDP_COMFYUI"):
        v = os.environ.get(env_name)
        if v:
            b = Path(v).expanduser()
            cands.extend([b.parent, b.parent.parent])
    seen: set[str] = set()
    out: list[Path] = []
    for c in cands:
        try:
            key = str(c.resolve())
        except Exception:
            key = str(c)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def workspace_root() -> Path:
    """发布包工作区根目录（含 skills/ 与 fourier-* 目录）。"""
    tried: list[str] = []
    for cand in _workspace_candidates():
        try:
            resolved = cand.resolve()
        except Exception:
            continue
        if _is_workspace(resolved):
            return resolved
        tried.append(str(resolved))
    shown = "\n".join(f"  - {t}" for t in tried[:10])
    raise RuntimeError(
        "找不到 FDP workspace（应包含 skills/fourier-multistroke-training/scripts/"
        "layered_base.py）。\n"
        "解析顺序：FDP_WORKSPACE 环境变量 → 节点目录 fdp_config.json 的 workspace → "
        "发布包自动探测。\n"
        f"已尝试：\n{shown or '  (无)'}\n"
        "修复：运行 `python release/install.py --comfyui <ComfyUI目录>` 生成 "
        "fdp_config.json，或设置环境变量 FDP_WORKSPACE=<发布包根目录>/workspace。"
    )


def _python_missing_deps(py: str) -> list[str]:
    if py in _PY_DEP_CACHE:
        return _PY_DEP_CACHE[py]
    code = ("import importlib.util as u;"
            "mods=['numpy','scipy','PIL'];"
            "print(','.join(m for m in mods if u.find_spec(m) is None))")
    try:
        proc = subprocess.run([py, "-c", code], capture_output=True, text=True,
                              timeout=60, encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        missing = [f"无法运行 {py}: {exc}"]
    else:
        if proc.returncode != 0:
            missing = [f"{py} 运行失败: {(proc.stderr or '').strip()[-300:]}"]
        else:
            missing = [m.strip() for m in (proc.stdout or "").strip().split(",") if m.strip()]
    _PY_DEP_CACHE[py] = missing
    return missing


def workspace_python() -> str:
    """执行 skill 脚本用的 Python（默认 ComfyUI 自带 python）及其依赖校验。"""
    cfg = _load_config()
    candidates: list[str] = []
    env_py = os.environ.get("FDP_PYTHON")
    if env_py:
        candidates.append(os.path.expanduser(env_py))
    if cfg.get("python"):
        candidates.append(os.path.expanduser(str(cfg["python"])))
    candidates.append(sys.executable)
    first_bad: tuple[str, list[str]] | None = None
    for py in candidates:
        if not py:
            continue
        if not os.path.isfile(py):
            first_bad = first_bad or (py, [f"python 不存在: {py}"])
            continue
        missing = _python_missing_deps(py)
        if not missing:
            return py
        first_bad = first_bad or (py, missing)
    py, missing = first_bad or (sys.executable, ["未知"])
    try:
        req = workspace_root() / "release" / "requirements-core.txt"
        hint = f'  "{py}" -m pip install -r "{req}"'
    except Exception:
        hint = f'  "{py}" -m pip install -r requirements-core.txt'
    raise RuntimeError(
        "FDP python 缺少核心依赖：" + ", ".join(missing) + "\n"
        "解析顺序：FDP_PYTHON 环境变量 → fdp_config.json 的 python → 当前解释器 "
        "sys.executable。\n"
        f"修复：\n{hint}\n"
        "（ComfyUI 自带 python 通常已包含 numpy/scipy/Pillow）"
    )


def _py() -> str:
    return workspace_python()


# --------------------------------------------------------------------------- 工具
def _workspace_paths() -> tuple[Path, Path, Path, Path, Path]:
    ws = workspace_root()
    fbs_py = ws / "fourier-brushstroke" / "scripts" / "fbs.py"
    prompt_dir = ws / "fourier-prompt"
    fdrender = prompt_dir / "fourier_prompt" / "fdrender_cli.py"
    video_py = ws / "poc" / "t2i" / "fdraw_fourier_video.py"
    missing = [str(p) for p in (fbs_py, fdrender) if not p.is_file()]
    if missing:
        raise RuntimeError(
            "FDP workspace 结构不完整，缺少：\n" + "\n".join(missing)
            + "\n请确认发布包包含 fourier-brushstroke/ 与 fourier-prompt/；"
              "运行 `python release/check_env.py` 查看详情。")
    return ws, fbs_py, prompt_dir, fdrender, video_py


def _multistroke_paths() -> tuple[Path, Path]:
    """v1.36 人类画师式纯笔画流水线（fourier-multistroke-training，全本地无 AI）。"""
    scripts = workspace_root() / "skills" / "fourier-multistroke-training" / "scripts"
    layered = scripts / "layered_base.py"
    build = scripts / "build_strokes_fbs.py"
    if not layered.is_file() or not build.is_file():
        raise RuntimeError(
            f"找不到 v1.36 脚本：{layered} / {build}\n"
            "请确认发布包 workspace/skills/ 完整，或运行 `python release/check_env.py` 自检。")
    return layered, build


FBS_PATH = "FBS_PATH"
FDRAW_PATH = "FDRAW_PATH"
RUN_DIR = "RUN_DIR"
SOURCE_MODE = "SOURCE_MODE"

PACKAGE_VERSION = "3.1.0"

MODE_T2I = "文生图（Krea2）"
MODE_I2I = "图生图（手动导入）"

# 质量预设：数值按「高质量输出」重新标定（2026-10-21）。
QUALITY_PRESETS: dict[str, dict[str, float | int]] = {
    "标准（快）": {
        "epsilon_px": 0.30,
        "N_max": 512,
        "curve_tol": 0.010,
        "supersample": 1,
        "max_curves": 100000,
    },
    "高": {
        "epsilon_px": 0.15,
        "N_max": 8192,
        "curve_tol": 0.005,
        "supersample": 2,
        "max_curves": 20000,
    },
    "极高": {
        "epsilon_px": 0.08,
        "N_max": 16384,
        "curve_tol": 0.002,
        "supersample": 2,
        "max_curves": 40000,
    },
    "极限": {
        "epsilon_px": 0.04,
        "N_max": 32768,
        "curve_tol": 0.001,
        "supersample": 3,
        "max_curves": 80000,
    },
}

VIDEO_CIRCLES_DEFAULT = 16384
VIDEO_SIZE_DEFAULT = "2160x2160"
BUILD_RES_DEFAULT = 1536
RENDER_RES_DEFAULT = 4096


def _output_base() -> Path:
    if folder_paths is not None:
        try:
            return Path(folder_paths.get_output_directory())
        except Exception:
            pass
    return Path.cwd() / "output"


def _new_run_dir(mode: str) -> Path:
    root = _output_base() / "fdp_t2i_runs"
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    if "文生图" in mode:
        tag = "t2i"
    elif "图生图" in mode:
        tag = "i2i"
    else:
        tag = "video"
    run = root / f"{stamp}_{tag}_{uuid.uuid4().hex[:6]}"
    run.mkdir(parents=True, exist_ok=False)
    return run


def _ensure_run_dir(run_dir: str) -> Path:
    p = Path(str(run_dir)).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _env() -> dict:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # 让 skill / fourier-* 子进程在任意 cwd 下都能定位发布包（FOURIER_WS 是
    # skills 脚本的正式覆盖口；PYTHONPATH 保证 -m fourier_prompt 与 fbs_core 可导入）
    try:
        ws = workspace_root()
        env.setdefault("FOURIER_WS", str(ws))
        parts = [str(ws / "fourier-prompt"),
                 str(ws / "fourier-brushstroke" / "scripts")]
        old = env.get("PYTHONPATH")
        if old:
            parts.append(old)
        env["PYTHONPATH"] = os.pathsep.join(parts)
    except Exception:
        pass
    return env


def _tail(text: str, n: int = 5000) -> str:
    return (text or "")[-n:]


def _run_cmd(cmd: list[str], *, cwd: Path | None, timeout: int, what: str) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=int(timeout),
            env=_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{what} 超时（>{int(timeout)}s）。命令：{cmd}") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"{what} 失败（exit={proc.returncode}）。\n命令：{' '.join(cmd)}\n"
            f"--- stdout ---\n{_tail(proc.stdout)}\n--- stderr ---\n{_tail(proc.stderr)}"
        )
    return proc


def _save_tensor_png(image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import torch  # type: ignore

        if isinstance(image, torch.Tensor):
            arr = image[0].detach().cpu().numpy()
        else:
            arr = np.asarray(image)
            if arr.ndim == 4:
                arr = arr[0]
    except Exception:  # pragma: no cover
        arr = np.asarray(image)
        if arr.ndim == 4:
            arr = arr[0]
    arr = np.asarray(arr)
    if arr.dtype != np.uint8:
        arr = (np.clip(arr.astype(np.float32), 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    Image.fromarray(arr).save(path)


def _load_png_tensor(path: Path):
    import torch  # type: ignore

    with Image.open(path) as im:
        arr = np.asarray(im.convert("RGB"), dtype=np.float32) / 255.0
    return torch.from_numpy(arr).unsqueeze(0)


def _auto_width_scale(doc: dict) -> tuple[float, str]:
    klass = str((doc.get("image_class") or {}).get("klass") or "")
    evidence = bool((doc.get("image_class") or {}).get("evidence"))
    strokes = len(doc.get("strokes") or [])
    if evidence and strokes >= 100 and klass in ("lineart", "flat_art"):
        return 0.95, f"klass={klass!r}, evidence=有, strokes={strokes} → 0.95"
    return 1.0, f"klass={klass!r}, evidence={'有' if evidence else '无'}, strokes={strokes} → 1.0"


def _is_blank_image(image) -> bool:
    try:
        import torch  # type: ignore

        t = image[0] if isinstance(image, torch.Tensor) and image.ndim == 4 else image
        arr = t.detach().cpu().numpy() if hasattr(t, "detach") else np.asarray(t)
        arr = np.asarray(arr)
        if arr.ndim == 3:
            arr = arr[..., :3]
        return float(arr.std()) < 0.01
    except Exception:  # pragma: no cover
        return False


def _is_i2i_mode(source_mode: str) -> bool:
    return "图生图" in str(source_mode or "")


def _folder_summary(run_dir: Path) -> str:
    files = [p.name for p in sorted(run_dir.rglob("*")) if p.is_file()]
    return f"{run_dir} | {len(files)} files"


# --------------------------------------------------------------------------- ⓪ 入口：模式选择 + 任务目录 + 懒加载选图
class FDPT2IWorkflowStart:
    """整个工作流最早执行的节点：选文生图/图生图，并创建本次任务唯一输出目录。

    两个 IMAGE 输入都是 ``lazy``：只执行当前选中分支；图生图模式下 Krea2 不加载/不采样。
    """

    CATEGORY = "Fourier/T2I"
    FUNCTION = "execute"
    RETURN_TYPES = (SOURCE_MODE, RUN_DIR, "IMAGE")
    RETURN_NAMES = ("source_mode", "run_dir", "image")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "source_mode": ([MODE_T2I, MODE_I2I], {
                    "default": MODE_T2I,
                    "tooltip": "文生图=Krea2 扩散；图生图=手动 LoadImage（Krea2 分支自动跳过）",
                }),
                "krea2_image": ("IMAGE", {"lazy": True,
                                          "tooltip": "来自 KSampler→VAEDecode 的扩散图"}),
                "manual_image": ("IMAGE", {"lazy": True,
                                           "tooltip": "来自 LoadImage 的手动图片"}),
            }
        }

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # 每次 Queue 都重新建目录（节点本身没有输入时也能保证新任务文件夹）
        return time.time()

    def check_lazy_status(self, source_mode, krea2_image=None, manual_image=None):
        if _is_i2i_mode(source_mode):
            return ["manual_image"] if manual_image is None else []
        return ["krea2_image"] if krea2_image is None else []

    def execute(self, source_mode, krea2_image=None, manual_image=None):
        if _is_i2i_mode(source_mode):
            if manual_image is None:
                raise RuntimeError("模式=图生图，但 manual_image 未连接；请接一个 LoadImage。")
            image = manual_image
        else:
            if krea2_image is None:
                raise RuntimeError("模式=文生图，但 krea2_image 未连接/未产出。")
            image = krea2_image

        run_dir = _new_run_dir(source_mode)
        summary = {
            "source_mode": source_mode,
            "created": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "output_dir": str(run_dir),
            "tool": {"package": "ComfyUI-FDP-T2I", "version": PACKAGE_VERSION},
        }
        (run_dir / "run.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return (str(source_mode), str(run_dir), image)


# --------------------------------------------------------------------------- ① 图生图：IMAGE → .fbs
class FDPT2IBuildFromImage:
    CATEGORY = "Fourier/T2I"
    FUNCTION = "execute"
    RETURN_TYPES = (FBS_PATH, "INT", "STRING")
    RETURN_NAMES = ("fbs_path", "elements", "summary")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE", {"tooltip": "入口节点选中的图像"}),
                "run_dir": (RUN_DIR, {"tooltip": "本次任务唯一输出目录"}),
                "resolution": ("INT", {"default": BUILD_RES_DEFAULT, "min": 256, "max": 8192, "step": 32,
                                       "tooltip": "傅里叶采样目标分辨率（长边）；高质量建议 1536–4096"}),
                "quality": (list(QUALITY_PRESETS), {"default": "极高",
                                                    "tooltip": "精度预设：越高 ε/N_max/曲线容差越精细，体积与时间越大"}),
            }
        }

    async def execute(self, image, run_dir, resolution, quality):
        return await asyncio.to_thread(self._run, image, run_dir, resolution, quality)

    def _run(self, image, run_dir, resolution, quality):
        if _is_blank_image(image):
            raise RuntimeError("输入图像近乎空白：请检查 Krea2 采样/提示词，未送入 build。")
        preset = QUALITY_PRESETS.get(str(quality))
        if preset is None:
            raise RuntimeError(f"未知 quality 预设：{quality}")
        ws, fbs_py, _prompt_dir, _fdrender, _video = _workspace_paths()
        if not fbs_py.is_file():
            raise RuntimeError(f"找不到 fourier-brushstroke 脚本：{fbs_py}")

        out = _ensure_run_dir(run_dir)
        src_png = out / "source.png"
        _save_tensor_png(image, src_png)
        build_dir = out / "build"
        build_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            _py(), str(fbs_py), "build", str(src_png),
            "-o", str(build_dir),
            "--resolution", str(int(resolution)),
            "--epsilon-px", f"{float(preset['epsilon_px']):g}",
            "--N-max", str(int(preset["N_max"])),
            "--curve-tol", f"{float(preset['curve_tol']):g}",
            "--supersample", str(int(preset["supersample"])),
            "--max-curves", str(int(preset["max_curves"])),
            "--no-svg",
        ]
        _run_cmd(cmd, cwd=ws / "fourier-brushstroke" / "scripts",
                 timeout=7200, what="fbs.py build")

        fbs_path = build_dir / f"{src_png.stem}.fbs.json"
        report_path = build_dir / f"{src_png.stem}.report.json"
        if not fbs_path.is_file():
            raise RuntimeError(f"build 结束但没找到 .fbs：{fbs_path}")
        rep = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
        stats = rep.get("decomposition_stats") or {}
        elements = int(stats.get("element_count") or (rep.get("fbs") or {}).get("stroke_count") or 0)
        coefs = int((rep.get("fbs") or {}).get("coef_count") or 0)
        quality_info = rep.get("quality") or {}
        summary = (
            f"build R={resolution} | quality={quality} | elements={elements} | coefs={coefs} | "
            f"ssim={quality_info.get('ssim')} | psnr={quality_info.get('psnr')}"
        )
        return (str(fbs_path), elements, summary)


# --------------------------------------------------------------------------- ①b 图生图：IMAGE → v1.36 人类画师式笔画 → .fbs
class FDPT2IBuildFromImageV136:
    """当前 skill（fourier-multistroke-training v1.36）的「人类画师式画法」本地流水线。

    全程**不依赖 AI / 不联网**：wash 铺底 → 顺形体正式层 → 区域成组排序 → 编译 .fbs。
    与位图→.fbs 的老节点（fbs 线稿引擎）并列保留；下游「②打包 .fdraw / ③渲染 / ④视频」直接复用。
    """

    CATEGORY = "Fourier/T2I"
    FUNCTION = "execute"
    RETURN_TYPES = (FBS_PATH, "INT", "STRING")
    RETURN_NAMES = ("fbs_path", "elements", "summary")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE", {"tooltip": "任意位图（本地/扩散/手动导入均可；本节点不调用任何 AI"}),
                "run_dir": (RUN_DIR, {"tooltip": "本次任务唯一输出目录"}),
                "work_res": ("INT", {"default": 1280, "min": 256, "max": 2048, "step": 32,
                                     "tooltip": "v1.36 绘制工作分辨率（长边像素）；1280 画质/速度均衡，1536 最细但更慢"}),
                "resolution": ("INT", {"default": 1536, "min": 256, "max": 1536, "step": 32,
                                       "tooltip": ".fbs 的傅里叶采样目标分辨率（长边）；1536 为交付上限"}),
                "quality": (list(QUALITY_PRESETS), {"default": "标准（快）",
                                                    "tooltip": "傅里叶精度预设（仅影响 ε/N_max/曲线数，不影响画法）；标准(快)=本地交互推荐，极高/极限更慢更精细"}),
                "texture_flat": ("INT", {"default": 12000, "min": 0, "max": 40000, "step": 500,
                                         "tooltip": "细纹理层笔数（打散平色块/颜色分层）；0=关闭"}),
                "thick_dabs": ("INT", {"default": 0, "min": 0, "max": 30000, "step": 500,
                                       "tooltip": "厚涂短笔层笔数（油画/厚涂风格）；0=关闭"}),
                "clip_to_fg": ("BOOLEAN", {"default": True,
                                           "tooltip": "True=只画主体（白底留白，推荐照片/线稿）；False=整幅铺调"}),
                "paint_order": (["region", "scan", "legacy"], {"default": "region",
                                                               "tooltip": "绘制顺序：region=按区域成组（人类画法）；scan/legacy=旧扫描式"}),
            }
        }

    async def execute(self, image, run_dir, work_res, resolution, quality,
                      texture_flat, thick_dabs, clip_to_fg, paint_order):
        return await asyncio.to_thread(
            self._run, image, run_dir, work_res, resolution, quality,
            texture_flat, thick_dabs, clip_to_fg, paint_order,
        )

    def _run(self, image, run_dir, work_res, resolution, quality,
             texture_flat, thick_dabs, clip_to_fg, paint_order):
        if _is_blank_image(image):
            raise RuntimeError("输入图像近乎空白：未送入 v1.36 绘制。")
        preset = QUALITY_PRESETS.get(str(quality))
        if preset is None:
            raise RuntimeError(f"未知 quality 预设：{quality}")
        layered_py, build_py = _multistroke_paths()
        if not layered_py.is_file() or not build_py.is_file():
            raise RuntimeError(
                f"找不到 v1.36 脚本：{layered_py} / {build_py}\n"
                f"请确认 workspace_root()={workspace_root()} 下存在 "
                f"skills/fourier-multistroke-training/scripts/"
            )

        out = _ensure_run_dir(run_dir)
        src_png = out / "source.png"
        _save_tensor_png(image, src_png)
        human_dir = out / "human"
        human_dir.mkdir(parents=True, exist_ok=True)

        wr = int(work_res)
        coarse = max(6, int(round(wr / 64.0)))
        mid = max(4, int(round(wr / 110.0)))
        fine = max(2, int(round(wr / 220.0)))
        p4_spacing = max(3, int(round(wr / 220.0)))
        cmd = [
            _py(), str(layered_py),
            "--input", str(src_png), "--outdir", str(human_dir),
            "--work-res", str(wr),
            "--coarse", str(coarse), "--mid", str(mid), "--fine", str(fine),
            "--mid-err-pct", "60", "--fine-err-pct", "45",
            "--pass4", "--pass4-err-pct", "45", "--pass4-spacing", str(p4_spacing),
            "--base-color-mode", "toned", "--base-color", "auto",
            "--paint-order", str(paint_order),
            "--clip-to-fg", "auto" if clip_to_fg else "off",
            "--max-curves", "100000",
            "--upper-color-mode", "source", "--upper-blur", "2.0", "--upper-gradient",
        ]
        if int(texture_flat) > 0:
            cmd += ["--texture-flat", "--texture-flat-count", str(int(texture_flat)),
                    "--texture-flat-ref-std", "0.015"]
        if int(thick_dabs) > 0:
            cmd += ["--thick-dabs", str(int(thick_dabs))]
        t0 = time.time()
        _run_cmd(cmd, cwd=layered_py.parent, timeout=7200, what="layered_base (v1.36 人类画师式)")

        report_path = human_dir / "layered_base_report.json"
        strokes_path = human_dir / "layered_base_strokes.json"
        if not strokes_path.is_file():
            raise RuntimeError(f"v1.36 绘制结束但没有 strokes：{strokes_path}")
        rep = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
        strokes_n = int(rep.get("strokes_out") or 0)
        if strokes_n > 100000:
            raise RuntimeError(f"v1.36 生成 {strokes_n} 条笔画超过 100k 交付上限；请降低 work_res/纹理笔数/厚涂笔数")

        build_dir = out / "build"
        build_dir.mkdir(parents=True, exist_ok=True)
        fbs_path = build_dir / f"{src_png.stem}_v136.fbs.json"
        cmd2 = [
            _py(), str(build_py),
            "--strokes", str(strokes_path), "--input", str(src_png),
            "--out", str(fbs_path),
            "--resolution", str(int(resolution)),
            "--epsilon", f"{float(preset['epsilon_px']):g}",
            "--nmax", str(int(preset["N_max"])),
            "--max-curves", "100000",
        ]
        _run_cmd(cmd2, cwd=build_py.parent, timeout=7200, what="build_strokes_fbs (v1.36)")
        if not fbs_path.is_file():
            raise RuntimeError(f".fbs 编译结束但没找到输出：{fbs_path}")

        elapsed = time.time() - t0
        size_mb = fbs_path.stat().st_size / (1024 * 1024)
        paint = str(rep.get("paint_order") or paint_order)
        summary = (
            f"v1.36 人类画师式 | strokes={strokes_n} | base={rep.get('base_coat_pattern')} | "
            f"flow={rep.get('flow_strokes')} | paint_order={paint} | "
            f"clip_to_fg={'auto' if clip_to_fg else 'off'} | texture={rep.get('texture_flat_strokes')} | "
            f"dabs={rep.get('thick_dab_strokes')} | fbs={size_mb:.1f}MB | {elapsed:.1f}s"
        )
        return (str(fbs_path), strokes_n, summary)


# --------------------------------------------------------------------------- ② 打包：.fbs → .fdraw
class FDPT2IPackFdraw:
    CATEGORY = "Fourier/T2I"
    FUNCTION = "execute"
    RETURN_TYPES = (FDRAW_PATH, "INT", "STRING")
    RETURN_NAMES = ("fdraw_path", "elements", "summary")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "fbs_path": (FBS_PATH, {"tooltip": "接「位图 → .fbs」"}),
                "source_mode": (SOURCE_MODE, {"tooltip": "由入口节点提供；图生图不写自定义 meta"}),
                "run_dir": (RUN_DIR, {"tooltip": "本次任务唯一输出目录"}),
            }
        }

    async def execute(self, fbs_path, source_mode, run_dir):
        return await asyncio.to_thread(self._run, fbs_path, source_mode, run_dir)

    def _run(self, fbs_path, source_mode, run_dir):
        fbs = Path(str(fbs_path))
        if not fbs.is_file():
            raise RuntimeError(f"找不到 .fbs：{fbs}")
        out = _ensure_run_dir(run_dir)
        ws, _fbs_py, prompt_dir, _fdrender, _video = _workspace_paths()
        if not (prompt_dir / "fourier_prompt").is_dir():
            raise RuntimeError(f"找不到 fourier-prompt 包：{prompt_dir}")

        doc = json.loads(fbs.read_text(encoding="utf-8"))
        width_scale, ws_reason = _auto_width_scale(doc)
        doc.setdefault("render_contract", {})["width_scale"] = width_scale
        packed_fbs = out / "fdraw.packed.fbs.json"
        packed_fbs.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

        fdraw_path = out / "fdraw.fdraw"
        cmd = [
            _py(), "-m", "fourier_prompt.fdraw_cli", "pack",
            "--fbs", str(packed_fbs), "--out", str(fdraw_path),
        ]
        meta_path = None
        if not _is_i2i_mode(source_mode):
            src = doc.get("source") or {}
            meta = {
                "schema": "fdraw/1",
                "provenance_schema": "fdraw-meta/comfyui/2",
                "source": "comfyui-workflow",
                "workflow": "FDP_T2I_Krea2_fdraw",
                "mode": str(source_mode),
                "models": {
                    "unet": "redcraftHybridH3Krea2dual_30Krea2_fp8.safetensors",
                    "clip": "qwen3VL4BAbliteratedComfyui_v10_full_bf16.safetensors",
                    "vae": "qwen_image_vae.safetensors",
                    "lora": {"name": "Krea2_色彩增强.safetensors",
                             "strength_model": 0.8, "strength_clip": 0.8},
                },
                "width_scale": {"mode": "auto", "value": width_scale, "reason": ws_reason},
                "source_png": {k: src.get(k) for k in ("path", "sha256", "width", "height", "mode")},
                "image_class": doc.get("image_class"),
                "quality": doc.get("quality"),
                "created": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                "tool": {"package": "ComfyUI-FDP-T2I", "version": PACKAGE_VERSION},
            }
            meta_path = out / "fdraw.meta.json"
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            cmd += ["--meta", str(meta_path)]

        _run_cmd(cmd, cwd=prompt_dir, timeout=3600, what="fdraw pack")
        _run_cmd(
            [_py(), "-m", "fourier_prompt.fdraw_cli", "validate", "--fdraw", str(fdraw_path)],
            cwd=prompt_dir, timeout=3600, what="fdraw validate",
        )
        info = json.loads(_run_cmd(
            [_py(), "-m", "fourier_prompt.fdraw_cli", "inspect",
             "--fdraw", str(fdraw_path), "--json"],
            cwd=prompt_dir, timeout=3600, what="fdraw inspect",
        ).stdout)
        elements = int((info.get("counts") or {}).get("elements") or 0)
        summary = f"{fdraw_path.name} | elements={elements} | width_scale={width_scale} | meta={'有' if meta_path else '无（图生图）'}"
        return (str(fdraw_path), elements, summary)


# --------------------------------------------------------------------------- ③ 渲染：.fdraw → IMAGE
class FDPT2IRenderFdraw:
    CATEGORY = "Fourier/T2I"
    FUNCTION = "execute"
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "fdraw_path": (FDRAW_PATH, {"tooltip": "接「打包 .fdraw」"}),
                "run_dir": (RUN_DIR, {"tooltip": "本次任务唯一输出目录"}),
                "resolution": ("INT", {"default": RENDER_RES_DEFAULT, "min": 256, "max": 8192, "step": 32,
                                       "tooltip": "输出 PNG 长边；高质量建议 4096，极限 8192"}),
            }
        }

    async def execute(self, fdraw_path, run_dir, resolution):
        return await asyncio.to_thread(self._run, fdraw_path, run_dir, resolution)

    def _run(self, fdraw_path, run_dir, resolution):
        fd = Path(str(fdraw_path))
        if not fd.is_file():
            raise RuntimeError(f"找不到 .fdraw：{fd}")
        out = _ensure_run_dir(run_dir)
        res = int(resolution)
        # --- v3.0.1: OpenGL GPU 光栅化优先（复用本机 ComfyUI python 的 PyOpenGL/glfw）
        # 任何失败（无 GL/异常/超时）都自动回退到原来的 CPU exact 渲染；
        # 环境变量 FDP_GPU_RENDER=0 可强制走 CPU。
        gpu_png = out / f"fdraw_R{res}_gpu.png"
        gpu_script = (Path(workspace_root()) / "skills" / "fourier-multistroke-training"
                      / "scripts" / "gpu_render.py")
        use_gpu = os.environ.get("FDP_GPU_RENDER", "1").strip().lower() not in (
            "0", "false", "off", "no")
        if use_gpu and gpu_script.is_file():
            t_gpu = time.time()
            try:
                timing_path = out / f"fdraw_R{res}_gpu_timing.json"
                proc = _run_cmd(
                    [sys.executable, str(gpu_script), "--fdraw", str(fd),
                     "--out", str(gpu_png), "--resolution", str(res),
                     "--supersample", "2", "--timing-json", str(timing_path)],
                    cwd=out, timeout=900, what="gpu-render",
                )
                if gpu_png.is_file():
                    last = (proc.stdout or "").strip().splitlines()
                    print(f"[FDP-T2I] GPU render ok res={res} "
                          f"{time.time() - t_gpu:.1f}s -> {gpu_png.name}")
                    if last:
                        print(f"[FDP-T2I] gpu timing: {last[-1]}")
                    return (_load_png_tensor(gpu_png),)
                print("[FDP-T2I] GPU render 未产生 PNG，回退 CPU exact")
            except Exception as exc:  # noqa: BLE001 - fallback must never crash the node
                print(f"[FDP-T2I] GPU render 失败（{exc}），回退 CPU exact")
        else:
            print("[FDP-T2I] GPU render 未启用/脚本缺失，使用 CPU exact")
        out_png = out / f"fdraw_R{res}_exact.png"
        _ws, _fbs_py, prompt_dir, _fdrender, _video = _workspace_paths()
        _run_cmd(
            [_py(), "-m", "fourier_prompt.fdrender_cli", "render",
             "--fdraw", str(fd), "--out", str(out_png),
             "--resolution", str(res), "--quality", "exact"],
            cwd=prompt_dir, timeout=7200, what="fdrender",
        )
        if not out_png.is_file():
            raise RuntimeError(f"fdrender 结束但没找到输出：{out_png}")
        return (_load_png_tensor(out_png),)


# --------------------------------------------------------------------------- ④ 独立 .fdraw 路径 / 独立任务目录
class FDPT2IFdrawPath:
    CATEGORY = "Fourier/T2I"
    FUNCTION = "execute"
    RETURN_TYPES = (FDRAW_PATH,)
    RETURN_NAMES = ("fdraw_path",)

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"fdraw_path": ("STRING", {"default": "", "multiline": False,
                                                       "tooltip": "已有 .fdraw 的绝对路径"})}}

    def execute(self, fdraw_path):
        fd = Path(str(fdraw_path).strip().strip('"'))
        if not fd.is_file():
            raise RuntimeError(f"找不到 .fdraw：{fd}")
        return (str(fd),)


class FDPT2IRunDir:
    """给独立 .fdraw→视频/渲染工作流用：创建本次任务唯一输出目录。"""

    CATEGORY = "Fourier/T2I"
    FUNCTION = "execute"
    RETURN_TYPES = (RUN_DIR,)
    RETURN_NAMES = ("run_dir",)

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return time.time()

    def execute(self):
        run = _new_run_dir("视频/独立任务")
        (run / "run.json").write_text(json.dumps({
            "created": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "output_dir": str(run),
            "tool": {"package": "ComfyUI-FDP-T2I", "version": PACKAGE_VERSION},
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return (str(run),)


# --------------------------------------------------------------------------- ⑤ 傅里叶作画视频
class FDPT2IFourierVideo:
    CATEGORY = "Fourier/T2I"
    FUNCTION = "execute"
    RETURN_TYPES = ("STRING", "IMAGE")
    RETURN_NAMES = ("video_path", "poster")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "fdraw_path": (FDRAW_PATH, {}),
                "run_dir": (RUN_DIR, {"tooltip": "本次任务唯一输出目录"}),
                "size": ("STRING", {"default": VIDEO_SIZE_DEFAULT,
                                    "tooltip": "视频尺寸；默认 2160x2160，可 3840x2160 / 3840x3840"}),
                "circles": ("INT", {"default": VIDEO_CIRCLES_DEFAULT, "min": 256, "max": 65536, "step": 256,
                                    "tooltip": "可见/参与计算的傅里叶圆数量；高质量默认 16384"}),
                "duration_s": ("FLOAT", {"default": 30.0, "min": 2.0, "max": 300.0, "step": 1.0}),
            }
        }

    async def execute(self, fdraw_path, run_dir, size, circles, duration_s):
        return await asyncio.to_thread(self._run, fdraw_path, run_dir, size, circles, duration_s)

    def _run(self, fdraw_path, run_dir, size, circles, duration_s):
        fd = Path(str(fdraw_path))
        if not fd.is_file():
            raise RuntimeError(f"找不到 .fdraw：{fd}")
        out = _ensure_run_dir(run_dir)
        ws, _fbs_py, _prompt_dir, _fdrender, video_py = _workspace_paths()
        if not video_py.is_file():
            raise RuntimeError(f"找不到视频脚本：{video_py}")
        n = max(256, min(int(circles), 65536))
        out_mp4 = out / f"fourier_{int(float(duration_s))}s.mp4"
        cmd = [
            _py(), str(video_py), "--fdraw", str(fd), "--out", str(out_mp4),
            "--duration", f"{float(duration_s):g}", "--fps", "30",
            "--size", str(size), "--circles", str(n),
            "--circle-max-k", str(n), "--circle-min-px", "0.12",
            "--poster-resolution", "2048", "--title", "傅里叶一笔画",
        ]
        _run_cmd(cmd, cwd=ws, timeout=7200, what="fourier video")
        if not out_mp4.is_file():
            raise RuntimeError(f"视频脚本结束但没找到输出：{out_mp4}")
        # 视频脚本自己会写 <mp4 stem>_poster.png + report.json；不重复复制，保持任务目录整洁
        poster_path = out_mp4.with_name(out_mp4.stem + "_poster.png")
        if not poster_path.is_file():
            poster_path = out / "poster.png"
            Image.new("RGB", (64, 64), (255, 255, 255)).save(poster_path)
        return (str(out_mp4), _load_png_tensor(poster_path))


NODE_CLASS_MAPPINGS = {
    "FDPT2IWorkflowStart": FDPT2IWorkflowStart,
    "FDPT2IBuildFromImage": FDPT2IBuildFromImage,
    "FDPT2IBuildFromImageV136": FDPT2IBuildFromImageV136,
    "FDPT2IPackFdraw": FDPT2IPackFdraw,
    "FDPT2IRenderFdraw": FDPT2IRenderFdraw,
    "FDPT2IFdrawPath": FDPT2IFdrawPath,
    "FDPT2IRunDir": FDPT2IRunDir,
    "FDPT2IFourierVideo": FDPT2IFourierVideo,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "FDPT2IWorkflowStart": "⓪ 模式选择 · 文生图 / 图生图（最早设定）",
    "FDPT2IBuildFromImage": "① 位图 → 傅里叶笔触 (.fbs) · 线稿/扁平",
    "FDPT2IBuildFromImageV136": "① 位图 → 人类画师式笔画 v1.36 (.fbs) · 本地无 AI",
    "FDPT2IPackFdraw": "② 打包 .fdraw",
    "FDPT2IRenderFdraw": "③ 渲染 .fdraw（exact）",
    "FDPT2IFdrawPath": "· 填 .fdraw 路径",
    "FDPT2IRunDir": "· 新建任务目录",
    "FDPT2IFourierVideo": "④ 傅里叶作画视频",
}
