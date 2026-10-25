# 笔刷库（现代绘图软件分类 → 可执行预设）
调研依据（只取分类/行为，不复制素材）：
- Krita 4 预设包分类：Basic / Pencils / Inking / Markers / Dry Painting / Chalk·Pastel·Charcoal / Wet Painting / Watercolors / Blender / Spray …
  <https://docs.krita.org/en/reference_manual/krita_4_preset_bundle.html>
- Krita 笔刷引擎：Pixel / Bristle / Chalk / Color Smudge / Spray / Particle …
  <https://docs.krita.org/en/reference_manual/brushes/brush_engines.html>
- Photoshop 笔刷预设家族（General/Bristle/Erodible/Airbrush/Wet Media/Special Effect）与动态项（Shape/Scatter/Texture/Color/Transfer）；
  Procreate 笔刷库分区（Sketching/Inking/Painting/Artistic/Texture…）；
  Corel Painter 笔刷类别（Acrylics/Oils/Watercolor/Palette Knives/Pastel…）作为分类对照。

## 笔形内核（render 端注册，`shapelib.py`）
| shape | 编号 | 行为 |
|---|---|---|
| `round` | 0 | 硬圆盘（圆头笔） || `soft` | 1 | 柔边圆（基础软笔） || `marker` | 2 | 扁平笔头（马克笔/平头刷，各向异性） || `bristle` | 3 | 鬃毛刷：沿笔触横向的确定性条纹（油脂/干画） || `chalk` | 4 | 粉笔/炭笔：确定性颗粒覆盖 || `spatter` | 5 | 喷溅：稀疏点阵 || `airbrush` | 6 | 喷枪：二次衰减极柔边 || `pencil` | 7 | 铅笔：细颗粒 || `watercolor` | 8 | 水彩：柔边 + 颜料边缘沉积 || `ink` | 9 | 墨水笔：硬边紧抗锯齿 |
所有内核都是「精确带覆盖 × 确定性纹理/衰减核」，无贴图、无填充；同一笔触/像素每帧结果一致（视频不闪烁）。

## 笔刷预设（20 个，按分区）
| id | 名称 | 分区 | 笔形 | size× | alpha | 宽度抖动 | 颜色抖动 | 压力 | 说明 |
|---|---|---|---|---|---|---|---|---|---|
| `basic_round_hard` | Basic Round Hard | basic | `round` | 1.0 | 0.92–1.0 | 0.1 | 0.006 | 0.55 | 基础硬圆笔；起稿/铺色 |
| `basic_round_soft` | Basic Round Soft | basic | `soft` | 1.05 | 0.78–0.95 | 0.12 | 0.008 | 0.65 | 基础柔边圆笔；大色块与过渡 |
| `basic_airbrush` | Airbrush Soft | airbrush | `airbrush` | 1.5 | 0.5–0.8 | 0.15 | 0.01 | 0.45 | 喷枪：极柔边、低不透明度；雾化/环境色 |
| `pencil_2b` | Pencil 2B | pencil | `pencil` | 0.55 | 0.7–0.92 | 0.22 | 0.012 | 0.35 | 铅笔：细、颗粒、压力敏感；草稿/细节 |
| `pencil_hb` | Pencil HB | pencil | `pencil` | 0.42 | 0.65–0.88 | 0.18 | 0.01 | 0.3 | 硬铅笔：更细更淡；起形 |
| `ink_fineliner` | Ink Fineliner | inking | `round` | 0.48 | 0.85–1.0 | 0.06 | 0.004 | 0.25 | 针管笔：等宽细线；轮廓/排线 |
| `ink_gpen` | Ink GPen | inking | `ink` | 0.5 | 0.95–1.0 | 0.05 | 0.004 | 0.3 | G 笔：硬边墨线、粗细随压力 |
| `ink_brush_rough` | Ink Brush Rough | inking | `bristle` | 0.35 | 0.85–1.0 | 0.12 | 0.006 | 0.45 | 毛笔勾线：鬃毛纹理、带飞白 |
| `sumi_e` | Sumi-e Brush | inking | `bristle` | 1.2 | 0.7–0.95 | 0.25 | 0.01 | 0.8 | 写意水墨：宽笔、强压力变化、鬃毛 |
| `marker_chisel` | Marker Chisel | marker | `marker` | 0.9 | 0.75–0.92 | 0.1 | 0.006 | 0.4 | 方头马克笔：扁笔头、半透明叠加 |
| `marker_alcohol` | Marker Alcohol | marker | `marker` | 1.1 | 0.55–0.8 | 0.12 | 0.01 | 0.35 | 酒精马克笔：更透、叠加混色感 |
| `bristle_oil` | Bristle Oil | dry_painting | `bristle` | 1.0 | 0.8–0.97 | 0.18 | 0.014 | 0.6 | 油画鬃毛：条纹笔触、覆盖力强 |
| `dry_flat` | Dry Flat Brush | dry_painting | `marker` | 1.15 | 0.82–0.98 | 0.14 | 0.012 | 0.5 | 干画法平头刷：宽扁、边缘干涩 |
| `palette_knife` | Palette Knife | dry_painting | `marker` | 1.25 | 0.9–1.0 | 0.2 | 0.01 | 0.3 | 刮刀：厚涂、硬边、长条肌理 |
| `chalk` | Chalk | chalk_pastel_charcoal | `chalk` | 1.05 | 0.72–0.95 | 0.28 | 0.02 | 0.55 | 粉笔：颗粒强、覆盖不均 |
| `charcoal` | Charcoal | chalk_pastel_charcoal | `chalk` | 1.15 | 0.75–0.98 | 0.3 | 0.022 | 0.6 | 炭笔：更黑更粗、飞白 |
| `pastel` | Pastel | chalk_pastel_charcoal | `chalk` | 0.95 | 0.65–0.9 | 0.24 | 0.018 | 0.5 | 粉彩：柔和颗粒、色彩轻微变化 |
| `wet_oil` | Wet Oil | wet_painting | `bristle` | 0.95 | 0.6–0.85 | 0.16 | 0.016 | 0.55 | 湿画法油彩：半透明、与底层混色感（无真实smudge） |
| `watercolor_wash` | Watercolor Wash | watercolor | `watercolor` | 1.35 | 0.45–0.7 | 0.18 | 0.014 | 0.45 | 水彩淡彩：低不透明度、颜料边缘沉积 |
| `spatter_brush` | Spatter Spray | spray | `spatter` | 1.05 | 0.5–0.8 | 0.35 | 0.02 | 0.4 | 喷点：稀疏颗粒，点缀/肌理 |

## 由粗到细的分层用法（`brushfit.py`）
| 层（笔刷半径 @512） | 当前允许笔刷（按权重） |
|---|---|
| 22px（铺底） | `basic_round_soft`、`bristle_oil`、`dry_flat`、`watercolor_wash` |
| 14px | `bristle_oil`、`dry_flat`、`basic_round_soft`、`marker_chisel`、`wet_oil` |
| 9px | `bristle_oil`、`chalk`、`dry_flat`、`marker_alcohol`、`pastel`、`ink_gpen` |
| 5.5px | `bristle_oil`、`chalk`、`ink_gpen`、`pencil_2b`、`palette_knife`、`pastel` |
| 3px（细节） | `ink_gpen`、`bristle_oil`、`pencil_2b`、`chalk`、`ink_fineliner`、`pastel` |

这套映射模拟真实画师：铺底色用软圆/鬃毛油画笔，中景用干画法/粉笔，细节用墨线笔/铅笔；笔刷属性（宽度曲线、alpha、颜色抖动、压力锥度）随笔画进入时间轴。

## 与视频/时间轴的关系
每个笔刷预设最终落到 `.fbs` 的 `brush_styles[].shape`（分类曲线）与逐笔 width 曲线；`timeline` 导出的 `styles` 里能看到每类笔触的 shape/color/alpha 曲线，画笔时间轴上可直接替换笔刷预设对应属性。

## 实测（lenna，R=1024）

笔刷库版：5213 笔 / 0 填充，PSNR 22.164 / SSIM 0.6511，时间轴 9.8MB；逐笔渲染约 670s。纹理/半透明笔刷会牺牲部分像素保真，换来鬃毛/颗粒/墨线等真实笔触层次（对照无笔刷库版 22.9dB 的均匀柔笔）。
