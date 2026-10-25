# WP-B · skill-refactor 报告（skill 组装公共 API）

- 子代理：WP-B ｜ 共享任务：task-13 ｜ 日期：2026-10-20
- 工作目录：`<workspace>`
- 依据：`docs/impl/KICKOFF_v1.md` §2.2/§3/§4、`analysis/08_工程实现可行性评审.md` §2.1–§2.5、`fourier-brushstroke/CONTRACT.md` §6
- **结论：重构完成且行为不变**——10 个 build case 的「归一化 doc hash + PNG 像素 hash + `write_fbs` 字节数 + 量化内容 hash」改前/改后**逐项相等**；skill 全量 unittest **290/290 OK**；验收子集 **12/12 PASS**；`check_hygiene --strict` **0 命中 / exit 0**。

## 1. 基线（改前冻结）口径

基线文件：`fourier-brushstroke/tests/_baseline/baseline.json`（schema `fbs-build-baseline/1`，含命令/时间/版本），
冻结工具：`tests/_baseline/freeze_baseline.py`（`--mode check` 复跑比对）。

每个 case 的四项**硬判据**（必须全等）：

| 判据 | 定义 |
|---|---|
| `doc_sha256` | 归一化 doc（剔除唯一非确定字段 `created` + 绝对路径 `source.path`）的 `sha256_json` |
| `png_sha256` | `render.render(doc, R, supersample=2, mode="sweep")` 的 RGB uint8 原始字节 sha256 + `png_shape` |
| `fbs_bytes` | `io_format.write_fbs()` 返回的字节数（= 写入 JSON 文本 UTF-8 字节数） |
| `fbs_content_sha256` | `quantize_doc(doc)` 后再归一化的 JSON sha256（比字节数更严，能抓精度漂移） |

> `write_fbs` 落盘文件自身 sha256 每次都因 `created` 变化，因此**不作为判据**；另记录 `stroke_count / style_count / fft_passes / fill_soft_px / method / warning_codes` 供解释。

**7 个官方 case**：T1/T2/T3/T4（KICKOFF 指定）+ T5/T12/T13（合成线稿：尖角、颜色渐变、step 宽度）。
**3 个额外分支 case**（不进 7-case 判据，但同样比对，专门覆盖 `fill_soft` auto / mesh / painterly 三条覆盖逻辑）：
T1@R512、T2@R256+mesh、T1@R256+painterly（`brush_strokes=300` 控制耗时）。

环境：Python 3.13.14 / numpy 2.5.4 / Pillow 12.3.0 / Windows-11-10.0.26200。冻结耗时 31.45 s。

## 2. 改前 / 改后逐项对比（全部 OK）

| case | 归一化 doc hash（前 16） | PNG 像素 hash（前 16） | `write_fbs` 字节 | 复跑确定 | 改前=改后 |
|---|---|---|---|---|---|
| T1_signature@R256 | `30007101a25a4218` | `a02ea7f823cb4f04` | 8,543 | 是 | ✅ |
| T2_icon@R256 | `faa3b4fd2417…` | `29e4d1054c70…` | 236,697 | 是 | ✅ |
| T3_flat_illustration@R256 | `f1416b3517e5…` | `e143d3d7b344…` | 336,354 | 是 | ✅ |
| T4_taper@R256 | `70e08de3f224…` | `df1eb351eb0c…` | 12,168 | 是 | ✅ |
| T5_sharp_corners@R256 | `09911669c682…` | `e516dfdb9133…` | 112,587 | 是 | ✅ |
| T12_color_gradient@R256 | `90a6f8d44ad6…` | `6fab3cf76a69…` | 73,044 | 是 | ✅ |
| T13_step_intervals@R256 | `3fe028e79e3e…` | `6fd4f08c89a3…` | 9,592 | 是 | ✅ |
| T1_signature@R512（extra） | `3f5fcaf469b1…` | `e960cf3b3e5a…` | 9,439 | 是 | ✅ |
| T2_icon@R256+mesh（extra） | `70b8228ce65b…` | `5c571d85852e…` | 2,657,845 | 是 | ✅ |
| T1_signature@R256+painterly（extra） | `28780bd89c68…` | `cb740b361fb4…` | 514,372 | 是 | ✅ |

完整 64 位 hash 见 `baseline.json`；改后复跑日志 `tests/_baseline/check_after.log`（`[check] PASS：10 项 doc/PNG/字节数/量化内容 全部与基线相等，且复跑确定`）。
分支语义核对：普通图 `fill_soft_px=0.5/2.0`（lineart/flat_art vs illustration）、mesh `fill_soft_px=0.0` + `method=adaptive_gradient_mesh`、painterly `method=painterly_brush_strokes`，`stats.fft_passes == stroke_count`（逐笔画通道）均与改前一致；warning code 及其顺序（如 T5 `W_GAP_BRIDGED`、mesh `W_MESH_ACTIVE`、painterly 5×`W_PAINTERLY_LAYER` + `W_PAINTERLY_ACTIVE`）逐项相等。

## 3. 重构内容（`scripts/fbs_core/document.py`，仅此一个 skill 源文件）

新增（additive，均为公开符号）：

1. `class StyleTable`：`brush_styles` 精确去重（键 `sha256_json(style)`，浮点全等才共享，首见顺序=style id）；接口 `add(style)->int` / `styles` / `__len__`。
2. `def stroke_record(idx, el, fit, brush, cfg=None, *, bbox=None, layer=None, region_id=None)`：原 `_stroke_record` 函数体 + bbox/layer/region_id 注入（FDSL `gen` 元素无 `element["points"]` 时用展开采样点 bbox）；`_stroke_record = stroke_record` 保留旧私有名（`structdoc.py` 的 5 位置参数调用不变）。
3. `def assemble_document(elements, *, cfg, warn, source, image_class, canvas, decomposition, base_color, contract=None, accelerators=None, regions=None, mode="per_stroke", fill_soft_px=None, method=None, fft_passes=None, record_ids="index", catch_brush_errors=False) -> (doc, info)`：
   与 `build_from_image` 原内联段**同序**（fit → brush → StyleTable → stroke_record → `new_document` → fill_soft/method 覆盖 → `stats.fft_passes` → `finalize_document`）；
   `record_ids="index"` 保持 build 的「跳过元素→id 留洞」语义，`"sequential"` 重排；`catch_brush_errors=False` 保持 brush 异常冒泡，`True` 降级为 `W_GEOMETRY_SKIP`（stage=brush）并跳过；`info = {strokes, styles, skipped[], fft_passes, timings.stage2_3_fourier_brush}`。
4. `build_from_image` 的组装段（样式建表 + strokes 循环 + 骨架 + fill_soft/method + finalize）替换为**一次** `assemble_document` 调用；`W_MAX_CURVES` 截断、Stage0–1、info 其余字段仍在原函数内，行为不变。

代码统计：

| 项 | 改前 | 改后 |
|---|---|---|
| `document.py` 总行数 | 338 | 440（+102：additive API 约 +217 行，删旧内联组装 55 行 + 旧 `_stroke_record` 32 行） |
| `build_from_image` 行跨度 | 68–229（162 行） | 237–366（130 行，**−32**） |
| 组装实现份数 | 1（内联，私有） | 1（`assemble_document`，公开；`build_from_image` 只调用） |

新增测试 `tests/test_document_assemble.py`（15 例，unittest，0.67 s）：`StyleTable` 去重/顺序/近似不合并、`stroke_record` 旧字段与 bbox/layer/region_id 注入 + poly/holes、`assemble_document` 默认值/显式 `fft_passes`/`fill_soft_px`/`method`/`record_ids`/warnings 顺序/brush 异常两策略/非法参数、`build_from_image` 仅调用一次 `assemble_document`（auto fill_soft=0.5、不传 fft_passes/record_ids）、T1 与冻结基线 doc/PNG/字节数相等。

## 4. 回归测试（全部通过）

```powershell
$PY = "<python>"
$SK = "<workspace>\fourier-brushstroke"
$env:PYTHONDONTWRITEBYTECODE = "1"

# ① 基线冻结（改前）/ 校验（改后）
& $PY -B "$SK\tests\_baseline\freeze_baseline.py"                 # → baseline.json（31.45 s）
& $PY -B "$SK\tests\_baseline\freeze_baseline.py" --mode check    # PASS，10/10 identical

# ② skill 全量单测
cd $SK; & $PY -B -m unittest discover -s tests                    # Ran 290 tests in 17.160s — OK

# ③ 验收子集（build/render/onestroke，quick + 跳过视频）
cd "$SK\scripts"
& $PY -B fbs.py acceptance --quick --skip-video `
  --items A1,A2,A3,A5,T1,T2,T3,T4,T5,T12,T13,R2 `
  --report-dir "$SK\tests\_baseline\acceptance"                   # 12/12 PASS，0 fail/0 blocked，14.2 s

# ④ 静态卫生
cd "$SK\scripts"; & $PY -B check_hygiene.py --strict               # 57 个 .py，0 命中，exit 0
```

- 验收 `R2`（一笔画）：`stroke_count=1 / fft_passes=1 / path_deviation_px≈0.0012`（PASS），说明 `assemble_document` 未影响 onestroke 输入文档契约。
- 单测数：重构后新增测试前本机实测 275 → 290（+15 新增）。
- `check_hygiene` 报告落 `assets/out/acceptance/hygiene.txt`（工具默认产物，未改源码）；验收报告在 `tests/_baseline/acceptance/`。

## 5. 契约解释与偏差（需 Lead 知晓）

1. **`stroke_record` 签名**：KICKOFF §2.2 写作 `stroke_record(element, *, cfg, layer, region_id, warn, bbox=None, catch_brush_errors=False)`，但它未交代 `fit`/`brush` 从哪来、以及 style id 需要 `StyleTable` 上下文——单靠 element 无法产出合法 `brush.style`。按 Lead 明确要求「公开 `_stroke_record`（保留兼容）」与 analysis/08 §2.2 的精确签名，实现为**记录构造器** `stroke_record(idx, el, fit, brush, cfg=None, *, bbox, layer, region_id)`；元素级流程由 `assemble_document` 承担（其签名与 KICKOFF 完全一致，含 `catch_brush_errors`）。batch 2 的 `compile.py` 可直接复用二者，无需再改 skill。
2. `stroke_record` 的 `cfg` 形参保持为**兼容位**（原函数体从未使用，保留位置参数以免 `structdoc.py`/外部 PoC 破坏）；`bbox/layer/region_id` 为新增 keyword-only。
3. `assemble_document` 的 `contract=None` → 用 `cfg.contract()`；`fft_passes=None` → `len(strokes)`；`fill_soft_px/method=None` → 不覆盖文档原值（build 显式传入计算值，因此与旧行为逐字节等价）。
4. 参数名 `accelerators` 与模块级 `util.accelerators` 同名（KICKOFF 原签名如此），函数体内只使用入参，不调用 util，无遮蔽风险。

## 6. 风险 / 未完成

- **无 git**：无法给出 `git diff`；改动锚点是本报告 + baseline.json + 新增测试。`document.py` 已包含全部改动，可用 analysis/08 §2.2 代码块目视回滚（无其他文件依赖新符号）。
- `write_fbs` 文件 sha256 不可作判据（`created` 每次变）；已用「量化内容 hash」替代并证明相等。
- 验收只跑子集（12 项，quick+skip-video）；全量含 R1024 渲染/60s 视频/真实图，按 KICKOFF 时间预算未跑（风险：低——改动只在文档组装，渲染/视频路径未触碰，R2 已覆盖 onestroke 输入契约）。
- `tests/_baseline/baseline.json` 与 `out` 产物与机器/库版本绑定（numpy/Pillow 升级后像素 hash 可能变）；换机复跑需先确认渲染确定性，再决定是否重新冻结。
- 边界说明：本轮写盘仅 `fbs_core/document.py`、`tests/test_document_assemble.py`、`tests/_baseline/**`；`assets/out/acceptance/hygiene.txt` 是验收/卫生工具自身产物，未手改。

## 7. 产物

| 文件 | 说明 |
|---|---|
| `scripts/fbs_core/document.py` | 唯一 skill 源改动：`StyleTable` / `stroke_record` / `assemble_document` + `build_from_image` 改一次调用 |
| `tests/test_document_assemble.py` | 新增 15 例公共 API/回归测试 |
| `tests/_baseline/baseline.json` | 改前冻结的 10 case 四项指纹 + 环境/命令/时间 |
| `tests/_baseline/freeze_baseline.py` | 冻结/校验工具（`--mode freeze|check`） |
| `tests/_baseline/freeze.log` / `check_after.log` | 冻结与改后比对原始输出 |
| `tests/_baseline/unittest_final.log` | 全量单测输出 |
| `tests/_baseline/acceptance/report.{json,md}` | 验收子集报告 |
