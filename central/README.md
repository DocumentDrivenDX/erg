# Experimental central delivery

The 0.1.0rc1 release candidate supports local collection, SQLite storage and the
loopback dashboard. Central delivery and cloud reporting code is retained for
contributors and is excluded from the installed local package's dependencies.
It is experimental and requires separately provisioned infrastructure, explicit
configuration, enrollment and access qualification. Installation does not enable
export or deploy infrastructure. See [privacy](../PRIVACY.md).

Application source lives in `erg_central/`, `receiver/` and `databricks/`.
The local client/protocol modules remain in `erg/` so local status and opt-in
configuration do not require Azure, Spark or cryptographic libraries.

Build reviewable artifacts from explicit allowlists:

```sh
python3 -B scripts/package-central-receiver.py /tmp/erg-central-receiver.zip
python3 -B scripts/package-central-processor.py /tmp/erg-central.pyz
python3 -B scripts/check-delivery-boundaries.py
```

The receiver requires the dependencies in `receiver/requirements.txt`. The
processor requires a qualified Databricks runtime and explicitly scoped identity.
Generated archives, bundle deployment state and operator-specific qualification
logs are local artifacts and are not committed or included in the source release.
The bundle is paused and execution-disabled by default; configure its workspace
through an explicitly selected Databricks profile or `DATABRICKS_HOST`.

Fleet enrollment/recovery, retention policy, sustained cloud capacity and general
central reporting acceptance are separate from the local release gate. No local
test result establishes production readiness for another deployment.

For Unity Catalog volumes, production reconciliation uses resumable Files API
pages and an incremental completion cache. See [bounded central discovery](DISCOVERY.md)
for cursor semantics, runtime qualification and rollback. The UMF/TableSpec
project remains a separate migration candidate.
