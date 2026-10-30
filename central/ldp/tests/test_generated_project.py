"""Compiler-environment tests; they do not claim native LDP execution."""

import ast
import copy
import json
import tempfile
import unittest
from pathlib import Path

from tablespec.ldp import generate_ldp_project
from tablespec.ldp.capability import capability_for_umf
from tablespec.models.umf import UMF
from umf import Document

from central.ldp.project import ROOT, bind_document, compile_project, digest
from central.ldp.qualification import IDENTITY


class GeneratedProject(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.profile = dict(
            mode="synthetic",
            namespace="ergqual_output",
            source_table="erg.telemetry.ergqual_output_source",
            identity=IDENTITY,
            artifact_uri="/private/qualified-adapter.zip",
            artifact_sha256="a" * 64,
            artifact_version="1.1.0",
            schema_uris={p.stem: str(p) for p in (ROOT / "schemas").glob("*.json")},
        )

    def test_files_equal_owning_emitter_without_rewritten_spark_graph(self):
        document = bind_document(self.profile)
        view = UMF.from_document(Document.model_validate(document))
        expected = generate_ldp_project([view])
        manifest = compile_project(self.profile, self.directory / "project")
        for name, value in expected.items():
            self.assertEqual((self.directory / "project" / name).read_text(), value)
            self.assertEqual(manifest["file_sha256"][name], digest(value.encode()))
        self.assertFalse(manifest["production_cutover_allowed"])
        self.assertEqual(manifest["static_readiness"]["status"], "ready")
        self.assertEqual(len(manifest["static_readiness"]["generated_pipeline_dependencies"]), 5)

    def test_generated_graph_keeps_append_state_and_separate_latest_projection(self):
        manifest = compile_project(self.profile, self.directory / "project")
        raw = (self.directory / "project" / manifest["runtime_libraries"][0]).read_text()
        code = (self.directory / "project" / manifest["runtime_libraries"][1]).read_text()
        ast.parse(code)
        self.assertIn("STARTINGVERSION=0", raw)
        for value in IDENTITY.values():
            self.assertIn(value, raw)
        self.assertEqual(code.count(".transformWithStateInPandas("), 2)
        self.assertIn('outputMode="Append"', code)
        self.assertIn("dp.create_auto_cdc_flow(", code)
        self.assertIn('sequence_by=F.col("record_revision")', code)
        self.assertIn("_ARCHIVE.archive_schema.items()", code)
        self.assertNotIn("collect_list", code)
        self.assertNotIn("withWatermark", code)
        self.assertNotIn("MERGE INTO", code)
        self.assertEqual(code.count('table_properties={"pipelines.reset.allowed": "false"}'), 2)

    def test_exact_bigint_schema_and_typed_pinned_stages_survive_umf_roundtrip(self):
        document = bind_document(self.profile)
        view = UMF.from_document(Document.model_validate(document))
        capability = capability_for_umf(view)
        self.assertEqual(capability.archive.archive_schema["record_revision"], "long")
        self.assertEqual(capability.archive.archive_schema["delivery_sequence"], "long")
        self.assertEqual(capability.transform.sha256, "a" * 64)
        self.assertEqual(capability.transform.max_group_items, 128)
        self.assertEqual(capability.transform.max_group_bytes, 96 * 1024 * 1024)
        for stage in capability.transform.stages:
            for binding in (stage.input, stage.output):
                self.assertEqual(digest(Path(binding.schema_uri).read_bytes()), binding.sha256)

    def test_invalid_scope_path_or_old_vocabulary_cannot_compile(self):
        for value in (
            dict(namespace="../escape"),
            dict(source_table="erg.telemetry.source;DROP"),
            dict(identity=dict(IDENTITY, unqualified="extra")),
            dict(mode="implicit"),
            dict(identity=dict(IDENTITY, enrollment_id="real")),
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bind_document(dict(self.profile, **value))
        template = json.loads((ROOT / "umf/revisions.umf.json").read_text())
        template["vocabularies"]["tablespec.pipeline"]["version"] = "0.1.0"
        with self.assertRaises(ValueError):
            bind_document(self.profile, template)

    def test_profiles_are_resolved_explicitly_without_mutating_authoritative_template(self):
        original = (ROOT / "umf/revisions.umf.json").read_bytes()
        profile = copy.deepcopy(self.profile)
        profile["namespace"] = "ergqual_second"
        profile["entrypoint_module"] = "erg_central.ldp_runtime"
        first = bind_document(self.profile)
        second = bind_document(profile)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual((ROOT / "umf/revisions.umf.json").read_bytes(), original)
        view = UMF.from_document(Document.model_validate(second))
        for stage in capability_for_umf(view).transform.stages:
            self.assertTrue(stage.entrypoint.startswith("erg_central.ldp_runtime."))
