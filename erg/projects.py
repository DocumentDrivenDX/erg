"""Local project catalog and deterministic allocation policy; no source/network IO."""

import json
import posixpath
import re
import time
import uuid
from urllib.parse import urlsplit

UNITS = 1000000


class RevisionConflict(ValueError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS project_config(id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL,body TEXT NOT NULL);
INSERT OR IGNORE INTO project_config VALUES(1,0,'{"version":1,"revision":0,"projects":[],"bindings":[],"aliases":[]}');
CREATE TABLE IF NOT EXISTS project_catalog(project_key TEXT PRIMARY KEY,name TEXT,state TEXT,source TEXT,source_revision TEXT,archived_at INTEGER);
CREATE TABLE IF NOT EXISTS project_bindings(binding_id TEXT PRIMARY KEY,revision INTEGER,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS project_aliases(authority TEXT,external_id TEXT,project_key TEXT,PRIMARY KEY(authority,external_id));
CREATE TABLE IF NOT EXISTS project_config_history(revision INTEGER PRIMARY KEY,ts INTEGER,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS project_contexts(machine TEXT,tool TEXT,session TEXT,source_uid TEXT,ts INTEGER,cwd TEXT,repo TEXT,
 PRIMARY KEY(machine,source_uid));
CREATE INDEX IF NOT EXISTS project_context_session ON project_contexts(machine,tool,session,ts);
"""


def initialize(db):
    db.executescript(SCHEMA)


def snapshot(db):
    return json.loads(db.execute("SELECT body FROM project_config WHERE id=1").fetchone()[0])


def string(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError("Invalid " + field)
    return value


def integer(value, field, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError("Invalid " + field)
    return value


def repository(value):
    value = string(value, "repository")
    value = re.sub(r"^git@([^:]+):", r"https://\1/", value)
    p = urlsplit(value if "://" in value else "https://" + value)
    path = p.path.strip("/")
    path = path[:-4] if path.endswith(".git") else path
    if (
        p.username
        or p.password
        or p.query
        or p.fragment
        or not p.hostname
        or not re.fullmatch(r"[\w.-]+/[\w.-]+", path)
        or any(x in (".", "..") for x in path.split("/"))
    ):
        raise ValueError("Invalid repository")
    return p.hostname.lower() + "/" + path.lower()


def validate(value):
    if (
        not isinstance(value, dict)
        or set(value) - {"version", "revision", "projects", "bindings", "aliases"}
        or type(value.get("version")) is not int
        or value.get("version") != 1
    ):
        raise ValueError("Unsupported project configuration")
    revision = integer(value.get("revision"), "revision")
    result = dict(version=1, revision=revision, projects=[], bindings=[], aliases=[])
    names = set()
    ids = set()
    alias_ids = set()
    for kind in ("projects", "bindings", "aliases"):
        if not isinstance(value.get(kind, []), list) or len(value.get(kind, [])) > 10000:
            raise ValueError("Invalid " + kind)
    for row in value.get("projects", []):
        if not isinstance(row, dict) or set(row) - {
            "project_key",
            "name",
            "state",
            "source",
            "source_revision",
            "archived_at",
        }:
            raise ValueError("Invalid project")
        key = string(row.get("project_key"), "project_key")
        if key.startswith("@"):
            raise ValueError("Reserved project_key")
        if key in names:
            raise ValueError("Duplicate project_key")
        names.add(key)
        state = row.get("state", "active")
        if state not in ("active", "archived"):
            raise ValueError("Invalid project state")
        archived = integer(row.get("archived_at"), "archived_at") if state == "archived" else None
        result["projects"].append(
            dict(
                project_key=key,
                name=string(row.get("name"), "project name"),
                state=state,
                source=string(row.get("source", "local"), "source"),
                source_revision=string(row.get("source_revision", "local"), "source_revision"),
                archived_at=archived,
            )
        )
    for row in value.get("aliases", []):
        if not isinstance(row, dict) or set(row) != {"authority", "external_id", "project_key"}:
            raise ValueError("Invalid project alias")
        key = (string(row["authority"], "authority"), string(row["external_id"], "external_id"))
        string(row["project_key"], "project_key")
        if key in alias_ids or row["project_key"] not in names:
            raise ValueError("Conflicting or missing alias target")
        alias_ids.add(key)
        result["aliases"].append(dict(row))
    for row in value.get("bindings", []):
        allowed = {
            "binding_id",
            "priority",
            "selectors",
            "allocations",
            "valid_from",
            "valid_to",
            "enabled",
            "provenance",
        }
        if not isinstance(row, dict) or set(row) - allowed:
            raise ValueError("Invalid project binding")
        key = string(row.get("binding_id"), "binding_id")
        if key in ids:
            raise ValueError("Duplicate binding_id")
        ids.add(key)
        selectors = row.get("selectors")
        allocations = row.get("allocations")
        targets = set()
        total = 0
        if (
            not isinstance(selectors, dict)
            or not selectors
            or set(selectors) - {"repository", "directory", "session", "agent_project"}
        ):
            raise ValueError("Unsupported selector")
        selectors = dict(selectors)
        if "repository" in selectors:
            selectors["repository"] = repository(selectors["repository"])
        for name, fields in (
            ("directory", {"machine", "root"}),
            ("session", {"machine", "tool", "id"}),
            ("agent_project", {"machine", "tool", "id"}),
        ):
            if name not in selectors:
                continue
            item = selectors[name]
            if not isinstance(item, dict) or set(item) != fields:
                raise ValueError("Invalid " + name + " selector")
            for field in fields:
                string(item[field], field)
            if name == "directory":
                if not item["root"].startswith("/"):
                    raise ValueError("Directory root must be absolute")
                selectors[name] = dict(item, root=posixpath.normpath(item["root"]))
        if not isinstance(allocations, list) or not allocations:
            raise ValueError("Missing allocations")
        normalized = []
        for allocation in allocations:
            if not isinstance(allocation, dict) or set(allocation) != {"project_key", "units"}:
                raise ValueError("Invalid allocation")
            target = string(allocation["project_key"], "project_key")
            units = integer(allocation["units"], "allocation units", 1)
            if target not in names or target in targets:
                raise ValueError("Missing or duplicate allocation target")
            targets.add(target)
            total += units
            normalized.append(dict(project_key=target, units=units))
        if total > UNITS:
            raise ValueError("Allocations exceed one request")
        start = integer(row.get("valid_from"), "valid_from")
        end = row.get("valid_to")
        if end is not None and integer(end, "valid_to") <= start:
            raise ValueError("Invalid binding interval")
        priority = row.get("priority", 0)
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ValueError("Invalid priority")
        enabled = row.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("Invalid enabled flag")
        result["bindings"].append(
            dict(
                binding_id=key,
                priority=priority,
                selectors=selectors,
                allocations=sorted(normalized, key=lambda a: a["project_key"]),
                valid_from=start,
                valid_to=end,
                enabled=enabled,
                provenance=string(row.get("provenance", "operator_configuration"), "provenance"),
            )
        )
    for kind, key in (("projects", "project_key"), ("bindings", "binding_id")):
        result[kind].sort(key=lambda r: r[key])
    result["aliases"].sort(key=lambda r: (r["authority"], r["external_id"]))
    return result


def same_content(left, right):
    return {k: v for k, v in left.items() if k != "revision"} == {
        k: v for k, v in right.items() if k != "revision"
    }


def check_change(old, value):
    if same_content(old, value):
        return
    if old["revision"] != value["revision"]:
        raise RevisionConflict("Project configuration revision changed; export and review again")
    if {p["project_key"] for p in old["projects"]} - {p["project_key"] for p in value["projects"]}:
        raise ValueError("Archive existing projects instead of removing their identities")


def apply(db, value):
    value = validate(value)
    with db:
        db.execute("BEGIN IMMEDIATE")
        old = snapshot(db)
        check_change(old, value)
        if same_content(value, old):
            return dict(changed=False, revision=old["revision"])
        value["revision"] += 1
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        db.execute(
            "UPDATE project_config SET revision=?,body=? WHERE id=1", (value["revision"], encoded)
        )
        db.execute(
            "INSERT INTO project_config_history VALUES(?,?,?)",
            (value["revision"], int(time.time()), encoded),
        )
        db.execute("DELETE FROM project_catalog")
        db.execute("DELETE FROM project_bindings")
        db.execute("DELETE FROM project_aliases")
        db.executemany(
            "INSERT INTO project_catalog VALUES(?,?,?,?,?,?)",
            [
                (
                    p["project_key"],
                    p["name"],
                    p["state"],
                    p["source"],
                    p["source_revision"],
                    p["archived_at"],
                )
                for p in value["projects"]
            ],
        )
        db.executemany(
            "INSERT INTO project_bindings VALUES(?,?,?)",
            [
                (p["binding_id"], value["revision"], json.dumps(p, sort_keys=True))
                for p in value["bindings"]
            ],
        )
        db.executemany(
            "INSERT INTO project_aliases VALUES(?,?,?)",
            [(p["authority"], p["external_id"], p["project_key"]) for p in value["aliases"]],
        )
    return dict(changed=True, revision=value["revision"])


def create(db, name):
    value = snapshot(db)
    key = "local:" + uuid.uuid4().hex
    value["projects"].append(
        dict(
            project_key=key,
            name=string(name, "project name"),
            state="active",
            source="local",
            source_revision="local",
            archived_at=None,
        )
    )
    result = apply(db, value)
    return dict(result, project_key=key)


def resolve(config, call, context, rules=None):
    matching = []
    for rule in config["bindings"] if rules is None else rules:
        if (
            not rule["enabled"]
            or call["ts"] < rule["valid_from"]
            or (rule["valid_to"] is not None and call["ts"] >= rule["valid_to"])
        ):
            continue
        selectors = rule["selectors"]
        session = selectors.get("session")
        directory = selectors.get("directory")
        if session and (call["machine"], call["tool"], call.get("session", "")) != (
            session["machine"],
            session["tool"],
            session["id"],
        ):
            continue
        if "repository" in selectors and context.get("repo") != selectors["repository"]:
            continue
        native = selectors.get("agent_project")
        if native and (
            call["machine"],
            call["tool"],
            call.get("observed_project", call.get("project")),
        ) != (native["machine"], native["tool"], native["id"]):
            continue
        if directory:
            cwd = context.get("cwd")
            root = directory["root"]
            if (
                call["machine"] != directory["machine"]
                or not cwd
                or not (cwd == root or cwd.startswith(root.rstrip("/") + "/"))
            ):
                continue
        matching.append(rule)
    if not matching:
        return dict(
            state="unassigned", reason="no_matching_binding", allocations=[], binding_refs=[]
        )
    priority = max(r["priority"] for r in matching)
    matching = [r for r in matching if r["priority"] == priority]
    variants = {json.dumps(r["allocations"], sort_keys=True) for r in matching}
    refs = [r["binding_id"] for r in matching]
    if len(variants) != 1:
        return dict(
            state="ambiguous", reason="conflicting_bindings", allocations=[], binding_refs=refs
        )
    allocations = json.loads(next(iter(variants)))
    targets = {p["project_key"]: p for p in config["projects"]}
    if any(
        targets[a["project_key"]]["archived_at"] is not None
        and call["ts"] >= targets[a["project_key"]]["archived_at"]
        for a in allocations
    ):
        return dict(
            state="unassigned",
            reason="project_archived_at_event_time",
            allocations=[],
            binding_refs=refs,
        )
    total = sum(a["units"] for a in allocations)
    return dict(
        state="assigned" if total == UNITS else "partially_assigned",
        reason=None if total == UNITS else "partial_binding",
        allocations=allocations,
        binding_refs=refs,
    )


def observe_context(db, machine, tool, session, ts, uid, cwd, repo=""):
    if not isinstance(cwd, str) or not cwd.startswith("/") or len(cwd) > 4096:
        return
    db.execute(
        "INSERT INTO project_contexts VALUES(?,?,?,?,?,?,?) ON CONFLICT(machine,source_uid) DO UPDATE SET ts=excluded.ts,cwd=excluded.cwd,repo=excluded.repo",
        (machine, tool, session, uid, int(ts), posixpath.normpath(cwd), repo),
    )


class Resolver:
    def __init__(self, config):
        self.config = config
        self.index = {}
        for rule in config["bindings"]:
            s = rule["selectors"]
            if "session" in s:
                item = s["session"]
                key = ("session", item["machine"], item["tool"], item["id"])
            elif "agent_project" in s:
                item = s["agent_project"]
                key = ("agent_project", item["machine"], item["tool"], item["id"])
            elif "repository" in s:
                key = ("repository", s["repository"])
            else:
                key = ("directory", s["directory"]["machine"])
            self.index.setdefault(key, []).append(rule)

    def resolve(self, call, context):
        keys = [
            ("session", call["machine"], call["tool"], call.get("session", "")),
            (
                "agent_project",
                call["machine"],
                call["tool"],
                call.get("observed_project", call.get("project")),
            ),
            ("repository", context.get("repo")),
            ("directory", call["machine"]),
        ]
        rules = [rule for key in keys for rule in self.index.get(key, [])]
        return resolve(self.config, call, context, rules)
