# 结构感知笔触绘画（structpaint，实验）

用户要求：笔触要像人类画师——顺着结构走向；按部位（线条/涂色/尖锐细节）选笔刷；
先做局部实验并记录，再"学习"后画完整图。

## 流程

```
位图
 ├─ 结构分解（调色板区域 + 中轴线）→ 结构元素表
 │    flat   大块平涂（裙子/头发主体/皮肤）
 │    shade  柔和明暗（阴影/渐变/高光）
 │    detail 小结构（眼睛/纹样/纽扣）
 │    line_fine / line_thick  线条（轮廓/发丝/内部线）
 ├─ 局部 patch 实验（每个结构类 × 候选笔刷策略）→ experiments/*.json + *.md
 │    每类取若干 patch，对候选策略实际绘制（该 patch 内同类元素整体重绘），
 │    只在该类元素覆盖的像素上算 PSNR/SSIM（避免留白主导评分）
 ├─ UCB1 多臂老虎机：reward = 0.7×归一化PSNR + 0.3×SSIM，选每类策略
 └─ 完整作画：先铺大块 → 中景 → 线条/细节（layer 0 填充、layer 1 线条）
      平涂 = 硬圆底色笔沿区域中轴骨架铺满 + 可选低透明肌理笔
      明暗 = 底色 + 空气刷/水彩低透明
      线条 = 沿骨架的墨线笔（压力两端收、3 次平滑）
```

## 实测（chibi 鲸鱼娘，R=1024，1024×1002）

| 项 | 数值 |
|---|---|
| 结构元素 | flat 56 / shade 20 / detail 49 / line_fine 39 / line_thick 435 |
| 生成笔触 | 4164 笔规划（纯傅里叶笔触、0 填充多边形） |
| 在线 RL（真渲染器） | 接受 **2434 笔**（58.5%），决策画布 PSNR **16.13**、J=16.07 |
| 最终渲染 @2048 | PSNR **15.913 / SSIM 0.8138**（@1024 时 15.954/0.7569） |
| 局部实验 | 5 类 × 3-4 策略 × 3 patch；记录 `experiments/brush_policy_experiments.{json,md}` |
| 离线 UCB 选笔 | flat=`dry_flat`、shade=`basic_airbrush`、detail=`marker_chisel`、line_fine=`ink_fineliner`、line_thick=`ink_gpen` |
| 线条合并 | 中轴碎片 474 → 355 条（保守衔接：2.5px 端点 + cos≥0.72 + 单链 ≤220px） |
| 在线 RL 力度臂 | 各类"保守/标准/大胆"三臂；flat 偏保守、shade/detail 偏标准（见 stroke_rl_log.md） |

## 已知不足（诚实记录）

1. **区域分割粒度不够**：调色板区域只是颜色连通块，对动漫角色（发丝/裙褶/蕾丝）会把
   相邻结构并成大块，铺色笔触沿骨架走但形状控制不了 → 画面呈"水彩团块"。
2. **宽度取 2×DT 的骨架笔触会外溢**：圆头端帽 + 骨架分叉让笔触越出轮廓（画面外飞线/气泡）。
3. **线条重复描画**：分解出的中轴线由大量短分支组成，叠加后线条发毛、抖动。
4. **局部实验的 PSNR 仍偏低**（mask 内 6.5–7.3dB），说明候选策略在 110px patch 上
   差异不大——需要更贴近真实结构的 patch 协议（例如线条/平涂/尖端分别定制 patch 与评分）。
5. **在线 RL 已接入（stroke_rl.py）**：逐笔落笔→局部误差→接受/撤回，每类结构用 UCB 在线
   学习"用笔力度臂"（保守/标准/大胆）。当前 68.6% 接受率、训练画布 16.36dB；但 RL 的
   光栅化是软边近似（决策用），与最终 10 种笔形内核渲染仍有偏差 → 需把笔形纹理纳入决策模型。
6. 最终渲染 PSNR 与训练画布的差已从 0.46dB 降到 **0.22dB**（RL 已改用真渲染器 FloatCanvas）。
7. R=4096 测量实验：**不采纳**（源图 421px 是上限；放大后测量碎片化、笔画数暴涨）。
8. 封闭轮廓分区（contour）：R=1024 实测 13.27dB < palette 15.91dB → **验证失败未采纳**，
   代码与记录保留（见 references/resolution_survey.md）。

## 下一步（按优先级）

1. 用线稿 mask 做"封闭轮廓感知"的分区（anime 线稿把区域围起来，区域=轮廓内）；
2. 笔触宽度改由区域轮廓约束（signed distance + 端帽裁剪），避免外溢；
3. 线条合并：先把中轴分支按共线/端点桥接合并成整条长线，一笔画完；
4. 在线 stroke RL：每画一笔渲染一次，局部误差下降才接受（奖励=ΔPSNR/ΔSSIM），失败回退；
5. patch 实验协议：按结构定制（线条 patch 只评线条带、平涂 patch 只评平涂区域）。

## 运行

```bash
python scripts/structpaint_demo.py --input assets/out/user_chibi/chibi_whale.png \
    --out assets/out/user_chibi/structpaint_final2 --resolution 1024
```

产物：`<stem>_R1024.png`、`structure_map.png`、`experiments/brush_policy_experiments.{json,md}`、
`structpaint_report.json`、`<stem>.fbs.json`（纯笔触，无填充多边形）。
