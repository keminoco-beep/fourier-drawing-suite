# 需求追溯与验收状态

> **作用**：本文件是「需求 → 设计 → 代码 → 验收 → 实测」的**唯一追溯入口**。
> 任何"已实现/已验证"的声明都必须能在这里找到对应行；任何实测数字都必须登记在 §8。
>
> **来源文档**（按优先级）：
> 1. `CONTRACT.md`（接口契约）与 `references/fbs_format_spec.md`（数据格式）——**当前事实**
> 2. `docs/交接说明_傅里叶笔触_2026-10-18.md`（R1–R10 需求、旧版缺陷 D1–D5、硬约束）
> 3. `docs/傅里叶笔触复刻Skill_设计文档_v2.1.md`（设计决策 D1–D9、Stage 0–6、A1–A15 / T1–T13）
> 4. `docs/傅里叶笔触复刻Skill_设计文档_v1.0.md`（历史）、`docs/傅里叶笔触方案_评估报告_v2.0.md`（能力边界/定位）
>
> **状态口径**：
> `✅` 已实现且已有验收证据 ｜ `🟡` 已实现、待验收/待实测 ｜ `🟠` 部分实现 ｜
> `❌` 未实现/不承诺 ｜ `⛔` 用户已明确搁置 ｜ `📝` 待实测（占位，由验收报告回填）
>
> **注意**：交接说明里有两套同号 D：§2 的 **R 编号**、§4 的 **旧版缺陷 D1–D5**；
> 设计文档 §0.1 的 **设计决策 D1–D9**。本文用「缺陷 D」「决策 D」区分，引用时写全。

---

## 1. 需求总表 R1–R10

> 需求原文见交接说明 §2。**实现模块**指 v2 重建后的文件；验收项以 `scripts/run_acceptance.py` 为准。

| 编号 | 需求（要点） | 设计条款 | 实现模块 / 入口 | 验收项 | 状态 |
|---|---|---|---|---|---|
| **R1** | 用傅里叶系数表示图像并渲染，保真度尽量高 | 设计 v2.1 §1.2、§4.1（Stage2）、§5.2（Stage5）、§7.1 A1/A2/A9；format spec §2–§5 | `fbs_core/fourier.py`、`brush.py`、`document.py`、`render.py`、`scripts/fbs.py build` | A1、A2、A9 + 真实图 horn/chibi 的 PSNR/SSIM | ✅ A1 35.91 dB / A2 0.9947 / A9 35.58 dB（均 PASS）；真实图 horn 19.95 dB/0.916、chibi 19.04 dB/0.824（记录，无硬阈值） |
| **R2** | **单个傅里叶变换 + 一笔连续画完**（不是每条笔画各做一次） | 交接说明 §1；`CONTRACT.md` §7（onestroke）；format spec §6 | `fbs_core/onestroke.py`（`build_one_stroke`/`build_path`）、`scripts/fbs.py onestroke` | R2 专项：`stats.fft_passes==1`、恰好 1 条闭合 stroke、`path_deviation_px ≤0.05 @R1024`、travel/fill 段 width=0 | ✅ R2 PASS：fft_passes=1、1 条 stroke、RMS 偏差 0.0013 px（max 0.0611）；两图 onestroke 均 PASS |
| **R3** | 视频 ~1 分钟；**不省略任何作画步骤**；时间轴按 t / 弧长**等比**压缩 | `CONTRACT.md` §7（video）；format spec §6 尾 | `fbs_core/video.py`（`make_video`/`probe_video`）、`scripts/fbs.py video` | R3 专项：时长 60 s ±5%、帧数 ≥ 作画步骤数（least 1 frame/step）、`timeline="arc"`、`probe_video` 帧数/尺寸/时长一致 | ✅ R3 PASS：1800 帧 / 60.0 s / 720×1080；步骤 608（horn）与 940（chibi）均 ≥1 帧/步；arc 等比 |
| **R4** | 笔触属性必须完整：线宽、颜色、填充色、笔形 | 设计 v2.1 §4.2/§4.3；`CONTRACT.md` §2/§5；format spec §4 | `fbs_core/brush.py`、`attribute.py`、`document.py`、`onestroke.py` | R4 专项：每条 stroke 有 width/color/shape 三类曲线；region_fill 有 `poly`/填充记录；A13–A15 | ✅ R4 PASS；A13/A14/A15 全 PASS |
| **R5** | 画出的必须是**线**，不是一串点 | 设计 v2.1 §3.4；format spec §5 | `fbs_core/render.py`（`FloatCanvas.add_band`，`mode="sweep"`） | R5 专项：sweep 连续性（非点串）、亚像素精确带覆盖、抬笔不落墨 | ✅ R5 PASS（采样覆盖率 1.0、连通域 1）；R5b 渲染基元回归 PASS |
| **R6** | 连续扫掠中，宽度与颜色可**平滑渐变** | 设计 v2.1 §4.3；format spec §4 | `fbs_core/attribute.py`（求值）+ `brush.py`（拟合）+ `render.py`（逐点求值） | R6 专项：宽度沿 t 渐变（T4/T11）、颜色沿 t 渐变（T12）、无分块色阶（块间色差上限） | ✅ R6 PASS（宽度跳变比 0.0249 ≤0.10；相邻 ΔE 1.86 ≤12） |
| **R7** | 作画顺序像人：先铺底层，再压上层 | 交接说明 §2；`CONTRACT.md` §3（`layer`）/§7（渲染顺序） | `fbs_core/decompose.py`（layer 排序）、`render.py`（layer 升序） | R7 专项：layer 升序绘制、region_fill 在底层、同层按 footprint 降序（§8 D5 回归） | ✅ R7 PASS（fill 层 < medial 层；同层面积降序无违规） |
| **R8** | 精度：傅里叶阶数提到 **N≈65536**；笔触曲线的 t 精度要够；明确**适用条件与 Nyquist 边界** | 交接说明 §2；`CONTRACT.md` §4（Nyquist）、§7（harmonics/max_fft）；format spec §6 | `fbs_core/fourier.py`（`fit_curve` 钳制）、`onestroke.py`（`harmonics`/`max_fft`） | R8 专项：Nyquist 钳制（`N ≤ resample_points/2-2`，无回绕）、`W_N_CLIPPED`、路径偏差、"65536 只在独立采样数足够时有意义"写清 | ✅ R8 PASS：horn N=65534（Nyquist 钳制 + `W_N_CLIPPED`，RMS 0.0038 px）、chibi N=65536（RMS 0.019 px） |
| **R9** | 元素拆解 + 图层 + 每层一次 FFT，做成**可生成新图**的架构 | — | **不实现** | 无（**不得主动重启**） | ⛔ 用户已搁置 |
| **R10** | 输入图固定为两张测试图 | 交接说明 §5；设计 v2.1 §7.4 T1–T13 | `tests/gen_fixtures.py`（T1–T13）+ `assets/testcases/` + 两张真实图 | 真实图 horn/chibi 全链路（.fbs/PNG/SVG/一笔画/视频）+ 合成用例跑通 | ✅ 两图全链路 PASS（build/onestroke/video）；T1–T13 全 PASS |

**真实测试图（R10，2026-10-18 复核存在）**：

| 名称 | 路径 | 尺寸/类型 |
|---|---|---|
| horn 线稿 | `<path>)] ttus (@ttuttus_)\109：1807373590473445577_1.png` | 505×712 黑白线稿 |
| chibi 彩插 | `<path>` | 421×412，约 4 万色 |

> 验收报告必须包含这两张图的实测数字；路径不存在时**跳过并记录**（不得用其他图冒充）。
> 说明：交接说明 §3 里的老数字（PSNR 19.87/21.50 等）是**旧版历史值，不是 v2 指标**，
> 不得复制进 v2 报告（见 `engineering_guardrails.md` §10）。

---

## 2. 旧版缺陷回归表（缺陷 D1–D5，交接说明 §4）

> 这 5 条是旧版遗留缺陷。v2 的验收必须逐条给出「是否回归」的结论，不能只说"新版重写了"。

| 编号 | 旧版缺陷（已知事实） | v2 规避设计 | 回归验收项 | 状态 / 实测 |
|---|---|---|---|---|
| **缺陷 D1** | 一笔画扫掠覆盖只有逐笔画的 **0.50×**（同墨线总长、同宽度源，按构造应接近） | ① **先度量后改**：用「墨线总长 × 平均宽度」标量 + 同分辨率四维判"真丢宽度"还是"度量低估"；② 一笔画宽度与逐笔画**同源**（同一 width 曲线映射到合成路径）；③ travel/填充段 width=0 且跳过 | R2 路径偏差 + 一笔画 vs 逐笔画覆盖比 + PSNR/SSIM 差 | 🟡 通道级 PASS（R2：RMS 0.0013 px；R8：两图 RMS 0.0038/0.019 px）；**"覆盖比"标量未在终版报告单列**，不编造 |
| **缺陷 D2** | 彩色一笔画整体保真低（旧版 10.4 vs 21.5 dB，主因是覆盖不足） | 依赖缺陷 D1 修复 + 颜色曲线逐段映射 + `one_stroke_fills` 显式色侧表 | 一笔画 PSNR/SSIM + 色调四维 vs 逐笔画参考 | 🟡 通道级 PASS（chibi onestroke PASS，RMS 0.019 px）；**一笔画 vs 逐笔画 PSNR 未单列**，不编造 |
| **缺陷 D3** | 腮红 / 嘴画不出来（色相在采样阶段被抹掉；暖色 R−G 被中位数拉回背景） | 区域代表色取**腐蚀内部**统计色，并在**色度轴取高分位**（不用简单中位数）；调色板 AA 过渡色合并加**空间证据闸门**；另由**区域颜色场**（`regions[].color_field` 双线性采样）保留区域内部连续色，柔边渐变不再被涂成单色 | T3/T12 颜色用例 + 真实 chibi 的**色调混淆矩阵/色差**（不能只看结构指标） | 🟡 色调维度已量化：chibi 色调 ΔE **10.53**、wrong_ink 25.16%；**腮红专项检查未单列**（不声称已修复） |
| **缺陷 D4** | 一笔画只能有一个笔形（旧格式 `shape` 是每笔画一个值，不是曲线） | v2 `shape` 是**分类曲线**（`{"interp":"step","keys":[[t,"round"],...]}`），一笔画 `brush_styles` 逐段存；`step_spans` 记录作画步骤 | A15 / T11 的 shape 曲线断言 + 一笔画样张中段笔形变化 | ✅ 格式+渲染证据：A15 `shape_curve=true`、B3 PASS、R4 PASS；**一笔画逐段 shape 的视频专项未单列** |
| **缺陷 D5** | `layer` 按**亮度**排，不是"脸/头发/眼睛"语义 | v2 把 `layer` 定义为**绘制顺序**（fill 底层 → 细笔画上层；同层按 footprint 降序），**不承诺语义分组**；语义化属 R9（已搁置） | R7 专项：layer 升序、fill 在底层 | ✅ R7 PASS（fill 层 < medial 层；同层面积降序无违规） |

> 纪律：D3 这类"色相丢失"问题**结构指标看不见**（灰画成黑时 IoU 可以很漂亮），
> 必须同时看色调维度（见 `troubleshooting.md` §6）。

---

## 3. 设计决策落实表（决策 D1–D9，设计文档 v2.1 §0.1）

| 编号 | 决策（批复） | v2 落实情况 | 状态 |
|---|---|---|---|
| 决策 D1 | 主表示载体 **C** 自动路由（傅里叶为主，硬边降级贝塞尔） | 傅里叶主路径 ✅；**贝塞尔降级分支不在 v2 范围**（不是遗漏，Lead 裁决）：`repr` 只有 `fourier`，硬边由 **`region_fill` 显式 `poly`** 承担 | ❌ 部分范围（贝塞尔不实现） |
| 决策 D2 | 依赖 **C** 运行时探测，有则加速 | `fbs_core/util.py`：scipy 存在走加速（EDT/高斯/连通域），否则 numpy 回退；`.fbs.accelerators` 记录实际后端 | ✅ |
| 决策 D3 | 文件格式 **A** 首版仅 `.fbs.json` | 仅 `.fbs.json`（version 2.0）；不产二进制 `.fbs` | ✅ |
| 决策 D4 | 笔画分解主策略：**中轴变换（MAT）** | `decompose.py`：距离变换→脊线→剪枝→图切分→亚像素精修→宽度/颜色/alpha；块状区域另走 `region_fill` 轮廓（混合分解 hybrid） | ✅ |
| 决策 D5 | 照片类输入 **暂缓支持**（明确拒绝） | `preprocess.py` 多特征分类 → `E_PHOTO_DEFERRED` + 实测依据 + 替代建议；**不提供 `--allow-photo`** | ✅ |
| 决策 D6 | 首版范围 **C** 全量 Stage 0–6 | 流水线 `Stage0→6` 全链路（CLI build/render/eval/acceptance） | ✅ |
| 决策 D7 | 输出能力 **C** `.fbs` + 重建 PNG + SVG | `build` 产 `.fbs.json` + `_R{R}.png` + `.svg`（SVG 带 `W_SVG_APPROX`，互通≠保真） | ✅ |
| 决策 D8 | 安装位置 **A** 项目级 `.workbuddy/skills/` | 见 `README.md` 安装节 | ✅ |
| 决策 D9 | Skill 命名 **`fourier-brushstroke`** | 目录名、`SKILL.md` frontmatter、`.fbs.format` 一致 | ✅ |

---

## 4. 验收标准覆盖（A1–A15，设计文档 v2.1 §7）

> 阈值来自设计文档。若验收时发现判据前提不成立（旧版 A3/A8 都出现过），
> 必须**修订判据并留下测量证据**，不允许"硬凑阈值"（见 `engineering_guardrails.md` §3.6）。
> 验收报告的 **`判据修正记录`**（状态 `BROKEN`）专门保存被证伪的旧判据及其实测值，
> **不计入通过/失败**——目的是让判据修订可审计，而不是掩盖失败。

| 编号 | 级别 | 指标 | 阈值 | 验收落点 | 状态 / 实测 |
|---|---|---|---|---|---|
| A1 | MUST | 线稿类 PSNR @R=1024 | ≥ 30 dB | `run_acceptance`（T1 合成） | ✅ PASS **35.9082 dB** |
| A2 | MUST | 线稿类 SSIM @R=1024 | ≥ 0.90 | 同上 | ✅ PASS **0.994749** |
| A3 | MUST | N–R 契约（修正判据：契约+紧致+绑定） | 见下注 | `run_acceptance` A3 | ✅ PASS（contract/tightness/monotonic 全 true；`max_err·R`=0.4971/0.4930/0.4987；N 中位 31/50/69） |
| A4 | MUST | 全流程零外部网络依赖 | 断网跑通 | `run_acceptance` A4：**静态 AST 扫描** | ✅ PASS（hits=0；断网 smoke 为人工复核项） |
| A5 | MUST | 异常用例崩溃率 | 0 崩溃，全部给出明确原因 | `run_acceptance` A5：照片/透明/损坏/过小/格式/纯色异常用例 | ✅ PASS（0 崩溃） |
| A6 | MUST | 双渲染后端一致性（Python vs HTML Canvas） | 最大偏差 < 1 px | **NOT_IMPLEMENTED**：HTML Canvas 第二渲染后端 v2 不做（Lead 最终裁决；不在 R1–R10 需求内）。报告以 `NOT_IMPLEMENTED` 记录、**不计失败**；渲染基元一致性由 R5b 回归项覆盖 | ❌ NOT_IMPLEMENTED（不计失败） |
| A7 | MUST | 宽度反演精度（覆盖率/投影法向直取） | 均值相对误差 < 5% | `run_acceptance` A7：T4 | ✅ PASS（均值 **0.276%**、max 0.461%、max_abs 0.053 px） |
| A8 | MUST | stamp 渲染 vs 变宽线段渲染的形状误差 | **修正判据**：`stamp_band ≤0.5ε`（=0.25px@R512）**且** `stamp_band ≤ line_band` | `run_acceptance` A8 + A8-legacy；旧判据「stamp ≤50%×line」记录为 legacy/BROKEN | ✅ PASS（stamp 0.2069 px ≤0.25 且 ≤ line 0.3327 px；ratio 0.622 仅记录） |
| A9 | SHOULD | 扁平插画 PSNR @R=1024 | ≥ 26 dB | `run_acceptance` A9：T3 | ✅ PASS **35.5776 dB** |
| A10 | SHOULD | 中轴分支数 / 预期笔画数 | ≤ 1.5× | `run_acceptance` A10：T4 单笔画真值 | ❌ FAIL（2 段 vs 预期 1，ratio **2.0**；已知未解决，见 §6） |
| A11 | MAY | `.fbs` 体积 vs 同分辨率 PNG | 记录值（≤3× 护栏；MAY，体积目标已放弃） | `run_acceptance` A11（必须同时报 R 与绝对值） | ❌ FAIL（@R=512 **37.35×**：284,554 B / 7,619 B；MAY，主因 color_field+indent） |
| A12 | MAY | SVG 导出体积 vs `.fbs` | **记录即可**，不设阈值 | `run_acceptance` A12 | ✅ PASS（记录：SVG 65,246 B / `.fbs` 144,240 B = 0.452） |
| **A13** | MUST | 锥形笔画宽度曲线反演（T4），沿 t 取 32 点逐一比对 | 最大宽度误差 < 5% | T4/T11 | ✅ PASS（32 点口径 **0.219%**；全样本 max 0.461%） |
| **A14** | MUST | `linear`/`spline`/`preset` 三种 taper 表达的渲染一致性 | 两两差异 < 1% | T11 | ✅ PASS（worst **0.0009**） |
| **A15** | MUST | 属性曲线支持 6 种 `interp` 且 `step` 区间语义正确 | 全部通过，无退化（T13 方向归一化） | T11/T13 + `tests/test_brush.py` | ✅ PASS（属性层 8/8 true；T13 reversed 方向误差 5.62%，边界 t=0.52） |

**A8 与 A15 的判据修正**（均为 Lead 裁定，报告中有 `criteria` / `A8-legacy` 字段可审计）：
- **A8**：旧判据「变宽折线必须差 2 倍」（stamp ≤50%×line）的前提被证伪（旧版 0.824；v2 干净 doc ratio 0.622
  但 stamp 绝对误差 0.207px 已 ≤0.5ε），改为「stamp_band ≤0.5ε 且 ≤line_band」；旧判据实测保留为
  `A8-legacy`（BROKEN，不计失败）。
- **A15/T13**：中轴参数 t 的**方向天然任意**（弧长参数反转等价），判据改为**双向取优**并记录 orientation
  （本例 reversed：err 5.62%，反向 151.7%）；`interp=step` 只作偏好记录（step 语义已在属性层硬验），不因此判 FAIL。

**A3 判据修订说明（不许写回旧判据）**：设计文档 v2.1 原文是"每档分辨率翻倍则 N 翻倍 ±10%"（倍率落 1.8–2.2）。
旧版实测已**证伪**该经验律的前提（局部指数在 1.17–2.4 间切换，曾出现 1.28×/2.74×），
继续沿用会产出"假失败"。v2 验收改用旧版已验证的新判据，验收实现取 **R ∈ {256, 512, 1024}**：
1. **契约**：`max_err · R ≤ epsilon_px`；
2. **紧致**：`max_err · R ≥ 0.5 · epsilon_px`（证明 N 没为契约过度供给）；
3. **绑定**：N 中位随 R 严格递增。
三项合起来即"N 是为该 R 下亚像素契约所取的**最小**阶数"。
**不要写 A3 会通过"每档 2×"**——那个判据已被实测否掉。

**B 系列定性指标**（设计 v2.1 §7.3；B1–B3 在 `run_acceptance.py` 有自动化条目，B4/B5 人工复核）：

| 编号 | 级别 | 要求 | 验收落点 | 状态 |
|---|---|---|---|---|
| B1 | MUST | 一条命令完成线稿 → `.fbs` + PNG + report.json，无需手工调参 | `run_acceptance` `B1`：CLI 退出码 0 且产物齐备、report 含 `quality.psnr` | ✅ PASS（exit_code 0、files_exist、report_valid） |
| B2 | MUST | 报告能区分"忠实复刻"与"降级复刻" | `run_acceptance` `B2`：warnings/quality 字段可判定 | ✅ PASS（T8a 正常 build 且 `warnings=['W_SOLID_COLOR']`，真证据；M2 修复后） |
| B3 | MUST | 笔刷属性在渲染中肉眼可辨（变宽 / 变色 / 变透明） | `run_acceptance` `B3`：压平属性曲线做消融对比 | ✅ PASS（变宽/变色消融可见；alpha 维度按无变化记 n/a） |
| B4 | SHOULD | `.fbs` JSON 可人工阅读与手工微调 | format spec §2–§4；人工复核 | ✅（格式设计目标） |
| B5 | SHOULD | SVG 导出可在 Illustrator / Figma 打开 | `export_svg` + 人工打开验证 | 📝 人工 |

---

## 5. 测试集覆盖（T1–T13，设计 v2.1 §7.4）

> 合成用例由 `tests/gen_fixtures.py` 确定性生成（manifest seed=20261018，共 21 个文件），
> 每个用例在 `run_acceptance.py` 中都有独立条目 T1–T13（级别 MUST/SHOULD 见代码）。

| 用例 | 内容 | 验收项（`run_acceptance`） | 生成/来源 | 状态 |
|---|---|---|---|---|
| T1 | 签名线稿（单笔画，平滑） | MUST：build 成功且分类=`lineart`；A1/A2/A6/R5 也用它 | `tests/gen_fixtures.py` | ✅ PASS（39.1055 dB/0.9986 @R512；klass=lineart） |
| T2 | 图标（直角 + 曲线） | SHOULD：build 成功、`fill_count>0` | 同上 | ✅ PASS（fill_count 4） |
| T3 | 扁平插画（8 色） | SHOULD：build 成功、多区域 + 填充；A9/A11 | 同上 | ✅ PASS（32.4464 dB/0.9742 @R512；fill 8） |
| T4 | 合成图：已知 `w(t)` 的锥形笔画 | MUST：build 成功且有 `medial_branch`；A3/A7/A10/R6 | 同上 | ✅ PASS（34.1561 dB；A10 的 FAIL 见 §8.4） |
| T5 | 合成图：带已知尖角的形状 | MUST：build 成功、`fill_count>0`；A6/A8 | 同上 | ✅ PASS（22.9004 dB @R512，仅记录；A8 用 T5b） |
| T6 | 渐变图 | SHOULD：无硬边 → 必须明确降级/告警，不崩溃 | 同上 | ✅ PASS（W_SHORT_BRANCHES/W_PRUNED/W_GAP_BRIDGED） |
| T7 | 伪照片（合成高熵图） | MUST：明确拒绝 `E_PHOTO_DEFERRED`，错误信息含证据与建议 | 同上 | ✅ PASS（evidence+suggestions 非空） |
| T8 | 纯黑 / 纯白 / 全透明 | MUST：纯色告警 `W_SOLID_COLOR`、全透明拒绝 | 同上 | ✅ PASS |
| T9 | 16bit / CMYK / alpha / 调色板 | SHOULD：均可自动转换（T9c 15.57 dB 仅记录） | 同上 | ✅ PASS（4 种模式） |
| T10 | 中文名大图 / 损坏文件 | MUST：中文名成功 + 损坏文件明确拒绝 | 同上 | ✅ PASS |
| T11 | 合成图：已知 taper（linear/spline/preset 三种表达） | MUST：build 成功且有 `medial_branch`；A14 | 同上 | ✅ PASS（36.6903 dB） |
| T12 | 合成图：颜色沿笔画渐变（已知 stops） | SHOULD：恢复 color 曲线与真值 stops 平均 ΔE ≤ 25；R6 | 同上 | ✅ PASS（平均 ΔE 16.993） |
| T13 | 合成图：分段区间笔触（step） | MUST：宽度曲线保留 t=0.5 处 20→6px 的区间语义（方向归一化）；A15 | 同上 | ✅ PASS（reversed 方向误差 5.62%；w=20.02/5.979 px） |

---

## 6. 未实现 / 不承诺 / 搁置清单（必须与 `SKILL.md`、`capability_boundary.md` 一致）

| 项 | 分类 | 依据 | 说明 |
|---|---|---|---|
| **R9**：元素拆解 + 图层 + 每层一次 FFT 的"可生成新图"架构 | ⛔ 搁置 | 交接说明 §2（用户明确搁置） | 算力/数据量过大；**不要主动重启** |
| 决策 D1 的**贝塞尔硬边降级**分支 | ❌ 不在 v2 范围（不是遗漏） | Lead 裁决（2026-10-18）；`CONTRACT.md` 与 format spec 均无 `repr="bezier"`；警告码只有 `W_N_CLIPPED`，**不存在** `W_BEZIER_FALLBACK` | v2 `repr` 只有 `fourier`；硬边由 `region_fill` 显式 `poly` 覆盖（旧版实测 hybrid 优于纯 MAT 的方向）。如需要贝塞尔属后续扩展 |
| **HTML Canvas 第二渲染后端**（A6） | ❌ NOT_IMPLEMENTED（已知、裁决不做） | Lead 最终裁决：v2 不做 HTML Canvas 后端；不在 R1–R10 需求内；`CONTRACT.md` 只有 Python/Pillow 渲染 | A6 在验收报告中记为 NOT_IMPLEMENTED（不计失败）；渲染基元一致性由 R5b 回归项覆盖 |
| **A10 一笔画/分解的过度切分**（T4 单笔画→2 段 medial，ratio 2.0） | ❌ 已知失败（SHOULD，未解决） | 终版验收 A10 FAIL；根因：端帽臂约 9.2 px 未被正确连接 | 修复需**拓扑级**处理（不是调参）；不影响 MUST，按已知问题登记，不许"调阈值掩盖" |
| **A11 体积护栏超标**（T3 `.fbs`/PNG=37.35× @R=512） | ❌ 已知不达标（MAY，已放弃目标） | 终版验收 A11 FAIL；`.fbs` 284,554 B / PNG 7,619 B；主因 `color_field` + JSON indent | 设计阶段已放弃"比 PNG 小"目标（硬边下傅里叶天然更大）；MAY 失败不阻断发布，但必须如实登记 |
| **照片类输入** | ❌ 不承诺（决策 D5） | 设计 v2.1 §2.5；Lead 裁决 | 明确拒绝，**不可绕过**（`--force-class` 只覆盖非 photo 类别） |
| **v1 `.fbs` 兼容读取** | ❌ 不承诺 | format spec §8；`io_format.read_fbs` 抛 `E_FBS_INVALID`；CONTRACT §11-5 已同步改为"只读 v2" | v2 只读 v2，旧产物提示"用新流水线重新生成"；不做 v1 迁移兼容 |
| **`--allow-photo` 之类的越权开关** | ❌ 不提供 | 设计 v2.1 §2.5 | 不留"稳定产出垃圾"的入口 |
| **语义图层**（脸/头发/眼睛） | ❌ 不承诺 | 交接说明 §4 缺陷 D5 + R9 搁置 | `layer` 只是绘制顺序 |
| **体积优势**（比 PNG/JPEG 小） | ❌ 放弃 | 设计 v2.1 §1.1 C3、评估报告 §6 | 硬边下傅里叶天然更大；A11 阈值刻意宽松 |
| **任意分辨率清晰** | ❌ 不承诺 | 设计 v2.1 §5.2；format spec §1 | 同一 `.fbs` 在 2R 渲染有振铃，是 N 绑定 R 的语义 |
| **细密黑白线稿的高保真** | ❌ 不承诺（表示法边界） | 旧版实测定位（复杂黑白线稿 ~15 dB） | 圆盘并集需数千条精确细笔画，误差随笔画数累积；输出带 warning，别承诺"能画好" |
| **照片级写实 / 生成 / 超分 / 修复 / 风格迁移 / GUI** | ❌ 非目标 | 设计 v2.1 §1.3 | 属不同问题域 |
| **SVG 保真** | ❌ 不承诺 | 设计 v2.1 §5.2 | SVG 是互通格式，变宽笔画需离散化，精度低于 `.fbs` |

---

## 7. 验收命令与产物追溯

```bash
PY="<python>"
cd fourier-brushstroke

# 方式 A（推荐，统一入口）：
"$PY" scripts/fbs.py acceptance --quick
# 方式 B（直跑验收脚本）：
"$PY" scripts/run_acceptance.py --quick
# 只跑指定项：
"$PY" scripts/run_acceptance.py --items A1,A7,R2,T4
# 静态守卫（独立脚本）：
"$PY" scripts/check_hygiene.py --strict
```

**验收项清单（`run_acceptance.py` 实际实现的条目）**：

| 组 | 条目 |
|---|---|
| 设计指标 | `A1`–`A15` |
| 定性指标 | `B1`（一条命令端到端）、`B2`（报告区分忠实/降级）、`B3`（笔刷属性肉眼可辨） |
| 需求专项 | `R2`（单次 FFT/单笔画/路径偏差）、`R3`（60 s 视频/不省略步骤/等比时间轴）、`R4`（属性齐全+填充）、`R5`（线不是点串）、`R6`（宽度/颜色连续）、`R7`（分层顺序）、`R8`（高谐波+路径偏差） |
| 回归 sanity | `R5b`（stamp 圆盘并集 vs sweep 生产扫掠基元的边界/PSNR 一致性；**非设计文档项**，不冒充 A6） |
| 合成用例 | `T1`–`T13`（manifest seed=20261018） |
| 判据修正记录 | `A8-legacy`（被证伪的旧判据实测值；状态 `BROKEN`，不计通过/失败） |
| 真实图 | `real_images` 段（horn / chibi，非 quick 模式）：质量指标 + 一笔画路径偏差 + 视频 probe |
| 预检 | `preflight`：document/render/onestroke/video/fourier/cli 可导入性；失败项标 `BLOCKED` 不算通过 |

**产物**：`assets/out/acceptance/report.json`（机器可读）、`report.md`（人读）；
静态守卫：`hygiene.txt` + `hygiene_summary.json`（同目录）。

> **官方报告口径**：只认 `assets/out/acceptance/report.json` / `report.md`（代码冻结后由 metrics 重跑全量覆盖；
> `report.md` 顶部带代码快照与 run 时间）。`assets/out/verify_acceptance/` 是 verifier 的**独立复验**，
> 不作为官方数据，但可作为「独立审计」交叉证据引用（路径：`assets/out/verify_acceptance/report.json` / `report.md`）。
> 定向运行（如只跑 2 项）覆盖过的 report 不算验收结论，**不得引用其数字**。

`report.json` 关键字段：`schema` / `generated` / `quick` / `env` / `fixtures` / `preflight` /
`summary{passed,failed,blocked,skipped,total,must_*}` / `first_failure` / `items[]` /
`real_images` / `timings_s` / `total_elapsed_s` / `repro`。
每个 `items[]` 条目：`id`、`level`、`desc`、`status`（PASS/FAIL/BLOCKED/SKIP/BROKEN）、`value`、
`threshold`、`note`、`cmd`。**报告中的每个数字都能回溯到：用例 + 命令 + 产物路径。**
`BLOCKED` = 依赖模块未就绪（不是通过）；`BROKEN` = 判据修正记录（不是失败）；
`NOT_IMPLEMENTED` = 设计项明确不做（不是失败）；`SKIP` = 设计上不跑（不是通过）。

---

## 8. 实测数据登记（终版：2026-10-18T08:34:55 全量验收）

> **数据来源（唯一口径）**：`assets/out/acceptance/report.json` / `report.md`；
> 终版运行参数：`quick=False`、`skip_video=False`、代码快照 digest=**`8695c5845828b997`**、
> 环境 Python 3.13.14 / numpy 2.5.4 / Pillow 12.3.0 / metrics 2.0、夹具 seed=20261018 count=22、
> 总耗时 **61.8 s**。
>
> **终版结论**：**36 PASS / 2 FAIL / 1 BROKEN（判据修正记录）/ 1 NOT_IMPLEMENTED**，共 40 项；
> **MUST 27/27 全通过**。两处 FAIL：`A10`（SHOULD）、`A11`（MAY）；
> `A6`=NOT_IMPLEMENTED、`A8-legacy`=BROKEN（两者均不计失败）。
> 分组：A 组 12P/2F/1BROKEN/1NI；B 组 3P；R 组 8P；T 组 13P。
> `report.md` 的结论已区分 MUST：**"✅ 项目级通过（MUST 27/27 全过；非 MUST 失败 2 项）"**；
> 机读 `ok=false` 只是因为 SHOULD/MAY 仍有 FAIL。**MUST 已全过，但不粉饰 A10/A11**。
>
> 纪律：每个值都必须能从报告回溯；历史/中间轮数字不得引用；
> verifier 的 `assets/out/verify_acceptance/` 只作独立审计交叉证据（见 §8.5）。

### 8.1 质量指标

| 指标 | 用例 | 阈值 | 实测值 | 状态 |
|---|---|---|---|---|
| A1 PSNR @R=1024（线稿） | T1 合成签名（klass=lineart） | ≥30 dB | **35.9082 dB** | ✅ PASS |
| A2 SSIM @R=1024（线稿） | T1 | ≥0.90 | **0.994749** | ✅ PASS |
| A9 PSNR @R=1024（扁平插画） | T3 | ≥26 dB | **35.5776 dB** | ✅ PASS |
| 真实图 horn 线稿（记录） | horn 505×712 @R1024 | 无硬阈值 | PSNR **19.9504** / SSIM **0.915627** / RMSE 25.6461 / MAE 5.4666；IoU 0.8533（漏画 8.42% / 多画 7.33%）；ΔE 13.71 | ✅ 记录 |
| 真实图 chibi 彩插（记录） | chibi 421×412 @R1024 | 无硬阈值 | PSNR **19.0382** / SSIM **0.823518** / RMSE 28.486 / MAE 9.6879；IoU 0.8851（漏画 9.03% / 多画 2.77%）；ΔE 10.53 | ✅ 记录 |
| A3 N–R 契约（修正判据） | T4：R=256/512/1024 | 契约+紧致+单调 | contract/tightness/monotonic 均 true；`max_err·R` = **0.4971 / 0.4930 / 0.4987**（ε=0.5）；N 中位 **31 / 50 / 69**（严格增）；诊断倍率 2.226（仅记录） | ✅ PASS |
| A7 宽度反演误差（均值） | T4 锥形笔画 | <5% | 均值 **0.276%**（最大 0.461%，max_abs 0.053 px；32 点口径 0.219%） | ✅ PASS |
| A13 锥形宽度最大误差（32 点） | T4 | <5% | **0.219%**（全样本最大 0.461%，31/32 点有对应） | ✅ PASS |
| A14 taper 三表达渲染一致性 | T11 | 两两差异 <1% | worst **0.0009**（≈0.09%）；linear vs preset 0.0 | ✅ PASS |
| A15 六种 interp + step 语义 | T11/T13 | 全部通过（T13 方向归一化） | 属性层 8/8 项 true；T13：orientation=**reversed**，`err_rev=5.62%`（`err_fwd=151.7%`，方向天然任意故双向取优）、w(0.25)=**20.02 px** vs 真值 20、w(0.75)=**5.979 px** vs 6、边界 t=0.52；`interp=step` 仅偏好记录（属性层已硬验 step 语义） | ✅ PASS |
| A8 stamp vs line 形状误差（修正判据） | T5b 干净 spec doc + 解析圆盘并集真值 | stamp band ≤0.5ε（=0.25px）且 ≤ line band | stamp band **0.2069 px**（≤0.25 ✓）、line band **0.3327 px**（0.207≤0.333 ✓）；ratio 0.6219 仅记录；IoU 误差 0.01418 vs 0.0223 | ✅ PASS（判据修正后） |
| A11 `.fbs`/PNG 体积比（含 R 与绝对值） | T3 @R=512 | ≤3×（MAY） | **37.3479×**（`.fbs` **284,554 B** / PNG **7,619 B**） | ❌ FAIL（MAY；体积目标早已放弃，仅作护栏记录；主因 color_field + JSON indent） |

**A8 判据修正记录（可审计）**：旧判据「stamp ≤ 0.5 × line」依赖"变宽折线明显更差"的前提，
旧版实测 0.824 已证伪；v2 用干净 spec doc + 解析圆盘并集真值重测：stamp band **0.207 px**、
line band **0.333 px**、ratio 0.622（>0.5 但 stamp 绝对误差已 ≤0.5ε=0.25px 且 ≤line）。
**现行判据 = stamp_band ≤0.5ε 且 stamp_band ≤ line_band → PASS**；
旧判据实测值保留为 **`A8-legacy`（BROKEN，判据修正记录，不计通过/失败）**。
上游 pipeline doc 上 stamp 形状误差仍偏大（诊断：shape_err 0.310 vs line 0.043，p99 11.4 px vs 3.0 px），
说明瓶颈仍在上游分解/宽度——**不删项、不调阈值、如实记录**。

### 8.2 通道与视频（R2/R3）

| 指标 | 用例 | 阈值 | 实测值 | 状态 |
|---|---|---|---|---|
| R2 一笔画 `stats.fft_passes` | T1 合成（R=512 代理） | ==1 | **1** | ✅ PASS |
| R2 一笔画 stroke 数 | 同上 | ==1（闭合） | **1** | ✅ PASS |
| R2 `path_deviation_px`（RMS） | 同上 | ≤0.05 | **0.001287** | ✅ PASS |
| R2 `path_deviation_max_px` | 同上 | ≤0.5 | **0.06114** | ✅ PASS |
| R8 horn 一笔画（真实图，Nyquist 边界） | horn @R=1024 | N≈65536（±2 或 `W_N_CLIPPED`）+ 偏差 | N=**65534**（Nyquist 钳制，`W_N_CLIPPED`）；RMS **0.0038 px**、max 0.0735、p99.5 0.0297；路径采样 131,072；fft_passes 1；travel 6.09 | ✅ PASS |
| R8 chibi 一笔画（真实图） | chibi @R=1024 | 同上 | N=**65536**（未钳制）；RMS **0.019 px**、max 0.1449、p99.5 0.0893；路径采样 524,288；fft_passes 1；travel 13.61 | ✅ PASS |
| R3 视频时长 | horn/chibi 60 s 演示视频 | 60 s ±5% | **60.0 s**（两图） | ✅ PASS |
| R3 视频帧数 / 步骤覆盖 | 同上 | least 1 frame/step | **1800 帧**；作画步骤 horn 608 / chibi 940，`frames_ge_steps=true`、`steps_covered=true` | ✅ PASS |
| R3 时间轴等比 | 同上 | t/弧长等比 | `timeline="arc"`；`max_abs_dev` horn 1e-5 / chibi 0.0 | ✅ PASS |
| R3 视频尺寸/编码 | 同上 | probe 与请求一致 | 1800 帧 / 30 fps / 720×1080；文件 14,886,343 B（horn）、20,503,370 B（chibi） | ✅ PASS |
| 缺陷 D1/D2（一笔画覆盖/保真） | 两图一笔画 | 记录，不得显著劣化 | 通道级 PASS（R2/R8）；**覆盖比与"一笔画 vs 逐笔画 PSNR"未在终版报告中单列**——不编造数字 | 🟡 未单列 |

### 8.3 性能（对照 `CONTRACT.md` §12 预算）

> 数据来源：`assets/out/demo/{horn,chibi}/*.report.json` 的 `timings_s`（演示产物的真实构建计时）
> 与终版 `report.json` 的 `timings_s` / `total_elapsed_s`。

| 阶段 | 预算 | horn 505×712 @R1024 | chibi 421×412 @R1024 | 状态 |
|---|---|---|---|---|
| Stage 0+1（预处理 + 分解） | ≤120 s | **8.98 s**（stage0 0.434 + stage1 8.544） | **7.87 s**（stage0 1.786 + stage1 6.082） | ✅ |
| Stage 2+3（傅里叶 + 笔刷） | ≤60 s | **3.10 s** | **4.72 s** | ✅ |
| Stage 5 静态渲染（SS=2） | horn ≤120 s；chibi ≤180 s | **11.13 s** | **13.33 s** | ✅ |
| Stage 0–4 全构建（含文档） | —（参考） | **12.08 s** | **12.59 s** | ✅ 记录 |
| 一笔画构建（N=65534/65536） | horn ≤120 s；chibi ≤180 s | 产物已生成；**耗时未写入报告（未记录，不编造）** | 同左 | 📝 未记录 |
| 60 s 视频编码 | ≤300 s | 1800 帧/60 s/720×1080 已生成；**编码耗时未写入报告** | 同左（20.5 MB） | 📝 未记录 |
| 全量验收总耗时 | ≤600 s | **69.76 s**（终版全量；真实图复用已生成演示产物，合成用例全新构建） | | ✅ |

### 8.4 验收通过统计（终版）

| 项 | 目标 | 实测 | 状态 |
|---|---|---|---|
| A1–A15（16 项：含 A6 NI、A8-legacy BROKEN） | MUST 全过 | **A 组 12 PASS / 2 FAIL（A10,A11）/ 1 BROKEN / 1 NOT_IMPLEMENTED** | ✅ MUST 全过（27/27）；🟡 2 项 SHOULD/MAY 失败如实记录 |
| B1–B3 定性项 | 全过 | **B 组 3/3 PASS** | ✅ |
| R2–R8 + R5b | 全过 | **R 组 8/8 PASS** | ✅ |
| T1–T13 合成用例 | 全部跑通 | **T 组 13/13 PASS**（T5 22.90 dB@R512、T9c 15.57 dB 仅记录；质量门以 build/属性完整性为准） | ✅ |
| 异常用例（T7–T10 + A5） | 0 崩溃、原因明确 | A5 PASS（照片/透明/损坏/过小/格式/纯色 0 崩溃）；T7–T10 PASS | ✅ |
| 静态守卫 `check_hygiene.py --strict` | 无新增命中 | **hits=0**（扫 27 个文件；`assets/out/verify_final/hygiene.txt`） | ✅ |
| **总计** | — | **36 PASS / 2 FAIL / 1 BROKEN / 1 NI / 0 BLOCKED / 0 SKIP（40 项）；MUST 27/27** | ✅ **项目级通过**（按 MUST 口径）；2 项非 MUST 失败如实登记 |

**终版 FAIL 明细与处置**：
- `A10`（SHOULD，已知未解决）：T4 单笔画真值被切成 **2 段** medial 分支（ratio 2.0 >1.5）；
  根因：端帽臂约 9.2 px 未被正确连接；修复需**拓扑级**处理（不是调参）；不影响 MUST，已记录为已知问题；
- `A11`（MAY，已放弃目标）：T3 @R=512 为 **37.35×** PNG（`.fbs` 284,554 B / PNG 7,619 B）；
  体积目标在设计阶段就已放弃（硬边下傅里叶天然更大），MAY 失败不阻断发布，但必须如实登记；
  主因：`color_field`（区域颜色场）+ JSON indent。

### 8.5 独立审计交叉证据（不作为官方数据）

verifier 用 fresh 上下文独立复验，结论报告：**`references/verification_report.md`**（284 行，
含 R1–R10 / A1–A15 / B1–B5 逐项复算、CLI 冒烟、复制安装可移植性、UTF-8 无 BOM、测试/卫生复核）；
复跑产物在 `assets/out/verify_acceptance/`、`assets/out/verify_final/`、`assets/out/verify_final2/` 等。
verifier 独立 quick 复跑（04:33:16，digest 相同）与官方报告在全部重叠项上数字一致，
并独立复验了 quick 模式 SKIP 的 R3/R8。用途与纪律：

- 只作**交叉审计**：官方口径是 `assets/out/acceptance/report.*`（终版全量，代码快照 digest=`8695c5845828b997`）；
- verifier 报告里发现过的差异（A8/T13 判据口径、onestroke 默认命名断链、render `--mode` 缺省、
  `eval --render` 跨分辨率示例、分类阈值文档不同步等）已被用于修正代码/验收/文档，记录在此防回归；
- **不得把 verifier 数字写进 §8.1–§8.3 冒充官方实测**；引用时标注"独立审计（verifier）"。

### 8.6 已知问题跟踪（独立审计提出，交付时必须明示）

> 来源：`references/verification_report.md` §6/§7（fresh verifier，2026-10-18 04:38 冻结态）。
> 这些**不改变**官方验收 36P/2F/MUST 27/27 的事实，但属于交付说明里必须让用户知道的事项。

| 编号 | 问题 | 归属 | 文档/交付处理 |
|---|---|---|---|
| M1 | `report.md` 结论用"❌ 失败"（`ok = failed==0`，把 SHOULD/MAY 也算失败），与 SKILL "通过标准 = MUST 全过"的判读口径不一致 | 验收台（`run_acceptance.py`，T5 scope） | ✅ **已修复**：终版 `report.md` 结论区分 MUST，显示"✅ 项目级通过（MUST 27/27 全过；非 MUST 失败 2 项）"；SKILL/README/本表 §8 的判读口径保留 |
| M2 | 验收台 `Ctx._build_path` 覆盖 `doc.warnings`，导致纯色图在验收路径下写盘 `E_FBS_INVALID`；B2 把该 bug 当成"明确拒绝"证据 | 验收台（T5 scope），**非产品 bug**：生产 CLI 同配置 exit 0 正常 | ✅ **已修复**：`_build_path` 改为**合并** warnings（`W_SOLID_COLOR` 不再丢失，T8a/T8b 正常 build+写盘）；B2 改用真证据（T8a `codes=['W_SOLID_COLOR']`） |
| m1 | 打包清理未做：ACL 锁死临时目录、`__pycache__`、`assets/out` 调试脚本；zip 未生成 | 打包（Lead） | 交付前由 Lead 清理并生成 `fourier-brushstroke-v2.zip`（README 已写该名） |
| m2 | `capability_boundary.md §3` 分类阈值与 `preprocess.py` 不一致（lineart hard_edge 0.02 vs 0.008；flat_art 0.05 vs 0.08） | 文档（已修） | ✅ 已按冻结代码同步为 **0.008 / 0.08** |
| m2 | `run_acceptance.py` docstring 写"38 项"实为 **40 项** | 脚本（T5 scope） | ✅ **已修复**：docstring 改为 40 项 |
| M3 | A8/T13 属"判据修正后通过"，非按设计原文达标 | 设计/验收裁定（Lead） | 本表 §4/§8 与 SKILL 已明确写"判据修正/legacy BROKEN"，不表述为"完全达标" |
| M4 | A10（SHOULD）/ A11（MAY）失败 | 产品已知限制 | 本表 §6/§8.4 已如实登记，不粉饰 |

---

## 9. 维护纪律

1. 需求/设计变更 → 先改本表，再改代码；不允许"代码改了文档没改"。
2. 新增验收项 → 必须同时写进 `scripts/run_acceptance.py` 与本表 §4。
3. 判据修订（如 A3/A8）→ 在 §4 备注里写**原判据、证伪证据、新判据**，不许静默放宽。
4. 实测值回填 → 只填 `run_acceptance.py` 产出的数字，附产物路径。
5. 本表与 `SKILL.md` / `README.md` / `references/*` 出现矛盾时，以**代码与契约**为准，
   并修正文档；矛盾本身视为一个待修缺陷。

## 8.7 2026-10-18 保真度修复（第二轮：误差归因 → 修正 → 复测）

用户诉求：“尽可能完全还原原图，差距在哪里”。先用 scripts/analyze_gap.py 对 5 张例图做逐阶段误差归因
（
eferences/gap_analysis.md），再按 dB 头寸修 6 项缺陷，最后用同一口径（R=1024、同 ε、同代码代）复测。

| # | 缺陷（证据） | 修法 | 实测 |
|---|---|---|---|
| F1 | metrics.check_contract() 把 resolution=[W,H] 当 (H,W) 算宽高比 → 所有非方图 contract 误报“不可比” | 参数顺序修正（1 行） | 5 图 contract_errors 0 |
| F2 | 线稿填充路由：细长实心结构（logo 字母/大色块）sprawl>3 被推给中轴 → 硬边丢失 | 增加“自身厚度证据”（薄像素占比≤0.55、厚度中位数≥2px）；纯二值/硬量化源（AA 像素<5%）不启用；内部渐变区（颜色 std>12）不启用 | horn 填充 1→3；logo/horn 回归受控 |
| F3 | 宽度反演系统性偏粗（覆盖率积分在邻接结构处外溢） | width_mode=auto（复杂图 DT / 单笔画 hybrid）；渲染侧按类别补偿 0.95（线稿），不写进 doc | 线稿 +0.19~+1.22dB（T9 独立 A/B） |
| F4 | 填充软边默认 2.0px 对平涂/硬边过大 | ill_soft_px 默认 0=auto（线稿 0.5 / 插画 2.0），写入 .fbs 参数 | 线稿 +0.25~+1.17dB（T9） |
| F5 | 区域颜色场网格 20 对柔光插画分辨率不足 | 默认 20→32 | pale +0.2dB / chibi 同向 |
| F6 | 颜色场量纲按单元自身 max 猜 → 近黑单元被放大成纯绿/纯青噪点；环状区域轮廓填充把洞涂实 | 整场统一量纲；W_HOLE_REGION 回退中轴 | 彩虹噪点 1479→600px；pale +0.5dB（相对修复前同参） |

**复测（新默认 vs 旧默认，R=1024、同 ε；PNG/.fbs 均同代码代）**

| 例图 | 类别 | 修复前 PSNR / SSIM | 修复后 PSNR / SSIM | ΔPSNR |
|---|---|---|---|---|
| elf 线稿 | lineart | 22.90 / 0.930 | 23.24 / 0.919 | +0.34 |
| DeepSeek logo | lineart（硬量化源） | 22.88 / 0.935 | 23.47 / 0.947 | +0.59 |
| pale 柔光插画 | illustration | 23.03 / 0.823 | 23.50 / 0.857 | +0.47 |
| chibi 彩插 | illustration | 19.04 / 0.824 | 19.76 / 0.840 | +0.72 |
| horn 线稿 | lineart | 19.95 / 0.916 | 20.91 / 0.912 | +0.96 |

**剩余差距（归因结论，详见 
eferences/gap_analysis.md）**
1. 边界环是线稿头号误差：elf 96.0%、horn 80.4%、logo 75.3% 的误差能量落在参考墨迹边界 0.5–2px 环（面积仅 17.9%）。
   下一层修法是把线稿按中轴按“中心线+可变宽度”而非区域轮廓表达（本轮只回收了宽度偏差部分）。
2. 柔光插画误差主要在填充层内部：pale 78.3% 误差能量落在 fill-only 覆盖区；高频/柔边细节被“区域代表色+颜色场”平滑。
3. 分辨率不是瓶颈：R=2048 重建实测 PSNR 不升（chibi 18.70 / horn 19.38 / pale 23.04），SSIM 略升（+0.02~0.03）。
4. 一笔画通道相对逐笔画有 0.9–6.5dB 代价（logo 最大），属“单次 FFT + 填充侧表”的通道固有近似，不用于静态保真交付。
5. 表示上界（调色板+区域）与管线损失：pale 的硬调色板上界反而低于当前渲染（22.52 < 23.03），说明颜色场/代表色已越过硬调色板；
   其余图管线上界与实际差 2.5–8dB（elf 31.10 / horn 24.66 / logo 54.74 为调色板量化理想值，不可达）。

## 8.8 2026-10-18 一笔画/视频保真修复（用户反馈“视频没画出 PNG 预览的样子”）

**诊断结论：不是没画完，是精度不够**。视频走一笔画通道；一笔画填充侧表 one_stroke_fills 只存
[t0,t1,rgba,poly]，**丢了区域的 color_field**，260 个填充全部被涂成单一平色 → 柔光插画被整片“拍平”，
与逐笔画 PNG 差距达 4.36dB（one-stroke 18.87dB vs per-stroke 23.23dB）。

**修法**：one_stroke_fills 允许可选第 5 元素 = 区域 color_field；
ender._render_one_stroke 与
ideo.make_video 的填充绘制把它按世界→像素换算 bbox 后传给 ill_polygon 双线性采样。

**实测（pale_girl，R=1024 同参考）**
| 指标 | 修复前 | 修复后 |
|---|---|---|
| 一笔画静态 PSNR | 18.87 | **22.50**（逐笔画 23.23；通道代价 4.36→0.73dB） |
| 一笔画静态 SSIM | 0.800 | **0.819** |
| 视频末帧 vs 逐笔画 PNG（同尺寸） | 14.5dB / MAE 20.8 | **22.9dB / MAE 6.4** |
| 视频末帧 vs 一笔画静态 | — | **24.0dB / MAE 5.6** |

剩余差异主要来自视频作画区分辨率（720×1080 帧内 art 473×789，≈0.77×线分辨率）；
需要逐像素对齐静态 PNG 时用更高 --size 1080x1620（art 710×1183，已产出样例）。
其他例图（elf/logo/chibi/horn）的视频同步受益，但需要重新执行 onestroke+video 才带上新侧表。

## 8.9 2026-10-18 区域填充接缝黑线修复（用户报告）

**症状**：成品图每个区域填色的边缘出现一圈偏暗黑线/虚线，层层叠叠，pale_girl 胸口最明显。
**归因**：fills-only 渲染即可复现；关掉 
egions[].color_field 后黑线消失 → 颜色场网格的**边界单元**
把抗锯齿/描边墨色像素（比区域内部暗）平均进了颜色，双线性采样后在填充边缘形成暗色虚线。
**修法**：_component_color_field 只用**内部像素**（区域 DT ≥ 1px；区域过细则回退）估计单元颜色；
边界单元（无内部像素）用最近邻有效单元逐轮扩散回填（_backfill_cell_colors），不再用代表色涂平。
**实测（同口径 R=1024，同参）**
| 例图 | 修复前 PSNR/SSIM | 修复后 PSNR/SSIM |
|---|---|---|
| pale_girl | 23.23 / 0.8297 | **23.50 / 0.8565** |
| chibi | 19.73 / 0.8347 | **19.76 / 0.8399** |
胸口接缝黑线消失（对照裁图 ssets/out/audit/seamfix/）；各例图成品与视频已用新管线重出。

## 8.10 2026-10-18 近背景色内容被误判为背景（用户报告：填色消失、肤色不填）

**症状**：成品图里部分区域填色整块消失（白色洞/白边），皮肤等浅色区域没有填色。
**归因（算法层）**：coverage.analyze 的背景/前景判定 = 与背景色距离 ≤ fg_tol（最高 24 颜色单位）。
近白的真实内容（肤色 rgb≈252,242,240，dist_bg≈15–18；浅蓝衣褶；头发浅高光）落在门限内 →
被当成背景剔除：无覆盖率、无标签、无区域 → 填色层直接缺一块。调色板聚类阶段还有第二个同源缺陷：
与背景差 6–24px 的真实色调簇被整簇丢弃（keep2 = d_bg > fg_tol），浅肤色因此没有自己的墨色。
**修法**：
1. 内容判定 = **最近色分类**：dist_ink + 1 < dist_bg 的近背景像素救回为内容（cov 下限 0.6），
   纯背景/JPEG 晕圈（更接近背景）仍归零；
2. 调色板近背景簇保留门限从 g_tol 降到 6（只有真正的 AA 尾才丢）；
3. 新增回归单测 TestPaleContentSurvival（封闭浅色内部必须进入 mask/标签；纯背景角落不得产墨）。
**实测**：manga 图的头发白洞、浅蓝袖子白块、肤色（胸/脸）恢复填色；已有例图指标不变或微升
（logo 23.468 不变、pale 23.503→23.506、chibi 19.761→19.765、horn 20.913→20.967）。

## 8.11 2026-10-18 带洞填充 + 短笔触保真（算法级改进）

**用户诉求**：继续改进（基于上一轮"填色消失/细线丢失"的归因）。

**改动**
1. **hole-aware region_fill**：`region_fill` 新增 `fill_holes`（世界坐标洞环）。decompose 从
   `trace_boundary_loops` 提取洞并输出；渲染端（逐笔画/一笔画/视频）先填外轮廓、再逐洞用
   `base_color` 挖掉；一笔画填充侧表升级为 6 元素
   `[t0,t1,[r,g,b,a],poly,color_field_or_None,holes_or_None]`（旧 4/5 元素兼容）。
   环状实心结构在洞显著（>12% 面积）且洞数≤64 时强制走填充（flat_art 也生效），不再回退中轴丢填色。
2. **短笔触保真**：长度 <0.5×`min_branch_length_px` 的分支不再一律丢弃，改按墨量
   （长度×中位全宽 ≥2.5px²）保留为短笔画；只丢亚像素噪声。`W_SHORT_BRANCHES` 分别报告保留/丢弃。
3. 新增单测：decompose `TestHoleAwareFill`（带洞填充/短分支墨量/小墨块不丢）、render/onestroke/video
   洞渲染与旧格式兼容（render +8、onestroke +3、video +2）。

**验证**：洞渲染端到端（合成环）逐笔画 PSNR 31.09dB、一笔画 vs 逐笔画 34.02dB、视频末帧洞=底色；
短笔触保真使 manga 重建笔画 3882→5688（+47%），PSNR 19.66→19.61（基本持平，换来排线保留）；
既有例图无回归：elf 23.41 / logo 23.55 / horn 21.04 / chibi 19.77 / pale 23.51（同口径 R=1024）。
边界：洞数>4 的蔓延线网、插画类仍回退中轴（强制带洞填充实测 horn −3.7dB / chibi −0.2dB，已加限制）。
全量测试 245 项全绿；验收 MUST 27/27（36 通过 / 2 已知非 MUST 失败）。

## 8.12 2026-10-18 A/B/C 三方向（线稿锐度 / 近背景浅色 / 高分辨率）

按用户要求三项都做，A 先落地：

- **A 亚像素边界**（默认启用）：`_subpixel_loop` 将 region_fill 的格点轮廓沿法向对齐到覆盖率场
  50% 等值线（σ=0.8px、±1.2px 搜索、逐顶点双线性求交）。仅对 AA 像素占比 ≥5% 的非插画源启用
  （硬量化/NEAREST 源与插画类强制启用会掉分：logo −0.15dB、chibi −0.16dB，已加闸门）。
  实测：elf 23.41→**23.56dB**（SSIM 0.9213→0.9226）、manga 19.61→19.65、horn 21.04→21.08；
  horn 边界环 MAE 28.27→27.90（误差能量占比 44.9%→44.5%）。
- **B 近背景浅色补色**（`--pale-band-palette`，**默认关**）：对封闭浅色块补 ≤4 个调色板颜色。
  修复过程中修掉两个真实回归：白纸/边角浅色与背景连通 → 会把整张纸变内容（horn 21.1→13.5dB），
  加"封闭组件"过滤；插画类补色掉分 → 按类别闸门。最终 manga 仍为负收益（19.65→19.61、
  SSIM 0.7679→0.7551），故默认关闭、保留为可选项与负结果记录。
- **C 高分辨率**：manga 以 R=1536 重建（PNG 1:1 交付）。PSNR 19.49（@1536 口径）低于 R1024 的
  19.65、SSIM 持平，证明分辨率不是保真瓶颈；高分辨率的价值在展示锐度/细节密度。

全量测试 245 项全绿；验收 MUST 27/27（36 通过 / 2 已知非 MUST 失败）。

## 8.13 2026-10-18 暗墨细节保护（D：插画类，用户要求"要成果"）

**症状**：pale 脸部/发丝/眼睑的细暗线在重建里变淡或消失（小连通块被吸收进浅色区域）。
**修法**：`absorb_small_components` 增加暗墨保护——小块标签色与吸收目标色差 ≥24（0..255 欧氏）
且明显更暗（亮度差 >8）时不吸收，保留为独立标签（之后由笔画/填充层表达）。仅对插画类启用：
线稿类启用会保留 JPEG 暗噪点/双重描线，实测 manga 19.65→19.18dB（已闸门）。
配套：`region_fill` 内部洞若以墨色内容为主（而非背景空白），实心填外轮廓、洞内细节由后续笔画层覆盖。

**实测（R=1024，同参）**
| 例图 | 修前 | 修后 |
|---|---|---|
| pale_girl | 23.506 / 0.8565 | **23.974 / 0.8561** |
| chibi | 19.765 / 0.8399 | 19.765 / 0.8399 |
| elf / horn / logo / manga | — | 与修前逐位一致 |

**本轮合计（A+D）**：elf 23.410→23.560（+0.15）、pale 23.506→23.974（+0.47）、
manga 19.614→19.654（+0.04）、horn 21.044→21.078（+0.03）；logo/chibi 不变。
B（近背景补色）实验为负收益默认关闭；C 交付 R=1536 高分辨率渲染（PSNR@1536 19.49，分辨率非瓶颈）。
全量 245 单测全绿；验收 MUST 27/27。

## 8.14 2026-10-18 纹理感知的表示选择（E）

**做法**
1. **texture_dot 表示**：长度 <2.5px 的短墨段（排线点/网点/碎笔）不再走"圆头短笔画"（圆头把
   1–2px 的点放大、过黑），改为点盘元素：半径 = max(0.5, 宽度/2, sqrt(墨量/π))，颜色/alpha 取
   该墨段覆盖率最高处；只对真实抗锯齿源启用（logo 无效果、无回归）。manga 产生 467 个 dots。
2. **暗墨薄结构保护**（D 的线稿版）放 `--texture-preserve` 实验开关：保留"暗墨在浅底上的薄纹理"
   为独立标签；配套"墨色洞实心填外轮廓、由笔画层覆盖"。
3. 洞逻辑按洞的**内容类型**分流：背景洞（≤4 个）→ 带洞填充挖背景；背景洞很多 → 线网回退中轴；
   墨色洞为主 → 实心填充、由笔画层覆盖。

**实测（R=1024，同参）**
| 例图 | 本轮前 | 本轮后（默认） |
|---|---|---|
| manga | 19.654 / 0.7679 | **19.665 / 0.7681** |
| horn | 21.078 / 0.9139 | **21.097 / 0.9140** |
| elf | 23.560 / 0.9226 | **23.574 / 0.9227** |
| pale（D 插画保护） | 23.506 / 0.8565 | **23.974 / 0.8561** |
| logo / chibi | — | 逐位不变 |

**`--texture-preserve` 的诚实结论**：manga 19.665→19.130dB（−0.54）、SSIM 0.7681→0.7786（+0.011）；
视觉上排线/发丝更多，但存在洞内墨线覆盖不全造成的白色缝隙伪影 → 默认关闭，作为实验开关保留；
下一步需"洞内按源色填充 + 墨线完整覆盖"才能兼顾结构指标与像素指标。

全量 245 单测全绿；验收 MUST 27/27（36 通过 / 2 已知非 MUST 失败）。

## 8.15 2026-10-18 照片实验通道（新例图 lenna）

用户提供 lenna 照片作新例图。默认合同（D5）对照片明确拒绝，实测证据：
唯一颜色 148279（阈值 ≥4096）、颜色熵 6.95（≥6.0）、噪声 2.88（≥1.5）、平坦区 0.195（<0.6）。

新增 `--allow-photo-experimental`（默认关，合同与验收不变）：照片按"海报化插画"分解，
输出 `W_PHOTO_EXPERIMENTAL` 警告。lenna 实测（R=1024）：
- v1（colors48 / denoise1.0 / min-area40 / field-grid48）：PSNR **19.685** / SSIM **0.7205**，
  1460 笔画 / 192 填充——细节较多但有碎线/斑块。
- v2（colors64 / denoise2.0 / min-area150 / prune0.3 / min-branch20 / fill-soft2.5）：
  PSNR 18.641 / SSIM 0.7058——更干净、更风格化但保真更低。
结论：本 skill 的表示（区域填充+中轴笔画）适合平涂/线稿/插画，不适合照片；实验通道只用于
海报化演示。默认 build 对照片仍抛 `E_PHOTO_DEFERRED`；全量测试 246 项全绿、验收 MUST 27/27。

## 8.16 2026-10-18 自适应梯度面片（A-GMesh，连续色调/照片表示）

**动机（用户要求）**：不借助外部海报化预处理，用算法自身的表示精度表达照片。

**方法**
1. 四叉树面片：每片一个矩形 + 2×2 颜色场（四角颜色，渲染端双线性采样）；
2. 误差驱动细分：按面片残差 RMS 用**优先队列**优先分裂误差最大的面片，保证预算下误差最优分配；
3. 四角颜色按渲染端真实插值基（边缘钳制 + 中间线性）做**最小二乘**，序列化后重建误差最小；
4. 复用 CONTRACT 的 `region_fill + region.color_field` 表示，逐笔画/一笔画/视频三路零改动；
   `fill_soft_px=0` 硬边拼接；面片是表示单元，不受 `--max-curves` 截断。

**结果（lenna，R=1024，同分辨率口径）**
| 配置 | 面片 | PSNR | SSIM | .fbs |
|---|---|---|---|---|
| 旧"海报化插画"路径 | 1460 笔画 | 19.685 | 0.7205 | 17.9MB |
| A-GMesh `--mesh-tol 0.03` | 12976 | **33.550** | **0.8600** | 25.4MB |
| A-GMesh `--mesh-tol 0.02 --mesh-max-cells 24000` | 21139 | **35.620** | **0.8958** | 40.3MB |

自重建极限（同口径）：tol 0.03→34.0dB / 0.02→35.9dB，实测渲染与自重建差 ~0.3dB。
默认 build 对照片仍抛 `E_PHOTO_DEFERRED`（合同不变）；`--allow-photo-experimental` 启用 A-GMesh。
新增 4 项单测（渐变精度/硬边细分与覆盖性/预算报告/文档集成与不截断）；全量测试 250 项全绿。

## 8.17 2026-10-18 纯笔触绘画 + 画笔属性时间轴（目标表示）

**用户定义（纠正方向）**：不要"复刻图像"的色块/面片；整个绘画过程必须能总结为
**一串傅里叶函数 + 一串随时间 t 定义的画笔属性**，在函数与笔刷属性上模拟真实画师笔触。

**实现**
1. `brushfit.decompose_painterly`：纯笔画分解，输出逐点宽度/颜色/alpha 的路径元素，
   **无 fill_poly/fill_holes/regions**（单测强制断言）。
2. 算法：分层（笔刷半径 22→14→9→5.5→3 @512）由粗到细；每层参考图模糊到笔刷尺度，
   在误差明显处落笔；方向沿结构张量切向积分（弯曲运笔）；压力曲线两端细中段宽；
   颜色取该层概括色；alpha 分层叠加。
3. `timeline` 子命令 / build 自动导出：`*.brush_timeline.json` = 样式表 + 逐笔
   `{t0,t1,fourier coef,width 曲线}`，时间为可编辑作画时间轴（时长∝弧长、允许重叠）。
4. 照片通道：默认仍 `E_PHOTO_DEFERRED`；`--painterly`（或旧 `--allow-photo-experimental`）
   显式开启后走纯笔触通道；A-GMesh 降级为 `--mesh` 旧实验（不属于目标表示）。

**lenna R=1024 实测**：5168 笔 / 0 填充 / 0 区域，PSNR 22.909 / SSIM 0.6729，
`.fbs` 23.9MB，时间轴 9.5MB，60s 720p 过程视频 9.9MB；渲染耗时 243s（逐笔画通道）。
新增 7 项单测（纯笔画契约/分层粗细/预算/照片通道/时间轴结构/系数精度/文件读写）；
全量测试 262 项全绿；验收 MUST 27/27。

## 8.18 2026-10-18 笔刷库安装（现代绘图软件笔刷分类 → 可执行预设）

**调研**（只取分类/行为，不复制素材）：
Krita 4 预设包分类与笔刷引擎（Pixel/Bristle/Chalk/Color Smudge/Spray/Particle…）、
Photoshop 笔刷预设家族与动态项、Procreate 笔刷库分区、Corel Painter 笔刷类别。

**实现**
1. `shapelib.py`：注册 10 个笔形内核 `round/soft/marker/bristle/chalk/spatter/airbrush/
   pencil/watercolor/ink`（render 的精确带覆盖 × 确定性纹理/衰减核；无贴图、无填充）。
2. `brushlib.py`：20 个笔刷预设，字段 = shape / size_mult / alpha 区间 / width_jitter /
   color_jitter / pressure / length_mult / category / notes；`LAYER_BRUSHES` 定义由粗到细
   五层各自允许的笔刷，模拟画师换笔。
3. `brushfit.py`：每笔按层抽预设，宽度抖动/alpha/颜色抖动/压力锥度写入逐笔属性曲线；
   element 携带 `shape` 与 `preset`（诊断/时间轴）。
4. `timeline` 导出的 `styles[].shape` 即笔形名；笔刷目录图 `brush_catalog.png` 给出
   每个预设的真实渲染样本。

**验证**：5 项单测（预设合法性/分层引用/10 个笔形内核对每帧确定/纹理内核高频能量更高/
纯笔触确实多笔刷多笔形）；全量测试 262 项全绿；验收 MUST 27/27；hygiene 0。

## 8.19 2026-10-18 结构感知笔触绘画（structpaint，实验）

**用户要求**：笔触要像人类画师——顺结构走向；按部位（线条/涂色/尖锐结构）谨慎选笔刷；
先对结构做局部实验并记录，再"强化学习"后尝试完整图像。

**实现（scripts/structpaint_demo.py + fbs_core/structpaint.py + structdoc.py）**
1. 结构分解：复用调色板区域 + 中轴分解，按面积/色场方差/线条宽度分为
   flat / shade / detail / line_fine / line_thick，并算块面主方向与区域 mask；
2. 局部 patch 实验：每类取若干 patch，对候选策略（预设+宽度/长度/透明度）实际重绘，
   只在该类元素覆盖像素上算 PSNR/SSIM，记录 `experiments/brush_policy_experiments.{json,md}`；
3. UCB1 多臂老虎机：reward=0.7×归一化PSNR+0.3×SSIM 选每类笔刷；
4. 完整作画：先铺大块（骨架宽笔+硬圆底色）、再明暗、后线条（沿骨架墨线笔，压力两端收）；
   输出纯傅里叶笔触 `.fbs`（0 填充多边形）。

**实测（chibi 鲸鱼娘 R=1024）**：结构 flat56/shade20/detail49/line_fine39/line_thick435，
4742 笔，PSNR 15.24 / SSIM 0.733。**结论：框架跑通，但视觉仍不理想**：
区域分割过粗、骨架笔宽外溢、线条重复描画——见 `references/structpainting.md` 的
"已知不足/下一步"（含在线 stroke RL 设计）。

新增 5 项单测（UCB 选择/策略合法性/区域 mask 几何/结构笔触生成/笔触→文档可渲染）；
全量测试 267 项全绿；验收 MUST 27/27。

## 8.20 2026-10-18 结构感知绘画 v2：线条合并 + 端帽内缩 + 在线 stroke RL

在 8.19 框架上按"局部实验 → 训练 → 完整图"继续训练：

1. **线条保守合并**（`merge_line_chains`）：中轴碎片 474 → 355 条（端点 2.5px、
   方向 cos≥0.72、单链 ≤220px）；修正上一版把整幅线稿串成 1 条巨链的 bug。
2. **端帽内缩**：填充/线条笔画两端沿弧长裁 0.4×半宽，抑制圆头端帽越出轮廓 / 外飞线。
3. **两遍铺色**：flat=硬圆底色笔铺满（干净平涂）；shade=底色+15% 透明度肌理；
   detail=策略小笔实填。补洞点盘只用底色笔，消除"气泡"。
4. **在线 stroke RL**（`fbs_core/stroke_rl.py`）：维护浮点画布逐笔落笔，按局部误差变化
   接受/撤回（ΔE<0 才保留，中性笔 35% 概率保留），每类结构用 **UCB 在线学习
   保守/标准/大胆三个"用笔力度臂"**；训练日志 `stroke_rl_log.{json,md}`（接受率、
   每臂奖励、每 200 笔误差曲线）。

**实测（chibi 鲸鱼娘 R=1024）**：规划 4164 笔 → RL 接受 2856（68.6%）、训练画布
PSNR 15.72→16.36；最终渲染 **15.90 / 0.743**（v1 15.24 / 0.733）。
**未解决**：区域分割粒度、RL 决策画布（软边近似）与真实 10 笔形渲染不一致（最终
15.90 < 训练 16.36）——下一步让 RL 使用真实渲染链/笔形近似。

新增 2 项单测（线条合并共线/非共线、stroke RL 接受与日志）；全量测试 269 项全绿。

## 8.21 2026-10-18 分辨率实验 + 训练前测绘 + RL 真渲染器 + 次评分

**用户要求**：①确认提高 R 是否有益（有益则 R=4096 再训练）；②先做"封闭轮廓分区 + RL 对齐真渲染"
优化并验证有效再训练；③训练前对原图做高精度测绘；④训练评分除保真度外，低权重计入
笔触数量与连续线段数量。

**结论与实现**
1. **分辨率**：同一套笔触渲染 R=1024/2048/4096 → PSNR 15.954/15.913/15.891（不升），
   SSIM 0.7569/0.8165/0.8715（升）。源图 421px 是 Nyquist 上限（预处理只降采样），
   放大后测量触发碎片化（线条 474→2208、笔画规划 62k、J 变差）→ **不提高到 R=4096 训练**；
   采纳：测量/训练在源分辨率，渲染 R=2048（展示 4096），报告 `references/resolution_survey.md`。
2. **训练前测绘**：新增 `survey.py` + `scripts/survey_image.py`，输出线宽/长度/区域/AA 带/
   分辨率收敛性 JSON+MD（原图：线宽中位 2px、AA 带 0.19、区域 125 个）。
3. **封闭轮廓分区**：实现并对比，R=1024 下 13.27dB < palette 15.91dB → **验证失败，不采纳**
   （线稿墙闭合性不足/切碎），代码与实验记录保留。
4. **RL 对齐真渲染**：`stroke_rl_render.py` 用真 FloatCanvas 逐笔决策，决策-渲染差
   0.46→**0.22dB**；实测接受 2434/4164（58.5%）。
5. **次评分**：`J = PSNR − 0.02×(笔数/1000) − 0.01×(线段数/100)`，训练日志记录笔数/线段数/J。
   最终：笔数 2434、线条段 139、J=16.07，渲染 @2048 **15.913 / SSIM 0.8138**。

新增 2 项单测（survey 输出/报告文件、真渲染器 RL 的 J/线段统计）；全量测试 271 项全绿。

## 8.22 2026-10-18 线稿闭合分区 v2 + 连续动作 RL + 主动剪枝 + 9h 训练循环

**用户要求**：按上轮方案继续做，无严重问题就启动至少 9 小时的训练循环。

**实现**
1. **线稿闭合 + 区域生长（contour v2）**：线条端点 5px 内桥接、形态学闭运算封缝、
   线条像素按最近区域生长（填色覆盖到线条下方）；小区域按邻居多数合并。
   在训练循环里作为可探索的分区之一（与 palette 用 UCB 竞争）。
2. **RL 连续动作搜索**（`run_stroke_rl_search`）：每笔除力度臂外采样位置(±4px)/
   宽度(×0.8–1.2)/alpha(×0.85–1.15)/颜色(±0.03) 候选，在真 FloatCanvas 上逐个
   局部评估取最优；记录每笔收益供剪枝。
3. **次评分主动剪枝**：按收益升序尝试删 0/10/20%，同分辨率渲染评估，取
   J=PSNR−0.02×(笔数/1000)−0.01×(线段/100) 最大且 PSNR 损失≤0.15dB 的变体。
4. **训练循环**（`trainloop.py`）：每轮结构抖动 → RL → 剪枝 → 评估 → checkpoint；
   `state.json` 原子保存以断点续训，`DONE` 标记正常结束；wrapper 崩溃自动重启至 deadline。

**首轮实测（eval R=1024）**：规划 4164 → 接受 2822 → 剪枝 10% 后 **2540 笔 / 150 线段**，
PSNR **17.216** / SSIM **0.7698** / J=17.151（上一版同口径 15.95，+1.27dB），单轮 89.6s。
按此速度 9.35h 可跑 ~370 轮，持续刷新 best J / best PSNR 到 checkpoints。
