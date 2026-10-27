"""Shared temporal project hierarchy; no source, filesystem, network or UI reads."""

import bisect
import hashlib
import json
from collections import defaultdict

from . import projects


def context_index(db, end, identities=None):
    native = defaultdict(lambda: defaultdict(list))
    work = defaultdict(lambda: defaultdict(list))

    def rows(table, fields):
        if identities is None:
            return db.execute("SELECT " + fields + " FROM " + table + " WHERE ts<?", (end,))
        return (
            row
            for machine, tool, sid in identities
            for row in db.execute(
                "SELECT "
                + fields
                + " FROM "
                + table
                + " WHERE machine=? AND tool=? AND session=? AND ts<?",
                (machine, tool, sid, end),
            )
        )

    for machine, tool, sid, ts, cwd, repo in rows(
        "project_contexts", "machine,tool,session,ts,cwd,repo"
    ):
        native[(machine, tool, sid)][ts].append(dict(cwd=cwd, repo=repo))
    for machine, tool, sid, ts, repo, tree in rows(
        "work_contexts", "machine,tool,session,ts,repo,worktree"
    ):
        work[(machine, tool, sid)][ts].append(dict(repo=repo, cwd_hash=tree))
    result = {}
    for source, carry in ((native, True), (work, False)):
        built = {}
        for key, groups in source.items():
            times = []
            values = []
            previous = {}
            for ts, rows in sorted(groups.items()):
                fields = {field: {row.get(field) or "" for row in rows} for field in rows[0]}
                value = {
                    field: next(iter(items)) if len(items) == 1 else ""
                    for field, items in fields.items()
                }
                if carry:
                    value["repo"] = value["repo"] or (
                        previous.get("repo", "")
                        if value["cwd"] and value["cwd"] == previous.get("cwd")
                        else ""
                    )
                    value["cwd_hash"] = (
                        hashlib.sha256(value["cwd"].encode()).hexdigest()[:16]
                        if value["cwd"]
                        else ""
                    )
                    previous = value
                times.append(ts)
                values.append(value)
            built[key] = (times, values)
        result["native" if carry else "work"] = built
    return result


def context_at(index, call):
    key = (call["machine"], call["tool"], call["session"])
    found = {}
    for source in ("native", "work"):
        times, rows = index[source].get(key, ([], []))
        i = bisect.bisect_right(times, call["ts"]) - 1
        found[source] = (times[i], rows[i]) if i >= 0 else (-1, {})
    nt, native = found["native"]
    wt, work = found["work"]
    if nt < 0:
        return dict(repo=work.get("repo", ""), cwd="")
    if wt < 0:
        return dict(repo=native.get("repo", ""), cwd=native.get("cwd", ""))
    same = bool(native.get("cwd_hash") and native.get("cwd_hash") == work.get("cwd_hash"))
    if same:
        return dict(repo=work.get("repo") or native.get("repo", ""), cwd=native.get("cwd", ""))
    return (
        dict(repo=work.get("repo", ""), cwd="")
        if wt >= nt
        else dict(repo=native.get("repo", ""), cwd=native.get("cwd", ""))
    )


class Scope:
    """One allocation policy and source hierarchy for every reporting metric."""

    def __init__(self, db, end, config=None, identities=None):
        self.config = projects.validate(config if config is not None else projects.snapshot(db))
        self.contexts = context_index(db, end, identities)
        self.resolver = projects.Resolver(self.config)
        self.catalog = {p["project_key"]: p for p in self.config["projects"]}
        self.rules = {r["binding_id"]: r for r in self.config["bindings"]}
        self.folders = {}

    def resolve(self, observation):
        row = dict(observation)
        row.setdefault("session", "")
        row.setdefault("tool", "none")
        context = context_at(self.contexts, row)
        # Commit/source metadata can supply an explicit repository even without a
        # session; never turn a folder basename into a guessed repository.
        if row.get("repo"):
            try:
                context["repo"] = projects.repository(row["repo"])
            except ValueError:
                pass
        allocation = self.resolver.resolve(row, context)
        native = row.get("observed_project", row.get("project")) or ""
        cwd = context.get("cwd") or ""
        if cwd:
            # A configured parent directory is the rollup folder for its children.
            roots = [
                self.rules[ref]["selectors"]["directory"]["root"]
                for ref in allocation["binding_refs"]
                if ref in self.rules and "directory" in self.rules[ref]["selectors"]
            ]
            path = max(roots, key=len) if roots else cwd
            identity = ["folder", row["machine"], path]
            kind = "folder"
            label = path
        elif native:
            identity = ["agent_project", row["machine"], row["tool"], native]
            kind = "agent_project"
            label = native
        elif context.get("repo"):
            identity = ["repository", row["machine"], context["repo"]]
            kind = "repository"
            label = context["repo"]
        else:
            # Unknown folders cannot assert a shared physical parent identity.
            identity = ["unobserved", row["machine"], row["tool"], row["session"]]
            kind = "unobserved"
            label = "Folder not observed"
        key = (
            kind
            + ":"
            + hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
        )
        folder = dict(
            folder_key=key,
            name=label,
            kind=kind,
            machine=row["machine"],
            tool=row["tool"] if kind in ("agent_project", "unobserved") else None,
            observed_project=native,
            cwd=cwd or None,
            repository=context.get("repo") or None,
            identity_basis="recorded_directory"
            if kind == "folder"
            else "recorded_repository"
            if kind == "repository"
            else "observed_agent_project_hint"
            if kind == "agent_project"
            else "unknown_session_scoped",
        )
        folder = self.folders.setdefault(key, dict(folder, project_keys=[]))
        portions = list(allocation["allocations"])
        remaining = projects.UNITS - sum(p["units"] for p in portions)
        if remaining:
            portions.append(dict(project_key="@unassigned", units=remaining))
        folder["project_keys"] = sorted(
            set(folder["project_keys"]) | {part["project_key"] for part in portions}
        )
        return dict(
            folder=folder,
            allocations=portions,
            binding_refs=list(allocation["binding_refs"]),
            state=allocation["state"],
            reason=allocation["reason"],
            revision=self.config["revision"],
        )

    def weight(self, observation, project_key="", folder_key=""):
        result = self.resolve(observation)
        if folder_key and (
            result["folder"]["kind"] != "unobserved"
            if folder_key == "@unobserved"
            else result["folder"]["folder_key"] != folder_key
        ):
            return 0, result
        return sum(
            p["units"]
            for p in result["allocations"]
            if not project_key or p["project_key"] == project_key
        ), result
