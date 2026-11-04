# -*- coding: utf-8 -*-
"""Checked geometry operations used by the planner.

Design rules implemented here:

* Every GEOS operation is *checked*. A null result, an exception or a result
  that contradicts an independent predicate raises :class:`GeometryOpError`.
  The planner never treats a failed operation as "nothing remains, delete it".
* An empty difference is only accepted as legitimate when an independent
  DE-9IM ``relate`` test confirms that the subtracted mask really covers the
  input (QGIS returns a *null* geometry both for some legitimate empty results
  and for failed operations, so the result alone is ambiguous).
* Coordinates are never snapped, buffered or rounded here. The only tolerance
  is ``noding_tolerance`` which decides whether an *existing* vertex of one
  geometry lies on a segment of another one; matching vertices are inserted,
  never moved.

The operations are methods of :class:`GeometryOps` so tests can subclass it to
force failures at precise points.
"""

import math

from qgis.core import QgsGeometry, QgsPointXY, QgsRectangle, QgsSpatialIndex, QgsWkbTypes

from . import compat


class GeometryOpError(Exception):
    """A geometry operation failed or produced an untrustworthy result."""


# DE-9IM patterns: "interiors intersect with the dimension of the family".
# Polygons sharing only an edge/vertex -> no '2' -> not an overlap.
# Lines crossing at a point -> interior/interior is '0', not '1' -> no removal.
_INTERIOR_PATTERN = {0: "0********", 1: "1********", 2: "2********"}


def measure(geometry, dim):
    """Area / length / part count: the natural size of a geometry of family ``dim``."""
    if geometry is None or geometry.isNull() or geometry.isEmpty():
        return 0.0
    if dim == 2:
        return geometry.area()
    if dim == 1:
        return geometry.length()
    return float(len(single_parts(geometry)))


def single_parts(geometry):
    """Flatten a geometry into singlepart QgsGeometry objects (recursing into collections)."""
    if geometry is None or geometry.isNull() or geometry.isEmpty():
        return []
    if not geometry.isMultipart():
        return [QgsGeometry(geometry)]
    parts = []
    for part in geometry.asGeometryCollection():
        if part.isNull() or part.isEmpty():
            continue
        if part.isMultipart():
            parts.extend(single_parts(part))
        else:
            parts.append(part)
    return parts


def family_parts(geometry, dim):
    """Singlepart components of ``geometry`` that belong to the family ``dim``.

    Lower-dimensional debris of an overlay (a line where two polygons touched)
    has zero measure in the family and is legitimately not part of the result.
    """
    wanted = compat.geometry_type_for_dimension(dim)
    return [p for p in single_parts(geometry) if p.type() == wanted]


def collect(parts):
    """Build one (multi)geometry from singlepart geometries, keeping coordinates untouched."""
    if not parts:
        return QgsGeometry()
    if len(parts) == 1:
        return QgsGeometry(parts[0])
    return QgsGeometry.collectGeometry(parts)


def part_sort_key(part, dim):
    """Deterministic ordering: largest first, then lower-left bbox corner, then WKB."""
    box = part.boundingBox()
    return (-measure(part, dim), box.xMinimum(), box.yMinimum(), bytes(part.asWkb()))


def adapt_to_layer(parts, layer_is_multi):
    """Destination-type adaptation of a result given as singlepart geometries.

    Multipart-capable target: all parts stay together in one multipart geometry.
    Strictly singlepart target: one geometry per part - nothing is discarded.
    """
    if not parts:
        return []
    if layer_is_multi:
        geometry = collect(parts)
        if not geometry.isMultipart():
            geometry.convertToMultiType()
        return [geometry]
    result = []
    for part in parts:
        single = QgsGeometry(part)
        if single.isMultipart():
            single.convertToSingleType()
        result.append(single)
    return result


def coordinate_magnitude(rect):
    values = [abs(rect.xMinimum()), abs(rect.xMaximum()), abs(rect.yMinimum()), abs(rect.yMaximum())]
    values = [v for v in values if math.isfinite(v)]
    return max(values) if values else 1.0


def noding_tolerance(rect):
    """Numerical tolerance for 'this vertex lies on that segment'.

    ~4000 ulp of the largest coordinate: far below any real-world size
    (0.6 micrometre at 650 km eastings, 5e-11 degrees for lon/lat) but above
    the rounding noise of a computed intersection point. It is NOT a snapping
    distance: vertices within it are inserted into the other boundary at their
    own coordinates; nothing is ever moved or removed because of it.
    """
    return max(coordinate_magnitude(rect), 1.0) * 1e-12


class GeometryOps(object):
    """Checked GEOS operations. Subclass in tests to inject failures."""

    # -- predicates -------------------------------------------------------------
    def engine(self, geometry):
        engine = QgsGeometry.createGeometryEngine(geometry.constGet())
        engine.prepareGeometry()
        return engine

    def relate(self, a, b):
        try:
            matrix = self.engine(a).relate(b.constGet())
        except Exception as exc:  # pragma: no cover - depends on GEOS build
            raise GeometryOpError("relate failed: %s" % exc)
        if isinstance(matrix, (tuple, list)):
            matrix = matrix[0]
        if not matrix or len(matrix) != 9:
            raise GeometryOpError("relate failed (no DE-9IM matrix returned)")
        return matrix

    def interiors_intersect(self, engine, other, dim):
        """True when ``other`` shares a ``dim``-dimensional portion with the engine geometry."""
        try:
            result = engine.relatePattern(other.constGet(), _INTERIOR_PATTERN[dim])
        except Exception as exc:  # pragma: no cover
            raise GeometryOpError("relatePattern failed: %s" % exc)
        if isinstance(result, (tuple, list)):
            result = result[0]
        return bool(result)

    def is_covered_by(self, geometry, mask):
        """geometry is covered by mask: neither its interior nor its boundary meets mask's exterior."""
        matrix = self.relate(geometry, mask)
        return matrix[2] == "F" and matrix[5] == "F"

    def is_valid(self, geometry):
        return bool(geometry.isGeosValid())

    def equal(self, a, b):
        try:
            return compat.topologically_equal(a, b)
        except Exception:
            return False

    # -- constructive operations ----------------------------------------------------
    def union(self, geometries):
        geometries = [g for g in geometries if g is not None and not g.isNull() and not g.isEmpty()]
        if not geometries:
            return QgsGeometry()
        if len(geometries) == 1:
            return QgsGeometry(geometries[0])
        result = QgsGeometry.unaryUnion(geometries)
        if result is None or result.isNull() or result.isEmpty():
            raise GeometryOpError("union failed: %s" % (result.lastError() if result is not None else "null"))
        return result

    def difference(self, geometry, mask, dim):
        """Checked ``geometry - mask`` restricted to the family ``dim``.

        Returns a list of singlepart geometries; an empty list means the mask
        legitimately covers the whole geometry. Raises GeometryOpError for
        anything that is not provably correct.
        """
        if self.is_covered_by(geometry, mask):
            return []
        result = self._raw_difference(geometry, mask)
        if result is None or result.isNull():
            raise GeometryOpError(
                "difference returned no geometry although the input is not fully covered (%s)"
                % ((result.lastError() if result is not None else "") or "no GEOS message")
            )
        parts = family_parts(result, dim)
        if not parts:
            # GEOS returned a real (non-null) result with nothing of the family left: empty, or only
            # lower-dimensional debris. That happens when the mask covers the input up to rounding
            # noise - a boundary vertex lies a few ulp outside the mask, so relate() reports "not
            # covered" although no area/length remains. Accept it as a full cover ONLY when an
            # independent intersection confirms that the mask holds the entire measure of the input.
            if self._covered_up_to_noise(geometry, mask, dim):
                return []
            raise GeometryOpError("difference lost every part although the input is not fully covered")
        for part in parts:
            if not self.is_valid(part):
                raise GeometryOpError("difference produced an invalid geometry")
        if dim == 1:
            parts = self._merge_line_parts(parts, geometry)
        return parts

    def _raw_difference(self, geometry, mask):
        return geometry.difference(mask)

    def _covered_up_to_noise(self, geometry, mask, dim):
        """Independent check: does ``geometry ∩ mask`` hold the whole measure of ``geometry``?

        The slack is a numerical noise bound (noding tolerance times the perimeter for
        polygons), not a cleanup size: a remainder of any real width still fails it.
        """
        total = measure(geometry, dim)
        if total <= 0.0:
            return False
        inside = self.intersection_measure(geometry, mask, dim)
        if dim == 0:
            return inside >= total
        tolerance = noding_tolerance(geometry.boundingBox())
        if dim == 2:
            # QgsGeometry.length() is 0 for polygons since QGIS 3.0; use the perimeter
            slack = tolerance * max(geometry.constGet().perimeter(), 1.0)
        else:
            slack = tolerance
        return abs(total - inside) <= slack + 1e-12 * total

    def intersection_measure(self, a, b, dim):
        result = a.intersection(b)
        if result is None or result.isNull():
            return 0.0
        return sum(measure(p, dim) for p in family_parts(result, dim))

    def intersection_points(self, a, b):
        """0-dimensional components of a ∩ b (line crossings) as (x, y) tuples."""
        result = a.intersection(b)
        points = []
        if result is None or result.isNull():
            return points
        for part in family_parts(result, 0):
            p = part.asPoint()
            points.append((p.x(), p.y()))
        return points

    def _merge_line_parts(self, parts, original):
        """Re-join pieces that GEOS split only because of noding.

        A difference of lines returns a separate piece for every node, even for
        a mere crossing. mergeLines() joins them again (vertices are kept), so a
        strictly singlepart target is not shattered into needless features.
        GEOS may reverse a merged line; the original digitising direction is
        restored by comparing positions along the original line.
        """
        merged = collect(parts).mergeLines()
        if merged is None or merged.isNull():
            return parts
        merged_parts = family_parts(merged, 1)
        if not merged_parts:
            return parts
        if abs(sum(p.length() for p in merged_parts) - sum(p.length() for p in parts)) > 1e-9 * max(
            1.0, original.length()
        ):
            return parts
        result = []
        for part in merged_parts:
            line = part.asPolyline()
            if len(line) >= 2:
                start = original.lineLocatePoint(QgsGeometry.fromPointXY(line[0]))
                end = original.lineLocatePoint(QgsGeometry.fromPointXY(line[-1]))
                if start >= 0 and end >= 0 and end < start:
                    part = QgsGeometry.fromPolylineXY(list(reversed(line)))
            result.append(part)
        return result

    # -- repair -------------------------------------------------------------------------
    def make_valid(self, geometry, dim):
        """makeValid on a copy. Returns (parts, note) or raises GeometryOpError.

        All parts of the family are preserved. A repair that leaves nothing of
        the family (dimensional collapse) is a failure, not an empty result.
        """
        try:
            fixed = compat.make_valid(geometry)
        except Exception as exc:
            raise GeometryOpError("makeValid raised: %s" % exc)
        if fixed is None or fixed.isNull() or fixed.isEmpty():
            raise GeometryOpError("makeValid returned nothing")
        parts = family_parts(fixed, dim)
        if not parts:
            raise GeometryOpError("repair collapsed the geometry to a lower dimension")
        for part in parts:
            if not self.is_valid(part):
                raise GeometryOpError("geometry is still invalid after makeValid")
        dropped = len(single_parts(fixed)) - len(parts)
        return parts, dropped

    def snap_to_grid(self, geometry, grid):
        snapped = geometry.snappedToGrid(grid, grid)
        if snapped is None or snapped.isNull():
            raise GeometryOpError("snapping to the precision grid failed")
        return snapped


# ---------------------------------------------------------------------------------------
# Shared-boundary noding
# ---------------------------------------------------------------------------------------
def _to_paths(geometry, dim):
    """Geometry -> [part][path][(x, y)] plus a rebuild recipe. Polygon rings stay closed."""
    multi = geometry.isMultipart()
    if dim == 2:
        polygons = geometry.asMultiPolygon() if multi else [geometry.asPolygon()]
        return [[[(p.x(), p.y()) for p in ring] for ring in polygon] for polygon in polygons], multi
    lines = geometry.asMultiPolyline() if multi else [geometry.asPolyline()]
    return [[[(p.x(), p.y()) for p in line]] for line in lines], multi


def _from_paths(parts, dim, multi):
    if dim == 2:
        polygons = [[[QgsPointXY(x, y) for x, y in ring] for ring in polygon] for polygon in parts]
        return QgsGeometry.fromMultiPolygonXY(polygons) if multi else QgsGeometry.fromPolygonXY(polygons[0])
    lines = [[QgsPointXY(x, y) for x, y in part[0]] for part in parts]
    return QgsGeometry.fromMultiPolylineXY(lines) if multi else QgsGeometry.fromPolylineXY(lines[0])


def node_shared_boundaries(geometries, receive_all, dim, tolerance, extra_points=(), ops=None):
    """Insert vertices so that touching boundaries carry identical nodes.

    ``geometries``  dict key -> QgsGeometry (2D, linear, family ``dim``).
    ``receive_all`` keys whose vertices may come from every other geometry
                    (the pasted outputs and trimmed remainders). Every other key
                    is a merely *touching neighbour*: it only receives vertices
                    of ``receive_all`` geometries, so the operation cannot turn
                    into a clean-up of boundaries that the paste never touched.
    ``extra_points`` computed intersection points (line crossings) that must
                    become vertices of every geometry passing through them.

    A vertex is inserted into a segment when it lies within ``tolerance`` of the
    segment but is not one of its end points. Vertices are inserted at their own
    coordinates; existing vertices never move, so the boundary itself is not
    relocated. Returns (dict key -> new geometry for changed keys, warnings).
    """
    if dim not in (1, 2) or not geometries:
        return {}, []
    ops = ops or GeometryOps()
    tol2 = tolerance * tolerance
    paths = {}
    multis = {}
    for key, geometry in geometries.items():
        paths[key], multis[key] = _to_paths(geometry, dim)

    segments = []  # (key, part, path, index, x1, y1, x2, y2)
    index = QgsSpatialIndex()
    for key, parts in paths.items():
        for pi, part in enumerate(parts):
            for ri, path in enumerate(part):
                for si in range(len(path) - 1):
                    x1, y1 = path[si]
                    x2, y2 = path[si + 1]
                    rect = QgsRectangle(
                        min(x1, x2) - tolerance, min(y1, y2) - tolerance, max(x1, x2) + tolerance, max(y1, y2) + tolerance
                    )
                    index.addFeature(len(segments), rect)
                    segments.append((key, pi, ri, si, x1, y1, x2, y2))

    # source points: (x, y) -> set of owning keys (None = computed intersection point)
    sources = {}
    for key, parts in paths.items():
        for part in parts:
            for path in part:
                for xy in path:
                    sources.setdefault(xy, set()).add(key)
    for xy in extra_points:
        sources.setdefault((xy[0], xy[1]), set()).add(None)

    insertions = {}  # (key, part, path, segment) -> {(x, y): t}
    for (x, y), owners in sources.items():
        rect = QgsRectangle(x - tolerance, y - tolerance, x + tolerance, y + tolerance)
        for seg_id in index.intersects(rect):
            key, pi, ri, si, x1, y1, x2, y2 = segments[seg_id]
            if key not in receive_all:
                # touching neighbour: accept only vertices of the pasted / trimmed set
                if not any(o is None or o in receive_all for o in owners):
                    continue
            if owners == {key}:
                continue  # a geometry does not node itself
            if (x, y) == (x1, y1) or (x, y) == (x2, y2):
                continue
            dx, dy = x2 - x1, y2 - y1
            length2 = dx * dx + dy * dy
            if length2 == 0.0:
                continue
            t = ((x - x1) * dx + (y - y1) * dy) / length2
            if t <= 0.0 or t >= 1.0:
                continue
            px, py = x1 + t * dx, y1 + t * dy
            if (x - px) ** 2 + (y - py) ** 2 > tol2:
                continue
            # Almost-but-not-exactly an end point: leave it alone (no snapping).
            if (x - x1) ** 2 + (y - y1) ** 2 <= tol2 or (x - x2) ** 2 + (y - y2) ** 2 <= tol2:
                continue
            insertions.setdefault((key, pi, ri, si), {})[(x, y)] = t

    changed_keys = {}
    for (key, pi, ri, si), points in insertions.items():
        changed_keys.setdefault(key, []).append((pi, ri, si, points))

    result = {}
    warnings = []
    for key, items in changed_keys.items():
        new_parts = [[list(path) for path in part] for part in paths[key]]
        # insert from the last segment backwards so earlier indices stay valid
        for pi, ri, si, points in sorted(items, key=lambda item: (item[0], item[1], -item[2])):
            ordered = sorted(points.items(), key=lambda kv: (kv[1], kv[0]))
            accepted = []
            for xy, _t in ordered:
                if accepted and (xy[0] - accepted[-1][0]) ** 2 + (xy[1] - accepted[-1][1]) ** 2 <= tol2:
                    continue  # two distinct sources within tolerance: one node is enough
                accepted.append(xy)
            new_parts[pi][ri][si + 1 : si + 1] = accepted
        candidate = _from_paths(new_parts, dim, multis[key])
        original = geometries[key]
        size = max(measure(original, dim), 1e-300)
        if (
            candidate.isNull()
            or not ops.is_valid(candidate)
            or abs(measure(candidate, dim) - measure(original, dim)) > 1e-9 * size + tolerance * tolerance
        ):
            warnings.append(key)
            continue
        result[key] = candidate
    return result, warnings


def has_z_or_m(wkb_type):
    return QgsWkbTypes.hasZ(wkb_type) or QgsWkbTypes.hasM(wkb_type)


def drop_z_m(geometry):
    """2D copy of a geometry (used for incoming data pasted into a 2D layer; always reported)."""
    copy = QgsGeometry(geometry)
    abstract = copy.get()
    if abstract is not None:
        abstract.dropZValue()
        abstract.dropMValue()
    return copy
