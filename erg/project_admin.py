"""Local, revision-checked project metadata changes; never edit captured data."""

import uuid

from . import project_reports, projects


def change(db, body, canonical_select=None):
    allowed = {
        "action",
        "revision",
        "apply",
        "name",
        "project_key",
        "selectors",
        "binding_id",
        "valid_from",
    }
    if not isinstance(body, dict) or set(body) - allowed:
        raise ValueError("Invalid project change")
    action = body.get("action")
    apply = body.get("apply", False)
    if action not in ("create", "bind") or not isinstance(apply, bool):
        raise ValueError("Invalid project change")
    expected = projects.integer(body.get("revision"), "revision")
    config = projects.snapshot(db)
    result = dict(available=True, action=action, applied=False, revision=config["revision"])
    if action == "create":
        if set(body) & {"selectors", "binding_id", "valid_from"}:
            raise ValueError("Invalid project creation")
        name = projects.string(body.get("name"), "project name")
        key = body.get("project_key") or "local:" + uuid.uuid4().hex
        projects.string(key, "project_key")
        if not key.startswith("local:"):
            raise ValueError("New projects require a local identity")
        project = dict(
            project_key=key,
            name=name,
            state="active",
            source="local",
            source_revision="local",
            archived_at=None,
        )
        existing = next((p for p in config["projects"] if p["project_key"] == key), None)
        mutation = dict(action=action, revision=expected, project_key=key, name=name)
        if existing:
            if existing != project:
                raise projects.RevisionConflict("Project identity already has different metadata")
            return dict(
                result, changed=False, applied=apply, mutation=mutation, catalog=config["projects"]
            )
        config["projects"].append(project)
    else:
        if "name" in body:
            raise ValueError("Invalid project binding")
        target = body.get("project_key")
        key = body.get("binding_id") or "local:" + uuid.uuid4().hex
        projects.string(key, "binding_id")
        if not key.startswith("local:"):
            raise ValueError("New bindings require a local identity")
        rule = dict(
            binding_id=key,
            priority=0,
            selectors=body.get("selectors"),
            allocations=[dict(project_key=target, units=projects.UNITS)],
            valid_from=projects.integer(body.get("valid_from", 0), "valid_from"),
            provenance="local_ui",
        )
        normalized = projects.validate(dict(config, bindings=[rule]))["bindings"][0]
        existing = next((r for r in config["bindings"] if r["binding_id"] == key), None)
        mutation = dict(
            action=action,
            revision=expected,
            project_key=target,
            binding_id=key,
            selectors=normalized["selectors"],
            valid_from=normalized["valid_from"],
        )
        if existing:
            if existing != normalized:
                raise projects.RevisionConflict("Binding identity already has different metadata")
            return dict(
                result, changed=False, applied=apply, mutation=mutation, catalog=config["projects"]
            )
        config["bindings"].append(normalized)
    if config["revision"] != expected:
        raise projects.RevisionConflict("Project configuration changed; refresh and preview again")
    config = projects.validate(config)
    if apply:
        applied = projects.apply(db, config)
        return dict(result, **applied, applied=True, mutation=mutation, catalog=config["projects"])
    if action == "bind":
        before = project_reports.report(
            db, 0, 2**63 - 1, config=projects.snapshot(db), canonical_select=canonical_select
        )
        after = project_reports.report(
            db, 0, 2**63 - 1, config=config, canonical_select=canonical_select
        )
        result["preview"] = dict(
            before=before["projects"],
            after=after["projects"],
            unassigned_reasons=after["unassigned_reasons"],
            scope="All retained usage from valid_from onward; source history and billing evidence unchanged",
        )
    return dict(result, changed=False, mutation=mutation, catalog=config["projects"])
