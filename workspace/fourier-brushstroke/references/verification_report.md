# 独立验收审计报告（fresh verifier）

- **审计对象**：`fourier-brushstroke` v2（重建版），根目录 `fourier-brushstroke/`
- **审计方式**：fresh 上下文独立复跑，不继承 Lead/队友自述；所有数字为本人实跑或从产物独立复算，文档声称一律回查代码
- **审计时间**：2026-10-18 04:09–04:38（Asia/Shanghai）。冻结后最终一轮：04:30–04:38
- **审计环境**：Windows 11 / Python 3.13.14 / numpy 2.5.4 / Pillow 12.3.0 / scipy、imageio、imageio-ffmpeg 可用；`PY = <python>`；所有命令在 `scripts/` 下执行
- **代码快照（冻结）**：`assets/out/acceptance/report.json` 记录 `code_snapshot.digest = e823254a4683e2df`（18 个 .py 的 sha256 前 16 位聚合）。**本人在审计时重算了该 digest，与当前磁盘文件完全一致**；本人独立 quick 复跑（04:33:16）同样生成 `digest=e823254a4683e2df`，可作为"官方报告=当前代码"的交叉证据。
- **本报告产物位置**：本文件仅此一份；全部复现输出在 `assets/out/verify_*`（`verify_final/`、`verify_final2/`、`verify_acceptance/`、`verify_acceptance_items2/`、`verify_acceptance_fullkey/` 等），未改动任何他人文件。

---

## 0. 结论摘要

**一句话**：`fourier-brushstroke` v2 **可用**——线稿/签名/图标/扁平插画能跑通 build→onestroke→60s 视频→任意分辨率渲染→四维度量的完整链路，照片/损坏/透明等异常输入有明确拒绝，单元测试与静态卫生全绿；**自身的 MUST 验收 27/27 全过**。但它**不是"验收全绿"的交付**：2 个非 MUST 项失败（A10 SHOULD、A11 MAY），2 个设计 MUST 项以"判据修正/NOT_IMPLEMENTED"方式处理而非按原文达标（A8、A6），另有 2 个测试基础设施问题和若干文档/打包清理项需在交付说明中明示。

### 三态计数（逐项核验表 41 行）

| 状态 | 数量 | 明细 |
|---|---|---|
| **PASS** | **38** | R1–R8、R10；A1–A5、A7–A9、A12–A15；B1–B4；T1–T13 |
| **FAIL** | **2** | A10（SHOULD）、A11（MAY） |
| **UNVERIFIED** | **1** | B5（SVG 在 Illustrator/Figma 打开——无 GUI 无法验证） |
| 不计入三态 | 3 | A6 = NOT_IMPLEMENTED（设计 MUST，v2 明确不做）；R9 = 用户已搁置；A8-legacy = BROKEN（判据修正记录） |
| 额外专项 | 1 PASS | R5b（非设计文档项，stamp vs sweep 生产基元一致性） |

### 官方验收最终口径（`assets/out/acceptance/report.json`，2026-10-18T04:31:58 全量）

- 40 项：**36 PASS / 2 FAIL / 1 BROKEN / 1 NOT_IMPLEMENTED / 0 BLOCKED / 0 SKIP**，耗时 63.44 s
- **MUST 27/27 全过**；FAIL 仅 `A10`（SHOULD）、`A11`（MAY）
- 报告 `ok=false`、`report.md` 结论写"❌ 失败"，**仅因 2 个 SHOULD/MAY 项失败**（见问题 M1）
- 与本人独立 quick 复跑（04:33:16，digest 相同）在全部重叠项上数字一致；`R3/R8` 在 quick 模式为 SKIP，已由本报告独立复验（见 §5）

---

## 1. 审计快照（冻结态）

代码快照 digest：`e823254a4683e2df`（报告内 18 文件聚合，本人重算一致）。关键文件 sha256 前 16 位 / mtime（04:36 复核）：

| 文件 | sha256[:16] | mtime |
|---|---|---|
| `scripts/fbs.py` | `42612c9f811dac7b` | 04:29:07（冻结前最后 2 处 CLI 修复） |
| `scripts/run_acceptance.py` | `01fac387442c2765` | 04:30:39（A8/A15/T13 判据修正） |
| `scripts/fbs_core/decompose.py` | `ccd999cd20b24991` | 04:22:30 |
| `scripts/fbs_core/render.py` | `3ea0bc67a94de789` | 04:17:42 |
| `scripts/fbs_core/onestroke.py` | `9d2de42f4eeb1175` | 04:10:22 |
| `scripts/fbs_core/video.py` | `25d230f10ec664a3` | 04:16:41 |
| `scripts/fbs_core/coverage.py` | `01eebe31e6922244` | 04:15:12 |
| `scripts/fbs_core/preprocess.py` | `319b5bf1a0e1ef8c` | 04:08:21 |
| `tests/`（10 个 .py） | — | ≤ 04:18:33 |
| 文档 `SKILL.md` / `README.md` | — | 04:34:34 / 04:34:28（终版：嵌入终版验收结果与代码快照 digest） |
| `references/requirements_traceability.md` | — | 04:32:42（终版回填，与 04:31:58 报告一致） |

> 文档在 04:32–04:34 完成"终版结果回填"（SKILL/README 写入 36 PASS/2 FAIL/MUST 27/27 与 digest）；本人核对文档数字与官方报告一致。审计结论以 04:35 的磁盘状态为准。

> 审计过程中代码与验收脚本持续变动（04:09–04:31）。冻结前的中间快照曾复现出 `onestroke NameError`（h 已由其 owner 修复）、A8/A10/A11 旧判据、验收报告被旧代码代进程覆盖等问题。**本报告最后结论一律以 04:31 之后冻结快照为准**，历史现象仅在 §6/§7 作为审计轨迹记录。

---

## 2. 需求覆盖：R1–R10（交接说明 §2）

| 编号 | 需求 | 实测证据 | 状态 |
|---|---|---|---|
| R1 | 傅里叶表示 + 渲染，保真尽量高 | T1@R1024 **35.9082 dB / SSIM 0.99475**；T3@R1024 **35.5776 dB / 0.9839**；chibi 真实图 R1024 **19.0382 dB / 0.8235**（记录值，无硬阈值）；build 产 `.fbs`+PNG+SVG+report | **PASS**（彩插保真中等，属设计预期"⚠️ 谨慎"场景） |
| R2 | 单次 FFT + 一笔连续画完 | ONESTROKE：`mode=one_stroke`、恰好 1 条闭合 stroke、`stats.fft_passes=1`；T1 路径偏差 RMS **0.001287 px**、max 0.0611；真实图 chibi N=65536 RMS **0.019 px**、horn N=65534（Nyquist 钳制+`W_N_CLIPPED`）RMS 0.0038 px | **PASS** |
| R3 | 视频 ~60s、不省略步骤、t/弧长等比 | 演示视频 horn/chibi 均 **1800 帧 / 60.00 s / 30 fps / 720×1080**；horn 608 步、chibi 940 步，`frames_ge_steps=true`、`steps_covered=true`；`timeline="arc"`，`|arc_cum/total − t|` 最大 **1e-05**；`step_spans` 区间连续覆盖 [0,1]（gaps 全 0） | **PASS** |
| R4 | 笔触属性完整（线宽/颜色/填充/笔形） | R4 专项 PASS：25 条 stroke 的 width/style 齐全；T3 fill_count=8；`shape` 为分类曲线（step）；format spec §4 字段与 `io_format.validate` 一致 | **PASS** |
| R5 | 画出的必须是线，不是点串 | R5 专项：中心线 200 采样点覆盖率 **1.0**、墨迹连通域 **1**（点串会碎裂）；sweep 语义 + 逐点宽度/颜色；抬笔段 width=0 跳过（代码/契约核对） | **PASS** |
| R6 | 宽度/颜色可平滑渐变 | R6 专项：宽度相邻最大跳变比 **0.0249 ≤ 0.10**；相邻 ΔE **2.04 ≤ 12**；T12 颜色反演平均 ΔE **16.993 ≤ 25** | **PASS** |
| R7 | 先铺底层再压上层 | R7 专项：T2/T3 的 fill 层 < medial 层、fill 面积降序无违规；渲染 layer 升序、region_fill 底层（代码核对） | **PASS** |
| R8 | N≈65536 + t 精度 + 适用条件/Nyquist | horn N=**65534**（Nyquist 钳制，`W_N_CLIPPED`）RMS 0.0038/max 0.0735 px；chibi N=**65536**（未钳制）RMS 0.019/max 0.1449 px；均 `fft_passes=1`；Nyquist 上限与"65536 只在独立采样足够时有意义"写入 SKILL/format spec，且代码钳制有告警 | **PASS** |
| R9 | 元素拆解+分层+每层一次 FFT 的生成架构 | **用户已明确搁置**，SKILL/README/追溯表一致声明"不重启" | 搁置（不计） |
| R10 | 输入图固定为两张测试图 | 两图正式演示产物齐备：`assets/out/demo/{horn,chibi}/` 各 6 件（`.fbs.json`/`_R1024.png`/`.svg`/`.report.json`/`.onestroke.fbs.json`/`_1min.mp4`）；chibi 质量数字已由本人重渲染复算；horn 的 PSNR 无法独立复算（原图当前不可见，见 UNVERIFIED-1） | **PASS（1 项复算 UNVERIFIED）** |

---

## 3. 验收项覆盖：A1–A15（设计 v2.1 §7）

| 编号 | 级别 | 阈值（设计原文） | 实测 | 状态 |
|---|---|---|---|---|
| A1 | MUST | 线稿 PSNR @R1024 ≥ 30 dB | T1 **35.9082 dB**（klass=lineart） | **PASS** |
| A2 | MUST | 线稿 SSIM @R1024 ≥ 0.90 | T1 **0.994749** | **PASS** |
| A3 | MUST | N–R 线性律（200→400→800 倍率 1.8–2.2） | **判据已按旧版实测修正**：契约 `max_err·R ≤ ε`（0.4971/0.4930/0.4987 ≤ 0.5）、紧致 `≥ 0.5ε`、N 中位严格增 31→50→69 均成立。原文"每档 2×"判据已被历史实测证伪（旧版 1.17–2.74 波动），属**判据修正**非达标 | **PASS（修正判据）** |
| A4 | MUST | 全流程零外部网络依赖（断网跑通） | 静态 AST 扫描：scripts/ 与 fbs_core/ 无任何网络库 import；全链路无网络调用。**物理断网未实测**（UNVERIFIED-2） | **PASS（静态）** |
| A5 | MUST | 异常用例 0 崩溃且原因明确 | A5 专项 PASS：照片 `E_PHOTO_DEFERRED`、全透明 `E_INPUT_TRANSPARENT`、损坏 `E_INPUT_CORRUPT/FORMAT`、过小、纯色 `W_SOLID_COLOR` 全部明确；本人 CLI 复测损坏图/照片均 exit=2 且带证据与建议 | **PASS** |
| A6 | MUST | 双渲染后端一致性（Python vs HTML Canvas）< 1px | v2 **不实现** HTML Canvas 后端（Lead 裁决，不在 R1–R10 内）；验收报告以 `NOT_IMPLEMENTED` 记录，不计失败；渲染一致性由 R5b 覆盖 | NOT_IMPLEMENTED（不计） |
| A7 | MUST | 宽度反演相对误差 < 5% | T4：均值 **0.276%**、最大 0.461%、32 点口径 0.219%（31/32 点有对应） | **PASS** |
| A8 | MUST | stamp ≤ 50% × 变宽线段误差 | **设计原文未达**：干净 spec doc 下 stamp band **0.2069 px**、line band **0.3327 px**，比值 **0.6219 > 0.5**。冻结前判据修订为 `stamp_band ≤ 0.5·ε（=0.25px）且 ≤ line_band`，据此 **PASS**；旧判据实测作为 **A8-legacy=BROKEN** 保留（pipeline 载体诊断比值 7.17）。**属判据修正，不是按设计原文达标** | **PASS（修正判据）；设计原文未达** |
| A9 | SHOULD | 扁平插画 PSNR @R1024 ≥ 26 dB | T3 **35.5776 dB** | **PASS** |
| A10 | SHOULD | 中轴分支数 / 预期 ≤ 1.5× | T4 单笔画被切成 **2 段** medial（ratio **2.0**） | **FAIL**（已知未解决，见 M4） |
| A11 | MAY | `.fbs`/PNG ≤ 3× | T3@R512：284,554 B / 7,619 B = **37.35×**（主因 color_field + JSON indent） | **FAIL**（MAY，设计已放弃体积目标，见 M4） |
| A12 | MAY | SVG 体积记录即可 | 导出成功，SVG/fbs 比值记录（04:26 轮 0.446；导出 XML 合法） | **PASS** |
| A13 | MUST | 锥形笔画宽度反演 32 点 max 误差 < 5% | T4 **0.219%**（全样本 max 0.461%，≤2 点无对应已记录=1） | **PASS** |
| A14 | MUST | linear/spline/preset 三表达渲染差异 < 1% | 两两平均绝对差 worst **0.0009**（≈0.09%） | **PASS** |
| A15 | MUST | 6 种 interp + step 区间语义正确 | 属性层 8/8 项 true；T13 端到端：`orientation=reversed`（前向误差 151.7%、反向 5.62%），w(0.25)=20.02 vs 20、w(0.75)=5.979 vs 6、边界 t=0.52。**判据已修正**：方向取优 + interp step 降为偏好项（`interp_step_preferred=false` 如实记录）。见 M3 | **PASS（修正判据）** |

---

## 4. 定性项 B1–B5 与合成用例 T1–T13

### B 组

| 编号 | 要求 | 实测 | 状态 |
|---|---|---|---|
| B1 | 一条命令线稿→.fbs+PNG(+report)，无需手工调参 | `fbs.py build T2_icon.png -o ... --resolution 512 --mode sweep` → exit 0、产物齐、report 含 quality.psnr，实测 3.32 s（T1@512 2.66 s） | **PASS** |
| B2 | 报告区分忠实/降级复刻 | 忠实样例 T1 四维齐备；但"降级样例"实为验收台的写盘 bug 被当作"明确拒绝 E_FBS_INVALID"（见 M2），**结论可达但证据路径不成立** | **PASS（证据有缺陷，建议修后重跑）** |
| B3 | 笔刷属性渲染肉眼可辨 | 消融实验：宽度 changed_px 3926、P99 差 230/255；颜色 changed_frac 1.5%+，可见 | **PASS** |
| B4 | `.fbs` JSON 可人工阅读/微调 | 本人直接读取并解析全部 demo `.fbs`（含中文全角冒号文件名），字段自描述、UTF-8 无 BOM、indent=1 | **PASS** |
| B5 | SVG 可在 Illustrator/Figma 打开 | 无 GUI 环境无法实测；已验证导出 SVG 为合法 XML（`{svg}svg` 根、width/height/viewBox 正确、体积正常） | **UNVERIFIED** |

### T1–T13（合成用例，manifest seed=20261018 count=22）

全部 13 项在终版全量验收中 **PASS**（T1 build 成功且 klass=lineart；T7 照片明确拒绝；T8 纯色/透明边界正确；T9 四种特殊模式转换成功；T10 中文名与损坏文件正确；T13 step 区间判据修正后 PASS）。本人另行抽跑 T1/T2/T3/T4/T5/T6/T7/T8/T9/T10/T11/T12/T13 相关 build，均与报告一致。

---

## 5. 快速开始原样复跑（冻结态，2026-10-18 04:30–04:35）

命令均按 `SKILL.md §快速开始` 在 `fourier-brushstroke/scripts/` 下执行；输入用合成图 `assets/testcases/T1_signature.png`（384×384）与 T2_icon，输出到 `assets/out/verify_final2/`。

| # | 命令（原样） | 退出码 | 耗时 | 产物 / 关键数字 |
|---|---|---|---|---|
| 0 | `fbs.py --help`；`python -c "import numpy, PIL"` | 0 / 0 | 1.4 s / 0.5 s | 列出 6 个子命令；numpy 2.5.4 / Pillow 12.3.0 |
| 1 | `build T1_signature.png -o ... --resolution 512 --mode sweep` | **0** | **2.8 s** | `.fbs` 14.7 KiB / `_R512.png` / `.svg` 44.3 KiB / `.report.json`；PSNR **39.105 dB**、SSIM 0.9986、1 stroke、无 warning |
| 1' | `build ... --resolution 1024 --mode sweep`（SKILL #1 原样口径） | **0** | **6.1 s** | PSNR **35.908 dB** / 0.9947；N 中位 41；0 warning |
| 2 | `onestroke --fbs .../T1_signature.fbs.json`（默认输出） | **0** | **1.7 s** | 产物名已符合文档：`T1_signature.onestroke.fbs.json`（396 KiB）；N=4094、`fft_passes=1`、偏差 RMS **0.0013 px**、`W_N_CLIPPED` 可见 |
| 3 | `video --fbs .../T1_signature.onestroke.fbs.json --out ... --duration 6 --fps 10 --size 360x480` | **0** | **2.6 s** | 60 帧 / **6.00 s** / 10 fps / 360×480 / 0.1 MiB |
| 3' | 同链路 60 s：`--duration 60 --fps 30 --circles 600 --mode sweep` | **0** | **49.6 s** | 1800 帧 / **60.00 s** / 30 fps / 720×1080 / 2.2 MiB（T2 链路同样全通） |
| 4 | `render --fbs .../T1_signature.fbs.json --out ... --resolution 256`（**不带** `--mode`，SKILL 常用配方原样） | **0** | 1.9 s | 256×256 PNG；修复后未给 `--mode` 正确回退 sweep |
| 5 | `eval --fbs ... --reference T1_signature.png`（SKILL #5 原样，不带 `--render`） | **0** | 2.4 s | 自动按参考图原生 384 渲染后度量：PSNR **44.42 dB** / SSIM 0.99962 / IoU 0.9795 |
| 5' | `eval --render <R1024.png> --reference <384 原图>` | **2（设计行为）** | 1.6 s | `E_METRIC_RESOLUTION`：禁止跨分辨率上采样比较；文档已改为不给 `--render` |
| 6 | `acceptance --quick`（本人独立跑，报告目录 `assets/out/verify_acceptance/`） | 1（有 2 个非 MUST 失败） | **32.6 s** | 40 项：34 PASS / 2 FAIL（A10,A11）/ 2 SKIP（R3,R8）/ 1 BROKEN / 1 NI；`digest=e823254a4683e2df` 与官方一致 |

**结论**：冻结后 SKILL 快速开始**可原样跑通**；曾出现的三个文档级 blocker（`onestroke` 默认命名断链、`render` 缺 `--mode` 报错、`eval --render` 示例必然报错）均已修复/改文档，本人逐条复验通过。

---

## 6. 验收报告一致性核对

**最终报告**：`assets/out/acceptance/report.json` / `report.md`，generated **2026-10-18T04:31:58**，全量 40 项，`code_snapshot.digest=e823254a4683e2df`。本人重算 digest 与当前磁盘一致；独立 quick 复跑 digest 相同。**即：当前报告确实对应当前冻结代码，不是旧代码代产物。**

抽查 ≥10 项与本人独立复跑对比（全部一致）：

| 项 | 官方报告值 | 本人独立复跑 / 复算 | 一致 |
|---|---|---|---|
| A1 | 35.9082 dB @R1024 lineart | 直跑 build R1024 = **35.908 dB**、class=lineart；独立 acceptance 同值 | ✅ |
| A2 | 0.994749 | build 复算 0.9947；独立 acceptance 0.994749 | ✅ |
| A3 | N 中位 31/50/69；err·R 0.4971/0.4930/0.4987 | `--items A3` 独立复跑：完全相同 | ✅ |
| A7 | 均值 0.276%、32 点 max 0.219%、n_medial=2 | `--items A7` 独立复跑：完全相同 | ✅ |
| A8 | PASS；stamp band 0.2069 / line 0.3327 / ratio 0.6219 | `--items A8` 独立复跑：完全相同（新判据下 PASS） | ✅ |
| A10 | FAIL：medial=2、ratio 2.0 | 独立复跑完全相同 | ✅ |
| A11 | FAIL：284,554 B / 7,619 B = 37.3479× | 独立 build T3@R512：284,468 B / 7,619 B = 37.34×（差值 0.03%，`created` 时间戳/换行级别差异） | ✅ |
| A13 | max 32pt 0.219%、全样本 0.461% | `--items A13` 完全相同 | ✅ |
| A15/T13 | PASS；orientation=reversed、oriented_err 5.624%、interp=linear、interp_step_preferred=false | 独立复跑完全相同；独立 build T13 复核 `.fbs` 宽度曲线：t<0.47 为 6 px、t>0.5 为 20 px → **确为方向反转，非数值错误** | ✅ |
| R2 | fft_passes=1、1 stroke、RMS 0.001287、max 0.061139、N=4094 | 独立 onestroke 逐项复核一致 | ✅ |
| R3 | horn/chibi 视频 1800 帧/60.0 s/30 fps/720×1080、steps 608/940、timeline=arc | 独立 probe 两视频一致；本人自建 T1 60 s 视频也 1800/60.00/720×1080 | ✅ |
| R8 | horn N=65534（clipped）、RMS 0.0038；chibi N=65536、RMS 0.019 | 直接读取 demo onestroke 文件独立复核：完全一致 | ✅ |
| 真实图 chibi 质量 | 19.0382 dB / 0.8235 | 本人用当前代码重渲染 demo `.fbs` 后重新度量：**19.0479 dB / 0.82384**（Δ=0.0097 dB，量化/渲染重建差异级） | ✅ |
| T1 | 39.1055 dB @R512 / 0.998601 | 独立 build：39.105 dB / 0.9986 | ✅ |

**历史问题说明（已消除，仅记录）**：审计中段（04:10–04:28）该目录的 report.json 曾被一个 04:06 启动的旧代码代进程"跑完回写"覆盖，出现过与当前代码相反的结论（A1=29.36 flat_art、A8=FAIL、A15/T13=PASS 等），也一度被过滤模式重写为 4 项。最终 04:31:58 报告已由 metrics 用冻结代码重跑，digest 吻合，**"报告陈旧/不可复现"问题已解决**。

---

## 7. 契约与文档一致性

| 检查点 | 结果 |
|---|---|
| `.fbs` 读写往返 | `read_fbs(write_fbs(doc))` 往返 OK；v1 `.fbs`（version=1.0）被 `E_FBS_INVALID` 明确拒绝；空 strokes 无 `W_SOLID_COLOR` 时校验报错（符合格式规范）；demo 4 个 `.fbs`（per_stroke/one_stroke × horn/chibi，含中文全角冒号文件名）全部通过 `read_fbs`+`validate` |
| format spec vs `io_format.validate` | 核心结构（format/version/mode/canvas/render_contract/strokes 的 id/closed/fill_role/repr/coef 排序与 N/closure/brush.style+width/layer/region_fill poly/one_stroke 恰好 1 条+closed+one_stroke_fills/fingerprint）均被强制校验，**字段名/必填项/语义一致**。`validate` 未强制 `source/image_class/quality/bbox/stats/warnings` 的"存在性"（规范列举但非硬校验），属宽松而非冲突 |
| AttributeCurve | `attribute.py` 数值 6 种 interp + color 3 种 + shape step，与 format spec §4/§2、CONTRACT §2 一致；preset 公式 `f(t)=scale·(1−strength+strength·g(t))` 与契约一致。**边界**：`spline` + `clamp=False` 时仍钳制而非外推（契约 §2 语义为"否则按端点外推"）——未见于验收用例，属低危语义缺口（minor） |
| CLI `--help` vs 文档 | 6 子命令、全部参数与 SKILL/README 表格一致；默认值均由 `FBSConfig` 提供、argparse 默认一律 None（`check_hygiene` ARGPARSE_DEFAULT=0 命中）；文档已修正 onestroke 命名与 eval 示例，与实测一致。`--mode` render 缺省回退 sweep 已是实测通过 |
| CONTRACT §9 错误/警告码表 vs 代码 | 代码实际产生 **21 个 E_**、**20 个 W_**；CONTRACT 清单覆盖全部实际码，无"代码有而契约无"。**遗留**：`W_COLOR_FIELD_OFF` / `W_ONE_STROKE_TRAVEL` 在 CONTRACT 标注"实现级全集"但代码从不产生（capability_boundary 标注"契约保留码"）；`--color-field-grid 0` 实测无告警（区域退化单色静默发生），与"不静默失败"纪律存在张力（minor） |
| 文档数值 vs 代码 | **`references/capability_boundary.md §3` 与 `preprocess.py` 阈值不一致**：lineart 的 `hard_edge_frac` 文档写 `≥ 0.02`、代码为 **0.008**；flat_art 文档写 `≤ 0.05`、代码为 **0.08**（minor，文档自述"以代码为准"但数字未同步） |
| 验收口径文档 | 终版 04:34 的 SKILL/README 已嵌入终版结果（36 PASS/2 FAIL/MUST 27/27 + digest），traceability §8 同步；但 `run_acceptance.py` docstring 仍写"四组共 **38** 项"（实际 **40**），且 `report.md` 结论用"**❌ 失败**"，与 SKILL"通过标准：**MUST 全过**"的判读口径仍不一致（42–43 行处已注明 `ok = failed==0`，但报告总结未区分 MUST/SHOULD/MAY）——见 M1 |
| 验收证据可追溯 | 官方报告每个 items 带 `cmd`；本报告 §6 抽查数字可回溯到命令+产物；R3/R8/真实图采用"回读 Lead 正式演示产物"（`source=demo_artifacts`，报告已注明），非验收时重新 build——方法学 weaken，但本报告已独立复核 demo 产物有效性 |

---

## 8. 不静默失败与工程纪律（静态 + 动态）

- **静态卫生**：`python scripts/check_hygiene.py --strict` → **exit 0，0 命中 / 27 个 .py**（终版复跑，报告 `assets/out/verify_final2/hygiene.txt`）。AST 规则覆盖裸 except、except: pass、except 只 print、`x or 数字`、argparse 非 None 默认值、按像素 Python 循环、手写 6–9 位精度、UTF-8 BOM 等。
- **裸 except 扫描**：`scripts/` 全部 .py 无裸 `except:`；异常均带类型，降级走 `WarningLog`，拒绝走 `FBSError(code, message, evidence, suggestions)`。
- **写死上限扫描**：抽查 `guard/range(...)/max_iter/for _ in range(...)` 等 20 余处，上限均由数据/图像尺寸/弧长导出（如 `skeletonize` 的 `max_iter=ceil(maxDT)+2`、采样 `M = next_pow2(4·arc·R)`），未发现旧版的 `guard < 20000` / `range(200)` 类数据无关硬上限。
- **单元测试**：`python -m unittest discover -s tests` → **Ran 227 tests, OK**（冻结态复跑：unittest 内部 9.4 s；重定位副本内 8.9 s；含 T1–T13 夹具生成器与各模块测试）。
- **确定性**：同一输入连续两次 build，除 `created` 时间戳外 `.fbs` 逐字节一致（sha256 相同）；无字典序/随机依赖。
- **BOM**：`scripts/`、`tests/`、`references/`、根 `.md` 全部 UTF-8 无 BOM；`.fbs` 输出无 BOM、`newline="\n"`。
- **异常拒绝**：照片 `--force-class illustration` 仍被 `E_PHOTO_DEFERRED` 拒绝（不可绕过）；损坏图/透明图/过小图均明确报错并给建议；纯黑/纯白在图 R=512 与 R=1024 均为 exit 0 + `W_SOLID_COLOR`（旧报告中"T8a R=1024 写盘失败"在冻结代码下不再出现于 CLI；验收台内仍会触发，见 M2）。
- **性能**（对照 CONTRACT §12）：chibi R1024 build 全流程 **34.8 s**（Stage0–1 16.2 s、渲染 7.6 s）、onestroke 65536 **10.6 s**、T1 60 s 视频 **49.6–55.9 s**、acceptance --quick **32.6 s**、全量 **63.4 s** —— 均显著低于预算。

---

## 9. 交付物完整性

- **目录自包含（复制安装）**：冻结态复测——将 `SKILL.md/README.md/CONTRACT.md/references/scripts/tests/assets/testcases`（**83 文件 / 1.4 MB**，排除 `assets/out` 与锁死临时目录）复制到新目录后，`build`→`onestroke`→`unittest` 227 项全部通过（39.105 dB 与原地一致；onestroke N=4094 正常）。**证明无绝对路径/无 G: 盘依赖、可移植**。
  - 绝对路径仅出现在两处允许位置：`run_acceptance.py` 的两张真实图可选路径（文件不存在则跳过，符合"验收脚本可选分支"）与 `util.py` 的 Windows 字体回退候选。
- **BOM/编码**：全部源文件 UTF-8 无 BOM；代码/文档中文正常。
- **打包清理（未完成）**：
  - 残留 3 个不可复制的 ACL 锁死临时目录：`fbs_test_tmp_8g9i4b72/`、`tmplbjqgeh_/`、`tests/_tmp_probe/`（`icacls` 显示 `Everyone:(DENY)(DC)` 等，普通 `Copy-Item -Recurse` 会报"访问被拒绝"；需管理员/取得所有权后删除）。
  - 3 个 `__pycache__/` 共 50 个文件 1.95 MB。
  - `assets/out/` 下有 **28 个调试/补丁脚本**（`dbg_*.py`、`patch_*.py`、`render_selftest*` 等）。
  - skill 目录当前 **131.1 MB / 509 文件**（清理后；`assets/out` 占绝大部分），复制安装会连同演示产物与验收证据一起拷贝。
  - README"方式 2：分发包"引用的 `工作区根目录 fourier-brushstroke-v2.zip` **不存在**（`Test-Path` = False）。
- **真实测试图**：chibi 源图 `<path>` 存在；horn 源图 `<path>` **当前不可见**（`<path>` 盘在、文件 Test-Path=False）。因此 horn 的质量数字只能引用演示产物报告（UNVERIFIED-1）。

---

## 10. 问题清单

### BLOCKER

无（核心链路可跑、`.fbs` 读写往返正常、无关键需求缺失；快速开始命令在冻结后已能原样执行）。

### MAJOR

- **M1. 验收结论语义的两种判读**：`SKILL.md §验收`/`README.md` 写"通过标准：**MUST 全过**；SHOULD/MAY 记录实测值"，而 `run_acceptance` 以 `ok = failed==0 && blocked==0` 判定，导致 MUST 27/27 全过的当前交付在 `report.md` 里仍写"**结论：❌ 失败**"。终版文档（04:34）已用"终版结果/MUST 27/27/ok=false 仅因 SHOULD/MAY 失败"做了缓解说明，但**报告本身的结论措辞仍会让只看 report.md 的用户误判为"整体不通过"**。建议：`report.md/report.json` 的结论区分 MUST/SHOULD/MAY（或把 SHOULD/MAY FAIL 单列为"已知不达标"），与 SKILL 措辞统一。**当前 MUST 27/27 全过 → 按 SKILL 的通过标准，本交付应判"通过（带 2 项已知非 MUST 不达标）"**。
- **M2. 验收台写盘 bug 污染 B2 证据**：`run_acceptance.py::Ctx._build_path` 第 317 行 `doc["warnings"] = warn.to_list()` 覆盖了 `document.build_from_image` 已写入 doc 的 `W_SOLID_COLOR`，导致纯色图在该路径下 `write_fbs` 校验失败（`E_FBS_INVALID: strokes 为空且没有 W_SOLID_COLOR 警告`）。本人在验收上下文中**必现复现**（T8a R512）；生产 CLI 同配置 exit 0 正常。后果：① 最终报告 `notes` 带"T8a_solid_black R=512 写盘失败"；② B2 把该写盘 bug 当成"明确拒绝 E_FBS_INVALID"而 PASS，降级样例证据不成立（应用 T6 梯度样例）。建议修 merge warnings + B2 换样例后重跑。
- **M3. A8/A15/T13 属"判据修正后通过"，不是按设计原文达标**：A8 设计原文要求"stamp ≤ 线段的 50%"，实测比值 **0.6219**（未达），冻结前改为"≤0.5ε 且 ≤ line_band"后 PASS（A8-legacy=BROKEN 记录）；T13 恢复曲线的 `interp` 仍为 **linear**（非 step），靠"方向取优 + 数值容差"通过（`interp_step_preferred=false`）。两者都有实测依据、 Lead 裁定且文档记录，但**交付时需向用户明示"这些是判据修正/已知表示缺口"**，不能表述为"完全达标"。
  - 附带说明（应 Lead 要求在报告中如实记录）：**T13 的 t 方向歧义是 MAT（中轴变换）的固有属性**——中轴分支方向任意，几何与宽度曲线同向反转、渲染自洽（T13 渲染 PSNR 33.62 dB 正常）。判据改为两方向取优并记录 orientation，是测试判据修正，不是表示层 bug；但"step 显式表达"在数据层未实现。
- **M4. 两个非 MUST 验收项失败（如实记录）**：`A10`（SHOULD）T4 单笔画被切成 2 段中轴（ratio 2.0 > 1.5，根因端帽臂未连接）；`A11`（MAY）T3@R512 `.fbs`/PNG = **37.35×**（主因 color_field + JSON indent，设计已放弃体积目标）。不阻塞 MUST，但报告已把 `ok=false`。
- **M5. horn 真实图当前不可见 + 真实图验收方法学**：R10 要求的 horn 质量数字当前无法独立复算（源图不在）；官方报告的 R8/real_images 采用"回读 Lead 演示产物"而非验收时重新 build（报告已注明 `source=demo_artifacts`）。本报告已独立验证 demo 产物有效性（4 个 .fbs 可读、2 个视频 1800 帧/60 s、chibi 可重渲染复算），但"完全可复现"仍缺 horn 源图这一环境条件。

### MINOR

- **m1. 打包清理未做**：3 个 ACL 锁死临时目录（普通复制报错）+ 3 个 `__pycache__`（1.95 MB）+ 28 个 `assets/out` 调试脚本；README 方式 2 引用的 `fourier-brushstroke-v2.zip` 不存在。
- **m2. 文档残留数字/口径不一致**：`capability_boundary.md §3` 分类阈值与 `preprocess.py` 不符（lineart hard_edge 0.02 vs 0.008；flat_art 0.05 vs 0.08）；`run_acceptance.py` docstring "38 项"实为 40；CONTRACT §9 称码表"实现级全集"但 `W_COLOR_FIELD_OFF`/`W_ONE_STROKE_TRAVEL` 从不触发（capability 已标"契约保留码"）。
- **m3. 属性曲线语义缺口**：`spline` 曲线 `clamp=False` 时不做端点外推（契约 §2 为"否则外推"）；`--color-field-grid 0` 关闭颜色场时不产生 `W_COLOR_FIELD_OFF` 告警（区域退化单色静默发生）。
- **m4. 演示相关记录项**：T1 一笔画视频 `visible_circles=20`（简单图，远低于 `--circles 600` 名义值，非验收项）；A11 的 fbs 体积随 R 漂移（报告已要求同时报 R 与绝对值）。

---

## 11. 未验证项（UNVERIFIED）

| 编号 | 项 | 无法验证的原因 | 已做的最佳替代 |
|---|---|---|---|
| UNVERIFIED-1 | R10 horn 的 PSNR/SSIM 独立复算 | 源图 `<path>` 当前不可见（`Test-Path=False`） | 验证 horn demo `.fbs` 可读/合法、视频 1800 帧/60 s/720×1080、报告数字与该产物自洽 |
| UNVERIFIED-2 | A4 "物理断网跑通" | 未在真正断网环境执行 | 静态 AST 扫描 0 网络 import + 人工审查全链路无网络调用 |
| UNVERIFIED-3 | B5 "SVG 可在 Illustrator / Figma 正常打开" | 无 GUI/该软件环境 | SVG 为合法 XML、根元素/尺寸/viewBox 正确、`E_SVG_INVALID` 自检存在 |
| （低） | 真正 "2R 渲染振铃" 的可视化确认 | 属语义说明，非验收项 | 代码/文档一致，未解码逐像素核验 |
| （低） | 视频逐帧画面对齐 | 未解码全部 1800 帧逐帧比对 | `step_spans` 连续覆盖 [0,1]、frames≥steps、timeline=arc 的 |arc−t|max=1e-05、probe 元数据一致 |

---

## 12. 复现命令（冻结态，可直接照跑）

```bash
PY="<python>"
cd fourier-brushstroke/scripts

# 1) 快速开始全链（T1 合成图 @R=512）
"$PY" fbs.py build ../assets/testcases/T1_signature.png -o ../assets/out/verify_final2/t1 --resolution 512 --mode sweep
"$PY" fbs.py onestroke --fbs ../assets/out/verify_final2/t1/T1_signature.fbs.json
"$PY" fbs.py video --fbs ../assets/out/verify_final2/t1/T1_signature.onestroke.fbs.json --out ../assets/out/verify_final2/t1/T1_6s.mp4 --duration 6 --fps 10 --size 360x480
"$PY" fbs.py render --fbs ../assets/out/verify_final2/t1/T1_signature.fbs.json --out ../assets/out/verify_final2/t1/render_R256.png --resolution 256
"$PY" fbs.py eval --fbs ../assets/out/verify_final2/t1/T1_signature.fbs.json --reference ../assets/testcases/T1_signature.png

# 2) 本人独立 quick 验收（报告目录不影响官方证据）
"$PY" fbs.py acceptance --quick --report-dir ../assets/out/verify_acceptance

# 3) 定向复跑（A8/A15/T13/A10/A11）
"$PY" fbs.py acceptance --items A8,A15,T13,A10,A11 --report-dir ../assets/out/verify_final2/items

# 4) 工程门禁
"$PY" -m unittest discover -s tests          # 227 tests OK
"$PY" scripts/check_hygiene.py --strict      # exit 0, 0 hits

# 5) 官方验收（全量，约 65 s；由 metrics 负责重跑）
"$PY" fbs.py acceptance
```

**本人留存的复现证据**（均在 `assets/out/verify_*`）：
- `assets/out/verify_acceptance/report.json` —— 冻结代码独立 quick 复跑（digest 与官方一致）
- `assets/out/verify_final2/items/report.json` —— A8/A15/T13/A10/A11 定向复跑
- `assets/out/verify_final2/hygiene.txt`、`verify_final2/t1/`、`verify_final2/demo/` —— 快速开始与卫生检查产物
- `assets/out/verify_final/chibi1024/` —— chibi R1024 build + onestroke + 渲染/度量独立复算
- `assets/out/verify_final2/` —— T1/T2 快速开始复跑产物

---

## 13. 审计方法与限制

- 本文全部结论来自 fresh 上下文独立执行：需求仅取自 `docs/交接说明_2026-10-18.md §2` 与 `设计文档 v2.1 §7`，未继承任何 Lead/队友的"已完成"叙述；文档数字一律以代码/实跑复核。
- 审计期间（04:09–04:31）代码与验收判据持续变动，故中途结果一律标注快照；最终结论只采用 04:31 之后的冻结快照（digest `e823254a4683e2df`，重算一致）。
- 限制：无 GUI 环境（B5）；无物理断网（A4）；horn 源图不可见（R10 部分复算）；未解码全部视频逐帧比对（只做元数据+时间轴连续性）；未在 Illustrator/Figma 打开 SVG。
- 三态口径：**PASS** = 本人复跑/复算达到声称标准；**FAIL** = 复跑/复算明确未达阈值；**UNVERIFIED** = 当次无法复现或无法验证，**不写作 PASS**。

---

## §附：最终版增量复核（2026-10-18 04:38，Lead 打包前增量）

- 终版快照 digest 更新为 **`92bd87a48b79c2a3`**（`assets/out/acceptance/report.json` generated **2026-10-18T04:37:42**，全量 40 项：**36 PASS / 2 FAIL（A10,A11）/ 1 BROKEN / 1 NOT_IMPLEMENTED，MUST 27/27**）。本人重算 18 文件聚合 digest 与磁盘当前文件一致。
- **M1 已修复并复核**：`report.md` 结论改为"✅ 项目级通过（MUST 27/27 全过；非 MUST 失败 2 项）"，与 SKILL"通过标准：MUST 全过"不再矛盾。
- **M2 已修复并复核**：`run_acceptance.py::_build_path` 现已合并 warnings（不再覆盖）；终版报告 `notes` 无 T8a 写盘失败；B2 降级样例改为真实证据——`T8a_solid_black → codes=["W_SOLID_COLOR"]`、`distinguishable=true`。
- 原审计结论不变：**PASS 38 / FAIL 2（A10 SHOULD、A11 MAY）/ UNVERIFIED 1（B5）；BLOCKER 无**；M3（A8/T13"判据修正后通过"、非设计原文达标）、M4（A10/A11 非 MUST 失败）、M5（horn 原图不可见 → R10 子项 UNVERIFIED）均维持。正文 §0–§13 保留审计当时快照时点/数字不改（审计诚实性）。
- 残留 minor：`run_acceptance.py` 顶部 docstring 仍写"共 **38** 项"（实测 40 项，该文件 04:36:33 未同步）；其余 minor 见 §10 不变。
