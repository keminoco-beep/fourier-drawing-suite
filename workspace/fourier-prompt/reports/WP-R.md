# WP-R 报告 — OmniSVG1.1-4B 本机 text→SVG 可行性

- 日期：2026-10-21 ｜ 状态：**跑通（有合法 SVG 产出），但“复杂插画”质量不达标，需 prompt/候选筛选或换路线**
- 工作包：analysis/15_复杂图像生成路线.md §5 WP-R
- 写盘范围：`poc/omnisvg/`、`models/`、本报告；**未修改** `refs/OmniSVG/`、`skill/`、核心代码、`docs/`、`analysis/`
- 上游/下游：WP-S（SVG→FDSL）已交付，可直接拿本报告的 SVG 做导入测试

## 0. 结论（TL;DR）

1. **本机可以跑通 OmniSVG1.1-4B text→SVG**：RTX 4070 Ti Super 16GB 上 bf16 单实例，峰值显存 **9.5GB（nvidia-smi）/ 7.9GB（torch 统计）**，**未 OOM**；模型装载 **12.8–18.7s**，生成 **10–19 token/s**。
2. **合法 SVG 已产出（3 个）**：官方示例 prompt “A red heart shape…” → 1 path / 261B / 4 段三次 Bézier，Chrome 渲染为正确心形（`omnisvg_heart.png`）；官方插画示例 “cartoon character with dark blue hair…” → **11 paths / 8.9KB / 1024 token / 56.9s**，渲染是“青圆底 + 深蓝西装 + 粉脸 + 黑发”的可辨认卡通（`omnisvg_cartoon.png`）。
3. **质量不稳定**：`anime-style portrait of a girl...` 生成 1024 token 但塌缩成一条重复水平线（2 paths / 9.5KB，渲染几乎空白）；`cat on windowsill` 仅 34 token 就 EOS，输出三角形+空 path。**当前单候选/默认采样下不能稳定产出“真正复杂图像”**。
4. 与验收线（路径数 ≥50 或官方示例级）相比：**未达到**（最好 11 paths）。但“至少 1 个合法 SVG”的 M3.1 门槛已达到；复杂化需要更多候选 + 筛选 / 官方 caption 风格 prompt / 或换生成路线。
5. 环境未污染：**没有**在现有 3.13 env 里装 transformers 4.51.3；而是用独立 `PYTHONPATH` 目录 `<path>` 覆盖依赖，torch 复用现有 2.9.1+cu128。

## 1. 环境（与官方要求的差异）

| 项 | 官方 | 本机实测 | 说明 |
|---|---|---|---|
| Python | 3.10 | **3.13.14** | 现有 venv 的 base interpreter（`<path>`） |
| torch | 2.3.0+cu121 | **2.9.1+cu128** | 复用现有环境，未重装；`torch.cuda.is_available()=True` |
| transformers | 4.51.3 | **4.51.3** | 装在隔离 `<path>`，`PYTHONPATH` 优先于现有 5.20 |
| accelerate | — | 0.34.2 | 隔离目录 |
| CUDA GPU | 17G 标称 | RTX 4070 Ti SUPER 16GB，驱动 610.88 | 单实例可跑 |
| CairoSVG | 必需 | **未安装/未使用** | 自写 runner 只保留 SVG 字符串，跳过 PNG 预览与“空图”拒绝 |
| 模型装载 | `SketchDecoder` + Qwen base from_pretrained | **meta-device 构造 + 直接 load OmniSVG state_dict** | 跳过 7.5GB Qwen base 下载与 4B 随机初始化（后者在本机曾卡 20min+） |

关键实现：`poc/omnisvg/run_text2svg.py`
- 从 `config.json` 在 `torch.device("meta")` 构造 `Qwen2_5_VLForConditionalGeneration`，`to_empty("cpu")` 后 `load_state_dict(..., assign=True)`；
- **必须重算 RoPE `inv_freq`**（37 个 rotary 模块）：`to_empty` 不会初始化这些 non-persistent buffer，缺失会导致 NaN logits / `CUDA error: device-side assert triggered`；这是本次调试的主要坑；
- 复用 `refs/OmniSVG/tokenizer.py`（stub 掉 cairosvg/moviepy/IPython 后 import）；
- 加载结果：**825 keys，missing=0，unexpected=0，state_dict 8.5GB**，说明 4B checkpoint 是完整模型（含 vision tower），不需要 Qwen base 权重。

## 2. 权重下载（证据）

- 目标：`OmniSVG/OmniSVG1.1_4B` 的 `pytorch_model.bin`，**7,694,002,862 B（7.69GB）**；Qwen 侧只下 config/tokenizer/preprocessor 共 ~12MB（不需要 7.5GB base 权重）。
- 网络问题：
  - Python OpenSSL 证书链校验失败（`CERTIFICATE_VERIFY_FAILED`，系统代理根证书只在 Windows store）；用 **truststore** 注入解决；`hf_xet` 在本机连不上（xet log 0B、cache 0B）。
  - `hf_hub_download` 单流/多文件会卡在 0B；最终用自写 **24 线程 HTTP Range 分块下载器**（8MB/块，断点续传）：`poc/omnisvg/pdl.py`。
- 结果：**918 块 / 702s / 平均 11.27MB/s**（含重试）；合并后 7.69GB，helper 已复制到 `models/OmniSVG1.1_4B/pytorch_model.bin`。
- 工作区中的 `models/Qwen2.5-VL-3B-Instruct/` 只有 config/tokenizer 等小文件（另有 helper 下载的 LICENSE），**没有** safetensors 权重；本 runner 不需要它们。

## 3. 推理结果

统一参数：bf16、`sdpa`、单候选、seed 42；装载峰值 7.81GB(torch)/9.5GB(SMI)。GPU 空载约 1.5–1.9GB。
> 注：cartoon 运行期间另一进程（Lead 的 `poc/omnisvg2`，`--gpu-gib 5.5`）也占用 GPU，SMI 峰值 15,920MB 是两进程叠加，**不是本 runner 单实例需求**；单独跑时峰值为 9.5GB。

| prompt | max_new | 实际 token | 生成耗时 | tok/s | paths | bytes | XML | 视觉 |
|---|---|---|---|---|---|---|---|---|
| A red heart shape with smooth curved edges, centered.（官方示例，icon 参数） | 512 | 23 (EOS) | 2.17s | 10.6 | 1 | 261 | ✅ | **正确心形** |
| a black and white anime-style portrait of a girl with long hair, line art, white background（illustration 参数 0.6/0.9/60/1.03） | 1024 | 1024 (未 EOS) | 54.67s | 18.73 | 2 | 9,535 | ✅ | **塌缩**：一条重复水平线，几乎空白 |
| a detailed ... cat sitting on a windowsill with potted plants（rep 1.15, temp 0.5） | 1536 | 34 (EOS) | 2.77s | 12.27 | 2 | 282 | ✅ | 三角形 + 空 path，语义错误 |
| A cartoon character with dark blue hair and a mustache wears a blue suit against a light blue circular background（官方 illustration 示例） | 1024 | 1024 | 56.91s | 17.99 | 11 | 8,884 | ✅ | **可辨认卡通**：青圆底+西装+脸+发，但线条粗糙、非“复杂插画” |

复杂 prompt 的 1536-token 尝试曾两次在 ~60s 被 DSH 后台 job runner 杀掉（不是 OOM/模型崩溃；`exit=-1`，无 traceback）。改用 **WMI `Win32_Process.Create` 脱离 job object** 启动后才稳定跑完 1024 token。这是本机评测基础设施限制，不是模型限制。

## 4. 证据文件

- 心形（成功、可视觉验证）：
  - `poc/omnisvg/out/omnisvg_heart_c1.svg`（261B）
  - `poc/omnisvg/out/omnisvg_heart.png`（Chrome headless 渲染，800×800）
  - `poc/omnisvg/out/omnisvg_heart_meta.json`
- 卡通（当前最复杂、可辨认，推荐给 WP-S 做导入样例）：
  - `poc/omnisvg/out/omnisvg_cartoon_c1.svg`（8,894B，11 paths，1023 条命令）
  - `poc/omnisvg/out/omnisvg_cartoon.png`（Chrome headless 渲染）
  - `poc/omnisvg/out/omnisvg_cartoon_meta.json`
- 女孩（合法 XML 但视觉失败，作反例）：
  - `poc/omnisvg/out/omnisvg_girl1024_c1.svg`（9,535B，1023 条命令）
  - `poc/omnisvg/out/omnisvg_girl1024.png`（空白+一条短线）
  - `poc/omnisvg/out/omnisvg_girl1024_meta.json`
- 猫（过早 EOS 反例）：`poc/omnisvg/out/omnisvg_cat_c1.svg`（283B，2 paths）
- 运行器与下载器：`poc/omnisvg/run_text2svg.py`、`poc/omnisvg/pdl.py`
- 完整元数据含：`generate_s / gen_tok_per_s / load_total_s / vram_peak_smi_mb / torch_peak_alloc_gb / candidates[].paths|bytes|xml_valid`

## 5. 失败模式与风险

1. **重复塌缩**：1024 token 全部用于同一条水平线；repetition_penalty 1.03 不足。建议 1.1–1.2、降低 temperature、或 8 候选筛选。
2. **过早 EOS**：cat 仅 34 token 就结束；与 prompt 分布/采样有关。
3. **路径数少**：模型按颜色合并 path，一个 path 可含上千命令；WP-S 的“路径数≥50”门槛对 OmniSVG 不适用，建议改用 **命令数/字节数/渲染非空像素** 作为复杂度指标。
4. **长序列稳定性**：>1024 token 未验证完；1536 需脱离 DSH job runner。
5. **基础设施**：HF 证书/xet/单流卡死；工作区 `models/` 与 `poc/omnisvg/` 在 3:03–3:20 被其他进程删除两次（非本 WP 行为），最终改用 `<path>` 落盘 + 复制回工作区。

## 6. 许可证（重要）

- **OmniSVG 代码/权重**：Apache-2.0（官方 README / HF）。
- **Qwen2.5-VL-3B-Instruct**：`models/Qwen2.5-VL-3B-Instruct/LICENSE` 明确为 **Qwen RESEARCH LICENSE AGREEMENT**（2024-09-19），**不是 Apache-2.0**。OmniSVG 4B checkpoint 是 Qwen2.5-VL-3B 的微调衍生物；即使只分发 OmniSVG 权重，底层 Qwen 条款可能仍适用，商用前必须法务核查。
- **MMSVG 数据集**：CC-BY-NC-SA 4.0（非商用）；本次未下载数据集，只用了官方示例 prompt。
- 本次产物仅用于本地技术验证，未用于训练/分发。

## 7. 对 Lead / WP-S 的建议

1. **可继续用本 SVG 做 SVG→FDSL 链路验证**：心形（简单、正确）与女孩（长 path、退化）各一个，覆盖“短路径/长路径/空白”三种情况。
2. **不要按 path 数评估 OmniSVG**；用命令数、XML 合法性、渲染非空像素、人工 rubric。
3. **复杂插画要加候选筛选**：4–8 候选 + 非空/命令数/颜色数过滤；本机单候选 55s/1024 token，4 候选约 3–4min，可接受。
4. 若质量仍不达标：备选路线 B（代码 LLM→SVG）、D（检索/拼装 CC0 SVG）、或图像→矢量化（sibling WP 的 `poc/complex_online` vtracer 路线）。
5. **不要**在本机尝试 8B（官方 26GB）；4B 已占 9.5GB，余量不足以并行第二实例。

## 8. 复现命令（精简）

```powershell
# 1) 依赖（隔离目录，不污染现有 env）
$env:PYTHONPATH='<path>;<path>'
# transformers==4.51.3 accelerate==0.34.2 tokenizers==0.21.2 等已装于 <path>

# 2) 下载（已完成的权重在 models/OmniSVG1.1_4B/pytorch_model.bin）
python <path>

# 3) 推理（脱离 DSH job object，避免 60s 被杀）
cmd /c <path>
```

运行器参数：`--prompt --max-new-tokens --temperature --top-p --top-k --repetition-penalty --tag --out-dir`。

---

*结论：OmniSVG1.1-4B 在本机“能跑、能出合法 SVG、显存够”，但“复杂插画”这一目标在当前单候选/默认采样下没有达成；下一步应优先做候选筛选与 prompt/参数调优，而不是继续加机器。*
