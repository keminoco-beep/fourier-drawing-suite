# 算法说明（多笔画纯傅里叶笔触训练）

## 1. 表示

每个元素是一条 **stroke**：

```
stroke = {
  repr: "fourier",
  coef: [ [k, re, im], ... ],          # z(t)=Σ c_k e^{i2πkt}
  arc_length, bbox, layer,
  brush: {
    style: <brush_styles index>,
    width: {interp, keys/...},          # 全宽，世界单位
  }
}
brush_style = {
  color: {interp, stops:[{t,rgb,a}, ...]},   # 颜色 + alpha
  alpha: {interp, keys:[[t,a], ...]},
  shape: {interp, keys:[[t,"round"|"ink"|...]]},
  jitter
}
```

- 文档 `mode="per_stroke"`：每条 stroke 各自一次 FFT；整图 = N 条曲线的有序集合。
- **没有 region_fill / color_field / mesh 元素**；铺色必须由宽笔或密集平行排线完成。
- **端点渐变笔**：stroke 可有 `"gradient":{"start":[r,g,b],"end":[r,g,b]}`，
  颜色按弧长 `u∈[0,1]` 线性过渡；`.fbs` brush color curve 用 2 个 stop 表达。
  这是替代大面积排线的原语：一条长宽笔覆盖一个沿主方向渐变的色区，而不是几十条细排线。
- 所有坐标归一化到长边 `[0,1]`；`target_resolution` 决定 N 和误差契约，不决定几何坐标。
- 交付前断言 `fill_count == 0`、`stroke_count <= max_curves`、`.fdraw` validate 通过。

## 2. 生成：hybrid_gen 的分层画法

输入图先做两套颜色分割：

1. **粗分割**（`base_blur≈4–8`, `base_k≈8–16`）→ 大色区；
2. **细分割**（`tex_blur≈1.5–3`, `tex_k≈24–40`）→ 小色块/细节。

各层：

| 层 | 名称 | 实现 |
|---|---|---|
| 0 | 铺色 base | 每个粗色区按 PCA/结构张量方向做**扫描线排线**；宽 6–16px、间距≈0.48×宽；压力两端 0.68×；颜色取局部源图中位数 |
| 2 | 纹理 tex | 细色区短排线，宽 2–6px、间距≈0.9×宽、alpha≈0.8 |
| 3 | 高光/白回收 | 小亮块白笔（可选，易过绘，默认不启用） |
| 5 | 细节线 detail | 源图梯度/暗墨骨架 → skeleton graph → 长线；宽 1.35–2.2px、锥度收尾 |
| 6 | 补洞 fix | 渲染误差 > 分位数的近白洞 → 局部短排线；alpha 1 |
| 7/8 | 脸墨/发丝 | 脸部暗边深色线；蓝色 hair mask 内沿结构张量切向的长曲线（550 笔，宽 2–4px，alpha≈0.72），打散量化色块 |

生成器不是逐像素抄色：每笔颜色取该尺度的源图中位数/局部样本，笔宽和间距控制覆盖。

## 3. 训练循环

每轮：

1. 从 best 参数变异 1–3 个字段（`mutate`），或每 N 轮 fresh 探索；
2. `hybrid_gen.generate()` → strokes；
3. 自定义 canvas 渲染（2× 超采样，本地 capsule）；
4. `fix_strokes()` 做 1–2 轮误差补洞；
5. 变体评估：all / no_tex（省笔）；
6. `score = PSNR + ssim_weight*SSIM − stroke_weight*(strokes/1000)`；
   质量优先 `(0.02, 1.0)`；平衡 `(0.10, 1.5)`；
7. 只把更高 J 的笔触集写 best；每轮写 `rounds/round_XXXX_R{R}.png` + report + state；
8. STOP 文件在轮边界退出；支持断点续训。

参数变异范围（`run_train.py` 内）：
`base_blur 3–10 / base_k 6–16 / base_min_area 60–400 / base_width 3–22 /
base_spacing 0.36–0.70 / tex_k 16–48 / tex_width 1.5–10 / tex_spacing 0.65–1.3 /
tex_alpha 0.45–1.0 / edge_percentile 90–97.5 / edge_width 0.9–2.6 /
edge_dark 0.55–1.05 / max_edge 500–2600 / color_jitter 0–0.04`。

## 4. 精修

1. `refine_best.py`：脸部强梯度/暗边骨架 → 深色细线（~260–300 笔），带锥度；PSNR 通常 −0.05~−0.08，视觉更清楚。
2. `refine_visual.py`：
   - 对 detail/faceink/outline 的宽度乘 `0.62 + 0.55*sin(πt)^0.55`（两端细、中段满）；
   - 蓝色 hair mask 内由结构张量追踪 ~550 条长流线，宽 2.2–4.2px、alpha 0.72；
   - 这一步在 1408 上常见 PSNR +0.0~+0.2、SSIM +0.002~+0.006，并显著减轻发顶条带。
3. `prune_strokes.py`：按 `length*width` 从低到高剪掉非线笔（保护 detail/faceink/hairflow/outline），
   再补洞；产出省笔 Pareto 档（如 17.2k→14.8k 笔，2048 PSNR 18.36→17.96）。

## 4.4 低色差纹理 + 去抖动

**问题**：色差很小但纹理存在（向日葵花盘纤维、厚涂肌理），普通梯度/颜色边缘阈值抓不到；
orientation 流线在叶尖/花瓣尖会卷曲，Fourier 拟合把每个抖动都保留下来。

**纹理 pass（refine_detail.py 的 oil_texture）**：
1. 灰度 `gray`，高通 `hp = gray - blur(gray, 3px)`；
2. 局部能量 `E = blur(hp^2, 2px)`，按 `E` 的 58 分位取掩码（不看颜色差）；
3. 对 `hp` 做结构张量，得到纹理方向；
4. 每 3px 种种子，追 10–30px 短曲线，平滑后作为 `oil_texture` 笔（宽 0.55–1.0px，alpha 0.88）；
5. 数量 `9000 × megapixels`。

**去抖动（clean_strokes.py）**：
1. 所有笔画按弧长重采样到 ≤16 点；
2. 3 次 `[1,2,1]/4` 平滑；
3. 计算总转角与单步转角；对非 `base/tex/fix` 的细节笔，超过
   `max-turn=2.2 rad` 或 `max-step-turn=0.75 rad` 的丢弃（卷曲/打结）；
4. 保持大色块/基础笔不动，重新渲染评估。

## 4.45 粗短笔画层（thick_dabs.py）

油画厚涂（麦田、天空、土路）需要短而粗的方向笔；已有的长流线/细排线不能替代。

1. 4px 抖动网格撒种，只保留 fg；
2. 取局部结构张量切向，叠加 ±0.22 rad 抖动；
3. 每笔 3 点，长 3–9px、宽 3–8px，端部宽度 0.78×；
4. 颜色取三点源图中位数 + 极小噪声，alpha 0.94；
5. 层 4（base/tex 之上、detail 之下），**增量添加**，旧笔全部保留。
6. 数量按画面复杂度给（示例 18k）；与 4GiB 上限配合，总笔数 47k 时 `.fbs` ≈1.7GB。

**宽幅/竖幅渲染修复**：所有 producer 用长边 `L=max(H,W)` 归一化 x/y；
`hatch_gen._render_strokes` 也必须用 `L` 同时缩放两轴（旧版用 W/H 分轴，方形时等价，
宽幅会把 y 压扁）。修复后 2959×1417 宽幅可直接训练，无需补白。

## 4.46 柔化/雾感模式（soft preset + atmosphere + glaze）

低对比、雾感、印象派画面（莫奈）的瓶颈不是细节，而是硬分割/排线破坏大气层。

1. `run_train.py --preset soft`：base 宽 8–30px、blur 6.5–10、edge 阈值 95.5–98.5、
   tex alpha 0.40–0.65，减少硬边界；
2. `soft_atmosphere.py`：σ=2/5/10 三层模糊，宽 8/14/24px、alpha 0.42 的宽色带，
   layer 1/2 做底色统一；
3. `soft_glaze.py`：在细节层下方（layer 3）加宽 20–45px、alpha 0.35 的柔光罩，
   压掉斜向排线同时保留船/太阳/倒影；
4. 细节 pass 只保留低数量（scale≈0.35），clean 去抖动；
5. `analyze_input.py` 用 contrast<0.16 或 soft_score>0.55 自动选 soft 模式。

莫奈《日出·印象》实测：soft base 25.88/0.550 → +atmosphere 26.86/0.577 →
轻细节 27.09/0.582 → clean 26.93/0.580 → 细节下 soft glaze **27.87/0.641**（2048 训练渲染）。
最终 `.fbs` 2048 渲染 26.29/0.5915；残余差距是 skill 渲染器在高分辨率下会暴露底层排线。

## 4.47 一键本地流水线（pipeline.py / analyze_input.py）

`pipeline.py` 串起本地分析、训练、细节/柔化、清理、校色、编译、打包、校验；
输出 `pipeline_summary.json`。`analyze_input.py` 只读图像统计，不调用 LLM，
自动推荐 preset/work_res/rounds/细节数量。agent 只需一次调用，减少 token 与工具往返。

## 4.48 少笔 / 去纹路（sparse_precision.py）

笔画预算的三个来源：base 扫描线（约 74%）、tex（约 10%）、fix/detail（约 16%）。
大面积色块纹路主要来自 base/tex 半透明胶囊边缘重叠 + fix 小排线。

`sparse_precision.py`：

1. 保护 `detail/outline/faceink/boundary/hairflow`；
2. 非线笔按 `length × width` 从弱到强删到 `--target-keep`；
3. 大平坦区（低梯度、低色方差）base/tex 改 `alpha=1`、宽度 +10%，删该区 fix/detail；
4. 只在**高梯度残差**区域补少量 `precision_repair`。

实测（19,656 笔 @1408）：keep .75 → 15,437 笔 / 18.20；keep .60 → 12,890 笔 / 17.76；
原 baseline 18.73。即“删笔”能减 21–34%，但有 0.5–1.0dB 代价。

`soft_glaze.py`（宽 15–30px、alpha .25）压纹路：19,656 → 21,815 笔，PSNR 18.84 / SSIM .596，
比 clean 18.73 / .587 更好——**加宽笔比删笔更能去纹路**，但笔数增加。

结论：两者目标冲突。默认交付保留完整笔；要少笔用 sparse，要去纹路用 glaze。
真正同时解决需要“少而宽、逐点颜色渐变的 broad strokes”：目前 fbs 支持 color curve，
训练渲染器只支持单色，下一步扩展逐点颜色后可用一条宽笔替代整片扫描线。

## 4.49 上层渐变安全门（`layered_base.py`，v1.33）

**问题**：`--upper-gradient` 的端点色直接取局部模糊源色（pass2 σ=2.5、pass3 1.75、pass4 1.125），
笔中心单点查 mask、宽笔可跨到边界另一侧；两端落在不同色区时，一条笔会把“边界两侧颜色”
沿弧长插值成彩色楔形/糊斑。v1.32 canonical 输出 57,976 条渐变笔中，端点 Δ>0.12 有 5,329 条
（Δ>0.15 3,160、>0.2 1,417、最大 0.6875）。

**门控（默认开，`--gradient-gate on`）**：在 `tile_pass()` 的 gradient 分支内、写渐变之前判定：

1. 沿笔轴取 **9 个路径采样点**（最近像素），任一落在前景外 → 拒绝（`rejected_oob`）；
2. 端点色差 `Δ = max_c abs(src(start)_c − src(end)_c) > --gradient-max-delta`（默认 **0.12**）
   → 拒绝（`rejected_delta`）；**无平滑 ramp 例外**，落盘 jitter 后的存储端点同样受限；
3. 相邻采样色跳 `max_k ∞norm(src_k − src_{k+1}) > --gradient-edge-delta`（默认 **0.10**）
   → 拒绝（`rejected_edge_color`）；
4. 若 `--gradient-edge-pct` <100（默认 **100.0 = 关闭该子门**），采样点命中高梯度 edge-map
   `max_filter(|∇gray|, 5) > P_pct(grad[fg])` → 拒绝（`rejected_edge_grad`）。

被拒笔退化为“**有效采样均值单色笔**”（不带 `gradient` 键，保留约 0.5(j0+j1) 抖动），
笔几何不变；`--gradient-gate off` = 完全跳过子门 = 旧行为（逐字节）。RNG 消耗与门控无关，
所以 gate on/off 的前两 pass 笔几何一致，可做公平 A/B。

**report 缺陷指标**（6 个冻结字段 + 诊断字段）：

| 字段 | 定义 |
|---|---|
| `gradient_strokes` | 实际带 gradient 输出的笔数（= accepted） |
| `gradient_rejected_delta` | 因端点 Δ>max-delta 被拒的笔数 |
| `gradient_rejected_edge` | 因边界判据（相邻色跳 / 高梯度像素）被拒的笔数（两子项之和） |
| `gradient_mean_delta` | **候选笔**端点 Δ 均值（源色口径，0–1） |
| `gradient_max_delta` | 候选笔端点 Δ 最大值 |
| `stray_color_px` | 前景内 `max_c abs(ren−rgb)>0.25` 的像素数（终图 vs 参考，全层） |

诊断字段：`gradient_candidates`、`gradient_rejected_oob`、`gradient_rejected_edge_color/grad`、
`gradient_accepted_mean_delta`、`gradient_mean_step/max_step`、`gradient_by_pass`、
`gradient_edge_frac`、`stray_color_frac`、`gradient_gate_params`。
恒等式：`candidates = accepted + rejected_delta + rejected_edge + rejected_oob`。

**实测（canonical 1536，同参数同 detail）**：81,815 → 81,010 笔；接受率 89.1%（50,950/57,171）；
输出渐变笔端点 Δ>0.12/>0.15/>0.2 = **0/0/0**、Δ max 0.1200（before 5,329/3,160/1,417、0.6875）；
全幅 PSNR 23.1883→23.1059（float）、内容区 doc 协议 24.5058→24.4302；耗时 99.2→97.1 s。
`--gradient-edge-pct 90` 对照：接受率 15.7%、全幅 PSNR 23.0642（默认关 edge-map 子门的依据）。

**限制**：门控只消掉“跨色边界的彩色渐变”，不解决红圈 A 的糊斑——压平全部 57,976 条渐变笔后
A/B ROI mean|Δ| 仅 0.0052/0.0031，视觉主体来自 K8 宽笔几何 + 调色板区域边界 + pass2/3/4
只局部覆盖；红圈 B 楔形区 mean|Δ| 0.1768→0.1602（局部减轻）。
后续（**未实现、未验证**）：结构区减小 pass2 笔宽、提高 pass3/4 覆盖、色边界拆笔；
详见 `SKILL.md`「上层渐变安全门（v1.33）」与 `training/sunflowers/GRADIENT_SAFETY_FIX.md`。

## 4.50 细纹理层 texture-flat（`layered_base.py`，v1.34）

**问题**：pass1 底色笔是“整块平面色”（宽 1.5×coarse、长 2×coarse，颜色取 palette median 或
source 三点中位色）；Pass2/3/4 只在误差分位以上补笔，低误差的平涂区（Sunflowers A 区）
从未被打散 → 观感“颜色分层/平色块”。v1.33 after 的 A 区：3×3 luma std<0.01 占比 49.1%
（reference 3.8%）、≥50px 平色块 103（ref 2）、16 级亮度 run_y 5.99（ref 2.43）。

**texture-flat pass（默认 off，opt-in）**：在 detail 合并后、最终 report 前执行：

1. 渲染当前 strokes，计算 3×3 uniform_filter 的 luma std；std < `--texture-flat-lstd`
   （默认 0.01）的像素为“平坦候选”；
2. 候选按 `--texture-flat-spacing`（2.5px@1280，按 rs=max(H,W)/1280 缩放）网格 + 抖动取点；
3. 若 `--texture-flat-ref-std` > 0，候选还需参考图 3×3 luma std ≥ 该阈值（推荐 0.015–0.02），
   并按参考纹理强度降序分配预算（保证高纹理平坦区先拿到笔）；
4. 每点生成一条细短笔：长 `len-min/max`、宽 `width-min/max`（5–9 / 2.5–3.5px@1280，按 rs 缩放），
   方向取参考结构张量 + 抖动，颜色 = 参考图局部均值 + `--texture-flat-jitter`（0.01），alpha=1，
   `layer=19`（detail 之上、最顶层 20 之下），kind=`oil_flat_texture`；
5. 追加后重渲染；总笔数受 100000 硬上限约束（超预算截断并 WARNING，
   report `texture_flat_params.capped_100k`）。

**配套 pass1 底色软化（可选）**：`--base-color-mode source` + `--base-blur 4.0`（pass1 源色高斯模糊
后取三点中位色）、`--base-gradient`（pass1 端点渐变，走 v1.33 门控）、
`--base-width-factor/--base-length-factor`（默认 1.5/2.0 = 旧硬编码）。source 底色单独能把
A PSNR 24.06→~24.54、blocks 103→53，但 flat_frac 反而升高（更平滑），所以必须叠加 texture-flat。

**report 字段**：`texture_flat_strokes`（实际输出纹理笔数）、`texture_flat_candidates`、
`texture_flat_params`（requested/emitted/candidates/budget/ref_std_thr/spacing_px/
flat_frac_fg/capped_100k 等）；另有 `base_color_mode/base_blur/base_gradient/
base_width_factor/base_length_factor`。默认关闭时这些字段为 0/不变，旧行为逐字节一致
（256 渲染 md5 `5CEDAAC084511CA7`）。

**实测（canonical v1.34，Sunflowers 1536）**：99,929 笔 / 105.6 s；A 纹理笔 2,320（覆盖 74.9%）；
A PSNR 24.0588→24.7211、blocks 103→74、flat_frac .491→.437、run_y 5.99→5.45、uniq16 183→195；
全幅 23.0791→24.3322、内容 24.4302→24.8173、B 26.9247→28.0935。
预算：去 pass4（约 24k 笔）+ source base（62,929 笔），100k 上限剩 37,071 → texture count 37,000。

**限制**：blocks 74>65、largest_flat_fraction 0.064→0.112（唯一反向）、grad_x/uniq16/flat_frac
三项 SHOULD 未达；纹理偏糊（texture_ratio 0.615 vs 0.62）、4× 可见“梳状”细纹；
100k 上限下 A 区只拿到 2,320 条是根本瓶颈。改进（**未实现、未验证**）：提高曲线上限、
区域自适应密度、更细 detail 替换部分 pass3。通用性：只在 Sunflowers 1536 实测；纹理笔颜色来自
参考图、风格化输入应关闭；去 pass4 对依赖它的图可能掉色准。详见 `SKILL.md`
「细纹理层（texture-flat，v1.34）」与 `training/sunflowers/COLOR_BANDING_FIX.md`。

## 4.51 人类画师式底色与 100k 硬上限（`layered_base.py`，v1.35）

**原则**：底色只允许纯色；一个区域只涂一次；底色不得呈现原图草图/多色草稿；画面由正式绘制层
承担。旧 `quantized`/`source` pass1 按原图局部取色 + 多遍重叠铺满（v1.33 底 4,543 色/重叠
99.9%、v1.34 底 12,544 色），底色本身就是原图模糊草图 → v1.35 改默认 `toned`。

**`base_coat_pass()`（toned）**：`--base-coat-spacing`（0→`--coarse`）确定平行行带；每带一条长笔
覆盖到 fg 边界（首末行内收 0.5px 保证栅格全 alpha）；笔宽 = pitch × `--base-coat-width-factor`
（默认 1.04；栅格门禁 overlap>5% 时自动收窄重测）；`--base-coat-jitter` 只沿笔方向抖动（不开缝）；
`--base-coat-angle` 控制行扫方向（0=水平、90=垂直）。颜色由 `_parse_base_color()` 解析
`--base-color`（`auto`=整幅图像逐通道中位色 / `#RRGGBB` / `"r,g,b"` 0–1 或 0–255），全底同色；
笔 kind=`base_toned`、layer=0；**无 gradient、无逐笔颜色抖动、无局部取色**、alpha=1.0。
`none`：不生成底色（0 笔；report coverage/overlap=None、color_std=0.0）。
legacy `single|quantized|source` 走原分支不动，仅用于逐字节复现历史产物。

**`_base_raster_metrics()`**：用渲染器逐笔 magic 色（G<0.5 标记）计数，输出前景口径
coverage/overlap（>1、>2 层像素）、max_count、canvas 口径 overlap。实测：稠密画面覆盖 1.0000、
overlap 3.19–4.00%、maxcnt=2；Sunflowers toned 底座 77 笔 1 色 std=0、覆盖 1.0、重叠 0.0。

**正式绘制默认**：`--mid-err-pct/--fine-err-pct/--pass4-err-pct` 82/72/60 → **60/45/45**，
Pass2/3/4 承担 97–99% 笔数；显式传参的旧命令不受影响。

**100k 硬上限（`--max-curves` 默认 100000）**：所有层 + detail 合并后超限时，按
**texture-flat → pass4 → pass3 → pass2** 顺序确定性均匀抽稀（`np.linspace` 等距保留），base 底色
与用户 detail 仅在 cap 小于其自身时最后兜底；抽稀后重渲染、重算指标与层笔数，report 写
`max_curves / capped_100k / cap_adjustments`（每层 `removed`/`frac`）/ `pre_cap_strokes`。
实测：纯默认 37,843（不 cap）；默认+`--pass4` 109,548→100,000（base_pass4 removed 9,548、
frac 0.1332；pass2/pass3 全保留）。

**report 字段**：`base_color_mode / base_color / base_coat_angle / base_coat_spacing /
base_coat_width / base_coat_width_factor / base_coat_width_factor_requested / base_coat_jitter /
base_strokes / base_coverage / base_overlap_frac / base_overlap_canvas_frac / base_color_std /
base_gradient_strokes`；硬上限字段同上。旧字段全保留。

**实测/限制**：Sunflowers toned 89,205 笔：全幅 21.7959/0.60797、内容区 24.5205/0.51072、
A 24.309（全幅不可与 v1.33 横比：正式层笔越界污染白边 mean 0.9777→0.9701/0.9637；doc 内容区
Lead 复算 24.511/0.5351，主体不降）。线稿 `none` 96,636 笔：IoU 0.8492/recall 0.9755/
precision 0.8677/PSNR 18.276/SSIM 0.776（旧 source 底：IoU 0.7369/precision 0.7574/PSNR 14.437）。
toned 单独时 A 区 banding 回退（largest flat 6.4%→44.7%）→ 油画场景叠加 `--texture-flat`。
稀疏主体（线稿）toned 的 fg overlap 偏高，推荐 `none`；行带几何对非凸/多孔主体会涂带内空白
（只保证覆盖主体）；`--base-color auto` 对白边多的图偏亮，可显式指定；抽稀不重新采样，
被裁层轻微稀疏、`gradient_by_pass` 未回算（仅诊断）。详见 `SKILL.md`「人类画师式底色（v1.35）」
与 `training/sunflowers/UNDERPAINT_PRINCIPLE.md`。

## 4.52 人类画师式画法：wash / 顺形体 / 区域顺序（`layered_base.py`，v1.36）

**问题**：v1.35 底色是 77 条水平行扫长笔；正式层用轴对齐 tile 网格按行生成；笔序列按 layer/y
排序 → 全画布多轮扫描感（`full_canvas_rounds=5`、layer1/2/3 y-Spearman=+1.000）。v1.36 改画法：

1. **`base_coat_pass()` wash 分支**（`--base-coat-pattern wash`，默认）：按 `--wash-angle`
   （-22.0°）生成少量大宽笔行带（每带 1–3 条长笔），带内方向随带变化（非水平/垂直）；
   仍单一纯色、无 gradient/颜色抖动/局部取色、一次薄涂（覆盖 ≥99%、overlap fg 3.7–5.4%）。
   `rows`=v1.35 旧行扫几何（legacy 兼容）。
2. **`flow_direction_metrics()` + 正式层 flow 几何**（`--flow-strokes on`，默认）：正式笔方向
   取源图结构张量切向（低相干时区域主方向/±抖动）；report `flow_orientation_frac`
   （笔轴与切向 ≤20° 比例）、`axis_aligned_frac`（≤10° 于水平/垂直比例）、`direction_strokes`。
3. **`paint_order_indices()`**（`--paint-order region`，默认）：先按空间 k-means
   （`--region-count` 12）分配 region id，区域顺序按质心最近邻路径，区域内按 layer
   （block-in→fix→detail→texture），同层按大小降序；渲染时按该顺序重排并赋 layer=序号
   （最终图与视频顺序一致）。report `paint_order/paint_order_effective/order_y_spearman/
   order_adjacent_median_px/order_region_run_frac/region_count_used`。
4. **legacy 自动回退**：显式 legacy 底色模式且未传 flow/paint-order 时自动
   flow=off + paint_order=legacy + pattern=rows → v1.33/v1.34 旧命令逐字节复现。

**report 字段**：见 SKILL.md「人类画师式画法（v1.36）」；v1.35 底色字段与 v1.33/v1.34
渐变/texture 字段、`capped_100k/cap_adjustments` 全保留。

**实测与限制**：Sunflowers 1536 原生、100,000 笔、178.6 s：wash 71 笔纯色 coverage 1.0 /
overlap 0.0713（analytic）；full 25.2338/0.62321（白边已修）、content **24.1320（doc 协议；
冻结基线 runE_dabs 24.6184 → Δ−0.486dB，超 ≤0.4 线 0.086 → CONDITIONAL）**、square 24.1985；
A 24.115/blocks 65/largest_flat 0.416（v1.35 runE_dabs 24.625/64/0.222 → A 回退）；B 27.4957（v1.35 28.149）；
flow_orientation 0.9086、axis_aligned 0.2663、order_y_spearman 0.197、region_run 0.9993。
analyst 独立核算：wash 底 axis≤10° 1.000→0.183、扫描层数 3→0、layer 块连续性 1.000→0.121、
簇 span 0.974→0.548、formal y-Spearman 0.201 → 顺序/扫描性 PASS；compactness 0.085→0.201
仍低于目标 0.25（区域推进仍有前沿）。限制：质量 CONDITIONAL（doc −0.486dB 超线 0.086；runE 24.5367 为无 dabs 错口径勿引用）、A largest_flat 0.222→0.416、A blocks 64→65、
运行 178.6s 未达 ≤120s、>768px base 指标为 analytic 口径、flow_align 两口径
（report 0.9086 / analyst 0.819）、clip-to-fg auto 裁白纸（全幅不可与 v1.35 横比）、
wash 为对角主扫+带内弓形（非任意交叉）。详见 `SKILL.md`「人类画师式画法（v1.36）」与
`training/sunflowers/HUMAN_PAINT_ORDER.md`。

## 4.53 剪影感知笔触生成（`layered_base.py`，v1.37）

**问题**：白底 AI 插画有柔和光晕/抗锯齿边，旧 fg 掩码把它算前景；宽笔在光晕区取样，笔宽
（中位 7px）超出剪影 → 边缘毛刺/溢出（chibi 1280：5,677 笔越界 >0.5px、3,223 笔 >2px、max 16.97px）。
`clip_to_fg` 硬切合成只能裁掉 fg 外像素，修不掉 fg 内/光晕区的越界笔。

**算法（生成期，非后期）**：

1. **剪影**：`sil = distance_transform_edt(fg) > --silhouette-shrink-px`；若 `sil.mean() <
   0.2*fg.mean()`（小图/细特征）fallback 到 fg 并 warn；report `silhouette_on/area_frac`。
2. **笔触几何裁剪**（所有层生成后、hard-cap/paint_order 前；对非 `base_toned` 笔）：
   - 端点回缩：循环去掉两端满足 `d_i < max(0.5, width_i/2 - 0.5)` 的点（round cap 不出界），
     点数 <2 → 整笔丢弃；
   - 逐点宽钳制：`width_i ← min(width_i, 2·max(d_i - margin_px, 0))`；
   - 过细丢弃：钳制后 `max(width_i) < min_width_px` → 整笔丢弃；
   - 统计 `strokes_clipped/points_trimmed/strokes_dropped` 与 `overshoot_before/after_max`
     （overshoot = width/2 − d_i，px）。
3. **渲染合成**：`m = distance_transform_edt(~sil)`，`alpha = clip(1 - m/feather, 0, 1)`，
   `ren = ren·alpha + 1·(1-alpha)`（属渲染合成的一部分，替代原硬切 `np.where(fg,...)`）。
4. **auto 语义**：`base_coat_pattern=wash` 且 `paint_order≠legacy` 且 `clip_to_fg≠off` 且白底
   占比>2% → on；legacy 模式自动 off（v1.33/v1.34 逐字节不变）。

**report 字段**：`silhouette_on/silhouette_area_frac/silhouette_shrink_px/silhouette_feather_px/
strokes_clipped/points_trimmed/strokes_dropped/overshoot_before_max/overshoot_after_max`。

**实测 before/after（chibi 1280×1253，同参考掩码）**：非底色笔（n=38,683→34,053）越界
>0.5px **5,636→0**、>2px **3,182→0**、max overshoot **10.00→0.25px**；全部笔口径
（含 54 条未裁剪 `base_toned` wash 底笔）5,677/3,223/16.97px → 41/41/16.97px；painted outside
sil **13,325→0 px**；确定角色 mask（max(rgb)≤0.88+opening）外污染 56,821px/3.54% → 40,976px/
**2.55%**（残余为半透明 AA 边）；PSNR 同参考口径 25.8273→25.3655（−0.46dB，羽化）、report 内部
口径 25.0158→**25.3636（+0.35dB）**；SSIM .88795→.88562；笔数 38,737→34,107（clipped 3,893 /
trimmed 2,013 点 / dropped 4,630）；耗时 49.5→63.4s；report silhouette on、area_frac 0.9768、
fallback false、overshoot 9.999→0.0；legacy v1.33 `30CDCE74…` 逐字节不变。

**限制**：依赖背景可分（白底 >2% 才 auto 触发；非白底可显式 on，有裁错风险）；`base_toned`
54 条 wash 笔按规格跳过裁剪（由剪影合成遮住，如需可去掉 skip，+54 笔成本）；细特征可能变细
（shrink=1.0 本图鲸尾/鳍完整，觉得削薄可 0.5）；羽化让边缘更柔（feather 0.8→PSNR −0.46dB，
更锐可 0.4，略增碎屑）；确定角色外残余 2.55% 是 AA 边非越界笔；耗时 +14s。详见
`SKILL.md`「剪影感知笔触生成（v1.37）」与 `training/silhouette/SILHOUETTE_AWARE.md`。

## 4.54 GPU 渲染：OpenGL 离屏胶囊光栅化（`gpu_render.py`，v1.38）

**问题**：CPU `fdrender exact` 逐笔光栅化太慢（chibi `.fdraw` 38,737 笔 1024=219s、4096≈21min）。

**算法**（GPU 路径，ComfyUI python + PyOpenGL/glfw 离屏 FBO，桌面 GL 4.6）：

1. 读入 `.fdraw`（render.json）/`.fbs`/layered_base `strokes` JSON；采样**复用**
   `fbs_core.render.sample_stroke`（CPU 采样，7.7s 量级）；
2. 每段构造 capsule 几何（A,B,半径,颜色,alpha；gradient 用逐段两端色近似）→ 每段 2 三角形覆盖
   capsule bbox，VBO 上传；
3. GLSL 330 片元着色器：`cov = clamp(r + 0.5 - dist, 0, 1) * alpha`（距线段距离 + 0.5px 抗锯齿），
   按绘制顺序 alpha blend 到 supersample（默认 2×）FBO；
4. `glReadPixels` → 垂直翻转 → SS 下采样 → PNG；输出计时 JSON
   （`load/sample/geometry/gpu/readback/total`）；
5. 无 PyOpenGL/无 GL/上下文失败 → exit code **3** + 明确 stderr；ComfyUI 节点据此自动回退
   CPU `fdrender exact`（`FDP_GPU_RENDER=0` 可强制 CPU）。

**质量/速度（chibi `.fdraw`）**：

**质量/速度（chibi `.fdraw`，ComfyUI python + RTX 4070 Ti SUPER）**：GPU 1024 **14.2s** /
2048 **15.1s** / 4096 **23.6s**，CPU fdrender exact 对应 **219s** / **581.6s** / ≈1260s（~21min）
→ 加速 **≈15.4× / ≈38.5× / ≈53×**（analyst 独立复跑 14.5/16.5/23.3s 一致）；GPU 光栅化本身仅
**0.1–1.2s**（load/sample/geometry/gpu/readback = 1024 3.64/8.27/0.50/0.11/0.34），其余为 CPU
load+sample ~80%。质量 GPU vs CPU exact：1024 **PSNR 40.219/SSIM 0.99566**、2048 代理
**41.9643/0.99623（verifier 原生 2048）**、40.542/0.99458（早期代理）、4096 **44.109/0.99724**
（>32dB）；与源图差异 ≤0.11dB/0.004，差图仅笔触边缘 AA、平坦区差 ≈0（均值差 0.15/255）；
alpha=1 时边缘差 ≤0.42/255。节点 RenderFdraw 自动 GPU（1024 端到端 **17.1s**）+
`FDP_GPU_RENDER=0`/无 GL/alpha<1/region_fill 自动回退 CPU（exit 3/5/4）；workflow/widget 不变；
`PACKAGE_VERSION=3.0.1`；repo/部署 nodes.py hash 一致。

**限制**：PyOpenGL+glfw+桌面 GL 依赖（无 GL 时 exit 3 → 节点回退 CPU）；4096 显存 16GB 实测通过、
更大未验证；gradient 逐段近似；CPU exact 仍是 legacy/canonical 参考；训练/评估与视频渲染未改；
无新第三方依赖。详见 `SKILL.md`「GPU 渲染（v1.38）」与 `training/gpu_render/GPU_RENDER.md`。

## 4.5 通用色彩校准（calibrate_color.py）

1. 渲染当前 strokes；
2. 统计源图与渲染图的逐通道均值/方差、HSV 平均饱和度；
3. 每条 stroke 颜色：`c' = (c - mean_ren)*gain + mean_ref`，再以亮度为轴做饱和度缩放；
4. 可选的局部残差：在笔画质心采样 `ref - render`，以 `--local-k` 混入；
5. 重新渲染，按 PSNR 选 best；迭代 1 次通常足够。
6. 默认 `--sat-mul 1.0`（匹配统计）；`1.05` 轻微提饱和；`1.15` 用于偏灰图。
   这是全局反馈，不依赖图像内容；目标不是把 PSNR 推到最高，而是让颜色统计/观感更接近源图。

## 5. 编译

`compile_best.py`：

- 读 strokes JSON → 每条 `points_px = points*L`, `widths_px=widths*L`；
- 全部 `preset=basic_round_hard`, `shape=round`；
- `FBSConfig(target_resolution=1536, epsilon_px=0.08, N_max=16384, curve_tol=0.002, max_curves=100000, resample_points=4096, supersample=2)`；4GiB 档可把 N_max 提到 32768。
- `structdoc.build_doc_from_strokes` → io_format.write_fbs；
- 用 `fourier_prompt.render_fast` 渲染 2048/4096（fast 仅支持 round/ink 等；全 round 无 fallback）；
- 写 compile_report.json。

`pack_fdraw.py`：`fdraw.pack(doc, meta, out)` → `fdraw.validate`；记录 zip_bytes/uncompressed_bytes/root_sha256。

`verify_delivery.py`：断言 `mode=per_stroke`、`fill_count=0`、`stroke_count≤100000`、`.fbs`<4GiB、
`render_contract.target_resolution≤8192`、`epsilon=0.08`、`.fdraw` validate、渲染复算 PSNR/SSIM、SHA256。

## 6. 参数边界与实测

- 分辨率：1024 快训 → 1408 精调 → 编译 R=1536、渲染 2048/4096。
- 1408 训练约 95–110s/轮；16 核 CPU、单进程。
- `.fbs` 体积：21,694 笔 ≈450MB（`N_max=16384`/ε=0.08）；17,693 笔 ≈286MB；
  30,000 笔细节档 ≈1.2GB（4GiB 容器内）。
- `.fdraw`：47.1MB（完整）/31.4MB（精简）；4GiB 档可到 ~100MB 级，解压总大小仍 <4GiB。
- 质量上限：纯笔画 + 零填色 + 彩铅插画，PSNR≈18.2@1024 / 18.3@2048；细节/纸纹不承诺像素级。
- **single-curve 否决**：一条路径 + 一次 FFT + 0 填色在 17k 笔彩插上只能得到线稿/糊块；
  N 提到 65536 仍无法同时保颜色面与细节，因此本 skill 只做多笔画。
