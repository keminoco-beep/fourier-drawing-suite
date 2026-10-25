# VERDICT v1.38 — GPU 渲染（真实提速 / 质量 / 节点回退 / 无新依赖）（task-37 / verifier）

**结论（先看 7 行）**

1. **总评：PASS**。GPU 渲染真实生效且提速远超门槛，质量满足目标，节点自动 GPU + CPU fallback 实测可用，
   workflow/widget 未变，legacy/测试/包/文档全部核验通过；无新第三方依赖、无硬崩溃。
2. **速度（我自跑 ComfyUI python，RTX 4070 Ti SUPER 16GB，SS=2）**：chibi `.fdraw`（38,737 笔）
   GPU **1024=14.57s / 2048=16.77s / 4096=21.87s**（内部 total 13.82/15.98/21.00s，其中 GPU 光栅化仅
   0.11/0.31/1.15s，瓶颈是 CPU load+sample ≈8.6–9.0s）；CPU exact 同口径 **1024=219s（Lead 基线）/
   298.8s（我并发实测）**、**2048=581.6s（Lead）/596.2s（我自己原生跑）**、4096≈1260s → 加速比
   **≈15–20× / ≈35× / ≈58×**，全部 ≥10× 目标，1024≤20s、2048≤30s 达标。
3. **质量（GPU vs CPU exact 同分辨率，我独立复算）**：1024 **PSNR 40.2187 / SSIM 0.99566**；
   2048 **原生 CPU exact 参考（596.2s）** → **41.9643 / 0.99623**（比文档的 4096 降采样代理 40.542 更好）；
   4096 **44.1092 / 0.99724**；均远超 ≥30dB 目标。差异只在笔触边缘 AA/覆盖（边缘环带 PSNR 33.4–38.5，
   平坦区均值差 0.0006≈0.15/255）；GPU PNG 与 fixer 产物 **SHA 完全相同**（1024/2048/4096，确定性）。
4. **视觉（我自裁 4 区 × 3 分辨率 4×）**：发丝 / 鲸尾棘 / 脸颊渐变 / 蕾丝蝴蝶结——GPU 与 CPU exact
   并排肉眼无差，无新锯齿、无色带、无漏笔；差图仅勾边（见 `visual/R1024_diff_x6.png`）。
5. **节点回退**：直接调 `FDPT2IRenderFdraw._run`（我加载部署 nodes.py）—GPU 成功 chibi@1024 **15.56s**、
   tensor `[1,1002,1024,3]`、输出 PNG 与直调 GPU 渲染 SHA 相同；`FDP_GPU_RENDER=0` → CPU fallback 成功
   （小文档@768 139.92s，tensor `[1,648,768,3]`）；`PACKAGE_VERSION=3.0.1`；**workflow JSON 全部未改
   （7 个文件 hash 不变），RenderFdraw `widgets_values` 长度=1**；repo/部署 `nodes.py` hash 相同 `8FE41719…`。
6. **无新依赖/降级**：workbuddy python（无 PyOpenGL/glfw）→ `--device gl` **exit 3** 且 stderr 明确、无 PNG；
   `--device cpu` 正常出图；alpha<1 文档 → **exit 5** 明确（`alpha<1 not supported`），alpha=1 正常；
   gpu_render 仅用 stdlib+numpy（PyOpenGL/glfw 惰性导入），`requirements.txt` 未变。
7. **测试/legacy/包/文档**：我复跑 **142 tests OK (skipped=13) / 84.5s**、RUN_SLOW **3/3 / 10.4s**、
   ComfyUI python `RUN_GPU=1` **19 OK (skipped=1) / 20.3s**；legacy **`30CDCE74…` / `69820EEF…` 逐字节不变**；
   zip **54 文件 / 7,998,523 B / SHA256 `BF4D6A0D…6DFD75`**（README 一致、54/54 sha==源树、绝对路径 0、
   死链 0、旧包未动）；SKILL.md 数字与实测一致，已写明 GL 依赖/质量差/CPU exact 参考，**无“画质无损/
   任何环境可用”宣称**（原文明确写“不能宣称任何环境可用”）。

> 方法：全部数字由 verifier 自己复跑（`work/verify7/`），未采信 fixer/test-harness 自述；CPU 参考与 GPU
> 输出均为本机独立生成（CPU 参考与 fixer 参考 PNG PSNR=120/max diff=0，即逐字节一致）。

---

## 1. 真实 GPU 生效 + 计时（我自己跑，非引用）

环境：`<comfyui>\...\python\python.exe` (3.11.9, PyOpenGL 3.1.10, glfw 2.10.0, torch 2.9.0+cu130)；
`glfw` 隐藏窗口创建成功，GL **4.6.0 NVIDIA RTX 4070 Ti SUPER/PCIe/SSE2**。

| 分辨率（chibi fdraw） | 我的 GPU wall | 内部 total | load / sample / geometry / gl_init / gpu / readback (s) | CPU exact 基线 | 加速比 |
|---|---|---|---|---|---|
| 1024×1002 | **14.57 s** | 13.82 | 3.64 / 8.59 / 0.52 / 0.29 / 0.107 / 0.38 | **219 s**（Lead）/ 298.8 s（我并发实测；内部 render_s 283.8） | **15.0–20.5×** |
| 2048×2004 | **16.77 s** | 15.98 | 4.02 / 8.67 / 1.02 / 0.28 / 0.312 / 1.35 | **581.6 s**（Lead）/ **596.2 s**（我自己跑原生 exact） | **34.7–35.6×** |
| 4096×4008 | **21.87 s** | 21.00 | 3.38 / 9.01 / 1.87 / 0.31 / 1.147 / 4.99 | ≈1260 s（Lead/旧值 ~21min） | **≈58×** |
| 4096 显存（nvidia-smi 轮询，74 样本） | 峰值 3,258 MB、增量 **+947 MB**（总 16,376MB） | | | | 16GB 内安全 |

- 输出维度/背景正确：1024×1002、2048×2004、4096×4008，白底；`float_fbo=true`（RGBA16F）、SS=2。
- 与 Lead/analyst 声明的 14.5/16.5/23.3s 同量级（我的机器负载略异）；与 fixer 内部 14.2/15.1/23.6s 一致量级。
- 计时脚本/JSON：`work/verify7/gpu_timing.py`、`timing_gpu*_verifier_R*.json`、`logs/gpu*.log`。

## 2. 质量（GPU vs CPU exact，独立复算）

| 指标 | 1024×1002 | 2048×2004（**原生 CPU exact**） | 4096×4008 |
|---|---|---|---|
| **PSNR / SSIM（GPU vs CPU）** | **40.2187 / 0.99566** | **41.9643 / 0.99623** | **44.1092 / 0.99724** |
| MAE / max / p99(maxch) | 0.00231 / 0.298 / 0.051 | 0.00141 / 0.380 / 0.039 | 0.00080 / 0.357 / 0.027 |
| fg 区 / bg 区 PSNR | 39.32 / 41.07 | 40.84 / 43.09 | 42.89 / 45.36 |
| 边缘环带 PSNR（fg 边界±3px） | 33.40 | 35.90 | 38.48 |
| 差 >0.05 像素占比 | 1.06% | 0.70% | 0.42% |
| GPU vs 源图 PSNR（CPU 对比） | 21.126（CPU 21.239，Δ0.11） | 20.945（CPU 21.004，Δ0.06） | 20.846（CPU 20.876，Δ0.03） |

- **与文档核对**：1024/4096 的 PSNR/SSIM 与 fixer/analyst 声明**完全一致**；2048 文档用的是 4096 下采样代理
  （40.542/0.99458），我用**原生 CPU exact 2048（我自跑 596.2s）**复核为 41.964/0.99623（比代理更好，方向一致）。
- **确定性**：我的 GPU PNG 与 fixer `gpu_R1024/2048/4096.png` **SHA256 完全相同**；CPU exact 参考与 fixer 的
  `cpu_exact_R1024.png` PSNR=120/max=0（逐字节一致）→ 双方数字可互相复现。
- **视觉**（我自裁 4 区，4× 三栏 source / CPU exact / GPU，每个分辨率一组）：
  `work/verify7/visual/R{1024,2048,4096}_{A_hair_strands,B_whale_tail_spikes,C_cheek_gradient,D_lace_bow}_4x.png`。
  肉眼无差、无新锯齿/色带/漏笔；差图（`R1024_diff_x6.png`）差异只分布在笔触轮廓，内部大面积为 0。
- 说明：GPU 逐 segment 合成 vs CPU 整笔 band 并集，alpha=1 时边缘差 ≤0.42/255（文档已写明，属预期）。

## 3. 节点集成 / fallback / 契约（直接调用部署 nodes.py）

| 检查 | 结果 |
|---|---|
| `PACKAGE_VERSION` | **3.0.1** ✅ |
| `FDPT2IRenderFdraw._run` GPU（chibi 1024） | 成功 **15.56s**、tensor `[1,1002,1024,3]` range `[0.0039,1]`，输出 `fdraw_R1024_gpu.png` + `_gpu_timing.json`（total_s 12.495）；**该 PNG SHA == 直调 gpu_render 输出** |
| `FDP_GPU_RENDER=0` 强制 CPU | 成功（小文档 8MB fdraw@768：**139.92s**，tensor `[1,648,768,3]`，输出 `fdraw_R768_exact.png`）→ fallback 路径可用 |
| GPU 失败自动回退机制 | `_run_cmd` 非零即 raise → `_run` except 后走 `fdrender exact`；exit 3/5/4/超时均落入 fallback（alpha<1 exit5 已单独实测） |
| workflow JSON 未改 | 7 个 workflow 文件 SHA 全部与 v1.38 改动前一致（`logs/workflows_baseline.txt` vs `workflows_after.txt`，Compare-Object 空） |
| RenderFdraw widget 数=1 | 4 个 UI workflow 中 `widgets_values` 长度均 **1**；节点 `INPUT_TYPES` 3 项中仅 `resolution` 为 primitive widget（fdraw_path/run_dir 为自定义 socket 类型） |
| repo/部署 nodes.py 一致 | 两处 hash 均 **`8FE41719564CDAA5…`** ✅；节点 README 的 GPU 章节两副本**完全相同（16 行/1115 字符）** |
| 节点输出正确性 | 节点 GPU 输出 PNG 与直调 GPU 渲染 **SHA 相同**（含默认 SS=2、1024） |

## 4. 无 GL / alpha<1 / CPU 路径 / 依赖

| 检查 | 结果 |
|---|---|
| 无 PyOpenGL/glfw（workbuddy python）`--device gl` | **exit 3**，stderr：`GPU 渲染不可用：PyOpenGL/glfw 缺失…可用 --device cpu`，不产 PNG、不崩溃 ✅ |
| `--device cpu` 同一环境 | 成功出图（256px 小文档，45.9s，device=cpu）✅ |
| alpha<1 文档 GPU | **exit 5** + `alpha<1 not supported by GPU renderer; use --device cpu`；alpha=1 → 正常 ✅（节点据此回退 CPU） |
| 新第三方依赖 | gpu_render.py 仅 `argparse/ctypes/json/math/numpy/os/sys/time/zipfile`（PyOpenGL/glfw 惰性导入）；节点 requirements.txt hash 未变；ComfyUI python 自带 PyOpenGL/glfw 已存在 ✅ |
| 报告/计时字段 | `load_s/sample_s/geometry_s/gl_init_s/gpu_s/readback_s/total_s` + `segments/supersample/device` 齐全 ✅ |
| `fbs.py render --device` | `--device {auto,gl,cpu}`，**默认 cpu**；我实测 default 与显式 cpu 输出 SHA 相同（旧行为不变）、gl 路径可出图 ✅（该文件属 fourier-brushstroke skill，不在本 zip 内） |

## 5. 测试 / legacy / 包 / 文档

| 项 | 结果 |
|---|---|
| 全量测试（workbuddy python，我复跑） | **142 tests / 0 failed / 13 skipped / 84.46s / rc=0**（13 skip = 3 慢 + 10 GPU） |
| RUN_SLOW（我复跑） | **3 / 3 OK / 10.37s** |
| GPU 子集（ComfyUI python，`RUN_GPU=1`，我复跑） | **19 tests / 0 failed / 1 skipped / 20.33s**（1 skip 为“无 GL”用例在 GL 环境跳过） |
| legacy v1.33 canonical（我自跑 74.9s） | PNG **`30CDCE74F11A8E2C0CFBA7A0D760E49AC773DECB902180E2584EC17D6DEB9A01`** ✅ 逐字节不变 |
| legacy v1.34 推荐（我自跑） | PNG **`69820EEF69386B8DC0561AF70DCA1363991D1A81D9C8CA8F52984E1CC4C265C3`** ✅ |
| v1.38 zip | **54 文件 / 7,998,523 B / SHA256 `BF4D6A0DBCAC35BEDFE50B1301FA15DF6EFF9CD42EB42F075BD71217316DFD75`**；testzip None；README sha/bytes 一致；54/54 逐文件 sha==源树；scripts 32 / refs 7 / assets 2 / tests 12；无 training/dist/.dsh/`__pycache__`；旧 v1.36 (`3B2ADF87…`)、v1.37 (`A877DDF9…`) 包未动 |
| 可移植性 | 52 个文本文件扫描 `<path>`、`<path>`、`<path>`、`<path>` → **0 命中**；markdown 相对链接 **0 失效** |
| SKILL.md（v1.38） | 有「GPU 渲染（v1.38）」节 + 版本记录；参数/时间/PSNR/SSIM/退出码/回退/限制齐全；实测数字一致（14.2/15.1/23.6s vs 我 14.6/16.8/21.9s；40.219/40.542/44.109 与实测完全一致）；明确 GL 依赖、alpha<1 限制、CPU exact 仍是参考、**“不能宣称任何环境可用”**；**无“画质无损/质量无损/任何环境可用”宣称** ✅ |
| 节点 README | GPU 章节已加（部署副本 16 行 == repo 副本 16 行），写明 exit 3/5/4、fallback、FDP_GPU_RENDER=0 ✅ |

## 6. 独立发现的问题（均为 P3，不阻塞发布）

| # | 级别 | 问题 | 证据 | 建议 |
|---|---|---|---|---|
| 1 | P3 | SKILL 说“**平坦区 0 差**”属绝对表述：我按局部 std<0.01 定义平坦区（79% 像素），GPU vs CPU 的 max 通道差可达 0.349/255（少数边缘邻近像素），均值 0.0006（≈0.15/255） | `work/verify7/quality_R1024.json`、我的复算脚本输出 | 改为“平坦区差异≈0（均值 0.15/255）” |
| 2 | P3 | SKILL 限制写“**2048 无原生 CPU exact 参考（未测）**”已过时：我本次实测原生 CPU exact 2048=596.2s，GPU 41.964/0.99623（优于代理 40.542） | `work/verify7/ref/cpu_exact_R2048.png`、`quality_R2048.json` | 文档可更新为原生 2048 parity 实测值 |
| 3 | P3 | **节点 README 计时**（16.8/19.4/27.6s，fixer 首次实测）与 **SKILL/dist README**（14.2/15.1/23.6s，后续复测）数字不一致（均真实，机器状态不同） | `node_readme_gpu_{deployed,repo}.txt` vs SKILL §GPU | 统一为同一组终值（建议 14.5/16.5/23.3 或注明“多测区间”） |
| 4 | P3 | 节点包 repo 副本 README（77 行）与部署副本（158 行）整体不同（**GPU 章节相同**）——属既有结构差异，非本次回归 | `logs/node_readme_diff.txt` | 可后续统一（不影响 GPU 修复） |
| 5 | P3 | 提速被 CPU 端 load+sample 限制（8.6–13s，占总时 60–80%）；GPU 光栅化本身仅 0.1–1.4s | 计时 JSON | 可缓存采样几何/并行化进一步提速（文档已列） |
| 6 | P3 | 4096 显存：文档按组件估算 ≈525MB FBO + 347MB 实例 + ~1GB 读回；我实测进程峰值增量 ≈947MB（基线 2.3GB，总 16.4GB） | `logs/vram_probe.log` | 补充“实测增量 ~0.95GB” |
| 7 | P3 | 节点对 `region_fill`（exit 4）与 alpha<1（exit 5）自动回退仅在测试/脚本层验证（我直测了 exit 5 与 standalone fallback，未构造 region_fill 文档直测节点） | tests6 报告、我的 exit5 直测 | 当前流水线不产出这两类文档，风险低 |

## 7. 裁决理由

- **真实提速达标**：我自跑 GPU 1024/2048/4096 = 14.57/16.77/21.87s，对 CPU exact 219/581.6/≈1260s →
  15–20× / ~35× / ~58×，远超 ≥10× 目标，且输出维度/背景正确。
- **质量达标**：1024 40.219dB、4096 44.109dB（与声明一致）；我额外补上**原生 2048 CPU exact parity
  41.964dB/0.99623**；4 区 4× 视觉无新锯齿/色带/漏笔；差异仅边缘 AA。
- **节点集成/回退达标**：GPU 成功 15.56s@1024、CPU fallback 成功（139.92s@768）、workflow/widget 未变、
  PACKAGE_VERSION 3.0.1、repo/部署一致。
- **无新依赖/降级清晰**：exit 3（无 GL）、exit 5（alpha<1）均明确并可回退；CPU 路径可用。
- **测试/legacy/包/文档全部通过**：142 tests + GPU 子集 + legacy 双 SHA + zip 54 文件/新 SHA/结构/可移植性 +
  SKILL 数字一致且诚实（GL 依赖/质量差/CPU exact 参考，未过度宣称）。
- **结论：PASS**（残余均 P3，已在此报告列明；建议按 §6 更新文档表述/统一数字）。

## 8. 证据索引

| 内容 | 路径 |
|---|---|
| GPU 计时（1024/2048/4096，含阶段拆分 JSON） | `work/verify7/timing_gpu*_verifier_R*.json`、`logs/gpu*.log` |
| CPU exact 参考（我自跑 1024=298.8s、2048=596.2s） | `work/verify7/ref/cpu_exact_R{1024,2048}.png`、`logs/cpu_exact*.log` |
| 质量/差异（full/fg/bg/edge-ring、4 区 crop、diff） | `work/verify7/quality_R{1024,2048,4096}.json`、`visual/` |
| 我自渲染 GPU 图 | `work/verify7/runs/gpu_gpu{1024,2048,4096}_verifier_R*.png` |
| 节点直调（GPU/forced CPU/PACKAGE_VERSION/widget） | `work/verify7/node_run_result.json`、`node_run_chibi1024.json`、`runs/node_gpu*` |
| 无 GL exit3 / alpha<1 exit5 / CPU 路径 | `work/verify7/logs/`（本次终端输出）、`runs/cpu_small256.png`、`runs/alpha_*` |
| 测试日志 | `work/verify7/logs/tests_full.log`、`tests_slow.log`、`tests_gpu_subset.log` |
| legacy 复现 | `work/verify7/runs/legacy_v133/`、`runs/legacy_v134/` |
| 包校验 + 文档核对 | `work/verify7/package_validation_v138.json`、`logs/pkg_validation2.log`、`pkg_v138/` |
| workflow 未变证据 | `work/verify7/logs/workflows_baseline.txt` vs `workflows_after.txt` |
| VRAM 实测 | `work/verify7/logs/vram_probe.log` |
| 复算脚本 | `work/verify7/gpu_timing.py`、`quality_compare.py`、`node_run_test.py`、`node_chibi_test.py`、`validate_pkg_v138.py`、`vram_probe.py` |
