"""Own-enrollment progress and immutable, scoped receipt snapshot pagination."""

import base64
import time
import uuid

from erg import delivery_protocol as wire

from .receiver import Forbidden, namespace

MAX_RECEIPTS = 50000


def _uuid(value):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError):
        raise wire.ProtocolError("invalid status cursor") from None


def _cursor(snapshot, page, scope):
    return (
        base64.urlsafe_b64encode(wire.encode(dict(snapshot=snapshot, page=page, scope=scope)))
        .decode()
        .rstrip("=")
    )


def progress(store, identity):
    raw = store.read_control(namespace(identity) + "/status.json")
    if raw is None:
        return dict(
            enrollment_id=identity["enrollment_id"],
            destination_generation=identity["destination_generation"],
            materialized_through=0,
            projected_through=0,
            publication_state="pending",
        )
    value = wire.decode(raw)
    if not isinstance(value, dict) or any(
        value.get(k) != identity[k] for k in ("enrollment_id", "destination_generation")
    ):
        raise wire.ProtocolError("control status scope mismatch")
    mat = value.get("materialized_through")
    proj = value.get("projected_through")
    if type(mat) is not int or type(proj) is not int or not 0 <= proj <= mat:
        raise wire.ProtocolError("invalid control progress")
    return dict(value, publication_state="published")


def delivery_status(store, identity, limit=100, cursor=None, batch_id=None):
    if identity.get("active") is not True:
        raise Forbidden("revoked enrollment")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise wire.ProtocolError("invalid status limit")
    scope = namespace(identity)
    if batch_id is not None:
        _uuid(batch_id)
        raw = store.get(scope + "/batches/" + batch_id + "/accepted.json")
        receipt = wire.decode(raw) if raw is not None else None
        return dict(
            progress(store, identity),
            protocol_version=1,
            receipts=[receipt] if receipt else [],
            next_cursor=None,
            receipt_scope="single_batch",
            missing_ranges=None,
        )
    if cursor is not None:
        if not isinstance(cursor, str) or len(cursor) > 2048:
            raise wire.ProtocolError("invalid status cursor")
        try:
            token = wire.decode(
                base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            )
        except (ValueError, TypeError):
            raise wire.ProtocolError("invalid status cursor") from None
        if not isinstance(token, dict) or token.get("scope") != scope:
            raise Forbidden("status cursor scope mismatch")
        snapshot = _uuid(token.get("snapshot"))
        page = token.get("page")
        if type(page) is not int or not 0 <= page < MAX_RECEIPTS:
            raise wire.ProtocolError("invalid status cursor")
        meta_raw = store.read_control(scope + "/receipt-snapshots/" + snapshot + "/meta.json")
        if meta_raw is None:
            raise wire.ProtocolError("status snapshot unavailable")
        meta = wire.decode(meta_raw)
        if meta.get("limit") != limit or not 0 <= page < meta["pages"]:
            raise wire.ProtocolError("status snapshot limit mismatch")
    else:
        snapshot = str(uuid.uuid4())
        page = 0
        keys = []
        for key in store.keys(scope + "/batches/"):
            if key.endswith("/accepted.json"):
                keys.append(key)
                if len(keys) > MAX_RECEIPTS:
                    raise wire.ProtocolError("status snapshot capacity exceeded")
        keys.sort()
        pages = max(1, (len(keys) + limit - 1) // limit)
        meta = dict(
            progress(store, identity),
            protocol_version=1,
            limit=limit,
            pages=pages,
            created_at=int(time.time()),
            receipt_scope="immutable_snapshot",
            missing_ranges=None,
            missing_ranges_state="unknown; receipt pages do not prove global acceptance completeness",
        )
        for index in range(pages):
            store.put_control_once(
                scope + "/receipt-snapshots/" + snapshot + "/%06d.json" % index,
                wire.encode(keys[index * limit : (index + 1) * limit]),
            )
        store.put_control_once(
            scope + "/receipt-snapshots/" + snapshot + "/meta.json", wire.encode(meta)
        )
    raw = store.read_control(scope + "/receipt-snapshots/" + snapshot + "/%06d.json" % page)
    if raw is None:
        raise wire.ProtocolError("status snapshot page unavailable")
    keys = wire.decode(raw)
    if (
        not isinstance(keys, list)
        or len(keys) > limit
        or any(
            not isinstance(k, str)
            or not k.startswith(scope + "/batches/")
            or not k.endswith("/accepted.json")
            for k in keys
        )
    ):
        raise wire.ProtocolError("invalid snapshot page")
    receipts = []
    for key in keys:
        raw = store.get(key)
        if raw is None:
            raise wire.ProtocolError("accepted receipt unavailable")
        receipt = wire.decode(raw)
        if any(receipt.get(k) != identity[k] for k in ("enrollment_id", "destination_generation")):
            raise wire.ProtocolError("accepted receipt scope mismatch")
        receipts.append(receipt)
    result = {k: v for k, v in meta.items() if k not in ("pages", "limit")}
    result.update(
        receipts=receipts,
        next_cursor=_cursor(snapshot, page + 1, scope) if page + 1 < meta["pages"] else None,
    )
    return result
