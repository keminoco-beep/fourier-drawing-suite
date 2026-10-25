# 训练循环（9 小时 + 断点续训）

按用户要求：优化验证通过后启动**至少 9 小时**的训练循环，自动 checkpoint、断点续训、崩溃重启。

## 启动方式

```powershell
# 9.5 小时训练（含崩溃自动重启；内部 deadline 由 state.json 控制，重启会续训）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/train_loop_9h.ps1
```

```bash
# 单次运行（dry-run 用；--max-rounds 限制轮数）
python scripts/train_loop.py --input assets/out/user_chibi/chibi_whale.png \
  --outdir assets/out/user_chibi/train_loop_9h --eval-res 1024 --final-res 2048
```

## 每轮流程

1. **分区选择**：palette / contour 两种结构分区，按历史 J 的 UCB 选择（contour 是线稿闭合+区域生长版）；
2. **结构抖动**：对块面方向/颜色/线宽做小幅随机扰动 → 训练多样性；
3. **在线 RL（真渲染器 + 连续动作搜索）**：
   - 力度臂（保守/标准/大胆：宽度×alpha）由 UCB 选择；
   - 每笔再采样 `n_candidates` 个连续动作（位置偏移 ±4px、宽 ×0.8~1.2、alpha ×0.85~1.15、颜色 ±0.03），
     在真 `FloatCanvas` 上逐个局部评估，取误差最小者落笔；
   - ΔE<0（且超过阈值）才接受，否则撤回；
4. **次评分剪枝**：按每笔收益升序尝试删除 0%、10%、20%，同分辨率渲染评估，
   选 `J = PSNR − 0.02×(笔数/1000) − 0.01×(线段数/100)` 最大且 PSNR 损失 ≤0.15dB 的变体；
5. **记录与 checkpoint**：每轮写 `training_log.jsonl`、`state.json`（原子写）；
   best J / best PSNR 分别保存 `.fbs.json` + `.png` + `.json`；每 20 轮额外渲染 final_res 展示图。

## 产物（`assets/out/user_chibi/train_loop_9h/`）

| 路径 | 内容 |
|---|---|
| `state.json` | 轮数、deadline、best 指标、历史（断点续训用） |
| `training_log.jsonl` | 每轮：分区/规划笔数/接受/剪枝比例/笔数/线段/PSNR/SSIM/J/耗时 |
| `checkpoints/best_J.fbs.json|json|_R*.png` | 按 J 最优的笔触文档与渲染 |
| `checkpoints/best_psnr.fbs.json|...` | 按 PSNR 最优 |
| `last_rl_log.json|md` | 最近一轮 RL 日志（力度臂/连续动作分布/收益分位） |
| `summary.md` | 训练结束总结 + 每轮表格 |
| `chibi_whale_best_R2048.png` | 训练结束时的 best 展示图（final_res） |
| `DONE` | 正常结束标记（wrapper 据此停止重启） |

## 已验证（dry-run）

1 轮（eval 640）：seg=palette、规划 4164 笔 → RL 接受 2620 笔 → 剪枝 10% 后 **2358 笔 / 144 线段**，
PSNR 16.864、SSIM 0.732、J=16.803，单轮 52.6 秒。结构与日志/checkpoint 写入均正常。

## 注意

- 训练循环不修改 skill 代码；纯产出 checkpoint/日志；
- 若机器重启/会话断开：重新执行 `train_loop_9h.ps1` 会从 `state.json` 续训（deadline 保存于 state）；
- 训练期间可随时打开 `training_log.jsonl` 看进度。

## 无限训练模式（2026-10-19 起）

用户要求：**只要没有停止指令就一直训练**。

- 启动：scripts/train_forever.ps1（通过环境变量 FBS_TRAIN_OUTDIR / FBS_TRAIN_SEED / FBS_TRAIN_WORKERS 配置；内部 while (True) 循环，崩溃自动重启并断点续训）。
- 停止：在对应输出目录放一个 STOP 空文件（或直接告知助手），trainloop 会在轮次边界停训、渲染 best、写 summary/DONE 后退出。
- 训练评估默认：--eval-res 768 --eval-ss 1（训练/选笔用，速度优先）；最终交付图仍按 --final-res 2048 全质量渲染。
- 当前并行进程：主 	rain_loop_9h（seed 20261018）+ 	rain_loop_9h_w1（seed 111）+ 	rain_loop_9h_w2（seed 222），三个进程各自 checkpoint、共享同一 deadline（已改为 10 年后，实际无限）。
