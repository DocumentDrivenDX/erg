"""Pure bounded reconstruction for a UMF/LDP worker; no I/O or receipt publication.

The caller must group within one qualified enrollment/generation, deduplicate by
sequence before collecting, and fail the publication barrier on any conflict.
Spark adapters and the native UMF compiler own the execution graph.
"""

from erg import delivery_protocol as wire

from .disposition import raw_disposition
from .receiver import Conflict

MAX_GROUP_ITEMS = 128
MAX_GROUP_BYTES = 96 * 1024 * 1024


def checked_item(row, identity):
    """Bind every outer raw-table field to validated canonical wire bytes."""
    if any(row.get(k) != identity[k] for k in wire.ENROLLMENT):
        raise Conflict("raw enrollment mismatch")
    encoded = row.get("item_json")
    if not isinstance(encoded, str) or len(encoded.encode()) > wire.MAX_BATCH:
        raise Conflict("raw item exceeds bound")
    try:
        item = wire.validate_item(wire.decode(encoded.encode()))
    except (wire.ProtocolError, UnicodeError):
        raise Conflict("invalid raw item") from None
    if wire.encode(item).decode() != encoded:
        raise Conflict("noncanonical raw item encoding")
    if any(row.get(k) != item[k] for k in wire.IDENTITY + ("delivery_seq", "record_sha256")):
        raise Conflict("raw envelope item mismatch")
    return item


def unique_items(rows, identity):
    """Finite qualification/coordinator helper, not an unbounded Spark collect."""
    items = {}
    total = 0
    for row in rows:
        item = checked_item(row, identity)
        seq = item["delivery_seq"]
        if seq in items and items[seq] != item:
            raise Conflict("immutable sequence conflict")
        if seq not in items:
            total += len(row["item_json"].encode())
            if len(items) >= 8000 or total > 192 * 1024 * 1024:
                raise Conflict("qualification window exceeds bound")
            items[seq] = item
    return [items[k] for k in sorted(items)]


def reconstruct_revision(encoded_items):
    """UDF-compatible output; malformed groups expose no source text in errors.

    Duplicate deliveries must be removed by sequence before calling this worker.
    All revisions remain independently keyed. Incomplete chunks are never facts.
    """
    result = dict(state="conflict", payload_json=None, record_sha256=None, disposition=None)
    if (
        not isinstance(encoded_items, (list, tuple))
        or not 1 <= len(encoded_items) <= MAX_GROUP_ITEMS
    ):
        return result
    if not all(isinstance(s, str) for s in encoded_items):
        return result
    try:
        if sum(len(s.encode()) for s in encoded_items) > MAX_GROUP_BYTES:
            return result
        items = [wire.decode(s.encode()) for s in encoded_items]
        for item in items:
            wire.validate_item(item)
        if len({i["delivery_seq"] for i in items}) != len(items):
            return result
        raw = wire.reconstruct(items, as_bytes=True)
        payload = wire.decode(raw)
    except (wire.ProtocolError, UnicodeError) as error:
        if isinstance(error, wire.ProtocolError) and str(error) == "incomplete record":
            result["state"] = "pending"
        return result
    return dict(
        state="complete",
        payload_json=raw.decode(),
        record_sha256=wire.digest(raw),
        disposition=raw_disposition(payload),
    )


def _advance(items, revisions, previous, maximum, projection=False):
    cursor = previous
    for item in items:
        seq = item["delivery_seq"]
        if seq <= cursor:
            continue
        if seq != cursor + 1 or seq > maximum:
            break
        fact = revisions.get((item["record_id"], item["record_revision"]))
        if not fact or fact.get("state") != "complete":
            break
        if fact["record_sha256"] != item["record_sha256"]:
            raise Conflict("immutable revision conflict")
        if projection and fact.get("disposition") is None:
            break
        cursor = seq
    return cursor


def certify_prefix(identity, previous, rows, revisions):
    """Read-only barrier model over one fixed, durable, bounded Delta snapshot.

    This does not publish receipts. A real coordinator must prove a common
    snapshot, the global conflict gate and exclusive writer lease separately.
    Revisions are indexed by (record_id, record_revision) within this scope.
    Previous materialized/projected cursors remain independent, and evidence
    must include the earlier projected backlog before certifying newer work.
    """
    if not isinstance(previous, dict):
        raise Conflict("invalid prior cursors")
    mat, proj = (previous.get(k) for k in ("materialized_through", "projected_through"))
    if type(mat) is not int or type(proj) is not int or not 0 <= proj <= mat:
        raise Conflict("invalid prior cursors")
    items = unique_items(rows, identity)
    needed = [i["delivery_seq"] for i in items if i["delivery_seq"] > proj]
    if needed and needed[0] != proj + 1:
        raise Conflict("projected backlog evidence missing")
    maximum = max([mat] + [i["delivery_seq"] for i in items])
    materialized = _advance(items, revisions, mat, maximum)
    projected = _advance(items, revisions, proj, materialized, projection=True)
    return dict(materialized_through=materialized, projected_through=projected)
