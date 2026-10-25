"""R3 60 秒作画视频：旋转圆（epicycle）作画 + 逐帧增量扫掠 + UI 叠加。

职责
----
`make_video(one_doc, out_path, ...)`：把 one_stroke `.fbs` 渲染成 ~1 分钟的 mp4；
`probe_video(path)`：用解码器读回帧数/尺寸/时长做产物校验。

关键决策与原因（旧版坑 6 等）
------------------------------
- **笔尖位置来自一次 ifft 预计算**（`onestroke.build_path`），逐帧只做切片与增量绘制；
  绝不逐帧遍历 65536 个系数（旧版那样 N=65536 时约 164 亿次、跑几小时）。
- **旋转圆链**：取 |coef| 最大的 K 个系数做链式圆；每个圆的圆心/半径随参数 u 旋转，
  链末端与真实笔尖之间画一条连线（经典傅里叶作图视觉）。半径 < 0.5px 的圆不绘制
  （不可见；K 只作计算上限），绘制开销与可见圆数成正比而不是 K。
- **逐帧增量扫掠**：每帧只把 t_prev→t_now 的新路径段交给 FloatCanvas.add_band
  （与静态渲染同一基元），因此画出的是连续线（R5）而不是点串；续画用
  start_cap=False 避免关节处重复叠帽。
- **不省略作画步骤（R3）**：按 `step_spans` 分配帧数，每步至少 `min_frames_per_step`
  帧；帧数不够时自动提高 fps 并 warn("W_VIDEO_FPS_RAISED")，时长保持 60s±5%。
- **时间轴按 t/弧长等比压缩**：帧目标参数取自 step_spans 的弧长归一化区间，
  逐步线性推进（= 按 t 等比）。
- **填充按时间铺开**：填充侧表记录 [t0,t1,rgba,poly]，路径走到 t 区间时按进度
  复合（增量 alpha 用 `A·dp/(1−A·p_prev)` 精确收敛到目标不透明度，不会越铺越深）。
- **编码失败必须显式抛 FBSError**，不静默产出空文件；产物用 `probe_video` 复核。
"""
from __future__ import annotations

import math
import os
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .onestroke import build_path
from .render import RENDER_MODES, FloatCanvas
from .util import FBSError, WarningLog, find_font_path

_DEFAULT_PORTRAIT = (720, 1080)   # 竖版默认（CONTRACT §7）
_DEFAULT_LANDSCAPE = (1080, 720)  # 横图自适应
_VIDEO_SUPERSAMPLE = 2            # 视频画布超采样（精确带覆盖已含 AA，SS 主要服务填充边缘）
_ART_MARGIN_X = 0.06              # 画布左右留白（占帧宽）
_HEADER_RATIO = 0.105             # 顶部标题区占帧高
_FOOTER_RATIO = 0.165             # 底部 UI 区占帧高
_MIN_ART_RES = 32
_UI_BG = (250, 250, 252)
_UI_INK = (52, 62, 84)
_UI_SOFT = (150, 162, 186)
_UI_TRACK = (218, 224, 236)
_UI_ACCENT = (74, 120, 196)
_CIRCLE_EDGE = (132, 166, 212)
_CIRCLE_LINE = (168, 192, 224)
_CIRCLE_DOT = (108, 138, 178)
_TIP_COLOR = (30, 38, 56)
_MIN_CIRCLE_PX = 0.5              # 半径小于半像素的圆不可见，不绘制
_CIRCLE_MAX_K = 240               # 视觉圆的最大谐波：更高次的圆在相邻帧间旋转角过大，会闪烁成噪点
                                  # （链线仍完整穿过这些位置，只省略它们的圆轮廓）


# ------------------------------------------------------------------ 布局 / 字体
def _pick_size(canvas: tuple[float, float], size: tuple[int, int] | None) -> tuple[int, int]:
    """选择帧尺寸：默认竖版 720×1080，横图自适应为 1080×720。"""
    if size is not None:
        w, h = int(size[0]), int(size[1])
        if w < 64 or h < 64:
            raise FBSError("E_CONFIG", f"视频尺寸过小: {size!r}", suggestions=["例如 --size 720x1080"])
        return w, h
    cw, ch = canvas
    # 默认竖版；只有明显横图（宽 ≥ 1.15×高）才转 1080×720（近方形图仍走竖版 720×1080）
    return _DEFAULT_LANDSCAPE if cw >= ch * 1.15 else _DEFAULT_PORTRAIT


def _layout(canvas: tuple[float, float], W: int, H: int) -> dict:
    """计算作画区在帧内的位置与长边分辨率 R_art（长边像素数）。"""
    cw, ch = float(canvas[0]), float(canvas[1])
    if cw <= 0.0 or ch <= 0.0:
        raise FBSError("E_FBS_INVALID", f"canvas 尺寸非法: {canvas!r}")
    mx = int(round(W * _ART_MARGIN_X))
    header = int(round(H * _HEADER_RATIO))
    footer = int(round(H * _FOOTER_RATIO))
    box_w = max(16, W - 2 * mx)
    box_h = max(16, H - header - footer)
    R_art = int(math.floor(min(box_w / cw, box_h / ch)))
    R_art = max(_MIN_ART_RES, min(R_art, max(box_w, box_h)))
    art_w = max(1, int(round(cw * R_art)))
    art_h = max(1, int(round(ch * R_art)))
    if art_w > box_w:
        art_w = box_w
    if art_h > box_h:
        art_h = box_h
    x0 = (W - art_w) // 2
    y0 = header + max(0, (box_h - art_h) // 2)
    return {
        "W": int(W),
        "H": int(H),
        "R_art": int(R_art),
        "art_w": int(art_w),
        "art_h": int(art_h),
        "x0": int(x0),
        "y0": int(y0),
        "header": int(header),
        "footer": int(footer),
        "margin_x": int(mx),
    }


def _load_font(size: int) -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, bool]:
    """加载中文字体（msyh.ttc 等），失败回退默认字体并返回是否支持中文。"""
    path = find_font_path()
    font: Any = None
    if path:
        try:
            font = ImageFont.truetype(path, int(max(6, size)))
        except OSError:
            font = None  # 字体存在但不可读/损坏：退回默认字体（上层按 has_cjk=False 改用 ASCII 文案）
    if font is not None:
        return font, True
    try:
        return ImageFont.load_default(size=int(max(6, size))), False
    except TypeError:  # pragma: no cover - 老版本 Pillow 的 load_default 无 size 参数
        return ImageFont.load_default(), False


def _time_str(seconds: float) -> str:
    """秒 → mm:ss。"""
    s = int(max(0.0, float(seconds)))
    return f"{s // 60:02d}:{s % 60:02d}"


def _text(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, font: Any, fill: Any, anchor: str | None = None) -> None:
    """PIL 文本绘制；位图回退字体不支持 anchor 时自动退回默认对齐。"""
    if anchor is None:
        draw.text(xy, text, font=font, fill=fill)
        return
    try:
        draw.text(xy, text, font=font, fill=fill, anchor=anchor)
    except (TypeError, ValueError):
        x, y = xy
        if anchor == "ma":
            draw.text((x, y), text, font=font, fill=fill)
        else:  # "ra" 右对齐：按文本宽度近似回退
            draw.text((x - len(text) * 7, y), text, font=font, fill=fill)


def _bg_rgb(one_doc: dict) -> tuple[float, float, float]:
    """作画区底色 = 文档 base_color（0..255），缺省白。"""
    base = one_doc.get("base_color")
    if not base:
        return (1.0, 1.0, 1.0)
    vals = np.asarray(base, dtype=np.float64).reshape(-1)[:3]
    if vals.size < 3:
        return (1.0, 1.0, 1.0)
    if float(np.max(np.abs(vals))) > 1.5:
        vals = vals / 255.0
    vals = np.clip(np.nan_to_num(vals, nan=1.0), 0.0, 1.0)
    return (float(vals[0]), float(vals[1]), float(vals[2]))


# ------------------------------------------------------------------ 帧预算 / 时间轴
def _allocate_intervals(weights: np.ndarray, intervals: int, min_frames: int) -> np.ndarray:
    """把 n_frames-1 个区间分配给各作画步骤：每步 ≥ min_frames，其余按弧长权重比例。"""
    steps = len(weights)
    if steps == 0:
        return np.zeros(0, dtype=np.int64)
    intervals = int(intervals)
    base = np.full(steps, max(1, int(min_frames)), dtype=np.int64)
    if base.sum() > intervals:
        raise FBSError(
            "E_CONFIG",
            f"帧预算不足：需要至少 {int(base.sum()) + 1} 帧，实际 {intervals + 1}",
        )
    rem = intervals - int(base.sum())
    w = np.asarray(weights, dtype=np.float64)
    w = w / max(float(w.sum()), 1e-15)
    frac = rem * w
    extra = np.floor(frac).astype(np.int64)
    left = rem - int(extra.sum())
    if left > 0:
        order = np.argsort(-(frac - extra), kind="stable")
        for k in range(left):
            extra[int(order[k % steps])] += 1
    return base + extra


def _frame_budget(
    duration: float,
    fps: int,
    n_steps: int,
    min_frames: int,
    warn: WarningLog,
) -> tuple[int, int]:
    """计算 (帧数, 实际 fps)：不省略步骤，不足时自动提高 fps 并 warn。"""
    duration = float(duration)
    fps = int(fps)
    if not math.isfinite(duration) or duration <= 0.0:
        raise FBSError("E_CONFIG", f"duration 非法: {duration!r}")
    if fps < 1 or fps > 240:
        raise FBSError("E_CONFIG", f"fps 非法（应为 1..240）: {fps!r}")
    min_frames = max(1, int(min_frames))
    needed = 1 + max(0, int(n_steps)) * min_frames
    n_frames = max(int(round(duration * fps)), needed, 2)
    fps_eff = max(fps, int(math.ceil(n_frames / duration)))
    n_frames = max(n_frames, int(round(duration * fps_eff)))
    if fps_eff != fps:
        warn.add(
            "W_VIDEO_FPS_RAISED",
            f"作画步骤 {n_steps} 个、每步至少 {min_frames} 帧，{duration:.1f}s 内需要 ≥{needed} 帧；"
            f"fps {fps} → {fps_eff}（总 {n_frames} 帧）",
        )
    duration_s = n_frames / float(fps_eff)
    if abs(duration_s - duration) / duration > 0.05:
        # 兜底：按实际帧数对齐到最接近的目标时长（±5% 硬约束）
        fps_fix = max(1, int(round(n_frames / duration)))
        if abs(n_frames / float(fps_fix) - duration) / duration <= 0.05:
            fps_eff = fps_fix
            duration_s = n_frames / float(fps_eff)
        else:
            raise FBSError(
                "E_VIDEO_DURATION",
                f"无法在 {duration:.2f}s ±5% 内安排 {n_frames} 帧",
                evidence={"n_frames": n_frames, "fps": fps_eff, "duration_s": duration_s},
            )
    return int(n_frames), int(fps_eff)


def _frame_parameters(spans: list, alloc: np.ndarray) -> np.ndarray:
    """生成逐帧路径参数 t（弧长归一化，逐步线性推进，覆盖 [0,1]）。"""
    if len(spans) != len(alloc):
        raise FBSError("E_FBS_INVALID", "step_spans 与帧分配长度不一致")
    vals: list[float] = [0.0]
    for span, a in zip(spans, alloc):
        t0 = float(span[0])
        t1 = float(span[1])
        a = int(max(1, a))
        for j in range(1, a + 1):
            vals.append(t0 + (t1 - t0) * (j / float(a)))
    arr = np.asarray(vals, dtype=np.float64)
    arr = np.clip(np.maximum.accumulate(arr), 0.0, 1.0)
    arr[-1] = 1.0
    return arr


# ------------------------------------------------------------------ 旋转圆
def _top_coefficients(stroke: dict, circles: int) -> tuple[np.ndarray, np.ndarray]:
    """取 |coef| 最大的至多 K 个系数（视觉圆链用；笔尖位置另由全系数 ifft 给出）。"""
    coef = np.asarray(stroke.get("coef") or [], dtype=np.float64).reshape(-1, 3)
    if len(coef) == 0 or circles <= 0:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.complex128)
    k = coef[:, 0].astype(np.int64)
    c = coef[:, 1] + 1j * coef[:, 2]
    order = np.argsort(-np.abs(c), kind="stable")[: int(circles)]
    return k[order], c[order]


def _draw_ui(
    draw: ImageDraw.ImageDraw,
    layout: dict,
    fonts: dict,
    *,
    title: str,
    progress: float,
    elapsed: float,
    total_s: float,
    strokes_done: int,
    strokes_total: int,
    step_kind: str,
    has_cjk: bool,
) -> None:
    """绘制标题/进度条/时间/笔触数 UI（无中文字体时自动改用 ASCII 标签）。"""
    W, H = layout["W"], layout["H"]
    mx = layout["margin_x"]
    header = layout["header"]
    footer = layout["footer"]
    f_title = fonts["title"]
    f_info = fonts["info"]
    label = title if has_cjk else f"Fourier one-stroke · {title}"
    _text(draw, (mx, int(header * 0.28)), label, f_title, _UI_INK)
    # 顶部细线
    draw.line([(mx, header - 6), (W - mx, header - 6)], fill=_UI_SOFT, width=1)
    # 底部 UI 面板
    panel_y = H - footer
    draw.line([(mx, panel_y), (W - mx, panel_y)], fill=_UI_SOFT, width=1)
    bar_y = panel_y + int(footer * 0.26)
    bar_h = max(4, int(H * 0.006))
    draw.rounded_rectangle(
        [mx, bar_y, W - mx, bar_y + bar_h], radius=bar_h // 2, fill=_UI_TRACK
    )
    fill_w = int((W - 2 * mx) * max(0.0, min(1.0, progress)))
    if fill_w > 0:
        draw.rounded_rectangle(
            [mx, bar_y, mx + max(fill_w, bar_h), bar_y + bar_h], radius=bar_h // 2, fill=_UI_ACCENT
        )
    if has_cjk:
        left = f"进度 {progress * 100:5.1f}%"
        right = f"时间 {_time_str(elapsed)} / {_time_str(total_s)}"
        mid = f"笔触 {strokes_done}/{strokes_total}"
        kind_map = {"stroke": "落笔", "travel": "抬笔", "fill": "铺色"}
        kind = f"当前: {kind_map.get(step_kind, step_kind)}"
    else:
        left = f"progress {progress * 100:5.1f}%"
        right = f"time {_time_str(elapsed)} / {_time_str(total_s)}"
        mid = f"strokes {strokes_done}/{strokes_total}"
        kind = f"phase: {step_kind}"
    text_y = bar_y + bar_h + int(footer * 0.10)
    _text(draw, (mx, text_y), left, f_info, _UI_INK)
    _text(draw, (W // 2, text_y), mid, f_info, _UI_INK, anchor="ma")
    _text(draw, (W - mx, text_y), right, f_info, _UI_INK, anchor="ra")
    _text(draw, (mx, text_y + int(footer * 0.20)), kind, f_info, _UI_SOFT)


def _span_kind_at(spans: list, t: float) -> str:
    """当前 t 所在的作画步骤类型。"""
    for span in spans:
        if float(span[0]) <= t < float(span[1]) or (t >= 1.0 and float(span[1]) >= 1.0 - 1e-12):
            return str(span[2])
    return "stroke"


# ------------------------------------------------------------------ 编码后端
def _writer_or_raise(out_path: str, fps: int) -> Any:
    """建立 imageio/ffmpeg 写入器；缺失后端或初始化失败抛 FBSError。"""
    try:
        import imageio
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise FBSError(
            "E_VIDEO_BACKEND",
            "缺少 imageio，无法编码视频；请安装 imageio + imageio-ffmpeg",
            suggestions=["pip install imageio imageio-ffmpeg"],
        ) from exc
    try:
        import imageio_ffmpeg  # noqa: F401
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise FBSError(
            "E_VIDEO_BACKEND",
            "缺少 imageio-ffmpeg（libx264 编码器），无法编码视频",
            suggestions=["pip install imageio-ffmpeg"],
        ) from exc
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    try:
        return imageio.get_writer(
            out_path,
            fps=int(fps),
            codec="libx264",
            quality=7,
            pixelformat="yuv420p",
            macro_block_size=2,
        )
    except Exception as exc:  # noqa: BLE001 - 第三方后端异常统一转为 FBSError
        raise FBSError(
            "E_VIDEO_ENCODE",
            f"视频写入器初始化失败: {exc}",
            evidence={"out": out_path, "fps": fps},
        ) from exc


# ------------------------------------------------------------------ 主入口
def make_video(
    one_doc: dict,
    out_path: str,
    duration: float = 60.0,
    fps: int = 30,
    size: tuple[int, int] | None = None,
    circles: int = 600,
    mode: str = "sweep",
    warn: WarningLog | None = None,
    smooth: bool = False,
) -> dict:
    """把 one_stroke 文档渲染成 ~`duration` 秒 mp4（旋转圆 + 逐帧增量扫掠 + UI）。

    smooth=False（默认，R3 严格档）：按 step_spans 分配帧，每步至少 min_frames_per_step 帧，
    步骤多时自动提高 fps（绝不省略作画步骤）。
    smooth=True（平滑档，适合笔画数很多的复杂图）：帧数 = duration×fps，t 在时间上均匀推进，
    同一帧可覆盖多个微步骤（所有墨迹仍会被画出，只是不保证每个微步骤独占一帧）；
    圆链与笔尖匀速运动，避免高帧率 + 步进式推进造成的抖动/闪烁。
    """
    warn = warn if warn is not None else WarningLog()
    smooth = bool(smooth)
    if not isinstance(one_doc, dict):
        raise FBSError("E_INPUT_MODE", f"make_video 需要 dict 文档，收到 {type(one_doc).__name__}")
    if one_doc.get("mode") != "one_stroke":
        raise FBSError("E_INPUT_MODE", f"视频需要 one_stroke 文档（当前 mode={one_doc.get('mode')!r}）")
    if mode not in RENDER_MODES:
        raise FBSError("E_RENDER_MODE", f"未知渲染模式: {mode!r}", suggestions=[f"可用: {list(RENDER_MODES)}"])
    strokes = one_doc.get("strokes") or []
    if len(strokes) != 1:
        raise FBSError("E_FBS_INVALID", f"one_stroke 文档必须恰好 1 条笔画，实际 {len(strokes)}")
    stroke = strokes[0]
    params = (one_doc.get("decomposition") or {}).get("params") or {}
    min_frames = max(1, int(params.get("min_frames_per_step", 1)))
    canvas = (float((one_doc.get("canvas") or {}).get("width", 1.0)),
              float((one_doc.get("canvas") or {}).get("height", 1.0)))
    W, H = _pick_size(canvas, size)
    layout = _layout(canvas, W, H)

    spans = [s for s in (stroke.get("step_spans") or []) if isinstance(s, (list, tuple)) and len(s) >= 3]
    if not spans:
        spans = [[0.0, 1.0, "stroke"]]
        warn.add("W_VIDEO_STEPS", "一笔画文档缺少 step_spans，按单一连续笔画处理")
    spans = sorted(([float(s[0]), float(s[1]), str(s[2])] for s in spans), key=lambda x: x[0])
    weights = np.asarray([max(float(s[1]) - float(s[0]), 1e-12) for s in spans], dtype=np.float64)
    if smooth:
        # 平滑档：t 在时间上均匀推进（t 本身是弧长归一化参数 → 匀速作画、圆链匀速旋转）
        n_frames = max(2, int(round(float(duration) * int(fps))))
        fps_eff = int(fps)
        frame_t = np.linspace(0.0, 1.0, n_frames, dtype=np.float64)
        duration_s = n_frames / float(fps_eff)
        if abs(duration_s - float(duration)) / float(duration) > 0.05:
            raise FBSError(
                "E_VIDEO_DURATION",
                f"视频时长 {duration_s:.2f}s 超出 {duration:.1f}s ±5%",
                evidence={"frames": n_frames, "fps": fps_eff},
            )
    else:
        n_frames, fps_eff = _frame_budget(float(duration), int(fps), len(spans), min_frames, warn)
        alloc = _allocate_intervals(weights, n_frames - 1, min_frames)
        frame_t = _frame_parameters(spans, alloc)
        n_frames = int(len(frame_t))
        fps_eff = max(1, int(round(n_frames / float(duration))))
        duration_s = n_frames / float(fps_eff)
        if abs(duration_s - float(duration)) / float(duration) > 0.05:
            raise FBSError(
                "E_VIDEO_DURATION",
                f"视频时长 {duration_s:.2f}s 超出 {duration:.1f}s ±5%",
                evidence={"frames": n_frames, "fps": fps_eff},
            )

    # ---- 一次 ifft 预计算路径（笔尖位置切片；禁止逐帧遍历系数）----
    path = build_path(one_doc, layout["R_art"])
    pts_world = np.asarray(path["points_world"], dtype=np.float64)
    t_path = np.asarray(path["t"], dtype=np.float64)
    radii = np.asarray(path["radii_world"], dtype=np.float64)
    rgb = np.asarray(path["rgb"], dtype=np.float64)
    alpha = np.asarray(path["alpha"], dtype=np.float64)
    shapes = list(path.get("shapes") or [])  # 一笔画 shape step 曲线 → 逐帧逐点笔形（D4）
    V = len(pts_world)
    R_art = float(layout["R_art"])
    pts_px = pts_world * R_art
    radii_px = radii * R_art
    fill_soft = float(params.get("fill_soft_px", 2.0))
    bg_rgb01 = np.asarray(_bg_rgb(one_doc), dtype=np.float64).reshape(3)
    fill_states: list[dict] = []
    for rec in path.get("fills") or []:
        if not (isinstance(rec, (list, tuple)) and len(rec) >= 4):
            continue
        color = np.asarray(rec[2], dtype=np.float64).reshape(-1)
        if color.size < 4:
            color = np.resize(color, 4)
        rgb_f = color[:3]
        if float(np.max(np.abs(rgb_f))) > 1.5:
            rgb_f = rgb_f / 255.0
        poly = np.asarray(rec[3], dtype=np.float64).reshape(-1, 2)
        if len(poly) < 3:
            continue
        cf = rec[4] if len(rec) >= 5 and isinstance(rec[4], dict) else None
        if isinstance(cf, dict) and isinstance(cf.get("bbox"), (list, tuple)) and len(cf["bbox"]) == 4:
            cf = dict(cf)
            cf["bbox"] = [float(v) * R_art for v in cf["bbox"]]
        # 带洞区域：新 6 元素格式 rec[5] 仅在 list/tuple 时当洞环列表（旧 4/5 元素记录无洞）
        holes_px: list[np.ndarray] = []
        if len(rec) >= 6 and isinstance(rec[5], (list, tuple)):
            for ring in rec[5]:
                if not isinstance(ring, (list, tuple, np.ndarray)):
                    continue
                try:
                    harr = np.asarray(ring, dtype=np.float64).reshape(-1, 2)
                except (TypeError, ValueError):
                    continue
                if len(harr) >= 3 and bool(np.all(np.isfinite(harr))):
                    holes_px.append(harr * R_art)
        fill_states.append(
            {
                "t0": float(rec[0]),
                "t1": float(rec[1]),
                "rgb": np.clip(rgb_f, 0.0, 1.0),
                "A": float(np.clip(color[3], 0.0, 1.0)),
                "poly_px": poly * R_art,
                "cf": cf,
                "holes_px": holes_px,
                "p": 0.0,
                "done": False,
            }
        )
    k_top, c_top = _top_coefficients(stroke, int(circles))

    # 步骤统计（不省略步骤 → 每帧都要能看出推进）
    stroke_t1 = np.asarray([s[1] for s in spans if s[2] == "stroke"], dtype=np.float64)
    strokes_total = int(len(stroke_t1))

    # ---- 渲染循环 ----
    canvas_obj = FloatCanvas(layout["art_w"], layout["art_h"], _VIDEO_SUPERSAMPLE, tuple(bg_rgb01))
    font_title, has_cjk = _load_font(max(10, int(H * 0.030)))
    font_info, _ = _load_font(max(9, int(H * 0.022)))
    fonts = {"title": font_title, "info": font_info}
    title = _video_title(one_doc)
    art_w, art_h = layout["art_w"], layout["art_h"]
    x0, y0 = layout["x0"], layout["y0"]
    prev_idx = 0
    writer = _writer_or_raise(out_path, fps_eff)
    try:
        for k in range(n_frames):
            t_now = float(frame_t[k])
            # 1) 增量扫掠：只画 t_prev → t_now 的新路径段
            idx = int(np.clip(np.searchsorted(t_path, t_now, side="right"), prev_idx, V - 1))
            if idx > prev_idx:
                seg_pts = pts_px[prev_idx : idx + 1]
                seg_r = radii_px[prev_idx : idx + 1]
                seg_c = rgb[prev_idx : idx + 1]
                seg_a = alpha[prev_idx : idx + 1]
                seg_shape = shapes[prev_idx : idx + 1] if len(shapes) == V else None
                if idx == V - 1:
                    # 闭合整条路径：末尾接回首点
                    seg_pts = np.vstack([seg_pts, pts_px[:1]])
                    seg_r = np.concatenate([seg_r, radii_px[:1]])
                    seg_c = np.vstack([seg_c, rgb[:1]])
                    seg_a = np.concatenate([seg_a, alpha[:1]])
                    if seg_shape is not None:
                        seg_shape = seg_shape + shapes[:1]
                canvas_obj.add_band(
                    seg_pts, seg_r, seg_c, seg_a, mode=mode, start_cap=(prev_idx == 0), shapes=seg_shape
                )
                prev_idx = idx
            # 2) 填充：路径走到其 t 区间时按进度铺开（增量 alpha 精确收敛，不越铺越深）
            for fl in fill_states:
                if fl["done"]:
                    continue
                if t_now < fl["t0"]:
                    continue
                span_t = fl["t1"] - fl["t0"]
                p_new = 1.0 if span_t <= 1e-12 else min(1.0, (t_now - fl["t0"]) / span_t)
                if p_new <= fl["p"]:
                    continue
                a_target = float(fl["A"])
                denom = max(1e-9, 1.0 - a_target * fl["p"])
                e = a_target * (p_new - fl["p"]) / denom
                e = float(np.clip(e, 0.0, 1.0))
                if e > 0.0:
                    canvas_obj.fill_polygon(fl["poly_px"], fl["rgb"], e, fill_soft, color_field=fl.get("cf"))
                # 带洞区域：洞用背景色按同一进度一起铺开（目标不透明度 1，最终帧洞=背景色；
                # 中间过渡与静态渲染允许近似）
                holes = fl.get("holes_px") or []
                if holes:
                    denom_h = max(1e-9, 1.0 - fl["p"])
                    e_h = float(np.clip((p_new - fl["p"]) / denom_h, 0.0, 1.0))
                    if e_h > 0.0:
                        for hole_px in holes:
                            canvas_obj.fill_polygon(hole_px, bg_rgb01, e_h, fill_soft)
                fl["p"] = p_new
                if p_new >= 1.0:
                    fl["done"] = True
            # 3) 合成帧：作画区贴入帧 + 旋转圆 + UI
            art = canvas_obj.to_uint8()
            frame = np.empty((H, W, 3), dtype=np.uint8)
            frame[...] = np.asarray(_UI_BG, dtype=np.uint8)
            if art.shape[0] != art_h or art.shape[1] != art_w:
                art_img = Image.fromarray(art, mode="RGB").resize((art_w, art_h), Image.BILINEAR)
                art = np.asarray(art_img)
            frame[y0 : y0 + art_h, x0 : x0 + art_w] = art
            im = Image.fromarray(frame, mode="RGB")
            draw = ImageDraw.Draw(im)
            draw.rectangle([x0 - 1, y0 - 1, x0 + art_w, y0 + art_h], outline=_UI_SOFT, width=1)
            # 旋转圆（视觉元素；笔尖用全系数路径的当前位置）
            probe_idx = int(np.clip(np.searchsorted(t_path, t_now, side="right"), 0, V - 1))
            tip_px = (x0 + float(pts_px[probe_idx, 0]), y0 + float(pts_px[probe_idx, 1]))
            u = float(path["t"][probe_idx])
            if len(k_top) > 0:
                # 圆链用**绝对坐标**（partial 前缀和就是 z(u) 的截断），不做"把链尾平移到笔尖"的
                # 残差平移：残差 = 被截断的尾部系数和在逐帧剧烈变化（本例 median 5.8px / max 41px，
                # 逐帧变化 p99 达 50px），平移会让整条圆链无规律抖动。现在只从链尾画一条到笔尖的
                # 细连线（残差线），抖动被限制在这条短线上。
                _draw_epicycles_scaled(draw, k_top, c_top, u, tip_px, (float(x0), float(y0)), R_art)
            strokes_done = int(np.searchsorted(stroke_t1, t_now, side="right"))
            _draw_ui(
                draw,
                layout,
                fonts,
                title=title,
                progress=t_now,
                elapsed=(k / float(max(n_frames - 1, 1))) * duration_s,
                total_s=duration_s,
                strokes_done=min(strokes_done, strokes_total),
                strokes_total=strokes_total,
                step_kind=_span_kind_at(spans, t_now),
                has_cjk=has_cjk,
            )
            frame = np.asarray(im, dtype=np.uint8)
            writer.append_data(frame)
    except FBSError:
        raise
    except Exception as exc:  # noqa: BLE001 - 第三方编码异常转 FBSError（不允许静默失败）
        raise FBSError(
            "E_VIDEO_ENCODE",
            f"视频帧写入/编码失败: {exc}",
            evidence={"out": out_path, "frames_written": int(n_frames)},
        ) from exc
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001 - close 失败不掩盖主动异常
            pass

    if not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
        raise FBSError(
            "E_VIDEO_ENCODE",
            f"视频编码未产出有效文件: {out_path}",
            evidence={"exists": os.path.exists(out_path)},
        )
    return {
        "out": out_path,
        "frames": int(n_frames),
        "fps": int(fps_eff),
        "duration_s": float(duration_s),
        "size": [int(W), int(H)],
        "circles": int(circles),
        "visible_circles": int(np.sum(np.abs(c_top) * R_art >= _MIN_CIRCLE_PX)) if len(c_top) else 0,
        "bytes": int(os.path.getsize(out_path)),
        "timeline": "uniform" if smooth else "arc",
        "art_resolution": int(R_art),
        "steps": int(len(spans)),
        "min_frames_per_step": int(min_frames),
        "mode": mode,
    }


def _video_title(one_doc: dict) -> str:
    """视频标题：源文件 stem（过长截断）。"""
    src = (one_doc.get("source") or {}).get("path") or ""
    stem = os.path.splitext(os.path.basename(str(src)))[0] if src else "fourier-brushstroke"
    if len(stem) > 22:
        stem = stem[:21] + "…"
    return str(stem)


def _draw_epicycles_scaled(
    draw: ImageDraw.ImageDraw,
    k_top: np.ndarray,
    c_top: np.ndarray,
    u: float,
    tip_px: tuple[float, float],
    origin_px: tuple[float, float],
    scale_px: float,
) -> None:
    """旋转圆链绘制（像素尺度 = 世界单位 × scale_px）。"""
    if len(k_top) == 0:
        return
    phase = np.exp(2j * math.pi * k_top.astype(np.float64) * u)
    partial = np.cumsum(c_top * phase)
    xs = origin_px[0] + partial.real * scale_px
    ys = origin_px[1] + partial.imag * scale_px
    px = np.concatenate([[origin_px[0]], xs])
    py = np.concatenate([[origin_px[1]], ys])
    for i in range(len(k_top)):
        cx, cy = float(px[i]), float(py[i])
        r = float(abs(c_top[i])) * scale_px
        kk = abs(int(k_top[i]))
        if r >= _MIN_CIRCLE_PX and kk <= _CIRCLE_MAX_K:
            if r >= 1.5:
                draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=_CIRCLE_EDGE, width=1)
                if r >= 2.5:
                    draw.ellipse([cx - 1, cy - 1, cx + 1, cy + 1], fill=_CIRCLE_DOT)
        draw.line([(cx, cy), (float(px[i + 1]), float(py[i + 1]))], fill=_CIRCLE_LINE, width=1)
    draw.line(
        [(float(px[-1]), float(py[-1])), (float(tip_px[0]), float(tip_px[1]))],
        fill=_CIRCLE_LINE,
        width=1,
    )
    draw.ellipse(
        [tip_px[0] - 2.2, tip_px[1] - 2.2, tip_px[0] + 2.2, tip_px[1] + 2.2], fill=_TIP_COLOR
    )


# ------------------------------------------------------------------ 产物校验
def _finite_number(value: Any) -> float | None:
    """把元数据值转成有限 float；inf/nan/非数值返回 None（mp4 的 nframes 常为 inf）。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


def probe_video(path: str) -> dict:
    """用解码器复核视频：返回 {frames, fps, duration_s, size:[w,h]}。"""
    if not os.path.exists(path):
        raise FBSError("E_INPUT_MISSING", f"视频不存在: {path}")
    if os.path.getsize(path) <= 0:
        raise FBSError("E_VIDEO_ENCODE", f"视频文件为空: {path}")
    try:
        import imageio
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise FBSError("E_VIDEO_BACKEND", "缺少 imageio，无法 probe 视频") from exc
    try:
        reader = imageio.get_reader(path)
    except Exception as exc:  # noqa: BLE001 - 解码器异常统一转 FBSError
        raise FBSError("E_VIDEO_ENCODE", f"无法打开视频: {exc}", evidence={"path": path}) from exc
    try:
        meta = dict(reader.get_meta_data() or {})
        frames = _finite_number(meta.get("nframes"))
        fps = _finite_number(meta.get("fps"))
        size = meta.get("size")
        duration = _finite_number(meta.get("duration"))
        if frames is None or frames <= 0:
            if duration is not None and fps is not None and duration > 0.0 and fps > 0.0:
                frames = int(round(duration * fps))
            else:
                try:
                    frames = int(reader.count_frames())
                except Exception:  # noqa: BLE001 - 某些容器不给帧数，退回 duration*fps 估算
                    frames = 0
    finally:
        try:
            reader.close()
        except Exception:  # noqa: BLE001
            pass
    try:
        frames_i = int(frames) if frames is not None else 0
        fps_f = float(fps) if fps is not None else 0.0
    except (TypeError, ValueError) as exc:
        raise FBSError("E_VIDEO_ENCODE", f"视频元数据非法: {exc}", evidence={"meta": meta}) from exc
    if frames_i <= 0:
        raise FBSError("E_VIDEO_ENCODE", "视频没有可解码帧（可能编码中断）", evidence={"path": path})
    if fps_f <= 0:
        fps_f = 30.0
    duration_s = float(duration) if (duration is not None and duration > 0.0) else frames_i / fps_f
    if size is None:
        raise FBSError("E_VIDEO_ENCODE", "视频元数据缺少尺寸", evidence={"meta": meta})
    return {
        "frames": int(frames_i),
        "fps": float(fps_f),
        "duration_s": float(duration_s),
        "size": [int(size[0]), int(size[1])],
    }
