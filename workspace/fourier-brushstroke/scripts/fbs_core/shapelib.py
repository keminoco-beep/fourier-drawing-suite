"""笔形（shape）注册表：渲染端内核编号 + 合法笔形名。

现代绘画软件（Krita/Photoshop/Procreate/Corel Painter 等）的笔刷差异很大一部分
落在**笔尖形状与边缘/纹理分布**上：硬圆、柔圆、扁平笔头、鬃毛、粉笔、喷溅、
喷枪、铅笔、水彩、墨线……本模块把它们统一成"笔形"这一属性，由渲染端的笔形内核
解释；上层（分解/一手画/时间轴）只引用名字。

新增笔形时必须同步：
- `SHAPE_CODES`（渲染端内核编号）；
- `render._apply_shape_mod` 里的内核实现；
- `brushlib.BRUSHES` 里的预设引用。
"""
from __future__ import annotations

# 渲染端内核编号（0..9；round/soft/marker 保持历史编号不变，兼容旧 .fbs）
SHAPE_CODES = {
    "round": 0,      # 硬圆盘（基础圆头笔）
    "soft": 1,       # 柔边圆（基础软笔）
    "marker": 2,     # 扁平笔头（马克笔/方头）
    "bristle": 3,    # 鬃毛刷（沿笔触横向的条纹）
    "chalk": 4,      # 粉笔/炭笔（颗粒噪声）
    "spatter": 5,    # 喷溅/喷点（稀疏点）
    "airbrush": 6,   # 喷枪（极柔边）
    "pencil": 7,     # 铅笔（细颗粒）
    "watercolor": 8, # 水彩（柔边 + 颜料边缘沉积）
    "ink": 9,        # 墨水笔（硬边、紧抗锯齿）
}

VALID_SHAPES = tuple(SHAPE_CODES.keys())
# 需要"横向纹理/颗粒"内核的笔形（用于快速分支）
TEXTURED_SHAPES = frozenset({"bristle", "chalk", "spatter", "pencil", "watercolor"})
