# WP-J 报告 — 渐进式绘画视频（fdvideo）

- 日期：2026-10-20 ｜ 状态：**完成**（短视频端到端 + 20s 帧流式 + 上限/错误路径 + 26 测试）
- 任务：KICKOFF v4 §3（WP-J，共享 task-20）
- 新增文件：`fourier_prompt/fdvideo.py`、`fourier_prompt/fdvideo_cli.py`、`tests/video/`、本报告
- 未改动：skill、`cli.py`、`fdrender_cli.py`、`fdraw.py`、`compile.py`、`render_fast.py`、`docs/`、`analysis/`

## 1. 接口与语义

```python
# fdvideo.py
class VideoError(code, message, evidence, hints)

def frame_indices(n_strokes: int, frames: int) -> list[int]
    # 长度 = frames；第 i 项 = 第 i 帧可见的前缀元素数（渲染顺序：层→fill→文档序）
    # 首帧 1 个元素、末帧 = n_strokes、单调不减；n=0 → 全 0；frames=1 → [n]

def render_video(doc_or_path, *, out, resolution=512, duration_s=20.0, fps=24,
                 quality="fast", codec="libx264", max_frames=600, background=None) -> dict
def render_video_from_fdraw(path, **kwargs) -> dict   # unpack → render.json
def render_frames(doc_or_path, *, out_dir, count=10, resolution=512, duration_s=20.0,
                  fps=24, quality="fast", max_frames=600, background=None) -> dict
def probe_video(path) -> dict                          # 解码端复核 size/fps/duration/frames

# fdvideo_cli.py
# python -m fourier_prompt.fdvideo_cli video  --fdraw X.fdraw --out X.mp4 [...] [--json]
# python -m fourier_prompt.fdvideo_cli frames --fdraw X.fdraw --out-dir D --count 10 --resolution 256 [--json]
```

- **揭示顺序** = `render_fast.truncate_doc` 的前缀（层→fill→文档序），与 `.fbs` 绘制顺序一致；每帧只是比上一帧多了若干新元素，**不会**出现已画内容消失。
- 每帧用 `render_fast.render_auto`（`fast` 不支持 shape 时自动回退 `exact`）；`fallbacks` 记录原因、`quality_used` 记录实际后端，从不静默。
- 只有「可见元素数变化」的帧才重新渲染（`unique_renders`），元素数不变的帧复用上一帧 RGB —— 51 元素的 480 帧视频只渲染 51 次。
- 返回：`{out,width,height,fps,frames,duration_s,bytes,sha256,quality,fallbacks,...}`，另附 `frame_hashes`（每帧原始 RGB 的 SHA-256）、`frame_hashes_sha256`、`frame_hashes_sampled`（10 帧抽样）、`quality_used`、`approximations`、`elements`、`unique_renders`、`requested_frames`、`warnings`、`probe`、`elapsed_s/render_s/encode_s`、`rss_mb/peak_rss_mb`、`ffmpeg/imageio_ffmpeg` 版本。
- CLI 退出码：`0` 成功、`1` 输入/渲染/编码失败（含 `VD_E_NO_FFMPEG`）、`2` 参数错误（argparse + `VD_E_ARG`）、`3` IO/内部错误。

## 2. 帧流式与内存

- 编码走 `imageio_ffmpeg.write_frames`（`subprocess.Popen(ffmpeg, stdin=PIPE)`），逐帧 `send(rgb24 bytes)`，**不保留帧列表**；同一时刻只有 1 帧 + 1 块渲染画布。
- 奇数尺寸：`yuv420p` 要求偶数，最后一行/列做边缘复制补 1px（确定性），返回/报告的 `width/height` 是实际编码尺寸。
- 编码参数：`imageio-ffmpeg 0.6.0` + 内置 `ffmpeg 7.1-essentials_build (gyan.dev)`，`libx264`、`-crf 15`（`quality=7` 映射）、`yuv420p`、`macro_block_size=1`（不由 imageio 缩放）。

实测（`out/verify/01.fdraw`，6 层 / 51 元素，纯 CPU）：

| 档位 | 帧数 | 尺寸 | 耗时 | render / encode | 峰值 RSS | tracemalloc 峰值 | mp4 体积 |
|---|---|---|---|---|---|---|---|
| DoD 短视频 R256 2s@12fps | 24 | 256×256 | **13.8–14.3 s** | 13.6 / 0.02 s | 97.5 MB | — | 36,000 B |
| 20s@24fps R256 | 480 | 256×256 | **27.31 s** | 27.02 / 0.05 s | 98.2 MB | 49.47 MB | 74,704 B |
| 20s@24fps R512 | 480 | 512×512 | **70.50 s** | 69.88 / 0.12 s | 193.9 MB | — | 159,984 B |
| 上限演示 R64 60s@24fps→600 帧 | 600 | 64×64 | 7.00 s | — / — | — | — | 33,820 B |

内存判据：若把 480 帧全部留在内存，R256 需 480×256²×3 = **90.0 MB**、R512 需 **377.5 MB**；实测 tracemalloc 峰值仅 **49.47 MB**（R256，含渲染超采样画布），进程峰值 RSS 98/194 MB，均 **远低于 1 GB** 目标，且编码耗时与帧数近似线性（0.05–0.12 s/480 帧）——瓶颈在 51 次前缀渲染（R256 ≈ 0.53 s/次，R512 ≈ 1.37 s/次），不在流式编码。

> tracemalloc 峰值来自独立进程的复跑（同参数输出 `01_20s_r256_mem.mp4`，与普通运行同为 74,704 B 且 `frame_hashes_sha256` 相同）；tracemalloc 自身带来约 3× 开销（84.8 s vs 27.3 s），故耗时列取未开 tracemalloc 的 CLI 运行。

## 3. 确定性

判据 = **每帧原始 RGB 的 SHA-256 序列**（`frame_hashes_sha256` = 全部帧 hash 串接后的 SHA-256），不是 mp4 容器字节。

| 证据 | 结果 |
|---|---|
| 01 R256 2s@12fps 连续三次运行（run1/run2/run3） | `frame_hashes_sha256` 完全相同（`e7f668653f58e9c2…`），24 帧逐帧 hash 相同；mp4 `sha256` 也相同（`e39fcee5bc436996…`，36,000 B） |
| 01 R256 20s@24fps 两个独立进程（普通运行 / tracemalloc 运行） | `frame_hashes_sha256` 均为 `9404f84364b06a45…`（480 帧） |
| `tests/video` 内两两重复运行 | 单笔画/多元素/01_rose 三组 `frame_hashes`、`frame_hashes_sampled`、`frame_hashes_sha256` 全等 |

说明：本机同 ffmpeg 构建下 mp4 容器字节也恰好一致，但报告与测试**只以帧 hash 为判据**——容器可含编码器元数据/封装时间戳，不能作为跨环境承诺。

## 4. 帧数上限 / 抽样

- `frames = min(round(duration_s × fps), min(max_frames, 600))`；`max_frames` 硬上限 600（批次「视频 ≤600 帧」约束）。
- 触发上限时：**缩短时长**到 `frames/fps`，写入 `warnings`（`W_VIDEO_FRAME_CAP`），并保留 `requested_frames` / `requested_duration_s` 供调用方判断；绝不静默。
- 实测：`--duration 60 --fps 24` → 请求 1440 帧 → 实际 600 帧 / 25.0 s，warning 原文：
  `W_VIDEO_FRAME_CAP: 请求 1440 帧（60s@24fps）> max_frames=600，按时长上限编码 600 帧（25.000s）`
- `frames` 子命令按 `sample_positions` 均匀抽样（含首末帧，`count>frames` 时钳制为全部帧）。

## 5. DoD 核对

| # | 要求 | 结果 |
|---|---|---|
| 1 | `01_rose` 短视频（R256、1–2s、12fps）成功，帧数/时长/尺寸正确，解码器读回一致 | ✅ R256/2.0s/12fps → 24 帧、256×256、2.0 s；`imageio_ffmpeg` 解码 **24 帧**、`size=(256,256)`、`fps=12.0`、首帧 196,608 B = 256×256×3 |
| 2 | 20s/24fps 档冒烟，帧流式内存 ≤1 GB | ✅ R256 与 R512 各 480 帧；峰值 RSS 98.2 / 193.9 MB，tracemalloc 49.5 MB（R256）；720×1080 档未跑（R≤512 约束），以 R512 20s 替代并在此注明 |
| 3 | 同输入两次运行抽样帧像素 hash 序列相同 | ✅ 见 §3（10 帧抽样 + 全 480 帧序列跨进程一致） |
| 4 | `tests/video/` ≥10 例 | ✅ **26 例 OK**（10.3 s） |
| 5 | 报告帧数/耗时/RSS/编码器/限制 | ✅ 本报告 |

`01_rose` 在 `fast` 下无回退（`fallbacks=[]`、`quality_used=["fast"]`），与 WP-F 的 fast 覆盖一致；回退路径由 `shape=pencil`（fast 不支持 → `exact`，`fallbacks` 含 `shape=pencil…`）与 `quality=exact` 两个用例覆盖。

## 6. 测试（`tests/video/`，26 例）

```
cd fourier-prompt
& $PY -X utf8 -m unittest discover -s tests/video -v
# Ran 26 tests in 10.328s — OK (exit 0)
```

- **帧规划**：`frame_indices` 基本/退化（n=0、frames=0/1、帧多于元素、帧少于元素、负数报错）、`sample_positions` 抽样与钳制；`_frame_payload` 奇数尺寸补边（末列/末行复制、偶数尺寸零拷贝）；
- **空文档**（0 元素 → 4 帧全同 hash、可解码）、**单笔画**（3 帧全同、`unique_renders=1`、两次运行同 hash）；
- **揭示进度**：6 元素/6 帧 → 6 次唯一渲染，末帧像素 hash **等于**完整文档的 `render_auto` 输出，首帧不同；
- **奇数画布**：0.9×1.0 画布 R33 → 30×34（补 1 行），解码尺寸/首帧字节/probe 尺寸均与报告一致；
- **上限**：300 请求 → 5 帧 + warning + 0.167 s；`max_frames=5000` → 硬上限 warning；
- **`quality=exact` + background 覆盖**；**`shape=pencil` 回退 exact**（`quality_used=["exact"]`、`fallbacks` 非空且含 pencil）；**无 ffmpeg**（`VD_E_NO_FFMPEG`、有安装 hint、不生成文件）；**非法参数**（fps=0/1000、duration=0/-1、quality、resolution、max_frames、codec、background 共 10 组）；**输入缺失/类型错误**；**probe**；
- **CLI**：`video --json` 成功（4 帧/48×48/source/sha256 与文件一致）、`--fbs` 文本输出、无参数/缺 source/非法 quality → 2、非法 fps → 2（`VD_E_ARG`）、文件缺失 → 1（`VD_E_INPUT`）、**无 ffmpeg → 1**（含 hint）、`frames` → 3 PNG + 帧列表 + 复跑同 hash；
- **端到端**：01_rose R256 1s@12fps（12 帧、尺寸、解码帧数、probe）与 01_rose R128 0.5s@8fps 两次运行确定性。

## 7. 复现命令

```powershell
$PY = "<python>"
cd "<workspace>\fourier-prompt"

# DoD 短视频（R256 / 2s / 12fps）
& $PY -X utf8 -m fourier_prompt.fdvideo_cli video --fdraw out\verify\01.fdraw --out out\verify\01_run1.mp4 --resolution 256 --duration 2 --fps 12 --json

# 20s@24fps 帧流式（R256 / R512）
& $PY -X utf8 -m fourier_prompt.fdvideo_cli video --fdraw out\verify\01.fdraw --out out\verify\01_20s_r256.mp4 --resolution 256 --duration 20 --fps 24 --json
& $PY -X utf8 -m fourier_prompt.fdvideo_cli video --fdraw out\verify\01.fdraw --out out\verify\01_20s_r512.mp4 --resolution 512 --duration 20 --fps 24 --json

# 帧数上限（1440 → 600）
& $PY -X utf8 -m fourier_prompt.fdvideo_cli video --fdraw out\verify\01.fdraw --out out\verify\01_cap600.mp4 --resolution 64 --duration 60 --fps 24 --json

# 抽样帧 PNG（与视频帧逐像素一致）
& $PY -X utf8 -m fourier_prompt.fdvideo_cli frames --fdraw out\verify\01.fdraw --out-dir out\verify\frames_01 --count 10 --resolution 256 --duration 2 --fps 12 --json
```

产物（`out/verify/`）：`01_run1.mp4`/`01_run2.mp4`/`01_run3.mp4`（三次运行均 36,000 B，同 `sha256`）、`01_smoke.mp4`（早期 R256/1s 冒烟）、`01_20s_r256.mp4`（74,704 B）、`01_20s_r256_mem.mp4`（tracemalloc 复跑，同 74,704 B）、`01_20s_r512.mp4`（159,984 B）、`01_cap600.mp4`（33,820 B）、`frames_01/frame_*.png`（10 张）。抽样 PNG 的帧 hash 与同一揭示位置视频帧的 `frame_hashes` 相同（如 frame 0 = `63d5e65e80c9…`，末帧 = `6bc3edf257c0…`），可作独立校验。

## 8. 限制与风险

1. **渲染是「前缀重渲」而非增量画布**：每次提交元素变化都重新渲染整个前缀，总成本 O(元素数²)；51 元素 R512 约 70 s。若后续要 1080p 长视频，建议做层/元素增量合成或前缀帧缓存。编码本身很便宜（480 帧 0.05–0.12 s）。
2. **`max_frames` 硬上限 600**：超过时**缩短时长**（不是降 fps），并通过 `warnings`/`requested_*` 明示；调用方若需要严格 60 s 必须自行分段。
3. **容器字节非确定性判据**：同机同 ffmpeg 下 mp4 sha256 恰好一致，但跨 ffmpeg 版本/线程配置不保证；确定性证据以 `frame_hashes` 为准。帧 hash 是**编码前 RGB**，yuv420p 解码后像素有损（有差异属正常）。
4. **奇数尺寸补 1px**：yuv420p 要求偶数，边缘复制后报告尺寸为编码尺寸；`frames` 子命令 PNG 用真实渲染尺寸，两者在奇数画布下可能差 1px（已有 0.9×1.0@R33 → 30×34 用例）。
5. **只验证了 mp4/H.264**：`codec` 参数透传，其它容器/编码器未测；无音频、无 UI/时间轴缓动、按元素均匀提交（无分层停顿时长控制）。
6. **RSS 依赖 Windows psapi**（ctypes），非 Windows 下 `rss_mb/peak_rss_mb` 省略；tracemalloc 峰值会显著拖慢渲染（84.8 s vs 27.3 s），仅用于内存证据。
7. **720×1080 档未跑**（批次 R≤512 约束），以 R512 20s/480 帧替代；真实 1080p 长时间视频耗时/内存需另测。
8. 视频渲染复用 `render_fast`，因此继承其 fast 语义：不支持 shape 会回退 `exact`（逐帧变慢但视觉一致），`approximations` 会记录 soft/marker/watercolor/bristle 的近似。
9. 输入 `.fdraw` 只读；输出目录不存在时自动创建；编码失败/空文件/无法解码均抛 `VideoError`，不会留下「成功但 0 字节」的结果。
