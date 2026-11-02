"""Build orchestration for an already compiled and pinned LDP pipeline.

This module emits Jobs settings only. It neither generates a data graph nor
creates cloud resources. Backend-normalized settings must be read back before
binding the external deployment digest with ldp_authority.bind_job_settings.
"""

import re

PLACEHOLDER = "__ERG_DEPLOYMENT_SHA256__"


def job_settings(profile):
    """Produce the sole-writer job template, initially paused for cutover."""
    required = {
        "name",
        "processor_principal",
        "pipeline_id",
        "bootstrap",
        "application_archive",
        "deployment",
        "requirements",
    }
    if set(profile) != required:
        raise ValueError("explicit orchestration profile fields required")
    if not isinstance(profile["name"], str) or not profile["name"].strip():
        raise ValueError("explicit job name required")
    for field in ("processor_principal", "pipeline_id"):
        if not isinstance(profile[field], str) or not re.fullmatch(
            r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", profile[field]
        ):
            raise ValueError("explicit backend identity required")
    for field in ("bootstrap", "application_archive", "deployment", "requirements"):
        value = profile[field]
        prefix = (
            "/Volumes/erg/ingest/control/" if field in ("deployment", "requirements") else "/Users/"
        )
        if (
            not isinstance(value, str)
            or not value.startswith(prefix)
            or any(part in ("", ".", "..") for part in value.split("/")[1:])
            or "\\" in value
            or any(char.isspace() for char in value)
        ):
            raise ValueError("explicit private immutable artifact path required")
        if field != "deployment" and not any(
            re.fullmatch(r"[0-9a-f]{64}", part) for part in value.split("/")
        ):
            raise ValueError("content-addressed deployment artifact required")

    def python_task(action):
        return dict(
            task_key=action,
            environment_key="central",
            max_retries=0,
            spark_python_task=dict(
                python_file=profile["bootstrap"],
                parameters=[
                    "--action",
                    action,
                    "--deployment",
                    profile["deployment"],
                    "--deployment-sha256",
                    PLACEHOLDER,
                    "--application-archive",
                    profile["application_archive"],
                    "--job-id",
                    "{{job.id}}",
                    "--run-id",
                    "{{job.run_id}}",
                    "--task-key",
                    action,
                ],
            ),
        )

    stage, publish = python_task("stage"), python_task("publish")
    publish["depends_on"] = [dict(task_key="pipeline")]
    return dict(
        name=profile["name"],
        max_concurrent_runs=1,
        queue=dict(enabled=False),
        timeout_seconds=900,
        run_as=dict(service_principal_name=profile["processor_principal"]),
        schedule=dict(
            quartz_cron_expression="0 0/5 * * * ?", timezone_id="UTC", pause_status="PAUSED"
        ),
        environments=[
            dict(
                environment_key="central",
                spec=dict(environment_version="4", dependencies=["-r " + profile["requirements"]]),
            )
        ],
        tasks=[
            stage,
            dict(
                task_key="pipeline",
                depends_on=[dict(task_key="stage")],
                max_retries=0,
                pipeline_task=dict(pipeline_id=profile["pipeline_id"], full_refresh=False),
            ),
            publish,
        ],
    )
