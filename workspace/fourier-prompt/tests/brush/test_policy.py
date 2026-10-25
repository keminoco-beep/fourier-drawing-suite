"""``fourier_prompt.brush.policy`` 测试：角色→池/压力档/风格包/未知 fail-loud/planned 排除。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fourier_prompt.brush.catalog import ROLES, BrushCatalog  # noqa: E402
from fourier_prompt.brush.policy import (  # noqa: E402
    ROLE_POOLS,
    ROLE_PRESSURE,
    TRAIT_KEYS,
    UnknownRoleError,
    UnknownStyleError,
    choose_brush,
    explain_choice,
    load_style_packs,
    pool_for,
    pressure_for,
    validate_policy,
)

CONTRACT_ROLES = (
    "bed", "wash", "line_hairline", "line_taper", "line_broad",
    "detail", "texture", "effect", "marker", "sketch",
)


class TestRoles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = BrushCatalog.load()

    def test_roles_match_contract(self):
        self.assertEqual(ROLES, CONTRACT_ROLES)
        self.assertEqual(set(ROLE_POOLS), set(CONTRACT_ROLES))
        self.assertEqual(set(ROLE_PRESSURE), set(CONTRACT_ROLES))

    def test_every_role_chooses_renderable(self):
        active = set(self.cat.ids())
        planned = set(self.cat.planned_ids())
        for role in ROLES:
            pid = choose_brush(role, catalog=self.cat)
            self.assertIn(pid, active, f"{role} 选到了非 active 笔 {pid}")
            self.assertNotIn(pid, planned)

    def test_default_choices_follow_design_table(self):
        # 设计 13 §3.5 的池顺序 + 13a §3.1 实测分组
        self.assertEqual(choose_brush("bed", catalog=self.cat), "basic_round_hard")
        self.assertEqual(choose_brush("line_hairline", catalog=self.cat), "ink_fineliner")
        self.assertEqual(choose_brush("line_taper", catalog=self.cat), "ink_gpen")
        self.assertEqual(choose_brush("line_broad", catalog=self.cat), "sumi_e")
        self.assertEqual(choose_brush("sketch", catalog=self.cat), "pencil_hb")
        self.assertEqual(choose_brush("texture", catalog=self.cat), "chalk")
        self.assertEqual(choose_brush("effect", catalog=self.cat), "basic_airbrush")
        self.assertEqual(choose_brush("marker", catalog=self.cat), "marker_chisel")

    def test_unknown_role_fails_loud(self):
        with self.assertRaises(UnknownRoleError) as ctx:
            choose_brush("line_detail")
        self.assertIsInstance(ctx.exception, KeyError)
        self.assertIn("line_detail", str(ctx.exception))
        with self.assertRaises(KeyError):
            pressure_for("erase")

    def test_pressure_bands(self):
        for role in ROLES:
            lo, hi = pressure_for(role)
            self.assertTrue(0.0 <= lo < hi <= 1.0, f"{role}: {(lo, hi)}")
        # 13a §3.1：线稿 p∈[0.55,0.85]、铺底 p∈[0.7,1.0]、细节 p∈[0.2,0.7]
        self.assertEqual(pressure_for("line_hairline"), (0.60, 0.85))
        self.assertEqual(pressure_for("bed"), (0.70, 1.00))
        self.assertEqual(pressure_for("detail"), (0.20, 0.70))
        self.assertLess(pressure_for("line_taper")[0], pressure_for("line_taper")[1])
        # 提按角色的动态范围应大于均匀线角色
        taper_span = pressure_for("line_taper")[1] - pressure_for("line_taper")[0]
        hair_span = pressure_for("line_hairline")[1] - pressure_for("line_hairline")[0]
        self.assertGreater(taper_span, hair_span)


class TestPoolsAndTraits(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = BrushCatalog.load()

    def test_pool_ids_are_active_and_declare_role(self):
        for role, pool in ROLE_POOLS.items():
            self.assertGreaterEqual(len(pool), 2, role)
            for pid in pool:
                meta = self.cat.get(pid)
                self.assertEqual(meta["status"], "active")
                self.assertIn(role, meta["roles"], f"{pid} 未声明 {role}")

    def test_pool_for_never_contains_planned(self):
        planned = set(self.cat.planned_ids())
        for role in ROLES:
            for pid in pool_for(role, catalog=self.cat):
                self.assertNotIn(pid, planned)

    def test_explicit_width_gain_trait_changes_pick(self):
        # 需要"提按强（W+）"时，line_taper 应从 ink_gpen（W~）切到 sumi_e（W+ 60%）
        default = choose_brush("line_taper", catalog=self.cat)
        high = choose_brush("line_taper", traits={"width_gain": "high"}, catalog=self.cat)
        self.assertNotEqual(default, high)
        self.assertEqual(high, "sumi_e")
        self.assertIn("W+", self.cat.get(high)["traits"])

    def test_trait_low_gain_keeps_hairline(self):
        pid = choose_brush("line_hairline", traits={"width_gain": "low", "alpha_dynamic": True},
                           catalog=self.cat)
        self.assertEqual(pid, "ink_fineliner")

    def test_unknown_trait_key_fails_loud(self):
        with self.assertRaises(ValueError) as ctx:
            choose_brush("bed", traits={"graininess": True}, catalog=self.cat)
        self.assertIn("graininess", str(ctx.exception))
        with self.assertRaises(ValueError):
            choose_brush("bed", traits={"width_gain": "extreme"}, catalog=self.cat)

    def test_trait_keys_are_documented(self):
        self.assertEqual(set(TRAIT_KEYS), {"width_gain", "alpha_dynamic", "textured", "soft_edge", "aniso"})

    def test_width_filter_is_deterministic(self):
        wide = choose_brush("bed", width_px=120, catalog=self.cat)
        narrow = choose_brush("detail", width_px=3, catalog=self.cat)
        self.assertIn(wide, self.cat.ids())
        self.assertIn(narrow, self.cat.ids())
        self.assertIn("detail", self.cat.get(narrow)["roles"])
        with self.assertRaises(ValueError):
            choose_brush("bed", width_px=0, catalog=self.cat)

    def test_repeatable(self):
        for role in ROLES:
            self.assertEqual(choose_brush(role, catalog=self.cat), choose_brush(role, catalog=self.cat))

    def test_explain_choice_consistent(self):
        info = explain_choice("line_taper", traits={"width_gain": "high"}, catalog=self.cat)
        self.assertEqual(info["chosen"], choose_brush("line_taper", traits={"width_gain": "high"},
                                                      catalog=self.cat))
        self.assertEqual(info["pressure"], [0.3, 0.9])
        self.assertTrue(info["candidates"])
        scores = [c["score"] for c in info["candidates"]]
        best = info["candidates"][scores.index(max(scores))]["id"]
        self.assertEqual(best, info["chosen"])


class TestStylePacks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = BrushCatalog.load()
        cls.packs = load_style_packs()

    def test_six_packs_present(self):
        ids = set(self.packs["packs"])
        self.assertEqual(ids, {"ink_wash", "pencil", "marker", "oil", "manga", "watercolor"})
        for pid, pack in self.packs["packs"].items():
            self.assertTrue(pack.get("name_zh"), pid)
            self.assertTrue(pack.get("pools"), pid)

    def test_style_changes_pick(self):
        # 水墨风格下 line_taper 首选 sumi_e；漫画风格下回落 ROLE_POOLS 首选 ink_gpen
        self.assertEqual(choose_brush("line_taper", style="ink_wash", catalog=self.cat), "sumi_e")
        self.assertEqual(choose_brush("line_taper", style="manga", catalog=self.cat), "ink_gpen")

    def test_style_alias(self):
        for alias in ("水墨", "sumi", "INK"):
            self.assertEqual(choose_brush("line_taper", style=alias, catalog=self.cat), "sumi_e")

    def test_unknown_style_fails_loud(self):
        with self.assertRaises(UnknownStyleError) as ctx:
            choose_brush("bed", style="gongbi")
        self.assertIsInstance(ctx.exception, KeyError)
        self.assertIn("gongbi", str(ctx.exception))

    def test_style_pools_have_no_planned_or_unknown(self):
        planned = set(self.cat.planned_ids())
        active = set(self.cat.ids())
        for pack_id, pack in self.packs["packs"].items():
            for role, ids in (pack.get("pools") or {}).items():
                self.assertIn(role, ROLES, f"{pack_id} 未知角色 {role}")
                for bid in ids:
                    self.assertNotIn(bid, planned, f"{pack_id}/{role} 混入 planned {bid}")
                    self.assertIn(bid, active, f"{pack_id}/{role} 引用未知笔 {bid}")

    def test_every_style_resolves_for_core_roles(self):
        for pack_id in self.packs["packs"]:
            for role in ("line_hairline", "detail", "bed"):
                pid = choose_brush(role, style=pack_id, catalog=self.cat)
                self.assertIn(pid, self.cat.ids())


class TestValidatePolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = BrushCatalog.load()

    def test_validate_ok(self):
        self.assertEqual(validate_policy(catalog=self.cat), [])

    def test_detects_planned_in_pool(self):
        import copy

        import fourier_prompt.brush.policy as policy_mod

        original = policy_mod.ROLE_POOLS
        data = {k: tuple(v) for k, v in original.items()}
        data["bed"] = data["bed"] + ("fill_round",)
        policy_mod.ROLE_POOLS = data
        try:
            errs = validate_policy(catalog=self.cat)
        finally:
            policy_mod.ROLE_POOLS = original
        self.assertTrue(any("planned" in e for e in errs), errs)
        # 且 choose_brush 运行期也不会选中它（pool_for 过滤 planned）
        self.assertNotEqual(choose_brush("bed", catalog=self.cat), "fill_round")

    def test_detects_unknown_id_in_pool(self):
        import fourier_prompt.brush.policy as policy_mod

        original = policy_mod.ROLE_POOLS
        data = {k: tuple(v) for k, v in original.items()}
        data["detail"] = data["detail"] + ("ink_ballpen",)
        policy_mod.ROLE_POOLS = data
        try:
            errs = validate_policy(catalog=self.cat)
        finally:
            policy_mod.ROLE_POOLS = original
        self.assertTrue(any("ink_ballpen" in e for e in errs), errs)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
