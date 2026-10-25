"""FBSConfig：全流水线唯一参数来源。

纪律（旧版 R4 根因）：CLI 的 argparse 默认值一律 None，只有显式给出时才覆盖这里的默认值，
禁止「CLI 默认值无声覆盖 config 默认值」。所有参数都必须可序列化进 .fbs 的
decomposition.params，保证任何产物可复现。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, fields
from typing import Any

from .util import FBSError


@dataclass
class FBSConfig:
    # ---- 渲染契约（Stage0/2/5 共享；N 与精度全部由它反推）----
    target_resolution: int = 1024
    epsilon_px: float = 0.5

    # ---- Stage 0 预处理 ----
    max_pixels: int = 4_000_000
    denoise_sigma: float = 0.6
    alpha_threshold: int = 128
    max_side: int = 0                 # 0 = 不额外限制短边（长边由分辨率决定）

    # ---- 颜色/区域 ----
    colors: int = 0                   # 0 = auto（8..64 由唯一色数导出）
    min_region_area: int = 16
    coverage_block: int = 12          # 局部质量守恒阈值块边长（源像素）
    absorb_small_components: bool = True

    # ---- Stage 1 中轴 ----
    prune_threshold: float = 0.15
    corner_preserve: bool = True
    merge_distance_px: float = 2.0
    min_branch_length_px: float = 8.0
    max_curves: int = 8000            # 安全阀非常规限制；超限按长度降序截断并 warn
    width_mode: str = "auto"          # auto（复杂图→dt / 单笔画类→hybrid）| coverage | dt | hybrid
    width_scale: float = 0.0          # 0 = auto（按图像类别）
    subpixel_axis: str = "auto"       # auto | on | off
    hybrid: str = "auto"              # auto | on | off
    fill_soft_px: float = 0.0         # 0 = auto（lineart 0.5 / illustration 2.0；由 generate 时写入 .fbs）
    color_field_grid: int = 32        # 0 = 关闭区域颜色场
    pale_band_palette: bool = False   # 实验（B 方向）：近背景浅色内容补色；默认关（manga 实测 −0.04dB/SSIM −0.013）
    texture_preserve: bool = False    # 实验（纹理感知）：线稿保留暗底浅底的薄纹理为独立笔画；默认关（manga PSNR −0.45 / SSIM +0.011）
    allow_photo_experimental: bool = False  # 实验：照片输入按"海报化插画"处理（默认拒绝；合同仍延期照片）
    mesh: bool = False                # 连续色调通道：自适应双线性梯度面片（照片实验通道默认启用）
    mesh_tol: float = 0.04            # 面片残差 RMS 容差（0..1）：越小越准、面片越多
    mesh_min_side: int = 4            # 最小面片边（像素）
    mesh_max_cells: int = 12000       # 面片数安全阀
    painterly: bool = False           # 纯笔触绘画：只输出 Fourier 曲线 + 画笔属性（无填充/面片）
    brush_strokes: int = 3000         # 笔触预算
    brush_work_res: int = 512         # 笔触拟合的工作分辨率（长边像素）
    fill_mode: str = "polygon"        # polygon（整块多边形填充）| brush（宽笔扫掠填充，画面随笔画出现）
    path_order: str = "layer"         # layer（填充先铺、线条后画）| lines_first（先线稿后铺色）| spatial（按区域逐块画）| reading（从左到右，logo/文字推荐）

    # ---- Stage 2 傅里叶 ----
    N_mode: str = "error_driven"      # error_driven | energy | fixed
    N_max: int = 2000
    resample_points: int = 4096
    closure: str = "hermite_c1"       # 开曲线闭环方式

    # ---- Stage 3 笔刷 ----
    curve_tol: float = 0.02
    shape_mode: str = "auto"          # auto | round | soft | marker
    brush_jitter: float = 0.0

    # ---- Stage 5 渲染 ----
    supersample: int = 2
    render_mode: str = "sweep"        # stamp | sweep | line

    # ---- R2 一笔画 / R3 视频 ----
    harmonics: int = 65536            # 一笔画目标谐波数（Nyquist 上限内）
    max_fft: int = 1_048_576          # 一笔画 FFT 采样数上限
    min_frames_per_step: int = 1      # 不省略作画步骤
    video_duration: float = 60.0
    video_fps: int = 30
    video_circles: int = 600

    seed: int = 0

    # ------------------------------------------------------------------
    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if self.target_resolution < 32:
            raise FBSError("E_CONFIG", "target_resolution 必须 ≥ 32", evidence={"value": self.target_resolution})
        if not (0 < self.epsilon_px <= 8):
            raise FBSError("E_CONFIG", "epsilon_px 必须在 (0, 8]", evidence={"value": self.epsilon_px})
        if self.supersample < 1 or self.supersample > 4:
            raise FBSError("E_CONFIG", "supersample 必须在 1..4", evidence={"value": self.supersample})
        if self.N_mode not in ("error_driven", "energy", "fixed"):
            raise FBSError("E_CONFIG", f"未知 N_mode: {self.N_mode}")
        if self.width_mode not in ("auto", "coverage", "dt", "hybrid"):
            raise FBSError("E_CONFIG", f"未知 width_mode: {self.width_mode}")
        if self.hybrid not in ("auto", "on", "off"):
            raise FBSError("E_CONFIG", f"未知 hybrid: {self.hybrid}")
        if self.subpixel_axis not in ("auto", "on", "off"):
            raise FBSError("E_CONFIG", f"未知 subpixel_axis: {self.subpixel_axis}")
        if self.fill_mode not in ("polygon", "brush"):
            raise FBSError("E_CONFIG", f"未知 fill_mode: {self.fill_mode}")
        if self.path_order not in ("layer", "lines_first", "spatial", "reading"):
            raise FBSError("E_CONFIG", f"未知 path_order: {self.path_order}")
        if self.shape_mode not in ("auto", "round", "soft", "marker"):
            raise FBSError("E_CONFIG", f"未知 shape_mode: {self.shape_mode}")
        if self.harmonics < 64:
            raise FBSError("E_CONFIG", "harmonics 必须 ≥ 64", evidence={"value": self.harmonics})
        if self.max_fft < 256:
            raise FBSError("E_CONFIG", "max_fft 必须 ≥ 256", evidence={"value": self.max_fft})

    # ------------------------------------------------------------------
    def contract(self) -> dict[str, float]:
        """Stage2/5 共享的渲染契约。"""
        return {
            "target_resolution": int(self.target_resolution),
            "epsilon_px": float(self.epsilon_px),
        }

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("seed", None)  # seed 不参与内容哈希（只影响可复现性）
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FBSConfig":
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in valid})

    @classmethod
    def merge_cli(cls, base: dict[str, Any] | None = None, **cli: Any) -> "FBSConfig":
        """用显式 CLI 参数覆盖配置：None 一律忽略（防止默认值无声覆盖）。"""
        data = dict(base or {})
        for k, v in cli.items():
            if v is not None:
                data[k] = v
        return cls(**{k: v for k, v in data.items() if k in {f.name for f in fields(cls)}})
