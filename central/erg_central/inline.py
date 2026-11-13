"""Reconstruct inline records before their final item MERGE, within existing bounds."""

from erg import delivery_protocol as wire

from .receiver import Conflict


def additions(items, present):
    # Envelope/item validation precedes this selection in cohort_values. Existing
    # sequences remain in the queried group and are never appended twice on replay.
    return [i for i in items if "payload_json" in i and i["delivery_seq"] not in present]


def check_bounds(bounds, pending):
    counts = {(r["record_id"], r["record_revision"]): r["n"] for r in bounds}
    total = sum(r["bytes"] for r in bounds)
    for item in pending:
        key = item["record_id"], item["record_revision"]
        counts[key] = counts.get(key, 0) + 1
        total += len(wire.encode(item))
    if any(n > 128 for n in counts.values()) or total > 192 * 1024 * 1024:
        raise Conflict("bounded reconstruction manifest exceeded")
