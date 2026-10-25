# WP-Q 报告 — 混合路径：LLM 解析意图 JSON + 规则 planner 画图

- 日期：2026-10-20 ｜ 状态：**已交付并实测（5/5 意图合法，5/5 plan+compile+render）**
- 共享任务：task-27 ｜ 上游：规则 planner（WP-N，20/20 benchmark）、本地 LLM 管线（WP-O）
- 写盘范围：`fourier_prompt/llm/intent.py`、`tests/llm/test_intent.py`、`out/llm/intent/`、本报告。**未改** skill/核心/planner/docs/analysis/用户图/模型，也未改任何已有 `llm/` 文件（只读 import）。
- 动机：全 FDSL 生成训练尚不成熟（阶段 2 验收：base 零样本 3 次校验失败→规则回退，72 s）。本任务把 LLM 的作用收缩到**意图解析**，画图交回确定性的规则 planner，先保证“可用”。

## 1. 方法

```
prompt(zh/en) ──Qwen2.5-1.5B-Instruct(few-shot)──> intent JSON(9 字段)
                                                        │ 规范化/校验/回退(warnings)
                                                        ▼
                            intent_to_prompt(词表中文别名) + plan_prompt(style=...)
                                                        ▼
                                          FDSL v2.1 plan → compile → PNG
```

| 层 | 实现 |
|---|---|
| few-shot prompt | 中英双语系统提示：9 字段 + 6 条示例（中文 4 / 英文 2），subject/style/colors/position/canvas 取值**直接取自** `fourier_prompt.prompts`（15 主体 / 9 风格 / 13 颜色+hex / 5 位置 / 3 比例），避免词表漂移；`max_new_tokens` 默认 256（实测输出 186–207 字符） |
| 容错提取 | 复用 `formatting.extract_json`（```json 围栏/前后废话/BOM/尾逗号/`{"intent":{...}}` 信封）；另容忍 `subjects/colour/ratio/aspect` 等字段别名 |
| 字段校验 | 非法主体/颜色/风格/位置/比例 → 丢弃并记 `FP_W_*` warning；count 夹到 1..12、confidence 夹到 0..1、text ≤2×40 字；`no_text` 与显式 text 冲突时以 text 为准；未知字段丢弃并 warning |
| 词表兜底 | 模型漏掉 subject/colors/style 时，用 planner 的**同一份词表**从原 prompt 回填并记 `FP_W_INTENT_FIELD_FALLBACK`（不改 planner，只是复用 `prompts.find_terms`） |
| 画图 | `plan_from_intent` 把 intent 重建成 planner 能解析的中文 prompt（`5个五角星，紫色，居中，竖版，文字："夏天"`），**通过 `plan_prompt(style=canonical_key, seed=seed)` 调用**；未复制任何 planner 几何/解析逻辑。多主体+count 等 planner 限制以 `FP_W_INTENT_COUNT_IGNORED` 显式提示 |
| 完全失败 | 模型不可用/加载失败/JSON 非法 → CLI 用原 prompt 走规则 planner（`source="rule_fallback"`, `fallback=true`），仍产出可用 plan |

接口（`fourier_prompt/llm/intent.py`）：

```python
extract_intent(prompt, *, model_dir=None, device="cuda", max_new_tokens=256,
               seed=0, generator=None, model=None, tokenizer=None) -> (intent|None, meta)
normalize_intent(payload, *, prompt=None) -> (intent, warnings)
intent_to_prompt(intent) -> str
plan_from_intent(intent, *, seed=0, planner_fn=None) -> (plan, warnings)
plan_from_prompt(prompt, *, seed=0, planner_fn=None) -> (plan, warnings)   # --rule-only / 回退
```

`generator`/`model`/`tokenizer`/`planner_fn` 是测试与离线钩子；模型加载与生成复用 `infer.load_model` / `infer._generate_raw`（greedy + `torch.manual_seed(seed)`，`seed` 确定性）。

CLI：

```powershell
$PY = "<python>"
cd "<workspace>\fourier-prompt"
& $PY -m fourier_prompt.llm.intent --prompt "画一只黑猫，线稿，居中" `
    --model-dir ..\models\Qwen2.5-1.5B-Instruct --device cuda `
    --out out\llm\intent\plan.json --render out\llm\intent\plan.png `
    --intent-out out\llm\intent\intent.json --report out\llm\intent\report.json [--json|--quiet|--rule-only]
```

退出码：`0` 产出 plan（LLM 意图或规则回退都算成功）、`1` compile/render/IO 失败、`2` 用法错误（已含空 prompt/坏 `--resolution`/坏 `--device`）、`3` 无 plan 可产出（意图失败**且**规则 planner 拒绝原 prompt，如 `FP_E_PROMPT_UNPARSEABLE`）。

## 2. 实测（5 条中英文 prompt，Qwen2.5-1.5B-Instruct base，cuda，seed=0，greedy）

产物：`out/llm/intent/i01..i05_{intent,plan}.json` + `i01..i05.png` + `summary.json` + `contact_sheet.png`；复跑脚本 `out/llm/intent/_runner.py`（单进程加载模型一次）。

| id | prompt | 意图 JSON（规范化后） | 合法 | plan | compile | render | 元素/文字 | 意图延迟 | 回退 |
|---|---|---|---|---|---|---|---|---|---|
| i01 | 画一只黑色的猫，线稿风格，居中 | cat ×1, black, mono_ink, center, square, conf .92 | ✅ | ✅ | ✅ | ✅ | 20 / 0 | 4.95 s | 否 |
| i02 | a golden star, minimal style, centered, square | star ×1, gold, minimal, center, square, conf .90 | ✅ | ✅ | ✅ | ✅ | 2 / 0 | 4.51 s | 否 |
| i03 | 画一朵粉色的花，水彩风格，文字："春天"，竖版 | flower ×1, pink, watercolor, portrait, text 春天, conf .90 | ✅ | ✅ | ✅ | ✅ | 11 / 1 | 4.32 s | 否 |
| i04 | 五颗紫色的星星，深色背景，赛博朋克，方形 | star ×5, purple, cyber, square, no_background, conf .85 | ✅ | ✅ | ✅ | ✅ | 10 / 0 | 4.68 s | 否 |
| i05 | draw a blue wave in pencil sketch style, landscape, text: "SEA" | wave ×1, blue, pencil, landscape, text SEA, conf .90 | ✅ | ✅ | ✅ | ✅ | 3 / 1 | 4.09 s | 否 |

!5 prompt contact sheet

- **意图合法 5/5**：全部 9 字段、0 字段回填、0 意图 warning；JSON 解析 5/5（输出均为纯 JSON，无围栏）。
- **plan compile + render 5/5**：唯一 plan warning 是 i04 的 `FP_W_NEGATION_APPLIED`（“深色背景”被规范化成 `no_background` 并生效；cyber 包底色本来就是 `#0c0c16`）。
- **回退 0/5**：本次真实模型无需回退；回退路径由单测（模型加载失败/坏 JSON）与 CLI 冒烟覆盖。
- 语义保持：i04 的“五颗”保持 5 颗（plan 10 元素=5×(填+描)）；i03/i05 文字进入 `texts`（outline）；三条比例（square/portrait/landscape）与 prompt 一致。i01 未说比例，模型填了 `square`（与 planner 默认一致，无副作用）。

### 延迟（RTX 4070 Ti SUPER 16 GB，单进程串行）

| 阶段 | 数值 |
|---|---|
| 模型加载（base，bf16，一次） | **12.1 s**（首次文件缓存冷：CLI 单次进程 15.5 s 含加载与生成） |
| 意图生成（256 tokens 预算，实际 186–207 字符） | 4.09–4.95 s，均值 **4.51 s** |
| intent → plan（规则 planner） | 1–13 ms（均值 3.8 ms） |
| compile（R256） | 27–358 ms（均值 166 ms，i03 文字轮廓最慢） |
| render（R256） | 77–184 ms（均值 121 ms） |
| 单条端到端（模型已加载，含写盘） | **约 4.3–5.3 s** |
| CLI 冷启动单条（含 12 s 加载） | 15.9 s（`out/llm/intent/cli_p01_report.json`） |

### 与纯 LLM 全 plan 对比（同一模型/同一 prompt i01/seed 0）

| 路径 | 输出 | 首轮合法 | 延迟 | 结果 |
|---|---|---|---|---|
| 纯 LLM 全 FDSL（base，1 次生成 1024 tokens，无 adapter） | 完整 FDSL JSON（约 2–8 KB） | **0/1**：`llm/json_invalid`（line 21 column 18，非 JSON） | 15.2 s | 校验失败 → **回退规则 planner** |
| 混合 WP-Q（意图 JSON） | 9 字段 / 186–207 字符 | **5/5** | 4.5 s + 3.8 ms plan | 5/5 compile+render |

与阶段 2 验收报告一致：base 零样本全 plan 3 次尝试 72 s 全部失败。**把输出从“自由 FDSL”收缩到“受词表约束的 9 字段意图”后，1.5B 模型即可稳定合法**——这是本路径可行的核心原因。

## 3. 测试

`tests/llm/test_intent.py` **36 例**（新增；`python -m unittest discover -s tests/llm` 全套 **97 例 OK（2 skipped），10.5 s**，无需 GPU/模型/网络）：

- 规范化 12 例：合法回填/字段别名/未知值丢弃+warning/数量与置信度夹取/颜色别名与 hex 上限/文字截断与上限/比例 3 种形态（含 `{"width","height"}` 吸附）/否定规范化与冲突/未知字段/`{"intent":...}` 信封/词表兜底；
- `extract_intent` 7 例：围栏+废话解析、坏 JSON 回退、生成异常、模型加载失败、seed 透传与确定性、空 prompt、非法 device；
- intent→planner 9 例：compile+schema 校验、同 seed 哈希一致、count/ratio 到达 planner、文字/否定、多主体 count 警告、空意图通用构图、style kwarg 透传、fake planner 契约、非 dict 拒绝、prompt 重建内容；
- CLI 8 例：`--rule-only --json --out`、`--render`、缺 prompt/坏 resolution/坏 device → 2、不可解析 prompt → 3、模型失败→规则回退 exit 0、stub 意图端到端（`--intent-out`）。

## 4. 失败与降级（设计 vs 实测）

| 故障 | 行为 | 验证 |
|---|---|---|
| 模型目录缺失 / torch 栈异常 | `meta.errors[0].stage="load"`，回退原 prompt 规则 planner，exit 0 | 单测 + CLI stub |
| 生成异常（OOM 等） | `stage="generate"`，同样回退 | 单测 |
| 输出非 JSON | `stage="extract"`，回退 | 单测（实测纯 LLM 列即此情形） |
| 字段非法/越界 | 不回退，逐字段默认值 + `FP_W_INTENT_*` warning，继续画 | 12 例规范化测试 |
| planner 拒绝原 prompt | `PlannerError` → exit 3，stderr 带 `FP_E_PROMPT_*` | CLI 测试 |
| compile/render/写盘失败 | exit 1 | CLI 测试（路径/参数） |

本次 5 条真实 prompt **无失败、无回退**；模型自信度自报 0.85–0.92，未做校准。

## 5. 限制（必须如实告知）

1. **意图粒度有限**：9 字段只覆盖 主体/数量/颜色/风格/文字/位置/比例/否定，**没有** layout（网格/一排/重复）、逐主体配色、相对大小与坐标、背景色、更多否定（如“不要人物”）。因此“深色背景”这类要求只能退化为 `no_background` 或由风格包决定底色；i01 的 `canvas=square` 是模型补的默认值而非 prompt 明示。
2. **画面上限 = 规则 planner 的上限**：15 类模板几何、≤3 主体、数量 ≤12、文字 ≤2×40 字；无透视/遮挡/光影/人体结构。例如 i02 的“星星”只有 2 个元素（填充+描边），i05 的“波浪”是 3 条正弦线——意图对了，画面仍简单。
3. **与参考图（黑白人物插画）的风格不可达**：参考图仅做过只读聚合统计（WP-P，第三方版权、部分成人向，不进训练/产品/文档），本路径**没有也不打算**学习该画师的人物风格；FDSL 模板生成不出那种人物复杂度。
4. **模型行为**：base 模型可能“照抄”few-shot；同义/生僻主体（如“龙”）会得到空 subject → planner 通用几何构图 + `FP_W_SUBJECT_UNKNOWN`。`confidence` 是模型自报，不可当作可靠性指标。
5. **评测规模小**：5 条 prompt、单 seed、greedy；延迟为单卡单进程串行（GPU 空闲时），CPU 路径未计时（`--device cpu` 可用但明显更慢）。
6. **训练未介入**：只用 base Instruct，未用任何 LoRA adapter；本路径不依赖训练即可用，但也不会随 LoRA 提升——LoRA 只影响纯 LLM 全 plan 路径。

## 6. 交付物

| 文件 | 内容 |
|---|---|
| `fourier_prompt/llm/intent.py` | few-shot 意图 prompt、`normalize_intent`、`extract_intent`、`intent_to_prompt`、`plan_from_intent`、`plan_from_prompt`、CLI（`--report` 直写 UTF-8 JSON） |
| `tests/llm/test_intent.py` | 36 例（fake generator / rule-only / 真实 planner / CLI） |
| `out/llm/intent/` | `i01..i05_{intent,plan}.json`、`i01..i05.png`、`contact_sheet.png`（5 格）、`summary.json`（逐条+纯 LLM 对比）、`cli_p01_*`（CLI 冷启动证据）、`_runner.py`（可复跑） |
| `reports/WP-Q.md` | 本报告 |

复跑命令（GPU 单卡，约 55 s）：

```powershell
cd "<workspace>\fourier-prompt"
& $PY out\llm\intent\_runner.py            # 重写 i01..i05 + summary.json（UTF-8）
& $PY -m unittest discover -s tests/llm    # 97 例 OK（2 skipped）
```
