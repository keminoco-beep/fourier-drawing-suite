# VERDICT v1.36 — 人类画师式画法（非扫描）（task-26 / verifier）

**结论（先看 6 行）**

1. **总评：CONDITIONAL（条件性通过）**。用户核心诉求“**不要扫描式画法**”已达成（画法子项 **PASS**）：
   wash 铺底不再水平行扫、正式笔顺形体、绘制顺序按区域成组、逐层 y-Scan 消失；legacy 逐字节兼容、测试与包均通过。
2. **质量子项 CONDITIONAL**：按冻结基线 `runE_dabs`（doc content **24.6184**），v1.36 final **24.1320** →
   **Δ = −0.486 dB，超出 ≤0.4 允许线 0.086 dB**；A 区 PSNR 24.625→24.115（−0.51）、A `largest_flat` 0.222→**0.416**（回退）；
   全幅 PSNR 25.234（白边已修 0.966→0.998，不可与 v1.35 方图直接横比）。差异在工程可接受范围，如实记录、不判 FAIL。
3. **画法证据（自复跑）**：toned/wash 底 71 笔、**1 种纯色 std=0、无 gradient**、axis_aligned **0.4%**（v1.35 行扫 100%）、
   y_spearman **−0.05**（v1.35 +1.0）；flow 笔与结构张量切向对齐 **92.7%**（report 0.9086）、axis_aligned **0.251**（legacy 0.314）；
   region 顺序 y_spearman **0.197**（≤0.3）、**逐层 y_spearman −0.04~0.31 vs v1.35 的 1.0**、相邻笔距 190.7px < scan 252.0px、
   region-run 0.602 > scan 0.436；进度帧确认“区域成组推进、非逐行出现”。
4. **测试**：我复跑 **104 tests OK (skipped=3) / 76.8 s / rc=0**（3 个 skip 全为 RUN_SLOW 慢测试）；
   `RUN_SLOW=1` **3/3 OK / 12.3 s**。
5. **包（task-27 重打包后复核）**：`dist/…v1.36-human-paint-order.zip` **51 文件 / 7,954,082 B /
   SHA256 `3B2ADF87…7994F5A78`**（与 README 一致）；51/51 逐文件 sha==源树、无绝对路径（49 文件 0 命中）、死链 0、
   无 training/__pycache__、旧包未动。文档已按冻结基线更正（`24.6184→24.1320（−0.486，超线 0.086）`），
   无“完全像人类/质量无损”类宣称。
6. **发布建议**：可作为 v1.36 发布（画法升级 + 白边修复），发布说明必须写：质量相对冻结基线 −0.486 dB（超 0.4 线 0.086）、
   A 区 largest_flat 回退、运行 178.6 s 未达 120 s 目标、区域推进仍有“前沿”（compactness 0.201 < 0.25）。

> 方法：所有数字由 verifier 自己复跑（`work/verify5/`：`order_*`、`quality_*`、`banding_v136.json`、
> `logs/`、`visual/`、`pkg_v136/`），未采信 fixer/analyst 自述。

---

## 1. 质量：两口径对照与 Δ 计算（核心争议点）

| 对象 | square 口径（Sunflowers ref，x[186,1350)） | doc 口径（8iNfq→1165×1536，x[185,1350)） |
|---|---|---|
| v1.35 `runE`（**仅正式层 85,683 笔，无 thick_dabs**） | 24.5367 | 24.4675 |
| **v1.35 `runE_dabs`（冻结基线，99,683 笔）** | **24.6875** | **24.6184** |
| **v1.36 final（100,000 笔）** | **24.1985** | **24.1320** |
| Δ（final − 冻结基线） | **−0.4890** | **−0.4864** |

- **结论**：按 Lead 冻结口径（doc 24.6184），**Δ = −0.4864 dB，超 ≤0.4 允许线 0.0864 dB** → **质量 CONDITIONAL**。
- **口径错误点名**：fixer `v136_metrics.json`/初版文档用 `runE`（85,683 笔、无 dabs）的 24.5367 作基线，得 Δ−0.338
  “在 ≤0.4 内”；而 task 冻结的 v1.35 fresh 是 `runE_dabs`（99,683 笔）24.6184。**该基线错误已在 task-27 更正**：
  重打包后的 SKILL.md/references/README/训练笔记均写 “24.6184→24.1320（−0.486 dB），超 ≤0.4 允许线 0.086 dB → 质量 CONDITIONAL”。
- 其他质量项（doc 口径）：full **25.2338**（v1.35 21.8451；白边 mean 0.9665→**0.9984**、white_frac 0.8973→0.9946，
  白边过笔已修，故方图口径不可与 v1.35 直接横比）；A **24.1154** vs 24.6254（−0.51）；B **27.6288** vs 28.1486（−0.52）；
  A blocks **64**（baseline 64）、A `largest_flat` **0.4161** vs 0.2215（**回退**）、A run_y 7.20 vs 6.62。
- 笔数 **100,000 ≤ 100k**（texture 请求 20,000 被预算截为 17,464，`texture_flat_params.capped_100k=true`；
  顶层 `capped_100k=false`、`cap_adjustments=None`、`max_curves=100000`）。`fill=0`：本轮 `.fbs` 未生成，
  layered_base 输出为纯 stroke JSON（结构上无 fill），未能用容器校验器独立确认。

## 2. 画法：PASS 逐条证据（自复算）

| 验收项 | v1.35 基线（自复跑） | **v1.36 final（自复跑）** | 判定 |
|---|---|---|---|
| 底色 pattern | rows（水平行扫） | **wash**（-22° 带状洗刷） | ✅ |
| 底色方向轴对齐率 | **100%**（77 条全水平） | **0.44%**（71 条 wash） | ✅ |
| 底色 y-Spearman | **+1.000**（自上而下） | **−0.046** | ✅ |
| 底色纯色/契约 | 1 色 std 0 / 无 gradient（v1.35 已是） | **1 色 std=0、gradient=0、coverage 1.0**（overlap 0.0713 analytic，≤15%） | ✅ 不回归 |
| flow 对齐（≤20°） | 0.839（旧方向） | **0.9273**（我的口径）/ report **0.9086** | ✅ 高 |
| axis_aligned（≤10°） | legacy 路径 **0.3138** | **0.2510**（report 0.2663） | ✅ 下降（−20%） |
| paint_order | legacy/scan（逐层 y 排序） | **region** | ✅ |
| 全局 y-Spearman（顺序） | 0.1296（层优先混合假象） | **0.1970**（=report；≤0.3） | ✅ |
| **逐层 y-Spearman** | layer1/2/3 = **1.000**（整轮扫描） | **−0.043 / 0.223 / 0.222 / 0.071 / 0.309** | ✅ 扫描消失 |
| 相邻笔中心距中位数 | 9.03px（行内连续） | **190.7px** vs scan-sim **252.0px** | ✅ 比 scan 小（区域切换跳距，如实） |
| region run（K=12 同簇连续比例） | 0.8105（v1.33）/ 0.6595（v1.35） | **0.6019** vs scan-sim **0.4364** | ✅ 高于 scan；未达 analyst 0.75 目标 |
| 区域层序单调 | 1.0（层优先假象） | 0.9996（块内 layer 有序） | ✅ |
| 扫描层数（我口径：逐层 y_spearman≥0.8） | **3**（layer1/2/3） | **0** | ✅ |

- **进度帧（我自己渲染，8 帧序列）**：`work/verify5/visual/contact_sheet_v136_final.png` 对比
  `contact_sheet_v135_fresh_runE_dabs.png`。v1.35 呈现“整幅逐步加细节”的层优先推进（前 14% 已见全图轮廓，
  细节自上而下补）；v1.36 前 14%~42% 只出现**分离的区域组**（左上/右上花头、左下叶、底部地面），
  随后各区域交替补细节，**不是逐行出现**。fixer contact sheet（`work/humanpaint/v136_paint_order_contact_sheet.png`）
  一致。**残余**：v1.36 仍存在多区域“并行前沿”（不是严格“一块画完再下一块”，compactness 0.201 < 0.25），
  且前段由 wash 先铺全画布淡黄底调（人画铺底行为，但铺底本身覆盖全画布）。
- 视频：本轮未出（Lead 正在做），未验收。

## 3. 兼容 / 契约（1536 全尺寸自复跑）

| 项 | 结果 |
|---|---|
| v1.33 legacy（显式 `--base-color-mode quantized … --base-coat-pattern rows --paint-order legacy --gradient-gate off`） | PNG SHA `30CDCE74F11A8E2C0…EB9A01`、strokes SHA `110C9635…`、81,815 笔 / 23.1883 / 0.61788 —— **与 doc 逐字节一致** ✅ |
| v1.34 legacy（显式 source + texture 37000 + rows/legacy） | PNG SHA `69820EEF69386B8D…C265C3`、strokes `3E7CB032…`、99,929 笔 / 24.3628 / 0.62545 —— **与历史产物逐字节一致** ✅ |
| 默认契约 | `--base-coat-pattern wash`（choices wash/rows）、`--wash-angle -22`、`--flow-strokes on`、`--paint-order region`（choices region/scan/legacy）、`--region-count 12`、`--thick-dabs 0`、`--max-curves 100000`、`--base-color-mode toned`、err-pct 60/45 —— AST 实测与文档一致 ✅ |
| legacy 自动回退 | 显式 legacy 底色模式且未传 flow/order → flow=off、paint_order=legacy、pattern=rows（report 实测 `paint_order_effective=legacy`、`flow_strokes=False`）✅ |
| v1.35 原则不回归 | 底色 1 色 std=0、无 gradient、coverage 1.0 ✅；`white_px_in_fg=0` ✅ |

## 4. 测试 / 包 / 文档（最终状态）

- **测试（task-27 文档更正后我再次复跑）**：`python -m unittest discover -s tests -v` →
  **104 tests OK (skipped=3) / 76.8 s / rc=0**（3 个 skip 全为 `RUN_SLOW`）；`RUN_SLOW=1` → **3/3 OK / 12.3 s**。
  新增 `tests/test_human_paint.py`（18 个 test 方法）覆盖 wash/flow/region/legacy 契约。日志：`work/verify5/logs/tests_*.log`。
- **包（task-27 重打包后复核）**：`dist/fourier-multistroke-training-skill-v1.36-human-paint-order.zip`
  = **51 文件 / 7,954,082 B / SHA256 `3B2ADF87D80263E413DD1DFDD53EF4A0BA0D65A02DBD4DD38B6A1427994F5A78`**；
  `dist/README_v1.36.md` 声明一致；`testzip=None`；平铺 `SKILL.md` + scripts(31)/references(7)/assets(2)/tests(10)；
  51/51 逐文件 sha256 == 源树；无 training/dist/.dsh/__pycache__；旧 v1.32–v1.35 zip 未动。
- **可移植性**：49 个文本文件扫描 `<path>`、`<path>`、`<path>`、`<path>` → **0 命中**；markdown 相对链接 **0 失效**。
- **文档**：SKILL.md 新增「人类画师式画法（v1.36）」节（原则/3 件事/参数默认/report 字段/推荐命令/已知限制/实测表）；
  质量数字已按冻结基线更正（24.6184→24.1320，−0.486，超线 0.086 → 质量 CONDITIONAL）；square 24.6875→24.1985 仅作对照；
  运行时间 178.6s（v1.35 runE_dabs 148.0s）未达 ≤120s 如实写；`flow_align` 双口径（0.9086 fixer / 0.819 analyst）如实并列；
  analytic 覆盖口径与 wash 重叠说明在列；**无“完全像人类/质量无损/扫描彻底消除”类过度宣称**（全文扫描确认）。

## 5. 独立发现的问题（按严重度）

| # | 级别 | 问题 | 状态/证据 |
|---|---|---|---|
| 1 | **P1（已由 task-27 修复）** | **质量基线口径错误**：初版文档用 `runE`（85,683 笔、无 dabs）24.5367 作基线得 Δ−0.338“达标”，而冻结基线是 `runE_dabs` 24.6184 → 真实 Δ−0.486（超线 0.086）。该错误曾出现在 SKILL.md/references/README/训练笔记 | 我复算两口径（§1）；Lead 裁决开 task-27；重打包后文档已改（`0.338` 已从 SKILL.md 消失，`24.6184→24.1320（−0.486，超线 0.086）` 已写） |
| 2 | P2（已文档化） | 质量未达 ≤0.4 线（超 0.086 dB）；A 区 PSNR −0.51、`largest_flat` 0.222→0.416 回退、A run_y 6.62→7.20 | 本报告 §1；最终文档已如实写 |
| 3 | P2（已文档化） | 运行时间 **178.6s**（v1.35 runE_dabs 148.0s），未达 ≤120s 理想线（5 次全场渲染） | report.seconds；文档已写 |
| 4 | P3（已文档化） | 区域推进仍有“并行前沿”：compactness 0.201 < 0.25，非严格“一块画完再下一块”；wash 铺底本身覆盖全画布 | 进度帧；analyst ANALYSIS；文档已写 |
| 5 | P3 | `flow_orientation_frac` 口径差异：fixer 0.9086 / analyst 0.819 / 我 0.9273（角度/切向/加权定义不同）——不取单一口径宣称达标，文档已并列 | 本报告 §2；docs 已写 |
| 6 | P3 | `>768px` 的底色 coverage/overlap 为 **analytic 几何口径**（`base_metrics_mode=analytic`），真实栅格断言只在 ≤384px；1536 的 overlap 0.0713 是 analytic 值 | report；文档已写 |
| 7 | P3 | `fill=0` 未独立验证（本轮 `.fbs` 未生成，纯 stroke JSON 无 fill）；100k 由 texture 预算截断实现（17,464/20,000），顶层 `capped_100k=false` | report；发布前若生成 `.fbs` 建议补 verify_delivery |
| 8 | P3 | A 区 banding 回退（largest_flat 0.222→0.416）+ A PSNR −0.51：与 v1.36 的 wash/flow/顺序改动相关，下一轮质量优化项 | banding_v136.json |

## 6. 裁决理由

- **画法子项 = PASS**：用户核心诉求“不要从上到下扫描式地画了好几轮”在**量化指标 + 进度帧**两个层面均达成：
  底色 100% 轴对齐水平（y_spearman +1.0）→ 0.4%/y_spearman −0.05 的 wash；逐层整轮 y-Scan（1.0）→ −0.04~0.31（扫描层 3→0）；
  区域顺序 global y_spearman 0.197、相邻笔距 < scan、区域 run > scan；flow 顺形体 92.7%/axis 0.251 vs legacy 0.314；白边污染修复。
- **质量子项 = CONDITIONAL**：相对冻结基线 doc −0.486 dB（超 ≤0.4 线 0.086）、A 区 largest_flat 回退、A PSNR −0.51；
  但全幅/白边显著改善、画质差异处于工程可接受范围，且文档已如实记录。
- **整体 = CONDITIONAL（不判 FAIL、不判纯 PASS）**：核心需求已达成、可交付；质量与性能超线/回退项需下一轮优化并在发布说明如实写出。
- **发布建议**：可以发布 v1.36（画法升级 + 白边修复 + legacy 兼容），发布说明写明 §5-2/3/4 各项；建议下一轮：
  ① 质量回补（A 区/内容 PSNR、largest_flat）；② 性能优化到 ≤120s；③ 区域顺序进一步“逐区完成”（降低并行前沿）；
  ④ 生成 `.fbs` 并跑 verify_delivery（fill=0 硬校验）。

## 7. 证据索引

| 内容 | 路径 |
|---|---|
| 顺序/方向/扫描指标（final、v1.35 基线、legacy） | `work/verify5/order_v136_final.json`、`order_v135_fresh.json`、`order_legacy_v133.json` |
| 质量（final / 基线，两口径） | `work/verify5/quality_v136_final.json`、`quality_v135_baseline.json`、`evidence_summary.json` |
| A/B banding | `work/verify5/banding_v136.json`（含 v1.35 基线行） |
| 进度帧/接触表/热图 | `work/verify5/visual/contact_sheet_v136_final.png`、`contact_sheet_v135_fresh_runE_dabs.png`、`visual/frame_*.png`、`heatmap_first_last25_*.png` |
| 测试日志（104 tests / slow） | `work/verify5/logs/tests_full_final.log`、`tests_slow.log` |
| 包校验（task-27 新 SHA） | `work/verify5/package_validation_v136.json`、`work/verify5/pkg_v136/` |
| legacy 1536 复现产物 | `work/verify5/runs/legacy_v133/`、`runs/legacy_v134/` |
| 复算脚本 | `work/verify5/order_metrics.py`、`quality_metrics.py`、`validate_pkg_v136.py`、`make_evidence.py` |
