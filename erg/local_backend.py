"""Explicit macOS enrollment of an existing ds4 server behind local capture."""

import os
import plistlib
import shlex
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import collect, history, service


def plan(pid, internal_port):
    command = subprocess.run(
        ["ps", "-p", str(int(pid)), "-o", "command="], capture_output=True, text=True, check=True
    ).stdout.strip()
    args = shlex.split(command)
    if not args or Path(args[0]).name != "ds4-server":
        raise ValueError("PID is not a ds4-server process")
    if "--port" not in args:
        raise ValueError("explicit current --port is required")
    index = args.index("--port") + 1
    front = int(args[index])
    replacement = list(args)
    replacement[index] = str(int(internal_port))
    if front == int(internal_port):
        raise ValueError("internal port must differ from the public port")
    directory = subprocess.run(
        ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    cwd = next((line[1:] for line in directory.splitlines() if line.startswith("n")), None)
    if not cwd or not Path(cwd).is_dir():
        raise ValueError("could not identify ds4 working directory")
    return dict(
        pid=int(pid),
        original=args,
        backend=replacement,
        cwd=cwd,
        front_port=front,
        internal_port=int(internal_port),
        label="dev.ddx.erg.local-model." + str(front),
    )


def write_agent(spec, args):
    state = Path(collect.STATE_DIR)
    folder = Path(collect.HOME) / "Library/LaunchAgents"
    folder.mkdir(parents=True, exist_ok=True)
    out = state / ("local-model-" + str(spec["front_port"]) + ".out")
    err = out.with_suffix(".err")
    for p in (out, err):
        fd = os.open(str(p), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        os.close(fd)
        os.chmod(p, 0o600)
    value = dict(
        Label=spec["label"],
        ProgramArguments=args,
        WorkingDirectory=spec["cwd"],
        RunAtLoad=True,
        KeepAlive=True,
        ThrottleInterval=10,
        ExitTimeOut=120,
        Umask=0o077,
        StandardOutPath=str(out),
        StandardErrorPath=str(err),
        EnvironmentVariables=dict(
            HOME=collect.HOME, PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        ),
    )
    p = folder / (spec["label"] + ".plist")
    p.write_bytes(plistlib.dumps(value))
    os.chmod(p, 0o600)
    return p


def ready(port, deadline):
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:" + str(port) + "/v1/models", timeout=2
            ) as response:
                if response.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def swap(spec, idle_seconds=5, idle_timeout=120, ready_timeout=180):
    if sys.platform != "darwin":
        raise RuntimeError("managed backend enrollment currently requires macOS")
    domain = "gui/" + str(os.getuid())
    state = Path(collect.STATE_DIR)
    original_config = history.config()
    deadline = time.monotonic() + idle_timeout
    idle_since = None
    # Do not replace an unrelated service or steal a port.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", spec["internal_port"]))
    if (Path(collect.HOME) / "Library/LaunchAgents" / (spec["label"] + ".plist")).exists():
        raise ValueError("backend enrollment already exists")
    while time.monotonic() < deadline:
        connections = subprocess.run(
            ["lsof", "-nP", "-iTCP:" + str(spec["front_port"]), "-sTCP:ESTABLISHED", "-t"],
            capture_output=True,
            text=True,
        )
        if not connections.stdout.strip():
            idle_since = idle_since or time.monotonic()
            if time.monotonic() - idle_since >= idle_seconds:
                break
        else:
            idle_since = None
        time.sleep(0.5)
    else:
        raise RuntimeError("no idle window observed; ds4 was left unchanged")
    service.atomic_json(state / "local-backend-original.json", spec)
    plist = write_agent(spec, spec["backend"])
    changed = False
    try:
        print("Idle window observed; requesting graceful ds4 shutdown.", flush=True)
        os.kill(spec["pid"], signal.SIGTERM)
        changed = True
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                os.kill(spec["pid"], 0)
            except ProcessLookupError:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("ds4 did not finish draining; not forcing termination")
        service.bootstrap(domain, plist)
        print("Backend restarting on internal port " + str(spec["internal_port"]) + ".", flush=True)
        if not ready(spec["internal_port"], time.monotonic() + ready_timeout):
            raise RuntimeError("backend did not become ready")
        config = dict(original_config)
        config["endpoints"] = [
            e for e in config.get("endpoints", []) if e["port"] != spec["front_port"]
        ]
        config["endpoints"].append(
            dict(
                name="ds4",
                upstream="http://127.0.0.1:" + str(spec["internal_port"]),
                port=spec["front_port"],
            )
        )
        service.atomic_json(state / "capture.json", config)
        service.install()
        if not ready(spec["front_port"], time.monotonic() + 30):
            raise RuntimeError("capture listener did not become ready")
        service.atomic_json(
            state / "local-backend-status.json",
            dict(
                state="running",
                upstream_port=spec["internal_port"],
                capture_port=spec["front_port"],
                changed_at=int(time.time()),
            ),
        )
        print("Capture front door ready; existing client URLs are unchanged.", flush=True)
        return dict(
            state="running",
            capture_port=spec["front_port"],
            backend_port=spec["internal_port"],
            label=spec["label"],
        )
    except Exception:
        # If the original process is still draining, leave it intact. If it
        # exited, restore its exact command under the managed service.
        service.atomic_json(state / "capture.json", original_config)
        subprocess.run(["launchctl", "bootout", domain + "/" + spec["label"]], capture_output=True)
        if changed:
            try:
                os.kill(spec["pid"], 0)
            except ProcessLookupError:
                write_agent(spec, spec["original"])
                service.bootstrap(domain, plist)
        service.install()
        raise
