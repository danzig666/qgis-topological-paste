# -*- coding: utf-8 -*-
"""Acceptance tests R (lines and points)."""

import unittest

from topological_paste.tests.utils import (
    QgisTestCase,
    by_attribute,
    features_of,
    make_layer,
    make_snapshot,
    paste,
    state,
)


def vertices(geometry):
    return [(v.x(), v.y()) for v in geometry.vertices()]


class TestLines(QgisTestCase):
    def test_r_partial_coincident_line(self):
        layer = make_layer(wkb="LineString", rows=[("LINESTRING(0 0,10 0,20 0)", ["old", 1])])
        plan, _ = paste(layer, make_snapshot([("LINESTRING(12 0,30 0)", {"name": "new", "n": 2})]))
        old = by_attribute(layer, "name", "old")[0].geometry()
        new = by_attribute(layer, "name", "new")[0].geometry()
        self.assertAlmostEqual(old.length(), 12.0)  # the coincident portion 12..20 was removed
        self.assertEqual(vertices(old), [(0.0, 0.0), (10.0, 0.0), (12.0, 0.0)])  # direction and vertices kept
        self.assertAlmostEqual(new.length(), 18.0)
        self.assertEqual(vertices(new), [(12.0, 0.0), (30.0, 0.0)])  # pasted line is inserted as it is
        self.assertEqual(plan.count_trimmed(), 1)

    def test_r_line_cut_in_the_middle(self):
        for wkb, expected_features in (("LineString", 2), ("MultiLineString", 1)):
            with self.subTest(target=wkb):
                layer = make_layer(wkb=wkb, rows=[("LINESTRING(0 0,30 0)", ["old", 1])])
                paste(layer, make_snapshot([("LINESTRING(10 0,20 0)", {"name": "new", "n": 2})]))
                old = by_attribute(layer, "name", "old")
                self.assertEqual(len(old), expected_features)  # every uncovered portion survives
                self.assertAlmostEqual(sum(f.geometry().length() for f in old), 20.0)

    def test_r_perpendicular_crossing_deletes_nothing_but_nodes(self):
        layer = make_layer(wkb="LineString", rows=[("LINESTRING(0 0,20 0)", ["old", 1])])
        plan, _ = paste(layer, make_snapshot([("LINESTRING(5 -5,5 5)", {"name": "new", "n": 2})]))
        self.assertEqual(plan.count_trimmed(), 0)
        self.assertEqual(plan.count_deleted(), 0)
        self.assertEqual(layer.featureCount(), 2)
        old = by_attribute(layer, "name", "old")[0].geometry()
        new = by_attribute(layer, "name", "new")[0].geometry()
        self.assertAlmostEqual(old.length(), 20.0)
        self.assertAlmostEqual(new.length(), 10.0)
        # consistent 2D noding: the crossing point is a vertex of BOTH lines, neither is split
        self.assertEqual(vertices(old), [(0.0, 0.0), (5.0, 0.0), (20.0, 0.0)])
        self.assertEqual(vertices(new), [(5.0, -5.0), (5.0, 0.0), (5.0, 5.0)])

    def test_r_identical_line_reversed_is_reused(self):
        layer = make_layer(wkb="LineString", rows=[("LINESTRING(0 0,10 0,20 5)", ["old", 1])])
        before = state(layer)
        plan, _ = paste(layer, make_snapshot([("LINESTRING(20 5,10 0,0 0)", {"name": "old", "n": 1})]))
        self.assertTrue(plan.is_noop())
        self.assertEqual(state(layer), before)

    def test_r_incoming_priority_between_lines(self):
        layer = make_layer(wkb="LineString", rows=[])
        snapshot = make_snapshot(
            [("LINESTRING(0 0,20 0)", {"name": "first", "n": 1}), ("LINESTRING(10 0,30 0)", {"name": "last", "n": 2})]
        )
        paste(layer, snapshot)
        self.assertAlmostEqual(by_attribute(layer, "name", "first")[0].geometry().length(), 10.0)
        self.assertAlmostEqual(by_attribute(layer, "name", "last")[0].geometry().length(), 20.0)


class TestPoints(QgisTestCase):
    def test_r_duplicate_points(self):
        layer = make_layer(wkb="Point", rows=[("POINT(1 1)", ["old", 1]), ("POINT(9 9)", ["other", 2])])
        snapshot = make_snapshot(
            [
                ("POINT(1 1)", {"name": "first", "n": 10}),
                ("POINT(1 1)", {"name": "last", "n": 11}),  # duplicate inside the batch: last wins
                ("POINT(5 5)", {"name": "fresh", "n": 12}),
            ]
        )
        plan, _ = paste(layer, snapshot)
        self.assertEqual(plan.superseded, [0])
        self.assertEqual(layer.featureCount(), 3)
        at_origin = [f for f in layer.getFeatures() if f.geometry().asPoint().x() == 1.0]
        self.assertEqual(len(at_origin), 1)
        self.assertEqual(at_origin[0].id(), 1)  # the coincident existing point is updated, not duplicated
        self.assertEqual(at_origin[0]["name"], "last")
        self.assertEqual(by_attribute(layer, "name", "other")[0]["n"], 2)
        again, _ = paste(layer, snapshot)
        self.assertTrue(again.is_noop())

    def test_r_partially_coincident_multipoints(self):
        layer = make_layer(wkb="MultiPoint", rows=[("MULTIPOINT((0 0),(1 1),(2 2))", ["old", 1])])
        paste(layer, make_snapshot([("MULTIPOINT((1 1),(5 5))", {"name": "new", "n": 2})]))
        old = by_attribute(layer, "name", "old")[0].geometry()
        new = by_attribute(layer, "name", "new")[0].geometry()
        self.assertEqual(sorted((p.x(), p.y()) for p in old.asMultiPoint()), [(0.0, 0.0), (2.0, 2.0)])
        self.assertEqual(sorted((p.x(), p.y()) for p in new.asMultiPoint()), [(1.0, 1.0), (5.0, 5.0)])

    def test_r_multipoint_into_single_point_layer(self):
        layer = make_layer(wkb="Point", rows=[])
        paste(layer, make_snapshot([("MULTIPOINT((1 1),(5 5))", {"name": "m", "n": 2})]))
        self.assertEqual(layer.featureCount(), 2)
        self.assertEqual(set(f["name"] for f in features_of(layer)), {"m"})


if __name__ == "__main__":
    unittest.main()
