import copy
import json
import tempfile
import unittest
from pathlib import Path

from erg import (
    call_projection,
    history,
    history_projection,
    project_rollups,
    projects,
    server,
    store,
    work_projection,
)
from erg import (
    cloud_dashboard as cloud,
)
from erg import (
    delivery_protocol as wire,
)


class CloudDashboard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.local = store.connect(self.root / "local.db")
        self.cache = store.connect(self.root / "cloud.db")
        self.addCleanup(self.local.close)
        self.addCleanup(self.cache.close)
        cloud.initialize(self.cache, {"enrollment": "fixture"})
        cfg = projects.snapshot(self.local)
        cfg["projects"] = [dict(project_key="human", name="Human project")]
        cfg["bindings"] = [
            dict(
                binding_id="app",
                selectors={"agent_project": dict(machine="host", tool="pi", id="app")},
                allocations=[dict(project_key="human", units=1000000)],
                valid_from=0,
            )
        ]
        projects.apply(self.local, cfg)
        cloud.sync_policy(self.cache, self.root / "local.db")
        self.payloads = []
        bodies = [
            dict(type="session", id="s", cwd="/work/app"),
            dict(type="model_change", provider="fixture", modelId="test-model"),
            dict(
                type="message",
                id="input",
                message=dict(role="user", content="Build a feature", timestamp=1000),
            ),
            dict(
                type="message",
                id="reply",
                message=dict(
                    role="assistant",
                    model="test-model",
                    usage=dict(input=100, output=20, cacheRead=5, cacheWrite=2),
                ),
            ),
        ]
        for i, body in enumerate(bodies):
            with self.local:
                key, _ = history.put(
                    self.local,
                    "pi",
                    "s",
                    "app",
                    1000 + i,
                    body["type"],
                    body,
                    str(i),
                    machine="host",
                )
            self.payloads.append(
                dict(
                    record_id=key,
                    event_id=key,
                    record_revision=1,
                    record_kind="session_event",
                    machine="host",
                    tool="pi",
                    session="s",
                    project="app",
                    ts=1000 + i,
                    kind=body["type"],
                    role="",
                    origin="native",
                    source="fixture",
                    body=body,
                    capture_version=2,
                )
            )
        self.rows = []
        for p in self.payloads:
            for item in wire.record_items(p, len(self.rows) + 1):
                self.rows.append((item["delivery_seq"], wire.encode(item).decode()))

    def project(self, db, path):
        for _ in range(100):
            a = history_projection.run(db, limit=128, budget=1)
            b = work_projection.run(db, limit=128, budget=1)
            c = call_projection.run(db, seed_limit=10000, limit=10000, budget=1)
            if not a["pending"] and not b["pending"] and c["ready"]:
                break
        with db:
            project_rollups.initialize(db)
            project_rollups.put(db, enabled=1)
        project_rollups.rebuild(path)

    def test_replay_and_shared_reports_preserve_allocation_and_isolate_retained_local_data(self):
        self.assertEqual(cloud.apply_page(self.cache, self.rows), 4)
        self.assertEqual(cloud.apply_page(self.cache, self.rows), 0)
        self.project(self.local, self.root / "local.db")
        self.project(self.cache, self.root / "cloud.db")
        a = server.Reports(database=self.root / "local.db")
        b = server.Reports(database=self.root / "cloud.db", source_backend="databricks")
        self.assertEqual(a.data(0)["llm"], b.data(0)["llm"])
        self.assertEqual(a.data(0)["session_allocations"], b.data(0)["session_allocations"])
        self.assertEqual(a.costs()["totals"], b.costs()["totals"])
        for view in ("work", "accounts"):
            left = json.loads(a.encoded_scoped_work({"start": 0, "end": 2000}, view=view))
            right = json.loads(b.encoded_scoped_work({"start": 0, "end": 2000}, view=view))
            left.pop("rollup_status", None)
            right.pop("rollup_status", None)
            left.pop("generated", None)
            right.pop("generated", None)
            self.assertEqual(left, right)
        self.assertEqual(b.data(0)["backend"], "databricks")
        with self.local:
            history.put(
                self.local,
                "pi",
                "retained",
                "private",
                1,
                "session",
                {"type": "session"},
                "old",
                machine="host",
            )
        self.assertEqual(
            self.cache.execute(
                "SELECT count(*) FROM history_events WHERE session='retained'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.cache.execute("SELECT count(*) FROM delivery_journal").fetchone()[0], 0
        )

    def test_gap_never_advances_cursor_and_corruption_rolls_back_page(self):
        self.assertEqual(cloud.apply_page(self.cache, self.rows[1:]), 0)
        damaged = wire.decode(self.rows[1][1].encode())
        damaged["record_sha256"] = "0" * 64
        with self.assertRaises((ValueError, wire.ProtocolError)):
            cloud.apply_page(self.cache, [self.rows[0], (2, wire.encode(damaged).decode())])
        self.assertEqual(self.cache.execute("SELECT cursor FROM cloud_config").fetchone()[0], 0)
        self.assertEqual(self.cache.execute("SELECT count(*) FROM history_events").fetchone()[0], 0)
        self.assertEqual(cloud.apply_page(self.cache, self.rows), 4)

    def test_partial_chunk_restart_and_policy_revision(self):
        p = self.payloads[0]
        p = copy.deepcopy(p)
        p["body"]["large"] = "x" * (wire.CHUNK_BYTES + 10)
        items = wire.record_items(p, 1)
        self.assertGreater(len(items), 1)
        cloud.apply_page(self.cache, [(1, wire.encode(items[0]).decode())])
        self.assertEqual(self.cache.execute("SELECT count(*) FROM history_events").fetchone()[0], 0)
        self.cache.close()
        self.cache = store.connect(self.root / "cloud.db")
        self.addCleanup(self.cache.close)
        cloud.initialize(self.cache, {"enrollment": "fixture"})
        cloud.apply_page(
            self.cache, [(i["delivery_seq"], wire.encode(i).decode()) for i in items[1:]]
        )
        self.assertEqual(self.cache.execute("SELECT count(*) FROM history_events").fetchone()[0], 1)
        cfg = projects.snapshot(self.local)
        cfg["projects"][0]["name"] = "Renamed"
        projects.apply(self.local, cfg)
        self.assertTrue(cloud.sync_policy(self.cache, self.root / "local.db"))
        self.assertEqual(projects.snapshot(self.local), projects.snapshot(self.cache))
        with self.assertRaises(ValueError):
            cloud.initialize(self.cache, {"enrollment": "other"})

    def test_caught_up_worker_does_not_query_sql_warehouse(self):
        import threading
        from unittest.mock import Mock, patch

        client = Mock()
        client.sql.side_effect = AssertionError("idle cache must not wake SQL")
        cfg = {
            "identity": {
                "enrollment_id": "fixture",
                "installation_id": "install",
                "destination_generation": "generation",
            },
            "cloud": {"namespace": "erg.telemetry", "host": "https://example.invalid"},
        }
        stop = threading.Event()

        def wait(_):
            stop.set()
            return True

        with patch(
            "erg.cloud_history.connection", return_value=(client, "`erg`.`telemetry`", cfg)
        ), patch.object(cloud, "sync_policy", return_value=False), patch.object(
            cloud, "receipt_cursor", return_value=0
        ), patch.object(cloud, "forward_boundary", return_value=100), patch.object(
            stop, "wait", side_effect=wait
        ):
            cloud.worker(self.root / "idle.db", stop)
        client.sql.assert_not_called()

    def test_sql_page_is_bounded_before_inline_materialization(self):
        from unittest.mock import Mock

        client = Mock()
        client.sql.return_value = []
        self.assertEqual(
            cloud.fetch_page(
                self.cache,
                client,
                "`erg`.`telemetry`",
                {"enrollment_id": "e", "destination_generation": "g"},
            ),
            0,
        )
        sql, params = client.sql.call_args[0]
        self.assertIn("LIMIT 10000", sql)
        self.assertIn("bytes<=8388608", sql)
        self.assertIn("octet_length(to_json(array(item_json)))+64", sql)
        self.assertEqual(
            {x["name"]: x["value"] for x in params}, {"e": "e", "g": "g", "after": "0"}
        )

    def test_metadata_lane_uses_verified_receipt_bound_and_separate_cursor(self):
        from unittest.mock import Mock

        client = Mock()
        client.sql.return_value = []
        self.assertEqual(
            cloud.fetch_metadata(
                self.cache,
                client,
                "`erg`.`telemetry`",
                {"enrollment_id": "e", "destination_generation": "g"},
                100,
            ),
            0,
        )
        sql, params = client.sql.call_args[0]
        self.assertIn("delivery_seq<=:through", sql)
        self.assertIn("IN ('work_evidence','account_evidence')", sql)
        self.assertEqual({x["name"]: x["value"] for x in params}["through"], "100")
        self.assertEqual(
            self.cache.execute("SELECT cursor,metadata_cursor FROM cloud_config").fetchone(),
            (0, 100),
        )
        client.reset_mock()
        cloud.fetch_metadata(self.cache, client, "`erg`.`telemetry`", {}, 100)
        client.sql.assert_not_called()

    def test_caught_up_worker_drains_published_rollup_debt_without_sql_or_idle_sleep(self):
        import threading
        from unittest.mock import Mock, patch

        client = Mock()
        client.sql.side_effect = AssertionError("local rollup debt must not wake SQL")
        cfg = {
            "identity": {
                "enrollment_id": "fixture",
                "installation_id": "install",
                "destination_generation": "generation",
            },
            "cloud": {"namespace": "erg.telemetry", "host": "https://example.invalid"},
        }
        stop = threading.Event()

        def wait(_):
            stop.set()
            return True

        with patch(
            "erg.cloud_history.connection", return_value=(client, "`erg`.`telemetry`", cfg)
        ), patch.object(cloud, "sync_policy", return_value=False), patch.object(
            cloud, "receipt_cursor", return_value=0
        ), patch.object(cloud, "forward_boundary", return_value=100), patch.object(
            project_rollups, "rebuild", return_value={"state": "published"}
        ) as rebuild, patch.object(
            project_rollups, "status", side_effect=[{"pending_hours": 1}, {"pending_hours": 0}]
        ), patch.object(stop, "wait", side_effect=wait) as sleeping:
            cloud.worker(self.root / "rollup-debt.db", stop)
        self.assertEqual(rebuild.call_count, 2)
        sleeping.assert_called_once_with(60)
        client.sql.assert_not_called()
