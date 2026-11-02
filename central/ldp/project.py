"""Bind versioned UMF deployment inputs and call the owning TableSpec compiler.

No Spark graph, SQL rewriting or alternate compiler lives here. The resolved UMF
is emitted unchanged alongside exact compiler output and a provenance manifest.
"""

import hashlib
import importlib.metadata
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
TABLE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*\.[A-Za-z][A-Za-z0-9_]*\.[A-Za-z][A-Za-z0-9_]*$")
IDENTITY = ("enrollment_id", "installation_id", "destination_generation")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def bind_document(profile, template=None):
    """Materialize explicit deployment bindings, preserving the typed vocabulary."""
    namespace = profile["namespace"]
    source = profile["source_table"]
    identity = profile["identity"]
    if not isinstance(namespace, str) or not NAME.fullmatch(namespace):
        raise ValueError("invalid LDP namespace")
    if not isinstance(source, str) or not TABLE.fullmatch(source):
        raise ValueError("fully qualified LDP source required")
    if set(identity) != set(IDENTITY) or any(
        not isinstance(v, str) or not v for v in identity.values()
    ):
        raise ValueError("exact processing identity required")
    if profile["mode"] not in ("synthetic", "production"):
        raise ValueError("explicit deployment mode required")
    if profile["mode"] == "synthetic" and not identity["enrollment_id"].startswith("synthetic-"):
        raise ValueError("synthetic deployment requires fabricated identity")
    document = json.loads(
        encoded(template)
        if template is not None
        else (ROOT / "umf/revisions.umf.json").read_bytes()
    )
    if document["vocabularies"]["tablespec.pipeline"]["version"] != "0.2.0":
        raise ValueError("typed pipeline vocabulary 0.2.0 required")
    table = namespace + "_revisions"
    module = document["modules"][0]
    module["namespace"] = table
    document["id"] = table
    metadata = module["extensions"]["tablespec.pipeline"]["metadata"]
    metadata["description"] = (
        "Erg immutable revision pipeline; explicit " + profile["mode"] + " profile."
    )
    metadata["source"].update(
        table=source, starting_version=profile.get("starting_version", 0), identity_scope=identity
    )
    ldp = metadata["metadata"]["ldp"]
    prefix = ".".join(source.split(".")[:2]) + "."
    targets = dict(
        raw_target=prefix + "raw_" + table,
        sequence_decisions_target=prefix + "scoped_sequence_decisions_" + table,
        target=prefix + namespace + "_archive",
        fact_events_target=prefix + "revision_fact_events_" + table,
        latest_target=prefix + namespace + "_latest",
    )
    ldp["archive"].update(targets)
    ldp["required_dependencies"] = list(targets.values()) + profile.get("extra_dependencies", [])
    transform = ldp["transform"]
    transform.update(
        artifact_uri=profile["artifact_uri"],
        sha256=profile["artifact_sha256"],
        version=profile["artifact_version"],
    )
    for stage in transform["stages"]:
        if profile.get("entrypoint_module"):
            stage["entrypoint"] = (
                profile["entrypoint_module"] + "." + stage["entrypoint"].rsplit(".", 1)[-1]
            )
        for direction in ("input", "output"):
            binding = stage[direction]
            name = binding["schema_id"].split(":")[-2]
            raw = (ROOT / "schemas" / (name + ".json")).read_bytes()
            schema = json.loads(raw)
            if schema["$id"] != binding["schema_id"]:
                raise ValueError("schema identity differs from declaration")
            binding.update(sha256=digest(raw), schema_uri=profile["schema_uris"][name])
    return document


def compile_project(profile, output, template=None):
    from tablespec.ldp import assess_ldp_readiness, generate_ldp_project
    from tablespec.models.umf import UMF
    from umf import Document

    document = bind_document(profile, template)
    view = UMF.from_document(Document.model_validate(document))
    readiness = assess_ldp_readiness([view])[view.table_name]
    if readiness.status != "ready":
        raise ValueError("static LDP readiness failed")
    files = generate_ldp_project([view])
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    for name, value in files.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    (output / "resolved.umf.json").write_bytes(encoded(document))
    source_pins = {}
    import tablespec.ldp.artifact_runtime as loader
    import tablespec.ldp.project as compiler
    import tablespec.ldp.stateful as stateful

    for module in (compiler, stateful, loader):
        source_pins[module.__name__] = digest(Path(module.__file__).read_bytes())
    manifest = dict(
        document_sha256=digest(encoded(document)),
        packages={name: importlib.metadata.version(name) for name in ("umf-core", "umf-tablespec")},
        compiler_source_sha256=source_pins,
        file_sha256={name: digest(value.encode()) for name, value in files.items()},
        runtime_libraries=[
            "raw/raw_" + view.table_name + ".sql",
            "pipeline/ldp_" + view.table_name + ".py",
        ],
        static_readiness=readiness.model_dump(mode="json"),
        production_cutover_allowed=False,
    )
    (output / "manifest.json").write_bytes(encoded(manifest))
    return manifest
