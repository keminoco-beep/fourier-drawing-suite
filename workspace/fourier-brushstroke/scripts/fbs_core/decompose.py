"""Stage 1 :: 混合笔画分解（区域轮廓填充 + 中轴分支）。

输入：`img`（preprocess 输出）+ `analysis`（coverage 输出：bg / palette / labels / cov / mask）。
输出：`{"elements", "regions", "stats", "method", "accelerators"}`，
element 字段严格按 CONTRACT.md §3。

算法主线（每一步都对应旧版被量化验证过的教训）
------------------------------------------------
1. 逐「颜色标签连通块」处理（不是整幅图一套中轴）。
2. **路由靠结构量而非图像类别**：`sprawl = 面积/(π·maxDT²)`
   （实心块 ≈1，蔓延细线网络 ≫10）+ 最大内切半径。块状 → `region_fill`；
   其余 → 中轴分支。旧版按类别路由分不开「线稿」与「填色插画」，改按此量才分开。
3. **region_fill**：追踪掩码边界（像素边链 → 闭合多边形，天然位于像素边缘 =
   旧版「内边界 + 亚像素外扩 0.5px」的等价做法）→ 亚像素级简化 →
   显式 `fill_poly`（渲染不依赖有损重建，P3）。代表色取**腐蚀内部**、
   沿**色度轴高分位**（中位数会把柔边腮红拉回背景色，P4）。
4. **medial_branch**：DT → Zhang-Suen 细化 → 去阶梯伪交叉点 → 尖角补支 /
   端点延伸（corner_preserve，避免尖角被圆化）→ 图切分（端点/交汇点）→
   缺口桥接（虚线接回）→ 剪枝（阈值按局部 DT）→ 覆盖修复（圆盘并集残差补笔）→
   亚像素中心线（覆盖率法向质量重心 + **单峰判据**，平行双线会落到中间）→
   宽度（**覆盖率法向积分 ∫cov dn**，亚像素细线也精确）→ 颜色（去混色 + 双线性）→
   alpha（核心覆盖/源 alpha）。
5. **退化点盘**（实心圆盘骨架为 0 长度）单独输出，**不允许消失**。
6. **layer**：所有 region_fill 在底层（面积降序），所有 medial_branch 在上层
   （max halfwidth 降序）——R7「先铺底层再压上层」。
"""
from __future__ import annotations

import math
import time
from typing import Any

import numpy as np

from .config import FBSConfig
from .util import (
    WarningLog,
    bilinear_sample,
    distance_transform,
    erode,
    fill_holes,
    gaussian_blur,
    label_components,
)

# ---------------------------------------------------------------- 常量（均有量纲说明）
# 块状区域判据：最大内切半径下限(px) 与 面积/(π·maxDT²) 上限。
# 实心方块/圆 sprawl≈1.0–1.3；星形约 1.5–3；蔓延细线网络 ≫10（旧版实测校准）。
_FILL_MIN_DT_PX = 3.0
_FILL_SPRAWL_MAX = 3.0
# 线稿/扁平类补充判据：细长**实心**笔画（logo 字母、色块）sprawl 也会 >3，但必须走填充；
# 蔓延细线网络（发丝、排线）的薄像素占比接近 1。用自身厚度证据区分（实测：logo/horn
# 实心区 thin_frac 0.11–0.25、elf 大色块 0.03、细线网络 ≈1.0）。
_FILL_MAX_THIN_FRAC = 0.55
_FILL_MIN_DT_MED_PX = 2.0
# 单笔画/签名类输入（连通块很少）保持中轴分支语义（宽度反演/笔触顺序有意义）；
# 复杂作品（多区域）才启用上面的"细长实心结构 → 填充"放宽。
_FILL_THIN_ROUTE_MIN_REGIONS = 3
# 纯二值/硬量化源（无抗锯齿信息）不启用上述放宽：轮廓填充会复现阶梯，中轴渲染更接近重采样参考。
_FILL_THIN_ROUTE_MIN_AA_FRAC = 0.05
# 只有"作品类"输入（连通块足够多）才启用放宽；渐变/单笔画等合成或稀疏输入保持中轴语义
# （B3/R6/T12 的宽度/颜色曲线反演依赖 median 分支，不能被填充吞掉）。
_FILL_THIN_ROUTE_MIN_COMPONENTS = 20
# width_mode="auto"：连通块 ≥ 该值时用 DT 宽度（复杂密排内容），否则 hybrid（单笔画类）。
_AUTO_WIDTH_DT_MIN_REGIONS = 10
# 混合宽度：width = min(覆盖率法向积分, K·DT 宽度)。覆盖率积分在邻接结构/AA 晕圈处会
# 沿法向多积到别人的墨量 → 宽度偏粗；DT 是保守下界（整数像素栅格）。取 min 防"法向外溢"。
_HYBRID_WIDTH_K = 1.25
# 亚像素边界：把像素边格点回路沿法向对齐到 50% 覆盖率等值线。
# 格点轮廓天然是整像素阶梯，边界误差环占线稿误差能量的大头；覆盖率场在抗锯齿带内
# 是连续的，模糊后 0.5 等值线就是"真实边界"，逐顶点沿法向找交点即可亚像素化。
_SUBPIXEL_EDGE_SIGMA = 0.8        # 覆盖率场平滑（px）；太小会贴回阶梯，太大糊掉小特征
_SUBPIXEL_EDGE_SEARCH_PX = 1.2    # 沿法向搜索范围（px）
_SUBPIXEL_EDGE_STEP_PX = 0.05     # 搜索步长（px）
_SUBPIXEL_EDGE_LEVEL = 0.5        # 等值线水平（覆盖率）
# 亚像素边界只对"真实抗锯齿源"启用：硬量化/NEAREST 放大源（AA 像素占比低）与插画类
# 的边界本就靠软边表达，强行对齐 50% 等值线会改变外扩量、实测掉分（logo −0.15dB）。
_SUBPIXEL_MIN_AA_FRAC = 0.05
# 带洞填充：洞面积门限（px² 且 ≥ 外轮廓面积的比例）与单区域洞数上限。
# 洞由渲染端用背景色挖掉，因此带洞区域不再回退中轴（回退会整块丢填色）。
_HOLE_MIN_AREA_PX = 6.0
_HOLE_MIN_FRAC = 0.002
_HOLE_MAX_COUNT = 64
# lineart/flat_art 的"带洞填充"最多容忍 4 个洞：更多洞通常是线网之间的空白，
# 轮廓填充会把它们整块涂掉（回退中轴才是正确表达）。
_HOLE_FILL_MAX_COUNT = 4
# 短笔画保真：原本"长度 < 0.5×min_branch_length"的分支会被直接丢弃；对**有实际墨量**的短分支
# （排线/点状笔触/细小结构）改为保留为短笔画，只有墨量太小的噪声才丢。
_SHORT_BRANCH_MIN_MASS_PX2 = 2.5
# 纹理感知表示：长度 <2.5px 的短墨段（排线点/网点/小碎笔）不再画成"圆头短笔画"
# （圆头会把 1–2px 的点画成更大的团，导致过黑/糊），而按**墨量面积**换成点盘元素
# （半径由墨量/DT 导出、颜色取该点在源图上的墨色）。这是"纹理区选点表示"的最小实现。
_TEXTURE_DOT_MAX_LEN_PX = 2.5
_TEXTURE_DOT_MIN_MASS_PX2 = 1.5
# 插画类的短碎片多为抗锯齿/纹理噪声，保留门槛更高（否则短笔画会把噪声画出来）。
_SHORT_BRANCH_MIN_MASS_PX2_ILLUSTRATION = 6.0
# 轮廓简化容差(px)：填充边界是显式多边形，0.35px 在亚像素量级且省一半顶点
_CONTOUR_SIMPLIFY_PX = 0.35
# 中轴重采样密度（点/像素弧长）。点数 = ceil(弧长×密度)，由数据导出，不是写死点数。
_RESAMPLE_DENSITY = 2.0
# 亚像素中心线：法向采样步长(px)、每侧余量(px)、单点最大偏移(px)、迭代轮数
_COV_STEP_PX = 0.25
_COV_MARGIN_PX = 1.5
_REFINE_ITERS = 3
_REFINE_MAX_SHIFT_PX = 1.0
# 覆盖率积分：支撑阈值（低于它视为离开本结构）、最小可测墨量(px)、单峰判据比例
_COV_SUPPORT = 0.03
_COV_MIN_MASS_PX = 0.10
_COV_HUMP_FRAC = 0.5
# 积分计算的单次参与采样点上限（纯性能控制：超限时对中心线点抽稀后线性插值回来，
# 不丢任何中心线点；不影响几何语义）
_COV_MAX_CELLS = 400_000
# 覆盖率积分的法向窗口相对局部厚度的放大系数（阈值由局部 DT 导出，R3.1）
_COV_SPAN_FACTOR = 3.0
# 覆盖修复：残差占比低于它视为已收敛；最大轮数由残差连通块数导出
_REPAIR_MIN_RESIDUAL_FRAC = 0.004
# 点到段/线段的最小有效长度(px)
_EPS_LEN = 1e-9
# 尖角检测：转角阈值(deg) 与基线占周长比例（尺度无关判据，旧版实测 50°）
_CORNER_TURN_DEG = 50.0
_CORNER_BASELINE_FRAC = 1.0 / 64.0
# 区域颜色场：立方单元最小边长(px) 与启用场的最小区域面积(px)
_COLOR_FIELD_MIN_CELL_PX = 4.0
_COLOR_FIELD_MIN_AREA_PX = 64
# 毛刺剪枝：短分支长度还必须 ≥ 该比例 × 交汇处局部 DT（阈值来自局部尺度，R3.1）
_SPUR_LEN_PER_RADIUS = 1.2
# 分支串接：共享端点两分支外向方向夹角余弦上限（越接近 −1 越共线）
_MERGE_BRANCH_COS = 0.85
# 尖角补支/端点延伸的结构量门槛：只对块状形状做（蔓延细线网络不需要，且会引入伪毛刺）
_CORNER_MAX_SPRAWL = 4.0
# 缺口桥接：共线判据余弦下限（由"两端外向切向都要指向对方"导出）
_BRIDGE_COS_MIN = 0.72


# ================================================================ 坐标
def _to_world(pts_px: np.ndarray, long_side: int) -> np.ndarray:
    """像素索引坐标（整数=像素中心）→ 世界坐标（CONTRACT §1：world=(i+0.5)/L）。"""
    return (np.asarray(pts_px, dtype=np.float64) + 0.5) / float(long_side)


def _lattice_to_world(pts_lat: np.ndarray, long_side: int) -> np.ndarray:
    """像素边格点坐标（像素 i 覆盖 [i,i+1]）→ 世界坐标（world = lattice / L）。"""
    return np.asarray(pts_lat, dtype=np.float64) / float(long_side)


# ================================================================ 细化（Zhang-Suen）
_NB8 = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))


def _neighbour_stack(img: np.ndarray) -> tuple[np.ndarray, ...]:
    """返回 8 个方向的邻居视图（顺序：N, NE, E, SE, S, SW, W, NW）。"""
    p = np.pad(img, 1, mode="constant", constant_values=False)
    h, w = img.shape
    return (
        p[0:h, 1:w + 1],
        p[0:h, 2:w + 2],
        p[1:h + 1, 2:w + 2],
        p[2:h + 2, 2:w + 2],
        p[2:h + 2, 1:w + 1],
        p[2:h + 2, 0:w],
        p[1:h + 1, 0:w],
        p[0:h, 0:w],
    )


def skeletonize(mask: np.ndarray, max_iter: int) -> np.ndarray:
    """Zhang-Suen 细化，返回 1px 骨架。

    `max_iter` 由调用方按 `ceil(maxDT)+2` 从数据导出（每轮最多削掉约 1px 边界），
    不做写死上限。
    """
    img = np.asarray(mask, dtype=np.uint8)
    if img.size == 0:
        return np.zeros_like(img, dtype=bool)
    img = img.copy()
    for _ in range(max(1, int(max_iter))):
        changed = False
        for step in (0, 1):
            n_, ne, e_, se, s_, sw, w_, nw = _neighbour_stack(img)
            seq = (n_, ne, e_, se, s_, sw, w_, nw)
            b = sum(seq)
            a = sum(
                ((seq[i] == 0) & (seq[(i + 1) % 8] == 1)).astype(np.uint8)
                for i in range(8)
            )
            if step == 0:
                c1 = (n_ * e_ * s_) == 0
                c2 = (e_ * s_ * w_) == 0
            else:
                c1 = (n_ * e_ * w_) == 0
                c2 = (n_ * s_ * w_) == 0
            cond = ((img == 1) & (b >= 2) & (b <= 6) & (a == 1) & c1 & c2)
            if bool(cond.any()):
                img[cond] = 0
                changed = True
        if not changed:
            break
    return img.astype(bool)


def _build_neighbour_table() -> np.ndarray:
    """256 种 8 邻域掩码 → 删除中心点后其 8 邻域前景是否仍 8 连通。

    注意：这里只做**局部**连通性判据（C8）。删除一个「2×2 块恰 3 像素」的角点
    在 8 邻域意义下总是不破坏局部连通，但它仍可能切断全局细线，
    因此 `clean_skeleton` 每相位删除后再做一次**全局连通域数**校验。
    """
    tab = np.zeros(256, dtype=bool)
    for m in range(256):
        cells = [_NB8[b] for b in range(8) if (m >> b) & 1]
        if len(cells) <= 1:
            tab[m] = True
            continue
        cs = set(cells)
        seen = {cells[0]}
        stack = [cells[0]]
        while stack:
            cy, cx = stack.pop()
            for dy, dx in _NB8:
                q = (cy + dy, cx + dx)
                if q in cs and q not in seen:
                    seen.add(q)
                    stack.append(q)
        tab[m] = len(seen) == len(cs)
    return tab


_NEIGHBOUR_TABLE = _build_neighbour_table()


def _n_components_bool(mask: np.ndarray) -> int:
    from .util import label_components as _lc

    return int(_lc(mask)[1])


def clean_skeleton(sk: np.ndarray) -> np.ndarray:
    """清理 Zhang-Suen 留下的阶梯状伪交叉点（2×2 块恰 3 像素 → 删对角那个）。

    判据是「简单点」的**局部**版本 + **全局连通性守卫**（每相位删除后校验
    连通域数不增加）。前者保证局部连通，后者防止并行删除/细线夹点把一条
    光滑笔画切断（实测只做局部判据会把 T4 锥形笔画的细尾切成 24 段）。
    9 个相位并行删除：同一相位内邻域互不重叠，等价于顺序删除但全向量化。
    """
    s = np.asarray(sk, dtype=bool).copy()
    h, w = s.shape
    if h < 2 or w < 2:
        return s
    max_rounds = max(1, min(int(s.sum()), 64))
    for _ in range(max_rounds):
        changed = False
        cnt = (
            s[:-1, :-1].astype(np.uint8) + s[:-1, 1:].astype(np.uint8)
            + s[1:, :-1].astype(np.uint8) + s[1:, 1:].astype(np.uint8)
        )
        three = cnt == 3
        if not bool(three.any()):
            break
        cand = np.zeros_like(s)
        cand[:-1, :-1] |= three & (~s[1:, 1:])
        cand[:-1, 1:] |= three & (~s[1:, :-1])
        cand[1:, :-1] |= three & (~s[:-1, 1:])
        cand[1:, 1:] |= three & (~s[:-1, :-1])
        cand &= s
        if not bool(cand.any()):
            break
        p = np.pad(s, 1, mode="constant", constant_values=False)
        mask = np.zeros(s.shape, dtype=np.uint8)
        for b, (dy, dx) in enumerate(_NB8):
            mask |= (
                p[1 + dy:1 + dy + h, 1 + dx:1 + dx + w].astype(np.uint8) << b
            )
        sel = cand & _NEIGHBOUR_TABLE[mask]
        if not bool(sel.any()):
            break
        n_comp0 = _n_components_bool(s)
        for ph in range(9):
            phy, phx = divmod(ph, 3)
            sub = np.zeros(s.shape, dtype=bool)
            sub[phy::3, phx::3] = True
            ok = sel & sub
            if not bool(ok.any()):
                continue
            trial = s & ~ok
            if _n_components_bool(trial) > n_comp0:
                continue  # 全局连通性被破坏：整个相位放弃（保住细线）
            s = trial
            changed = True
        if not changed:
            break
    return s


# ================================================================ 骨架图 → 分支
def _neighbours(p: tuple[int, int], pix: set[tuple[int, int]]) -> list[tuple[int, int]]:
    y, x = p
    out = []
    for dy, dx in _NB8:
        q = (y + dy, x + dx)
        if q in pix:
            out.append(q)
    return out


def skeleton_graph(skel: np.ndarray) -> list[list[tuple[int, int]]]:
    """把 1px 骨架按「端点(度=1) / 交汇点(度≠2)」切分成有序像素路径列表。

    纯环（所有点度=2）单独成一条闭环分支。
    """
    ys, xs = np.nonzero(skel)
    pix = set(zip(ys.tolist(), xs.tolist()))
    if not pix:
        return []
    nbrs = {p: _neighbours(p, pix) for p in pix}
    deg = {p: len(v) for p, v in nbrs.items()}
    nodes = {p for p, d in deg.items() if d != 2}
    visited: set[tuple[tuple[int, int], tuple[int, int]]] = set()

    def edge(a: tuple[int, int], b: tuple[int, int]):
        return (a, b) if a <= b else (b, a)

    branches: list[list[tuple[int, int]]] = []
    for node in nodes:
        for nb in nbrs[node]:
            e = edge(node, nb)
            if e in visited:
                continue
            path = [node, nb]
            visited.add(e)
            prev, cur = node, nb
            while deg[cur] == 2:
                nxt = [q for q in nbrs[cur] if q != prev]
                if not nxt:
                    break
                nxt_p = nxt[0]
                visited.add(edge(cur, nxt_p))
                path.append(nxt_p)
                prev, cur = cur, nxt_p
            if len(path) >= 2:
                branches.append(path)
    # 纯环
    for p in pix:
        if deg[p] != 2:
            continue
        if all(edge(p, q) in visited for q in nbrs[p]):
            continue
        path = [p]
        prev, cur = None, p
        while True:
            cand = [q for q in nbrs[cur] if q != prev]
            if not cand:
                break
            nxt_p = cand[0]
            e = edge(cur, nxt_p)
            if e in visited:
                break
            visited.add(e)
            path.append(nxt_p)
            prev, cur = cur, nxt_p
            if nxt_p == p:
                break
        if len(path) >= 3:
            branches.append(path)
    return branches


def _branch_length(path: list[tuple[int, int]]) -> float:
    if len(path) < 2:
        return 0.0
    a = np.asarray(path, dtype=np.float64)
    return float(np.linalg.norm(np.diff(a, axis=0), axis=1).sum())


def _branch_ink_mass(path: list[tuple[int, int]], dt: np.ndarray) -> float:
    """短分支的近似墨量（px²）：长度 × 中位全宽（2×DT 的局部中位）。

    用于区分"真实短笔触/排线/点"与"只剩一两个像素的 AA 噪声"：前者应保留，
    后者丢弃。DT 取分支像素上的值（裁切局部坐标）。
    """
    a = np.asarray(path, dtype=np.float64)
    if len(a) < 2:
        return 0.0
    length = float(np.linalg.norm(np.diff(a, axis=0), axis=1).sum())
    ys = np.clip(np.rint(a[:, 1]).astype(np.int64), 0, dt.shape[0] - 1)
    xs = np.clip(np.rint(a[:, 0]).astype(np.int64), 0, dt.shape[1] - 1)
    width = float(np.median(2.0 * np.maximum(dt[ys, xs], 0.5)))
    return float(length * width)


# ================================================================ 轮廓（像素边链）
_DIRS_CW = ((1, 0), (0, 1), (-1, 0), (0, -1))  # 屏幕上顺时针次序


def trace_boundary_loops(mask: np.ndarray) -> list[np.ndarray]:
    """追踪二值区域的**像素边**闭合回路（格点坐标，像素 i 覆盖 [i,i+1]）。

    相比 Moore 邻域追踪：结果天然闭合、无重复起点、不需要"走不完重试"，
    并且多边形就在像素边缘 —— 等价于旧版「内边界像素中心 + 外扩 0.5px」，
    正是 50% 覆盖位置。8 连通对角接触的"夹点"处取**最右转**保证回路不自交。
    """
    m = np.pad(np.asarray(mask, dtype=bool), 1, mode="constant", constant_values=False)
    h, w = m.shape
    if not bool(m.any()):
        return []
    out_edges: dict[tuple[int, int], list[tuple[int, int]]] = {}
    ys, xs = np.nonzero(m)
    for y, x in zip(ys.tolist(), xs.tolist()):
        # 格点坐标：cell (y,x) 占 [x, x+1]×[y, y+1]，行走方向让前景在右侧
        if not m[y - 1, x]:
            out_edges.setdefault((x, y), []).append((x + 1, y))
        if not m[y + 1, x]:
            out_edges.setdefault((x + 1, y + 1), []).append((x, y + 1))
        if not m[y, x - 1]:
            out_edges.setdefault((x, y + 1), []).append((x, y))
        if not m[y, x + 1]:
            out_edges.setdefault((x + 1, y), []).append((x + 1, y + 1))
    loops: list[np.ndarray] = []
    starts = list(out_edges.keys())
    for start in starts:
        while out_edges.get(start):
            path = [start]
            cur = start
            prev = None
            closed = False
            for _ in range(m.size * 4 + 8):
                cands = out_edges.get(cur, [])
                if not cands:
                    break
                if len(cands) == 1:
                    nxt = cands.pop(0)
                else:
                    # 夹点：选**最右转**（屏幕顺时针的下一个方向优先），让回路贴着前景走。
                    # 优先级：右转(rank1) > 直行(rank0) > 左转(rank3) > 掉头(rank2)。
                    if prev is None:
                        nxt = cands.pop(0)
                    else:
                        d = (cur[0] - prev[0], cur[1] - prev[1])
                        base = _DIRS_CW.index(d) if d in _DIRS_CW else 0
                        pref = {1: 0, 0: 1, 3: 2, 2: 3}
                        best_i, best_rank = 0, None
                        for idx, cand in enumerate(cands):
                            nd = (cand[0] - cur[0], cand[1] - cur[1])
                            if nd not in _DIRS_CW:
                                continue
                            rank = (_DIRS_CW.index(nd) - base) % 4
                            if best_rank is None or pref[rank] < best_rank:
                                best_rank, best_i = pref[rank], idx
                        nxt = cands.pop(best_i)
                path.append(nxt)
                prev, cur = cur, nxt
                if cur == start:
                    closed = True
                    break
            if closed and len(path) >= 5:
                # 去掉 pad 造成的 1px 平移：padded 像素 (y,x) 对应原始像素 (y-1,x-1)，
                # 其格点坐标为 [x-1, x]×[y-1, y]（原始像素 i 覆盖 [i,i+1]）。
                loops.append(np.asarray(path[:-1], dtype=np.float64) - 1.0)
            elif not closed and len(path) >= 5:
                # 极少数夹点组合可能闭合到另一个起点；闭合性无法保证就不采用
                # （宁缺勿凑：填充轮廓不闭合时跳过，绝不填出碎片）
                continue
    return loops


def _signed_area(poly: np.ndarray) -> float:
    x = poly[:, 0]
    y = poly[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _simplify_closed(pts: np.ndarray, eps: float) -> np.ndarray:
    """对闭合多边形做 Douglas-Peucker 简化（保持闭合、去共线冗余）。"""
    p = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
    if len(p) <= 3 or eps <= 0.0:
        return p
    ext = np.vstack([p, p[:1]])
    keep = np.zeros(len(ext), dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(ext) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        a, b = ext[i], ext[j]
        ab = b - a
        l2 = float(ab @ ab)
        seg = ext[i + 1:j]
        if l2 < _EPS_LEN:
            d = np.linalg.norm(seg - a, axis=1)
        else:
            t = np.clip(((seg - a) @ ab) / l2, 0.0, 1.0)
            d = np.linalg.norm(seg - (a + t[:, None] * ab), axis=1)
        k = int(np.argmax(d))
        if float(d[k]) > eps:
            m = i + 1 + k
            keep[m] = True
            stack.append((i, m))
            stack.append((m, j))
    out = ext[keep]
    return out[:-1]  # 去掉重复的闭合点


def _drop_collinear(pts: np.ndarray) -> np.ndarray:
    """去掉三点共线的中间点（简化后常见，减少顶点数）。"""
    p = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
    if len(p) <= 3:
        return p
    keep = np.ones(len(p), dtype=bool)
    n = len(p)
    for i in range(n):
        a = p[(i - 1) % n]
        b = p[i]
        c = p[(i + 1) % n]
        ab = b - a
        bc = c - b
        cross = abs(ab[0] * bc[1] - ab[1] * bc[0])
        if cross <= 1e-9 and float(ab @ bc) >= 0.0:
            keep[i] = False
    if int(keep.sum()) < 3:
        return p
    return p[keep]


# ================================================================ 尖角补支 / 端点延伸
def _detect_corners(loop: np.ndarray, turn_deg: float, baseline_frac: float) -> np.ndarray:
    """在闭合多边形上用「按周长比例取基线」的转角判据检测尖角索引。"""
    n = len(loop)
    if n < 12:
        return np.zeros(0, dtype=np.int64)
    k = max(2, int(n * baseline_frac))
    idx = np.arange(n)
    a = loop[(idx - k) % n]
    b = loop[idx]
    c = loop[(idx + k) % n]
    v1 = a - b
    v2 = c - b
    n1 = np.linalg.norm(v1, axis=1)
    n2 = np.linalg.norm(v2, axis=1)
    ok = (n1 > _EPS_LEN) & (n2 > _EPS_LEN)
    cosang = np.ones(n)
    cosang[ok] = np.clip((v1[ok] * v2[ok]).sum(axis=1) / (n1[ok] * n2[ok]), -1.0, 1.0)
    turn = np.pi - np.arccos(cosang)
    turn[~ok] = 0.0
    cand = np.flatnonzero(turn > np.deg2rad(turn_deg))
    if cand.size == 0:
        return np.zeros(0, dtype=np.int64)
    order = cand[np.argsort(-turn[cand])]
    chosen: list[int] = []
    for i in order:
        if all(
            min(abs(int(i) - c2), n - abs(int(i) - c2)) > 2 * k for c2 in chosen
        ):
            chosen.append(int(i))
    return np.asarray(chosen, dtype=np.int64)


def add_corner_branches(
    skel: np.ndarray,
    mask: np.ndarray,
    dt: np.ndarray,
) -> np.ndarray:
    """补出真实中轴的尖角分支（Zhang-Suen 是拓扑细化，实心凸形会被缩成一点）。

    做法：追踪边界 → 检测尖角 → 沿直线连到最近的骨架点；宽度由 DT 自然给出。
    双重过滤避免把圆头端帽误判成尖角：转角 > 阈值 且 尖角到骨架点距离 > 1.2×该处 DT。
    """
    loops = trace_boundary_loops(mask)
    if not loops:
        return skel
    # 只处理外轮廓（面积最大的回路）
    outer = max(loops, key=lambda p: abs(_signed_area(p)))
    pts = outer
    idxs = _detect_corners(pts, _CORNER_TURN_DEG, _CORNER_BASELINE_FRAC)
    if idxs.size == 0:
        return skel
    sk_ys, sk_xs = np.nonzero(skel)
    if sk_ys.size == 0:
        return skel
    # 格点 → 像素索引坐标（整数 = 像素中心）：pixel = lattice - 0.5
    sk_pts = np.stack([sk_xs, sk_ys], axis=1).astype(np.float64)
    out = skel.copy()
    h, w = skel.shape
    max_branches = min(48, max(1, int(pts.__len__() // 8)))  # 由轮廓长度导出
    added = 0
    for i in idxs:
        if added >= max_branches:
            break
        b = pts[i] - 0.5  # 像素索引坐标
        d2 = ((sk_pts - b[None, :]) ** 2).sum(axis=1)
        j = int(np.argmin(d2))
        target = sk_pts[j]
        dist = float(np.linalg.norm(target - b))
        dt_j = float(dt[int(round(target[1])), int(round(target[0]))])
        if dist < max(2.5, 1.2 * dt_j):
            continue
        added += 1
        steps = int(np.ceil(dist * 2.0)) + 1
        for s_i in range(steps + 1):
            u = s_i / float(max(steps, 1))
            py = b[1] + (target[1] - b[1]) * u
            px = b[0] + (target[0] - b[0]) * u
            iy, ix = int(round(py)), int(round(px))
            if 0 <= iy < h and 0 <= ix < w:
                out[iy, ix] = True
    return out


def _contact_clusters_ok(
    p: np.ndarray,
    dt_p: float,
    boundary: np.ndarray,
    ann_ratio: float = 0.02,
    gap_deg: float = 70.0,
) -> bool:
    """p 处「等距最近边界点」是否分成 ≥2 个角向簇（真实中轴点/尖角满足）。"""
    ann = max(0.7, ann_ratio * dt_p)
    y0 = max(0, int(np.floor(p[0] - dt_p - ann)))
    y1 = min(boundary.shape[0], int(np.ceil(p[0] + dt_p + ann)) + 1)
    x0 = max(0, int(np.floor(p[1] - dt_p - ann)))
    x1 = min(boundary.shape[1], int(np.ceil(p[1] + dt_p + ann)) + 1)
    if y1 <= y0 or x1 <= x0:
        return False
    ys, xs = np.nonzero(boundary[y0:y1, x0:x1])
    if ys.size < 2:
        return False
    dy = ys + y0 - p[0]
    dx = xs + x0 - p[1]
    r = np.sqrt(dx * dx + dy * dy)
    sel = np.abs(r - dt_p) <= ann
    if int(sel.sum()) < 2:
        return False
    ang = np.sort(np.arctan2(dy[sel], dx[sel]))
    gaps = np.diff(np.concatenate([ang, [ang[0] + 2.0 * np.pi]]))
    return int(np.count_nonzero(gaps > np.deg2rad(gap_deg))) >= 2


def extend_to_medial_ends(
    skel: np.ndarray,
    mask: np.ndarray,
    dt: np.ndarray,
) -> np.ndarray:
    """把细化骨架的自由端延伸到真实中轴端点（尖角），避免填充区尖角被圆化。"""
    h, w = skel.shape
    boundary = mask & ~erode(mask, 1)
    branches = skeleton_graph(skel)
    out = skel.copy()
    for b in branches:
        if len(b) < 3:
            continue
        for ei in (0, -1):
            base = b[ei]
            ref = b[min(8, len(b) - 1)] if ei == 0 else b[max(-9, -len(b))]
            v = np.array([base[0] - ref[0], base[1] - ref[1]], dtype=np.float64)
            nv = float(np.linalg.norm(v))
            if nv < _EPS_LEN:
                continue
            v /= nv
            dt_here = float(dt[base[0], base[1]])
            if dt_here < 3.0:
                continue
            limit = int(dt_here * 4.0 * 2.0)  # 由局部 DT 导出，不写死步数
            cur = np.array([float(base[0]), float(base[1])])
            for _ in range(limit):
                cur = cur + v * 0.5
                iy, ix = int(round(cur[0])), int(round(cur[1]))
                if not (0 <= iy < h and 0 <= ix < w) or not mask[iy, ix]:
                    break
                d0 = float(dt[iy, ix])
                if d0 < 0.6:
                    break
                if not _contact_clusters_ok(cur, d0, boundary):
                    break
                out[iy, ix] = True
    return out


# ================================================================ 分支后处理
def bridge_branch_gaps(
    branches: list[list[tuple[int, int]]],
    max_gap: float,
    cos_min: float = _BRIDGE_COS_MIN,
) -> tuple[list[list[tuple[int, int]]], int]:
    """把「本是同一根线、却被逐点阈值切断」的碎段接起来（分支图层的零副作用修法）。

    条件：端点距离 ≤ max_gap、两端外向切向都指向对方（余弦 ≥ cos_min）、不同分支。
    按距离贪心匹配，每个端点最多用一次（避免交叉连接）。
    """
    nb = len(branches)
    if nb < 2 or max_gap <= 0.0:
        return branches, 0
    epts: list[tuple[int, int, np.ndarray, np.ndarray]] = []
    for i, b in enumerate(branches):
        if len(b) < 2:
            continue
        a = np.asarray(b, dtype=np.float64)
        k = min(3, len(a) - 1)
        for end, p, q in ((0, a[0], a[k]), (1, a[-1], a[-1 - k])):
            v = p - q
            nv = float(np.linalg.norm(v))
            if nv > _EPS_LEN:
                epts.append((i, end, p, v / nv))
    m = len(epts)
    if m < 2:
        return branches, 0
    P = np.stack([e[2] for e in epts])
    D = np.stack([e[3] for e in epts])
    BID = np.array([e[0] for e in epts], dtype=np.int64)
    cand: list[tuple[float, int, int]] = []
    for i in range(m):
        dv = P - P[i]
        dd = np.linalg.norm(dv, axis=1)
        ok = (dd > 1e-6) & (dd <= max_gap) & (BID != BID[i])
        if not bool(ok.any()):
            continue
        u = dv / np.maximum(dd, 1e-9)[:, None]
        ok &= ((u * D[i][None, :]).sum(axis=1) >= cos_min)
        ok &= ((-u * D).sum(axis=1) >= cos_min)
        for j in np.nonzero(ok)[0]:
            cand.append((float(dd[j]), i, int(j)))
    if not cand:
        return branches, 0
    cand.sort()
    used = np.zeros(m, dtype=bool)
    link: dict[int, list[tuple[int, int, int]]] = {}
    n_link = 0
    max_links = max(1, int(m // 2))  # 每条端点最多用一次 → 由端点总数导出
    for _d, i, j in cand:
        if used[i] or used[j] or n_link >= max_links:
            continue
        used[i] = used[j] = True
        n_link += 1
        link.setdefault(int(BID[i]), []).append((epts[i][1], epts[j][1], int(BID[j])))
        link.setdefault(int(BID[j]), []).append((epts[j][1], epts[i][1], int(BID[i])))
    if n_link == 0:
        return branches, 0
    visited = [False] * nb
    out: list[list[tuple[int, int]]] = []

    def walk(start: int, entry_end: int) -> None:
        chain: list[tuple[int, int]] = []
        bi, ee = start, entry_end
        while not visited[bi]:
            visited[bi] = True
            seq = branches[bi] if ee == 0 else branches[bi][::-1]
            chain.extend(seq if not chain else seq[1:])
            ex = 1 - ee
            nxt = None
            for this_end, other_end, other_b in link.get(bi, ()):
                if this_end == ex and not visited[other_b]:
                    nxt = (other_b, other_end)
                    break
            if nxt is None:
                break
            bi, ee = nxt
        if len(chain) >= 2:
            out.append(chain)

    for i in range(nb):
        if not visited[i] and len(link.get(i, ())) <= 1:
            free = 0
            if link.get(i):
                free = 1 - link[i][0][0]
            walk(i, free)
    for i in range(nb):
        if not visited[i]:
            walk(i, 0)
    return out, n_link


def prune_branches(
    branches: list[list[tuple[int, int]]],
    dt: np.ndarray,
    cfg: FBSConfig,
) -> tuple[list[list[tuple[int, int]]], int]:
    """剪枝毛刺分支。阈值按**局部 DT**（junc 端的内切半径）与配置的 px 尺度导出。

    corner_preserve=True 时只按 `min_branch_length_px` 剪（保尖角，避免过度圆化）；
    否则 additionally 要求长度 ≥ prune_threshold × 局部半径。
    """
    if not branches:
        return branches, 0
    dt_max = float(dt.max()) if dt.size else 1.0
    min_len = float(cfg.min_branch_length_px)
    removed_total = 0
    max_rounds = max(1, len(branches))  # 收敛上限由数据导出
    for _ in range(max_rounds):
        ends: dict[tuple[int, int], int] = {}
        for b in branches:
            ends[b[0]] = ends.get(b[0], 0) + 1
            ends[b[-1]] = ends.get(b[-1], 0) + 1
        kept: list[list[tuple[int, int]]] = []
        removed = 0
        for b in branches:
            terminal = (ends.get(b[0], 0) == 1) or (ends.get(b[-1], 0) == 1)
            if not terminal:
                kept.append(b)
                continue
            length = _branch_length(b)
            free_end = b[0] if ends.get(b[0], 0) == 1 else b[-1]
            junc_end = b[-1] if free_end == b[0] else b[0]
            r_j = float(dt[junc_end[0], junc_end[1]])
            # 毛刺判据：长度必须同时 ≥ 「绝对最小长度」与「局部半径的 1.2 倍」。
            # 只按绝对长度会放过粗区上的阶梯毛刺（T4 锥形笔画被切成 10 段就是这个原因）；
            # 局部半径来自 DT，符合 R3.1「阈值量纲来自局部尺度」。
            too_short = length < min_len or length < _SPUR_LEN_PER_RADIUS * max(r_j, 1.0)
            if not cfg.corner_preserve:
                too_short = too_short or length < cfg.prune_threshold * max(r_j, 1.0)
            if too_short and length < 0.6 * dt_max:
                removed += 1
                continue
            kept.append(b)
        branches = kept
        removed_total += removed
        if removed == 0:
            break
    return branches, removed_total


def _branch_direction(path: list[tuple[int, int]], end: int, k: int = 4) -> np.ndarray:
    """分支端点处「朝外」的单位方向。"""
    a = np.asarray(path, dtype=np.float64)
    if end == 0:
        v = a[0] - a[min(k, len(a) - 1)]
    else:
        v = a[-1] - a[max(-k - 1, -len(a))]
    nv = float(np.linalg.norm(v))
    return v / nv if nv > _EPS_LEN else np.zeros(2, dtype=np.float64)


def merge_collinear_branches(
    branches: list[list[tuple[int, int]]],
    cos_min: float = 0.85,
) -> tuple[list[list[tuple[int, int]]], int]:
    """把「同一条光滑笔画被伪交汇点/短毛刺切开」的相邻分支串接回一条。

    判据：两分支共享同一个端点、该端点恰好只连两条（真交汇点有 ≥3 条，不合并）、
    且两者在该点的外向方向近似相反（夹角余弦 ≤ −cos_min）。
    这是 T4「一条锥形笔画应只有 1 段」的直接修法；对真实图的细线碎片同样有效
    （端点越少 → 缺口/断线越少）。
    """
    br = [list(b) for b in branches if len(b) >= 2]
    if len(br) < 2:
        return br, 0
    n_merged = 0
    for _ in range(len(br) + 1):
        end_map: dict[tuple[int, int], list[tuple[int, int]]] = {}
        for i, b in enumerate(br):
            end_map.setdefault(b[0], []).append((i, 0))
            end_map.setdefault(b[-1], []).append((i, 1))
        merged = False
        for _p, lst in end_map.items():
            if len(lst) != 2:
                continue
            (i, ei), (j, ej) = lst
            if i == j:
                continue
            di = _branch_direction(br[i], ei)
            dj = _branch_direction(br[j], ej)
            if di.size == 0 or dj.size == 0:
                continue
            if float(di @ dj) > -cos_min:
                continue
            a = br[i] if ei == 1 else br[i][::-1]   # a 以共享端点结尾
            b = br[j] if ej == 0 else br[j][::-1]   # b 以共享端点开头
            chain = a + b[1:]
            for idx in sorted((i, j), reverse=True):
                br.pop(idx)
            br.append(chain)
            n_merged += 1
            merged = True
            break
        if not merged:
            break
    return br, n_merged


def _disk_union(branches: list[list[tuple[int, int]]], dt: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """按圆盘并集（MAT 重建语义）计算分支集合的覆盖掩码（局部坐标）。"""
    h, w = shape
    cov = np.zeros((h, w), dtype=bool)
    for b in branches:
        for iy, ix in b:
            r = float(dt[iy, ix])
            if r < 0.5:
                continue
            y0 = max(0, int(iy - r) - 1)
            y1 = min(h, int(iy + r) + 2)
            x0 = max(0, int(ix - r) - 1)
            x1 = min(w, int(ix + r) + 2)
            if y1 <= y0 or x1 <= x0:
                continue
            yy, xx = np.mgrid[y0:y1, x0:x1]
            cov[y0:y1, x0:x1] |= ((yy - iy) ** 2 + (xx - ix) ** 2) <= r * r
    return cov


def repair_coverage(
    comp: np.ndarray,
    dt: np.ndarray,
    branches: list[list[tuple[int, int]]],
) -> tuple[list[list[tuple[int, int]]], int]:
    """对圆盘并集未覆盖的区域残差做迭代补笔。

    细化不是完整中轴：复杂/锯齿边界上圆盘并集盖不满整个区域，会留下成片空白。
    做法：算残差 → 对残差细化 → 用**原区域 DT**（真实半宽）生成补充笔画 → 迭代。
    轮数与残差判据都由数据导出（残差像素数 / 总像素数）。
    """
    total = int(comp.sum())
    if total == 0:
        return branches, 0
    added_total = 0
    cur = [list(b) for b in branches]
    max_rounds = max(1, int(math.ceil(math.log2(max(total, 2)))))  # ≤ 22 轮，实际 1–3 轮收敛
    for _ in range(max_rounds):
        cov = _disk_union(cur, dt, comp.shape)
        residual = comp & ~cov
        rn = int(residual.sum())
        if rn < max(30, int(_REPAIR_MIN_RESIDUAL_FRAC * total)):
            break
        dt_res = distance_transform(residual)
        max_iter = int(np.ceil(float(dt_res.max()))) + 2 if dt_res.size else 2
        skel_r = clean_skeleton(skeletonize(residual, max_iter))
        if not bool(skel_r.any()):
            break
        new_branches = [b for b in skeleton_graph(skel_r) if _branch_length(b) >= 1.5]
        if not new_branches:
            break
        cur.extend(new_branches)
        added_total += len(new_branches)
    return cur, added_total


# ================================================================ 几何工具
def _resample_path(pts: np.ndarray, density: float) -> np.ndarray:
    """按弧长均匀重采样（点数 = ceil(弧长×密度)，由数据导出）。"""
    p = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
    if len(p) < 2:
        return p
    seg = np.linalg.norm(np.diff(p, axis=0), axis=1)
    total = float(seg.sum())
    if total <= _EPS_LEN:
        return np.repeat(p[:1], 2, axis=0)
    n = int(max(3, math.ceil(total * float(density)) + 1))
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    u = np.linspace(0.0, total, n)
    return np.stack(
        [np.interp(u, cum, p[:, 0]), np.interp(u, cum, p[:, 1])], axis=1
    )


def _tangent_normal(pts: np.ndarray, smooth: int = 5) -> tuple[np.ndarray, np.ndarray]:
    """(x,y) 路径的单位切向与法向（切向先做移动平均，抑制像素量化抖动）。"""
    p = np.asarray(pts, dtype=np.float64)
    if len(p) >= smooth and smooth >= 3:
        k = int(smooth)
        pad = k // 2
        ext = np.pad(p, ((pad, pad), (0, 0)), mode="edge")
        cs = np.cumsum(np.vstack([np.zeros((1, 2)), ext]), axis=0)
        p_s = (cs[k:] - cs[:-k]) / float(k)
    else:
        p_s = p
    t = np.gradient(p_s, axis=0)
    tn = np.linalg.norm(t, axis=1, keepdims=True)
    t = t / np.maximum(tn, 1e-9)
    nrm = np.stack([-t[:, 1], t[:, 0]], axis=1)
    return t, nrm


def _same_label_weight(
    lab_img: np.ndarray | None,
    xs: np.ndarray,
    ys: np.ndarray,
    lab_val: int | None,
) -> np.ndarray:
    """采样点处是否属于同一颜色标签（1/0）。用于把宽度积分/质心限制在本区域内。

    没有它时，紧邻另一色块的笔画法向射线会扫过邻区（邻区 cov 也是 1），
    把宽度系统性放大（实测 chibi 出现 37px 的"细线"）。

    **背景（label=-1）必须保留权重**：抗锯齿过渡带的像素往往落在掩码之外，
    但它们的覆盖率正是本笔画墨量的一部分；把它们一起排除会让宽度系统性偏小
    （实测 taper 宽度整体低 8%）。只有"另一块真实墨区"才需要排除。
    """
    if lab_img is None or lab_val is None:
        return np.ones_like(xs, dtype=np.float64)
    s = bilinear_sample(lab_img, xs, ys).astype(np.float64)
    # 注意用 <=：采样点恰好落在「本墨色 0.5 / 背景 0.5」时 s=-0.5，
    # 若用 < 会被判成非本人墨色而丢掉抗锯齿尾巴（实测 1px 线宽度 1.0→0.75）。
    same_ink = np.abs(s - float(lab_val)) <= 0.5 + 1e-6
    is_bg = s < -0.5
    return np.where(is_bg, 1.0, same_ink.astype(np.float64))


def refine_axis_coverage(
    cov: np.ndarray,
    pts_xy: np.ndarray,
    half_hint: np.ndarray,
    iters: int = _REFINE_ITERS,
    max_shift: float = _REFINE_MAX_SHIFT_PX,
    lab_img: np.ndarray | None = None,
    lab_val: int | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """把中心线吸附到覆盖率的**法向质量重心**（亚像素），带单峰性/完整截面判据。

    cov: (H,W) 全局覆盖率场；pts_xy: (n,2) 全局像素索引坐标（整数=像素中心）。
    三条准入判据缺一不可：
      ① 含中心的连通支撑段不得触到窗口边界（完整看见横截面）；
      ② 中心本身在支撑内；
      ③ 截面必须**单峰**（高于峰值一半的区段数 ≤1）—— 平行双线的谷底经插值后
         仍可能高于任何固定阈值，「连通段唯一」不足以排除它（质心会落到两线中间）。
    给了 lab_img/lab_val 时只统计同色标签的覆盖（防止质心被邻区拉走）。
    返回 (精修后的点, 是否采纳过掩码)。
    """
    p = np.asarray(pts_xy, dtype=np.float64).reshape(-1, 2).copy()
    n = len(p)
    if cov.size == 0 or n < 2:
        return p, np.zeros(n, dtype=bool)
    hint = np.asarray(half_hint, dtype=np.float64).reshape(-1)
    if hint.size != n:
        hint = np.full(n, 2.0, dtype=np.float64)
    span = np.clip(hint + _COV_MARGIN_PX, 2.0, hint * _COV_SPAN_FACTOR + _COV_MARGIN_PX * 2.0)
    m2 = int(np.ceil(float(span.max()) / _COV_STEP_PX))
    s = np.arange(-m2, m2 + 1, dtype=np.float64) * _COV_STEP_PX
    center = m2
    adopted = np.zeros(n, dtype=bool)
    for _ in range(max(1, int(iters))):
        _t, nrm = _tangent_normal(p)
        P = p[:, None, :] + nrm[:, None, :] * s[None, :, None]
        C = bilinear_sample(cov, P[..., 0], P[..., 1]).astype(np.float64)
        C = np.clip(C, 0.0, 1.0)
        C = C * _same_label_weight(lab_img, P[..., 0], P[..., 1], lab_val)
        C = np.where(np.abs(s)[None, :] <= span[:, None], C, 0.0)
        support = C > _COV_SUPPORT
        run_r = np.logical_and.accumulate(support[:, center:], axis=1)
        run_l = np.logical_and.accumulate(support[:, :center + 1][:, ::-1], axis=1)[:, ::-1]
        run = np.concatenate([run_l[:, :center], run_r], axis=1)
        peak = (C * run).max(axis=1)
        hi = run & (C > _COV_HUMP_FRAC * np.maximum(peak, 1e-9)[:, None])
        n_hump = (hi[:, 1:] & ~hi[:, :-1]).sum(axis=1)
        ok = (
            (~run[:, 0]) & (~run[:, -1]) & support[:, center]
            & (n_hump <= 1) & (peak > _COV_SUPPORT)
        )
        mass = (C * run).sum(axis=1) * _COV_STEP_PX
        ok &= mass >= _COV_MIN_MASS_PX
        with np.errstate(invalid="ignore", divide="ignore"):
            centroid = (
                (C * run * s[None, :]).sum(axis=1) * _COV_STEP_PX
                / np.maximum(mass, 1e-9)
            )
        centroid = np.where(ok, np.clip(centroid, -max_shift, max_shift), 0.0)
        if not bool(ok.any()):
            break
        p = p + centroid[:, None] * nrm
        adopted |= ok
    return p, adopted


def measure_widths_coverage(
    cov: np.ndarray,
    pts_xy: np.ndarray,
    dt_hint: np.ndarray,
    lab_img: np.ndarray | None = None,
    lab_val: int | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """沿法向积分覆盖率测**全宽**（像素）：∫cov dn 即真实弦长/墨量。

    对厚区它是弦长（=2r），对亚像素细线它是墨量（=线宽）；不需要「截面是方波平台」
    的假设。积分窗口从 DT 提示 + 余量开始并迭代收缩（自我校正）。
    给了 lab_img/lab_val 时只积分同色标签的覆盖（防止邻区墨量被吞进宽度）。
    返回 (widths_px, 中心覆盖率 core_cov)。
    """
    p = np.asarray(pts_xy, dtype=np.float64).reshape(-1, 2)
    n = len(p)
    if n < 2 or cov.size == 0:
        return np.zeros(n, dtype=np.float64), np.zeros(n, dtype=np.float64)
    hint = np.clip(np.asarray(dt_hint, dtype=np.float64).reshape(-1), 0.5, None)
    _t, nrm = _tangent_normal(p)
    span = np.clip(
        hint + _COV_MARGIN_PX, 2.0, hint * _COV_SPAN_FACTOR + _COV_MARGIN_PX * 2.0
    )
    widths = np.zeros(n, dtype=np.float64)
    core_cov = np.zeros(n, dtype=np.float64)
    # 厚分支的弦长可达数十像素：按中心线点抽稀控制 n×S（纯性能控制，结果插值回全部点）
    max_span = float(span.max())
    m2 = int(np.ceil(max_span / _COV_STEP_PX))
    samples = 2 * m2 + 1
    stride = 1
    if n * samples > _COV_MAX_CELLS:
        stride = int(math.ceil(n * samples / float(_COV_MAX_CELLS)))
    sub = np.arange(0, n, stride)
    for _it in range(3):
        sub_span = span[sub]
        m2 = int(np.ceil(float(sub_span.max()) / _COV_STEP_PX))
        s = np.arange(-m2, m2 + 1, dtype=np.float64) * _COV_STEP_PX
        P = p[sub][:, None, :] + nrm[sub][:, None, :] * s[None, :, None]
        Q = np.clip(bilinear_sample(cov, P[..., 0], P[..., 1]).astype(np.float64), 0.0, 1.0)
        Q = Q * _same_label_weight(lab_img, P[..., 0], P[..., 1], lab_val)
        Q = np.where(np.abs(s)[None, :] <= sub_span[:, None], Q, 0.0)
        # 连通支撑：从中心向两侧连续 cov>阈值 的区段，断开处外侧不计入（防吞邻线墨量）
        center = m2
        gap = Q < _COV_SUPPORT
        gap_in = np.zeros_like(gap)
        gap_in[:, 1:] = gap[:, :-1]
        run = ~(gap & gap_in)
        run[:, center] = True
        w_int = (Q * run).sum(axis=1) * _COV_STEP_PX
        r_int = 0.5 * w_int
        span[sub] = np.clip(
            np.minimum(sub_span, r_int + _COV_MARGIN_PX),
            2.0,
            hint[sub] * _COV_SPAN_FACTOR + _COV_MARGIN_PX * 2.0,
        )
        widths_sub = w_int
        core_sub = Q[:, center]
        if stride > 1 and len(sub) >= 2:
            widths = np.interp(np.arange(n), sub.astype(np.float64), widths_sub)
            core_cov = np.interp(np.arange(n), sub.astype(np.float64), core_sub)
        else:
            widths = widths_sub
            core_cov = core_sub
    return widths, core_cov


def _ink_colors(
    pts_xy: np.ndarray,
    rgb: np.ndarray,
    cov: np.ndarray,
    bg: np.ndarray,
    labels: np.ndarray,
    palette: np.ndarray,
) -> np.ndarray:
    """沿中心线取墨色：先去混色（rgb = (1−cov)·bg + cov·ink → ink），再钳位。

    直接采样原图像素会把 AA 边缘的混色当成墨色（细线会被画淡一半）；
    去混色能用上 cov 携带的亚像素信息。cov 很小的点回退到该处调色板颜色。
    """
    n = len(pts_xy)
    x = pts_xy[:, 0]
    y = pts_xy[:, 1]
    rgb_s = bilinear_sample(rgb, x, y)
    if rgb_s.ndim == 1:  # 极端情形（单通道）防御
        rgb_s = np.stack([rgb_s] * 3, axis=1)
    cov_s = np.clip(bilinear_sample(cov, x, y).astype(np.float64), 0.0, 1.0)
    bg = np.asarray(bg, dtype=np.float64).reshape(3)
    denom = np.clip(cov_s, 0.25, 1.0)[:, None]
    c = bg[None, :] + (rgb_s - bg[None, :]) / denom
    h, w = labels.shape
    iy = np.clip(np.round(y).astype(np.int64), 0, h - 1)
    ix = np.clip(np.round(x).astype(np.int64), 0, w - 1)
    lab = labels[iy, ix]
    pal = np.asarray(palette, dtype=np.float64)
    if len(pal) > 0:
        lab_c = np.clip(lab, 0, len(pal) - 1)
        c_pal = pal[lab_c]
        use_pal = (lab < 0) | (cov_s < 0.05)
        c = np.where(use_pal[:, None], c_pal, c)
    return np.clip(c, 0.0, 1.0).astype(np.float64)


# ================================================================ element 构造
def _element(
    points: np.ndarray,
    closed: bool,
    fill_role: str,
    widths: np.ndarray,
    colors: np.ndarray,
    alphas: np.ndarray,
    fill_poly: np.ndarray | None,
    region_id: int,
    layer: int,
    meta: dict[str, Any],
    fill_holes: list[np.ndarray] | None = None,
) -> dict[str, Any]:
    """按 CONTRACT §3 组装 element（几何为世界坐标）。

    `fill_holes`：region_fill 的内部洞（世界坐标闭合环列表，不含重复首尾点）。
    渲染端先填外轮廓、再逐洞用背景色挖掉；没有洞时保持旧行为（None）。
    """
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    if closed:
        seg = np.linalg.norm(np.diff(np.vstack([pts, pts[:1]]), axis=0), axis=1)
    else:
        seg = np.linalg.norm(np.diff(pts, axis=0), axis=1) if len(pts) > 1 else np.zeros(0)
    total = float(seg.sum())
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    if closed:
        s = cum[: len(pts)] / max(total, _EPS_LEN)
    else:
        s = cum / max(total, _EPS_LEN) if total > _EPS_LEN else np.zeros(len(pts))
    return {
        "points": np.ascontiguousarray(pts, dtype=np.float64),
        "s": np.ascontiguousarray(s, dtype=np.float64),
        "closed": bool(closed),
        "fill_role": str(fill_role),
        "widths": np.ascontiguousarray(widths, dtype=np.float64).reshape(-1),
        "colors": np.ascontiguousarray(colors, dtype=np.float32).reshape(-1, 3),
        "alphas": np.ascontiguousarray(alphas, dtype=np.float32).reshape(-1),
        "shapes": None,
        "fill_poly": None if fill_poly is None else np.ascontiguousarray(fill_poly, dtype=np.float64).reshape(-1, 2),
        "fill_holes": (
            None
            if not fill_holes
            else [np.ascontiguousarray(h, dtype=np.float64).reshape(-1, 2) for h in fill_holes]
        ),
        "region_id": int(region_id),
        "layer": int(layer),
        "meta": meta,
    }


def _region_representative_color(
    comp: np.ndarray,
    rgb255: np.ndarray,
    alpha: np.ndarray,
    fallback_rgb01: np.ndarray,
    fallback_alpha: float,
) -> tuple[np.ndarray, float, dict[str, Any]]:
    """区域代表色：腐蚀内部 + **色度轴高分位**（中位数会把柔边腮红拉回背景色）。

    返回 (rgb 0..1, alpha, 统计信息)。
    """
    inner = comp
    for _ in range(2):
        nxt = erode(inner, 1)
        if not bool(nxt.any()):
            break
        inner = nxt
    if not bool(inner.any()):
        return (
            np.clip(np.asarray(fallback_rgb01, dtype=np.float64).reshape(3), 0.0, 1.0),
            float(np.clip(fallback_alpha, 0.0, 1.0)),
            {"inner_px": 0, "chroma_pct": None},
        )
    sub = rgb255[inner].astype(np.float64)
    a_sub = alpha[inner].astype(np.float64)
    mx = sub.max(axis=1)
    mn = sub.min(axis=1)
    chroma = mx - mn
    if float(chroma.max() - chroma.min()) > 1.0:
        thr = float(np.percentile(chroma, 75.0))
        sel = chroma >= thr
        if int(sel.sum()) < 1:
            sel = np.ones(len(sub), dtype=bool)
    else:
        sel = np.ones(len(sub), dtype=bool)
    col = np.median(sub[sel], axis=0) / 255.0
    a_rep = float(np.median(a_sub[sel]))
    return (
        np.clip(col, 0.0, 1.0),
        float(np.clip(a_rep, 0.0, 1.0)),
        {"inner_px": int(inner.sum()), "chroma_pct": 75.0},
    )


# ================================================================ 主入口
def decompose(img: dict, analysis: dict, cfg: FBSConfig, warn: WarningLog) -> dict:
    """Stage1 主入口：混合分解为 region_fill + medial_branch 两类 element。"""
    t_start = time.perf_counter()
    rgb = np.asarray(img["rgb"], dtype=np.float64)
    alpha = np.asarray(img["alpha"], dtype=np.float64)
    h, w = rgb.shape[:2]
    long_side = int(max(w, h))
    cov = np.asarray(analysis["cov"], dtype=np.float64)
    labels = np.asarray(analysis["labels"]).astype(np.int32)
    palette01 = np.asarray(analysis.get("palette", np.zeros((0, 3))), dtype=np.float64)
    bg = np.asarray(analysis.get("bg", [1.0, 1.0, 1.0]), dtype=np.float64).reshape(3)
    rgb255 = rgb * 255.0
    klass = str(analysis.get("klass", img.get("klass", "unknown")))
    hybrid_on = cfg.hybrid in ("auto", "on")
    subpixel_on = cfg.subpixel_axis in ("auto", "on")
    width_mode = str(cfg.width_mode)
    # 宽度全局补偿：0 = auto = 1.0（渲染侧按图像类别乘补偿，见 render.render；
    # 这里不再写进 doc 宽度，保证 A7/A13 宽度反演精度不受渲染补偿影响）
    width_scale_eff = float(cfg.width_scale)
    if width_scale_eff <= 0.0:
        width_scale_eff = 1.0
    # 覆盖率场里"既非背景也非实心"的抗锯齿像素占比：用来识别纯二值/硬量化源。
    # 这类源（例如 8× NEAREST 放大的 logo）边界本来就是阶梯块；轮廓填充会忠实复现阶梯，
    # 而中轴圆盘/变宽笔画会把阶梯抹圆，反而更接近重采样参考。故对 blocky 源保留保守路由。
    _cov = np.asarray(analysis.get("cov", np.zeros((1, 1))), dtype=np.float64)
    _ink = _cov[_cov > 0.02]
    aa_frac = float(((_ink > 0.05) & (_ink < 0.95)).mean()) if _ink.size else 0.0

    elements: list[dict[str, Any]] = []
    regions: list[dict[str, Any]] = []
    if klass == "gradient":
        # 渐变/无硬边输入：中轴表示只能近似复刻（画面信息主要在颜色连续变化上），
        # 必须让调用方从 warnings 明确看出"这是降级复刻"，不能静默。
        warn.add(
            "W_GRADIENT_DEGRADED",
            "渐变/无硬边输入：区域边界不可靠，中轴分支只能近似复刻，颜色场/宽度为降级结果",
            severity="info",
        )
    stats: dict[str, Any] = {
        "region_count": 0,
        "fill_count": 0,
        "branch_count": 0,
        "point_disk_count": 0,
        "pruned_branches": 0,
        "merged_branches": 0,
        "dropped_short": 0,
        "rescued_short": 0,
        "hole_regions": 0,
        "bridged_gaps": 0,
        "repair_branches": 0,
        "degenerate_disks": 0,
        "total_arc_length": 0.0,
        "klass": klass,
        "hybrid": "on" if hybrid_on else "off",
    }

    if labels.size == 0 or not bool((labels >= 0).any()):
        # coverage.analyze 已就「无墨色」告警，这里不重复告警（WarningLog 只去重完全相同的条目）
        stats["elapsed_s"] = round(time.perf_counter() - t_start, 3)
        return {
            "elements": elements,
            "regions": regions,
            "stats": stats,
            "method": "hybrid_medial_contour",
            "accelerators": {"decompose": "numpy_vectorized"},
        }

    # ---- 逐颜色标签 → 连通块（每个连通块一个唯一 region_id）----
    components: list[tuple[int, int, np.ndarray]] = []  # (label, region_id, comp_full)
    label_ids = np.unique(labels[labels >= 0])
    raw: list[tuple[int, np.ndarray]] = []
    for rid_lab in label_ids.tolist():
        mask_lab = labels == rid_lab
        comp_lab, n_comp = label_components(mask_lab)
        if n_comp <= 0:
            continue
        for ci in range(1, n_comp + 1):
            comp_full = comp_lab == ci
            if int(comp_full.sum()) <= 0:
                continue
            raw.append((int(rid_lab), comp_full))

    def _comp_key(item: tuple[int, np.ndarray]) -> tuple:
        lab, c = item
        ys, xs = np.nonzero(c)
        return (-int(c.sum()), lab, int(ys[0]) if ys.size else 0, int(xs[0]) if xs.size else 0)

    raw.sort(key=_comp_key)
    for i, (lab, comp_full) in enumerate(raw):
        components.append((lab, i, comp_full))
    stats["region_count"] = len(components)
    if width_mode == "auto":
        # 复杂作品（多连通块）：DT 宽度实测更稳（覆盖率积分在密排细线处相互外溢）；
        # 单笔画/签名类（连通块很少）：用覆盖率法向积分（亚像素精度最高，stamp/sweep 一致性最好）。
        width_mode = "dt" if len(components) >= _AUTO_WIDTH_DT_MIN_REGIONS else "coverage"

    margin = max(3, int(math.ceil(3.0 * float(cfg.denoise_sigma))) + 2)
    # 空间证据：颜色标签图（与宽度/质心测量共用，只转一次 float32）
    lab_img = labels.astype(np.float32)

    for rid_lab, rid, comp_full in components:
        ys, xs = np.nonzero(comp_full)
        if ys.size == 0:
            continue
        y0 = max(0, int(ys.min()) - margin)
        y1 = min(h, int(ys.max()) + 1 + margin)
        x0 = max(0, int(xs.min()) - margin)
        x1 = min(w, int(xs.max()) + 1 + margin)
        comp = comp_full[y0:y1, x0:x1]
        off = (y0, x0)
        # 边界处补 0 背景，保证 EDT 在图像边缘也正确
        comp_pad = np.pad(comp, 1, mode="constant", constant_values=False)
        dt_pad = distance_transform(comp_pad)
        dt = dt_pad[1:-1, 1:-1]
        dtm = float(dt.max()) if dt.size else 0.0
        area_px = int(comp.sum())
        sprawl = float(area_px / (np.pi * dtm * dtm)) if dtm > 0 else float("inf")
        # 自身厚度证据：薄像素占比 / 厚度中位数（区分"实心结构"与"蔓延细线网络"）
        thin_frac = float((dt[comp] <= 1.5).mean()) if area_px > 0 else 1.0
        dt_med = float(np.median(dt[comp])) if area_px > 0 else 0.0

        # 代表色（fill 与 region 记录共用；medial 的颜色逐点采样）
        fallback_col = palette01[rid_lab] if 0 <= rid_lab < len(palette01) else (1.0 - bg)
        rep_col, rep_alpha, rep_stats = _region_representative_color(
            comp,
            rgb255[off[0]:off[0] + comp.shape[0], off[1]:off[1] + comp.shape[1]],
            alpha[off[0]:off[0] + comp.shape[0], off[1]:off[1] + comp.shape[1]],
            fallback_col,
            1.0,
        )
        bbox_world = [
            float((x0 + 0.5) / long_side),
            float((y0 + 0.5) / long_side),
            float((x1 - 0.5) / long_side),
            float((y1 - 0.5) / long_side),
        ]
        region = {
            "id": int(rid),
            "rgb": [int(round(float(v) * 255.0)) for v in np.clip(rep_col, 0.0, 1.0)],
            "area_px": int(area_px),
            "bbox": bbox_world,
            "sprawl": round(float(sprawl), 4),
            "dt_peak_px": round(float(dtm), 3),
        }
        regions.append(region)

        use_fill = False
        if hybrid_on and dtm >= _FILL_MIN_DT_PX:
            # 路由判据（带空间/厚度证据）：
            # - 插画：大块面即使形状复杂也要填充（中轴圆盘盖不满）；
            # - 线稿/扁平：sprawl 只适合识别"蔓延细线网络"；细长实心笔画（logo 字母、色块）
            #   sprawl 也会偏大，但它们必须走轮廓填充，否则中轴重绘会丢硬边/涂成糊团。
            #   补充判据 = 自身厚度：薄像素占比低 + 厚度中位数 ≥ 2px → 实心结构 → 填充。
            if klass == "illustration":
                use_fill = True
            else:
                use_fill = (sprawl <= _FILL_SPRAWL_MAX) or (
                    len(components) >= _FILL_THIN_ROUTE_MIN_COMPONENTS
                    and aa_frac >= _FILL_THIN_ROUTE_MIN_AA_FRAC
                    and thin_frac <= _FILL_MAX_THIN_FRAC
                    and dt_med >= _FILL_MIN_DT_MED_PX
                )
            # 环状实心结构（气泡/环/带孔块面）：带洞填充已支持，若内部洞显著且洞数可控，
            # 强制走 region_fill——否则中轴分支会把环画成实心/丢填色（flat_art 尤其明显）。
            if not use_fill and thin_frac <= 0.45:
                filled0 = fill_holes(comp)
                holes0 = int(filled0.sum()) - int(area_px)
                if holes0 > max(int(0.12 * area_px), 24):
                    lab_h, n_h = label_components(filled0 & ~comp)
                    # 只对"环/气泡"类（洞少、壁厚）强制填充；蔓延细线网络会自然围出大量
                    # 背景洞（n_h 大），否则会被误判成带洞实心块而整块涂掉（horn 实测 -3.7dB）。
                    if (
                        0 < int(n_h) <= 4
                        and dt_med >= 2.0
                        and float((dt[comp] <= 1.5).mean()) <= 0.45
                        # 大面蔓延网络（占画布 >3%）即使只围出少量洞也可能是线网，不能整块填充
                        and area_px <= 0.03 * float(h * w)
                    ):
                        use_fill = True
        emit_holes_region = (klass != "illustration")
        if use_fill:
            # 带洞区域：让 region_fill 携带洞几何，渲染端先填外轮廓再逐洞用背景色挖掉。
            # 但**蔓延线网**（洞多/外轮廓极不规则）不能用外轮廓填充：它的"洞"是线间空白，
            # 外轮廓一填就整块涂掉（horn 实测 -3.7dB）。lineart/flat_art 遇到洞数 >4 的
            # 大网络仍回退中轴；illustration 的洞多是其他色块（会被后续更小区域覆盖），保留填充。
            filled = fill_holes(comp)
            holes_px = int(filled.sum()) - int(area_px)
            if holes_px > max(int(0.12 * area_px), 24):
                holes_m = filled & ~comp
                lab_h, n_h = label_components(holes_m)
                lab_win = labels[off[0]:off[0] + comp.shape[0], off[1]:off[1] + comp.shape[1]]
                bg_frac = float((lab_win[holes_m] < 0).mean()) if bool(holes_m.any()) else 0.0
                if klass == "illustration":
                    # 插画类保持旧行为：洞内常是别的色块/笔触细节（未必有 fill 覆盖），
                    # 用背景色挖洞会把这些细节抹掉。
                    warn.add(
                        "W_HOLE_REGION",
                        f"区域 {rid} 洞 {holes_px}px（{int(n_h)} 个），按旧策略回退中轴分支",
                        severity="info",
                    )
                    use_fill = False
                elif bg_frac > 0.5 and int(n_h) > _HOLE_FILL_MAX_COUNT:
                    # 洞主要是背景（线网空白）且数量多 → 外轮廓填充会涂掉线间空白
                    warn.add(
                        "W_HOLE_REGION",
                        f"区域 {rid} 洞 {holes_px}px（{int(n_h)} 个，背景洞占 {bg_frac * 100:.0f}%），"
                        "疑似线网，回退中轴分支",
                        severity="info",
                    )
                    use_fill = False
                elif bg_frac > 0.5:
                    # 少量背景洞（气泡/高光空隙）→ 带洞填充，洞用背景色挖掉
                    warn.add(
                        "W_HOLE_REGION",
                        f"区域 {rid} 内部有洞 {holes_px}px（{int(n_h)} 个，占 "
                        f"{holes_px / max(area_px, 1) * 100:.1f}%），按带洞填充处理（洞用背景色挖掉）",
                        severity="info",
                    )
                    stats["hole_regions"] = int(stats.get("hole_regions", 0)) + 1
                else:
                    # 洞主要是**墨色内容**（被暗墨保护保留的细线/排线标签）→ 实心填外轮廓，
                    # 这些"洞"随后由各自的笔画/填充层覆盖：大块填色与细线都保住。
                    emit_holes_region = False
                    warn.add(
                        "W_HOLE_REGION",
                        f"区域 {rid} 内部洞 {holes_px}px（{int(n_h)} 个）以墨色内容为主，"
                        "实心填充、由笔画层覆盖",
                        severity="info",
                    )
        if use_fill:
            # 区域内部颜色场（渐变/柔边块面不再被涂成单一平色；渲染端双线性采样）
            cf = _component_color_field(
                comp, off, rgb255, cov, int(cfg.color_field_grid), rep_col, long_side
            )
            if cf is not None:
                region["color_field"] = cf
                stats["color_fields"] = int(stats.get("color_fields", 0)) + 1
            use_subpixel_edge = (klass != "illustration" and aa_frac >= _SUBPIXEL_MIN_AA_FRAC)
            el = _component_region_fill(
                comp, off, long_side, rid, rep_col, rep_alpha,
                cov_local=(
                    cov[off[0]:off[0] + comp.shape[0], off[1]:off[1] + comp.shape[1]]
                    if use_subpixel_edge else None
                ),
                emit_holes=emit_holes_region,
            )
            if el is not None:
                elements.append(el)
                stats["fill_count"] += 1
                continue
            # 轮廓追踪失败 → 回退中轴（宁缺勿凑的反面：绝不静默丢区域）
            warn.add("W_GAP_BRIDGED", f"区域 {rid} 轮廓追踪失败，回退中轴分解", severity="info")
        # ---- 中轴分支 ----
        branch_els, bstats = _component_to_branches(
            comp,
            off,
            dt,
            dtm,
            sprawl,
            rgb,
            rgb255,
            alpha,
            cov,
            labels,
            lab_img,
            palette01,
            bg,
            long_side,
            rid,
            int(rid_lab),
            cfg,
            klass,
            subpixel_on,
            width_mode,
            width_scale_eff,
            texture_dots=aa_frac >= _SUBPIXEL_MIN_AA_FRAC,
        )
        stats["pruned_branches"] += int(bstats.get("pruned", 0))
        stats["merged_branches"] += int(bstats.get("merged", 0))
        stats["dropped_short"] += int(bstats.get("dropped_short", 0))
        stats["rescued_short"] += int(bstats.get("rescued_short", 0))
        stats["bridged_gaps"] += int(bstats.get("bridged", 0))
        stats["repair_branches"] += int(bstats.get("repaired", 0))
        stats["point_disk_count"] += int(bstats.get("point_disks", 0))
        stats["texture_dot_count"] = int(stats.get("texture_dot_count", 0)) + int(bstats.get("texture_dots", 0))
        stats["degenerate_disks"] += int(bstats.get("point_disks", 0))
        elements.extend(branch_els)
        stats["branch_count"] += sum(1 for e in branch_els if e["fill_role"] == "medial_branch")

    # ---- 排序 / 分层：R7 先铺底层（大色块填充，面积降序）再压中轴分支 ----
    fills = [e for e in elements if e["fill_role"] == "region_fill"]
    medials = [e for e in elements if e["fill_role"] != "region_fill"]
    fills.sort(key=lambda e: (-int(e["meta"].get("area_px", 0)), -float(e["meta"].get("dt_peak_px") or 0.0), e["region_id"]))
    medials.sort(
        key=lambda e: (
            -float(e["meta"].get("max_halfwidth_px", 0.0)),
            -float(e["meta"].get("dt_peak_px") or 0.0),
            e["region_id"],
        )
    )
    for e in fills:
        e["layer"] = 0
    for e in medials:
        e["layer"] = 1
    elements = fills + medials

    # max_curves 安全阀：**先保底层填充、再保大 footprint 分支**（不是常规限制）。
    # 若一律按 footprint 排序，region_fill 的 footprint 恒为 0（宽度为 0）→ 填充被整批砍掉，
    # 这正是旧版「补洞笔画被 max_curves 误伤」的同类错误。
    if len(elements) > int(cfg.max_curves):
        def _footprint(e: dict) -> float:
            arc = float(np.max(e["s"]) if len(e["s"]) else 0.0)
            return arc * float(np.mean(e["widths"]) if len(e["widths"]) else 0.0)

        fills = [e for e in elements if e["fill_role"] == "region_fill"]
        medials = [e for e in elements if e["fill_role"] != "region_fill"]
        dropped = len(elements) - int(cfg.max_curves)
        keep_fills = fills[: max(0, int(cfg.max_curves))]
        remain = int(cfg.max_curves) - len(keep_fills)
        keep_medials = sorted(medials, key=_footprint, reverse=True)[: max(0, remain)]
        elements = keep_fills + keep_medials
        warn.add(
            "W_MAX_CURVES",
            f"笔画数超过 max_curves={cfg.max_curves}，优先保留填充与大 footprint 分支，"
            f"截断 {dropped} 条",
        )
        # 重新分层/排序，保持 region_fill 全部在下层
        elements.sort(
            key=lambda e: (
                0 if e["fill_role"] == "region_fill" else 1,
                -int(e["meta"].get("area_px", 0)) if e["fill_role"] == "region_fill"
                else -float(e["meta"].get("max_halfwidth_px", 0.0)),
                e["region_id"],
            )
        )

    stats["total_arc_length"] = float(
        sum(
            float(np.sum(np.linalg.norm(np.diff(e["points"], axis=0), axis=1)))
            for e in elements
            if not e["closed"]
        )
    )
    stats["element_count"] = len(elements)
    stats["elapsed_s"] = round(time.perf_counter() - t_start, 3)
    if stats["dropped_short"] > 0:
        warn.add("W_SHORT_BRANCHES", f"丢弃 {stats['dropped_short']} 条墨量过小的过短分支")
    if int(stats.get("rescued_short", 0)) > 0:
        warn.add(
            "W_SHORT_BRANCHES",
            f"保留 {stats['rescued_short']} 条有墨量的短笔触（原会被丢弃）",
            severity="info",
        )
    if stats["pruned_branches"] > 0:
        warn.add("W_PRUNED", f"中轴剪枝移除 {stats['pruned_branches']} 条毛刺分支", severity="info")
    if stats["bridged_gaps"] > 0:
        warn.add("W_GAP_BRIDGED", f"分支图桥接接回 {stats['bridged_gaps']} 处缺口", severity="info")
    return {
        "elements": elements,
        "regions": regions,
        "stats": stats,
        "method": "hybrid_medial_contour",
        "accelerators": {
            "decompose": "numpy_vectorized",
            "skeleton": "zhang_suen_vectorized",
            "subpixel_axis": "coverage_centroid" if subpixel_on else "off",
            "width": {"coverage": "coverage_integral", "dt": "dt", "hybrid": "hybrid_min_cov_dt"}.get(width_mode, width_mode),
        },
    }


def _subpixel_loop(
    loop_lat: np.ndarray,
    comp: np.ndarray,
    cov_local: np.ndarray,
    sigma: float = _SUBPIXEL_EDGE_SIGMA,
) -> np.ndarray:
    """像素边格点回路 → 沿法向对齐到 50% 覆盖率等值线的亚像素回路（局部格点坐标）。

    - 覆盖率场 = 区域内 cov、区域外 0，然后高斯平滑（σ 由抗锯齿带宽导出）；
    - 每个格点顶点转成"像素中心"坐标，沿局部法向双向搜索 f=0.5 的交点；
    - 交点线性插值，得到亚像素位置；找不到交点的顶点保持原样（闭环仍闭合）。
    返回坐标仍是"格点"口径（像素 i 覆盖 [i,i+1]），可直接走 _lattice_to_world。
    """
    p = np.asarray(loop_lat, dtype=np.float64).reshape(-1, 2)
    if len(p) < 8:
        return p
    h, w = comp.shape
    if h < 5 or w < 5:
        return p
    pad = 3
    f = np.where(comp, np.clip(cov_local, 0.0, 1.0), 0.0).astype(np.float32)
    f = gaussian_blur(f, float(sigma))
    f = np.pad(f, pad, mode="constant", constant_values=0.0)

    # 格点坐标 → 像素中心坐标（数组索引）→ 加 pad 偏移
    pc = p - 0.5
    nxt = np.roll(pc, -1, axis=0)
    prv = np.roll(pc, 1, axis=0)
    tang = nxt - prv
    nrm = np.stack([-tang[:, 1], tang[:, 0]], axis=1)
    ln = np.linalg.norm(nrm, axis=1, keepdims=True)
    ln[ln < 1e-9] = 1.0
    nrm = nrm / ln

    ts = np.arange(-_SUBPIXEL_EDGE_SEARCH_PX, _SUBPIXEL_EDGE_SEARCH_PX + 1e-9, _SUBPIXEL_EDGE_STEP_PX)
    xs = pc[:, 0][:, None] + ts[None, :] * nrm[:, 0][:, None] + pad
    ys = pc[:, 1][:, None] + ts[None, :] * nrm[:, 1][:, None] + pad
    vals = np.asarray(bilinear_sample(f, xs, ys), dtype=np.float64)
    d = vals - _SUBPIXEL_EDGE_LEVEL
    sign = np.sign(d)
    cross = (sign[:, :-1] * sign[:, 1:]) <= 0.0
    v0, v1 = d[:, :-1], d[:, 1:]
    denom = v0 - v1
    frac = np.where(np.abs(denom) < 1e-9, 0.0, v0 / np.where(np.abs(denom) < 1e-9, 1.0, denom))
    t_cross = ts[:-1][None, :] + frac * _SUBPIXEL_EDGE_STEP_PX
    cost = np.where(cross, np.abs(t_cross), np.inf)
    j = np.argmin(cost, axis=1)
    has = np.isfinite(cost[np.arange(len(p)), j])
    best = t_cross[np.arange(len(p)), j]
    out = p.copy()
    sel = has
    out[sel, 0] = pc[sel, 0] + best[sel] * nrm[sel, 0] + 0.5
    out[sel, 1] = pc[sel, 1] + best[sel] * nrm[sel, 1] + 0.5
    # 数值稳定：偏移不超过搜索半径（防平滑场在角点处把顶点拉飞）
    out[:, 0] = np.clip(out[:, 0], p[:, 0] - _SUBPIXEL_EDGE_SEARCH_PX, p[:, 0] + _SUBPIXEL_EDGE_SEARCH_PX)
    out[:, 1] = np.clip(out[:, 1], p[:, 1] - _SUBPIXEL_EDGE_SEARCH_PX, p[:, 1] + _SUBPIXEL_EDGE_SEARCH_PX)
    return out


def _component_region_fill(
    comp: np.ndarray,
    off: tuple[int, int],
    long_side: int,
    rid: int,
    rep_col: np.ndarray,
    rep_alpha: float,
    cov_local: np.ndarray | None = None,
    emit_holes: bool = True,
) -> dict[str, Any] | None:
    """把一个块状连通域转成 region_fill element（闭合轮廓 + 显式填充多边形 + 可选内部洞）。

    边界默认做**亚像素对齐**（沿覆盖率场 50% 等值线），减少线稿边界环误差；
    cov_local 为与 comp 同窗口的覆盖率场（None 时退回纯格点轮廓）。
    洞（环状区域的内部空白/气泡/高光空隙）以世界坐标闭合环列表返回；渲染端先填外轮廓，
    再逐洞用背景色挖掉，因此带洞区域不再需要回退中轴分支（那会丢填色）。
    洞太多时返回 None（调用方回退中轴），避免格式体积失控。
    """
    loops = trace_boundary_loops(comp)
    if not loops:
        return None
    outer = max(loops, key=lambda p: abs(_signed_area(p)))
    if len(outer) < 5:
        return None
    outer_src = outer
    if cov_local is not None and np.asarray(cov_local).shape == comp.shape:
        outer_src = _subpixel_loop(outer, comp, cov_local)
    poly = _simplify_closed(outer_src, _CONTOUR_SIMPLIFY_PX)
    poly = _drop_collinear(poly)
    if len(poly) < 3:
        return None
    # trace 返回的是**裁切局部**格点坐标，必须加回裁切原点才是全局位置
    poly = poly + np.array([float(off[1]), float(off[0])], dtype=np.float64)
    poly_w = _lattice_to_world(poly, long_side)

    # --- 内部洞：除最大外轮廓外的其余回路（按面积过滤噪声）---
    hole_loops: list[np.ndarray] = []
    if emit_holes:
        outer_area = abs(_signed_area(outer))
        hole_min_area = max(float(_HOLE_MIN_AREA_PX), float(_HOLE_MIN_FRAC) * outer_area)
        for lp in loops:
            if lp is outer:
                continue
            a = abs(_signed_area(lp))
            if a < hole_min_area:
                continue
            h = _simplify_closed(lp, _CONTOUR_SIMPLIFY_PX)
            h = _drop_collinear(h)
            if len(h) < 3:
                continue
            h = h + np.array([float(off[1]), float(off[0])], dtype=np.float64)
            hole_loops.append(_lattice_to_world(h, long_side))
        if len(hole_loops) > _HOLE_MAX_COUNT:
            return None

    n = len(poly_w)
    col = np.tile(rep_col.reshape(1, 3), (n, 1))
    return _element(
        points=poly_w,
        closed=True,
        fill_role="region_fill",
        widths=np.zeros(n, dtype=np.float64),
        colors=col,
        alphas=np.full(n, rep_alpha, dtype=np.float64),
        fill_poly=poly_w,
        fill_holes=hole_loops or None,
        region_id=rid,
        layer=0,
        meta={
            "max_halfwidth_px": 0.0,
            "sprawl": None,
            "dt_peak_px": None,
            "source": "region_fill",
            "area_px": int(comp.sum()),
            "hole_count": int(len(hole_loops)),
        },
    )


def _backfill_cell_colors(
    colors: np.ndarray, valid: np.ndarray, rep_col: np.ndarray
) -> np.ndarray:
    """无效颜色场单元用最近邻有效单元回填（逐轮 8 邻域均值扩散），不引入区域平色。"""
    if bool(valid.all()):
        return colors
    gh, gw = valid.shape
    out = colors.copy()
    v = valid.copy()
    if not bool(v.any()):
        out[:] = rep_col
        return out
    max_rounds = max(gh, gw)
    for _ in range(max_rounds):
        if bool(v.all()):
            break
        acc = np.zeros_like(out)
        cnt = np.zeros((gh, gw), dtype=np.float64)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy == 0 and dx == 0:
                    continue
                sh_v = np.roll(np.roll(v, dy, axis=0), dx, axis=1)
                sh_c = np.roll(np.roll(out, dy, axis=0), dx, axis=1)
                acc += sh_c * sh_v[..., None]
                cnt += sh_v
        upd = (~v) & (cnt > 0.0)
        if not bool(upd.any()):
            break
        out[upd] = acc[upd] / cnt[upd][:, None]
        v |= upd
    if not bool(v.all()):
        out[~v] = rep_col
    return out


def _component_color_field(
    comp: np.ndarray,
    off: tuple[int, int],
    rgb255: np.ndarray,
    cov: np.ndarray,
    grid: int,
    rep_col: np.ndarray,
    long_side: int,
    min_cell_px: float = _COLOR_FIELD_MIN_CELL_PX,
    min_area_px: int = _COLOR_FIELD_MIN_AREA_PX,
) -> dict[str, Any] | None:
    """区域内部颜色场：bbox 内 N×N 加权均值网格（只保存有效单元）。

    为什么需要：区域代表色是**一个**颜色，渐变/柔边区域（头发高光、腮红、皮肤阴影）
    会被涂成平坦色 —— 这是旧版实测与"20×20 色场模拟上界"之间最大的差距来源。
    颜色场让 region_fill 的填充色在区域内部连续变化（渲染端双线性采样）。

    关键坑（旧版实测 PSNR 19.8 → 11.2）：落在区域外的网格单元（den≈0）均值是垃圾，
    双线性插值会把垃圾拖进边界；这里用**区域代表色回填**无效单元。
    """
    if grid <= 0 or int(comp.sum()) < int(min_area_px):
        return None
    ys, xs = np.nonzero(comp)
    if ys.size == 0:
        return None
    bx0, by0 = int(xs.min()), int(ys.min())
    bx1, by1 = int(xs.max()) + 1, int(ys.max()) + 1
    bw, bh = bx1 - bx0, by1 - by0
    if bw <= 0 or bh <= 0:
        return None
    # 每轴网格自适应：单元边长不小于 min_cell_px（避免小区域浪费体积）
    gw = int(np.clip(round(bw / float(min_cell_px)), 2, int(grid)))
    gh = int(np.clip(round(bh / float(min_cell_px)), 2, int(grid)))
    n_cells = gw * gh
    # 像素中心在 bbox 内的归一化坐标 → 单元索引
    u = (xs.astype(np.float64) + 0.5 - bx0) / float(bw)
    v = (ys.astype(np.float64) + 0.5 - by0) / float(bh)
    gx = np.clip((u * gw).astype(np.int64), 0, gw - 1)
    gy = np.clip((v * gh).astype(np.int64), 0, gh - 1)
    cell = gy * gw + gx
    oy, ox = int(off[0]), int(off[1])
    gys = ys + oy
    gxs = xs + ox
    # 边界像素混有抗锯齿/描边墨色（比区域内部暗），直接入均值会把颜色场边缘单元拉暗，
    # 渲染成"区域接缝处的黑线/虚线"。只用**内部像素**估计单元颜色，边界单元用最近邻回填。
    dt_local = distance_transform(np.pad(comp, 1, mode="constant", constant_values=False))[1:-1, 1:-1]
    interior = dt_local >= 1.0
    if int(interior.sum()) < max(8, int(0.10 * int(comp.sum()))):
        interior = comp  # 区域太细（没有成片内部）→ 保持旧行为
    # 权重：覆盖率（抗锯齿边界按墨量加权）× 内部掩码（排除边界混色像素）
    wt = np.clip(cov[gys, gxs].astype(np.float64), 0.0, 1.0) * interior[ys, xs].astype(np.float64)
    if float(wt.sum()) <= 1e-9:
        wt = interior[ys, xs].astype(np.float64)
    sub = rgb255[gys, gxs].astype(np.float64)
    den = np.bincount(cell, weights=wt, minlength=n_cells)
    sums = np.stack(
        [np.bincount(cell, weights=sub[:, c] * wt, minlength=n_cells) for c in range(3)],
        axis=1,
    )
    # 单元面积（px²）：den 低于其 5% 视为无效（区域外 / 只有边界擦过）
    cell_area = float(bw * bh) / float(n_cells)
    valid = den >= max(1.0, 0.05 * cell_area)
    if not bool(valid.any()):
        return None
    colors = np.zeros((n_cells, 3), dtype=np.float64)
    colors[valid] = sums[valid] / den[valid, None]
    # 无效单元用最近邻有效单元回填（逐轮 8 邻域扩散），而不是用区域代表色涂平：
    # 平涂会在填充边缘形成一圈与渐变不连续的色带（接缝）。
    colors = _backfill_cell_colors(
        colors.reshape(gh, gw, 3), valid.reshape(gh, gw),
        np.asarray(rep_col, dtype=np.float64).reshape(3),
    ).reshape(n_cells, 3)
    colors = np.clip(colors, 0.0, 255.0)
    bbox_world = [
        float((bx0 + ox)) / float(long_side),
        float((by0 + oy)) / float(long_side),
        float((bx1 + ox)) / float(long_side),
        float((by1 + oy)) / float(long_side),
    ]
    return {
        "grid": int(gw),
        "gw": int(gw),
        "gh": int(gh),
        "bbox": bbox_world,
        "colors": [[int(round(float(c))) for c in colors[k]] for k in range(n_cells)],
        "valid": int(valid.sum()),
    }


def _component_to_branches(
    comp: np.ndarray,
    off: tuple[int, int],
    dt: np.ndarray,
    dtm: float,
    sprawl: float,
    rgb: np.ndarray,
    rgb255: np.ndarray,
    alpha: np.ndarray,
    cov: np.ndarray,
    labels: np.ndarray,
    lab_img: np.ndarray,
    palette01: np.ndarray,
    bg: np.ndarray,
    long_side: int,
    rid: int,
    lab_val: int,
    cfg: FBSConfig,
    klass: str,
    subpixel_on: bool,
    width_mode: str,
    width_scale: float,
    texture_dots: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """单个连通块 → 中轴分支 element 列表（含退化点盘路径）。"""
    w_loc = comp.shape[1]
    oy, ox = int(off[0]), int(off[1])
    bstats = {
        "pruned": 0,
        "dropped_short": 0,
        "bridged": 0,
        "repaired": 0,
        "point_disks": 0,
        "texture_dots": 0,
    }
    # --- 细化 ---
    max_iter = int(math.ceil(max(dtm, 1.0))) + 2  # 由最大内切半径导出
    skel = skeletonize(comp, max_iter=max_iter)
    skel = clean_skeleton(skel)
    # 尖角补支/端点延伸只对**块状**形状有意义（拓扑细化把凸形缩成一点/一段）。
    # 判据用结构量 sprawl：蔓延细线网络（sprawl≫）的骨架本来就贴合中心线，
    # 强行补支只会在阶梯状边界上长出伪毛刺（实测 taper 因此被切成多段）。
    thick_enough = dtm >= 2.0
    compact_enough = sprawl <= _CORNER_MAX_SPRAWL
    if cfg.corner_preserve and thick_enough and compact_enough:
        skel = add_corner_branches(skel, comp, dt)
        skel = clean_skeleton(skel)
        skel = extend_to_medial_ends(skel, comp, dt)
        skel = clean_skeleton(skel)
    branches = skeleton_graph(skel)

    # 缺口桥接放在剪枝**之前**：虚线碎段两端都是自由端，先接起来才活得下来
    bridge_gap = max(1.0, 2.0 * float(cfg.merge_distance_px))
    branches, n_b = bridge_branch_gaps(branches, bridge_gap)
    bstats["bridged"] += n_b
    branches, n_p = prune_branches(branches, dt, cfg)
    bstats["pruned"] += n_p
    branches, n_m = merge_collinear_branches(branches, _MERGE_BRANCH_COS)
    bstats["merged"] = int(bstats.get("merged", 0)) + n_m

    # 覆盖修复：圆盘并集盖不满块状/锯齿区域时补笔（只在有厚度时做；阈值由局部 DT 导出）
    if dtm >= 1.5 and sprawl > 1.0:
        branches, n_r = repair_coverage(comp, dt, branches)
        bstats["repaired"] += n_r
        if n_r > 0:
            branches, n_b2 = bridge_branch_gaps(branches, bridge_gap)
            bstats["bridged"] += n_b2
            branches, n_m2 = merge_collinear_branches(branches, _MERGE_BRANCH_COS)
            bstats["merged"] = int(bstats.get("merged", 0)) + n_m2

    # 去掉零长度分支；对**有实际墨量**的短分支保留（排线/点状笔触），只丢墨量太小的噪声。
    # 插画类（illustration）的短碎片几乎都是抗锯齿/纹理噪声（实测加入后 PSNR/SSIM 均下降），
    # 不启用短笔触救援；线稿/扁平类才需要保留排线。
    mass_min = (
        float("inf")
        if klass == "illustration"
        else _SHORT_BRANCH_MIN_MASS_PX2
    )
    valid: list[list[tuple[int, int]]] = []
    short_rescued: list[list[tuple[int, int]]] = []
    min_len = max(1.0, 0.5 * float(cfg.min_branch_length_px))
    for b in branches:
        if len(b) < 2:
            continue
        length = _branch_length(b)
        if length < min_len:
            if _branch_ink_mass(b, dt) >= mass_min and length >= 1.0:
                short_rescued.append(b)
                bstats["rescued_short"] = int(bstats.get("rescued_short", 0)) + 1
            else:
                bstats["dropped_short"] += 1
            continue
        valid.append(b)
    valid.sort(key=_branch_length, reverse=True)
    short_rescued.sort(key=_branch_length, reverse=True)
    if short_rescued:
        # 先画长分支、再画短笔触（短笔触通常在上层细节）
        valid = valid + short_rescued

    if not valid:
        # 退化：实心圆盘骨架长度为 0。必须单独输出点盘，不能消失（MAT 对它的精确重建）
        if dtm >= 0.5:
            iy, ix = divmod(int(np.argmax(dt)), w_loc)
            cy = float(iy + oy)
            cx = float(ix + ox)
            r_px = float(np.clip(dtm, 0.5, None))
            # 沿 x 给两个相距 0.5px 的点：让傅里叶参数化有正弧长（否则整条被跳过）
            d = np.array([0.25, 0.0])
            pts_px = np.stack([np.array([cx, cy]) - d, np.array([cx, cy]) + d])
            pts_w = _to_world(pts_px, long_side)
            gx = int(np.clip(round(cx), 0, rgb255.shape[1] - 1))
            gy = int(np.clip(round(cy), 0, rgb255.shape[0] - 1))
            col = rgb255[gy, gx] / 255.0
            a_core = float(np.clip(cov[gy, gx], 0.0, 1.0))
            bstats["point_disks"] = 1
            return (
                [
                    _element(
                        points=pts_w,
                        closed=False,
                        fill_role="medial_branch",
                        widths=np.full(2, 2.0 * r_px / long_side),
                        colors=np.tile(col.reshape(1, 3), (2, 1)),
                        alphas=np.full(2, max(a_core, float(alpha[gy, gx]))),
                        fill_poly=None,
                        region_id=rid,
                        layer=1,
                        meta={
                            "max_halfwidth_px": r_px,
                            "sprawl": sprawl,
                            "dt_peak_px": dtm,
                            "source": "point_disk",
                            "degenerate": True,
                        },
                    )
                ],
                bstats,
            )
        return [], bstats

    out: list[dict[str, Any]] = []
    # 合并距离内的邻近分支端点已在 bridge 中处理；这里按长度降序逐条转 element
    for b in valid:
        blen = _branch_length(b)
        if texture_dots and blen < _TEXTURE_DOT_MAX_LEN_PX and _branch_ink_mass(b, dt) >= _TEXTURE_DOT_MIN_MASS_PX2:
            dot = _texture_dot_element(b, comp, dt, rgb, cov, long_side, rid, off)
            if dot is not None:
                out.append(dot)
                bstats["texture_dots"] = int(bstats.get("texture_dots", 0)) + 1
                continue
        el = _branch_to_element(
            b,
            comp,
            dt,
            rgb,
            rgb255,
            alpha,
            cov,
            labels,
            lab_img,
            palette01,
            bg,
            long_side,
            rid,
            lab_val,
            cfg,
            subpixel_on,
            width_mode,
            width_scale,
            off,
        )
        if el is None:
            bstats["dropped_short"] += 1
            continue
        out.append(el)
    return out, bstats


def _texture_dot_element(
    b: list[tuple[int, int]],
    comp: np.ndarray,
    dt: np.ndarray,
    rgb: np.ndarray,
    cov: np.ndarray,
    long_side: int,
    rid: int,
    off: tuple[int, int],
) -> dict[str, Any] | None:
    """短墨段 → 点盘元素（纹理帧：排线点/网点/碎笔）。

    半径由局部厚度（2×DT）与墨量面积双约束导出，颜色/alpha 取墨段内覆盖率最高的像素，
    避免用"圆头短笔画"把 1–2px 的点放大成一团。
    """
    arr = np.asarray(b, dtype=np.float64)
    if arr.size == 0:
        return None
    oy, ox = int(off[0]), int(off[1])
    ys = np.clip(np.rint(arr[:, 1]).astype(np.int64), 0, dt.shape[0] - 1)
    xs = np.clip(np.rint(arr[:, 0]).astype(np.int64), 0, dt.shape[1] - 1)
    gy = ys + oy
    gx = xs + ox
    covs = np.clip(cov[gy, gx].astype(np.float64), 0.0, 1.0)
    k = int(np.argmax(covs)) if len(covs) else 0
    width_px = float(max(1.0, 2.0 * dt[ys[k], xs[k]]))
    mass = _branch_ink_mass(b, dt)
    r_px = float(max(0.5, width_px * 0.5, float(np.sqrt(max(mass, 0.0) / np.pi))))
    gy0, gx0 = int(gy[k]), int(gx[k])
    col = rgb[gy0, gx0].reshape(3).astype(np.float64)
    a = float(max(covs[k], 0.05))
    cx = float(gx0)  # 像素索引坐标（整数=像素中心），交给 _to_world 统一 +0.5
    cy = float(gy0)
    d = 0.125  # 沿 x 给两点极小弧长（与退化点盘同手法，保证傅里叶参数化有正弧长）
    pts_px = np.array([[cx - d, cy], [cx + d, cy]])
    pts_w = _to_world(pts_px, long_side)
    w_world = (2.0 * r_px) / long_side
    return _element(
        points=pts_w,
        closed=False,
        fill_role="medial_branch",
        widths=np.full(2, w_world),
        colors=np.tile(col.reshape(1, 3), (2, 1)),
        alphas=np.full(2, a),
        fill_poly=None,
        region_id=rid,
        layer=1,
        meta={
            "max_halfwidth_px": r_px,
            "sprawl": None,
            "dt_peak_px": 2.0 * r_px,
            "source": "texture_dot",
            "arc_length_px": float(_branch_length(b)),
        },
    )


def _branch_to_element(
    b: list[tuple[int, int]],
    comp: np.ndarray,
    dt: np.ndarray,
    rgb: np.ndarray,
    rgb255: np.ndarray,
    alpha: np.ndarray,
    cov: np.ndarray,
    labels: np.ndarray,
    lab_img: np.ndarray,
    palette01: np.ndarray,
    bg: np.ndarray,
    long_side: int,
    rid: int,
    lab_val: int,
    cfg: FBSConfig,
    subpixel_on: bool,
    width_mode: str,
    width_scale: float,
    off: tuple[int, int],
) -> dict[str, Any] | None:
    """单条中轴像素路径 → medial_branch element。"""
    oy, ox = int(off[0]), int(off[1])
    arr = np.asarray(b, dtype=np.float64)  # (y,x)
    pts_global = np.stack([arr[:, 1] + ox, arr[:, 0] + oy], axis=1)  # (x,y) 全局像素索引
    length_px = float(
        np.linalg.norm(np.diff(pts_global, axis=0), axis=1).sum()
    )
    if length_px < 1.0:
        return None
    # DT 提示（取在最近的整数像素中心上）
    ly = np.clip(np.round(pts_global[:, 1]).astype(np.int64) - oy, 0, dt.shape[0] - 1)
    lx = np.clip(np.round(pts_global[:, 0]).astype(np.int64) - ox, 0, dt.shape[1] - 1)
    dt_hint = np.maximum(dt[ly, lx].astype(np.float64), 0.5)

    # --- 亚像素中心线（只统计同色标签的覆盖率，防被邻区拉走）---
    if subpixel_on:
        pts_global, _adopted = refine_axis_coverage(
            cov, pts_global, dt_hint, lab_img=lab_img, lab_val=lab_val
        )

    # --- 按弧长均匀重采样（点数由弧长导出）---
    pts_global = _resample_path(pts_global, _RESAMPLE_DENSITY)
    if len(pts_global) < 2:
        return None
    n = len(pts_global)
    # 重采样后重新取 DT 提示（亚像素位置用双线性）
    dt_hint2 = np.maximum(
        bilinear_sample(dt, pts_global[:, 0] - ox, pts_global[:, 1] - oy),
        0.5,
    )
    dt_hint2 = np.clip(dt_hint2, 0.5, None)

    # --- 宽度：覆盖率法向积分 / DT / 混合（默认）---
    if width_mode == "coverage":
        widths_px, core_cov = measure_widths_coverage(
            cov, pts_global, dt_hint2, lab_img=lab_img, lab_val=lab_val
        )
        bad = ~np.isfinite(widths_px) | (widths_px <= 1e-6)
        widths_px = np.where(bad, 2.0 * dt_hint2, widths_px)
        alphas = np.clip(
            bilinear_sample(alpha, pts_global[:, 0], pts_global[:, 1]).astype(np.float64),
            0.0,
            1.0,
        )
    elif width_mode == "dt":
        widths_px = 2.0 * dt_hint2
        core_cov = np.clip(
            bilinear_sample(cov, pts_global[:, 0], pts_global[:, 1]).astype(np.float64),
            0.05,
            1.0,
        )
        a_src = np.clip(
            bilinear_sample(alpha, pts_global[:, 0], pts_global[:, 1]).astype(np.float64),
            0.0,
            1.0,
        )
        alphas = np.clip(a_src * core_cov, 0.0, 1.0)
    else:  # hybrid：min(覆盖积分, K·DT)，抑制法向积分在邻接结构/AA 晕圈处的外溢
        widths_cov, core_cov = measure_widths_coverage(
            cov, pts_global, dt_hint2, lab_img=lab_img, lab_val=lab_val
        )
        bad = ~np.isfinite(widths_cov) | (widths_cov <= 1e-6)
        widths_cov = np.where(bad, 2.0 * dt_hint2, widths_cov)
        widths_dt = 2.0 * dt_hint2
        widths_px = np.minimum(widths_cov, _HYBRID_WIDTH_K * widths_dt)
        # 宽度仍主要是覆盖率口径（亚像素细线靠它），alpha 保持源 alpha
        alphas = np.clip(
            bilinear_sample(alpha, pts_global[:, 0], pts_global[:, 1]).astype(np.float64),
            0.0,
            1.0,
        )

    # --- 颜色（去混色）与 alpha ---
    colors = _ink_colors(pts_global, rgb, cov, bg, labels, palette01)

    pts_w = _to_world(pts_global, long_side)
    widths_w = widths_px / long_side
    if float(width_scale) > 0.0:
        widths_w = widths_w * float(width_scale)
    meta = {
        "max_halfwidth_px": float(widths_px.max()) * 0.5,
        "sprawl": None,
        "dt_peak_px": float(dt.max()),
        "source": "medial_branch",
        "arc_length_px": length_px,
        # 分支颜色逐点来自源图去混色采样（空间变化），不会把底下的填充色盖成单色
        "color_source": "source_unmix",
    }
    return _element(
        points=pts_w,
        closed=False,
        fill_role="medial_branch",
        widths=widths_w,
        colors=colors,
        alphas=alphas,
        fill_poly=None,
        region_id=rid,
        layer=1,
        meta=meta,
    )
