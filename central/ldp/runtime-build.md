# Native runtime qualification

Keep compiler/build dependencies separate from the installed Erg runtime. The
compiler lock in this directory pins TableSpec source commit
`236c32f1fee17cfc949c4eb0479765d4797af165` and UMF core 0.10.3. The deployment
build labels that exact TableSpec source `0.0.16.dev1`; generated-file and source
hashes must match the locked compiler tests before deployment.

Databricks serverless allocated both Linux ARM and x86 during qualification.
UMF core 0.10.3 publishes a Linux x86 wheel but no Linux ARM wheel. The private
ARM build used the exact published source archive, its bundled Cargo.lock and
Rust 1.90.0. No UMF source or published release asset was changed.

| Input/artifact | SHA-256 |
|---|---|
| UMF core 0.10.3 source archive | `61b3e12a84f68f890fa34e2da14bbda47e101b7c6eadd5207680827e8daa694c` |
| Official Linux x86 wheel, cp311 abi3 / manylinux 2.34 | `cd35f255900ec122e5e2d47d1fbac658cc127d3f16ba82e643e199b7d8cebd75` |
| Private Linux ARM wheel, cp311 abi3 / audited manylinux 2.17+ | `b063ac9dd94a1c721fddd3c916ca7fa1758e0f93e10036b265bb2a0bbf39eba6` |
| TableSpec deployment wheel 0.0.16.dev1 | `9df6e867a0060fb0ecfeac1ff0216385ebf5854ad81ed86559864ca060f4d1ff` |

The ARM wheel was built in
`ghcr.io/pyo3/maturin@sha256:8b1adbcdc912ea215e6f45e1838006f234b17412c9234918a85c4865ea910258`
with `/opt/python/cp312-cp312/bin/python -m hatchling build -t wheel`, then
`auditwheel repair --plat manylinux_2_28_aarch64`. Auditwheel verified the ABI;
the filename was not manually retagged. A separate Linux ARM install verified
exact compiler-output parity and ran authority/readiness tests (33 passed, two
Spark-only skips). Native LDP output checks additionally exercise the real worker.

Freeze a complete hash-checked dependency resolution for Python 3.12 with managed
NumPy 2.1.3 and Pandas 2.2.3 constraints. Use mutually exclusive
`platform_machine == 'aarch64'` / `'x86_64'` markers for the two content-addressed
core wheel paths. Pin the compiler wheel by content hash too. Install neither
PySpark nor a replacement PyArrow; preserve the managed Spark/PyArrow runtime.

Build the pure adapter with `scripts/package-ldp-adapter.py --runtime-pins`.
Runtime pins include Python and package versions, managed PySpark's public
`__version__` (Databricks does not supply its pip metadata), critical compiler
source hashes, allowed architectures and the exact native binary hash for each
architecture. An admission mismatch is an operational failure, never a data
conflict. Install requirements and schema/adapter assets at immutable private
paths, read them back, and retain their hashes in the deployment workload
manifest. The job definition externally pins deployment configuration bytes.
