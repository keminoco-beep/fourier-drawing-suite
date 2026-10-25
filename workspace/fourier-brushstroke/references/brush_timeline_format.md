# 画笔属性时间轴格式（fbs-brush-timeline/1）

产品定义：整个绘画过程 = **一串傅里叶函数（笔触路径）+ 一串随时间 t 定义的画笔属性**。
`.fbs` 存几何与逐笔属性曲线；`timeline` 把它们按时间展开成可直接读/编辑的时间轴文件。

## 顶层

| 字段 | 类型 | 含义 |
|---|---|---|
| `schema` | str | `fbs-brush-timeline/1` |
| `duration_s` | float | 总时长（秒） |
| `stroke_count` | int | 笔触数 |
| `overlap` | float | 相邻笔触时间重叠系数（>1 表示同时落笔/湿画法） |
| `brush_model` | dict | 表示说明（fourier_stroke；属性 width/color/alpha/shape；相位说明） |
| `styles` | list | 画笔样式表：`{shape,color,alpha}` 属性曲线；笔触按索引引用 |
| `strokes` | list | 逐笔时间轴记录 |

## 逐笔记录

```json
{
  "id": 0, "order": 0,
  "t0": 0.0, "t1": 0.92, "duration_s": 0.92,
  "start": 0.0, "end": 0.0153,
  "fourier": {"N": 25, "closed": false, "arc_length": 0.073,
              "coef": [[0, 0.51234, 0.48671], [1, 0.00042, -0.00031]]},
  "brush": {"style": 0, "width": {"interp": "spline", "keys": [[0,0.0026],[0.5,0.0048],[1,0.0027]]}}
}
```

- `coef`：傅里叶系数表 `[k, re, im]`（`k` 可正可负，世界坐标归一化单位）。
- `t0/t1`：作画时间区间（秒）；`start/end`：归一化到 [0,1] 的同值。
- `brush.style`：指向 `styles` 表的索引（颜色/alpha/笔形是属性曲线）。笔形即笔刷预设的 `shape`（round/soft/marker/bristle/chalk/spatter/airbrush/pencil/watercolor/ink），见 `references/brush_library.md`。
- `brush.width`：该笔的宽度曲线（弧长归一化 t 求值，世界单位全宽）。
- 每笔时长 ∝ 曲线弧长（运笔快慢），起始时间按 `order` 铺满总时长并允许重叠；
  时间轴只做展开，不重新拟合属性曲线（与 `.fbs` 一致）。

## 与视频的关系

`fbs.py video` 用一笔画文档渲染过程视频；时间轴文件是**同一作画过程的显式数据版**：
可用于外部重绘（按 `t0..t1` 和属性曲线采样）、逐笔编辑（改颜色/宽度/时间）或换渲染器。
