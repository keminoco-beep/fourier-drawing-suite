"""`.fbs` v2 格式的读写/校验/量化/指纹（合同见 references/fbs_format_spec.md）。

设计纪律（旧版 R6 教训）：
- 写入精度由渲染契约反推，不凭手感写死小数位；
- 共享笔刷样式必须建表去重（只做浮点全等去重，不做有损合并）；
- 不同代码代 / 不同渲染契约的产物禁止互相比较（fingerprint + same_generation）；
- 格式变更加字段（additive），不改既有字段语义。
"""
from __future__ import annotations

import copy
import json
import math
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any

import numpy as np

from .util import (
    FBSError,
    WarningLog,
    sha256_file,
    sha256_json,
)

FORMAT = "fourier-brushstroke"
VERSION = "2.0"
CODEGEN = "fbs-v2"
MODE_PER_STROKE = "per_stroke"
MODE_ONE_STROKE = "one_stroke"
FILL_ROLES = ("medial_branch", "region_fill")
_TZ = timezone(timedelta(hours=8))


# ------------------------------------------------------------------ 精度
def decimals_for_coef(coef_scalar_count: int, epsilon_px: float, target_resolution: int) -> int:
    """系数小数位由契约反推。

    第 k 个实分量舍入误差 |δ| ≤ 0.5·10^-nd，S 个实分量的曲线偏差上界
    Σ|δ| ≤ S·0.5·10^-nd = S/2·10^-nd；要求 ≤ epsilon_px/(2R)（世界单位），
    即 S·R·10^-nd ≤ epsilon_px → nd ≥ log10(S·R/epsilon_px)。

    coef_scalar_count 必须传 **实分量个数 2·(2N+1)**（旧版少算 2 倍）。上界很保守
    （实际误差是随机走 ~√S 而非 S），这里按上界取位以保证契约。
    """
    s = max(int(coef_scalar_count), 1)
    nd = int(math.ceil(math.log10(max(s * float(target_resolution) / max(float(epsilon_px), 1e-9), 1.0))))
    return int(max(2, min(nd, 12)))


def decimals_for_scalar(epsilon_px: float, target_resolution: int) -> int:
    """标量（宽度/坐标/透明度/bbox）小数位：量化步长 = epsilon_px·0.5/R。"""
    qstep = max(float(epsilon_px) * 0.5 / max(float(target_resolution), 1.0), 1e-12)
    return int(max(2, min(int(math.ceil(math.log10(1.0 / qstep))), 9)))


def _q(value: float, nd: int) -> float:
    return float(round(float(value), nd))


def _q_any(value, nd: int):
    """量化数值；分类曲线（shape step）的字符串取值原样保留。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    return _q(float(value), nd)


def _q_curve(curve: dict | None, nd: int, tnd: int | None = None) -> dict | None:
    """量化曲线：数值用 nd，参数 t（keys/stops 的 t）用 tnd（缺省 = nd）。

    t 的精度必须单独由「总弧长像素数」反推：一笔画路径弧长可达 1e4 px 量级，
    用 4 位小数会让 t 误差对应 1–2 个渲染像素，抬笔/作画区间边界会横向抖动。
    """
    if not curve:
        return curve
    c = copy.deepcopy(curve)
    t_nd = nd if tnd is None else tnd
    if "keys" in c and c["keys"]:
        c["keys"] = [[_q_any(k[0], t_nd), _q_any(k[1], nd)] for k in c["keys"]]
    if "coef" in c and c["coef"]:
        c["coef"] = [[int(k), _q(re, nd), _q(im, nd)] for k, re, im in c["coef"]]
    if "stops" in c and c["stops"]:
        c["stops"] = [
            {"t": _q(float(s["t"]), t_nd), "rgb": [int(v) for v in s["rgb"]], "a": _q(float(s.get("a", 1.0)), nd)}
            for s in c["stops"]
        ]
    if "scale" in c and c["scale"] is not None:
        c["scale"] = _q(float(c["scale"]), nd)
    if "strength" in c and c["strength"] is not None:
        c["strength"] = _q(float(c["strength"]), nd)
    return c


def _q_poly(poly, nd: int):
    if not poly:
        return poly
    return [[_q(float(p[0]), nd), _q(float(p[1]), nd)] for p in poly]


def quantize_doc(doc: dict) -> dict:
    """返回按契约量化后的深拷贝文档。"""
    out = copy.deepcopy(doc)
    contract = out.get("render_contract", {})
    eps = float(contract.get("epsilon_px", 0.5))
    res = int(contract.get("target_resolution", 1024))
    snd = decimals_for_scalar(eps, res)
    # 参数 t 的小数位：由最长笔画的弧长像素数反推（t 误差 × 弧长像素 ≤ epsilon_px/2）
    arcs = [
        float(s.get("arc_length", 0.0))
        for s in out.get("strokes", [])
        if isinstance(s.get("arc_length"), (int, float, float))
    ]
    arc_px = max(arcs, default=0.0) * float(res)
    tnd = max(snd, min(9, int(math.ceil(math.log10(max(arc_px, 1.0) / max(eps * 0.5, 1e-9))))))

    for stroke in out.get("strokes", []):
        n = int(stroke.get("N", 0))
        cnd = decimals_for_coef(2 * (2 * n + 1), eps, res)
        if stroke.get("coef"):
            stroke["coef"] = [[int(k), _q(re, cnd), _q(im, cnd)] for k, re, im in stroke["coef"]]
        if "arc_length" in stroke:
            stroke["arc_length"] = _q(float(stroke["arc_length"]), snd)
        if "max_err" in stroke:
            stroke["max_err"] = _q(float(stroke["max_err"]), snd)
        if "bbox" in stroke and stroke["bbox"]:
            stroke["bbox"] = [_q(float(v), snd) for v in stroke["bbox"]]
        if "path_deviation_px" in stroke:
            stroke["path_deviation_px"] = _q(float(stroke["path_deviation_px"]), max(4, snd))
        if "poly" in stroke and stroke["poly"]:
            stroke["poly"] = _q_poly(stroke["poly"], snd)
        # 带洞填充：洞几何与 poly 同精度量化（缩放到 tnd/snd 与 poly 一致）
        holes = stroke.get("fill_holes")
        if isinstance(holes, list) and holes:
            stroke["fill_holes"] = [_q_poly(h, snd) for h in holes if h]
        closure = stroke.get("closure")
        if isinstance(closure, dict) and "t_end" in closure:
            closure["t_end"] = _q(float(closure["t_end"]), tnd)
        brush = stroke.get("brush") or {}
        if "width" in brush:
            brush["width"] = _q_curve(brush["width"], snd, tnd)
    for style in out.get("brush_styles", []):
        for key in ("color", "alpha", "shape"):
            if key in style:
                style[key] = _q_curve(style[key], snd, tnd)
    for rec in out.get("one_stroke_fills", []):
        if isinstance(rec, list) and len(rec) >= 4:
            rec[0] = _q(float(rec[0]), tnd)
            rec[1] = _q(float(rec[1]), tnd)
            rec[3] = _q_poly(rec[3], snd)
            # 第 5 元素=color_field（dict，不动颜色场网格），第 6 元素=fill_holes（世界坐标）
            if len(rec) >= 6 and isinstance(rec[5], (list, tuple)) and rec[5]:
                rec[5] = [_q_poly(h, snd) for h in rec[5] if h]
    for spans in (out.get("strokes") or [{}])[0].get("step_spans") or []:
        if isinstance(spans, list) and len(spans) >= 2:
            spans[0] = _q(float(spans[0]), tnd)
            spans[1] = _q(float(spans[1]), tnd)
    if "quality" in out and isinstance(out["quality"], dict):
        for k, v in list(out["quality"].items()):
            if isinstance(v, float):
                out["quality"][k] = round(float(v), 4)
    out["_quant"] = {"scalar_decimals": int(snd), "t_decimals": int(tnd)}
    return out


# ------------------------------------------------------------------ 文档骨架
def new_document(
    *,
    mode: str,
    source: dict,
    image_class: dict,
    canvas: dict,
    render_contract: dict,
    decomposition: dict,
    base_color: list[int],
    warnings: WarningLog | list[dict],
    accelerators: dict[str, str],
    regions: list[dict] | None = None,
) -> dict:
    """建立 v2 文档骨架（strokes/brush_styles 由调用方填）。"""
    if mode not in (MODE_PER_STROKE, MODE_ONE_STROKE):
        raise FBSError("E_FBS_INVALID", f"未知 mode: {mode}")
    now = datetime.now(_TZ).isoformat(timespec="seconds")
    warn_list = warnings.to_list() if isinstance(warnings, WarningLog) else list(warnings)
    return {
        "format": FORMAT,
        "version": VERSION,
        "mode": mode,
        "created": now,
        "source": source,
        "image_class": image_class,
        "canvas": canvas,
        "render_contract": render_contract,
        "decomposition": decomposition,
        "base_color": [int(v) for v in base_color],
        "regions": list(regions or []),
        "brush_styles": [],
        "strokes": [],
        "stats": {},
        "quality": {},
        "warnings": warn_list,
        "accelerators": dict(accelerators),
        "fingerprint": {},
    }


def finalize_document(doc: dict, *, image_sha256: str | None = None) -> dict:
    """计算 stats 与 fingerprint，返回同一对象（原地）。"""
    strokes = doc.get("strokes", [])
    coef_count = 0
    for st in strokes:
        n = int(st.get("N", 0))
        coef_count += 2 * n + 1 if st.get("coef") else 0
    doc["stats"] = {
        "stroke_count": len(strokes),
        "coef_count": int(coef_count),
        "fill_count": sum(1 for s in strokes if s.get("fill_role") == "region_fill"),
        "region_count": len(doc.get("regions", []) or []),
        "fft_passes": int(doc.get("stats", {}).get("fft_passes", 0) or 0),
    }
    image_sha = image_sha256 or (doc.get("source") or {}).get("sha256", "")
    params = {
        "decomposition": doc.get("decomposition", {}).get("params", {}),
        "render_contract": doc.get("render_contract", {}),
        "codegen": CODEGEN,
    }
    doc["fingerprint"] = {
        "image_sha256": image_sha,
        "params_sha256": sha256_json(params),
        "codegen": CODEGEN,
        "contract_sha256": sha256_json(doc.get("render_contract", {})),
    }
    return doc


# ------------------------------------------------------------------ 校验
def validate(doc: dict) -> list[str]:
    """返回错误字符串列表；空列表表示合法。"""
    errs: list[str] = []

    def err(msg: str) -> None:
        errs.append(msg)

    if doc.get("format") != FORMAT:
        err(f"format != {FORMAT}: {doc.get('format')!r}")
    if str(doc.get("version", "")).split(".")[0] != "2":
        err(f"version 不是 2.x: {doc.get('version')!r}")
    mode = doc.get("mode")
    if mode not in (MODE_PER_STROKE, MODE_ONE_STROKE):
        err(f"mode 非法: {mode!r}")
    canvas = doc.get("canvas") or {}
    for k in ("width", "height", "long_side_units"):
        if k not in canvas:
            err(f"canvas 缺少 {k}")
    contract = doc.get("render_contract") or {}
    for k in ("target_resolution", "epsilon_px"):
        if k not in contract:
            err(f"render_contract 缺少 {k}")
    styles = doc.get("brush_styles") or []
    if not isinstance(styles, list):
        err("brush_styles 必须是列表")
        styles = []
    strokes = doc.get("strokes") or []
    if not isinstance(strokes, list):
        err("strokes 必须是列表")
        return errs
    if not strokes:
        codes = {w.get("code") for w in doc.get("warnings", []) if isinstance(w, dict)}
        if "W_SOLID_COLOR" not in codes:
            err("strokes 为空且没有 W_SOLID_COLOR 警告")
    ids: set[int] = set()
    for i, st in enumerate(strokes):
        tag = f"strokes[{i}]"
        if not isinstance(st, dict):
            err(f"{tag} 不是对象")
            continue
        sid = st.get("id")
        if not isinstance(sid, int):
            err(f"{tag}.id 必须是 int")
        elif sid in ids:
            err(f"{tag}.id 重复: {sid}")
        else:
            ids.add(sid)
        if not isinstance(st.get("closed"), bool):
            err(f"{tag}.closed 必须是 bool")
        if st.get("fill_role") not in FILL_ROLES:
            err(f"{tag}.fill_role 非法: {st.get('fill_role')!r}")
        if st.get("repr", "fourier") != "fourier":
            err(f"{tag}.repr 必须为 fourier（v2 只支持傅里叶主表示）")
        coef = st.get("coef") or []
        if not coef:
            err(f"{tag}.coef 为空")
        else:
            ks = []
            for row in coef:
                if not (isinstance(row, (list, tuple)) and len(row) == 3):
                    err(f"{tag}.coef 行格式错误（应为 [k,re,im]）")
                    break
                k = int(row[0])
                ks.append(k)
                if not (math.isfinite(float(row[1])) and math.isfinite(float(row[2]))):
                    err(f"{tag}.coef 含非有限数")
                    break
            if ks:
                n = st.get("N")
                if not isinstance(n, int) or n != max(abs(k) for k in ks):
                    err(f"{tag}.N 与 coef 最大 |k| 不一致: N={n}, max|k|={max(abs(k) for k in ks)}")
                if any(abs(ks[j]) > abs(ks[j + 1]) for j in range(len(ks) - 1)):
                    err(f"{tag}.coef 未按 |k| 升序")
                if len(set(ks)) != len(ks):
                    err(f"{tag}.coef 含重复 k")
        closure = st.get("closure") or {}
        kind = closure.get("kind")
        if st.get("closed") is True:
            if kind not in ("none", None):
                err(f"{tag}.closure.kind 对闭合曲线应为 none: {kind!r}")
        elif st.get("closed") is False:
            if kind != "hermite_c1":
                err(f"{tag} 开曲线 closure.kind 应为 hermite_c1: {kind!r}")
            t_end = closure.get("t_end")
            if not isinstance(t_end, (int, float)) or not (0.0 < float(t_end) <= 1.0):
                err(f"{tag}.closure.t_end 非法: {t_end!r}")
        brush = st.get("brush") or {}
        if not isinstance(brush.get("style"), int):
            err(f"{tag}.brush.style 必须是 int")
        elif styles and not (0 <= brush["style"] < len(styles)):
            err(f"{tag}.brush.style 越界: {brush['style']} / {len(styles)}")
        if not isinstance(brush.get("width"), dict):
            err(f"{tag}.brush.width 缺失")
        if not isinstance(st.get("layer"), int):
            err(f"{tag}.layer 必须是 int")
        if st.get("fill_role") == "region_fill":
            if not st.get("poly"):
                err(f"{tag} region_fill 缺少 explicit poly")
            holes = st.get("fill_holes")
            if holes is not None:
                if not isinstance(holes, list):
                    err(f"{tag}.fill_holes 必须是列表")
                else:
                    for hi, hole in enumerate(holes):
                        if not isinstance(hole, list) or len(hole) < 3:
                            err(f"{tag}.fill_holes[{hi}] 至少 3 点")
                            continue
                        for p in hole:
                            try:
                                x, y = float(p[0]), float(p[1])
                            except (TypeError, ValueError, IndexError):
                                err(f"{tag}.fill_holes[{hi}] 点非法")
                                break
                            if not (math.isfinite(x) and math.isfinite(y)):
                                err(f"{tag}.fill_holes[{hi}] 含非有限数")
                                break
        for k in ("arc_length",):
            if k in st and not (isinstance(st[k], (int, float)) and float(st[k]) > 0):
                err(f"{tag}.{k} 非法: {st.get(k)!r}")
        for k in ("max_err",):
            if k in st and not (isinstance(st[k], (int, float)) and float(st[k]) >= 0):
                err(f"{tag}.{k} 非法: {st.get(k)!r}")
    for i, style in enumerate(styles):
        if not isinstance(style, dict):
            err(f"brush_styles[{i}] 不是对象")
            continue
        if not isinstance(style.get("color"), dict):
            err(f"brush_styles[{i}].color 缺失")
        if not isinstance(style.get("alpha"), dict):
            err(f"brush_styles[{i}].alpha 缺失")
        if not isinstance(style.get("shape"), dict):
            err(f"brush_styles[{i}].shape 缺失")
    if mode == MODE_ONE_STROKE:
        if len(strokes) != 1:
            err(f"one_stroke 模式必须恰好 1 条笔画，实际 {len(strokes)}")
        if strokes and strokes[0].get("closed") is not True:
            err("one_stroke 笔画必须 closed=True")
        if not isinstance(doc.get("one_stroke_fills"), list):
            err("one_stroke 模式缺少 one_stroke_fills 列表")
    if not (doc.get("fingerprint") or {}).get("params_sha256"):
        err("缺少 fingerprint.params_sha256")
    return errs


# ------------------------------------------------------------------ 读写
def write_fbs(doc: dict, path: str) -> dict:
    """量化 + 校验 + 原子写入 JSON。返回 {path, bytes, decimals, warnings}。"""
    out = quantize_doc(doc)
    quant_meta = out.pop("_quant", {"scalar_decimals": None, "t_decimals": None})
    errs = validate(out)
    if errs:
        raise FBSError("E_FBS_INVALID", f".fbs 校验失败（{len(errs)} 项）", evidence={"errors": errs[:12]})
    text = json.dumps(out, ensure_ascii=False, indent=1, separators=(",", ": "))
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.write("\n")
    os.replace(tmp, path)
    contract = out.get("render_contract", {})
    return {
        "path": path,
        "bytes": len(text.encode("utf-8")),
        "decimals": {
            "scalar": decimals_for_scalar(float(contract.get("epsilon_px", 0.5)), int(contract.get("target_resolution", 1024))),
            "t": quant_meta.get("t_decimals"),
        },
    }


def read_fbs(path: str) -> dict:
    """读取 v2 `.fbs.json` 并校验；v1 文件明确拒绝（v2 是干净重建，不承诺向后读）。"""
    if not os.path.exists(path):
        raise FBSError("E_INPUT_MISSING", f"文件不存在: {path}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except json.JSONDecodeError as exc:
        raise FBSError("E_FBS_INVALID", f"JSON 解析失败: {exc}", evidence={"path": path}) from exc
    if doc.get("format") != FORMAT:
        raise FBSError("E_FBS_INVALID", f"不是 .fbs 文件（format={doc.get('format')!r}）", evidence={"path": path})
    major = str(doc.get("version", "0")).split(".")[0]
    if major != "2":
        raise FBSError(
            "E_FBS_INVALID",
            f"仅支持 v2 .fbs（当前 version={doc.get('version')!r}）。旧 v1 产物请用旧 skill 渲染"
            "或用新版流水线重新生成。",
            evidence={"path": path},
        )
    errs = validate(doc)
    if errs:
        raise FBSError("E_FBS_INVALID", f".fbs 校验失败（{len(errs)} 项）", evidence={"errors": errs[:12], "path": path})
    return doc


# ------------------------------------------------------------------ 指纹/工具
def fingerprint(doc: dict) -> dict:
    fp = doc.get("fingerprint")
    if isinstance(fp, dict) and fp.get("params_sha256"):
        return dict(fp)
    return finalize_document(copy.deepcopy(doc)).get("fingerprint", {})


def same_generation(a: dict, b: dict) -> bool:
    """同代码代 + 同渲染契约 + 同参数才能互相比较。"""
    fa, fb = fingerprint(a), fingerprint(b)
    keys = ("params_sha256", "codegen", "contract_sha256")
    return all(fa.get(k) == fb.get(k) for k in keys)


def coef_to_arrays(coef) -> tuple[np.ndarray, np.ndarray]:
    """[[k,re,im],...] → (k int64 (K,), c complex128 (K,))。"""
    arr = np.asarray(coef, dtype=np.float64)
    if arr.ndim != 2 or arr.shape[1] != 3:
        raise FBSError("E_FBS_INVALID", "coef 形状非法")
    return arr[:, 0].astype(np.int64), arr[:, 1] + 1j * arr[:, 2]


def arrays_to_coef(k: np.ndarray, c: np.ndarray) -> list[list]:
    order = np.argsort(np.abs(k), kind="stable")
    return [[int(k[i]), float(c[i].real), float(c[i].imag)] for i in order]


def source_meta(path: str, *, width: int, height: int, mode: str, scale: float = 1.0) -> dict:
    return {
        "path": str(path),
        "sha256": sha256_file(path) if os.path.exists(path) else "",
        "width": int(width),
        "height": int(height),
        "mode": str(mode),
        "scale_applied": float(scale),
    }


def estimate_bytes(doc: dict) -> int:
    return len(json.dumps(doc, ensure_ascii=False, default=lambda o: None).encode("utf-8"))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
