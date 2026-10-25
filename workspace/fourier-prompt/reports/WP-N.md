# WP-N — 规则 planner + 生成 CLI + 20 prompt benchmark（共享任务 task-24）

- 日期：2026-10-20 ｜ 状态：**完成**
- 写盘范围：`fourier_prompt/{planner.py,prompts.py,generate_cli.py}`、`tests/planner/`、`assets/eval/prompts_20.jsonl`、`out/planner/`、`reports/WP-N.md`
- 未改动：`fourier-brushstroke/`（skill）、`fourier_prompt/` 其他核心文件（只读）、`docs/`、`analysis/`、用户图文件夹
- 环境：Windows / Python 3.13.14 / numpy 2.5.4 / Pillow 12.3.0；纯 CPU、离线、无 LLM、不装包；渲染 R=256

---

## 1. 结论

- `plan_prompt(prompt, *, style=None, seed=0) -> (plan, warnings)` 已实现：中英文关键词 → 规范 FDSL v2.1（`palette/layers/elements/texts`），文本走 `as: "outline"`，输出前调用 `schema.validate_plan_detailed` 自检。
- **20/20 prompt 编译 + 渲染成功**；同一 prompt+seed 两次 plan 的规范化 SHA-256 **20/20 相同**；仅 2 条带 warning（1 条未知主体降级、1 条否定语处理），无崩溃。
- `tests/planner/` 共 **52 例全部通过**（planner 40 例 + CLI 12 例）。
- `generate_cli` 三类输出（plan JSON / PNG / .fdraw）可用，退出码 `0/1/2/3` 已覆盖测试。
- 全部随机性来自 `random.Random(seed)`，无时钟、无网络、无外部服务。

---

## 2. 交付文件

| 文件 | 行数 | 字节 | 说明 |
|---|---:|---:|---|
| `fourier_prompt/prompts.py` | 499 | 16.9 KiB | 主体/颜色/风格/位置/数量词表；9 个风格包（仅引用 catalog active 笔刷） |
| `fourier_prompt/planner.py` | 1094 | 39.7 KiB | 解析 + 槽位 + 15 类主体几何构图 + FDSL 组装 + 哈希/摘要 |
| `fourier_prompt/generate_cli.py` | 386 | 14.8 KiB | prompt/side outputs/benchmark CLI |
| `tests/planner/test_planner.py` | 307 | 13.6 KiB | 40 例：关键词/颜色/文字/风格/seed/失败/编译渲染 |
| `tests/planner/test_generate_cli.py` | 158 | 5.9 KiB | 12 例：三类输出/退出码/benchmark |
| `assets/eval/prompts_20.jsonl` | 20 | 2.3 KiB | 20 条中英文评测 prompt（主体/文字/风格/比例/否定/长 prompt/未知主体） |
| `out/planner/benchmark.json` | — | 14.4 KiB | 逐条结果 + 汇总 |
| `out/planner/contact_sheet.png` | — | 158 KiB | 4×5 缩略图（256 px/格，带编号/风格/主体标签） |
| `out/planner/png/*.png` | — | 145 KiB | 20 张 R256 渲染图 |
| `out/planner/cli_demo/*` | — | 53 KiB | CLI 三输出示例（cat.json/png/fdraw） |

---

## 3. 能力（v1）

### 3.1 词表与槽位

| 槽位 | 支持 |
|---|---|
| 主体（15） | 圆 circle / 星 star / 心 heart / 云 cloud / 山 mountain / 树 tree / 房 house / 猫 cat / 花 flower / 太阳 sun / 月亮 moon / 波浪 wave / 雪 snow / 眼睛 eye / 几何 geometry |
| 数量 | 阿拉伯数字、中文数字（一…十/两）、`个/颗/只/朵/座/棵/片/轮/枚/条/张`；1–12，超出截断 + warning |
| 位置 | 居中/左/右/上/下；网格 grid/九宫格/阵列；一排/row；重复/平铺/repeat |
| 颜色 | 黑/白/灰/红/蓝/绿/金/紫/橙/粉/青/棕/黄 + `#rrggbb`；首个颜色→主色，第二个→辅色；`X色背景`→背景 |
| 文字 | 引号（`"" '' 「」『』 “”`）、`文字:`/`text:`/`写上`/`标题:`；最多 2 条、每条 ≤40 字；中英混排走内置 Noto Sans SC outline |
| 风格 | 黑白/线稿→`mono_ink`、漫画→`manga_color`、铅笔→`pencil`、水墨→`ink_wash`、马克笔→`marker`、油画→`oil`、水彩→`watercolor`、极简/扁平→`minimal`、赛博/neon→`cyber` |
| 比例 | 竖版 768×1024、横版 1024×768、方形 768×768（默认方形；渲染长边仍由 `--resolution` 决定） |
| 否定 | `不要文字/no text`、`不要背景/no background`、`不要红色/without red`；其余否定给 warning 并忽略 |

### 3.2 风格包 → active 笔刷 + 调色板 + 背景

所有笔刷均通过 `prompts.py` 导入期断言，只允许 `schema.BRUSH_PRESETS`（catalog 20 条 active）。

| 风格包 | 填充笔 | 线笔 | 细节笔 | 背景 | 主色示例 |
|---|---|---|---|---|---|
| `mono_ink` | `basic_round_hard` (α0.96) | `ink_gpen` | `ink_fineliner` | 纸白 #fefdfd | 灰阶（ink #060505 / mid #a4a2a2 / shadow #686566） |
| `manga_color` | `basic_round_hard` (α1.0) | `ink_brush_rough` | `ink_gpen` | 白纸 | 红/蓝/黄 |
| `pencil` | `pencil_hb` (α0.35) | `pencil_2b` | `pencil_hb` | 米纸 | 灰绿 |
| `ink_wash` | `watercolor_wash` (α0.35) | `sumi_e` | `ink_gpen` | 宣纸 | 淡墨 |
| `marker` | `marker_chisel` (α0.82) | `marker_chisel` | `ink_fineliner` | 白纸 | 黄/蓝/粉 |
| `oil` | `bristle_oil` (α0.95) | `ink_brush_rough` | `ink_gpen` | 米黄 | 橙/蓝/橄榄 |
| `watercolor` | `watercolor_wash` (α0.55) | `ink_gpen` | `ink_fineliner` | 白纸 | 粉/青/黄 |
| `minimal` | `basic_round_hard` (α1.0) | `basic_round_hard` | `ink_fineliner` | 纯白 | 红/蓝/黄 |
| `cyber` | `marker_alcohol` (α0.90) | `ink_fineliner` | `ink_fineliner` | 深底 #0c0c16 | 霓虹粉/青/紫 |

> 注：`style_packs.json` 现有 6 包（ink_wash/pencil/marker/oil/manga/watercolor）；`mono_ink`、`manga_color`、`minimal` 由本包按同一笔刷目录补齐。`mono_ink` 已按 WP-P 的 `assets/eval/style_rubric.json` 对齐（paper/haze/mid/shadow/ink 五档、cap `basic_round_hard` 平涂、contour `ink_gpen`、hairline `ink_fineliner`）；若 rubric 再调整，本表需同步。

### 3.3 明确不支持（会降级或报错，不静默）

- 未知主体（如「龙」「鲸鱼」）→ **通用几何构图**（圆/三角/星/螺旋 + seed 装饰点）+ `FP_W_SUBJECT_UNKNOWN`，仍可编译渲染；
- prompt 只有动作词（如「画」）→ 同上通用构图 + warning；
- 空/全空白 prompt、无字母/数字/汉字的怪输入 → `PlannerError`，不返回空图；
- 复杂语义关系（「猫在房子后面」「左边的山比右边高」）、任意物体、写实照片、多轮编辑→ 不在 v1 范围；
- 文本最多 2 条/每条 40 字，固定在底部文字带（不解析「文字在顶部」）；`as=glyph` 不支持；
- 多主体最多 3 个，超出截断 + warning；数量最多 12，超出截断 + warning。

---

## 4. 用法

```powershell
$PY = "<python>"
$FP = "<workspace>\fourier-prompt"
cd $FP

# 1) prompt → plan / PNG / .fdraw（三输出可任选）
& $PY -m fourier_prompt.generate_cli --prompt "画一只黑色的猫，线稿风格，居中" `
    --seed 1 --out out\planner\cli_demo\cat.json --png out\planner\cli_demo\cat.png `
    --fdraw out\planner\cli_demo\cat.fdraw --resolution 256

# 2) 20 prompt benchmark（写 benchmark.json + contact_sheet.png）
& $PY -m fourier_prompt.generate_cli --benchmark assets\eval\prompts_20.jsonl --out-dir out\planner --resolution 256

# 3) 测试
& $PY -m unittest discover -s tests/planner -v
```

CLI 参数：`--prompt/-p`、`--style/-s`、`--seed`、`--out`、`--png`、`--fdraw`、`--resolution`（16–512，默认 256）、`--benchmark`、`--out-dir`、`--json`。

### 退出码

| 码 | 含义 | 触发 |
|---:|---|---|
| 0 | 成功（warning 不失败） | 正常生成/benchmark 全渲染 |
| 1 | 编译/渲染/IO 失败 | 字体缺字、渲染异常、写盘失败 |
| 2 | 用法错误 | argparse、缺 `--prompt`、`--resolution` 越界、benchmark 输入坏 |
| 3 | planner 错误 | 空 prompt、无有效字符、prompt 超 2000 字、seed/style 类型错 |

---

## 5. 20 prompt benchmark 结果（R=256）

汇总：**20 plan / 20 compile / 20 render / 20 deterministic**，共 291 个 FDSL 元素，总耗时 6.17 s（CPU）。warning 2 条，均为显式降级。

| # | prompt（截断） | 主体 | 风格 | 元素 | 文字 | warnings | s |
|---|---|---|---|---:|---:|---|---:|
| 01 | 画一只黑色的猫，线稿风格，居中 | cat | mono_ink | 20 | 0 | - | 1.18 |
| 02 | 一个红色的爱心，水彩风格 | heart | watercolor | 2 | 0 | - | 0.12 |
| 03 | 紫色的星星，网格排列，9个，方形 | star | minimal | 18 | 0 | - | 0.32 |
| 04 | 蓝色的波浪，横幅 | wave | minimal | 3 | 0 | - | 0.10 |
| 05 | 金色的太阳，居中，极简风格 | sun | minimal | 14 | 0 | - | 0.22 |
| 06 | 月亮和山，竖版，水墨 | moon,mountain | ink_wash | 11 | 0 | - | 0.16 |
| 07 | 画一朵花，文字："春天" | flower | minimal | 11 | 1 | - | 0.37 |
| 08 | 一棵树，绿色，铅笔画，左边 | tree | pencil | 7 | 0 | - | 0.23 |
| 09 | 一座房子，右边，漫画风格 | house | manga_color | 8 | 0 | - | 0.19 |
| 10 | 雪花，蓝白，赛博朋克，方形 | snow | cyber | 31 | 0 | - | 0.47 |
| 11 | 画一只眼睛，水墨风格，文字: EYE | eye | ink_wash | 9 | 1 | - | 0.20 |
| 12 | 几何图形组合，红蓝金，网格，6个 | geometry | minimal | 36 | 0 | - | 0.40 |
| 13 | 云朵，三朵，上方，水彩 | cloud | watercolor | 21 | 0 | - | 0.29 |
| 14 | a black cat, manga style, centered | cat | manga_color | 20 | 0 | - | 0.33 |
| 15 | draw a golden star with text "STAR", minimal style | star | minimal | 2 | 1 | - | 0.18 |
| 16 | 画三颗心，红粉，重复排列，横版 | heart | minimal | 6 | 0 | - | 0.16 |
| 17 | 雪山，黑白，横版，不要文字 | snow,mountain | mono_ink | 37 | 0 | `FP_W_NEGATION_APPLIED` | 0.49 |
| 18 | 五颗星星，紫色，深色背景，方形（`--style cyber`） | star | cyber | 10 | 0 | - | 0.20 |
| 19 | 一条龙，线稿风格，居中 | generic | mono_ink | 8 | 0 | `FP_W_SUBJECT_UNKNOWN` | 0.19 |
| 20 | 极简海报：金色太阳+蓝色波浪，文字："夏日 2026"，竖版 | sun,wave | minimal | 17 | 1 | - | 0.36 |

!20 prompt contact sheet

产物：`out/planner/benchmark.json`（逐条 plan_hash/elements/compile/render/warnings/耗时）、`out/planner/png/p01..p20.png`、`out/planner/contact_sheet.png`。

### 示例

| 猫（黑/线稿/居中） | 花 + 中文文字 |
|---|---|
| !cat | !flower text |

| 眼睛 + 英文文字 | 长 prompt 海报（太阳+波浪+文字） |
|---|---|
| !eye | !poster |

---

## 6. 测试

```powershell
cd $FP
& $PY -m unittest discover -s tests/planner -v
# Ran 52 tests ... OK
```

| 测试组 | 例数 | 覆盖 |
|---|---:|---|
| `SubjectAndColorCase` | 7 | 中文猫、英文猫/manga、雪花最长别名、hex 颜色、否定颜色、背景色、深色背景不误判 |
| `CountLayoutRatioCase` | 5 | 9 个网格、三颗心重复、数量截断、三种比例、左右位置几何差异 |
| `TextCase` | 5 | 引号中文、`text: EYE`、前缀引号剥离、no text 否定、文字 box 在画布内 |
| `StyleCase` | 4 | 9 包全可解析、显式 style 覆盖 prompt、未知 style 回退、7 个中文别名 |
| `DeterminismAndSchemaCase` | 5 | 同 seed 哈希一致、seed 改变 fallback、JSON 无 NaN、schema/layers/id 连续、15 主体全覆盖 |
| `FailureCase` | 9 | 空/空白/纯符号/emoji、类型错、超长、坏 seed/style、仅动作词、未知主体 |
| `CompileRenderCase` | 5 | 猫/文字/多主体/未知主体编译渲染、坐标有限 |
| CLI（`CliOutputCase`/`ExitCodeCase`/`BenchmarkCase`） | 12 | plan/png/fdraw 三输出、seed 复跑字节一致、`--json`、2/3/0 退出码、2-prompt benchmark + contact sheet |

DoD 对照：≥20 单测（40 planner + 12 CLI）；20/20 benchmark；三类输出可用；同 prompt+seed 哈希相同（benchmark `deterministic=20/20`）。

---

## 7. 失败/降级与错误码

| 情况 | 行为 | 码 |
|---|---|---|
| 未知主体 | 通用几何构图 + 继续 | `FP_W_SUBJECT_UNKNOWN` |
| 不支持的否定 | 移除否定短语 + warning | `FP_W_NEGATION_UNSUPPORTED` |
| 否定文字/背景/颜色 | 按否定执行 + warning | `FP_W_NEGATION_APPLIED` |
| 数量 >12 / 主体 >3 / 文字 >40 字 | 截断 + warning | `FP_W_COUNT_CLAMPED` / `FP_W_SUBJECT_TRUNCATED` / `FP_W_TEXT_TRUNCATED` |
| 未知 style 参数 | 回退 prompt/默认风格 + warning | `FP_W_STYLE_UNKNOWN` |
| 空/空白 prompt | 不返回 plan | `FP_E_PROMPT_EMPTY` |
| 无字母/数字/汉字 | 不返回 plan | `FP_E_PROMPT_UNPARSEABLE` |
| prompt 类型/长度 | 不返回 plan | `FP_E_PROMPT_TYPE` / `FP_E_PROMPT_TOO_LONG` |
| seed/style 类型 | 不返回 plan | `FP_E_SEED_TYPE` / `FP_E_STYLE_TYPE` |
| 内部 schema 自检失败 | 抛错（本版本实测未触发） | `FP_E_INTERNAL` |

计划先经 `schema.validate_plan_detailed` 校验，再由 `compile_plan` 二次校验；未知 gen/笔刷/字段不会静默通过。

---

## 8. 规模与性能

- 20 条 prompt：元素 **2–37**（均值 14.6，合计 291）；plan 紧凑 JSON **1.3–25.2 KiB**（均值 6.6 KiB，p12 最大因 6 组几何共 36 元素）。
- 单条 benchmark（plan+compile+render）**0.10–1.18 s**，20 条合计 6.17 s；首条 1.18 s 含字体/模块首次加载。
- CLI 示例（`--seed 1`）：`cat.json` 20.9 KiB（indent=1）、`cat.png` 10.3 KiB、`cat.fdraw` 22.7 KiB。
- 成本约束：纯 CPU、R=256、单线程；无 numpy 之外新增依赖；无网络/LLM。

---

## 9. 风险与未完成

1. **这是模板几何，不是语义理解**：猫/房子/眼睛由圆、椭圆、三角、弧线组合；复杂 prompt 只能近似，视觉保真度低于扩散模型。未知主体只给抽象通用构图（有 warning）。
2. **风格包来源**：`style_packs.json` 只有 6 包，`mono_ink/manga_color/minimal` 由本包补齐（笔刷取 catalog active 20 条）；`mono_ink` 已与 WP-P `style_rubric.json` 对齐（灰阶五档 + ink_gpen 轮廓 + ink_fineliner 细节 + basic_round_hard 平涂），后续 rubric 若调整需同步。
3. **月亮实现**：用同色 `paper` 圆覆盖主圆形成弯月，会覆盖其下层元素；当前只在单主体/侧边布局安全，后续应改为显式月牙多边形。
4. **文字限制**：2 条 × 40 字、固定底部文字带、仅 Noto Sans SC outline；不支持 glyph/字体选择/顶部文字。
5. **数量/主体上限**：>12 截断、>3 主体截断，只给了 warning。
6. **CLI 分辨率上限 512**（编译支持 4096），是为本包时间/内存预算主动收紧；R≤256 是评测口径。
7. **没有 LLM planner**：本包是可立即使用的确定性基线（WP-O 训练后可用于 A/B）；不含 JSON 修复/LLM 推理。

---

## 10. 复现与产物核对

```powershell
cd $FP
& $PY -m unittest discover -s tests/planner -v
& $PY -m fourier_prompt.generate_cli --benchmark assets\eval\prompts_20.jsonl --out-dir out\planner --resolution 256
& $PY -c "import json;d=json.load(open('out/planner/benchmark.json',encoding='utf-8'));print(d['summary'])"
# {'total': 20, 'plan_ok': 20, 'compiled': 20, 'rendered': 20, 'deterministic': 20, ...}
```

- 同 prompt+seed 哈希：benchmark 每行 `plan_hash`，重复跑两次 `deterministic=true`。
- 产物目录：`out/planner/benchmark.json`、`out/planner/contact_sheet.png`、`out/planner/png/`、`out/planner/cli_demo/`。
