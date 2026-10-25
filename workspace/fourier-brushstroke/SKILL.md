---
name: fourier-brushstroke
description: 把位图分解为中轴笔画并用傅里叶系数表示，渲染静态图/一笔画/1 分钟作画视频的复刻工具；适用于线稿、签名、图标、扁平插画，照片类输入明确拒绝。触发词：傅里叶笔触、傅里叶绘图、中轴复刻、笔触复刻、一笔画、fourier brushstroke、.fbs、位图转笔触矢量。
agent_created: true
---

# fourier-brushstroke

把一张位图复刻为「**中轴笔画 + 傅里叶系数 + 逐 t 画笔属性**」的 `.fbs` 文件，
再从它渲染任意分辨率的静态图、**一次 FFT、一笔连续画完**的 1 分钟作画视频。

**定位**：`位图 → 矢量笔触 → 位图` 的**往返复刻工具**，不是生成工具——
它不改善画面内容，只改变表示与绘制方式。
当前是 **v2**：从头重建的干净实现，只读 v2 `.fbs`，不接手旧版代码与产物。

---

## 何时使用 / 何时不要用

| 输入 | 用不用 | 预期 |
|---|---|---|
| 线稿、签名、手写、漫画线稿 | ✅ 首选 | 中轴≈真实中心线，效果最好 |
| 图标、Logo、扁平插画 | ✅ 用 | 区域填充 + 中轴分支混合分解 |
| 彩色插画、柔光插画 | ⚠️ 谨慎 | 笔画数多、体积大、耗时高；保真中等 |
| 渐变图、无明显硬边 | ⚠️ 会降级 | 中轴退化，结果可能为斑块，带 warning |
| 照片、实拍、写实场景 | ❌ **拒绝** | 明确拒绝 + 实测依据 + 替代建议，不可绕过 |
| 需要"从文字生成图" | ❌ 不用 | 本 skill 不做生成 |
| 需要比 PNG 更小的文件 | ❌ 不承诺 | 硬边内容下傅里叶天然更大 |
| 需要 2R 直接清晰 | ❌ 不承诺 | N 绑定 `target_resolution`，2R 会振铃 |
| 细密黑白线稿（1–3px 蛛网 + 大片填充） | ❌ 不承诺高保真 | 表示法边界，输出带 warning |

详细判据、拒绝策略、警告码表：`references/capability_boundary.md`。

### 核心原理（一句话版）

**中轴变换（MAT）是 2D 形状的完备表示**：`形状 = ∪ disc(中轴点, DT 半径)`。
所以"中心线 + 沿 t 的宽度函数"在原理上可以精确重建任意区域，包括填充区域；
由此强制两条渲染语义：**中轴分支按圆盘并集（stamp）重建**、**连续作画用路径⊕圆盘（sweep）**。
傅里叶系数 `z(t)=Σ c_k·e^{i2πkt}` 是曲线的周期参数化载体，`t` 是**弧长归一化参数（不是时间）**。

---

## 快速开始

所有命令在 skill 目录的 `scripts/` 下执行（Windows 路径用引号包住）：

```bash
PY="<python>"
cd fourier-brushstroke/scripts

# 0) 环境自检（首次使用建议）
"$PY" -c "import numpy, PIL; print('numpy', numpy.__version__, 'Pillow', PIL.__version__)"
"$PY" fbs.py --help
```

```bash
# 1) 逐笔画通道（保真优先，R1）：位图 → .fbs + PNG + SVG + report.json
"$PY" fbs.py build "INPUT.png" -o "../assets/out/demo" --resolution 1024 --mode sweep
#    产物：<stem>.fbs.json / <stem>_R1024.png / <stem>.svg / <stem>.report.json

# 2) 一笔画通道（R2）：逐笔画 .fbs → 一笔画 .fbs（单次 FFT）
"$PY" fbs.py onestroke --fbs "../assets/out/demo/<stem>.fbs.json"
#    默认产物：<stem>.onestroke.fbs.json；可加 --harmonics 65536 --max-fft 1048576

# 3) 60 秒作画视频（R3）
"$PY" fbs.py video --fbs "../assets/out/demo/<stem>.onestroke.fbs.json" \
    --out "../assets/out/demo/<stem>_1min.mp4" \
    --duration 60 --fps 30 --circles 600 --mode sweep

# 4) 任意分辨率渲染
"$PY" fbs.py render --fbs "../assets/out/demo/<stem>.fbs.json" \
    --out "../assets/out/demo/render_R2048.png" --resolution 2048 --mode sweep

# 5) 度量（同分辨率强制：不给 --render 时自动按参考图原生分辨率现渲染）
#    注意：--render 传入的图必须与 --reference 同分辨率，否则抛 E_METRIC_RESOLUTION
"$PY" fbs.py eval --fbs "../assets/out/demo/<stem>.fbs.json" --reference "INPUT.png"
#    若已有同分辨率渲染图，才用：--render <同分辨率.png> --reference <原图.png>

# 6) 验收（A1–A15 + R2–R8 专项）
"$PY" fbs.py acceptance --quick
```

### 两条通道（不要混为一谈）

| 通道 | 语义 | 命令 | 验收硬要求 |
|---|---|---|---|
| **逐笔画** | 每条笔画各一次 FFT，按层序逐条绘制，**保真优先** | `build` | 质量指标 A1/A2/A9 |
| **一笔画** | 全部笔画串成**一条**闭合路径，只做**一次** FFT，连续一笔画完 | `onestroke` → `video` | `stats.fft_passes==1`；恰好 1 条闭合 stroke；路径偏差 ≤0.05 px @R=1024；travel/填充段 width=0 不落墨 |

两条通道共享同一份几何与笔刷属性（不是两套独立数据）。

### 真实测试图（R10）

```bash
# horn 线稿 505×712
"$PY" fbs.py build "<path>)] ttus (@ttuttus_)/109：1807373590473445577_1.png" \
    -o "../assets/out/horn" --resolution 1024
# chibi 彩插 421×412
"$PY" fbs.py build "<path>" \
    -o "../assets/out/chibi" --resolution 1024
```

### 常用配方（先从这里挑，再微调）

| 任务 | 推荐起点 | 说明 |
|---|---|---|
| 线稿 / 签名（最佳场景） | `build INPUT -o OUT --resolution 1024` | 默认参数即可；中轴≈真实中心线 |
| 图标 / 硬边扁平图 | `build INPUT -o OUT --resolution 1024 --hybrid on` | 块状区域走显式多边形填充，避免圆盘包络把尖角磨圆（软边 auto：线稿 0.5px） |
| 彩色插画（笔画多、慢） | `build INPUT -o OUT --resolution 1024`；嫌碎就调 `--denoise-sigma`/`--prune-threshold` | 先跑通再看四维度量，别一上来全参数拉满 |
| 只要某分辨率静态图 | `render --fbs X.fbs.json --out Y.png --resolution R` | 同一 `.fbs` 任意 R；2R 振铃是语义不是 bug |
| 一笔画 + 60s 视频 | `onestroke --fbs X.fbs.json` → `video --fbs X.onestroke.fbs.json --out X_1min.mp4` | 默认即 `--harmonics 65536`、60 s / 30 fps / 600 圆 |
| A/B 对比 | 两边**同 R、同 ε、同代码代**各跑 `build` + `eval` | 不可比的数据不要出结论（见"三条硬规则"） |
| 排障快速看门狗 | `eval --fbs ... --reference 原图`（不给 `--render` 会自动渲染） | 四维 + warnings 一次看全 |

> 配方只是起点。**每次只改一个参数并复测**；参数含义与副作用见 `references/troubleshooting.md` §4。

---

## CLI 参考

唯一入口是 `scripts/fbs.py`。**默认值以 `fbs.py <子命令> --help` 与 `FBSConfig` 为准**；
CLI 默认值一律 `None`，只在显式给出时覆盖配置（防止"CLI 默认值无声覆盖 config"）。

```bash
"$PY" fbs.py build      INPUT.png -o OUTDIR [选项]
"$PY" fbs.py onestroke  --fbs X.fbs.json [--out Y.onestroke.fbs.json] [选项]
"$PY" fbs.py video      --fbs X.onestroke.fbs.json --out X_1min.mp4 [选项]
"$PY" fbs.py render     --fbs X.fbs.json --out Y.png --resolution R [选项]
"$PY" fbs.py eval       --fbs X.fbs.json [--render Y.png] --reference INPUT.png
"$PY" fbs.py acceptance [--quick] [--items A1,R2] [--report-dir assets/out/acceptance]
```

### `build` 常用参数

| 参数 | 默认 | 作用 | 注意 |
|---|---|---|---|
| `--resolution R` | 1024 | 目标分辨率（长边像素）。**N 的精度锚点** | 改 R 后旧产物不可比 |
| `--mode` | `sweep` | 首帧渲染模式：`sweep`（画线）/`stamp` | `line` 仅调试 |
| `--epsilon-px` | 0.5 | 亚像素容差，直接决定 N | 跨契约不可比 |
| `--denoise-sigma` | 0.6 | 高斯预平滑，决定中轴稳定度 | 太大糊细节 |
| `--colors` | 0=auto | 颜色分层数（8–64 自动） | 柔光/渐变插画可调大保色调；过高笔画爆炸 |
| `--N-mode` / `--N-max` | `error_driven` / 2000 | 自适应 N 策略 / 单曲线谐波上限 | 到顶不达标 → 钳制 + `W_N_CLIPPED` |
| `--hybrid` | auto | 区域填充 + 中轴混合分解 | 方块/硬边内容需要填充路径 |
| `--width-mode` | auto | `auto`（复杂图→`dt` / 单笔画类→`hybrid`）、`coverage`、`dt`、`hybrid` | 默认 auto；合成宽度反演用 hybrid/coverage，复杂图 dt 实测更稳 |
| `--width-scale` | 0=auto | 渲染侧宽度补偿（0=auto：线稿/扁平 0.925、插画 1.0） | 只在渲染侧生效、不写进 doc 宽度（A7/A13 宽度反演不受影响）；实测线稿 +0.19~+1.22dB |
| `--subpixel-axis` | auto | 亚像素中心线精修 | 厚区有漂移风险，默认按尺度判据 |
| `--fill-soft` | 0=auto | 填充边界软过渡（px） | 0=auto：线稿/扁平 0.5px、插画 2.0px（T9 实测线稿 2.0px 过大，+0.25~+1.17dB 头寸） |
| `--color-field-grid` | 32 | 区域颜色场网格上限（0=关闭） | 填充时按 `regions[].color_field` 双线性采样；无效单元用区域代表色回填；关闭会丢区域内部渐变（头发高光/腮红/皮肤阴影） |
| `--min-region-area` | 16 | 最小区域面积（源像素），更小的区域被吸收 | 柔光插画调大（如 100–200）可抑制碎斑/彩虹噪点 |
| `--prune-threshold` / `--min-branch-length` / `--merge-distance` | 0.15 / 8 / 2.0 | 中轴剪枝 / 最短分支 / 邻近端点合并 | 剪枝过度圆化尖角 |
| `--max-curves` | 8000 | 笔画数**安全阀** | 不是常规调参旋钮；截断必报数量 |
| `--supersample` | 2 | 抗锯齿超采样 | 3/4 收益递减、耗时翻倍 |
| `--shape-mode` | auto | 笔形选择：`auto`/`round`/`soft`/`marker` | auto 由柔边/等宽判据选 |
| `--curve-tol` | 0.02 | 属性曲线相对拟合容差（宽度按最大宽度归一；alpha/颜色按各自量纲） | 过小会把像素抖动当信号 |
| `--no-svg` | 关 | 不导出 SVG | SVG 只是互通格式 |
| `--force-class` | 无 | 覆盖**非 photo** 分类 | 仅排障；photo 拒绝不可绕过 |
| `--quiet` | 关 | 减少控制台输出 | warnings 仍写入 `.fbs` |

#### 2026-10-18 保真度修复：默认参数按内容类别自动分档（实测，R=1024、同 ε、同代码代）

| 例图 | 类别 | 修复前 PSNR | 修复后 PSNR | Δ |
|---|---|---|---|---|
| elf 线稿 | lineart | 22.90 | 23.21 | +0.34 |
| DeepSeek logo | lineart（硬量化源） | 22.88 | 23.22 | +0.59 |
| pale 柔光插画 | illustration | 23.03 | 23.23 | +0.47 |
| chibi 彩插 | illustration | 19.04 | 19.73 | +0.72 |
| horn 线稿 | lineart | 19.95 | 20.69 | +0.96 |

修复项（详见 `references/gap_analysis.md` 与 `references/requirements_traceability.md` §8.7）：
1. 度量闸门 bug：`check_contract` 宽高比按 (H,W) 误用 [W,H]，所有非方图 contract 误报"不可比"（已修 1 行）。
2. 线稿填充路由：细长**实心**结构（logo 字母/大色块）此前按 sprawl 全走中轴 → 硬边丢失；
   新增"自身厚度证据"（薄像素占比 ≤0.55 且厚度中位数 ≥2px）启用 `region_fill`。
   纯二值/硬量化源（抗锯齿像素占比 <5%）不启用该放宽。
3. 宽度反演：`width_mode=auto`（复杂图 DT / 单笔画 coverage）+ 渲染侧类别补偿 0.95（线稿，
   仅对 `image_class.evidence` 非空且笔画数 ≥100 的真实素材生效）——只在渲染生效、不写进 doc，
   A7/A13 宽度反演与 A8/R5b 基元回归不受影响。
4. 填充软边 `fill_soft_px` 默认 0=auto（线稿 0.5 / 插画 2.0）；`color_field_grid` 默认 20→32。
5. 颜色场量纲 bug：近黑单元（通道 ≤1.5）曾被误判为 0..1 浮点 → 填充出现纯绿/纯青噪点（已按整场统一量纲修复）。
6. 空心/环状区域：轮廓填充会把洞涂实（气泡、高光描边）→ `W_HOLE_REGION` 回退中轴分支。
7. 一笔画填充侧表补 `color_field`：旧版 `one_stroke_fills` 只存平色，260 个填充被涂平，
   导致**视频与 PNG 预览差距 4.4dB**；现在侧表可带区域颜色场，一笔画静态 18.87→22.50dB
   （距逐笔画仅 0.73dB），视频末帧 vs PNG 14.5→22.9dB（720×1080）/ **24.4dB（1080×1620）**。
   视频作画区分辨率较低时仍比静态软，要逐像素对齐用 `--size 1080x1620`。
8. 区域填充接缝黑线：颜色场把**边界像素（混有抗锯齿/描边墨色，比内部暗）**也平均进网格，
   渲染后每个填充边缘出现一圈偏暗虚线（典型：pale 胸口）。改为**只用内部像素（DT≥1px）估计单元颜色，
   边界单元用最近邻内部单元回填**；pale PSNR 23.23→**23.50**（D 暗墨保护后 **23.97**）、SSIM 0.830→**0.857**，接缝消失。
9. 视频/一笔画与静态对齐后，剩余差异主要来自视频作画区分辨率与圆链叠加，属于展示层。
10. 背景判定只看颜色距离：近背景色的**真实内容**（肤色、浅高光、浅蓝衣褶）会被整块当背景剔除 →
    填色层直接消失（白洞/肤色不填）。修复：内容判定改为"比背景**更接近某个墨色**"（最近色分类，
    近背景像素可救回），并把调色板近背景真实色调簇的保留门限从 fg_tol 降到 6（只有 AA 尾才丢）。
    新增单测 `test_pale_content_survival` 锁定；pale/chibi/logo/horn 等既有例图指标不变或微升。
11. 带洞区域填色消失：环/气泡/带孔块面此前被路由回中轴分支（填色整块丢）。现在 `region_fill` 支持
    `fill_holes`：decompose 输出洞几何，渲染端先填外轮廓、再逐洞用背景色挖掉（逐笔画/一笔画/视频三路，
    旧 4/5 元素侧表兼容）。环状实心结构（洞数 ≤4、占画布 ≤3%）强制走填充（lineart/flat_art 生效）；
    洞数 >4 的蔓延线网与插画类仍回退中轴（否则线间空白/细节会被整块涂掉，实测 horn −3.7dB、
    chibi/pale −0.2~−0.4dB）。合成环端到端：逐笔画 PSNR 31.09dB、一笔画 34.02dB、视频末帧洞=底色。
12. 短笔触/排线丢失：过短分支此前被直接丢弃（manga 实测丢 3191 条）。现在按**墨量**（长度×中位全宽 ≥2.5px²）
    保留为短笔画，只丢墨量过小的 AA 噪声；`W_SHORT_BRANCHES` 分别报告"保留/丢弃"数量。
    manga 重建笔画 3882→5688（+47%），PSNR 19.66→19.61（基本持平，换来排线/短笔触保留）。
    插画类不启用短笔触救援（短碎片多为 AA 噪声，实测反而掉分）；新增单测：
    `TestHoleAwareFill`（带洞填充 + 短分支墨量判据 + 小墨块不静默丢失）。
13. **亚像素边界（A：线稿锐度）**：区域填充边界原是整像素格点阶梯 + 软边。现在
    `_subpixel_loop` 把格点回路沿法向对齐到覆盖率场 50% 等值线（σ=0.8px、搜索 ±1.2px），
    边界 0.5px 级修正。仅对"真实抗锯齿源"（AA 像素占比 ≥5%）与非插画类启用；硬量化/NEAREST
    源与插画类仍走旧边界（实测强制启用 logo −0.15dB / chibi −0.16dB）。
    实测：elf **23.41→23.56dB**（SSIM 0.9213→0.9226）、manga 19.61→19.65、horn 21.04→21.08；
    horn 边界环 MAE 28.27→27.90（−1.3% 相对值）。
14. **近背景浅色补色（B：实验，默认关）**：`--pale-band-palette` 对封闭的浅色块（浅肤色/浅蓝衣褶/
    浅高光）补最多 4 个调色板颜色，缓解"浅色内容被判背景 → 填色白洞"。默认关闭：
    manga 实测 19.65→19.61dB、SSIM 0.7679→0.7551（补色引入额外区域/噪声），负结果已记录。
15. **高分辨率输出（C）**：同一 `.fbs` 可用 `--resolution 1536/2048` 重渲（线稿更锐、细节更多）；
    但同分辨率口径 PSNR 不升（manga R1536 19.49 vs R1024 19.65）——分辨率不是保真瓶颈，
    仅作展示/放大用途，`.fbs` 体积与渲染耗时随 R 增长。
16. **暗墨细节保护（D，插画类）**：`absorb_small_components` 会把小块并进邻居标签；当小块
    "明显更暗且与邻居色差 ≥24" 时（细线/排线/瞳孔/发丝）不再吸收，保留为独立标签（随后走笔画层）。
    仅对插画类启用：pale **23.51→23.97dB（+0.47）**，眼睛/发丝/口鼻线明显更清晰；线稿类
    （manga/horn/logo）实测掉分（manga 19.65→19.18）故不启用。
17. 配套修 `region_fill` 的"墨色洞"：若区域内部洞以墨色内容为主（而非背景空白），实心填外轮廓，
    洞内细节由后续笔画层覆盖（既保住大块填色又保住细线）。
18. **纹理感知表示选择（E）**：
    - 短墨段（<2.5px）不再画成"圆头短笔画"（会把点画大、过黑），改按**墨量面积**输出点盘
      `texture_dot`（半径由墨量与局部 DT 导出、颜色/alpha 取源图该点），manga 467 个 dots；
    - 线稿"暗底薄纹理"保真放 `--texture-preserve` 实验开关（默认关）：开后会保留更多排线/发丝，
      但洞内墨线覆盖不全时会出现白色缝隙伪影，且像素指标变差（manga 19.665→19.130dB，
      SSIM 0.7681→0.7786）——视觉结构与色调是权衡，默认关闭并在文档记录负结果。
    - 默认收益：manga 19.654→19.665、horn 21.078→21.097、elf 23.560→23.574（均 +0.01~0.02），
      logo/chibi 不变；插画类暗墨保护（D）保持 pale **23.974dB**。
柔光插画的剩余误差主要在填充层内部颜色（高频细节被表示法平滑）；分辨率不是瓶颈（R=2048 实测 PSNR 不升、SSIM 略升）。

19. **纯笔触绘画通道（`--painterly`；照片实验通道默认走它）**：这是本 skill 的**目标表示**——
    整个绘画过程只由"一串傅里叶曲线 + 一串随时间 t 的画笔属性"构成，**没有任何区域填充/
    颜色场/面片元素**。照片默认仍拒绝（`E_PHOTO_DEFERRED`），显式 `--painterly`
    （或 `--allow-photo-experimental`）后按连续色调走纯笔触绘画。lenna R=1024 实测：
    5213 笔、0 填充、PSNR 22.16 / SSIM 0.65，`Lenna.brush_timeline.json` 9.8MB（笔刷库版：20 个预设 / 10 种笔形，见 §23）。
    判定标准不是 PSNR（绘画不是复刻），而是"过程可画、笔触可信、属性可编辑"。
20. **绘画算法（沿结构走向的分层笔触拟合）**：
    - **分层**由粗到细（笔刷半径 22→14→9→5.5→3 @work512）：每层先把目标模糊到与笔刷
      同尺度（画师看到的概括形），在**误差明显**处落笔，半径递减收拾细节；
    - **运笔方向沿结构走向**（梯度切向积分成弯曲笔触），平坦区随机——头发/轮廓处笔触
      顺着走向走，不是乱涂；笔触有**压力曲线**（两端细、中段宽）与软毛笔边；
    - 颜色取**该层模糊参考色**（概括色，不是逐像素抄色）；alpha 1→0.88 产生叠加层次；
    - 输出每笔路径点 + 逐点宽度/颜色/alpha，走现有 Fourier 拟合/笔刷属性曲线/一笔画/
      视频链路；笔触是表示单元，不受 `--max-curves` 截断。
    - 参数：`--brush-strokes`（预算，默认 3000；lenna 用 6000）、`--brush-work-res`
      （拟合工作分辨率，默认 512）、`--painterly`（开启通道）。
21. **画笔属性时间轴（`timeline` 子命令；build 在 painterly 模式自动导出）**：
    `Lenna.brush_timeline.json` = 样式表 + 逐笔记录：
    `{id, t0, t1, duration_s, start, end, fourier:{N,closed,arc_length,coef}, brush:{style,width}}`，
    属性曲线（width/color/alpha/shape）按弧长归一化参数 t∈[0,1] 求值；时间是**可编辑**的
    作画时间轴（每笔时长∝弧长、顺序铺满总时长、允许重叠=同时落笔）。格式见
    `references/brush_timeline_format.md`。
22. **A-GMesh（自适应梯度面片，`--mesh`）保留为旧实验**：它把图像当连续色调"复刻"，
    不属于"绘画过程"表示，默认/拍照通道都不再使用；仅在需要极高保真近似时显式开启。
23. **笔刷库（现代绘图软件分类 → 可执行预设）**：`brushlib.py` 安装了 20 个笔刷预设，
    覆盖 Basic / Pencils / Inking / Markers / Dry Painting / Chalk·Pastel·Charcoal /
    Wet Painting / Watercolors / Airbrush / Spray-Texture 等分区（完整参数表见
    `references/brush_library.md`；分类对照 Krita 4 预设包与 Photoshop/Procreate/Corel 体系）。
    - 笔形内核 `shapelib.py` 注册 10 种：round / soft / marker / bristle / chalk / spatter /
      airbrush / pencil / watercolor / ink——全部是"精确带覆盖 × 确定性纹理核"，无贴图/填充；
      同一笔触每帧结果一致（视频不闪烁）。
    - 纯笔触绘画按层选用不同笔刷（铺底软圆/油画鬃毛 → 中层干画/粉笔 → 细节墨线/铅笔）；
      宽度抖动、alpha 区间、颜色抖动、压力锥度都进入逐笔属性曲线与画笔时间轴。
    - 笔刷目录图：`assets/out/user_lenna/brush_catalog.png`（每个预设的真实渲染样本）。
    - 时间轴的 `styles[].shape` 即笔形名；改预设 = 改 shape + 属性曲线，不需要改几何。

24. **结构感知笔触绘画（实验，`scripts/structpaint_demo.py`）**：按"人类画师看结构下笔"实现——
    先把图分解成结构类（flat/shade/detail/line_fine/line_thick），再对每类做**局部 patch
    笔刷策略实验**并记录（`experiments/brush_policy_experiments.{json,md}`），用 **UCB 多臂
    老虎机**从记录中选每类笔刷；最后按"先铺大块 → 中景 → 线条/细节"生成纯傅里叶笔触。
    细节/实测/不足见 `references/structpainting.md`。
    - chibi 鲸鱼娘 R=1024 实测：结构 flat56/shade20/detail49/line435；规划 4164 笔 →
      在线 RL 接受 2856 笔（68.6%，训练画布 16.36dB）→ 最终 PSNR **15.90 / SSIM 0.743**。
      改进项（线条保守合并、端帽内缩、两遍铺色、底色/纹理分离）已接入；仍不理想的根因
      （区域分割粒度、RL 决策画布与真渲染不一致）见 references/structpainting.md。
    - 命令：`python scripts/structpaint_demo.py --input <png> --out <dir> --resolution 1024`；
      产物：成图、结构分类图、实验记录、报告、纯笔触 `.fbs`。

25. **分辨率/测绘结论（2026-10-18 实验）**：提高 R **不会提升保真度**——同一套笔触在
    R=1024/2048/4096 渲染 PSNR 15.954/15.913/15.891（持平/略降），SSIM 0.757/0.817/0.872
    （随 R 上升）。原因是源图分辨率（421px）就是 Nyquist 上限：预处理只降采样不升采样，
    把图放大后再测量反而触发碎片化（线条 474→2208、规划笔画 62k、J 更差）。
    采纳配置：**测量/训练在源分辨率、渲染用 R=2048（展示可用 4096）**；测绘报告见
    `references/resolution_survey.md`，`scripts/survey_image.py` 可在训练前输出
    线宽/区域/AA 带/分辨率收敛性（训练前必跑）。
26. **在线 RL 真渲染器版 + 次评分**：`stroke_rl_render.py` 用真 `FloatCanvas`（10 笔形内核）
    逐笔决策，与最终渲染的 PSNR 差从 0.46dB 降到 0.22dB；目标函数
    `J = PSNR − 0.02×(笔数/1000) − 0.01×(线段数/100)`（低权重，防舍本逐末）。
    实测接受 2434 笔（58.5%）、线条段 139、J=16.07；细节见 references/structpainting.md。

27. **训练循环（9h+，断点续训，`scripts/train_loop_9h.ps1` / `scripts/train_loop.py`）**：
    按用户要求跑满 9 小时以上。每轮：分区 UCB 选择（palette / 线稿闭合+区域生长 contour）→
    结构抖动 → **真渲染器 RL + 连续动作搜索**（位置/宽度/alpha/颜色候选）→ **次评分剪枝**
    （按每笔收益删 0/10/20%，取 J 最大且 PSNR 损失≤0.15dB 的变体）→ 记录/checkpoint。
    产物：`assets/out/user_chibi/train_loop_9h/`（state.json 断点、training_log.jsonl、
    checkpoints/best_J|best_psnr、summary.md、DONE 标记）；详见 references/training_loop.md。
    首轮实测（eval R=1024）：规划 4164 笔 → RL 接受 2822 → 剪枝后 2540 笔/150 线段，
    **PSNR 17.216 / SSIM 0.7698 / J=17.151**（对比上一版 15.95 显著提升），单轮 89.6s。

### 其他子命令参数

| 参数 | 子命令 | 默认 | 作用 |
|---|---|---|---|
| `--harmonics` | onestroke | 65536 | 一笔画目标谐波数（受 Nyquist 钳制） |
| `--max-fft` | onestroke | 1048576 | 一笔画 FFT 采样数上限 |
| `--resolution` | onestroke | 沿用 `.fbs` 契约 | 路径偏差复核用的分辨率 |
| `--out` | onestroke | `<stem>.onestroke.fbs.json` | 输出路径 |
| `--fill-mode` | onestroke | polygon | `polygon`=整块多边形填充（默认）；`brush`=**宽笔扫掠填充**：填充区由笔一行行刷出来（视频里不会"先整块淡入再被笔压过"） |
| `--order` | onestroke | layer | 一笔画作画顺序：`layer`=先铺色后勾线；**`lines_first`=先画线稿、最后用宽笔铺黑/色块（像人手绘，过程视频推荐）**；`spatial`=按区域逐块画；`reading`=从左到右（logo / 文字 / 签名推荐） |
| `--duration` / `--fps` | video | 60 / 30 | 时长（R3：60 s ±5%）/ 帧率（不足自动提高并 warning） |
| `--circles` | video | 600 | 可见旋转圆数量（视觉上限；笔尖仍用全系数 ifft） |
| `--smooth` | video | 关 | 平滑档：`duration×fps` 帧、t 在时间上匀速推进。**笔画数很多（步骤 > 帧数）时推荐**——避免自动升到 96fps 这类高帧率 + 步进式推进造成圆链抖动/闪烁；所有墨迹仍会画出，只是不保证每个微步骤独占一帧 |
| `--size` | video | 自适应竖版 720×1080 | 画面尺寸 `WxH` |
| `--mode` | video | sweep | `sweep` / `stamp` |
| `--resolution` | render | 契约 R | 任意渲染分辨率 |
| `--supersample` / `--width-scale` | render | 契约 / 0=auto | 超采样 / 宽度补偿（内容相关） |
| `--render` | eval | 自动渲染 | 已有渲染图时直接给，**必须与参考图同分辨率** |
| `--save-render` | eval | 无 | 现渲染时顺带保存 PNG |
| `--resolution` / `--mode` / `--supersample` | eval | 参考图原生 / sweep / 契约 | 现渲染时的覆盖参数 |
| `--out` | eval | 无 | 度量报告 JSON 输出路径（非 0 返回码表示契约错误） |
| `--duration` / `--overlap` | timeline | 60 / 2.5 | 时间轴总时长（秒）/ 相邻笔触时间重叠系数 |
| `--quick` / `--items` / `--skip-video` / `--report-dir` | acceptance | 全量 | 快速模式 / 只跑指定项 / 跳过视频 / 报告目录 |

> 更细的调参含义与副作用见 `references/troubleshooting.md` §4；
> CLI 未暴露的参数属 `FBSConfig` 内部项，改默认值需同步文档与验收。

---

## 流水线（Stage 0–6）

| Stage | 模块 | 产出 | 异常 / 降级 |
|---|---|---|---|
| **0 预处理 + 分类** | `fbs_core/preprocess.py` | RGBA8 画布、`source`、`image_class`（分类特征实测）、`accelerators` | `E_INPUT_*`、`E_PHOTO_DEFERRED`、`W_DOWNSCALED`、`W_MULTIFRAME` |
| **1 混合分解** | `fbs_core/coverage.py` + `decompose.py` | element 列表：points/s/closed/fill_role/widths/colors/alphas/shapes/fill_poly/region_id/layer | `W_PRUNED`、`W_SHORT_BRANCHES`、`W_SMALL_REGIONS`、`W_MAX_CURVES`、`W_GAP_BRIDGED`、`W_COLOR_FIELD_OFF` |
| **2 傅里叶参数化** | `fbs_core/fourier.py` | `coef`（按 \|k\| 升序）、`N`、`max_err`、`arc_length`、`closure` | Nyquist 钳制 + `W_N_CLIPPED` |
| **3 笔刷属性反演** | `fbs_core/brush.py` | `width`/`color`/`alpha`/`shape` 曲线（AttributeCurve），常量压 `constant` | 共享样式表精确去重 |
| **4 文档组装** | `fbs_core/document.py` + `io_format.py` | `.fbs.json`（v2.0，含 `warnings`/`quality`/`fingerprint`） | `E_FBS_INVALID` |
| **5 渲染复刻** | `fbs_core/render.py` | 任意分辨率 PNG、SVG、填充/扫掠渲染 | `E_RENDER_RESOLUTION`、`W_SVG_APPROX` |
| **6 质量评估** | `fbs_core/metrics.py` | PSNR/SSIM/RMSE/MAE + 四维（结构/厚度谱/色调/线宽） | `E_METRIC_RESOLUTION`（分辨率不一致直接拒绝） |

**数据流**：

```
原图 ─Stage0→ RGBA8+类别 ─Stage1→ element ─Stage2→ coef ─Stage3→ brush 曲线
    ─Stage4→ .fbs.json ─Stage5→ PNG/SVG；.fbs ─onestroke→ 一笔画 .fbs ─video→ mp4
```

---

## 产物与命名约定

| 子命令 | 产物 | 说明 |
|---|---|---|
| `build` | `OUTDIR/<stem>.fbs.json` | 核心表示，自描述 JSON（v2.0） |
| | `OUTDIR/<stem>_R{R}.png` | 重建图，文件名带渲染分辨率 R |
| | `OUTDIR/<stem>.svg` | 互通导出（`--no-svg` 可关；带 `W_SVG_APPROX`） |
| | `OUTDIR/<stem>.report.json` | 质量报告（四维 + warnings + 计时） |
| `onestroke` | `OUTDIR/<stem>.onestroke.fbs.json` | `mode="one_stroke"`，单次 FFT，含 `step_spans`/`one_stroke_fills` |
| `video` | 由 `--out` 指定 | mp4；帧数/尺寸/时长可用 `probe_video` 核验 |
| `render` | 由 `--out` 指定 | 任意分辨率 PNG |
| `acceptance` | `assets/out/acceptance/report.json`、`report.md`、`hygiene.txt` | 验收证据；数字以它为准 |

**纪律**：

1. 产物文件名必须带 R（或由命令显式指定），防止跨分辨率混用当基线；
2. 不要手改 `.fbs` 后直接当产物——写盘走 `io_format.write_fbs`（自动量化 + 校验）；
3. `.fbs` 里的 `warnings` 是产物的一部分，报告时不得隐藏；
4. `.fbs`/PNG 体积比、PSNR 等数字只有连同 R、`epsilon_px`、代码指纹一起报才有意义。

---

## 渲染语义（正确性要求，不是风格选项）

| 要求 | 语义 | 违反后果 |
|---|---|---|
| **stamp** | 中轴分支 = 圆盘并集 `∪ disc(c(t), r(t))` | 用变宽线段会在拐弯处缺口、尖角处溢出 |
| **sweep** | 路径 ⊕ 圆盘（Minkowski 和）连续扫掠，画出**真实的线**（R5） | 画成"点串"= 不合格 |
| **精确带覆盖** | `coverage = min(0.5, d+r) − max(−0.5, d−r)` 钳位 [0,1] | 用 `clip(r+0.5−d)` 会把亚像素细线画胖近一倍 |
| **width=0 跳过** | 抬笔 travel / 区域填充段宽度为 0，**必须跳过** | 设 `max(r,0.3)` 下限会在图上画出横穿直丝 |
| **采样 ∝ 路径长度** | 约 `4·arc_px`，禁止写死采样点数 | 长路径画成弦 |
| **分层绘制** | `layer` 升序；region_fill 在底层，细笔画压上层 | 覆盖关系错乱 |

- **填充**：region_fill 使用**显式 `poly` 多边形**，不依赖有损重建；支持亚像素外扩 0.5px 与软边。
  区域可带颜色场 `regions[].color_field`（bbox 内加权均值网格），渲染时**双线性采样**，
  使区域内部颜色连续变化（渐变/柔边/腮红）；无效单元用区域代表色回填，避免插值拖入垃圾色。
- **属性**：`width` / `color` / `alpha` / `shape` 按**弧长归一化 t** 求值，与几何严格同步。
- **分辨率无关**：同一 `.fbs` 可渲染任意 R；R 变化只改变采样密度，不改变语义。
  在 `2R` 出现振铃**不是 bug**——N 的语义绑定在 `target_resolution` 上；要 2R 清晰需以 2R 重新生成。

---

## 笔刷属性（R4 / R6）

`.fbs` 的笔刷是**四条相互独立的属性曲线**（不是区间常量），按 t 连续求值：

| 属性 | 类型 | 关键约束 |
|---|---|---|
| `width` 全宽（直径） | 数值曲线 | 6 种插值：`constant`/`linear`/`spline`/`step`/`fourier`/`preset`（taper-in/out/both、ease-in/out、sigmoid） |
| `color` 颜色 | 色标曲线 `stops` | 每个 stop 自带 rgb + alpha；颜色渐变是一等公民 |
| `alpha` 透明度 | 数值曲线 | 与 width/color 独立组合 |
| `shape` 笔形 | 分类曲线（step） | `round`/`soft`/`marker`，**可沿 t 变化**（旧版 D4「一笔画只能一个笔形」的格式限制由分类曲线承担；验收见 A15/T11） |

语义纪律：
- `step` 表示**区间语义**，不得用常量近似代替；
- 属性曲线 interp 可以与几何表示不同（几何用 fourier、宽度用 spline）；
- 颜色合并**不得平均墨色**（会把墨色拉淡）；
- 常量属性必须压成 `constant`（体积纪律）；共享样式进 `brush_styles` 精确去重。

---

## 精度与 N（R8）

- **N = 保留的谐波数**，对应 `2N+1` 个圆（k>0 逆时针、k<0 顺时针、k=0 直流）。
- **硬性 Nyquist 上限**：有 m 个独立采样点的曲线只有 m 个不同圆可用，
  即 `N ≤ M/2−2`（单曲线 `M = resample_points`，默认 4096 → N ≤ 2046）。
  越界必须**钳制 + `W_N_CLIPPED`**，绝不允许系数回绕重复取（旧版曾因此 PSNR 30.7 → 5.47）。
- **N≈65536 的适用条件**：只在一笔画的**合成路径独立采样数**足够时有意义
  （≈ 2×弧长像素数，受 `--max-fft` 约束）。对单条笔画请求 65536 没有信息可编码。
- **精度判据由契约反推**：世界单位阈值 `thr = epsilon_px / target_resolution`；
  取满足契约的**最小 N**，并记录 `max_err`；序列化小数位也由契约算出，禁止手写。
- **精度来自数据质量，不来自参数规模**：把 N 从 ~40 拉到 Nyquist 上限，
  旧版实测只换回 0.02–0.05 dB、文件涨 10–17 倍。先修几何/宽度，再谈 N。
- 一笔画验收：路径偏差 `path_deviation_px ≤ 0.05 @R=1024`；`stats.fft_passes == 1`。

---

## 视频（R3）

`onestroke` 产出的 `mode="one_stroke"` 文档交给 `video` 子命令：

- **旋转圆作画**：top-|c| 系数画可见圆链（`--circles` 是视觉上限），
  笔尖位置用**全部系数的一次 `ifft` 预计算**，逐帧切片——禁止逐帧遍历系数；
  圆链按**绝对坐标**绘制（不做"整链平移到笔尖"的残差平移），被截断的尾部系数只体现为
  链尾到笔尖的一根细连线，否则整条圆链会随残差逐帧变化而无规律抖动；
- **单次 FFT、一笔连续画完**（R2）：`stats.fft_passes == 1`；
- **时间轴两档**（`timeline`）：
  - 默认严格档（`timeline="arc"`）：`step_spans` 里每个 stroke/travel/fill 步骤至少 1 帧，
    帧数不足时自动提高 fps 并记 `W_VIDEO_FPS_RAISED`——**不省略任何作画步骤**（R3）；
  - `--smooth` 平滑档（`timeline="uniform"`）：固定 `duration×fps` 帧，t 在时间上匀速推进；
    笔画上千的复杂线稿推荐用它，作画速度和圆链旋转都均匀，不会因步进式推进而抖动/闪烁；
- 时长 `--duration 60`，允许 ±5%；填充在路径走到其 t 区间时铺开；travel 不落墨；
- 产物用 `probe_video` 校验帧数/尺寸/时长。

```bash
"$PY" fbs.py video --fbs "X.onestroke.fbs.json" --out "X_1min.mp4" \
    --duration 60 --fps 30 --circles 600 --mode sweep --size 720x1080
# 笔画数很多（本图 2879 条笔画、5758 个步骤）时推荐加 --smooth：
"$PY" fbs.py video --fbs "X.onestroke.fbs.json" --out "X_1min_smooth.mp4" \
    --duration 60 --fps 30 --circles 600 --smooth
# 「画笔画出来」的过程视频（先出线稿、宽笔再铺色，不是整块淡入）：
"$PY" fbs.py onestroke --fbs "X.fbs.json" --fill-mode brush --order lines_first
"$PY" fbs.py video --fbs "X.onestroke.fbs.json" --out "X_paint_1min.mp4" \
    --duration 60 --fps 30 --circles 600 --smooth
```

> 视频编码需要可选依赖 `imageio` + `imageio-ffmpeg`；缺失时 `video` 明确报错，
> 不影响 `.fbs`/PNG 静态产物。字体缺失时中文字幕回退系统字体或英文标签。

---

## 异常与降级（不静默）

**纪律：所有降级必须进 `.fbs` 的 `warnings`，所有拒绝必须带实测依据并以非 0 退出码结束。**

| 典型情况 | 行为 |
|---|---|
| 照片 / 实拍 | `E_PHOTO_DEFERRED` 拒绝（附分类特征实测值 + 替代建议），**不可绕过** |
| 格式不支持 / 损坏 / 过小 / 全透明 | `E_INPUT_FORMAT` / `E_INPUT_CORRUPT` / `E_INPUT_TOO_SMALL` / `E_INPUT_TRANSPARENT` |
| 像素数超 `max_pixels` | 等比降采样 + `W_DOWNSCALED`（不拒绝） |
| 多帧 GIF / 多页 TIFF | 取首帧 + `W_MULTIFRAME` |
| 笔画数超 `max_curves` | 按长度降序截断（安全阀），必须报告丢弃数量 + `W_MAX_CURVES` |
| N 到 Nyquist 上限而精度未达 | 钳制 + `W_N_CLIPPED` |
| 纯色 / 无有效前景 | 特殊路径，记录基色 + `W_SOLID_COLOR` |
| 渲染分辨率内存超限 | `E_RENDER_RESOLUTION` 拒绝并提示 |
| 度量分辨率不一致 | `E_METRIC_RESOLUTION` **拒绝比较**（禁止上采样再比） |

完整码表（`W_*` / `E_*`）、拒绝策略、替代建议：`references/capability_boundary.md`。
报错排查与调参：`references/troubleshooting.md`。

### 30 秒排查

| 症状 | 第一反应 |
|---|---|
| 输入被拒绝 | 看 `E_*` 码 + `image_class` 实测特征；照片拒绝不可绕过；SVG/PDF 请直接用矢量 |
| 笔画数爆炸 / 很慢 | 先 `--denoise-sigma` 0.8–1.0，再 `--prune-threshold`；别先砍 `--max-curves` |
| 细线丢失 / 变虚线 | 细线是局部质量守恒掩码问题，**不要全局调低阈值**（会让多画 11%→34%） |
| 填充区域被圆角化 | `--hybrid on` 让块状区域走显式多边形填充；检查 `corner_preserve` |
| 图上横穿直丝 | 抬笔段 width=0 必须跳过（这是渲染红线，不是调参问题） |
| 视频步骤被跳过 / 时长不对 | 查 `W_VIDEO_FPS_RAISED`；`--duration`/`--fps`/`--circles` 组合 |
| 指标差但"看着差不多" | 四维里必有一维在报警；结构指标看不出色调错误 |
| 改完"变好了" | 先核对两边 R/ε/代码指纹；不可比就是假结论 |

---

## 需求速查（R1–R10 与 v2 的对应）

> 来源：`docs/交接说明_傅里叶笔触_2026-10-18.md` §2。完整追溯见
> `references/requirements_traceability.md`。

| 编号 | 需求 | v2 对应能力 | 怎么验 |
|---|---|---|---|
| R1 | 傅里叶表示 + 渲染，保真尽量高 | 逐笔画通道 `build`；四维度量 | A1/A2/A9 + 真实图 |
| R2 | **单次 FFT + 一笔连续画完** | `onestroke` + `video`；`stats.fft_passes==1`、单条闭合路径 | R2 专项 |
| R3 | 视频 ~60 s；**不省略步骤**；t/弧长等比 | `video`：`step_spans` 每步至少 1 帧；`timeline="arc"` | R3 专项 |
| R4 | 笔触属性完整（线宽/颜色/填充色/笔形） | `AttributeCurve` 四条曲线 + `poly` 填充 + 分类笔形 | R4 专项 + A13–A15 |
| R5 | 画出的必须是**线**，不是点串 | `sweep` = 路径⊕圆盘；采样密度∝弧长 | `test_render` + 看图 |
| R6 | 宽度与颜色**平滑渐变** | 逐点求值 + 颜色 `stops`；块间色差上限 | R6 专项 |
| R7 | 作画顺序像人：先铺底层再压上层 | `layer` 升序、region_fill 底层、同层 footprint 降序 | R7 专项 |
| R8 | N≈65536；t 精度；**适用条件与 Nyquist** | Nyquist 钳制 + `W_N_CLIPPED`；路径偏差 ≤0.05px | R8 专项 |
| R9 | 元素拆解 + 分层 + 每层一次 FFT 的生成架构 | **用户已搁置**（算力/数据量过大） | 不验收、不重启 |
| R10 | 输入图固定为两张测试图 | 真实图示例（horn/chibi）+ T1–T13 合成用例 | 验收报告含两图数字 |

**旧版遗留缺陷（D1–D5）的 v2 处理**：一笔画覆盖/保真（D1/D2）→ 度量先行 + 宽度同源；
腮红色相（D3）→ 区域代表色色度轴高分位 + 空间证据；一笔画笔形（D4）→ shape 分类曲线；
层序语义（D5）→ 只承诺绘制顺序，语义化属 R9。回归状态见追溯表 §2，**未实测前不许写"已修复"**。

---

## 质量报告怎么读（防止被单一数字骗）

| 你看到 | 可能被掩盖的问题 | 还要看什么 |
|---|---|---|
| PSNR 不低 | 线质量差、渐变不自然、笔触感不对 | **看图** + 结构/线宽维度 |
| 掩码/IoU 漂亮 | "灰画成黑"这类色调错误完全不可见 | **色调维度**（混淆矩阵/色差）+ PSNR/MAE |
| 漏画/多画互相抵消 | 整体偏胖或偏瘦被平均掉 | 漏画%、多画% 分开看 |
| `.fbs/PNG` 比值"更小" | 比值随 R 漂移 | 必须同报 R 与分子分母绝对值 |
| 某项验收只差一点 | 判据前提可能不成立（旧版 A3 长期假失败） | 先审判据，再改算法（见 engineering_guardrails §3.6） |
| 报告"已修复" | 可能没做同口径对照 | 必须有「改前/改后」同 R/ε/指纹的数据 |

**判读顺序**：先看 `warnings`（是否降级）→ 再比四维（是否同分辨率/同代）→ 最后看图。

---

## 验收

```bash
"$PY" fbs.py acceptance --quick          # 快速
"$PY" fbs.py acceptance                  # 全量
"$PY" fbs.py acceptance --items A1,R2    # 只跑指定项
```

- 覆盖：**A1–A15**（设计文档 §7，含 A6=NOT_IMPLEMENTED、A8=判据修正）+ **B1–B3** 定性项 +
  **R2–R8 专项** + **T1–T13** 合成用例 + 两张真实测试图（horn/chibi）；
- 判据纪律：被实测证伪的旧判据不删除、转记为 **`A8-legacy`（BROKEN，判据修正记录）**，不计通过/失败；
- 产物：`assets/out/acceptance/report.json` / `report.md`；静态守卫 `hygiene.txt`；
- 通过标准：MUST 全过；SHOULD/MAY 记录实测值；失败必须打印第一处差异与复现命令；
- **所有验收数字以 `run_acceptance.py` 的输出为准**，文档里的历史数字不是 v2 指标；
- **终版结果（2026-10-18T17:41:14 全量，代码快照 `e7d5742da9c87c2b`）**：
  **36 PASS / 2 FAIL（A10 SHOULD、A11 MAY）/ 1 BROKEN（A8-legacy）/ 1 NOT_IMPLEMENTED（A6）**，
  **MUST 27/27 全过**；实测明细登记在 `references/requirements_traceability.md` §8。
- **判读口径**：`report.md` 的"❌ 失败"来自机读判据 `ok = failed==0 && blocked==0`（把 SHOULD/MAY 失败也算进去）；
  按本 skill 的通过标准（**MUST 全过**），终版应判**"通过（带 2 项已知非 MUST 不达标）"**。
  独立审计交叉证据见 `references/verification_report.md`。

### 三条硬规则（给任何要改这个 skill 的 agent）

1. **先度量后归因**：质量结论只来自 `metrics.compare()` 四维（结构/厚度谱/色调/线宽），
   **禁止自建临时指标**；旧版所有错误归因都始于"度量错了"。
2. **同分辨率 / 同契约 / 同代码代才可比**：`target_resolution`、`epsilon_px`、代码指纹
   任一不同就不可比；尺寸不同**拒绝比较**，禁止先上采样再比；不可比宁可跳过，不给假数字。
3. **不静默失败**：降级进 warning、拒绝带依据、上限由数据导出（禁止写死步数/采样数）、
   热路径必须向量化（禁止逐帧遍历系数）。

更完整的六类根因、七个历史坑与改动前检查表：**`references/engineering_guardrails.md`（改动前必读）**。

---

## 参考文档

| 文件 | 内容 |
|---|---|
| `references/capability_boundary.md` | 输入格式矩阵、分类判据、照片拒绝（D5）、警告码表、能力上限、性能预算 |
| `references/troubleshooting.md` | 异常码、按症状排查、调参手册、性能说明、度量结果怎么读 |
| `references/engineering_guardrails.md` | **改动前必读**：六类根因、七个坑、检查表、红线清单 |
| `references/requirements_traceability.md` | R1–R10 → 设计 → 代码 → 验收 → 实测 追溯表；未实现/不承诺清单 |
| `references/verification_report.md` | **独立验收审计报告**（fresh verifier 复跑/复算，交叉证据，非官方口径） |
| `references/fbs_format_spec.md` | `.fbs` v2.0 格式规范（坐标系 / AttributeCurve / 一笔画 / 精度纪律） |
| `CONTRACT.md` | 内部接口契约（开发者：模块边界与函数签名） |
| `README.md` | 安装、依赖、快速开始、目录结构、v2 与旧版差异 |

---

## 版本与兼容

- 当前格式 **`.fbs` version 2.0**（`mode` = `per_stroke` | `one_stroke`）。
- **只读 v2**：旧版 v1 `.fbs` 明确拒绝（`E_FBS_INVALID`），请用新流水线重新生成。
- 后续版本只允许**加字段**（additive），不改变既有字段语义；未知字段读取方必须忽略。
- 不承诺：照片复刻、语义图层、体积优势、任意分辨率清晰、SVG 保真、
  HTML Canvas 第二渲染后端、贝塞尔降级表示（v2 表示载体只有 `fourier` +
  `region_fill` 显式多边形）；R9（元素拆解 + 分层 + 每层一次 FFT 的生成架构）**用户已搁置，不要主动重启**。
