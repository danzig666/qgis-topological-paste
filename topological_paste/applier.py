# -*- coding: utf-8 -*-
"""Phase B - apply a plan as ONE undoable edit command.

Safety rules (design section 9):

* the plan is re-verified against the live layer first (schema, edit state,
  undo stack position, every feature in the affected boxes *including features
  that appeared since planning*); a stale plan is refused, never "adapted";
* all edits go through the layer's edit buffer inside a single
  beginEditCommand()/endEditCommand() pair, so one Undo reverts everything and
  Redo reapplies it;
* every edit call is checked; on any failure or exception the command is
  removed with destroyEditCommand(), which undoes exactly the partial edits of
  this command and nothing else - pre-existing unsaved edits stay intact.
  rollBack() is only used to leave an edit session that this very operation
  opened and that contains no modification at all;
* the result is read back and compared with the plan *before* the command is
  closed, so "success" is only reported for a verified state;
* commitChanges() is never called and the provider is never written directly.
"""

from qgis.core import QgsFeature, QgsProject, QgsVectorLayer

from . import compat
from .attributes import values_equal
from .model import ApplyError, NewFeature, StalePlanError
from .planner import check_layer_preconditions, compute_fingerprint


class ApplyResult(object):
    def __init__(self):
        self.new_fids = []  # ids of inserted incoming features
        self.fragment_fids = []
        self.reused_fids = []
        self.started_editing = False

    def output_fids(self):
        return list(self.new_fids) + list(self.reused_fids)


def find_stale_reasons(layer, plan):
    """Compare the live layer with the state the plan was computed from."""
    reasons = []
    fingerprint = plan.fingerprint
    current = compute_fingerprint(layer, fingerprint["rects"], fingerprint["tolerance"])
    if current["fields"] != fingerprint["fields"]:
        reasons.append("the layer's fields changed")
    if current["crs"] != fingerprint["crs"]:
        reasons.append("the layer's CRS changed")
    if current["subset"] != fingerprint["subset"]:
        reasons.append("the layer's filter changed")
    if current["editable"] != fingerprint["editable"]:
        reasons.append("the layer's edit state changed")
    elif current["undo_index"] != fingerprint["undo_index"]:
        reasons.append("the layer was edited (undo stack moved)")
    before, after = fingerprint["features"], current["features"]
    appeared = sorted(set(after) - set(before))
    vanished = sorted(set(before) - set(after))
    modified = sorted(fid for fid in set(before) & set(after) if before[fid] != after[fid])
    if appeared:
        reasons.append("new feature(s) in the affected area: %s" % appeared[:10])
    if vanished:
        reasons.append("feature(s) removed from the affected area: %s" % vanished[:10])
    if modified:
        reasons.append("feature(s) modified in the affected area: %s" % modified[:10])
    return reasons


def apply_plan(layer, plan, fault_hook=None):
    """Apply ``plan`` to ``layer``. Returns ApplyResult or raises ApplyError/StalePlanError.

    ``fault_hook(step_number)`` is called before every edit call; tests use it to
    force a failure in the middle of the command.
    """
    if layer is None or not isinstance(layer, QgsVectorLayer) or layer.id() != plan.layer_id:
        raise StalePlanError("The target layer is no longer available.")
    if QgsProject.instance().mapLayer(plan.layer_id) is None and layer.id() != plan.layer_id:
        raise StalePlanError("The target layer was removed from the project.")
    if plan.blocking():
        raise ApplyError("The plan has blocking issues and cannot be applied.")
    try:
        check_layer_preconditions(layer)
    except Exception as exc:
        raise StalePlanError(str(exc))
    reasons = find_stale_reasons(layer, plan)
    if reasons:
        raise StalePlanError("The layer changed after the preview was computed: " + "; ".join(reasons))

    result = ApplyResult()
    if plan.is_noop():
        result.reused_fids = sorted(plan.reused.keys())
        return result

    if not layer.isEditable():
        if not layer.startEditing():
            raise ApplyError("Editing could not be started on the target layer.")
        result.started_editing = True
    if layer.isEditCommandActive():
        raise ApplyError("Another edit command is active on the target layer.")

    step = [0]

    def guard():
        step[0] += 1
        if fault_hook is not None:
            fault_hook(step[0])

    selection_before = list(layer.selectedFeatureIds())
    layer.beginEditCommand("Topological Paste")
    try:
        deletions = sorted(fid for fid, change in plan.changes.items() if change.delete)
        for fid in deletions:
            guard()
            if not layer.deleteFeature(fid):
                raise ApplyError("Deleting feature %d failed." % fid)

        for fid in sorted(plan.changes):
            change = plan.changes[fid]
            if change.delete:
                continue
            if change.new_geometry is not None:
                guard()
                if not layer.changeGeometry(fid, change.new_geometry):
                    raise ApplyError("Changing the geometry of feature %d failed." % fid)
            for index in sorted(change.attribute_changes):
                old_value, new_value = change.attribute_changes[index]
                guard()
                if not layer.changeAttributeValue(fid, index, new_value, old_value):
                    raise ApplyError(
                        "Changing attribute '%s' of feature %d failed." % (layer.fields().at(index).name(), fid)
                    )

        added = []
        for new_feature in plan.new_features:
            guard()
            feature = QgsFeature(layer.fields())
            feature.setGeometry(new_feature.geometry)
            feature.setAttributes(list(new_feature.attributes))
            if not layer.addFeature(feature):
                raise ApplyError("Adding a feature failed (%s)." % _describe(new_feature))
            added.append((new_feature, feature.id()))
            if new_feature.kind == NewFeature.INCOMING:
                result.new_fids.append(feature.id())
            else:
                result.fragment_fids.append(feature.id())

        guard()
        _verify(layer, plan, deletions, added)
    except BaseException as exc:
        # Removes this command and undoes its partial edits; earlier edits are untouched.
        layer.destroyEditCommand()
        layer.selectByIds(selection_before)  # deleting deselects; undoing does not reselect
        if result.started_editing and not layer.isModified():
            # The edit session was opened by this operation and is empty again:
            # closing it discards nothing. Never used when the user had edits.
            layer.rollBack()
        if isinstance(exc, ApplyError):
            raise
        if isinstance(exc, Exception):
            raise ApplyError("Unexpected error while applying: %s" % exc)
        raise
    layer.endEditCommand()
    result.reused_fids = sorted(plan.reused.keys())
    return result


def _describe(new_feature):
    if new_feature.kind == NewFeature.INCOMING:
        return "incoming #%d" % (new_feature.order + 1)
    return "fragment of feature %s" % new_feature.origin_fid


def _geometry_matches(actual, planned):
    if actual is None or actual.isNull():
        return False
    if actual.equals(planned):
        return True
    try:
        return compat.topologically_equal(actual, planned)
    except Exception:
        return False


def _verify(layer, plan, deletions, added):
    """Read the edited state back through the layer and compare it with the plan."""
    volatile = set(layer.fields().lookupField(name) for name in plan.apply_on_update_fields)
    for fid in deletions:
        if layer.getFeature(fid).isValid():
            raise ApplyError("Verification failed: feature %d still exists." % fid)
    for fid, change in plan.changes.items():
        if change.delete:
            continue
        feature = layer.getFeature(fid)
        if not feature.isValid():
            raise ApplyError("Verification failed: feature %d disappeared." % fid)
        if change.new_geometry is not None and not _geometry_matches(feature.geometry(), change.new_geometry):
            raise ApplyError("Verification failed: geometry of feature %d differs from the plan." % fid)
        attributes = feature.attributes()
        for index, (_old, new_value) in change.attribute_changes.items():
            if index in volatile:
                continue
            if not values_equal(attributes[index], new_value):
                raise ApplyError(
                    "Verification failed: attribute '%s' of feature %d differs from the plan."
                    % (layer.fields().at(index).name(), fid)
                )
    managed = set(layer.primaryKeyAttributes())
    fields = layer.fields()
    for index in range(fields.count()):
        # joined / virtual values are computed by QGIS, not written by the plan
        if fields.fieldOrigin(index) in (compat.ORIGIN_JOIN, compat.ORIGIN_EXPRESSION):
            managed.add(index)
    for new_feature, fid in added:
        feature = layer.getFeature(fid)
        if not feature.isValid():
            raise ApplyError("Verification failed: an added feature is missing (%s)." % _describe(new_feature))
        if not _geometry_matches(feature.geometry(), new_feature.geometry):
            raise ApplyError("Verification failed: geometry of %s differs from the plan." % _describe(new_feature))
        attributes = feature.attributes()
        for index, planned in enumerate(new_feature.attributes):
            if index in volatile or index in managed:
                continue
            if not values_equal(attributes[index], planned):
                raise ApplyError(
                    "Verification failed: attribute '%s' of %s differs from the plan."
                    % (layer.fields().at(index).name(), _describe(new_feature))
                )
