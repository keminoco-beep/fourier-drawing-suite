"""静态卫生守卫 :: 用**高置信**规则堵住「静默失败 / 契约脱钩 / 热路径循环」三类根因。

为什么需要（references/engineering_guardrails.md §4/§5/§6）
--------------------------------------------------------
这几类问题在本项目反复出现，且都能静态发现，不必等到跑出坏结果：

· 静默失败：裸 ``except`` / ``except: pass`` / ``except: print(...)``；
  ``x or 12`` 把 0/False 一起吞掉；CLI 的 ``add_argument(default=非 None)`` 无声覆盖
  FBSConfig（实测把 max_curves 从 8000 打回 2000）；
· 契约脱钩：``round(x, 6..9)`` 这类手写序列化精度（精度必须由 render_contract 反推）；
  6–9 位小数字面量（除标准色度矩阵等已注明来源的常数）；
· 热路径：``for i in range(h)`` / ``range(s.size)`` 这种按像素/采样点的 Python 循环；
· 编码纪律：UTF-8 **无 BOM**（契约 §11.9）。

设计取舍：**只留高置信规则**。检查器的误报一多就会被彻底忽略（那样连真问题也一起漏），
所以：
1. 规则基于 ``ast`` 精确匹配（不是裸正则扫源码，避免命中注释/字符串）；
2. 只报「语义上几乎必然是 bug」的模式；
3. 支持 ``# noqa`` 行内白名单（命中行或上两行内出现 noqa 即跳过），让"确认无害"有明确写法。

用法：
    python scripts/check_hygiene.py            # 只报不拦（退出码 0），写 assets/out/acceptance/hygiene.txt
    python scripts/check_hygiene.py --strict   # 有命中即退出码 1（提交前 / CI）
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_OUT = os.path.join(ROOT, "assets", "out", "acceptance", "hygiene.txt")
_TZ = timezone(timedelta(hours=8))
SKIP_DIRS = {"__pycache__", ".git", "assets", "docs"}
#: 标准色度学矩阵 / 白点常数允许 6–9 位小数（有明确外部来源，见 metrics.srgb_to_lab）。
SELF_NAME = "check_hygiene.py"

_PIXEL_NAMES = {
    "h", "w", "hh", "ww", "H", "W", "height", "width", "rows", "cols",
    "n_pixels", "num_pixels", "npix", "size",
    "samples", "n_samples", "nsamples", "n_points", "indices",
}
_PIXEL_ATTRS = {"size", "shape"}

RULES: dict[str, dict[str, str]] = {
    "BARE_EXCEPT": {
        "desc": "裸 except（连 Exception 都不写）会吞掉 KeyboardInterrupt/SystemExit 之外的一切",
        "fix": "写明异常类型；必须保留的兜底用 `except Exception as e:` 并记录上下文",
    },
    "SILENT_PASS": {
        "desc": "except 分支直接 pass = 静默失败（错误消失了，结果悄悄变差）",
        "fix": "至少转成 FBSError 或 WarningLog.add(...)，不允许无声吞错",
    },
    "EXCEPT_PRINT_ONLY": {
        "desc": "except 里只 print，不进 WarningLog / 不抛 FBSError",
        "fix": "降级/失败必须进 WarningLog（warn.add）或抛带 code 的 FBSError",
    },
    "OR_DEFAULT_CALL": {
        "desc": "`x or <非零数字>` 会把 0/False/None 一起吞掉（实测 color_field_grid=0 关不掉）",
        "fix": "显式判 `is None`；0 与 False 是合法值",
    },
    "ARGPARSE_DEFAULT": {
        "desc": "CLI add_argument(default=非 None) 会无声覆盖 FBSConfig 默认值（实测 max_curves 8000→2000）",
        "fix": "默认值一律 None，只有显式给出时才覆盖 config",
    },
    "PIXEL_PYLOOP": {
        "desc": "热路径按像素/采样点做 Python 循环（单图耗时差一个数量级）",
        "fix": "用 numpy 向量化/广播；仅在'每条笔画/每帧'一级允许 Python 循环",
    },
    "ROUND_PRECISION_LITERAL": {
        "desc": "手写 round(x, 6..9) 序列化精度与渲染契约脱钩",
        "fix": "精度由 io_format.decimals_for_coef/decimals_for_scalar 反推；否则加 noqa 说明",
    },
    "FLOAT_DECIMALS_6_9": {
        "desc": "6–9 位小数的浮点字面量（疑似手写序列化精度 / 与数据无关的硬编码）",
        "fix": "精度由契约反推；标准常数（色度矩阵等）请注明来源并加 noqa",
    },
    "UTF8_BOM": {
        "desc": "文件带 UTF-8 BOM（契约 §11.9 要求 UTF-8 无 BOM）",
        "fix": "以无 BOM UTF-8 保存（python: open(..., encoding='utf-8')）",
    },
}


def _iter_py_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for fn in sorted(filenames):
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def _has_noqa(src: str, lineno: int) -> bool:
    """命中行或它上 5 行内出现 noqa 即豁免（多行语句/矩阵常数的惯例）。"""
    lines = src.splitlines()
    lo = max(0, lineno - 1 - 5)  # 0-based 起始索引：命中行 + 上 5 行
    hi = min(len(lines), lineno + 1)
    return any("noqa" in lines[i].lower() for i in range(lo, hi))


def _node_source(src: str, node: ast.AST) -> str:
    seg = ast.get_source_segment(src, node)
    if seg:
        return " ".join(seg.split())[:100]
    return ast.dump(node)[:100]


def _scan_ast(path: str, src: str) -> list[dict]:
    """AST 精确规则：静默失败 / or 数字 / argparse 默认值 / 逐点循环 / 手写精度。"""
    hits: list[dict] = []
    rel = os.path.relpath(path, ROOT)

    def add(rule: str, node: ast.AST, extra: str = "") -> None:
        lineno = int(getattr(node, "lineno", 1))
        if _has_noqa(src, lineno):
            return
        hits.append({"rule": rule, "file": rel, "line": lineno,
                     "code": _node_source(src, node), "extra": extra})

    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError as exc:
        hits.append({"rule": "SYNTAX_ERROR", "file": rel, "line": int(exc.lineno or 1),
                     "code": str(exc.msg)[:100], "extra": "文件无法解析（会在运行时报错）"})
        return hits

    has_fbsconfig = "FBSConfig" in src

    for node in ast.walk(tree):
        # ---- 异常处理 ----------------------------------------------------
        if isinstance(node, ast.ExceptHandler):
            if node.type is None:
                add("BARE_EXCEPT", node)
            body = node.body
            if len(body) == 1 and isinstance(body[0], ast.Pass):
                add("SILENT_PASS", node)
            elif (len(body) == 1 and isinstance(body[0], ast.Expr)
                  and isinstance(body[0].value, ast.Call)
                  and isinstance(body[0].value.func, ast.Name)
                  and body[0].value.func.id == "print"):
                add("EXCEPT_PRINT_ONLY", node)
        # ---- x or <非零数字> --------------------------------------------
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            for value in node.values[1:]:
                if isinstance(value, ast.Constant) and isinstance(value.value, (int, float)) \
                        and not isinstance(value.value, bool) and float(value.value) != 0.0:
                    add("OR_DEFAULT_CALL", node, extra=f"右操作数常量 {value.value!r}")
                    break
        # ---- CLI 默认值 --------------------------------------------------
        if isinstance(node, ast.Call) and has_fbsconfig:
            fname = ""
            if isinstance(node.func, ast.Attribute):
                fname = node.func.attr
            elif isinstance(node.func, ast.Name):
                fname = node.func.id
            if fname == "add_argument":
                for kw in node.keywords:
                    if kw.arg == "default" and not (
                        isinstance(kw.value, ast.Constant) and kw.value.value is None
                    ):
                        add("ARGPARSE_DEFAULT", node,
                            extra="default 不是 None → 会覆盖 FBSConfig")
        # ---- 热路径逐点循环 ---------------------------------------------
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Call) \
                and isinstance(node.iter.func, ast.Name) and node.iter.func.id == "range" \
                and node.iter.args:
            arg = node.iter.args[0]
            flagged = (isinstance(arg, ast.Name) and arg.id in _PIXEL_NAMES) or (
                isinstance(arg, ast.Attribute) and arg.attr in _PIXEL_ATTRS)
            if flagged:
                add("PIXEL_PYLOOP", node, extra=f"range({_node_source(src, arg)})")
        # ---- 手写序列化精度 ---------------------------------------------
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "round" and len(node.args) >= 2:
            nd = node.args[1]
            if isinstance(nd, ast.Constant) and isinstance(nd.value, int) \
                    and 6 <= int(nd.value) <= 9:
                add("ROUND_PRECISION_LITERAL", node, extra=f"round(..., {nd.value})")
    return hits


def _scan_float_literals(path: str, src: str) -> list[dict]:
    """6–9 位小数的浮点字面量（用 tokenize，天然跳过注释/字符串）。"""
    import io
    import tokenize

    hits: list[dict] = []
    rel = os.path.relpath(path, ROOT)
    try:
        tokens = tokenize.generate_tokens(io.StringIO(src).readline)
    except tokenize.TokenError:
        return hits
    for tok in tokens:
        if tok.type != tokenize.NUMBER:
            continue
        text = tok.string
        if "." not in text or "e" in text.lower() or "j" in text.lower():
            continue
        frac = text.split(".")[1]
        if 6 <= len(frac) <= 9 and frac.isdigit():
            lineno = int(tok.start[0])
            if _has_noqa(src, lineno):
                continue
            hits.append({"rule": "FLOAT_DECIMALS_6_9", "file": rel, "line": lineno,
                         "code": text, "extra": f"{len(frac)} 位小数"})
    return hits


def scan(root: str) -> tuple[list[dict], list[str]]:
    """扫描 root 下所有 .py；返回 (hits, scanned_files)。"""
    hits: list[dict] = []
    scanned: list[str] = []
    for path in _iter_py_files(root):
        if os.path.basename(path) == SELF_NAME:
            continue
        scanned.append(os.path.relpath(path, root))
        raw = open(path, "rb").read()
        if raw.startswith(b"\xef\xbb\xbf"):
            hits.append({"rule": "UTF8_BOM", "file": os.path.relpath(path, ROOT), "line": 1,
                         "code": "<BOM>", "extra": "文件以 UTF-8 BOM 开头"})
        try:
            src = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            hits.append({"rule": "UTF8_DECODE", "file": os.path.relpath(path, ROOT),
                         "line": 1, "code": str(exc)[:100], "extra": "非 UTF-8 文件"})
            continue
        hits.extend(_scan_ast(path, src))
        hits.extend(_scan_float_literals(path, src))
    hits.sort(key=lambda h: (h["file"], h["line"], h["rule"]))
    return hits, scanned


def render_report(hits: list[dict], scanned: list[str], root: str) -> str:
    now = datetime.now(_TZ).isoformat(timespec="seconds")
    lines = [
        "# fourier-brushstroke v2 静态卫生检查",
        "",
        f"- 时间：{now}",
        f"- 扫描根目录：{root}",
        f"- 扫描 .py 文件数：{len(scanned)}",
        f"- 命中：{len(hits)} 处（0 处 = 通过）",
        "",
    ]
    if not hits:
        lines += ["结论：未发现静默失败 / 契约脱钩 / 热路径循环 / BOM 类问题。", ""]
        with_bom = sorted(f for f in scanned)
        lines += ["扫描的文件：", ""] + [f"- {f}" for f in with_bom] + [""]
        return "\n".join(lines)

    by_rule: dict[str, list[dict]] = {}
    for h in hits:
        by_rule.setdefault(h["rule"], []).append(h)
    lines += ["## 命中明细", ""]
    for rule, items in sorted(by_rule.items()):
        info = RULES.get(rule, {"desc": "未知规则", "fix": ""})
        lines += [f"### {rule}（{len(items)} 处）", "", f"- 为什么有害：{info['desc']}",
                  f"- 怎么改：{info.get('fix', '')}", ""]
        for h in items[:40]:
            lines.append(f"  - `{h['file']}:{h['line']}`  `{h['code']}`"
                         + (f"  （{h['extra']}）" if h.get("extra") else ""))
        if len(items) > 40:
            lines.append(f"  - …其余 {len(items) - 40} 处")
        lines.append("")
    lines += [
        "## 处理约定",
        "",
        "本检查器只报**高置信模式**；确认无害的写法请在命中行（或上两行内）加 `# noqa` 并写明原因，",
        "不要删除规则本身 —— 误报一多检查器就会被忽略，那样真问题也会一起漏掉。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="fourier-brushstroke v2 静态卫生守卫（高置信规则）")
    ap.add_argument("--strict", action="store_true", help="有命中即退出码 1（提交前/CI 用）")
    ap.add_argument("--path", default=ROOT, help=f"扫描根目录（默认技能根 {ROOT}）")
    ap.add_argument("--out", default=DEFAULT_OUT, help="报告输出路径")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    t0 = time.perf_counter()
    root = os.path.abspath(args.path)
    hits, scanned = scan(root)
    text = render_report(hits, scanned, root)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text + "\n")
    if not args.quiet:
        print(text)
        print(f"[hygiene] 报告: {os.path.relpath(args.out, ROOT)}  "
              f"({time.perf_counter() - t0:.2f}s)")
    # 供上游（验收报告）机读的摘要
    summary_path = os.path.join(os.path.dirname(os.path.abspath(args.out)), "hygiene_summary.json")
    try:
        with open(summary_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"hits": len(hits),
                       "by_rule": {k: sum(1 for h in hits if h["rule"] == k)
                                   for k in sorted({h["rule"] for h in hits})},
                       "scanned_files": len(scanned),
                       "out": os.path.relpath(args.out, ROOT)}, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
    except OSError as exc:
        print(f"[hygiene] 写 summary 失败（不阻塞）：{exc}", file=sys.stderr)
    return 1 if (args.strict and hits) else 0


if __name__ == "__main__":
    raise SystemExit(main())



