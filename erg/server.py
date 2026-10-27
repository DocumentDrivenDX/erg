"""Independent reporting service; loopback project metadata administration only."""

import contextlib
import http.server
import json
import os
import sqlite3
import threading
import time
import urllib.parse

from . import store

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class Reports:
    def __init__(
        self,
        backend="sqlite",
        profile=None,
        warehouse=None,
        namespace=None,
        access_token=None,
        database=None,
        source_backend=None,
        source_status=None,
    ):
        self.database = database or store.DB_PATH
        self.source_backend = source_backend or backend
        self.source_status = source_status
        self.work_cache = None
        self.accounts_cache = None
        self.work_view_cache = None
        self.report_reply_lock = threading.Lock()
        self.report_replies = {}
        self.report_revision = 0
        self.work_revision = 0
        self.request_lock = threading.Lock()
        self.request_metrics = {}
        self.cost_cache = {}
        self.cost_lock = threading.Lock()
        self.scoped_work_cache = {}
        self.scoped_work_lock = threading.Lock()
        self.scoped_work_guard = threading.Lock()
        self.backend = backend
        self.lock = threading.RLock()
        self.cache = {}
        self.allocated_cache = {}
        self.allocated_pieces = {}
        self.wire_data = {}
        self.wire_work = None
        self.wire_work_views = {}
        if backend == "databricks":
            from .databricks import Client, ReportDB

            self.remote = ReportDB(Client(profile, warehouse, access_token=access_token), namespace)

    def read_db(self):
        return (
            store.open_readonly()
            if self.database == store.DB_PATH
            else store.open_readonly(self.database)
        )

    def record_request(self, path, cpu, wall):
        # Fixed routes only: never retain query strings, session IDs or content.
        route = urllib.parse.urlsplit(path).path
        if route not in (
            "/api/money",
            "/api/billing",
            "/api/data",
            "/api/effectiveness",
            "/api/costs",
            "/api/projects",
            "/api/session",
            "/api/capture/status",
            "/api/history/status",
            "/api/history/discovery",
            "/api/history/reconciliation",
            "/api/history/sessions",
            "/api/history/session",
        ):
            route = "/api/other"
        with self.request_lock:
            row = self.request_metrics.setdefault(
                route, dict(requests=0, cpu_seconds=0, wall_seconds=0, max_wall_seconds=0)
            )
            row["requests"] += 1
            row["cpu_seconds"] += cpu
            row["wall_seconds"] += wall
            row["max_wall_seconds"] = max(row["max_wall_seconds"], wall)

    def request_stats(self):
        with self.request_lock:
            return dict(
                scope="Request handler thread CPU only; no query strings or session content",
                routes={k: dict(v) for k, v in self.request_metrics.items()},
            )

    def project_revision(self):
        if self.backend != "sqlite":
            return None
        with contextlib.closing(self.read_db()) as db:
            if not isinstance(db, sqlite3.Connection):
                return None
            row = db.execute("SELECT revision FROM project_config WHERE id=1").fetchone()
            return row[0] if row else None

    def rollup_stamp(self):
        if self.backend != "sqlite":
            return None
        from . import project_rollups

        with contextlib.closing(self.read_db()) as db:
            if not isinstance(db, sqlite3.Connection):
                return None
            value = project_rollups.status(db)
            return (value["enabled"], value.get("updated_at", 0))

    def data(self, days=90, refresh=False):
        revision = (self.project_revision(), self.rollup_stamp())
        with self.lock:
            cached = self.cache.get(days)
            if (
                cached
                and not refresh
                and time.monotonic() - cached[0] < 900
                and (len(cached) < 3 or cached[2] == revision)
            ):
                self.cache.pop(days)
                self.cache[days] = cached
                data = dict(cached[1])
                if self.backend == "sqlite" and self.source_backend == "sqlite":
                    from .service import status

                    data["capture_service"] = status()
                return data
            since = int(time.time() - days * 86400) if days else 0
            if self.backend == "databricks":
                data = store.dataset(self.remote, since)
            else:
                db = self.read_db()
                try:
                    db.execute("BEGIN")
                    if isinstance(db, sqlite3.Connection):
                        from . import project_dataset, project_rollups

                        data = store.dataset(db, since, False)
                        rollup = project_rollups.status(db)
                        key = (
                            since // 3600,
                            rollup.get("updated_at"),
                            rollup.get("project_revision"),
                        )
                        allocated = self.allocated_cache.get(key) if rollup["enabled"] else None
                        if allocated is None:
                            allocated = project_rollups.dataset(db, since, self.allocated_pieces)
                        if allocated is not None:
                            self.allocated_cache.pop(key, None)
                            self.allocated_cache[key] = allocated
                            while len(self.allocated_cache) > 4:
                                self.allocated_cache.pop(next(iter(self.allocated_cache)))
                            data.update(allocated)
                            data["rollup_status"] = rollup
                        else:
                            data.update(project_dataset.build(db, since))
                    else:
                        data = store.dataset(db, since)
                finally:
                    db.close()
            if self.backend == "sqlite" and self.source_backend == "sqlite":
                from .service import status

                data["capture_service"] = status()
            data["backend"] = self.source_backend
            if self.source_status:
                data["report_source"] = self.source_status()
            data["window_days"] = days
            tools = {r[2] for r in data["llm"]}
            data["warnings"] = []
            if data.get("rollup_status", {}).get("enabled") and not data["rollup_status"]["ready"]:
                data["warnings"].append(
                    "Usage rollups are warming in the background; totals are not yet available."
                )
            elif data.get("rollup_status", {}).get("stale_policy"):
                data["warnings"].append(
                    "Project allocation is rebuilding; showing the previous completed allocation revision."
                )
            elif data.get("rollup_status", {}).get("pending_hours"):
                data["warnings"].append(
                    "Showing completed usage rollups; recent changes are processing in the background."
                )
            if "gateway" in tools and len(tools) > 1:
                data["warnings"].append(
                    "Gateway and host observations without shared request identity remain unreconciled. Source totals may overlap."
                )
            if "local" in tools and any(t not in ("local", "gateway") for t in tools):
                data["warnings"].append(
                    "Unlinked local-server receipts remain separate from agent observations. Without shared request identity, combined source totals may overlap."
                )
            data["reporting"] = {
                "cost_provenance": "unknown",
                "unknown_cost_calls": sum(r[11] for r in data["llm"]),
            }
            # Multiple open tabs may use different windows. Retaining only the
            # latest window makes their background polling evict one another.
            self.wire_data.pop(days, None)
            self.cache.pop(days, None)
            self.cache[days] = (time.monotonic(), data, revision)
            while len(self.cache) > 4:
                self.cache.pop(next(iter(self.cache)))
            return data

    def invalidate_report_replies(self):
        # Never take the build lock here: refresh must invalidate ready replies
        # even when an older report is still being calculated.
        with self.report_reply_lock:
            self.report_revision += 1
            self.report_replies.clear()

    def sync_report_revision(self):
        # Caller holds self.lock. A queued refresh makes every older calculated
        # snapshot ineligible for reuse, including snapshots built during refresh.
        with self.report_reply_lock:
            revision = self.report_revision
        if self.work_revision != revision:
            self.work_cache = None
            self.accounts_cache = None
            self.work_view_cache = None
            self.wire_work = None
            self.wire_work_views.clear()
            self.work_revision = revision

    def ready_report_reply(self, view):
        with self.report_reply_lock:
            cached = self.report_replies.get(view)
            if cached and cached[1] == self.report_revision and time.monotonic() - cached[0] < 900:
                return cached[2]
        return None

    def publish_report_reply(self, view, stamp, body):
        with self.report_reply_lock:
            # An older in-flight calculation may finish after refresh started;
            # it can answer its original caller but cannot restore a stale cache.
            if self.work_revision == self.report_revision:
                self.report_replies[view] = (stamp, self.work_revision, body)
        return body

    def effectiveness(self, refresh=False):
        if refresh:
            self.invalidate_report_replies()
        if self.backend != "sqlite":
            return dict(
                available=False, reason="PR effectiveness currently requires the local ledger"
            )
        with self.lock:
            self.sync_report_revision()
            if self.work_cache and not refresh and time.monotonic() - self.work_cache[0] < 900:
                return self.work_cache[1]
            from . import work

            db = self.read_db()
            try:
                db.execute("BEGIN")
                data = work.effectiveness(db)
            finally:
                db.close()
            with self.report_reply_lock:
                self.report_replies.clear()
            data["available"] = True
            self.accounts_cache = None
            self.work_view_cache = None
            self.wire_work = None
            self.wire_work_views.clear()
            self.work_cache = (time.monotonic(), data)
            return data

    def encoded_data(self, days=90, refresh=False):
        # Reuse serialization as well as queries. Only capture health changes
        # between cached report requests; its fixed-size payload stays fresh.
        with self.lock:
            data = self.data(days, refresh)
            source = self.cache[days][1]
            cached = self.wire_data.get(days)
            if not cached or cached[0] is not source:
                stable = {k: v for k, v in data.items() if k != "capture_service"}
                body = json.dumps(stable, allow_nan=False, separators=(",", ":")).encode()
                cached = (source, body)
                self.wire_data[days] = cached
            for window in list(self.wire_data):
                if window not in self.cache:
                    self.wire_data.pop(window)
            body = cached[1]
            if "capture_service" in data:
                health = json.dumps(
                    data["capture_service"], allow_nan=False, separators=(",", ":")
                ).encode()
                body = (
                    body[:-1]
                    + (b"," if body != b"{}" else b"")
                    + b'"capture_service":'
                    + health
                    + b"}"
                )
            return body

    def encoded_effectiveness(self, refresh=False, view="full"):
        from . import report_views

        if view not in report_views.VIEWS:
            raise ValueError("invalid report view")
        if view in ("work", "accounts") and self.backend == "sqlite":
            stamp = self.rollup_stamp()
            if stamp and stamp[0]:
                return self.encoded_scoped_work({}, refresh, view)
        if refresh:
            self.invalidate_report_replies()
        if self.backend == "sqlite" and not refresh:
            body = self.ready_report_reply(view)
            if body is not None:
                return body
        with self.lock:
            self.sync_report_revision()
            # Coalesce cold callers that were queued behind the same build.
            if self.backend == "sqlite" and not refresh:
                body = self.ready_report_reply(view)
                if body is not None:
                    return body
            if view in ("accounts", "work") and self.backend == "sqlite":
                full_fresh = self.work_cache and time.monotonic() - self.work_cache[0] < 900
                if not full_fresh:
                    cached = self.accounts_cache if view == "accounts" else self.work_view_cache
                    if cached and time.monotonic() - cached[0] < 900:
                        return self.publish_report_reply(view, cached[0], cached[1])
                    from . import account_reports, work

                    db = self.read_db()
                    try:
                        db.execute("BEGIN")
                        data = (
                            account_reports.report(db)
                            if view == "accounts"
                            else work.effectiveness(db, view="work")
                        )
                    finally:
                        db.close()
                    data["available"] = True
                    body = json.dumps(
                        report_views.project(data, view), allow_nan=False, separators=(",", ":")
                    ).encode()
                    stamp = time.monotonic()
                    if view == "accounts":
                        self.accounts_cache = (stamp, body)
                    else:
                        self.work_view_cache = (stamp, body)
                    return self.publish_report_reply(view, stamp, body)
            data = self.effectiveness()
            if self.work_cache is None:
                return json.dumps(data, allow_nan=False, separators=(",", ":")).encode()
            if view == "full":
                if self.wire_work is None or self.wire_work[0] is not data:
                    self.wire_work = (
                        data,
                        json.dumps(data, allow_nan=False, separators=(",", ":")).encode(),
                    )
                return self.publish_report_reply(view, self.work_cache[0], self.wire_work[1])
            cached = self.wire_work_views.get(view)
            if cached is None or cached[0] is not data:
                cached = (
                    data,
                    json.dumps(
                        report_views.project(data, view), allow_nan=False, separators=(",", ":")
                    ).encode(),
                )
                self.wire_work_views[view] = cached
            return self.publish_report_reply(view, self.work_cache[0], cached[1])

    def encoded_scoped_work(self, filters, refresh=False, view="work"):
        if self.backend != "sqlite":
            return json.dumps(
                dict(available=False, reason="Work currently requires the local ledger")
            ).encode()
        from . import report_scope, report_views, work

        selection = report_scope.Selection(filters, store.my_identities())
        key = (
            view,
            self.project_revision(),
            self.rollup_stamp(),
            tuple(sorted(selection.values.items())),
            tuple(sorted(selection.identities)),
        )

        def ready():
            with self.scoped_work_guard:
                row = self.scoped_work_cache.get(key)
                return row[1] if row and time.monotonic() - row[0] < 900 else None

        if not refresh:
            body = ready()
            if body is not None:
                return body
        else:
            with self.scoped_work_guard:
                self.scoped_work_cache.pop(key, None)
        with self.scoped_work_lock:
            if not refresh:
                body = ready()
                if body is not None:
                    return body
            with contextlib.closing(self.read_db()) as db:
                db.execute("BEGIN")
                from . import report_evidence

                data = report_evidence.report(db, view, selection)
                if data is None:
                    if view == "work":
                        data = work.effectiveness(db, view="work", selection=selection)
                    else:
                        from . import account_reports

                        data = account_reports.report(db)
                data.setdefault("available", True)
                from . import monetary

                monetary.enrich(db, data, selection, view)
            body = json.dumps(
                report_views.project(data, view), allow_nan=False, separators=(",", ":")
            ).encode()
            with self.scoped_work_guard:
                if len(self.scoped_work_cache) >= 8:
                    self.scoped_work_cache.pop(next(iter(self.scoped_work_cache)))
                self.scoped_work_cache[key] = (time.monotonic(), body)
            return body

    def costs(self, **filters):
        if self.backend != "sqlite":
            return dict(
                available=False, reason="Project accounting currently requires the local ledger"
            )
        from . import project_reports, projects, report_sqlite

        with self.cost_lock:
            with contextlib.closing(self.read_db()) as db:
                db.execute("BEGIN")
                from . import project_rollups

                rs = project_rollups.status(db)
                revision = (projects.snapshot(db)["revision"], rs.get("updated_at", 0))
                key = (revision, tuple(sorted(filters.items())))
                cached = self.cost_cache.get(key)
                if cached and time.monotonic() - cached[0] < 30:
                    return cached[1]
                data = project_rollups.costs(db, **filters)
                if data is None:
                    data = project_reports.report(
                        db,
                        canonical_select=lambda columns: report_sqlite.calls_select(columns, db=db),
                        identities=store.my_identities(),
                        **filters,
                    )
                    data["available"] = True
                from . import monetary, report_scope

                monetary.enrich(
                    db, data, report_scope.Selection(filters, store.my_identities()), "costs"
                )
            if len(self.cost_cache) >= 8:
                self.cost_cache.clear()
            self.cost_cache[key] = (time.monotonic(), data)
            return data

    def project_change(self, body):
        if self.backend != "sqlite":
            raise ValueError("Project administration is local only")
        from . import project_admin, report_sqlite

        apply = isinstance(body, dict) and body.get("apply") is True
        with contextlib.closing(
            store.connect(path=store.DB_PATH, initialize=False) if apply else self.read_db()
        ) as db:
            if not apply:
                db.execute("BEGIN")
            result = project_admin.change(
                db,
                body,
                canonical_select=lambda columns: report_sqlite.calls_select(columns, db=db),
            )
        if result.get("applied"):
            with self.lock:
                self.cache.clear()
                self.wire_data.clear()
            with self.cost_lock:
                self.cost_cache.clear()
            with self.scoped_work_guard:
                self.scoped_work_cache.clear()
        return result

    def session(self, tool, session):
        if not tool or not session or len(session) > 256:
            raise ValueError("tool and session are required")
        cols = [
            "uid",
            "ts",
            "tool",
            "session",
            "project",
            "model",
            "fresh_in",
            "cache_read",
            "cache_write",
            "out",
            "reasoning",
        ]
        db = self.remote if self.backend == "databricks" else self.read_db()
        try:
            if self.backend == "sqlite":
                from . import session_usage

                # Keep call detail, counter and full-session metrics in one read
                # snapshot without touching the whole-ledger effectiveness cache.
                db.execute("BEGIN")
                cursor = session_usage.calls(db, tool, session, cols, 1001)
            else:
                cursor = db.execute(
                    "SELECT "
                    + ",".join(cols)
                    + " FROM calls WHERE tool=? AND session=? ORDER BY ts LIMIT 1001",
                    (tool, session),
                )
            rows = [dict(zip(cols, r)) for r in cursor]
            truncated = len(rows) > 1000
            rows = rows[:1000]
            fields = [
                "usage_status",
                "orchestrator",
                "conversation_id",
                "surface",
                "summary_in",
                "summary_out",
                "summary_cache_read",
                "summary_cache_write",
            ]
            metadata = db.execute(
                "SELECT "
                + ",".join(fields)
                + " FROM report_sessions WHERE tool=? AND session=? LIMIT 1",
                (tool, session),
            ).fetchone()
            from . import history_projection

            counter = (
                history_projection.latest_counter(db, tool, session)
                if self.backend == "sqlite"
                else None
            )
            metrics = session_usage.metrics(db, tool, session) if self.backend == "sqlite" else None
            return dict(
                tool=tool,
                session=session,
                calls=rows,
                usage_counter_snapshot=counter,
                metadata=dict(zip(fields, metadata)) if metadata else {},
                metrics=metrics,
                limit=1000,
                calls_truncated=truncated,
                cost_provenance="reported_estimates_and_reference_valuation; actual_billed_unknown",
            )
        finally:
            if self.backend != "databricks":
                db.close()


def serve(port, bind="127.0.0.1", backend="sqlite", profile=None, warehouse=None, namespace=None):
    reports = Reports(backend, profile, warehouse, namespace)
    cloud_reports = None
    cloud_lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            cpu = time.thread_time()
            wall = time.monotonic()
            try:
                self.get_response()
            finally:
                if self.path.startswith("/api/"):
                    reports.record_request(
                        self.path, time.thread_time() - cpu, time.monotonic() - wall
                    )

        def local_project_request(self):
            import ipaddress

            host = self.headers.get("Host", "")
            try:
                hostname = urllib.parse.urlsplit("http://" + host).hostname
            except ValueError:
                return False
            if (
                backend != "sqlite"
                or hostname not in ("127.0.0.1", "localhost", "::1")
                or not ipaddress.ip_address(self.client_address[0]).is_loopback
            ):
                return False
            origin = self.headers.get("Origin")
            if origin:
                try:
                    parsed = urllib.parse.urlsplit(origin)
                except ValueError:
                    return False
                if (
                    parsed.scheme not in ("http", "https")
                    or parsed.netloc != host
                    or parsed.path not in ("", "/")
                    or parsed.query
                    or parsed.fragment
                ):
                    return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                return False
            return True

        def do_POST(self):
            cpu = time.thread_time()
            wall = time.monotonic()
            try:
                if urllib.parse.urlsplit(self.path).path == "/api/delivery/costs/refresh":
                    if not self.local_project_request():
                        self.send_error(403)
                        return
                    if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        self.send_error(415)
                        return
                    try:
                        size = int(self.headers.get("Content-Length", "0"))
                        if not 0 <= size <= 100:
                            raise ValueError()
                        self.rfile.read(size)
                        from . import costs_bridge

                        body = json.dumps(costs_bridge.request_refresh()).encode()
                    except Exception:
                        self.send_error(503, "Cost refresh unavailable")
                        return
                    self.send_response(202)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if urllib.parse.urlsplit(self.path).path == "/api/billing":
                    if not self.local_project_request():
                        self.send_error(403)
                        return
                    if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        self.send_error(415)
                        return
                    try:
                        size = int(self.headers.get("Content-Length", "0"))
                        if not 0 < size <= 65536:
                            raise ValueError()
                        value = json.loads(self.rfile.read(size))
                        from . import accounting, collect

                        with contextlib.closing(store.connect()) as db:
                            count = accounting.import_metadata(db, value, collect.machine_name())
                        reports.invalidate_report_replies()
                        body = json.dumps(
                            dict(
                                imported=count,
                                status="Recorded locally; approved metadata sync and reporting publication follow in the background",
                            )
                        ).encode()
                    except (ValueError, TypeError):
                        self.send_error(400, "Invalid billing metadata")
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if urllib.parse.urlsplit(self.path).path != "/api/projects":
                    self.send_error(404)
                    return
                if not self.local_project_request():
                    self.send_error(403, "Project administration is local only")
                    return
                if (
                    self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    != "application/json"
                ):
                    self.send_error(415, "JSON required")
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 65536:
                        raise ValueError("Invalid request size")
                    value = json.loads(self.rfile.read(size).decode("utf-8"))
                except (ValueError, UnicodeError):
                    self.send_error(400, "Invalid project request")
                    return
                from . import projects

                try:
                    result = reports.project_change(value)
                except projects.RevisionConflict:
                    self.send_error(409, "Project configuration changed; refresh and preview again")
                    return
                except (ValueError, TypeError):
                    self.send_error(400, "Invalid project change")
                    return
                except Exception:
                    self.send_error(503, "Project metadata unavailable")
                    return
                body = json.dumps(result, allow_nan=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            finally:
                reports.record_request(self.path, time.thread_time() - cpu, time.monotonic() - wall)

        def get_response(self):
            u = urllib.parse.urlparse(self.path)
            selected = reports
            nonlocal cloud_reports
            query = urllib.parse.parse_qs(u.query, keep_blank_values=True)
            choice = query.pop("report_backend", None)
            if choice:
                if choice != ["databricks"] or not self.local_project_request():
                    self.send_error(403)
                    return
                try:
                    with cloud_lock:
                        if cloud_reports is None:
                            from . import cloud_dashboard

                            cloud_reports = cloud_dashboard.start()
                        selected = cloud_reports
                except Exception:
                    self.send_error(503, "Databricks report cache unavailable")
                    return
                u = u._replace(query=urllib.parse.urlencode(query, doseq=True))
            if (
                backend == "databricks"
                and os.environ.get("ERG_AUTH_MODE") == "user"
                and u.path.startswith("/api/")
            ):
                token = self.headers.get("x-forwarded-access-token")
                if not token:
                    self.send_error(401, "Databricks user authorization is required")
                    return
                # Request-scoped token and cache: never share one reader's data with another.
                selected = Reports(backend, None, warehouse, namespace, access_token=token)
            if u.path == "/healthz":
                from . import overhead

                body = json.dumps(
                    dict(
                        status="ok",
                        instance=os.environ.get("ERG_REPORTER_INSTANCE"),
                        cpu_accounting=overhead.reading(),
                        pid=os.getpid(),
                        request_cpu=reports.request_stats(),
                    )
                ).encode()
                ct = "application/json"
            elif u.path == "/api/reporting/status":
                if backend != "sqlite":
                    self.send_error(404)
                    return
                from . import project_rollups

                with contextlib.closing(selected.read_db()) as db:
                    db.execute("BEGIN")
                    body = json.dumps(
                        dict(
                            project_rollups.status(db),
                            report_source=selected.source_status()
                            if selected.source_status
                            else None,
                        )
                    ).encode()
                    ct = "application/json"
            elif u.path == "/api/delivery/costs":
                if not self.local_project_request():
                    self.send_error(403)
                    return
                from . import costs_bridge

                body = json.dumps(costs_bridge.status()).encode()
                ct = "application/json"
            elif u.path == "/api/delivery/status":
                if not self.local_project_request():
                    self.send_error(403)
                    return
                from . import delivery_service

                body = json.dumps(delivery_service.status()).encode()
                ct = "application/json"
            elif u.path.startswith("/api/cloud/"):
                if not self.local_project_request():
                    self.send_error(403)
                    return
                try:
                    from . import cloud_history

                    args = urllib.parse.parse_qs(u.query)
                    if u.path == "/api/cloud/sessions":
                        result = cloud_history.sessions(args)
                    elif u.path == "/api/cloud/events":
                        result = cloud_history.events(args)
                    else:
                        self.send_error(404)
                        return
                    body = json.dumps(result).encode()
                    ct = "application/json"
                except ValueError:
                    self.send_error(400, "Invalid Databricks session filter")
                    return
                except Exception:
                    self.send_error(
                        503, "Databricks reader unavailable; check operator login and warehouse"
                    )
                    return
            elif u.path == "/api/capture/status":
                if backend != "sqlite":
                    self.send_error(404)
                    return
                from . import service

                body = json.dumps(service.status()).encode()
                ct = "application/json"
            elif u.path.startswith("/api/history"):
                import ipaddress

                host = self.headers.get("Host", "").split(":")[0]
                if (
                    backend != "sqlite"
                    or host not in ("127.0.0.1", "localhost", "[::1]")
                    or not ipaddress.ip_address(self.client_address[0]).is_loopback
                ):
                    self.send_error(403, "History is local-only")
                    return
                try:
                    from . import history

                    args = urllib.parse.parse_qs(u.query)
                    with contextlib.closing(selected.read_db()) as db:
                        db.execute("BEGIN")
                        if u.path == "/api/history/status":
                            result = history.overview(db)
                        elif u.path == "/api/history/discovery":
                            from . import source_audit

                            result = source_audit.status(db)
                        elif u.path == "/api/history/reconciliation":
                            result = history.reconciliation(db)
                        elif u.path == "/api/history/inferences":
                            result = {
                                "inferences": history.inference_rows(
                                    db, args.get("after", [0])[0], args.get("limit", [100])[0]
                                )
                            }
                        elif u.path == "/api/history/sessions":
                            if args.get("scope") == ["1"]:
                                from . import report_scope

                                if set(args) - report_scope.FIELDS - {
                                    "scope",
                                    "after",
                                    "limit",
                                    "through_seq",
                                } or any(len(v) != 1 for v in args.values()):
                                    raise ValueError("Invalid history scope")
                                result = history.scoped_sessions(
                                    db,
                                    {k: v[0] for k, v in args.items() if k in report_scope.FIELDS},
                                    **{
                                        k: v[0]
                                        for k, v in args.items()
                                        if k in ("after", "limit", "through_seq")
                                    },
                                )
                            else:
                                result = history.sessions(
                                    db,
                                    **{
                                        k: v[0]
                                        for k, v in args.items()
                                        if k
                                        in (
                                            "tool",
                                            "session",
                                            "project",
                                            "start",
                                            "end",
                                            "after",
                                            "limit",
                                        )
                                    },
                                )
                        elif u.path == "/api/history/events":
                            result = history.events(
                                db,
                                args.get("tool", [""])[0],
                                args.get("session", [""])[0],
                                args.get("after", [0])[0],
                                args.get("limit", [50])[0],
                            )
                        else:
                            self.send_error(404)
                            return
                    body = json.dumps(result, allow_nan=False).encode()
                    ct = "application/json"
                except (ValueError, TypeError):
                    self.send_error(400, "Invalid history filters")
                    return
                except Exception:
                    self.send_error(503, "History unavailable")
                    return
            elif u.path == "/api/money":
                try:
                    from . import monetary, report_scope

                    args = urllib.parse.parse_qs(u.query, keep_blank_values=True)
                    if any(len(v) != 1 for v in args.values()):
                        raise ValueError()
                    selection = report_scope.Selection(
                        {k: v[0] for k, v in args.items()}, store.my_identities()
                    )
                    with contextlib.closing(selected.read_db()) as db:
                        db.execute("BEGIN")
                        result = monetary.report(db, selection)
                    body = json.dumps(result, allow_nan=False).encode()
                    ct = "application/json"
                except (ValueError, TypeError):
                    self.send_error(400, "Invalid accounting scope")
                    return
            elif u.path == "/api/projects":
                if not self.local_project_request():
                    self.send_error(403, "Project administration is local only")
                    return
                from . import projects

                try:
                    with contextlib.closing(store.open_readonly(store.DB_PATH)) as db:
                        body = json.dumps(projects.snapshot(db), allow_nan=False).encode()
                    ct = "application/json"
                except Exception:
                    self.send_error(503, "Project metadata unavailable")
                    return
            elif u.path == "/api/costs":
                try:
                    args = urllib.parse.parse_qs(u.query, keep_blank_values=True)
                    if set(args) - {
                        "start",
                        "end",
                        "machine",
                        "tool",
                        "session",
                        "project_key",
                        "folder_key",
                        "tools",
                        "kinds",
                        "requester",
                    } or any(len(v) != 1 for v in args.values()):
                        raise ValueError()
                    filters = {
                        k: (int(v[0]) if k in ("start", "end") else v[0])
                        for k, v in args.items()
                        if v[0] != ""
                    }
                    body = json.dumps(selected.costs(**filters), allow_nan=False).encode()
                    ct = "application/json"
                except (ValueError, TypeError):
                    self.send_error(400, "Invalid cost filters")
                    return
                except Exception:
                    self.send_error(503, "Project costs unavailable")
                    return
            elif u.path == "/api/effectiveness":
                from . import report_scope, report_views

                args = urllib.parse.parse_qs(u.query, keep_blank_values=True)
                view = args.get("view", ["full"])[0]
                if view not in report_views.VIEWS:
                    self.send_error(400, "Invalid work report view")
                    return
                try:
                    if set(args) - report_scope.FIELDS - {"view", "refresh"} or any(
                        len(v) != 1 for v in args.values()
                    ):
                        raise ValueError("Invalid work filters")
                    filters = {k: v[0] for k, v in args.items() if k in report_scope.FIELDS}
                    if filters and view not in ("work", "accounts"):
                        raise ValueError("Scope applies to work and accounts")
                    if filters:
                        report_scope.Selection(filters)
                except (ValueError, TypeError):
                    self.send_error(400, "Invalid work filters")
                    return
                try:
                    body = (
                        selected.encoded_scoped_work(
                            filters, refresh=args.get("refresh") == ["1"], view=view
                        )
                        if filters
                        else selected.encoded_effectiveness(
                            refresh=args.get("refresh") == ["1"], view=view
                        )
                    )
                    ct = "application/json"
                except Exception:
                    self.send_error(503, "PR evidence unavailable")
                    return
            elif u.path == "/api/session":
                try:
                    args = urllib.parse.parse_qs(u.query)
                    body = json.dumps(
                        selected.session(args.get("tool", [""])[0], args.get("session", [""])[0])
                    ).encode()
                    ct = "application/json"
                except ValueError:
                    self.send_error(400, "tool and session are required")
                    return
                except Exception:
                    self.send_error(503, "Session backend unavailable")
                    return
            elif u.path == "/api/data":
                try:
                    days = int(urllib.parse.parse_qs(u.query).get("days", ["90"])[0])
                    if days < 0 or days > 3660:
                        raise ValueError()
                    body = selected.encoded_data(
                        days, refresh=urllib.parse.parse_qs(u.query).get("refresh") == ["1"]
                    )
                    ct = "application/json"
                except (ValueError, TypeError):
                    self.send_error(400, "days must be between 0 and 3660")
                    return
                except Exception:
                    self.send_error(
                        503, "Report unavailable; check backend connectivity and permissions."
                    )
                    return
            elif u.path == "/databricks/traces":
                if not self.local_project_request():
                    self.send_error(403)
                    return
                with open(os.path.join(STATIC, "databricks.html"), "rb") as f:
                    body = f.read()
                ct = "text/html; charset=utf-8"
            elif u.path in ("/", "/index.html", "/databricks"):
                with open(os.path.join(STATIC, "index.html"), "rb") as f:
                    body = f.read()
                ct = "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    print("erg reporter: http://%s:%s (%s)" % (bind, port, backend), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
