# -*- coding: utf-8 -*-
"""Plain data objects shared by the clipboard reader, planner, applier and UI."""


class Issue(object):
    """One diagnostic of the plan.

    severity:
      INFO      - worth logging, nothing to decide
      WARNING   - shown in the preview, does not stop the paste
      ACCEPT    - material change (geometry repair, lossy conversion, coordinate
                  modification): the user has to tick an explicit acceptance box
      BLOCKING  - the paste cannot be applied safely
    category groups issues for the three-way topology report
    ("validated", "pre-existing", "blocking") and for the summary counters.
    """

    INFO = "info"
    WARNING = "warning"
    ACCEPT = "accept"
    BLOCKING = "blocking"

    def __init__(self, severity, code, message, fids=None, category="general"):
        self.severity = severity
        self.code = code
        self.message = message
        self.fids = list(fids or [])
        self.category = category

    def __repr__(self):
        return "<Issue %s %s: %s>" % (self.severity, self.code, self.message)


class IncomingFeature(object):
    """One clipboard feature. ``order`` is the deterministic incoming order (last wins)."""

    def __init__(self, order, geometry, attributes):
        self.order = order
        self.geometry = geometry  # QgsGeometry in the snapshot CRS
        self.attributes = attributes  # {source field name: value} - only fields present in the source

    def label(self):
        return "incoming #%d" % (self.order + 1)


class ClipboardSnapshot(object):
    """Immutable copy of what was on the clipboard when the action started."""

    ORIGIN_NATIVE = "QGIS feature clipboard"
    ORIGIN_GEOJSON = "GeoJSON text"
    ORIGIN_TABULAR = "QGIS tabular WKT text"
    ORIGIN_WKT = "WKT / EWKT text"

    def __init__(self, origin, crs, field_names, features, notes=None, field_types=None):
        self.origin = origin
        self.crs = crs  # QgsCoordinateReferenceSystem; invalid = unknown
        self.field_names = list(field_names)
        self.field_types = dict(field_types or {})
        self.features = list(features)
        self.notes = list(notes or [])
        self.crs_note = ""


class ExistingChange(object):
    """Planned change of one existing target feature."""

    def __init__(self, fid, original_geometry, original_attributes):
        self.fid = fid
        self.original_geometry = original_geometry
        self.original_attributes = list(original_attributes)
        self.new_geometry = None  # QgsGeometry when the geometry changes
        self.delete = False
        self.attribute_changes = {}  # index -> (old, new)
        self.reasons = []  # 'trimmed', 'covered', 'reused', 'noded', 'duplicate', 'grid'
        self.reused_for = None  # incoming order when reused

    def is_noop(self):
        return not self.delete and self.new_geometry is None and not self.attribute_changes


class NewFeature(object):
    """Planned insertion."""

    INCOMING = "incoming"
    FRAGMENT = "fragment"

    def __init__(self, kind, geometry, attributes, order=None, origin_fid=None):
        self.kind = kind
        self.geometry = geometry
        self.attributes = attributes
        self.order = order  # incoming order for INCOMING
        self.origin_fid = origin_fid  # existing fid for FRAGMENT
        self.key = None  # internal key used during noding / validation


class PlanOptions(object):
    def __init__(self, grid_size=0.0, mapping_override=None):
        self.grid_size = float(grid_size or 0.0)  # 0 = coordinates are preserved
        self.mapping_override = mapping_override  # {source field: target field name or None}


class Plan(object):
    """Explicit change plan. Building it never touches the target layer."""

    def __init__(self, layer_id, layer_name):
        self.layer_id = layer_id
        self.layer_name = layer_name
        self.options = None
        self.snapshot = None
        self.dim = None
        self.changes = {}  # fid -> ExistingChange (no-ops removed)
        self.new_features = []  # [NewFeature]
        self.superseded = []  # incoming orders fully covered by later incoming features
        self.skipped = []  # incoming orders without usable geometry
        self.reused = {}  # fid -> incoming order
        self.issues = []
        self.mapping = None  # attributes.FieldMapping
        self.apply_on_update_fields = []
        self.fingerprint = None  # stale detection data
        self.affected_rect = None
        self.grid_size = 0.0
        self.validated = []  # human-readable list of checks that passed in the affected area
        self.tolerance = 0.0

    # -- derived numbers for the preview ------------------------------------------------
    def count_incoming(self):
        return len(self.snapshot.features) if self.snapshot else 0

    def count_new_outputs(self):
        return sum(1 for f in self.new_features if f.kind == NewFeature.INCOMING)

    def count_fragments(self):
        return sum(1 for f in self.new_features if f.kind == NewFeature.FRAGMENT)

    def count_trimmed(self):
        return sum(1 for c in self.changes.values() if "trimmed" in c.reasons and not c.delete)

    def count_deleted(self):
        return sum(1 for c in self.changes.values() if c.delete)

    def count_reused(self):
        return len(self.reused)

    def count_attribute_updates(self):
        return sum(1 for c in self.changes.values() if c.attribute_changes)

    def count_noded_only(self):
        return sum(
            1
            for c in self.changes.values()
            if c.new_geometry is not None and not c.delete and "trimmed" not in c.reasons
        )

    def blocking(self):
        return [i for i in self.issues if i.severity == Issue.BLOCKING]

    def needs_acceptance(self):
        return [i for i in self.issues if i.severity == Issue.ACCEPT]

    def is_noop(self):
        return not self.changes and not self.new_features

    def modifies_existing(self):
        return bool(self.changes)


class PlanError(Exception):
    """The paste was rejected before any plan could be built (nothing was changed)."""


class ApplyError(Exception):
    """Applying failed; the edit command was destroyed and the layer is as before."""


class StalePlanError(ApplyError):
    """The layer changed between planning and applying."""
