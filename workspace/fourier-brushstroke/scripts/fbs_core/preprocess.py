"""Stage 0 :: 载入 / 模式归一化 / 像素预算 / 图像分类。

职责
----
1. `load_and_prepare`：任意 Pillow 可解码位图 → 统一 `RGBA8` + `rgb`/`alpha` float 视图；
   GIF / 多页 TIFF 取首帧并告警；> `max_pixels` 等比降采样并告警；损坏 / 过小 / 全透明
   明确抛 `FBSError`（带机器可读 code），绝不静默降级。
2. `classify`：用**可测量特征**（唯一色数、颜色熵、边缘密度、饱和度占比、噪声水平、
   平坦区占比）区分 lineart / flat_art / illustration / gradient / photo。
   照片类（D5）**明确拒绝**并附实测证据与替代建议；不提供越权开关。

关键设计（均为旧版实测教训，不是风格选择）
----------------------------------------
* alpha 阈值 `cfg.alpha_threshold` 同时用于「可见性」与「全透明判定」：alpha ≤ 阈值的像素
  不参与颜色统计，避免透明区域把调色板拉向 (0,0,0)。
* 16bit / `I` / `F` 先按位深归一化到 0..255 再转 8bit，**不能直接 convert("L")**
  （Pillow 对 I;16 直接转换会截断高 8 位，整图变黑）。
* 降采样用 LANCZOS：它保留抗锯齿过渡信息，后续覆盖率场（coverage.py）正是靠这些
  过渡带恢复亚像素几何；用 NEAREST 会把亚像素结构直接判死。
* 分类的熵用 **4bit/通道的分箱直方图**（4096 桶）。它刻意对「大量相近色阶」不敏感：
  照片在 4bit 空间里接近均匀（熵 ≥6），而抗锯齿/JPEG 造成的数千种灰阶在线稿里
  仍挤在少数桶内（熵 <2）。这是唯一可靠区分「灰阶多」与「连续色调」的低成本特征。
* 噪声用 **3×3 盒平滑残差的 MAD**（乘 1.4826 换成 σ 口径）：MAD 对少量强边缘鲁棒，
  用 std 会把线稿的硬边当成噪声（实测 horn 的 std 残差 17.4，MAD 残差 0.0）。
* 「平坦区占比」= 3×3 局部标准差的像素占比：照片纹理使该值接近 0，渐变/平涂接近 1。
  它是把「平滑渐变」与「照片」分开的关键维度（两者熵都高）。
"""
from __future__ import annotations

import os
from typing import Any

import numpy as np
from PIL import Image, ImageOps

from .config import FBSConfig
from .util import FBSError, WarningLog, sha256_file

# 受支持扩展名（用于错误信息；实际以 Pillow 能否解码为准）
SUPPORTED_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff")
# 最小边长（设计文档 v2.1 §6.1：<32×32 拒绝）
MIN_SIDE = 32
# 分类特征的分箱/抽样常量（与数据规模相关，不是数据上限）
_ENTROPY_BITS_PER_CHANNEL = 4
_UNIQUE_SAMPLE_PIXELS = 1_000_000
# 像素预算的「解码安全上限」倍率：远超 max_pixels 的图先拒绝解码，避免内存打爆
_DECODE_BOMB_FACTOR = 256
# 噪声 MAD → σ 口径的换算系数（正态分布下 MAD·1.4826 = σ）
_MAD_TO_SIGMA = 1.4826


# ---------------------------------------------------------------- 载入与归一化
def _open_image(image_path: str) -> Image.Image:
    """打开并完整解码图像；所有失败路径转成带 code 的 FBSError。"""
    path = os.fspath(image_path)
    if not os.path.exists(path):
        raise FBSError(
            "E_INPUT_MISSING",
            f"输入文件不存在：{path}",
            suggestions=["确认路径拼写（支持中文路径）", "用绝对路径重试"],
        )
    try:
        with Image.open(path) as probe:
            size = probe.size
            n_frames = getattr(probe, "n_frames", None)
            frame_count = 1 if n_frames is None else max(1, int(n_frames))
            # 解码前先审尺寸：解压炸弹在 load() 里才爆内存，挡不住就没有可读错误
            if size[0] * size[1] > _DECODE_BOMB_FACTOR * 10**9:
                raise FBSError(
                    "E_INPUT_CORRUPT",
                    f"图像尺寸异常（{size[0]}×{size[1]}），拒绝解码以免耗尽内存",
                    evidence={"width": int(size[0]), "height": int(size[1])},
                )
            probe.load()
            probe.info["_fbs_frame_count"] = frame_count
            img = probe.copy()
    except FBSError:
        raise
    except FileNotFoundError as exc:  # pragma: no cover - 已由 exists 挡掉
        raise FBSError("E_INPUT_MISSING", f"输入文件不存在：{path}") from exc
    except Image.UnidentifiedImageError as exc:
        raise FBSError(
            "E_INPUT_FORMAT",
            f"无法识别的图像格式：{path}。受支持：{', '.join(SUPPORTED_EXTS)}",
            suggestions=[
                "若是 SVG / PDF 等矢量文件：它们本身即矢量，无需本工具转换",
                "先转成 PNG / JPEG 再重试",
            ],
        ) from exc
    except (
        OSError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombError,
    ) as exc:
        raise FBSError(
            "E_INPUT_CORRUPT",
            f"图像损坏或无法解码：{path}（{type(exc).__name__}: {exc}）",
            suggestions=["确认文件完整（重新下载/导出）", "用图像查看器打开一次验证"],
        ) from exc
    return img


def _normalize_to_rgba8(img: Image.Image, meta: dict[str, Any]) -> np.ndarray:
    """任意 Pillow 模式 → (H,W,4) uint8 RGBA。不能解码的模式抛 E_INPUT_MODE。"""
    meta["mode"] = img.mode
    mode = img.mode
    if mode in ("P", "PA"):
        meta["had_palette"] = True
        return np.asarray(img.convert("RGBA"), dtype=np.uint8)
    if mode in ("CMYK", "YCbCr"):
        meta["had_cmyk"] = mode == "CMYK"
        return np.asarray(img.convert("RGB").convert("RGBA"), dtype=np.uint8)
    if mode == "1":
        meta["bit_depth"] = 1
        return np.asarray(img.convert("L").convert("RGBA"), dtype=np.uint8)
    if mode in ("I", "I;16", "I;16B", "I;16L", "I;16N", "F"):
        arr = np.asarray(img)
        if arr.dtype == np.bool_:
            arr = arr.astype(np.float64) * 255.0
        else:
            arr = np.asarray(arr, dtype=np.float64)
        finite = np.isfinite(arr)
        hi = float(arr[finite].max()) if finite.any() else 0.0
        if mode == "F":
            # 浮点图约定 0..1；若明显超出则按实测最大值归一化
            scale = 255.0 if hi <= 1.0 + 1e-6 else (255.0 / max(hi, 1e-9))
            meta["bit_depth"] = 32 if hi > 1.0 + 1e-6 else 8
        elif mode == "I":
            # 32bit 整数：常见是 16bit 数据落在 I 里；≤255 视为已经是 8bit 口径
            if hi <= 255.0:
                scale = 1.0
                meta["bit_depth"] = 8
            else:
                scale = 255.0 / 65535.0 if hi <= 65535.0 else 255.0 / max(hi, 1e-9)
                meta["bit_depth"] = 16
        else:  # I;16 系列：固定 16bit 位深
            scale = 255.0 / 65535.0
            meta["bit_depth"] = 16
        gray = np.clip(arr * scale, 0.0, 255.0)
        meta["intensity_max"] = round(hi, 3)
        return np.asarray(
            Image.fromarray(gray.astype(np.uint8)).convert("RGBA"), dtype=np.uint8
        )
    if mode in ("L", "LA", "RGB", "RGBA", "RGBX"):
        return np.asarray(img.convert("RGBA"), dtype=np.uint8)
    # 其余罕见模式：能做就做，做不了明确报错
    try:
        converted = img.convert("RGB").convert("RGBA")
    except (OSError, ValueError) as exc:
        raise FBSError(
            "E_INPUT_MODE",
            f"不支持的像素模式：{mode}（{type(exc).__name__}: {exc}）",
            suggestions=["用 Pillow/PIL 先 convert('RGBA') 后再输入"],
        ) from exc
    meta["mode_converted"] = True
    return np.asarray(converted, dtype=np.uint8)


def _resize_rgba8(rgba: np.ndarray, new_w: int, new_h: int) -> np.ndarray:
    """LANCZOS 等比缩放 RGBA8（保留抗锯齿过渡信息）。"""
    img = Image.fromarray(np.ascontiguousarray(rgba), mode="RGBA")
    return np.asarray(img.resize((int(new_w), int(new_h)), Image.LANCZOS), dtype=np.uint8)


def load_and_prepare(image_path: str, cfg: FBSConfig, warn: WarningLog) -> dict:
    """Stage0 主入口：位图 → 归一化 RGBA8 视图（含 rgb/alpha float 视图）。

    返回 dict（键固定，供 coverage/decompose/document 使用）：
      path, sha256, rgba (H,W,4 uint8), rgb (H,W,3 float32 0..1), alpha (H,W) float32,
      width, height, long_side, scale, source_mode, original_size, multiframe, meta
    """
    img = _open_image(image_path)
    meta: dict[str, Any] = {}
    n_frames_info = img.info.get("_fbs_frame_count", None)
    frame_count = 1 if n_frames_info is None else max(1, int(n_frames_info))

    # --- EXIF 方向（手机照片/截图常见）；失败只告警不中止 ---
    try:
        transposed = ImageOps.exif_transpose(img)
        if transposed is not None:
            img = transposed
    except (AttributeError, OSError, ValueError) as exc:
        warn.add(
            "W_DOWNSCALED",
            f"EXIF 方向信息处理失败，按原始方向继续（{type(exc).__name__}: {exc}）",
            severity="info",
        )

    if frame_count > 1:
        img.seek(0)
        warn.add(
            "W_MULTIFRAME",
            f"多帧/动图输入（共 {frame_count} 帧），仅取首帧",
            severity="info",
        )
    meta["multiframe"] = frame_count

    rgba = _normalize_to_rgba8(img, meta)
    h, w = int(rgba.shape[0]), int(rgba.shape[1])
    original_size = (w, h)

    if min(h, w) < MIN_SIDE:
        raise FBSError(
            "E_INPUT_TOO_SMALL",
            f"图像过小：{w}×{h}，最小支持 {MIN_SIDE}×{MIN_SIDE}",
            evidence={"width": w, "height": h, "min_side": MIN_SIDE},
            suggestions=[f"把图像放大到至少 {MIN_SIDE}×{MIN_SIDE} 再输入"],
        )

    # --- 像素预算：> max_pixels 等比降采样 + 告警 ---
    scale = 1.0
    if w * h > int(cfg.max_pixels):
        scale = float(np.sqrt(cfg.max_pixels / float(w * h)))
        nw = max(MIN_SIDE, int(round(w * scale)))
        nh = max(MIN_SIDE, int(round(h * scale)))
        rgba = _resize_rgba8(rgba, nw, nh)
        warn.add(
            "W_DOWNSCALED",
            f"像素数 {w}×{h}={w * h} 超过上限 {cfg.max_pixels}，"
            f"已等比降采样到 {nw}×{nh}（scale={scale:.4f}）",
        )
        h, w = nh, nw
    # --- max_side>0 时长边额外限制 ---
    if int(cfg.max_side) > 0 and max(h, w) > int(cfg.max_side):
        s2 = float(cfg.max_side) / float(max(h, w))
        nw = max(MIN_SIDE, int(round(w * s2)))
        nh = max(MIN_SIDE, int(round(h * s2)))
        rgba = _resize_rgba8(rgba, nw, nh)
        warn.add(
            "W_DOWNSCALED",
            f"长边 {max(h, w)} 超过 max_side={cfg.max_side}，缩放到 {nw}×{nh}",
        )
        scale *= s2
        h, w = nh, nw
        if min(h, w) < MIN_SIDE:
            raise FBSError(
                "E_INPUT_TOO_SMALL",
                f"按 max_side 缩放后图像过小：{w}×{h}",
                evidence={"width": w, "height": h},
            )

    # --- 透明度：全透明明确报错；半透明保留（alpha 参与覆盖率/alpha 反演）---
    alpha_u8 = np.ascontiguousarray(rgba[..., 3])
    alpha_thr_u8 = int(np.clip(cfg.alpha_threshold, 0, 255))
    visible = alpha_u8 > alpha_thr_u8
    if not bool(visible.any()):
        raise FBSError(
            "E_INPUT_TRANSPARENT",
            "图像无可见内容：所有像素 alpha 均不高于阈值 "
            f"{alpha_thr_u8}（alpha 最大值 {int(alpha_u8.max())}）",
            evidence={"alpha_threshold": alpha_thr_u8, "alpha_max": int(alpha_u8.max())},
            suggestions=["检查图像是否导出成空画布", "先与白色背景合成为不透明 PNG"],
        )

    alpha = np.ascontiguousarray(rgba[..., 3].astype(np.float32) / 255.0)
    # 半透明/透明像素的 RGB 是**直通色**，直接用它做覆盖率投影会把「0 alpha 的墨色」
    # 误当成墨。统一合成为「白底上的显示结果」：
    #   rgb = alpha·直通色 + (1−alpha)·白
    # 这样 alpha 的墨量被折进覆盖率（cov 投影 = alpha），参考图也与最终渲染口径一致；
    # 同时 img["alpha"] 仍保留原始 alpha，供 element 的 alphas 反演使用。
    rgb_straight = rgba[..., :3].astype(np.float32) / 255.0
    composite = alpha[..., None]
    rgb = np.ascontiguousarray(rgb_straight * composite + (1.0 - composite))

    return {
        "path": os.fspath(image_path),
        "sha256": sha256_file(os.fspath(image_path)),
        "rgba": rgba,
        "rgb": rgb,
        "alpha": alpha,
        "width": int(w),
        "height": int(h),
        "long_side": int(max(w, h)),
        "scale": float(scale),
        "source_mode": str(meta.get("mode", "RGBA")),
        "original_size": original_size,
        "multiframe": frame_count,
        "meta": meta,
    }


# ---------------------------------------------------------------- 分类特征
def _box3_mean(arr: np.ndarray) -> np.ndarray:
    """3×3 均值滤波（edge 填充），用于噪声与平坦区估计。"""
    p = np.pad(arr, 1, mode="edge")
    return (
        p[0:-2, 0:-2] + p[0:-2, 1:-1] + p[0:-2, 2:]
        + p[1:-1, 0:-2] + p[1:-1, 1:-1] + p[1:-1, 2:]
        + p[2:, 0:-2] + p[2:, 1:-1] + p[2:, 2:]
    ) / 9.0


def _gray01(rgb: np.ndarray) -> np.ndarray:
    """线性亮度（0..1）。"""
    return (
        0.299 * rgb[..., 0].astype(np.float64)
        + 0.587 * rgb[..., 1].astype(np.float64)
        + 0.114 * rgb[..., 2].astype(np.float64)
    )


def _color_entropy_bits(rgb_u8: np.ndarray) -> float:
    """4bit/通道分箱的颜色熵（bits，上限 12）。"""
    bits = _ENTROPY_BITS_PER_CHANNEL
    q = (rgb_u8 >> (8 - bits)).astype(np.int64)
    idx = (q[..., 0] << (2 * bits)) | (q[..., 1] << bits) | q[..., 2]
    hist = np.bincount(idx.ravel(), minlength=1 << (3 * bits)).astype(np.float64)
    total = float(hist.sum())
    if total <= 0.0:
        return 0.0
    p = hist[hist > 0] / total
    return float(-(p * np.log2(p)).sum())


def _unique_color_count(rgb_u8: np.ndarray) -> int:
    """唯一颜色数（>100 万像素时等距抽样，抽样不改变量级判断）。"""
    flat = rgb_u8.reshape(-1, 3)
    if flat.shape[0] > _UNIQUE_SAMPLE_PIXELS:
        step = flat.shape[0] // _UNIQUE_SAMPLE_PIXELS + 1
        flat = flat[::step]
    packed = (
        (flat[:, 0].astype(np.uint32) << 16)
        | (flat[:, 1].astype(np.uint32) << 8)
        | flat[:, 2].astype(np.uint32)
    )
    return int(np.unique(packed).size)


def measure_features(img: dict) -> dict[str, float]:
    """计算分类用的可测特征（全部是统计量，不含任何图像类别先验）。"""
    rgb = np.asarray(img["rgb"], dtype=np.float64)
    alpha = np.asarray(img["alpha"], dtype=np.float64)
    rgb_u8 = np.asarray(img["rgba"])[..., :3]
    visible = alpha > 0.5

    gray = _gray01(rgb)
    gx = np.zeros_like(gray)
    gy = np.zeros_like(gray)
    gx[:, 1:-1] = (gray[:, 2:] - gray[:, :-2]) * 0.5
    gy[1:-1, :] = (gray[2:, :] - gray[:-2, :]) * 0.5
    mag = np.sqrt(gx * gx + gy * gy)

    smooth = _box3_mean(gray)
    residual = gray - smooth
    res_med = float(np.median(residual)) if residual.size else 0.0
    noise_mad = float(
        _MAD_TO_SIGMA * np.median(np.abs(residual - res_med)) * 255.0
    ) if residual.size else 0.0
    local_var = np.maximum(_box3_mean(gray * gray) - smooth * smooth, 0.0)
    local_std = np.sqrt(local_var) * 255.0
    flat_frac = float((local_std < 2.0).mean()) if local_std.size else 1.0

    if bool(visible.any()):
        sub = rgb[visible]
        mx = sub.max(axis=1)
        mn = sub.min(axis=1)
        sat = (mx - mn) / np.maximum(mx, 1.0)
        sat_mean = float(sat.mean())
        sat_frac = float((sat > 0.25).mean())
    else:
        sat_mean = 0.0
        sat_frac = 0.0

    return {
        "unique_colors": float(_unique_color_count(rgb_u8)),
        "entropy_bits": round(_color_entropy_bits(rgb_u8), 3),
        "edge_density": round(float(mag.mean()), 5),
        "hard_edge_frac": round(float((mag > 0.12).mean()), 5),
        "mean_saturation": round(sat_mean, 4),
        "colorful_frac": round(sat_frac, 4),
        "noise_level": round(noise_mad, 3),
        "flat_frac": round(flat_frac, 4),
        "visible_frac": round(float(visible.mean()), 4),
    }


# 分类阈值：全部由旧版实测校准 + 本机两张真实图（chibi / horn）与合成用例复核。
# 说明见模块 docstring；数值均为**可测量特征**上的判据，不是魔法拍脑袋常量。
_PHOTO_MIN_UNIQUE = 4096.0
_PHOTO_MIN_ENTROPY = 6.0
_PHOTO_MIN_NOISE = 1.5
_PHOTO_MAX_FLAT = 0.6
_LINEART_MAX_SAT = 0.12
_LINEART_MAX_COLORFUL = 0.03
# 稀疏签名/极细线稿的硬边像素占比可以低到 ~1%（一条线只占画面 1–2%），
# 因此线稿判据的硬边门槛要低于 flat_art；chibi 这类彩色插画由 colorful_frac 排除。
_LINEART_MIN_HARD_EDGE = 0.008
_FLAT_MAX_UNIQUE = 512.0
_FLAT_MAX_HARD_EDGE = 0.08
_GRADIENT_MIN_UNIQUE = 2048.0
_GRADIENT_MAX_NOISE = 0.8
_GRADIENT_MIN_FLAT = 0.85
_GRADIENT_MAX_HARD_EDGE = 0.02


def classify(img: dict, cfg: FBSConfig) -> dict:
    """图像分类：返回 {"klass": str, "evidence": {...}}；照片类附 "reject"。

    klass ∈ {lineart, flat_art, illustration, gradient, photo}。
    photo 时返回 reject 结构（document.py 会转成 FBSError("E_PHOTO_DEFERRED")），
    并在 img 上记录 klass 供 coverage 使用。
    """
    feats = measure_features(img)
    uniq = feats["unique_colors"]
    ent = feats["entropy_bits"]
    noise = feats["noise_level"]
    flat = feats["flat_frac"]
    sat = feats["mean_saturation"]
    colorful = feats["colorful_frac"]
    hard = feats["hard_edge_frac"]

    is_photo = (
        uniq >= _PHOTO_MIN_UNIQUE
        and ent >= _PHOTO_MIN_ENTROPY
        and noise >= _PHOTO_MIN_NOISE
        and flat < _PHOTO_MAX_FLAT
    )
    if is_photo:
        klass = "photo"
        evidence = dict(feats)
        message = (
            "检测为照片类输入，本 Skill 暂缓支持（D5）。\n"
            f"  实测判定依据：唯一颜色数={int(uniq)}（阈值 ≥{int(_PHOTO_MIN_UNIQUE)}）、"
            f"颜色熵={ent:.2f} bits（阈值 ≥{_PHOTO_MIN_ENTROPY:.1f}）、"
            f"噪声水平={noise:.2f}（阈值 ≥{_PHOTO_MIN_NOISE:.1f}）、"
            f"平坦区占比={flat:.3f}（阈值 <{_PHOTO_MAX_FLAT:.1f}）。\n"
            "  原因：照片中不存在有意义的区域划分，也不存在稳定中轴；"
            "按区域/中轴分解会得到上万条噪声笔画，重建结果无意义。"
        )
        suggestions = [
            "先做插画化 / 海报化 / 色阶量化（外部工具），再输入本 Skill",
            "若目标是矢量线稿，先用边缘检测或描线工具提取线稿",
            "若需要照片级矢量化，本 Skill 不是合适工具（见 references/capability_boundary.md）",
        ]
        reject = {
            "code": "E_PHOTO_DEFERRED",
            "message": message,
            "evidence": evidence,
            "suggestions": suggestions,
        }
        if bool(getattr(cfg, "allow_photo_experimental", False)) or bool(getattr(cfg, "painterly", False)):
            # 实验通道：显式开启时按"连续色调/纯笔触绘画"处理；默认仍拒绝，合同不变。
            img["klass"] = "illustration"
            img["class_evidence"] = evidence
            return {"klass": "illustration", "evidence": evidence, "photo_experimental": True}
        img["klass"] = klass
        img["class_evidence"] = evidence
        return {"klass": klass, "evidence": evidence, "reject": reject}

    if sat <= _LINEART_MAX_SAT and hard >= _LINEART_MIN_HARD_EDGE and colorful <= _LINEART_MAX_COLORFUL:
        klass = "lineart"
    elif uniq < _FLAT_MAX_UNIQUE and hard <= _FLAT_MAX_HARD_EDGE:
        klass = "flat_art"
    elif (
        uniq >= _GRADIENT_MIN_UNIQUE
        and noise <= _GRADIENT_MAX_NOISE
        and flat >= _GRADIENT_MIN_FLAT
        and hard <= _GRADIENT_MAX_HARD_EDGE
    ):
        klass = "gradient"
    else:
        klass = "illustration"

    evidence = dict(feats)
    evidence["klass_rules"] = {
        "photo": [int(_PHOTO_MIN_UNIQUE), _PHOTO_MIN_ENTROPY, _PHOTO_MIN_NOISE, _PHOTO_MAX_FLAT],
        "lineart": [_LINEART_MAX_SAT, _LINEART_MIN_HARD_EDGE, _LINEART_MAX_COLORFUL],
        "gradient": [_GRADIENT_MIN_UNIQUE, _GRADIENT_MAX_NOISE, _GRADIENT_MIN_FLAT, _GRADIENT_MAX_HARD_EDGE],
    }
    img["klass"] = klass
    img["class_evidence"] = evidence
    return {"klass": klass, "evidence": evidence}
