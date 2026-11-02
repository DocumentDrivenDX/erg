"""Bounded metadata-only contiguous-prefix proof for pinned LDP snapshots."""

from .receiver import Conflict

MAX_WINDOW = 64000
MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_COPY_BYTES = 192 * 1024 * 1024


def prove_materialized(previous, rows):
    cursor = previous
    seen = set()
    records = set()
    copy_bytes = 0
    for row in rows:
        seq = row["delivery_seq"]
        if type(seq) is not int or seq <= 0 or seq in seen:
            raise Conflict("invalid or duplicate LDP sequence proof")
        seen.add(seq)
        if (
            row.get("revision_sha256") is not None
            and row["record_sha256"] != row["revision_sha256"]
        ):
            raise Conflict("LDP revision hash disagrees with delivery")
        if seq != cursor + 1 or row.get("revision_sha256") is None:
            break
        if "payload_bytes" in row:
            size = row["payload_bytes"]
            if type(size) is not int or not 0 <= size <= 64 * 1024 * 1024:
                raise Conflict("invalid LDP payload size proof")
            key = row["record_id"], row["record_revision"]
            if key not in records:
                if copy_bytes + size > MAX_COPY_BYTES:
                    break
                records.add(key)
                copy_bytes += size
        cursor = seq
    return cursor


def prove_projected(previous, materialized, rows):
    cursor = previous
    for row in rows:
        seq = row["delivery_seq"]
        if (
            type(seq) is not int
            or seq != cursor + 1
            or seq > materialized
            or row.get("projection_disposition") is None
        ):
            break
        cursor = seq
    return cursor


def prove_window(previous, materialized_rows, projected_rows):
    mat, proj = previous.get("materialized_through"), previous.get("projected_through")
    if type(mat) is not int or type(proj) is not int or not 0 <= proj <= mat:
        raise Conflict("invalid prior LDP publication cursors")
    if len(materialized_rows) > MAX_WINDOW or len(projected_rows) > MAX_WINDOW:
        raise Conflict("LDP prefix proof exceeds metadata row bound")
    # Bytes are checked before collector input reaches this helper as well.
    from erg import delivery_protocol as wire

    if len(wire.encode([materialized_rows, projected_rows])) > MAX_METADATA_BYTES:
        raise Conflict("LDP prefix proof exceeds metadata byte bound")
    materialized = prove_materialized(mat, materialized_rows)
    projected = prove_projected(proj, materialized, projected_rows)
    return dict(materialized_through=materialized, projected_through=projected)
