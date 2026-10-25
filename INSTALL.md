# INSTALL — 安装与部署（v1.0.2）

> 目标读者：第一次安装的人。安装完成后请先读 [`USER_GUIDE.md`](USER_GUIDE.md)。
> 本文所有命令以 Windows PowerShell 为例；路径含空格/中文都可以，**请给路径加双引号**。

---

## 1. 准备清单

| 需要 | 说明 |
|---|---|
| Windows 10/11 | 当前发布包只在 Windows 上验证过（脚本是跨平台 Python，但未做 mac/Linux 验证） |
| ComfyUI | 已装好并能正常打开（portable 版或手动安装版均可） |
| ComfyUI 自带 python | 安装器默认用它；**不需要**另装 Python，也不用系统 Python |
| 磁盘空间 | 发布包解压后约 **24–30 MB**（最终包以实际为准）；ComfyUI 输出会占更多 |
| GPU（可选） | 想用 GPU 渲染需要 PyOpenGL + glfw + 桌面 OpenGL 4.x。没有也能用 CPU（慢，但结果更“参考”精确） |

**确认 ComfyUI python 能跑**（用它跑，不要用系统 python）：

```powershell
# ComfyUI portable 版通常在这里（按你的实际路径改）：
"D:\ComfyUI_windows_portable\python_embeded\python.exe" -c "import sys; print(sys.version)"
```

## 2. 解压

把 `fourier-drawing-suite-v1.0.2.zip` 解压到任意目录，例如：

```
D:\FDP\fourier-drawing-suite-v1.0.2\
```

解压后会有一个顶层目录 `fourier-drawing-suite-v1.0.2\`，**先进入该目录**（后续命令都在发布包根目录执行）。
目录内应能看到 `README.md`、`INSTALL.md`、`workspace\`、`comfyui\`、`release\`。
**不要**只解压其中一部分，也不要把 `workspace\` 移走（安装器按相对位置找脚本）。

## 3. 安装节点（自动写配置）

在**发布包根目录**执行：

```powershell
"<ComfyUI python>" release\install.py --comfyui "<ComfyUI 目录>"
# 例（portable）：
"D:\ComfyUI_windows_portable\python_embeded\python.exe" release\install.py --comfyui "D:\ComfyUI_windows_portable\ComfyUI"
# 例（手动安装）：
"D:\ComfyUI_windows_portable\python_embeded\python.exe" release\install.py --comfyui "D:\ComfyUI_windows_portable\ComfyUI"
```

安装器会：

1. 把 `comfyui\custom_nodes\ComfyUI-FDP-T2I` 复制/覆盖到 `<ComfyUI 目录>\custom_nodes\ComfyUI-FDP-T2I`；
2. 在节点目录写入 `fdp_config.json`（记录本发布包 `workspace` 路径与要用的 python 路径）；
3. 打印后续步骤（装依赖、重启 ComfyUI、导入工作流）。

可选参数：

| 参数 | 说明 |
|---|---|
| `--workspace <发布包根目录>` | 显式指定 workspace（默认=脚本所在发布包根目录） |
| `--python <python.exe>` | 显式指定 skill 脚本要用的 python（默认=运行 install.py 的解释器） |
| `--dry-run` | 只打印将做什么，不复制/不写文件 |

> 手动安装（不想跑脚本）：把 `comfyui\custom_nodes\ComfyUI-FDP-T2I` 整个复制到
> `<ComfyUI>\custom_nodes\`，再在节点目录放一个 `fdp_config.json`：
> ```json
> {"workspace": "D:\\FDP\\fourier-drawing-suite-v1.0.2\\workspace", "python": "D:\\ComfyUI_windows_portable\\python_embeded\\python.exe"}
> ```
> 注意 Windows 路径里的 `\` 要写成 `\\`。

## 4. 安装依赖

ComfyUI 自带 python 通常已含全部核心依赖；`check_env.py`/`install.py` 发现缺依赖时会给命令。
在**发布包根目录**执行（用 ComfyUI python）：

```powershell
"<ComfyUI python>" -m pip install -r release\requirements-core.txt
```

核心依赖：`numpy`、`scipy`、`Pillow`、`imageio`、`imageio-ffmpeg`。

**可选：GPU 渲染**（想快就装；缺了会自动用 CPU）：

```powershell
"<ComfyUI python>" -m pip install -r release\requirements-gpu.txt
```

GPU 依赖：`PyOpenGL>=3.1`、`glfw>=2.6`；还需要**桌面 OpenGL 4.x 上下文**（普通 Windows 桌面
都有；无显示/远程服务/headless 环境没有 → 自动回退 CPU，不会崩）。

节点包自身**不需要**额外 pip 依赖（只用 ComfyUI 自带 torch/numpy/Pillow）。

## 5. 重启 ComfyUI

装完节点必须**完全重启 ComfyUI**（不是刷新页面），否则自定义节点列表不会更新。
重启后打开 `comfyui\workflows\` 里的工作流 JSON（拖进 ComfyUI 窗口，或从 ComfyUI 菜单打开）。

## 6. 自检（推荐）

```powershell
"<ComfyUI python>" release\check_env.py --comfyui "<ComfyUI 目录>"
# 只看结果 JSON：加 --json；跳过 GL 检查（无显示环境）：加 --skip-gl
```

检查内容：workspace 结构、python 核心依赖、可选 GPU 依赖/GL 上下文、节点包是否就位、版本与
config、5 个 workflow JSON 是否可解析、源码绝对路径扫描。输出 **PASS/WARN/FAIL** 与修复建议，
**不联网**，退出码 0（无 FAIL）/1。开源发布自测示例：`PASS=11 / WARN=0 / FAIL=0`（有 GL 的
桌面环境）；无 GL 环境 GPU 一项为 WARN，CPU 仍可用。

## 7. 卸载

```powershell
"<ComfyUI python>" release\uninstall.py --comfyui "<ComfyUI 目录>" --yes
```

显示将执行的操作而不删除可加 `--dry-run`。只删除 `<ComfyUI>\custom_nodes\ComfyUI-FDP-T2I`
（含 `fdp_config.json`），**不动** ComfyUI 其它内容、不动你的 `output\fdp_t2i_runs\` 产物。
删除发布包目录即可完成卸载（workspace 里没有写入内容）。

## 8. 离线安装

发布包**不含** pip wheel。离线机器请先在一台能联网、**相同 Python 版本/平台**的机器：

```powershell
"<ComfyUI python>" -m pip download -r release\requirements-core.txt -d wheels
"<ComfyUI python>" -m pip download -r release\requirements-gpu.txt -d wheels   # 可选
```

把 `wheels\` 拷到离线机 → `pip install --no-index --find-links wheels -r requirements-core.txt`。
GPU 的 PyOpenGL/glfw 是纯 Python/预编译 wheel，一般不需要编译。

## 9. 配置与环境变量

| 变量/文件 | 作用 | 解析顺序 |
|---|---|---|
| `FDP_WORKSPACE` | 指定 `workspace` 目录（含 skills/fourier-brushstroke/fourier-prompt） | env > `fdp_config.json` > 发布包布局自动探测 |
| `FDP_PYTHON` | skill 子进程使用的 python | env > `fdp_config.json` > 当前解释器 |
| `FDP_GPU_RENDER=0` | **强制 CPU** 渲染（排错/对比用） | 设置即生效 |
| `fdp_config.json` | 由 `install.py` 写入节点目录；也可手写 | 见上 |

**不想写 config**：设置环境变量即可（推荐在启动 ComfyUI 的脚本里设置，或系统环境变量）：

```powershell
$env:FDP_WORKSPACE = "D:\FDP\fourier-drawing-suite-v1.0.2\workspace"
$env:FDP_PYTHON    = "D:\ComfyUI_windows_portable\python_embeded\python.exe"
```

## 10. 常见安装失败

| 现象 | 原因 | 处理 |
|---|---|---|
| ComfyUI 里看不到 `FDPT2I*` 节点 | 没重启 / 复制错目录 | 确认 `<ComfyUI>\custom_nodes\ComfyUI-FDP-T2I\nodes.py` 存在；重启 ComfyUI；看启动日志报错 |
| `ModuleNotFoundError: numpy` 等 | 装依赖用的不是 ComfyUI python | 用 ComfyUI 自带的 `python.exe` 重新执行 pip install |
| `workspace not found` / 找不到 skill 脚本 | 发布包被移动/只解压部分 | 在节点目录放 `fdp_config.json`，或设 `FDP_WORKSPACE` |
| GPU 渲染立刻回退 CPU、日志 `exit 3` | 无 PyOpenGL/glfw/桌面 GL | 属正常降级；装 GPU 依赖或保持 CPU |
| GPU 渲染起不来、卡住 | 显存不足/驱动/远程桌面 | 改用 1024 或 `FDP_GPU_RENDER=0`；更新显卡驱动 |
| `pip install` 权限错误 | 安装到系统目录 | 用 ComfyUI portable 自带 python（通常无需管理员） |
| 中文/空格路径报错 | 旧版本工具链限制 | 本发布包已按含空格/中文路径测试；仍报错请把路径贴给维护者，先试纯英文短路径 |
| ComfyUI 启动变慢 | 节点包在自定义节点列表里 import | 正常；若超过 30s 看启动日志是否有异常 import |
| 命令行输出里中文/符号显示成 `?` | GBK 控制台编码限制（脚本已按替换字符输出） | 不影响文件与功能；可 `chcp 65001` 切 UTF-8 控制台，或设 `PYTHONIOENCODING=utf-8` |

## 11. GPU 渲染未生效排查

**先确认是否真的回退 CPU**：正常 GPU 渲染输出 `fdraw_R<分辨率>_gpu.png` 与
`fdraw_R<分辨率>_gpu_timing.json`；若只有 `*_exact.png`，说明走了 CPU exact 回退。

| 检查 | 方法 |
|---|---|
| 是否被强制 CPU | 环境变量 `FDP_GPU_RENDER` 不能是 `0` |
| GPU 依赖 | `"<ComfyUI python>" -m pip install -r release\requirements-gpu.txt`（PyOpenGL/glfw） |
| 桌面 GL | 普通 Windows 桌面应有 OpenGL 4.x；远程桌面/无显示服务可能没有 |
| 宿主 env 污染 | **v1.0.2 已修复**：`PYOPENGL_PLATFORM=egl` 会被脚本自动清除；升级到 v1.0.2 即可，无需重启 ComfyUI |
| 文档类型 | `alpha<1`（exit 5）或含 `region_fill`（exit 4）的文档按设计走 CPU exact |
| 看日志 | ComfyUI 日志里 `[FDP-T2I] GPU render ok ...` 表示走了 GPU；回退会打印原因 |

**v1.0.2 修复说明**：旧版在宿主 env `PYOPENGL_PLATFORM=egl` 下，子进程继承后 Windows 加载 EGL 失败 →
`gpu_render.py` exit 3 → 节点静默回退 CPU（100k 笔 4096 实测 3159s）。v1.0.2 的 `gpu_render.py`
在加载 OpenGL/glfw 前清除该变量并加防御重试；修复后同图 **56.4s**（≈56×），节点代码未改、无需重启。

## 12. 版本与兼容

- 节点包版本：`3.1.0`（可移植配置）；`PACKAGE_VERSION` 可用 `FDPT2IWorkflowStart` 节点的元数据查看。
- skill 版本：`v1.38.1-gpu-envfix`（v1.0.2 起；修复宿主 `PYOPENGL_PLATFORM=egl` 导致的 GPU
  静默回退 CPU；节点包仍为 3.1.0）。
- 本发布包不保证兼容未列出的 ComfyUI 版本；实测环境为 ComfyUI（Windows）+ ComfyUI python 3.11。
- 详细故障排查见 [`USER_GUIDE.md`](USER_GUIDE.md) 的 FAQ（含 GPU 未生效排查）。

## 支持这个项目

本项目由个人独立开发与维护；如果它对你的创作或工作有帮助，欢迎赞助支持后续开发。
**赞助 / 合作 / 定制开发请联系：keminoco@outlook.com**

