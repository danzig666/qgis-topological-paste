# -*- coding: utf-8 -*-
"""Acceptance tests P (editing / undo safety) and Q (scope)."""

import unittest

from qgis.core import QgsFeature, QgsGeometry

from topological_paste.applier import apply_plan
from topological_paste.model import ApplyError, PlanError, StalePlanError
from topological_paste.planner import Feedback, PlanCanceled, build_plan
from topological_paste.tests.utils import (
    PROVIDERS,
    QgisTestCase,
    by_attribute,
    features_of,
    make_layer,
    make_snapshot,
    paste,
    state,
    wkt_rect,
)


def add_in_buffer(layer, wkt, attributes):
    feature = QgsFeature(layer.fields())
    feature.setGeometry(QgsGeometry.fromWkt(wkt))
    full = list(attributes)
    if layer.fields().count() == len(full) + 1:  # GeoPackage: leading managed fid
        full = ["Autogenerate"] + full
    feature.setAttributes(full)
    assert layer.addFeature(feature)
    return feature.id()


class TestEditing(QgisTestCase):
    def test_p_undo_redo_is_one_command(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                layer = make_layer(
                    provider,
                    rows=[(wkt_rect(0, 0, 10, 10), ["a", 1]), (wkt_rect(12, 2, 14, 4), ["covered", 2]), (wkt_rect(0, 0, 10, 10), ["dup", 3])],
                )
                before = state(layer)
                snapshot = make_snapshot(
                    [(wkt_rect(5, -1, 20, 11), {"name": "new", "n": 9}), (wkt_rect(0, 0, 10, 10), {"name": "a2", "n": 8})]
                )
                plan, _ = paste(layer, snapshot)
                self.assertGreater(len(plan.changes) + len(plan.new_features), 2)  # several edits...
                self.assertEqual(layer.undoStack().count(), 1)  # ...one command
                after = state(layer)
                self.assertNotEqual(after, before)
                layer.undoStack().undo()
                self.assertEqual(state(layer), before)  # ONE undo restores everything
                layer.undoStack().redo()
                self.assertEqual(state(layer), after)  # redo reapplies it

    def test_p_existing_unsaved_edits_and_temporary_ids(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                layer = make_layer(provider, rows=[(wkt_rect(0, 0, 10, 10), ["saved", 1])])
                layer.startEditing()
                temp_fid = add_in_buffer(layer, wkt_rect(10, 0, 20, 10), ["unsaved", 2])
                self.assertLess(temp_fid, 0)
                saved_fid = [f.id() for f in layer.getFeatures() if f["name"] == "saved"][0]
                layer.changeAttributeValue(saved_fid, layer.fields().lookupField("n"), 111)
                user_state = state(layer)
                commands_before = layer.undoStack().count()

                # pasted polygon lies on both: the pending feature (temporary id) is part of the snapshot
                plan, _ = paste(layer, make_snapshot([(wkt_rect(8, 2, 12, 8), {"name": "new", "n": 3})]))
                self.assertIn(temp_fid, plan.changes)
                self.assertEqual(layer.undoStack().count(), commands_before + 1)
                unsaved = by_attribute(layer, "name", "unsaved")[0]
                self.assertEqual(unsaved.id(), temp_fid)
                self.assertAlmostEqual(unsaved.geometry().area(), 100.0 - 12.0)
                self.assertEqual(by_attribute(layer, "name", "saved")[0]["n"], 111)

                layer.undoStack().undo()
                self.assertEqual(state(layer), user_state)  # previous user edits intact
                self.assertTrue(layer.isEditable())
                self.assertTrue(layer.isModified())

    def test_p_forced_failure_leaves_no_partial_changes(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                layer = make_layer(
                    provider, rows=[(wkt_rect(0, 0, 10, 10), ["a", 1]), (wkt_rect(12, 2, 14, 4), ["covered", 2])]
                )
                layer.startEditing()
                add_in_buffer(layer, wkt_rect(100, 0, 110, 10), ["user edit", 5])
                layer.selectByIds([f.id() for f in layer.getFeatures() if f["name"] == "covered"])
                before = state(layer)
                selection = sorted(layer.selectedFeatureIds())
                commands = layer.undoStack().count()
                plan = build_plan(layer, make_snapshot([(wkt_rect(5, -1, 20, 11), {"name": "new", "n": 9})]))

                def explode(step):
                    if step == 3:  # after a delete and a geometry change have already happened
                        raise RuntimeError("simulated provider failure")

                with self.assertRaises(ApplyError):
                    apply_plan(layer, plan, fault_hook=explode)
                self.assertEqual(state(layer), before)
                self.assertEqual(sorted(layer.selectedFeatureIds()), selection)
                self.assertFalse(layer.isEditCommandActive())
                self.assertEqual(layer.undoStack().count(), commands)
                self.assertTrue(layer.isEditable())  # the user's session and edits survive
                self.assertEqual(len(by_attribute(layer, "name", "user edit")), 1)

    def test_p_failure_closes_a_session_it_opened(self):
        layer = make_layer(rows=[(wkt_rect(0, 0, 10, 10), ["a", 1])])
        before = state(layer)
        plan = build_plan(layer, make_snapshot([(wkt_rect(5, 0, 15, 10), {"name": "new", "n": 9})]))

        def explode(step):
            if step == 2:
                raise RuntimeError("boom")

        with self.assertRaises(ApplyError):
            apply_plan(layer, plan, fault_hook=explode)
        self.assertEqual(state(layer), before)
        self.assertFalse(layer.isEditable())

    def test_p_cancellation_changes_nothing(self):
        class CancelSoon(Feedback):
            def __init__(self):
                self.calls = 0

            def progress(self, fraction, text=None):
                self.calls += 1

            def canceled(self):
                return self.calls >= 3

        layer = make_layer(rows=[(wkt_rect(0, 0, 10, 10), ["a", 1])])
        before = state(layer)
        with self.assertRaises(PlanCanceled):
            build_plan(layer, make_snapshot([(wkt_rect(5, 0, 15, 10), {"name": "new", "n": 9})]), feedback=CancelSoon())
        self.assertEqual(state(layer), before)
        self.assertFalse(layer.isEditable())

    def test_p_planning_never_touches_the_layer(self):
        layer = make_layer(rows=[(wkt_rect(0, 0, 10, 10), ["a", 1])])
        before = state(layer)
        build_plan(layer, make_snapshot([(wkt_rect(5, 0, 15, 10), {"name": "new", "n": 9})]))
        self.assertEqual(state(layer), before)
        self.assertFalse(layer.isEditable())

    def test_p_stale_preview_is_refused(self):
        snapshot = make_snapshot([(wkt_rect(5, 0, 15, 10), {"name": "new", "n": 9})])
        with self.subTest(case="geometry edited after planning"):
            layer = make_layer(rows=[(wkt_rect(0, 0, 10, 10), ["a", 1])])
            layer.startEditing()
            plan = build_plan(layer, snapshot)
            layer.changeGeometry(1, QgsGeometry.fromWkt(wkt_rect(0, 0, 9, 9)))
            with self.assertRaises(StalePlanError):
                apply_plan(layer, plan)
        with self.subTest(case="a new spatial candidate appeared"):
            layer = make_layer(rows=[(wkt_rect(0, 0, 10, 10), ["a", 1])])
            layer.startEditing()
            plan = build_plan(layer, snapshot)
            add_in_buffer(layer, wkt_rect(12, 2, 14, 4), ["late", 2])
            with self.assertRaises(StalePlanError):
                apply_plan(layer, plan)
            self.assertEqual(len(by_attribute(layer, "name", "new")), 0)
        with self.subTest(case="edit state changed"):
            layer = make_layer(rows=[(wkt_rect(0, 0, 10, 10), ["a", 1])])
            plan = build_plan(layer, snapshot)
            layer.startEditing()
            with self.assertRaises(StalePlanError):
                apply_plan(layer, plan)
        with self.subTest(case="schema changed"):
            from qgis.core import QgsField
            from qgis.PyQt.QtCore import QVariant

            layer = make_layer(rows=[(wkt_rect(0, 0, 10, 10), ["a", 1])])
            layer.startEditing()
            plan = build_plan(layer, snapshot)
            layer.addAttribute(QgsField("later", QVariant.String))
            with self.assertRaises(StalePlanError):
                apply_plan(layer, plan)

    def test_p_nested_edit_command_is_refused(self):
        layer = make_layer(rows=[(wkt_rect(0, 0, 10, 10), ["a", 1])])
        layer.startEditing()
        layer.beginEditCommand("someone else")
        with self.assertRaises(PlanError):
            build_plan(layer, make_snapshot([(wkt_rect(5, 0, 15, 10), {"name": "new", "n": 9})]))
        layer.destroyEditCommand()


class TestScope(QgisTestCase):
    def test_q_selection_plays_no_role(self):
        layer = make_layer(rows=[(wkt_rect(0, 0, 10, 10), ["selected", 1]), (wkt_rect(10, 0, 20, 10), ["unselected", 2])])
        layer.selectByIds([1])
        paste(layer, make_snapshot([(wkt_rect(5, 2, 15, 8), {"name": "new", "n": 3})]))
        self.assertAlmostEqual(by_attribute(layer, "name", "selected")[0].geometry().area(), 70.0)
        self.assertAlmostEqual(by_attribute(layer, "name", "unselected")[0].geometry().area(), 70.0)

    def test_q_subset_filter_is_refused_and_kept(self):
        layer = make_layer("gpkg", rows=[(wkt_rect(0, 0, 10, 10), ["visible", 1]), (wkt_rect(5, 0, 15, 10), ["hidden", 2])])
        self.assertTrue(layer.setSubsetString("\"n\" = 1"))
        with self.assertRaises(PlanError) as caught:
            build_plan(layer, make_snapshot([(wkt_rect(5, 2, 15, 8), {"name": "new", "n": 3})]))
        self.assertIn("filter", str(caught.exception))
        self.assertEqual(layer.subsetString(), "\"n\" = 1")  # the user's filter is not removed
        self.assertFalse(layer.isEditable())

    def test_q_unrelated_features_and_problems_are_left_alone(self):
        rows = [
            (wkt_rect(0, 0, 10, 10), ["a", 1]),
            # two features overlapping each other partly outside the pasted coverage
            (wkt_rect(8, 8, 14, 14), ["b", 2]),
            # far away: overlapping pair and an invalid polygon - none of our business
            (wkt_rect(1000, 0, 1010, 10), ["far1", 3]),
            (wkt_rect(1005, 0, 1015, 10), ["far2", 4]),
            ("POLYGON((2000 0,2010 10,2010 0,2000 10,2000 0))", ["farinvalid", 5]),
        ]
        layer = make_layer(rows=rows)
        far_before = [s for s in state(layer) if s[0] >= 3]
        plan, _ = paste(layer, make_snapshot([(wkt_rect(9, 9, 11, 11), {"name": "new", "n": 9})]))
        self.assertEqual([s for s in state(layer) if s[0] in (3, 4, 5)], far_before)
        # the old a/b overlap outside U is reported, not "resolved"
        preexisting = [i for i in plan.issues if i.category == "preexisting"]
        self.assertTrue(any(i.code == "preexisting_overlap" for i in preexisting))
        a = by_attribute(layer, "name", "a")[0].geometry()
        b = by_attribute(layer, "name", "b")[0].geometry()
        self.assertAlmostEqual(a.intersection(b).area(), 4.0 - 1.0)  # [8,10]^2 minus the pasted [9,10]^2 share
        self.assertFalse(any("entire layer" in text.lower() for text in plan.validated))

    def test_q_large_batch_uses_index(self):
        # 30 x 30 grid of existing cells, 100 pasted cells shifted by half a cell
        rows = [(wkt_rect(x, y, x + 1, y + 1), ["c%d_%d" % (x, y), x * 100 + y]) for x in range(30) for y in range(30)]
        layer = make_layer(rows=rows)
        incoming = [
            (wkt_rect(x + 0.5, y + 0.5, x + 1.5, y + 1.5), {"name": "p%d_%d" % (x, y), "n": 1})
            for x in range(5, 15)
            for y in range(5, 15)
        ]
        plan, _ = paste(layer, make_snapshot(incoming))
        total = sum(f.geometry().area() for f in layer.getFeatures())
        self.assertAlmostEqual(total, 900.0, places=6)
        self.assertEqual(plan.count_new_outputs(), 100)


if __name__ == "__main__":
    unittest.main()
