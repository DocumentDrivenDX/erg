"""Pinned generated-LDP job bootstrap; no implicit local or cloud activation."""

import argparse
import hashlib
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", choices=("stage", "publish"), required=True)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--deployment-sha256", required=True)
    parser.add_argument("--application-archive", required=True)
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--task-key", required=True)
    args = parser.parse_args()
    config_path = Path(args.deployment)
    if (
        not str(config_path).startswith("/Volumes/erg/ingest/control/")
        or ".." in config_path.parts
        or config_path.stat().st_size > 1024 * 1024
    ):
        raise ValueError("qualified private deployment required")
    raw = config_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != args.deployment_sha256:
        raise ValueError("deployment configuration digest differs")
    config = json.loads(raw)
    archive = Path(
        "/Workspace" + args.application_archive
        if args.application_archive.startswith(("/Users/", "/Shared/"))
        else args.application_archive
    )
    if hashlib.sha256(archive.read_bytes()).hexdigest() != config["application_sha256"]:
        raise ValueError("application archive digest differs")
    sys.path.insert(0, str(archive))
    from erg_central.ldp_driver import execute
    from pyspark.sql import SparkSession

    result = execute(
        SparkSession.builder.getOrCreate(),
        str(config_path),
        args.job_id,
        args.run_id,
        args.task_key,
        args.action,
    )
    print(json.dumps(dict(erg_ldp_metrics=result), sort_keys=True))


if __name__ == "__main__":
    main()
