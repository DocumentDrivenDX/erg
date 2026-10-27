"""Databricks AI Gateway erg ingest (system.ai_gateway.usage).

Uses the Databricks CLI's existing auth (`databricks api ...`); no tokens are read or stored here.
Config: ~/.config/erg/gateway.json  {"profile","warehouse_id","table","me"}
"""

import calendar
import json
import os
import subprocess
import time

CONF = os.path.join(os.path.expanduser("~"), ".config", "erg", "gateway.json")
TABLE = "system.ai_gateway.usage"
MACHINE = "databricks"


def load_conf():
    try:
        return json.load(open(CONF))
    except FileNotFoundError:
        return None


def _api(profile, method, path, body=None):
    cmd = ["databricks", "api", method, path, "-p", profile]
    if body is not None:
        cmd += ["--json", json.dumps(body)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip()[-400:]
        hint = (
            " (try: databricks auth login -p %s)" % profile
            if "auth" in msg.lower() or "token" in msg.lower()
            else ""
        )
        raise RuntimeError("databricks api %s failed: %s%s" % (path, msg, hint))
    return json.loads(r.stdout)


def discover_warehouse(profile):
    ws = _api(profile, "get", "/api/2.0/sql/warehouses").get("warehouses", [])
    ws.sort(key=lambda w: (w.get("state") != "RUNNING", w.get("cluster_size", "")))
    if not ws:
        raise RuntimeError("no SQL warehouse visible to profile %s" % profile)
    return ws[0]["id"]


def run_sql(conf, sql):
    st = _api(
        conf["profile"],
        "post",
        "/api/2.0/sql/statements",
        {
            "warehouse_id": conf["warehouse_id"],
            "statement": sql,
            "wait_timeout": "50s",
            "format": "JSON_ARRAY",
            "disposition": "INLINE",
            "on_wait_timeout": "CONTINUE",
        },
    )
    sid = st["statement_id"]
    t0 = time.time()
    while st["status"]["state"] in ("PENDING", "RUNNING"):
        if time.time() - t0 > 600:
            raise RuntimeError("statement timed out")
        time.sleep(3)
        st = _api(conf["profile"], "get", "/api/2.0/sql/statements/" + sid)
    if st["status"]["state"] != "SUCCEEDED":
        raise RuntimeError("SQL failed: %s" % json.dumps(st["status"])[:400])
    rows = list((st.get("result") or {}).get("data_array") or [])
    nxt = (st.get("result") or {}).get("next_chunk_index")
    while nxt is not None:
        ch = _api(
            conf["profile"], "get", "/api/2.0/sql/statements/%s/result/chunks/%d" % (sid, nxt)
        )
        rows += ch.get("data_array") or []
        nxt = ch.get("next_chunk_index")
    return rows


def _ts(s):
    return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))


def _i(x):
    return int(x) if x not in (None, "") else 0


QUERY = """SELECT coalesce(invocation_id, request_id), event_time, requester, requester_type, service_type,
  session_metadata.coding_agent, session_metadata.surface, session_metadata.client_session_id,
  regexp_extract(user_agent, '^([A-Za-z0-9_.-]+) \\\\(', 1), destination_model, endpoint_name,
  input_tokens, output_tokens, token_details.cache_read_input_tokens, token_details.cache_creation_input_tokens,
  token_details.output_reasoning_tokens, status_code
FROM {table} WHERE event_time >= '{a}' AND event_time < '{b}' AND coalesce(total_tokens, 0) > 0
ORDER BY event_time"""


def fetch(conf, since_ts):
    """Yield llm-shaped dicts, one day-window query at a time."""
    day = since_ts // 86400 * 86400
    end = int(time.time()) + 86400
    while day < end:
        a, b = (
            time.strftime("%Y-%m-%d", time.gmtime(day)),
            time.strftime("%Y-%m-%d", time.gmtime(day + 86400)),
        )
        for r in run_sql(conf, QUERY.format(table=conf.get("table", TABLE), a=a, b=b)):
            (
                uid,
                et,
                req,
                rtype,
                stype,
                agent,
                surface,
                sess,
                app,
                model,
                ep,
                inp,
                out,
                cr,
                cw,
                rsn,
                code,
            ) = r
            if code and int(code) >= 400:
                continue
            inp, cr, cw = _i(inp), _i(cr), _i(cw)
            yield {
                "uid": "g:" + str(uid),
                "ts": _ts(et),
                "tool": "gateway",
                "session": sess or "",
                "requester": req or "",
                "project": app or (agent and "(gateway:%s)" % agent) or "(gateway)",
                "model": model or ep or "unknown",
                "kind": "main",
                "client": agent or app or stype or "",
                "fresh_in": max(inp - cr - cw, 0),
                "cache_read": cr,
                "cache_write": cw,
                "out": _i(out),
                "reasoning": _i(rsn),
                "rtype": rtype,
            }
        day += 86400


def sync(db, conf, full=False):
    """Incremental by event_time; refreshes the last 2 days. Returns rows ingested."""
    from . import store

    r = db.execute("SELECT MAX(ts) FROM llm WHERE machine=?", (MACHINE,)).fetchone()[0]
    since = 0 if (full or not r) else r - 2 * 86400
    if not since:  # first run: start at the table's earliest row instead of probing empty days
        first = run_sql(conf, "SELECT min(event_time) FROM %s" % conf.get("table", TABLE))
        since = _ts(first[0][0]) if first and first[0][0] else int(time.time()) - 30 * 86400
    rows = list(fetch(conf, since))
    store.ingest_gateway(db, rows, MACHINE)
    return len(rows)
