"""Bundle entrypoint. No cloud action unless enable flag explicitly true."""

import argparse
import sys
from pathlib import Path


# Databricks executes script tasks without defining __file__. The deployment
# passes the exact private application archive explicitly instead of guessing.
def application_archive(value):
    if value.startswith("/Users/") or value.startswith("/Shared/"):
        value = "/Workspace" + value
    path = Path(value)
    if not path.is_file():
        raise ValueError("deployed application archive unavailable")
    return str(path)


def main():
    parser = argparse.ArgumentParser()
    for name in (
        "action",
        "enabled",
        "workspace",
        "workspace-id",
        "zerobus-endpoint",
        "catalog",
        "ingest-schema",
        "telemetry-schema",
        "spool-volume",
        "control-volume",
        "client-id",
        "secret-scope",
        "secret-key",
        "oauth-scope",
    ):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--verification-manifest", default="")
    parser.add_argument("--application-archive", required=True)
    args = parser.parse_args()
    if args.enabled != "true":
        raise SystemExit("central execution disabled; qualify auth/private access/retention first")
    sys.path.insert(0, application_archive(args.application_archive))
    from erg_central.ddl import statements
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.getOrCreate()
    if args.action == "migrate":
        for statement in statements(args.catalog, args.ingest_schema, args.telemetry_schema):
            spark.sql(statement)
        return
    if args.action == "verify":
        if not args.verification_manifest:
            print('{"synthetic_verification":"not requested"}')
            return
        import json

        from erg_central.verify import verify

        print(
            json.dumps(
                dict(
                    synthetic_verification=verify(
                        spark,
                        args.verification_manifest,
                        args.catalog,
                        args.ingest_schema,
                        args.telemetry_schema,
                        args.control_volume,
                    )
                ),
                sort_keys=True,
            )
        )
        return
    if args.action != "reconcile":
        raise SystemExit("unknown action")
    # Approved, dedicated processor credential only. Missing scope/key fails closed.
    # Runtime authentication is used only to read this approved secret scope.
    # Zerobus still receives its own explicit, narrowly scoped OAuth credential.
    from databricks.sdk.runtime import dbutils

    credential = dbutils.secrets.get(scope=args.secret_scope, key=args.secret_key)
    from erg_central.processor import run
    from erg_central.zerobus import Zerobus, table_name

    client = Zerobus(
        args.workspace,
        args.workspace_id,
        args.zerobus_endpoint,
        table_name(args.catalog, args.ingest_schema, "delivery_items_raw"),
        args.client_id,
        credential,
        oauth_scope=args.oauth_scope,
    )
    result = run(
        spark,
        client,
        args.spool_volume,
        args.control_volume,
        args.catalog,
        args.ingest_schema,
        args.telemetry_schema,
    )
    import json

    print(json.dumps(dict(erg_processing_metrics=result), sort_keys=True))


if __name__ == "__main__":
    main()
