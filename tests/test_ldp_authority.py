import copy
import unittest

from central.erg_central.ldp_authority import bind_job_settings, collect_job_authority, job_digest
from central.erg_central.receiver import Conflict
from erg import delivery_protocol as wire


class BackendAuthority(unittest.TestCase):
    def setUp(self):
        self.settings = dict(
            max_concurrent_runs=1, queue=dict(enabled=False), tasks=[dict(task_key="publish")]
        )
        self.expected = job_digest(self.settings)
        self.runs = dict(runs=[dict(run_id=456)])
        self.run = dict(
            job_id=123,
            state=dict(life_cycle_state="RUNNING"),
            tasks=[dict(task_key="publish", state=dict(life_cycle_state="RUNNING"))],
        )
        self.code = b"immutable application"
        self.writes = []

    def api(self, method, path):
        self.assertEqual(method, "GET")
        if "jobs/get?" in path:
            return dict(settings=copy.deepcopy(self.settings))
        if "runs/list?" in path:
            return self.runs
        if "runs/get?" in path:
            return self.run
        self.fail(path)

    def collect(self):
        return collect_job_authority(
            self.api,
            123,
            456,
            "publish",
            self.expected,
            wire.digest(b"immutable application"),
            lambda: self.code,
            "private-observation.json",
            lambda path, value: self.writes.append((path, value)),
        )

    def test_real_backend_shape_produces_pinned_observation_without_fence(self):
        value = self.collect()
        self.assertEqual(value["kind"], "serialized_job_execution")
        self.assertNotIn("writer_fence", value)
        observation = self.writes[0][1]
        self.assertEqual(
            value["exclusive_validation"]["evidence_sha256"], wire.digest(wire.encode(observation))
        )
        self.assertEqual(value["exclusive_validation"]["observed_active_run_id"], 456)

    def test_definition_and_code_changes_fail_before_any_receipt(self):
        self.settings["name"] = "changed"
        with self.assertRaises(Conflict):
            self.collect()
        self.settings.pop("name")
        self.code = b"changed"
        with self.assertRaises(Conflict):
            self.collect()
        self.assertEqual(self.writes, [])

    def test_competing_missing_paginated_or_wrong_run_refuses_publication(self):
        for result in (
            dict(runs=[]),
            dict(runs=[dict(run_id=999)]),
            dict(runs=[dict(run_id=456), dict(run_id=999)]),
            dict(runs=[dict(run_id=456)], has_more=True),
            dict(runs=[dict(run_id=456)], next_page_token="more"),
        ):
            self.runs = result
            with self.subTest(result=result), self.assertRaises(Conflict):
                self.collect()
        self.assertEqual(self.writes, [])

    def test_job_and_task_identity_and_running_state_are_observed(self):
        for field, value in (
            ("job_id", 999),
            ("state", dict(life_cycle_state="TERMINATED")),
            ("tasks", []),
            ("tasks", [dict(task_key="publish", state=dict(life_cycle_state="SUCCESS"))]),
        ):
            old = self.run[field]
            self.run[field] = value
            with self.subTest(field=field), self.assertRaises(Conflict):
                self.collect()
            self.run[field] = old
        self.assertEqual(self.writes, [])

    def test_native_terminal_cursor_is_followed_and_old_task_attempt_is_ignored(self):
        self.runs = dict(runs=[dict(run_id=456)], has_more=False, next_page_token="end")
        self.run["tasks"].append(
            dict(task_key="publish", state=dict(life_cycle_state="TERMINATED"))
        )
        original = self.api

        def api(method, path):
            if "page_token=end" in path:
                return dict(runs=[], has_more=False, next_page_token="end")
            return original(method, path)

        self.api = api
        self.assertEqual(self.collect()["run_id"], 456)

    def test_backend_failure_does_not_become_an_asserted_authority(self):
        def failed(*_):
            raise RuntimeError("backend unavailable")

        with self.assertRaises(RuntimeError):
            collect_job_authority(
                failed,
                123,
                456,
                "publish",
                self.expected,
                wire.digest(self.code),
                lambda: self.code,
                "observation.json",
                lambda *_: self.fail("write"),
            )


class JobConfigurationBinding(unittest.TestCase):
    def test_full_job_settings_bind_external_config_hash_without_self_reference(self):
        template = dict(
            tasks=[
                dict(
                    spark_python_task=dict(
                        parameters=["--deployment-sha256", "__ERG_DEPLOYMENT_SHA256__"]
                    )
                )
            ]
        )
        result = bind_job_settings(template, "a" * 64)
        self.assertEqual(result["tasks"][0]["spark_python_task"]["parameters"][-1], "a" * 64)
        self.assertEqual(
            template["tasks"][0]["spark_python_task"]["parameters"][-1], "__ERG_DEPLOYMENT_SHA256__"
        )
        for invalid in (dict(tasks=[]), result):
            with self.assertRaises(Conflict):
                bind_job_settings(invalid, "b" * 64)
