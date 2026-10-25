# `.fbs` 格式规范 v2.0

`.fbs`（Fourier BrushStroke）是「位图 → 傅里叶笔触 → 位图/视频」往返复刻的**自描述中间表示**。
v2 是从头重建的干净格式：一份几何（复平面傅里叶系数）+ 三条独立笔刷属性曲线
（宽度 / 颜色 / 透明度）+ 分类笔形曲线 + 显式填充多边形 + 契约与指纹。

文件扩展名 `.fbs.json`，UTF-8，可直接阅读与手工微调。

---

## 1. 坐标系（唯一权威）

- 画布左上角为原点，**y 轴向下**。
- 世界坐标 = 像素中心坐标 / `long_side`，`long_side = max(源图宽, 源图高)`。
  因此长边恒为 `1.0`，短边 ∈ (0, 1]；像素 i 的中心世界坐标为 `(i + 0.5) / long_side`。
- `canvas.width/height` = 源图宽/高 / long_side。
- **`target_resolution` R = 长边渲染像素数**，所以 1 世界单位 = R 像素。
- 亚像素契约：世界单位阈值 `thr = epsilon_px / R`。
- `width` 字段存的是**全宽（直径）**，渲染半径 = width / 2。
- `t ∈ [0,1]` 是**弧长归一化**参数（drawable 参数），不是时间。

## 2. 顶层结构

```jsonc
{
  "format": "fourier-brushstroke",
  "version": "2.0",
  "mode": "per_stroke",                  // per_stroke | one_stroke
  "created": "2026-10-18T12:00:00+08:00",
  "source":   {"path": "in.png", "sha256": "...", "width": 421, "height": 412,
               "mode": "RGBA", "scale_applied": 1.0},
  "image_class": {"klass": "illustration",
                  "evidence": {"unique_colors": 40924, "entropy_bits": 7.1,
                               "edge_density": 0.21, "mean_saturation": 0.31,
                               "noise_level": 2.2}},
  "canvas": {"width": 1.0, "height": 0.9786, "long_side_units": 1.0,
             "origin": "top-left", "y_axis": "down"},
  "render_contract": {"target_resolution": 1024, "epsilon_px": 0.5},
  "decomposition": {"method": "hybrid_medial_contour",
                    "params": { /* FBSConfig 全量，可复现 */ },
                    "region_count": 42},
  "base_color": [255, 255, 255, 255],
  "regions": [ {"id": 0, "rgb": [230, 180, 170], "area_px": 8123,
                "bbox": [0.1, 0.2, 0.4, 0.5],
                "color_field": {                  // 可选：区域内部颜色场（渐变/纹理）
                  "grid": 20, "gw": 20, "gh": 14,
                  "bbox": [0.1, 0.2, 0.4, 0.5],   // 世界坐标，与 poly 同一空间
                  "colors": [[230,180,170], ...], // 行主序扁平 gw*gh 个 int RGB
                  "valid": 273                    // 有效单元数；无效单元已用代表色回填
                } } ],
  "brush_styles": [ /* 见 §4，共享表，精确去重 */ ],
  "strokes": [ /* 见 §5 */ ],
  "stats": {"stroke_count": 1014, "coef_count": 52341, "fill_count": 101,
            "region_count": 42, "fft_passes": 0},
  "quality": {"psnr": 20.97, "ssim": 0.83, "rmse": ...,
              "structure": {...}, "tone": {...}, "thickness": {...}, "linewidth": {...}},
  "warnings": [{"code": "W_PRUNED", "message": "...", "severity": "warn"}],
  "accelerators": {"edt": "scipy", "gaussian": "scipy", "label": "scipy"},
  "fingerprint": {"image_sha256": "...", "params_sha256": "...",
                  "codegen": "fbs-v2", "contract_sha256": "..."}
}
```

## 3. stroke 条目

```jsonc
{
  "id": 0,
  "closed": false,
  "fill_role": "medial_branch",        // medial_branch | region_fill
  "repr": "fourier",
  "N": 21,                             // 保留谐波数；coef 中 max|k| == N
  "arc_length": 0.9626,                // drawable 弧长（世界单位）
  "max_err": 0.00021,                  // drawable 区间上最大重建偏差（世界单位）
  "layer": 3,                          // 绘制层序：小的先画（底层）
  "region_id": 0,
  "bbox": [x0, y0, x1, y1],
  "coef": [[-21, 0.0, -0.000123], ..., [0, 0.4212, 0.1333], ..., [21, 0.0, 0.00045]],
  "closure": {"kind": "hermite_c1", "t_end": 0.63},
  "brush": {"style": 7, "width": { /* AttributeCurve，见 §4 */ }},
  "poly": [[x, y], ...],               // 仅 region_fill：显式外轮廓多边形
  "fill_holes": [[[x, y], ...], ...]   // 仅 region_fill：可选洞（世界坐标闭合环，无重复首尾；缺失=无洞）
}
```

规则：
- `coef` 按 `|k|` 升序，`k` 为整数，含义 `z(t) = Σ c_k·e^{i2πkt}`，`c_k = re + i·im`。
- `fill_holes`（可选，仅 region_fill）：洞列表，每个洞是与 `poly` 同一坐标系（世界单位）的
  闭合环、不含重复首尾点；渲染时在外轮廓之后用文档 `base_color` 挖掉（环/气泡/高光）。
  缺失、空列表、非法环均按"无洞"处理（旧文档语义不变）。
- 闭合曲线（`closed=true`）：`closure.kind="none"`，`t` 直接是闭环参数，`t∈[0,1)`。
- 开曲线（`closed=false`）：先做 **C1 Hermite 闭环**（端点切向连续）再周期 FFT；
  drawable 区间是闭环参数的 `[0, t_end)`。渲染时按 `t_closed = t_draw · t_end` 映射，
  笔刷属性曲线仍用 `t_draw∈[0,1]` 求值。闭环段**不绘制**，只负责周期化。
- `max_err · R ≤ epsilon_px` 是该笔画的渲染契约；`N` 取满足契约的最小值（Nyquist 钳制：
  `N ≤ resample_points/2 - 2`，到顶不达标记 `W_N_CLIPPED`）。

## 4. brush_styles 与 AttributeCurve

`brush_styles` 是共享表：内容完全相同（浮点全等）的样式只存一份，`stroke.brush.style` 为索引。
一条样式含三条**相互独立**的属性曲线：

```jsonc
{
  "color": {"interp": "linear",
            "stops": [{"t": 0.0, "rgb": [12, 34, 56], "a": 1.0},
                      {"t": 1.0, "rgb": [120, 90, 60], "a": 0.6}]},
  "alpha": {"interp": "step", "keys": [[0.0, 1.0], [0.85, 0.4]], "clamp": true},
  "shape": {"interp": "step", "keys": [[0.0, "round"], [0.7, "marker"]]},
  "jitter": 0.0
}
```

数值曲线（width / alpha）：
```jsonc
{"interp": "constant"|"linear"|"spline"|"step"|"fourier"|"preset",
 "keys": [[t, v], ...], "coef": [[k, re, im], ...],
 "preset": "taper-out", "strength": 1.0, "scale": 0.0148, "clamp": true}
```

| interp | 语义 | 典型用途 |
|---|---|---|
| `constant` | 全程恒定 | 均匀笔画（体积纪律：常量必须压成 constant） |
| `linear` | 关键点分段线性 | 从粗到细（直边锥形） |
| `spline` | Catmull-Rom 三次样条 | 平滑自然的粗细过渡 |
| `step` | 左闭右开区间保持 | **显式分段区间语义** |
| `fourier` | 傅里叶级数（周期 1） | 平滑周期变化 |
| `preset` | 命名预设 + 强度 | `taper-in/taper-out/taper-both/ease-in/ease-out/sigmoid` |

preset 统一形式：`f(t) = scale · (1 − strength + strength · g(t))`。

分类曲线（shape）：取值 `round`（圆盘）/ `soft`（柔边圆盘）/ `marker`（扁平笔头胶囊）。
v2 允许 shape 沿 t 变化（step），因此**一笔画通道不再受"一个笔形"限制**（旧版 D4）。

硬性约束：
1. width / color / alpha 三条曲线相互独立，可任意组合。
2. 区间语义必须用 `step` 显式表达，不得用常量近似代替。
3. 所有属性按**弧长归一化参数 t** 求值，与几何严格同步。
4. 属性曲线的 interp 可与几何表示不同。

## 5. 渲染语义（正确性要求，不是风格选项）

- **中轴分支必须按圆盘并集（stamp）语义重建**：`形状 = ∪ disc(c(t), r(t))`。
  这是中轴变换（MAT）的逆运算，宽度的定义就是 `2×DT`。
- `sweep` 模式 = 路径 ⊕ 圆盘（Minkowski 和），逐帧连续扫掠，画出**真实的线**（R5），
  不是沿路径盖点。宽度与颜色可沿 t 平滑渐变（R6）。
- 亚像素精确带覆盖：`coverage = min(0.5, d+r) − max(−0.5, d−r)`（钳位 0..1），
  禁止 `clip(r+0.5−d)`（r≪1 时会把细线画胖近一倍）。
- 半径 0 的样本（抬笔 travel / 区域填充段）**必须跳过**，不得设 `max(r,0.3)` 下限。
- 采样密度必须 ∝ 路径长度（约 `4·arc_px`），不得写死采样点数。
- 绘制顺序：`layer` 升序；region_fill 在底层，细笔画压在上层。
- **带洞区域填充**：region_fill 先按常规填外轮廓（含颜色场），再对每个 `fill_holes` 洞用
  文档 `base_color`（画布实际底色）以同一软边 `fill_soft_px` 填一次 → 洞内显示背景色。
  一笔画通道：洞环随 `one_stroke_fills` 第 6 元素透传，铺填充时同步用底色挖洞。
- **区域颜色场**：region_fill 的 `poly` 内若 `region.color_field` 存在，渲染时按单元**双线性采样**
  颜色（`bbox` 世界坐标 → 渲染像素；`gw/gh` 可不同；行主序 `y*gw+x`），再乘多边形软边覆盖。
  无效单元（区域内不存在的网格）已由生成方用区域代表色回填，渲染方不得自行外推。
  关闭该特性（`--color-field-grid 0`）时区域内为区域代表色（平坦填充）。

## 6. 一笔画文件（`mode="one_stroke"`）

由逐笔画 `.fbs` 合成，仍然沿用 v2 顶层结构，附加：

```jsonc
{
  "mode": "one_stroke",
  "source_fbs": {"path": "...", "sha256": "...", "stroke_count": 1014},
  "strokes": [ {                         // 恰好一条
      "id": 0, "closed": true, "fill_role": "medial_branch", "repr": "fourier",
      "N": 65534, "arc_length": 12.83, "max_err": 2.1e-6,
      "path_deviation_px": 0.029,        // 与原逐笔画采样点的最大偏差 @R
      "travel_length": 1.42,
      "closure": {"kind": "none"},
      "coef": [ ... ],                   // 单个 FFT 的全部系数
      "brush": {"style": 0, "width": { /* step 曲线：逐段宽度，抬笔为 0 */ }},
      "step_spans": [[t0, t1, "stroke"], [t1, t2, "travel"], ...]   // 作画步骤序列
  }],
  "brush_styles": [ {"color": {...}, "alpha": {...}, "shape": {...}} ],
  "one_stroke_fills": [
      // 新格式 6 元素： [t0, t1, [r,g,b,a], [[x,y],...], color_field|None, holes|None]
      [t0, t1, [r, g, b, a], [[x, y], ...],
       {"grid": 20, "gw": 20, "gh": 14, "bbox": [...], "colors": [...], "valid": 273},  // 可选颜色场
       [[[x, y], ...], ...]]   // 可选洞（世界坐标闭合环；无洞写 null）
  ],
  "stats": {"fft_passes": 1, ...}
}
```

语义：
- **单次 FFT**：整条合并路径只做一次傅里叶变换（`stats.fft_passes == 1`，R2）。
- 抬笔（travel）与区域填充区间宽度为 0，渲染不落墨；填充在路径走到其区间时铺开。
- `one_stroke_fills` 是**显式多边形**（不是有损重建轮廓），保证填充边界正确；每项 6 元素
  `[t0,t1,[r,g,b,a],poly,color_field|None,holes|None]`。渲染端必须兼容旧 4 元素与旧 5 元素
  （`rec[4]` 仅 dict 才算 color_field，`rec[5]` 仅 list/tuple 才算 holes）。带洞记录在铺填充时
  先填外轮廓、再逐洞用 `base_color` 以同一进度（视频）/ 一次（静态）铺开。
- **两种填充画法**（构建时由 `FBSConfig.fill_mode` 决定）：
  - `polygon`（默认）：填充区作为一条 width=0 的轮廓进入路径，面积填充记录在
    `one_stroke_fills` 侧表里，按 t 区间整块铺开；
  - `brush`：填充区改写成**宽笔扫掠线段**（每个 `step_spans` 条目 kind="fill" 但 width>0，
    水平蛇形扫描、线宽/行距由区域尺寸导出），`one_stroke_fills` 为空——视频里颜色会被笔
    一行行刷出来，而不是整块淡入；带洞区域的洞在扫描掩码里被挖掉（洞内不落填充笔）。
- **作画顺序**（`FBSConfig.path_order`）：`layer`（填充先铺/线条后画）、`lines_first`
  （先线稿后铺色，最像人手绘）、`spatial`（按区域逐块画）、`reading`（从左到右，
  logo/文字/签名推荐）。顺序只影响路径拼接与
  `step_spans`，不改变表示语义。
- 时间轴按 t/弧长等比压缩；视频必须在有限帧数内覆盖全部 `step_spans`（R3，不省略步骤）。

## 7. 精度与体积纪律

- 系数小数位由契约反推：`nd = ceil(log10(S·R/epsilon_px))`，`S = 2·(2N+1)` 为实分量个数
  （上界保守；实现见 `fbs_core/io_format.py:decimals_for_coef`）。
- 标量小数位：量化步长 `epsilon_px·0.5/R`。
- 共享笔刷样式建表去重；常量属性压成 `constant`；默认值不逐笔重发。
- `.fbs` 体积相对 PNG 的比值**必须同时给出 R 与分子分母绝对值**才有意义
  （比值随 R 漂移，旧版曾因此误判"表示效率"）。

## 8. 版本兼容

- v2 只保证读 v2。旧版 v1 `.fbs` **明确拒绝**（`E_FBS_INVALID`），需用新版流水线重新生成。
- v2 后续版本只允许 **additive** 加字段；既有字段语义不得改变。未知字段读取方必须忽略而非报错。
