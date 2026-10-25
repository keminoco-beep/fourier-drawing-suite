# WP-U 在线复杂文本→SVG 能力调研与实测（共享任务 task-31）

- 日期：2026-10-21 03:04–03:12（Asia/Shanghai）｜状态：**完成（2/3 prompt 拿到在线模型 SVG，第 3 条因匿名 GPU 配额失败并留证）**
- 写盘边界：`poc/online_gen/`、`fourier-prompt/reports/WP-U.md`（本文件）
- 未改：`fourier_prompt/` 核心、`skill/`、`docs/`、`tests/`；未训练、未下载权重、未使用本地 LLM/本地模型；**未使用任何私有 key/token**；未触碰 `[X(Twitter)] ttus` 版权参考图
- 环境：Windows / Python 3.13.14 / `requests`；未安装 `gradio_client`，直接实现 Gradio HTTP + SSE 调用

---

## 0. 结论（TL;DR）

1. **能找到“无需 key、许可明确、返回 SVG”的公开服务**：OmniSVG 官方 Space（Apache-2.0）与 SVGThinker-7B Space（MIT）。匿名 HTTP 200，能返回合法 SVG，**不需要注册/不需要 token**。
2. **但匿名 GPU 配额只够每个 Space 生成 1 次**：首次分别 51.4 s / 59.0 s 成功；同一 IP 随后再调在 ~1.6 s 返回 `event: error` + `data: null`。3 条 prompt 最终只拿到 2 条模型 SVG。
3. **质量远未达到“复杂黑白人物线稿”目标**：OmniSVG 4B 只输出 **3 条 path 的黑色剪影**（PNG ink 25.9%，不是线稿）；SVGThinker 输出 29 个元素，但画面只占画布 **0.6%**（48×84 px / 363×512），构图不可用。
4. **确定性转换链路已打通**：2 条模型 SVG + 2 条 agent 手写 SVG，全部经 `svg_import` → FDSL → PNG + `.fdraw` 成功；模型 SVG 0 warning，agent 场景 1 个 degenerate warning。
5. **现实结论**：在线无 key 服务目前适合“打通链路/做演示”，不足以稳定产出复杂线稿。近期可用 **在线 agent 直接编写 SVG**（本报告第 4 节）兜底；模型路线需账号配额（8B）、自托管或付费 API（需用户决策）。

---

## 1. 调研表

| 服务 / 模型 | 许可 | 实测可用性（2026-10-21） | 调用方式 | 返回 / 限制 | 证据 |
|---|---|---|---|---|---|
| [OmniSVG/OmniSVG-3B](https://huggingface.co/spaces/OmniSVG/OmniSVG-3B)（官方 Space） | **Apache-2.0**（Space 卡 + [OmniSVG1.1_4B](https://huggingface.co/OmniSVG/OmniSVG1.1_4B) 模型卡） | **RUNNING**，zero-a10g，公开；匿名调用成功 1 次 | `POST /gradio_api/call/gradio_text_to_svg`，参数：prompt、`4B/8B`、候选数 1–4、temp/top-p/top-k/rep、max_length 256–2048 | 返回 SVG 代码（HTML 画廊 + code + status）；单次 20–60 s；**实测匿名仅成功 1 次（同一 IP/Space 再调即 error）** | HTTP 200；首调 51.42 s；再调 `error:null` 1.5–1.7 s；见 `p1_girl_horns.svg.raw.json`、`p3_mech_owl_omnisvg.svg.raw.json` |
| [Caffin/SVGThinker-7B](https://huggingface.co/spaces/Caffin/SVGThinker-7B) | **MIT**（Space 卡 + [模型卡](https://huggingface.co/Caffin/SVGThinker-7B)） | **RUNNING**，zero-a10g，公开；匿名调用成功 1 次 | `POST /gradio_api/call/gradio_text_to_svg`，仅 prompt；返回 SVG 代码 + WebP 预览 | 单次 59.0 s；**实测匿名仅成功 1 次**（后续两次 p3 均 `error:null` @1.6 s） | `p2_cottage_moon.svg.raw.json`、`p3_mech_owl.svg.raw.json` |
| [SVGRender/DiffSketcher](https://huggingface.co/spaces/SVGRender/DiffSketcher) | **Apache-2.0** | **RUNNING**，cpu-basic，公开 | Gradio 4.21 `POST /call/process_images`，参数 prompt、path 数、seed 等 | **只返回 Image（PNG/WebP 预览），不返回 SVG**；有 500 path 参数但无矢量文件出口，故未用于 FDSL 链路 | `/info` 的 `returns` 为 `Image`（[API 信息](https://svgrender-diffsketcher.hf.space/info)） |
| [multimodalart/OmniSVG-3B](https://huggingface.co/spaces/multimodalart/OmniSVG-3B) 等 fork | Apache-2.0 | **不可用**：`BUILD_ERROR` / `RUNTIME_ERROR` / `PAUSED` | — | 不能作为匿名备选 | HF API `runtime.stage`：multimodalart=BUILD_ERROR；jieye06 / Brightcodelab / lukapercic=RUNTIME_ERROR；ifiokjr=PAUSED |
| [JohanDL/Text_to_SVG_GRPO](https://huggingface.co/spaces/JohanDL/Text_to_SVG_GRPO) | 未标注 | **RUNTIME_ERROR**，cpu-basic | — | 不可用 | HF API `runtime.stage=RUNTIME_ERROR` |
| [shljessie/starvector-text2svg](https://huggingface.co/spaces/shljessie/starvector-text2svg) | 未标注 | **RUNTIME_ERROR**，cpu-basic | — | StarVector 主线是 image→SVG；文本 Space 未跑起来 | HF API `runtime.stage=RUNTIME_ERROR` |
| [quiverai/arrow-1.1](https://replicate.com/quiverai/arrow-1.1)（Replicate） | 平台条款 | 需 **API token**，且**付费**：$0.01/credit × 20 credits/次 | Replicate API | 质量可能更高，但违反“无私有 key / 不付费”约束，**排除** | 页面价格字段 `"20 credits per generation at $0.01/credit"` |
| Hugging Face Inference API / 官方 endpoint | 模型各自许可 | 需 HF token（账号私有凭据） | REST | **排除**（需私有 key） | 文档要求 Authorization: Bearer |

> 许可说明：Space 卡与模型卡均写明 Apache-2.0 / MIT；**生成结果的再许可条款未单独声明**，商用前需法务复核（见第 8 节）。

---

## 2. 实测方法（无 key）

- 所有请求**不带 `Authorization` 头**，不读取 `HF_TOKEN`；仅用公开域名。
- Gradio 调用协议（Space 为 Gradio 6.0.2）：
  1. `POST https://<space>/gradio_api/call/<endpoint>`，body `{"data":[...]}` → `{"event_id": ...}`；
  2. `GET .../call/<endpoint>/<event_id>`，按 SSE 读取 `event:` / `data:`，直到 `complete` 或 `error`。
- 超时 180–600 s；`--retries 1`；每个调用保存 `.raw.json`（SSE 事件、耗时、prompt、Space id），便于复核。
- 脚本：`poc/online_gen/call_omnisvg.py`（多参数 OmniSVG）、`poc/online_gen/call_gradio.py`（单 prompt 通用）、`poc/online_gen/make_agent_svgs.py`（agent 手写 SVG）。
- 已观察到的失败形态：`event: error` + `data: null`，HTTP 本身仍为 200（SSE 连接成功），说明是 Space 侧配额/队列拒绝而非网络或鉴权失败。

---

## 3. 三条复杂 prompt 实测

四图对比：`poc/online_gen/compare_grid.png`（P1 / P2 / agent girl / agent scene）。

| # | prompt | 服务 / 模型 | 结果 | 耗时 | SVG 大小 | 转换后元素 / 点数 |
|---|---|---|---|---|---:|---:|---:|
| P1 | black-and-white anime-style portrait of a girl with long hair and horns, clean ink line art, detailed hair strands, symmetrical composition | OmniSVG-3B Space / 4B，max_length 768 | **成功**（1 条） | 51.42 s | 6 413 B，3 path | 3 / 704 |
| P2 | a small cottage with a tree and a fence under a crescent moon, black-and-white line art, clean outlines, night scene | SVGThinker-7B Space | **成功**（1 条） | 58.98 s | 2 139 B，17 shape | 29 / 230 |
| P3 | a detailed mechanical owl with spread wings, black ink line art, intricate gears and feathers, symmetrical, high detail vector illustration | SVGThinker-7B（2 次）+ OmniSVG（1 次） | **失败**：3 次均 `error:null`，@1.5–1.7 s | — | 0 | — |

### 3.1 P1：OmniSVG 4B（成功，但只是剪影）

- 原始 SVG：200×200、**仅 3 条 path**（白底 + 1 px 黑框 + 1 条 700 点黑色轮廓），无 `<text>`/`<image>`。
- `svg_import`：384×384 canvas，3 elements（3 fill / 0 stroke），704 points，3 colours，**0 warning**；PNG 512×512。
- 视觉：**黑色剪影 + 白色脸形挖空**，不是“line art”；ink 覆盖率 25.9%（大面积实心）。这是 4B 匿名 demo 在当前参数下的真实能力，不能当作“复杂线稿已解决”的证据。
- 证据：`p1_girl_horns.svg.raw.json`（status `✅ Using 4B model`）。

### 3.2 P2：SVGThinker-7B（成功，但画布利用率极低）

- 原始 SVG：`width=744.09 height=1052.36`，**无 viewBox**，17 个 SVG 形状（8 rect + 9 path），内容集中在 x 250–330 / y 295–450。
- `svg_import`：272×384 canvas，拆成 29 elements（15 fill / 14 stroke），230 points，2 colours，**0 warning**；PNG 363×512。
- 视觉：画面只在中央偏左占 **48×84 px**（bbox），ink **0.618%**；构图与“cottage/moon scene”可辨认但太小、偏移，直接使用不可接受。
- 证据：`p2_cottage_moon.svg.raw.json`（58.98 s，返回 SVG code + WebP 预览）。

### 3.3 P3：配额失败（证据）

- SVGThinker 第 1 次：`error` @1.7 s；第 2 次（约 1–2 min 后）：`error` @1.6 s。
- OmniSVG 4B：`error` @1.5 s。
- 均发生在 POST 成功后约 1.5 s，`data=null`，无错误文案；结合 P1/P2 的“首调成功、次调立即失败”，**判断**为匿名用户 GPU 配额耗尽（疑似每 Space/IP 一次生成配额，未取得服务端错误文案），而非代码或许可问题。
- 证据文件：`p3_mech_owl.svg.raw.json`、`p3_mech_owl_omnisvg.svg.raw.json`（多次尝试的耗时/事件）。

---

## 4. Agent 手写 SVG 兜底（2 个复杂示例）

> 这两份是**在线 agent 用 Python 手工编写/程序化生成**的 SVG，**不是模型输出**；SVG 内已写注释 `agent-authored programmatic SVG (WP-U fallback), not model output`。不冒充 OmniSVG/SVGThinker 结果。

| 文件 | 内容 | 结构 | 转换结果 |
|---|---|---|---|
| `agent_girl_halfbody.svg` | 半身少女线稿：双角、长发、刘海、眼睛/眉/鼻/嘴、颈肩、衣领 | 480×620；50 path + 2 ellipse + 2 circle | 64 elements（14 fill / 50 stroke），894 points，2 colours，**0 warning**；PNG 396×512，ink 8.2% |
| `agent_scene_cottage.svg` | 场景线稿：月牙、星星、山丘、小屋（屋顶/烟囱/门窗）、树、栅栏、小路、草石 | 640×420；98 path + 4 circle + 3 ellipse | 148 elements（39 fill / 109 stroke），973 points，3 colours，**1 warning**（`SI_W_DEGENERATE_SKIPPED`，退化小路径被跳过，不影响主体）；PNG 512×336，ink 7.4% |

- 视觉：少女示例可辨认出角、长发、动漫眼形与半身构图；场景示例包含完整的小屋/树/月亮/栅栏层次。二者结构复杂度（64/148 元素）明显高于两条模型输出（3/29 元素），但仍达不到参考图的美术水准。
- 结论：**“agent 直接写 SVG + svg_import”是当前最可控的兜底路径**，成本是人工/程序编写，不是模型推理。

---

## 5. 产物清单（`poc/online_gen/`）

| 文件 | 大小 | SHA-256 前 12 | 说明 |
|---|---:|---|---|
| `p1_girl_horns.svg` / `.plan.json` / `.png` / `.fdraw` | 6.4K / 40.4K / 14.1K / 44.6K | `95D545F516E9` / `6A68EC6404DF` / `5115BFDEB517` / `21E2AEC615C6` | OmniSVG P1 原始 SVG + FDSL + PNG + 容器 |
| `p2_cottage_moon.svg` / `.plan.json` / `.png` / `.fdraw` | 2.1K / 19.6K / 7.2K / 12.7K | `ED9E491A840F` / `742CE18C9998` / `92496F09A82B` / `47911939B957` | SVGThinker P2 |
| `agent_girl_halfbody.svg` / `.plan.json` / `.png` / `.fdraw` | 5.6K / 66.0K / 51.8K / 79.9K | `DD9D649A5809` / `490415B58C86` / `9606130657F9` / `90885D1C566C` | agent 手写半身少女 |
| `agent_scene_cottage.svg` / `.plan.json` / `.png` / `.fdraw` | 9.1K / 88.8K / 39.0K / 78.9K | `0295EE4E8915` / `3B2381B9EC38` / `B4E057B733E3` / `71D69B0164CC` | agent 手写场景 |
| `p1_girl_horns.svg.raw.json` | 14.7K | — | OmniSVG SSE 原始证据 |
| `p2_cottage_moon.svg.raw.json` | 3.5K | — | SVGThinker SSE 原始证据 |
| `p3_mech_owl.svg.raw.json` / `p3_mech_owl_omnisvg.svg.raw.json` | 0.3K / 0.3K | — | 配额失败证据（error/null） |
| `call_omnisvg.py` / `call_gradio.py` / `make_agent_svgs.py` | 5.4K / 4.2K / 13.5K | — | 复跑脚本 |
| `compare_grid.png` | 116.0K | `D99A4843542E` | 四图对比（P1/P2/agent girl/agent scene），便于快速目检 |

- 所有 SVG 经 `xml.etree.ElementTree` 解析通过；无 `<text>`、`<image>`、`foreignObject`。
- PNG 均为 `svg_import --png` 的 FDSL 编译+渲染链路产物，非外部截图。

---

## 6. 结论

| 问题 | 回答 |
|---|---|
| 能否在线生成复杂文本→SVG？ | **能，但当前只到“链路可行”**：2 个公开 Space 匿名返回合法 SVG，无 key、许可清晰。 |
| 质量能否满足目标？ | **不能**。P1 是 3-path 黑色剪影；P2 只占画布 0.6%；离“黑白人物线稿/复杂场景”差距大。 |
| 是否可重复？ | **匿名不可**：每 Space/IP 仅约 1 次 GPU 生成；失败为 `error:null`，无重试空间。 |
| SVG→FDSL 是否可靠？ | **可靠**：4 个 SVG 全部转换+渲染成功，0/1 warning，含 fill/stroke/group/transform 等真实结构。 |
| 无 key 的可行产品路径？ | **agent 直接写 SVG + svg_import**（确定性、可控、无模型）；或等待配额/账号方案。 |

---

## 7. 下一步建议

1. **拿到更多模型样本**：用 HF 账号（免费 300 s/day GPU 配额）重跑 OmniSVG **8B**，每 prompt 2–4 candidates，优先验证 P1/P3；或复制 Space 到自己的 GPU。注意这会引入账号（仍非付费 API key），需用户确认是否接受。
2. **加质量门禁**（不 model-specific）：`paths < 10` 或 `ink_bbox_area/canvas < 0.3` 或 `ink < 1%` 的 SVG 直接判为无效，重试/换候选；P2 会被此规则拦截。
3. **画布归一化**：对无 viewBox 或内容偏小的 SVG 做 ink-bbox 自动裁切 + 重新 viewBox，再进 `svg_import`（属于 WP-S 增强，本包未改核心）。
4. **Prompt 模板**：明确“full-bleed, subject fills 80% of canvas, black ink line art, 100+ paths, white background, no fills/shadows”；要求多候选并人工挑选。
5. **产品兜底**：保留 agent-authored SVG 生成器（可按主题扩展：人物/场景/图标），用确定性代码生成 path；复杂美术仍建议走“在线模型候选 + 质量门禁 + 人工修”。
6. **许可/合规**：Apache-2.0/MIT 是**模型/代码**许可，生成物的商用再许可未单独声明；上线前需法务确认，并记录模型名/版本/Space SHA。

---

## 8. 复跑命令

```powershell
$PY = "<python>"
cd "<workspace>"

# 在线模型（实测匿名每个 Space 仅约 1 次成功）
& $PY poc\online_gen\call_omnisvg.py --prompt "<prompt>" --out poc\online_gen\out.svg `
    --model-size 4B --max-length 768 --timeout 180 --retries 1
& $PY poc\online_gen\call_gradio.py --base https://caffin-svgthinker-7b.hf.space `
    --endpoint gradio_text_to_svg --prompt "<prompt>" --out poc\online_gen\out.svg

# agent 手写 SVG
& $PY poc\online_gen\make_agent_svgs.py

# SVG -> FDSL -> PNG/.fdraw
cd fourier-prompt
& $PY -m fourier_prompt.svg_import --svg ..\poc\online_gen\agent_girl_halfbody.svg `
    --out ..\poc\online_gen\agent_girl_halfbody.plan.json `
    --png ..\poc\online_gen\agent_girl_halfbody.png `
    --fdraw ..\poc\online_gen\agent_girl_halfbody.fdraw --resolution 512 --json
```

---

## 9. 限制与合规声明

- **样本量小**：只成功 2 条模型 SVG（P1/P2）；P3 因配额失败，未测 8B、未测多候选；质量判断为人工目视 + 客观 ink/bbox/path 统计，非 PSNR/SSIM（无参考图）。
- **可用性会变**：HF Space 可能暂停/改版/改配额；Gradio API 非正式稳定协议，脚本按 2026-10-21 的 6.0.2 行为实现。
- **未使用本地模型/本地 LLM**：无权重下载、无训练、无微调；傅里叶系数由 `compile_plan` 确定性计算。
- **无私有 key**：未读取/使用 `HF_TOKEN` 或任何 Authorization；所有请求可匿名复现。
- **版权**：未读取/复制 `[X(Twitter)] ttus` 参考图；模型训练数据（OmniSVG 的 MMSVG、SVGThinker 的基座 DeepSeek-R1-Distill-Qwen-7B）许可需单独核查；生成物商用前需法务确认。
- **agent 手写 SVG 的定位**：是“确定性程序化 SVG”，不是模型能力证明；报告中已明确标注，避免与模型输出混淆。
