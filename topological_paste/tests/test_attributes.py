# -*- coding: utf-8 -*-
"""Acceptance tests L (attributes, defaults, keys, constraints)."""

import unittest

from qgis.core import NULL, QgsDefaultValue, QgsFieldConstraints, QgsVectorLayerJoinInfo
from qgis.PyQt.QtCore import QDate

from topological_paste import compat
from topological_paste.model import Issue, PlanOptions
from topological_paste.planner import build_plan
from topological_paste.tests.utils import (
    QgisTestCase,
    by_attribute,
    features_of,
    make_layer,
    make_snapshot,
    paste,
    state,
    wkt_rect,
)

FIELDS = "name:string(80),n:integer,d:double(20,15),dt:date,b:boolean"


class TestAttributes(QgisTestCase):
    def test_l_field_order_and_value_fidelity(self):
        layer = make_layer(fields=FIELDS, rows=[])
        values = {"b": True, "dt": QDate(2024, 2, 29), "d": 0.1234567890123, "n": 0, "name": "árvíztűrő tükörfúrógép ŐŰ"}
        # the source lists its fields in a different order than the target
        snapshot = make_snapshot([(wkt_rect(0, 0, 1, 1), values)], field_names=["b", "dt", "d", "n", "name"])
        paste(layer, snapshot)
        feature = features_of(layer)[0]
        self.assertEqual(feature["name"], "árvíztűrő tükörfúrógép ŐŰ")  # Unicode incl. ő and ű
        self.assertEqual(feature["n"], 0)  # numeric zero is a value, not NULL
        self.assertFalse(compat.is_null(feature["n"]))
        self.assertEqual(feature["d"], 0.1234567890123)
        self.assertEqual(feature["dt"], QDate(2024, 2, 29))
        self.assertIs(feature["b"], True)

    def test_l_empty_string_false_and_null_are_distinct(self):
        layer = make_layer(fields=FIELDS, rows=[])
        snapshot = make_snapshot(
            [(wkt_rect(0, 0, 1, 1), {"name": "", "n": None, "b": False})], field_names=["name", "n", "b"]
        )
        paste(layer, snapshot)
        feature = features_of(layer)[0]
        self.assertEqual(feature["name"], "")
        self.assertFalse(compat.is_null(feature["name"]))
        self.assertTrue(compat.is_null(feature["n"]))
        self.assertIs(feature["b"], False)
        self.assertTrue(compat.is_null(feature["d"]))  # absent in the source, no default -> NULL

    def test_l_absent_field_versus_explicit_null_on_reuse(self):
        rect = wkt_rect(0, 0, 10, 10)
        layer = make_layer(fields=FIELDS, rows=[(rect, ["keep", 5, 2.5, QDate(2020, 1, 1), True])])
        # 'n' is present and NULL -> overwrites; 'd', 'dt', 'b' are absent -> preserved
        snapshot = make_snapshot([(rect, {"name": "renamed", "n": None})], field_names=["name", "n"])
        plan, _ = paste(layer, snapshot)
        feature = features_of(layer)[0]
        self.assertEqual(feature["name"], "renamed")
        self.assertTrue(compat.is_null(feature["n"]))
        self.assertEqual(feature["d"], 2.5)
        self.assertEqual(feature["dt"], QDate(2020, 1, 1))
        self.assertIs(feature["b"], True)
        self.assertEqual(sorted(plan.changes[feature.id()].attribute_changes.keys()), [0, 1])

    def test_l_defaults_for_missing_fields_only(self):
        layer = make_layer(fields=FIELDS, rows=[])
        layer.setDefaultValueDefinition(1, QgsDefaultValue("42"))
        layer.setDefaultValueDefinition(2, QgsDefaultValue("$area"))
        snapshot = make_snapshot(
            [(wkt_rect(0, 0, 2, 3), {"name": "a"}), (wkt_rect(10, 0, 12, 3), {"name": "b", "n": 7})],
            field_names=["name", "n"],
        )
        # feature 1 has no 'n' at all (absent), feature 2 brings its own value
        snapshot.features[0].attributes = {"name": "a"}
        paste(layer, snapshot)
        a = by_attribute(layer, "name", "a")[0]
        b = by_attribute(layer, "name", "b")[0]
        self.assertEqual(a["n"], 42)  # legitimate target default for a missing field
        self.assertEqual(b["n"], 7)  # source value wins over the default
        self.assertAlmostEqual(a["d"], 6.0)  # expression default sees the new geometry

    def test_l_trimmed_features_keep_attributes(self):
        layer = make_layer(fields=FIELDS, rows=[(wkt_rect(0, 0, 10, 10), ["old", 5, 2.5, QDate(2020, 1, 1), True])])
        layer.setDefaultValueDefinition(2, QgsDefaultValue("$area", True))  # apply on update
        plan, _ = paste(layer, make_snapshot([(wkt_rect(5, 0, 15, 10), {"name": "new", "n": 1})]))
        old = by_attribute(layer, "name", "old")[0]
        self.assertEqual((old["n"], old["dt"], old["b"]), (5, QDate(2020, 1, 1), True))
        self.assertAlmostEqual(old["d"], 50.0)  # only the configured apply-on-update default is recomputed
        self.assertTrue(any(i.code == "apply_on_update" for i in plan.issues))

    def test_l_managed_geopackage_key(self):
        layer = make_layer("gpkg", rows=[(wkt_rect(0, 0, 10, 10), ["old", 1])])
        self.assertEqual(layer.primaryKeyAttributes(), [0])
        old_fid = features_of(layer)[0]["fid"]
        # the source carries its own 'fid' values: they must not be written into the managed key
        snapshot = make_snapshot(
            [(wkt_rect(4, -1, 6, 11), {"fid": old_fid, "name": "strip", "n": 2})], field_names=["fid", "name", "n"]
        )
        plan, _ = paste(layer, snapshot)
        self.assertTrue(any(i.code == "not_writable" and "primary key" in i.message for i in plan.issues))
        self.assertTrue(layer.commitChanges(), layer.commitErrors())  # the TEST saves; the plugin never does
        fids = [f["fid"] for f in layer.getFeatures()]
        self.assertEqual(len(fids), 3)
        self.assertEqual(len(set(fids)), 3)  # new feature + fragment received fresh keys
        self.assertIn(old_fid, fids)  # the reused/trimmed feature kept its key
        self.assertEqual(len(by_attribute(layer, "name", "old")), 2)

    def test_l_unique_constraint_blocks_instead_of_renumbering(self):
        layer = make_layer(rows=[(wkt_rect(0, 0, 10, 10), ["P-1", 1]), (wkt_rect(50, 0, 60, 10), ["P-2", 2])])
        layer.setFieldConstraint(0, QgsFieldConstraints.ConstraintUnique, QgsFieldConstraints.ConstraintStrengthHard)
        before = state(layer)
        # 1) incoming value collides with an existing business identifier
        plan, _ = paste(layer, make_snapshot([(wkt_rect(100, 0, 110, 10), {"name": "P-2", "n": 9})]), expect_blocking=True)
        self.assertTrue(any(i.code == "unique" for i in plan.blocking()))
        # 2) a split would duplicate the identifier of the fragment
        plan, _ = paste(layer, make_snapshot([(wkt_rect(4, -1, 6, 11), {"name": "P-3", "n": 9})]), expect_blocking=True)
        self.assertTrue(any(i.code == "unique" and "fragment" in i.message for i in plan.blocking()))
        self.assertEqual(state(layer), before)
        # 3) replacing the feature that owns the value is fine (the old owner is deleted)
        plan, _ = paste(layer, make_snapshot([(wkt_rect(49, -1, 61, 11), {"name": "P-2", "n": 9})]))
        self.assertEqual(len(by_attribute(layer, "name", "P-2")), 1)

    def test_l_soft_constraint_only_warns(self):
        layer = make_layer(rows=[(wkt_rect(0, 0, 10, 10), ["P-1", 1])])
        layer.setFieldConstraint(0, QgsFieldConstraints.ConstraintUnique, QgsFieldConstraints.ConstraintStrengthSoft)
        plan = build_plan(layer, make_snapshot([(wkt_rect(100, 0, 110, 10), {"name": "P-1", "n": 9})]))
        self.assertFalse(plan.blocking())
        self.assertTrue(any(i.code == "unique" and i.severity == Issue.WARNING for i in plan.issues))

    def test_l_not_null_constraint(self):
        layer = make_layer(rows=[])
        layer.setFieldConstraint(1, QgsFieldConstraints.ConstraintNotNull, QgsFieldConstraints.ConstraintStrengthHard)
        plan = build_plan(layer, make_snapshot([(wkt_rect(0, 0, 1, 1), {"name": "x", "n": None})]))
        self.assertTrue(any(i.code == "not_null" for i in plan.blocking()))
        layer.setDefaultValueDefinition(1, QgsDefaultValue("1"))
        plan = build_plan(layer, make_snapshot([(wkt_rect(0, 0, 1, 1), {"name": "x", "n": None})]))
        self.assertFalse(plan.blocking())  # a legitimate default satisfies the constraint

    def test_l_non_writable_fields_are_not_written(self):
        layer = make_layer(rows=[])
        other = make_layer(wkb="None", fields="key:string(80),joined:string(80)", rows=[])
        join = QgsVectorLayerJoinInfo()
        join.setJoinLayer(other)
        join.setJoinFieldName("key")
        join.setTargetFieldName("name")
        join.setPrefix("j_")
        self.assertTrue(layer.addJoin(join))
        from qgis.core import QgsField
        from qgis.PyQt.QtCore import QVariant

        layer.addExpressionField("n * 2", QgsField("virt", QVariant.Int))
        names = [f.name() for f in layer.fields()]
        self.assertIn("j_joined", names)
        self.assertIn("virt", names)
        snapshot = make_snapshot(
            [(wkt_rect(0, 0, 1, 1), {"name": "x", "n": 4, "j_joined": "hack", "virt": 99})],
            field_names=["name", "n", "j_joined", "virt"],
        )
        plan, _ = paste(layer, snapshot)
        skipped = dict((source, reason) for source, _t, reason in plan.mapping.skipped)
        self.assertIn("joined", skipped["j_joined"])
        self.assertIn("virtual", skipped["virt"])
        feature = features_of(layer)[0]
        self.assertEqual(feature["virt"], 8)  # computed by QGIS, not the pasted 99

    def test_l_unmapped_and_lossy_are_reported(self):
        layer = make_layer(fields="name:string(5),n:integer", rows=[])
        snapshot = make_snapshot(
            [(wkt_rect(0, 0, 1, 1), {"name": "much too long", "n": 5.7, "extra": "lost?"})],
            field_names=["name", "n", "extra"],
        )
        plan = build_plan(layer, snapshot)
        self.assertEqual(plan.mapping.unmapped_source, ["extra"])
        self.assertTrue(any(i.code == "unmapped" for i in plan.issues))
        conversions = [i for i in plan.needs_acceptance() if i.code == "conversion"]
        self.assertEqual(len(conversions), 2)  # failed (too long) + lossy (5.7 -> 6): never silent
        self.assertEqual(layer.fields().count(), 2)  # the schema is never extended

    def test_l_explicit_mapping_override(self):
        layer = make_layer(rows=[])
        snapshot = make_snapshot([(wkt_rect(0, 0, 1, 1), {"NAME": "upper", "n": 1})], field_names=["NAME", "n"])
        plan = build_plan(layer, snapshot)
        self.assertEqual(plan.mapping.unmapped_source, ["NAME"])  # no guessing, not even by letter case
        plan, _ = paste(layer, snapshot, PlanOptions(mapping_override={"NAME": "name"}))
        self.assertEqual(features_of(layer)[0]["name"], "upper")


if __name__ == "__main__":
    unittest.main()
