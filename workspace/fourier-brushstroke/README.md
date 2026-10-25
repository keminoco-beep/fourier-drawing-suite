# fourier-brushstroke v2

把一张位图复刻成「**中轴笔画 + 傅里叶系数 + 逐 t 画笔属性**」的 `.fbs` 文件，
再从它渲染任意分辨率的静态图 / **一次 FFT、一笔连续画完**的 1 分钟作画视频。

> **定位**：`位图 → 矢量笔触 → 位图` 的**往返复刻工具**，不是生成工具。
> 它不改善画面内容，只改变表示与绘制方式。
> **v2 是从头重建的干净实现**：不继承旧版代码与旧版产物，旧版 30+ 次返工史只作为工程教训来源
> （见 `references/engineering_guardrails.md`）。

---

## 能力边界（先看这个再决定是否使用）

| 输入类型 | 预期 | 说明 |
|---|---|---|
| 线稿 / 签名 / 手写 | ⭐ 最佳 | 中轴≈真实中心线 |
| 图标 / Logo / 扁平插画 | ✅ 良好 | 区域填充 + 中轴分支混合分解 |
| 彩色插画 / 柔光插画 | ⚠️ 中等 | 笔画数多、体积大、耗时高 |
| 渐变图 | ⚠️ 降级 | 无硬边 → 中轴退化，带 warning |
| 照片 / 实拍 | ❌ **拒绝** | 明确拒绝并给出实测依据与替代建议，不可绕过 |
| 细密黑白线稿（1–3px 蛛网 + 大片填充） | ❌ **不承诺高保真** | 圆盘并集的表示法边界，旧版实测 ~15 dB；会带 warning |

不承诺：比 PNG/JPEG 更小、任意分辨率都清晰（2R 会振铃，是 N 绑定 R 的语义）、
SVG 保真、语义图层、v1 `.fbs` 兼容读取。完整清单见 `references/capability_boundary.md`。

---

## 安装

v2 是**项目级 skill**（设计决策 D8）：把整个 `fourier-brushstroke/` 目录复制到目标项目的
`.workbuddy/skills/` 下即可，无需 pip 安装本包、无网络依赖。

```bash
# 方式 1：目录复制（开发/调试推荐；Windows 路径用引号）
cp -r "fourier-brushstroke" "<目标项目>/.workbuddy/skills/"
```

```bash
# 方式 2：分发包（工作区根目录的 fourier-brushstroke-v2.zip）
# 解压到 .workbuddy/skills/，保证解压后目录名为 fourier-brushstroke/
unzip "fourier-brushstroke-v2.zip" -d "<目标项目>/.workbuddy/skills/"
```

安装后自检（在 skill 的 `scripts/` 下）：

```bash
PY="<python>"
"$PY" fbs.py --help     # 应列出 build/onestroke/video/render/eval/acceptance 六个子命令
"$PY" -c "import numpy, PIL; print('numpy', numpy.__version__, 'Pillow', PIL.__version__)"
```

目录自包含：`SKILL.md`（技能入口）+ `scripts/`（实现）+ `references/`（规范/手册）+ `tests/` + `assets/`。
运行时只需 Python 环境，不修改系统任何配置。

### 依赖

| 依赖 | 级别 | 用途 |
|---|---|---|
| Python 3.13 | **必需** | 本机 managed 环境：`<python>` |
| numpy | **必需** | FFT / 数组运算 |
| Pillow | **必需** | 图像读写 / 归一化 |
| scipy | 可选 | EDT / 高斯 / 连通域加速（运行时探测，缺失自动回退 numpy） |
| imageio + imageio-ffmpeg | 可选 | 视频编码（`video` 子命令）；不装仍可产 `.fbs` / PNG / SVG |

```bash
PY="<python>"
"$PY" -m pip install numpy pillow                    # 必需
"$PY" -m pip install scipy imageio imageio-ffmpeg    # 可选（加速 / 视频）
```

所有阶段都**不联网**（验收项 A4）。

---

## 快速开始

所有命令在 `fourier-brushstroke/scripts/` 下执行。先看 `--help` 确认参数：

```bash
PY="<python>"
cd fourier-brushstroke/scripts

# 1) 逐笔画通道：位图 → .fbs + 重建 PNG + SVG + report.json
"$PY" fbs.py build "INPUT.png" -o "../assets/out/demo" --resolution 1024 --mode sweep
#    产物：demo/<stem>.fbs.json、<stem>_R1024.png、<stem>.svg、<stem>.report.json

# 2) 一笔画通道：逐笔画 .fbs → 一笔画 .fbs（单次 FFT，R2）
"$PY" fbs.py onestroke --fbs "../assets/out/demo/<stem>.fbs.json"
#    产物：<stem>.onestroke.fbs.json（(可加 --harmonics 65536 --max-fft 1048576)）

# 3) 60 秒作画视频（R3）
"$PY" fbs.py video --fbs "../assets/out/demo/<stem>.onestroke.fbs.json" \
    --out "../assets/out/demo/<stem>_1min.mp4" --duration 60 --fps 30 --circles 600 --mode sweep
#    笔画数很多的复杂线稿推荐加 --smooth（t 匀速、固定帧率，圆链不抖不闪）

# 4) 任意分辨率渲染
"$PY" fbs.py render --fbs "../assets/out/demo/<stem>.fbs.json" \
    --out "../assets/out/demo/render_R2048.png" --resolution 2048 --mode sweep

# 5) 度量（同分辨率强制：不给 --render 时自动按参考图原生分辨率现渲染）
#    注意：--render 必须是和 --reference 同分辨率的图，否则抛 E_METRIC_RESOLUTION
"$PY" fbs.py eval --fbs "../assets/out/demo/<stem>.fbs.json" --reference "INPUT.png"

# 6) 验收套件（A1–A15 + R2–R8 专项）
"$PY" fbs.py acceptance --quick
```

### 两条输出通道

| 通道 | 语义 | 命令 | 优先级 |
|---|---|---|---|
| **逐笔画** | 每条笔画各做一次傅里叶变换，按层序逐条绘制 | `build` | **保真优先**（R1） |
| **一笔画** | 全部笔画串成**一条**闭合路径，只做**一次** FFT，连续一笔画完 | `onestroke` → `video` | 视觉震撼（R2） |

一笔画通道额外要求：恰好 1 条闭合 stroke、`stats.fft_passes == 1`、
路径偏差 ≤ 0.05 px @R=1024、travel/填充段宽度为 0 不落墨。
**两条通道共享同一份几何与笔刷属性**，不是两套独立数据。

### 示例输入（本机真实测试图，R10）

```bash
# horn 线稿（505×712）
"$PY" fbs.py build "<path>)] ttus (@ttuttus_)/109：1807373590473445577_1.png" \
    -o "../assets/out/horn" --resolution 1024
# chibi 彩插（421×412）
"$PY" fbs.py build "<path>" \
    -o "../assets/out/chibi" --resolution 1024
```

---

## 目录结构

```
fourier-brushstroke/
├── SKILL.md                       # 技能入口（agent 用：能力边界 / 命令 / 流水线）
├── README.md                      # 本文件（人用：安装 / 依赖 / 快速开始 / v2 差异）
├── CONTRACT.md                    # 内部接口契约（Lead 维护；模块间唯一集成依据）
├── references/
│   ├── fbs_format_spec.md         # .fbs v2.0 数据格式规范
│   ├── capability_boundary.md     # 能力边界 / 分类判据 / 照片拒绝 / 警告码表
│   ├── troubleshooting.md         # 异常码 / 症状排查 / 调参手册 / 性能
│   ├── engineering_guardrails.md  # ★ 改动前必读：六类根因 / 七个坑 / 检查表
│   └── requirements_traceability.md  # R1–R10 追溯 + A1–A15 覆盖 + 实测登记
├── scripts/
│   ├── fbs.py                     # 唯一 CLI 入口（build/onestroke/video/render/eval/acceptance）
│   ├── run_acceptance.py          # 验收套件（A1–A15 + R 专项）
│   ├── check_hygiene.py           # 静态守卫（裸 except / 写死上限 / 手写精度…）
│   └── fbs_core/                  # 核心库
│       ├── config.py              # FBSConfig：全流水线唯一参数来源
│       ├── util.py                # 通用工具 / scipy 探测与 numpy 回退
│       ├── attribute.py           # AttributeCurve 求值（6 种 interp）
│       ├── io_format.py           # .fbs 读写 / 校验 / 量化 / 指纹
│       ├── document.py            # element → .fbs 文档组装
│       ├── preprocess.py          # Stage 0 预处理 + 图像分类
│       ├── coverage.py            # 调色板 + 覆盖率场 + 局部质量守恒掩码
│       ├── decompose.py           # Stage 1 混合分解（中轴 + 区域填充）
│       ├── fourier.py             # Stage 2 傅里叶参数化（自适应 N / Nyquist 钳制）
│       ├── brush.py               # Stage 3 笔刷属性曲线反演
│       ├── render.py              # Stage 5 渲染（stamp/sweep + 填充 + SVG）
│       ├── onestroke.py           # R2 一笔画合成（单次 FFT）
│       ├── video.py               # R3 作画视频
│       └── metrics.py             # Stage 6 四维度量
├── tests/                         # unittest（合成用例 T1–T13 + 各模块测试）
└── assets/
    ├── testcases/                 # 合成测试图（确定性生成）
    └── out/                       # 演示 / 验收产物
```

---

## v2 与旧版的差异（重建原因与结果）

旧版（`<path>`，
SKILL.md 1178 行）经历过 30+ 次返工。v2 **不接手旧实现**，按需求与设计文档从头重建；
"v2 比旧版好"这类话必须由 v2 自己的实测数据支撑，**旧版数字不作为 v2 指标**。

| 维度 | 旧版 | v2 |
|---|---|---|
| 代码组织 | 十余个平铺脚本 + 近百个 `_probe*.py` 临时脚本 | `fbs_core/` 分层模块，单一 CLI `scripts/fbs.py` |
| 格式 | v1（`color_fields`/`one_stroke_fills` 等补丁式字段） | 干净的 v2.0 契约（`references/fbs_format_spec.md`），只读 v2；v1 明确拒绝 |
| 表示载体 | 设计上 fourier + bezier 自动路由 | **只用 fourier**；硬边由 `region_fill` 显式 `poly` 承担（贝塞尔降级不在 v2 范围，不是遗漏） |
| 笔形 | 每笔画一个值 → 一笔画只有一个笔形（旧缺陷 D4） | `shape` 是**分类曲线**（step：round/soft/marker），一笔画可逐段变化 |
| 宽度来源 | DT + 后期全局补偿 | 覆盖率场**法向积分**为主 + 亚像素中心线精修；`dt` 为回退 |
| 硬边/填充 | 后期加 `hybrid` 补丁 | 分解阶段就是**中轴 + 区域填充**混合路径；区域带**颜色场**（bbox 内加权网格，渲染双线性采样）保留内部渐变 |
| 度量 | 后期补四维，曾长期自建临时指标 → 假回归 | `metrics.compare()` **四维 + 同分辨率强校验 + fingerprint/same_generation** 从第一天进代码 |
| 序列化 | 后期做共享表/精度反推（`.fbs` −15.8%） | `brush_styles` 共享表 + 精度由契约反推，**从第一天就是设计约束** |
| 失败处理 | 曾有三处裸 `except` 吞错、CLI 默认值覆盖 config | `FBSError` + `WarningLog` 全链路；CLI 默认值一律 `None` |
| 文档 | 1178 行入口，混入 30 次返工史 | 300–500 行 `SKILL.md` 入口 + `references/` 按职责分工 |
| 验收 | 后期才有 `run_acceptance.py` | 验收项从设计文档 A1–A15 / T1–T13 直接落到代码 |

**保留并强化的旧版教训**（完整版见 `references/engineering_guardrails.md`）：

1. **先度量后归因**：质量结论只来自 `metrics.compare()` 四维，禁止自建临时指标；
2. **同分辨率 / 同契约 / 同代码代才可比**：尺寸不同直接抛 `E_METRIC_RESOLUTION`，禁止上采样再比；
3. **阈值由局部尺度/数据导出**：禁止全局标量调参、禁止写死上限（步数/采样数/循环保护）；
4. **不静默失败**：降级必进 `warnings`，拒绝必带实测依据；
5. **热路径向量化**：一次 `ifft` 预计算 + 逐帧切片，禁止逐帧遍历全部系数；
6. **精度由契约反推**：小数位由 `epsilon_px/R` 算出，禁止手写；
7. **格式变更 additive**：v2 后续版本只加字段，不改既有语义；
8. **宁缺勿凑**：填充轮廓不闭合就跳过，不填碎片；不可比就跳过，不给假数字。

---

## 验收

```bash
"$PY" fbs.py acceptance --quick      # 快速；全量去掉 --quick
```

- 验收项：A1–A15（设计文档 §7）+ B1–B3 + R2–R8 专项 + T1–T13 合成用例 + 两张真实图；
- 终版实测（2026-10-18T17:41:14 全量，代码快照 `e7d5742da9c87c2b`）：
  **36 PASS / 2 FAIL（A10 SHOULD、A11 MAY）/ 1 BROKEN（A8-legacy 判据修正记录）/ 1 NOT_IMPLEMENTED（A6）**，
  **MUST 27/27 全过**；明细与数字登记在 `references/requirements_traceability.md` §8。
- 产物：`assets/out/acceptance/report.json`、`report.md`；
- 通过标准：MUST 全过，SHOULD/MAY 记录实测值；失败项必须写清第一处差异与复现命令；
- **判读口径**：`report.md` 里的"❌ 失败"是机读判据 `ok = failed==0`（含 SHOULD/MAY）；
  按"**MUST 全过**"的通过标准，终版应判**通过（带 2 项已知非 MUST 不达标：A10 SHOULD、A11 MAY）**；
- 独立审计交叉证据：`references/verification_report.md`（fresh verifier 复跑/复算，不作为官方数字）；
- 实测数字登记在 `references/requirements_traceability.md` §8（终版已回填）。

---

## 文档索引

| 想看什么 | 看哪里 |
|---|---|
| 作为 agent 怎么用这个 skill | `SKILL.md` |
| 什么图能跑、会被拒绝、降级警告码 | `references/capability_boundary.md` |
| 报错了 / 结果不满意怎么调 | `references/troubleshooting.md` |
| 要改代码之前必读 | `references/engineering_guardrails.md` |
| `.fbs` 字段含义 / 坐标系 / 曲线语义 | `references/fbs_format_spec.md` |
| 每条需求对应哪段代码、验收跑到哪了 | `references/requirements_traceability.md` |
| 独立复验/审计结论（fresh verifier） | `references/verification_report.md` |
| 模块间接口（开发者） | `CONTRACT.md` |

## 笔刷库（现代绘图软件分类）

安装 20 个笔刷预设 / 10 种笔形内核（round/soft/marker/bristle/chalk/spatter/airbrush/pencil/watercolor/ink），参数表见 references/brush_library.md，渲染样本见 assets/out/user_lenna/brush_catalog.png。

## 结构感知笔触绘画（实验）

`scripts/structpaint_demo.py`：结构分级 → 每类局部 patch 笔刷实验（记录）→ UCB 选笔 →先铺后勾的纯笔触作画。方法/实测/不足见 references/structpainting.md。
