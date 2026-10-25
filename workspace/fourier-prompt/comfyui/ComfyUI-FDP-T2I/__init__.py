"""ComfyUI-FDP-T2I — 把「Krea2 扩散出图 → 傅里叶笔触 → .fdraw → 渲染」接进 ComfyUI。

安装：
  1) 把本目录复制/链接到 ``ComfyUI/custom_nodes/ComfyUI-FDP-T2I``；
  2) 重启 ComfyUI（或让 ComfyUI-Manager 重新加载）；
  3) 打开 ``workflows/FDP_T2I_Krea2_fdraw_UI.json``（或从 ComfyUI 的 workflow 列表打开）。

设计：
  - 扩散部分只用 ComfyUI 自带核心节点（UNETLoader / CLIPLoader / VAELoader /
    CLIPTextEncode / EmptyLatentImage / KSampler / VAEDecode）；
  - 缺少的功能（位图→.fbs、.fbs→.fdraw、.fdraw→IMAGE、作画视频）由本包补齐；
  - 工程侧调用外部 Python 子进程，不污染 ComfyUI 的 Python 环境。
"""

from __future__ import annotations

import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_nodes_module():
    spec = importlib.util.spec_from_file_location(
        "ComfyUI_FDP_T2I_nodes", os.path.join(_HERE, "nodes.py")
    )
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError("ComfyUI-FDP-T2I: 无法加载 nodes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_nodes = _load_nodes_module()

NODE_CLASS_MAPPINGS = _nodes.NODE_CLASS_MAPPINGS
NODE_DISPLAY_NAME_MAPPINGS = _nodes.NODE_DISPLAY_NAME_MAPPINGS
WEB_DIRECTORY = None

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
