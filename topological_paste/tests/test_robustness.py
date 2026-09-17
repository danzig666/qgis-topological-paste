# -*- coding: utf-8 -*-
"""Acceptance tests K (geometry robustness) and the special-geometry policy."""

import unittest

from qgis.core import QgsGeometry

from topological_paste.geometry_ops import GeometryOps
from topological_paste.model import Issue, PlanError, PlanOptions
from topological_paste.planner import build_plan
from topological_paste.tests.utils import (
    QgisTestCase,
    by_attribute,
    layer_union,
    make_layer,
    make_snapshot,
    max_pairwise_overlap,
    paste,
    state,
    wkt_rect,
)

BOWTIE = "POLYGON((0 0,10 10,10 0,0 10,0 0))"


class FailingDifference(GeometryOps):
    """GEOS 'fails': returns a null geometry, exactly what QGIS hands back on an error."""

    def _raw_difference(self, geometry, mask):
        return QgsGeometry()


class TestRobustness(QgisTestCase):
    def test_k_repairable_incoming_needs_acceptance(self):
        layer = make_layer(rows=[(wkt_rect(0, 0, 10, 4), ["old", 1])])
        plan = build_plan(layer, make_snapshot([(BOWTIE, {"name": "bow", "n": 2})]))
        self.assertFalse(plan.blocking())
        codes = [i.code for i in plan.needs_acceptance()]
        self.assertIn("repaired_incoming", codes)  # material repair: explicit acceptance required
        _plan, _result = paste(layer, make_snapshot([(BOWTIE, {"name": "bow", "n": 2})]))
        bows = by_attribute(layer, "name", "bow")
        # both triangles of the repaired bow-tie are preserved
        self.assertAlmostEqual(sum(f.geometry().area() for f in bows), 50.0)
        self.assertLessEqual(max_pairwise_overlap(layer), 1e-9)

    def test_k_unrepairable_incoming_blocks(self):
        layer = make_layer(rows=[(wkt_rect(0, 0, 10, 10), ["old", 1])])
        before = state(layer)
        collapsed = "POLYGON((0 0,10 0,20 0,0 0))"  # zero area: repair collapses to a line
        plan, result = paste(layer, make_snapshot([(collapsed, {"name": "bad", "n": 2})]), expect_blocking=True)
        self.assertIsNone(result)
        self.assertTrue(any(i.code == "invalid_incoming" for i in plan.blocking()))
        self.assertEqual(state(layer), before)
        self.assertFalse(layer.isEditable())

    def test_k_invalid_existing_is_repaired_on_a_copy(self):
        layer = make_layer(rows=[(BOWTIE, ["bow", 1])])
        plan = build_plan(layer, make_snapshot([(wkt_rect(-1, -1, 5, 11), {"name": "new", "n": 2})]))
        self.assertFalse(plan.blocking())
        self.assertTrue(any(i.code == "repaired_existing" for i in plan.needs_acceptance()))

    def test_k_tiny_valid_fragments_are_kept(self):
        # a 1 mm wide sliver of the old polygon survives: it must not be "cleaned away"
        layer = make_layer(rows=[(wkt_rect(0, 0, 10, 10), ["old", 1])])
        paste(layer, make_snapshot([(wkt_rect(0.001, -1, 20, 11), {"name": "new", "n": 2})]))
        old = by_attribute(layer, "name", "old")
        self.assertEqual(len(old), 1)
        self.assertAlmostEqual(old[0].geometry().area(), 0.01, places=9)
        # and a tiny incoming polygon is pasted like any other
        tiny = "POLYGON((3 3,3.0001 3,3.0001 3.0001,3 3.0001,3 3))"
        paste(layer, make_snapshot([(tiny, {"name": "tiny", "n": 3})]))
        self.assertEqual(len(by_attribute(layer, "name", "tiny")), 1)

    def test_k_near_coincident_edges_are_not_snapped(self):
        layer = make_layer(rows=[(wkt_rect(0, 0, 10, 10), ["old", 1])])
        # 1e-7 away from the old edge: a real (if tiny) gap, which is preserved, not snapped
        plan, _ = paste(layer, make_snapshot([(wkt_rect(10.0000001, 0, 20, 10), {"name": "new", "n": 2})]))
        self.assertEqual(plan.count_trimmed(), 0)
        old = by_attribute(layer, "name", "old")[0].geometry()
        new = by_attribute(layer, "name", "new")[0].geometry()
        self.assertEqual(old.boundingBox().xMaximum(), 10.0)
        self.assertEqual(new.boundingBox().xMinimum(), 10.0000001)
        # 1e-7 overlap: a real sliver, trimmed exactly, no vertex moved
        layer2 = make_layer(rows=[(wkt_rect(0, 0, 10, 10), ["old", 1])])
        paste(layer2, make_snapshot([(wkt_rect(9.9999999, 0, 20, 10), {"name": "new", "n": 2})]))
        self.assertEqual(by_attribute(layer2, "name", "old")[0].geometry().boundingBox().xMaximum(), 9.9999999)
        self.assertLessEqual(max_pairwise_overlap(layer2), 1e-12)

    def test_k_forced_geometry_error_never_deletes(self):
        layer = make_layer(rows=[(wkt_rect(0, 0, 10, 10), ["old", 1])])
        before = state(layer)
        plan, result = paste(
            layer, make_snapshot([(wkt_rect(5, 0, 15, 10), {"name": "new", "n": 2})]), ops=FailingDifference(),
            expect_blocking=True,
        )
        self.assertIsNone(result)
        self.assertTrue(any(i.code == "geometry_op" for i in plan.blocking()))
        self.assertFalse(any(c.delete for c in plan.changes.values()))  # failure != "nothing remains"
        self.assertEqual(state(layer), before)

    def test_legitimate_empty_difference_is_not_an_error(self):
        # same FailingDifference ops: full coverage is decided by the relate test, not by the result
        layer = make_layer(rows=[(wkt_rect(2, 2, 8, 8), ["old", 1])])
        plan, _ = paste(layer, make_snapshot([(wkt_rect(0, 0, 10, 10), {"name": "new", "n": 2})]), ops=FailingDifference())
        self.assertEqual(plan.count_deleted(), 1)

    def test_precision_grid_is_opt_in_and_consistent(self):
        rows = [(wkt_rect(0, 0, 10.0004, 10), ["old", 1]), (wkt_rect(10.0004, 0, 20, 10), ["nb", 2])]
        incoming = [("POLYGON((5.0003 -1,15.0002 -1,15.0002 5.0004,5.0003 5.0004,5.0003 -1))", {"name": "new", "n": 3})]
        # default: coordinates preserved bit by bit
        layer = make_layer(rows=rows)
        paste(layer, make_snapshot(incoming))
        self.assertEqual(by_attribute(layer, "name", "new")[0].geometry().boundingBox().xMinimum(), 5.0003)
        # grid: needs acceptance, everything involved ends on the same grid, coverage re-validated
        layer = make_layer(rows=rows)
        plan = build_plan(layer, make_snapshot(incoming), PlanOptions(grid_size=0.001))
        self.assertFalse(plan.blocking(), [i.message for i in plan.blocking()])
        self.assertTrue(any(i.code.startswith("grid") for i in plan.needs_acceptance()))
        paste(layer, make_snapshot(incoming), PlanOptions(grid_size=0.001))
        for feature in layer.getFeatures():
            for vertex in feature.geometry().vertices():
                self.assertAlmostEqual(vertex.x() * 1000, round(vertex.x() * 1000), places=6)
                self.assertAlmostEqual(vertex.y() * 1000, round(vertex.y() * 1000), places=6)
        self.assertLessEqual(max_pairwise_overlap(layer), 1e-9)
        self.assertAlmostEqual(layer_union(layer).area(), 200.0 + 10.0, places=2)


class TestSpecialGeometry(QgisTestCase):
    def test_family_mismatch_is_rejected_before_editing(self):
        layer = make_layer(rows=[(wkt_rect(0, 0, 10, 10), ["old", 1])])
        with self.assertRaises(PlanError):
            build_plan(layer, make_snapshot([("LINESTRING(0 0,10 10)", {"name": "line", "n": 1})]))
        self.assertFalse(layer.isEditable())

    def test_curved_incoming_is_rejected(self):
        layer = make_layer(rows=[])
        curved = "CURVEPOLYGON(CIRCULARSTRING(0 0,5 5,10 0,5 -5,0 0))"
        with self.assertRaises(PlanError):
            build_plan(layer, make_snapshot([(curved, {"name": "c", "n": 1})]))

    def test_z_target_is_rejected(self):
        layer = make_layer(wkb="PolygonZ", rows=[])
        with self.assertRaises(PlanError):
            build_plan(layer, make_snapshot([(wkt_rect(0, 0, 1, 1), {"name": "c", "n": 1})]))

    def test_incoming_z_is_dropped_only_with_acceptance(self):
        layer = make_layer(rows=[])
        plan = build_plan(layer, make_snapshot([("POLYGONZ((0 0 5,10 0 5,10 10 5,0 10 5,0 0 5))", {"name": "z", "n": 1})]))
        self.assertTrue(any(i.code == "dropped_zm" and i.severity == Issue.ACCEPT for i in plan.issues))
        self.assertFalse(plan.blocking())


if __name__ == "__main__":
    unittest.main()
