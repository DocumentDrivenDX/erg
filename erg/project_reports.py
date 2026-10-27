"""Read-only project cost reporting over canonical calls and recorded context."""

import contextlib
import math
import time
from collections import defaultdict
from decimal import Decimal

from . import project_scope, projects, rates, report_scope

# Compatibility names; temporal context is shared by all project-aware reports.
context_index = project_scope.context_index
context_at = project_scope.context_at


def empty():
    return dict(
        distinct_calls=0,
        request_equivalents=Decimal(0),
        tokens=Decimal(0),
        priced_equivalents=Decimal(0),
        unpriced_equivalents=Decimal(0),
        known_value_low_usd=Decimal(0),
        known_value_high_usd=Decimal(0),
        reported_equivalents=Decimal(0),
        known_reported_usd=Decimal(0),
    )


def add(row, call, weight, valuation):
    row["distinct_calls"] += 1
    row["request_equivalents"] += weight
    row["tokens"] += (
        sum(
            Decimal(str(call.get(k) or 0)) for k in ("fresh_in", "cache_read", "cache_write", "out")
        )
        * weight
    )
    if valuation:
        row["priced_equivalents"] += weight
        for bound in ("low", "high"):
            row["known_value_" + bound + "_usd"] += Decimal(str(valuation[bound + "_usd"])) * weight
    else:
        row["unpriced_equivalents"] += weight
    reported = call.get("reported_cost_usd")
    if (
        isinstance(reported, (int, float))
        and not isinstance(reported, bool)
        and math.isfinite(reported)
        and reported >= 0
    ):
        row["reported_equivalents"] += weight
        row["known_reported_usd"] += Decimal(str(reported)) * weight


def finish(row):
    row = dict(row)
    row["actual_billed_usd"] = None
    row["allocated_subscription_usd"] = None
    row["reported_complete"] = bool(
        row["request_equivalents"] and row["reported_equivalents"] == row["request_equivalents"]
    )
    row["price_coverage"] = (
        float(row["priced_equivalents"] / row["request_equivalents"])
        if row["request_equivalents"]
        else None
    )
    for key, value in row.items():
        if isinstance(value, Decimal):
            row[key] = float(value)
    return row


def _report(
    db,
    start=0,
    end=None,
    machine="",
    tool="",
    session="",
    project_key="",
    config=None,
    canonical_select=None,
    folder_key="",
    tools="",
    kinds="",
    requester="",
    identities=(),
):
    end = int(time.time()) + 1 if end is None else end
    if (
        isinstance(start, bool)
        or isinstance(end, bool)
        or not isinstance(start, int)
        or not isinstance(end, int)
        or not 0 <= start < end <= 2**63 - 1
    ):
        raise ValueError("Invalid cost interval")
    config = projects.validate(config if config is not None else projects.snapshot(db))
    scope = project_scope.Scope(db, end, config)
    selection = report_scope.Selection(
        dict(
            start=start,
            end=end,
            machine=machine,
            tool=tool,
            session=session,
            project_key=project_key,
            folder_key=folder_key,
            tools=tools,
            kinds=kinds,
            requester=requester,
        ),
        identities,
    )
    names = {p["project_key"]: p["name"] for p in config["projects"]}
    if project_key and project_key != "@unassigned" and project_key not in names:
        raise ValueError("Unknown accounting project")
    if not isinstance(folder_key, str) or len(folder_key) > 256:
        raise ValueError("Invalid folder filter")
    where = ["ts>=?", "ts<?"]
    params = [start, end]
    for field, value in (("machine", machine), ("tool", tool), ("session", session)):
        if not isinstance(value, str) or len(value) > 256:
            raise ValueError("Invalid cost filter")
        if value:
            where.append(field + "=?")
            params.append(value)
    card = rates.load(db)
    totals = empty()
    buckets = {}
    folder_buckets = {}
    sessions = {}
    reasons = defaultdict(int)
    sources = set()
    dates = set()
    rate_sources = set()
    columns = "machine uid ts tool session project model fresh_in cache_read cache_write out cache_write_5m cache_write_1h reported_cost_usd reported_cost_source kind requester".split()
    if canonical_select is not None and not callable(canonical_select):
        raise ValueError("Backend query factory required")
    relation = "(" + canonical_select(columns) + ")" if canonical_select is not None else "calls"
    for values in db.execute(
        "SELECT "
        + ",".join(columns)
        + " FROM "
        + relation
        + " WHERE "
        + " AND ".join(where)
        + " ORDER BY ts",
        params,
    ):
        call = dict(zip(columns, values))
        if not selection.accepts(call):
            continue
        allocation = scope.resolve(call)
        folder = allocation["folder"]
        if folder_key and (
            folder["kind"] != "unobserved"
            if folder_key == "@unobserved"
            else folder["folder_key"] != folder_key
        ):
            continue
        portions = list(allocation["allocations"])
        if project_key:
            portions = [p for p in portions if p["project_key"] == project_key]
        if not portions:
            continue
        valuation = rates.value(call, card)
        sources.add(call["tool"])
        if valuation:
            dates.add(valuation["as_of"])
            rate_sources.add(valuation["source"])
        total_weight = Decimal(sum(p["units"] for p in portions)) / projects.UNITS
        add(totals, call, total_weight, valuation)
        if allocation["reason"]:
            reasons[allocation["reason"]] += 1
        key = (call["machine"], call["tool"], call["session"])
        if key not in sessions:
            sessions[key] = dict(
                empty(),
                machine=key[0],
                tool=key[1],
                session=key[2],
                projects=set(),
                folders=set(),
                reasons=set(),
                binding_refs=set(),
                first_ts=call["ts"],
                last_ts=call["ts"],
            )
        sr = sessions[key]
        add(sr, call, total_weight, valuation)
        sr["last_ts"] = call["ts"]
        sr["binding_refs"].update(allocation["binding_refs"])
        sr["folders"].add(folder["folder_key"])
        if allocation["reason"]:
            sr["reasons"].add(allocation["reason"])
        for portion in portions:
            name = portion["project_key"]
            weight = Decimal(portion["units"]) / projects.UNITS
            if name not in buckets:
                buckets[name] = dict(empty(), project_key=name, name=names.get(name, "Unassigned"))
            add(buckets[name], call, weight, valuation)
            sr["projects"].add(name)
            fk = (name, folder["folder_key"])
            if fk not in folder_buckets:
                folder_buckets[fk] = dict(empty(), **folder, project_key=name, sessions=set())
            add(folder_buckets[fk], call, weight, valuation)
            folder_buckets[fk]["sessions"].add(key)
    title_map = {
        (r[0], r[1], r[2]): r[3]
        for r in db.execute("SELECT machine,tool,session,title FROM session_titles")
    }
    ordered = sorted(sessions.items(), key=lambda item: item[1]["tokens"], reverse=True)
    session_rows = []
    for key, row in ordered[:100]:
        row = dict(row, title=title_map.get(key))
        row.update(
            {
                field: sorted(row[field])
                for field in ("projects", "folders", "reasons", "binding_refs")
            }
        )
        session_rows.append(finish(row))
    rows = [finish(row) for row in buckets.values()]
    rows.sort(key=lambda row: row["tokens"], reverse=True)
    folder_rows = []
    for row in folder_buckets.values():
        row = dict(row)
        row["session_count"] = len(row.pop("sessions"))
        folder_rows.append(finish(row))
    folder_rows.sort(key=lambda row: -row["tokens"])
    overlap = ("local" in sources and any(x not in ("local", "gateway") for x in sources)) or (
        "gateway" in sources and len(sources) > 1
    )
    return dict(
        generated=int(time.time()),
        revision=config["revision"],
        catalog=config["projects"],
        binding_count=len(config["bindings"]),
        filters=dict(
            start=start,
            end=end,
            machine=machine,
            tool=tool,
            session=session,
            project_key=project_key,
            folder_key=folder_key,
            tools=tools,
            kinds=kinds,
            requester=requester,
        ),
        totals=finish(totals),
        projects=rows,
        folders=folder_rows,
        folder_catalog=list(scope.folders.values()),
        observed_tools=sorted(sources),
        sessions=session_rows,
        session_count=len(sessions),
        sessions_truncated=len(sessions) > 100,
        unassigned_reasons=dict(reasons),
        rate_card_dates=sorted(dates),
        rate_sources=sorted(rate_sources),
        warnings=[
            "Unreconciled agent/server source observations may overlap; this is not a deduplicated physical-spend total."
        ]
        if overlap
        else [],
        monetary_basis="Reference token value and native reported estimates are separate; actual billed and allocated subscription expense are unknown",
        context_scope="Recorded native directory and work context only; folder labels are not project assignments",
    )


@contextlib.contextmanager
def read_snapshot(db):
    owned = not db.in_transaction
    if owned:
        db.execute("BEGIN")
    try:
        yield
    finally:
        if owned:
            db.rollback()


def report(
    db,
    start=0,
    end=None,
    machine="",
    tool="",
    session="",
    project_key="",
    config=None,
    canonical_select=None,
    folder_key="",
    tools="",
    kinds="",
    requester="",
    identities=(),
):
    with read_snapshot(db):
        return _report(
            db,
            start,
            end,
            machine,
            tool,
            session,
            project_key,
            config,
            canonical_select,
            folder_key,
            tools,
            kinds,
            requester,
            identities,
        )
