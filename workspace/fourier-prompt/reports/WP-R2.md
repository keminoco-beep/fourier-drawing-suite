# WP-R2：OmniSVG1.1-4B 本地 text→SVG 可行性（复杂图像路线）

- 日期：2026-10-21 ｜ 状态：**跑通（端到端），但 4B 对“复杂自由描述”质量不足**
- 任务：共享 task-34 ｜ 前序：15_复杂图像生成路线
- 写盘边界：`poc/omnisvg2/`、`models/`、本报告；**未改**产品核心/skill/docs/analysis
- 约束：不删 `models/`、不用付费 API、不碰 ttus 图、单实例加载（本机 16GB 显存 / 32GB 内存）

## 0. 结论（TL;DR）

1. **能跑通**：OmniSVG1.1-4B 本地加载 **4.6–5.5 s**（meta 初始化 + 状态字典直载），峰值显存 **10.2–10.5 GB**（SMI）/ 8.5 GB（torch alloc），生成 **14.7–18.1 tok/s**；3 条复杂 prompt 均产出合法 SVG。
2. **端到端可用**：3/3 SVG → `fourier_prompt.svg_import` → FDSL v2.1 → `compile_plan` → R=384 PNG **全部成功**，0 丢弃、0 warning、渲染 0.02–0.09 s。
3. **质量是瓶颈，不是管线**：对“黑白动漫少女 / 机械猫头鹰 / 月夜小屋”这类自由描述，4B 输出**抽象碎片**（1–11 条路径、墨迹 0.6%–26%、从不到 EOS）；而官方示例风格的**具体图标/角色描述**能画对（红心 22 token 即 EOS、1 路径；卡通角色 22 路径、671 点、墨迹 78.7%，可辨认人像）。
4. **关键工程发现**：
   - 不需要 Qwen2.5-VL-3B 的 6GB 基础权重——`OmniSVG1.1_4B/pytorch_model.bin` 就是完整微调模型（config 即 Qwen2.5-VL-3B + vocab 197000），官方脚本先载 Qwen 再整包覆盖是浪费；
   - **transformers 5.20 不兼容**该 checkpoint（config 改为 `text_config` 嵌套、键名 `model.visual.*`），必须 **4.51.3**；
   - 纯文本任务**不需要 AutoProcessor**（它要 torchvision，本机未装）；用 `AutoTokenizer` 即可，也不需要 `qwen-vl-utils`/CairoSVG；
   - 直接用 `Qwen2_5_VLForConditionalGeneration(config)` 在 CPU 上随机初始化 **要 255.7 s**；改用 `from_pretrained(None, config=..., state_dict=..., device_map="auto", max_memory=...)` **只要 3.1–3.6 s**，且显存可控。

## 1. 环境与版本

| 项 | 值 |
|---|---|
| OS / GPU | Windows｜RTX 4070 Ti SUPER 16GB（空闲约 14.3GB）｜RAM 32GB |
| Python | 3.13.14（`<path>`） |
| torch | 2.9.1+cu128（CUDA 12.8，bf16 可用） |
| transformers | **4.51.3**（隔离目录 `poc/omnisvg2/pylibs`，PYTHONPATH 注入，不污染主环境） |
| accelerate / safetensors / tokenizers | 0.34.2 / 0.8.0 / 0.21.2 |
| numpy / Pillow | 2.5.4 / 13.0.0 |
| 未安装/不需要 | torchvision、qwen-vl-utils、CairoSVG（SVG 生成不需要；`tokenizer.py` 的 cairosvg/moviepy/IPython 导入用 stub 屏蔽） |
| 模型 | `models/Qwen2.5-VL-3B-Instruct`（仅 tokenizer/config，11 MB）<br>`models/OmniSVG1.1_4B/pytorch_model.bin` 7,694,002,862 B + config.json（vocab 197000, bos/eos 196998/196999） |

> 权重来源：Lead 下载 Qwen 时卡在 1.85 GB/0 增长（xet 并发退化到 0.26 MB/s）；发现 OmniSVG 权重即全量模型后停止该下载，复用并行 WP-R 代理（`<path>`）的 24 线程 range 下载（约 11 MB/s），完成后复制并校验 zip（`poc/omnisvg2/wait_and_copy.py`，18.3 s，COPY_OK）。

## 2. 复现命令

```powershell
# 1) 生成（3 条复杂 prompt，一次加载）
$env:PYTHONPATH='<workspace>\poc\omnisvg2\pylibs'
python -u -B poc\omnisvg2\run_text2svg.py --max-new-tokens 768 --gpu-gib 13 --cpu-gib 20 `
  --prompt "a black and white anime girl portrait, half body, long flowing hair, large expressive eyes, detailed line art" `
  --prompt "a mechanical owl made of gears and metal feathers, steampunk, detailed line art" `
  --prompt "a small cottage in a moonlit forest, stars and pine trees, detailed illustration" `
  --out-dir poc\omnisvg2\out --tag omnisvg

# 2) 对照（官方示例风格）：红心 + 卡通角色
python -u -B poc\omnisvg2\run_text2svg.py --max-new-tokens 768 --gpu-gib 13 `
  --prompt "A red heart shape with smooth curved edges, centered." `
  --prompt "A cartoon character with dark blue hair and a mustache wears a blue suit against a light blue circular background" `
  --out-dir poc\omnisvg2\out2 --tag control

# 3) 长生成（1536 token，具体描述）
python -u -B poc\omnisvg2\run_text2svg.py --max-new-tokens 1536 --gpu-gib 13 `
  --prompt "Black and white line art portrait of an anime girl with long flowing hair, large eyes and a school uniform, centered on a white background" `
  --out-dir poc\omnisvg2\out2 --tag omnisvg_long

# 4) SVG → FDSL → PNG（R=384）
python -u -B poc\omnisvg2\convert_fdsl.py --svg-glob "poc/omnisvg2/out/omnisvg_*.svg" --out-dir poc\omnisvg2\out\fdsl
```

日志：`poc/omnisvg2/run_final.log`、`run_long.log`、`run_control.log`、`diag_init2.log`、`download_copy.log`。

## 3. 加载 / 生成性能

| 运行 | 加载总耗时 | from_pretrained | 峰值 SMI | 生成 768 token | tok/s | EOS |
|---|---|---|---|---|---|---|
| 3 条复杂 prompt | 4.58 s | 3.07 s | 10,245 MB | 42.4 / 52.3 / 48.6 s | 18.1 / 14.7 / 15.8 | 否 |
| 1536 token 单条 | 4.74 s | 3.19 s | 10,508 MB | 1536 token = 88.5 s | 17.36 | 否 |
| 对照组（红心+角色） | 5.49 s | 3.59 s | 10,376 MB | 红心 22 token=2.18 s；角色 768=49.7 s | 10.1 / 15.5 | 红心**是** |

- 首 token（prefill）0.07–0.54 s；稳态 **0.055–0.068 s/token**（≈15–18 tok/s）。
- 显存：torch `max_memory_allocated` 8.5 GB，nvidia-smi 峰值 10.2–10.5 GB（含桌面 1.6 GB）；`device_map="auto" + max_memory={0:"13GiB","cpu":"20GiB"}` 全模型上卡，**无 OOM、无降级**。
- 注：18 tok/s 明显高于 README 的 4B 指标（512 token/8.68 s ≈ 59 tok/s），本机为共享 CPU/GPU 环境、且中途有并行代理占卡，仅供参考。

## 4. 生成结果与质量

### 4.1 复杂 prompt（目标场景）

| # | prompt | 路径 | 字节 | XML | 墨迹占比（FDSL 渲染 R=384） | ink bbox |
|---|---|---|---|---|---|---|
| 1 | 黑白动漫少女半身像 | 4 | 6,997 | ✓ | 25.96% | 60.6% |
| 2 | 机械猫头鹰 | 1 | 4,869 | ✓ | 0.99% | 6.0% |
| 3 | 月夜小屋 | 11 | 6,590 | ✓ | 1.45% | 16.4% |
| 4 | 少女（具体描述，1536 token） | 3 | 11,956 | ✓ | 0.58% | 1.7% |

**判定：不合格**。4 条全部**未生成 EOS**（768/1536 用满），路径数 1–11，画面是抽象色块/碎片；第 4 条 1536 token 反而收缩到 47×51 px 的小区域（坐标坍缩）。视觉证据：`poc/omnisvg2/out/fdsl/*.fdsl.png`、`out2/fdsl/omnisvg_long_*.fdsl.png`。

### 4.2 对照：官方示例风格（证明管线正确）

| prompt | 路径 | 点 | 颜色 | 墨迹 | bbox | 结果 |
|---|---|---|---|---|---|---|
| A red heart shape…（22 token 即 EOS） | 1 | 65 | 1 | 35.2% | 51.1% | **正确红心**（与并行代理结果一致） |
| A cartoon character with dark blue hair and a mustache… | 22 | 671 | 9 | 78.7% | 99.7% | **可辨认卡通人像**（黄底圆形、深蓝衣服；头发/胡子不准确） |

结论：**模型与解码链路正确**，差距在 4B 对“复杂/抽象描述”的能力；具体、构图化的 caption（官方 examples.txt 风格）能显著提升。

## 5. SVG → FDSL → PNG（`fourier_prompt.svg_import`）

| 来源 SVG | elements | points | colors | convert s | compile s | render s | warnings | 非白占比 | 备注 |
|---|---|---|---|---|---|---|---|---|---|
| 少女（4 路径） | 4 | 365 | 4 | 0.004 | 0.544 | 0.062 | 0 | 25.96% | ✓ |
| 猫头鹰（1 路径） | 21 | 359 | 1 | 0.004 | 0.079 | 0.017 | 0 | 0.99% | ✓ |
| 月夜小屋（11 路径） | 11 | 268 | 3 | 0.004 | 0.054 | 0.015 | 0 | 1.45% | ✓ |
| 少女长生成（3 路径） | 3 | 427 | 3 | 0.006 | 0.541 | 0.062 | 0 | 0.58% | ✓ |
| 红心（对照） | 1 | 65 | 1 | 0.001 | 0.591 | 0.053 | 0 | 35.18% | ✓ |
| 卡通角色（对照） | 22 | 671 | 9 | 0.008 | 0.106 | 0.093 | 0 | 78.66% | ✓ |

- 6/6 转换+编译+渲染成功，**0 warning、0 路径丢弃**；`target_size=384`、`resolution=384`、`supersample=2`、`fill_dilate_px=0.5`。
- 参考：454 元素 / 4,612 点的真实复杂 SVG 预检（`poc/omnisvg2/preflight/`）convert 0.05 s、compile 2.65 s、render 8.0 s，说明导入器可承载更复杂输入，当前慢在 OmniSVG 本身。
- 产物：`out/fdsl/`、`out2/fdsl/` 下 `*.fdsl.json` / `*.fdsl.png` / `*.fdraw` + `fdsl_report.json`。

## 6. 失败证据与坑（可复现）

1. **transformers 5.20.2 不可用**：`Qwen2_5_VLConfig` 无 `vocab_size`（嵌套 `text_config`），模型参数名变 `model.visual.*`，与旧 checkpoint（`visual.*`、`transformer.` 前缀 825 键）不匹配 → 必须 4.51.3（`poc/omnisvg2/pylibs`）。
2. **AutoProcessor 需要 torchvision**：本机未装，报 `Qwen2VLVideoProcessor requires the Torchvision library`；纯文本用 `AutoTokenizer` 规避。
3. **CPU 随机初始化极慢**：`Qwen2_5_VLForConditionalGeneration(config)` + 3.85B 参数 bf16 初始化实测 **255.7 s**；`from_pretrained(None, config, state_dict, device_map, max_memory)` 仅 **3.1 s**。
4. **内存竞争会静默杀进程**：并行 WP-R 代理与本次同时加载 8 GB 模型+7.7 GB 权重（32 GB 机器），进程被 Windows 直接终止（无 traceback，exit=-1）；必须**串行**（`launch_when_idle.py` 监测 RSS/GPU 后启动）。
5. **后台作业被杀**：一次 `run_in_background` 的生成作业被 job runner 以 `killed before exit` 终止（非模型错误）；最终改用前台运行 + 日志重定向完成。
6. **WSL/系统 LiveKernelEvent 141/193**：3:43 出现 GPU 相关内核事件（并发占卡期间），未影响最终单实例运行，记录备查。

## 7. 限制

- 仅验证 **4B**；8B（17.2 GB 权重，官方 26 GB 显存）本机 16 GB 需 4bit/offload，未测。
- 复杂 prompt 下模型**不收敛/不 EOS**，当前 max_new_tokens=1536 用满；未做 best-of-N、prompt 改写、CFG/惩罚调参。
- 质量评估以目检 + 墨迹/bbox 为主，未做 PSNR/SSIM（无参考图）。
- 生成 1536 token 约 88 s，交互场景不可接受，只能离线/批处理。
- 许可证：OmniSVG 代码/权重 Apache-2.0；Qwen2.5-VL 许可需单独核查（分发时保留 NOTICE）。

## 8. 后续建议

1. **prompt 工程优先**：按 `refs/OmniSVG/examples.txt` 的“具体元素+颜色+构图+居中/白底”风格改写复杂需求；实测同一模型可从“碎片”变为“可辨认角色”。
2. **多候选 + 质量门**：`num_return_sequences=2–4` + `svg_quality.evaluate_svg`（ink/bbox/structure）选优；4B 单候选成功率低。
3. **8B / 4bit**：若质量仍不足，评估 OmniSVG1.1_8B + bnb 4bit（需装 bitsandbytes，Windows wheel 可行）或 CPU offload，预期质量提升但更慢。
4. **token 预算与停止**：允许 2048–4096 token（README 4B 4096≈82 s），并加“连续 N token 无新 M 命令/路径数饱和”早停。
5. **接入形态**：把 `run_text2svg.py` 作为可选离线 backend，输出 SVG 后走既有 `svg_import → FDSL → fdraw`；不要让 4B 直接产出 FDSL。

## 9. 交付物索引

| 路径 | 内容 |
|---|---|
| `poc/omnisvg2/run_text2svg.py` | 本地 text→SVG runner（state_dict 直载、max_memory、每 token 计时、VRAM 采样、多 prompt） |
| `poc/omnisvg2/convert_fdsl.py` | SVG→FDSL→compile→PNG→指标 |
| `poc/omnisvg2/pylibs/` | transformers 4.51.3 等隔离依赖（196.6 MB，7,791 文件） |
| `poc/omnisvg2/out/` | 3 条复杂 prompt 的 SVG + meta + `fdsl/` |
| `poc/omnisvg2/out2/` | 1536-token 单条 + 对照组（红心/角色）+ `fdsl/` |
| `poc/omnisvg2/preflight/` | 真实复杂 SVG 的导入/渲染预检（454 元素，8 s） |
| `models/OmniSVG1.1_4B/` | 7.69 GB 权重 + config（zip 校验通过） |
| `poc/omnisvg2/run_*.log`、`download_copy.log` | 完整命令输出与失败证据 |
