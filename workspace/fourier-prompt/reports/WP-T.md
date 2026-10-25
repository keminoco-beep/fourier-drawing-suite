# WP-T：复杂图像 → 矢量 → FDSL → CPU 傅里叶渲染验证

- 日期：2026-10-21 ｜ 状态：**完成（10/10 复杂 SVG + 2/2 位图矢量化全部跑通，发现 4 类真实降级/失败模式）**
- 任务：共享 task-30 ｜ 前置：分析 15 · 复杂图像生成路线、WP-S `fourier_prompt/svg_import.py`（v1.0）
- 约束：**不用本地 LLM**；Fourier 系数全部由 `compile_plan`（numpy/FFT）确定性计算；渲染 `R=384 ≤ 512`；未触碰 `[X(Twitter)] ttus...` 参考图。

## 0. 结论（TL;DR）

1. **在线 CC0 复杂 SVG 10/10 全链路成功**：`svg_import → FDSL v2.1 → compile_plan → skill CPU render → PNG`，无导入/编译/渲染异常；批量墙钟 **89.9 s**，其中导入 5.7 s / 编译 34.0 s / 渲染 43.5 s（R=384，`max_paths=600`）。
2. 覆盖真实复杂度：最大 **37 268 个子路径**（low-poly 场景）、**12 485 个 `<path>`**（街景照片描摹）、4 MB 单文件（Mughal 肖像）、2 550 路径 low-poly 人像；点总数 165 544。
3. **视觉验收**：7/10 可辨且结构正确（海滩、斑马、怪物、线稿、汽车剪影、街景、肖像可辨但偏色）；2/10 部分失败（Gandhi 白色裂缝、郁金香风车偏色）；1/10 视觉失败（低多边形沙漠，只剩稀疏三角形）。
4. **位图→矢量**：`vtracer 0.6.15`（CPU）+ 2 张 Met 公有领域浮世绘（CC0）→ SVG → FDSL → PNG，**PSNR 18.99 / 23.54 dB，SSIM 0.708 / 0.750**（cutout + 64 色）；同一素材 stacked + 32 色只有 15.84 / 19.72 dB——**调色板上限与 vtracer 分层模式是保真度主因**。
5. 失败均可解释、可复现：600 路径上限按“墨迹面积”裁剪、32/64 色量化、相邻填充多边形接缝、vtracer stacked 全画布带孔图层与导入器 fill-rule 近似的叠加效应。

## 1. 环境与范围

| 项 | 值 |
|---|---|
| Python | 3.13.14（`<python>`） |
| 库 | numpy 2.5.4、Pillow 12.3.0、scipy 1.18.1、**vtracer 0.6.15（本次安装）** |
| 导入器 | `fourier_prompt.svg_import` v1.0（WP-S，2026-10-21 落盘，未修改） |
| 渲染 | `compile_plan(resolution=384)` → `compat.skill_api.render(supersample=2)` |
| 画布 | 长边 384 px（R≤512） |
| 素材许可 | Openclipart = CC0-1.0；Met Open Access = CC0（均记录 URL/作者/SHA-256） |

## 2. 素材：10 个开放许可复杂 SVG（`poc/complex_online/svg/`）

全部来自 Openclipart（100% Public Domain / CC0-1.0），逐件保留 detail 页 JSON-LD 许可证据（`meta/<id>.json`）。

| # | ID / 文件 | 标题 | 作者 | 主题 | 字节 | SVG 内路径 | 子路径（导入器计） | SHA-256（前 16） |
|---|---|---|---|---|---|---|---|---|
| 1 | 349708_portrait-of-a-beauty | Portrait of a Beauty | shuvashish76 | 人物/名画 | 4 076 495 | 14 | **8 115** | f2b81fa2ae60e109 |
| 2 | 309601_gandhi-portrait-low-poly | Gandhi Portrait Low Poly | GDJ | 人物/low-poly | 285 844 | 2 550 | 5 072 | 0261dded09d18b52 |
| 3 | 316748_ancient-greek-woman-line-art-14 | Ancient Greek Woman Line Art 14 | GDJ | 人物/线稿 | 263 704 | 1 | 441 | 0acfbb29dbf9c5f7 |
| 4 | 263394_vintage-mythical-monster-illustration | Vintage Mythical Monster Illustration | GDJ | 插画/版画 | 216 552 | 225 | 583 | a185f3f8aa42f4f9 |
| 5 | 260552_realistic-zebra-illustration | Realistic Zebra Illustration | GDJ | 动物插画 | 262 865 | 773 | 847 | 4e450cc81ef13d78 |
| 6 | 255209_low-poly-desert-landscape-sunset | Low Poly Desert Landscape Sunset | GDJ | 场景/low-poly | 3 694 379 | 0（40 650 polygon） | **37 268** | de57efd50e4d7d0a |
| 7 | 227690_Street-Scene-01 | Street Scene 01 | EdR | 街景/照片描摹 | 1 783 849 | **12 485** | 8 461 | 54cb8ee27fd374c6 |
| 8 | 221924_beach-scene | Beach Scene | GDJ | 场景/扁平插画 | 92 691 | 222 | 454 | 5ff864ec418d9e75 |
| 9 | 342602_vintage-automobile | Vintage Automobile | trygstad | 物件/剪影 | 709 175 | 1 | 1 987 | 663b8b3a2c3d177a |
| 10 | 245096_tulips-and-windmill-scene | Tulips And Windmill Scene | GDJ | 场景 | 362 258 | 830 | 1 761 | 2a2bb9b0a5c63fd4 |

下载 URL 形如 `https://openclipart.org/download/<id>/<slug>.svg`，detail 页 `https://openclipart.org/detail/<id>/<slug>`；完整字段见 `meta/index.json`。

> 说明：这 10 件均**不含渐变 / text / filter / image / clipPath / mask**（`features` 全 0），因此本次没有触发 `SI_W_GRADIENT_APPROX` / `SI_W_TEXT_SKIPPED` 等降级码；这些路径由 WP-S 单测覆盖。选样刻意偏向“结构复杂”而非“特效复杂”。

## 3. SVG → FDSL → PNG 结果（`max_paths=600`、R=384、`on_overflow=trim`）

| 文件 | 子路径 seen → kept（dropped） | 元素 | fill/stroke | 点 | 颜色(量化) | import / compile / render (s) | 非背景占比 | warnings | 视觉判定 |
|---|---|---|---|---|---|---|---|---|---|
| 221924_beach-scene | 454 → 454 (0) | 454 | 222/232 | 4 612 | 32 (0) | 0.059 / 2.729 / 8.517 | 0.84 | 9 | **成功**，结构/配色正确 |
| 227690_Street-Scene-01 | 8 461 → 600 (7 861) | 600 | 600/0 | 31 199 | 32 (0) | 1.200 / 3.975 / 0.362 | 0.754 | 10 | **成功**，照片场景可辨（细节丢失） |
| 245096_tulips-and-windmill-scene | 1 761 → 600 (1 161) | 600 | 414/186 | 19 441 | 32 (549) | 0.257 / 3.969 / **18.041** | 0.920 | 12 | **部分失败**：风车/天空偏色，层次丢失 |
| 255209_low-poly-desert-landscape-sunset | **37 268 → 600 (36 668)** | 600 | 600/0 | 1 882 | 32 (530) | 1.737 / 3.343 / 0.249 | 0.9995 | 11 | **视觉失败**：只剩稀疏三角形 |
| 260552_realistic-zebra-illustration | 847 → 600 (247) | 600 | 83/517 | 11 150 | 23 (0) | 0.157 / 4.487 / 8.830 | 0.259 | 11 | **成功**，斑马线稿完整 |
| 263394_vintage-mythical-monster-illustration | 583 → 583 (0) | 583 | 583/0 | 9 843 | 1 (0) | 0.115 / 2.708 / 0.287 | 0.303 | 10 | **成功**，版画剪影正确 |
| 309601_gandhi-portrait-low-poly | 5 072 → 600 (4 472) | 600 | 497/103 | 1 800 | 32 (515) | 0.532 / 3.399 / 5.747 | 0.639 | 11 | **部分失败**：白色裂缝 + 缺三角 |
| 316748_ancient-greek-woman-line-art-14 | 441 → 441 (0) | 441 | 441/0 | 9 438 | 1 (0) | 0.104 / 2.219 / 0.172 | 0.563 | 1 | **成功**，线稿干净 |
| 342602_vintage-automobile | 1 987 → 600 (1 387) | 600 | 600/0 | 15 560 | 1 (0) | 0.324 / 3.115 / 0.306 | 0.676 | 3 | **成功**（源为剪影，黑色轮廓正确） |
| 349708_portrait-of-a-beauty | **8 115 → 600 (7 515)** | 600 | 600/0 | **60 619** | 14 (0) | 1.248 / 4.033 / 0.935 | 0.696 | 12 | **可辨**，脸部/头纱正确但偏橄榄色、细节丢失 |

- 合计：**10/10 无异常**；墙钟 89.9 s（最长单件 23.0 s）；元素点数 165 544；PNG 见 `png/`，FDSL 见 `plans/`，contact sheet 见 `contact_sheet.png`。
- 3 件未触顶（454 / 583 / 441），7 件触顶被裁剪；`compile_plan` 对 600 个 fill（含 60 k 点）最慢 4.5 s，渲染最慢 18.0 s（414 fill + 186 stroke 的郁金香场景）。
- 渲染画布由 viewBox 决定（如 384×246、260×384、384×288），未强行拉伸。

### 3.1 降级码统计（本次实际出现）

| 代码 | 触发件数 | 含义/证据 |
|---|---|---|
| `SI_W_PATH_LIMIT` | 7 | 按墨迹面积裁剪；349708 丢 7 515/8 115，255209 丢 36 668/37 268 |
| `SI_W_FILL_RULE_APPROX` | 2（349708 11 条） | 单路径 310–956 个子环，超出嵌套分析上限，逐环独立填充 |
| `SI_W_HOLE_LIMIT` | 1（349708） | 单元素 175 个孔 > 64，最小的被丢弃 |
| `SI_W_COLOR_QUANTIZED` | 3（245096 549、255209 530、309601 515） | 超过 32 色调色板，映射到最近色 |
| `SI_W_STROKE_WIDTH_CLAMPED` | 1（221924，222/232） | viewBox 单位换算后线宽超上限被夹取 |
| `SI_W_DEGENERATE_SKIPPED` | 少量 | 极小面积环被丢弃 |
| `SI_W_UNSUPPORTED_ELEMENT` | 1 | Inkscape `<namedview>` 跳过（无害） |

## 4. 位图 → 矢量（vtracer）→ FDSL → PNG

### 4.1 素材（公有领域，Met Open Access，CC0）

| 文件 | 作品 | 作者/年代 | 原图 | Met object |
|---|---|---|---|---|
| `images/met_45294.jpg` | Twilight Moon at Ryōgoku Bridge | 歌川广重，约 1830s | 3.12 MB | https://www.metmuseum.org/art/collection/search/45294 |
| `images/met_56906.jpg` | Station Thirty-Eight: Fujikawa, Scene at the Border（东海道五十三次） | 歌川广重，约 1833–34 | 2.88 MB | https://www.metmuseum.org/art/collection/search/56906 |

`meta/met_*.json` 保存 Met API 返回的 `isPublicDomain: true`、`primaryImage`、`creditLine`、`objectURL`。

### 4.2 流程与参数

`原图 → 长边 512 px PNG（LANCZOS）→ vtracer(CPU) → SVG → svg_import(max_paths=2000) → compile_plan → PNG(384) → 与原图同画布尺寸比 PSNR/SSIM`。
vtracer 固定：`colormode=color, mode=spline, filter_speckle=8, color_precision=6, layer_difference=24, corner_threshold=60, length_threshold=4, path_precision=8`；对比 `hierarchical=stacked` + `max_colors=32` 与 `cutout` + `max_colors=64`。

### 4.3 结果

| 运行 | 元素 / 点 | 颜色(量化) | vt / import / compile / render (s) | PSNR | SSIM | 画布 |
|---|---|---|---|---|---|---|
| 45294 stacked + 32 色 | 574 / 20 002 | 32 (538) | 0.74 / 0.27 / 2.88 / 0.54 | 15.84 dB | 0.624 | 384×246 |
| **45294 cutout + 64 色** | **690 / 15 016** | 64 (570) | 0.66 / 0.22 / 3.07 / 0.39 | **18.99 dB** | **0.708** | 384×246 |
| 56906 stacked + 32 色 | 423 / 16 680 | 32 (390) | 0.56 / 0.27 / 2.29 / 0.38 | 19.72 dB | 0.716 | 384×256 |
| **56906 cutout + 64 色** | **503 / 12 334** | 64 (389) | 0.56 / 0.26 / 2.45 / 0.28 | **23.54 dB** | **0.750** | 384×256 |

结论：
- 全链路成功，2 张浮世绘在 384 px 下结构、构图、船/桥/人物位置可辨（`png/met_*_vt_cutout.png`）。
- `cutout + 64 色` 比 `stacked + 32 色` 提升 **+3.15 / +3.82 dB**、SSIM +0.084 / +0.033。32 色时分别量化了 538 / 390 个源色，是主要失真源。
- `stacked` 模式（每层=整画布+孔洞）在 45294 上出现大片**白色空洞**：vtracer 的全画布图层依赖孔洞与叠放顺序，导入器对非零填充规则的近似（同向环独立填充）无法完全还原；`cutout` 模式（互不重叠色块）明显更稳。56906 在两种模式下都完整。
- PSNR 18–24 dB 属于“结构保真、色彩偏保守”的量级：除量化外，还有 512→384 重采样、纸纹/云纹高频细节被抹平、fill 接缝。

## 5. 失败原因与最小复现

| 现象 | 根因（证据） | 建议 |
|---|---|---|
| 低多边形沙漠只剩稀疏三角形 | `SI_W_PATH_LIMIT`：37 268 个 ~同面积小 polygon，按墨迹面积取前 600（1.6%），画面必然支离破碎 | 该类素材不适合路径级 FDSL；需区域合并/栅格底图，或把硬上限提升到万级并分块渲染 |
| Gandhi 人像白色裂缝、缺三角 | 5 072 个子路径裁到 600；相邻三角形 fill 各自独立光栅化，抗锯齿边缘互相不覆盖，露出白底 | fill 外扩 0.5–1 px / 先铺底色层；或对 low-poly 提高上限 |
| 郁金香风车偏色 | 32 色量化 549 个色 + 1 161 子路径被裁；源图依赖细碎色块 | `max_colors` 提到 64（硬上限）；或先做颜色聚类再导入 |
| 45294 白色空洞 | vtracer `stacked` 的全画布带孔路径 + 导入器 fill-rule 近似（同向环独立填充） | 位图矢量化默认用 `hierarchical="cutout"` |
| 渲染 18 s（郁金香） | 414 fill + 186 stroke，stroke 走 Fourier 笔画 | 复杂 SVG 可 `--no-stroke` 或提高 `stroke-width` 阈值批量转 fill；R=384 下仍可接受 |

最小复现（PowerShell）：

```powershell
$PY = "<python>"
cd "<workspace>\fourier-prompt"

# 单个 SVG 全链路（导入 + 编译 + 渲染，含 stats/warnings）
& $PY -m fourier_prompt.svg_import --svg "..\poc\complex_online\svg\349708_portrait-of-a-beauty.svg" `
  --out "..\poc\complex_online\plans\demo.json" --png "..\poc\complex_online\png\demo.png" `
  --target-size 384 --resolution 384 --json

# 位图 → 矢量 → FDSL → PNG（含 PSNR/SSIM）
& $PY "..\poc\complex_online\vec_pipeline.py" --max-paths 2000 --max-colors 64 --hierarchical cutout --tag cutout
```

失败证据：`results/255209_*.json`（`paths_dropped=36668`）、`results/309601_*.json`（`SI_W_PATH_LIMIT`）、`results/met_45294_vt_2000.json`（32 色/PSNR 15.84）。所有结果 JSON 均保留 `stats`、`warnings`、分阶段耗时，未静默跳过。

## 6. 限制与后续建议（给 WP-U / planner）

1. **路径上限是复杂图像的第一瓶颈**：600 对“照片描摹 / 低多边形”远远不够，2000 也仅覆盖 5%；建议按“元素类型”分流——填充优先，超限时合并同色相邻区域（marching squares 后简化），而不是单纯按面积丢弃。
2. **填充接缝**需要渲染端修补（dilate 0.5–1 px 或底层同色铺底），否则任何多色块矢量图都会有白缝；这比提高路径上限更能改善观感。
3. **调色板**默认 32 太紧；对 vtracer/照片类素材建议默认 64，并在导入 stats 中把 `quantized_colors` 作为质量信号暴露给 planner。
4. **位图矢量化固定 `cutout`**，并把 vtracer 参数（`color_precision/layer_difference/filter_speckle`）与目标 `max_paths` 联动（先粗后细，超限自动加 `filter_speckle`）。
5. **性能**：R=384 下 600–700 元素编译 2–4.5 s、渲染 0.2–18 s；若 WP-U 要批量 10 prompt，建议并行进程 + 单件 60 s 超时，郁金香这类 stroke 密集件单独计时。
6. 本次未覆盖渐变/text/filter（素材不含）；OmniSVG 真实输出可能包含，需要在 WP-U 用含这些特性的样本复测 `SI_W_*` 降级链。

## 7. 产物清单（均在 `poc/complex_online/`）

| 路径 | 内容 |
|---|---|
| `svg/*.svg`（10） | 下载的 CC0 复杂 SVG |
| `meta/index.json`、`meta/<id>.json` | 许可/作者/URL/SHA-256/结构特征证据 |
| `plans/*.json`（14） | FDSL v2.1 计划（10 SVG + 4 矢量化运行） |
| `png/*.png`（14） | CPU 渲染结果 |
| `results/*.json`（14） | 每件 stats/warnings/分阶段耗时/PSNR/SSIM |
| `summary.json` | 汇总（totals + svg_runs + vector_runs） |
| `contact_sheet.png` | 12 图拼版（10 SVG + 2 最佳矢量化） |
| `fetch_openclipart.py` / `run_one.py` / `run_batch.py` / `vec_pipeline.py` / `summarize.py` | 可复现脚本 |
| `images/met_*.jpg`、`images/*_512.png`、`vtracer/*.svg` | 位图源、vtracer 输入/输出 |
| `pip_vtracer.log` | vtracer 安装记录 |

## 8. 合规声明

- 所有素材均为 **CC0-1.0 / Public Domain**（Openclipart 站点声明 + 逐件 JSON-LD；Met Open Access `isPublicDomain=true`），作者与许可 URL 已逐件记录。
- **未使用** `[X(Twitter)] ttus (@ttuttus_)` 目录下任何参考图；未训练、未调用本地 LLM；Fourier 系数与栅格化均由 numpy/Pillow/编译器确定性完成。
- 仅写入 `poc/complex_online/` 与本报告；`fourier_prompt/svg_import.py` 等核心文件**只读未改**。vtracer 为系统级 pip 安装（3.13 env），已记录版本与日志。
