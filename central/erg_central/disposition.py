"""Shared projection disposition policy for batch and generated LDP workers."""


def raw_disposition(payload):
    if payload["record_kind"] in ("session_event", "content_tombstone"):
        return "not_applicable:raw-session-v1"
    if (
        payload.get("origin") == "derived_reporting"
        and payload.get("kind") == payload["record_kind"]
        and payload["record_kind"] in ("usage_evidence", "work_evidence", "account_evidence")
        and isinstance(payload.get("body"), dict)
        and payload["body"].get("schema_version") == 1
    ):
        return "not_applicable:raw-reporting-v1"
    return None
