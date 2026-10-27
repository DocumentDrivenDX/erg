"""Independent local reporter lifecycle; never starts collection or exports."""

import contextlib
import json
import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

from . import collect, store
from .service import atomic_json, bootstrap

LABEL = "dev.ddx.erg.reporter"


def paths():
    state = Path(collect.STATE_DIR).resolve()
    return state, Path(collect.HOME) / "Library/LaunchAgents" / (LABEL + ".plist")


def read_config():
    try:
        with (paths()[0] / "reporter-service.json").open() as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return None


def job():
    result = subprocess.run(
        ["launchctl", "print", "gui/" + str(os.getuid()) + "/" + LABEL],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode:
        return dict(loaded=False, pid=None)
    match = re.search(r"^\s*pid = (\d+)\s*$", result.stdout, re.M)
    return dict(loaded=True, pid=int(match[1]) if match else None)


def health(config):
    # An unrelated listener at the configured port is not evidence of readiness.
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(
            "http://127.0.0.1:" + str(config["port"]) + "/healthz", timeout=2
        ) as response:
            value = json.loads(response.read(4096))
        return value.get("status") == "ok" and value.get("instance") == config["instance"]
    except (OSError, ValueError, KeyError):
        return False


def status():
    if sys.platform != "darwin":
        return dict(supported=False, running=False, state="unsupported_platform")
    config = read_config()
    if not config:
        return dict(supported=True, running=False, state="not_installed")
    process = job()
    ready = bool(process["pid"] and health(config))
    return dict(
        supported=True,
        label=LABEL,
        running=ready,
        state="running" if ready else "starting_or_unhealthy" if process["loaded"] else "stopped",
        pid=process["pid"],
        port=config["port"],
        url="http://127.0.0.1:" + str(config["port"]) + "/",
        database=config["database"],
    )


def write_bytes(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(path))
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def wait_ready(timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = status()
        if value["running"]:
            return value
        time.sleep(0.25)
    raise RuntimeError(
        "Reporter did not become healthy; inspect reporter.err in the erg state directory"
    )


def owned_plist(plist, config):
    try:
        data = plistlib.loads(plist.read_bytes())
        argv = data.get("ProgramArguments", [])
        return (
            data.get("Label") == LABEL
            and ["-m", "erg.cli", "serve"] == argv[3:6]
            and data.get("EnvironmentVariables", {}).get("ERG_REPORTER_INSTANCE")
            == config.get("instance")
        )
    except (OSError, ValueError, TypeError):
        return False


def install(port=None):
    if sys.platform != "darwin":
        raise RuntimeError(
            "Automatic reporter installation currently supports macOS launchd; use erg serve on other platforms"
        )
    previous = read_config()
    state, plist = paths()
    port = previous["port"] if port is None and previous else 8787 if port is None else port
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    process = job()
    if (plist.exists() or process["loaded"]) and not (previous and owned_plist(plist, previous)):
        raise RuntimeError("Existing reporter job is not recognized; no job was changed")
    own_listener = bool(
        previous and process["pid"] and previous["port"] == port and health(previous)
    )
    if not own_listener:
        try:
            with socket.socket() as listener:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("127.0.0.1", port))
        except OSError:
            raise RuntimeError(
                "Reporter port is already occupied; the existing listener was not changed"
            ) from None
    # Each installation gets an immutable code snapshot, separate from collector upgrades.
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = state / "reporter-runtime"
    root.mkdir(mode=0o700, exist_ok=True)
    instance = uuid.uuid4().hex
    runtime = root / instance
    package = runtime / "erg"
    package.mkdir(parents=True, mode=0o700)
    source = Path(__file__).parent
    for item in list(source.glob("*.py")) + list(source.glob("*.json")):
        shutil.copy2(str(item), str(package / item.name))
    shutil.copytree(str(source / "static"), str(package / "static"))
    from . import rate_catalog, rates

    atomic_json(package / "rate_card.json", rate_catalog.clean(rates.load()))
    database = str(Path(store.DB_PATH).resolve())
    env = dict(
        HOME=collect.HOME,
        PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        PYTHONPATH=str(runtime),
        PYTHONDONTWRITEBYTECODE="1",
        ERG_STATE_DIR=str(state),
        ERG_DATABASE=database,
        ERG_REPORTER_INSTANCE=instance,
    )
    config = dict(instance=instance, port=port, database=database, runtime=str(runtime))
    agent = dict(
        Label=LABEL,
        ProgramArguments=[
            sys.executable,
            "-B",
            "-u",
            "-m",
            "erg.cli",
            "serve",
            "--backend",
            "sqlite",
            "--bind",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        WorkingDirectory=str(runtime),
        EnvironmentVariables=env,
        RunAtLoad=True,
        KeepAlive=True,
        ThrottleInterval=3,
        ExitTimeOut=10,
        Umask=0o077,
        ProcessType="Standard",
        LowPriorityBackgroundIO=True,
        StandardOutPath=str(state / "reporter.out"),
        StandardErrorPath=str(state / "reporter.err"),
    )
    for name in ("reporter.out", "reporter.err"):
        fd = os.open(str(state / name), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(fd)
        os.chmod(str(state / name), 0o600)
    old_plist = plist.read_bytes() if plist.exists() else None
    domain = "gui/" + str(os.getuid())
    manifest = state / "reporter-service.json"
    if process["loaded"]:
        result = subprocess.run(["launchctl", "bootout", domain + "/" + LABEL], capture_output=True)
        if result.returncode:
            raise RuntimeError("Could not stop previous reporter; installation was not replaced")
    try:
        write_bytes(plist, plistlib.dumps(agent))
        atomic_json(manifest, config)
        bootstrap(domain, plist)
        value = wait_ready()
    except Exception:
        subprocess.run(["launchctl", "bootout", domain + "/" + LABEL], capture_output=True)
        if old_plist is not None:
            write_bytes(plist, old_plist)
            atomic_json(manifest, previous)
            if process["loaded"]:
                bootstrap(domain, plist)
        else:
            with contextlib.suppress(FileNotFoundError):
                plist.unlink()
            with contextlib.suppress(FileNotFoundError):
                manifest.unlink()
        shutil.rmtree(str(runtime))
        raise
    # Keep the immediately previous snapshot for recovery; prune only owned versions.
    keep = {instance, Path(previous["runtime"]).name if previous else ""}
    for item in root.iterdir():
        if (
            item.is_dir()
            and not item.is_symlink()
            and re.fullmatch("[0-9a-f]{32}", item.name)
            and item.name not in keep
        ):
            shutil.rmtree(str(item))
    return value


def stop():
    if sys.platform != "darwin":
        raise RuntimeError("Automatic reporter lifecycle currently supports macOS launchd")
    config = read_config()
    _, plist = paths()
    if not config:
        return dict(running=False, state="not_installed")
    if not owned_plist(plist, config):
        raise RuntimeError("Reporter job is not recognized; nothing was stopped")
    if job()["loaded"]:
        result = subprocess.run(
            ["launchctl", "bootout", "gui/" + str(os.getuid()) + "/" + LABEL], capture_output=True
        )
        if result.returncode:
            raise RuntimeError("Could not stop reporter")
    return dict(running=False, state="stopped", next_login="starts_again")
