# v1.0.0 发布包跨环境兼容性测试报告（task-40）

> 生成时间：2026-10-25T11:31:21+0800　总耗时：562.5s

## 结论

- 矩阵结果：**PASS** — PASS 24 / FAIL 0 / SKIP 0（共 24 项）
- 发布候选：`<workspace>\release\fourier-drawing-suite-v1.0.0`
- ComfyUI python：`<comfyui>\ComfyUI-aki-v1.7\ComfyUI-aki-v1.7\python\python.exe` (3.11.9)
- workbuddy python：`<python>` (3.13.14)
- 备注：F0 为候选目录原样（干净机器）默认套件：缺少 work/、training/ 下的 demo 图片时，依赖这些素材的测试类会 skip，计数低于 142 属预期。
- 备注：F1–F3 在 tmp/test_candidate 临时副本中注入仓库的少量测试素材（work/input2 线稿、training/input 真实图、tiny .fdraw），仅改临时副本、候选目录保持只读，因此完整覆盖 142 用例。
- 备注：F1（ComfyUI python 有 GL）比 F2 多 1 个 skip：test_no_gl_exit_code 在有 GL 的解释器下按设计跳过。

## 矩阵

| ID | 组 | 环境 | 场景 | 状态 | 耗时(s) | 详情 |
|---|---|---|---|---|---|---|
| A1 | A | ComfyUI python | 短英文路径 (65 chars) | **PASS** | 0.5 | copy 501 files/25.0MB; checks={'check_env': {'rc': 0, 'attempts': 1}, 'layered_base': {'rc': 0, 'seconds': 1.0}, 'build_fbs': {'rc': 0, 'seconds': 1.37}, 'cpu_render': {'rc': 0, 'seconds': 1.87}} |
| A2 | A | ComfyUI python | 空格+中文路径 (32 chars) | **PASS** | 0.6 | copy 501 files/25.0MB; checks={'check_env': {'rc': 0, 'attempts': 1}, 'layered_base': {'rc': 0, 'seconds': 0.97}, 'build_fbs': {'rc': 0, 'seconds': 1.37}, 'cpu_render': {'rc': 0, 'seconds': 1.81}} |
| A3 | A | ComfyUI python | 长路径≥120字符 (156 chars) | **PASS** | 0.6 | copy 501 files/25.0MB; checks={'check_env': {'rc': 0, 'attempts': 1}, 'layered_base': {'rc': 0, 'seconds': 1.01}, 'build_fbs': {'rc': 0, 'seconds': 1.49}, 'cpu_render': {'rc': 0, 'seconds': 1.83}} |
| B1 | B | ComfyUI python | 仅 env FDP_WORKSPACE/FDP_PYTHON | **PASS** | 0 | resolved={"ok": true, "workspace": "<workspace>/release/fourier-drawing-suite-v1.0.0/workspace", "fbs": "<workspace>/release/fourier-drawing-suite-v1.0.0/workspace/fourier-brush... |
| B2 | B | ComfyUI python | 仅 fdp_config.json（installer 写） | **PASS** | 0 | install_rc_ok=True config=True resolved={"ok": true, "workspace": "<workspace>/release/fourier-drawing-suite-v1.0.0/workspace", "fbs": "<workspace>/release/fourier-drawing-suite... |
| B3 | B | ComfyUI python | 无 env 无 config → 可操作报错 | **PASS** | 0 | error=RuntimeError: 找不到 FDP workspace（应包含 skills/fourier-multistroke-training/scripts/layered_base.py）。 解析顺序：FDP_WORKSPACE 环境变量 → 节点目录 fdp_config.json 的 workspace → 发布包自动探测。 已尝试：   - <workspace>\wo... |
| C1 | C | ComfyUI python | install.py 安装到 fake ComfyUI | **PASS** | 0.2 | node_copied=True config=True dry_run_ok=True config={"workspace": "<path> skill\\release\\fourier-drawing-suite-v1.0.0\\workspace", "python": "<path> |
| C2 | C | ComfyUI python | check_env.py（安装后 fake ComfyUI） | **PASS** | 0 | attempts=[([], 0)] |
| C3 | C | ComfyUI python | node BuildV136→Pack→Render（GPU） | **PASS** | 28.68 | {"ok": true, "version": "3.1.0", "mode": "gpu", "artifacts": ["<path> skill\\work\\release_test\\tmp\\node_chain\\fdraw_R512_exact.png", "<path> skill\\work\\release_test\\t... |
| C4 | C | ComfyUI python | node RenderFdraw CPU fallback（FDP_GPU_RENDER=0） | **PASS** | 44.72 | {"ok": true, "version": "3.1.0", "mode": "cpu", "artifacts": ["<path> skill\\work\\release_test\\tmp\\node_chain\\fdraw_R512_exact.png", "<path> skill\\work\\release_test\\t... |
| D1 | D | ComfyUI python | layered_base 256（候选） | **PASS** | 1.02 | strokes=115 silhouette_on=True psnr=17.3338 |
| D2 | D | ComfyUI python | build_strokes_fbs 512（候选） | **PASS** | 1.98 | fbs=True size=595213 |
| D3 | D | ComfyUI python | pack_fdraw + fdrender CPU exact 512 | **PASS** | 4.33 | fdraw=True png=True pack_seconds=0.3 render_seconds=4.33 |
| D4 | D | ComfyUI python | gpu_render GL 512 + CPU 512 | **PASS** | 1.51 | gpu_seconds=1.51 cpu_seconds=4.07 timing_total=1.018 device=gl |
| D5 | D | ComfyUI python | node video 2s/512（FDPT2IFourierVideo） | **PASS** | 63.28 | {"ok": true, "version": "3.1.0", "seconds": 62.5, "mp4": "<path> skill\\work\\release_test\\tmp\\video\\fourier_2s.mp4", "poster_shape": [1, 1638, 2048, 3], "log": ""} |
| D6 | D | ComfyUI python | 工作流 JSON 结构校验（3 个 v136） | **PASS** | 0.39 | [  {   "file": "FDP_v136_Local_NoAI_UI.json",   "status": "PASS",   "problems": [],   "node_count": 9,   "link_count": 12,   "render_widgets": 1  },  {   "file": "FDP_v136_T2I_Krea2_API_UI.json",   "status": "PASS",  ... |
| E1 | E | empty venv | 缺依赖检测（空 venv） | **PASS** | 0.32 | rc=1 actionable_message=True t\tmp\empty_venv\Scripts\python.exe 缺少 ['numpy', 'scipy', 'PIL']         -> 运行 release/requirements-core.txt [WARN] python 可选依赖          缺少 ['imageio', 'imageio_ffmpeg']（视频/序列帧功能不可用）      ... |
| E2 | E | workbuddy python | 无 GL → gpu_render exit 3 | **PASS** | 0.31 | rc=3 |
| E3 | E | ComfyUI python | alpha<1 → exit 5 / CPU 可用 | **PASS** | 0.68 | gpu_rc=5 cpu_rc=0 cpu_png=True |
| F0 | F | ComfyUI python | 干净候选默认套件（未注入测试图片） | **PASS** | 67.74 | ran=139 skipped=16（发布包按规则不含 work/、training/ demo 图片，依赖它们的测试类按预期 skip；F1 起在临时副本注入这些图片以覆盖 142 用例） |
| F1 | F | ComfyUI python | 候选默认套件（ComfyUI python） | **PASS** | 71.62 | ran=142 skipped=14 copy=501files/25.0MB |
| F2 | F | workbuddy python | 候选默认套件（workbuddy python） | **PASS** | 86.19 | ran=142 skipped=13 copy=501files/25.0MB |
| F3 | F | ComfyUI python | 候选 RUN_GPU 子集（19 用例） | **PASS** | 21.05 | ran=19 skipped=1 |
| F4 | F | workbuddy python | legacy v1.33/v1.34 逐字节 | **PASS** | 166.82 | {"v133": {"sha_match": true, "seconds": 74.77, "png": "<path> skill\\work\\release_test\\tmp\\legacy\\v133\\layered_base_R1536.png"}, "v134": {"sha_match": true, "seconds": 92.05, "png": "<path> |

## 关键产物

- `<workspace>\work\release_test\tmp\node_chain\fdraw_R512_exact.png`
- `<workspace>\work\release_test\tmp\node_chain\fdraw_R512_gpu.png`
- `<workspace>\work\release_test\tmp\node_chain\source.png`
- `<workspace>\work\release_test\tmp\node_chain\fdraw_R512_exact.png`
- `<workspace>\work\release_test\tmp\node_chain\fdraw_R512_gpu.png`
- `<workspace>\work\release_test\tmp\node_chain\source.png`
- `<workspace>\work\release_test\tmp\chain\human\layered_base_report.json`
- `<workspace>\work\release_test\tmp\chain\doc.fbs`
- `<workspace>\work\release_test\tmp\chain\fdrender_cpu_512.png`
- `<workspace>\work\release_test\tmp\chain\gpu_gl_512.png`
- `<workspace>\work\release_test\tmp\chain\cpu_512.png`
- `<workspace>\work\release_test\tmp\chain\gpu_512_timing.json`
- `<workspace>\work\release_test\tmp\video\fourier_2s.mp4`
- `<workspace>\work\release_test\tmp\alpha_cpu.png`

## 方法与限制

- 被测候选目录只读；所有重定位副本/临时配置/安装目标都在 `work/release_test/tmp/` 或 `<path> 发布 测试\`。
- GPU 用例使用本机 ComfyUI python（PyOpenGL/glfw + 桌面 GL）；无 GL 环境（workbuddy python）用于 exit 3/CPU 降级测试。
- 缺依赖检测用空 venv（不联网、不安装任何包）。
- 视频节点若候选未随包分发 `poc/t2i/fdraw_fourier_video.py` 则 SKIP 并注明。

## 关键产物（已收集到 work/release_test/artifacts/）

- `artifacts/D1_layered_base__layered_base_R256.png`（7103 bytes）
- `artifacts/D1_layered_base__layered_base_report.json`（4414 bytes）
- `artifacts/D1_layered_base__layered_base_strokes.json`（26302 bytes）
- `artifacts/D2_fbs__doc.fbs`（595213 bytes）
- `artifacts/D3_fdrender_cpu_512__fdrender_cpu_512.png`（16266 bytes）
- `artifacts/D4_gpu_render__gpu_gl_512.png`（15353 bytes）
- `artifacts/D4_gpu_render__cpu_512.png`（16266 bytes）
- `artifacts/D4_gpu_render__gpu_512_timing.json`（362 bytes）
- `artifacts/C_node_chain__fdraw_R512_gpu.png`（42391 bytes）
- `artifacts/C_node_chain__fdraw_R512_exact.png`（44082 bytes）
- `artifacts/C_node_chain__source.png`（2075 bytes）
- `artifacts/D5_video__fourier_2s.mp4`（265023 bytes）
- `artifacts/F3_gpu_subset_log__F3_gpu_subset.log`（8206 bytes）

## 变更说明

- task-40 未修改被测候选目录（只读）；重定位副本、安装目标、临时配置、
  测试素材注入均在 `work/release_test/tmp/` 下。
- task-40 未修改 `tests/`（本轮只需 work/release_test 写域）；此前版本兼容
  断言修复只在 `tests/test_gpu_render.py`，已由 fixer-docs 收入候选。

