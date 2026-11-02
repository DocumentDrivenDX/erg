"""Databricks job adapter for generated LDP staging and guarded publication."""

import importlib
import importlib.metadata
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from erg import delivery_protocol as wire

from .ldp_authority import bind_job_settings, collect_job_authority, job_digest
from .processor import atomic_control
from .receiver import Conflict


def read_file(value, limit=32 * 1024 * 1024):
    path = Path("/Workspace" + value if value.startswith(("/Users/", "/Shared/")) else value)
    if not path.is_file() or path.stat().st_size > limit:
        raise Conflict("declared deployment artifact unavailable")
    return path.read_bytes()


def native_api(method, path, body=None):
    from databricks.sdk import WorkspaceClient

    parsed = urlsplit(path)
    return WorkspaceClient().api_client.do(
        method, parsed.path, query=dict(parse_qsl(parsed.query)) or None, body=body
    )


def code_bytes(config):
    observed = {path: wire.digest(read_file(path)) for path in config["code_manifest"]}
    if observed != config["code_manifest"]:
        raise Conflict("declared workload artifact changed")
    return wire.encode(observed)


def verify_compiler(config):
    for package, expected in config["compiler_packages"].items():
        if importlib.metadata.version(package) != expected:
            raise Conflict("publication compiler version differs")
    for name, expected in config["compiler_source_sha256"].items():
        module = importlib.import_module(name)
        if wire.digest(read_file(module.__file__)) != expected:
            raise Conflict("publication compiler source differs")


def validate_config(config):
    if config.get("version") != 1 or config.get("mode") not in ("synthetic", "production"):
        raise Conflict("explicit LDP deployment profile required")
    identity = config["identity"]
    if set(identity) != set(wire.ENROLLMENT):
        raise Conflict("complete LDP identity required")
    if config["mode"] == "synthetic":
        if (
            not identity["enrollment_id"].startswith("synthetic-")
            or "/qualification/ergqual_" not in config["control"]
        ):
            raise Conflict("synthetic publisher control must be isolated")
        if any(
            not config[key].startswith("erg.telemetry.ergqual_")
            for key in ("source_table", "records_table", "items_table")
        ):
            raise Conflict("synthetic publisher table must be isolated")
    elif config["control"] != "/Volumes/erg/ingest/control":
        raise Conflict("production control must be the qualified private root")
    if config["mode"] == "production":
        admission = config.get("admission", {})
        mat, proj, version = (
            admission.get(key)
            for key in ("materialized_through", "projected_through", "raw_version")
        )
        if (
            any(type(value) is not int for value in (mat, proj, version))
            or not 0 <= proj <= mat
            or version < 0
            or admission.get("source_table_id") != config.get("source_table_id")
            or not admission.get("source_table_id")
            or not admission.get("raw_table_id")
            or not admission.get("raw_table")
        ):
            raise Conflict("explicit pinned legacy admission boundary required")
    if wire.digest(wire.encode(config["code_manifest"])) != config["code_sha256"]:
        raise Conflict("workload manifest digest differs")


def check_pipeline(api, config, run_id):
    pipeline = api("GET", "/api/2.0/pipelines/" + config["pipeline_id"])
    if pipeline.get("state") != "IDLE" or not pipeline.get("latest_updates"):
        raise Conflict("generated pipeline is not idle after an update")
    latest = pipeline["latest_updates"][0]
    if latest.get("state") != "COMPLETED":
        raise Conflict("generated pipeline update did not complete")
    if pipeline.get("run_as_user_name") != config["processor_principal"]:
        raise Conflict("generated pipeline execution identity changed")
    if wire.digest(wire.encode(pipeline["spec"])) != config["pipeline_spec_sha256"]:
        raise Conflict("generated pipeline configuration changed")
    if pipeline["spec"].get("libraries") != config["pipeline_libraries"]:
        raise Conflict("generated pipeline libraries changed")
    if config["mode"] == "synthetic":
        if latest["update_id"] != config["expected_update_id"]:
            raise Conflict("synthetic update identity changed")
    else:
        run = api("GET", "/api/2.1/jobs/runs/get?run_id=" + str(run_id))
        tasks = {task["task_key"]: task for task in run.get("tasks", [])}
        if any(
            tasks.get(key, {}).get("state", {}).get("result_state") != "SUCCESS"
            for key in ("stage", "pipeline")
        ):
            raise Conflict("publication prerequisites did not succeed in this job run")
        task = tasks["pipeline"].get("pipeline_task", {})
        if (
            task.get("pipeline_id") != config["pipeline_id"]
            or task.get("full_refresh", False) is not False
        ):
            raise Conflict("job pipeline task differs from declared incremental pipeline")
        created = (
            datetime.fromisoformat(latest["creation_time"].replace("Z", "+00:00")).timestamp()
            * 1000
        )
        if created < run["start_time"]:
            raise Conflict("pipeline update predates this job run")
    return latest["update_id"]


def stage(spark, config):
    from databricks.sdk.runtime import dbutils

    from .ldp_stage import StageProcessor
    from .processor import run
    from .zerobus import Zerobus

    settings = config["zerobus"]
    secret = dbutils.secrets.get(scope=settings["secret_scope"], key=settings["secret_key"])
    client = Zerobus(
        settings["workspace"],
        settings["workspace_id"],
        settings["endpoint"],
        settings["table"],
        settings["client_id"],
        secret,
        oauth_scope=settings["oauth_scope"],
    )
    private = Path(config["control"]) / "ldp-staging"

    def factory(spark, client, catalog, ingest, telemetry, control):
        return StageProcessor(
            spark,
            client,
            catalog,
            ingest,
            telemetry,
            control,
            config["source_table"],
            config["identity"],
            config["source_table_id"],
            after_sequence=config.get("admission", {}).get("materialized_through", 0),
        )

    return run(
        spark,
        client,
        config["spool"],
        private,
        config["catalog"],
        config["ingest_schema"],
        config["telemetry_schema"],
        max_batches=2048,
        max_seconds=200,
        processor_factory=factory,
    )


def execute(spark, config_path, job_id, run_id, task_key, action, api=native_api):
    raw = read_file(config_path, 1024 * 1024)
    config = wire.decode(raw)
    validate_config(config)
    config_sha = wire.digest(raw)

    expected_update = [None]

    def recheck():
        if wire.digest(read_file(config_path, 1024 * 1024)) != config_sha:
            raise Conflict("deployment configuration changed")
        code_bytes(config)
        update = check_pipeline(api, config, run_id)
        if expected_update[0] is not None and update != expected_update[0]:
            raise Conflict("pipeline update changed during publication")
        return update

    code_bytes(config)
    if action == "stage":
        return stage(spark, config)
    if action != "publish":
        raise ValueError("unknown LDP job action")
    from tablespec.ldp.capability import capability_for_umf
    from tablespec.models.umf import UMF
    from umf import Document

    from .ldp_publish import publish

    document = Document.model_validate(json.loads(read_file(config["document"], 1024 * 1024)))
    capability = capability_for_umf(UMF.from_document(document))
    if (
        capability.source.table != config["source_table"]
        or capability.source.identity_scope.model_dump() != config["identity"]
    ):
        raise Conflict("resolved UMF and deployment scope differ")
    verify_compiler(config)
    expected_update[0] = recheck()

    def authority():
        ref = (
            Path(config["control"])
            / "ldp-authority"
            / (str(run_id) + "-" + uuid.uuid4().hex + ".json")
        )
        return collect_job_authority(
            api,
            job_id,
            run_id,
            task_key,
            job_digest(bind_job_settings(config["job_settings_template"], config_sha)),
            config["code_sha256"],
            lambda: code_bytes(config),
            ref,
            atomic_control,
        )

    authority()
    started = time.monotonic()
    try:
        result = publish(
            spark,
            capability,
            config,
            authority,
            recheck,
            (lambda phase: (_ for _ in ()).throw(RuntimeError("synthetic publication crash")))
            if config.get("fail_after_copy") and config["mode"] == "synthetic"
            else None,
        )
    except Conflict as error:
        if (
            config.get("expect_conflict") is True
            and config["mode"] == "synthetic"
            and str(error) == config.get("expected_conflict_reason")
        ):
            return dict(state="expected_conflict_blocked", no_receipt_published=True)
        raise
    if config.get("expect_conflict") is True:
        raise AssertionError("fault fixture unexpectedly published")
    return dict(result, elapsed_seconds=time.monotonic() - started)
