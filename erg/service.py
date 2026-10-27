"""User service, durable Pi hook setup and independent fast/full local capture."""

import contextlib
import io
import json
import os
import plistlib
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import collect, store

LABEL = "dev.ddx.erg.collector"
DISCOVERY_ENV = (
    "ERG_DISCOVERY_FILE",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
    "PI_CODING_AGENT_DIR",
    "PI_CODING_AGENT_SESSION_DIR",
    "OMNIGENT_DATA_DIR",
)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + ".%d.tmp" % os.getpid())
    try:
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(temp), str(path))
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()


def status():
    snapshots = list(Path(collect.STATE_DIR, "pi-snapshots").glob("*.jsonl"))
    common = {
        "pi_hook_installed": Path(collect.STATE_DIR, "hooks", "erg-pi.mjs").is_file(),
        "pi_snapshots": len(snapshots),
    }
    try:
        with open(os.path.join(collect.STATE_DIR, "collector-status.json")) as f:
            value = json.load(f)
    except (OSError, ValueError):
        return dict(common, running=False, state="not_started")
    fresh = time.time() - value.get("heartbeat", 0) < max(15, value.get("poll", 2) * 5)
    value["running"] = bool(fresh and not value.get("stopped"))
    # PR enrichment is independent of durable agent capture. Keep both visible.
    capture_keys = (
        ("fast", "full", "sample", "context", "usage", "account_probe")
        + (
            ("history", "discovery")
            if value.get("source_discovery_enabled")
            else ("history",)
            if value.get("history_enabled")
            else ()
        )
        + (("endpoint",) if value.get("endpoints_enabled") else ())
    )
    budgets = dict(
        fast=max(15, value.get("poll", 2) * 5),
        full=value.get("interval", 900) + 660,
        sample=90,
        context=120,
        usage=120,
        account_probe=420,
        history=120,
        discovery=660,
        endpoint=15,
    )
    workers = {}
    now = time.time()
    for name in capture_keys:
        last = value.get("last_" + name)
        error = value.get("last_" + name + "_error")
        age = now - (last or value.get("started") or now)
        state = (
            "error"
            if error
            else "stale"
            if age > budgets[name]
            else "running"
            if last
            else "starting"
        )
        workers[name] = dict(state=state, last_success=last, error=error)
    value["workers"] = workers
    capture_failed = any(w["state"] in ("error", "stale") for w in workers.values())
    value["capture_state"] = (
        ("degraded" if capture_failed else "running") if value["running"] else "stopped_or_stale"
    )
    pr_error = value.get("last_pr_error")
    value["enrichment_state"] = (
        "degraded"
        if pr_error
        else "deferred"
        if value.get("enrichment_deferred_reason")
        else "running"
        if value.get("last_pr_refresh")
        else "not_observed"
    )
    value["state"] = (
        ("degraded" if capture_failed or pr_error else "running")
        if value["running"]
        else "stopped_or_stale"
    )
    return dict(value, **common)


def fast_capture(db, since, state=None, include_pi=True):
    """Never block on a broad repository scan; native IDs make replays harmless."""
    state = {} if state is None else state
    full = not state or time.monotonic() - state.get("full_at", 0) > 300
    native = state.setdefault("native", {})
    fields = ["machine"] + store.SESSION_COLS
    query = (
        "SELECT "
        + ",".join(fields)
        + " FROM sessions WHERE machine=? AND COALESCE(provenance,'')<>'omnigent_metadata'"
    )
    params = [collect.machine_name()]
    if not full:
        query += " AND last_ts>=?"
        params.append(max(0, since - 2))
    for row in db.execute(query, params):
        item = dict(zip(fields, row))
        native[(item["tool"], item["session"])] = dict(item, t="session")
    if full:
        state["full_at"] = time.monotonic()
    output = io.StringIO()
    if include_pi:
        with contextlib.redirect_stdout(output):
            collect.collect_pi(since)
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    for row in rows:
        if row["t"] == "session":
            native[(row["tool"], row["session"])] = row
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        collect.collect_omnigent(0, native, state=state.setdefault("omnigent", {}))
    rows += [json.loads(line) for line in output.getvalue().splitlines()]
    # No recently modified Pi file is different from no history at all.
    rows = [
        r
        for r in rows
        if not (r["t"] == "source" and r["tool"] == "pi" and not r["files"] and not r["errors"])
    ]
    rows.insert(
        0,
        dict(
            t="hello",
            machine=collect.machine_name(),
            os=collect.platform.system(),
            ncpu=os.cpu_count(),
        ),
    )
    result = store.ingest(db, map(json.dumps, rows), machine=collect.machine_name())
    from . import routes

    routes.capture_wrappers(db)
    return result


def native_usage_capture(db, since):
    # A subprocess isolates collector stdout from the concurrent Pi capture thread.
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(Path(__file__).with_name("collect.py")),
            "native-usage",
            "--since",
            str(since),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        raise RuntimeError("native usage capture failed")
    rows = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    errors = sum(r.get("errors", 0) for r in rows if r.get("t") == "source")
    store.ingest(
        db,
        map(json.dumps, [r for r in rows if r.get("t") not in ("source", "cwd")]),
        machine=collect.machine_name(),
    )
    return errors


def defer_optional_enrichment(info, retained):
    """Prioritize durable forward projection over repeatable Git/host history.

    Legacy source adapters and the live host sampler keep their normal cadence.
    Missing progress during startup is not evidence of spare catch-up capacity.
    """
    if not retained:
        return False
    usage = info.get("usage_backlog")
    work = (info.get("work_projection") or {}).get("pending")
    return usage is None or work is None or usage > 1000 or work > 10000


def full_command(since, retained, deferred=False):
    cmd = [
        sys.executable,
        "-B",
        "-m",
        "erg.collect",
        "collect",
        "--days",
        "2",
        "--perf-since",
        str(since),
        "--roots",
        "~/Projects",
    ]
    if retained:
        cmd += ["--retained", "--retained-database", str(store.DB_PATH)]
    if retained and deferred:
        cmd += ["--no-git", "--no-metrics"]
    return cmd


def run(interval=900, poll=2):
    if poll < 1 or interval < poll:
        raise ValueError("require interval >= poll >= 1 second")
    from . import file_events, history, overhead

    cpu = overhead.WorkerCPU(
        (
            "full_enrichment",
            "pr_refresh",
            "context_scan",
            "title_refresh",
            "profile_refresh",
            "history_scan",
            "work_projection",
            "usage_projection",
            "edit_projection",
            "host_sample",
            "fast_capture",
            "metric_rollups",
            "call_projection",
            "project_rollups",
            "history_browse",
        )
    )
    feed = file_events.Feed()
    content_enabled = bool(history.config().get("content"))
    stop = threading.Event()
    guard = threading.Lock()
    info = {
        "pid": os.getpid(),
        "started": time.time(),
        "poll": poll,
        "interval": interval,
        "last_fast": None,
        "last_full": None,
        "last_sample": None,
        "last_fast_error": None,
        "last_full_error": None,
        "last_sample_error": None,
    }

    def update(**values):
        with guard:
            info.update(values)

    def full_scan():
        if content_enabled:
            update(full_mode="periodic_retained_enrichment", next_full_at=time.time() + interval)
            if stop.wait(interval):
                return
        while not stop.is_set():
            proc = None
            try:
                with cpu.measure("full_enrichment"):
                    with contextlib.closing(
                        store.connect(initialize=False, autocheckpoint=False)
                    ) as db:
                        since = store.max_perf_ts(db, collect.machine_name())
                    with guard:
                        deferred = defer_optional_enrichment(info, content_enabled)
                    update(enrichment_deferred_reason="projection_backlog" if deferred else None)
                    cmd = full_command(since, content_enabled, deferred)
                    proc = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
                    )
                    deadline = time.monotonic() + 600
                    while True:
                        try:
                            output, error = proc.communicate(timeout=1)
                            break
                        except subprocess.TimeoutExpired:
                            if stop.is_set() or time.monotonic() > deadline:
                                proc.terminate()
                                try:
                                    proc.communicate(timeout=5)
                                except subprocess.TimeoutExpired:
                                    proc.kill()
                                    proc.communicate()
                                raise RuntimeError("full capture interrupted or timed out")
                    if proc.returncode:
                        raise RuntimeError("full capture failed")
                    with contextlib.closing(
                        store.connect(initialize=False, autocheckpoint=False)
                    ) as db:
                        store.ingest(db, output.splitlines())
                update(last_full=time.time(), last_full_error=None)
                try:
                    from . import work

                    config = work.refresh_configuration() if not deferred else None
                    if config:
                        with contextlib.closing(
                            store.connect(initialize=False, autocheckpoint=False)
                        ) as db:
                            result = cpu.call(
                                "pr_refresh",
                                work.refresh,
                                db,
                                config["repos"],
                                config.get("limit", 10),
                            )
                        update(
                            last_pr_refresh=time.time(),
                            last_pr_error="metadata_fetch_failed" if result["errors"] else None,
                            last_pr_errors=result["errors"],
                            last_pr_updated=result["prs"],
                            last_pr_unavailable=result.get("unavailable", []),
                        )
                except Exception as error:
                    update(
                        last_pr_error=type(error).__name__, last_pr_errors=[type(error).__name__]
                    )
            except Exception as error:
                update(last_full_error=type(error).__name__)
                print(
                    "erg full capture failed (%s); fast capture continues" % type(error).__name__,
                    file=sys.stderr,
                    flush=True,
                )
            finally:
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.communicate()
            with guard:
                failed = info["last_full_error"] is not None
            delay = min(60, interval) if failed else interval
            update(next_full_at=time.time() + delay)
            stop.wait(delay)

    def context_loop():
        from . import titles, work

        usage_since = time.time() - 60
        last_usage = last_metadata = last_titles = last_reconcile = float("-inf")
        title_cache = {}
        with contextlib.closing(store.connect(initialize=False, autocheckpoint=False)) as db:
            while not stop.is_set():
                try:
                    paths, rescan = feed.drain("context")
                    now = time.monotonic()
                    reconcile = rescan or now - last_reconcile >= 300 or feed.native is None
                    selected = []
                    for path in paths:
                        item = feed.sources.classify(path)
                        if item and item[0] in ("claude", "codex") and os.path.isfile(item[1]):
                            selected.append(item)
                    metadata = now - last_metadata >= 300
                    if reconcile or selected or metadata:
                        outcome = cpu.call(
                            "context_scan",
                            work.scan,
                            db,
                            paths=None if reconcile else selected,
                            metadata=metadata,
                        )
                        if metadata:
                            last_metadata = now
                        if reconcile:
                            last_reconcile = now
                        update(last_context_error="source_errors" if outcome["errors"] else None)
                        if outcome["errors"]:
                            feed.mailbox.retry("context", [path for tool, path in selected])
                            if reconcile:
                                feed.mailbox.request_rescan()
                    if now - last_titles >= 60:
                        title_result = cpu.call(
                            "title_refresh", titles.refresh, db, cache=title_cache
                        )
                        last_titles = now
                        update(
                            last_title_refresh=time.time(),
                            last_title_error="source_errors" if title_result["errors"] else None,
                            title_scan=title_result,
                        )
                    update(
                        last_context=time.time(),
                        context_idle=not (selected or reconcile or metadata),
                    )
                    if content_enabled:
                        update(usage_mode="retained_corpus_projection")
                    elif now - last_usage >= 30:
                        started = time.time()
                        errors = native_usage_capture(db, max(0, usage_since - 2))
                        usage_since = started
                        last_usage = now
                        update(
                            last_usage=time.time(),
                            last_usage_error="source_errors" if errors else None,
                            usage_mode="legacy_native_poll",
                        )
                except Exception as error:
                    feed.mailbox.retry("context", paths)
                    if reconcile:
                        feed.mailbox.request_rescan()
                    update(last_context_error=type(error).__name__)
                stop.wait(poll)

    def profile_loop():
        from . import account_probe, routes

        while not stop.is_set():
            try:
                with cpu.measure("profile_refresh"):
                    routes.profiles()
                    with contextlib.closing(
                        store.connect(initialize=False, autocheckpoint=False)
                    ) as db:
                        routes.backfill_once(db)
                        captured = account_probe.poll(db)
                update(
                    last_account_probe=time.time(),
                    last_account_probe_error=None,
                    account_profiles_observed=captured,
                    account_probe_stats=getattr(account_probe.poll, "last_stats", None),
                )
            except Exception as error:
                update(last_account_probe_error=type(error).__name__)
            stop.wait(300)

    def history_loop():
        update(history_enabled=content_enabled, source_discovery_enabled=content_enabled)
        last_reconcile = float("-inf")
        with contextlib.closing(
            store.connect(initialize=False, autocheckpoint=False)
        ) as db, contextlib.closing(
            store.connect(initialize=False, autocheckpoint=False)
        ) as maintenance_db:
            while not stop.is_set():
                raw = []
                try:
                    raw, rescan = feed.drain("history")
                    now = time.monotonic()
                    if any(
                        p.endswith("/state.json")
                        and ("/claude-native/" in p or "/codex-native/" in p)
                        for p in raw
                    ):
                        feed.refresh()
                    reconcile = rescan or now - last_reconcile >= 300 or feed.native is None
                    selected = []
                    sqlite_changed = False
                    for path in raw:
                        item = feed.sources.classify(path)
                        if item is None and path.endswith((".jsonl", ".jsonl.zst")):
                            feed.sources.refresh()
                            item = feed.sources.classify(path)
                        if item and os.path.isfile(item[1]):
                            selected.append(item)
                        if Path(path).name in ("opencode.db", "opencode.db-wal"):
                            sqlite_changed = True
                    from . import file_events

                    active = file_events.active_sources(db, feed.sources)
                    selected.extend(active)
                    pending = db.execute(
                        "SELECT tool,path FROM history_sources WHERE state='pending' LIMIT 500"
                    ).fetchall()
                    selected.extend(
                        (tool, path)
                        for tool, path in pending
                        if tool != "opencode" and os.path.isfile(path)
                    )
                    sqlite_changed |= any(tool == "opencode" for tool, path in pending)
                    selected = list(dict.fromkeys(selected))
                    excess = selected[500:]
                    selected = selected[:500]
                    if reconcile:
                        feed.refresh()
                    result = cpu.call(
                        "history_scan",
                        history.scan,
                        db,
                        paths=None if reconcile else selected,
                        scan_sqlite=True,
                        projection_budget=0.06,
                        project_work=False,
                        project_usage=False,
                    )
                    deferred = result.pop("deferred_paths", []) + excess
                    if deferred:
                        feed.mailbox.retry("history", [path for tool, path in deferred])
                    if result.get("errors"):
                        feed.mailbox.retry("history", [path for tool, path in selected])
                        if reconcile:
                            feed.mailbox.request_rescan()
                    if reconcile:
                        last_reconcile = now
                        inventory = result.get("source_inventory") or {}
                        update(
                            last_discovery=time.time(),
                            last_discovery_error="source_discovery_gaps"
                            if inventory.get("error_paths")
                            else None,
                            discovery_inventory={
                                k: inventory.get(k)
                                for k in (
                                    "current_state",
                                    "observed_at",
                                    "inventory_paths",
                                    "checked_paths",
                                    "pending_paths",
                                    "error_paths",
                                )
                            },
                        )
                    update(
                        last_history=time.time(),
                        last_history_error="source_errors"
                        if result.get("errors") or result.get("source_error_count")
                        else None,
                        history_scan=result,
                        history_enabled=result["enabled"],
                        active_source_checks=len(active),
                        active_source_limit=file_events.ACTIVE_SOURCE_LIMIT,
                        file_events=feed.status(),
                    )
                except Exception as error:
                    db.rollback()
                    feed.mailbox.retry("history", raw)
                    if reconcile:
                        feed.mailbox.request_rescan()
                    import traceback

                    frames = traceback.extract_tb(error.__traceback__, limit=5)
                    update(
                        last_history_error=type(error).__name__,
                        last_history_error_frames=[
                            dict(function=f.name, line=f.lineno) for f in frames
                        ],
                    )
                try:
                    from . import metric_rollups

                    outcome = cpu.call("metric_rollups", metric_rollups.run, maintenance_db)
                    update(metric_rollup_cache=outcome, last_metric_rollup_error=None)
                except Exception as error:
                    # Derived reporting can fall back to raw data; maintenance
                    # errors cannot retract or stop durable trace capture.
                    update(last_metric_rollup_error=type(error).__name__)
                try:
                    from . import call_projection

                    outcome = cpu.call("call_projection", call_projection.maintain, maintenance_db)
                    update(
                        call_projection_cache=outcome,
                        last_call_projection_error=outcome.get("error"),
                    )
                except Exception as error:
                    maintenance_db.rollback()
                    update(last_call_projection_error=type(error).__name__)
                stop.wait(poll)

    def sample_loop():
        while not stop.is_set():
            try:
                cpu.call("host_sample", collect.take_sample)
                update(last_sample=time.time(), last_sample_error=None)
            except Exception as error:
                update(last_sample_error=type(error).__name__)
            stop.wait(30)

    def shutdown(*_):
        stop.set()

    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, shutdown)
    from . import history, local_proxy

    proxies = []
    proxy_threads = []
    update(endpoints_enabled=bool(history.config().get("endpoints")))
    for endpoint in history.config().get("endpoints", []):
        proxy = local_proxy.make_server(endpoint["upstream"], endpoint["port"])
        proxies.append(proxy)
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        proxy_threads.append(thread)

    def history_browse_loop():
        if not content_enabled:
            return
        from . import history_browse

        while not stop.is_set():
            outcome = {}
            try:
                outcome = cpu.call("history_browse", history_browse.run, store.DB_PATH, max_hours=1)
                update(history_browse_cache=outcome, last_history_browse_error=None)
            except Exception as error:
                update(last_history_browse_error=type(error).__name__)
            stop.wait(0.1 if outcome.get("pending_closed_hours", 0) else 15)

    def project_rollup_loop():
        from . import project_rollups

        project_rollups.run_worker(
            store.DB_PATH,
            stop,
            lambda result: update(project_rollup_cache=result),
            measure=cpu.call,
        )

    def work_projection_loop():
        if not content_enabled:
            return
        from . import work_projection

        def progress(result):
            update(
                work_projection=result,
                last_work_projection=time.time(),
                last_work_projection_error=result.get("error")
                or ("projection_errors" if result.get("errors") else None),
            )
            usage = result.get("usage_projection")
            edits = result.get("edit_projection")
            if usage is not None:
                update(
                    last_usage=time.time(),
                    last_usage_error="projection_errors" if usage.get("errors") else None,
                    usage_backlog=usage["pending"],
                    usage_mode="retained_corpus_projection",
                )
            if edits is not None:
                update(
                    edit_projection_backlog=edits["pending"],
                    last_edit_projection_error="projection_errors" if edits.get("errors") else None,
                )
            if result.get("error_stage") == "usage":
                update(last_usage_error=result["error"])
            if result.get("error_stage") == "edits":
                update(last_edit_projection_error=result["error"])

        work_projection.run_worker(
            store.DB_PATH, stop, progress, measure=cpu.call, poll=poll, include_usage=True
        )

    work_projector = threading.Thread(target=work_projection_loop, daemon=True)
    project_worker = threading.Thread(target=project_rollup_loop, daemon=True)
    historian = threading.Thread(target=history_loop, daemon=True)
    browse_worker = threading.Thread(target=history_browse_loop, daemon=True)
    worker = threading.Thread(target=full_scan, daemon=True)
    sampler = threading.Thread(target=sample_loop, daemon=True)
    contexts = threading.Thread(target=context_loop, daemon=True)
    profiles = threading.Thread(target=profile_loop, daemon=True)
    try:
        with collect.collector_lock():
            fast_db = store.connect(autocheckpoint=False)
            fast_state = {}
            worker.start()
            sampler.start()
            contexts.start()
            profiles.start()
            historian.start()
            work_projector.start()
            project_worker.start()
            browse_worker.start()
            since = 0
            try:
                while not stop.is_set():
                    started = time.time()
                    if proxies:
                        update(
                            last_endpoint=started,
                            last_endpoint_error=None
                            if all(t.is_alive() for t in proxy_threads)
                            else "listener_stopped",
                        )
                    try:
                        changed, rescan = feed.drain("fast")
                        pi_changed = any(
                            (feed.sources.classify(p) or ("", ""))[0] == "pi" for p in changed
                        )
                        omni = collect.omni_home() + os.sep
                        metadata_changed = any(
                            p.startswith(omni)
                            and (
                                p.endswith((".json", ".db", ".db-wal"))
                                or Path(p).name == "state.json"
                            )
                            for p in changed
                        )
                        due = (
                            since == 0
                            or rescan
                            or pi_changed
                            or metadata_changed
                            or time.time() - since >= 300
                            or feed.native is None
                        )
                        if due:
                            cpu.call(
                                "fast_capture",
                                fast_capture,
                                fast_db,
                                max(0, since - 2),
                                state=fast_state,
                                include_pi=not content_enabled
                                and (pi_changed or since == 0 or rescan),
                            )
                            since = started
                        update(
                            last_fast=time.time(),
                            last_fast_error=None,
                            fast_idle=not due,
                            omnigent_metadata_cache={
                                k: fast_state.get("omnigent", {}).get(k, 0)
                                for k in ("cache_hits", "cache_misses", "cached_records")
                            },
                        )
                    except Exception as error:
                        update(last_fast_error=type(error).__name__)
                        print(
                            "erg fast capture failed (%s); retrying" % type(error).__name__,
                            file=sys.stderr,
                            flush=True,
                        )
                    with guard:
                        snapshot = dict(info, heartbeat=time.time())
                    from . import overhead

                    snapshot["cpu_accounting"] = overhead.reading()
                    snapshot["worker_cpu"] = cpu.snapshot()
                    atomic_json(os.path.join(collect.STATE_DIR, "collector-status.json"), snapshot)
                    stop.wait(poll)
            finally:
                fast_db.close()
                stop.set()
                for proxy in proxies:
                    proxy.shutdown()
                    proxy.server_close()
                historian.join(2)
                work_projector.join(2)
                worker.join(3)
                sampler.join(1)
                contexts.join(1)
                profiles.join(1)
                project_worker.join(2)
                with guard:
                    snapshot = dict(info, heartbeat=time.time(), stopped=True)
                atomic_json(os.path.join(collect.STATE_DIR, "collector-status.json"), snapshot)
    finally:
        feed.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def install_pi_hook(state):
    """Explicit global extension paths are inherited by Omnigent managed Pi dirs."""
    state = Path(state)
    loader = state / "hooks" / "erg-pi.mjs"
    loader.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    source = state / "runtime" / "erg" / "hooks" / "pi.mjs"
    loader.write_text(
        "import {install} from "
        + json.dumps(source.as_uri())
        + ";\nexport default pi => install(pi,{stateDir:"
        + json.dumps(str(state))
        + "});\n"
    )
    os.chmod(str(loader), 0o600)
    agent = Path(
        os.environ.get("PI_CODING_AGENT_DIR") or os.path.join(collect.HOME, ".pi", "agent")
    )
    paths = list(
        dict.fromkeys(
            [agent / "settings.json", Path(collect.HOME) / ".pi" / "agent" / "settings.json"]
        )
    ) + list(Path(collect.omni_home()).glob("pi-native/*/pi-agent/settings.json"))
    for path in paths:
        try:
            settings = json.loads(path.read_text())
        except FileNotFoundError:
            settings = {}
        if not isinstance(settings, dict) or not isinstance(settings.get("extensions", []), list):
            raise ValueError("Pi settings must have an extensions list")
        extensions = settings.setdefault("extensions", [])
        if str(loader) not in extensions:
            if path.exists():
                backup = path.with_name(path.name + ".erg-backup-" + str(time.time_ns()))
                shutil.copy2(str(path), str(backup))
                os.chmod(str(backup), 0o600)
            extensions.append(str(loader))
            atomic_json(path, settings)
    return str(loader)


def install_claude_identity_hook(state):
    directory = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path(collect.HOME) / ".claude")
    path = directory / "settings.json"
    try:
        settings = json.loads(path.read_text())
    except FileNotFoundError:
        settings = {}
    if not isinstance(settings, dict):
        raise ValueError("Claude settings must be an object")
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Claude hooks must be an object")
    entries = hooks.setdefault("SessionStart", [])
    if not isinstance(entries, list):
        raise ValueError("Claude SessionStart hooks must be a list")
    command = (
        "PYTHONPATH="
        + shlex.quote(str(state / "runtime"))
        + " "
        + shlex.quote(sys.executable)
        + " -B -m erg.cli accounts capture-hook --tool claude"
    )
    existing = [
        h
        for e in entries
        if isinstance(e, dict)
        for h in e.get("hooks", [])
        if isinstance(h, dict) and h.get("command") == command
    ]
    changed = not existing or any(h.get("async") or h.get("timeout") != 8 for h in existing)
    if changed:
        if path.exists():
            backup = path.with_name(path.name + ".erg-backup-" + str(time.time_ns()))
            shutil.copy2(str(path), str(backup))
            os.chmod(str(backup), 0o600)
        if existing:
            for h in existing:
                h.pop("async", None)
                h["timeout"] = 8
        else:
            entries.append(
                dict(
                    matcher="startup|resume|clear|compact|fork",
                    hooks=[dict(type="command", command=command, timeout=8)],
                )
            )
        atomic_json(path, settings)
    return str(path)


def bootstrap(domain, plist):
    # launchd may return before a previous bootout finishes tearing down its job.
    for attempt in range(5):
        result = subprocess.run(["launchctl", "bootstrap", domain, str(plist)], capture_output=True)
        if result.returncode == 0:
            return
        if result.returncode != 5 or attempt == 4:
            raise RuntimeError("launchd could not start service (exit %s)" % result.returncode)
        time.sleep(0.5 * (attempt + 1))


def install_cli(state, runtime, database, source_env=None):
    """Private absolute entrypoint for cron; does not alter the shell PATH."""
    state = Path(state).resolve()
    runtime = Path(runtime).resolve()
    database = Path(database).resolve()
    folder = state / "bin"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / "erg"
    lines = ["#!/bin/sh"]
    defaults = dict(ERG_STATE_DIR=str(state), ERG_DATABASE=str(database))
    defaults.update(
        {key: source_env[key] for key in DISCOVERY_ENV if source_env and source_env.get(key)}
    )
    for key, value in defaults.items():
        lines.append(
            'if [ -z "${' + key + ':-}" ]; then export ' + key + "=" + shlex.quote(value) + "; fi"
        )
    lines.append("export PYTHONPATH=" + shlex.quote(str(runtime)) + '"${PYTHONPATH:+:$PYTHONPATH}"')
    bootstrap = (
        "import sys; sys.path.insert(0,"
        + repr(str(runtime))
        + "); from erg.cli import main; main()"
    )
    lines.append(
        "exec " + shlex.quote(sys.executable) + " -B -c " + shlex.quote(bootstrap) + ' "$@"'
    )
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=folder, prefix=".erg-", delete=False
        ) as stream:
            temporary = stream.name
            stream.write("\n".join(lines) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o700)
        os.replace(temporary, path)
        temporary = None
        directory = os.open(str(folder), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
    return str(path)


def install(interval=900, poll=2):
    if poll < 1 or interval < poll:
        raise ValueError("require interval >= poll >= 1 second")
    if sys.platform != "darwin":
        raise RuntimeError("automatic service installation currently supports macOS launchd")
    state = Path(collect.STATE_DIR).resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime = state / "runtime"
    package = runtime / "erg"
    package.mkdir(parents=True, exist_ok=True, mode=0o700)
    source = Path(__file__).parent
    for item in list(source.glob("*.py")) + list(source.glob("*.json")):
        shutil.copy2(str(item), str(package / item.name))
    from . import rate_catalog, rates

    atomic_json(package / "rate_card.json", rate_catalog.clean(rates.load()))
    for name in ("hooks", "static"):
        shutil.copytree(str(source / name), str(package / name), dirs_exist_ok=True)
    hook = install_pi_hook(state)
    _claude_hook = install_claude_identity_hook(state)
    env = {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": collect.HOME,
        "PYTHONPATH": str(runtime),
        "PYTHONDONTWRITEBYTECODE": "1",
        "ERG_STATE_DIR": str(state),
        "ERG_DATABASE": str(Path(store.DB_PATH).resolve()),
    }
    for name in DISCOVERY_ENV:
        if os.environ.get(name):
            env[name] = os.environ[name]
    cli = install_cli(state, runtime, env["ERG_DATABASE"], env)
    paths = Path(collect.HOME) / "Library" / "LaunchAgents"
    paths.mkdir(parents=True, exist_ok=True)
    plist = paths / (LABEL + ".plist")
    config = {
        "Label": LABEL,
        "ProgramArguments": [
            sys.executable,
            "-B",
            "-u",
            "-m",
            "erg.cli",
            "service",
            "run",
            "--interval",
            str(interval),
            "--poll",
            str(poll),
        ],
        "WorkingDirectory": str(runtime),
        "EnvironmentVariables": env,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ExitTimeOut": 10,
        "Umask": 0o077,
        "ProcessType": "Standard",
        "LowPriorityBackgroundIO": True,
        "StandardOutPath": str(state / "collector.out"),
        "StandardErrorPath": str(state / "collector.err"),
    }
    for name in ("collector.out", "collector.err"):
        fd = os.open(str(state / name), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(fd)
        os.chmod(str(state / name), 0o600)
    with plist.open("wb") as f:
        plistlib.dump(config, f)
    os.chmod(str(plist), 0o600)
    domain = "gui/" + str(os.getuid())
    # The collector owns host sampling; disable only erg's known sampler job.
    old = paths / "dev.ddx.erg.sampler.plist"
    if old.exists():
        with old.open("rb") as f:
            sampler = plistlib.load(f)
        argv = sampler.get("ProgramArguments", [])
        if (
            sampler.get("Label") == "dev.ddx.erg.sampler"
            and str(state / "collect.py") in argv
            and "sample" in argv
        ):
            subprocess.run(
                ["launchctl", "bootout", domain + "/dev.ddx.erg.sampler"], capture_output=True
            )
            old.rename(old.with_suffix(".plist.disabled"))
    subprocess.run(["launchctl", "bootout", domain + "/" + LABEL], capture_output=True)
    bootstrap(domain, plist)
    return {
        "label": LABEL,
        "plist": str(plist),
        "hook": hook,
        "state_dir": str(state),
        "interval": interval,
        "poll": poll,
        "cli": cli,
    }


def stop():
    subprocess.run(
        ["launchctl", "bootout", "gui/" + str(os.getuid()) + "/" + LABEL],
        check=True,
        capture_output=True,
    )
