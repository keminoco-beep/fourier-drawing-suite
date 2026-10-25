# WP-F — fast 光栅后端 + `fd-render` CLI（任务 task-17）

- 日期：2026-10-20 ｜ 状态：**实现完成，parity 门禁未达字面阈值（已实测并如实记录）**
- 契约：KICKOFF_v3 §2 ｜ 设计依据：analysis/11 §3.5/§8.3

## 1. 交付

| 文件 | 内容 |
|---|---|
| `fourier_prompt/render_fast.py` | `fast_supported` / `render` / `render_with_info` / `render_auto` / `compare_images` / `bench` / `truncate_doc`（preview/frame 共用）；PIL 四边形带 @SS≥4 + 面积平均（BOX）下采样；不支持自动回退 skill exact 并记录 `fallback_reason` |
| `fourier_prompt/fdrender_cli.py` | `render / info / preview / frame / compare / bench`；`--fdraw` 走 `fdraw.unpack`，`--fbs` 直读；`--quality fast\|exact`、`--resolution`、`--fraction`、`--at`、`--json`；退出码 0/1/2/3 |
| `tests/render/` | 36 例（23 后端 + 13 CLI）、`fixtures/hand_rings.plan.json`、`fixtures/hand_waves.plan.json`、`parity_bench.py`（复现本报告数字） |
| `reports/WP-F.md` | 本文件 |

**实现边界**：只读 skill；`sample_stroke` / `eval_*` / `_output_size` / `_resolve_style` / `_fill_rgba` /
`_ink_runs` / `_parse_fill_holes` 全部复用 skill，未复制 FFT/文档组装/容器逻辑；未改 `cli.py`、`tools/`、
`fdraw.py`、docs/、analysis/。

关键语义：
- `width_scale`：显式参数 > `doc.render_contract.width_scale` > 1.0；fast 与 exact 参考渲染使用**同一个**解析值（避免双后端语义漂移）。
- 采样几何：`sample_stroke` 后按 layer → 同层 fill 先 → 文档顺序渲染，与 skill `_render_per_stroke` 一致。
- 光栅化：每笔按颜色/alpha 自适应块切分，块内画四边形带（左/右法向偏移，10 采样点为一 chunk 且 chunk 间重叠一段）→ `ImageDraw.polygon`；闭合/自交带用 chunk 分块避免 even-odd 打洞；端点补圆帽；亚像素细笔触对二值掩码做 0.8 SS-px 预滤波。
- 下采样用**面积平均（BOX）**而非设计稿里的 LANCZOS：exact 的 `FloatCanvas.to_uint8` 也是 SS×SS 盒式平均，BOX 与参考实现同口径、且无 LANCZOS 振铃，实测 parity 更好；这是与原型的唯一有意偏差。
- 内部分辨率：`supersample` 低于 4 时抬到 4（质量下限），8 封顶；超采样画布 >600 MB 时自动降档。R1024 冒烟 SS=4。
- 无随机；输出 `(H,W,3) uint8`，与 skill 相同的 `+0.5` 四舍五入。

## 2. shape 覆盖表（8 个验收文档）

| 文档 | shapes（笔画） | 走 fast？ | 说明 |
|---|---|---|---|
| `01_rose_medallion` | round, ink（+4 region_fill） | ✅ fast | 参考样例 |
| `02_star_badge` | ink, round（+3 fill） | ✅ fast | 深底亮线 |
| `03_golden_spiral` | round, ink（+1 fill） | ✅ fast | 最轻（7 元素） |
| `04_ocean_waves` | round, ink（+3 fill） | ✅ fast | |
| `05_aurora` | round, ink, **airbrush**(4), soft(7) | ⛔ 回退 exact | `fallback_reason`：`shape=airbrush（stroke id=56..59，需 exact）` |
| `06_cat_tophat` | round, ink, **pencil**(7) | ⛔ 回退 exact | `fallback_reason`：`shape=pencil（stroke id=36..42，需 exact）` |
| `hand_rings`（手写 plan） | round, ink（+2 fill） | ✅ fast | tests/render/fixtures |
| `hand_waves`（手写 plan） | round, ink（+1 fill） | ✅ fast | 512×320 画布 |

支持集合：`round / soft / ink / marker / watercolor / bristle`。`soft`（Gaussian 近似 smoothstep）、
`marker`（各向异性椭圆近似）、`watercolor/bristle`（**纹理核未复制**，仅基带）为近似实现，并在
`info["approximations"]` 记录；不在本批 8 个文档中出现。
不支持：`chalk / spatter / pencil / airbrush`、`mode=one_stroke`、`decomposition.method=adaptive_gradient_mesh`、
`color_field` 填充、文本层 —— 一律整篇回退 exact，**不静默出近似图**。

## 3. parity 表（R256，fast vs exact）

`parity` = `max|Δ|≤2 且 PSNR≥42 dB`；`within2` = `|Δ|≤2` 像素占比。

| 文档 | 质量 | max\|Δ\| | mean\|Δ\| | PSNR | within1 | within2 | 字面门禁 |
|---|---|---|---|---|---|---|---|
| 01_rose_medallion | fast | 50 | 2.652 | 31.24 dB | 79.77% | 81.09% | ❌ |
| 02_star_badge | fast | 56 | 1.436 | 33.90 dB | 88.60% | 89.85% | ❌ |
| 03_golden_spiral | fast | 40 | 0.497 | 39.73 dB | 94.86% | 95.45% | ❌ |
| 04_ocean_waves | fast | 44 | 0.519 | 39.81 dB | 94.02% | 94.97% | ❌ |
| 05_aurora | **exact 回退** | 0 | 0 | 99.0 dB | 100% | 100% | ✅（同图） |
| 06_cat_tophat | **exact 回退** | 0 | 0 | 99.0 dB | 100% | 100% | ✅（同图） |
| hand_rings | fast | 61 | 1.105 | 34.36 dB | 92.54% | 93.19% | ❌ |
| hand_waves | fast | 50 | 0.437 | 39.24 dB | 96.00% | 96.75% | ❌ |

R512 追加实测：`01` max 55 / PSNR 34.24 / within2 89.98；`03` max 47 / PSNR 42.81 / within2 97.58。
填充单独看（01，仅 4 个 fill）：max 44 / PSNR 46.72 / within1 98.84 —— **误差主因是墨迹边缘，不是填充**。

### 3.1 为什么字面门禁达不到（结论，不是掩饰）

`max|Δ|≤2` 要求逐像素几乎解析一致。skill exact 对**每个超采样像素**计算距离带覆盖
`cov=min(.5,d+r)−max(−.5,d−r)`（连续值）；fast 按契约用二值 `ImageDraw.polygon` + SS 下采样，覆盖只能取
`k/SS²` 的离散值（内部 SS=4 → 1/16 级）。本批 6 个样例的线宽只有 0.5–3 render px（01 多数为 0.5–0.75 px 全宽），
一条细线几乎全部像素都是“边缘像素”，离散覆盖误差 5–16/255 直接体现在这些像素上；因此
global max 必然 ~40–60，PSNR 31–40 dB。这是**方法本身的极限**，不是 bug，也无法通过继续调参跨到 max≤2。
exact 路径仍是唯一能过字面门禁的参考实现（回退文档 05/06 因此 100% 通过，且与 exact 逐像素相同）。

**建议（交 Lead 决策）**：
1. fast 作为默认交互/预览后端；parity 门禁改为 `PSNR≥30 dB 且 within2≥80%`（本批 8/8 支持文档均满足）
   + 粗笔触文档可再加 `PSNR≥42`（03/04 在 R512 已达 42.8）；`max|Δ|≤2` 仅用于 exact/回退文档；
2. 若必须逐像素门禁，需后续单独任务实现“边缘像素解析覆盖修正”（仅对掩码边界像素算距离，预计 +0.5–1.5 s），
   本批时间盒内不做。

## 4. 耗时 / 内存（本机 CPU，Python 3.13 / numpy 2.5.4 / Pillow 12.3）

| 场景 | fast | exact | 加速 | 备注 |
|---|---|---|---|---|
| R256 01（51 元素） | **0.73 s** | 3.50 s | 4.8× | |
| R256 02（73 元素） | 0.65 s | 1.59 s | 2.4× | |
| R256 03（7 元素） | 0.23 s | 0.71 s | 3.1× | |
| R256 04（13 元素） | 0.26 s | 0.86 s | 3.3× | |
| R256 05（回退） | 1.92 s | 1.83 s | — | 回退 = exact |
| R256 06（回退） | 3.97 s | 3.95 s | — | 回退 = exact |
| **R512 01（典型，目标 ≤3 s）** | **1.82–2.24 s（P50 1.91 s）** | 7.69 s | 4.0× | 首跑 2.24 s（冷缓存） |
| R512 03 | 0.60 s（P50 0.62 s） | 1.56 s | 2.5× | |
| R1024 03（冒烟） | **1.98 s** | 未跑 | — | 1024²，SS=4 |

内存（`tracemalloc`，含采样数组）：R512 01 峰值 **124.5 MB**；R512 03 124.6 MB；R256 约 **31.5 MB**。
R512 P50 1.91 s 满足 DoD“R512 典型 fast P50 ≤3 s”；R1024 仅冒烟（03，1.98 s），未做全量基准。
注意：上表 fast 耗时是**同进程热跑**；CLI 单次冷跑含 skill 首次 import/缓存，比热跑多 ~0.3–0.5 s
（R512 01 实测首跑 2.24 s，仍 ≤3 s）。

确定性：8/8 文档同输入两次 fast 渲染**逐像素相同**；CLI 两次 `render` 输出 PNG **文件字节相同**
（`test_render_is_deterministic_pixels`）。`--quality exact` 与 skill 直调逐像素相同。

preview/frame：`truncate_doc` 按渲染顺序（layer → fill 先 → 文档序）取前缀，不改原文档；
`fraction=0.3`（51 元素）→ 16 元素，`--at 5` → 5 元素；合成双笔画用例中前半图逐像素不变、后半图回到背景
（测试断言），截断稳定可复现。

## 5. 测试

```text
python -m unittest discover -s tests/render -v
Ran 36 tests in 14.2s
OK
```

覆盖：fast_supported 的 6 类判据、shape/dtype/background、确定性、width_scale 优先级（显式/contract/默认）、
fallback 与 exact 逐像素一致、compare 指标（同图 PASS/扰动 FAIL/尺寸错误）、bench 字段、
preview/frame 截断、CLI 全命令 + 缺文件退出码 1 + 参数缺失退出码 2 + 坏背景/坏分辨率退出码 1、6 样例 + 2 手写 plan 的
支持/回退集合与 parity 指标记录（断言 PSNR>25、within2>70 的回归下限，字面门禁结果见 §3 并明确写为已知限制）。

集成自检（WP-H 并联包，只读运行）：`python -m unittest discover -s tests/tools` → **33 tests OK**，
即 WP-H 的 `render` 工具通过 `render_fast.render_auto(..., return_info=True)` 调用本后端，`fallback_reason`/
`approximations` 字段被正确消费，输入文件不被修改。

## 6. 已知限制 / 风险

1. **parity 字面门禁未达成**（§3.1）：fast 对细笔触只能近似；建议改门禁或后续做边缘解析覆盖。不会静默当作通过。
2. `watercolor/bristle` 纹理核、`marker` 各向异性、`soft` smoothstep 为近似；真实文档若大量使用这些笔形，视觉差异比 round/ink 大。
3. `fill_soft` 用 PIL GaussianBlur（skill 用自研 gaussian_blur），填充边缘有 ~1% 像素级差异（PSNR 46.7）。
4. `color_field` 填充、文本、mesh、one_stroke 全部回退 exact（05/06 即为回退样例），回退耗时=exact。
5. R1024 只做 1 个冒烟；`--resolution` 上限沿用 skill 的 32768，但 fast 的内存上限会自动把 SS 降到 2（画质下降，未在报告中当达标）。
6. `compare_images` 的 `parity_pass` 严格按契约实现，未做任何容差/区域豁免。

## 7. 复现

```powershell
$PY = "<python>"
cd "<workspace>\fourier-prompt"
& $PY -m unittest discover -s tests/render -v
& $PY -B tests/render/parity_bench.py            # 输出 §3/§4 的原始 JSON
& $PY -m fourier_prompt.fdrender_cli render --fdraw out\verify\01.fdraw --out $env:TEMP\01_fast.png --resolution 512 --quality fast --json
& $PY -m fourier_prompt.fdrender_cli bench  --fdraw out\verify\01.fdraw --resolution 512 --repeats 2 --json
```
