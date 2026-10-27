"""erg CLI: sync (local + ssh hosts), serve, sampler."""
import argparse, json, os, subprocess, sys, time
from contextlib import closing

from . import store

COLLECT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "collect.py")
CONF = os.path.join(os.path.expanduser("~"), ".config", "erg", "hosts.json")


def load_hosts():
    try:
        return json.load(open(CONF))
    except FileNotFoundError:
        return []


def run_collect(host, args):
    """host None => local. Remote runs the script from stdin: nothing to install."""
    if host is None:
        if '--retained' in args:
            # Checkout entrypoints adjust sys.path, which subprocesses do not
            # inherit. Give the package-mode worker its explicit package root.
            env=dict(os.environ)
            root=os.path.dirname(os.path.dirname(COLLECT))
            env['PYTHONPATH']=root+(os.pathsep+env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
            bootstrap='import runpy,sys; sys.path.insert(0,'+repr(root)+"); runpy.run_module('erg.collect',run_name='__main__')"
            return subprocess.run([sys.executable,'-B','-c',bootstrap,'collect']+args,capture_output=True,text=True,timeout=600,env=env)
        cmd = [sys.executable, COLLECT, "collect"] + args
        return subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host["ssh"]]
    cmd = ssh + [host.get("python", "python3"), "-", "collect"] + args
    return subprocess.run(cmd, input=open(COLLECT).read(), capture_output=True, text=True, timeout=600)


def sync_one(db, host, full=False, retained=False):
    if retained and host is not None:raise ValueError("retained enrichment is local-only")
    name = host["name"] if host else None
    known = store.last_sync(db, name) if name else None
    if not name:  # local machine name comes from hello; peek at DB by hostname
        from . import collect
        name = collect.machine_name()
        known = store.last_sync(db, name)
    days = 400 if full or not known else max(2, int((time.time() - known) / 86400) + 2)
    args = ["--days", str(days), "--perf-since", "0" if full else str(store.max_perf_ts(db, name))]
    if host:
        if host.get("roots"): args += ["--roots", host["roots"]]
        if host.get("emails"): args += ["--emails", host["emails"]]
    else:
        args += ["--roots", "~/Projects"]
    if retained:
        path=next((r[2] for r in db.execute('PRAGMA database_list') if r[1]=='main'),None)
        if not path:raise RuntimeError('retained enrichment requires a persistent local ledger')
        args+=['--retained','--retained-database',path]
    t0 = time.time()
    r = run_collect(host, args)
    if r.returncode != 0:
        print("  %s: FAILED (%s)" % (name, r.stderr.strip()[-300:]), file=sys.stderr)
        raise RuntimeError("collection failed for " + name)
    m, counts = store.ingest(db, r.stdout.splitlines(), machine=host["name"] if host else None)
    print("  %-14s %5.1fs  %s" % (m, time.time() - t0, " ".join("%s=%d" % kv for kv in sorted(counts.items()))))



def capture_once(db,full=False):
    """One local cycle; the caller owns the collector lock."""
    from . import history,work,titles,account_probe,metric_rollups
    retained=bool(history.config().get('content'))
    if retained and not db.execute("SELECT 1 FROM history_config WHERE key='enrolled_at'").fetchone():
        raise RuntimeError('this ledger is not enrolled for retained capture')
    # Bodies and their durable projection queue precede optional enrichment.
    captured=history.scan(db) if retained else dict(enabled=False,events=0,errors=0)
    metadata=None
    if retained:
        from . import service,routes
        routes.profiles();routes.backfill_once(db)
        metadata=service.fast_capture(db,0,include_pi=False)
    sync_one(db,None,full,retained=retained)
    context=work.scan(db,full=full)
    title_result=titles.refresh(db)
    accounts=account_probe.poll(db)
    try:rollups=metric_rollups.run(db)
    except Exception as error:rollups=dict(error=type(error).__name__)
    return dict(history=captured,metadata=metadata,work=context,titles=title_result,account_profiles=accounts,metric_rollups=rollups)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="erg")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync", help="scrape this machine + configured hosts into the DB")
    s.add_argument("--full", action="store_true"); s.add_argument("--no-local", action="store_true")
    s.add_argument("--host", help="only this configured host name")
    sv = sub.add_parser("serve", help="serve the dashboard")
    sv.add_argument("--port", type=int, default=int(os.environ.get("DATABRICKS_APP_PORT", "8787")))
    sv.add_argument("--bind", default="127.0.0.1")
    sv.add_argument("--backend", choices=["sqlite", "databricks"], default=os.environ.get("ERG_BACKEND", "sqlite"))
    sv.add_argument("--profile", default=os.environ.get("ERG_PROFILE"))
    sv.add_argument("--warehouse", default=os.environ.get("ERG_WAREHOUSE_ID"))
    sv.add_argument("--namespace", default=os.environ.get("ERG_NAMESPACE"))
    co=sub.add_parser("collect", help="capture locally once (cron), or continuously with --interval seconds")
    co.add_argument("--interval", type=int, default=0)
    co.add_argument("--full", action="store_true")
    co.add_argument("--push", action="store_true", help="export after each collection")
    co.add_argument("--profile"); co.add_argument("--warehouse"); co.add_argument("--namespace")
    for name in ("push", "provision"):
        pp=sub.add_parser(name, help="export durable observations" if name=="push" else "create erg-owned Delta tables and views")
        pp.add_argument("--profile", required=True); pp.add_argument("--warehouse", required=True); pp.add_argument("--namespace", required=True)
        if name=="push":
            pp.add_argument("--batch-size", type=int, default=5000)
            pp.add_argument("--replay", action="store_true", help="replay all retained snapshots to repair a destination")
    wk=sub.add_parser("work",help="capture task context, refresh PR evidence or assign session usage")
    wk.add_argument("action",choices=["scan","refresh","link","unlink","replay-retained"])
    wk.add_argument("--full",action="store_true")
    wk.add_argument("--reset",action="store_true",help="re-read native input/context evidence without deleting observations")
    wk.add_argument("--repo",action="append",default=[])
    wk.add_argument("--limit",type=int,default=50)
    wk.add_argument("--save",action="store_true",help="retain repository list for service PR refresh")
    wk.add_argument('--apply',action='store_true',help='insert missing work evidence from retained session records')
    wk.add_argument('--max-events',type=int,default=5000)
    wk.add_argument('--budget-seconds',type=float,default=10)
    wk.add_argument('--machine',help='retained session machine (default: this collector)')
    wk.add_argument("--tool");wk.add_argument("--session");wk.add_argument("--pr");wk.add_argument("--task",default="")
    wk.add_argument("--from-ts",type=int,default=0);wk.add_argument("--to-ts",type=int,default=0)
    ac=sub.add_parser('accounts',help='inspect discovered identity/quotas or import evolving billing metadata')
    ac.add_argument('action',choices=['status','import','capture-hook','backfill']);ac.add_argument('--file');ac.add_argument('--tool')
    service=sub.add_parser("service",help="install, run, inspect or stop continuous local capture")
    service.add_argument("action",choices=["install","run","status","stop"])
    service.add_argument("--interval",type=int,default=900)
    service.add_argument("--poll",type=int,default=2)
    reporter=sub.add_parser('reporter',help='install, inspect or stop the independent local dashboard service')
    reporter.add_argument('action',choices=['install','status','stop'])
    reporter.add_argument('--port',type=int,help='local dashboard port; preserve installed port by default')
    pc=sub.add_parser('projects',help='manage the local project catalog and preview configuration-driven cost allocation')
    pc.add_argument('action',choices=['list','export','create','import','preview','index-context'])
    pc.add_argument('--name');pc.add_argument('--file');pc.add_argument('--apply',action='store_true')
    pc.add_argument('--start',type=int,default=0);pc.add_argument('--end',type=int)
    de=sub.add_parser('delivery',help='inspect central delivery or estimate retained volume without uploading')
    de.add_argument('action',choices=['status','inventory','enroll','install','run','stop'])
    de.add_argument('--database',default=store.DB_PATH)
    de.add_argument('--config')
    hs=sub.add_parser('history',help='enroll forward capture, inspect coverage, or expire local content')
    hs.add_argument('action',choices=['enroll','status','expire','project','repair-metadata'])
    hs.add_argument('--before',type=int);hs.add_argument('--apply',action='store_true')
    hs.add_argument('--rebuild',action='store_true',help='requeue retained corpus only; never read upstream history')
    hs.add_argument('--tool');hs.add_argument('--session')
    lp=sub.add_parser('local-proxy',help='capture new local inference requests on a loopback endpoint')
    lp.add_argument('--upstream',required=True);lp.add_argument('--port',type=int,required=True)
    lp.add_argument('--save',action='store_true',help='enroll endpoint in the background collector')
    sub.add_parser("sources",help="show installed agents and available capture history")
    ad = sub.add_parser("add-host", help="register a remote machine")
    ad.add_argument("name"); ad.add_argument("ssh", help="user@host"); ad.add_argument("--python", default="python3")
    ad.add_argument("--roots", help="comma-separated git scan roots on remote, default ~/Projects")
    ag = sub.add_parser("add-gateway", help="configure Databricks AI Gateway erg ingest (uses the databricks CLI's auth)")
    ag.add_argument("--profile", required=True); ag.add_argument("--warehouse", help="SQL warehouse id (auto-discovered if omitted)")
    ag.add_argument("--table", default="system.ai_gateway.usage")
    sa = sub.add_parser("sampler", help="install the perf sampler locally or on a host")
    sa.add_argument("--host", help="configured host name (default: local)")
    a = ap.parse_args(argv)

    if a.cmd=='accounts':
        from . import accounting,work,collect
        with closing(store.connect()) as db:
            if a.action=='capture-hook':
                if a.tool!='claude':ap.error('capture-hook supports claude')
                try:accounting.claude_session_start(json.load(sys.stdin),db)
                except (ValueError,OSError):pass
            elif a.action=='backfill':
                from . import routes
                print(json.dumps(routes.backfill(db)))
            elif a.action=='import':
                if not a.file:ap.error('accounts import requires --file')
                with open(a.file) as file:data=json.load(file)
                print(json.dumps(dict(imported=accounting.import_metadata(db,data,collect.machine_name()))))
            else:
                data=work.effectiveness(db)
                print(json.dumps({key:data[key] for key in ('accounts','subscription_periods','quota_latest','quota_counts')},indent=2))
    elif a.cmd=="work":
        from . import work
        if a.action=='replay-retained':
            from . import work_retained
            if a.tool not in ('claude','codex') or not a.session:ap.error('replay-retained requires --tool claude|codex and --session')
            with closing(store.connect() if a.apply else store.open_readonly()) as db:
                result=work_retained.replay(db,a.tool,a.session,a.machine,a.apply,a.max_events,a.budget_seconds)
                print(json.dumps(result,indent=2))
                if a.apply and not result['applied']:raise RuntimeError('retained work replay is incomplete or conflicting; see result')
        else:
            with closing(store.connect()) as db:
                if a.action=="scan":
                    if a.reset:
                        with db:db.execute("DELETE FROM native_offsets")
                    print(json.dumps(work.scan(db,a.full)))
                elif a.action=="refresh":
                    if not a.repo or not 1<=a.limit<=100: ap.error("refresh requires --repo and --limit 1..100")
                    if a.save:
                        from .service import atomic_json
                        atomic_json(os.path.join(os.path.expanduser("~"),".config","erg","prs.json"),dict(repos=[work.repository(r) for r in a.repo],limit=a.limit))
                    print(json.dumps(work.refresh(db,a.repo,a.limit)))
                else:
                    if not all([a.tool,a.session,a.pr]):ap.error("link requires --tool, --session and --pr")
                    if a.action=="unlink": print("retracted",work.unlink(db,a.tool,a.session,a.pr),"links")
                    else:work.bind(db,a.tool,a.session,a.pr,a.task,a.from_ts,a.to_ts);print("recorded explicit PR attribution")
    elif a.cmd=='delivery':
        if a.action=='inventory':
            from . import delivery_inventory
            print(json.dumps(delivery_inventory.inventory(a.database),indent=2))
        else:
            from . import delivery_service
            if a.action=='run':delivery_service.run(a.config,a.database)
            elif a.action=='enroll':print(json.dumps(delivery_service.enroll(a.config,a.database),indent=2))
            elif a.action=='install':print(json.dumps(delivery_service.install(a.config,a.database),indent=2))
            elif a.action=='stop':
                subprocess.run(['launchctl','bootout','gui/'+str(os.getuid())+'/'+delivery_service.LABEL],check=True,capture_output=True)
            else:print(json.dumps(delivery_service.status(a.database),indent=2))
    elif a.cmd=='history':
        from . import history
        with closing(store.connect()) as db:
            if a.action=='repair-metadata':
                from . import history_repair
                if a.tool and a.tool!='claude':ap.error('metadata repair currently supports Claude only')
                print(json.dumps(history_repair.repair(db,a.apply,a.session or '')))
            elif a.action=='project':
                from . import history_projection
                if a.rebuild:history_projection.requeue(db,a.tool or '',a.session or '')
                print(json.dumps(history_projection.run(db)))
            elif a.action=='enroll':print(json.dumps(history.enroll(db),indent=2))
            elif a.action=='status':print(json.dumps(history.overview(db),indent=2))
            else:
                if not a.before or a.before<0:ap.error('expiration requires --before UNIX_SECONDS; omit --apply for preview')
                print(json.dumps(history.expire(db,a.before,a.apply),indent=2))
    elif a.cmd=='local-proxy':
        from . import history,local_proxy
        # Validate the route before persisting it. Close the temporary listener.
        listener=local_proxy.make_server(a.upstream,a.port);listener.server_close()
        with closing(store.connect()) as db:settings=history.enroll(db)
        if a.save:
            from .service import atomic_json
            endpoints=[e for e in settings.get('endpoints',[]) if e['port']!=a.port]
            endpoints.append(dict(name='local-'+str(a.port),upstream=a.upstream,port=a.port))
            settings['endpoints']=endpoints;atomic_json(os.path.join(collect.STATE_DIR,'capture.json'),settings)
            print('Enrolled loopback endpoint; reinstall/restart the collector to activate it.')
        else:local_proxy.serve(a.upstream,a.port)
    elif a.cmd=='projects':
        from . import projects,project_reports,history
        with closing(store.connect()) as db:
            if a.action=='create':
                if not a.name:ap.error('projects create requires --name')
                value=projects.create(db,a.name)
            elif a.action=='index-context':value=history.index_project_contexts(db)
            elif a.action in ('list','export'):value=projects.snapshot(db)
            else:
                if not a.file:ap.error('projects import/preview requires --file')
                with open(a.file) as stream:config=projects.validate(json.load(stream))
                projects.check_change(projects.snapshot(db),config)
                if a.action=='import' and a.apply:value=projects.apply(db,config)
                else:
                    with project_reports.read_snapshot(db):
                        before=project_reports.report(db,a.start,a.end)
                        after=project_reports.report(db,a.start,a.end,config=config)
                    value=dict(applied=False,revision=config['revision'],before=before['projects'],after=after['projects'],unassigned_reasons=after['unassigned_reasons'],scope=after['filters'])
            print(json.dumps(value,indent=2,allow_nan=False))
    elif a.cmd=='reporter':
        from . import reporter_service
        if a.port is not None and a.action!='install':ap.error('--port only applies to reporter install')
        if a.action=='install':value=reporter_service.install(a.port)
        elif a.action=='status':value=reporter_service.status()
        else:value=reporter_service.stop()
        print(json.dumps(value,indent=2))
    elif a.cmd=="service":
        from . import service
        if a.poll<1 or a.interval<a.poll: ap.error("require interval >= poll >= 1 second")
        if a.action=="install": print(json.dumps(service.install(a.interval,a.poll),indent=2))
        elif a.action=="run": service.run(a.interval,a.poll)
        elif a.action=="status": print(json.dumps(service.status(),indent=2))
        else: service.stop()
    elif a.cmd in ("push", "provision"):
        from . import databricks
        client=databricks.Client(a.profile,a.warehouse)
        if a.cmd=="provision":
            databricks.provision(client,a.namespace)
            print("provisioned erg objects in",a.namespace)
        else:
            if not 1 <= a.batch_size <= 10000: ap.error("batch size must be 1..10000")
            with closing(store.connect()) as db:
                print("exported",databricks.sync(db,client,a.namespace,a.batch_size,replay=a.replay),"observations")
    elif a.cmd == "sources":
        from . import collect
        import shutil
        roots = {"claude":collect.claude_roots(), "codex":collect.native_roots("codex"), "pi":collect.pi_roots(),
                 "copilot":[os.path.join(collect.HOME,".copilot/session-state")],
                 "opencode":[os.path.join(collect.HOME,".local/share/opencode")]}
        for tool, paths in roots.items():
            n = len(list(collect.log_paths(paths)))
            if tool == "opencode":
                n += sum(os.path.isfile(os.path.join(p,"opencode.db")) for p in paths)
            print("%-10s installed=%s history_files=%d roots=%d"%(tool,bool(shutil.which(tool)),n,sum(os.path.exists(p) for p in paths)))
        print("%-10s installed=%s metadata_db=%s"%("omnigent",bool(shutil.which("omnigent")),os.path.isfile(os.path.join(collect.omni_home(),"chat.db"))))
    elif a.cmd == "collect":
        if a.interval < 0: ap.error("interval cannot be negative")
        if a.push and not all([a.profile,a.warehouse,a.namespace]):
            ap.error("--push requires --profile, --warehouse and --namespace")
        import threading
        from . import collect
        with collect.collector_lock():
            stop=threading.Event()
            def sample_loop():
                while not stop.is_set():
                    collect.take_sample()
                    stop.wait(30)
            sampler=threading.Thread(target=sample_loop,daemon=True)
            if a.interval: sampler.start()
            else: collect.take_sample()
            try:
                while True:
                    with closing(store.connect()) as db:
                        result=capture_once(db,a.full)
                        print('capture',json.dumps(result,allow_nan=False))
                        captured=result['history']
                        if captured.get('errors') or captured.get('source_error_count') or captured.get('projection',{}).get('errors'):
                            raise RuntimeError('forward content capture needs attention; see capture result')
                        if a.push:
                            from . import databricks
                            print("exported",databricks.sync(db,databricks.Client(a.profile,a.warehouse),a.namespace),"observations")
                    if not a.interval: break
                    stop.wait(a.interval)
            except KeyboardInterrupt:
                pass
            finally:
                stop.set()
                if sampler.is_alive(): sampler.join(20)
    elif a.cmd == "add-host":
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
        if gc and not a.host and not os.environ.get("ERG_SKIP_GATEWAY"):
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
            sh("mkdir -p ~/.local/share/erg && cat > ~/.local/share/erg/collect.py", open(COLLECT).read())
            sh("%s ~/.local/share/erg/collect.py sampler-install" % h.get("python", "python3"))
        else:
            subprocess.run([sys.executable, COLLECT, "sampler-install"])
    elif a.cmd == "serve":
        from . import server
        if a.backend=="databricks" and not all([a.warehouse,a.namespace]):
            ap.error("Databricks reporting requires --warehouse and --namespace")
        server.serve(a.port, bind=a.bind, backend=a.backend, profile=a.profile, warehouse=a.warehouse, namespace=a.namespace)


if __name__ == "__main__":
    main()
