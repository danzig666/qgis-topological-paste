# -*- coding: utf-8 -*-
"""Phase A - build an explicit change plan without touching the target layer.

Overwrite semantics (design section 2), for incoming features P1..Pn in the
deterministic snapshot order:

    Q_i = P_i - union(P_j, j > i)        last incoming feature wins inside overlaps
    U   = union(P_i)                     used ONLY as a cutting mask, never as output
    R   = E - U                          for every existing feature E underneath

The same algebra is used for polygons, lines and points; only the notion of
"underneath" changes with the dimension of the family (shared area / shared
line portion / coincident point), expressed as a DE-9IM interior pattern in
``geometry_ops``.

Performance notes (section 10): one spatially filtered pass over the target,
one spatial index over the incoming boxes, local masks (the union of just the
incoming features that really interact with a given existing feature) cached
per combination, no global union of the whole batch.
"""

import hashlib
import math

from qgis.core import (
    QgsCoordinateTransform,
    QgsCsException,
    QgsFeatureRequest,
    QgsGeometry,
    QgsProject,
    QgsRectangle,
    QgsSpatialIndex,
    QgsVectorLayer,
    QgsWkbTypes,
)

from . import compat
from .attributes import AttributePlanner
from .geometry_ops import (
    GeometryOpError,
    GeometryOps,
    adapt_to_layer,
    collect,
    drop_z_m,
    family_parts,
    has_z_or_m,
    measure,
    node_shared_boundaries,
    noding_tolerance,
    part_sort_key,
)
from .model import ExistingChange, Issue, NewFeature, Plan, PlanError, PlanOptions


class PlanCanceled(Exception):
    pass


class Feedback(object):
    """No-op feedback; the GUI passes a subclass bound to a modal progress dialog."""

    def progress(self, fraction, text=None):
        pass

    def canceled(self):
        return False


class _Incoming(object):
    __slots__ = ("feature", "geometry", "parts", "effective", "untouched")

    def __init__(self, feature, geometry):
        self.feature = feature
        self.geometry = geometry  # prepared P_i (destination CRS, valid, 2D)
        self.parts = None
        self.effective = []  # singlepart pieces of Q_i
        self.untouched = True


class _Candidate(object):
    __slots__ = ("fid", "original", "work", "attributes", "interacting", "repaired", "rect")

    def __init__(self, fid, geometry, attributes):
        self.fid = fid
        self.original = geometry
        self.work = geometry
        self.attributes = attributes
        self.interacting = []
        self.repaired = False
        self.rect = geometry.boundingBox()


# ---------------------------------------------------------------------------------------
# preconditions
# ---------------------------------------------------------------------------------------
def check_layer_preconditions(layer):
    """Reject targets for which the guarantees cannot be met. Returns the family dimension."""
    if layer is None or not isinstance(layer, QgsVectorLayer):
        raise PlanError("The active layer is not a vector layer.")
    if not layer.isValid():
        raise PlanError("The target layer is not valid.")
    if not layer.isSpatial():
        raise PlanError("The target layer has no geometry.")
    dim = compat.geometry_dimension(layer.geometryType())
    if dim is None:
        raise PlanError("Only point, line and polygon layers are supported.")
    wkb = layer.wkbType()
    if QgsWkbTypes.isCurvedType(wkb):
        raise PlanError(
            "The target layer has a curved geometry type (%s). Overlay results would have to be "
            "segmentised, which this plugin refuses to do silently." % QgsWkbTypes.displayString(wkb)
        )
    if has_z_or_m(wkb):
        raise PlanError(
            "The target layer stores Z or M values (%s). The overlay is a 2D operation and would have to "
            "invent Z/M values for new vertices, so such layers are rejected."
            % QgsWkbTypes.displayString(wkb)
        )
    if layer.subsetString():
        raise PlanError(
            "The target layer has a provider filter (%s). Features hidden by the filter could collide "
            "with the pasted coverage without being seen. Remove the filter yourself and run the paste again."
            % layer.subsetString()
        )
    if layer.readOnly() or not layer.supportsEditing():
        raise PlanError("The target layer is read-only.")
    provider = layer.dataProvider()
    if not compat.provider_has_capability(provider, compat.CAP_ADD_FEATURES):
        raise PlanError("The data provider of the target layer cannot add features.")
    if compat.layer_in_transaction(layer):
        raise PlanError(
            "The target layer is edited through a transaction group. Edits would bypass the ordinary edit "
            "buffer, so a single undoable, uncommitted paste cannot be guaranteed. Disable transaction "
            "groups for this project to use Topological Paste."
        )
    if layer.isEditable() and layer.isEditCommandActive():
        raise PlanError("Another edit command is still active on the target layer.")
    return dim


# ---------------------------------------------------------------------------------------
# stale detection
# ---------------------------------------------------------------------------------------
def _feature_digest(feature):
    digest = hashlib.sha1()
    geometry = feature.geometry()
    if geometry is not None and not geometry.isNull():
        digest.update(bytes(geometry.asWkb()))
    digest.update(repr([None if compat.is_null(v) else v for v in feature.attributes()]).encode("utf-8", "replace"))
    return digest.hexdigest()


def _fields_signature(layer):
    return [(f.name(), compat.enum_int(f.type()), f.length(), f.precision()) for f in layer.fields()]


def _incoming_index(rects):
    index = QgsSpatialIndex()
    for i, rect in enumerate(rects):
        index.addFeature(i, rect)
    return index


def iter_candidates(layer, rects, tolerance):
    """Existing features (edit buffer included) whose box meets the box of an incoming feature.

    One spatially filtered request; selection and canvas visibility play no role.
    """
    if not rects:
        return
    total = QgsRectangle(rects[0])
    for rect in rects[1:]:
        total.combineExtentWith(rect)
    total.grow(tolerance)
    index = _incoming_index(rects)
    request = QgsFeatureRequest().setFilterRect(total)
    for feature in layer.getFeatures(request):
        if not feature.hasGeometry():
            continue
        box = feature.geometry().boundingBox()
        box.grow(tolerance)
        hits = index.intersects(box)
        if hits:
            yield feature, hits


def compute_fingerprint(layer, rects, tolerance):
    features = {}
    for feature, _hits in iter_candidates(layer, rects, tolerance):
        features[feature.id()] = _feature_digest(feature)
    return {
        "rects": [QgsRectangle(r) for r in rects],
        "tolerance": tolerance,
        "features": features,
        "fields": _fields_signature(layer),
        "editable": layer.isEditable(),
        "undo_index": layer.undoStack().index() if layer.isEditable() else -1,
        "crs": layer.crs().authid() or layer.crs().toWkt(),
        "subset": layer.subsetString(),
    }


# ---------------------------------------------------------------------------------------
# the planner
# ---------------------------------------------------------------------------------------
class Planner(object):
    def __init__(self, layer, snapshot, options=None, feedback=None, ops=None, transform_context=None):
        self.layer = layer
        self.snapshot = snapshot
        self.options = options or PlanOptions()
        self.feedback = feedback or Feedback()
        self.ops = ops or GeometryOps()
        self.transform_context = transform_context or QgsProject.instance().transformContext()
        self.plan = None
        self.dim = None
        self.grid = 0.0
        self.tolerance = 0.0

    # -- small helpers --------------------------------------------------------------------
    def _issue(self, severity, code, message, fids=None, category="general"):
        self.plan.issues.append(Issue(severity, code, message, fids, category))

    def _tick(self, fraction, text=None):
        self.feedback.progress(fraction, text)
        if self.feedback.canceled():
            raise PlanCanceled()

    # -- entry point ----------------------------------------------------------------------------
    def build(self):
        layer = self.layer
        self.dim = check_layer_preconditions(layer)
        plan = self.plan = Plan(layer.id(), layer.name())
        plan.options = self.options
        plan.snapshot = self.snapshot
        plan.dim = self.dim

        self.grid = max(0.0, self.options.grid_size)
        layer_precision = 0.0
        try:
            layer_precision = float(layer.geometryOptions().geometryPrecision() or 0.0)
        except AttributeError:
            pass
        if layer_precision > 0.0:
            # QGIS itself snaps every edited geometry of this layer to that precision.
            # Planning on unsnapped coordinates would be invalidated during apply, so
            # the layer's own precision becomes the (consistent) grid of the operation.
            self.grid = layer_precision
            self._issue(
                Issue.WARNING,
                "layer_precision",
                "The layer's geometry precision (%g %s) is active: QGIS snaps every edited geometry to it, "
                "so it is used as the precision grid of this paste." % (layer_precision, self._units()),
            )
        plan.grid_size = self.grid

        self._tick(0.02, "Preparing clipboard features")
        incoming = self._prepare_incoming()
        if not incoming and not plan.blocking():
            raise PlanError("The clipboard contains no usable geometry for this layer.")

        rects = [QgsRectangle(item.geometry.boundingBox()) for item in incoming]
        total = QgsRectangle(rects[0]) if rects else QgsRectangle()
        for rect in rects[1:]:
            total.combineExtentWith(rect)
        self.tolerance = plan.tolerance = noding_tolerance(total) if rects else 0.0
        plan.affected_rect = total

        # The fingerprint is taken first so that anything changing the layer during
        # planning is detected as well.
        plan.fingerprint = compute_fingerprint(layer, rects, self.tolerance)

        self._tick(0.10, "Resolving overlaps inside the clipboard batch")
        self._resolve_incoming_priority(incoming)
        outputs = self._make_outputs(incoming)

        self._tick(0.25, "Searching existing features underneath")
        affected, neighbours = self._classify_candidates(incoming, rects, outputs)

        self._tick(0.50, "Detecting identical geometries")
        attribute_planner = AttributePlanner(layer, self.snapshot.field_names, self.options.mapping_override)
        plan.mapping = attribute_planner.mapping
        reuse = self._detect_reuse(outputs, affected)

        self._tick(0.60, "Trimming existing features")
        remainders = self._trim_existing(incoming, affected, reuse)

        self._tick(0.75, "Noding shared boundaries")
        self._node(outputs, reuse, remainders, neighbours)
        if self.grid > 0.0:
            self._snap_results(outputs, reuse, remainders, neighbours)

        self._tick(0.85, "Mapping attributes")
        self._plan_attributes(attribute_planner, incoming, outputs, reuse, remainders, neighbours)

        self._tick(0.92, "Validating the planned result")
        self._validate(incoming, outputs, reuse, remainders, neighbours, affected)
        self._check_capabilities()
        self._issue(
            Issue.INFO,
            "provider_checks",
            "Provider-side rules (database triggers, foreign keys, CHECK constraints) are evaluated only "
            "when you save the layer edits; they have not been checked.",
        )
        self._tick(1.0)
        return plan

    def _units(self):
        try:
            from qgis.core import QgsUnitTypes

            return QgsUnitTypes.toString(self.layer.crs().mapUnits())
        except Exception:
            return "map units"

    # -- step 1: incoming features -----------------------------------------------------------------
    def _prepare_incoming(self):
        plan, layer, dim = self.plan, self.layer, self.dim
        source_crs = self.snapshot.crs
        target_crs = layer.crs()
        transform = None
        if source_crs != target_crs:
            if not source_crs.isValid():
                raise PlanError("The CRS of the clipboard data is unknown; it is never guessed.")
            if not target_crs.isValid():
                raise PlanError("The target layer has no valid CRS, so clipboard data cannot be transformed.")
            transform = QgsCoordinateTransform(source_crs, target_crs, self.transform_context)
            if not transform.isValid():
                raise PlanError(
                    "No coordinate transformation from %s to %s is available."
                    % (source_crs.authid(), target_crs.authid())
                )

        wanted_type = compat.geometry_type_for_dimension(dim)
        prepared = []
        dropped_zm = 0
        snapped = 0
        for feature in self.snapshot.features:
            geometry = feature.geometry
            if geometry is None or geometry.isNull() or geometry.isEmpty():
                plan.skipped.append(feature.order)
                continue
            if geometry.type() != wanted_type:
                raise PlanError(
                    "%s is a %s geometry but the target layer '%s' stores %s geometries. Nothing was changed."
                    % (
                        feature.label(),
                        QgsWkbTypes.geometryDisplayString(geometry.type()),
                        layer.name(),
                        QgsWkbTypes.geometryDisplayString(wanted_type),
                    )
                )
            if compat.geometry_is_curved(geometry):
                raise PlanError(
                    "%s contains curved segments. Curves are not flattened silently; segmentise the "
                    "source features first if that is acceptable." % feature.label()
                )
            geometry = QgsGeometry(geometry)
            if has_z_or_m(geometry.wkbType()):
                geometry = drop_z_m(geometry)
                dropped_zm += 1
            if transform is not None:
                try:
                    result = geometry.transform(transform)
                except QgsCsException as exc:
                    raise PlanError("Transforming %s to the layer CRS failed: %s" % (feature.label(), exc))
                if compat.enum_int(result) != 0:
                    raise PlanError("Transforming %s to the layer CRS failed." % feature.label())
                box = geometry.boundingBox()
                if not all(
                    math.isfinite(v) for v in (box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum())
                ):
                    raise PlanError("Transforming %s produced non-finite coordinates." % feature.label())
            if self.grid > 0.0:
                try:
                    grid_geometry = self.ops.snap_to_grid(geometry, self.grid)
                except GeometryOpError as exc:
                    self._issue(Issue.BLOCKING, "grid", "%s: %s" % (feature.label(), exc), category="blocking")
                    continue
                if not grid_geometry.equals(geometry):
                    snapped += 1
                geometry = grid_geometry
                if not family_parts(geometry, dim):
                    self._issue(
                        Issue.BLOCKING,
                        "grid",
                        "%s collapses on the precision grid." % feature.label(),
                        category="blocking",
                    )
                    continue
            if dim > 0 and not self.ops.is_valid(geometry):
                before = measure(geometry, dim)
                try:
                    parts, dropped = self.ops.make_valid(geometry, dim)
                except GeometryOpError as exc:
                    self._issue(
                        Issue.BLOCKING,
                        "invalid_incoming",
                        "%s is invalid and cannot be repaired safely (%s)." % (feature.label(), exc),
                        category="blocking",
                    )
                    continue
                geometry = collect(parts)
                after = measure(geometry, dim)
                self._issue(
                    Issue.ACCEPT,
                    "repaired_incoming",
                    "%s was invalid and has been repaired on a copy (makeValid): size %.6g -> %.6g, %d part(s)%s."
                    % (
                        feature.label(),
                        before,
                        after,
                        len(parts),
                        ", %d lower-dimensional piece(s) dropped" % dropped if dropped else "",
                    ),
                )
            item = _Incoming(feature, geometry)
            item.parts = family_parts(geometry, dim)
            if not item.parts:
                plan.skipped.append(feature.order)
                continue
            prepared.append(item)

        if plan.skipped:
            self._issue(
                Issue.WARNING,
                "no_geometry",
                "%d clipboard feature(s) have no geometry and are skipped: %s"
                % (len(plan.skipped), ", ".join("#%d" % (o + 1) for o in plan.skipped[:20])),
            )
        if dropped_zm:
            self._issue(
                Issue.ACCEPT,
                "dropped_zm",
                "%d incoming feature(s) carry Z/M values; the target layer is 2D, so Z/M will be dropped."
                % dropped_zm,
            )
        if snapped:
            self._issue(
                Issue.ACCEPT,
                "grid_incoming",
                "The precision grid (%g %s) changes the coordinates of %d incoming feature(s)."
                % (self.grid, self._units(), snapped),
            )
        return prepared

    # -- step 2: priority inside the batch -------------------------------------------------------------
    def _resolve_incoming_priority(self, incoming):
        """Q_i = P_i - union(P_j for j > i), computed with local unions only."""
        dim = self.dim
        index = _incoming_index([item.geometry.boundingBox() for item in incoming])
        for position, item in enumerate(incoming):
            if self.feedback.canceled():
                raise PlanCanceled()
            later = [j for j in index.intersects(item.geometry.boundingBox()) if j > position]
            if later:
                engine = self.ops.engine(item.geometry)
                try:
                    later = [j for j in sorted(later) if self.ops.interiors_intersect(engine, incoming[j].geometry, dim)]
                except GeometryOpError as exc:
                    self._issue(Issue.BLOCKING, "geometry_op", "%s: %s" % (item.feature.label(), exc), category="blocking")
                    later = []
            if not later:
                item.effective = list(item.parts)
                continue
            try:
                mask = self.ops.union([incoming[j].geometry for j in later])
                item.effective = self.ops.difference(item.geometry, mask, dim)
                item.untouched = False
            except GeometryOpError as exc:
                self._issue(
                    Issue.BLOCKING,
                    "geometry_op",
                    "Resolving the overlap of %s with later clipboard features failed: %s" % (item.feature.label(), exc),
                    category="blocking",
                )
                item.effective = []
                continue
            if not item.effective:
                # Entirely covered by later incoming features: superseded, not an error.
                self.plan.superseded.append(item.feature.order)
        if self.plan.superseded:
            self._issue(
                Issue.INFO,
                "superseded",
                "%d incoming feature(s) are completely covered by later clipboard features and produce no output: %s"
                % (len(self.plan.superseded), ", ".join("#%d" % (o + 1) for o in self.plan.superseded[:20])),
            )

    def _make_outputs(self, incoming):
        """Final incoming output geometries after destination-type adaptation."""
        layer_is_multi = QgsWkbTypes.isMultiType(self.layer.wkbType())
        outputs = []
        for position, item in enumerate(incoming):
            if not item.effective:
                continue
            parts = sorted(item.effective, key=lambda p: part_sort_key(p, self.dim))
            for k, geometry in enumerate(adapt_to_layer(parts, layer_is_multi)):
                outputs.append(
                    {
                        "key": ("out", position, k),
                        "position": position,
                        "item": item,
                        "geometry": geometry,
                        "reuse_fid": None,
                    }
                )
        return outputs

    # -- step 3: existing features ------------------------------------------------------------------------
    def _classify_candidates(self, incoming, rects, outputs):
        dim, plan = self.dim, self.plan
        affected = []
        neighbours = []
        output_index = QgsSpatialIndex()
        for i, output in enumerate(outputs):
            output_index.addFeature(i, output["geometry"].boundingBox())

        for feature, hits in iter_candidates(self.layer, rects, self.tolerance):
            if self.feedback.canceled():
                raise PlanCanceled()
            candidate = _Candidate(feature.id(), QgsGeometry(feature.geometry()), feature.attributes())
            geometry = candidate.work
            if geometry.isEmpty():
                continue
            if geometry.type() != compat.geometry_type_for_dimension(dim):
                continue
            curved = compat.geometry_is_curved(geometry) or has_z_or_m(geometry.wkbType())
            if self.grid > 0.0 and not curved:
                try:
                    candidate.work = geometry = self.ops.snap_to_grid(geometry, self.grid)
                except GeometryOpError as exc:
                    self._issue(Issue.BLOCKING, "grid", "Feature %d: %s" % (candidate.fid, exc), [candidate.fid], "blocking")
                    continue
            if dim > 0 and not self.ops.is_valid(geometry):
                try:
                    parts, _dropped = self.ops.make_valid(geometry, dim)
                except GeometryOpError as exc:
                    self._issue(
                        Issue.BLOCKING,
                        "invalid_existing",
                        "Existing feature %d near the pasted area is invalid and cannot be repaired (%s); it is "
                        "impossible to decide safely what lies underneath the paste." % (candidate.fid, exc),
                        [candidate.fid],
                        "blocking",
                    )
                    continue
                candidate.work = geometry = collect(parts)
                candidate.repaired = True
            try:
                engine = self.ops.engine(geometry)
                candidate.interacting = [
                    j for j in sorted(hits) if self.ops.interiors_intersect(engine, incoming[j].geometry, dim)
                ]
            except GeometryOpError as exc:
                self._issue(Issue.BLOCKING, "geometry_op", "Feature %d: %s" % (candidate.fid, exc), [candidate.fid], "blocking")
                continue
            if candidate.interacting:
                if curved:
                    self._issue(
                        Issue.BLOCKING,
                        "special_geometry",
                        "Existing feature %d under the pasted area has curved or Z/M geometry, which is not "
                        "modified by this plugin." % candidate.fid,
                        [candidate.fid],
                        "blocking",
                    )
                    continue
                affected.append(candidate)
                continue
            if candidate.repaired:
                self._issue(
                    Issue.WARNING,
                    "preexisting_invalid",
                    "Existing feature %d next to the pasted area is invalid; it is not underneath the paste and "
                    "is left unchanged." % candidate.fid,
                    [candidate.fid],
                    "preexisting",
                )
                continue
            if curved:
                continue
            # merely touching (or, for lines, crossing) an output -> boundary neighbour
            box = QgsRectangle(candidate.rect)
            box.grow(self.tolerance)
            for i in output_index.intersects(box):
                if engine.intersects(outputs[i]["geometry"].constGet()):
                    neighbours.append(candidate)
                    break
        affected.sort(key=lambda c: c.fid)
        neighbours.sort(key=lambda c: c.fid)
        return affected, neighbours

    @staticmethod
    def _survivor_key(candidate):
        # committed features (fid >= 0) first, then the lowest id: deterministic
        return (candidate.fid < 0, abs(candidate.fid))

    def _detect_reuse(self, outputs, affected):
        """Match FINAL output geometries against existing ones (topological equality)."""
        reuse = {}  # fid -> output dict
        if not affected:
            return reuse
        index = QgsSpatialIndex()
        for i, candidate in enumerate(affected):
            index.addFeature(i, candidate.work.boundingBox())
        slack = self.tolerance * 10.0
        for output in outputs:
            box = output["geometry"].boundingBox()
            matches = []
            for i in index.intersects(box):
                candidate = affected[i]
                other = candidate.work.boundingBox()
                # a bounding box comparison only pre-filters; equality is decided by GEOS
                if (
                    abs(other.xMinimum() - box.xMinimum()) > slack
                    or abs(other.yMinimum() - box.yMinimum()) > slack
                    or abs(other.xMaximum() - box.xMaximum()) > slack
                    or abs(other.yMaximum() - box.yMaximum()) > slack
                ):
                    continue
                if candidate.fid in reuse:
                    continue
                if self.ops.equal(candidate.work, output["geometry"]):
                    matches.append(candidate)
            if not matches:
                continue
            matches.sort(key=self._survivor_key)
            survivor = matches[0]
            reuse[survivor.fid] = output
            output["reuse_fid"] = survivor.fid
            output["candidate"] = survivor
            if len(matches) > 1:
                conflicts = []
                fields = self.layer.fields()
                for duplicate in matches[1:]:
                    differing = [
                        fields.at(i).name()
                        for i in range(min(len(duplicate.attributes), fields.count()))
                        if i not in self.layer.primaryKeyAttributes()
                        and repr(duplicate.attributes[i]) != repr(survivor.attributes[i])
                    ]
                    if differing:
                        conflicts.append("feature %d differs in %s" % (duplicate.fid, ", ".join(differing)))
                self._issue(
                    Issue.WARNING,
                    "duplicates",
                    "Existing features %s have the identical geometry as %s. Feature %d is kept, the others are "
                    "removed as covered duplicates%s."
                    % (
                        ", ".join(str(c.fid) for c in matches),
                        output["item"].feature.label(),
                        survivor.fid,
                        (" (attribute conflicts: %s)" % "; ".join(conflicts)) if conflicts else "",
                    ),
                    [c.fid for c in matches],
                )
        return reuse

    def _trim_existing(self, incoming, affected, reuse):
        """R = E - U with a local mask; returns {fid: [pieces adapted to the layer type]}."""
        dim, plan = self.dim, self.plan
        layer_is_multi = QgsWkbTypes.isMultiType(self.layer.wkbType())
        masks = {}
        remainders = {}
        for n, candidate in enumerate(affected):
            if self.feedback.canceled():
                raise PlanCanceled()
            if candidate.fid in reuse:
                change = self._change(candidate)
                change.reasons.append("reused")
                change.reused_for = reuse[candidate.fid]["item"].feature.order
                continue
            mask_key = tuple(candidate.interacting)
            try:
                if mask_key not in masks:
                    masks[mask_key] = self.ops.union([incoming[j].geometry for j in mask_key])
                pieces = self.ops.difference(candidate.work, masks[mask_key], dim)
                if pieces:
                    # independent bookkeeping: nothing of E may vanish except what U covers
                    kept = sum(measure(p, dim) for p in pieces)
                    removed = self.ops.intersection_measure(candidate.work, masks[mask_key], dim)
                    total = measure(candidate.work, dim)
                    if abs(kept + removed - total) > 1e-7 * max(total, 1e-300) + self._measure_tolerance(candidate.work):
                        raise GeometryOpError(
                            "size bookkeeping failed (kept %.9g + removed %.9g != original %.9g)" % (kept, removed, total)
                        )
            except GeometryOpError as exc:
                # A failed operation is NEVER read as "nothing remains".
                self._issue(
                    Issue.BLOCKING,
                    "geometry_op",
                    "Cutting existing feature %d failed: %s. The feature is left untouched and the paste is blocked."
                    % (candidate.fid, exc),
                    [candidate.fid],
                    "blocking",
                )
                continue
            change = self._change(candidate)
            if candidate.repaired:
                self._issue(
                    Issue.ACCEPT,
                    "repaired_existing",
                    "Existing feature %d is invalid; it was repaired on a copy (makeValid) before cutting." % candidate.fid,
                    [candidate.fid],
                )
            if not pieces:
                change.delete = True
                change.reasons.append("covered")
                continue
            pieces.sort(key=lambda p: part_sort_key(p, dim))
            adapted = adapt_to_layer(pieces, layer_is_multi)
            change.reasons.append("trimmed")
            remainders[candidate.fid] = {"candidate": candidate, "geometries": adapted}
        return remainders

    def _measure_tolerance(self, geometry):
        if self.dim == 2:
            return self.tolerance * max(geometry.length(), 1.0)
        return self.tolerance

    def _change(self, candidate):
        change = self.plan.changes.get(candidate.fid)
        if change is None:
            change = ExistingChange(candidate.fid, candidate.original, candidate.attributes)
            self.plan.changes[candidate.fid] = change
        return change

    # -- step 4: shared boundaries ------------------------------------------------------------------------------
    def _node(self, outputs, reuse, remainders, neighbours):
        dim = self.dim
        if dim == 0:
            return
        geometries = {}
        receive_all = set()
        for output in outputs:
            key = output["key"]
            geometries[key] = output["candidate"].work if output["reuse_fid"] is not None else output["geometry"]
            receive_all.add(key)
        for fid, entry in remainders.items():
            for k, geometry in enumerate(entry["geometries"]):
                geometries[("rem", fid, k)] = geometry
                receive_all.add(("rem", fid, k))
        for candidate in neighbours:
            geometries[("nb", candidate.fid)] = candidate.work

        extra_points = []
        if dim == 1:
            # Lines that merely cross share no vertex yet: compute the crossing points so that
            # both lines receive the identical node.
            keys = list(geometries.keys())
            index = QgsSpatialIndex()
            for i, key in enumerate(keys):
                index.addFeature(i, geometries[key].boundingBox())
            done = set()
            for i, key in enumerate(keys):
                if key[0] != "out":
                    continue
                for j in index.intersects(geometries[key].boundingBox()):
                    if j == i or (min(i, j), max(i, j)) in done:
                        continue
                    done.add((min(i, j), max(i, j)))
                    extra_points.extend(self.ops.intersection_points(geometries[key], geometries[keys[j]]))

        changed, failed = node_shared_boundaries(geometries, receive_all, dim, self.tolerance, extra_points, self.ops)
        for key in failed:
            self._issue(
                Issue.WARNING,
                "noding",
                "Shared-boundary vertices could not be inserted safely into %s; its vertices are left as they are."
                % self._describe_key(key),
                category="topology",
            )
        for output in outputs:
            if output["key"] in changed:
                output["geometry_noded"] = changed[output["key"]]
        for fid, entry in remainders.items():
            entry["geometries"] = [changed.get(("rem", fid, k), g) for k, g in enumerate(entry["geometries"])]
        for candidate in neighbours:
            new_geometry = changed.get(("nb", candidate.fid))
            if new_geometry is not None:
                candidate.work = new_geometry
                change = self._change(candidate)
                change.new_geometry = new_geometry
                change.reasons.append("noded")

    @staticmethod
    def _final_geometry(output):
        """Geometry an output ends up with: noded/snapped > reused existing representation > computed."""
        noded = output.get("geometry_noded")
        if noded is not None:
            return noded
        if output["reuse_fid"] is not None:
            return output["candidate"].work
        return output["geometry"]

    def _describe_key(self, key):
        if key[0] == "out":
            return "pasted feature"
        return "existing feature %s" % key[1]

    def _snap_results(self, outputs, reuse, remainders, neighbours):
        """Second half of the single, consistent precision policy: every geometry that takes
        part (pasted, trimmed, touching) ends on the same grid, including computed vertices."""
        dim = self.dim

        def snap(geometry, label, fids=None):
            try:
                snapped = self.ops.snap_to_grid(geometry, self.grid)
            except GeometryOpError as exc:
                self._issue(Issue.BLOCKING, "grid", "%s: %s" % (label, exc), fids, "blocking")
                return geometry
            if len(family_parts(snapped, dim)) != len(family_parts(geometry, dim)) or (
                dim > 0 and not self.ops.is_valid(snapped)
            ):
                self._issue(
                    Issue.BLOCKING,
                    "grid",
                    "%s degenerates on the precision grid (%g)." % (label, self.grid),
                    fids,
                    "blocking",
                )
                return geometry
            return snapped

        for output in outputs:
            output["geometry_noded"] = snap(self._final_geometry(output), output["item"].feature.label())
        for fid, entry in remainders.items():
            entry["geometries"] = [snap(g, "existing feature %d" % fid, [fid]) for g in entry["geometries"]]
        changed_neighbours = 0
        for candidate in neighbours:
            snapped = snap(candidate.work, "existing feature %d" % candidate.fid, [candidate.fid])
            if not snapped.equals(candidate.original):
                candidate.work = snapped
                change = self._change(candidate)
                change.new_geometry = snapped
                if "grid" not in change.reasons:
                    change.reasons.append("grid")
                changed_neighbours += 1
        if changed_neighbours:
            self._issue(
                Issue.ACCEPT,
                "grid_neighbours",
                "The precision grid also changes the coordinates of %d touching neighbour feature(s), so that "
                "both sides of every shared boundary are treated identically." % changed_neighbours,
            )

    # -- step 5: attributes ------------------------------------------------------------------------------------------
    def _plan_attributes(self, attribute_planner, incoming, outputs, reuse, remainders, neighbours):
        plan = self.plan
        updated = {}
        new_rows = []
        for output in outputs:
            feature = output["item"].feature
            final_geometry = output.get("geometry_noded")
            if output["reuse_fid"] is not None:
                candidate = output["candidate"]
                change = self._change(candidate)
                if final_geometry is not None and not final_geometry.equals(candidate.original):
                    # Only when noding/grid really requires it; otherwise the existing
                    # geometry representation stays exactly as it is.
                    change.new_geometry = final_geometry
                    change.reasons.append("noded")
                change.attribute_changes = attribute_planner.reuse_changes(
                    candidate.attributes, feature.attributes, feature.order
                )
                plan.reused[candidate.fid] = feature.order
                if change.attribute_changes:
                    attributes = list(candidate.attributes)
                    for index, (_old, new) in change.attribute_changes.items():
                        attributes[index] = new
                    updated[candidate.fid] = (
                        "existing feature %d (reused for %s)" % (candidate.fid, feature.label()),
                        attributes,
                        change.new_geometry if change.new_geometry is not None else candidate.original,
                    )
                continue
            geometry = final_geometry if final_geometry is not None else output["geometry"]
            attributes = attribute_planner.new_feature_attributes(feature.attributes, geometry, feature.order)
            new_feature = NewFeature(NewFeature.INCOMING, geometry, attributes, order=feature.order)
            new_feature.key = output["key"]
            plan.new_features.append(new_feature)
            new_rows.append((feature.label(), attributes, geometry))

        for fid in sorted(remainders):
            entry = remainders[fid]
            candidate = entry["candidate"]
            change = self._change(candidate)
            geometries = entry["geometries"]
            # the largest remainder (deterministic tie-break) keeps the feature id
            if not geometries[0].equals(candidate.original):
                change.new_geometry = geometries[0]
            for k, geometry in enumerate(geometries[1:], 1):
                attributes = attribute_planner.fragment_attributes(candidate.attributes)
                fragment = NewFeature(NewFeature.FRAGMENT, geometry, attributes, origin_fid=fid)
                fragment.key = ("rem", fid, k)
                plan.new_features.append(fragment)
                new_rows.append(("fragment of existing feature %d" % fid, attributes, geometry))

        # conversions: aggregated so that a big batch does not flood the preview
        grouped = {}
        for order, source, target, message, failed in attribute_planner.conversion_problems:
            grouped.setdefault((source, target, failed), []).append((order, message))
        for (source, target, failed), rows in sorted(grouped.items()):
            order, message = rows[0]
            if failed:
                text = (
                    "Field '%s' -> '%s': %d value(s) cannot be converted and would stay at the field's default/NULL "
                    "(first: incoming #%d: %s)." % (source, target, len(rows), order + 1, message)
                )
            else:
                text = "Field '%s' -> '%s': %d value(s) lose precision (first: incoming #%d: %s)." % (
                    source,
                    target,
                    len(rows),
                    order + 1,
                    message,
                )
            self._issue(Issue.ACCEPT, "conversion", text)

        mapping = attribute_planner.mapping
        if mapping.unmapped_source:
            self._issue(
                Issue.WARNING,
                "unmapped",
                "Source field(s) without a target field of the same name are not pasted: %s. "
                "Use 'Field mapping...' to map them explicitly." % ", ".join(mapping.unmapped_source),
            )
        for source, target, reason in mapping.skipped:
            self._issue(Issue.WARNING, "not_writable", "Field '%s' -> '%s' is not written: %s." % (source, target, reason))

        deleted = [fid for fid, change in plan.changes.items() if change.delete]
        plan.issues.extend(attribute_planner.check_constraints(new_rows, updated, deleted))

        plan.apply_on_update_fields = attribute_planner.apply_on_update_fields()
        if plan.apply_on_update_fields and any(
            c.new_geometry is not None or c.attribute_changes for c in plan.changes.values()
        ):
            self._issue(
                Issue.INFO,
                "apply_on_update",
                "QGIS recomputes the 'apply default value on update' field(s) %s of every modified existing feature. "
                "No other field of trimmed features is recalculated." % ", ".join(plan.apply_on_update_fields),
            )

        # drop planned changes that turned out to be no-ops (true no-op requirement)
        for fid in [fid for fid, change in plan.changes.items() if change.is_noop()]:
            del plan.changes[fid]

    # -- step 6: validation of the planned final state ------------------------------------------------------------------
    def _validate(self, incoming, outputs, reuse, remainders, neighbours, affected):
        plan, dim = self.plan, self.dim
        if plan.blocking():
            return
        final = []  # (label, geometry, is_pasted, fid)
        for output in outputs:
            final.append((output["item"].feature.label(), self._final_geometry(output), True, output["reuse_fid"]))
        for fid, entry in remainders.items():
            for geometry in entry["geometries"]:
                final.append(("existing feature %d" % fid, geometry, False, fid))
        for candidate in neighbours:
            final.append(("existing feature %d" % candidate.fid, candidate.work, False, candidate.fid))

        layer_is_multi = QgsWkbTypes.isMultiType(self.layer.wkbType())
        for label, geometry, _pasted, fid in final:
            problem = None
            if geometry is None or geometry.isNull() or geometry.isEmpty():
                problem = "is empty"
            elif dim > 0 and not self.ops.is_valid(geometry):
                problem = "is not valid"
            elif geometry.isMultipart() and not layer_is_multi:
                problem = "is multipart but the layer is singlepart"
            if problem:
                self._issue(
                    Issue.BLOCKING, "result_invalid", "Planned geometry of %s %s." % (label, problem),
                    [fid] if fid is not None else None, "blocking",
                )
        if plan.blocking():
            return
        plan.validated.append("every planned geometry is valid, non-empty and matches the layer type")
        if dim == 0:
            return

        # pairwise overlap test in the affected area only
        index = QgsSpatialIndex()
        for i, (_label, geometry, _pasted, _fid) in enumerate(final):
            index.addFeature(i, geometry.boundingBox())
        new_overlaps = 0
        old_overlaps = []
        for i, (label, geometry, pasted, fid) in enumerate(final):
            if self.feedback.canceled():
                raise PlanCanceled()
            engine = None
            for j in index.intersects(geometry.boundingBox()):
                if j <= i:
                    continue
                other_label, other, other_pasted, other_fid = final[j]
                if fid is not None and fid == other_fid:
                    continue  # pieces of the same trimmed feature
                if engine is None:
                    engine = self.ops.engine(geometry)
                if not self.ops.interiors_intersect(engine, other, dim):
                    continue
                size = self.ops.intersection_measure(geometry, other, dim)
                limit = self.tolerance * (min(geometry.length(), other.length()) if dim == 2 else 1.0)
                if size <= limit:
                    continue
                if pasted or other_pasted:
                    new_overlaps += 1
                    self._issue(
                        Issue.BLOCKING,
                        "overlap",
                        "The planned result would overlap: %s / %s (%.6g)." % (label, other_label, size),
                        category="blocking",
                    )
                else:
                    old_overlaps.append((fid, other_fid, size))
        if not new_overlaps:
            plan.validated.append(
                "no overlap between pasted features, trimmed remainders and touching neighbours "
                "(tolerance %.3g x boundary length)" % self.tolerance
            )
        for fid, other_fid, size in old_overlaps[:50]:
            self._issue(
                Issue.WARNING,
                "preexisting_overlap",
                "Existing features %s and %s already overlap outside the pasted coverage (%.6g). "
                "That overlap is not yours to be decided by this paste and is left unchanged." % (fid, other_fid, size),
                [fid, other_fid],
                "preexisting",
            )

        # coverage identity: union(result) == union(original) U union(incoming)
        try:
            before = self.ops.union([c.work for c in affected] + [item.geometry for item in incoming])
            after_parts = [g for _l, g, pasted, fid in final if pasted or fid in remainders]
            after = self.ops.union(after_parts)
            difference = before.symDifference(after)
            size = sum(measure(p, dim) for p in family_parts(difference, dim)) if not difference.isNull() else 0.0
            limit = (self.tolerance + self.grid) * (max(before.length(), 1.0) if dim == 2 else 1.0)
            if size > limit:
                self._issue(
                    Issue.BLOCKING,
                    "coverage",
                    "Coverage check failed: the planned result differs from 'existing + incoming' by %.6g "
                    "(limit %.3g)." % (size, limit),
                    category="blocking",
                )
            else:
                plan.validated.append(
                    "coverage of the affected features is preserved: union(result) = union(existing, incoming) "
                    "(difference %.3g, limit %.3g)" % (size, limit)
                )
        except GeometryOpError as exc:
            self._issue(Issue.WARNING, "coverage", "The coverage check could not be computed: %s" % exc, category="topology")

    def _check_capabilities(self):
        plan = self.plan
        provider = self.layer.dataProvider()
        needs = []
        if any(c.delete for c in plan.changes.values()):
            needs.append((compat.CAP_DELETE_FEATURES, "delete features"))
        if any(c.new_geometry is not None for c in plan.changes.values()):
            needs.append((compat.CAP_CHANGE_GEOMETRIES, "change geometries"))
        if any(c.attribute_changes for c in plan.changes.values()):
            needs.append((compat.CAP_CHANGE_ATTRIBUTES, "change attribute values"))
        for capability, text in needs:
            if not compat.provider_has_capability(provider, capability):
                self._issue(Issue.BLOCKING, "capability", "The data provider cannot %s." % text, category="blocking")


def build_plan(layer, snapshot, options=None, feedback=None, ops=None, transform_context=None):
    return Planner(layer, snapshot, options, feedback, ops, transform_context).build()
