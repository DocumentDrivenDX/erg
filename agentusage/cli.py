"""usage CLI: sync (local + ssh hosts), serve, sampler."""
import argparse, json, os, subprocess, sys, time

from . import store

COLLECT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "collect.py")
CONF = os.path.join(os.path.expanduser("~"), ".config", "usage", "hosts.json")


def load_hosts():
    try:
        return json.load(open(CONF))
    except FileNotFoundError:
        return []


def run_collect(host, args):
    """host None => local. Remote runs the script from stdin: nothing to install."""
    if host is None:
        cmd = [sys.executable, COLLECT, "collect"] + args
        return subprocess.run(cmd, capture_output=True, text=True)
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host["ssh"]]
    cmd = ssh + [host.get("python", "python3"), "-", "collect"] + args
    return subprocess.run(cmd, input=open(COLLECT).read(), capture_output=True, text=True)


def sync_one(db, host, full=False):
    name = host["name"] if host else None
    known = store.last_sync(db, name) if name else None
    if not name:  # local machine name comes from hello; peek at DB by hostname
        import socket
        name = socket.gethostname().split(".")[0]
        known = store.last_sync(db, name)
    days = 400 if full or not known else max(2, int((time.time() - known) / 86400) + 2)
    args = ["--days", str(days), "--perf-since", "0" if full else str(store.max_perf_ts(db, name))]
    if host:
        if host.get("roots"): args += ["--roots", host["roots"]]
        if host.get("emails"): args += ["--emails", host["emails"]]
    else:
        args += ["--roots", "~/Projects"]
    t0 = time.time()
    r = run_collect(host, args)
    if r.returncode != 0:
        print("  %s: FAILED (%s)" % (name, r.stderr.strip()[-300:]), file=sys.stderr)
        return
    m, counts = store.ingest(db, r.stdout.splitlines(), machine=host["name"] if host else None)
    print("  %-14s %5.1fs  %s" % (m, time.time() - t0, " ".join("%s=%d" % kv for kv in sorted(counts.items()))))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="usage")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync", help="scrape this machine + configured hosts into the DB")
    s.add_argument("--full", action="store_true"); s.add_argument("--no-local", action="store_true")
    s.add_argument("--host", help="only this configured host name")
    sv = sub.add_parser("serve", help="serve the dashboard")
    sv.add_argument("--port", type=int, default=8787); sv.add_argument("--sync-every", type=int, default=0, help="minutes; 0=off")
    ad = sub.add_parser("add-host", help="register a remote machine")
    ad.add_argument("name"); ad.add_argument("ssh", help="user@host"); ad.add_argument("--python", default="python3")
    ad.add_argument("--roots", help="comma-separated git scan roots on remote, default ~/Projects")
    ag = sub.add_parser("add-gateway", help="configure Databricks AI Gateway usage ingest (uses the databricks CLI's auth)")
    ag.add_argument("--profile", required=True); ag.add_argument("--warehouse", help="SQL warehouse id (auto-discovered if omitted)")
    ag.add_argument("--table", default="system.ai_gateway.usage")
    sa = sub.add_parser("sampler", help="install the perf sampler locally or on a host")
    sa.add_argument("--host", help="configured host name (default: local)")
    a = ap.parse_args(argv)

    if a.cmd == "add-host":
        hosts = [h for h in load_hosts() if h["name"] != a.name]
        hosts.append({"name": a.name, "ssh": a.ssh, "python": a.python, **({"roots": a.roots} if a.roots else {})})
        os.makedirs(os.path.dirname(CONF), exist_ok=True)
        json.dump(hosts, open(CONF, "w"), indent=2)
        print("saved", CONF)
    elif a.cmd == "sync":
        db = store.connect()
        print("syncing…")
        if not a.no_local and not a.host:
            sync_one(db, None, a.full)
        for h in load_hosts():
            if not a.host or h["name"] == a.host:
                sync_one(db, h, a.full)
        from . import gateway
        gc = gateway.load_conf()
        if gc and not a.host and not os.environ.get("USAGE_SKIP_GATEWAY"):
            t0 = time.time()
            try:
                n = gateway.sync(db, gc, a.full)
                print("  %-14s %5.1fs  gateway_calls=%d" % ("databricks", time.time() - t0, n))
            except Exception as e:
                print("  databricks: FAILED (%s)" % e, file=sys.stderr)
    elif a.cmd == "add-gateway":
        from . import gateway
        conf = {"profile": a.profile, "table": a.table, "warehouse_id": a.warehouse or gateway.discover_warehouse(a.profile),
                "me": gateway._api(a.profile, "get", "/api/2.0/preview/scim/v2/Me").get("userName", "")}
        os.makedirs(os.path.dirname(gateway.CONF), exist_ok=True)
        json.dump(conf, open(gateway.CONF, "w"), indent=2)
        print("saved", gateway.CONF, conf)
    elif a.cmd == "sampler":
        if a.host:
            h = next(h for h in load_hosts() if h["name"] == a.host)
            sh = lambda c, inp=None: subprocess.run(["ssh", h["ssh"], c], input=inp, text=True)
            sh("mkdir -p ~/.local/share/usage && cat > ~/.local/share/usage/collect.py", open(COLLECT).read())
            sh("%s ~/.local/share/usage/collect.py sampler-install" % h.get("python", "python3"))
        else:
            subprocess.run([sys.executable, COLLECT, "sampler-install"])
    elif a.cmd == "serve":
        from . import server
        server.serve(a.port, a.sync_every, lambda: main(["sync"]))


if __name__ == "__main__":
    main()
