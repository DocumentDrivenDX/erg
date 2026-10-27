#!/usr/bin/env python3
"""erg collector: single-file, stdlib-only, Python 3.8+.

Runs locally or is piped over SSH (`ssh host python3 - collect < collect.py`).
Emits NDJSON events on stdout; every event has a stable `uid` so the central
store can upsert idempotently.

  collect.py collect [--days N] [--perf-since TS] [--no-git] [--roots A,B] [--emails a@b,c@d]
  collect.py sample  [--loop SECONDS]          # append one perf sample (or loop forever)
  collect.py sampler-install                   # write launchd/systemd unit for the sampler
"""

import glob
import hashlib
import json
import math
import os
import platform
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from urllib.parse import urlsplit

VERSION = 2
HOME = os.path.expanduser("~")
STATE_DIR = os.environ.get("ERG_STATE_DIR") or os.path.join(HOME, ".local", "share", "erg")
PERF_FILE = os.path.join(STATE_DIR, "perf.ndjson")
PERF_STATE = os.path.join(STATE_DIR, "perf.state.json")

DOC_EXT = {".md", ".mdx", ".rst", ".txt", ".adoc", ".org", ".tex", ".markdown"}
SKIP_FILE = re.compile(
    r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|Cargo\.lock|uv\.lock|poetry\.lock|"
    r"go\.sum|Gemfile\.lock|bun\.lockb?|composer\.lock)$|\.min\.(js|css)$|\.map$|"
    r"(^|/)(node_modules|vendor|dist|build|\.next|__pycache__)/|\.(svg|png|jpg|jpeg|gif|ico|pdf|woff2?|ttf|lock|csv|tsv|jsonl|ndjson|parquet|snap|xml|ipynb|json|sqlite|db)$"
)


def emit(o):
    sys.stdout.write(json.dumps(o, separators=(",", ":")) + "\n")


def is_doc(path):
    return os.path.splitext(path or "")[1].lower() in DOC_EXT


def parse_ts(s):
    try:
        from datetime import datetime, timezone

        if isinstance(s, (int, float)):
            return int(s / 1000 if s > 1e11 else s)
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(
            d.replace(tzinfo=timezone.utc).timestamp() if d.tzinfo is None else d.timestamp()
        )
    except (ValueError, TypeError, AttributeError):
        return None


@contextmanager
def transcript(path):
    """Stream archives without modifying the agent's files."""
    if path.endswith(".zst"):
        if not shutil.which("zstd"):
            raise RuntimeError("zstd is required to read compressed Codex archives")
        proc = subprocess.Popen(
            ["zstd", "-qdc", path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        try:
            yield proc.stdout
            if proc.wait() != 0:
                raise RuntimeError("cannot decompress archive")
        finally:
            proc.stdout.close()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    else:
        with open(path, "rb") as fh:
            yield fh


def session_record(tool, session, ts, cwd=None, **extra):
    provenance = extra.pop("provenance_override", "reconstructed")
    return dict(
        t="session",
        uid=tool + ":" + session,
        session=session,
        ts=ts,
        tool=tool,
        project=project_of(cwd),
        provenance=provenance,
        **extra,
    )


def machine_name():
    """Persist enrollment identity; hostname is only a readable initial label."""
    path = os.path.join(STATE_DIR, "machine-id")
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        import uuid

        name = socket.gethostname().split(".")[0] + "-" + uuid.uuid4().hex[:12]
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(name)
        except FileExistsError:
            with open(path) as f:
                return f.read().strip()
        return name


@contextmanager
def collector_lock():
    """One scheduled/continuous collector per enrollment on macOS/Linux."""
    import fcntl

    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    with open(os.path.join(STATE_DIR, "collector.lock"), "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("a collector is already running for this enrollment") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def source_status(tool, root, files=0, errors=0, **extra):
    emit(
        dict(
            t="source",
            uid=tool,
            tool=tool,
            ts=int(time.time()),
            files=files,
            errors=errors,
            status="partial"
            if errors or extra.get("missing")
            else "available"
            if files
            else "no_history",
            installed=bool(shutil.which(tool)),
            **extra,
        )
    )


def project_of(cwd):
    if not cwd:
        return "(unknown)"
    if (
        "local-agent-mode-sessions" in cwd
        or "Claude/scratch-workspaces" in cwd
        or "claude-501" in cwd
    ):
        return "(claude-scratch)"
    m = re.search(r"/\.claude/worktrees/", cwd)
    if m:
        return os.path.basename(cwd[: m.start()]) or "(unknown)"
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


# Discovery is separate from parsing: wrappers relocate native homes, not formats.
def discovery_config():
    path = os.environ.get("ERG_DISCOVERY_FILE") or os.path.join(
        HOME, ".config", "erg", "discovery.json"
    )
    try:
        with open(path) as f:
            value = json.load(f)
        if not isinstance(value, dict):
            raise ValueError("discovery config must be an object")
        for key in ("codex_homes", "claude_homes", "pi_session_dirs", "opencode_data_dirs"):
            if key in value and (
                not isinstance(value[key], list) or not all(isinstance(x, str) for x in value[key])
            ):
                raise ValueError("discovery paths must be lists of strings")
        return value
    except FileNotFoundError:
        return {}


def omni_home():
    return os.path.expanduser(
        discovery_config().get("omnigent_home")
        or os.environ.get("OMNIGENT_DATA_DIR")
        or os.path.join(HOME, ".omnigent")
    )


def native_roots(tool):
    conf = discovery_config()
    base = conf.get(tool + "_homes", []) + [os.path.join(HOME, "." + tool)]
    env = os.environ.get("CODEX_HOME" if tool == "codex" else "CLAUDE_CONFIG_DIR")
    if env:
        base.append(env)
    bridge = os.path.join(omni_home(), tool + "-native")
    base += glob.glob(os.path.join(bridge, "*", tool + "-home"))
    base += glob.glob(os.path.join(bridge, "*", "." + tool))
    # Read only the specific path field from wrapper state, never auth/config values.
    for path in glob.glob(os.path.join(bridge, "*", "state.json")):
        try:
            with open(path) as f:
                d = json.load(f)
            value = d.get(tool + "_home") or (
                d.get("claude_config_dir") if tool == "claude" else None
            )
            if isinstance(value, str):
                base.append(value)
        except (OSError, ValueError, AttributeError):
            pass
    suffixes = ("sessions", "archived_sessions") if tool == "codex" else ("projects",)
    roots = [os.path.join(os.path.expanduser(b), d) for b in base for d in suffixes]
    # Some Claude bridges persist their project tree directly under the bridge.
    if tool == "claude":
        roots += glob.glob(os.path.join(bridge, "*", "projects"))
    return list(dict.fromkeys(os.path.realpath(r) for r in roots))


def log_paths(roots, cutoff=0, on_error=None):
    seen = set()
    for root in roots:

        def report(error, operation="walk"):
            # Missing optional home directories are normal. A vanished explicit
            # file or an unreadable discovered directory remains visible.
            path = str(getattr(error, "filename", None) or root)
            if (
                isinstance(error, FileNotFoundError)
                and operation == "walk"
                and path == str(root)
                and not path.endswith((".jsonl", ".jsonl.zst"))
            ):
                return
            if on_error:
                on_error(path, operation, getattr(error, "errno", None))

        if os.path.isfile(root):
            candidates = [root]
        else:
            candidates = (
                os.path.join(dp, fn) for dp, _, fns in os.walk(root, onerror=report) for fn in fns
            )
        for path in candidates:
            if not path.endswith((".jsonl", ".jsonl.zst")):
                continue
            try:
                stat = os.stat(path)
                key = (stat.st_dev, stat.st_ino)
                if key in seen or stat.st_mtime < cutoff:
                    continue
                seen.add(key)
                yield path
            except OSError as error:
                report(error, "stat")
                continue


def claude_roots():
    roots = native_roots("claude")
    import tempfile

    bridges = [os.path.join(omni_home(), "claude-native")]
    if HOME == os.path.expanduser("~"):
        bridges.append(
            os.path.join(tempfile.gettempdir(), "omnigent-" + str(os.getuid()), "claude-native")
        )
    override = os.environ.get("HARNESS_CLAUDE_NATIVE_BRIDGE_DIR")
    paths = [os.path.join(override, "state.json")] if override else []
    for bridge in bridges:
        paths += glob.glob(os.path.join(bridge, "*", "state.json"))
    # Observer hooks can identify transcripts outside any known home.
    for path in paths:
        try:
            with open(path) as f:
                d = json.load(f)
            value = d.get("transcript_path")
            if isinstance(value, str):
                roots.append(value)
        except (OSError, ValueError, AttributeError):
            pass
    return roots


def pi_roots():
    conf = discovery_config()
    agent = os.environ.get("PI_CODING_AGENT_DIR") or os.path.join(HOME, ".pi", "agent")
    state = os.environ.get("ERG_STATE_DIR") or os.path.join(HOME, ".local", "share", "erg")
    roots = [os.path.join(agent, "sessions"), os.path.join(state, "pi-snapshots")]
    if os.environ.get("PI_CODING_AGENT_SESSION_DIR"):
        roots.append(os.environ["PI_CODING_AGENT_SESSION_DIR"])
    roots += conf.get("pi_session_dirs", [])
    roots += glob.glob(os.path.join(omni_home(), "pi-native", "*", "sessions"))
    return list(dict.fromkeys(os.path.realpath(os.path.expanduser(r)) for r in roots))


def pi_usage_kind(message):
    usage = message.get("usage") or {}
    if message.get("usageProvenance") == "synthetic_history" or message.get("timestamp") == 0:
        return "synthetic_history"
    if any((usage.get(k) or 0) > 0 for k in ("input", "output", "cacheRead", "cacheWrite")):
        return "observed"
    return "unknown"


def reported_cost(usage):
    value = (usage.get("cost") or {}).get("total") if isinstance(usage.get("cost"), dict) else None
    return (
        value
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
        else None
    )


def collect_pi(cutoff):
    files = errors = 0
    for path in log_paths(pi_roots(), cutoff):
        files += 1
        meta = None
        provider, model = "", "unknown"
        calls, prompts = {}, []
        try:
            with transcript(path) as stream:
                for n, line in enumerate(stream):
                    try:
                        d = json.loads(line)
                    except ValueError:
                        errors += 1
                        continue
                    ts = parse_ts(d.get("timestamp"))
                    if not ts:
                        continue
                    if d.get("type") == "session":
                        sid = str(d.get("id") or "")
                        if sid:
                            meta = session_record(
                                "pi",
                                sid,
                                ts,
                                d.get("cwd"),
                                surface="cli",
                                agent_version=str(d.get("version") or ""),
                                parent_session=d.get("parentSession") or "",
                            )
                    elif d.get("type") == "model_change":
                        provider, model = d.get("provider") or "", d.get("modelId") or "unknown"
                    elif d.get("type") == "erg_route" and meta:
                        # Package runtime enriches lifecycle snapshots. Standalone
                        # SSH collection still supports metadata without dependencies.
                        try:
                            from erg import routes

                            for route in routes.native(
                                "pi", d, d, ts, dict(session=meta["session"])
                            ):
                                emit(route)
                        except ImportError:
                            pass
                    elif d.get("type") == "message" and meta:
                        meta["last_ts"] = max(meta.get("last_ts", meta["ts"]), ts)
                        m = d.get("message") or {}
                        if m.get("role") == "assistant":
                            usage = m.get("usage")
                            if not isinstance(usage, dict) or not any(
                                k in usage for k in ("input", "output", "cacheRead", "cacheWrite")
                            ):
                                continue
                            try:
                                from erg import routes

                                for route in routes.native(
                                    "pi", d, d, ts, dict(session=meta["session"])
                                ):
                                    emit(route)
                            except ImportError:
                                pass
                            uid = "pi:" + meta["session"] + ":" + str(d.get("id") or "line:%d" % n)
                            rec = dict(
                                t="llm",
                                uid=uid,
                                ts=ts,
                                tool="pi",
                                session=meta["session"],
                                project=meta["project"],
                                model=m.get("model") or model,
                                provider=m.get("provider") or provider,
                                kind="main",
                                client="pi",
                                fresh_in=usage.get("input") or 0,
                                cache_read=usage.get("cacheRead") or 0,
                                cache_write=usage.get("cacheWrite") or 0,
                                out=usage.get("output") or 0,
                                reasoning=usage.get("reasoning") or 0,
                                response_id=m.get("responseId") or "",
                                request_id=m.get("requestId") or "",
                                usage_kind=pi_usage_kind(m),
                                reported_cost_usd=reported_cost(usage),
                                reported_cost_source="agent_reported_estimate"
                                if isinstance(usage.get("cost"), dict)
                                else None,
                            )
                            if uid not in calls or rec["out"] >= calls[uid]["out"]:
                                calls[uid] = rec
                        elif m.get("role") == "user":
                            if (
                                m.get("timestamp") == 0
                                or m.get("usageProvenance") == "synthetic_history"
                            ):
                                continue
                            content = m.get("content") or []
                            text = (
                                content
                                if isinstance(content, str)
                                else "\n".join(
                                    b.get("text", "")
                                    for b in content
                                    if isinstance(b, dict) and b.get("type") == "text"
                                )
                            )
                            stats = m.get("inputStats") or dict(
                                chars=len(text),
                                words=len(text.split()),
                                est_tokens=(len(text) + 3) // 4,
                                origin="unknown",
                                provenance="native_user_message",
                            )
                            prompts.append(
                                dict(
                                    t="prompt",
                                    uid="pi:" + meta["session"] + ":" + str(d.get("id") or n),
                                    ts=ts,
                                    tool="pi",
                                    session=meta["session"],
                                    project=meta["project"],
                                    model=model,
                                    **{
                                        k: stats.get(k)
                                        for k in (
                                            "chars",
                                            "words",
                                            "est_tokens",
                                            "origin",
                                            "provenance",
                                        )
                                    },
                                )
                            )
            if meta:
                providers = {c["provider"] for c in calls.values() if c.get("provider")}
                meta.update(
                    provider=provider or (next(iter(providers)) if len(providers) == 1 else ""),
                    usage_status="observed"
                    if any(c.get("usage_kind") == "observed" for c in calls.values())
                    else "unknown",
                )
                emit(meta)
                for event in list(calls.values()) + prompts:
                    emit(event)
        except (OSError, RuntimeError, TypeError, AttributeError):
            errors += 1
    source_status(
        "pi",
        "",
        files,
        errors,
        detail="Default/configured session directories and Omnigent Pi bridges. Pruned histories may have session summaries only.",
    )


def omni_id(value):
    if isinstance(value, bytes) and len(value) == 16:
        return str(uuid.UUID(bytes=value))
    return str(value or "")


def omni_json(value):
    """Decode Omnigent's metadata framing, without loading its runtime or credentials."""
    if not value:
        return {}
    if isinstance(value, bytes):
        if value[:2] == b"\x00\x01":
            if not shutil.which("zstd"):
                raise ValueError("zstd required for compressed metadata")
            value = subprocess.run(
                ["zstd", "-qdc", "--memory=8MB"],
                input=value[2:],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=True,
            ).stdout
        elif value[:2] == b"\x00\x00":
            value = value[2:]
    if len(value) > 1048576:
        raise ValueError("metadata exceeds limit")
    result = json.loads(value)
    return result if isinstance(result, dict) else {}


def collect_omnigent(cutoff, native_sessions, state=None):
    """Read metadata only; mirrored message bodies and cumulative usage aren't calls.

    A linked native session is enriched in place. Unresolved conversations are
    retained as metadata-only sessions so missing coverage stays visible.
    """
    path = os.path.join(omni_home(), "chat.db")
    if not os.path.isfile(path):
        source_status("omnigent", path)
        return
    found = linked = errors = hits = misses = 0
    state = {} if state is None else state
    cached = state.setdefault("records", {})
    seen = set()
    by_session = {}
    for (agent, session), record in native_sessions.items():
        by_session.setdefault(session, []).append(record)

    def digest(value):
        if value is None:
            return None
        return hashlib.sha256(value.encode() if isinstance(value, str) else value).digest()

    db = None
    try:
        from urllib.parse import quote

        db = sqlite3.connect("file:" + quote(path) + "?mode=ro", uri=True)
        db.execute("PRAGMA query_only=ON")
        labels = {}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_labels'").fetchone():
            for cid, workspace, value in db.execute(
                "SELECT conversation_id,workspace_id,value FROM conversation_labels WHERE key='omnigent.wrapper'"
            ):
                labels[(omni_id(cid), workspace)] = value
        snapshot_column = (
            "m.inference_snapshot"
            if any(
                r[1] == "inference_snapshot"
                for r in db.execute("PRAGMA table_info(omnigent_conversation_metadata)")
            )
            else "NULL"
        )
        query = """SELECT c.id,c.created_at,c.updated_at,c.parent_conversation_id,
                   m.external_session_id,c.workspace_id,m.session_usage FROM conversations c
                   LEFT JOIN omnigent_conversation_metadata m ON c.id=m.id AND c.workspace_id=m.workspace_id"""
        query = query.replace(
            "m.session_usage FROM", "m.session_usage," + snapshot_column + " FROM"
        )
        for (
            cid,
            created,
            updated,
            parent,
            external,
            workspace,
            usage_blob,
            inference_blob,
        ) in db.execute(query):
            first, last = parse_ts(created), parse_ts(updated)
            if not first:
                errors += 1
                continue
            last = last or first
            if last < cutoff:
                continue
            found += 1
            sid = omni_id(cid)
            wrapper = labels.get((sid, workspace), "")
            hint = (
                wrapper.removesuffix("-native-ui")
                if hasattr(wrapper, "removesuffix")
                else wrapper.replace("-native-ui", "")
            )
            tool = hint if hint in ("codex", "claude", "pi", "opencode", "copilot") else "omnigent"
            key = (sid, workspace)
            seen.add(key)
            fingerprint = (digest(usage_blob), digest(inference_blob), tool, external, first)
            prior = cached.get(key)
            if prior and prior[0] == fingerprint:
                summary, route_rows = prior[1:]
                hits += 1
            else:
                misses += 1
                route_rows = []
                valid = True
                if inference_blob and external and tool != "omnigent":
                    try:
                        from erg import routes

                        route_rows = routes.omnigent_snapshot(
                            tool, external, first, omni_json(inference_blob)
                        )
                    except ImportError:
                        valid = False
                try:
                    usage = omni_json(usage_blob)
                    summary = {
                        "summary_" + name: int(usage[src])
                        for name, src in [
                            ("in", "input_tokens"),
                            ("out", "output_tokens"),
                            ("cache_read", "cache_read_input_tokens"),
                            ("cache_write", "cache_creation_input_tokens"),
                        ]
                        if isinstance(usage.get(src), (int, float)) and usage[src] >= 0
                    }
                except (ValueError, OSError, subprocess.SubprocessError, OverflowError):
                    errors += 1
                    summary = {}
                    valid = False
                if valid:
                    # Cache only allowlisted projection results and digests, not
                    # compressed blobs, credentials or complete provider config.
                    cached.pop(key, None)
                    cached[key] = (fingerprint, summary, route_rows)
                    while len(cached) > 1024:
                        cached.pop(next(iter(cached)))
            for route in route_rows:
                emit(route)
            matches = (
                by_session.get(external, [])
                if external and tool == "omnigent"
                else [native_sessions[(tool, external)]]
                if external and (tool, external) in native_sessions
                else []
            )
            if len(matches) == 1:
                r = dict(matches[0], orchestrator="omnigent", conversation_id=sid, **summary)
                emit(r)
                linked += 1
            else:
                emit(
                    session_record(
                        tool,
                        external or "omnigent:" + sid,
                        first,
                        kind="main",
                        surface="omnigent",
                        last_ts=last,
                        parent_session=omni_id(parent),
                        orchestrator="omnigent",
                        conversation_id=sid,
                        usage_status="summary_only" if summary else "unknown",
                        external_session=external or "",
                        provenance_override="omnigent_metadata",
                        **summary,
                    )
                )
    except (sqlite3.Error, ValueError, TypeError):
        errors += 1
    finally:
        if db is not None:
            db.close()
    for key in list(cached):
        if key not in seen:
            cached.pop(key, None)
    state.update(cache_hits=hits, cache_misses=misses, cached_records=len(cached))
    source_status(
        "omnigent",
        path,
        found,
        errors,
        linked=linked,
        missing=found - linked,
        detail="Conversation metadata; linked native usage counted once. Missing native histories retain summary-only usage when recorded; summaries are excluded from call charts.",
    )


# ---------------------------------------------------------------- Claude Code


def merge_claude_usage(previous, usage):
    """Merge exact-message usage fragments; omitted fields never mean zero."""
    result = dict(previous)
    fields = {
        "fresh_in": "input_tokens",
        "out": "output_tokens",
        "cache_read": "cache_read_input_tokens",
        "cache_write": "cache_creation_input_tokens",
    }
    supplied = {
        name: usage[key] for name, key in fields.items() if key in usage and usage[key] is not None
    }
    details = usage.get("output_tokens_details") or {}
    cache = usage.get("cache_creation") or {}
    if details.get("thinking_tokens") is not None:
        supplied["reasoning"] = details["thinking_tokens"]
    for name, key in (
        ("cache_write_5m", "ephemeral_5m_input_tokens"),
        ("cache_write_1h", "ephemeral_1h_input_tokens"),
    ):
        if cache.get(key) is not None:
            supplied[name] = cache[key]
    if any(
        not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v < 0
        for v in supplied.values()
    ):
        raise ValueError("invalid usage")
    if "out" in supplied and supplied["out"] < result.get("out", 0):
        return result
    for key, value in supplied.items():
        if "out" in supplied or key not in result:
            result[key] = value
    return result


def claude_edit_events(record, session, project, ts):
    message = record.get("message") or {}
    model = message.get("model") or ""
    if not model or model.startswith("<") or not isinstance(message.get("content"), list):
        return []
    rows = []
    for block in message["content"]:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name")
        inputs = block.get("input") or {}
        if not isinstance(inputs, dict):
            continue
        edits = []
        if name == "Write":
            edits.append((inputs.get("file_path"), count_lines(inputs.get("content")), 0))
        elif name == "Edit":
            edits.append(
                (
                    inputs.get("file_path"),
                    count_lines(inputs.get("new_string")),
                    count_lines(inputs.get("old_string")),
                )
            )
        elif name == "MultiEdit":
            for item in inputs.get("edits") or []:
                if isinstance(item, dict):
                    edits.append(
                        (
                            inputs.get("file_path"),
                            count_lines(item.get("new_string")),
                            count_lines(item.get("old_string")),
                        )
                    )
        elif name == "NotebookEdit":
            edits.append((inputs.get("notebook_path"), count_lines(inputs.get("new_source")), 0))
        for i, (path, added, removed) in enumerate(edits):
            rows.append(
                dict(
                    t="edit",
                    uid="c:%s:%d" % (block.get("id"), i),
                    ts=ts,
                    tool="claude",
                    session=session,
                    project=project,
                    model=model,
                    doc=int(is_doc(path)),
                    added=added,
                    removed=removed,
                )
            )
    return rows


def codex_edit_events(payload, session, project, ts, model):
    text = payload.get("input") or payload.get("arguments") or ""
    if not isinstance(text, str) or "Begin Patch" not in text:
        return []
    ident = payload.get("call_id") or payload.get("id")
    return [
        dict(
            t="edit",
            uid="x:%s:%d" % (ident, i),
            ts=ts,
            tool="codex",
            session=session,
            project=project,
            model=model,
            doc=int(is_doc(path)),
            added=added,
            removed=removed,
        )
        for i, (path, added, removed) in enumerate(patch_stats(text))
    ]


def collect_claude(cutoff):
    roots = claude_roots()
    llm = {}
    usage_parts = {}
    cwds = set()
    sessions = {}
    file_count = 0
    for path in log_paths(roots, cutoff):
        fn = os.path.basename(path)
        try:
            fh = transcript(path)
            file_count += 1
        except OSError:
            continue
        pending = []
        with fh as stream:
            for line in stream:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get("type")
                if t not in ("assistant", "user"):
                    continue
                ts = parse_ts(d.get("timestamp") or "")
                if ts is None:
                    continue
                cwd = d.get("cwd")
                proj = project_of(cwd)
                if cwd:
                    cwds.add(cwd)
                parent = d.get("sessionId") or fn[:-6]
                side = bool(d.get("isSidechain") or "/subagents/" in path)
                sess = (d.get("agentId") or fn[:-6]) if side else parent
                key = "claude:" + sess
                meta = sessions.get(key) or session_record(
                    "claude",
                    sess,
                    ts,
                    cwd,
                    parent_session=parent if side else "",
                    surface=d.get("entrypoint") or "unknown",
                    agent_version=d.get("version") or "",
                    kind="subagent" if side else "main",
                )
                meta["ts"] = min(meta["ts"], ts)
                meta["last_ts"] = max(meta.get("last_ts", ts), ts)
                sessions[key] = meta
                msg = d.get("message") or {}
                kind = "subagent" if side else "main"
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
                    try:
                        merged = merge_claude_usage(usage_parts.get(uid, {}), u)
                    except (ValueError, TypeError, AttributeError):
                        merged = {}
                    usage_parts[uid] = merged
                    if uid and "fresh_in" in merged and "out" in merged:
                        rec = {
                            "t": "llm",
                            "uid": "c:" + str(uid),
                            "ts": ts,
                            "tool": "claude",
                            "session": sess,
                            "project": proj,
                            "model": model,
                            "kind": kind,
                            "client": d.get("entrypoint") or "",
                            "request_id": d.get("requestId") or "",
                            "response_id": msg.get("id") or "",
                            **{
                                name: merged.get(name, 0)
                                for name in (
                                    "fresh_in",
                                    "out",
                                    "cache_read",
                                    "cache_write",
                                    "reasoning",
                                )
                            },
                            **{
                                name: merged.get(name)
                                for name in ("cache_write_5m", "cache_write_1h")
                            },
                        }
                        old = llm.get(rec["uid"])
                        if old is None or rec["out"] >= old["out"]:
                            llm[rec["uid"]] = rec
                    for event in claude_edit_events(d, sess, proj, ts):
                        emit(event)
                elif t == "user" and kind == "main" and not d.get("isMeta"):
                    c = msg.get("content")
                    text = None
                    if isinstance(c, str):
                        text = c
                    elif (
                        isinstance(c, list)
                        and c
                        and all(isinstance(b, dict) and b.get("type") == "text" for b in c)
                    ):
                        text = "\n".join(b.get("text", "") for b in c)
                    if not text or text.startswith(
                        (
                            "<command-",
                            "<local-command",
                            "<system-reminder",
                            "<task-notification",
                            "[Request interrupted",
                            "Caveat:",
                            "<bash-",
                        )
                    ):
                        continue
                    pending.append(
                        {
                            "t": "prompt",
                            "uid": "c:" + str(d.get("uuid")),
                            "ts": ts,
                            "tool": "claude",
                            "session": sess,
                            "project": proj,
                            "model": "",
                            "chars": len(text),
                            "words": len(text.split()),
                            "est_tokens": (len(text) + 3) // 4,
                        }
                    )
        for p in pending:  # prompts never followed by a reply
            emit(p)
    observed = {r["session"] for r in llm.values()}
    for r in sessions.values():
        r["usage_status"] = "observed" if r["session"] in observed else "unknown"
        emit(r)
    source_status(
        "claude",
        "",
        file_count,
        detail="Native project trees and Omnigent observer paths; mirrored message IDs count once.",
    )
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


CODEX_KEYS = (
    b"token_usage_record",
    b'"turn_context"',
    b'"session_meta"',
    b'"role":"user"',
    b"Begin Patch",
    b'"token_count"',
)


def collect_codex(cutoff):
    roots = native_roots("codex")
    cwds = set()
    file_count = errors = 0
    for path in log_paths(roots, cutoff):
        fn = os.path.basename(path)
        if fn.endswith(".zst") and not shutil.which("zstd"):
            errors += 1
            continue
        fh = transcript(path)
        file_count += 1
        sess, cwd, kind = (fn[:-10] if fn.endswith(".jsonl.zst") else fn[:-6]), None, "main"
        turn_model, model = {}, "unknown"
        tur, fallback, last_total = {}, [], None
        meta = None
        ptexts = []
        with fh as stream:
            for line_no, line in enumerate(stream):
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
                    source = p.get("source")
                    spawn = (
                        source.get("subagent", {}).get("thread_spawn", {})
                        if isinstance(source, dict)
                        else {}
                    )
                    parent = p.get("parent_thread_id") or spawn.get("parent_thread_id") or ""
                    ts_kind = p.get("thread_source") or (
                        "subagent" if isinstance(source, dict) else "user"
                    )
                    kind = (
                        "review"
                        if "guardian" in str(ts_kind)
                        else "subagent"
                        if parent or ts_kind != "user"
                        else "main"
                    )
                    meta = session_record(
                        "codex",
                        sess,
                        ts,
                        cwd,
                        parent_session=parent,
                        surface=p.get("originator")
                        or (source if isinstance(source, str) else "subagent"),
                        agent_version=p.get("cli_version") or "",
                        kind=kind,
                        forked_from=p.get("forked_from_id") or "",
                        provider=p.get("model_provider") or "",
                    )
                elif t == "turn_context":
                    model = p.get("model") or model
                    cwd = p.get("cwd") or cwd
                    turn_model[p.get("turn_id")] = model
                    if cwd:
                        cwds.add(cwd)
                elif t == "token_usage_record":
                    rid = p.get("response_id")
                    rid = rid or "%s:line:%s" % (sess, line_no)
                    usage = p.get("usage") or {}
                    previous = tur.get(rid)
                    if not previous or usage.get("output_tokens", 0) >= previous[2].get(
                        "output_tokens", 0
                    ):
                        tur[rid] = (ts, rid, usage, turn_model.get(p.get("turn_id"), model))
                elif t == "event_msg" and p.get("type") == "token_count":
                    info = p.get("info") or {}
                    tot = (info.get("total_token_usage") or {}).get("total_tokens")
                    if tot and tot != last_total:
                        last_total = tot
                        fallback.append(
                            (ts, "%s:%s" % (sess, tot), info.get("last_token_usage") or {}, model)
                        )
                elif t == "response_item":
                    pt = p.get("type")
                    if pt == "message" and p.get("role") == "user" and kind == "main":
                        for c in p.get("content") or []:
                            tx = c.get("text") if isinstance(c, dict) else None
                            if not tx:
                                continue
                            if "## My request for Codex:" in tx:
                                tx = tx.split("## My request for Codex:", 1)[1].strip()
                            if (
                                tx.startswith(("#", "<", ">>>", "Host notice", "Retained"))
                                or not tx.strip()
                            ):
                                continue
                            emit(
                                {
                                    "t": "prompt",
                                    "uid": "x:%s:%s:%s"
                                    % (
                                        sess,
                                        d.get("ordinal", line_no),
                                        hashlib.sha256(tx.encode()).hexdigest()[:12],
                                    ),
                                    "ts": ts,
                                    "tool": "codex",
                                    "session": sess,
                                    "project": project_of(cwd),
                                    "model": model,
                                    "chars": len(tx),
                                    "words": len(tx.split()),
                                    "est_tokens": (len(tx) + 3) // 4,
                                }
                            )
                    elif pt in ("custom_tool_call", "function_call"):
                        txt = p.get("input") or p.get("arguments") or ""
                        if isinstance(txt, str) and "Begin Patch" in txt:
                            ptexts.append((ts, p.get("call_id") or p.get("id"), txt, model))
        proj = project_of(cwd)
        if meta:
            meta["usage_status"] = "observed" if tur or fallback else "unknown"
            meta["last_ts"] = max(
                [meta["ts"]] + [x[0] for x in tur.values()] + [x[0] for x in fallback]
            )
            emit(meta)
        for ts, rid, u, mdl in list(tur.values()) or fallback:
            inp, cached = u.get("input_tokens") or 0, u.get("cached_input_tokens") or 0
            emit(
                {
                    "t": "llm",
                    "uid": "x:" + str(rid),
                    "ts": ts,
                    "tool": "codex",
                    "session": sess,
                    "project": proj,
                    "model": mdl,
                    "kind": kind,
                    "client": "codex",
                    "response_id": str(rid),
                    "fresh_in": max(inp - cached - (u.get("cache_write_input_tokens") or 0), 0),
                    "cache_read": cached,
                    "cache_write": u.get("cache_write_input_tokens") or 0,
                    "out": u.get("output_tokens") or 0,
                    "reasoning": u.get("reasoning_output_tokens") or 0,
                }
            )
        for ts, cid, txt, mdl in ptexts:
            for event in codex_edit_events(dict(input=txt, call_id=cid), sess, proj, ts, mdl):
                emit(event)
    source_status(
        "codex",
        roots[0],
        file_count,
        errors,
        detail="Default, configured and isolated Omnigent homes; native response IDs count once.",
    )
    for c in cwds:
        emit({"t": "cwd", "cwd": c})


def collect_copilot(cutoff):
    """Persisted session events; usage is unknown unless actually present.

    Copilot assistant.usage is normally ephemeral. Never infer missing tokens
    from message count or treat Copilot premium-request units as dollars.
    """
    root = os.path.join(HOME, ".copilot", "session-state")
    files = errors = 0
    for dp, _, names in os.walk(root):
        if "events.jsonl" not in names:
            continue
        path = os.path.join(dp, "events.jsonl")
        if os.path.getmtime(path) < cutoff:
            continue
        files += 1
        sess, cwd, model, meta = os.path.basename(dp), None, "unknown", None
        with open(path, "rb") as fh:
            for n, ln in enumerate(fh):
                try:
                    d = json.loads(ln)
                except ValueError:
                    errors += 1
                    continue
                p = d.get("data") or {}
                ts = parse_ts(d.get("timestamp"))
                if ts is None:
                    continue
                typ = d.get("type")
                if typ == "session.start":
                    sess = p.get("sessionId") or sess
                    cwd = (p.get("context") or {}).get("cwd") or p.get("cwd")
                    model = p.get("selectedModel") or p.get("model") or model
                if typ == "session.context_changed":
                    cwd = p.get("cwd") or cwd
                if meta is None:
                    meta = session_record(
                        "copilot", sess, ts, cwd, surface="cli", kind="main", usage_status="unknown"
                    )
                meta["last_ts"] = ts
                if typ == "assistant.usage":
                    cr, cw = p.get("cacheReadTokens") or 0, p.get("cacheWriteTokens") or 0
                    emit(
                        dict(
                            t="llm",
                            uid="copilot:"
                            + str(p.get("apiCallId") or d.get("id") or "%s:%s" % (sess, n)),
                            ts=ts,
                            tool="copilot",
                            session=sess,
                            project=project_of(cwd),
                            model=p.get("model") or model,
                            kind="subagent" if d.get("agentId") else "main",
                            client="copilot",
                            fresh_in=max(0, (p.get("inputTokens") or 0) - cr - cw),
                            cache_read=cr,
                            cache_write=cw,
                            out=p.get("outputTokens") or 0,
                            reasoning=p.get("reasoningTokens") or 0,
                            response_id=p.get("apiCallId") or "",
                            request_id=p.get("serviceRequestId") or "",
                        )
                    )
                    meta["usage_status"] = "observed"
                elif typ == "user.message":
                    tx = p.get("content")
                    if isinstance(tx, str):
                        emit(
                            dict(
                                t="prompt",
                                uid="copilot:" + str(d.get("id") or "%s:%s" % (sess, n)),
                                ts=ts,
                                tool="copilot",
                                session=sess,
                                project=project_of(cwd),
                                model=model,
                                chars=len(tx),
                                words=len(tx.split()),
                                est_tokens=(len(tx) + 3) // 4,
                            )
                        )
        if meta:
            emit(meta)
    source_status("copilot", root, files, errors)


def opencode_roots():
    base = os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share"), "opencode"
    )
    return list(
        dict.fromkeys(
            os.path.realpath(os.path.expanduser(p))
            for p in [base] + discovery_config().get("opencode_data_dirs", [])
        )
    )


def collect_opencode(cutoff):
    files = errors = 0
    roots = opencode_roots()
    for root in roots:
        f, e = _collect_opencode_root(cutoff, root)
        files += f
        errors += e
    source_status("opencode", roots[0], files, errors)


def _collect_opencode_root(cutoff, root):
    """Read legacy JSON and the SQLite message projection, without starting agents."""
    import sqlite3

    files = errors = 0
    sessions = {}
    storage = os.path.join(root, "storage")
    for dp, _, names in os.walk(os.path.join(storage, "session")):
        for fn in names:
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(dp, fn)) as f:
                        m = json.load(f)
                    sessions[m["id"]] = m
                except (ValueError, OSError, KeyError):
                    errors += 1
    messages = []
    for dp, _, names in os.walk(os.path.join(storage, "message")):
        for fn in names:
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(dp, fn)) as f:
                        messages.append(json.load(f))
                except (ValueError, OSError):
                    errors += 1
    dbpath = os.path.join(root, "opencode.db")
    if os.path.isfile(dbpath):
        db = sqlite3.connect("file:" + dbpath + "?mode=ro", uri=True)
        try:
            db.row_factory = sqlite3.Row
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            session_table = "session" if "session" in tables else "session_v2"
            for row in db.execute("SELECT * FROM " + session_table):
                m = dict(row)
                sessions[m["id"]] = dict(
                    id=m["id"],
                    parentID=m.get("parent_id"),
                    directory=m.get("directory"),
                    time=dict(created=m.get("time_created"), updated=m.get("time_updated")),
                )
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            table = "message" if "message" in tables else "session_message"
            for row in db.execute("SELECT * FROM " + table):
                r = dict(row)
                m = json.loads(r["data"])
                if not isinstance(m, dict) or not m.get("role"):
                    errors += 1
                    continue
                m.update(id=r["id"], sessionID=r["session_id"])
                messages.append(m)
        except (sqlite3.Error, ValueError, KeyError):
            errors += 1
        finally:
            db.close()
    for sid, m in sessions.items():
        tm = m.get("time") or {}
        ts = parse_ts(tm.get("created"))
        if ts is None:
            continue
        files += 1
        emit(
            session_record(
                "opencode",
                sid,
                ts,
                m.get("directory"),
                last_ts=parse_ts(tm.get("updated")) or ts,
                parent_session=m.get("parentID") or "",
                surface="cli",
                kind="subagent" if m.get("parentID") else "main",
            )
        )
    for m in messages:
        if m.get("role") != "assistant" or not isinstance(m.get("tokens"), dict):
            continue
        u = m["tokens"]
        sid = m.get("sessionID")
        sess = sessions.get(sid, {})
        ts = parse_ts(
            (m.get("time") or {}).get("completed") or (m.get("time") or {}).get("created")
        )
        if ts is None or ts < cutoff:
            continue
        cache = u.get("cache") or {}
        emit(
            dict(
                t="llm",
                uid="opencode:" + m["id"],
                ts=ts,
                tool="opencode",
                session=sid,
                project=project_of(sess.get("directory")),
                model=m.get("modelID") or "unknown",
                kind="subagent" if sess.get("parentID") else "main",
                client="opencode",
                fresh_in=u.get("input") or 0,
                cache_read=cache.get("read") or 0,
                cache_write=cache.get("write") or 0,
                out=u.get("output") or 0,
                reasoning=u.get("reasoning") or 0,
                response_id=m.get("responseId") or m.get("response_id") or "",
            )
        )
    return files, errors


# ------------------------------------------------------------------------ Git


def git(repo, *args):
    try:
        return subprocess.run(
            ["git", "-C", repo] + list(args),
            capture_output=True,
            text=True,
            timeout=120,
            errors="replace",
        ).stdout
    except Exception:
        return ""


def _repository_marker(cwd, cache):
    """Resolve conventional checkouts without a Git process; None requests Git."""
    directory = os.path.realpath(cwd)
    visited = []
    result = ""
    while True:
        if directory in cache:
            result = cache[directory]
            break
        visited.append(directory)
        marker = os.path.join(directory, ".git")
        if os.path.isdir(marker) and not os.path.islink(marker):
            if all(
                os.path.isfile(os.path.join(marker, name)) for name in ("HEAD", "config")
            ) and os.path.isdir(os.path.join(marker, "objects")):
                result = directory
            else:
                result = None
            break
        if os.path.isfile(marker) and not os.path.islink(marker):
            try:
                with open(marker) as stream:
                    value = stream.read(8192).rstrip("\r\n")
                if not value.startswith("gitdir: "):
                    result = None
                    break
                gitdir = value[8:]
                if not os.path.isabs(gitdir):
                    gitdir = os.path.join(directory, gitdir)
                gitdir = os.path.realpath(gitdir)
                common = gitdir
                pointer = os.path.join(gitdir, "commondir")
                if os.path.islink(pointer):
                    result = None
                    break
                if os.path.isfile(pointer):
                    with open(pointer) as stream:
                        common = stream.read(8192).rstrip("\r\n")
                    if not os.path.isabs(common):
                        common = os.path.join(gitdir, common)
                    common = os.path.realpath(common)
                if (
                    os.path.basename(common) == ".git"
                    and all(
                        os.path.isfile(os.path.join(common, name)) for name in ("HEAD", "config")
                    )
                    and os.path.isdir(os.path.join(common, "objects"))
                ):
                    result = os.path.dirname(common)
                else:
                    result = None
            except (OSError, UnicodeError):
                result = None
            break
        if os.path.lexists(marker):
            result = None
            break
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        try:
            if os.stat(directory).st_dev != os.stat(parent).st_dev:
                break
        except OSError:
            result = None
            break
        directory = parent
    for directory in visited:
        cache[directory] = result
    return result


def find_repos(cwds, roots):
    repos = set()
    cache = {}
    # Git environment overrides can change discovery semantics. Let Git handle
    # them and uncommon layouts; the filesystem shortcut is invocation-local.
    discovery_env = (
        "GIT_DIR",
        "GIT_COMMON_DIR",
        "GIT_WORK_TREE",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_CONFIG",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
    )
    native = not any(name in os.environ for name in discovery_env)
    for c in cwds:
        if not os.path.isdir(c):
            continue
        candidate = _repository_marker(c, cache) if native else None
        if candidate is not None:
            if candidate:
                repos.add(candidate)
            continue
        common = git(c, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        if common.endswith("/.git"):
            repos.add(os.path.dirname(common))
        elif common:
            top = git(c, "rev-parse", "--show-toplevel").strip()
            if top:
                repos.add(top)
    skip = {
        "node_modules",
        ".venv",
        "venv",
        "target",
        "dist",
        "build",
        "Library",
        ".cache",
        ".npm",
        ".cargo",
    }
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


def remote_identity(value):
    value = re.sub(r"^git@([^:]+):", r"https://\1/", value or "")
    parsed = urlsplit(value)
    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    return (
        (parsed.hostname + "/" + path).lower()
        if parsed.hostname
        and len(parts) == 2
        and all(re.fullmatch(r"[\w.-]+", p) and p not in (".", "..") for p in parts)
        else ""
    )


def collect_git(repos, days, emails):
    seen = set()
    for repo in repos:
        remote = (
            remote_identity(git(repo, "remote", "get-url", "origin").strip())
            or "local:" + hashlib.sha256(repo.encode()).hexdigest()[:16]
        )
        authors = set(emails)
        e = git(repo, "config", "user.email").strip()
        if e:
            authors.add(e)
        cmd = [
            "log",
            "--all",
            "--no-merges",
            "--since=%d days ago" % days,
            "--numstat",
            "--format=%x1e%H%x1f%at%x1f%ae%x1f%(trailers:key=Co-authored-by,valueonly,separator=%x1d)",
        ]
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
                    da += a_
                    dr += r_
                    nd += 1
                else:
                    ca += a_
                    cr += r_
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
            emit(
                {
                    "t": "commit",
                    "uid": sha,
                    "ts": int(at),
                    "repo": repo,
                    "project": proj,
                    "author": ae,
                    "added_code": ca,
                    "removed_code": cr,
                    "added_doc": da,
                    "removed_doc": dr,
                    "files": nf,
                    "doc_files": nd,
                    "agent": agent,
                    "model": model,
                }
            )
            emit(
                dict(
                    t="work_commit",
                    uid=remote + ":" + sha,
                    ts=int(at),
                    repo=remote,
                    sha=sha,
                    added_code=ca,
                    removed_code=cr,
                    files=nf,
                    provenance="git_history",
                )
            )


# ------------------------------------------------- existing telemetry adapters
# Emits {"t":"metric","src":..,"m":canonical,"ts":..,"iv":seconds,"v":avg,"pk":peak}.
# Canonical units: percentages 0-100, memory MB, disk/net KB/s, temperature C.


def metric(src, m, ts, iv, v, pk=None):
    if v is None:
        return
    emit(
        {
            "t": "metric",
            "src": src,
            "m": m,
            "ts": int(ts),
            "iv": int(iv),
            "v": round(float(v), 3),
            "pk": None if pk is None else round(float(pk), 3),
        }
    )


def adapter_dirs():
    """Directories holding endpoint-metrics.csv / container-metrics.csv (5-minute samples), from
    $ERG_ENDPOINT_CSV_DIRS (colon-separated) or ~/.config/erg/config.json {"endpoint_csv_dirs": [...]}."""
    env = os.environ.get("ERG_ENDPOINT_CSV_DIRS")
    if env:
        return [d for d in env.split(":") if d]
    try:
        return list(
            json.load(open(os.path.join(HOME, ".config", "erg", "config.json"))).get(
                "endpoint_csv_dirs", []
            )
        )
    except (OSError, ValueError):
        return []


def collect_endpoint_csv(since):
    """Generic endpoint-metrics / container-metrics CSV adapter (see docs/adapters.md for the column contract)."""
    import csv

    dirs = adapter_dirs()
    fmap = {
        "cpu_busy_pct": "cpu",
        "cpu_user_pct": "cpu_user",
        "cpu_sys_pct": "cpu_sys",
        "am_used_pct": "mem_pct",
        "am_pressure_pct": "mem_pressure",
        "am_used_mb": "mem_mb",
        "am_compressed_mb": "mem_compressed_mb",
        "am_swap_used_mb": "swap_mb",
        "load_norm": "load_norm",
        "load1": "load1",
        "therm_pct": "therm",
        "disk_pct": "disk_used_pct",
        "comp_ratio": "comp_ratio",
        "demand_mb": "mem_demand_mb",
        "pressure": "pressure_level",
        "vm_footprint_mb": "container_vm_mb",
        "containers_running": "containers",
        "match_cpu_pct": "container_cpu_match",
        "other_cpu_pct": "container_cpu_other",
    }
    cum = {
        "decompressions_cum": "decompress_per_s",
        "swapouts_cum": "swapout_per_s",
        "swapins_cum": "swapin_per_s",
    }
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
                        emit(
                            {
                                "t": "note",
                                "src": "endpointcsv",
                                "k": "top_apps",
                                "ts": ts,
                                "text": r["top_apps_mem_plus_cmprs"][:200],
                            }
                        )
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
    import shutil
    import sqlite3
    import tempfile

    src = os.path.join(
        HOME, "Library", "Application Support", "iStat Menus 7", "history", "history.db"
    )
    if not os.path.isfile(src):
        return
    tmp = tempfile.mkdtemp(prefix="istat")
    try:
        for suf in ("", "-wal", "-shm"):
            if os.path.exists(src + suf):
                shutil.copy(src + suf, os.path.join(tmp, "history.db" + suf))
        db = sqlite3.connect(os.path.join(tmp, "history.db"))

        def q(sql):
            return db.execute(sql, (since,))

        for ts, iv, u, s in q("SELECT time,interval,user,system FROM cpu WHERE time>=?"):
            metric("istat", "cpu", ts, iv, u + s)
            metric("istat", "cpu_user", ts, iv, u)
            metric("istat", "cpu_sys", ts, iv, s)
        for ts, iv, pr, wired, app, comp, total in q(
            "SELECT time,interval,pressure,wired,app,compressed,total FROM memory WHERE time>=?"
        ):
            metric("istat", "mem_pressure", ts, iv, pr)
            metric("istat", "mem_mb", ts, iv, wired + app + comp)
            if total:
                metric("istat", "mem_pct", ts, iv, 100.0 * (wired + app + comp) / total)
            metric("istat", "mem_compressed_mb", ts, iv, comp)
        for ts, iv, one, five, _f in q(
            "SELECT time,interval,one,five,fifteen FROM load WHERE time>=?"
        ):
            metric("istat", "load1", ts, iv, one)
        for ts, iv, rd, wr, rp, wp in q(
            "SELECT time,interval,SUM(read),SUM(write),MAX(readpeak),MAX(writepeak) FROM diskactivity WHERE time>=? GROUP BY 1,2"
        ):
            metric("istat", "disk_r", ts, iv, rd, rp)
            metric("istat", "disk_w", ts, iv, wr, wp)
        # per-interface keys are hashed; tunnels duplicate physical traffic, so take the busiest key per sample
        for ts, iv, up, dn, upk, dnk in q(
            "SELECT time,interval,MAX(upload),MAX(download),MAX(uploadpeak),MAX(downloadpeak) FROM network WHERE time>=? GROUP BY 1,2"
        ):
            metric("istat", "net_up", ts, iv, up, upk)
            metric("istat", "net_down", ts, iv, dn, dnk)
        for ts, iv, p, mem in q(
            "SELECT time,interval,MAX(processor),MAX(memory) FROM gpu WHERE time>=? GROUP BY 1,2"
        ):
            metric("istat", "gpu", ts, iv, p)
            metric("istat", "gpu_mem", ts, iv, mem)
        for ts, iv, sw in q("SELECT time,interval,swap FROM swap WHERE time>=? AND interval>=30"):
            metric("istat", "swap_mb", ts, iv, sw)
        # sensor names are hashed; heuristics: 20-125 = temperature, >300 = fan RPM
        for (
            ts,
            iv,
            tmax,
            tpk,
            fmax,
        ) in q("""SELECT time,interval,MAX(CASE WHEN value BETWEEN 20 AND 125 THEN value END),
              MAX(CASE WHEN peak BETWEEN 20 AND 125 THEN peak END),MAX(CASE WHEN value>300 THEN value END) FROM sensors WHERE time>=? GROUP BY 1,2"""):
            metric("istat", "temp_max", ts, iv, tmax, tpk)
            metric("istat", "fan_rpm", ts, iv, fmax)
        for ts, iv, pct, srcv in q(
            "SELECT time,interval,percentage,source FROM battery WHERE key=1 AND time>=? AND interval>=90"
        ):
            metric("istat", "battery_pct", ts, iv, pct)
        db.close()
    except Exception as e:  # never let an adapter break the run
        sys.stderr.write("istat adapter: %s\n" % e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ Perf sampler


def sh(cmd, timeout=15):
    try:
        return subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout, errors="replace"
        ).stdout
    except Exception:
        return ""


def darwin_cpu_ticks():
    """Cumulative Mach CPU ticks, without spawning top or sampling every process."""
    import ctypes

    api = getattr(darwin_cpu_ticks, "api", None)
    if api is None:
        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        lib.mach_host_self.restype = ctypes.c_uint
        lib.host_statistics.argtypes = [
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint),
        ]
        lib.host_statistics.restype = ctypes.c_int
        # Keep one host-port right for this process rather than acquiring one per sample.
        api = (lib, lib.mach_host_self())
        darwin_cpu_ticks.api = api
    lib, host = api
    ticks = (ctypes.c_uint * 4)()
    count = ctypes.c_uint(4)
    result = lib.host_statistics(
        host, 3, ctypes.cast(ticks, ctypes.POINTER(ctypes.c_int)), ctypes.byref(count)
    )
    if result or count.value != 4:
        raise OSError("Mach CPU counters unavailable")
    # HOST_CPU_LOAD_INFO: user, system, idle, nice (SDK mach/host_info.h).
    return (int(ticks[0]) + int(ticks[1]) + int(ticks[3]), sum(ticks))


def raw_counters():
    """Cumulative counters + instantaneous gauges for this OS."""
    c = {"ts": time.time(), "load1": os.getloadavg()[0] if hasattr(os, "getloadavg") else None}
    sysname = platform.system()
    if sysname == "Darwin":
        try:
            c["cpu_raw"] = darwin_cpu_ticks()
        except (OSError, AttributeError):
            pass  # Missing counters stay unknown; no costly fallback.
        vm = sh("vm_stat")
        ps = re.search(r"page size of (\d+)", vm)
        ps = int(ps.group(1)) if ps else 16384

        def pg(k):
            return int((re.search(k + r":\s+(\d+)", vm) or [0, 0])[1])

        used = (
            pg("Pages active") + pg("Pages wired down") + pg("Pages occupied by compressor")
        ) * ps
        total = int(sh("sysctl -n hw.memsize").strip() or 0)
        c["mem_mb"] = used // 2**20
        c["mem_pct"] = round(100.0 * used / total, 2) if total else None
        io = sh("ioreg -c IOBlockStorageDriver -r -w0")
        c["disk_r_b"] = sum(int(x) for x in re.findall(r'"Bytes \(Read\)"=(\d+)', io))
        c["disk_w_b"] = sum(int(x) for x in re.findall(r'"Bytes \(Write\)"=(\d+)', io))
        rx = tx = 0
        for ln in sh("netstat -ibn").splitlines()[1:]:
            f = ln.split()
            if len(f) >= 10 and f[0].startswith("en") and f[2].startswith("<Link"):
                o = 6 if len(f) >= 11 else 5
                rx += int(f[o])
                tx += int(f[o + 3])
        c["net_rx_b"], c["net_tx_b"] = rx, tx
        hid = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', sh("ioreg -c IOHIDSystem -d 4"))
        c["idle_s"] = round(int(hid.group(1)) / 1e9, 1) if hid else None
    else:  # Linux
        try:
            f = open("/proc/stat").readline().split()[1:]
            v = list(map(int, f))
            c["cpu_raw"] = (sum(v) - v[3] - (v[4] if len(v) > 4 else 0), sum(v))
            mi = dict(
                (k, int(x.split()[0]))
                for k, x in (ln.split(":", 1) for ln in open("/proc/meminfo") if ":" in ln)
            )
            used = mi["MemTotal"] - mi.get("MemAvailable", mi.get("MemFree", 0))
            c["mem_mb"] = used // 1024
            c["mem_pct"] = round(100.0 * used / mi["MemTotal"], 2)
            r = w = 0
            for ln in open("/proc/diskstats"):
                p = ln.split()
                if re.match(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|xvd[a-z]+|mmcblk\d+)$", p[2]):
                    r += int(p[5]) * 512
                    w += int(p[9]) * 512
            c["disk_r_b"], c["disk_w_b"] = r, w
            rx = tx = 0
            for ln in list(open("/proc/net/dev"))[2:]:
                n, _, rest = ln.partition(":")
                if n.strip() != "lo":
                    p = rest.split()
                    rx += int(p[0])
                    tx += int(p[8])
            c["net_rx_b"], c["net_tx_b"] = rx, tx
        except Exception:
            pass
    return c


def take_sample():
    cur = raw_counters()
    try:
        with open(PERF_STATE) as previous_file:
            prev = json.load(previous_file)
    except Exception:
        prev = None
    s = {
        "t": "perf",
        "uid": str(int(cur["ts"])),
        "ts": int(cur["ts"]),
        "load1": cur.get("load1"),
        "mem_pct": cur.get("mem_pct"),
        "mem_mb": cur.get("mem_mb"),
        "idle_s": cur.get("idle_s"),
        "cpu": cur.get("cpu"),
    }
    if prev:
        dt = cur["ts"] - prev["ts"]
        if dt > 0:
            for k, o in (
                ("disk_r_b", "disk_r"),
                ("disk_w_b", "disk_w"),
                ("net_rx_b", "net_rx"),
                ("net_tx_b", "net_tx"),
            ):
                if k in cur and k in prev and cur[k] >= prev[k]:
                    s[o] = int((cur[k] - prev[k]) / dt)
            if "cpu_raw" in cur and "cpu_raw" in prev:
                db, dtot = (
                    cur["cpu_raw"][0] - prev["cpu_raw"][0],
                    cur["cpu_raw"][1] - prev["cpu_raw"][1],
                )
                s["cpu"] = round(100.0 * db / dtot, 2) if dtot > 0 and 0 <= db <= dtot else None
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(PERF_STATE, "w") as state_file:
        json.dump(cur, state_file)
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
        plist = os.path.join(HOME, "Library", "LaunchAgents", "dev.ddx.erg.sampler.plist")
        open(plist, "w").write(
            """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>dev.ddx.erg.sampler</string>
<key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>%s</string><string>sample</string><string>--loop</string><string>30</string></array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardErrorPath</key><string>%s/sampler.err</string>
</dict></plist>
"""
            % (script, STATE_DIR)
        )
        print("wrote %s\nactivate with:\n  launchctl bootstrap gui/$(id -u) %s" % (plist, plist))
    else:
        unit_dir = os.path.join(HOME, ".config", "systemd", "user")
        os.makedirs(unit_dir, exist_ok=True)
        open(os.path.join(unit_dir, "erg-sampler.service"), "w").write(
            "[Unit]\nDescription=erg perf sampler\n[Service]\nExecStart=%s %s sample --loop 30\nRestart=always\n[Install]\nWantedBy=default.target\n"
            % (sys.executable, script)
        )
        print("wrote systemd unit; activate with:\n  systemctl --user enable --now erg-sampler")


# ------------------------------------------------------------------------ main


def arg(name, default=None, flag=False):
    a = sys.argv
    if name in a:
        return True if flag else a[a.index(name) + 1]
    return False if flag else default


def retained_source_rows(db):
    """Cheap observed checkpoint inventory, not a claim of current file existence."""
    totals = {
        tool: (files, errors)
        for tool, files, errors in db.execute(
            "SELECT tool,count(*),coalesce(sum(errors),0) FROM history_sources WHERE state<>'consumed' GROUP BY tool"
        )
    }
    rows = []
    for tool in ("claude", "codex", "pi"):
        files, errors = totals.get(tool, (0, 0))
        rows.append(
            dict(
                t="source",
                uid=tool,
                tool=tool,
                ts=int(time.time()),
                files=files,
                errors=errors,
                installed=bool(shutil.which(tool)),
                status="partial" if errors else "available" if files else "no_history",
                detail="Observed retained checkpoints; usage and edits come from durable forward projection. Counter snapshots remain separate from identified requests.",
            )
        )
    return rows


def collect_retained(database=None):
    """Use enrolled local evidence for covered tools; never reopen native history."""
    from erg import history, store

    if not history.config().get("content"):
        raise RuntimeError("retained enrichment requires forward content capture")
    db = store.open_readonly(database or store.DB_PATH)
    try:
        if not db.execute("SELECT 1 FROM history_config WHERE key='enrolled_at'").fetchone():
            raise RuntimeError("this ledger is not enrolled for retained capture")
        for row in retained_source_rows(db):
            emit(row)
        return {
            row[0]
            for row in db.execute(
                "SELECT DISTINCT cwd FROM project_contexts WHERE machine=? AND cwd IS NOT NULL AND cwd<>''",
                (machine_name(),),
            )
        }
    finally:
        db.close()


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "collect"
    if cmd == "sample":
        loop = arg("--loop")
        while True:
            s = take_sample()
            if not loop:
                print(json.dumps(s))
                return
            time.sleep(float(loop))
    if cmd == "sampler-install":
        return sampler_install()
    if cmd == "native-usage":
        emit(dict(t="hello", machine=machine_name(), os=platform.system(), ncpu=os.cpu_count()))
        since = float(arg("--since", time.time() - 60))
        collect_claude(since)
        collect_codex(since)
        return
    days = float(arg("--days", 400))
    now = time.time()
    emit(
        {
            "t": "hello",
            "machine": machine_name(),
            "os": platform.system(),
            "ncpu": os.cpu_count(),
            "version": VERSION,
            "now": int(now),
        }
    )
    cutoff = now - days * 86400
    cwds = set()

    class Tap:  # capture cwd events for repo discovery while forwarding everything else
        pass

    import io

    real = sys.stdout
    _buf = io.StringIO()
    # stream through, but intercept cwd records
    native_sessions = {}

    class Filter(io.TextIOBase):
        def write(self, s):
            for ln in s.splitlines():
                if ln.startswith('{"t":"cwd"'):
                    cwds.add(json.loads(ln)["cwd"])
                else:
                    if ln.startswith('{"'):
                        event = json.loads(ln)
                        if event.get("t") == "session":
                            native_sessions[(event["tool"], event["session"])] = event
                    real.write(ln + "\n")
            return len(s)

    sys.stdout = Filter()
    try:
        retained = arg("--retained", flag=True)
        if retained:
            cwds.update(collect_retained(arg("--retained-database")))
        else:
            collect_claude(cutoff)
            collect_codex(cutoff)
        # Keep legacy adapters whose complete projection parity is not established.
        collect_copilot(cutoff)
        collect_opencode(cutoff)
        if not retained:
            collect_pi(cutoff)
            collect_omnigent(cutoff, native_sessions)
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
