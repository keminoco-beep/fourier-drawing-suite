---
name: fourier-prompt
description: 把 FDSL v2.1 绘图计划编译为傅里叶笔触 .fbs、PNG 和可移植 .fdraw，并可导出分层 PSD/KRA 与渐进作画视频的 agent 工作流。输入是自然语言 prompt 时，必须先由 agent 按模板写出 FDSL plan（本包没有本地 LLM）。触发词：傅里叶提示词、文生图、prompt 到绘图、FDSL、plan2fbs、文本转绘图、分层导出、PSD、KRA、作画视频、fourier prompt、.fbs、.fdraw。
agent_created: true
---

# fourier-prompt（fourier-brushstroke 项目的编译器/Agent 包）

把**结构化绘图计划（FDSL v2.1）**编译成「傅里叶笔触 + 显式填充」的 `.fbs`，
再渲染 PNG、打包 `.fdraw`，按需导出分层 PSD/KRA 或渐进作画视频。

**定位**：`prompt/plan → 可复现的矢量笔触文档 → PNG / 分层文件 / 视频`。
工具本身**不做语义生成**：自然语言到 FDSL 的那一步由 agent（或外部 planner）完成；
本包只负责校验、编译、渲染、打包和导出。

---

## 何时使用 / 何时不要用

| 场景 | 用不用 | 说明 |
|---|---|---|
| 已有一份 FDSL plan，要出图/分层/视频 | ✅ 首选 | `schema → plan2fbs → draw → fdraw → export` |
| 用户给一句 prompt，要“画出来” | ✅ 用 | 简单图：`generate_cli --prompt`；复杂图：agent 写 SVG → `svg_import`（见下） |
| 要复杂图像（人物/场景/插画） | ✅ 用 SVG 路径 | 由 **agent 编写/程序化生成 SVG** 或使用许可明确的在线 text→SVG 服务，再 `svg_import` → FDSL → 渲染；**不跑本地 LLM** |
| 要可复现、可版本管理的绘图源文件 | ✅ 用 | `.fbs`/`.fdraw` 是确定性 JSON/ZIP，适合进仓库 |
| 要把位图（PNG/照片/线稿）转成笔触 | ❌ 不用本 skill | 用 `fourier-brushstroke`（位图 → `.fbs`） |
| 要局部重绘、编辑已有 `.fdraw` 里的对象 | ❌ 当前不支持 | `fdsl-edit/1` 编辑 op 未实现，只能改 plan 重新编译 |
| 要像素级精确复刻某张参考图 | ❌ 不承诺 | 本包是“计划 → 绘制”，不是图像重建 |
| 要 GPU/本地大模型生成 | ❌ 默认不用 | 复杂图像走 SVG 路径；本地模型不参与（傅里叶由确定性代码算） |

---

## 输入与前置条件

**两种输入**：

1. **FDSL plan JSON**（推荐、完全离线）：`skill/examples/*.json` 是可编译的最小样例；
2. **自然语言 prompt**：
   - **规则模式（默认、纯 CPU）**：`python -m fourier_prompt.generate_cli --prompt "..."`，内部解析 15 类主体/颜色/文字/风格/比例 → FDSL；
   - **本地 LLM 意图模式（可选，需模型/GPU）**：`python -m fourier_prompt.llm.intent --prompt "..."`，Qwen 只输出意图 JSON，再交给规则 planner 绘图；模型路径 `models/Qwen2.5-1.5B-Instruct`（torch/transformers/peft 已装，约 3GB 显存）；失败自动回退规则模式。

> ⚠️ **没有云 LLM、没有图像生成模型**。规则模式不依赖任何模型；LLM 意图模式只做文本字段解析，不生成像素/画风。
> 若 agent 侧有外部 planner，可让它输出 FDSL，但**必须**经过 `schema` 校验。

**运行环境**：

- Python ≥ 3.10；`numpy` + `Pillow`（渲染/导出必需）；
- 同级 checkout `fourier-brushstroke/`（`.fbs` 的渲染与格式权威）；
  也可用环境变量 `FBS_SKILL_DIR` 指向该 skill 根目录或其 `scripts/`；
- 视频额外需要 `imageio` + `imageio-ffmpeg`（缺失时 `video` 明确报错，不影响静态产物）；
- LLM 意图模式额外需要 torch/transformers/peft + 本地 Qwen2.5-1.5B-Instruct；**默认规则路径纯 CPU**；
- demo `--resolution` 上限 **256**，CLI/分层导出上限 512。

```powershell
$PY = "<python>"
cd "<workspace>\fourier-prompt"   # 包根目录
& $PY -m fourier_prompt.cli selftest
```

---

## 快速开始（一键 demo）

```powershell
& $PY scripts\demo.py --plan skill\examples\text_plan.json `
    --outdir out\demo --psd --kra --resolution 256
```

产物（`out\demo\`）：

| 文件 | 内容 |
|---|---|
| `text_plan.fbs` | 编译后的傅里叶笔触文档（JSON） |
| `text_plan_R256.png` | 按长边 256 渲染的 PNG（exact 渲染） |
| `text_plan.fdraw` | 确定性容器（含 `render.json` + `source.fdsl.json` + meta） |
| `layers\text_plan.psd` / `.kra` | 分层导出（`--psd` / `--kra`，fast 渲染） |
| `text_plan_reveal_R256.mp4` | 仅 `--video` 时生成（fast 渲染） |

重复运行同一条命令，`.fbs` / PNG / `.fdraw` **字节一致**；demo 会打印每个产物的
路径、大小、SHA-256 和耗时。任一步失败即非零退出并打印 `FP_E_*` / `FD_E_*` / `VD_E_*` 错误码。

---

## 标准工作流

### 1. 写 plan / 校验

```powershell
& $PY -m fourier_prompt.cli schema --plan skill\examples\text_plan.json --strict
& $PY -m fourier_prompt.cli schema --plan skill\examples\text_plan.json --json
```

- `--strict` 把 warning 升级为错误；**提交前用 strict**。
- 错误带 JSON path（如 `$.elements[2].brush.preset`）；warning 不阻止编译，但会写入 `.fbs.warnings`。
- 最小骨架与字段说明见 `templates/text_to_drawing.md`，可复制 `skill/examples/simple_plan.json` 起手。

### 2. 编译为 `.fbs`

```powershell
& $PY -m fourier_prompt.cli plan2fbs --plan skill\examples\text_plan.json `
    --out out\demo\text_plan.fbs --resolution 256
```

`plan2fbs` 只编译不渲染；`--resolution` 决定 FDSL 宽度的世界单位契约（同一份 plan 可换 R 重编）。

### 3. 渲染 PNG

```powershell
& $PY -m fourier_prompt.cli draw --plan skill\examples\text_plan.json `
    --out out\demo\text_plan_R256.png --resolution 256 --fbs-out out\demo\text_plan.fbs
```

`draw` 使用 skill 的 **exact** 渲染（不是 fast 预览）。

### 4. 打包 `.fdraw`

```powershell
& $PY -m fourier_prompt.fdraw_cli pack `
    --fbs out\demo\text_plan.fbs `
    --source-plan skill\examples\text_plan.json `
    --out out\demo\text_plan.fdraw
& $PY -m fourier_prompt.fdraw_cli inspect --fdraw out\demo\text_plan.fdraw
& $PY -m fourier_prompt.fdraw_cli validate --fdraw out\demo\text_plan.fdraw
```

`.fdraw` 是单一可移植文件，`render.json` 是渲染权威；`source.fdsl.json` 保留源 plan
（用于溯源/重新编译，**不等于**可原地编辑）。

### 5. 分层导出（可选）

```powershell
& $PY -m fourier_prompt.layer_cli export --fdraw out\demo\text_plan.fdraw `
    --out-dir out\demo\layers --psd --kra --resolution 256 --quality fast
# 只看层表、不渲染：
& $PY -m fourier_prompt.layer_cli inspect-layers --fdraw out\demo\text_plan.fdraw
```

不给 `--psd/--kra/--layers-png` 时三种都导出；`--json` 输出机读报告。
**PSD/KRA 没有在真实 Photoshop / Krita 中打开验证过**（见“限制”）。

### 6. 渐进作画视频（可选）

```powershell
& $PY -m fourier_prompt.fdvideo_cli video --fdraw out\demo\text_plan.fdraw `
    --out out\demo\text_plan_reveal.mp4 --resolution 256 --duration 20 --fps 24 --quality fast
# 只看抽帧：
& $PY -m fourier_prompt.fdvideo_cli frames --fdraw out\demo\text_plan.fdraw `
    --out-dir out\demo\frames --count 8 --resolution 256
```

---

## 命令清单（从包根目录执行）

| 命令 | 作用 |
|---|---|
| `python -m fourier_prompt.cli schema --plan P.json [--strict] [--json]` | FDSL 校验 |
| `python -m fourier_prompt.generate_cli --prompt "..." [--style S] [--seed N] [--out P.json] [--png X.png] [--fdraw Y.fdraw]` | prompt → FDSL/PNG/`.fdraw`（规则模式） |
| `python -m fourier_prompt.svg_import --svg X.svg [--out P.json] [--png X.png] [--fdraw Y.fdraw] [--resolution R]` | **复杂图像**：SVG → FDSL → PNG/`.fdraw`（确定性；无本地模型） |
| `python -m fourier_prompt.llm.intent --prompt "..." [--out P.json] [--render X.png] [--rule-only] [--json]` | prompt → 意图 JSON → 规则 planner（可选本地 LLM；**默认不用**） |
| `python -m fourier_prompt.cli plan2fbs --plan P.json --out X.fbs [--resolution R]` | plan → `.fbs` |
| `python -m fourier_prompt.cli draw --plan P.json --out X.png [--resolution R] [--fbs-out X.fbs]` | plan → PNG（exact） |
| `python -m fourier_prompt.cli selftest` / `version` | 内置自检 / 版本 |
| `python -m fourier_prompt.fdraw_cli pack --fbs X [--source-plan P] --out Y.fdraw` | `.fbs` → `.fdraw` |
| `python -m fourier_prompt.fdraw_cli unpack --fdraw Y [--out-dir D]` | 解包 + 校验 root hash |
| `python -m fourier_prompt.fdraw_cli inspect --fdraw Y [--json]` | 容器/渲染元数据 |
| `python -m fourier_prompt.fdraw_cli validate --fdraw Y` | 容器完整性校验 |
| `python -m fourier_prompt.fdrender_cli render --fdraw Y --out X.png [--resolution R] [--quality fast\|exact]` | 从容器快速渲染 |
| `python -m fourier_prompt.fdrender_cli preview --fdraw Y --fraction 0.3 --out X.png` | 前 N% 元素的预览 |
| `python -m fourier_prompt.fdrender_cli frame --fdraw Y --at 20 --out X.png` | 第 N 个元素处的帧 |
| `python -m fourier_prompt.fdrender_cli compare --a A.png --b B.png [--json]` | 两图差异指标 |
| `python -m fourier_prompt.fdvideo_cli video --fdraw Y --out X.mp4` | 渐进作画视频 |
| `python -m fourier_prompt.fdvideo_cli frames --fdraw Y --out-dir D --count 8` | 抽帧 PNG |
| `python -m fourier_prompt.layer_cli export --fdraw Y --out-dir D [--psd] [--kra] [--layers-png]` | 分层导出 |
| `python -m fourier_prompt.layer_cli inspect-layers --fdraw Y` | 层表 |
| `python -m fourier_prompt.mcp_server` | stdio JSON-RPC MCP server（8 个工具） |
| `python scripts/demo.py --plan P --outdir D [--psd] [--kra] [--video]` | 一键演示（本 skill 的入口） |

---

## 复杂图像：SVG → FDSL（推荐路线）

当目标不是简单几何，而是人物/场景/插画时：

1. **agent（在线）编写/程序化生成 SVG**（或使用许可明确的在线 text→SVG 服务；不要用本地小模型）；
2. `python -m fourier_prompt.svg_import --svg X.svg --out plan.json --png out.png --fdraw out.fdraw --resolution 384`；
3. 后续编译/渲染/分层/视频全部复用现有链路：开放路径→FFT 笔画、闭合填充→fill，**傅里叶由代码算，不由 LLM 算**。

```powershell
& $PY -m fourier_prompt.svg_import --svg ..\poc\complex_online\svg\349708_portrait-of-a-beauty.svg `
    --out out\svg\portrait.json --png out\svg\portrait.png --resolution 384 --json
```

限制：渐变/pattern/filter 近似、text/image/use 跳过；默认 600 元素/32 色上限（超限 trim + warning）；复杂 SVG 渲染可能数秒到数十秒。

---

## FDSL plan 最小骨架

```json
{
  "schema": "fdsl/2.1",
  "name": "my_drawing",
  "canvas": {"width": 384, "height": 256, "background": [250, 248, 242]},
  "palette": {"ink": "#1e2430", "accent": "#c04030"},
  "layers": [
    {"id": 0, "name": "base"},
    {"id": 1, "name": "ink"}
  ],
  "elements": [
    {"kind": "fill", "layer": 0, "color": "ink",
     "poly": [[12, 12], [372, 12], [372, 244], [12, 244]]},
    {"kind": "stroke", "layer": 1, "points": [[40, 200], [120, 150], [260, 210]],
     "brush": {"preset": "ink_gpen", "width": 2.5, "color": "accent", "alpha": 0.9}},
    {"kind": "fourier_gen", "layer": 1, "gen": "star",
     "params": {"at": [320, 60], "r": 24, "ri": 10, "n": 5},
     "brush": {"preset": "ink_fineliner", "width": 2, "color": "ink"}},
    {"kind": "text", "layer": 1, "as": "outline", "text": "你好",
     "font": {"id": "source_han_sans_sc", "size_px": 40, "color": "ink"},
     "box": [24, 24, 220, 64]}
  ],
  "constraints": {"max_elements": 400}
}
```

要点：
- 每个 element 必须有 `layer`，且指向 `layers[].id`；
- `kind` ∈ `fourier_gen` / `stroke` / `fill` / `text`；
- `brush.preset` 必须是 20 个内置预设之一；未知值直接报错，不猜；
- `fill` 用 `poly`（≥3 点，可带 `holes`）；`stroke` 用 `points`（≥2 点）；
- `fourier_gen` 的 `gen` ∈ circle/ellipse/arc/regular_polygon/star/rose/spiral/wave/lissajous/superellipse；
- 文本目前只支持 `as: "outline"`（烘焙成 fills），字体只能用打包的 Noto Sans SC 子集。

完整字段、修复规则和错误码见包根 `README.md` 与 `fourier_prompt/schema.py`。

---

## 质量、确定性与分辨率

- **exact 是权威**：`cli draw`、demo 的 PNG、`fdrender --quality exact` 都走 skill 精确渲染。
- **fast 是预览/导出加速档**：质量口径为 **PSNR ≥ 30 dB 且 within2 ≥ 80%**；
  细笔触的 `max|Δ|` 仍可能到 40–60，**不能**用它做像素级对齐或验收；
  需要逐像素一致时用 `--quality exact`。fast 不支持某些笔形时会自动回退 exact 并记录 `fallback_reason`。
- **确定性**：同一 plan + 同一 resolution，`.fbs` / PNG / `.fdraw` 字节可复现。
  demo 会移除 `.fbs` 的墙钟字段 `created`（纯元数据，`compile.normalized_fbs` 同样排除它）；
  直接调用 `cli plan2fbs` 时该字段存在，但规范化 hash 仍稳定。
- **分辨率**：demo 上限 256；CLI 与分层导出上限 512。R 只影响采样密度与宽度契约，
  不改变 plan 语义；跨 R 的数字不可直接比较。

---

## 限制（诚实清单）

1. **无本地 LLM**：自然语言 → FDSL 由 agent 完成；本包不调用模型、不联网。
2. **无字体回退**：只打包 Noto Sans SC OFL 子集；编译器接受
   `noto_sans_sc` / `noto_sans_cjk_sc` / `source_han_sans_sc` 三个 id（schema 还列了其它内置 id，
   但编译器会对未打包字体抛 `FP_E_TEXT_FONT`）。缺字抛 `FP_E_TEXT_GLYPH`，
   **不会**退回系统字体。`as: "glyph"`（嵌入字体光栅）未实现，只能用 outline。
   文本被烘焙为填充轮廓，不是可编辑文字对象。
3. **无编辑 op**：`fdsl-edit/1` 尚未实现。`.fdraw` 里的 `source.fdsl.json` 用于溯源，
   不能原地编辑；修改方式是改 plan 后重新编译。见 `templates/edit_prompt.md`。
4. **PSD/KRA 未真机验证**：按公开格式写出并用 Pillow/自写解析器回读，但未在真实
   Photoshop / Krita 5.x 中打开抽检；ICC/字体等细节可能与商业软件行为有差异，按“可交换草稿”使用。
5. **fast 质量档是近似**：见上节；`exact` 才是权威。fast 并非所有笔形都支持，回退会记录原因。
6. **图层可见性/不透明度**：`.fbs` 笔触模型没有逐层可见性，编译期只给 warning；
   分层导出会按元数据烘焙 opacity / 标记 visible。不要把 plan 的 layer 设置当成渲染时图层开关。
7. **渲染是 CPU only**：没有 GPU 渲染加速；大 R / 长视频耗时线性增长；视频依赖 `imageio` + `imageio-ffmpeg`。可选 LLM 意图模式会占用 GPU（约 3GB），但只做文本字段解析，不参与渲染。
8. **不是图像重建**：不保证复刻参考图；位图转笔触请用 `fourier-brushstroke`。
9. **生成是模板组合**：规则/意图模式支持 15 类简单主体、数量/位置/颜色/文字/风格；**不支持人物/动漫角色/复杂场景/透视/光影**，也不学习任何参考图画风。接近参考图风格需要另立“图像→FDSL/线稿”研究。
10. **全 FDSL LLM 生成尚不可用**：本地 Qwen 零样本/短 LoRA 输出不合法或低质；当前稳定的是“意图 JSON + 规则 planner”混合路径。

---

## 退出码与排障

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 校验/编译/渲染/导出失败（`FP_E_*`、`FD_E_*`、`VD_E_*`） |
| 2 | 用法/输入错误（参数、文件不存在、JSON 非法） |
| 3 | 内部/IO 错误 |
| 130 | Ctrl+C |

常见错误：
- `FP_E_LOAD` / `FP_E_JSON`：plan 路径或 JSON 有问题；
- `FP_E_LAYER_MISSING` / `FP_E_LAYER_REF`：element 缺 `layer` 或引用了未声明的层；
- `FP_E_COMPILE_BRUSH`：未知 `brush.preset`/`shape` 或宽度曲线非法；
- `FP_E_TEXT_FONT` / `FP_E_TEXT_GLYPH`：字体未打包 / 字符不在子集（不会回退）；
- `FP_E_COMPILE_EMPTY`：所有元素被跳过或为空；
- `FD_E_*`：`.fdraw` 容器损坏、root hash 不匹配或输入非法；
- `VD_E_*`：视频参数/编码失败（先确认 `imageio-ffmpeg` 可用）。

---

## 文件导航

| 文件 | 用途 |
|---|---|
| `skill/examples/text_plan.json` | 含文本 + 笔刷 + 生成元的完整样例（推荐起点） |
| `skill/examples/simple_plan.json` | 无字体依赖的最小样例（smoke test） |
| `skill/examples/README.md` | 两个样例的说明与命令 |
| `skill/templates/text_to_drawing.md` | prompt → FDSL 的 agent 模板/检查表 |
| `skill/templates/edit_prompt.md` | 编辑请求模板；**明确编辑 op 未实现** |
| `scripts/demo.py` | 一键流水线（本 skill 的入口） |
| `README.md`（包根） | 安装、校验规则、API、错误码 |
| `fourier-prompt` 包内 `reports/WP-M.md` | 本封装批次的命令与验收记录 |
