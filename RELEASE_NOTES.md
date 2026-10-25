# 发布包说明：fourier-drawing-suite v1.0.1（2026-10-25，文档与展示更新）

## 包信息

| 项 | 值 |
|---|---|
| 文件 | `dist/fourier-drawing-suite-v1.0.1-release.zip` |
| SHA256 | `5FBF777074412200652D0AA7352D39770B22E3EB2D9DC8E18DFEF743D6768959` |
| 大小 | **21,840,881 字节（约 20.8 MiB）**；解压后约 **28.8 MB**（≤120MB 上限 ✅） |
| 文件数 | **547**（MANIFEST 收录 545 个载荷 + MANIFEST/SHA256SUMS） |
| 顶层目录 | 解压后为 `fourier-drawing-suite-v1.0.1/`（单层） |
| 上一版 | `dist/fourier-drawing-suite-v1.0.0-release.zip`（SHA `EB91A8…`）**保留不动** |

## 本版变更（相对 v1.0.0，功能代码/节点版本/测试结论均不变）

1. **作品示例图**：新增 `docs/images/sunflowers_v136_content_R2048.png`（1553×2048 PNG，
   3,236,750 B，《向日葵》v1.36 人类画师式纯笔画复刻、约 10 万笔、2048 长边、无区域填色）；
   `README.md`「作品示例」、`USER_GUIDE.md` §1、`ENGINEERING.md` §2 均以相对路径引用（GitHub 可渲染）。
2. **赞助/合作联系方式**：**keminoco@outlook.com**，置于 `README.md` 前 40 行醒目 callout、
   `USER_GUIDE.md` §1 末尾、`INSTALL.md`「支持这个项目」、`NOTICE.md`「赞助与联系」、
   `CHANGELOG.md` v1.0.1 条目。
3. **工业设备扩展路线**：`README.md` 新增「从屏幕到工业设备：可扩展方向」（CNC / 刺绣机 /
   激光·绘图仪 / 喷墨·UV / 机械臂，链接到 ENGINEERING §10）；`ENGINEERING.md` 新增
   **§10 工业设备扩展路线（CNC / 刺绣 / 激光 / 机器人）**：笔画数据模型、设备映射表、
   工具路径管线、物理量映射契约、各设备工程问题、Phase 0–4 路线图、代码落点、风险与边界、合作方式。
4. **版本串**：suite v1.0.1（README 标题/版本块/组件表、INSTALL/USER_GUIDE/NOTICE 标题、
   目录树根名 `fourier-drawing-suite-v1.0.1/`、CHANGELOG 顶部 v1.0.1 条目）；
   v1.0.0 仅作为“上一版”保留说明。
5. 顺带修复 v1.0.0 包里被脱敏成 `<path>` 的**示例路径**（README/USER_GUIDE/INSTALL 的安装与解压示例
   恢复为通用示例 `D:\FDP\...` / `D:\ComfyUI...`，不涉及本机真实路径）。

## 自检结果（`pip` 无关，`python work/release_docs/verify_release.py`，全 OK）

| 检查 | 结果 |
|---|---|
| zip 大小 ≤120MB / testzip / 文件数 | ✅ 20.8MB / None / 547 |
| 解压后逐文件 sha256 == 源候选目录 | ✅ 0 mismatch |
| 绝对路径（`C:\Users` / `G:\AI` 含日志/JSON 转义变体） | ✅ 0 |
| 失效相对链接（所有 .md，含新图与 §10 锚点） | ✅ 0 |
| 禁用目录（`__pycache__`/`work`/`training`/`dist`/`.dsh`） | ✅ 0 |
| 文档 UTF-8 / 包内所有 .json 可 `json.load` | ✅ |
| MANIFEST / SHA256SUMS 一致性 | ✅ 545 条全匹配 |
| 节点版本 3.1.0 / 5 workflows / 6 文档 / examples + artifacts / docs 图 | ✅ |

## 使用

与 v1.0.0 相同：见包内 `README.md`（5 分钟快速开始）、`INSTALL.md`、`USER_GUIDE.md`。
安装最短路径：

```powershell
cd fourier-drawing-suite-v1.0.1
"<ComfyUI python>" release\install.py --comfyui "<ComfyUI 目录>"
"<ComfyUI python>" release\check_env.py --comfyui "<ComfyUI 目录>"     # PASS/WARN/FAIL，不联网
# 重启 ComfyUI → 打开 comfyui\workflows\FDP_v136_Local_NoAI_UI.json
```

## 已知限制（与 v1.0.0 相同，不夸大）

- 画质：v1.36 人类画法相对 v1.35 冻结基线 content **−0.486dB**（质量子项 CONDITIONAL）；
  剪影羽化另一 **−0.46dB** 同参考口径代价。
- GPU 渲染非 bit-exact（PSNR 40.2/41.96/44.1dB、SSIM≥0.9957），依赖 PyOpenGL+glfw+桌面 GL 4.x；
  headless 自动回退 CPU；alpha<1/region_fill 文档回退 CPU（exit 5/4）。
- 训练/评估渲染与视频渲染未 GPU 化；仅 Windows+ComfyUI 实测。
- **工业设备扩展（ENGINEERING §10）是路线图，当前发布版尚未实现 CNC/刺绣/激光等导出器**；
  需要材料/刀具/张力实测标定与安全合规评估，不是 CAM 替代品。
- 发布包按规则剔除 `work/`、`training/`、`dist/` 旧包、`__pycache__`、LLM LoRA、demo 视频。

## 复现/再打包

```powershell
$env:FDP_RELEASE_DIR="fourier-drawing-suite-v1.0.1"; $env:FDP_RELEASE_VERSION="1.0.1"
python work/release_docs/build_manifest.py
python work/release_docs/build_zip.py --force
python work/release_docs/verify_release.py
```
