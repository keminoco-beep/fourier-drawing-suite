# fourier-brushstroke v2 — 内部接口契约（Lead 维护，改动需广播）

> 本文件是 5 个并行模块的唯一集成契约。任何实现不得私自偏离；接口有缺口先给 Lead 发消息。
> 数据格式的对外规范见 `references/fbs_format_spec.md`（同一份事实的两种写法：本文件偏 Python 接口，规范偏 JSON 契约）。

## 0. 产物与目录

```
fourier-brushstroke/
├── SKILL.md                     # 技能入口（docs 队友）
├── README.md                    # 安装/使用（docs 队友）
├── CONTRACT.md                  # 本文件（Lead）
├── references/                  # docs 队友 + Lead 的 format spec
├── scripts/
│   ├── fbs.py                   # 唯一 CLI 入口（Lead）
│   ├── run_acceptance.py        # 验收（T5 队友）
│   ├── check_hygiene.py         # 静态守卫（T5 队友）
│   └── fbs_core/
│       ├── __init__.py          # 导出公共 API（Lead）
│       ├── config.py            # FBSConfig（Lead）
│       ├── util.py              # 通用工具/科学计算回退（Lead）
│       ├── attribute.py         # AttributeCurve 求值（Lead）
│       ├── io_format.py         # .fbs 读写/校验/量化/指纹（Lead）
│       ├── document.py          # element → .fbs 文档组装（Lead）
│       ├── preprocess.py        # Stage0（T2 队友）
│       ├── coverage.py          # 调色板 + 覆盖率场 + 掩码（T2 队友）
│       ├── decompose.py         # Stage1 混合分解（T2 队友）
│       ├── fourier.py           # Stage2 傅里叶参数化（T3 队友）
│       ├── brush.py             # Stage3 笔刷属性曲线（T3 队友）
│       ├── render.py            # Stage5 渲染 + SVG（T4 队友）
│       ├── onestroke.py         # R2 一笔画合成（T4 队友）
│       ├── video.py             # R3 作画视频（T4 队友）
│       └── metrics.py           # Stage6 四维度量（T5 队友）
├── tests/                       # unittest（各自 owner）
└── assets/
    ├── testcases/               # 合成用例（T5）
    └── out/                     # 演示与验收产物
```

Python 运行时（本机生产环境，也是验收环境）：
`<python>`
（Python 3.13.14 / numpy 2.5.4 / Pillow 12.3.0 / scipy 1.18.1 / ImageIO 2.38.1 + imageio-ffmpeg 0.6.0。
**必需依赖只有 numpy + Pillow**；scipy/ImageIO 一律「运行时探测，有则加速/可用，无则回退且明确 warn」。禁止引入其他第三方依赖，禁止联网。）

导入约定：`scripts/fbs.py` 会把 `scripts/` 加入 sys.path，因此模块内一律
`from fbs_core import ...` 或 `from fbs_core.xxx import ...`，**不使用** `scripts.fbs_core...`。

## 1. 坐标系与世界单位（唯一权威，违反必跨模块错位）

- 图像像素索引 i 表示**像素中心**，像素 i 覆盖 `[i-0.5, i+0.5]`。
- 世界坐标：`world = pixel_center / long_side`，`long_side = max(W, H)`。
  因此长边 ∈ [0, 1]，短边 ∈ (0, 1]；左上角为世界原点，**y 轴向下**。
  源图像素中心的 world 坐标：`(i + 0.5) / long_side`。
- 渲染分辨率 R 指**长边像素数**。输出图第 i 个像素中心的 world 坐标：
  `(i + 0.5) / R`。R == long_side 时与源图逐像素对齐。
- 亚像素契约：`epsilon_px`（渲染像素）→ 世界单位 `thr_world = epsilon_px / R`。
- `.fbs` 中所有几何、width、tolerance、arc_length 用**世界单位**；`px` 字样的参数（如
  `min_branch_length_px`、`merge_distance_px`）在**源图像素**上度量（除以 long_side 进世界）。
- **width 存的是笔画全宽（直径），不是半径**；渲染半径 = width/2。

## 2. 曲线对象（AttributeCurve）

数值曲线（width / alpha）：
```jsonc
{"interp": "constant"|"linear"|"spline"|"step"|"fourier"|"preset",
 "keys": [[t, v], ...],          // constant/linear/spline/step；t 升序 ∈[0,1]
 "coef": [[k, re, im], ...],     // fourier
 "preset": "taper-out",          // preset
 "strength": 1.0, "scale": 0.01, // preset 参数：f(t)=scale*(1-strength+strength*g(t))
 "clamp": true}
```
颜色曲线（color）：
```jsonc
{"interp": "constant"|"linear"|"spline",
 "stops": [{"t": 0.0, "rgb": [12,34,56], "a": 1.0}, ...]}   // rgb 0..255 int，a 0..1
```
分类曲线（shape）：`{"interp": "step", "keys": [[t, "round"], ...]}`，取值 `round|soft|marker`。

求值 API（Lead 实现于 `attribute.py`，所有模块只能通过它求值，禁止各自重写插值）：
```python
eval_numeric(curve, t, default=None) -> np.ndarray        # t: (N,) float64
eval_color(curve, t) -> tuple[np.ndarray, np.ndarray]      # rgb (N,3) float 0..1, alpha (N,)
eval_shape(curve, t) -> list[str]
curve_is_constant(curve) -> bool
```
语义：`constant` 取 keys[0].v（允许无 keys 时用 default）；`linear` 分段线性；`spline` Catmull-Rom；
`step` 左闭右开区间保持；`fourier` 用 coef 求和（周期 1）；`preset` 按 g(t) 生成。
超出 keys 范围：clamp=True 时钳到端点值，否则按端点外推（默认 clamp=True）。

## 3. Stage1 输出：element（decompose.py 的公共数据结构）

```python
element = {
    "points":  np.ndarray (M,2) float64,   # 世界坐标；区域填充为闭合轮廓（首尾不重复）
    "s":       np.ndarray (M,)  float64,   # 沿本段归一化弧长 0..1 单调（drawable 参数）
    "closed":  bool,                       # True: 区域边界/闭合轮廓
    "fill_role": "medial_branch" | "region_fill",
    "widths":  np.ndarray (M,) float64,    # 全宽（世界单位）；region_fill 全 0
    "colors":  np.ndarray (M,3) float32,   # 0..1 RGB（线性 gamma 前，即 sRGB/255）
    "alphas":  np.ndarray (M,) float32,    # 0..1
    "shapes":  list[str] | None,           # 长度 M 或单值；None=自动
    "fill_poly": np.ndarray (K,2) | None,  # region_fill 的显式填充多边形（世界坐标）
    "fill_holes": list[np.ndarray] | None, # region_fill 的洞（世界坐标闭合环列表，无重复首尾；无洞 None/[]）
    "region_id": int,                      # -1 = 未归属
    "layer": int,                          # 绘制层序，小的先画（底层）
    "meta": {"max_halfwidth_px": float, "sprawl": float, "dt_peak_px": float, "source": str}
}
```
质量要求（由 T2 实现，T5 验收）：细线保留、尖角不过度圆化、填充边界亚像素外扩 0.5px、颜色代表色
在色度轴上取高分位（不得用简单中位数把腮红抹平）、退化点盘必须输出。
带洞区域（环/气泡/含高光的头发）：洞几何用 `fill_holes` 原样带出，渲染时用文档 `base_color` 挖掉，
不得退化成中轴分支（退化成笔触会丢填色）。

## 4. Stage2 输出：fourier.fit_curve

```python
fit_curve(points: np.ndarray (M,2), closed: bool, contract: dict, cfg: FBSConfig,
          warn: WarningLog, name: str = "") -> dict
# 返回：
{"coef": np.ndarray (K,3) float64,   # 每行 [k, re, im]，按 |k| 升序
 "N": int, "max_err": float,        # 世界单位，drawable 区间上的最大偏差
 "arc_length": float,               # drawable 弧长（世界单位）
 "closed": bool,
 "closure": {"kind": "none"} | {"kind": "hermite_c1", "t_end": float},
 "resampled": np.ndarray (M,2)}     # 均匀重采样后的闭环点（调试/测试用）

eval_coef(coef, t_closed) -> np.ndarray (N,2)
eval_curve(coef, closure, t_draw) -> np.ndarray (N,2)   # t_draw∈[0,1]；closed 时等价 t_closed=t_draw，
                                                        # 开曲线 t_closed=t_draw*closure.t_end
```
规则：
- `thr_world = contract["epsilon_px"] / contract["target_resolution"]`；取**满足契约的最小 N**，
  同时记录紧致性（max_err*R 不宜远低于 epsilon_px）。
- Nyquist 钳制：`N <= resample_points/2 - 2`；到顶仍不达标 → warn `W_N_CLIPPED`。
- 开曲线用 C1 Hermite 闭环（端点切向连续）；闭环段**不参与绘制**，只用于周期化。
- 禁止镜像延拓导致的 t 语义分裂；t 一律指 drawable 弧长归一化参数。
- 性能：1000+ 曲线 @R=1024 的 N 搜索总耗时 ≤ 60s。

## 5. Stage3 输出：brush.build_brush

```python
build_brush(element: dict, contract: dict, cfg: FBSConfig, warn: WarningLog)
  -> tuple[style: dict, brush: dict]
# style = {"color": <颜色曲线>, "alpha": <数值曲线>, "shape": <分类曲线>, "jitter": 0.0}
# brush = {"style": <int 占位，document.py 建表后回填>, "width": <数值曲线>}
```
另需提供（供测试与 document 组装）：
```python
select_shape(element, cfg) -> str
build_width_curve(s, widths, tol) -> dict
build_color_curve(s, rgb, alpha, tol) -> dict
build_alpha_curve(s, alpha, tol) -> dict
build_shape_curve(s, shapes) -> dict
```
要求：width/color/alpha 三条曲线相互独立；颜色合并不得平均墨色（会把墨色拉淡）；
常数属性必须压成 constant（体积纪律）；`curve_tol` 默认 0.02 是**相对容差**
（宽度按自身 max(width) 归一；alpha 按 0..1 绝对量纲；颜色按 8bit 色阶 ±4 合并 / >24 判硬跳变）。

## 6. 文档组装（document.py）与 .fbs 顶层

`build_from_image(image_path: str, cfg: FBSConfig) -> tuple[dict, dict]`：
执行 Stage0→4，返回 `(doc, info)`；`info` 含 `reference_rgb`（float32 H×W×3，源图 0..1）、
`long_side`、`warnings`、`stages` 计时。photo 分类直接抛 `FBSError("E_PHOTO_DEFERRED", ...)`。

`attach_quality(doc, metrics: dict) -> None`：把 `fbs_core.metrics.compare()` 的结果写进
`doc["quality"]`（至少 psnr/ssim/rmse/mae + 四维摘要），并更新 `doc["stats"]`。

`.fbs` 顶层（v2，完整字段见 format spec）：
```jsonc
{"format":"fourier-brushstroke","version":"2.0","mode":"per_stroke",
 "created": "...", "source": {...}, "image_class": {...},
 "canvas": {"width":..., "height":..., "long_side_units":1.0, "origin":"top-left","y_axis":"down"},
 "render_contract": {"target_resolution":1024,"epsilon_px":0.5},
 "decomposition": {"method":"hybrid_medial_contour","params":{...},"region_count":N},
 "base_color":[255,255,255,255], "regions":[...], "brush_styles":[...],
 "strokes":[...], "stats":{...}, "quality":{...}, "warnings":[...],
 "accelerators":{...}, "fingerprint":{...}}
```
stroke 字段：
```jsonc
{"id":0,"closed":false,"fill_role":"medial_branch","repr":"fourier",
 "N":21,"arc_length":0.96,"max_err":0.0004,"layer":3,"region_id":0,
 "bbox":[x0,y0,x1,y1],
 "coef":[[k,re,im],...],
 "closure":{"kind":"hermite_c1","t_end":0.63},
 "brush":{"style":7,"width":<数值曲线>},
 "poly":[[x,y],...],          // 仅 region_fill：外轮廓
 "fill_holes":[[[x,y],...], ...]}  // 仅 region_fill：可选洞（世界坐标闭合环，无重复首尾）
```
`brush_styles` 为共享表（精确去重，浮点全等才共享）；stroke.brush.style 必须合法。
精度纪律：小数字数由 `io_format.decimals_for_coef()` / `decimals_for_scalar()` 按契约反推，
禁止手写 6–9 位小数常量。写盘统一走 `io_format.write_fbs(doc, path)`（它负责量化+校验）。
`fingerprint(doc)` 记录 image_sha256/params_sha256/codegen="fbs-v2"；不同代/不同契约禁止比较。

## 7. 渲染接口（render.py）

```python
render(doc, resolution=None, supersample=None, mode="sweep",
       width_scale=None, background=None, warn=None) -> np.ndarray (H,W,3) uint8
render_to_image(doc, **kw) -> PIL.Image.Image
export_svg(doc, path, resolution=None) -> dict   # 返回 volume/notes
class FloatCanvas:                                # 渲染与视频共用的唯一基元
    def __init__(self, width_px:int, height_px:int, supersample:int, background_rgb)
    def add_band(self, points_px: np.ndarray (N,2) float64, radii_px: np.ndarray (N,),
                 colors: np.ndarray (N,3) float64 0..1, alphas: np.ndarray (N,),
                 mode: str = "sweep") -> None
    def fill_polygon(self, poly_px: np.ndarray (K,2), rgb: np.ndarray (3,) 0..1,
                     alpha: float = 1.0, soft_px: float = 0.0,
                     color_field: dict | None = None) -> None
    # 画布底色存在 self.background（0..1），带洞填充用它与文档 base_color 对齐
    def to_uint8(self) -> np.ndarray (H,W,3) uint8
```
- 中轴分支的渲染语义 = **圆盘并集（stamp）**；`sweep` = 路径 ⊕ 圆盘（Minkowski 和）连续扫掠。
- 亚像素精确带覆盖：`coverage = min(0.5, d+r) - max(-0.5, d-r)` 后钳位，**禁止** `clip(r+0.5-d)`。
- 半径（width）为 0 的样本/段必须**跳过**，不得设 `max(r, 0.3)` 之类的下限（会把抬笔画成横丝）。
- 采样密度必须 ∝ 路径长度（`n ≈ 4·arc_px`），禁止写死 200 点；上限/步数保护必须由数据导出，
  禁止 `guard < 20000` 之类写死常量。
- 渲染顺序：`layer` 升序，同层内按 doc 中 strokes 顺序；region_fill 在底层。
- **带洞区域填充**：region_fill 先填外轮廓（含 `region.color_field` 颜色场），再对
  `stroke.fill_holes` 的每个洞用文档 `base_color`（画布实际底色 `FloatCanvas.background`）
  以 `alpha=1.0`、同一 `fill_soft_px` 再填一次（洞内=背景色；洞环为世界坐标、无重复首尾）。
  缺失/空/非法洞环 = 无洞（旧文档行为不变），非法但非空的洞数据必须 warn `W_FILL_SKIPPED`。
- 分辨率无关：同一 doc 可渲染任意 R；R 变化只改变采样密度，不改变语义。
- 性能：R=1024, SS=2：chibi 1014 笔画 ≤180s，horn ≤120s；热路径禁止逐点 Python 循环。

一笔画（onestroke.py）：
```python
build_one_stroke(doc, cfg, warn, harmonics=None, max_fft=None) -> dict     # one_stroke 文档
build_path(one_doc, resolution:int, max_vertices:int|None=None) -> dict
# -> {"points_world": (V,2), "radii_world": (V,), "rgb": (V,3) float 0..1,
#     "alpha": (V,), "shapes": list[str], "arc_cum": (V,), "t": (V,), "fills": [...]}
```
要求：**恰好一次 FFT**（`doc["stats"]["fft_passes"] == 1`）；路径为单条闭合曲线；travel/填充段
width=0；`one_stroke_fills` 侧表每项 6 元素
`[t0, t1, [r,g,b,a], poly, color_field|None, holes|None]`（t 为合成路径参数）：
第 5 位是区域颜色场（dict，渲染端双线性采样；无则 None），第 6 位是洞列表
（`[[[x,y],...], ...]` 世界坐标闭合环；无洞 None）。渲染端必须兼容旧 4 元素
`[t0,t1,rgba,poly]` 与旧 5 元素（`rec[4]` 仅 dict 才当 color_field；`rec[5]` 仅 list/tuple
才当 holes）。带洞记录渲染时先铺外轮廓、再逐洞用 `base_color` 铺一次；路径偏差
`path_deviation_px` 用原逐笔画采样点复核，@R=1024 目标 ≤ 0.05px；一笔画最终渲染与逐笔画参考
的 PSNR 不再因覆盖损失显著低于逐笔画 PSNR（D1 修复目标，验收项见 report）。
`build_one_stroke` 写盘由 CLI 调 `io_format.write_fbs`。

**填充画法 / 作画顺序**（`FBSConfig.fill_mode` / `FBSConfig.path_order`，CLI：
`--fill-mode polygon|brush`、`--order layer|lines_first|spatial|reading`）：
- `fill_mode="polygon"`（默认）：填充 = 显式多边形 + `one_stroke_fills` 侧表（按 t 整块铺开）；
- `fill_mode="brush"`：填充 = 宽笔水平蛇形扫描线段（width>0，`one_stroke_fills` 为空），
  视频里颜色随笔画一行行出现；线宽/行距由区域 bbox 尺寸导出，扫描用"轮廓栅格化+填洞"保证不漏，
  带洞区域在实心掩码里挖掉洞环（洞内不落填充笔）；
- `path_order="lines_first"`：先画全部中轴线条、最后铺填充（最像人手绘，过程视频推荐）；
  `"layer"`=先铺色后勾线；`"spatial"`=按区域逐块画；`"reading"`=从左到右（logo/文字推荐）。

视频（video.py）：
```python
make_video(one_doc, out_path, duration=60.0, fps=30, size=None,
           circles=600, mode="sweep", warn=None, smooth=False) -> dict
# -> {"out","frames","fps","duration_s","size":[w,h],"circles","bytes",
#     "timeline":"arc"|"uniform"}
probe_video(path) -> dict   # {"frames","fps","duration_s","size":[w,h]}
```
- 旋转圆作画（top-|c| 系数画圆链；笔尖位置必须用全系数 ifft 结果，不能逐帧遍历系数）。
  **圆链用绝对坐标绘制**（partial 前缀和即 z(u) 的截断），禁止"整链平移到笔尖"的残差平移——
  否则被截断的尾部系数和逐帧剧变会让整条圆链无规律抖动；残差只画成链尾到笔尖的细连线。
  视觉圆的最大谐波 `_CIRCLE_MAX_K` 有上限（更高次圆在帧间旋转角过大，只保留链线）。
- 时间轴两档：
  - 默认严格档 `timeline="arc"`：按 `step_spans` 分配帧，**不省略作画步骤**（每步至少
    `min_frames_per_step` 帧；帧数 < 步骤数时自动提高 fps 并 warn）；
  - `smooth=True` 平滑档 `timeline="uniform"`：固定 `duration×fps` 帧，t 在时间上匀速推进；
    适合笔画数上千的复杂图（一帧可覆盖多个微步骤，所有墨迹仍会画出）。
  逐帧增量绘制；填充在路径走到其 t 区间时铺开；带洞填充的洞用背景色**以同一进度**
  同步铺开（最终帧洞=底色，中间过渡允许近似）。
- 60s ±5%；失败时抛 FBSError，不得静默产出空视频。

## 8. 度量接口（metrics.py）

```python
compare(render_rgb: np.ndarray, reference_rgb: np.ndarray, *,
        doc: dict | None = None, bg=None, thickness_bins=None) -> dict
# 必需键：resolution, psnr, ssim, rmse, mae,
#   structure {recall, precision, iou, missing_pct, extra_pct},
#   thickness {hist_ref, hist_rec, ...},
#   tone {mean_delta_e, confusion, ...},
#   linewidth {ref_median_px, rec_median_px, ratio},
#   contract {target_resolution, epsilon_px, fingerprint}
psnr/ssim/rmse/mae 独立函数；fingerprint(doc)/same_generation(a,b)
```
- **同分辨率强制**：两边尺寸不同直接抛 FBSError("E_METRIC_RESOLUTION")，禁止上采样再比。
- 墨迹掩码阈值默认 0.5（不能用 0.10 这种过宽阈值）。
- 厚度谱按连通域 EDT 峰值分箱（不是面积分箱）。
- 结构指标分不清"灰/黑"，必须同时给色调维度。

## 9. 错误 / 警告 / 降级（全链路统一）

```python
class FBSError(Exception):
    def __init__(self, code: str, message: str, *, evidence: dict | None = None,
                 suggestions: list[str] | None = None)
class WarningLog:
    def add(self, code: str, message: str, severity: str = "warn") -> None
    def extend(self, other) -> None
    def to_list(self) -> list[dict]        # [{"code","message","severity"}]
```
- 所有降级/超限/精度未达标必须进 WarningLog，禁止只 print；禁止裸 except 吞错。
- 错误码清单（实现级全集，新增码必须同步本节）：
  `E_CONFIG` / `E_CURVE` / `E_INPUT_MISSING` / `E_INPUT_FORMAT` / `E_INPUT_CORRUPT` /
  `E_INPUT_MODE` / `E_INPUT_TOO_SMALL` / `E_INPUT_TRANSPARENT` / `E_PHOTO_DEFERRED` /
  `E_FBS_INVALID` / `E_METRIC_INPUT` / `E_METRIC_RESOLUTION` / `E_RENDER_RESOLUTION` /
  `E_RENDER_MODE` / `E_RENDER_GEOM` / `E_SVG_INVALID` / `E_VIDEO_BACKEND` /
  `E_VIDEO_DURATION` / `E_VIDEO_ENCODE` / `E_ACCEPTANCE_ITEMS` / `E_FIXTURES`。
- 警告码（实现级全集）：
  `W_DOWNSCALED` / `W_MULTIFRAME` / `W_PRUNED` / `W_SHORT_BRANCHES` / `W_MAX_CURVES` /
  `W_SOLID_COLOR` / `W_SMALL_REGIONS` / `W_GAP_BRIDGED` / `W_HOLE_REGION` / `W_N_CLIPPED` / `W_COLOR_FIELD_OFF` /
  `W_FORCE_CLASS` / `W_GEOMETRY_SKIP` / `W_BRUSH_STYLE` / `W_BRUSH_COLOR_SHAPE` / `W_GRADIENT_DEGRADED` /
  `W_BRUSH_LENGTH_MISMATCH` / `W_FILL_SKIPPED` / `W_ONE_STROKE_TRAVEL` / `W_ONE_STROKE_N` /
  `W_VIDEO_FPS_RAISED` / `W_VIDEO_STEPS` / `W_SVG_APPROX` / `W_UNKNOWN`。

## 10. CLI（唯一入口 scripts/fbs.py）

```bash
PY="<python>"
cd fourier-brushstroke/scripts

# 1) 位图 → .fbs + 重建 PNG + SVG + report.json
"$PY" fbs.py build INPUT.png -o OUTDIR --resolution 1024 --mode sweep
# 2) 逐笔画 .fbs → 一笔画 .fbs（单次 FFT）
"$PY" fbs.py onestroke --fbs OUTDIR/stem.fbs.json --harmonics 65536 --max-fft 1048576
# 3) 一笔画 .fbs → 60s 作画视频
"$PY" fbs.py video --fbs OUTDIR/stem.onestroke.fbs.json --out OUTDIR/stem_1min.mp4 \
    --duration 60 --fps 30 --circles 600 --mode sweep
# 4) 任意分辨率渲染
"$PY" fbs.py render --fbs OUTDIR/stem.fbs.json --out OUTDIR/render_R2048.png --resolution 2048
# 5) 度量（同分辨率）
"$PY" fbs.py eval --fbs OUTDIR/stem.fbs.json --render OUTDIR/stem_R1024.png --reference INPUT.png
# 6) 验收
"$PY" fbs.py acceptance --quick
```
CLI 参数默认值一律 `None`，只在显式给出时覆盖 `FBSConfig`（防止 CLI 默认值无声覆盖 config）。
输出命名：`<stem>.fbs.json`、`<stem>_R{R}.png`、`<stem>.svg`、`<stem>.report.json`、
`<stem>.onestroke.fbs.json`、`<stem>_1min.mp4`。

## 11. 工程纪律（违反会被验收/静态守卫抓）

1. 不静默失败：异常必须带 code/message；降级必须进 WarningLog。
2. 不写死数据相关上限（步数、采样点数、循环保护）；由路径长度/像素数/数据导出。
3. 热路径向量化；禁止逐点 Python 循环处理几十万像素。
4. 精度由契约反推（见 §6），不凭手感。
5. 格式变更加字段（additive），不改既有字段语义；v2 **只读 v2**，旧版 v1 `.fbs` 明确拒绝
   （`E_FBS_INVALID`，不提供隐式兼容；旧产物请用新版流水线重新生成）。
6. 确定性：固定 seed、无字典序依赖、无时间戳参与数值。
7. 所有公开函数有 type hints + 一行 docstring；模块顶部写职责与关键决策原因。
8. 测试用 stdlib `unittest`（本环境无 pytest）；不联网；单测总时长 ≤ 5 min。
9. 中文注释/文档，UTF-8 无 BOM。
10. 只改自己 write scope 内的文件；跨模块需求 → 给 Lead 发消息，不自行改别人的模块。

## 12. 性能预算（本机实测基线，超出需报 Lead）

| 阶段 | chibi 421×412 @R1024 | horn 505×712 @R1024 |
|---|---|---|
| Stage0+1（预处理+分解） | ≤120s | ≤120s |
| Stage2+3（傅里叶+笔刷） | ≤60s | ≤60s |
| Stage5 静态渲染 SS=2 | ≤180s | ≤120s |
| 一笔画构建（65536 谐波） | ≤180s | ≤120s |
| 60s 视频（1800 帧 720×1080） | ≤300s | ≤300s |
| 全量验收 `--quick` | ≤600s | — |
