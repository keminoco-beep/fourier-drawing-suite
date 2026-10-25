# Lvpi5 纯笔画图生图训练（无区域填色）

目标：把 `input/Lvpi5.jpg`（蓝发鲸尾女仆彩铅插画，1408×1408）用**纯傅里叶路径 + 线条笔画属性**
复刻。生成物的每个元素都是 `stroke`（路径 + 逐点宽度/颜色/alpha/笔形/层序），
**`fill_count = 0`**，不使用任何区域填色、颜色场或面片。

## 1. 方法

每条笔画 = 一条 Fourier 曲线 + 四条属性曲线（width / color / alpha / shape）。
人类绘画逻辑用分层实现：

| 层 | 内容 | 生成方式 |
|---|---|---|
| 0 | 大色块铺底 | 粗分割色区内的**长向扫描线笔触**（宽 3–13px，压力两端收细） |
| 2 | 中间排线 | 细分割色区的短排线（宽 2–6px，半透明叠色） |
| 3/5 | 细节线 | 原图强梯度/暗墨骨架 → 细线笔触（眼、嘴、发丝、轮廓） |
| 6 | 补洞 | 渲染误差高的近白洞，用局部短笔补色 |
| 7 | 脸部墨线 | 脸部强边缘的深色细线（300 笔，眉毛/睫毛/嘴线/发丝；端点锥度） |
| 8 | 顺流发丝 | 蓝色头发 mask 内沿结构张量切向的长曲线（550 笔，宽 2–4px，alpha 0.72），打散量化色块 |

训练循环每轮：

1. 从当前 best 配置做参数变异（分区/模糊/笔宽/间距/细节阈值/颜色抖动…）；
2. 生成笔画 → 渲染 → 误差补笔；
3. 评分 `J = PSNR + SSIM − 0.02×(笔数/1000)`（质量优先轨；平衡轨用 `--stroke-weight 0.10`）；
4. 保存 `rounds/round_XXXX_R{R}.png`（左原图/右结果）与指标；
5. 只保留 J 更高的 best 笔触集，支持断点续训。

分辨率策略：先在 1024 上快速迭代 54+ 轮，确定参数与分层；再用 **1408 工作分辨率**
热启动微调（每轮约 108s）。1408 轨的几何细节直接使用源图像素，最终编译到 R=1536 契约
并渲染 2048，比 1024 笔画放大版更锐。

## 2. 训练结果（2026-10-21）

- **1408 画质优先轨**（`run_1408_q/`）：22 轮（仍在续训），best J=18.4104 @r21，
  **PSNR=18.2947 / SSIM=0.53259 / 20,844 笔**（1408 训练口径）。
  再叠加 300 笔脸部墨线、550 笔顺流长发丝与线条锥度后：**21,694 笔**，同一 `.fbs` 渲染：
  **PSNR 18.4505 / SSIM 0.53311 @1536**，**PSNR 18.3553 / SSIM 0.53538 @2048**，
  **PSNR 18.1059 / SSIM 0.58735 @4096**（4096 为展示分辨率，SSIM 随分辨率上升，PSNR 不可跨分辨率直接比较）。
- **精简高分辨率版**（`final_lean_r21/`）：在 r21 视觉精修件上剪掉 26% 低收益铺底/纹理笔，
  再补 448 笔洞：**17,228 笔**（比完整版少 20.6%），2048 PSNR **18.1249** / SSIM **0.53079**，
  1536 PSNR **18.2110** / SSIM **0.52685**；只比完整版低约 0.23dB。fbs 279.3MB、fdraw 31.4MB。
- **极简高分辨率版**（`final_minimal_r21/`）：保留 60% 非线笔 + 706 补洞笔，**14,839 笔**
  （比完整版少 31.6%），2048 PSNR **17.9554** / SSIM **0.52895**，1536 PSNR **18.0275** / SSIM **0.52385**；
  fbs 237.1MB、fdraw 26.8MB。三档形成完整“笔画数 ↔ 保真度”Pareto 曲线。
- **1024 质量轨**（`run_1024_q/`）：43 轮，best **PSNR=18.346 / SSIM=0.5337 / 17,626 笔**（1024 口径）；
  继续编译得 2048 PSNR 17.69 / SSIM 0.5148。
- **1024 平衡轨**（`run_1024/`）：31 轮，best J=17.406 @r11，PSNR 17.945 / SSIM 0.5144 / 13,105 笔。
- **精简对照**（`lean_q/`）：从 1024 best 剪掉 15% 低收益铺底笔再补 220 笔洞：
  **14,136 笔，PSNR 18.015 / SSIM 0.5178 @1024**。
- **默认改进版**（`final_lean_fix_r21/`）：在精简视觉件上补 **221 笔亮洞修补 + 107 笔暗线修复**：
  **17,693 笔**，2048 PSNR **18.2770** / SSIM **0.53362**，1536 PSNR **18.3701** / SSIM **0.53045**；
  比上一默认 +0.15dB、仅多 465 笔。fbs 286.0MB、fdraw 32.2MB。
- 结论：**默认交付选 1408 精简+补洞版**（17,693 笔；比完整版少 18.5% 笔画，2048 只 −0.078dB）；
  完整版作为画质优先档，极简版作为进一步省笔档。独立视觉评审同意“精简版优先”的取舍。

> 指标是同分辨率下与源图比较；纯笔画、零填色，目标不是像素级复刻，而是“可画、笔触可信、
> 结构/色调尽量接近”。参考是彩铅手绘，本方法输出也保留彩铅/排线质感。
> 独立视觉评审：明显优于早期版本，可作当前阶段成果；纸纹颗粒、睫毛锥度、眼白高光仍待下一阶段。

## 3. 最终产物

### 3.1 默认交付（`final_lean_fix_r21/`，17,693 笔）

- 2048 成图：`Lvpi5_purestroke_R2048.png`（PSNR 18.2770 / SSIM 0.53362）
- 1536 成图：`Lvpi5_purestroke_R1536.png`（PSNR 18.3701 / SSIM 0.53045）
- `.fbs`：286.0MB；`.fdraw`：32.2MB（validate OK，0 填色）
- 对照：`final_compare_sheet.png`
- 绘画过程视频：`../video/Lvpi5_fourier_circles_60s.mp4`（60s / 30fps / 1080²，傅里叶圆链逐笔）

### 3.2 画质优先（`final_r21/`，21,694 笔）

| 文件 | 说明 |
|---|---|
| `Lvpi5_purestroke_R1536.fbs.json` | 最终 `.fbs`；契约 R=1536 / ε=0.08 / N_max=16384 / curve_tol=0.002 / max_curves=40000；**fills=0** |
| `Lvpi5_purestroke_R1536.png` | 1536² 契约分辨率渲染（fast 后端） |
| `Lvpi5_purestroke_R2048.png` | 2048² 渲染成图 |
| `Lvpi5_purestroke_R4096.png` | 4096² 展示级渲染（SSIM 0.587；在容器 cap 内） |
| `Lvpi5_purestroke.fdraw` | `.fdraw` 单文件容器；`validate` 通过、root hash 校验通过；47.1MB |
| `compile_report.json` / `final_report.json` | 笔画数/系数数/字节数/渲染指标/容器上限 |
| `verification.json` + `../VERIFICATION.md` | 自动校验：fills=0、容器上限、validate、渲染复算与 SHA256 |
| `../eval/SUMMARY.md` | skill 官方 1536 四维度量（IoU/召回/精度/色调/线宽/墨量） |
| `../ROUNDS_AUDIT.md` | 每轮训练产物审计（132 轮、PNG/report 缺失 0） |
| `final_compare_sheet.png` | 原图 vs 2048 结果 + 脸部放大对照 |
| `../progress_1408/` | 1408 轨每轮成果总表 + GIF |
| `../final_lean_fix_r21/` | **默认精简+补洞版**（17,693 笔，2048 PSNR 18.277） |
| `../final_lean_r21/` | 上一默认精简版（17,228 笔，2048 PSNR 18.125） |
| `../video/` | 傅里叶圆绘画过程视频 + 抽帧 + 复现说明 |
| `../final_minimal_r21/` | 极简高分辨率版（14,839 笔，2048 PSNR 17.955；笔画少 31.6%） |
| `../final_visual/` | 上一版视觉精修件（22,751 笔，2048 PSNR 18.269） |
| `../final_1408_best/` | r9 精修件（22,231 笔，2048 PSNR 18.116） |
| `../final/` | 1024 主交付备份（16,324 笔，2048 PSNR 17.69） |

## 4. 继续训练 / 停止

```powershell
$PY = "<python.exe>"    # 换成你的 Python 解释器路径（已装 numpy/Pillow/scipy）
cd "<workspace>/training"    # 换成你的工作区 training 目录；<...> 均为占位符

# 继续 1408 画质轨（断点续训；不设轮数上限 = 持续训练）
& $PY scripts\run_train.py --input input\Lvpi5.jpg --outdir run_1408_q `
    --work-res 1408 --max-rounds 100000 --minutes 0 `
    --fix-passes 2 --fresh-every 12 --stroke-weight 0.02 --ssim-weight 1.0

# 停止：训练在下一轮边界退出
New-Item -ItemType File run_1408_q\STOP -Force

# 一键脚本（在 training/ 下）：
#   powershell -ExecutionPolicy Bypass -File .\start_training.ps1
#   powershell -ExecutionPolicy Bypass -File .\stop_training.ps1

# 重新编译 best：脸部墨线 -> 顺流发丝/锥度 -> .fbs/PNG
& $PY scripts\refine_best.py --run run_1408_q --input input\Lvpi5.jpg `
    --outdir refine_1408_r21 --count 300 --work-res 1408
& $PY scripts\refine_visual.py --strokes refine_1408_r21\refined_strokes.json `
    --input input\Lvpi5.jpg --outdir refine_visual_r21 --work-res 1408 --flow-count 550
& $PY scripts\compile_best.py --run run_1408_q --strokes refine_visual_r21\visual_strokes.json `
    --input input\Lvpi5.jpg --outdir final_r21 --resolution 1536 --render 2048
```

每轮都会写 `rounds/round_XXXX_R1408.png`；best 会在 `best_R1408.png` 与 `best_strokes.json` 更新。

## 5. 参数上限核对（画质优先完整版；默认精简+补洞版见 `final_lean_fix_r21/final_report.json`，17,693 笔 / 286.0MB）

| 项 | 设定 | 实测 |
|---|---|---|
| Build 目标分辨率 | ≤8192，本次 1536 | 1536 |
| ε | 0.08 | 0.08 |
| N_max | 16384 | 16384（单曲线受 Nyquist 钳制） |
| curve_tol | 0.002 | 0.002 |
| max_curves | 40000 | 21,694 |
| 渲染 | ≤8192 | 1536 / 2048 / 4096 |
| `.fbs` 体积 | 建议 <1GiB | 449.7MB |
| `.fbs` 系数数 | — | 8,520,196 |
| `.fdraw` render.json | ≤1GiB | 201.8MB |
| `.fdraw` 解压总大小 | ≤2GiB | 201.8MB |
| `.fdraw` 元素数 | ≤100000 | 21,694 |
| 填色元素 | **0** | **0** |

## 5.5 `layered_base.py` 分层底色 + 上层渐变安全门（v1.33，2026-10-24）

`layered_base.py` 是「铺满底色 → 按渲染误差局部补笔」的路径：第一层用粗网格（`--coarse`）
全幅覆盖唯一一次，Pass2/3/4 只在误差分位（`--mid-err-pct/--fine-err-pct/--pass4-err-pct`）
以上区域补笔；`--upper-gradient` 让上层笔携带两端渐变。v1.33 给渐变笔加了默认开启的安全门。

### 5.5.1 canonical 1536 命令（复现用户所见版本）

```powershell
python skills/fourier-multistroke-training/scripts/layered_base.py ^
  --input training/input/Sunflowers_square.png ^
  --detail training/sunflowers/clean_r1/cleaned_strokes.json ^
  --outdir <OUT> --work-res 1536 --coarse 20 --mid 9 --fine 4.5 ^
  --mid-err-pct 85 --fine-err-pct 78 --fine-grad-pct 55 ^
  --base-color-mode quantized --base-k 8 --base-jitter 0.006 ^
  --mid-jitter 0.006 --fine-jitter 0.008 ^
  --upper-color-mode source --upper-blur 2.5 --upper-gradient ^
  --pass4 --pass4-err-pct 75 --pass4-spacing 5
# A/B 修复前行为：同命令追加 --gradient-gate off（逐字节复现 v1.32：
#   81,815 笔 / 23.1883 / 0.61788，PNG SHA256 30CDCE74…EB9A01）
```

注意：这组是**历史实验参数**，不是脚本默认值；旧文档“建议命令”缺
`--coarse 20 --mid 9 --fine 4.5 --mid-err-pct 85 --fine-err-pct 78 --fine-grad-pct 55`
与 `--mid-jitter/--fine-jitter`（QA-02），照抄跑不出 81,815 笔。

### 5.5.2 渐变安全门参数（默认值冻结）

| 参数 | 默认 | 含义 |
|---|---|---|
| `--gradient-gate {on,off}` | **on** | `off` = 完全跳过子门，复现 v1.32 旧行为（A/B/回滚） |
| `--gradient-max-delta FLOAT` | **0.12** | 端点色差 `Δ = max_c abs(src(start)_c − src(end)_c)` 上限，超阈值→退单色；`<=0` 关闭 |
| `--gradient-edge-delta FLOAT` | **0.10** | 沿笔 9 点路径采样的相邻色跳上限，超阈值→退单色；`<=0` 关闭 |
| `--gradient-edge-pct FLOAT` | **100.0（关闭）** | `0<pct<100` 时启用 edge-map 子门：路径采样点落在高梯度像素→退单色；100 或 `<=0` 关闭 |

接受条件（Lead 冻结的严格契约，无平滑 ramp 例外）：9 点采样全在 fg 内；端点 Δ ≤ max-delta
（含 jitter 后的落盘值）；相邻采样色跳 ≤ edge-delta；若 edge-pct<100，采样点不得命中
`max_filter(|∇|,5) > P_pct(grad[fg])` 的像素。被拒笔退化为“有效采样均值单色笔”，
**不改变笔几何**，也不额外渲染；gate on/off 的 RNG 消耗相同，因此前两 pass 几何逐字节一致，
适合公平 A/B。`--gradient-edge-pct 90/95` 是更保守的可选档（见 5.5.4）。

### 5.5.3 前后实测（同参数同 detail，只有门控开关不同）

| 指标 | before（gate off） | after（默认安全门） | Δ |
|---|---|---|---|
| 总笔数 | 81,815 | 81,010 | −805 |
| 全幅 PSNR / SSIM（float 渲染） | 23.1883 / 0.61788 | 23.1059 / 0.61583 | −0.082 dB / −0.0021 |
| 内容区 PSNR / SSIM（doc 协议：8iNfq→1165×1536，render 裁 x[185,1350)） | 24.5058 / 0.53887 | 24.4302 / 0.53653 | −0.076 dB / −0.0023 |
| 输出渐变笔端点 Δ>0.12 / >0.15 / >0.2 | 5,329 / 3,160 / 1,417 | 0 / 0 / 0 | 清零 |
| 输出渐变笔端点 Δ 最大 | 0.6875 | 0.1200 | — |
| 渐变接受率 | 100% | 89.1%（50,950/57,171） | — |
| 耗时 | 99.2 s | 97.1 s | 无退化 |

`work/fix/metrics_1536_final.json` 用另一套裁剪/SSIM 实现口径，数值不同，不可与本表混用。

### 5.5.4 edge-pct 默认关的证据（同 canonical 参数，1536）

| 配置 | 接受渐变笔 | 全幅 PSNR（float 渲染） |
|---|---|---|
| pct=100（默认，edge-map 关） | 50,950（89.1%） | 23.1059 |
| pct=90（edge-map 开） | 9,019（15.7%） | 23.0642 |

pct=90 把 84% 渐变笔退化为单色、PSNR 反而更低 → 默认 100；保守场景可手动开 90/95。

### 5.5.5 report 缺陷指标（名字冻结，测试/verifier 断言用）

`gradient_strokes`（实际输出渐变笔数）、`gradient_rejected_delta`、`gradient_rejected_edge`、
`gradient_mean_delta`（候选笔 Δ 均值）、`gradient_max_delta`、`stray_color_px`（前景内终图与参考
`max_c abs(ren−rgb)>0.25` 的像素数）；另有 `gradient_candidates`、`gradient_rejected_oob`、
`gradient_rejected_edge_color/grad`、`gradient_accepted_mean_delta`、`gradient_by_pass`、
`gradient_edge_frac`、`stray_color_frac`、`gradient_gate_params` 等诊断字段，旧字段全部保留。
语义：`candidates = accepted + rejected_delta + rejected_edge + rejected_oob`。

### 5.5.6 已知限制与后续（未实现/未验证的已标注）

- 红圈 B（花瓶右缘/地面前景跨边界深色楔形）**局部减轻**：楔形区 mean|Δ| 0.1768→0.1602；
  红圈 A（花头糊斑）**基本无改善**（mae 0.04256→0.04294）。证据图：`work/fix/crops_A_triple.png`、
  `work/fix/crops_B_triple.png`、`work/fix/zoom_B_wedge_triple.png`。
- 根因不在渐变笔：把 doc 原图全部 57,976 条渐变笔压平后，A/B ROI mean|Δ| 仅 0.0052/0.0031；
  视觉主体来自 K8 底色宽笔 + 调色板区域边界 + pass2/3/4 高误差百分位只局部覆盖。
  本版只承诺消掉“跨色边界彩色渐变”这一类缺陷。
- 后续建议（**未实现、未验证**）：结构区减小 pass2 笔宽、提高 pass3/4 局部覆盖率、
  在色边界处把跨边界笔拆成两段单色笔；用红圈 ROI 局部指标替代对渐变不敏感的全局
  `stray_color_px` 做回归门禁。
- 参数上限/体积口径在本文 §5 仍是旧 2GiB 档数字，与 SKILL.md 的 4GiB 档不一致（QA-09，待统一）；
  以 SKILL.md「参数上限与实测（交付口径）」为准。

## 5.6 `layered_base.py` 细纹理层 texture-flat（v1.34，2026-10-24）

解决 Sunflowers 中间红圈 A（render `(735,692)-(1077,968)`，花头/花瓣）的“颜色分层”：
pass1 底色每笔是一块平面色（palette K=8 / 单色，宽 1.5×coarse、长 2×coarse），Pass2/3/4
只补高误差百分位 → A 区大面积停在平色块。v1.33 after 的 A 区 flat_frac(3×3 std<0.01)
**49.1%**（ref 3.8%）、≥50px 平色块 **103**（ref 2）、run_y **5.99**（ref 2.43）、grad_x 0.0156
（ref 0.0338）、uniq16 183（ref 407）。只换平滑底色会提 PSNR 但 flat_frac 更高，必须补纹理。

**v1.34 推荐 canonical 1536（opt-in recipe，实测 99,929 笔 / 105.6 s）**：

```powershell
python skills/fourier-multistroke-training/scripts/layered_base.py ^
  --input training/input/Sunflowers_square.png ^
  --detail training/sunflowers/clean_r1/cleaned_strokes.json ^
  --outdir <OUT> --work-res 1536 ^
  --coarse 12 --mid 9 --fine 4.5 ^
  --mid-err-pct 85 --fine-err-pct 78 --fine-grad-pct 55 ^
  --base-color-mode source --base-blur 4 --base-k 8 --base-jitter 0.006 ^
  --mid-jitter 0.006 --fine-jitter 0.008 ^
  --upper-color-mode source --upper-blur 2.5 --upper-gradient ^
  --texture-flat --texture-flat-count 37000 --texture-flat-ref-std 0.015 ^
  --texture-flat-layer 19
# 不传 --pass4（腾 ~24k 笔预算给纹理层）；gate 默认 0.12/0.10/100
# before 对照 = §5.5.1 的 v1.33 canonical（81,010 笔 / 23.0791 / 24.4302）
```

**参数（默认冻结；默认全部关闭 = v1.33 行为逐字节不变）**：

| 参数 | 默认 | 含义 |
|---|---|---|
| `--base-color-mode {single,quantized,source}` | **single** | source = pass1 取 `gaussian_blur(src, --base-blur)` 三点中位色（底色软化） |
| `--base-blur` | **4.0** | source 模式高斯 σ（@work_res 像素） |
| `--base-gradient` | off | pass1 端点渐变（走 v1.33 同一安全门） |
| `--base-width-factor` / `--base-length-factor` | **1.5 / 2.0** | pass1 笔宽/长 = 系数 × `--coarse`（= 旧硬编码） |
| `--texture-flat` | **off** | 细纹理层开关 |
| `--texture-flat-count` | **8000** | 纹理笔上限；0=按面积 auto（~6000@1536）；总笔数硬上限 100000 |
| `--texture-flat-spacing` | **2.5** | 候选网格 px@1280，按 rs=max(H,W)/1280 缩放 |
| `--texture-flat-len-min` / `--texture-flat-len-max` | **5.0 / 9.0** | 笔长 px@1280（按 rs 缩放） |
| `--texture-flat-width-min` / `--texture-flat-width-max` | **2.5 / 3.5** | 笔宽 px@1280（按 rs 缩放） |
| `--texture-flat-lstd` | **0.01** | 渲染 3×3 luma std 平坦阈值 |
| `--texture-flat-ref-std` | **0.0** | 参考图 3×3 luma std 下限（0=off；推荐 0.015–0.02），并作候选降序优先级 |
| `--texture-flat-layer` | **19** | 纹理笔层号（detail 之上、最顶层 20 之下） |
| `--texture-flat-jitter` | **0.01** | 每笔颜色抖动 |

机制：先渲染当前 strokes → 3×3 luma std < lstd 判为平坦候选 → 加 ref-std 门控并按参考纹理
强度降序分配预算 → 在平坦像素附近撒细短源色笔（kind=`oil_flat_texture`，颜色=源图局部均值
+ jitter）→ 再渲染。纹理笔给平涂区补高频颜色变化，把平色块/色带打散。

**前后实测（analyst banding 口径）**：

| 指标 | v1.33 after | final v1.34 | Δ |
|---|---|---|---|
| A 区 PSNR / MAE | 24.0588 / 0.04343 | **24.7211 / 0.03965** | +0.662 dB |
| A flat_frac / blocks | 0.491 / 103 | **0.437 / 74** | −0.054 / −29 |
| A run_y / bstep_p95 | 5.99 / 0.141 | **5.45 / 0.133** | −0.54 / −0.008 |
| A uniq16 / entropy16 | 183 / 4.97 | **195 / 5.32** | +12 / +0.35 |
| A 纹理笔 / 覆盖 | 0 | **2,320 / 37,000**（足迹 74.9%，含重叠） | — |
| 全幅 PSNR / SSIM | 23.0791 / 0.61572 | **24.3322 / 0.62533** | +1.253 / +0.0096 |
| 内容区 PSNR / SSIM | 24.4302 / 0.53653 | **24.8173 / 0.54451** | +0.387 / +0.0080 |
| B 区 PSNR | 26.9247 | **28.0935** | +1.169 |
| 总笔数 / 耗时 | 81,010 / 97 s | **99,929 / 105.6 s** | +18,919 / +8.6 s |

预算：v1.33 canonical 中 pass4 约 24,979–25,294 笔；去 pass4 后 base=62,929 笔，100k 上限
只剩 37,071 → texture count 37,000。去 pass4 是**本图预算取舍**（非默认行为变化）；
需要 pass4 的图可保留 pass4 并降低 texture count（对照 A PSNR 24.16–24.21，改善较小）。

report 字段：`texture_flat_strokes`、`texture_flat_candidates`、`texture_flat_params`
（requested/emitted/candidates/budget/ref_std_thr/spacing_px/flat_frac_fg/capped_100k 等）；
pass1：`base_color_mode/base_blur/base_gradient/base_width_factor/base_length_factor`。
旧字段与 v1.33 渐变门字段不变。

**关闭/回滚**：不传 `--texture-flat` = 无纹理层（默认行为与 v1.33 逐字节一致，256 渲染 md5
`5CEDAAC084511CA7`）；回 v1.33 canonical 再加 `--coarse 20 --base-color-mode quantized
--pass4 --pass4-err-pct 75 --pass4-spacing 5`。v1.33 canonical 仍可逐字节复现历史产物。

**已知限制（如实）**：blocks 74 > MUST 65；largest_flat_fraction 0.064→0.112（唯一反向指标：
残余最大连续平色块位于画面中部 render bbox (735,735)-(881,967)，参考图该处有中等局部纹理
ref 3×3 lstd p50≈0.027，预算限制下该区域纹理笔不足）；grad_x/uniq16/flat_frac 三项 SHOULD 未达；
纹理判词“偏糊”（texture_ratio 0.615 vs 0.62）、4× 可见规则“梳状”细纹（画风取舍）。
根本瓶颈：100k 上限下 A 区只能拿 2,320 条纹理笔。改进（未实现、未验证）：提高曲线上限、
区域自适应密度、更细 detail 替换部分 pass3。通用性：只在 Sunflowers 1536 实测；纹理笔颜色
来自参考图，风格化输入应关闭；去 pass4 对大面积平滑渐变/图形化输入可能掉色准，需按 flat
面积占比/误差决策。

## 5.7 人类画师式底色（v1.35，2026-10-24）

**原则（用户核心约束）**：底色只允许**纯色**；**一个区域只涂一次**；底色不得呈现“原图草图/
多色草稿”；作画重心在**正式绘制层**；一切以“人类画师能做到且会去做”为准。
v1.32–v1.34 的 `quantized`（K8 局部取色多色平铺）/`source`（逐笔局部源色 + 渐变）会让底色本身
就是原图模糊色块，像“多色草图底”。v1.35 改默认：`toned`（纯色单次薄涂）为默认，
`none`（无底色）用于线稿/白纸；legacy 三种模式保留但**不推荐**，仅用于复现历史产物。

| 参数 | 默认 | 含义 |
|---|---|---|
| `--base-color-mode {toned,none,single,quantized,source}` | **toned** | toned=纯色单次薄涂；none=无底色；其余 legacy |
| `--base-color` | **auto** | auto=整幅图像逐通道中位色；`#RRGGBB`；`"r,g,b"`（0–1 或 0–255） |
| `--base-coat-angle` | **0.0** | 行扫角度（0=水平） |
| `--base-coat-spacing` | **0.0** | 行距 px，0=取 `--coarse`（report 写解析后实际值） |
| `--base-coat-width-factor` | **1.04** | 笔宽=pitch×factor；稀疏 fg 自动收窄（report 实际值 vs `_requested`） |
| `--base-coat-jitter` | **0.5** | 中线沿笔方向抖动 px（不开缝） |
| `--mid-err-pct / --fine-err-pct / --pass4-err-pct` | **60 / 45 / 45** | 正式绘制重心默认（原 82/72/60） |
| `--max-curves` | **100000** | 4GiB 契约硬上限；超限确定性抽稀（见下） |

底色笔 kind=`base_toned`、layer=0；禁止 gradient / 逐笔颜色抖动 / 局部取色；alpha=1.0。
`none` 模式：`base_strokes=0`，coverage/overlap 字段为 None、`base_color_std=0.0`。

**底座合规实测**：Sunflowers toned 底座 77 条长笔、单一纯色 `[0.812,0.769,0.263]`、std=0、无
gradient、栅格覆盖 100%、行带重叠 0%；合成 96–256px（angle 0/90）与 384×288：覆盖 1.0000、
overlap 3.19–4.00% ≤5%、maxcnt=2、uncovered 行 0。旧底色（analyst 独立口径）：v1.34 source 底
12,544 色/重叠 99.9%/base-only std 0.154，v1.33 quantized 4,543 色/重叠 99.9%/std 0.140，
线稿旧 source 底 3,987 色/std 0.297/重叠 93.7% → 旧底确实是“多色草图”。

**100k 硬上限**：所有层与 detail 合并后若 > `--max-curves`，按 **texture-flat → pass4 → pass3 →
pass2** 均匀抽稀（base/detail 最后兜底），抽稀后重渲染并写 `capped_100k / cap_adjustments /
pre_cap_strokes`。实测：纯默认 37,843 笔（capped_100k=false）；默认+`--pass4` 109,548 → 100,000
（`cap_adjustments={"base_pass4":{"removed":9548,"frac":0.1332}}`，pass2 6,116 / pass3 31,663 保留）。

**推荐命令 A：线稿 / 白纸（base none）**

```powershell
python skills/fourier-multistroke-training/scripts/layered_base.py ^
  --input work/input2/img_20261016.png --outdir <OUT> ^
  --work-res 1200 --base-color-mode none ^
  --coarse 12 --mid 6 --fine 3 --mid-err-pct 55 --fine-err-pct 40 ^
  --pass4 --pass4-err-pct 40 --pass4-spacing 3 ^
  --mid-jitter 0.004 --fine-jitter 0.006 ^
  --upper-color-mode source --upper-blur 0.8 --upper-gradient
# 96,636 笔（0 底笔）；IoU 0.8492 / recall 0.9755 / precision 0.8677 / PSNR 18.276 / SSIM 0.776
# 旧 v1.34 source 底同图：IoU 0.7369 / precision 0.7574 / PSNR 14.437 / SSIM 0.6378
```

**推荐命令 B：油画 / 彩插（toned + 正式层 + texture-flat）**

```powershell
python skills/fourier-multistroke-training/scripts/layered_base.py ^
  --input training/input/Sunflowers_square.png ^
  --detail training/sunflowers/clean_r1/cleaned_strokes.json --work-res 1536 ^
  --coarse 20 --mid 10 --fine 5.5 --mid-err-pct 60 --fine-err-pct 45 ^
  --pass4 --pass4-err-pct 45 --pass4-spacing 7 ^
  --base-color-mode toned --base-color auto ^
  --mid-jitter 0.006 --fine-jitter 0.008 ^
  --upper-color-mode source --upper-blur 2.5 --upper-gradient ^
  --texture-flat --texture-flat-count 10700 --texture-flat-ref-std 0.015 ^
  --texture-flat-layer 19
# 99,905 笔；content 24.5669/0.51407、A PSNR 24.321；A blocks 103→44、largest 0.447→0.276、
# flat .578→.509、uniq16 178→192
```

**Sunflowers toned 演示（89,205 笔，质量代价如实）**：全幅 PSNR 21.7959 / SSIM 0.60797
（v1.33 23.0791 / v1.34 24.3322）、内容区 24.5205 / 0.51072（v1.33 24.4302 / v1.34 24.8173）、
A 区 24.309。**全幅 PSNR 不可与 v1.33 横比**：偏低主因是正式层笔触越界污染白边（mean 0.9777→
0.9701/0.9637；左/右白边 PSNR 19.7→18.6、21.6→17.3），不是底色；doc 内容区 Lead 复算
content 24.511 / 0.5351、A 24.309 → 画面主体不降、内容区略升。追求最大保真（含白边干净）用
legacy 配方。toned 单独时 A 区 banding 回退（largest flat 6.4%→44.7%、flat .491→.578）；
油画场景叠加 `--texture-flat`（推荐命令 B）后 blocks 103→44、largest 0.447→0.276。

**legacy 兼容**：显式传 legacy 模式 + 显式旧参数时，v1.33 canonical PNG SHA `30CDCE74…EB9A01`、
v1.34 推荐 SHA `69820EEF…` **逐字节一致**（`work/underpaint/legacy/legacy_compat.json`）。
默认行为变化仅两处：不显式传 `--base-color-mode` → toned；不显式传 err-pct → 60/45/45。

**限制**：toned 全幅 PSNR 低于 legacy 是设计原则直接后果（底色不带图像信息），非 bug；稀疏
主体（线稿）toned 的 fg overlap 偏高（res192 4.35%），推荐 `none`；`--base-color auto` 对白边
多的图偏亮；行带几何对非凸/多孔主体会涂带内空白（只保证覆盖主体），镂空/线稿用 `none`；
硬上限抽稀为均匀删减（不重新采样），被裁层轻微稀疏、`gradient_by_pass` 未回算（仅诊断）。

## 5.8 人类画师式画法（v1.36，2026-10-24）

**用户批评**：“现在的画法还是从上到下扫描式地画了好几轮，这是人类画师会做的事情吗？”
v1.35 已改底色为纯色单次薄涂，但画法仍是：水平行扫底色 + 轴对齐 tile 网格成行生成 + 按 y 排序
→ 视频里“逐行扫描、多轮铺”。v1.36 改“怎么画/什么顺序”，**v1.35 底色原则不变**。

**改前扫描性基线（analyst 口径）**：v1.35 从头重画 99,683 笔中 `full_canvas_rounds=5`
（6 层中 5 层覆盖整幅）、layer1/2/3 y-Spearman=+1.000、底色 77 条全水平（axis=1.0、方向熵=0）、
K=12 簇 `cluster_compactness=0.085`、`region_run_frac=0.661`、进度帧 3%/7%/12%/20% 已覆盖
顶部横带→几乎全图（`work/humanorder_eval/`）。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--base-coat-pattern {wash,rows}` | **wash** | 方向多变宽笔薄涂；rows=v1.35 行扫（legacy） |
| `--wash-angle` | **-22.0** | wash 行带主方向（度），带内方向随带变化 |
| `--flow-strokes {on,off}` | **on** | 正式层笔触顺源图结构张量切向 |
| `--paint-order {region,scan,legacy}` | **region** | 区域成组顺序；scan=按 y；legacy=旧行为 |
| `--region-count` | **12** | 区域簇数（8–16 可用） |
| `--thick-dabs` | **0** | 厚涂短笔数（0=关；油画 demo 14000） |
| `--clip-to-fg {on,off}` | **off** | 可选：笔端点裁剪到前景（opt-in） |

**legacy 自动回退**：显式 legacy 底色（single/quantized/source）且未传 flow/paint-order 时自动
flow=off + paint_order=legacy + pattern=rows；不传新参数时默认 wash/on/region。

**wash 实测**：canvas 192×144 res96–256 → coverage 0.9924–0.9982、overlap_fg 3.7–5.4%、
max_count=2、单色 std=0；384×288 → 0.9933–0.9949 / 4.3–5.0%；线稿 toned → 0.995–0.997 /
9.7–10.0%；Sunflowers 384 底座 225 笔、coverage 0.9943 / overlap 0.0435。

**推荐命令（Sunflowers 从头重画，v1.36 默认 recipe）**：

```powershell
python skills/fourier-multistroke-training/scripts/layered_base.py ^
  --input training/input/Sunflowers_square.png ^
  --outdir <OUT> --work-res 1536 ^
  --coarse 20 --mid 11 --fine 5.5 --mid-err-pct 60 --fine-err-pct 45 ^
  --pass4 --pass4-err-pct 45 --pass4-spacing 7 ^
  --upper-color-mode source --upper-blur 2.5 --upper-gradient ^
  --texture-flat --texture-flat-count 20000 --texture-flat-ref-std 0.015 ^
  --thick-dabs 14000
# wash 默认、flow=on、paint-order=region、clip-to-fg=auto
# 100,000 笔 / content 24.1320（doc 协议）/ full 25.2338 / 178.6 s
# 回 v1.35 画法：--base-coat-pattern rows --flow-strokes off --paint-order legacy
```

**report 字段**：`base_coat_pattern`、`wash_angle`、`base_metrics_mode`、`base_metrics_scale`、
`base_coverage`、`base_overlap_frac`、`base_strokes`、`flow_strokes`、`flow_orientation_frac`、
`axis_aligned_frac`、`direction_strokes`、`paint_order`、`paint_order_effective`、
`order_y_spearman`、`order_adjacent_median_px`、`order_region_run_frac`、`region_count_used`、
`clip_to_fg`、`thick_dabs`、`thick_dab_strokes`、`texture_flat_strokes`；旧
`gradient_strokes/capped_100k/cap_adjustments` 全保留。

**v1.36 实测（1536 原生，100,000 笔，178.6 s）**：wash 71 笔纯色 coverage 1.0/overlap 0.0713
（analytic，小图真实栅格 3.2–6.1%）；full PSNR/SSIM **25.2338/0.62321**（白边已修 +1.43dB）、
content **24.1320（doc 协议；冻结基线 runE_dabs 24.6184 → Δ−0.486dB，超 ≤0.4 线 0.086 →
质量 CONDITIONAL）**、square 口径 24.1985；A 24.115 / blocks 65 / largest_flat 0.416
（v1.35 runE_dabs 24.625/64/0.222 → A 回退）；B 27.4957（v1.35 28.149）；白边 mean 0.9984；
gradient 53,709 / texture 17,464 / dabs 14,000；flow_orientation 0.9086、axis_aligned 0.2663、
order_y_spearman 0.197、region_run 0.9993、adjacent 190.65px。analyst 独立验收：wash 底
axis≤10° 1.000→0.183、扫描层数 3→0、layer 块连续性 1.000→0.121、簇 span 0.974→0.548、
formal y-Spearman 0.201（顺序/扫描性 PASS）；compactness 0.085→0.201 仍低于 0.25（残余）。
**限制**：质量 CONDITIONAL（doc −0.486dB，超 ≤0.4 线 0.086；注 runE 24.5367 为无 dabs 错口径勿引用）；
A largest_flat 0.222→0.416、A PSNR −0.51、A blocks 64→65、B −0.65；运行 178.6s 未达
≤120s；>768px base 指标为 analytic 口径；flow_align 两口径（report 0.9086 / analyst 0.819）；
wash 为对角主扫+带内弓形、带间自然重叠 3–7%；clip-to-fg auto 裁白纸（全幅不可与 v1.35 横比）。

## 5.9 剪影感知笔触生成（v1.37，2026-10-25）

**问题**：白底/带柔和光晕的 AI 插画（chibi 421×412 → work_res 1280）轮廓边缘大量溢出笔触/毛刺。
根因（Lead 实测，1280×1253 / 38,737 笔）：① fg=`~all(rgb>0.945)` 把抗锯齿光晕也算前景
（fg 43.6% vs 收紧后 40.4%）；② 笔宽越界 **5,677 条 >0.5px、3,223 条 >2px、max 16.97px**
（中位笔宽 7.0px）；③ 旧 `clip_to_fg` 只是最终合成硬切（fg 外置白），光晕/fg 内的越界笔仍在。
**要求用绘制算法修复（生成期几何裁剪），禁止后期修图。**

**方案（生成期，非后期）**：收紧剪影 `sil = distance_transform_edt(fg) > shrink_px`（小图保护：
sil 面积 < 0.2×fg 则 fallback fg + warn）→ 所有 pass/detail/dabs/texture 生成后、hard-cap 与
paint_order 之前对每条非 `base_toned` 笔做：**端点回缩**（去掉 `d_i < max(0.5, width_i/2 - 0.5)`
的端点，round cap 不出界；点数<2 丢笔）→ **逐点笔宽钳制**（`width_i = min(width_i,
2·max(d_i - margin, 0))`）→ **过细笔丢弃**（`max(width) < min_width`）→ 渲染时**剪影羽化合成**
（`alpha = clip(1 - m/feather, 0, 1)`，`ren = ren·alpha + 1·(1-alpha)`，替代硬切 fg）。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--silhouette {auto,on,off}` | **auto** | auto 仅在 v1.36 风格（wash + 非 legacy order + clip≠off + 白底>2%）启用；legacy 自动 off |
| `--silhouette-shrink-px` | **1.0** | 从 fg 收缩得到绘制剪影（去光晕/飞边），px@work_res |
| `--silhouette-margin-px` | **0.25** | 笔宽保持 `2·(dist - margin)` 内 |
| `--silhouette-min-width-px` | **0.6** | 钳制后最大宽度低于此 → 丢笔 |
| `--silhouette-feather-px` | **0.8** | 最终合成 alpha 羽化（渲染合成的一部分） |

**report 字段**：`silhouette_on / silhouette_area_frac / silhouette_shrink_px /
silhouette_feather_px / strokes_clipped / points_trimmed / strokes_dropped /
overshoot_before_max / overshoot_after_max`；旧字段全保留。

**实测 before/after（chibi 1280×1253；同参考掩码）**：

| 指标 | before（v1.36，`--silhouette off`） | after（v1.37 auto→on） |
|---|---|---|
| 笔数 | 38,737 | **34,107**（clipped 3,893 / trimmed 2,013 点 / dropped 4,630） |
| 越界笔（非底色 n=38,683→34,053） >0.5px / >2px | **5,636 / 3,182** | **0 / 0** |
| 非底色 max overshoot | 10.00 px | **0.25 px**（= margin 界限） |
| 越界笔（全部笔，含 54 条 wash 底色） >0.5 / >2 / max | 5,677 / 3,223 / 16.97px | 41 / 41 / 16.97px（全为未裁剪的 `base_toned`，由剪影合成遮住） |
| 污染 painted outside **sil** | 13,325 px | **0 px** |
| 污染 outside char（max≤0.88+opening） | 56,821 px / 3.54% | 40,976 px / **2.55%**（残余为角色半透明 AA 边） |
| PSNR（同参考 resize）/ report 口径 | 25.8273 / 25.0158 | 25.3655（−0.46dB）/ **25.3636（+0.35dB）** |
| SSIM | 0.88795 | 0.88562（≈不变） |
| report silhouette | — | on、area_frac 0.9768、fallback false、overshoot 9.999→**0.0** |
| 耗时 / fill / ≤100k | 49.5s / 0 / ✅ | **63.4s** / 0 / 34,107 ✅ |

**推荐命令（白底插画 / chibi）**：

```powershell
python skills/fourier-multistroke-training/scripts/layered_base.py ^
  --input work/chibi_clip/source.png --outdir <OUT> ^
  --work-res 1280 --coarse 20 --mid 12 --fine 6 ^
  --mid-err-pct 60 --fine-err-pct 45 ^
  --pass4 --pass4-err-pct 45 --pass4-spacing 6 ^
  --base-color-mode toned --base-color auto ^
  --upper-color-mode source --upper-blur 2.0 --upper-gradient ^
  --texture-flat --texture-flat-count 12000 --texture-flat-ref-std 0.015 ^
  --clip-to-fg auto --silhouette on
# 期望：34,107 笔 / 非底笔越界 0/0（max 0.25px）/ PSNR 25.3655 / 63.4 s
# 人像照片/全幅背景：无需剪影（auto 不触发），不要强行 on
```

**限制**：

- 依赖背景可分（白底 >2% 才 auto 触发；非白底可显式 `--silhouette on` 强制，但有裁错风险）；
- `base_toned` 54 条 wash 底色笔按规格跳过裁剪（由剪影合成遮住；如需可去掉 skip，成本 +54 笔）；
- 细特征可能变细：shrink=1.0 为本图实测（鲸尾/鳍完整）；觉得削薄可 `--silhouette-shrink-px 0.5`；
- 羽化让边缘更柔（同参考口径 PSNR −0.46dB）；更锐用 `--silhouette-feather-px 0.4`（略增碎屑）；
- 确定角色 mask 外残余 2.55%（before 3.54%）是角色半透明 AA 边，不是越界笔；
- 耗时 +14s（34k 笔几何裁剪 + EDT）；legacy 自动 off、v1.33/v1.34 逐字节不变。

**节点/独立验证（补充）**：ComfyUI 节点重建 `.fbs` 34,107 笔 / 151.3MB、2048 渲染无溢出；
analyst 独立口径（chibi 640）off >0.5px/>2px 4,816/2,018/max 9.996px → on 0/0/0.25px（232px
AA 残差）、PSNR 640 on +0.84dB；合成图 off 36 条 >2px → on 0；`--silhouette off` 重建 render PNG
SHA 与旧版完全相同；legacy v1.33 `30CDCE74…` / v1.34 `69820EEF…` 逐字节一致。

## 5.10 GPU 渲染（v1.38，2026-10-25）

**痛点**：CPU `fdrender --quality exact` 实测 chibi `.fdraw`（38,737 笔）**1024 = 219 s**、
**4096 ≈ 21 min**；视频/高分辨率更慢。用户要求 GPU 加速。

**方案**：`scripts/gpu_render.py`（在 **ComfyUI python** 下运行；该解释器自带 PyOpenGL 3.1.10 +
glfw 2.10.0、桌面 GL 4.6）——OpenGL 离屏 FBO + GLSL 胶囊光栅化：采样复用
`fbs_core.render.sample_stroke`；每段 capsule 2 三角形 + 片元着色器
`cov=clamp(r+0.5-dist,0,1)*alpha`；按绘制顺序 alpha blend 到 supersample FBO → glReadPixels →
PNG；输出计时 JSON（load/sample/geometry/gpu/readback/total）。支持 `.fdraw/.fbs/strokes JSON`；
gradient 逐段近似。ComfyUI `RenderFdraw` 默认自动 GPU，失败/无 GL 自动回退 CPU fdrender exact；
`FDP_GPU_RENDER=0` 强制 CPU；workflow/widget 不变，`PACKAGE_VERSION=3.0.1`。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--fdraw` / `--render-json` / `--fbs` / `--strokes` | — | 输入四选一（`.fdraw` 优先） |
| `--out` / `--resolution` | — | 输出 PNG / 长边分辨率 |
| `--supersample {1,2,3}` | **2** | SSAA 倍数 |
| `--background` | 白 | 背景色 |
| `--device {gl,cpu}` | **gl** | 无 GL/PyOpenGL/上下文时退出码 **3**（供 fallback） |
| `--width-scale` | 1.0 | 笔宽缩放 |

**实测（chibi `.fdraw`；ComfyUI python + RTX 4070 Ti SUPER）**：

| 分辨率 | GPU 总耗时 | CPU `fdrender exact` | 加速比 |
|---|---|---|---|
| 1024 | **14.2 s**（内部 13.49） | 219 s | **≈15.4×** |
| 2048 | **15.1 s**（14.36） | 581.6 s | **≈38.5×** |
| 4096 | **23.6 s**（22.70） | ≈1260 s（~21 min） | **≈53×** |
| 512（冒烟） | 15.4 s | — | — |

GPU 阶段拆分（load/sample/geometry/gpu/readback）：1024 3.64/8.27/0.50/0.11/0.34；
2048 3.54/7.88/0.90/0.32/1.15；4096 3.78/10.13/1.81/1.20/5.14。**GPU 光栅化仅 0.1–1.2s**，
耗时主要在 CPU load+sample（~80%）。analyst 独立复跑 14.5/16.5/23.3s 一致。

**推荐命令（高分辨率出图，GPU）**：

```powershell
# 在 ComfyUI python 下运行（自带 PyOpenGL 3.1.10 + glfw 2.10.0 / 桌面 GL 4.6）
python skills/fourier-multistroke-training/scripts/gpu_render.py ^
  --fdraw <x.fdraw> --out <out_4096.png> --resolution 4096 ^
  --supersample 2 --device gl --timing-json <timing.json>
# 期望 4096 ≈21.9–27.6s、2048 ≈15.1–19.4s、1024 ≈14.2–16.8s（不同次测量/并发区间）；无 GL 时 exit code 3
# CPU exact 参考：fdrender --quality exact（慢）；ComfyUI 无需改 workflow
```

**质量与回退**：GPU vs CPU exact 同分辨率 PSNR 1024 **40.219/0.99566**、2048（代理）
**41.9643/0.99623（verifier 原生 2048）**、40.542/0.99458（早期代理）、4096 **44.109/0.99724**
（门槛 >32dB）；与源图差异 ≤0.11dB/0.004，差图仅笔触边缘 AA、平坦区差 ≈0（均值差 0.15/255）；4× 视觉（发丝/鳍边/蝴蝶结/裙摆渐变）无新锯齿/漏笔。`RenderFdraw` GPU
成功（1024 端到端 **17.1s**）、`FDP_GPU_RENDER=0` 回退 CPU 成功（256px 99.8s）、模拟无 GL 自动
回退；workflow JSON/widget 数不变；`PACKAGE_VERSION=3.0.1`；repo/部署 `nodes.py` hash 一致
（`8FE41719564CDAA5…`）。GPU 退出码：**3**=无 PyOpenGL/GL；**5**=alpha<0.999 文档；
**4**=region_fill 文档（节点自动回退 CPU）。当前流水线输出 alpha=1，不受影响。

**限制**：需要 PyOpenGL+glfw+桌面 GL 上下文（headless/服务器自动回退 CPU，exit 3 不崩）；
4096 显存 FBO≈525MB+实例≈347MB（16GB 实测通过，更大未验证）；gradient 逐段近似；
2048 原生 CPU exact 已由 verifier 补测（41.9643/0.99623）；CPU exact 仍是 legacy/canonical 参考
（GPU 非 bit-exact，alpha=1 边缘差 ≤0.42/255）；训练/评估渲染与视频渲染未改；无新第三方依赖；
瓶颈已转移到 CPU load+sample（~12s，占 80%）。

## 6. 文件结构

```
training/
├── input/Lvpi5.jpg                 源图（可写副本）
├── scripts/hatch_gen.py            纯笔触绘制原型/渲染器
├── scripts/hybrid_gen.py           质量轨生成器（base/tex/detail）
├── scripts/hybrid_gen2.py          长笔触实验版（对照）
├── scripts/run_train.py            多轮训练驱动（断点续训/每轮存图）
├── scripts/refine_best.py          最终脸部墨线精修
├── scripts/refine_visual.py        线条锥度 + 顺流发丝视觉精修
├── scripts/prune_strokes.py        笔画剪枝实验
├── scripts/compile_best.py         best → .fbs → 高分辨率 PNG
├── scripts/make_progress.py        轮次总表 + GIF
├── scripts/verify_delivery.py      交付合规校验 / 渲染复算 / SHA256
├── scripts/audit_rounds.py         每轮训练产物审计
├── scripts/make_video_circles.py   傅里叶圆过程视频（干净圆链版）
├── run_1408_q/                     1408 训练轨（主交付来源）
├── run_1024_q/                     1024 质量轨
├── run_1024/                       1024 平衡轨
├── refine_1408_r21/                r21 脸部墨线精修
├── refine_visual_r21/              r21 视觉精修（发丝/锥度）
├── refine_lean_r21/                r21 精简笔画集（17,228 笔）
├── pareto_r21/                     笔画数-保真度 Pareto 候选（16,541 / 14,839 笔）
├── final_r21/                      画质优先交付 .fbs / .fdraw / 2048-4096 PNG / 报告
├── final_lean_fix_r21/             默认交付（17,693 笔，补洞/暗线修复）
├── final_fix_r21/                  完整版补洞/暗线修复中间件
├── final_lean_r21/                 上一默认精简件（17,228 笔）
├── final_minimal_r21/              极简高分辨率交付（14,839 笔）
├── final_visual/                   上一版视觉精修件
├── final_1408_best/                r9 精修件
├── final/                          1024 备份交付
├── lean_q/                         14,136 笔精简对照
├── progress_1408/                  1408 训练过程总表与 GIF
├── progress_q/                     1024 训练过程总表与 GIF
├── video/                          傅里叶圆绘画过程视频 / 抽帧 / 复现说明
├── eval/                           skill 官方 1536 四维度量
└── VERIFICATION.md / ROUNDS_AUDIT.md / DEFAULT_DELIVERY.md
```
