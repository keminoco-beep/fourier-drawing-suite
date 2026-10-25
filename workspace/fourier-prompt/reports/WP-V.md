# WP-V：SVG 导入器画质修复（fill 接缝 / 64 色 / 细笔画 / 超限策略）

- 日期：2026-10-21 ｜ 状态：**完成**（v1.1，4 类画质/性能修复全部落盘，4 个真实 SVG 前后复测）
- 任务：共享 task-32 ｜ 前置：[WP-T](WP-T.md)（10 个 CC0 复杂 SVG 全链路验证 + 4 类问题根因）
- 约束：R≤384；纯 CPU；不联网/不装包；不改 `schema.py`、渲染器、核心其他文件；不触碰用户图/refs。
- 关键字：`fill_dilate_px`、`overflow=region`、`max_colors=64`、`stroke_to_fill`、`--no-stroke`。

## 0. 结论（TL;DR）

1. **fill 接缝**：新增 `fill_dilate_px`（miter 外扩，孔洞同步收缩）。Gandhi 白缝/缺块像素 **7 805 → 4 404（−43.6%）**，PSNR 对浏览器参考 **14.80 → 16.84 dB**（0.5 px 时 5 858 / 15.95 dB）；渲染耗时不变。剩余白块主要是 `max_paths=600` 裁掉的 4 472 条路径，不是接缝。
2. **64 色**：`max_colors` 硬上限本来就是 schema 的 64；本次把 CLI 默认改为 **64**，并把调色板从“首次出现优先”改为**按墨迹权重排序**。郁金香在 32 色下 PSNR **9.17 → 20.35 dB**、颜色直方图距离 **0.735 → 0.132**；升到 64 色再降到 **0.096**、量化色 364 → 297。偏色主因是旧调色板策略，64 色是二次增益。
3. **细笔画/性能**：郁金香 414 fill + 186 stroke、R=384 基线渲染 **19.3 s**；`--no-stroke` **0.498 s（38.8×）**、`--stroke-to-fill` **0.862 s（22.4×）**。前者更快但丢线稿，后者保留细线，PSNR 19.18 dB（比同配置基线低 0.24 dB）。
4. **超限策略**：新增 `--overflow area|region`。low-poly 沙漠上 **area 仍优**（600 路径 PSNR 5.75 vs region 4.71）；把 `max_paths` 提到 2 000 比任何排序都有效（**7.29 dB**）。`region` 是“同色桶配额”近似，不是真正的多边形合并，默认保持 `area`。
5. **测试**：新增 **11** 例（test_quality.py）；当前 `tests/svg` 共 **101** 例全部通过（原有 74 例未改且通过；另 16 例来自并行代理新增的 `test_quality_gate.py`，其中一条把 `import_seconds` 纳入确定性比较导致偶发红，已改为排除该计时字段）。
6. **API 兼容**：`svg_to_fdsl()` 保持 v1.0 默认（`fill_dilate_px=0.0`、`max_colors=32`、`overflow="area"`、`stroke_to_fill=False`）；画质默认只在 CLI 侧打开（`--max-colors 64 --fill-dilate 0.5`）。

## 1. 改动范围与参数

**核心源文件只改一个**：fourier_prompt/svg_import.py（`IMPORTER_VERSION` 1.0 → 1.1）；测试目录内另有 1 处并行文件的计时断言修正（见 §4/§7）。

| 参数 / CLI | API 默认 | CLI 默认 | 行为 | 落盘位置 |
|---|---|---|---|---|
| `fill_dilate_px` / `--fill-dilate` | `0.0` | `0.5` | 每个 fill 环沿顶点 miter 法线外扩 N px（miter 长度夹到 4×N）；孔洞用负偏移收缩同样 N px；`0` 输出与 v1.0 逐点一致 | `meta.svg_import.fill_dilate_px`、`stats.fill_dilate_px` |
| `overflow` / `--overflow` | `"area"` | `"area"` | `area`：按单条墨迹面积取前 N；`region`：fill 先按量化色桶分组、按桶总墨迹权重给配额（单桶≤35%），余量轮转分配，再按面积补 stroke | `meta.svg_import.overflow`、warning 文案 |
| `max_colors` / `--max-colors` | `32` | **`64`** | 仍是 1..64（65 抛 `SI_E_CONFIG`）；调色板改为按候选墨迹权重（fill 面积 / stroke 长×宽）排序，等权重按文档顺序 | `meta.svg_import.max_colors`、`stats.palette_ranking="ink_weight"` |
| `stroke_to_fill` / `--stroke-to-fill` | `False` | `False` | 画布线宽 ≤ 阈值的 stroke 转为 fill：开放路径 → butt 端帽 ribbon；闭合路径 → outer+inner 的 **keyhole 单多边形**（不用 FDSL `holes`，避免洞被背景色回填） | `meta.svg_import.stroke_to_fill`、`stats.strokes_to_fill` |
| `stroke_to_fill_max_width` / `--stroke-fill-max-width` | `2.0` | `2.0` | 仅影响 `stroke_to_fill=True`；范围 [0.05, 4.0] | 同上 |
| `--no-stroke`（v1.0 已有） | — | 关 | 丢弃全部 stroke，只保留 fill；fills 可用腾出的路径预算 | `stats.strokes=0` |

实现要点：

- 外扩/收缩使用 `_offset_polygon()`：按环面积符号判外法线，逐顶点 miter，4× 夹取，尖角不炸；外环 `+d`、孔洞 `-d`。
- 路径裁剪权重仍用**外扩前**面积，避免 dilation 改变 keep/drop 集合（首版踩过：600 元素中 56 个 fill 挤掉了 stroke）。
- `region` 不是几何 union：按量化色桶聚合，再按 35% 单桶上限 + 轮转配额分配。它只能近似“同色区域优先”，在 low-poly 上不如 `area`（见 §3.4）。
- 所有新参数都进 `plan["meta"]["svg_import"]` 与 `stats`；没有静默行为变化。

## 2. 评测方法

- **参考图**：本机 Edge headless（`--headless=new --disable-gpu`）对原始 SVG 同画布尺寸截图（完整源路径，如 Gandhi 5 072 / 郁金香 1 761 / 沙漠 37 268，浏览器 AA），作为 PSNR 参考；不是用本导入器生成，避免自证。
- **PSNR**：RGB 三通道 MSE → `10·log10(255²/MSE)`，越高越好。
- **白缝/缺块像素**：渲染图三通道均 ≥245 且参考图任一通道 ≤238 的像素数；分母为参考图的彩色像素数。它统计“该有色却变白”的缺口，包含接缝与被裁路径造成的缺块，因此是上界。
- **颜色直方图距离**：RGB 8×8×8 bin 归一化直方图的 total-variation 距离（0 最好，1 最差）。
- **配置**：`target_size=384`、`resolution=384`、`supersample=2`、`max_paths=600`（另测 2 000）、`max_total_points=200 000`、`on_overflow=trim`；画布取 viewBox 长边 384。
- 机器为共享 CPU，计时受并行任务影响，同一张图内部相对比较有效，绝对值有 ±10–20% 波动。

## 3. 前后对比（4 个真实用例）

### 3.1 Gandhi low-poly（260×384，5 072 子路径 → 裁到 600）

| 变体 | import / compile / render (s) | PSNR (dB) | 白缝/缺块 px（占彩色像素） | 连通域数 (≥4 px) | 直方图距离 |
|---|---|---|---|---|---|
| v1.0（WP-T PNG，32 色，无外扩） | 0.532 / 3.399 / 5.747 | 14.80 | 7 805（11.05%） | 163（47） | 0.1723 |
| v1.1 基线（32 色，d=0） | 0.547 / 3.314 / 6.002 | 14.76 | 7 776（11.01%） | 163（47） | 0.0928 |
| 64 色，d=0 | 0.666 / 3.124 / 5.991 | 14.95 | 7 728（10.94%） | 160（47） | 0.0865 |
| **64 色，d=0.5** | 0.731 / 3.016 / 6.007 | **15.95** | **5 858（8.29%）** | **97（45）** | 0.0684 |
| **64 色，d=1.0** | 0.733 / 2.888 / 6.099 | **16.84** | **4 404（6.24%）** | 105（52） | 0.0557 |
| 64 色，d=0.5，region | 0.831 / 2.673 / 0.263 | 9.87 | 30 457（43.12%） | — | 0.3154 |

结论：

- 0.5 px 外扩把白缝像素降 **25%**、连通域降 **40%**；1.0 px 降 **44%**，PSNR +2.08 dB，且渲染时间不变。默认取保守的 0.5，low-poly 建议显式 `--fill-dilate 1.0`。
- 剩余白色缺口集中在眼睛/胡须/肩部，是 `SI_W_PATH_LIMIT` 裁掉 4 472 条路径导致的缺三角，外扩无法补；这印证 WP-T 的判断“路径上限是第一瓶颈”。
- 64 色对这个灰度人像只 +0.19 dB（颜色本来就少），主要收益在别处。
- `region` 会优先保留少数大色桶的 fill、丢掉 103 条 stroke，画面结构崩坏；**不要用于人像/低多边形**，默认仍是 `area`。

### 3.2 郁金香风车（268×384，600/1 761 子路径，414 fill + 186 stroke）

| 变体 | 颜色（量化数） | PSNR (dB) | 白缝/缺块 px | 直方图距离 |
|---|---|---|---|---|
| v1.0（32 色，首次出现优先） | 32（364） | 9.17 | 199（0.19%） | 0.7347 |
| v1.1 基线（32 色，墨迹权重） | 32（364） | **20.35** | 204（0.20%） | **0.1321** |
| **64 色** | **64（297）** | **20.51** | 204（0.20%） | **0.0964** |
| 64 色 + d=0.5 | 64（297） | 19.42 | 70（0.07%） | 0.1083 |

结论：

- **偏色主因是旧调色板“首次出现优先”**：v1.0 把大量花色映射到先出现的深棕色（截图整体发黑），32 色下换权重排序即 **+11.18 dB**、直方图距离降 82%。
- 64 色在本用例再 +0.16 dB，直方图距离再降 27%，量化色 364→297；这就是 task-2 要的“64 色支持”量化证据。
- 外扩在郁金香上略降 PSNR（−1.09 dB），因为把细碎花瓣边缘膨胀、偏离浏览器 AA；白缝本身从 204→70。默认 0.5 是接缝与边缘保真的折中。

### 3.3 细笔画与渲染性能（同一郁金香，R=384）

| 变体 | 元素（fill/stroke） | 颜色 | render (s) | 相对基线 | PSNR (dB) | 白缝/缺块 px |
|---|---|---|---|---|---|---|
| 基线（64 色，d=0.5） | 600（414/186） | 64 | **19.325** | 1.0× | 19.42 | 70 |
| `--stroke-to-fill`（阈值 2.5） | 600（599/1） | 64 | **0.862** | **22.4×** | 19.18 | 70 |
| `--no-stroke` | 600（600/0） | 64 | **0.498** | **38.8×** | 21.43 | 0 |

- `stroke_to_fill` 把 863 个 stroke 子路径候选转成 fill，只剩 1 条 >2.5 px 的 stroke；视觉上风车桁架、草叶细线仍在（见下方支持矩阵）。耗时下降来自不再走 Fourier 笔画采样/FFT。
- `--no-stroke` 最快、PSNR 最高（少画细线反而更接近参考的整体色块），但风车桁架与草叶线稿明显变糊；适合“只看色块/缩略图”的批量场景，不适合线稿/版画。
- 视觉抽查：`stroke_to_fill` 与基线在 384 px 下几乎不可区分，牺牲 0.24 dB 换 22× 速度，是推荐的质量/性能折中。

**支持矩阵（stroke 相关）**

| 输入 | 默认 | `--no-stroke` | `--stroke-to-fill`（宽 ≤ 阈值） | `--stroke-to-fill`（宽 > 阈值） |
|---|---|---|---|---|
| 开放 stroke | Fourier stroke | 丢弃 | butt 端帽 fill ribbon | Fourier stroke |
| 闭合 stroke（circle/rect/closed path） | Fourier stroke | 丢弃 | keyhole 单多边形 fill（无 `holes` 回填） | Fourier stroke |
| 虚线 stroke-dasharray | 实线 stroke + `SI_W_DASH_IGNORED` | 丢弃 | 实线 ribbon + 同一警告 | 实线 stroke + 警告 |
| 极短/退化 stroke | 宽度夹取后 stroke | 丢弃 | 几何退化时整条 shape 回退为普通 stroke | 同左 |
| fill + `fill_dilate_px` | 外扩（含孔洞收缩） | 不变 | **不作用于转换出的 ribbon**（只作用于 SVG fill） | 不变 |

### 3.4 low-poly 沙漠：超限策略（384×256，37 268 子路径）

| 变体 | 元素 | 路径裁剪排序 | import / compile / render (s) | PSNR (dB) | 白缝/缺块 px | 直方图距离 |
|---|---|---|---|---|---|---|
| v1.0（600，area，32 色） | 600 | 单路径面积 | 1.737 / 3.343 / 0.249 | 5.73 | 72 317（73.57%） | 0.7933 |
| v1.1 基线（600，area） | 600 | 单路径面积 | 2.343 / 2.611 / 0.258 | 5.74 | 72 182（73.43%） | 0.7694 |
| 600，area，64 色 | 600 | 单路径面积 | 2.391 / 2.714 / 0.272 | 5.75 | 72 192（73.44%） | 0.7605 |
| **2 000，area，64 色** | 2 000 | 单路径面积 | 1.961 / **9.877** / 0.727 | **7.29** | **49 895（50.76%）** | **0.6097** |
| 600，region，64 色 | 600 | 同色桶配额 | 3.254 / 2.103 / 0.212 | 4.71 | 91 618（93.20%） | 0.9363 |

- `region` 在这里反而更差：37 268 个同尺寸小三角里，按“色桶总权重”会把预算堆到少数大色桶，空间上仍只覆盖局部；真正的修复是**同色相邻区域合并后再简化**，不是换排序。
- `area` 的 600→2 000 提升 +1.55 dB、缺口 73%→51%，再次证明 **`max_paths` 才是该类素材的主瓶颈**；但 2 000 元素编译 ~9.9 s，需与超时预算权衡。
- 结论：默认 `area` 不动；`region` 作为实验接口保留，报告明确其“配额近似”限制。按任务允许的“设计+接口占位”标准，接口、参数、测试、指标均已落地，真正的几何区域合并留作后续。

### 3.5 肖像（377×384，8 115 → 600）旁证

| 变体 | import / compile / render (s) | PSNR (dB) | 白缝/缺块 px | 直方图距离 |
|---|---|---|---|---|
| v1.0（32 色，area） | — / — / — | 21.50 | 28 | 0.1990 |
| v1.1 基线 | 1.869 / 4.087 / 0.980 | 21.50 | 28 | 0.1990 |
| 64 色，region，d=0 | 1.603 / 2.523 / 0.649 | 20.36 | 28 | 0.3448 |
| 64 色，region，d=0.5 | 1.766 / 2.534 / 0.651 | 19.94 | **0** | 0.3540 |

该图本来只有 14 色、白缝极少（28 px），64 色/外扩没有正收益；region 的白缝虽降为 0，但 PSNR 降 1.56 dB，说明 **外扩不是万能增益，干净矢量图应保持 d=0**（API 默认）。

## 4. 测试

新增 test_quality.py，11 例：

1. `test_dilation_expands_fill_and_is_recorded`：fill 外扩 1 px 的 bbox 精确值 + meta/stats。
2. `test_dilation_shrinks_holes`：evenodd 孔洞同步收缩 0.5 px。
3. `test_dilation_config_validation`：`fill_dilate_px` 越界/非数、`overflow` 非法、`stroke_to_fill_max_width` 为 0 均抛 `SI_E_CONFIG`。
4. `test_palette_ranks_by_ink_weight_not_document_order`：小绿/小蓝在前、大红在后，`max_colors=1` 仍保留红色。
5. `test_max_colors_64_is_fully_supported`：64 个不同色全部保留、0 量化；上限 32 时量化 32 个。
6. `test_stroke_to_fill_converts_only_thin_strokes`：1.5 px → 4 点 ribbon，6 px 保持 stroke，面积≈长×宽。
7. `test_stroke_to_fill_closed_ring_uses_keyhole_polygon`：闭合圆环转 fill 且**不带 `holes`**，schema 校验通过。
8. `test_no_stroke_switch_drops_all_strokes`：API `include_stroke=False` 只剩 fill。
9. `test_area_keeps_largest_individual_path`：`max_paths=2` 时 area 保留大红 + 一条小红。
10. `test_region_keeps_whole_dominant_colour_group`：region 保留两条红、warning 含 `same-colour`。
11. `test_cli_quality_flags_reach_plan_metadata`：`--no-stroke --max-colors 64 --fill-dilate 0.5 --overflow region` 全部进 meta，plan 可校验。

运行结果：

```text
python -m unittest discover -s tests/svg -q
Ran 101 tests in 15.9s
OK (skipped=1)
```

- 原有 74 例**未修改**，全部通过；`skipped=1` 是需要 `FDSL_SVG_REAL=1` 的多 MB 真实样例（默认跳过）。
- 另有并行代理新增的 `test_quality_gate.py` 16 例也在同一目录，其中 `test_report_is_deterministic` 把 `import_seconds` 也纳入相等比较（同一进程内 1 ms/2 ms 抖动即失败）。已做最小修正：比较前从 metrics 副本中剔除该计时字段；其余断言未动。

## 5. 复现命令

```powershell
$PY = "<python>"
cd "<workspace>\fourier-prompt"

# 推荐画质默认（CLI：64 色、0.5 px 外扩、细 stroke 转 fill）
& $PY -m fourier_prompt.svg_import `
  --svg "..\poc\complex_online\svg\245096_tulips-and-windmill-scene.svg" `
  --out "$env:TEMP\wpv\tulips.json" --png "$env:TEMP\wpv\tulips.png" `
  --target-size 384 --resolution 384 `
  --max-colors 64 --fill-dilate 0.5 --stroke-to-fill --stroke-fill-max-width 2.5 --json

# 接缝对比：0 / 0.5 / 1.0
& $PY -m fourier_prompt.svg_import --svg "..\poc\complex_online\svg\309601_gandhi-portrait-low-poly.svg" `
  --out "$env:TEMP\wpv\gandhi.json" --png "$env:TEMP\wpv\gandhi.png" `
  --target-size 384 --resolution 384 --max-colors 64 --fill-dilate 1.0 --json

# 超限策略对比：area vs region（默认 area）
& $PY -m fourier_prompt.svg_import --svg "..\poc\complex_online\svg\255209_low-poly-desert-landscape-sunset.svg" `
  --out "$env:TEMP\wpv\desert.json" --png "$env:TEMP\wpv\desert.png" `
  --target-size 384 --resolution 384 --max-colors 64 --overflow area --json

# 测试
& $PY -m unittest discover -s tests/svg -q
```

评测脚本（Edge headless 参考图 + PSNR/白缝/直方图）为一次性脚本，位于系统临时目录 `%TEMP%\wpv\`，未写入仓库；指标定义见 §2，可完全由上述 CLI + 任一 SVG 栅格器复算。

## 6. 限制与风险

1. **外扩是几何偏移，不是底层铺色**：会改变轮廓/孔洞最多 N px；尖角处有 4× miter 上限，但仍可能自交；0.5–1.0 px 在 384 画布上视觉安全，不适合需要像素级轮廓保真的图标。
2. **白缝指标包含缺块**：Gandhi 剩余 4 4xx px 主要是被 `max_paths` 裁掉的三角，外扩无法修复；真正的解是提高上限或做区域合并。报告没有把“白缝”与“缺块”分开声称。
3. **调色板排序改变 v1.0 输出**：同一输入在新版本可能得到不同 palette 顺序与颜色分配；所有 74 个旧测试通过，但下游若快照了旧 palette 需更新。等权重仍按文档顺序，保证确定性。
4. **API/CLI 默认不一致**：API 保持 v1.0 兼容（0.0/32/area），CLI 画质优先（0.5/64/area）。调用方若直接 `svg_to_fdsl()` 需显式传参才能得到报告中的“after”。
5. **`stroke_to_fill` 是近似**：butt 端帽（无圆帽/方帽）、miter 连接、闭合路径 keyhole 有一条零宽缝；自交路径依赖 fill-rule；转换后的 ribbon 不受 `fill_dilate_px` 影响。阈值默认 2.0，郁金香对比用了 2.5。
6. **`region` 不是区域合并**：只是同色桶配额，实测在 low-poly/人像上比 `area` 差，默认关闭；真正的“同色相邻区域合并（栅格化→轮廓→简化）”未实现，按任务允许的接口占位处理并在此明示。
7. **参考图是浏览器渲染**：Edge 的 AA、颜色管理与 `skill_api.render` 不同，PSNR 绝对值受此影响；但同一参考下所有变体可比，且视觉抽查与指标方向一致。
8. **计时受共享机器负载影响**：同一图内相对倍数（22×/39×）可信，绝对秒数有波动。
9. 未改 `schema.py`/`compile.py`/`render_fast.py`；`max_colors` 仍受 schema `MAX_PALETTE=64` 硬约束，无法通过导入器突破 64。
10. 未实现 `fill_underlay`（本次选 dilation 方案）；也未做真正的区域合并/分块渲染。

## 7. 产物与写盘

| 路径 | 说明 |
|---|---|
| fourier_prompt/svg_import.py | v1.1：新参数、调色板权重排序、外扩、stroke_to_fill、overflow、CLI、meta/stats |
| tests/svg/test_quality.py | 新增 11 例（fill/64 色/stroke/overflow/CLI/回归） |
| tests/svg/test_quality_gate.py | 并行代理的文件；仅修正 `test_report_is_deterministic` 对 `import_seconds` 的确定性比较（1 处，其余未动） |
| `reports/WP-V.md` | 本报告 |
| `%TEMP%\wpv\`（仓库外） | 一次性基准脚本、参考 PNG、before/after PNG、`bench*.json`、`post.json` |

未写入/未修改：`schema.py`、`compile.py`、`render_fast.py`、`docs/`、`analysis/`、`poc/complex_online/` 输入与旧结果、用户图/refs。

## 8. 合规声明

- 输入 4 个 SVG 均为 Openclipart CC0-1.0（见 WP-T §2 / `poc/complex_online/meta/`）；只读引用，未重新分发。
- **未联网、未安装任何包**；参考图由本机 Edge headless 离线渲染；无本地 LLM。
- 画布长边 384（R≤384）；纯 CPU；所有时间/PSNR/白缝/直方图指标均来自本次确定性运行，失败/退化未静默（warning 与 stats 保留）。
