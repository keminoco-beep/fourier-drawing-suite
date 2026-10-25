# WP-M — Agent Skill 封装 + 一键演示（共享任务 task-23）

- 日期：2026-10-20 ｜ 状态：**完成**
- 写盘范围：`fourier-prompt/skill/`、`scripts/demo.py`、`tests/skill/`、`reports/WP-M.md`
- 未改动：核心包、`fourier-brushstroke/`、`docs/`、`analysis/`、其它 `tests/`
- 环境：Windows / Python 3.13.14 / numpy 2.5.4 / Pillow 12.3.0 / imageio-ffmpeg 可用；纯 CPU、离线

---

## 1. 结论

- `skill/` 交付 agent 入口 `SKILL.md`（可无 PyYAML 解析的 frontmatter）、人类版 `README.md`、
  两个模板、两个可严格校验/可编译示例；
- `scripts/demo.py` 打通 `schema → plan2fbs → draw → fdraw pack →（layer export）→（video）`，
  逐步打印路径/大小/耗时/哈希，失败非零并带错误码；
- `tests/skill/` 共 **18 例**，全部通过；
- 验收命令（R=256，PSD+KRA）成功，重复运行 `.fbs` / PNG / `.fdraw`（以及 PSD/KRA）SHA-256 完全一致。

---

## 2. 交付文件

| 文件 | 大小 | 说明 |
|---|---|---|
| `skill/SKILL.md` | 14.1 KiB | agent 入口：frontmatter、触发词、输入约束、命令清单、流程、质量/限制、退出码 |
| `skill/README.md` | 6.4 KiB | 人类版：安装/依赖/quickstart/产物/测试/已知限制 |
| `skill/templates/text_to_drawing.md` | 6.5 KiB | prompt → FDSL 的填写模板、字段表、生成元参数、校验循环、交付自检 |
| `skill/templates/edit_prompt.md` | 3.5 KiB | 编辑请求模板；**明确 `fdsl-edit/1` 未实现、不能原地编辑** |
| `skill/examples/text_plan.json` | 1.9 KiB | 主 demo：中文/英文 outline 文本 + 笔刷下划线 + 五角星 + 3 层 |
| `skill/examples/simple_plan.json` | 1.2 KiB | 无字体依赖：圆 + 六角星填充 + 笔刷曲线 + 2 层 |
| `skill/examples/README.md` | 3.1 KiB | 两个示例的用途、命令、预期产物、确定性说明 |
| `scripts/demo.py` | 17.7 KiB | 一键演示（只编排 CLI/API，不复制核心逻辑） |
| `tests/skill/_support.py` + 3 个测试文件 | ~15.0 KiB | 18 例：示例、demo、文档契约 |
| `reports/WP-M.md` | 本文件 | 命令/产物/测试/限制记录 |

`SKILL.md` frontmatter 为单层 `key: value`（`name` / `description` / `agent_created`），
测试用 10 行解析器读取，**不依赖 PyYAML**。

---

## 3. 验收命令与实测输出

```powershell
$PY = "<python>"
$FP = "<workspace>\fourier-prompt"
cd $FP

& $PY scripts\demo.py --plan skill\examples\text_plan.json --outdir out\demo --psd --kra --resolution 256
```

实测（2026-10-20，最终代码）：

```text
fourier-prompt demo
plan:       ...\fourier-prompt\skill\examples\text_plan.json
outdir:     ...\fourier-prompt\out\demo
resolution: 256
extras:     psd, kra

[1/6] schema       ok  elements=5 layers=3 texts=0 warnings=0 (0.000s)
[2/6] plan2fbs     ok  out\demo\text_plan.fbs 338.3 KiB sha256=3f9ee476876d9e7a records=77 fills=76 strokes=1 text=2 (1.155s)
[3/6] draw         ok  out\demo\text_plan_R256.png 9.3 KiB 256x171 sha256=142241a1ba84946d (0.195s)
[4/6] fdraw pack   ok  out\demo\text_plan.fdraw 38.0 KiB root_sha256=6be139d668ec30b4 elements=77 layers=[0, 1, 2] (0.065s)
[5/6] layer export ok  psd=out\demo\layers\text_plan.psd 812.7 KiB; kra=out\demo\layers\text_plan.kra 39.1 KiB layers=3 (2.359s)
[6/6] video        skipped (pass --video)

done: out\demo in 3.98s
determinism: repeated runs with the same plan/resolution produce identical .fbs / PNG / .fdraw bytes
```

> 耗时为一次本机实测（随机器波动）；产物哈希是确定性的，见 §4/§5。

> 文本示例共 77 条渲染记录：76 fills（其中文本烘焙 74 fills）+ 1 stroke；
> 3 层均被 PSD/KRA 导出。PNG 为 256×171（长边 256，按 384×256 画布宽高比）。

---

## 4. 产物与哈希（out/demo）

| 产物 | 字节 | SHA-256 |
|---|---:|---|
| `text_plan.fbs` | 346,430 | `3F9EE476876D9E7AC212CBC3432B3FED60514321277BE01C3DF4E048F9C2C7C0` |
| `text_plan_R256.png` | 9,550 | `142241A1BA84946D14296E38936E043D0101B5175731D18EDC8CC9134617B4E1` |
| `text_plan.fdraw` | 38,935 | `458E53DE40C0240FBBB2D166CB9D72999DE13AFE2949E8FF701AEA1E145679F7` |
| `layers/text_plan.psd` | 832,210 | `7F883143A966DD102FE46B25BCAD03B979A7E24DF9E19F36EEAC200DFA240A2F` |
| `layers/text_plan.kra` | 40,089 | `DEA1CA4704DAB952CB2FDD7BC5C24B72ECB59CDD89207DEE696A1393E8FDC93D` |

`.fdraw` root hash：`6be139d668ec30b4…`（`fdraw_cli inspect` 校验通过：`root_sha256_ok`）。

---

## 5. 确定性验证

方法：同一命令输出到两个目录（`out/demo`、临时 `out/demo_b`），逐文件比较 SHA-256：

| 文件 | 两次运行一致 |
|---|---|
| `.fbs` | ✅ |
| `_R256.png` | ✅ |
| `.fdraw` | ✅ |
| `.psd` | ✅（附加） |
| `.kra` | ✅（附加） |

实现要点（不复制核心逻辑）：

- 编译走 `fourier_prompt.compile.compile_plan`，写盘走 `compat.skill_api.write_fbs`；
- 渲染走 `compat.skill_api.render`（exact）+ Pillow PNG；
- 容器走 `fourier_prompt.fdraw.pack`（固定 ZIP 时间戳、canonical JSON）；
- **`.fbs` 的 `created` 是墙钟元数据**，demo 在写盘前移除，以保证字节级可复现
  （`compile.normalized_fbs` 同样排除该字段）。此行为在 `demo.py` docstring、`skill/README.md`、
  `skill/examples/README.md` 中均已说明；直接调用 `cli plan2fbs` 时该字段仍存在。

失败路径实测（`tests/skill`）：不存在的 plan → 退出 2 + `FP_E_LOAD`；R=8 → 退出 2 + `FP_E_USAGE`；
schema 错误 → 退出 1 + 首个 `FP_E_*`；layer/video 子进程失败会透传其非零退出码并打印 `error[CODE]`。

---

## 6. 测试

```powershell
& $PY -m unittest discover -s tests/skill -v
```

结果：**Ran 18 tests in 10.196s — OK**（最终代码）。

| 文件 | 例数 | 覆盖 |
|---|---:|---|
| `test_examples.py` | 4 | 两个示例 JSON/schema 合法；`--strict` 零警告；可编译出记录；text/simple 的文本差异；examples README 引用 |
| `test_demo.py` | 7 | demo 成功产物齐全；`.fbs`/PNG/`.fdraw` 字节确定性；`--psd --kra` 导出（PSD `8BPS`、KRA 为 zip）；缺 plan / 坏 resolution 的退出码；**从包根之外调用**（相对路径解析回归）；demo 只调用公开接口、无核心逻辑复制 |
| `test_skill_docs.py` | 7 | frontmatter 解析与触发词；命令面完整（plan2fbs/draw/fdraw/fdrender/fdvideo/layer/mcp）；限制字段（无 LLM/无字体回退/编辑未实现/PSD-KRA 未真机/fast 契约）；模板存在且被引用；text 模板可执行；edit 模板明确“未实现”；README quickstart/布局 |

测试只用 stdlib `unittest`；demo 子进程固定小分辨率 R=64，整套 <12 s。

补充实测（未纳入自动测试，避免 CI 依赖 ffmpeg）：`simple_plan` + `--video --duration 1 --fps 12 --resolution 64`
→ 12 帧、2.8 KiB、约 1.2 s，退出 0。

---

## 7. 示例校验/编译

```powershell
& $PY -m fourier_prompt.cli schema --plan skill\examples\text_plan.json --strict
& $PY -m fourier_prompt.cli schema --plan skill\examples\simple_plan.json --strict
```

两者均 `0 error / 0 warning`；R=64 编译结果：

| 示例 | records | fills | strokes | text | text_fills |
|---|---:|---:|---:|---:|---:|
| `text_plan.json` | 77 | 76 | 1 | 2 | 74 |
| `simple_plan.json` | 3 | 2 | 1 | 0 | 0 |

字体使用 `source_han_sans_sc`（映射到打包 Noto Sans SC 子集），示例字符均覆盖；
换用未打包字体或子集外字符会 `FP_E_TEXT_FONT` / `FP_E_TEXT_GLYPH`，不会回退系统字体。

---

## 8. 限制与未完成项（诚实清单）

1. **无本地 LLM**：prompt→FDSL 由 agent 按模板完成；本包不调用模型/网络。SKILL 中已明确。
2. **无字体回退**：仅打包 Noto Sans SC 子集；`as:"glyph"` 未实现；文本烘焙为 fills，不是可编辑文字。
3. **无编辑 op**：`fdsl-edit/1` 未实现；`.fdraw` 的 `source.fdsl.json` 只用于溯源/重新编译。
   `templates/edit_prompt.md` 用“错误示范 + 正确替代”明确这一点，测试锁定。
4. **PSD/KRA 未真机验证**：仅在 Pillow/自写解析器层回读；未在真实 Photoshop/Krita 打开抽检。
   报告与 SKILL 均标注为“可交换草稿”，未夸大。
5. **fast 质量档**：口径为 PSNR ≥ 30 dB / within2 ≥ 80%，细笔触 `max|Δ|` 可达 40–60；
   `exact` 才是权威。demo 的 PNG 用 exact，PSD/KRA/video 用 fast。
6. **视频未全量验收**：仅做 1 s/R64 冒烟；未跑 20 s/R256 的完整视频（可选步骤，依赖 ffmpeg）。
7. **R 限制**：demo 16–256；CLI/分层导出 16–512；更大 R 未做性能验收。
8. **图层语义**：plan 的 `visible/opacity` 不改变 `.fbs` 渲染（编译 warning），导出侧按元数据烘焙。

---

## 9. 写盘边界与兼容性

- 新增/修改仅限任务允许的四个位置；本工作区不是 git 仓库，按 §2 交付清单核对：
  清单之外的既有文件均未改动。核心包、`fourier-brushstroke/`、`docs/`、`analysis/`、
  其它 `tests/` 均保持只读。
- 未新增第三方依赖；demo/tests 只用 stdlib + 既有 numpy/Pillow/技能包。
- `out/demo/` 为验收产物目录（按契约生成），不属于源码改动；临时对比目录已清理。
