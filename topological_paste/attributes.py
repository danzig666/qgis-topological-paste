# -*- coding: utf-8 -*-
"""Attribute mapping, value conversion, defaults and constraint checks.

Rules implemented here (design section 8):

* mapping is by field *name* (exact match; optional explicit override), never
  by column position and never by guessing between similar names;
* joined, virtual, read-only and provider-managed key fields are never written;
* a field that is *absent* from the source keeps the existing value (reused
  feature) or receives the legitimate target default (new feature), whereas an
  explicit incoming NULL is a value and overwrites;
* managed primary keys are recognised from provider metadata
  (``primaryKeyAttributes`` + a provider default clause such as GeoPackage's
  ``Autogenerate`` or a PostgreSQL ``nextval(...)``), not from field names;
* business identifiers are never altered to satisfy a unique constraint - the
  conflict is reported and blocks the paste.
"""

from qgis.core import (
    NULL,
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsFeature,
    QgsVectorLayerUtils,
)

from . import compat
from .model import Issue


class TargetField(object):
    __slots__ = ("index", "name", "field", "writable", "reason", "managed_key", "default_clause", "provider_index")

    def __init__(self, index, field):
        self.index = index
        self.name = field.name()
        self.field = field
        self.writable = True
        self.reason = ""
        self.managed_key = False
        self.default_clause = ""
        self.provider_index = -1


def describe_target_fields(layer):
    """Classify every target field: writable? managed key? provider default clause?"""
    fields = layer.fields()
    provider = layer.dataProvider()
    pk_indexes = set(layer.primaryKeyAttributes())
    result = []
    for index in range(fields.count()):
        info = TargetField(index, fields.at(index))
        origin = fields.fieldOrigin(index)
        if origin == compat.ORIGIN_JOIN:
            info.writable, info.reason = False, "joined field"
        elif origin == compat.ORIGIN_EXPRESSION:
            info.writable, info.reason = False, "virtual (expression) field"
        elif compat.field_is_read_only(info.field):
            info.writable, info.reason = False, "read-only / generated field"
        elif origin == compat.ORIGIN_PROVIDER:
            info.provider_index = fields.fieldOriginIndex(index)
            try:
                info.default_clause = provider.defaultValueClause(info.provider_index) or ""
            except Exception:
                info.default_clause = ""
            if index in pk_indexes and info.default_clause:
                # Provider-generated key (GPKG fid "Autogenerate", serial/identity columns).
                # A key column *without* a generator is an ordinary business field.
                info.managed_key = True
                info.writable, info.reason = False, "provider-managed primary key"
        result.append(info)
    return result


class FieldMapping(object):
    """source field name -> target field index, plus everything that was left out and why."""

    def __init__(self):
        self.pairs = []  # [(source name, TargetField)]
        self.unmapped_source = []  # source fields without target
        self.skipped = []  # [(source name, target name, reason)]

    def target_indexes(self):
        return [target.index for _name, target in self.pairs]


def build_mapping(source_field_names, target_fields, override=None):
    """Exact-name mapping with an optional explicit override {source: target name or None}.

    Ambiguity is never resolved by guessing: a source name that is not matched
    exactly stays unmapped (and is reported) unless the user maps it explicitly.
    """
    by_name = {}
    for target in target_fields:
        by_name.setdefault(target.name, target)
    mapping = FieldMapping()
    used = set()
    for name in source_field_names:
        if override is not None and name in override:
            wanted = override[name]
            target = by_name.get(wanted) if wanted else None
        else:
            target = by_name.get(name)
        if target is None:
            mapping.unmapped_source.append(name)
            continue
        if not target.writable:
            mapping.skipped.append((name, target.name, target.reason))
            continue
        if target.index in used:
            mapping.skipped.append((name, target.name, "target field already mapped"))
            continue
        used.add(target.index)
        mapping.pairs.append((name, target))
    return mapping


# ---------------------------------------------------------------------------------------
# value conversion
# ---------------------------------------------------------------------------------------
def values_equal(a, b):
    a_null, b_null = compat.is_null(a), compat.is_null(b)
    if a_null or b_null:
        return a_null and b_null
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    try:
        return bool(a == b)
    except Exception:
        return False


def _is_lossy(original, converted, kind):
    if compat.is_null(original):
        return False
    if kind == "int":
        try:
            return float(original) != float(converted)
        except (TypeError, ValueError):
            return False
    if kind == "double" and isinstance(original, (int, float)) and not isinstance(original, bool):
        return float(original) != float(converted)
    if kind == "date" and hasattr(original, "time") and hasattr(original, "date"):
        time = original.time()
        return bool(time.isValid() and (time.hour() or time.minute() or time.second() or time.msec()))
    return False


def convert_value(field, value):
    """Convert ``value`` for ``field``.

    Returns (ok, converted, lossy, message). NULL stays NULL. Conversion relies
    on QgsField.convertCompatible (the same rules QGIS applies while editing)
    and additionally detects silent precision loss such as 5.7 -> 6.
    """
    if compat.is_null(value):
        return True, NULL, False, ""
    kind = compat.field_kind(field)
    try:
        result = field.convertCompatible(value)
    except (ValueError, TypeError) as exc:
        return False, NULL, False, str(exc)
    if isinstance(result, bool) and kind != "bool" and not isinstance(value, bool):
        # Old bindings: convertCompatible() only reports success and does not
        # hand back the converted value -> convert conservatively ourselves.
        if not result:
            return False, NULL, False, "value is not compatible with field type %s" % field.typeName()
        result = _fallback_convert(kind, value)
        if result is None:
            return False, NULL, False, "value is not compatible with field type %s" % field.typeName()
    if compat.is_null(result):
        return False, NULL, False, "value could not be represented in field type %s" % field.typeName()
    lossy = _is_lossy(value, result, kind)
    message = "%r stored as %r" % (value, result) if lossy else ""
    return True, result, lossy, message


def _fallback_convert(kind, value):
    try:
        if kind == "int":
            return int(round(float(value)))
        if kind == "double":
            return float(value)
        if kind == "string":
            return value if isinstance(value, str) else str(value)
    except (TypeError, ValueError):
        return None
    return value


# ---------------------------------------------------------------------------------------
# planned attribute sets
# ---------------------------------------------------------------------------------------
class AttributePlanner(object):
    """Builds attribute lists for new / reused / fragment features of one target layer."""

    def __init__(self, layer, source_field_names, override=None):
        self.layer = layer
        self.fields = layer.fields()
        self.targets = describe_target_fields(layer)
        self.mapping = build_mapping(source_field_names, self.targets, override)
        self.conversion_problems = []  # (order, source field, target field, message, failed?)
        self._context = None

    # -- helpers ----------------------------------------------------------------------
    def expression_context(self, feature=None):
        if self._context is None:
            self._context = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(self.layer))
        context = QgsExpressionContext(self._context)
        if feature is not None:
            context.setFeature(feature)
        return context

    def _default_for(self, target, feature):
        """Legitimate default of a field that the source does not provide."""
        definition = self.layer.defaultValueDefinition(target.index)
        if definition.isValid():
            value = self.layer.defaultValue(target.index, feature, self.expression_context(feature))
            if not compat.is_null(value):
                return value
        if target.default_clause:
            # QGIS convention: the clause text is stored in the edit buffer and
            # evaluated by the provider on commit (e.g. "Autogenerate", "nextval(...)").
            return target.default_clause
        if target.provider_index >= 0:
            try:
                value = self.layer.dataProvider().defaultValue(target.provider_index)
            except Exception:
                value = None
            if not compat.is_null(value):
                return value
        return NULL

    def _converted_incoming(self, incoming, order):
        """{target index: converted value} for the mapped fields present in the source feature."""
        values = {}
        for source_name, target in self.mapping.pairs:
            if source_name not in incoming:
                continue  # absent in this source feature -> not an explicit NULL
            ok, converted, lossy, message = convert_value(target.field, incoming[source_name])
            if not ok:
                self.conversion_problems.append((order, source_name, target.name, message, True))
                continue
            if lossy:
                self.conversion_problems.append((order, source_name, target.name, message, False))
            values[target.index] = converted
        return values

    # -- public API -------------------------------------------------------------------------
    def new_feature_attributes(self, incoming, geometry, order):
        """Full attribute list for a brand-new feature carrying the incoming attributes."""
        values = self._converted_incoming(incoming, order)
        feature = QgsFeature(self.fields)
        feature.setGeometry(geometry)
        for index, value in values.items():
            feature.setAttribute(index, value)
        attributes = [NULL] * self.fields.count()
        for target in self.targets:
            if target.index in values and not compat.is_null(values[target.index]):
                attributes[target.index] = values[target.index]
            elif target.managed_key:
                attributes[target.index] = target.default_clause  # regenerated by the provider
            elif not target.writable:
                attributes[target.index] = NULL
            elif target.index in values and not self._not_null(target):
                attributes[target.index] = NULL  # explicit incoming NULL is a value
            else:
                attributes[target.index] = self._default_for(target, feature)
        return attributes

    def reuse_changes(self, existing_attributes, incoming, order):
        """{index: (old, new)} for an existing feature that is reused for an incoming one.

        Only mapped writable fields can change; fields absent from the source and
        the managed key keep their values. A NULL that a NOT NULL constraint
        forbids leaves the existing value in place (reported by the constraint check).
        """
        values = self._converted_incoming(incoming, order)
        changes = {}
        for index, new_value in values.items():
            old_value = existing_attributes[index] if index < len(existing_attributes) else NULL
            if compat.is_null(new_value) and self._not_null(self.targets[index]):
                continue
            if not values_equal(old_value, new_value):
                changes[index] = (old_value, new_value)
        return changes

    def fragment_attributes(self, existing_attributes):
        """Attributes of an additional fragment split off an existing singlepart feature.

        Ordinary attributes are copied unchanged; only provider-managed keys are
        regenerated. Nothing else is recomputed.
        """
        attributes = list(existing_attributes)
        while len(attributes) < self.fields.count():
            attributes.append(NULL)
        for target in self.targets:
            if target.managed_key:
                attributes[target.index] = target.default_clause
            elif not target.writable:
                attributes[target.index] = NULL
        return attributes

    def _not_null(self, target):
        return bool(target.field.constraints().constraints() & compat.CONSTRAINT_NOT_NULL)

    def apply_on_update_fields(self):
        names = []
        for target in self.targets:
            definition = self.layer.defaultValueDefinition(target.index)
            if definition.isValid() and definition.applyOnUpdate():
                names.append(target.name)
        return names

    # -- constraints -----------------------------------------------------------------------------
    def check_constraints(self, new_features, updated, deleted_fids):
        """Check the *planned final state* (including pending edits of the layer).

        ``new_features``: list of (label, attribute list, geometry)
        ``updated``:      dict fid -> (label, full attribute list after the update, geometry)
        ``deleted_fids``: features that will be gone (their values no longer collide)
        Returns a list of Issue objects. Hard constraints block, soft ones warn.
        Provider-side rules (triggers, foreign keys, CHECKs) cannot be evaluated
        here - they run when the user saves the layer.
        """
        issues = []
        deleted = set(deleted_fids)
        for target in self.targets:
            if target.managed_key or not target.writable:
                continue
            constraints = target.field.constraints()
            flags = constraints.constraints()

            if flags & compat.CONSTRAINT_NOT_NULL:
                severity = self._severity(constraints, compat.CONSTRAINT_NOT_NULL)
                for label, attributes, _geometry in new_features:
                    if compat.is_null(attributes[target.index]):
                        issues.append(
                            Issue(severity, "not_null", "%s: field '%s' must not be NULL" % (label, target.name))
                        )

            if flags & compat.CONSTRAINT_UNIQUE:
                severity = self._severity(constraints, compat.CONSTRAINT_UNIQUE)
                seen = {}
                planned = [(label, attributes[target.index], None) for label, attributes, _g in new_features]
                planned += [(label, attributes[target.index], fid) for fid, (label, attributes, _g) in updated.items()]
                existing_values = None
                for label, value, fid in planned:
                    if compat.is_null(value):
                        continue
                    key = repr(value)
                    if key in seen:
                        issues.append(
                            Issue(
                                severity,
                                "unique",
                                "%s and %s would both have %s = %r (unique constraint). "
                                "Business identifiers are not changed automatically."
                                % (seen[key], label, target.name, value),
                            )
                        )
                        continue
                    seen[key] = label
                    if existing_values is None:
                        existing_values = set(repr(v) for v in self.layer.uniqueValues(target.index))
                    if key not in existing_values:
                        continue
                    ignore = set(deleted) | set(updated.keys())
                    if fid is not None:
                        ignore.add(fid)
                    if QgsVectorLayerUtils.valueExists(self.layer, target.index, value, list(ignore)):
                        issues.append(
                            Issue(
                                severity,
                                "unique",
                                "%s: %s = %r already exists in the layer (unique constraint). "
                                "Business identifiers are not changed automatically." % (label, target.name, value),
                            )
                        )

            if flags & compat.CONSTRAINT_EXPRESSION and constraints.constraintExpression():
                severity = self._severity(constraints, compat.CONSTRAINT_EXPRESSION)
                expression = QgsExpression(constraints.constraintExpression())
                rows = [(label, attributes, geometry) for label, attributes, geometry in new_features]
                rows += [(label, attributes, geometry) for _fid, (label, attributes, geometry) in updated.items()]
                for label, attributes, geometry in rows:
                    feature = QgsFeature(self.fields)
                    feature.setAttributes(list(attributes))
                    if geometry is not None:
                        feature.setGeometry(geometry)
                    context = self.expression_context(feature)
                    expression.prepare(context)
                    value = expression.evaluate(context)
                    if expression.hasEvalError() or compat.is_null(value) or not value:
                        issues.append(
                            Issue(
                                severity,
                                "expression",
                                "%s: constraint on '%s' fails (%s)"
                                % (label, target.name, constraints.constraintExpression()),
                            )
                        )
        return issues

    @staticmethod
    def _severity(constraints, constraint):
        strength = constraints.constraintStrength(constraint)
        return Issue.WARNING if strength == compat.STRENGTH_SOFT else Issue.BLOCKING
