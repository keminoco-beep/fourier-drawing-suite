"""验收 swatch：用 skill 渲染器画「细线压力渐变 / 宽笔波形 / wash 叠加」验收板。

用法（仓库内）::

    python -m fourier_prompt.brush.swatch \
        --out assets/brush/previews/swatch.png --resolution 512

    # 代码
    from fourier_prompt.brush import render_swatch
    stats = render_swatch("assets/brush/previews/swatch.png", resolution=512)

设计（对齐 ``analysis/13_...`` §3.6 与 Daub ``_swatch_newbrushes.py`` 的方法）：
- 每支笔一行，三列：① 细线 size 12，压力 0.2→1.0→0.3 的提按线（宽度+不透明度都随压力）；
  ② 宽笔波形 size 44 全压；③ wash：3 笔 size 24 叠加，看低 α 叠色/边缘；
- 20 支笔两列排布成一张验收板 ``swatch.png``；每支笔的行图另存 ``previews/<id>.png``；
- ``status="planned"`` 的预设没有 skill 实现 → 生成占位卡（灰底 + 文字），不调用渲染器。

纪律：
- **不改 skill**、**不 import ``fbs_core.document``**（WP-B 正在改它）：只用
  ``io_format.new_document/finalize_document`` + ``brush.build_brush`` + ``fourier.fit_curve``
  + ``render.render`` 自建文档（同一套公共 API，见 ``poc/brushcalib/calib_render.py``）；
- 确定性：无随机、无时间参与数值；同输入同版本 → 同 PNG 字节；
- 渲染分辨率 ≤512（KICKOFF §4）；α 展示口径：把 ``color.stops[].a`` 归一到 1.0，
  只保留 ``style.alpha`` 一处（否则默认路径 α² 会让半透明笔整体偏淡一倍，见 13a §2.5）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

from .calib import BrushCalib
from .catalog import BrushCatalog, BrushCatalogError, UnknownPresetError

# 画布/版式（世界坐标 = 行画布像素；默认 R=512 时 1:1）
ROW_W = 512
ROW_H = 128
N_COLS = 3
COL_W = 160
GAP = 8
LABEL_H = 16
MAX_RESOLUTION = 512

COL_X0 = tuple(GAP + i * (COL_W + GAP) for i in range(N_COLS))  # (8, 176, 344)
STROKE_INSET = 10  # 列内左右留白

INK = (20, 20, 22)  # 与 13a 实测墨色一致（#141416）
BG = (255, 255, 255)

TAPER = 0.10  # 两端 10% 弧长起收笔包络


# ------------------------------------------------------------------ skill 定位
def resolve_skill_scripts() -> Path:
    """定位 skill 的 ``scripts/``（含 ``fbs_core``）。失败 → RuntimeError（不静默降级）。"""
    env = os.environ.get("FOURIER_BRUSHSTROKE_SCRIPTS")
    cands: list[Path] = []
    if env:
        cands.append(Path(env))
    repo = Path(__file__).resolve().parents[3]
    cands.extend(
        [
            repo / "fourier-brushstroke" / "scripts",
            Path.cwd() / "fourier-brushstroke" / "scripts",
            Path.cwd().parent / "fourier-brushstroke" / "scripts",
        ]
    )
    for cand in cands:
        if (cand / "fbs_core" / "render.py").is_file():
            return cand
    raise RuntimeError(
        "找不到 fourier-brushstroke/scripts（含 fbs_core/render.py）；"
        f"已尝试: {[str(c) for c in cands]}；可用环境变量 FOURIER_BRUSHSTROKE_SCRIPTS 指定"
    )


def _import_skill():
    """把 skill ``scripts/`` 加进 sys.path，返回 (brush, fourier, io_format, render, FBSConfig, WarningLog)。

    注意：**不 import** ``fbs_core.document``（WP-B 正在改）；这里只走 io_format/brush/
    fourier/render 四个公共模块。
    """
    scripts = resolve_skill_scripts()
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from fbs_core import brush as brush_mod  # noqa: PLC0415
    from fbs_core import fourier as fourier_mod  # noqa: PLC0415
    from fbs_core import io_format  # noqa: PLC0415
    from fbs_core import render as render_mod  # noqa: PLC0415
    from fbs_core.config import FBSConfig  # noqa: PLC0415
    from fbs_core.util import WarningLog  # noqa: PLC0415

    for mod in (brush_mod, fourier_mod, io_format, render_mod):
        if getattr(mod, "__name__", "").endswith("document"):
            raise RuntimeError("内部错误：不允许 import fbs_core.document")
    return brush_mod, fourier_mod, io_format, render_mod, FBSConfig, WarningLog


# ------------------------------------------------------------------ 几何 / 曲线
def _arclen_s(points) -> list[float]:
    import numpy as np

    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    seg = np.linalg.norm(pts[1:] - pts[:-1], axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    total = float(cum[-1])
    if total <= 1e-15:
        return [0.0] * len(pts)
    return [float(v / total) for v in cum]


def _taper_env(u: list[float], taper: float = TAPER) -> list[float]:
    out: list[float] = []
    for value in u:
        x = min(max(float(value), 0.0), 1.0)
        if x < taper:
            v = math.sin(0.5 * math.pi * x / taper) ** 0.8
        elif x > 1.0 - taper:
            v = math.sin(0.5 * math.pi * (1.0 - x) / taper) ** 0.8
        else:
            v = 1.0
        out.append(float(min(max(v, 0.0), 1.0)))
    return out


def _pressure_ramp(u: float) -> float:
    """细线列的压力轨迹：0.2 → 1.0（前半）→ 0.3（后半）。"""
    if u <= 0.5:
        return 0.2 + 0.8 * (u / 0.5)
    return 1.0 - 0.7 * ((u - 0.5) / 0.5)


def _alpha_curve_from_calib(cal: BrushCalib, preset_id: str, pressures: list[float]) -> list[float]:
    """按实测 α 响应给每个采样点取峰值 α（口径：color.a 归一到 1.0）。"""
    out: list[float] = []
    for p in pressures:
        out.append(float(cal.response(preset_id, 12.0, p)["alpha"]))
    return out


def _column_strokes(
    preset_id: str,
    meta: dict,
    cal: BrushCalib,
    *,
    n: int = 161,
) -> list[dict]:
    """构造该预设一行的 5 条笔触（1 细线 + 1 宽笔 + 3 wash），坐标为行画布像素。"""
    size_mult = float(meta.get("size_mult", 1.0))
    sens = float((meta.get("pressure") or {}).get("sensitivity", 0.0))
    shape = str(meta.get("shape"))
    y_mid = ROW_H / 2.0
    strokes: list[dict] = []

    def xs_of(col: int) -> tuple[float, float]:
        x0 = COL_X0[col] + STROKE_INSET
        return x0, x0 + (COL_W - 2 * STROKE_INSET)

    # ① 细线：size 12，压力 0.2→1.0→0.3（宽度按 1-sens*(1-p)，α 按实测响应）
    x0, x1 = xs_of(0)
    us = [i / (n - 1) for i in range(n)]
    ps = [_pressure_ramp(u) for u in us]
    env = _taper_env(us)
    s_eff = 12.0 * size_mult
    widths = [s_eff * (1.0 - sens * (1.0 - p)) * e for p, e in zip(ps, env)]
    alphas = _alpha_curve_from_calib(cal, preset_id, ps)
    pts = [[x0 + (x1 - x0) * u, y_mid + 0.8 * math.sin(2.0 * math.pi * u)] for u in us]
    strokes.append({"points": pts, "widths_px": widths, "alphas": alphas, "shape": shape, "column": "thin"})

    # ② 宽笔波形：size 44，全压
    x0, x1 = xs_of(1)
    s_eff = 44.0 * size_mult
    amp = float(min(max(0.10 * s_eff, 1.2), 5.0))
    widths = [s_eff * e for e in env]
    alpha_wide = float(cal.response(preset_id, 44.0, 1.0)["alpha"])
    pts = [[x0 + (x1 - x0) * u, y_mid + amp * math.sin(2.0 * math.pi * u)] for u in us]
    strokes.append(
        {
            "points": pts,
            "widths_px": widths,
            "alphas": [alpha_wide] * n,
            "shape": shape,
            "column": "wide",
        }
    )

    # ③ wash：3 笔 size 24 叠加（低 α 笔看叠色/边缘；高 α 笔会变成实心块，本身就是信息）
    x0, x1 = xs_of(2)
    s_eff = 24.0 * size_mult
    alpha_wash = float(cal.response(preset_id, 24.0, 1.0)["alpha"])
    for k, dy in enumerate((-18.0, 0.0, 18.0)):
        widths = [s_eff * e for e in env]
        pts = [[x0 + (x1 - x0) * u, y_mid + dy + 0.6 * math.sin(2.0 * math.pi * u + k)] for u in us]
        strokes.append(
            {
                "points": pts,
                "widths_px": widths,
                "alphas": [alpha_wash] * n,
                "shape": shape,
                "column": f"wash{k}",
            }
        )
    return strokes


# ------------------------------------------------------------------ 文档 / 渲染
def _normalize_color_alpha(style: dict) -> dict:
    """把 color stops 的 a 归一到 1.0（只保留 style.alpha 一处；见模块 docstring）。"""
    color = style.get("color") or {}
    for stop in color.get("stops") or []:
        stop["a"] = 1.0
    return style


def build_row_document(
    preset_id: str,
    *,
    catalog: BrushCatalog,
    calib: BrushCalib,
    long_side: float = float(ROW_W),
    normalize_alpha: bool = True,
):
    """构造该预设一行的合法 ``.fbs`` 文档（自建，不经 document.py）。返回 doc。"""
    import numpy as np

    brush_mod, fourier_mod, io_format, _render_mod, FBSConfig, WarningLog = _import_skill()
    meta = catalog.get(preset_id)
    warn = WarningLog()
    cfg = FBSConfig(target_resolution=int(round(long_side)), epsilon_px=0.5)
    contract = cfg.contract()
    canvas_w, canvas_h = ROW_W, ROW_H

    styles: list[dict] = []
    style_index: dict[str, int] = {}
    records: list[dict] = []
    strokes = _column_strokes(preset_id, meta, calib)

    for i, st in enumerate(strokes):
        pts_px = np.asarray(st["points"], dtype=np.float64).reshape(-1, 2)
        world = pts_px / float(long_side)
        s = np.asarray(_arclen_s(world), dtype=np.float64)
        colors = np.tile(np.asarray(INK, dtype=np.float64).reshape(1, 3) / 255.0, (len(world), 1))
        element = {
            "points": world,
            "s": s,
            "closed": False,
            "fill_role": "medial_branch",
            "widths": np.asarray(st["widths_px"], dtype=np.float64) / float(long_side),
            "colors": colors,
            "alphas": np.asarray(st["alphas"], dtype=np.float64),
            "shapes": [st["shape"]] * len(world),
            "fill_poly": None,
            "fill_holes": None,
            "region_id": -1,
            "layer": 0,
            "meta": {"max_halfwidth_px": 0.0, "sprawl": 0.0, "dt_peak_px": 0.0, "source": "brush_swatch"},
        }
        fit = fourier_mod.fit_curve(world, False, contract, cfg, warn, name=f"{preset_id}#{i}")
        style, brush = brush_mod.build_brush(element, contract, cfg, warn)
        if normalize_alpha:
            style = _normalize_color_alpha(style)
        key = json.dumps(style, sort_keys=True, ensure_ascii=False)
        if key not in style_index:
            style_index[key] = len(styles)
            styles.append(style)
        brush["style"] = style_index[key]
        records.append(
            {
                "id": i,
                "closed": bool(fit["closed"]),
                "fill_role": "medial_branch",
                "repr": "fourier",
                "N": int(fit["N"]),
                "arc_length": float(fit["arc_length"]),
                "max_err": float(fit["max_err"]),
                "layer": 0,
                "region_id": -1,
                "bbox": [
                    float(pts_px[:, 0].min()),
                    float(pts_px[:, 1].min()),
                    float(pts_px[:, 0].max()),
                    float(pts_px[:, 1].max()),
                ],
                "coef": [[int(k), float(r), float(im)] for k, r, im in np.asarray(fit["coef"])],
                "closure": dict(fit["closure"]),
                "brush": {"style": int(brush["style"]), "width": brush["width"]},
            }
        )

    doc = io_format.new_document(
        mode=io_format.MODE_PER_STROKE,
        source={
            "path": "<brush_swatch>",
            "sha256": hashlib.sha256(f"{preset_id}|{canvas_w}x{canvas_h}".encode("utf-8")).hexdigest(),
            "width": int(canvas_w),
            "height": int(canvas_h),
            "mode": "brush_swatch",
            "scale_applied": 1.0,
        },
        image_class={"klass": "brush_swatch", "evidence": {}},
        canvas={
            "width": canvas_w / float(long_side),
            "height": canvas_h / float(long_side),
            "long_side_units": 1.0,
            "origin": "top-left",
            "y_axis": "down",
        },
        render_contract=contract,
        decomposition={"method": "brush_swatch_synthetic", "params": {"preset": preset_id}, "region_count": 0},
        base_color=[BG[0], BG[1], BG[2], 255],
        warnings=warn,
        accelerators={},
    )
    doc["brush_styles"] = styles
    doc["strokes"] = records
    doc["stats"]["fft_passes"] = len(records)
    io_format.finalize_document(doc, image_sha256=doc["source"]["sha256"])
    errs = io_format.validate(doc)
    if errs:
        raise RuntimeError(f"{preset_id}: swatch 文档非法 {errs[:4]}")
    return doc


def render_row(
    preset_id: str,
    *,
    catalog: BrushCatalog,
    calib: BrushCalib,
    resolution: int = MAX_RESOLUTION,
    normalize_alpha: bool = True,
):
    """渲染单支笔的验收行，返回 (H,W,3) uint8 ndarray。"""
    brush_mod, fourier_mod, io_format, render_mod, FBSConfig, WarningLog = _import_skill()
    doc = build_row_document(preset_id, catalog=catalog, calib=calib, normalize_alpha=normalize_alpha)
    warn = WarningLog()
    rgb = render_mod.render(doc, resolution=int(resolution), warn=warn)
    expected_h = int(round(resolution * ROW_H / ROW_W))
    if tuple(rgb.shape[:2]) != (expected_h, int(resolution)):
        raise RuntimeError(f"{preset_id}: 渲染尺寸 {rgb.shape} != {(expected_h, resolution)}")
    return rgb


def _row_label(preset_id: str, meta: dict) -> str:
    tags = ",".join(meta.get("traits") or []) or "-"
    return f"{preset_id}  [{tags}]"


def _fit_text(text: str, width_px: int, *, char_px: int = 7) -> str:
    """按估算字宽把标签截断到格内（避免压到相邻格；标签只做定位，不做排版）。"""
    max_chars = max(6, int(width_px) // max(char_px, 1))
    if len(text) <= max_chars:
        return text
    return text[: max(1, max_chars - 2)] + ".."


def _placeholder_row(preset_id: str, meta: dict, resolution: int):
    """planned 预设的占位卡（灰底 + 说明），不调用 skill 渲染器。"""
    from PIL import Image, ImageDraw, ImageFont

    h = max(int(round(resolution * ROW_H / ROW_W)), 32)
    img = Image.new("RGB", (int(resolution), h), (236, 236, 236))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=max(12, h // 8))
    except TypeError:  # pragma: no cover - 旧 Pillow
        font = ImageFont.load_default()
    draw.rectangle([0, 0, int(resolution) - 1, h - 1], outline=(200, 200, 200))
    draw.text((8, max(2, h // 3)), f"planned: {preset_id}  (no engine implementation yet)", fill=(90, 90, 90), font=font)
    return img


def sheet_layout(count: int, *, cols: int = 2, resolution: int = MAX_RESOLUTION) -> tuple[int, int, int]:
    """返回 (cols, cell_h, rows)；每格 = 行图 + 标签条（行图高度随 resolution 缩放）。"""
    cols = max(1, min(int(cols), max(count, 1)))
    rows = int(math.ceil(count / cols))
    row_h = max(int(round(int(resolution) * ROW_H / ROW_W)), 8)
    return cols, row_h + LABEL_H, rows


def render_swatch(
    out_path: str | os.PathLike | None = None,
    *,
    presets: list[str] | None = None,
    resolution: int = MAX_RESOLUTION,
    previews: bool = True,
    include_planned: bool = False,
    normalize_alpha: bool = True,
    catalog: BrushCatalog | None = None,
    calib: BrushCalib | None = None,
) -> dict:
    """画验收板并写盘；返回统计 dict（路径/尺寸/哈希/耗时）。

    - ``presets=None`` → 目录里全部 active 预设（20 支，按目录顺序）；
    - ``presets`` 里出现未知 id → ``UnknownPresetError``；出现 ``status="planned"`` 的 id →
      ``BrushCatalogError``（没有 skill 实现，不能渲染，不静默跳过）；
    - ``resolution`` 上限 512（超出直接报错，避免误跑长任务）；
    - ``previews=True`` → 每支笔的行图另存 ``<out_dir>/<id>.png``；planned 条目写占位卡；
    - 同输入同版本 → 同 PNG 字节（确定性）；``elapsed_s`` 只进返回值、不进产物。
    """
    from PIL import Image, ImageDraw, ImageFont

    started = time.perf_counter()
    if not isinstance(resolution, int) or resolution <= 0:
        raise ValueError(f"resolution 必须是正整数，收到 {resolution!r}")
    if resolution > MAX_RESOLUTION:
        raise ValueError(f"resolution={resolution} 超过上限 {MAX_RESOLUTION}（KICKOFF §4：渲染 R<=512）")
    cat = catalog if catalog is not None else BrushCatalog.load()
    cal = calib if calib is not None else BrushCalib.load()

    if presets is None:
        chosen = cat.ids()
    else:
        chosen = []
        for pid in presets:
            if not isinstance(pid, str):
                raise BrushCatalogError(f"presets 里必须是字符串 id，收到 {pid!r}")
            meta = cat.get(pid)  # 未知 -> UnknownPresetError
            if meta.get("status") != "active" or not cat.is_renderable(pid):
                raise BrushCatalogError(
                    f"预设 {pid!r} 是 status={meta.get('status')!r}（无 skill 渲染实现），不能进 swatch"
                )
            chosen.append(pid)
    if not chosen:
        raise BrushCatalogError("没有可渲染的预设，swatch 不生成空板")

    out = Path(out_path) if out_path is not None else Path(__file__).resolve().parents[2] / "assets" / "brush" / "previews" / "swatch.png"
    out.parent.mkdir(parents=True, exist_ok=True)

    rows_png: dict[str, str] = {}
    row_imgs: list[Image.Image] = []
    row_meta: list[tuple[str, dict]] = []
    planned_rows: list[tuple[str, dict]] = []
    for pid in chosen:
        rgb = render_row(pid, catalog=cat, calib=cal, resolution=resolution, normalize_alpha=normalize_alpha)
        img = Image.fromarray(rgb, mode="RGB")
        row_imgs.append(img)
        meta = cat.get(pid)
        row_meta.append((pid, meta))
        if previews:
            pv_path = out.parent / f"{pid}.png"
            img.save(pv_path, format="PNG", optimize=False)
            rows_png[pid] = hashlib.sha256(pv_path.read_bytes()).hexdigest()

    if include_planned:
        for pid in cat.planned_ids():
            meta = cat.get(pid)
            planned_rows.append((pid, meta))
            if previews:
                pimg = _placeholder_row(pid, meta, resolution)
                pv_path = out.parent / f"{pid}.png"
                pimg.save(pv_path, format="PNG", optimize=False)
                rows_png[pid] = hashlib.sha256(pv_path.read_bytes()).hexdigest()

    # ---- 拼板：2 列 × N 行（行图 + 标签条）
    cells: list[tuple[str, dict] | None] = list(row_meta) + list(planned_rows)
    n_cells = len(cells)
    cols, cell_h, n_rows = sheet_layout(n_cells, cols=2, resolution=resolution)
    sheet_w = cols * int(resolution)
    sheet_h = n_rows * cell_h
    sheet = Image.new("RGB", (sheet_w, sheet_h), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=13)
    except TypeError:  # pragma: no cover
        font = ImageFont.load_default()
    idx = 0
    for (pid, meta), img in zip(row_meta, row_imgs):
        col = idx % cols
        row = idx // cols
        x = col * int(resolution)
        y = row * cell_h
        sheet.paste(img, (x, y + LABEL_H))
        draw.text((x + 4, y + 2), _fit_text(_row_label(pid, meta), int(resolution) - 8), fill=(20, 20, 20), font=font)
        draw.line([(x, y + LABEL_H - 1), (x + int(resolution) - 1, y + LABEL_H - 1)], fill=(220, 220, 220))
        idx += 1
    for pid, meta in planned_rows:
        col = idx % cols
        row = idx // cols
        x = col * int(resolution)
        y = row * cell_h
        pimg = _placeholder_row(pid, meta, resolution)
        sheet.paste(pimg, (x, y + LABEL_H))
        draw.text((x + 4, y + 2), f"{pid}  [planned]", fill=(120, 120, 120), font=font)
        draw.line([(x, y + LABEL_H - 1), (x + int(resolution) - 1, y + LABEL_H - 1)], fill=(220, 220, 220))
        idx += 1

    sheet.save(out, format="PNG", optimize=False)
    payload = out.read_bytes()
    elapsed = time.perf_counter() - started
    return {
        "out_path": str(out),
        "resolution": int(resolution),
        "columns": int(cols),
        "presets": list(chosen),
        "count": len(chosen),
        "planned_placeholders": [pid for pid, _ in planned_rows],
        "sheet": {
            "width": int(sheet.width),
            "height": int(sheet.height),
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
        "previews": {
            "dir": str(out.parent),
            "count": len(rows_png),
            "sha256": rows_png,
        },
        "alpha_normalized": bool(normalize_alpha),
        "elapsed_s": round(elapsed, 3),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成笔刷验收 swatch（细线压力渐变/宽笔/wash）")
    ap.add_argument("--out", default=None, help="输出 PNG（默认 assets/brush/previews/swatch.png）")
    ap.add_argument("--resolution", type=int, default=MAX_RESOLUTION, help=f"长边像素（<= {MAX_RESOLUTION}）")
    ap.add_argument("--presets", nargs="*", default=None, help="只画指定预设 id")
    ap.add_argument("--no-previews", action="store_true", help="不写每支笔的单行预览图")
    ap.add_argument("--include-planned", action="store_true", help="附 planned 占位卡（不渲染）")
    ap.add_argument("--json", action="store_true", help="打印统计 JSON")
    args = ap.parse_args(argv)
    stats = render_swatch(
        args.out,
        presets=args.presets,
        resolution=args.resolution,
        previews=not args.no_previews,
        include_planned=args.include_planned,
    )
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=1))
    else:
        print(
            f"swatch: {stats['out_path']} ({stats['sheet']['width']}x{stats['sheet']['height']}, "
            f"{stats['sheet']['bytes']} bytes, sha256={stats['sheet']['sha256'][:16]}…, "
            f"{stats['count']} presets, {stats['elapsed_s']}s)"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
