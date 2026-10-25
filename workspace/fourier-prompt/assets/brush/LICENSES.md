# assets/brush 数据许可与来源（LICENSES / provenance）

本目录**全部数据由本仓库自测/自生成**，不含任何第三方笔刷资产、纹理或 bitmap tip。

- 产品许可：**Proprietary, All Rights Reserved**（见根 `LICENSE`，决策 D17）。
  目录条目的 `license` 字段统一写 `"proprietary"`；数据不对外单独授权。
- 第三方声明：见根 `THIRD_PARTY_NOTICES.md`（本目录无新增第三方内容）。

## 1. 文件清单

| 文件 | 内容 | 生成方式 | 可复现 |
|---|---|---|---|
| `brush_catalog.json` | 20 个 active 预设（id 与 skill `brushlib` 完全一致）+ 11 个 `status="planned"` 起草条目；元数据/角色/有效宽/压力曲线/层建议/预览路径 | `python -m fourier_prompt.brush._build`（见 §2） | 是（同输入同字节） |
| `brush_calib.json` | preset → sizes → pressure → `width_px`/`alpha`/`width_eff`/`traits` 实测响应表 | 同上（从 `poc/brushcalib/out/brush_calib_measured.json` 转换） | 是 |
| `style_packs.json` | 6 个画风包（水墨/铅笔/马克笔/油画/漫画/水彩）的角色池 + 属性倾向 | 手工编辑（编辑口径数据，非生成物） | — |
| `previews/swatch.png` | 验收板：20 支 active 笔 ×（细线压力渐变/宽笔波形/wash 叠加）+ 11 张 planned 占位卡 | `python -m fourier_prompt.brush.swatch --resolution 512 --include-planned` | 是（PNG 字节级确定性） |
| `previews/<preset_id>.png` | 每支笔的单行预览（planned 为占位卡） | 同上（`previews=True`） | 是 |

## 2. 生成命令（在 `fourier-prompt/` 下执行）

```powershell
$PY = "<python>"

# 目录 + 校准表（输入是只读的 POC 实测数据）
& $PY -m fourier_prompt.brush._build `
    --measured "..\poc\brushcalib\out\brush_calib_measured.json" `
    --assets "assets\brush"

# 验收板（R<=512；约 17s，纯 CPU）
& $PY -m fourier_prompt.brush.swatch --resolution 512 --include-planned
```

生成物里的 `generated_from_sha256` / `generated` 记录上游实测文件哈希与日期；
`generated_by` 记录上面两条命令，保证"数据从哪来"可追溯。

## 3. 上游事实与口径

- **预设参数**（`name/category/shape/size_mult/alpha/pressure/jitter/length_mult`）：
  来自本仓库 skill `fourier-brushstroke/scripts/fbs_core/brushlib.py`，经
  `poc/brushcalib/out/brush_calib_measured.json` 转存（`_build` 不 import skill，避免运行期依赖）。
  **20 个 id 一个都没改名**（兼容既有 `.fbs` 与黄金回归）。
- **实测响应**：由本仓库自己的渲染器实测（`analysis/13a_笔刷实测与校准数据.md`）：
  320px 画布、supersample=2、白底 `#FFFFFF`、墨色 `#141416`、sweep 语义；
  尺寸 4/8/16/32/64/120、压力 0.25/0.5/0.75/1.0。测量窗口为弧长 25%–70% 平台段。
- **α 口径**：所有 `alpha` 都是把 `style.color.stops[].a` 归一到 1.0 后的峰值 α。
  默认 `build_brush` 会把 α 同时写进 `color.a` 与 `style.alpha`，渲染相乘 → 实测≈请求²（α² 缺陷，
  见 13a §2.5 与 `brush_calib.json` 的 `alpha_ladder` 证据）。swatch 用同一口径（`alpha_normalized`）。
- **planned 条目**：没有任何渲染实现，`renderable=false`、`calib="estimated"`（数值继承 `implements_base`），
  `policy` 永不选中；`previews/<planned>.png` 是占位卡，不是渲染结果。
- **没有使用** Daub / DaubPlanner / Krita `.kpp` 的任何代码或资产；分类与"目录+校准+策略+swatch"
  的方法论参考其公开文档，实现与数据独立完成。

## 4. 修改纪律

- 改 `_build.py` 的编辑口径（角色/不透明度/层/尺寸范围）→ 重新生成两个 JSON，并说明 `brush_catalog.json` 的 diff；
- 改渲染器或笔形内核 → 必须重生成 swatch 并在报告/CI 里记录 `swatch.png` 的 sha256 变化原因（不静默变更）；
- 新增预设：`brushlib` 加 id（20 个老 id 不动）→ `_build.py` 的 `PRESET_META` 补一条 → 重生成 → swatch 目评。
