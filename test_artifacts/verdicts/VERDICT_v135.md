# VERDICT v1.35 — 人类画师式底色（task-21 / verifier）

**结论（先看 6 行）**

1. **总评：PASS**。v1.35 的核心承诺全部经独立复跑成立：底色**纯色 / 单次薄涂 / 非草图**（toned）、
   **无底色**（none）、**正式绘制层承担画面**、**legacy 逐字节兼容**、默认/契约、84 测试、文档与包均通过。
   文档对质量代价（全幅 PSNR 下降、A 区 largest flat 回退、白边过笔）**如实写明、无过度宣称**。
2. **底色合规（PASS）**：toned 1536 Sunflowers：base 笔 **64（纯默认）/77（推荐 recipe）条**、**颜色唯一且 std=0**、
   **0 条 gradient**、alpha=1/layer=0/kind=base_toned、**覆盖 1.0、重叠 fg 4.10%（默认）/0.0%（recipe）≤5%、
   count>2=0**；base-only 前景色 std 0.0102（抗锯齿边缘主导，内部 0，测试断言 ≤1e-4 通过）、base 边缘能量仅源图 1.6%；
   旧 v1.33/v1.34 底为 4,543/12,544 色、std 0.142/0.156、平均叠涂 ~2.97-2.98 层 → 旧底“多色草图”实锤，新底合规。
3. **正式层承担（PASS）**：base-only→final PSNR **+7.12 dB**（v1.33 legacy 仅 +3.87）；边缘能量 **96.8% 来自正式层**
   （legacy 71.9%）；按面积 base 仅 77 条、正式层占 99.92%。
4. **线稿（PASS，独立复算与声明逐位一致）**：`none` 无底色 96,636 笔；IoU **0.8492**（旧 0.7369）、
   recall/precision 0.9755/0.8677（旧 0.9645/0.7574）、PSNR **18.276**（旧 14.437）、SSIM **0.776**（旧 0.6378）；
   参考白区"灰草图"比例（我的口径：白区像素中渲染 luma<0.80）**14.89%→6.29%**（声明 15.56%→7.19%，同方向同量级）；
   旧 source 底 4,115 笔 / 3,987 色 / 色 std 0.297 → “灰色草图底”确认。
5. **Sunflowers 质量代价（已如实写入文档，可接受）**：toned+texture 99,905 笔；content（square 口径）**24.5669/0.51407**
   （= 声明，略高于 v1.33 24.5202）、A PSNR **24.321**、A blocks **103→44**、A largest **0.447→0.276**（仍差于 v1.33 0.062）、
   全幅 **21.8122**，文档明确写“**不能与 v1.33 的 23.0791 直接横比**——主因正式层笔触越界污染白边”。我独立复算验证该解释：
   白边区 PSNR 20.60→17.93（污染确实存在），**排除左右白边后全幅 PSNR 24.44→24.49（反而略升）** → 结论成立。
6. **包/文档（PASS）**：v1.35 zip **50 文件 / 7,925,101 B / SHA256 `D148993F…FD59C31D`**（与 README 一致），
   逐文件 sha==源树、绝对路径 0、死链 0、无 training/__pycache__、旧包未动；SKILL.md 有 v1.35 原则节/新参数/默认值/
   指标/legacy 标注/84 测试，**无“质量无损/完全人类化/已解决”类过度宣称**，白边/A 区回退等残余如实列出。

> 方法：全部数字由 verifier 自己复跑（`work/verify4/runs/`、`*.json`、`visual/`），未采信 fixer/analyst 产物。

---

## 1. 契约 / 默认值 / report（独立核对）

| 项 | 声明 | 实测（`--help`/AST/report） | 结果 |
|---|---|---|---|
| base-color-mode 默认/choices | toned；toned/none/single/quantized/source | 默认 **toned**，choices 全 5 项 | ✅ |
| `--base-color` | auto \| #RRGGBB \| r,g,b | 默认 auto；#hex/r,g,b 解析测试通过（84 tests） | ✅ |
| base-coat 参数 | angle 0 / spacing 0(=coarse) / width-factor 1.04 / jitter 0.5 | 实测 report：angle 0、spacing 24=coarse、width 24.96=1.04×24、jitter 0.5 | ✅ |
| err-pct 默认 | 60/45/45 | report 与测试契约一致 | ✅ |
| `--max-curves` | 100000 硬上限 | report.max_curves=100000 | ✅ |
| report 新字段 | base_color_mode/base_color/base_coat_*/base_coverage/base_overlap_frac/base_overlap_canvas_frac/base_color_std/base_strokes/base_max_count/base_overlap_px_over2/gradient_strokes/max_curves/capped_100k/cap_adjustments/pre_cap_strokes | 全部存在（default1536/toned_tex 实测） | ✅ |
| `none` | 0 底笔、相关字段 None、std 0 | 测试 84 全绿覆盖；base_metrics 见线稿 none=0 笔 | ✅ |

## 2. 底座合规（硬指标，独立栅格复算）

| run | base 笔 / 色数 / std / gradient | 覆盖 fg / 重叠 fg / >2 层 / maxcnt | base-only fg std / 边缘比 | 判定 |
|---|---|---|---|---|
| **v1.35 toned 纯默认**（37,843 笔） | 64 / **1** / **0** / 0 | **1.0 / 4.10% / 0 / 2** | **0.0102** / **1.6%** | **PASS** |
| **v1.35 toned+texture recipe**（99,905 笔） | 77 / **1** / **0** / 0 | **1.0 / 0.0% / 0 / 1** | 0.0103 / 1.7% | **PASS** |
| 旧 v1.33 legacy（81,815 笔） | 4,543 / 4,543 / 0.142 / 0 | 解析平均 **2.97 层** | 0.140 / 13.9% | FAIL（基线） |
| 旧 v1.34 legacy（99,929 笔） | 12,544 / 12,544 / 0.156 / 0 | 解析平均 **2.98 层** | 0.154 / 20.1% | FAIL（基线） |

- 合成栅格网格（192×144 res 96/128/192/256 × angle 0/90 + 384×288 res 128/192，共 12 组）：
  **coverage 全部 1.0000、count>2=0、max_count=2、report 重叠 2.78–3.91%**；我的逐笔 alpha 累加口径 10/12 组
  3.1–3.9%，2/12 组（angle90/res128）为 7.81%——因抗锯齿缝隙像素叠加所致（>2 层=0，且 1536 实际图与 report 完全一致 4.10%），
  判为口径差异而非合规缺陷（见 §6-问题 3）。
- 数据：`work/verify4/base_compliance.json`、`work/verify4/runs/default1536/layered_base_report.json`。

## 3. 正式绘制承担 + 100k 契约（独立复跑）

| 指标 | v1.33 legacy | **v1.35 toned+texture** | 声明对照 |
|---|---|---|---|
| base-only PSNR（vs 源图） | 19.29 | **14.69**（纯色场，信息极少） | — |
| final PSNR（vs 源图） | 23.16 | **21.81** | ✅ |
| **base→final PSNR 增益** | +3.87 | **+7.12** | 声明 +7.10 ✅ |
| **正式层边缘能量占比** | 71.9% | **96.8%** | 声明 96.6% ✅ |
| base 边缘能量 / 源图 | 0.37% | **0.045%** | — |
| 纯默认调用 | — | **37,843 笔，capped=False**，pre_cap=37,843 | ✅ 一致 |
| 默认 + `--pass4` | — | pre **109,548 → 100,000**，capped=True，`cap_adjustments={"base_pass4":{"removed":9548,"frac":0.1332}}`，pass2 6,116/pass3 31,663/pass4 62,157 全保留 | ✅ 逐位一致 |

## 4. 线稿 A/B（独立复跑 + 自裁三联图）

| 指标（2400 口径） | v1.34 source 底 | **v1.35 none** | 声明 |
|---|---|---|---|
| 笔数 / base 笔 | 22,888 / 4,115（3,987 色，std 0.297） | **96,636 / 0** | ✅ |
| IoU / recall / precision | 0.7369 / 0.9645 / 0.7574 | **0.8492 / 0.9755 / 0.8677** | ✅ 逐位一致 |
| d1 recall / precision | 0.9670 / 0.7910 | **0.9802 / 0.9089** | ✅ 一致 |
| PSNR / SSIM | 14.437 / 0.6378 | **18.276 / 0.7760** | ✅ 逐位一致 |
| 白区灰草图（我的口径） | **14.89%** | **6.29%** | 声明 15.56%→7.19%，同方向 |
| 旧底 base-only fg std | 0.2945 | 0（无底） | 声明 0.2936 ✅ |

图：`work/verify4/visual/lineart_triple.png`（reference / v1.34 源色底 / v1.35 无底）、
`lineart_old_base_R2400.png`（旧多色灰草图底单独渲染）、`lineart_none_R2400.png`、`lineart_old_R2400.png`。
数据：`work/verify4/lineart_metrics.json`。

## 5. Sunflowers 质量与 banding（自复跑，含代价验证）

| 指标 | v1.33（gate off canonical，我复跑） | **v1.35 toned+texture** | 声明 |
|---|---|---|---|
| content PSNR/SSIM（square 口径） | 24.5202 / 0.51475 | **24.5669 / 0.51407** | ✅ 逐位一致（24.5669/0.51407），**不降略升** |
| content（doc 口径 8iNfq） | 24.5058 / 0.53887 | 24.5441 / 0.53684 | 略升 |
| A 区 PSNR / MAE | 24.1378 / 0.04308 | **24.321 / 0.04056** | ✅ 一致（文档 vs 24.0588 亦为升） |
| A blocks / largest_flat | 102 / 0.0623 | **44 / 0.2759** | ✅ blocks 103→44、largest .447→.276 中的 44/.276 复现 |
| A flat_frac / run_y / uniq16 / grad_x | 0.486 / 5.89 / 189 / 0.0157 | **0.509 / 6.05 / 192 / 0.0154** | 与文档一致（flat/run_y 相对 v1.33 略回退） |
| 全幅 PSNR/SSIM | 23.1613 / 0.61778 | **21.8122 / 0.61052** | 文档写 21.7959（toned）/ 21.8122（texture）✅ |
| 白边区（左右各 185px）PSNR | 20.60 | **17.93**（污染确认） | 文档“白边过笔”（mean/PSNR 方向一致） |
| **排除白边后全幅 PSNR** | 24.4424 | **24.4863**（略升） | 支持文档“偏低主因白边，不是底色” ✅ |
| 纯默认（37,843 笔）full/content | — | 20.66 / 22.94 | 测试基线记 20.6959（float）✅（默认仅为人类过程演示，质量低于 legacy，文档已提示推荐 recipe/legacy） |

- **判读**：v1.35 的 toned+texture recipe 在**内容区质量不降、A 区 PSNR/blocks 改善**的前提下实现了“人类画师式底色”；
  代价是**全幅方块口径 PSNR 明显下降**（白边越界污染，已独立验证），以及 **A 区 largest_flat 回退（0.062→0.276）**；
  文档在「已知问题」如实写明，并标注 legacy 配方用于最高保真。→ 质量代价**可接受但需向用户说明**。
- 数据：`work/verify4/banding_v135.json`、`work/verify4/runs/toned_tex1536/layered_base_report.json`。

## 6. 独立发现的问题（均不阻塞发布；1/2 已在 docs 写明）

| # | 级别 | 问题 | 证据 | 建议 |
|---|---|---|---|---|
| 1 | P3（已文档化） | v1.35 toned+texture 的 **A 区 largest_flat 相对 v1.33 回退**（0.062→0.276），flat_frac 0.486→0.509、run_y 5.89→6.05；纯 toned（无 texture）更差（文档 0.447，analyst 独立 0.4466；我未重复跑该中间档） | `banding_v135.json`；SKILL.md「已知问题」 | 已作为 tradeoff 写明；二期可用 v1.34 texture/flat 指标继续优化 |
| 2 | P3（已文档化，真实缺陷） | **正式层笔触越界污染左右白边**（白边 PSNR 20.60→17.93），导致全幅 PSNR 不可直接横比；排除白边后新配方 24.49 略高于 v1.33 24.44 | margin 复算；docs 13 处“白边” | docs 已列“后续可加 fg 裁剪”；建议二期实现笔端点 fg 裁剪 |
| 3 | P3（口径差异） | 合成栅格 2/12 组（angle90/res128）我的逐笔 alpha 累加重叠 7.81% > 5%，report 为 3.91%；>2 层=0、maxcnt=2，1536 实际图 report 4.10% 与我完全一致 → 抗锯齿缝隙叠加导致的口径差异，非合规缺陷 | `base_compliance.json` synthetic[] | 测试/文档可注明重叠口径（report 为几何口径） |
| 4 | P3（度量解释） | “base-only 前景 std≤1e-4” 实测全 fg 为 **0.0102**（抗锯齿边缘主导；内部像素 0，测试断言通过、edge ratio 1.6%） | `base_compliance.json`；84 tests（TestTonedDefault） | 文档已说明“只剩抗锯齿边缘”；可在契约里写明“内部/全 fg”口径 |
| 5 | P3 | v1.35 zip 与 SKILL.md 表中 toned+texture 的全幅 PSNR 21.8122 未单列（表中 toned 为 21.7959）；数值均已可复算，影响很小 | SKILL.md 表 | 可补一行 toned+texture 全幅数字 |
| 6 | P3 | 纯默认（toned/no pass4/no detail）full PSNR 20.66 明显低于 legacy 23.16——默认行为变更后的“裸跑质量”代价；文档有“推荐 recipe/legacy”指引但可更醒目 | §5 行；docs「纯默认」1 处 | 建议在 v1.35 节顶部用一句话提示“默认 toned 低笔数=过程演示，质量优先请用推荐 recipe 或 legacy” |

**与 fixer/analyst 声明不一致的关键项：无。** 所有核心数字（底座合规、cap、legacy SHA、线稿 IoU/PSNR/SSIM、
content/A/blocks/largest、全幅+白边解释、正式层增益/边缘占比）全部逐位或口径内复现。

## 7. 测试 / 包 / 文档

- **测试（我在 v1.35 代码上复跑）**：`python -m unittest discover -s tests -v` → **84 tests OK (skipped=3) / 62.7 s / rc=0**
  （3 个 skip 全为 `RUN_SLOW` 慢测试；新 `test_base_coat.py` 19 例通过）；`RUN_SLOW=1` → **3/3 OK / 11.0 s**。
  日志：`work/verify4/logs/tests_full.log`、`tests_slow.log`。
- **包（自解压 `work/verify4/pkg_v135/`）**：
  `dist/fourier-multistroke-training-skill-v1.35-human-underpainting.zip` = **7,925,101 B / 50 文件 /
  SHA256 `D148993FBD9B36AF73C5910D7486E4B2EF1451BDBF85F67B4BF36D75FD59C31D`**；`dist/README_v1.35.md` 声明一致
  （sha/bytes match）；`testzip=None`；平铺 `SKILL.md` + scripts(31)/references(7)/assets(2)/tests(**9**，含 `test_base_coat.py`)；
  `bad_names=[]`；**50/50 逐文件 sha256 == 源树**；无 training/dist/.dsh/__pycache__；旧 v1.32–v1.34 zip 未动。
- **可移植性**：48 个文本文件扫描 `<path>`、`<path>`、`<path>`、`<path>` → **0 命中**；markdown 相对链接 **0 失效**。
- **文档诚实性**：SKILL.md 含「人类画师式底色（v1.35）」原则节、toned/none/legacy 说明、新参数默认表、推荐命令、
  底座验证/线稿/质量代价指标、v1.35 版本记录、84 用例与目录树；明确写“全幅 PSNR 不能与 v1.33 直接横比（白边过笔）”、
  “A 区 largest flat 回退”、“质量有代价、最高保真用 legacy”；**无“质量无损/完全人类化/已完全解决”类宣称**。
- **legacy 逐字节（自复跑）**：v1.33 gate off canonical → PNG `30CDCE74…EB9A01`、strokes `110C9635…`、81,815 笔/23.1883/0.61788；
  v1.34 推荐 → PNG `69820EEF…C265C3`、strokes `3E7CB032…`、99,929 笔/24.3628/0.62545。均与历史产物一致。

## 8. 发布建议

- **可以发布 v1.35**（第三份 skill 包，默认行为切换为 toned）：用户原则（纯色底/单次薄涂/正式层承担）已量化证明，
  legacy 兼容、测试、包、文档全部达标。
- **发布说明必须包含**：① 默认 `--base-color-mode toned`（旧命令需显式传 legacy 模式+旧 err-pct 才能逐字节复现）；
  ② toned 配方全幅 PSNR 不能与 v1.33 直接横比（白边过笔；排除白边后 24.49 vs 24.44 不降）；③ A 区 largest flat
  0.062→0.276（已改善但未回到 v1.33）；④ 纯默认为“人类过程演示”，质量优先用推荐 recipe 或 legacy；⑤ 线稿推荐 `none`。
- 建议二期：① 正式层笔端点 fg 裁剪（消除白边污染）；② A 区 banding 继续用 texture-flat/自适应密度优化；
  ③ 稀疏主体 toned 的 overlap 口径文档化。

## 9. 证据索引

| 内容 | 路径 |
|---|---|
| 我独立跑的 v1.35 各 run（default/toned_tex/legacy_v133/legacy_v134/lineart） | `work/verify4/runs/` |
| 底座合规 + 合成栅格 | `work/verify4/base_compliance.json` |
| banding / content / A/B 指标 | `work/verify4/banding_v135.json`、`work/verify4/lineart_metrics.json` |
| 测试日志（84 tests / 慢测试） | `work/verify4/logs/tests_full.log`、`tests_slow.log` |
| 包校验 | `work/verify4/package_validation_v135.json`、`work/verify4/pkg_v135/` |
| 视觉图（线稿三联/旧灰草图底、A 区、包） | `work/verify4/visual/` |
| 复算脚本 | `work/verify4/base_compliance.py`、`lineart_eval.py`、`validate_pkg_v135.py`（banding 用 `work/visual2/banding_metrics.py`） |
