"""文档组装：把 Stage0–3 的输出（图片分析 + element 列表 + 傅里叶拟合 + 笔刷曲线）
装成 v2 `.fbs` 文档，并提供 quality 回填与报告写出。

组装顺序（任何一步失败都要带 code 抛出，不静默）：
  preprocess → coverage → decompose → fourier.fit_curve → brush.build_brush
  → brush_styles 精确去重建表 → strokes → stats/fingerprint
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import numpy as np

from . import brushfit as brushfit_mod
from . import brush as brush_mod
from . import coverage as coverage_mod
from . import decompose as decompose_mod
from . import fourier as fourier_mod
from . import io_format
from . import mesh as mesh_mod
from . import preprocess as preprocess_mod
from .config import FBSConfig
from .util import FBSError, WarningLog, accelerators, sha256_json, stem_of


def _source_info(image_path: str, img: dict) -> dict:
    return {
        "path": str(image_path),
        "sha256": io_format.sha256_file(image_path) if os.path.exists(image_path) else "",
        "width": int(img["width"]),
        "height": int(img["height"]),
        "mode": str(img.get("source_mode", "RGBA")),
        "scale_applied": float(img.get("scale", 1.0)),
    }


def _canvas(img: dict) -> dict:
    w, h = int(img["width"]), int(img["height"])
    long_side = float(max(w, h))
    return {
        "width": w / long_side,
        "height": h / long_side,
        "long_side_units": 1.0,
        "origin": "top-left",
        "y_axis": "down",
    }


def _base_color(analysis: dict) -> list[int]:
    bg = np.asarray(analysis.get("bg", [1.0, 1.0, 1.0]), dtype=np.float64).reshape(-1)[:3]
    return [int(round(float(np.clip(v, 0.0, 1.0)) * 255)) for v in bg] + [255]


def _bbox(points: np.ndarray) -> list[float]:
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    if len(pts) == 0:
        return [0.0, 0.0, 0.0, 0.0]
    return [float(pts[:, 0].min()), float(pts[:, 1].min()), float(pts[:, 0].max()), float(pts[:, 1].max())]


def _style_key(style: dict) -> str:
    return sha256_json(style)


class StyleTable:
    """`brush_styles` 精确去重建表（键 = `sha256_json(style)`，浮点全等才共享）。

    首见顺序即 style id（稳定；影响 fingerprint，不影响渲染）。不做有损合并——
    把两套近似样式并成一套会让颜色/宽度曲线漂移（contract §6）。
    """

    def __init__(self) -> None:
        self.styles: list[dict] = []
        self._index: dict[str, int] = {}

    def add(self, style: dict) -> int:
        """登记一套样式，返回它在 `brush_styles` 里的下标（已存在则复用）。"""
        key = _style_key(style)
        idx = self._index.get(key)
        if idx is None:
            idx = len(self.styles)
            self._index[key] = idx
            self.styles.append(style)
        return idx

    def __len__(self) -> int:
        return len(self.styles)


def stroke_record(
    idx: int,
    el: dict,
    fit: dict,
    brush: dict,
    cfg: FBSConfig | None = None,
    *,
    bbox: list[float] | None = None,
    layer: int | None = None,
    region_id: int | None = None,
) -> dict:
    """element + fit + brush → `.fbs` 单条 stroke 记录（渲染端契约 §6 字段集）。

    与原 `_stroke_record` 逐字段相同；`cfg` 保留为兼容位（函数体不用它，旧调用点
    `structdoc.py` 仍按位置传 5 个参数）。新增三个可注入字段，供 FDSL/plan 通道使用：

    - `bbox`：FDSL 的 `gen` 元素没有 `element["points"]`，bbox 由展开后的采样点给出；
    - `layer` / `region_id`：元素未携带这两个字段时的显式来源（默认取 `el`）。
    """
    coef = np.asarray(fit["coef"], dtype=np.float64).reshape(-1, 3)
    closure = dict(fit["closure"])
    box = _bbox(np.asarray(el["points"], dtype=np.float64)) if bbox is None else bbox
    rec: dict[str, Any] = {
        "id": int(idx),
        "closed": bool(fit["closed"]),
        "fill_role": str(el.get("fill_role", "medial_branch")),
        "repr": "fourier",
        "N": int(fit["N"]),
        "arc_length": float(fit["arc_length"]),
        "max_err": float(fit["max_err"]),
        "layer": int(el.get("layer", 0) if layer is None else layer),
        "region_id": int(el.get("region_id", -1) if region_id is None else region_id),
        "bbox": [float(v) for v in box],
        "coef": [[int(k), float(re), float(im)] for k, re, im in coef],
        "closure": closure,
        "brush": {
            "style": int(brush.get("style", 0)),
            "width": brush["width"],
        },
    }
    poly = el.get("fill_poly")
    if poly is not None and len(np.asarray(poly)) >= 3:
        rec["poly"] = [[float(x), float(y)] for x, y in np.asarray(poly, dtype=np.float64).reshape(-1, 2)]
    holes = el.get("fill_holes")
    if holes:
        rec["fill_holes"] = [
            [[float(x), float(y)] for x, y in np.asarray(h, dtype=np.float64).reshape(-1, 2)]
            for h in holes
            if np.asarray(h).size >= 6
        ]
    return rec


#: 旧私有名保留：`structdoc.py` 及外部 PoC 若按 `_stroke_record(idx, el, fit, brush, cfg)` 调用不破坏。
_stroke_record = stroke_record


def assemble_document(
    elements: list[dict],
    *,
    cfg: FBSConfig,
    warn: WarningLog,
    source: dict,
    image_class: dict,
    canvas: dict,
    decomposition: dict,
    base_color: list[int],
    contract: dict | None = None,
    accelerators: dict[str, str] | None = None,
    regions: list[dict] | None = None,
    mode: str = io_format.MODE_PER_STROKE,
    fill_soft_px: float | None = None,
    method: str | None = None,
    fft_passes: int | None = None,
    record_ids: str = "index",
    catch_brush_errors: bool = False,
) -> tuple[dict, dict]:
    """element 列表 → 合法 v2 `.fbs` 文档（未写盘）。返回 `(doc, info)`。

    与 `build_from_image` 原内联段**同序**：
    `fit_curve → build_brush → StyleTable 去重 → stroke_record → new_document
    → 覆盖 method/fill_soft_px → stats.fft_passes → finalize_document`。

    - `fill_soft_px=None` / `method=None` 表示不覆盖（文档保留 `decomposition` 原值）；
    - `fft_passes=None` → `len(strokes)`（逐笔画通道：每条曲线一次 FFT）；FDSL 通道
      按真实 FFT 次数显式传入（纯 gen 可为 0）——两通道语义不同，禁止跨通道比较；
    - `record_ids="index"` 保持 build 行为（跳过的元素让 id 留洞）；`"sequential"` 重排；
    - `catch_brush_errors=False` 保持 build 现状（`build_brush` 异常直接冒泡）；
      `True` 时降级为 `W_GEOMETRY_SKIP` 并跳过该元素（plan/FDSL 通道用）。
    """
    if record_ids not in ("index", "sequential"):
        raise FBSError(
            "E_CONFIG",
            f"record_ids 非法: {record_ids!r}",
            suggestions=["index（保持元素下标，允许留洞）", "sequential（按输出顺序重排）"],
        )
    warn = warn or WarningLog()
    contract = cfg.contract() if contract is None else contract
    # new_document 会直接引用传入的 dict；做成浅拷贝，避免调用方共享 decomposition 被改写。
    dec = dict(decomposition)
    dec["params"] = dict(decomposition.get("params") or {})

    table = StyleTable()
    strokes: list[dict] = []
    skipped: list[dict] = []
    t0 = time.perf_counter()
    for idx, el in enumerate(elements):
        name = f"{el.get('fill_role', 'medial')}#{idx}"
        points = np.asarray(el["points"], dtype=np.float64).reshape(-1, 2)
        closed = bool(el.get("closed", False))
        try:
            fit = fourier_mod.fit_curve(points, closed, contract, cfg, warn, name=name)
        except FBSError as exc:
            warn.add("W_GEOMETRY_SKIP", f"{name} 傅里叶拟合失败被跳过: {exc.message}", severity="degrade")
            skipped.append({"index": idx, "name": name, "code": "W_GEOMETRY_SKIP", "stage": "fit"})
            continue
        if catch_brush_errors:
            try:
                style, brush = brush_mod.build_brush(el, contract, cfg, warn)
            except FBSError as exc:
                warn.add("W_GEOMETRY_SKIP", f"{name} 笔刷构建失败被跳过: {exc.message}", severity="degrade")
                skipped.append({"index": idx, "name": name, "code": "W_GEOMETRY_SKIP", "stage": "brush"})
                continue
        else:
            style, brush = brush_mod.build_brush(el, contract, cfg, warn)
        brush["style"] = table.add(style)
        rid = len(strokes) if record_ids == "sequential" else idx
        strokes.append(stroke_record(rid, el, fit, brush, cfg))
    timings = {"stage2_3_fourier_brush": time.perf_counter() - t0}

    doc = io_format.new_document(
        mode=mode,
        source=dict(source),
        image_class=dict(image_class),
        canvas=dict(canvas),
        render_contract=contract,
        decomposition=dec,
        base_color=base_color,
        warnings=warn,
        accelerators=dict(accelerators or {}),
        regions=list(regions or []),
    )
    doc["brush_styles"] = table.styles
    doc["strokes"] = strokes
    doc["stats"]["fft_passes"] = int(len(strokes) if fft_passes is None else fft_passes)
    if fill_soft_px is not None:
        doc["decomposition"]["params"]["fill_soft_px"] = float(fill_soft_px)
    if method is not None:
        doc["decomposition"]["method"] = method
    io_format.finalize_document(doc, image_sha256=source.get("sha256") or None)

    info = {
        "strokes": len(strokes),
        "styles": len(table.styles),
        "skipped": skipped,
        "fft_passes": int(doc["stats"]["fft_passes"]),
        "timings": timings,
    }
    return doc, info


def build_from_image(
    image_path: str,
    cfg: FBSConfig,
    warn: WarningLog | None = None,
    force_class: str | None = None,
) -> tuple[dict, dict]:
    """Stage0→4：位图 → v2 `.fbs` 文档（未写盘）。

    返回 (doc, info)；info 含 reference_rgb / long_side / width / height / klass / timings。
    照片类输入抛 FBSError("E_PHOTO_DEFERRED", ...)。
    force_class 只允许覆盖非 photo 类别（照片拒绝不可绕过）。
    """
    warn = warn or WarningLog()
    timings: dict[str, float] = {}
    t_all = time.perf_counter()

    t0 = time.perf_counter()
    img = preprocess_mod.load_and_prepare(image_path, cfg, warn)
    timings["stage0_load"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    cls = preprocess_mod.classify(img, cfg)
    if isinstance(cls, dict) and cls.get("reject"):
        rej = cls["reject"]
        raise FBSError(
            str(rej.get("code", "E_PHOTO_DEFERRED")),
            str(rej.get("message", "图像类型不受支持，已拒绝")),
            evidence=dict(rej.get("evidence", {})),
            suggestions=list(rej.get("suggestions", [])),
        )
    if force_class and force_class != cls.get("klass"):
        if force_class == "photo":
            raise FBSError("E_PHOTO_DEFERRED", "照片类输入不受支持，不能通过 force-class 绕过", evidence={"detected": cls.get("klass")})
        warn.add("W_FORCE_CLASS", f"分类被 CLI 强制覆盖: {cls.get('klass')} → {force_class}")
        cls = dict(cls)
        cls["klass"] = force_class
    if cls.get("photo_experimental"):
        warn.add(
            "W_PHOTO_EXPERIMENTAL",
            "照片输入走实验通道（--allow-photo-experimental）：纯笔触绘画（Fourier 曲线 + "
            "画笔属性时间轴，无填充/面片）；默认合同仍拒绝照片",
            severity="warning",
        )
    timings["stage0_classify"] = time.perf_counter() - t0

    painterly_on = bool(cls.get("photo_experimental")) or bool(getattr(cfg, "painterly", False))
    mesh_on = (not painterly_on) and bool(getattr(cfg, "mesh", False))
    if painterly_on or mesh_on:
        # 连续色调/绘画通道：不走调色板+中轴分解。
        # painterly = 纯笔触（本 skill 的目标表示）；mesh = 旧的自适应梯度面片实验。
        img_rgb = np.asarray(img["rgb"], dtype=np.float64)
        border = np.concatenate(
            [img_rgb[0, :, :], img_rgb[-1, :, :], img_rgb[:, 0, :], img_rgb[:, -1, :]], axis=0
        )
        analysis = {
            "klass": "illustration",
            # 纯笔触绘画从**空白画布**起笔（视频里先白后画）；面片模式用边缘色铺底
            "bg": (np.array([1.0, 1.0, 1.0]) if painterly_on else np.clip(border.mean(axis=0), 0.0, 1.0)),
            "stats": {},
        }
        t_dec = time.perf_counter()
        if painterly_on:
            dec = brushfit_mod.decompose_painterly(img_rgb, cfg, warn, long_side=int(img["long_side"]))
            timings["stage1_painterly"] = time.perf_counter() - t_dec
        else:
            dec = mesh_mod.decompose_mesh(img_rgb, cfg, warn, long_side=int(img["long_side"]))
            timings["stage1_mesh"] = time.perf_counter() - t_dec
        timings["stage0_coverage"] = 0.0
    else:
        t0 = time.perf_counter()
        analysis = coverage_mod.analyze(img, cfg, warn, policy_klass=str(cls.get("klass") or ""))
        # --force-class 只覆盖了 doc 里的 image_class；decompose 读的是 analysis["klass"]，
        # 这里必须同步，否则 CLI 的强制分类对填充/中轴路由完全无效（静默失效）。
        if cls.get("klass") and analysis.get("klass") != cls.get("klass"):
            analysis = dict(analysis)
            analysis["klass"] = str(cls.get("klass"))
        timings["stage0_coverage"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        dec = decompose_mod.decompose(img, analysis, cfg, warn)
        timings["stage1_decompose"] = time.perf_counter() - t0

    elements = list(dec.get("elements", []))
    # 连续色调/纯笔触模式：笔触/面片是表示的单元，不是可选笔画，不能按 max_curves 截断
    # （截断会留下未覆盖空洞）。预算由 brush_strokes / mesh_max_cells 控制。
    structural = painterly_on or mesh_on
    curve_limit = len(elements) if structural else int(cfg.max_curves)
    if len(elements) > curve_limit:
        # 安全阀：按 footprint（弧长×平均宽度）降序保留；不是常规限制，必须 warn
        elements.sort(key=lambda e: float(np.sum(e["widths"]) * max(float(e["s"].max() if len(e["s"]) else 0.0), 1e-9)), reverse=True)
        dropped = len(elements) - curve_limit
        elements = elements[: curve_limit]
        warn.add("W_MAX_CURVES", f"笔画数超 max_curves={curve_limit}，按长度降序截断 {dropped} 条")

    contract = cfg.contract()
    # fill_soft auto：线稿/扁平（硬边、平涂）软边 0.5px 更准；插画（柔光/渐变）2.0px 更准
    # （T9 A/B 实测线稿三图 +0.25~+1.17dB）。写进 .fbs 参数，渲染端按原值消费。
    fill_soft = (float(cfg.fill_soft_px) if float(cfg.fill_soft_px) > 0.0
                 else (0.5 if cls.get("klass") in ("lineart", "flat_art") else 2.0))
    if mesh_on:
        # 面片是精确拼接的矩形：必须硬边填充（软边会在格子边界产生混色缝）
        fill_soft = 0.0
    method = ("painterly_brush_strokes" if painterly_on
              else "adaptive_gradient_mesh" if mesh_on
              else dec.get("method", "hybrid_medial_contour"))
    doc, ainfo = assemble_document(
        elements,
        cfg=cfg,
        warn=warn,
        contract=contract,
        source=_source_info(image_path, img),
        image_class={"klass": cls.get("klass", "unknown"),
                     "evidence": dict(cls.get("evidence", {}))},
        canvas=_canvas(img),
        decomposition={
            "method": dec.get("method", "hybrid_medial_contour"),
            "params": cfg.to_dict(),
            "region_count": len(dec.get("regions", []) or []),
        },
        base_color=_base_color(analysis),
        accelerators=accelerators(dec.get("accelerators")),
        regions=list(dec.get("regions", []) or []),
        fill_soft_px=fill_soft,
        method=method,
    )
    timings["stage2_3_fourier_brush"] = ainfo["timings"]["stage2_3_fourier_brush"]

    info = {
        "reference_rgb": img["rgb"],
        "width": int(img["width"]),
        "height": int(img["height"]),
        "long_side": int(max(img["width"], img["height"])),
        "klass": cls.get("klass", "unknown"),
        "warnings": warn,
        "timings": timings,
        "stage_total": time.perf_counter() - t_all,
        "decomposition_stats": dict(dec.get("stats", {})),
    }
    return doc, info


def attach_quality(doc: dict, metrics: dict[str, Any]) -> None:
    """把 metrics.compare() 的输出写进 doc["quality"]（四维摘要 + 关键标量）。"""
    res = metrics.get("resolution", doc.get("render_contract", {}).get("target_resolution", 0))
    if isinstance(res, (list, tuple)):
        res = max(int(v) for v in res) if res else 0
    q: dict[str, Any] = {
        "psnr": float(metrics.get("psnr", 0.0)),
        "ssim": float(metrics.get("ssim", 0.0)),
        "rmse": float(metrics.get("rmse", 0.0)),
        "mae": float(metrics.get("mae", 0.0)),
        "resolution": int(res),
    }
    for key in ("structure", "thickness", "tone", "linewidth", "contract"):
        if key in metrics:
            val = metrics[key]
            q[key] = {k: (float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
                      for k, v in val.items()} if isinstance(val, dict) else val
    doc["quality"] = q


def write_report(
    path: str,
    *,
    doc: dict,
    info: dict,
    metrics: dict[str, Any] | None,
    files: dict[str, str],
    timings: dict[str, float] | None = None,
) -> dict:
    """写机器可读的 report.json，返回报告 dict。"""
    contract = doc.get("render_contract", {})
    report = {
        "schema": "fbs-report/2",
        "image": {
            "path": doc.get("source", {}).get("path"),
            "sha256": doc.get("source", {}).get("sha256"),
            "width": doc.get("source", {}).get("width"),
            "height": doc.get("source", {}).get("height"),
        },
        "image_class": doc.get("image_class", {}),
        "contract": contract,
        "fbs": {
            "stroke_count": doc.get("stats", {}).get("stroke_count"),
            "coef_count": doc.get("stats", {}).get("coef_count"),
            "fill_count": doc.get("stats", {}).get("fill_count"),
            "region_count": doc.get("stats", {}).get("region_count"),
            "bytes": os.path.getsize(files["fbs"]) if files.get("fbs") and os.path.exists(files["fbs"]) else None,
        },
        "warnings": doc.get("warnings", []),
        "quality": doc.get("quality", {}),
        "metrics": metrics or {},
        "files": dict(files),
        "timings_s": {k: round(float(v), 3) for k, v in (timings or {}).items()},
        "decomposition_stats": (info or {}).get("decomposition_stats", {}),
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    return report


def output_paths(image_path: str, outdir: str, resolution: int) -> dict[str, str]:
    """统一产物命名：<stem>.fbs.json / <stem>_R{R}.png / <stem>.svg / <stem>.report.json。"""
    stem = stem_of(image_path)
    return {
        "fbs": os.path.join(outdir, f"{stem}.fbs.json"),
        "png": os.path.join(outdir, f"{stem}_R{int(resolution)}.png"),
        "svg": os.path.join(outdir, f"{stem}.svg"),
        "report": os.path.join(outdir, f"{stem}.report.json"),
        "onestroke": os.path.join(outdir, f"{stem}.onestroke.fbs.json"),
        "video": os.path.join(outdir, f"{stem}_1min.mp4"),
    }
