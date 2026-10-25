# WP-O 报告 — 本地 LLM planner（Qwen2.5 LoRA 基础设施）

- 日期：2026-10-20 ｜ 状态：**代码/测试完成；未实际训练**（等 Lead 安装好 torch/transformers/peft 并下载模型）
- 契约：KICKOFF_v8；规则 planner 契约：KICKOFF_v7
- 写盘范围：`fourier_prompt/llm/`、`scripts/train_lora.py`、`scripts/eval_planner.py`、`tests/llm/`、`out/llm/`、本报告。未改 skill/核心/docs/analysis/参考图/模型目录。

## 1. 交付文件

| 文件 | 内容 |
|---|---|
| `fourier_prompt/llm/__init__.py` | 惰性导出（PEP 562），无 torch 也可 import；`python -m` 不重复导入子模块 |
| `fourier_prompt/llm/formatting.py` | `FDSL_SYSTEM_PROMPT`、`canonical_plan_json`、`extract_json`、`build_messages`、`tokenize_example` |
| `fourier_prompt/llm/dataset.py` | `build_dataset`、`synthesise_prompts`、`call_planner`、`prepare_plan`、`load_jsonl` + `python -m ...dataset` CLI |
| `fourier_prompt/llm/infer.py` | `generate_plan`、`load_model`、`validate_candidate` + `python -m ...infer` CLI（`--rule-only`） |
| `scripts/train_lora.py` | Qwen2.5-1.5B LoRA 训练脚本（import 受 try 保护，`--help/--dry-run` 无需 torch） |
| `scripts/eval_planner.py` | 20 prompt 规则 vs LLM 评测（compile/首轮/重试/延迟/fallback） |
| `tests/llm/` | 5 个测试文件 + `llm_fixtures.py`，共 **61 例** |
| `out/llm/data/` | smoke 数据集（200 对，train 180 / val 20） |
| `out/llm/eval_planner.json` | 规则 planner 在 `assets/eval/prompts_20.jsonl` 上的 baseline |
| `out/llm/plan.json` | `infer --rule-only` CLI smoke 产物 |

## 2. 接口（与 KICKOFF_v8 §2 一致）

```python
build_dataset(*, out_dir, n=2000, seed=0, planner_fn=None, validate_ratio=0.1,
              compile_check_enabled=False) -> dict
# 缺省惰性 import fourier_prompt.planner.plan_prompt；plan 经 repair_plan+validate_plan；
# 写 train.jsonl / val.jsonl / stats.json；记录 {prompt, plan, plan_json, meta}

extract_json(text) -> dict          # ```json / 前后废话 / BOM / 尾逗号 / {"plan":{...}} 信封
build_messages(prompt, *, feedback=None, system=None) -> list[dict]
tokenize_example(tokenizer, prompt, plan_json, *, max_len=4096) -> dict
# chat template，prompt 段 labels=-100，仅 assistant 段计 loss；prompt 过长先削 prompt
generate_plan(prompt, *, model_dir=None, adapter_dir=None, max_new_tokens=2048,
              device="cuda", seed=0, ...) -> dict
# 生成→extract_json→repair_plan→validate_plan→compile_plan；失败带错误反馈重试 ≤2；
# 最终回退规则 planner；返回 plan/source("llm"|"rule")/attempts/compile_ok/errors/latency_s
# 附加：ok/fallback/warnings/attempt_trace/model_dir/adapter_dir
```

CLI：

```powershell
$PY = "<python>"
$FP = "<workspace>\fourier-prompt"; cd $FP
& $PY -m fourier_prompt.llm.dataset --out out\llm\data --n 2000 --seed 0 [--compile-check]
& $PY -m fourier_prompt.llm.infer --prompt "画一只黑猫，线稿，居中" --out out\llm\plan.json [--render out\llm\plan.png] [--rule-only]
& $PY scripts\eval_planner.py --rule-only --prompts assets\eval\prompts_20.jsonl --out out\llm\eval_planner.json
```

## 3. 数据集 smoke（已跑，用 WP-N 已落盘的 planner）

- 命令：`python -m fourier_prompt.llm.dataset --out out\llm\data --n 200 --seed 0 --quiet`
- 结果：200 请求 → 200 生成，0 丢弃，180 train / 20 val，200 唯一 prompt，耗时 **0.9 s**
- `plan_bytes`：min 1255 / p50 5213 / mean 4863 / **max 14313** 字节；`repaired_plans=169/200`（规则 planner 输出经确定性 repair 后入集），`warnings_total=318`
- 同一 seed 输出逐字节可复现（测试覆盖）；split 用独立 `Random(seed)` 打乱
- 提示词模板：中英双语，覆盖主体/颜色/风格（7 类）/位置/比例/文字/否定式长 prompt，20% 带标题文字

## 4. 训练脚本与配置（未执行）

默认值（KICKOFF_v8 §3）：Qwen2.5-1.5B-Instruct（`$FDP_LLM_MODEL` 或 `models/Qwen2.5-1.5B-Instruct`）、LoRA **r=16 α=32 dropout=0.05**，target `q/k/v/o/gate/up/down_proj`，bf16 优先、fp16 回退（`--precision auto|bf16|fp16|no`），batch 1、grad-accum 8、lr 1e-4、cosine + warmup 20、`max_len 4096`、1 epoch、`--gradient-checkpointing` 可选、`--resume`、`--max-steps`（smoke 20）、`--dataset/--output/--save-steps/--logging-steps/--seed`；离线 `local_files_only=True`。

```powershell
& $PY scripts\train_lora.py --dry-run                     # 不装 torch 也能看解析后的配置/步数
& $PY scripts\train_lora.py --smoke --max-steps 20 --dataset out\llm\data\train.jsonl `
    --output out\llm\qwen2.5-1.5b-lora-v1
& $PY scripts\train_lora.py --num-epochs 1 --batch-size 1 --grad-accum 8
```

产物：adapter + tokenizer + `train_meta.json`（含 loss history、dtype/device、LoRA 参数）写入 `out/llm/qwen2.5-1.5b-lora-v1/`；**base 权重不进仓库/产品**。

**显存/时间预估（RTX 4070 16GB，未实测）**：1.5B bf16 ≈ 3.1GB，LoRA r16 可训练参数 ≈ 18–19M（权重+梯度+Adam 约 0.3GB）；序列实际约 1–2.5k tokens（plan p50 5.2KB）时激活约 2–4GB，总计 **6–8GB**；不加 checkpointing、跑满 4096 时约 9–12GB，16GB 有余量。速度：micro-step 约 0.4–1.2 s，grad-accum 8 → **4–10 s/优化步**；2k 数据 1 epoch = 250 优化步 ≈ **20–45 分钟**；smoke 20 步约 **2–4 分钟**。以上为估算，以 Lead 实跑日志为准。

**当前环境阻塞**：`torch` 目录存在但 import 报 `OSError: caffe2_nvrtc.dll`（DLL 缺失，安装未完成）；`transformers/peft` 未装；`models/Qwen2.5-1.5B-Instruct` 未下载。因此 `train_lora.py` 在真实模式下干净退出码 2，`generate_plan` 自动回退规则 planner。

## 5. 评测 baseline（规则 planner，已跑）

`assets/eval/prompts_20.jsonl`（WP-N 已生成，20 条）：**compile 20/20（100%）**，规则侧平均 **180.6 ms/prompt**；`generate_plan(--rule-only)` 20/20 compile、fallback 100%、平均 0.136 s。LLM 列（首次成功率/重试/延迟）要等模型+adapter 就绪后运行：

```powershell
& $PY scripts\eval_planner.py --model-dir models\Qwen2.5-1.5B-Instruct `
    --adapter-dir out\llm\qwen2.5-1.5b-lora-v1 --out out\llm\eval_planner.json
```

## 6. 测试（无 GPU / 无模型 / 无网络）

```powershell
cd "<workspace>\fourier-prompt"
& $PY -m unittest discover -s tests/llm -v
# Ran 61 tests ... OK (skipped=2)
```

覆盖：`extract_json` 10 形态（纯 JSON/```json/前后废话/字符串内花括号/尾逗号/信封/坏 JSON/空/非字符串/顶层数组）；canonical JSON 紧凑+排序+确定性；messages/feedback；fake tokenizer 的 assistant-only mask、无 chat template 回退、截断；`build_dataset` 小 n/确定性/schema/规范 JSON/split/去重/坏 plan 丢弃/planner 异常/比例边界/CLI 默认值；`generate_plan` 无模型回退、rule-only、坏 JSON 重试、坏 plan 重试、3 次失败回退、重试上限、生成异常、compile 开关、空 prompt、planner 缺失软失败；`train_lora --help`/非法参数退出 2/`--dry-run`/`--smoke`；`eval_planner` 20 条内置 prompt、JSONL 读取、rule-only 聚合、planner 不可用、写报告、非法 limit。跳过 2 例为环境相关分支（WP-N planner 已就绪 / torch 未就绪）。

## 7. 限制与风险

1. **未训练/未做 LLM 推理**：本机 torch 栈损坏、模型未下载；所有 LLM 指标为空，需 Lead 就绪后跑 smoke（20 步）再跑 1 epoch。
2. **长 plan**：max 14.3KB ≈ 可能 >4096 tokens（p50 5.2KB 约 1.5–2k tokens）。`tokenize_example` 会先削 prompt、再截 assistant（`assistant_truncated` 标记），但训练脚本当前未丢弃此类样本；建议训练前按 `plan_bytes > 10000` 过滤（约 200 条中 1–2 条）或提高 `--max-len`。
3. 规则 planner 输出 169/200 需要确定性 repair（层引用/宽度等），训练目标=repair 后的规范 plan，与推理侧校验一致；若 WP-N 后续改输出，需重建数据集。
4. `FDSL_SYSTEM_PROMPT` 中笔刷/几何库为当前 schema 冻结列表；若 catalog 变更需同步（改 system prompt 会使已有 adapter 失配）。
5. 训练脚本的 GPU 路径（bf16/fp16、GradScaler、LoRA resume、保存）未经真机验证；只在 CPU 上验证了 import/参数/dry-run。
6. `generate_plan` 回退规则 planner 时 `source="rule"` 且 `fallback=true`，评测应把这类样本算作 fallback 而非 LLM 成功（脚本已按 `source` 统计）。
7. 合成数据只用规则 planner + 模板，无版权参考图；多样性受模板/槽位限制，扩到 5k–20k 时建议增加模板和长尾主体。

## 8. Lead 后续步骤

1. 修好 torch/transformers/peft（当前 DLL 报错）并下载模型到 `models/Qwen2.5-1.5B-Instruct`；
2. `python scripts/train_lora.py --dry-run` 核对配置 → `--smoke --max-steps 20` 验证显存/保存 → `--num-epochs 1` 全量；
3. `python scripts/eval_planner.py --model-dir ... --adapter-dir ...`，把 `out/llm/eval_planner.json` 的 LLM 列与本文 baseline 对比；
4. 若 2k 数据效果不足：`build_dataset(n=5000, seed=1)` 重建（确定性），或调 r=32 / 2 epochs。
