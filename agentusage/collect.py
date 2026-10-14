#!/usr/bin/env python3
"""agentusage collector: single-file, stdlib-only, Python 3.8+.

Runs locally or is piped over SSH (`ssh host python3 - collect < collect.py`).
Emits NDJSON events on stdout; every event has a stable `uid` so the central
store can upsert idempotently.

  collect.py collect [--days N] [--perf-since TS] [--no-git] [--roots A,B] [--emails a@b,c@d]
  collect.py sample  [--loop SECONDS]          # append one perf sample (or loop forever)
  collect.py sampler-install                   # write launchd/systemd unit for the sampler
"""
import json, os, re, subprocess, sys, time, socket, platform

VERSION = 1
HOME = os.path.expanduser("~")
STATE_DIR = os.environ.get("USAGE_STATE_DIR") or os.path.join(HOME, ".local", "share", "usage")
PERF_FILE = os.path.join(STATE_DIR, "perf.ndjson")
PERF_STATE = os.path.join(STATE_DIR, "perf.state.json")

DOC_EXT = {".md", ".mdx", ".rst", ".txt", ".adoc", ".org", ".tex", ".markdown"}
SKIP_FILE = re.compile(
    r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|Cargo\.lock|uv\.lock|poetry\.lock|"
    r"go\.sum|Gemfile\.lock|bun\.lockb?|composer\.lock)$|\.min\.(js|css)$|\.map$|"
    r"(^|/)(node_modules|vendor|dist|build|\.next|__pycache__)/|\.(svg|png|jpg|jpeg|gif|ico|pdf|woff2?|ttf|lock|csv|tsv|jsonl|ndjson|parquet|snap|xml|ipynb|json|sqlite|db)$")


def emit(o):
    sys.stdout.write(json.dumps(o, separators=(",", ":")) + "\n")


def is_doc(path):
    return os.path.splitext(path or "")[1].lower() in DOC_EXT


def parse_ts(s):
    # "2026-10-12T03:05:53.889Z" -> epoch seconds (UTC), no deps
    try:
        y, mo, d = int(s[0:4]), int(s[5:7]), int(s[8:10])
        h, mi, se = int(s[11:13]), int(s[14:16]), int(s[17:19])
        import calendar
        return calendar.timegm((y, mo, d, h, mi, se))
    except Exception:
        return None


def project_of(cwd):
    if not cwd:
        return "(unknown)"
    if "local-agent-mode-sessions" in cwd or "Claude/scratch-workspaces" in cwd or "claude-501" in cwd:
        return "(claude-scratch)"
    m = re.search(r"/\.claude/worktrees/", cwd)
    if m:
        return os.path.basename(cwd[:m.start()]) or "(unknown)"
    m = re.search(r"/\.codex/worktrees/[^/]+/([^/]+)", cwd)
    if m:
        return m.group(1)
    m = re.search(r"/(?:Projects|projects|src|code|repos|dev|work)/([^/]+)", cwd)
    if m:
        return m.group(1)
    if cwd.rstrip("/") == HOME:
        return "(home)"
    return os.path.basename(cwd.rstrip("/")) or "(root)"


def count_lines(s):
    if not s:
        return 0
    return s.count("\n") + (0 if s.endswith("\n") else 1)


# ---------------------------------------------------------------- Claude Code

def collect_claude(cutoff):
    root = os.path.join(HOME, ".claude", "projects")
    if not os.path.isdir(root):
        return
    llm = {}
    cwds = set()
    for dp, _, fns in os.walk(root):
        for fn in fns:
            if not fn.endswith(".jsonl"):
                continue
            path = os.path.join(dp, fn)
            try:
                if os.path.getmtime(path) < cutoff:
                    continue
                fh = open(path, "rb")
            except OSError:
                continue
            pending = []
            with fh:
                for line in fh:
                    if b'"type":"assistant"' not in line and b'"type":"user"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    t = d.get("type")
                    ts = parse_ts(d.get("timestamp") or "")
                    if ts is None:
                        continue
                    cwd = d.get("cwd")
                    proj = project_of(cwd)
                    if cwd:
                        cwds.add(cwd)
                    sess = d.get("sessionId") or fn[:-6]
                    msg = d.get("message") or {}
                    kind = "subagent" if d.get("isSidechain") else "main"
                    if t == "assistant":
                        model = msg.get("model") or ""
                        if model.startswith("<") or not model:
                            continue
                        for p in pending:
                            p["model"] = model
                            emit(p)
                        pending = []
                        u = msg.get("usage") or {}
                        uid = msg.get("id") or d.get("requestId") or d.get("uuid")
                        out = u.get("output_tokens") or 0
                        rec = {"t": "llm", "uid": "c:" + str(uid), "ts": ts, "tool": "claude", "session": sess,
                               "project": proj, "model": model, "kind": kind, "client": d.get("entrypoint") or "",
                               "fresh_in": u.get("input_tokens") or 0,
                               "cache_read": u.get("cache_read_input_tokens") or 0,
                               "cache_write": u.get("cache_creation_input_tokens") or 0,
                               "out": out,
                               "reasoning": (u.get("output_tokens_details") or {}).get("thinking_tokens") or 0}
                        old = llm.get(rec["uid"])
                        if old is None or out >= old["out"]:
                            llm[rec["uid"]] = rec
                        content = msg.get("content")
                        if isinstance(content, list):
                            for b in content:
                                if not isinstance(b, dict) or b.get("type") != "tool_use":
                                    continue
                                nm, inp = b.get("name"), b.get("input") or {}
                                edits = []
                                if nm == "Write":
                                    edits.append((inp.get("file_path"), count_lines(inp.get("content")), 0))
                                elif nm == "Edit":
                                    edits.append((inp.get("file_path"), count_lines(inp.get("new_string")), count_lines(inp.get("old_string"))))
                                elif nm == "MultiEdit":
                                    for e in inp.get("edits") or []:
                                        edits.append((inp.get("file_path"), count_lines(e.get("new_string")), count_lines(e.get("old_string"))))
                                elif nm == "NotebookEdit":
                                    edits.append((inp.get("notebook_path"), count_lines(inp.get("new_source")), 0))
                                for i, (fp, a, r) in enumerate(edits):
                                    emit({"t": "edit", "uid": "c:%s:%d" % (b.get("id"), i), "ts": ts, "tool": "claude",
                                          "session": sess, "project": proj, "model": model,
                                          "doc": 1 if is_doc(fp) else 0, "added": a, "removed": r})
                    elif t == "user" and kind == "main" and not d.get("isMeta"):
                        c = msg.get("content")
                        text = None
                        if isinstance(c, str):
                            text = c
                        elif isinstance(c, list) and c and all(isinstance(b, dict) and b.get("type") == "text" for b in c):
                            text = "\n".join(b.get("text", "") for b in c)
                        if not text or text.startswith(("<command-", "<local-command", "<system-reminder", "<task-notification",
                                                       "[Request interrupted", "Caveat:", "<bash-")):
                            continue
                        pending.append({"t": "prompt", "uid": "c:" + str(d.get("uuid")), "ts": ts, "tool": "claude",
                                        "session": sess, "project": proj, "model": "", "chars": len(text),
                                        "words": len(text.split()), "est_tokens": (len(text) + 3) // 4})
            for p in pending:  # prompts never followed by a reply
                emit(p)
    for r in llm.values():
        emit(r)
    for c in cwds:
        emit({"t": "cwd", "cwd": c})


# ---------------------------------------------------------------------- Codex

PATCH_FILE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$")


def patch_stats(text):
    """Yield (path, added, removed) from an apply_patch envelope found in text."""
    if "*** Begin Patch" not in text:
        return
    if "\\n" in text and text.count("\n") < 3:
        text = text.replace("\\n", "\n")
    cur, a, r = None, 0, 0
    for ln in text.split("\n"):
        m = PATCH_FILE.match(ln.strip("\r"))
        if m:
            if cur:
                yield cur, a, r
            cur, a, r = m.group(2).strip().rstrip("\\\"'`"), 0, 0
        elif cur and ln.startswith("+") and not ln.startswith("+++"):
            a += 1
        elif cur and ln.startswith("-") and not ln.startswith("---"):
            r += 1
        elif ln.startswith("*** End Patch"):
            if cur:
                yield cur, a, r
            cur = None
    if cur:
        yield cur, a, r


CODEX_KEYS = (b"token_usage_record", b'"turn_context"', b'"session_meta"', b'"role":"user"',
              b"Begin Patch", b'"token_count"')


def collect_codex(cutoff):
    roots = [os.path.join(HOME, ".codex", d) for d in ("sessions", "archived_sessions")]
    cwds = set()
    seen_resp = set()
    for dp, _, fns in (t for r in roots if os.path.isdir(r) for t in os.walk(r)):
        for fn in fns:
            if not fn.endswith(".jsonl"):
                continue
            path = os.path.join(dp, fn)
            try:
                if os.path.getmtime(path) < cutoff:
                    continue
                fh = open(path, "rb")
            except OSError:
                continue
            sess, cwd, kind = fn[:-6], None, "main"
            turn_model, model = {}, "unknown"
            tur, fallback, last_total = [], [], None
            ptexts = []
            with fh:
                for line in fh:
                    if not any(k in line for k in CODEX_KEYS):
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    t, p = d.get("type"), d.get("payload")
                    if not isinstance(p, dict):
                        continue
                    ts = parse_ts(d.get("timestamp") or "")
                    if ts is None:
                        continue
                    if t == "session_meta":
                        sess = p.get("id") or sess
                        cwd = p.get("cwd") or cwd
                        ts_kind = p.get("thread_source") or "user"
                        kind = "main" if ts_kind == "user" else ("review" if "guardian" in ts_kind else "subagent")
                    elif t == "turn_context":
                        model = p.get("model") or model
                        cwd = p.get("cwd") or cwd
                        turn_model[p.get("turn_id")] = model
                        if cwd:
                            cwds.add(cwd)
                    elif t == "token_usage_record":
                        rid = p.get("response_id")
                        if rid and rid in seen_resp:
                            continue
                        seen_resp.add(rid)
                        tur.append((ts, rid, p.get("usage") or {}, turn_model.get(p.get("turn_id"), model)))
                    elif t == "event_msg" and p.get("type") == "token_count":
                        info = p.get("info") or {}
                        tot = (info.get("total_token_usage") or {}).get("total_tokens")
                        if tot and tot != last_total:
                            last_total = tot
                            fallback.append((ts, "%s:%s" % (sess, tot), info.get("last_token_usage") or {}, model))
                    elif t == "response_item":
                        pt = p.get("type")
                        if pt == "message" and p.get("role") == "user" and kind == "main":
                            for c in p.get("content") or []:
                                tx = c.get("text") if isinstance(c, dict) else None
                                if not tx:
                                    continue
                                if "## My request for Codex:" in tx:
                                    tx = tx.split("## My request for Codex:", 1)[1].strip()
                                if tx.startswith(("#", "<", ">>>", "Host notice", "Retained")) or not tx.strip():
                                    continue
                                emit({"t": "prompt", "uid": "x:%s:%s" % (sess, d.get("ordinal")), "ts": ts, "tool": "codex",
                                      "session": sess, "project": project_of(cwd), "model": model,
                                      "chars": len(tx), "words": len(tx.split()), "est_tokens": (len(tx) + 3) // 4})
                        elif pt in ("custom_tool_call", "function_call"):
                            txt = p.get("input") or p.get("arguments") or ""
                            if isinstance(txt, str) and "Begin Patch" in txt:
                                ptexts.append((ts, p.get("call_id") or p.get("id"), txt, model))
            proj = project_of(cwd)
            for ts, rid, u, mdl in (tur or fallback):
                inp, cached = u.get("input_tokens") or 0, u.get("cached_input_tokens") or 0
                emit({"t": "llm", "uid": "x:" + str(rid), "ts": ts, "tool": "codex", "session": sess, "project": proj,
                      "model": mdl, "kind": kind, "client": "codex",
                      "fresh_in": max(inp - cached, 0), "cache_read": cached,
                      "cache_write": u.get("cache_write_input_tokens") or 0,
                      "out": u.get("output_tokens") or 0, "reasoning": u.get("reasoning_output_tokens") or 0})
            for ts, cid, txt, mdl in ptexts:
                for i, (fp, a, r) in enumerate(patch_stats(txt)):
                    emit({"t": "edit", "uid": "x:%s:%d" % (cid, i), "ts": ts, "tool": "codex", "session": sess,
                          "project": proj, "model": mdl, "doc": 1 if is_doc(fp) else 0, "added": a, "removed": r})
    for c in cwds:
        emit({"t": "cwd", "cwd": c})


# ------------------------------------------------------------------------ Git

def git(repo, *args):
    try:
        return subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True, timeout=120,
                              errors="replace").stdout
    except Exception:
        return ""


def find_repos(cwds, roots):
    repos = set()
    for c in cwds:
        if not os.path.isdir(c):
            continue
        common = git(c, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        if common.endswith("/.git"):
            repos.add(os.path.dirname(common))
        elif common:
            top = git(c, "rev-parse", "--show-toplevel").strip()
            if top:
                repos.add(top)
    skip = {"node_modules", ".venv", "venv", "target", "dist", "build", "Library", ".cache", ".npm", ".cargo"}
    for r in roots:
        r = os.path.expanduser(r)
        base_depth = r.rstrip("/").count("/")
        for dp, dns, _ in os.walk(r):
            if ".git" in dns:
                repos.add(dp)
                dns[:] = []
                continue
            if dp.count("/") - base_depth >= 3:
                dns[:] = []
                continue
            dns[:] = [d for d in dns if d not in skip and not d.startswith(".")]
    return sorted(repos)


def collect_git(repos, days, emails):
    seen = set()
    for repo in repos:
        authors = set(emails)
        e = git(repo, "config", "user.email").strip()
        if e:
            authors.add(e)
        cmd = ["log", "--all", "--no-merges", "--since=%d days ago" % days, "--numstat",
               "--format=%x1e%H%x1f%at%x1f%ae%x1f%(trailers:key=Co-authored-by,valueonly,separator=%x1d)"]
        for a in sorted(authors):
            cmd.append("--author=" + re.escape(a))
        out = git(repo, *cmd)
        proj = os.path.basename(repo.rstrip("/"))
        for chunk in out.split("\x1e"):
            if not chunk.strip():
                continue
            head, _, rest = chunk.partition("\n")
            parts = head.split("\x1f")
            if len(parts) < 4:
                continue
            sha, at, ae, trailers = parts[0], parts[1], parts[2], parts[3]
            if sha in seen:
                continue
            seen.add(sha)
            # %(trailers) may span multiple lines; numstat lines are tab-separated
            ca = cr = da = dr = nf = nd = 0
            for ln in (trailers + "\n" + rest).split("\n"):
                f = ln.split("\t")
                if len(f) != 3 or not f[0].lstrip("-").isdigit() and f[0] != "-":
                    continue
                if f[0] == "-" or SKIP_FILE.search(f[2]):
                    continue
                a_, r_ = int(f[0]), int(f[1])
                nf += 1
                if is_doc(f[2]):
                    da += a_; dr += r_; nd += 1
                else:
                    ca += a_; cr += r_
            agent, model = "none", ""
            low = trailers.lower()
            if "anthropic.com" in low or "claude" in low:
                agent = "claude"
                m = re.search(r"(Claude[^<\n]*?)\s*<", trailers, re.I)
                model = m.group(1).strip() if m else "Claude"
            elif "codex" in low or "openai" in low:
                agent = "codex"
                m = re.search(r"((?:Codex|OpenAI|GPT)[^<\n]*?)\s*<", trailers, re.I)
                model = m.group(1).strip() if m else "Codex"
            emit({"t": "commit", "uid": sha, "ts": int(at), "repo": repo, "project": proj, "author": ae,
                  "added_code": ca, "removed_code": cr, "added_doc": da, "removed_doc": dr,
                  "files": nf, "doc_files": nd, "agent": agent, "model": model})



# ------------------------------------------------- existing telemetry adapters
# Emits {"t":"metric","src":..,"m":canonical,"ts":..,"iv":seconds,"v":avg,"pk":peak}.
# Canonical units: percentages 0-100, memory MB, disk/net KB/s, temperature C.

def metric(src, m, ts, iv, v, pk=None):
    if v is None:
        return
    emit({"t": "metric", "src": src, "m": m, "ts": int(ts), "iv": int(iv), "v": round(float(v), 3),
          "pk": None if pk is None else round(float(pk), 3)})


def adapter_dirs():
    """Directories holding endpoint-metrics.csv / container-metrics.csv (5-minute samples), from
    $USAGE_ENDPOINT_CSV_DIRS (colon-separated) or ~/.config/usage/config.json {"endpoint_csv_dirs": [...]}."""
    env = os.environ.get("USAGE_ENDPOINT_CSV_DIRS")
    if env:
        return [d for d in env.split(":") if d]
    try:
        return list(json.load(open(os.path.join(HOME, ".config", "usage", "config.json"))).get("endpoint_csv_dirs", []))
    except (OSError, ValueError):
        return []


def collect_endpoint_csv(since):
    """Generic endpoint-metrics / container-metrics CSV adapter (see docs/adapters.md for the column contract)."""
    import csv
    dirs = adapter_dirs()
    fmap = {"cpu_busy_pct": "cpu", "cpu_user_pct": "cpu_user", "cpu_sys_pct": "cpu_sys", "am_used_pct": "mem_pct",
            "am_pressure_pct": "mem_pressure", "am_used_mb": "mem_mb", "am_compressed_mb": "mem_compressed_mb",
            "am_swap_used_mb": "swap_mb", "load_norm": "load_norm", "load1": "load1", "therm_pct": "therm",
            "disk_pct": "disk_used_pct", "comp_ratio": "comp_ratio", "demand_mb": "mem_demand_mb", "pressure": "pressure_level",
            "vm_footprint_mb": "container_vm_mb", "containers_running": "containers", "match_cpu_pct": "container_cpu_match",
            "other_cpu_pct": "container_cpu_other"}
    cum = {"decompressions_cum": "decompress_per_s", "swapouts_cum": "swapout_per_s", "swapins_cum": "swapin_per_s"}
    for d in dirs:
        for fn in ("endpoint-metrics.csv", "container-metrics.csv"):
            p = os.path.join(d, fn)
            if not os.path.isfile(p):
                continue
            try:
                rows = list(csv.DictReader(open(p, newline="", errors="replace")))
            except OSError:
                continue
            prev = None
            for r in rows:
                ts = parse_ts(r.get("ts_utc") or "")
                if ts is None:
                    continue
                if ts >= since:
                    for k, m in fmap.items():
                        if r.get(k) not in (None, ""):
                            try:
                                v = float(r[k])
                            except ValueError:
                                continue
                            if v < 0:
                                continue  # -1 sentinel = unavailable
                            metric("endpointcsv", m, ts, 300, v)
                    if r.get("top_apps_mem_plus_cmprs"):
                        emit({"t": "note", "src": "endpointcsv", "k": "top_apps", "ts": ts, "text": r["top_apps_mem_plus_cmprs"][:200]})
                    if prev:
                        dt = ts - prev[0]
                        for k, m in cum.items():
                            try:
                                dv = float(r[k]) - prev[1][k]
                            except (KeyError, ValueError):
                                continue
                            if dt > 0 and dv >= 0:  # counters reset at reboot
                                metric("endpointcsv", m, ts, 300, dv / dt)
                try:
                    prev = (ts, {k: float(r[k]) for k in cum if r.get(k) not in (None, "")})
                except ValueError:
                    prev = None


def collect_istat(since):
    """iStat Menus 7 history.db: multi-resolution ring buffers (finest tiers expire)."""
    import sqlite3, shutil, tempfile
    src = os.path.join(HOME, "Library", "Application Support", "iStat Menus 7", "history", "history.db")
    if not os.path.isfile(src):
        return
    tmp = tempfile.mkdtemp(prefix="istat")
    try:
        for suf in ("", "-wal", "-shm"):
            if os.path.exists(src + suf):
                shutil.copy(src + suf, os.path.join(tmp, "history.db" + suf))
        db = sqlite3.connect(os.path.join(tmp, "history.db"))
        q = lambda sql: db.execute(sql, (since,))
        for ts, iv, u, s in q("SELECT time,interval,user,system FROM cpu WHERE time>=?"):
            metric("istat", "cpu", ts, iv, u + s)
            metric("istat", "cpu_user", ts, iv, u); metric("istat", "cpu_sys", ts, iv, s)
        for ts, iv, pr, wired, app, comp, total in q("SELECT time,interval,pressure,wired,app,compressed,total FROM memory WHERE time>=?"):
            metric("istat", "mem_pressure", ts, iv, pr)
            metric("istat", "mem_mb", ts, iv, wired + app + comp)
            if total:
                metric("istat", "mem_pct", ts, iv, 100.0 * (wired + app + comp) / total)
            metric("istat", "mem_compressed_mb", ts, iv, comp)
        for ts, iv, one, five, _f in q("SELECT time,interval,one,five,fifteen FROM load WHERE time>=?"):
            metric("istat", "load1", ts, iv, one)
        for ts, iv, rd, wr, rp, wp in q("SELECT time,interval,SUM(read),SUM(write),MAX(readpeak),MAX(writepeak) FROM diskactivity WHERE time>=? GROUP BY 1,2"):
            metric("istat", "disk_r", ts, iv, rd, rp); metric("istat", "disk_w", ts, iv, wr, wp)
        # per-interface keys are hashed; tunnels duplicate physical traffic, so take the busiest key per sample
        for ts, iv, up, dn, upk, dnk in q("SELECT time,interval,MAX(upload),MAX(download),MAX(uploadpeak),MAX(downloadpeak) FROM network WHERE time>=? GROUP BY 1,2"):
            metric("istat", "net_up", ts, iv, up, upk); metric("istat", "net_down", ts, iv, dn, dnk)
        for ts, iv, p, mem in q("SELECT time,interval,MAX(processor),MAX(memory) FROM gpu WHERE time>=? GROUP BY 1,2"):
            metric("istat", "gpu", ts, iv, p); metric("istat", "gpu_mem", ts, iv, mem)
        for ts, iv, sw in q("SELECT time,interval,swap FROM swap WHERE time>=? AND interval>=30"):
            metric("istat", "swap_mb", ts, iv, sw)
        # sensor names are hashed; heuristics: 20-125 = temperature, >300 = fan RPM
        for ts, iv, tmax, tpk, fmax in q("""SELECT time,interval,MAX(CASE WHEN value BETWEEN 20 AND 125 THEN value END),
              MAX(CASE WHEN peak BETWEEN 20 AND 125 THEN peak END),MAX(CASE WHEN value>300 THEN value END) FROM sensors WHERE time>=? GROUP BY 1,2"""):
            metric("istat", "temp_max", ts, iv, tmax, tpk); metric("istat", "fan_rpm", ts, iv, fmax)
        for ts, iv, pct, srcv in q("SELECT time,interval,percentage,source FROM battery WHERE key=1 AND time>=? AND interval>=90"):
            metric("istat", "battery_pct", ts, iv, pct)
        db.close()
    except Exception as e:  # never let an adapter break the run
        sys.stderr.write("istat adapter: %s\n" % e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ Perf sampler

def sh(cmd, timeout=15):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, errors="replace").stdout
    except Exception:
        return ""


def raw_counters():
    """Cumulative counters + instantaneous gauges for this OS."""
    c = {"ts": time.time(), "load1": os.getloadavg()[0] if hasattr(os, "getloadavg") else None}
    sysname = platform.system()
    if sysname == "Darwin":
        top = sh("top -l 2 -n 0 -s 1 | grep '^CPU usage' | tail -1")
        m = re.search(r"([\d.]+)% idle", top)
        c["cpu"] = round(100 - float(m.group(1)), 2) if m else None
        vm = sh("vm_stat")
        ps = re.search(r"page size of (\d+)", vm)
        ps = int(ps.group(1)) if ps else 16384
        pg = lambda k: int((re.search(k + r":\s+(\d+)", vm) or [0, 0])[1])
        used = (pg("Pages active") + pg("Pages wired down") + pg("Pages occupied by compressor")) * ps
        total = int(sh("sysctl -n hw.memsize").strip() or 0)
        c["mem_mb"] = used // 2**20
        c["mem_pct"] = round(100.0 * used / total, 2) if total else None
        io = sh("ioreg -c IOBlockStorageDriver -r -w0")
        c["disk_r_b"] = sum(int(x) for x in re.findall(r'"Bytes \(Read\)"=(\d+)', io))
        c["disk_w_b"] = sum(int(x) for x in re.findall(r'"Bytes \(Write\)"=(\d+)', io))
        rx = tx = 0
        for ln in sh("netstat -ib").splitlines()[1:]:
            f = ln.split()
            if len(f) >= 10 and f[0].startswith("en") and f[2].startswith("<Link"):
                o = 6 if len(f) >= 11 else 5
                rx += int(f[o]); tx += int(f[o + 3])
        c["net_rx_b"], c["net_tx_b"] = rx, tx
        hid = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', sh("ioreg -c IOHIDSystem -d 4"))
        c["idle_s"] = round(int(hid.group(1)) / 1e9, 1) if hid else None
    else:  # Linux
        try:
            f = open("/proc/stat").readline().split()[1:]
            v = list(map(int, f))
            c["cpu_raw"] = (sum(v) - v[3] - (v[4] if len(v) > 4 else 0), sum(v))
            mi = dict((k, int(x.split()[0])) for k, x in
                      (ln.split(":", 1) for ln in open("/proc/meminfo") if ":" in ln))
            used = mi["MemTotal"] - mi.get("MemAvailable", mi.get("MemFree", 0))
            c["mem_mb"] = used // 1024
            c["mem_pct"] = round(100.0 * used / mi["MemTotal"], 2)
            r = w = 0
            for ln in open("/proc/diskstats"):
                p = ln.split()
                if re.match(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|xvd[a-z]+|mmcblk\d+)$", p[2]):
                    r += int(p[5]) * 512; w += int(p[9]) * 512
            c["disk_r_b"], c["disk_w_b"] = r, w
            rx = tx = 0
            for ln in list(open("/proc/net/dev"))[2:]:
                n, _, rest = ln.partition(":")
                if n.strip() != "lo":
                    p = rest.split(); rx += int(p[0]); tx += int(p[8])
            c["net_rx_b"], c["net_tx_b"] = rx, tx
        except Exception:
            pass
    return c


def take_sample():
    cur = raw_counters()
    try:
        prev = json.load(open(PERF_STATE))
    except Exception:
        prev = None
    s = {"t": "perf", "uid": str(int(cur["ts"])), "ts": int(cur["ts"]), "load1": cur.get("load1"),
         "mem_pct": cur.get("mem_pct"), "mem_mb": cur.get("mem_mb"), "idle_s": cur.get("idle_s"),
         "cpu": cur.get("cpu")}
    if prev:
        dt = cur["ts"] - prev["ts"]
        if dt > 0:
            for k, o in (("disk_r_b", "disk_r"), ("disk_w_b", "disk_w"), ("net_rx_b", "net_rx"), ("net_tx_b", "net_tx")):
                if k in cur and k in prev and cur[k] >= prev[k]:
                    s[o] = int((cur[k] - prev[k]) / dt)
            if "cpu_raw" in cur and "cpu_raw" in prev:
                db, dtot = cur["cpu_raw"][0] - prev["cpu_raw"][0], cur["cpu_raw"][1] - prev["cpu_raw"][1]
                s["cpu"] = round(100.0 * db / dtot, 2) if dtot > 0 else None
    os.makedirs(STATE_DIR, exist_ok=True)
    json.dump(cur, open(PERF_STATE, "w"))
    with open(PERF_FILE, "a") as f:
        f.write(json.dumps(s, separators=(",", ":")) + "\n")
    return s


def emit_perf(since):
    try:
        for ln in open(PERF_FILE):
            try:
                d = json.loads(ln)
            except Exception:
                continue
            if d["ts"] >= since:
                emit(d)
    except OSError:
        pass


def sampler_install():
    os.makedirs(STATE_DIR, exist_ok=True)
    script = os.path.join(STATE_DIR, "collect.py")
    if "__file__" in globals() and os.path.abspath(__file__) != script and os.path.exists(__file__):
        import shutil
        shutil.copy(__file__, script)
    if platform.system() == "Darwin":
        plist = os.path.join(HOME, "Library", "LaunchAgents", "ai.agentusage.sampler.plist")
        open(plist, "w").write("""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>ai.agentusage.sampler</string>
<key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>%s</string><string>sample</string><string>--loop</string><string>30</string></array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardErrorPath</key><string>%s/sampler.err</string>
</dict></plist>
""" % (script, STATE_DIR))
        print("wrote %s\nactivate with:\n  launchctl bootstrap gui/$(id -u) %s" % (plist, plist))
    else:
        unit_dir = os.path.join(HOME, ".config", "systemd", "user")
        os.makedirs(unit_dir, exist_ok=True)
        open(os.path.join(unit_dir, "agentusage-sampler.service"), "w").write(
            "[Unit]\nDescription=agentusage perf sampler\n[Service]\nExecStart=%s %s sample --loop 30\nRestart=always\n[Install]\nWantedBy=default.target\n"
            % (sys.executable, script))
        print("wrote systemd unit; activate with:\n  systemctl --user enable --now agentusage-sampler")


# ------------------------------------------------------------------------ main

def arg(name, default=None, flag=False):
    a = sys.argv
    if name in a:
        return True if flag else a[a.index(name) + 1]
    return False if flag else default


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "collect"
    if cmd == "sample":
        loop = arg("--loop")
        while True:
            s = take_sample()
            if not loop:
                print(json.dumps(s)); return
            time.sleep(float(loop))
    if cmd == "sampler-install":
        return sampler_install()
    days = float(arg("--days", 400))
    now = time.time()
    emit({"t": "hello", "machine": socket.gethostname().split(".")[0], "os": platform.system(),
          "ncpu": os.cpu_count(), "version": VERSION, "now": int(now)})
    cutoff = now - days * 86400
    cwds = set()

    class Tap:  # capture cwd events for repo discovery while forwarding everything else
        pass
    import io
    real = sys.stdout
    buf = io.StringIO()
    # stream through, but intercept cwd records
    class Filter(io.TextIOBase):
        def write(self, s):
            for ln in s.splitlines():
                if ln.startswith('{"t":"cwd"'):
                    cwds.add(json.loads(ln)["cwd"])
                else:
                    real.write(ln + "\n")
            return len(s)
    sys.stdout = Filter()
    try:
        collect_claude(cutoff)
        collect_codex(cutoff)
    finally:
        sys.stdout = real
    psince = int(float(arg("--perf-since", 0)))
    emit_perf(psince)
    if not arg("--no-metrics", flag=True):
        collect_endpoint_csv(psince)
        collect_istat(psince)
    if not arg("--no-git", flag=True):
        roots = [r for r in (arg("--roots", "~/Projects") or "").split(",") if r]
        emails = [e for e in (arg("--emails", "") or "").split(",") if e]
        collect_git(find_repos(cwds, roots), int(days), emails)
    emit({"t": "done", "now": int(time.time())})


if __name__ == "__main__":
    main()
