# -*- coding: utf-8 -*-
"""Acceptance tests N (CRS) and the secondary text clipboard formats."""

import unittest

from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsProject

from topological_paste.clipboard import ClipboardError, parse_clipboard_text, staged_matches_system_clipboard, StagedData
from topological_paste.model import ClipboardSnapshot, PlanError
from topological_paste.planner import build_plan
from topological_paste.tests.utils import (
    QgisTestCase,
    by_attribute,
    layer_union,
    make_layer,
    make_snapshot,
    paste,
    wkt_rect,
)


def to_crs(wkt, source, target):
    geometry = QgsGeometry.fromWkt(wkt)
    transform = QgsCoordinateTransform(
        QgsCoordinateReferenceSystem(source), QgsCoordinateReferenceSystem(target), QgsProject.instance()
    )
    geometry.transform(transform)
    return geometry


class TestCrs(QgisTestCase):
    HD72_RECT = wkt_rect(650000, 240000, 650100, 240100)  # Budapest, EPSG:23700 (metres)

    def test_n_projected_metric_target(self):
        layer = make_layer(crs="EPSG:23700", rows=[(self.HD72_RECT, ["old", 1])])
        incoming = wkt_rect(650050, 240000, 650150, 240100)
        paste(layer, make_snapshot([(incoming, {"name": "new", "n": 2})], crs="EPSG:23700"))
        self.assertAlmostEqual(by_attribute(layer, "name", "old")[0].geometry().area(), 5000.0, places=6)
        self.assertAlmostEqual(layer_union(layer).area(), 15000.0, places=6)

    def test_n_different_source_and_destination_crs(self):
        layer = make_layer(crs="EPSG:23700", rows=[(self.HD72_RECT, ["old", 1])])
        incoming_wgs84 = to_crs(wkt_rect(650050, 240000, 650150, 240100), "EPSG:23700", "EPSG:4326")
        snapshot = make_snapshot([(incoming_wgs84.asWkt(17), {"name": "new", "n": 2})], crs="EPSG:4326")
        paste(layer, snapshot)
        new = by_attribute(layer, "name", "new")[0].geometry()
        old = by_attribute(layer, "name", "old")[0].geometry()
        # the overlay happened in the LAYER's CRS: the pasted polygon is ~100 m x 100 m there
        self.assertAlmostEqual(new.area(), 10000.0, delta=1.0)
        self.assertAlmostEqual(old.area(), 5000.0, delta=1.0)
        self.assertLessEqual(old.intersection(new).area(), 1e-9)
        self.assertEqual(layer.crs().authid(), "EPSG:23700")  # layer CRS untouched

    def test_n_geographic_target(self):
        layer = make_layer(crs="EPSG:4326", rows=[(wkt_rect(19.0, 47.0, 19.1, 47.1), ["old", 1])])
        paste(layer, make_snapshot([(wkt_rect(19.05, 47.0, 19.15, 47.1), {"name": "new", "n": 2})], crs="EPSG:4326"))
        # planar operations in degrees; no ellipsoidal measure decides anything
        self.assertAlmostEqual(by_attribute(layer, "name", "old")[0].geometry().area(), 0.005, places=12)

    def test_n_unknown_crs_is_never_assumed(self):
        layer = make_layer(crs="EPSG:23700", rows=[])
        snapshot = parse_clipboard_text(self.HD72_RECT)
        self.assertFalse(snapshot.crs.isValid())  # plain WKT: unknown, to be asked from the user
        with self.assertRaises(PlanError):
            build_plan(layer, snapshot)  # planner refuses instead of assuming the layer CRS
        self.assertEqual(layer.featureCount(), 0)

    def test_n_transformation_failure_is_detected_before_editing(self):
        layer = make_layer(crs="EPSG:23700", rows=[])
        snapshot = make_snapshot([(wkt_rect(0, 95, 10, 1e6), {"name": "bad", "n": 1})], crs="EPSG:4326")
        with self.assertRaises(PlanError):
            build_plan(layer, snapshot)
        self.assertFalse(layer.isEditable())


class TestTextFormats(QgisTestCase):
    def test_ewkt_srid(self):
        snapshot = parse_clipboard_text("SRID=23700;" + TestCrs.HD72_RECT)
        self.assertEqual(snapshot.origin, ClipboardSnapshot.ORIGIN_WKT)
        self.assertEqual(snapshot.crs.authid(), "EPSG:23700")
        self.assertEqual(len(snapshot.features), 1)

    def test_wkt_lines_and_multiline_wkt(self):
        text = "POINT(1 2)\nPOINT(3 4)\n\nPOINT(5 6)\n"
        self.assertEqual(len(parse_clipboard_text(text).features), 3)
        wrapped = "POLYGON((0 0,\n 10 0,\n 10 10,\n 0 0))"
        self.assertEqual(len(parse_clipboard_text(wrapped).features), 1)

    def test_mixed_srids_are_refused(self):
        with self.assertRaises(ClipboardError):
            parse_clipboard_text("SRID=23700;POINT(1 2)\nSRID=4326;POINT(3 4)")
        with self.assertRaises(ClipboardError):
            parse_clipboard_text("SRID=23700;POINT(1 2)\nPOINT(3 4)")

    def test_qgis_tabular_wkt(self):
        text = 'wkt_geom\tname\tn\nPolygon ((0 0, 10 0, 10 10, 0 0))\tárvíztűrő\t5\nPoint (1 1)\t"multi\nline"\t\n'
        snapshot = parse_clipboard_text(text)
        self.assertEqual(snapshot.origin, ClipboardSnapshot.ORIGIN_TABULAR)
        self.assertEqual(snapshot.field_names, ["name", "n"])
        self.assertFalse(snapshot.crs.isValid())
        self.assertEqual(snapshot.features[0].attributes, {"name": "árvíztűrő", "n": "5"})
        self.assertEqual(snapshot.features[1].attributes, {"name": "multi\nline", "n": None})

    def test_tabular_text_values_convert_into_typed_fields(self):
        layer = make_layer(rows=[])
        snapshot = parse_clipboard_text("wkt_geom\tname\tn\nPolygon ((0 0, 10 0, 10 10, 0 0))\tx\t5")
        snapshot.crs = layer.crs()
        paste(layer, snapshot)
        self.assertEqual(by_attribute(layer, "name", "x")[0]["n"], 5)

    def test_geojson(self):
        text = (
            '{"type":"FeatureCollection","features":[{"type":"Feature","properties":{"name":"ő","n":1},'
            '"geometry":{"type":"Polygon","coordinates":[[[19,47],[19.1,47],[19.1,47.1],[19,47]]]}}]}'
        )
        snapshot = parse_clipboard_text(text)
        self.assertEqual(snapshot.origin, ClipboardSnapshot.ORIGIN_GEOJSON)
        self.assertEqual(snapshot.crs.authid(), "EPSG:4326")  # RFC 7946, stated in the notes
        self.assertTrue(any("RFC 7946" in note for note in snapshot.notes))
        self.assertEqual(snapshot.features[0].attributes["name"], "ő")
        self.assertEqual(snapshot.features[0].attributes["n"], 1)

    def test_geojson_with_projected_coordinates_is_unknown(self):
        text = (
            '{"type":"Feature","properties":{},"geometry":{"type":"Point","coordinates":[650000,240000]}}'
        )
        self.assertFalse(parse_clipboard_text(text).crs.isValid())
        declared = text[:-1] + ',"crs":{"type":"name","properties":{"name":"urn:ogc:def:crs:EPSG::23700"}}}'
        self.assertEqual(parse_clipboard_text(declared).crs.authid(), "EPSG:23700")

    def test_garbage_and_attribute_only_text(self):
        for text in ("", "hello world", "name\tn\nfoo\t1"):
            with self.assertRaises(ClipboardError):
                parse_clipboard_text(text)

    def test_native_detection_rules(self):
        from topological_paste.clipboard import staged_is_native

        crs = QgsCoordinateReferenceSystem("EPSG:23700")
        point = QgsGeometry.fromWkt("POINT(1 1)")
        typed = StagedData(["name", "n"], {}, crs, [(point, ["a", 1])], string_only=False)
        strings = StagedData(["name", "n"], {}, crs, [(point, ["a", "1"])], string_only=True)
        native_text = "wkt_geom\tname\tn\nPoint (1 1)\ta\t1"

        # header recognition of the text QGIS writes itself (both copy formats with a header)
        self.assertTrue(staged_matches_system_clipboard(typed, native_text, ""))
        self.assertTrue(staged_matches_system_clipboard(typed, "name\tn\na\t1", ""))
        self.assertFalse(staged_matches_system_clipboard(typed, "wkt_geom\tother\nPoint (1 1)\tx", ""))

        # 1) normal native copy: we own the clipboard, the text is the QGIS text for the staged features
        self.assertTrue(staged_is_native(typed, native_text, "", True))
        # 2) attributes-only copy format: that text cannot have produced the staged geometry -> internal
        self.assertTrue(staged_is_native(typed, "name\tn\na\t1", "", True))
        # 3) no / unreadable system text: QGIS cannot have parsed features from nothing -> internal
        self.assertTrue(staged_is_native(typed, "", "", False))
        # 4) the system clipboard kept OTHER content (Windows refused the copy): staged != text -> internal
        self.assertTrue(staged_is_native(typed, "POINT(5 5)", "", False))
        # 5) external WKT that QGIS parsed itself (stale CRS!): staged == text -> external
        external = StagedData([], {}, crs, [(QgsGeometry.fromWkt("POINT(5 5)"), [])], string_only=True)
        self.assertFalse(staged_is_native(external, "POINT(5 5)", "", True))
        self.assertFalse(staged_is_native(external, "POINT(5 5)", "", False))
        # 6) QGIS-format text from ANOTHER QGIS instance: same content, string fields, not ours -> external
        self.assertFalse(staged_is_native(strings, native_text, "", False))
        # 7) ...but typed fields cannot come from tabular text -> internal even without ownership
        self.assertTrue(staged_is_native(typed, native_text, "", False))
        # 8) nothing staged / attribute rows without geometry
        self.assertFalse(staged_is_native(None, native_text, "", True))
        no_geometry = StagedData(["name"], {}, crs, [(QgsGeometry(), ["a"])], string_only=True)
        self.assertFalse(staged_is_native(no_geometry, "name\na", "", True))

if __name__ == "__main__":
    unittest.main()
