# -*- coding: utf-8 -*-
"""Shared helpers for the headless test-suite (real QGIS geometries and edit buffers)."""

import atexit
import os
import re
import shutil
import tempfile
import unittest

from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransformContext,
    QgsFeature,
    QgsGeometry,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

from topological_paste.applier import apply_plan
from topological_paste.model import ClipboardSnapshot, IncomingFeature
from topological_paste.planner import build_plan

_APP = None
_TMP_DIRS = []


def start_app():
    global _APP
    if _APP is None and QgsApplication.instance() is None:
        _APP = QgsApplication([], False)
        _APP.initQgis()
    return QgsApplication.instance()


def _cleanup():
    for path in _TMP_DIRS:
        shutil.rmtree(path, ignore_errors=True)


atexit.register(_cleanup)

PROVIDERS = ("memory", "gpkg")


def make_layer(provider="memory", wkb="Polygon", crs="EPSG:23700", fields="name:string(80),n:integer", rows=()):
    """Create a layer with features. rows = [(wkt, [attribute values])]"""
    start_app()
    uri = "%s?crs=%s" % (wkb, crs)
    for definition in [f for f in re.split(r",(?![^(]*\))", fields) if f]:  # commas inside (len,prec) stay
        uri += "&field=" + definition
    memory = QgsVectorLayer(uri, "target", "memory")
    assert memory.isValid(), uri
    features = []
    for wkt, attributes in rows:
        feature = QgsFeature(memory.fields())
        feature.setGeometry(QgsGeometry.fromWkt(wkt))
        feature.setAttributes(list(attributes))
        features.append(feature)
    ok, _added = memory.dataProvider().addFeatures(features)
    assert ok
    memory.updateExtents()
    if provider == "memory":
        return memory
    directory = tempfile.mkdtemp(prefix="topopaste_")
    _TMP_DIRS.append(directory)
    path = os.path.join(directory, "target.gpkg")
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = "target"
    result = QgsVectorFileWriter.writeAsVectorFormatV3(memory, path, QgsCoordinateTransformContext(), options)
    assert result[0] == QgsVectorFileWriter.NoError, result
    layer = QgsVectorLayer("%s|layername=target" % path, "target", "ogr")
    assert layer.isValid()
    return layer


def make_snapshot(rows, field_names=("name", "n"), crs="EPSG:23700", origin=ClipboardSnapshot.ORIGIN_NATIVE):
    """rows = [(wkt, {field: value})] in incoming order."""
    start_app()
    features = [
        IncomingFeature(order, QgsGeometry.fromWkt(wkt) if wkt else QgsGeometry(), dict(attributes))
        for order, (wkt, attributes) in enumerate(rows)
    ]
    return ClipboardSnapshot(origin, QgsCoordinateReferenceSystem(crs), list(field_names), features)


def paste(layer, snapshot, options=None, ops=None, expect_blocking=False, fault_hook=None):
    plan = build_plan(layer, snapshot, options, ops=ops)
    if expect_blocking:
        assert plan.blocking(), "expected blocking issues"
        return plan, None
    assert not plan.blocking(), [i.message for i in plan.blocking()]
    result = apply_plan(layer, plan, fault_hook=fault_hook)
    return plan, result


def features_of(layer):
    return sorted(layer.getFeatures(), key=lambda f: f.id())


def by_attribute(layer, field, value):
    return [f for f in layer.getFeatures() if f[field] == value]


def state(layer):
    """Comparable snapshot of the layer contents as seen through the edit buffer."""
    return sorted((f.id(), f.geometry().asWkt(), [repr(v) for v in f.attributes()]) for f in layer.getFeatures())


def union_of(geometries):
    geometries = [g for g in geometries if g is not None and not g.isNull()]
    return QgsGeometry.unaryUnion(geometries) if geometries else QgsGeometry()


def layer_union(layer):
    return union_of([f.geometry() for f in layer.getFeatures()])


def max_pairwise_overlap(layer):
    features = list(layer.getFeatures())
    worst = 0.0
    for i, a in enumerate(features):
        for b in features[i + 1:]:
            if a.geometry().boundingBoxIntersects(b.geometry()):
                worst = max(worst, a.geometry().intersection(b.geometry()).area())
    return worst


def wkt_rect(x0, y0, x1, y1):
    return "POLYGON((%s %s,%s %s,%s %s,%s %s,%s %s))" % (x0, y0, x1, y0, x1, y1, x0, y1, x0, y0)


class QgisTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        start_app()

    def assertAreaAlmost(self, geometry, expected, places=9):  # noqa: N802
        self.assertAlmostEqual(geometry.area(), expected, places=places)

    def assertCoverageEqual(self, a, b, tolerance=1e-9):  # noqa: N802
        self.assertLessEqual(a.symDifference(b).area(), tolerance)
