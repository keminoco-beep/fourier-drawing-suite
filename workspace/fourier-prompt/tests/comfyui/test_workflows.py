"""Static validation of the four ComfyUI workflow templates (WP-L).

ComfyUI is not installed on this machine, so the templates are checked as data:
node/type legality, port declarations, link integrity and widget arity.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from _fixtures import CUSTOM_NODES, NODES

WORKFLOW_DIR = CUSTOM_NODES / "workflows"
WORKFLOWS = ("fdsl_to_fdraw.json", "fdraw_to_image.json", "fdraw_to_video.json", "batch_fdraw.json")
WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN"}
CORE_NODES = {
    "PreviewImage": {"inputs": [("images", "IMAGE")], "outputs": [], "widgets": 0},
    "SaveImage": {"inputs": [("images", "IMAGE")], "outputs": [], "widgets": 1},
}


def declarations() -> dict:
    table = {}
    for name, cls in NODES.items():
        inputs = []
        widgets = 0
        spec = cls.INPUT_TYPES()
        for section in ("required", "optional"):
            for input_name, definition in spec.get(section, {}).items():
                kind = definition[0]
                if isinstance(kind, list) or kind in WIDGET_TYPES:
                    widgets += 1
                    continue
                inputs.append((input_name, kind))
        outputs = [
            (name_, kind)
            for name_, kind in zip(
                getattr(cls, "RETURN_NAMES", cls.RETURN_TYPES), cls.RETURN_TYPES
            )
        ]
        table[name] = {"inputs": inputs, "outputs": outputs, "widgets": widgets}
    table.update(CORE_NODES)
    return table


TABLE = declarations()


def check_workflow(test: unittest.TestCase, payload: dict, name: str) -> None:
    for key in ("nodes", "links", "groups", "config", "last_node_id", "last_link_id", "version"):
        test.assertIn(key, payload, name)
    test.assertIsInstance(payload["nodes"], list)
    test.assertIsInstance(payload["links"], list)
    test.assertIsInstance(payload["groups"], list)
    test.assertIsInstance(payload["config"], dict)
    test.assertGreaterEqual(len(payload["nodes"]), 3, name)

    by_id = {}
    for item in payload["nodes"]:
        for key in ("id", "type", "pos", "size", "order", "mode", "inputs", "outputs", "widgets_values"):
            test.assertIn(key, item, f"{name}: {item.get('id')}")
        test.assertNotIn(item["id"], by_id, f"duplicate node id {item['id']}")
        by_id[item["id"]] = item
        test.assertIn(item["type"], TABLE, f"{name}: unknown node type {item['type']}")
        test.assertEqual(len(item["pos"]), 2)
        test.assertEqual(len(item["size"]), 2)
        declared = TABLE[item["type"]]
        actual_inputs = [(port["name"], port["type"]) for port in item["inputs"]]
        test.assertEqual(actual_inputs, declared["inputs"], f"{name}: inputs of {item['type']}")
        actual_outputs = [(port["name"], port["type"]) for port in item["outputs"]]
        test.assertEqual(actual_outputs, declared["outputs"], f"{name}: outputs of {item['type']}")
        test.assertEqual(
            len(item["widgets_values"]),
            declared["widgets"],
            f"{name}: widget count of {item['type']}",
        )

    test.assertEqual(payload["last_node_id"], max(by_id), name)
    test.assertGreaterEqual(payload["last_node_id"], max(by_id))
    test.assertEqual(len({link[0] for link in payload["links"]}), len(payload["links"]), "duplicate link id")
    test.assertEqual(payload["last_link_id"], max(link[0] for link in payload["links"]), name)

    referenced = set()
    for link in payload["links"]:
        test.assertEqual(len(link), 6, f"{name}: link {link}")
        link_id, origin, origin_slot, target, target_slot, link_type = link
        test.assertIn(origin, by_id, f"{name}: link {link_id} origin")
        test.assertIn(target, by_id, f"{name}: link {link_id} target")
        origin_node = by_id[origin]
        target_node = by_id[target]
        test.assertLess(origin_slot, len(origin_node["outputs"]), f"{name}: link {link_id} origin slot")
        test.assertLess(target_slot, len(target_node["inputs"]), f"{name}: link {link_id} target slot")
        test.assertEqual(origin_node["outputs"][origin_slot]["type"], link_type)
        test.assertEqual(target_node["inputs"][target_slot]["type"], link_type)
        test.assertIn(link_id, origin_node["outputs"][origin_slot]["links"])
        test.assertEqual(target_node["inputs"][target_slot]["link"], link_id)
        referenced.add(link_id)

    for item in payload["nodes"]:
        for port in item["outputs"]:
            for link_id in port["links"]:
                test.assertIn(link_id, referenced, f"{name}: dangling output link {link_id}")
        for port in item["inputs"]:
            if port["link"] is not None:
                test.assertIn(port["link"], referenced, f"{name}: dangling input link")


class TestWorkflowTemplates(unittest.TestCase):
    def test_exactly_four_files(self):
        files = sorted(path.name for path in WORKFLOW_DIR.glob("*.json"))
        self.assertEqual(files, sorted(WORKFLOWS))

    def test_all_templates_parse_and_validate(self):
        for name in WORKFLOWS:
            with self.subTest(workflow=name):
                path = WORKFLOW_DIR / name
                payload = json.loads(path.read_text(encoding="utf-8"))
                check_workflow(self, payload, name)

    def test_fdsl_to_fdraw_graph(self):
        payload = json.loads((WORKFLOW_DIR / "fdsl_to_fdraw.json").read_text(encoding="utf-8"))
        types = [item["type"] for item in payload["nodes"]]
        self.assertIn("FDPCompileFDSL", types)
        self.assertIn("FDPRenderFdraw", types)
        self.assertIn("FDPSaveFdraw", types)
        self.assertIn("PreviewImage", types)
        link_types = {link[5] for link in payload["links"]}
        self.assertIn("FDRAW", link_types)
        self.assertIn("IMAGE", link_types)

    def test_fdraw_to_image_graph(self):
        payload = json.loads((WORKFLOW_DIR / "fdraw_to_image.json").read_text(encoding="utf-8"))
        types = [item["type"] for item in payload["nodes"]]
        self.assertEqual(types[0], "FDPLoadFdraw")
        self.assertIn("FDPRenderFdraw", types)
        self.assertIn("PreviewImage", types)
        self.assertIn("SaveImage", types)
        self.assertEqual(sum(1 for link in payload["links"] if link[5] == "IMAGE"), 2)

    def test_fdraw_to_video_graph(self):
        payload = json.loads((WORKFLOW_DIR / "fdraw_to_video.json").read_text(encoding="utf-8"))
        video = next(item for item in payload["nodes"] if item["type"] == "FDPVideoFdraw")
        self.assertEqual(video["outputs"][0]["type"], "STRING")
        self.assertEqual(video["outputs"][0]["name"], "mp4_path")
        # duration=20, fps=24, resolution=256, quality, max_frames=600, background, prefix
        self.assertEqual(video["widgets_values"][0], 20.0)
        self.assertEqual(video["widgets_values"][1], 24)
        self.assertEqual(video["widgets_values"][2], 256)

    def test_batch_graph_uses_list_outputs(self):
        payload = json.loads((WORKFLOW_DIR / "batch_fdraw.json").read_text(encoding="utf-8"))
        batch = next(item for item in payload["nodes"] if item["type"] == "FDPBatchFdraw")
        self.assertIn("\n", batch["widgets_values"][0])
        self.assertEqual(len(batch["widgets_values"][0].splitlines()), 3)
        render = next(item for item in payload["nodes"] if item["type"] == "FDPRenderFdraw")
        self.assertEqual(render["inputs"][0]["type"], "FDRAW")
        batch_links = [link for link in payload["links"] if link[1] == batch["id"]]
        self.assertEqual(len(batch_links), 2)
        self.assertTrue(all(link[5] == "FDRAW" for link in batch_links))


if __name__ == "__main__":
    unittest.main()
