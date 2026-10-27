"""Allowlisted session-time routing evidence; configuration never rewrites history."""

import bisect
import collections
import json
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import accounting as a
from . import collect

VERSION = "session-routes-v1"


def endpoint(value):
    try:
        u = urlsplit(value if isinstance(value, str) else "")
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
            return {}
        # Queries/fragments can contain credentials; endpoint paths are unnecessary
        # beyond the protocol prefix and can also contain opaque identifiers.
        path = u.path.rstrip("/")
        safe_path = (
            path
            if re.fullmatch(r"/(?:api/|ai-gateway/|anthropic/|codex/|openai/)*v\d+", path)
            else ""
        )
        host = u.hostname.lower() + (":" + str(u.port) if u.port else "")
        return dict(endpoint_host=host, endpoint_path=safe_path, transport=u.scheme)
    except ValueError:
        return {}


def event(tool, session, ts, value, provenance, scope="session_snapshot", installation=""):
    if not isinstance(value, dict):
        return None
    provider = a.text(value.get("provider") or value.get("model_provider"))
    ep = endpoint(value.get("base_url") or value.get("baseUrl") or value.get("endpoint"))
    if not provider and not ep:
        return None
    row = dict(
        t="session_route",
        ts=int(ts),
        tool=tool,
        session=session,
        installation=installation,
        provider=provider,
        provenance=provenance,
        scope=scope,
        **ep,
    )
    for name in ("region", "profile", "billing_mode", "request_uid"):
        if value.get(name):
            label = a.text(value[name])
            row[name] = ("label:" + a.digest(label)[:12]) if "@" in label else label
    # Account namespace must be explicit; a wrapper/model provider isn't a payer.
    principal = value.get("account_id")
    realm = a.text(value.get("account_provider"))
    if principal and realm:
        row.update(account_key=a.digest(realm, a.text(principal)), account_provider=realm)
    if value.get("workspace_id"):
        row["workspace_key"] = a.digest(provider, a.text(value["workspace_id"]))
    row["uid"] = a.digest(
        {k: v for k, v in row.items() if k != "ts"}, row["ts"] if scope != "configuration" else ""
    )
    return row


def native(tool, d, p, ts, state):
    """Only native metadata records, never user content or tool output."""
    rows = []
    kind = d.get("type")
    if (
        tool == "claude"
        and kind == "bridge-session"
        and d.get("sessionId")
        and d.get("ownerAccountUuid")
    ):
        fields = a.identity_fields(
            dict(account_id=d["ownerAccountUuid"], organization_id=d.get("ownerOrganizationUuid")),
            "anthropic",
        )
        row = dict(
            t="identity",
            ts=int(ts or time.time()),
            tool=tool,
            session=d["sessionId"],
            provider="anthropic",
            provenance="claude_session_owner",
            scope="session_owner",
            **fields,
        )
        row["uid"] = a.digest("claude-owner", row["session"], fields)
        if row["uid"] not in state.setdefault("_owner_seen", []):
            rows.append(row)
            state["_owner_seen"].append(row["uid"])
    if tool == "codex" and kind == "session_meta" and ts:
        sid = p.get("id") or p.get("session_id")
        if sid:
            value = dict(p.get("route") or {}) if isinstance(p.get("route"), dict) else {}
            value.setdefault("provider", p.get("model_provider"))
            r = event(tool, sid, ts, value, "native_session_metadata")
            if r:
                rows.append(r)
            owner = d.get("creator_account_id") or p.get("creator_account_id")
            if owner:
                fields = a.identity_fields(dict(account_id=owner), "openai")
                rows.append(
                    dict(
                        t="identity",
                        uid=a.digest("codex-native-owner", sid, fields),
                        ts=int(ts),
                        tool=tool,
                        session=sid,
                        provider="openai",
                        provenance="codex_creator_account",
                        scope="session_owner",
                        **fields,
                    )
                )
    if kind == "erg_route" and d.get("sessionId") and ts:
        r = event(
            tool,
            d["sessionId"],
            ts,
            d.get("route"),
            "pi_lifecycle" if tool == "pi" else "native_route_metadata",
        )
        if r:
            rows.append(r)
    if tool == "pi" and kind == "message" and ts and state.get("session"):
        m = d.get("message") or {}
        if m.get("role") == "assistant" and isinstance(m.get("usage"), dict) and m.get("provider"):
            r = event(
                tool,
                state["session"],
                ts,
                dict(
                    provider=m["provider"],
                    request_uid="pi:" + state["session"] + ":" + str(d.get("id")),
                ),
                "native_request_provider",
                scope="request_provider",
            )
            if r:
                rows.append(r)
    return rows


def backfill(db):
    """Metadata-only scan. No collector lock, offset reset or usage reingestion."""
    from . import store

    count = files = errors = 0
    known = {
        (kind, r[0])
        for kind, table in [
            ("identity", "identity_events"),
            ("session_route", "session_route_events"),
        ]
        for r in db.execute("SELECT uid FROM " + table)
    }
    for tool, roots in [
        ("claude", collect.claude_roots()),
        ("codex", collect.native_roots("codex")),
        ("pi", collect.pi_roots()),
    ]:
        for path in collect.log_paths(roots):
            rows = []
            files += 1
            state = {}
            try:
                with collect.transcript(path) as stream:
                    for raw in stream:
                        try:
                            d = json.loads(raw)
                        except ValueError:
                            continue
                        if not isinstance(d, dict):
                            continue
                        p = d.get("payload") if tool == "codex" else d
                        if tool == "pi" and d.get("type") == "session":
                            state["session"] = d.get("id")
                        rows.extend(
                            native(tool, d, p or {}, collect.parse_ts(d.get("timestamp")), state)
                        )
                fresh = []
                for row in rows:
                    key = (row["t"], row["uid"])
                    if key not in known:
                        fresh.append(row)
                        known.add(key)
                if fresh:
                    store.ingest(db, map(json.dumps, fresh), machine=collect.machine_name())
                    count += len(fresh)
            except (OSError, RuntimeError, ValueError):
                errors += 1
    return dict(files=files, events=count, errors=errors)


def backfill_once(db):
    with db:
        db.execute("CREATE TABLE IF NOT EXISTS capture_revisions(name TEXT PRIMARY KEY,ts INT)")
    if db.execute("SELECT 1 FROM capture_revisions WHERE name=?", (VERSION,)).fetchone():
        return
    result = backfill(db)
    if result["errors"]:
        raise RuntimeError("metadata backfill has source errors")
    with db:
        db.execute(
            "INSERT OR REPLACE INTO capture_revisions VALUES(?,?)", (VERSION, int(time.time()))
        )
    return result


def codex_configuration(path, provider):
    """Read string-valued provider endpoints in TOML tables/inline tables.

    Auth commands, environment expansion and arbitrary TOML expressions are never
    evaluated. Unsupported configuration is left unknown, not guessed.
    """
    section = ""
    found = []
    for line in Path(path).read_text().splitlines():
        header = re.fullmatch(r"\s*\[([^\]]+)\]\s*(?:#.*)?", line)
        if header:
            section = header[1].replace('"', "").replace("'", "")
            continue
        literal = re.search(r'\bbase_url\s*=\s*(["\x27])([^"\x27]+)\1', line)
        if not literal:
            continue
        if section == "model_providers." + provider:
            found.append(literal[2])
        elif section == "model_providers":
            key = re.match(r'\s*["\x27]?([\w.-]+)["\x27]?\s*=\s*\{', line)
            if key and key[1] == provider:
                found.append(literal[2])
    return found[0] if len(set(found)) == 1 else ""


def omnigent_snapshot(tool, session, ts, snapshot):
    """Omnigent saves runtime configuration at session creation/resume boundaries.

    Only that saved configuration is historical evidence. A profile pointer does
    not resolve to the profile's current endpoint or authenticated principal.
    """
    if not isinstance(snapshot, dict):
        return []
    runtime = snapshot.get("runtime_config") or {}
    if not isinstance(runtime, dict):
        return []
    providers = runtime.get("providers") or {}
    inference = runtime.get("inference") or {}
    if not isinstance(providers, dict) or not isinstance(inference, dict):
        return []
    harnesses = inference.get("harnesses") or {}
    if not isinstance(harnesses, dict):
        return []
    names = {
        v.get("provider")
        for k, v in harnesses.items()
        if k in (tool, tool + "-native", tool + "-sdk")
        and isinstance(v, dict)
        and isinstance(v.get("provider"), str)
    }
    result = []
    for name in names:
        config = providers.get(name)
        if not isinstance(config, dict):
            continue
        value = {
            k: config[k]
            for k in ("profile", "region", "workspace_id", "account_id", "account_provider")
            if k in config
        }
        value["provider"] = name
        if config.get("kind") in ("subscription", "local", "gateway", "databricks"):
            value["billing_mode"] = config["kind"]
        family = (
            ("anthropic",)
            if tool == "claude"
            else ("openai",)
            if tool == "codex"
            else ("openai", "anthropic", "gemini")
        )
        urls = {
            c.get("base_url")
            for f in family
            for c in [config.get(f)]
            if isinstance(c, dict) and isinstance(c.get("base_url"), str)
        }
        if config.get("base_url"):
            urls.add(config["base_url"])
        for url in urls or {""}:
            row = event(tool, session, ts, dict(value, base_url=url), "omnigent_saved_inference")
            if row:
                result.append(row)
    return result


def profiles():
    """Non-secret profile inventory through the CLI; never open credential files."""
    now = time.monotonic()
    cached = getattr(profiles, "cache", None)
    if cached and now - cached[0] < 300:
        return cached[1]
    try:
        r = subprocess.run(
            ["databricks", "auth", "profiles", "--skip-validate", "-o", "json"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        values = json.loads(r.stdout).get("profiles", []) if r.returncode == 0 else []
        safe = [
            dict(
                profile=a.text(v.get("name")),
                workspace_id=a.text(v.get("workspace_id")),
                **endpoint(v.get("host")),
            )
            for v in values
            if isinstance(v, dict)
        ]
    except (OSError, ValueError, subprocess.SubprocessError):
        safe = []
    profiles.cache = (now, safe)
    return safe


def capture_wrappers(db):
    """Observe active wrapper configuration now, never at historical session start."""
    from . import store

    # Match native provider IDs; no machine-specific providers, hosts or regions.
    native_rows = db.execute(
        "SELECT tool,session,provider FROM report_sessions WHERE tool='codex'"
    ).fetchall()
    providers = {(r[0], r[1]): r[2] for r in native_rows}
    rows = []
    now = int(time.time())
    for path in (Path(collect.omni_home()) / "codex-native").glob("*/state.json"):
        try:
            state = json.loads(path.read_text())
            sid = state.get("thread_id") or state.get("session_id")
            provider = providers.get(("codex", sid))
            home = state.get("codex_home")
            if not sid or not provider or not home:
                continue
            url = codex_configuration(Path(home) / "config.toml", provider)
            if not url:
                continue
            value = dict(provider=provider, base_url=url)
            ep = endpoint(url)
            candidates = [
                p
                for p in getattr(profiles, "cache", (0, []))[1]
                if p.get("endpoint_host") == ep.get("endpoint_host")
            ]
            # Workspace identity is endpoint metadata; profile aliases aren't auth proof.
            workspaces = {p["workspace_id"] for p in candidates if p["workspace_id"]}
            if len(workspaces) == 1:
                value["workspace_id"] = next(iter(workspaces))
            names = {p["profile"] for p in candidates if p["profile"]}
            if len(names) == 1:
                value["profile"] = next(iter(names))
            # Optional non-secret metadata extends discovery on any machine.
            for item in collect.discovery_config().get("route_metadata", []):
                if isinstance(item, dict) and endpoint(item.get("endpoint")).get(
                    "endpoint_host"
                ) == ep.get("endpoint_host"):
                    for key in ("region",):
                        if item.get(key):
                            value[key] = item[key]
            row = event(
                "codex",
                sid,
                now,
                value,
                "omnigent_active_configuration",
                scope="configuration",
                installation=a.digest("codex", home),
            )
            if row:
                # Stable immutable first observation: replay cannot backdate evidence.
                if not db.execute(
                    "SELECT 1 FROM session_route_events WHERE uid=?", (row["uid"],)
                ).fetchone():
                    rows.append(row)
        except (OSError, ValueError, TypeError):
            continue
    if rows:
        store.ingest(db, map(json.dumps, rows), machine=collect.machine_name())
    return len(rows)


def context_route(tool, session, env, ts):
    """Actual lifecycle process context. Only explicit non-secret route fields."""
    url = env.get("ANTHROPIC_BASE_URL") if tool == "claude" else env.get("OPENAI_BASE_URL")
    if not url:
        return None
    value = dict(base_url=url, provider=env.get("ERG_ROUTE_PROVIDER") or "")
    if endpoint(env.get("DATABRICKS_HOST")).get("endpoint_host") == endpoint(url).get(
        "endpoint_host"
    ):
        value.update(
            profile=env.get("DATABRICKS_CONFIG_PROFILE") or "",
            region=env.get("ERG_ROUTE_REGION") or "",
        )
    return event(tool, session, ts, value, "lifecycle_environment")


def route_resolver(db):
    indexed = collections.defaultdict(lambda: collections.defaultdict(list))
    requests = collections.defaultdict(list)

    def merge(rows):
        merged = {}
        for field in (
            "provider",
            "endpoint_host",
            "endpoint_path",
            "region",
            "account_key",
            "account_provider",
            "billing_mode",
        ):
            values = {r[field] for r in rows if r.get(field)}
            if len(values) > 1:
                return None
            merged[field] = next(iter(values)) if values else ""
        return merged

    for r in a.rows(db, "session_route_events"):
        if r["scope"] == "configuration":
            continue
        if r.get("request_uid"):
            requests[(r["tool"], r["session"], r["request_uid"])].append(r)
            continue
        indexed[(r["tool"], r["session"])][r["ts"]].append(r)
    times = {k: sorted(v) for k, v in indexed.items()}
    session_routes = {}
    for key, groups in indexed.items():
        for stamp, rows in groups.items():
            session_routes[(key, stamp)] = merge(rows)
    requests = {key: merge(rows) for key, rows in requests.items()}

    def resolve(tool, session, ts, uid=""):
        key = (tool, session)
        stamps = times.get(key, [])
        i = bisect.bisect_right(stamps, ts) - 1
        request_key = (tool, session, uid)
        request = requests.get(request_key)
        if request_key in requests and request is None:
            return None, "conflicting_session_routes"
        if i < 0:
            return (request, None) if request else (None, "route_not_recorded_at_call")
        base = session_routes[(key, stamps[i])]
        if base is None:
            return None, "conflicting_session_routes"
        merged = dict(base)  # Request overrides and callers cannot mutate the cache.
        if request:
            if (
                request.get("provider")
                and merged.get("provider")
                and request["provider"] != merged["provider"]
            ):
                return request, None
            if (
                request.get("endpoint_host")
                and merged.get("endpoint_host")
                and request["endpoint_host"] != merged["endpoint_host"]
            ):
                # A request routed elsewhere cannot inherit the previous payer.
                merged.pop("account_key", None)
                merged.pop("account_provider", None)
            merged.update({k: v for k, v in request.items() if v})
        return merged, None

    return resolve


def summaries(db, sessions, resolve, root, identities):
    evidence = collections.defaultdict(list)
    owners = collections.defaultdict(set)
    route_rows = a.rows(db, "session_route_events")
    for r in route_rows:
        evidence[(r["tool"], r["session"])].append(r)
    # CLI workspace inventory identifies the service, never its paying principal.
    gateway_hosts = {
        r["endpoint_host"]
        for r in route_rows
        if r["provenance"] == "omnigent_active_configuration"
        and r.get("workspace_key")
        and r.get("endpoint_host")
    }
    for r in identities:
        if r.get("scope") == "session_owner" or r["provenance"] == "codex_creator_account":
            owners[(r["tool"], r["session"])].add(root(r["account_key"]))
    counts = collections.Counter()
    for key, s in sessions.items():
        rs = sorted(evidence[key], key=lambda r: r["ts"])
        reasons = []
        account = resolve(*key, s["last_ts"] or int(time.time()))
        if account:
            s["accounts"] = sorted(set(s["accounts"]) | {account["account_key"]})
            s["account_sources"] = sorted(set(s["account_sources"]) | {account["provenance"]})
        if not s["accounts"]:
            reasons.append(
                "owner_not_recorded" if len(owners[key]) < 2 else "conflicting_owner_ids"
            )
        if not rs:
            reasons.append("route_not_recorded")
        snapshots = [r for r in rs if r["scope"] != "configuration"]
        endpoints = [r for r in snapshots if r.get("endpoint_host")]
        coverage = s["route_call_coverage"]
        s["inference_route"] = None
        hosts = {r["endpoint_host"] for r in endpoints}
        native_providers = {r["provider"] for r in snapshots if r.get("provider")}
        configured_hosts = {
            r["endpoint_host"]
            for r in rs
            if r["scope"] == "configuration"
            and r.get("provider") in native_providers
            and r.get("endpoint_host")
        }
        if hosts and hosts <= gateway_hosts and not coverage["conflicting_route_calls"]:
            s["inference_route"] = dict(
                service="databricks",
                evidence="session_endpoint",
                label="Databricks gateway · session endpoint",
            )
        elif not hosts and len(configured_hosts) == 1 and configured_hosts <= gateway_hosts:
            s["inference_route"] = dict(
                service="databricks",
                evidence="native_provider_and_current_configuration",
                label="Databricks gateway · provider + current config",
            )
        if not coverage["endpoint_known_calls"]:
            reasons.append("session_time_endpoint_not_recorded")
        elif coverage["endpoint_known_calls"] < s["calls"]:
            reasons.append("partial_session_time_endpoint_coverage")
        if coverage["conflicting_route_calls"]:
            reasons.append("conflicting_session_routes")
        if any(r["scope"] == "configuration" for r in rs):
            reasons.append("current_configuration_is_not_historical_route_proof")
        if not coverage["region_known_calls"]:
            reasons.append("session_time_region_not_recorded")
        if not coverage["inference_account_known_calls"] and not (
            account and account["billing_evidence"]
        ):
            reasons.append("inference_account_not_recorded")
        if account and not account["billing_evidence"]:
            reasons.append("owner_identity_is_not_billing_evidence")
        if not account or not account.get("subscription_key"):
            reasons.append("subscription_id_not_recorded")
        if not s["calls"]:
            reasons.append("no_call_history")
        s["route_evidence"] = rs
        s["unknown_reasons"] = reasons
        s["ownership_accounts"] = sorted(owners[key])
        s["billing_account"] = account if account and account["billing_evidence"] else None
        for why in reasons:
            counts[why] += 1
    return dict(counts)
