#!/usr/bin/env python3
"""Deploy the same erg reporter as a private, user-authorized Databricks App.

Creates no warehouse and changes no source table or catalog permissions.
The operator supplies an existing provisioned erg namespace.
"""

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--warehouse", required=True)
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--name", default="erg")
    args = ap.parse_args()
    root = Path(__file__).resolve().parents[1]

    def cli(*parts):
        r = subprocess.run(
            ["databricks"] + list(parts) + ["-p", args.profile, "-o", "json"],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if r.returncode:
            raise RuntimeError(r.stderr.strip())
        if not r.stdout.strip():
            return {}
        try:
            return json.loads(r.stdout)
        except ValueError:
            return {}

    # Validate identifiers before making any external changes.
    import sys

    sys.path.insert(0, str(root))
    from erg.databricks import namespace

    namespace(args.namespace)
    me = cli("current-user", "me")["userName"]
    source = "/Workspace/Users/" + me + "/erg-reporter/" + args.name
    listing = cli("apps", "list")
    apps = listing if isinstance(listing, list) else listing.get("apps", [])
    with tempfile.TemporaryDirectory(prefix="erg-deploy-") as td:
        dst = Path(td)
        shutil.copytree(
            root / "erg", dst / "erg", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
        (dst / "requirements.txt").write_text("# Standard-library runtime; no external packages.\n")
        # JSON is valid YAML and avoids introducing a YAML runtime dependency.
        config = dict(
            command=[
                "python",
                "-m",
                "erg.cli",
                "serve",
                "--bind",
                "0.0.0.0",
                "--backend",
                "databricks",
            ],
            env=[
                dict(name="ERG_NAMESPACE", value=args.namespace),
                dict(name="ERG_WAREHOUSE_ID", value=args.warehouse),
                dict(name="ERG_AUTH_MODE", value="user"),
            ],
        )
        (dst / "app.yaml").write_text(json.dumps(config, indent=2))
        cli("workspace", "import-dir", str(dst), source, "--overwrite")
        if not any(a.get("name") == args.name for a in apps):
            body = dict(
                name=args.name,
                description="Private erg unified session reporting",
                compute_size="MEDIUM",
                user_api_scopes=["sql:restricted-query"],
            )
            bodypath = dst / "create.json"
            bodypath.write_text(json.dumps(body))
            app = cli("apps", "create", "--json", "@" + str(bodypath), "--no-wait")
            print("created", app["name"], flush=True)
        # Existing app permissions and resources are deliberately preserved.
        result = cli("apps", "deploy", args.name, "--source-code-path", source, "--no-wait")
        print(json.dumps(dict(app=args.name, workspace_source=source, deployment=result), indent=2))
        print("First browser access requires Databricks sign-in and read-only SQL consent.")


if __name__ == "__main__":
    main()
