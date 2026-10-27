"""Independent forward delivery service; collector and UI never perform uploads."""

import contextlib
import datetime
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict

from . import delivery_client as client
from . import delivery_journal as journal
from . import overhead, store

LABEL = "dev.ddx.erg.delivery"


def directory():
    from . import collect

    return Path(collect.STATE_DIR) / "delivery"


def configuration(path=None):
    value = json.loads(Path(path or directory() / "config.json").read_text())
    if value.get("forward_only") is not True or value.get("synthetic_only") is not False:
        raise ValueError("explicit real forward-only configuration required")
    if set(value.get("identity", {})) != set(journal.wire.ENROLLMENT):
        raise ValueError("qualified identity required")
    client.endpoint(value["broker_origin"])
    if not isinstance(value.get("token_helper"), list):
        raise ValueError("native token helper required")
    return value


def source_boundaries():
    from . import collect

    groups = {
        "claude": collect.claude_roots(),
        "codex": collect.native_roots("codex"),
        "pi": collect.pi_roots() + [str(Path(collect.STATE_DIR) / "pi-content-outbox")],
    }
    result = []
    counts = {}
    for tool, roots in groups.items():
        paths = set(collect.log_paths(roots))
        counts[tool] = len(paths)
        for path in paths:
            try:
                s = os.stat(path)
                result.append((str(path), s.st_ino, s.st_size))
            except FileNotFoundError:
                pass
    return list({row[0]: row for row in result}.values()), counts


def enroll(path=None, database=store.DB_PATH):
    cfg = configuration(path)
    floors, inventory = source_boundaries()
    with contextlib.closing(store.connect(database)) as db:
        journal.enroll(db, cfg["identity"], forward_only=True, source_floors=floors)
        result = journal.status(db)
    result.update(retained_replay=False, native_source_inventory=inventory)
    from .service import atomic_json

    proof = directory() / "forward-boundary-proof.json"
    if not proof.exists():
        atomic_json(
            proof,
            dict(
                result,
                recorded_at=time.time(),
                database=str(database),
                initial_journal_empty=result.get("pending_revisions") == 0
                and result.get("pending_items") == 0,
            ),
        )
    return result


class TokenCache:
    def __init__(self, argv):
        self.argv = argv
        self.token = None
        self.until = 0

    def __call__(self):
        if self.token and time.time() < self.until:
            return self.token
        token = client.helper_token(self.argv)
        until = time.time() + 60
        try:
            import base64

            part = token.split(".")[1]
            expires = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))["exp"]
            until = min(time.time() + 600, float(expires) - 60)
        except Exception:
            pass
        self.token = token
        self.until = until
        return token


def status(database=store.DB_PATH):
    try:
        value = json.loads((directory() / "status.json").read_text())
    except (OSError, ValueError):
        value = dict(state="not_running")
    value["worker_state"] = value.get("state")
    value["running"] = time.time() - value.get("heartbeat", 0) < 30
    try:
        with contextlib.closing(store.open_readonly(database)) as db:
            db.execute("BEGIN")
            value.update(journal.status(db))
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='delivery_http_daily'"
            ).fetchone():
                value["upload_usage"] = {
                    "daily": [
                        dict(
                            zip(
                                (
                                    "utc_day",
                                    "attempts",
                                    "accepted_responses",
                                    "offered_body_bytes",
                                    "elapsed_ms",
                                ),
                                r,
                            )
                        )
                        for r in db.execute("SELECT * FROM delivery_http_daily ORDER BY day")
                    ],
                    "unique_daily": [
                        dict(zip(("utc_day", "batches", "items", "payload_bytes"), r))
                        for r in db.execute("SELECT * FROM delivery_unique_daily ORDER BY day")
                    ],
                    "transport_wire_bytes": None,
                    "billed_bytes": None,
                    "scope": "Measured client attempts and unique accepted payloads; not billable quantities",
                }
            if value.get("state") != "not_enrolled":
                row = db.execute(
                    "SELECT j.captured_at FROM delivery_items i JOIN delivery_journal j ON j.id=i.journal_id WHERE i.materialized=0 ORDER BY i.delivery_seq LIMIT 1"
                ).fetchone()
                value["materialization_lag_seconds"] = (
                    max(0, int(time.time()) - row[0]) if row else 0
                )
                value["pending_materialization"] = value.get("accepted_through", 0) - value.get(
                    "materialized_through", 0
                )
    except Exception:
        value["journal_unavailable"] = True
    try:
        cfg = configuration()
        value["enabled"] = cfg.get("enabled") is True
        value["broker_origin"] = cfg["broker_origin"]
        value["certificate_expires_at"] = cfg.get("certificate_expires_at")
        value["central_credential_expires_at"] = cfg.get("central_credential_expires_at")
        value["credential_warnings"] = []
        for key in ("certificate_expires_at", "central_credential_expires_at"):
            if cfg.get(key):
                expiry = datetime.datetime.fromisoformat(
                    cfg[key].replace("Z", "+00:00")
                ).timestamp()
                if expiry - time.time() < 7 * 86400:
                    value["credential_warnings"].append(
                        key + (" expired" if expiry < time.time() else " expires within 7 days")
                    )
        value["automatic_credential_rotation"] = False
    except (OSError, ValueError, KeyError):
        value["enabled"] = False
    if value.get("state") == "not_enrolled":
        label = "Forward uploads not enrolled"
    elif not value.get("enabled"):
        label = "Uploads disabled"
    elif not value.get("running"):
        label = (
            "Uploader stopped"
            if value.get("worker_state") in ("stopped", "not_running")
            else "Uploader heartbeat delayed"
        )
    elif value.get("error") or value.get("blocked_reason") or value.get("blocked_revisions"):
        label = "Sync needs attention"
    else:
        label = "Forward sync running"
    if value.get("reporting_metadata_error") or value.get("reporting_metadata", {}).get("errors"):
        label += " · reporting metadata needs attention"
    try:
        value["reporting_metadata_enabled"] = (
            configuration().get("reporting_metadata_enabled") is True
        )
    except Exception:
        value["reporting_metadata_enabled"] = False
    value["summary"] = (
        label
        + " · "
        + str(value.get("pending_revisions", 0))
        + " revisions awaiting preparation · "
        + str(value.get("pending_items", 0))
        + " prepared items awaiting acceptance · "
        + str(value.get("pending_materialization", 0))
        + " accepted items awaiting Delta"
    )
    return value


def run(path=None, database=store.DB_PATH):
    from .service import atomic_json

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *args: stop.set())
    cfg = configuration(path)
    api = client.HTTPS(cfg["broker_origin"], TokenCache(cfg["token_helper"]))
    state: Dict[str, Any] = dict(pid=os.getpid(), started=time.time(), state="starting")
    last_status = 0
    last_write = 0
    last_cost_check = 0
    cpu = overhead.WorkerCPU(
        ("report_metadata", "prepare", "send", "receipt_refresh", "cost_refresh")
    )
    with contextlib.closing(store.connect(database, initialize=False, autocheckpoint=False)) as db:
        from . import report_delivery

        report_delivery.initialize(db)
        while not stop.is_set():
            now = time.time()
            productive = False
            try:
                cfg = configuration(path)
                if now - last_cost_check >= 60:
                    from . import costs_bridge

                    try:
                        cpu.call("cost_refresh", costs_bridge.request_refresh)
                    except Exception as error:
                        state["cost_refresh_error"] = type(error).__name__
                    last_cost_check = now
                if cfg.get("enabled") is not True:
                    state["state"] = "disabled"
                    stop.wait(5)
                else:
                    local = journal.status(db)
                    if any(local.get(k) != v for k, v in cfg["identity"].items()):
                        raise ValueError("configured enrollment differs from durable journal")
                    if (
                        cfg.get("reporting_metadata_enabled") is True
                        and not db.execute(
                            "SELECT 1 FROM delivery_journal WHERE prepared=0 LIMIT 1 OFFSET 999"
                        ).fetchone()
                    ):
                        try:
                            state["reporting_metadata"] = cpu.call(
                                "report_metadata", report_delivery.publish, db
                            )
                            state.pop("reporting_metadata_error", None)
                        except Exception as error:
                            db.rollback()
                            state["reporting_metadata_error"] = type(error).__name__
                    deadline = time.monotonic() + 0.25
                    prepared = 0
                    while (
                        prepared < 500
                        and time.monotonic() < deadline
                        and cpu.call("prepare", journal.prepare_one, db)
                    ):
                        prepared += 1
                    result = cpu.call("send", client.step, db, api)
                    productive = prepared > 0 or result in ("accepted", "prepared")
                    state["state"] = result
                    if result == "accepted":
                        state["last_accepted_at"] = now
                    if now - last_status >= 30:
                        with cpu.measure("receipt_refresh"):
                            row = db.execute(
                                "SELECT batch_id FROM delivery_batches WHERE accepted=1 ORDER BY created_at DESC LIMIT 1"
                            ).fetchone()
                            remote = (
                                api.status(batch_id=row[0], limit=1) if row else api.status(limit=1)
                            )
                            journal.reconcile_status(db, remote)
                            last_status = now
                            state["last_status_at"] = now
                            state["last_materialized_through"] = remote["materialized_through"]
                    if result not in ("retry", "blocked"):
                        state["last_success_at"] = now
                        state.pop("error", None)
            except Exception as error:
                # No exception messages: auth/SQL/network libraries may echo secrets.
                state.update(state="error", error=type(error).__name__, last_error_at=now)
                stop.wait(5)
            if now - last_write >= 5:
                state.update(
                    heartbeat=time.time(),
                    cpu_accounting=overhead.reading(),
                    worker_cpu=cpu.snapshot(),
                )
                atomic_json(directory() / "status.json", state)
                last_write = now
            if not productive:
                stop.wait(2)
        state.update(
            state="stopped",
            heartbeat=time.time(),
            cpu_accounting=overhead.reading(),
            worker_cpu=cpu.snapshot(),
        )
        atomic_json(directory() / "status.json", state)


def install(path=None, database=store.DB_PATH):
    from .service import bootstrap

    if sys.platform != "darwin":
        raise RuntimeError("Use erg delivery run under a service manager on this platform")
    path = Path(path or directory() / "config.json").resolve()
    configuration(path)
    root = directory()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime = root / "runtime"
    package = runtime / "erg"
    package.mkdir(parents=True, exist_ok=True)
    for src in Path(__file__).parent.glob("*.py"):
        shutil.copy2(src, package / src.name)
    agent = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
    env = dict(
        HOME=str(Path.home()),
        PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        PYTHONPATH=str(runtime),
        PYTHONDONTWRITEBYTECODE="1",
        ERG_DATABASE=str(database),
        ERG_STATE_DIR=str(root.parent),
    )
    value = dict(
        Label=LABEL,
        ProgramArguments=[
            sys.executable,
            "-B",
            "-u",
            "-m",
            "erg.cli",
            "delivery",
            "run",
            "--config",
            str(path),
            "--database",
            str(database),
        ],
        EnvironmentVariables=env,
        WorkingDirectory=str(runtime),
        RunAtLoad=True,
        KeepAlive=True,
        ThrottleInterval=10,
        Umask=0o077,
        ProcessType="Standard",
        StandardOutPath=str(root / "worker.out"),
        StandardErrorPath=str(root / "worker.err"),
    )
    for name in ("worker.out", "worker.err"):
        fd = os.open(str(root / name), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        os.close(fd)
    agent.write_bytes(plistlib.dumps(value))
    agent.chmod(0o600)
    domain = "gui/" + str(os.getuid())
    subprocess.run(["launchctl", "bootout", domain + "/" + LABEL], capture_output=True)
    bootstrap(domain, agent)
    return dict(label=LABEL, config=str(path), database=str(database))
