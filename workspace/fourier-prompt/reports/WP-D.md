# WP-D 报告 — 编译器与端到端（`plan2fbs` / `draw`）

- 日期：2026-10-20
- 范围：`fourier-prompt` 的 skill 适配层、生成元库、legacy 转换器、编译器、CLI 扩展、集成测试
- 约束遵守：只读使用 `fourier-brushstroke/`；未改 skill、`fourier_prompt/brush/`、`fdraw.py`、WP-A 其他文件、`docs/`、`analysis/`；纯 CPU、无联网、无新增依赖。

## 1. 交付文件

| 文件 | 作用 |
|---|---|
| `fourier_prompt/compat/__init__.py` | 兼容层包 |
| `fourier_prompt/compat/skill_api.py` | 唯一 skill 适配层：路径解析（`FBS_SKILL_DIR` → 工作区相对路径）、`sys.path` 注入、`assemble_document` / `stroke_record` / `StyleTable` / `render` / `write_fbs` / `selftest` 转发 |
| `fourier_prompt/generators.py` | 10 个确定性生成元（纯 `math`）：circle / ellipse / arc / regular_polygon / star / rose / spiral / wave / lissajous / superellipse |
| `fourier_prompt/legacy.py` | 早期 `fdsl/2` 草稿 → 规范 `fdsl/2.1`（`canvas.w/h`、`type`、`fill.gen`、`repeat`、`bezier`、`render` 归一） |
| `fourier_prompt/compile.py` | `compile_plan()`：元素 → skill 记录 → `assemble_document` → 校验 `.fbs` |
| `fourier_prompt/cli.py` | 新增 `plan2fbs` / `draw`，保留 `schema` / `selftest` / `version`；退出码 0/1/2/3 |
| `tests/integration/` | `_fixtures.py` + 5 个测试模块，共 **67 个测试** |
| `reports/WP-D.md` | 本报告 |

产物（可复现）：`out/wp-d/<name>.fbs`、`out/wp-d/<name>_R256.png`、`out/wp-d/summary.json`。

## 2. 复现命令

```powershell
$PY = "<python>"
$FP = "<workspace>\fourier-prompt"
cd $FP

# 集成测试（67 例）
& $PY -m unittest discover -s tests/integration -v

# 单例：编译 + 渲染
& $PY -m fourier_prompt.cli plan2fbs --plan ..\poc\fdsl\examples\01_rose_medallion.json --out out\wp-d\01_rose_medallion.fbs --resolution 256
& $PY -m fourier_prompt.cli draw --plan ..\poc\fdsl\examples\01_rose_medallion.json --out out\wp-d\01_rose_medallion_R256.png --resolution 256 --json

# 保留命令
& $PY -m fourier_prompt.cli version
& $PY -m fourier_prompt.cli selftest
```

`FBS_SKILL_DIR` 可指向 skill 根目录或 `scripts/`；未设置时从包路径推导 `<repo>/fourier-brushstroke`，再回退 `./fourier-brushstroke`。找不到时 `SkillApiError` fail-loud。

## 3. 六例端到端结果（R=256，supersample=2）

命令：`plan2fbs --resolution 256` + `draw --resolution 256`，全部 `exit=0`。机器可读明细见 `out/wp-d/summary.json`。

| # | 草稿 | 笔数（records/FFT） | fill / stroke | styles | 警告 | compile s | 总耗时 s | .fbs KiB | PNG |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `01_rose_medallion.json` | 51 / 51 | 4 / 47 | 7 | 6 | 0.28 | 3.48 | 144.0 | `out/wp-d/01_rose_medallion_R256.png` |
| 2 | `02_star_badge.json` | 73 / 73 | 3 / 70 | 6 | 6 | 0.46 | 1.79 | 117.6 | `out/wp-d/02_star_badge_R256.png` |
| 3 | `03_golden_spiral.json` | 7 / 7 | 1 / 6 | 5 | 6 | 0.06 | 0.66 | 27.3 | `out/wp-d/03_golden_spiral_R256.png` |
| 4 | `04_ocean_waves.json` | 13 / 13 | 3 / 10 | 9 | 6 | 0.11 | 0.80 | 54.1 | `out/wp-d/04_ocean_waves_R256.png` |
| 5 | `05_aurora.json` | 70 / 70 | 3 / 67 | 19 | 10 | 0.37 | 1.91 | 97.7 | `out/wp-d/05_aurora_R256.png` |
| 6 | `06_cat_tophat.json` | 51 / 51 | 11 / 40 | 12 | 6 | 0.28 | 3.77 | 208.1 | `out/wp-d/06_cat_tophat_R256.png` |

说明：
- “总耗时”= `draw` 的 CLI 墙钟时间（编译 + 渲染 + PNG 写盘），含进程内 CLI 开销；单次渲染本身 01/06 约 3.0–3.4 s，其余 0.6–1.6 s。
- 六例警告均来自 legacy 归一，不是错误：`FP_W_LEGACY_CANVAS`×3（w/h/bg）、`FP_W_LEGACY_LAYER`×1（合成 layers）、`FP_W_LEGACY_RENDER`×2（render/render_contract → meta）；05 另有 4 条 `FP_W_LEGACY_BRUSH_ALIAS`（`airbrush` → `basic_airbrush`）。
- 六例 `png_sha256` 均记录在 `summary.json`；PNG 目视复核：玫瑰窗、星章、金螺旋、海浪、极光、猫与礼帽的构图/层次/颜色均正确（重复刻度、星形尖角、贝塞尔尾巴、文字跳过均符合预期）。
- R=512 冒烟：`04_ocean_waves` → 512×512 PNG，2.49 s，exit 0。

## 4. 规范 FDSL v2.1 手写 plan

| plan | 画布 | 元素/records | fill/stroke | 警告 | compile s | render s（R=256） | 结果 |
|---|---|---|---|---|---|---|---|
| `canonical_plan_shapes()` | 256×256 | 5 / 4 | 2 / 2 | 1（text skipped） | 0.52 | 0.27 | `fourier_gen` 闭合→fill、`closed:false`→stroke、带洞 fill、text 跳过 |
| `canonical_plan_curves()` | 320×240 | 4 / 4 | 2 / 2 | 0 | 0.04 | 0.15 | 非正方形画布输出 256×192（长边=分辨率） |

两例均通过 `write_fbs` 结构校验并用 skill `read_fbs` 回读 + `render.render` 成功。

## 5. 确定性与校验

同一输入连跑两次（`01_rose_medallion`、`06_cat_tophat`，R=256）：

| 输入 | `.fbs` 归一化 hash（去除 `created`） | PNG 像素 hash | 两次相等 |
|---|---|---|---|
| 01_rose_medallion | `204228870d9ee1c6…` | `2b937e24ec34b661…` | 是 / 是 |
| 06_cat_tophat | `1a2d3dff561743ec…` | `69dc983adc85aaff…` | 是 / 是 |

- `.fbs` 原始字节含 skill 写入的 `created` 墙钟字段，因此用 `compile.normalized_fbs()`（仅剔除 `created`，其余字段全保留、`sort_keys`）比较；PNG 按像素字节 hash 比较。
- `compat.selftest()`：`skill_dir`、`fbs_core 2.0.0`、一次最小 `assemble_document`、`write_fbs`、`render 64×64×3` 全部 ok。
- `write_fbs` 在 `compile_plan` 内调用（量化 + 结构校验），CLI 再写到目标路径；坏文档在编译期即失败。

## 6. 测试

| 套件 | 结果 |
|---|---|
| `tests/integration`（WP-D） | **67 tests OK**（14.8 s） |
| `tests/brush`（回归） | 69 tests OK |
| `tests/core`（回归） | 82 tests，**1 个预期失效**（见风险 R1） |

集成测试覆盖：
- 10 个生成元各自几何/确定性/未知参数/退化参数（15 例）；
- legacy 检测 6/6、canvas/kind/fill.gen/repeat/scale/bezier/meta/layer/alias 归一、未知字段 warning、未知 type/repeat/gen 报错（17 例）；
- compile 映射（闭合 fill、`closed:false` stroke、holes、transform、palette、宽度曲线）、`.fbs` 回读渲染、归一化 hash（10 例）；
- 失败路径（未知 gen/brush/kind/参数、空 plan、退化 fill、非法 resolution、坏 plan/schema）（10 例）；
- 六草稿 + 两规范 plan 端到端、确定性、非空白图（5 例）；
- CLI 成功/JSON/`--fbs-out`/legacy 自动转换/确定性 + 退出码 1/2/3（12 例）。

## 7. 实现要点

- **唯一组装入口**：`compile.py` 只构造 WP-B 需要的 element 记录（`points/s/widths/colors/alphas/shapes/fill_role/fill_poly/fill_holes/layer`），一次调用 `assemble_document(..., record_ids="sequential", fill_soft_px=..., method="fdsl_compiler")`；Fourier 拟合、笔刷曲线、StyleTable 去重、文档骨架/量化全部由 skill 完成，包内没有第二套实现。
- **元素映射**：`stroke` 点列→FFT 笔画；`fourier_gen` 闭合轮廓（默认）→ 显式 `poly` 填充，`closed:false` → FFT 笔画；`fill` 显式 `poly`+`holes`；`text` 跳过并 warning。闭合 fill 使用显式多边形（尖角精确、零傅里叶误差），开曲线仍走 `fit_curve`（1 次 FFT/笔画）。
- **legacy 归一**：`canvas.w/h`→`width/height`，`bg`→`background`；`type`→`kind`；`fill.gen`→`poly`（`generators.contour`）；`repeat` 展开为带 `transform` 的显式副本（`around` 为 pivot，`sweep/n` 步进，`scale` 保留）；`bezier`→折线；`render`/`render_contract`→`meta.legacy_render*`；未声明 layers 自动合成；未知字段 warning 后丢弃，未知类型/gen/repeat 报错。
- **legacy `type:"gen"` → 规范 `stroke`**：规范 schema 中闭合 `fourier_gen` 的语义是**填充区域**，若直接映射会把所有旧线稿变成实心块。因此旧 gen 展开为闭合/开放折线笔画（256 采样），保留原渲染；代价是转换后的 plan 不再携带生成元参数（见风险 R2）。
- **颜色/透明度**：调色板名、`#rgb/#rrggbb/#rrggbbaa`、`[r,g,b]/[r,g,b,a]`（0–255 或 0–1）；alpha = `brush.alpha × 颜色alpha × element.opacity`。宽度支持标量或沿弧长的 `[[t,w],...]`，与旧草稿语义一致；transform 的 `scale` 不缩放笔宽（与旧 fdlib 行为一致）。
- **失败即报错**：schema 错误汇总成 `CompileError`（保留首个 code/path）；未知生成元参数、`transform.skew`、退化几何（fill 二维协方差为 0）、空可绘制元素、skill 组装/`.fbs` 校验异常都 fail-loud，不静默跳过（text 跳过是唯一显式 warning 例外）。
- **CLI**：`plan2fbs`/`draw` 自动识别 legacy，`--legacy` 可强制；`--resolution` 限 [16, 512]、`--supersample` 限 [1, 4]；`--json` 输出 stats/warnings/ledger/路径/hash；退出码 1=计划/编译失败、2=输入/用法错误、3=保留命令或内部错误。

## 8. 限制与风险

- **R1（需 Lead 处理）**：`tests/core/test_cli.py::test_reserved_command_fails_loudly` 仍把 `plan2fbs`/`draw` 当作“保留命令、退出 3”，与批次 2 契约冲突。该文件属 WP-A 写边界，WP-D 未改；现状是 `tests/core` 82 例中 1 例因这一陈旧断言失败（其余 81 例通过）。建议 Lead 从该测试的 reserved 列表移除 `plan2fbs`/`draw`，保留 `pack`/`eval`。
- **R2**：legacy `type:"gen"` 转换为显式 `stroke` 点列（见 §7）。旧 plan 的 `repeat` 也被展开为最多 60 份副本，转换后元素数从 13 增至 51–73；这不是渲染损失，但 `.fbs` 与转换后 plan 会更大，且不再保留“生成元 + 参数”的可编辑性。若后续需要“可编辑 repeat”，应在批次 3 增加 fbs 层 instance 表达，而不是回退本次映射。
- **R3**：文本元素本批跳过（`FP_W_TEXT_SKIPPED`），字体子系统属批次 4；含 text 的 plan 仍可编译（其余元素正常），纯 text plan 报 `FP_E_COMPILE_EMPTY`。
- **R4**：skill 的 per-stroke 文档没有 layer 级 `opacity/visible` 语义；编译器对 `layers[].opacity != 1` / `visible=false` 发 warning 并保留元素（逐元素 alpha 仍生效）。`element.visible=false` 会被编译器跳过。
- **R5**：`transform.skew` 未实现，非零时 `FP_E_COMPILE_TRANSFORM` 报错（不静默忽略）；`fourier_gen` 闭合轮廓按 256 点采样，R≤512 时弦高误差 < 0.1 px；极大画布/更高精度可用 `samples` 参数提高。
- **R6**：`compile_plan(resolution=...)` 允许 16–4096（仅生成 `.fbs`），CLI 按本批约束把 `draw`/`plan2fbs` 卡在 ≤512；渲染 R>512 未在本批验收。
- **R7**：`fourier_prompt/__init__.py` 仍是 WP-A 的 0.1.0 文档（写着 plan2fbs 未实现），CLI `version` 因此仍输出 0.1.0；该文件在 WP-D 写边界外，建议 Lead 在合并时统一版本/文档。
- **R8**：`draw` 的 PNG 像素确定性依赖 skill 渲染器无随机；当前 `brush_jitter=0`，若后续开启 jitter 需复核像素 hash。
- **R9**：`write_fbs` 每次写入 `created` 墙钟，原始 `.fbs` 字节不保证相同；归一化 hash/PNG 像素 hash 才是本批确定性口径（符合 DoD）。
- **R10**：全部 CPU，单例最长约 3.8 s（R=256，51–73 条笔画）；R=512 的 13 条笔画示例 2.5 s。未在 2000 元素上限、超大分辨率下做性能验收。

## 9. 与并行包的接口

- 只读依赖 WP-B：`assemble_document(elements, *, cfg, warn, source, image_class, canvas, decomposition, base_color, contract=None, accelerators=None, regions=None, mode=..., fill_soft_px=None, method=None, fft_passes=None, record_ids=..., catch_brush_errors=False)`、`stroke_record(idx, el, fit, brush, cfg=None, *, bbox=None, layer=None, region_id=None)`、`StyleTable`；未调用私有 `_stroke_record`。
- 只读依赖 WP-A：`schema.validate_plan_detailed/load_plan/FDSchemaError`、`KIND_ALIASES` 语义；未改 `schema.py`。
- WP-E 可直接使用 `compile_plan()["fbs"]` 作为 `pack()` 的 `render.json` 来源；`out/wp-d/*.fbs` 可作为 `pack --fbs` 的现成输入。
