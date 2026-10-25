# WP-W：Agent-authored SVG 工作流产品化 + 质量门禁 + 6 个复杂示例（task-33）

- 日期：2026-10-21 ｜ 状态：**完成（6/6 示例通过严格门禁，全部导入/编译/R=384 渲染成功，0 warning / 0 丢弃）**
- 写盘边界：`fourier-prompt/skill/templates/svg_drawing.md`、`fourier-prompt/fourier_prompt/svg_quality.py`、`fourier-prompt/tests/svg/`、`poc/agent_svg/`、本报告
- 未改动：`svg_import.py`（只读使用）、`skill/` 其他文件、核心/其他 docs/analysis/ttus；**未用本地 LLM/本地模型、未训练、未使用第三方图片或字体**
- 环境：Windows / Python 3.13.14 / numpy 2.5.4 / Pillow 12.3.0；R=384；纯 CPU

## 0. TL;DR

1. **模板**：`skill/templates/svg_drawing.md` 把“在线 agent 写复杂 SVG”固化为可执行规范——画布/构图（满幅、ink bbox ≥55%）、4 层结构、路径语法白名单/黑名单、30–120 形状 / 400–3000 点 / 2–6 色预算、按题材画法、13 项门禁、自查清单、可复制骨架。
2. **门禁**：`fourier_prompt/svg_quality.py` 用 `svg_import` 的真实几何 + Pillow 粗栅格（默认长边 256）估计 ink/bbox，输出 `ok / score / metrics / checks / errors / warnings`；CLI `python -m fourier_prompt.svg_quality --svg X.svg --json [--strict] [--config gate.json]`，退出码 0/1/2。
3. **6 个复杂示例**（人物/风景/动物/机械/静物/建筑）全部由 agent 编写/程序化生成，SVG 内注明 `agent-authored, not model output`：
   **77–208 个源形状、108–289 个导入元素、946–2 484 点、2–4 色、ink 4.1–11.9%、bbox 60–94%、严格门禁 6/6 通过、score 88.9–100**；导入 0 warning、0 路径丢弃；R=384 编译 0.80–1.73 s、渲染 2.40–8.06 s，合计 26.65 s。
4. **测试**：新增 `tests/svg/test_quality_gate.py` **16 例**（正常/路径不足/低 ink/小 bbox/unsupported/阈值/空 SVG/XML 错误/确定性/CLI 0-1 退出码/CLI config/6 示例严格门禁/6 示例转换渲染/WP-U 失败样本回归）；`tests/svg` 全量 **101 tests OK（skipped=1）**，未破坏原有 74 例。
5. 产物：`poc/agent_svg/{svg,plans,png,results,summary.json,contact_sheet.png,make_agent_svgs.py,run_batch.py,README.md}`。

## 1. 交付物

| 文件 | 内容 | 验证 |
|---|---|---|
| `fourier-prompt/skill/templates/svg_drawing.md` | 复杂 SVG 编写规范（构图/图层/路径/预算/门禁/清单/骨架） | 6 个示例按此规范实现并通过 |
| `fourier-prompt/fourier_prompt/svg_quality.py` | 质量门禁 API + CLI（13 项检查、可配阈值、0–100 评分） | 16 个单测 + 6 示例 |
| `fourier-prompt/tests/svg/test_quality_gate.py` | 门禁/示例测试 16 例 | 全量 101 tests OK |
| `poc/agent_svg/svg/*.svg`（6） | 6 个复杂示例源文件 | 严格门禁 6/6、导入 0 warning |
| `poc/agent_svg/plans/*.json`（6） | FDSL v2.1 | `compile_plan` 成功 |
| `poc/agent_svg/png/*.png`（6） | R=384 CPU 渲染 | 目检联系表 |
| `poc/agent_svg/results/*.json`（6）、`summary.json` | 每图/汇总指标、SHA-256、分阶段耗时 | 可复算 |
| `poc/agent_svg/contact_sheet.png` | 6 图拼版（带形状/点数/score/ink/bbox） | 目检 |
| `poc/agent_svg/make_agent_svgs.py` / `run_batch.py` / `README.md` | 生成器 / 批量流水线 / 复现说明 | 一键复跑 |

## 2. 模板（`skill/templates/svg_drawing.md`）

面向**在线 agent**（不是模型 prompt），核心约束：

- **画布**：必须有 `viewBox`；人物 480×640、风景 640×420、方形 560×560、动物 560×520、静物 520×620；主体 ink bbox ≥55%（硬门禁 30%），白底 underlay 第一层防填充白缝（WP-T 的 Gandhi 裂缝）。
- **图层**：`underlay → mass（白 fill + 黑描边）→ ink（轮廓/五官）→ detail（排线，opacity≈0.7）`；只用顶层 `<g>`，嵌套样式继承明确。
- **白名单**：path M/L/H/V/C/S/Q/T/A/Z、polygon/polyline/rect/circle/ellipse/line、g/transform、fill/stroke/opacity。
- **黑名单**：渐变、`text`、`image`、`foreignObject`、`use`、`filter/clip-path/mask/marker`、`pattern`、`stroke-dasharray`（导入器会降级/跳过，直接判失败）。
- **预算**：30–120 形状、400–3000 点、2–6 色、单 path ≤800 点、线宽 ≥0.8（480 画布）、文件 <200 KB；超过 600 元素会被 trim。
- **题材画法**：人物（三大块→五官→发丝→排线）、风景（远中近三层/地平线 55–65%）、动物（剪影→毛流）、机械（硬边+齿轮+铆钉+对称）、静物（桌面透视→剪影→花/果/布褶）、建筑（中轴+重复单元+玫瑰窗+边框）。
- **门禁与失败修复顺序**：先 bbox（缩放/平移）→ ink（加排线/加粗/减实心）→ 形状数（拆 path/加细节）→ unsupported；**不允许调门禁通过**。
- **自查清单 11 项**：viewBox/underlay、bbox≥55%、形状/点数/颜色、禁用特性、闭合 fill、线宽、`ok=true & score≥80`、导入 0 error、注释、PNG 目检。
- 末尾附可复制的 480×640 半身像骨架（underlay/mass/ink/detail 四组）。

## 3. 质量门禁（`fourier_prompt/svg_quality.py`）

### 3.1 方法

1. `ElementTree` 解析源 SVG，统计绘制形状（path/rect/circle/ellipse/line/polyline/polygon）、组数、unsupported 特性（标签 + `filter/clip-path/mask/marker-*` 属性；`defs` 内的渐变也会计入，但不把 `defs` 子元素当形状）。
2. 调 `svg_to_fdsl(..., target_size=384, max_paths=2000, max_colors=64)` 得到**真实几何**与导入 stats（`paths_seen/shapes_seen/elements/points/colors/paths_dropped/warnings`）。
3. 用 Pillow 在长边 256 的栅格上按文档顺序画 fill 多边形与 stroke 折线（painter's algorithm；白/浅灰填充按背景对比判定），统计 **ink 覆盖率** 与 **ink bbox**；背景默认白，ink 判定阈值 `max|RGB-bg| ≥ 24`。
4. 生成 13 项 check 与错误码 `SQ_E_*`；`ok = all(checks)`；`score` 为确定性 0–100 启发式。

### 3.2 默认阈值（可 `--config` / CLI 参数 / `QualityThresholds` 覆盖）

| 指标 | 默认 | 说明 |
|---|---|---|
| `paths` | ≥ 10 | 导入后 fill/stroke 路径数；WP-U 的 3-path 剪影会被拒 |
| `shapes` | ≥ 10 | 源形状数 |
| `points` | ≥ 150 | 总点数（严格模式 ≥400） |
| `ink_ratio` | 1%–92% | 非背景像素占比；过低=空/太小，过高=实心剪影 |
| `bbox_ratio` | ≥ 30%（≤100%） | ink bbox 面积/画布；WP-U P2 的 0.6% 会被拒 |
| `colors` | ≤ 64 | 导入调色板数（>32 会量化，>64 拒绝） |
| `unsupported` | ≤ 2 次（严格 0） | gradient/text/image/filter/clip/mask/pattern/use |
| `dropped_ratio` | ≤ 35% | 导入 trim 比例，高则细节丢失 |
| `empty` / `degenerate` | 必须 false | 无元素/无点/ink<0.1%/bbox<1% |

`--strict` 额外要求 ≥30 形状、≥400 点、0 unsupported（即模板预算）。

**对 WP-U 两个失败样本的回归**（实测）：

| 样本 | 门禁指标 | 结果 |
|---|---|---|
| `poc/online_gen/p1_girl_horns.svg`（OmniSVG 4B） | 3 路径 / 3 形状 / 704 点 / ink 99.2% | **FAIL**：`min_paths`、`min_shapes`、`max_ink`（score 42.5） |
| `poc/online_gen/p2_cottage_moon.svg`（SVGThinker-7B） | 29 路径 / 17 形状 / 230 点 / ink 0.53% / bbox 2.47% | **FAIL**：`min_ink`、`min_bbox`（score 27.4） |

即 WP-U 建议的“path<10、ink<1%、bbox<30%”规则已落地，并被这两个真实失败样本验证。

### 3.3 评分（`score_report`）

`25×min(1, paths/80) + 15×min(1, points/1600) + 25×ink_band（2%–25% 满分） + 20×min(1, bbox/0.80) + 15×cleanliness（unsupported/trim/warnings 扣分）`，四舍五入 1 位，范围 0–100。评分仅用于横向比较与报告，**不是语义/美术评分**。

### 3.4 CLI

```powershell
$PY = "<python>"
cd "<workspace>\fourier-prompt"
& $PY -m fourier_prompt.svg_quality --svg ..\poc\agent_svg\svg\01_portrait.svg --json
& $PY -m fourier_prompt.svg_quality --svg X.svg --strict
& $PY -m fourier_prompt.svg_quality --svg X.svg --config gate.json --min-bbox 0.4
# 退出码：0 pass / 1 质量失败 / 2 用法或输入错误
```

## 4. 六个复杂示例

### 4.1 结果（R=384，`run_batch.py`，2026-10-21）

| # | 主题 | viewBox → 画布 | 形状 / 元素 (fill/stroke) | 点数 | 颜色 | ink | bbox | 严格门禁 | score | import / compile / render (s) |
|---|---|---|---|---|---:|---:|---:|---|---:|---|
| 1 | 人物半身线稿（长发女性/耳环/衣领） | 480×640 → 288×384 | 117 / 143 (27/116) | 1 344 | 2 | 7.5% | 70% | PASS | 95.1 | 0.017 / 1.480 / 3.785 |
| 2 | 风景（月夜湖/山/松/小屋/围栏） | 640×420 → 384×252 | 134 / 200 (60/140) | 1 281 | 4 | 7.7% | 93% | PASS | 97.0 | 0.015 / 1.132 / 3.882 |
| 3 | 动物（坐姿狐狸/大尾/胡须/草） | 560×520 → 384×357 | 77 / 108 (22/86) | 946 | 4 | 4.1% | 60% | PASS | 88.9 | 0.014 / 0.800 / 2.400 |
| 4 | 机械（机甲半身/面罩/齿轮/铆钉） | 560×560 → 384×384 | 120 / 171 (52/119) | 1 383 | 3 | 6.1% | 66% | PASS | 94.4 | 0.015 / 1.015 / 3.048 |
| 5 | 静物（花瓶/6 花/苹果/梨/葡萄/布） | 520×620 → 322×384 | 157 / 252 (96/156) | 2 484 | 4 | 8.9% | 93% | PASS | 100.0 | 0.029 / 1.440 / 5.473 |
| 6 | 建筑（哥特教堂/玫瑰窗/三拱门/边框） | 560×560 → 384×384 | 208 / 289 (82/207) | 1 842 | 2 | 11.9% | 94% | PASS | 100.0 | 0.024 / 1.726 / 8.064 |

- **合计**：6 SVG、**1 163 元素、9 280 点**、**6/6 严格门禁通过**、**0 warning、0 路径丢弃**；质量扫描 0.019–0.053 s/图；编译 0.80–1.73 s；渲染合计 **26.65 s**（最长 8.06 s，教堂 289 元素/207 stroke）；端到端合计 **34.54 s**。
- 门禁指标全部在模板预算内：形状 77–208（≥30）、点数 946–2 484（≥400）、颜色 2–4、ink 4.1–11.9%、bbox 60–94%。
- 6 图均无渐变/text/image/filter/clip/mask/pattern/use；源文件 6.5–13.6 KB，注释均含 `agent-authored, not model output`。

### 4.2 视觉判定（`poc/agent_svg/contact_sheet.png`）

- **全部可辨**：1 长发半身像（发际线/双眼/鼻唇/耳环/衣领）；2 月夜湖景（月/云/山/松林/小屋/围栏/芦苇/倒影）；3 坐姿狐狸（耳/眼/吻/胡须/大尾/爪/草石/蘑菇）；4 机甲半身（面罩/天线/耳舱/齿轮/铆钉/胸核）；5 静物（花瓶/花束/苹果/梨/葡萄/桌面/布褶/挂框）；6 哥特教堂（三尖塔/玫瑰窗/三拱门/扶壁/台阶/边框角花）。
- 相对弱项：3 号狐狸（点数最少 946、bbox 60%、score 88.9，形体偏“圆润卡通”）；1 号人物为风格化线稿（非写实比例）。二者仍满足“结构可辨 + 预算达标”，未做美颜/修图。
- 本批**无失败图**；失败模式由门禁负例覆盖（<10 路径、ink<1%、bbox<30%、unsupported>2、空 SVG、XML 错误）。

### 4.3 生成方式

`poc/agent_svg/make_agent_svgs.py`：每张图由 agent 手写主体路径 + 小型程序化单元（`gear()` 齿轮多边形、`flower()` 花瓣、`pine()` 松树、`sparkle()` 星、`hatch()` 排线、`polar()` 极坐标），坐标保留 1 位小数；纯 Python 确定性输出（无随机数、无外部素材）。6 个 SVG 均为 4 组结构（underlay/mass/ink/detail），白底 + 黑墨 + 0–2 档浅灰。

## 5. 测试

`fourier-prompt/tests/svg/test_quality_gate.py`（16 例）：

| 类 | 用例 |
|---|---|
| `QualityGateCase` (11) | 正常 SVG 通过默认门禁；路径<10 拒绝（`SQ_E_MIN_PATHS`）；低 ink 拒绝；小 bbox 拒绝；gradient/text/image/filter/clipPath 拒绝；阈值覆盖可放宽/收紧；空 SVG（`SI_E_EMPTY`）；非法 XML（`SQ_E_XML`）；报告确定性；阈值校验；WP-U 两个失败样本回归（P1 3 path/99.2% ink、P2 bbox 2.47%） |
| `QualityCliCase` (2) | CLI `--json` 通过=0/失败=1；`--strict` 拒绝 + `--config` 放宽通过 |
| `ExampleSvgCase` (3) | 6 文件存在且注释/无禁用标签；6 图严格门禁（≥30 形状/≥400 点/0 unsupported/score≥85/均分≥90）；6 图 `svg_to_fdsl` + `compile_plan` + R=96 渲染且 PNG ink>2% |

全量：`python -m unittest discover -s tests/svg` → **Ran 101 tests, OK (skipped=1)**（原有 74 例未破坏；新增 16 例；其余为同批其他 WP 的用例）。

## 6. 限制与风险

1. **ink/bbox 是栅格近似**：忽略 fill 孔洞与元素间 alpha 合成（painter's algorithm），灰色 fill 若与白底差 >24 会计入 ink；因此门禁适合判“空/小/过实”，不等同于像素级真值。
2. **门禁不懂语义**：30 条无意义线条也可能通过；模板要求“PNG 目检结构可辨”，本批 6 图均人工/agent 目检，但批量生产仍需抽样目检。
3. **评分是启发式**：100 分只代表“预算/覆盖/干净度”满分，不代表美术质量；分数阈值仅在这 6 图 + 2 个 WP-U 样例上校准，样本量小。
4. **示例是 agent 程序化作品**：复杂度 77–208 形状，不是模型输出，也不承诺照片级/参考图级；人物/动物细节仍明显低于专业插画。
5. **渲染成本随 stroke 增长**：教堂 289 元素、207 stroke 需 8.06 s@R384；600 元素 stroke 密集图可能 >15 s。需要更快时用 `--no-stroke` / `stroke_to_fill` / 区域分块（导入器当前已支持相关参数，本批未改）。
6. **特性边界**：不支持渐变/text/image/filter/clip/mask；文字请走 FDSL `text`；R 上限本批 384，未测更高分辨率；无参考图故未计算 PSNR/SSIM。
7. **门禁依赖导入器 stats**：`paths/shapes/points/warnings` 取自 `svg_to_fdsl`；若导入器未来改计数口径（如 stroke→fill），阈值需复核（本批未改 `svg_import.py`）。
8. **时间/规模**：6 图总端到端 34.5 s（纯 CPU），适合单批演示；量产 50–100 图需并行进程 + 单图超时。

## 7. 复现

```powershell
$PY = "<python>"
cd "<workspace>"

# 生成 6 个 SVG（确定性）
& $PY poc\agent_svg\make_agent_svgs.py

# 门禁 + svg_import + compile + R=384 渲染 + summary + contact sheet
& $PY poc\agent_svg\run_batch.py

# 单图门禁 / 转换
cd fourier-prompt
& $PY -m fourier_prompt.svg_quality --svg ..\poc\agent_svg\svg\06_cathedral.svg --strict --json
& $PY -m fourier_prompt.svg_import --svg ..\poc\agent_svg\svg\06_cathedral.svg `
    --out ..\poc\agent_svg\plans\06_cathedral.json `
    --png ..\poc\agent_svg\png\06_cathedral.png --target-size 384 --resolution 384 --json

# 测试
& $PY -m unittest discover -s tests/svg
```

## 8. 合规声明

- 6 个 SVG 全部为**本 agent 编写/程序化生成**（非模型输出），文件内注释明确；未使用 `[X(Twitter)] ttus` 参考图、未使用任何第三方图片/字体/图标。
- **无本地 LLM、无本地生成模型、无训练/微调**；FFT 系数与栅格化由 `compile_plan`/`skill_api` 确定性完成；无网络调用。
- 仅写入任务允许的 5 个路径；`svg_import.py`、`skill/` 其他文件、核心、其他 docs/analysis 未改。
- 产物许可随本仓库；如需对外再许可，请按项目整体许可复核。
