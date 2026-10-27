"""Shared full-session usage metrics, independent of PR attribution.

Native drill-down reads only the session's candidate identities and their global
winners. Local/gateway receipts use the full canonical relation until their
cross-source matching can be narrowed with proven parity.
"""

from . import accounting, rates, routes

CALL_COLS = "machine tool session ts fresh_in cache_read cache_write out reasoning model cache_write_5m cache_write_1h reported_cost_usd uid".split()


def new_row(tool, session):
    return dict(
        tool=tool,
        session=session,
        calls=0,
        fresh_in=0,
        cache_read=0,
        cache_write=0,
        out=0,
        reasoning=0,
        first_ts=None,
        last_ts=None,
        largest_request_input_tokens=None,
        largest_request_total_tokens=None,
        valuation=rates.empty(),
        models=set(),
        accounts=set(),
        reported_call_value=0.0,
        reported_call_count=0,
        route_call_coverage=dict(
            provider_known_calls=0,
            endpoint_known_calls=0,
            region_known_calls=0,
            inference_account_known_calls=0,
            conflicting_route_calls=0,
        ),
    )


def add_native(row, native):
    row.update(
        project=native.get("project"),
        kind=native.get("kind"),
        usage_status=native.get("usage_status"),
        parent_session=native.get("parent_session"),
        provider=native.get("provider"),
        orchestrator=native.get("orchestrator"),
    )


def add_call(sr, call, card, resolve_account, resolve_route):
    tool, session, ts = call["tool"], call["session"], call["ts"]
    valuation = rates.value(call, card)
    sr["calls"] += 1
    rates.add(sr["valuation"], valuation)
    sr["models"].add(call["model"] or "unknown")
    sr["first_ts"] = min(sr["first_ts"] or ts, ts)
    sr["last_ts"] = max(sr["last_ts"] or ts, ts)
    for name in ("fresh_in", "cache_read", "cache_write", "out", "reasoning"):
        sr[name] += call[name] or 0
    prompt = sum(call[name] or 0 for name in ("fresh_in", "cache_read", "cache_write"))
    tokens = prompt + (call["out"] or 0)
    sr["largest_request_input_tokens"] = max(sr["largest_request_input_tokens"] or 0, prompt)
    sr["largest_request_total_tokens"] = max(sr["largest_request_total_tokens"] or 0, tokens)
    if accounting.number(call["reported_cost_usd"]) is not None:
        sr["reported_call_value"] += call["reported_cost_usd"]
        sr["reported_call_count"] += 1
    observed_route, route_error = resolve_route(tool, session, ts, call["uid"])
    for field, label in [
        ("provider", "provider_known_calls"),
        ("endpoint_host", "endpoint_known_calls"),
        ("region", "region_known_calls"),
        ("account_key", "inference_account_known_calls"),
    ]:
        if observed_route and observed_route.get(field):
            sr["route_call_coverage"][label] += 1
    if route_error == "conflicting_session_routes":
        sr["route_call_coverage"]["conflicting_route_calls"] += 1
    account = resolve_account(tool, session, ts, request_uid=call["uid"])
    if account:
        sr["accounts"].add(account["account_key"])
        sr.setdefault("account_sources", set()).add(account["provenance"])
    return valuation, account


def finish(db, session_results, resolve_account, account_root, identities):
    costs = {}
    cursor = db.execute("SELECT * FROM session_cost_events")
    names = [c[0] for c in cursor.description]
    for values in cursor:
        cost = dict(zip(names, values))
        key = (cost["tool"], cost["session"])
        previous = costs.get(key)
        if previous is None or cost["usd"] > previous["usd"]:
            costs[key] = cost
    for key, sr in session_results.items():
        sr["models"] = sorted(sr["models"])
        sr["accounts"] = sorted(sr["accounts"])
        sr["valuation"] = rates.finish(sr["valuation"])
        sr["total_tokens"] = (
            sum(sr[k] for k in ("fresh_in", "cache_read", "cache_write", "out"))
            if sr["calls"]
            else None
        )
        sr["account_sources"] = sorted(sr.get("account_sources", set()))
        sr["actual_billed_usd"] = None
        sr["reported_session_value"] = costs.get(key)
        if not sr["reported_call_count"]:
            sr["reported_call_value"] = None
    unknowns = routes.summaries(db, session_results, resolve_account, account_root, identities)
    plans = {}
    for q in accounting.rows(db, "quota_events"):
        if q["plan"]:
            plans.setdefault((q["tool"], q["session"]), set()).add(q["plan"])
    for identity in identities:
        if identity["session"] and identity["plan"]:
            plans.setdefault((identity["tool"], identity["session"]), set()).add(identity["plan"])
    for key, sr in session_results.items():
        sr["observed_plans"] = sorted(plans.get(key, set()))
    return unknowns


def calls(db, tool, session, columns=None, limit=None, ordered=True):
    from . import report_sqlite

    columns = columns or CALL_COLS
    fields = ",".join(columns)
    if tool in ("local", "gateway"):
        required = list(dict.fromkeys(columns + ["tool", "session", "ts"]))
        relation = "(" + report_sqlite.calls_select(required, db=db) + ")"
        sql = "SELECT " + fields + " FROM " + relation + " WHERE tool=? AND session=?"
        params = (tool, session)
    else:
        sql = report_sqlite.native_session_calls(fields)
        params = (tool, session, tool, session)
    if ordered:
        sql += " ORDER BY ts"
    if limit is not None:
        sql += " LIMIT ?"
        params += (limit,)
    return db.execute(sql, params)


def metrics(db, tool, session):
    result = new_row(tool, session)
    found = False
    cursor = db.execute("SELECT * FROM report_sessions WHERE tool=? AND session=?", (tool, session))
    names = [c[0] for c in cursor.description]
    for values in cursor:
        add_native(result, dict(zip(names, values)))
        found = True
    card = rates.load()
    resolve, root, identities = accounting.resolver(db)
    route = routes.route_resolver(db)
    for values in calls(db, tool, session, ordered=False):
        add_call(result, dict(zip(CALL_COLS, values)), card, resolve, route)
        found = True
    if not found:
        return None
    finish(db, {(tool, session): result}, resolve, root, identities)
    return result
