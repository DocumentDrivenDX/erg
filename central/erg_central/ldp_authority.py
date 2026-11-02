"""Evaluate serialized publication authority from actual backend job facts.

This adapter obtains evidence; TableSpec validates its typed shape and policy.
No distributed fence is asserted. Errors prevent receipt publication.
"""

import copy
from datetime import datetime, timezone
from urllib.parse import quote

from erg import delivery_protocol as wire

from .receiver import Conflict


def job_digest(settings):
    return wire.digest(wire.encode(settings))


def bind_job_settings(template, deployment_sha256):
    """Bind the externally pinned config digest without a self-hash cycle."""
    settings = copy.deepcopy(template)
    bound = 0
    for task in settings.get("tasks", []):
        parameters = task.get("spark_python_task", {}).get("parameters", [])
        if "--deployment-sha256" not in parameters:
            continue
        if parameters.count("--deployment-sha256") != 1:
            raise Conflict("duplicate deployment hash binding")
        index = parameters.index("--deployment-sha256") + 1
        if index >= len(parameters) or parameters[index] != "__ERG_DEPLOYMENT_SHA256__":
            raise Conflict("invalid deployment hash placeholder")
        parameters[index] = deployment_sha256
        bound += 1
    if bound not in (1, 2):
        raise Conflict("explicit deployment hash binding required")
    return settings


def active_runs(api, job_id):
    """Bounded inventory; Databricks can return an end cursor with has_more=false."""
    path = "/api/2.1/jobs/runs/list?job_id=" + str(job_id) + "&active_only=true&limit=25"
    active, tokens = [], set()
    token = None
    for _ in range(4):
        page = api("GET", path + ("&page_token=" + quote(token, safe="") if token else ""))
        rows = page.get("runs", [])
        if not isinstance(rows, list) or len(rows) > 25:
            raise Conflict("invalid active-run inventory")
        active.extend(rows)
        if len(active) > 1:
            raise Conflict("publication job has competing active runs")
        if not rows and page.get("has_more") is False:
            return active
        token = page.get("next_page_token")
        if not token:
            if page.get("has_more"):
                raise Conflict("active-run inventory is incomplete")
            return active
        if not isinstance(token, str) or len(token) > 16384 or token in tokens:
            raise Conflict("active-run pagination did not advance")
        tokens.add(token)
    raise Conflict("active-run inventory exceeds page bound")


def collect_job_authority(
    api,
    job_id,
    run_id,
    task_key,
    expected_settings_sha256,
    expected_code_sha256,
    read_code,
    evidence_ref,
    write,
):
    observed_at = datetime.now(timezone.utc)
    settings = api("GET", "/api/2.1/jobs/get?job_id=" + str(job_id))["settings"]
    if job_digest(settings) != expected_settings_sha256:
        raise Conflict("publication job definition changed")
    if type(settings.get("max_concurrent_runs")) is not int or settings["max_concurrent_runs"] != 1:
        raise Conflict("publication job concurrency is not one")
    if settings.get("queue", {}).get("enabled") is not False:
        raise Conflict("publication job queue is not disabled")
    active = active_runs(api, job_id)
    if len(active) != 1 or active[0].get("run_id") != run_id:
        observed = [row.get("run_id") for row in active]
        safe = [value for value in observed if type(value) is int]
        raise Conflict(
            "publication job does not have exactly the expected active run "
            + repr(dict(expected=run_id, observed=safe))
        )
    run = api("GET", "/api/2.1/jobs/runs/get?run_id=" + str(run_id))
    if run.get("job_id") != job_id or run.get("state", {}).get("life_cycle_state") != "RUNNING":
        raise Conflict("publication run identity or state changed")
    tasks = [
        task
        for task in run.get("tasks", [])
        if task.get("task_key") == task_key
        and task.get("state", {}).get("life_cycle_state") == "RUNNING"
    ]
    if len(tasks) != 1 or tasks[0].get("state", {}).get("life_cycle_state") != "RUNNING":
        raise Conflict("publication task is not the active expected task")
    actual_code = wire.digest(read_code())
    if actual_code != expected_code_sha256:
        raise Conflict("publication application archive changed")
    observation = dict(
        job_id=job_id,
        run_id=run_id,
        task_key=task_key,
        observed_active_run_id=active[0]["run_id"],
        job_definition_sha256=job_digest(settings),
        code_sha256=actual_code,
        observed_at=observed_at.isoformat(),
        backend_checked_at=observed_at.timestamp(),
    )
    write(evidence_ref, observation)
    validation = {
        key: value
        for key, value in observation.items()
        if key not in ("backend_checked_at", "observed_at")
    }
    validation.update(
        observed_at=observed_at,
        evidence_ref=str(evidence_ref),
        evidence_sha256=wire.digest(wire.encode(observation)),
    )
    return dict(
        kind="serialized_job_execution",
        job_id=job_id,
        run_id=run_id,
        task_key=task_key,
        job_definition_sha256=expected_settings_sha256,
        code_sha256=expected_code_sha256,
        max_concurrent_runs=1,
        queue_enabled=False,
        exclusive_validation=validation,
    )
