# -*- coding: utf-8 -*-
"""Acceptance tests A-J and S (polygon overwrite semantics, reuse, noding)."""

import unittest

from qgis.core import QgsGeometry

from topological_paste.tests.utils import (
    PROVIDERS,
    QgisTestCase,
    by_attribute,
    features_of,
    layer_union,
    make_layer,
    make_snapshot,
    max_pairwise_overlap,
    paste,
    state,
    union_of,
    wkt_rect,
)


class TestPolygonOverwrite(QgisTestCase):
    def test_a_partial_overlap(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                layer = make_layer(provider, rows=[(wkt_rect(0, 0, 10, 10), ["old", 1])])
                snapshot = make_snapshot([(wkt_rect(5, 0, 15, 10), {"name": "new", "n": 2})])
                plan, result = paste(layer, snapshot)
                self.assertEqual(plan.count_trimmed(), 1)
                self.assertEqual(plan.count_deleted(), 0)
                old = by_attribute(layer, "name", "old")
                new = by_attribute(layer, "name", "new")
                self.assertEqual((len(old), len(new)), (1, 1))
                self.assertAreaAlmost(old[0].geometry(), 50.0)
                self.assertAreaAlmost(new[0].geometry(), 100.0)
                self.assertAreaAlmost(layer_union(layer), 150.0)
                self.assertEqual(old[0]["n"], 1)
                self.assertEqual(new[0]["n"], 2)
                self.assertLessEqual(max_pairwise_overlap(layer), 1e-12)
                self.assertTrue(layer.isEditable())
                self.assertTrue(layer.isModified())  # never committed automatically

    def test_b_full_coverage_deletes_existing(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                layer = make_layer(provider, rows=[(wkt_rect(2, 2, 8, 8), ["old", 1]), (wkt_rect(100, 100, 110, 110), ["far", 9])])
                plan, _ = paste(layer, make_snapshot([(wkt_rect(0, 0, 10, 10), {"name": "new", "n": 2})]))
                self.assertEqual(plan.count_deleted(), 1)
                self.assertEqual(by_attribute(layer, "name", "old"), [])
                self.assertEqual(len(by_attribute(layer, "name", "far")), 1)
                self.assertAreaAlmost(by_attribute(layer, "name", "new")[0].geometry(), 100.0)

    def test_c_containment_makes_hole(self):
        layer = make_layer(rows=[(wkt_rect(0, 0, 10, 10), ["old", 1])])
        paste(layer, make_snapshot([(wkt_rect(4, 4, 6, 6), {"name": "new", "n": 2})]))
        old = by_attribute(layer, "name", "old")[0].geometry()
        self.assertAreaAlmost(old, 96.0)
        self.assertEqual(len(old.asPolygon()), 2)  # outer ring + hole
        self.assertAreaAlmost(by_attribute(layer, "name", "new")[0].geometry(), 4.0)
        self.assertAreaAlmost(layer_union(layer), 100.0)

    def test_d_split_remainder_single_and_multi(self):
        strip = make_snapshot([(wkt_rect(4, -1, 6, 11), {"name": "strip", "n": 2})])
        for provider in PROVIDERS:
            with self.subTest(provider=provider, target="singlepart"):
                layer = make_layer(provider, wkb="Polygon", rows=[(wkt_rect(0, 0, 10, 10), ["old", 1])])
                old_fid = features_of(layer)[0].id()
                plan, result = paste(layer, strip)
                self.assertEqual(plan.count_fragments(), 1)
                pieces = by_attribute(layer, "name", "old")
                self.assertEqual(len(pieces), 2)  # never "largest only"
                self.assertAlmostEqual(sum(p.geometry().area() for p in pieces), 80.0)
                self.assertIn(old_fid, [p.id() for p in pieces])  # identity preserved on one remainder
                for piece in pieces:
                    self.assertFalse(piece.geometry().isMultipart())
                    self.assertEqual(piece["n"], 1)
            with self.subTest(provider=provider, target="multipart"):
                layer = make_layer(provider, wkb="MultiPolygon", rows=[("MULTIPOLYGON(((0 0,10 0,10 10,0 10,0 0)))", ["old", 1])])
                old_fid = features_of(layer)[0].id()
                plan, _ = paste(layer, strip)
                self.assertEqual(plan.count_fragments(), 0)
                pieces = by_attribute(layer, "name", "old")
                self.assertEqual(len(pieces), 1)
                self.assertEqual(pieces[0].id(), old_fid)
                self.assertTrue(pieces[0].geometry().isMultipart())
                self.assertEqual(len(pieces[0].geometry().asMultiPolygon()), 2)
                self.assertAreaAlmost(pieces[0].geometry(), 80.0)

    def test_d_tie_break_is_deterministic(self):
        # two remainders of identical area: the lower-left one keeps the id, every time
        ids = set()
        for _ in range(3):
            layer = make_layer(rows=[(wkt_rect(0, 0, 10, 10), ["old", 1])])
            paste(layer, make_snapshot([(wkt_rect(4, -1, 6, 11), {"name": "strip", "n": 2})]))
            keeper = [f for f in by_attribute(layer, "name", "old") if f.id() == 1][0]
            ids.add(keeper.geometry().boundingBox().xMinimum())
        self.assertEqual(ids, {0.0})

    def test_e_boundary_contact_removes_nothing(self):
        layer = make_layer(rows=[(wkt_rect(0, 0, 10, 10), ["edge", 1]), (wkt_rect(20, 10, 30, 20), ["corner", 2])])
        before = state(layer)
        plan, _ = paste(layer, make_snapshot([(wkt_rect(10, 0, 20, 10), {"name": "new", "n": 3})]))
        self.assertEqual(plan.count_trimmed(), 0)
        self.assertEqual(plan.count_deleted(), 0)
        after = dict((fid, wkt) for fid, wkt, _a in state(layer))
        for fid, wkt, _attrs in before:
            self.assertEqual(after[fid], wkt)  # geometry untouched, coordinates identical
        self.assertAreaAlmost(layer_union(layer), 300.0)

    def test_f_identical_geometry_noop_and_update(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                layer = make_layer(provider, rows=[(wkt_rect(0, 0, 10, 10), ["same", 1])])
                fid = features_of(layer)[0].id()
                before = state(layer)
                plan, result = paste(layer, make_snapshot([(wkt_rect(0, 0, 10, 10), {"name": "same", "n": 1})]))
                self.assertTrue(plan.is_noop())
                self.assertFalse(layer.isEditable())  # a true no-op does not even start editing
                self.assertEqual(state(layer), before)
                self.assertEqual(result.reused_fids, [fid])

                plan, result = paste(layer, make_snapshot([(wkt_rect(0, 0, 10, 10), {"name": "changed", "n": 1})]))
                self.assertEqual(plan.count_reused(), 1)
                self.assertEqual(plan.count_new_outputs(), 0)
                self.assertEqual(layer.featureCount(), 1)
                feature = features_of(layer)[0]
                self.assertEqual(feature.id(), fid)  # identity preserved
                self.assertEqual(feature["name"], "changed")
                self.assertEqual(feature.geometry().asWkt(), before[0][1])  # representation untouched

    def test_g_equivalent_representations(self):
        base = wkt_rect(0, 0, 10, 10)
        variants = {
            "reversed ring": "POLYGON((0 0,0 10,10 10,10 0,0 0))",
            "other start vertex": "POLYGON((10 10,0 10,0 0,10 0,10 10))",
            "extra collinear vertices": "POLYGON((0 0,5 0,10 0,10 5,10 10,0 10,0 0))",
        }
        for label, wkt in variants.items():
            with self.subTest(variant=label):
                layer = make_layer(rows=[(base, ["same", 1])])
                plan, _ = paste(layer, make_snapshot([(wkt, {"name": "same", "n": 1})]))
                self.assertTrue(plan.is_noop(), label)
                self.assertEqual(layer.featureCount(), 1)
        with self.subTest(variant="reordered parts"):
            a, b = "((0 0,10 0,10 10,0 10,0 0))", "((20 0,30 0,30 10,20 10,20 0))"
            layer = make_layer(wkb="MultiPolygon", rows=[("MULTIPOLYGON(%s,%s)" % (a, b), ["same", 1])])
            plan, _ = paste(layer, make_snapshot([("MULTIPOLYGON(%s,%s)" % (b, a), {"name": "same", "n": 1})]))
            self.assertTrue(plan.is_noop())

    def test_g_duplicates_are_consolidated(self):
        layer = make_layer(rows=[(wkt_rect(0, 0, 10, 10), ["first", 1]), (wkt_rect(0, 0, 10, 10), ["second", 2])])
        plan, _ = paste(layer, make_snapshot([(wkt_rect(0, 0, 10, 10), {"name": "new", "n": 3})]))
        self.assertEqual(layer.featureCount(), 1)
        survivor = features_of(layer)[0]
        self.assertEqual(survivor.id(), 1)  # deterministic: lowest committed id
        self.assertEqual(survivor["name"], "new")
        self.assertTrue(any(i.code == "duplicates" and "name" in i.message for i in plan.issues))

    def test_h_overlapping_incoming_batch(self):
        layer = make_layer(rows=[])
        snapshot = make_snapshot(
            [(wkt_rect(0, 0, 10, 10), {"name": "first", "n": 1}), (wkt_rect(5, 0, 15, 10), {"name": "last", "n": 2})]
        )
        plan, _ = paste(layer, snapshot)
        first = by_attribute(layer, "name", "first")[0]
        last = by_attribute(layer, "name", "last")[0]
        self.assertAreaAlmost(first.geometry(), 50.0)  # later input wins only within the overlap
        self.assertAreaAlmost(last.geometry(), 100.0)
        self.assertEqual((first["n"], last["n"]), (1, 2))  # not dissolved, attributes kept apart
        self.assertLessEqual(max_pairwise_overlap(layer), 1e-12)
        self.assertAreaAlmost(layer_union(layer), 150.0)

    def test_h_superseded_incoming(self):
        layer = make_layer(rows=[])
        snapshot = make_snapshot(
            [(wkt_rect(2, 2, 4, 4), {"name": "hidden", "n": 1}), (wkt_rect(0, 0, 10, 10), {"name": "cover", "n": 2})]
        )
        plan, _ = paste(layer, snapshot)
        self.assertEqual(plan.superseded, [0])
        self.assertFalse(plan.blocking())
        self.assertEqual(layer.featureCount(), 1)

    def test_i_repeated_paste_is_stable(self):
        snapshot = make_snapshot(
            [
                (wkt_rect(0, 0, 10, 10), {"name": "a", "n": 1}),
                (wkt_rect(5, 0, 15, 10), {"name": "b", "n": 2}),
                # cuts "a"'s remainder in two -> split outputs on a singlepart target
                (wkt_rect(2, -5, 3, 15), {"name": "c", "n": 3}),
            ]
        )
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                layer = make_layer(provider, wkb="Polygon", rows=[(wkt_rect(-5, -5, 20, 20), ["base", 0])])
                paste(layer, snapshot)
                first = state(layer)
                for _ in range(2):
                    plan, _ = paste(layer, snapshot)
                    self.assertTrue(plan.is_noop(), [str(c.reasons) for c in plan.changes.values()])
                    self.assertEqual(state(layer), first)  # same count, same ids, same geometry
                self.assertEqual(len(by_attribute(layer, "name", "a")), 2)
                self.assertLessEqual(max_pairwise_overlap(layer), 1e-9)

    def test_j_holes_and_multipart_input(self):
        donut = "POLYGON((0 0,20 0,20 20,0 20,0 0),(5 5,15 5,15 15,5 15,5 5))"
        layer = make_layer(rows=[(wkt_rect(8, 8, 12, 12), ["island", 1]), (wkt_rect(-2, 8, 7, 12), ["crossing", 2])])
        paste(layer, make_snapshot([(donut, {"name": "donut", "n": 3})]))
        island = by_attribute(layer, "name", "island")[0]
        self.assertAreaAlmost(island.geometry(), 16.0)  # inside the hole: survives untouched
        crossing = by_attribute(layer, "name", "crossing")
        self.assertAlmostEqual(sum(f.geometry().area() for f in crossing), 2 * 4 + 2 * 4)  # x<0 and 5<x<7
        # multipart incoming into a multipart target stays one feature
        multi = "MULTIPOLYGON(((0 0,4 0,4 4,0 4,0 0)),((6 6,10 6,10 10,6 10,6 6)))"
        target = make_layer(wkb="MultiPolygon", rows=[])
        paste(target, make_snapshot([(multi, {"name": "m", "n": 1})]))
        self.assertEqual(target.featureCount(), 1)
        # ...and is split (equivalent type adaptation) on a singlepart target
        single = make_layer(wkb="Polygon", rows=[])
        paste(single, make_snapshot([(multi, {"name": "m", "n": 1})]))
        self.assertEqual(single.featureCount(), 2)
        self.assertEqual(set(f["name"] for f in single.getFeatures()), {"m"})

    def test_coverage_identity(self):
        layer = make_layer(
            rows=[(wkt_rect(0, 0, 10, 10), ["a", 1]), (wkt_rect(10, 0, 20, 10), ["b", 2]), (wkt_rect(0, 10, 20, 20), ["c", 3])]
        )
        incoming = [
            ("POLYGON((5 5,15 4,17 14,8 16,5 5))", {"name": "x", "n": 7}),
            ("POLYGON((12 12,25 12,25 25,12 25,12 12))", {"name": "y", "n": 8}),
        ]
        expected = union_of([f.geometry() for f in layer.getFeatures()] + [QgsGeometry.fromWkt(w) for w, _a in incoming])
        plan, _ = paste(layer, make_snapshot(incoming))
        self.assertCoverageEqual(layer_union(layer), expected)
        self.assertLessEqual(max_pairwise_overlap(layer), 1e-9)
        self.assertTrue(any("coverage" in text for text in plan.validated))

    def test_s_shared_boundary_noding(self):
        # existing neighbour has one long edge; the pasted polygon has an extra vertex on it,
        # and the other neighbour has a vertex the pasted polygon lacks.
        layer = make_layer(
            rows=[
                (wkt_rect(0, 0, 10, 10), ["left", 1]),
                ("POLYGON((20 0,30 0,30 10,20 10,20 4,20 0))", ["right", 2]),
            ]
        )
        before_left = by_attribute(layer, "name", "left")[0].geometry()
        before_right = by_attribute(layer, "name", "right")[0].geometry()
        pasted = "POLYGON((10 0,20 0,20 10,10 10,10 6,10 0))"
        plan, _ = paste(layer, make_snapshot([(pasted, {"name": "mid", "n": 3})]))
        left = by_attribute(layer, "name", "left")[0].geometry()
        mid = by_attribute(layer, "name", "mid")[0].geometry()
        right = by_attribute(layer, "name", "right")[0].geometry()

        def vertices(geometry):
            return set((round(v.x(), 9), round(v.y(), 9)) for v in geometry.vertices())

        self.assertIn((10.0, 6.0), vertices(left))  # neighbour received the pasted vertex
        self.assertIn((20.0, 4.0), vertices(mid))  # pasted polygon received the neighbour's vertex
        self.assertEqual(vertices(right), vertices(before_right))  # nothing to add there
        # coverage unchanged, no coordinates moved
        self.assertCoverageEqual(left, before_left, 0.0)
        self.assertCoverageEqual(right, before_right, 0.0)
        self.assertAreaAlmost(mid, 100.0)
        self.assertEqual(plan.count_noded_only(), 1)
        self.assertEqual(plan.count_trimmed(), 0)

    def test_s_cut_vertices_exist_on_both_sides(self):
        layer = make_layer(rows=[("POLYGON((0 0,10 0,10 10,0 10,0 0))", ["old", 1])])
        paste(layer, make_snapshot([("POLYGON((5 -5,15 5,5 15,5 -5))", {"name": "tri", "n": 2})]))
        old = by_attribute(layer, "name", "old")[0].geometry()
        tri = by_attribute(layer, "name", "tri")[0].geometry()
        shared = old.intersection(tri)
        old_vertices = set((v.x(), v.y()) for v in old.vertices())
        tri_vertices = set((v.x(), v.y()) for v in tri.vertices())
        # the computed intersection vertices (5,0) and (5,10) must be nodes of BOTH polygons
        for xy in [(5.0, 0.0), (5.0, 10.0)]:
            self.assertIn(xy, old_vertices)
            self.assertIn(xy, tri_vertices)
        self.assertLessEqual(shared.area(), 1e-12)


if __name__ == "__main__":
    unittest.main()
