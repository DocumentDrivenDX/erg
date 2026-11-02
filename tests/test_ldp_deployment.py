"""Orchestration safety and external configuration binding."""

import unittest

from central.erg_central.ldp_authority import bind_job_settings
from central.ldp.deployment import PLACEHOLDER, job_settings


class LdpDeployment(unittest.TestCase):
    def setUp(self):
        self.profile = dict(
            name="synthetic-orchestration",
            processor_principal="00000000-0000-0000-0000-000000000001",
            pipeline_id="00000000-0000-0000-0000-000000000002",
            bootstrap="/Users/synthetic/" + "a" * 64 + "/ldp_job.py",
            application_archive="/Users/synthetic/" + "b" * 64 + "/processor.pyz",
            deployment="/Volumes/erg/ingest/control/" + "c" * 64 + "/deployment.json",
            requirements="/Volumes/erg/ingest/control/" + "d" * 64 + "/requirements.txt",
        )

    def test_only_ordered_incremental_pipeline_can_reach_publisher(self):
        settings = job_settings(self.profile)
        stage, pipeline, publish = settings["tasks"]
        self.assertEqual(
            [t["task_key"] for t in settings["tasks"]], ["stage", "pipeline", "publish"]
        )
        self.assertNotIn("depends_on", stage)
        self.assertEqual(pipeline["depends_on"], [dict(task_key="stage")])
        self.assertEqual(publish["depends_on"], [dict(task_key="pipeline")])
        self.assertEqual(
            pipeline["pipeline_task"],
            dict(pipeline_id=self.profile["pipeline_id"], full_refresh=False),
        )
        self.assertEqual(settings["schedule"]["pause_status"], "PAUSED")
        self.assertEqual(settings["max_concurrent_runs"], 1)
        self.assertIs(settings["queue"]["enabled"], False)
        self.assertEqual(
            settings["run_as"]["service_principal_name"], self.profile["processor_principal"]
        )

    def test_external_digest_binds_both_tasks_without_mutating_template(self):
        template = job_settings(self.profile)
        settings = bind_job_settings(template, "e" * 64)
        for task in (settings["tasks"][0], settings["tasks"][2]):
            args = task["spark_python_task"]["parameters"]
            self.assertEqual(args[args.index("--deployment-sha256") + 1], "e" * 64)
            self.assertIn("{{job.id}}", args)
            self.assertIn("{{job.run_id}}", args)
        self.assertIn(PLACEHOLDER, template["tasks"][0]["spark_python_task"]["parameters"])

    def test_mutable_paths_incomplete_profiles_and_implicit_identities_fail_closed(self):
        for value in (
            dict(application_archive="/Users/synthetic/latest.pyz"),
            dict(bootstrap="/Users/synthetic/" + "a" * 64 + "/../job.py"),
            dict(requirements="https://example.invalid/requirements.txt"),
            dict(pipeline_id=True),
            dict(unqualified=True),
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                job_settings(dict(self.profile, **value))
