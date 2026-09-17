# -*- coding: utf-8 -*-
"""Human-readable summaries of a plan (used by the preview dialog and the message log)."""

from .model import Issue


def source_line(plan):
    snapshot = plan.snapshot
    crs = snapshot.crs.authid() or snapshot.crs.description() or "custom CRS"
    if snapshot.crs_note:
        crs += " (%s)" % snapshot.crs_note
    return "%s, %d feature(s), CRS %s" % (snapshot.origin, len(snapshot.features), crs)


def summary_rows(plan):
    """[(label, value)] for the compact preview table."""
    rows = [
        ("Incoming clipboard features", plan.count_incoming()),
        ("New features to insert", plan.count_new_outputs()),
        ("Existing features trimmed", plan.count_trimmed()),
        ("Existing features deleted (fully covered)", plan.count_deleted()),
        ("Existing features reused (identical geometry)", plan.count_reused()),
        ("  of which attributes updated", plan.count_attribute_updates()),
        ("Additional fragments (singlepart split)", plan.count_fragments()),
        ("Neighbours receiving boundary vertices only", plan.count_noded_only()),
        ("Superseded incoming features", len(plan.superseded)),
        ("Skipped incoming features (no geometry)", len(plan.skipped)),
    ]
    return rows


def _section(title, lines):
    if not lines:
        return []
    return [title] + ["  - " + line for line in lines] + [""]


def details_text(plan):
    lines = []
    lines += _section("BLOCKING - the paste cannot be applied:", [i.message for i in plan.blocking()])
    lines += _section(
        "NEEDS YOUR ACCEPTANCE (repairs, lossy conversions, coordinate changes):",
        [i.message for i in plan.needs_acceptance()],
    )
    warnings = [i for i in plan.issues if i.severity == Issue.WARNING and i.category != "preexisting"]
    lines += _section("Warnings:", [i.message for i in warnings])
    lines += _section("Topology validated in the affected area (not the whole layer):", plan.validated)
    lines += _section(
        "Pre-existing issues left unchanged:",
        [i.message for i in plan.issues if i.category == "preexisting"],
    )
    if len(plan.snapshot.features) > 1:
        lines += _section(
            "Incoming priority:",
            ["clipboard order is used; where incoming features overlap each other, the LAST one wins (#%d is last)."
             % len(plan.snapshot.features)],
        )
    mapping = plan.mapping
    if mapping is not None:
        mapped = ["%s -> %s" % (s, t.name) if s != t.name else s for s, t in mapping.pairs]
        lines += _section("Field mapping (by name):", [", ".join(mapped) if mapped else "no field is mapped"])
    lines += _section("Notes:", list(plan.snapshot.notes) + [i.message for i in plan.issues if i.severity == Issue.INFO])
    if plan.grid_size > 0:
        lines += _section("Precision grid:", ["%g (layer units) - coordinates are modified" % plan.grid_size])
    return "\n".join(lines).rstrip() + "\n"


def result_message(plan, result):
    parts = []
    if result.new_fids:
        parts.append("%d pasted" % len(result.new_fids))
    if result.reused_fids:
        parts.append("%d reused" % len(result.reused_fids))
    if plan.count_trimmed():
        parts.append("%d trimmed" % plan.count_trimmed())
    if plan.count_deleted():
        parts.append("%d deleted" % plan.count_deleted())
    if result.fragment_fids:
        parts.append("%d fragment(s) added" % len(result.fragment_fids))
    if plan.count_noded_only():
        parts.append("%d neighbour(s) noded" % plan.count_noded_only())
    return ", ".join(parts) if parts else "nothing changed"
