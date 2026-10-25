# 训练循环规则（2026-10-19 起，自动训练）

用户指令：**继续训练循环，直到用户明确说停**。本文件定义循环的运行规则、评估门槛与停止方式；
后续所有训练进程都必须遵守。

## 1. 配置基线（已验收，不得随意改动）

| 项 | 值 |
|---|---|
| 图像 | `assets/out/user_chibi/chibi_whale.png`（421×412） |
| 结构/分区 | contour（线稿闭合 + 区域生长）；palette 作为探索备选 |
| Fourier 文档契约 | **target_resolution = 2048**（与交付一致） |
| 每轮评估渲染 | 1024 + supersample=1 |
| 最终交付渲染 | **2048 + supersample=1** |
| 目标函数 | J = PSNR − 0.02×(笔数/1000) − 0.01×(线段/100) |
| 算法开关 | 仅启用已验证项：F（口径统一）；A/B/C 已评估并**放弃**，不得启用；D/E/G 待评估，通过门槛后才可启用 |
| 禁止 | 任何图像后处理（区域裁剪/锐化/超分等）、外部模型 |

## 2. 进程与资源规则

- 默认 **3 个训练进程**：`train_final/main`、`train_final/w1`、`train_final/w2`（不同 seed）；
  16 逻辑核下最多再 +1，禁止超过 4 个。
- 每个进程独立 `state.json`（原子写、断点续训）、独立 checkpoints；
- 崩溃自动重启（`scripts/train_forever.ps1`，30s 间隔），重启从 state 续训；
- 磁盘可用 <10GB 时立即停训（防止 checkpoint 写爆盘）；
- 单轮耗时 >10 分钟视为异常：记录到 `errors.log`，连续 3 次则重启该进程；
- 训练运行期间**不修改算法代码**；任何改动必须停训、记录版本、重启后再继续。

## 3. 每轮与 checkpoint 规则

- 每轮写 `training_log.jsonl`（round/mode/seg/strokes/segments/PSNR/SSIM/J/耗时）；
- best 必须同时保存 `best_J.fbs.json` + `best_J_R1024.png` + `best_J.json`；
- 每 20 轮把当前 best 渲染一次 **2048** 存档（`chibi_whale_R2048.png`）；
- 每 50 轮用 `scripts/eval_variant.py` 对 best 做一次 2048 全指标评估
  （PSNR/SSIM/edge_sharp/近边误差/笔宽/alpha），追加到 `eval_history.jsonl`。

## 4. 晋升与淘汰规则（硬门槛）

候选提升为新的全局 best 必须**同时**满足：

1. PSNR ≥ 旧 best − 0.05；
2. SSIM ≥ 旧 best − 0.002；
3. 不允许"PSNR 提升但边缘锐度明显下降"：`edge_sharp_ratio` 相对旧 best 下降 >5% 的候选只能
   存为实验 checkpoint，不得晋升 best（避免 A 类"糊面换 PSNR"）；
4. 每 50 轮的 2048 评估中，若 `edge_sharp_ratio` 低于 F 基线（0.222）的 90%（<0.200），
   标记 `VISUAL_RISK`，暂停该进程并等待人工/用户决策；
5. 目视规则：每 50 轮检查三分区裁切（`eval_variant` 的 crops.png），若比 F 基线更"气泡糊"，
   该 checkpoint 不得晋升。

## 5. 巡检与自动恢复

- 每小时外部巡检一次：检查三个目录的 round 是否增长、日志年龄、DONE/STOP 标记；
  进程死且无 DONE/STOP → 自动重启（`train_forever.ps1` + 环境变量）；
- 巡检记录写 `train_final/monitor_log.md`（时间/round/best/动作）。

## 6. 停止规则

用户说"停"时：

1. 在三个目录各放一个 `STOP` 文件；
2. 等每个进程在下一轮边界退出并写 `DONE`/`summary.md`/最终 2048 best 图；
3. 不再自动重启（巡检看到 DONE/STOP 不重启）；
4. 汇总交付：最终 best 图（2048）、对比图、`summary.md`、`eval_history.jsonl`。

## 7. 交付物位置

`fourier-brushstroke/assets/out/user_chibi/train_final/`
- `main/`、`w1/`、`w2/`：各进程 state/log/checkpoints
- `main/summary.md`：训练总结（停止时生成）
- `eval_history.jsonl`：每 50 轮的 2048 全指标记录
- `monitor_log.md`：巡检记录

## 8. 当前阶段：纯细线绘画训练（2026-10-19 起）

用户确认「精细元素识别 + 纯细线绘画」方向可用，按此为基础训练：

| 项 | 值 |
|---|---|
| 生成器 | generator=finelines（fbs_core/finelines.py）：颜色量化(K=32-56) → 细色块 → 结构张量切向曲线细线 + 色块边界细线；**零填色** |
| 训练目标 | 真渲染器逐笔 RL 接受/淘汰；mutate 轮追加新细线(densify)并扰动颜色/宽度/位置；fresh 轮随机超参(K/step/width/min_patch)重新生成 |
| 评估/交付 | 每轮 1024/SS1；最终 2048/SS1 |
| 进程 | train_finelines/main、w1、w2（独立 state/checkpoint） |
| 晋升门槛 | 沿用 §4：PSNR/SSIM 不回退、edge_sharp 不得比旧 best 降 >5%；每 50 轮目视检查（留白是否过多、结构是否清晰） |
| 额外淘汰条件 | 细线总数 >9000 仍无法显著提升覆盖率(PSNR)，或 2048 目视出现白洞/噪点，该轮不晋升 |
| 停止 | 用户说停 → 各目录放 STOP → 等 DONE/summary/2048 best |

产物目录：fourier-brushstroke/assets/out/user_chibi/train_finelines/
