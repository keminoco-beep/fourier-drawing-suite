# NOTICE — 授权与第三方许可证汇总（suite v1.0.2）

> 本文件是发布包的授权声明汇总。**请以各组件目录内的 `LICENSE` / `LICENSES.md` 原文为准**；
> 本文件只是导航与摘要，不改变任何原始许可证条款。

## 1. 本发布包自身（项目代码与文档）

| 组件 | 授权 | 依据文件 |
|---|---|---|
| `workspace/skills/fourier-multistroke-training` | **Proprietary / All Rights Reserved** | 见仓库根 README 声明（该 skill 无独立 LICENSE 文件） |
| `workspace/fourier-brushstroke` | **Proprietary / All Rights Reserved** | `workspace/fourier-brushstroke/LICENSE` |
| `workspace/fourier-prompt` | **Proprietary / All Rights Reserved** | `workspace/fourier-prompt/LICENSE`；`pyproject.toml` 的 `license = "Proprietary"` |
| `comfyui/custom_nodes/ComfyUI-FDP-T2I` | **Proprietary / All Rights Reserved**（随本项目发布） | — |
| `comfyui/workflows/*.json` | 随本项目，Proprietary | — |
| 本发布文档（README/INSTALL/USER_GUIDE/ENGINEERING/CHANGELOG/NOTICE） | 随本项目，Proprietary | — |

**保留所有权利（All Rights Reserved）**：未经项目所有者书面许可，不得复制、分发、修改或商用。
（如你的获取途径附带了其它许可协议，以该协议为准。）

## 2. 随包资产（字体/笔刷）

| 资产 | 授权 | 说明 |
|---|---|---|
| `fourier-prompt/assets/fonts/NotoSansSC-VF.ttf`（打包子集） | **SIL Open Font License 1.1（OFL-1.1）** | 完整许可证文本：`fourier-prompt/assets/fonts/OFL.txt`；来源：Noto Sans SC / Source Han Sans（OFL-1.1 设计） |
| `fourier-prompt/assets/brush/*`（brush_catalog/calib/style_packs/previews） | **Proprietary**（自测/自生成） | 见 `fourier-prompt/assets/brush/LICENSES.md`：全部数据由本仓库自测/自生成，不含任何第三方笔刷资产、纹理或 bitmap tip |
| `fourier-prompt/assets/fonts/LICENSES.md`、`assets/brush/LICENSES.md` | 说明文档 | 含来源、SHA-256 与生成/复现命令 |
| `docs/images/sunflowers_v136_content_R2048.png` | **Proprietary**（本项目 v1.36 渲染产物） | 文档示例图；由本套流水线生成 |

> 字体仅用于**离线编译文字轮廓**（`compile_plan` 会把字形烘焙成填充多边形），
> 生成的 `.fbs` / `.fdraw` **不依赖**字体文件；分发字体子集需遵守 OFL-1.1（保留版权与许可声明、不得单独售卖字体）。

## 3. 运行时第三方依赖（**不随包分发**，由用户环境提供）

本套件通过 Python 依赖使用以下第三方库；它们**不包含**在本发布包内，请自行遵守各自许可证：

| 依赖 | 用途 | 典型许可证 |
|---|---|---|
| numpy | 数值计算 | BSD-3-Clause |
| scipy | 距离变换/形态学/滤波（剪影 EDT 等） | BSD-3-Clause |
| Pillow (PIL) | 图像 I/O/缩放/合成 | MIT-CMU（HPND 变体） |
| imageio | 图像/视频读写 | BSD-2-Clause |
| imageio-ffmpeg（含 ffmpeg 二进制） | MP4 视频写入 | BSD-2-Clause（ffmpeg 二进制按 LGPL/GPL 分发，见其包内声明） |
| torch / torchvision（ComfyUI 自带，节点经 ComfyUI 使用） | 节点张量与 ComfyUI 运行 | BSD-3-Clause（另有第三方 notices，见 ComfyUI 包） |
| **PyOpenGL**（可选，GPU 渲染） | OpenGL 绑定 | BSD-3-Clause |
| **glfw**（可选，GPU 渲染） | 离屏 GL 上下文 | zlib/libpng |
| ComfyUI 本体（用户自行安装，本包只提供自定义节点） | 运行宿主 | 见 ComfyUI 项目授权（GPL-3.0） |

- 具体版本/许可证以你环境中的 `pip show <pkg>` 与各包 `dist-info` 为准；本表仅作导航。
- **PyOpenGL/glfw 是可选依赖**：缺失时 GPU 渲染自动回退 CPU（`gpu_render.py` exit code 3），
  不影响核心功能。

## 4. AI 模型与 API（不随包分发）

| 项 | 说明 |
|---|---|
| Krea2 本地模型（UNET/CLIP/VAE/LoRA 等） | **不随包分发**；本地工作流要求用户已在 ComfyUI 中装好相应模型 |
| Krea2 API | 需用户自备 API Key 并遵守服务方条款；本包不包含任何 Key |
| 任何 LLM / LoRA 适配器 | **未包含**（发布包已剔除 `fourier-prompt/out/llm` 下约 148 MB 的 LoRA 权重） |

## 5. 商标与名称

- “ComfyUI” 是其各自所有者的商标；本包只是 ComfyUI 的自定义节点扩展，非 ComfyUI 官方发行版。
- “Krea2” 等模型/服务名称归各自所有者；本包不包含其权重或服务。

## 6. 第三方许可证原文位置

| 位置 | 内容 |
|---|---|
| `workspace/fourier-brushstroke/LICENSE` | 组件授权全文（Proprietary） |
| `workspace/fourier-prompt/LICENSE` | 组件授权全文（Proprietary） |
| `workspace/fourier-prompt/assets/fonts/OFL.txt` | SIL OFL 1.1 全文（字体） |
| `workspace/fourier-prompt/assets/fonts/LICENSES.md` | 字体来源/SHA-256/许可说明 |
| `workspace/fourier-prompt/assets/brush/LICENSES.md` | 笔刷数据来源/生成方式/许可说明 |
| `workspace/skills/fourier-multistroke-training/references/` | skill 文档（含第三方算法参考说明） |

> 如果本文件与任何原始 `LICENSE`/`LICENSES.md` 文本冲突，以原始许可文本为准。
> 本项目不提供任何明示或暗示的担保；使用风险由使用者承担。

## 赞助与联系

本项目由个人独立开发与维护；赞助、合作或定制开发请联系：**keminoco@outlook.com**。

