# fourier-prompt Skill（人类阅读版）

`fourier-prompt/skill/` 是给 agent 和人类共用的**使用层**封装：把 FDSL v2.1 绘图计划
编译为傅里叶笔触 `.fbs`、PNG、可移植 `.fdraw`，并可导出分层 PSD/KRA 与渐进作画视频。

> 本目录只写“怎么用”；FDSL 校验/修复/编译的权威实现在包根的 `fourier_prompt/`，
> `.fbs` 渲染与格式权威在同级 `fourier-brushstroke/`（只读依赖）。

---

## 安装与依赖

**要求**：

- Python ≥ 3.10（`pyproject.toml` 声明）；
- `numpy`、`Pillow`：渲染与导出必需；
- 同级 checkout `../fourier-brushstroke/`（其中含 `scripts/fbs_core/`）；
  若不在默认位置，设置环境变量：
  ```powershell
  $env:FBS_SKILL_DIR = "<workspace>\fourier-brushstroke"
  ```
- 视频可选：`imageio`、`imageio-ffmpeg`（缺失时只有 `--video` 失败，静态产物不受影响）；
- 无需安装本包：在 `fourier-prompt/` 包根直接 `python -m ...` 即可（纯 stdlib + 上述依赖）。

**环境自检**：

```powershell
$PY = "<python>"
cd "<workspace>\fourier-prompt"
& $PY -m fourier_prompt.cli selftest
& $PY -m fourier_prompt.cli version
```

---

## Quickstart（3 分钟）

```powershell
# 1) 一键 demo：plan -> .fbs -> PNG -> .fdraw -> PSD/KRA
& $PY scripts\demo.py --plan skill\examples\text_plan.json `
    --outdir out\demo --psd --kra --resolution 256

# 2) 产物
Get-ChildItem out\demo -Recurse | Select-Object FullName, Length
```

预期输出（数值随机器略有差异）：

```
[1/6] schema       ok  elements=5 layers=3 texts=0 warnings=0 (0.000s)
[2/6] plan2fbs     ok  out\demo\text_plan.fbs 338.3 KiB sha256=... records=77 ...
[3/6] draw         ok  out\demo\text_plan_R256.png 9.3 KiB 256x171 sha256=... (0.172s)
[4/6] fdraw pack   ok  out\demo\text_plan.fdraw 38.0 KiB root_sha256=... elements=77 layers=[0, 1, 2]
[5/6] layer export ok  psd=out\demo\layers\text_plan.psd 812.7 KiB; kra=... 39.1 KiB layers=3
[6/6] video        skipped (pass --video)
done: out\demo in 3.8s
```

| 产物 | 说明 |
|---|---|
| `<stem>.fbs` | 傅里叶笔触文档（JSON；可由 `fourier-brushstroke` 渲染） |
| `<stem>_R<R>.png` | exact 渲染的静态图（长边 = R；PNG 尺寸按画布宽高比） |
| `<stem>.fdraw` | 确定性 ZIP 容器：`render.json` + `source.fdsl.json` + `meta.json` |
| `layers/<stem>.psd` / `.kra` | 分层导出（`--psd` / `--kra`） |
| `<stem>_reveal_R<R>.mp4` | 渐进作画视频（`--video`） |

**不写 demo 时**，也可以逐步调用 CLI，见 `skill/SKILL.md` 的“标准工作流”。

---

## 两个示例

| 文件 | 内容 | 用途 |
|---|---|---|
| `skill/examples/text_plan.json` | 中文/英文 outline 文本 + 笔刷下划线 + 五角星；3 层 | 主 demo，覆盖文本/笔刷/生成元/图层/PSD/KRA |
| `skill/examples/simple_plan.json` | 圆 + 六角星填充 + 一条笔刷曲线；无字体依赖 | 快速冒烟、CI、最小可编译样例 |

```powershell
& $PY scripts\demo.py --plan skill\examples\simple_plan.json --outdir out\simple --resolution 128
& $PY -m fourier_prompt.cli schema --plan skill\examples\text_plan.json --strict
```

两个示例都通过 `--strict` 校验，并已在 `tests/skill/` 中锁定可编译性。

写自己的 plan 时用 `skill/templates/text_to_drawing.md`（prompt → FDSL 模板与检查表）；
编辑请求见 `skill/templates/edit_prompt.md`（明确说明编辑 op 尚未实现）。

---

## 目录结构

```
fourier-prompt/
├── skill/
│   ├── SKILL.md                 # agent 入口（frontmatter / 命令 / 流程 / 限制）
│   ├── README.md                # 本文件
│   ├── templates/
│   │   ├── text_to_drawing.md   # prompt -> FDSL 的写作模板与检查表
│   │   └── edit_prompt.md       # 编辑请求模板（当前编辑 op 未实现）
│   └── examples/
│       ├── text_plan.json
│       ├── simple_plan.json
│       └── README.md
├── scripts/
│   └── demo.py                  # 一键 demo（只编排 CLI/API）
├── tests/skill/                 # ≥5 例：示例、demo、SKILL 字段、模板引用
└── reports/WP-M.md              # 本批次的命令/产物/限制记录
```

---

## 运行测试

```powershell
cd "<workspace>\fourier-prompt"
& $PY -m unittest discover -s tests/skill -v
```

测试只用 stdlib `unittest`，覆盖：

- 两个示例可严格校验、可编译；
- demo 成功、参数错误、可选 PSD/KRA 导出、重复运行确定性（`.fbs`/PNG/`.fdraw` hash）；
- `SKILL.md` frontmatter/命令/限制字段；
- 模板与示例 README 的交叉引用、编辑模板的“未实现”声明。

---

## 已知限制（摘要）

1. **无本地 LLM**：自然语言必须先由 agent 写成 FDSL plan；本包不做语义生成。
2. **无字体回退**：仅打包 Noto Sans SC 子集；缺字/未打包字体会报错，不会退回系统字体。
3. **无编辑 op**：`fdsl-edit/1` 未实现；修改只能改 plan 重新编译。
4. **PSD/KRA 未真机验证**：仅在 Pillow/自写解析器层面回读，未在真实 Photoshop/Krita 打开。
5. **fast 质量档**：质量口径 PSNR ≥ 30 dB、within2 ≥ 80%；`exact` 才是权威。
6. 文本只支持 `as: "outline"`（烘焙为填充），`as: "glyph"` 未实现。

完整说明见 `skill/SKILL.md` “限制（诚实清单）”与包根 `README.md`。

---

## 故障排查

| 现象 | 先查 |
|---|---|
| `cannot locate fourier-brushstroke scripts/` | 同级目录是否存在；或设置 `FBS_SKILL_DIR` |
| `FP_E_LOAD` / `FP_E_JSON` | plan 路径、UTF-8 JSON、文件是否存在 |
| `FP_E_LAYER_MISSING` | 每个 element 是否都有 `layer`，且层已声明 |
| `FP_E_TEXT_FONT` / `FP_E_TEXT_GLYPH` | 字体 id 是否打包、字符是否在子集 |
| `FD_E_ZIP` / `FD_E_HASH` | `.fdraw` 是否被截断/手改；重新 pack |
| `VD_E_*` | `imageio` / `imageio-ffmpeg` 是否可用，`--duration/--fps` 是否合法 |
| PSD/KRA 打不开 | 当前只保证格式结构自检；用真实软件抽检后再报 bug |

退出码：0 成功，1 业务失败，2 用法/输入错误，3 内部/IO，130 中断。
