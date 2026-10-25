# chibi 示例（suite v1.0.0）

本目录是一个**完整可复现**的小样例：白底 chibi 插画（421x412）→ 512 工作分辨率笔画画 →
`.fbs` 傅里叶表示 → `.fdraw` 便携容器 → 2048 渲染。

| 文件 | 说明 |
|---|---|
| `source.png` | 输入源图（白底 chibi） |
| `strokes.json`（或 `.json.gz`） | 512 工作分辨率下的全部笔触（归一化坐标/宽度/颜色） |
| `layered_base_R512.png` | 512 工作分辨率渲染 |
| `report.json` | 512 运行报告（参数/笔数/耗时/质量/剪影字段） |
| `chibi.fdraw` | 便携笔画容器（可直接渲染/出视频） |
| `render_R2048.png` | 2048 交付级渲染（**由本目录 `chibi.fdraw` 重新渲染**；GPU 约 5s，CPU 更慢） |

> 注：中间 `.fbs.json`（35MB）不随发布包提供；需要时按下方命令 2) 重新生成。

## 复现（在发布包根目录，ComfyUI 自带 python 或任意有 numpy/scipy/PIL 的 python）

```powershell
$PY = "<python>"
$SK = "workspace\skills\fourier-multistroke-training\scripts"

# 1) 生成笔画（512）
& $PY "$SK\layered_base.py" --input examples\chibi\source.png --outdir $env:TEMP\chibi512 `
  --work-res 512 --coarse 20 --mid 12 --fine 6 --mid-err-pct 60 --fine-err-pct 45 `
  --pass4 --pass4-err-pct 45 --pass4-spacing 6 --base-color-mode toned --base-color auto `
  --upper-color-mode source --upper-blur 2.0 --upper-gradient `
  --texture-flat --texture-flat-count 3000 --texture-flat-ref-std 0.015 `
  --clip-to-fg auto --silhouette auto

# 2) 编译 .fbs + 打包 .fdraw
& $PY "$SK\build_strokes_fbs.py" --strokes $env:TEMP\chibi512\layered_base_strokes.json `
  --input examples\chibi\source.png --out examples\chibi\my.fbs.json --resolution 512
& $PY "$SK\pack_fdraw.py" --fbs examples\chibi\my.fbs.json --out examples\chibi\my.fdraw

# 3) 渲染（GPU 可用时；否则 --device cpu）
& $PY "$SK\gpu_render.py" --fdraw examples\chibi\my.fdraw --out examples\chibi\my_R2048.png `
  --resolution 2048 --supersample 2

# 4) 傅里叶作画视频（可选，CPU，慢）
& $PY "$SK\fdraw_to_video.py" --fdraw examples\chibi\my.fdraw --out examples\chibi\my.mp4
```

> 本样例 strokes 数：**5665**（512 工作分辨率；`render_R2048.png` 由本目录 `chibi.fdraw`
> 用 `gpu_render.py --resolution 2048 --supersample 2` 重新渲染，GPU 约 5.1s）。
> 质量/耗时数字见 `report.json` 与 `test_artifacts/metrics/`（不同次测量有波动）。
