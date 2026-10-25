# WP-C 报告 — brush-catalog（共享任务 task-14）

- 日期：2026-10-20 ｜ 工作包：WP-C brush-catalog ｜ 状态：**完成（可跑通 + 69 测试全绿）**
- 契约：`docs/impl/KICKOFF_v1.md` §2.3/§3/§4
- 设计依据：`analysis/13_笔刷目录与用笔策略设计.md`、`analysis/13a_笔刷实测与校准数据.md`
- 实测输入（只读）：`poc/brushcalib/out/brush_calib_measured.json`

## 1. 交付文件

```
fourier-prompt/
├── fourier_prompt/brush/
│   ├── __init__.py     # 公共导出 + BRUSH_API_VERSION；render_swatch 延迟导入
│   ├── catalog.py      # CATALOG_VERSION / BrushCatalog(load/get/list/validate)
│   ├── calib.py        # BrushCalib(load/response/width_eff/traits/sizes/anisotropy/validate)
│   ├── policy.py       # ROLES / ROLE_POOLS / ROLE_PRESSURE / ROLE_BIAS / choose_brush / pressure_for
│   ├── swatch.py       # render_swatch / render_row（自建 doc，不 import document.py）
│   └── _build.py       # 数据生成器：measured JSON → brush_catalog.json + brush_calib.json
├── assets/brush/
│   ├── brush_catalog.json    # 20 active + 11 planned（31 条，50 KB）
│   ├── brush_calib.json      # 20 预设 × sizes × pressure（48 KB）
│   ├── style_packs.json      # 水墨/铅笔/马克笔/油画/漫画/水彩
│   ├── LICENSES.md           # 来源/口径/生成命令/修改纪律
│   └── previews/             # swatch.png + 31 支笔的单行预览（planned 为占位卡）
└── tests/brush/              # test_catalog / test_calib / test_policy / test_swatch
```

未改动：`fourier-prompt/fourier_prompt/__init__.py`（WP-A 的文件，本批不存在）、skill 任何文件、docs/、analysis/。

## 2. 关键实现决定（含理由）

1. **id 不改名**：20 个 active 条目的 id 与 skill `brushlib.BRUSHES` 逐一对应（测试断言集合相等），
   `shape` 使用 skill 已注册的 10 个笔形内核名；`_build.py` 不 import skill（core 只依赖 stdlib，红线 7）。
2. **planned 条目 11 个**（4B/侧锋铅笔、颗粒粉笔、干马克、细鬃毛、湿颜料、水彩晕边、干鬃毛、填色圆头、
   压感喷枪、圆珠笔）：`status="planned"`、`renderable=false`、`calib="estimated"`、
   `implements_base` 指向继承数值的 active 笔；**policy 在 `pool_for` 里按 `is_renderable` 过滤**，
   并有测试（`test_pool_for_never_contains_planned` / `validate_policy`）钉住。
3. **`width_eff` 来自实测**：有尺寸扫描的 4 支用 `size_scaling` 中位数，其余用 size12 四点中位数；
   `width_eff_source` 记录来源。实测复现：soft=0.749、airbrush=0.333、ink_gpen=1.001、watercolor=0.999。
4. **pressure 曲线直接由实测生成**：`width` 是 p=1.0 归一化因子（实测 `1−sens·(1−p)`，R²≥0.9999），
   `alpha` 是实测峰值 α（`color.a` 归一到 1.0 的口径）。校验器强制：压力升序、width>0、0≤alpha≤1。
5. **`choose_brush` 打分（确定性、无随机）**：池序 base(0–6) + 角色实测偏好(±1.0×5 项) +
   显式 `traits`(±8) + 宽度适配(±6)。显式约束能压过池序，弱偏好不会；默认结果与设计表一致
   （bed=basic_round_hard、line_hairline=ink_fineliner、line_taper=ink_gpen、line_broad=sumi_e、
   sketch=pencil_hb、texture=chalk、effect=basic_airbrush、marker=marker_chisel）。
   `traits={"width_gain":"high"}` 时 line_taper 从 `ink_gpen`(W~) 切到 `sumi_e`(W+ 60%)——实测分组真的在起作用。
6. **fail-loud 全覆盖**：未知 preset → `UnknownPresetError(KeyError)`；未知 role/style → `UnknownRoleError`
   /`UnknownStyleError`（KeyError）；未知 trait 键/非法值 → `ValueError`；空候选池 → `LookupError`；
   分辨率 >512 → `ValueError`。校准表的 pressure 越界是**夹紧 + `pressure_clamped=True`**（不静默改写输入）。
7. **swatch 不用 document.py**：走 `io_format.new_document/finalize_document` + `fourier.fit_curve` +
   `brush.build_brush` + `render.render` 自建文档（与 `poc/brushcalib/calib_render.py` 同一条公共 API），
   有测试断言渲染后 `fbs_core.document` 不在 `sys.modules` 里。
   α 展示口径：把 `color.stops[].a` 归一到 1.0，只留 `style.alpha`（否则 α² 会让半透明笔整体偏淡一倍）。

## 3. 测试与结果

```powershell
$PY = "<python>"
cd "<workspace>\fourier-prompt"
& $PY -m unittest discover -s tests/brush -v     # -> Ran 69 tests, OK（exit 0, ~2.2s）
```

覆盖（69 例，> 契约要求的 20 例）：

| 文件 | 例数 | 重点 |
|---|---|---|
| `test_catalog.py` | 20 | 20+11 计数、id 集合、必需字段、按分区/角色过滤、未知 id/角色、planned 默认排除与 `renderable=false`、深拷贝、`validate()` 全绿 + 8 种破坏性用例（非法 category/缺字段/width_eff=0/压力曲线 1 点/未知角色/planned 自称可渲染/active 用未注册笔形） |
| `test_calib.py` | 17 | schema/来源/生成命令、`width_eff` 实测值（1.001/0.749/0.333/0.999）、`response(ink_gpen,12,1.0)=6.007/α1.0`、α 与 `alpha_lo+(hi−lo)p` 一致（±0.02）、宽度随压力单调、sumi_e 2.52×/fineliner 1.23×、尺寸线性（24≈2×12，±2%）、压力夹紧、未知 preset/非法 size、traits 与目录一致、anisotropy=1.6、`validate(catalog=...)` 覆盖一致性 + 破坏性用例 |
| `test_policy.py` | 21 | ROLES 与契约一致、10 角色都有可渲染选择、8 个默认选择对齐设计表、压力档（线稿 0.60–0.85 / 铺底 0.70–1.00 / 细节 0.20–0.70，提按跨度 > 均匀线）、未知 role 报错、池内 id 都 active 且声明 role、planned 不入池、`width_gain=high` 改选 sumi_e、未知 trait 报错、确定性、6 个风格包/中文别名（水墨→ink_wash）/未知 style、`validate_policy()` 全绿 + planned 混入池被检出 |
| `test_swatch.py` | 8 | 子集板尺寸/哈希/字节、两次渲染字节相同、预览图落盘且哈希一致、`render_row` 形状 (32,128,3)+两次一致、未知 preset/planned preset 被拒、分辨率上下界、不 import `fbs_core.document` |

## 4. 验收板（swatch）

- 路径：`assets/brush/previews/swatch.png`（1024×2304，263 391 bytes）
- sha256：`8a79dee164a5b80c552bf741849cc5e2e518a64941a94c9a1809ab49e8eca488`
- 版式：2 列 × 16 行；每支 active 笔一行三列 = ① 细线 size 12 压力 0.2→1.0→0.3（宽度+α 同时随压力）
  ② 宽笔波形 size 44 全压 ③ wash（3×size 24 叠加）；末 11 格是 planned 占位卡（灰底 + "planned"）
- 确定性：连续两次生成 sha256 相同；耗时 17.1s（纯 CPU，R=512）
- 单行预览：`previews/<id>.png` 共 31 个（20 渲染 + 11 占位），与目录 `preview` 字段一一对应
- 目评要点：`ink_gpen` 细线等宽（W−）、`sumi_e` 提按最强、`basic_airbrush` 宽笔/ wash 明显雾化、
  `watercolor_wash`/`marker_alcohol` 的 wash 列可见叠色、`chalk/charcoal` 边缘毛糙但内部平滑
  （与 13a §2.5「纹理只在边缘」一致）。

## 5. 未完成 / 风险 / 需要 Lead 决策

1. **未改 `fourier_prompt/__init__.py`**（WP-A 文件；写作时 WP-A 已建好该文件，`__all__` 里只有
   schema/repair/CLI，没有 brush）。当前 `from fourier_prompt.brush import ...` 可直接用；
   若要把 WP-C 的 API 提到顶层（如 `fourier_prompt.choose_brush`），请 WP-A 加一行
   `from .brush import BrushCatalog, BrushCalib, choose_brush, pressure_for, render_swatch`。
2. **planned 预览是占位卡**，不是渲染结果；`brush_catalog.json` 里它们的 `width_eff/pressure` 是
   `estimated`（继承 base），在真正实现内核后必须重新实测（`_build` 会覆盖）。
3. **α² 缺陷未修**：本批只在 swatch/校准口径上做归一化规避；渲染器默认路径 `build_color_curve` 仍把
   α 写进 `color.a`，真实出图仍偏淡。修它属于渲染/编译器侧（13a §2.5 建议），本批不动 skill。
4. **swatch 未做 golden hash 门禁**：本批只保证字节级确定性并把 sha256 写入报告/统计；CI 阈值与
   "改笔刷必须附 hash 说明"的流程留给后续（G2-B）。
5. **校准覆盖面**：尺寸扫描只做了 4 支笔（`basic_round_hard/ink_gpen/watercolor_wash/pencil_2b`），
   其余预设的尺寸响应用「名义宽线性」外推（实测斜率 0.999–1.002 支撑，测试断言 24px≈2×12px ±2%）。
   若要逐支尺寸实测，需扩展 `poc/brushcalib`（P2.8 后续）。
6. **策略权重是设计值**（池序 6 / 角色偏好 ±1 / 显式 traits ±8 / 宽度 ±6），经 8 个默认选择与
   1 个 traits 反例钉住；后续 A/B 若要调，改 `policy.ROLE_BIAS` 与权重即可，测试会立刻暴露回归。
