# -*- coding: utf-8 -*-
"""Version compatibility helpers (QGIS 3.28+ / Qt5, QGIS 3.x Qt6 builds, QGIS 4 / Qt6).

Everything that differs between PyQt5/PyQt6 or between QGIS API generations is
funnelled through this module so the rest of the plugin stays version-agnostic.
Every branch here was checked against a real runtime (see README, compatibility
matrix) or is a guarded ``getattr`` fallback that cannot raise at import time.
"""

from qgis.core import (
    NULL,
    Qgis,
    QgsFeatureRequest,
    QgsFieldConstraints,
    QgsFields,
    QgsGeometry,
    QgsJsonUtils,
    QgsProject,
    QgsVectorDataProvider,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QT_VERSION_STR, QMetaObject, Qt

try:  # Qt6: QAction lives in QtGui
    from qgis.PyQt.QtGui import QAction  # noqa: F401
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction  # noqa: F401

IS_QT6 = QT_VERSION_STR.split(".")[0] == "6"
QGIS_VERSION_INT = Qgis.QGIS_VERSION_INT


def _pick(*candidates):
    """Return the first attribute path that resolves.

    Each candidate is ``(object, "Dotted.path")``. Used for enums that moved
    from class level (``Qgis.Info``) to scoped enums (``Qgis.MessageLevel.Info``).
    """
    for obj, path in candidates:
        try:
            for part in path.split("."):
                obj = getattr(obj, part)
            return obj
        except AttributeError:
            continue
    raise AttributeError("none of the enum candidates resolved: %r" % (candidates,))


def enum_int(value):
    """int() of an enum value; PyQt6 plain enums need ``.value``."""
    try:
        return int(value)
    except TypeError:
        return int(value.value)


# --- message levels -----------------------------------------------------------
MSG_INFO = _pick((Qgis, "MessageLevel.Info"), (Qgis, "Info"))
MSG_WARNING = _pick((Qgis, "MessageLevel.Warning"), (Qgis, "Warning"))
MSG_CRITICAL = _pick((Qgis, "MessageLevel.Critical"), (Qgis, "Critical"))
MSG_SUCCESS = _pick((Qgis, "MessageLevel.Success"), (Qgis, "Success"))

# --- geometry families --------------------------------------------------------
GEOM_POINT = _pick((Qgis, "GeometryType.Point"), (QgsWkbTypes, "PointGeometry"))
GEOM_LINE = _pick((Qgis, "GeometryType.Line"), (QgsWkbTypes, "LineGeometry"))
GEOM_POLYGON = _pick((Qgis, "GeometryType.Polygon"), (QgsWkbTypes, "PolygonGeometry"))


def geometry_dimension(geometry_type):
    """0 / 1 / 2 for point / line / polygon families, None otherwise."""
    if geometry_type == GEOM_POINT:
        return 0
    if geometry_type == GEOM_LINE:
        return 1
    if geometry_type == GEOM_POLYGON:
        return 2
    return None


def geometry_type_for_dimension(dim):
    return {0: GEOM_POINT, 1: GEOM_LINE, 2: GEOM_POLYGON}[dim]


# --- field origins --------------------------------------------------------------
ORIGIN_PROVIDER = _pick((Qgis, "FieldOrigin.Provider"), (QgsFields, "OriginProvider"))
ORIGIN_JOIN = _pick((Qgis, "FieldOrigin.Join"), (QgsFields, "OriginJoin"))
ORIGIN_EDIT = _pick((Qgis, "FieldOrigin.Edit"), (QgsFields, "OriginEdit"))
ORIGIN_EXPRESSION = _pick((Qgis, "FieldOrigin.Expression"), (QgsFields, "OriginExpression"))

# --- field constraints ----------------------------------------------------------
CONSTRAINT_NOT_NULL = _pick(
    (QgsFieldConstraints, "Constraint.ConstraintNotNull"), (QgsFieldConstraints, "ConstraintNotNull")
)
CONSTRAINT_UNIQUE = _pick(
    (QgsFieldConstraints, "Constraint.ConstraintUnique"), (QgsFieldConstraints, "ConstraintUnique")
)
CONSTRAINT_EXPRESSION = _pick(
    (QgsFieldConstraints, "Constraint.ConstraintExpression"), (QgsFieldConstraints, "ConstraintExpression")
)
STRENGTH_HARD = _pick(
    (QgsFieldConstraints, "ConstraintStrength.ConstraintStrengthHard"),
    (QgsFieldConstraints, "ConstraintStrengthHard"),
)
STRENGTH_SOFT = _pick(
    (QgsFieldConstraints, "ConstraintStrength.ConstraintStrengthSoft"),
    (QgsFieldConstraints, "ConstraintStrengthSoft"),
)
STRENGTH_NOT_SET = _pick(
    (QgsFieldConstraints, "ConstraintStrength.ConstraintStrengthNotSet"),
    (QgsFieldConstraints, "ConstraintStrengthNotSet"),
)

# --- provider capabilities ------------------------------------------------------
CAP_ADD_FEATURES = _pick(
    (Qgis, "VectorProviderCapability.AddFeatures"), (QgsVectorDataProvider, "AddFeatures")
)
CAP_DELETE_FEATURES = _pick(
    (Qgis, "VectorProviderCapability.DeleteFeatures"), (QgsVectorDataProvider, "DeleteFeatures")
)
CAP_CHANGE_GEOMETRIES = _pick(
    (Qgis, "VectorProviderCapability.ChangeGeometries"), (QgsVectorDataProvider, "ChangeGeometries")
)
CAP_CHANGE_ATTRIBUTES = _pick(
    (Qgis, "VectorProviderCapability.ChangeAttributeValues"),
    (QgsVectorDataProvider, "ChangeAttributeValues"),
)


def provider_has_capability(provider, capability):
    caps = provider.capabilities()
    try:
        return bool(caps & capability)
    except TypeError:
        return bool(enum_int(caps) & enum_int(capability))


# --- field type classification ---------------------------------------------------
# QMetaType::Type and the legacy QVariant::Type share their numeric values, so
# classifying by number works for Qt5 and Qt6 and for both QgsField APIs.
_KIND_BY_TYPE_ID = {
    1: "bool",
    2: "int",
    3: "int",
    4: "int",
    5: "int",
    6: "double",
    38: "double",  # float
    10: "string",
    14: "date",
    15: "time",
    16: "datetime",
}


def field_kind(field):
    return _KIND_BY_TYPE_ID.get(enum_int(field.type()), "other")


def field_is_read_only(field):
    """QgsField.isReadOnly() exists since QGIS 3.18."""
    try:
        return bool(field.isReadOnly())
    except AttributeError:
        return False


# --- NULL handling -----------------------------------------------------------------
def is_null(value):
    """True for Python None and for NULL QVariants (both occur, depending on bindings)."""
    if value is None:
        return True
    try:
        if value == NULL:
            return True
    except Exception:  # exotic values that refuse comparison
        pass
    is_null_method = getattr(value, "isNull", None)
    # QDate/QDateTime/QString-likes also have isNull(); an invalid date is a NULL.
    if callable(is_null_method):
        try:
            return bool(is_null_method())
        except Exception:
            return False
    return False


# --- geometry equality -----------------------------------------------------------------
def topologically_equal(a, b):
    """Topological (GEOS ``equals``) comparison - NOT structural coordinate equality.

    ``QgsGeometry.equals()`` / ``isExactlyEqual`` compare vertex by vertex and would
    miss rotated rings, reversed orientation or redundant collinear vertices.
    Newer QGIS exposes ``isTopologicallyEqual``; older versions call the same
    GEOS predicate ``isGeosEqual``.
    """
    method = getattr(a, "isTopologicallyEqual", None)
    if method is None:
        method = a.isGeosEqual
    return bool(method(b))


# --- JSON helpers (Qt6 builds dropped the QTextCodec argument) ----------------------------
def json_string_to_fields(text):
    try:
        return QgsJsonUtils.stringToFields(text)
    except TypeError:
        return QgsJsonUtils.stringToFields(text, None)


def json_string_to_features(text, fields):
    try:
        return QgsJsonUtils.stringToFeatureList(text, fields)
    except TypeError:
        return QgsJsonUtils.stringToFeatureList(text, fields, None)


# --- transactions -------------------------------------------------------------------------
def layer_in_transaction(layer):
    """True when edits would bypass the ordinary per-layer edit buffer.

    * automatic transaction groups: the provider holds a DB transaction and
      edits are written straight to the database (undo = savepoints);
    * buffered transaction groups (QGIS >= 3.26): one shared buffer for several
      layers, undo/rollback are group operations.
    In neither mode can this plugin guarantee "one layer, one undo command,
    nothing written", so such layers are rejected before planning.
    """
    provider = layer.dataProvider()
    try:
        if provider is not None and provider.transaction() is not None:
            return True
    except AttributeError:
        pass
    project = QgsProject.instance()
    mode_getter = getattr(project, "transactionMode", None)
    if mode_getter is not None:
        try:
            disabled = _pick((Qgis, "TransactionMode.Disabled"))
            if mode_getter() != disabled:
                group = getattr(project, "editBufferGroup", None)
                if group is not None and layer in group().layers():
                    return True
        except Exception:
            return False
    return False


# --- dialogs ----------------------------------------------------------------------------------
def exec_dialog(dialog):
    runner = getattr(dialog, "exec", None) or getattr(dialog, "exec_")
    return runner()


WINDOW_MODAL = _pick((Qt, "WindowModality.WindowModal"), (Qt, "WindowModal"))
APPLICATION_MODAL = _pick((Qt, "WindowModality.ApplicationModal"), (Qt, "ApplicationModal"))
DIRECT_CONNECTION = _pick((Qt, "ConnectionType.DirectConnection"), (Qt, "DirectConnection"))
ITEM_IS_EDITABLE = _pick((Qt, "ItemFlag.ItemIsEditable"), (Qt, "ItemIsEditable"))
PLAIN_TEXT = _pick((Qt, "TextFormat.PlainText"), (Qt, "PlainText"))
USER_ROLE = _pick((Qt, "ItemDataRole.UserRole"), (Qt, "UserRole"))


def invoke_slot_with_string(obj, slot_name, text):
    """Invoke a (not Python-wrapped) C++ slot taking one QString, synchronously.

    Used for QgisApp::pasteAsNewMemoryVector(QString), which is a public slot of
    the main window but is not part of the ``iface`` Python API. Returns False
    when the slot does not exist so callers can fall back gracefully.
    """
    meta = obj.metaObject()
    signature = ("%s(QString)" % slot_name).encode("ascii")
    if meta.indexOfMethod(signature) < 0:
        return False
    from qgis.PyQt.QtCore import Q_ARG

    QMetaObject.invokeMethod(obj, slot_name, DIRECT_CONNECTION, Q_ARG(str, text))
    return True


def feature_request_no_attributes():
    request = QgsFeatureRequest()
    request.setSubsetOfAttributes([])
    return request


def geometry_is_curved(geometry):
    return QgsWkbTypes.isCurvedType(geometry.wkbType())


def make_valid(geometry):
    """makeValid with default (GEOS linework) method on every supported version."""
    return QgsGeometry(geometry).makeValid()
