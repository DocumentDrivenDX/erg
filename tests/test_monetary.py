import datetime
import json
import unittest
from unittest.mock import patch

from erg import (
    account_reports,
    accounting,
    monetary,
    rate_catalog,
    rates,
    report_scope,
    server,
    store,
)
from tests import test_project_rollups as fixtures


class Monetary(unittest.TestCase):
    setUp = fixtures.DurableProjectRollups.setUp
    add = fixtures.DurableProjectRollups.add
    canonical = fixtures.DurableProjectRollups.canonical
    build = fixtures.DurableProjectRollups.build

    def identity(self):
        fields = accounting.identity_fields({"account_id": "a"}, "fixture")
        records = [
            dict(
                t="identity",
                uid="id" + s,
                ts=1,
                tool="pi",
                session=s,
                provider="fixture",
                provenance="native_metadata",
                **fields,
            )
            for s in ("s", "other")
        ]
        store.ingest(self.db, map(json.dumps, records), machine="host")
        return fields["account_key"]

    def charges(self, usd=30):
        charge = dict(
            provider="fixture",
            account_id="a",
            subscription_id="sub",
            period_start=1,
            period_end=20000,
        )
        data = dict(
            version=1,
            charges=[
                dict(charge, charge_id="invoice", usd=usd),
                dict(
                    charge, charge_id="reset", usd=10, charge_kind="purchased_reset", reset_at=7000
                ),
            ],
        )
        accounting.import_metadata(self.db, data, "host")
        accounting.import_metadata(self.db, data, "host")

    def test_filtered_session_and_split_projects_never_receive_entire_subscription(self):
        self.identity()
        self.charges()
        self.add(fresh_in=100, cache_read=0, cache_write=0, out=0)
        self.add(uid="two", session="other", fresh_in=300, cache_read=0, cache_write=0, out=0)
        self.build()
        all_rows = monetary.report(self.db, report_scope.Selection({"end": 20000}))
        self.assertAlmostEqual(all_rows["totals"]["effective_cost_usd"], 40)
        selected = monetary.report(self.db, report_scope.Selection({"end": 20000, "session": "s"}))
        self.assertAlmostEqual(selected["totals"]["effective_cost_usd"], 10)
        a = monetary.report(self.db, report_scope.Selection({"end": 20000, "project_key": "a"}))
        self.assertAlmostEqual(a["totals"]["effective_cost_usd"], 24)
        self.assertEqual(all_rows["subscription_periods"][0]["purchased_reset_usd"], 10)
        self.assertEqual(
            self.db.execute("select count(*) from subscription_charge_events").fetchone()[0], 2
        )
        reports = server.Reports(database=self.path)
        expected = reports.costs(end=20000)["totals"]["effective_cost_usd"]
        for view in ("work", "accounts"):
            actual = json.loads(reports.encoded_scoped_work({"end": 20000}, view=view))
            self.assertAlmostEqual(actual["monetary"]["totals"]["effective_cost_usd"], expected)

    def test_missing_charge_has_actionable_reason_and_is_never_zero(self):
        self.identity()
        self.add()
        self.build()
        result = monetary.report(self.db, report_scope.Selection({"end": 20000}))["totals"]
        self.assertIsNone(result["effective_cost_usd"])
        self.assertIn("subscription_charge_missing_or_ambiguous", result["missing_reasons"])

    def test_plan_price_accrues_from_observation_without_fabricating_charge(self):
        key = self.identity()
        accounting.import_metadata(
            self.db,
            dict(
                version=1,
                identities=[
                    dict(
                        provider="fixture",
                        account_key=key,
                        monthly_usd=500,
                        plan="Dynamic plan",
                        observed_at=1,
                    )
                ],
            ),
            "host",
        )
        self.add()
        self.build()
        with patch("erg.monetary.time.time", return_value=8000):
            result = monetary.report(self.db, report_scope.Selection({"end": 20000}))
        self.assertAlmostEqual(result["totals"]["effective_cost_usd"], 500 * 7999 / (31 * 86400))
        self.assertIsNone(result["subscription_periods"][0]["subscription_spend_usd"])
        self.assertEqual(
            self.db.execute("select count(*) from subscription_charge_events").fetchone()[0], 0
        )
        self.charges()
        self.build()
        result = monetary.report(self.db, report_scope.Selection({"end": 20000}))
        self.assertAlmostEqual(result["totals"]["effective_cost_usd"], 40)

    def test_receipt_validation_rejects_negative_price_and_unobserved_account(self):
        self.identity()
        for charge in [
            dict(
                provider="fixture",
                account_id="a",
                subscription_id="sub",
                period_start=1,
                period_end=20000,
                charge_id="bad",
                usd=-1,
            ),
            dict(
                provider="fixture",
                account_key="f" * 64,
                subscription_id="sub",
                period_start=1,
                period_end=20000,
                charge_id="bad",
                usd=1,
            ),
        ]:
            with self.assertRaises(ValueError):
                accounting.import_metadata(self.db, dict(version=1, charges=[charge]), "host")
        self.assertEqual(
            self.db.execute("select count(*) from subscription_charge_events").fetchone()[0], 0
        )

    def test_parent_model_range_is_machine_and_parent_scoped(self):
        self.add(model="low", session="parent")
        self.add(uid="two", model="high", session="parent")
        self.canonical()
        store.ingest(
            self.db,
            [
                json.dumps(
                    dict(
                        t="session",
                        uid="child",
                        ts=1,
                        tool="pi",
                        session="child",
                        parent_session="parent",
                    )
                )
            ],
            machine="host",
        )
        card = dict(
            reviewer_valuation="observed_parent_model_range.v1",
            entries=[
                dict(model="low", input=1, output=2, as_of="2026", source="fixture"),
                dict(model="high", input=3, output=4, as_of="2026", source="fixture"),
            ],
            _db=self.db,
        )
        self.add(uid="review", session="child", model="codex-auto-review")
        self.canonical()
        call = dict(
            machine="host",
            tool="pi",
            session="child",
            model="codex-auto-review",
            fresh_in=1000000,
            out=1000000,
        )
        v = rates.value(call, card)
        self.assertEqual((v["low_usd"], v["high_usd"]), (3, 7))
        self.assertEqual(v["basis"], "observed_parent_model_range")
        v = rates.value(dict(call, machine="other"), card)
        self.assertEqual(v["comparison_scope"], "observed_parent_model_pool_parent_unavailable")
        self.assertEqual((v["low_usd"], v["high_usd"]), (3, 7))

    def test_owner_only_usage_requires_explicit_payer_binding(self):
        fields = accounting.identity_fields({"account_id": "a"}, "fixture")
        store.ingest(
            self.db,
            [
                json.dumps(
                    dict(
                        t="identity",
                        uid="owner",
                        ts=1,
                        tool="pi",
                        session="s",
                        provider="fixture",
                        provenance="codex_creator_account",
                        scope="session_owner",
                        **fields,
                    )
                )
            ],
            machine="host",
        )
        self.charges()
        self.add()
        self.build()
        self.assertIsNone(
            monetary.report(self.db, report_scope.Selection({"end": 20000}))["totals"][
                "effective_cost_usd"
            ]
        )
        accounting.import_metadata(
            self.db,
            dict(
                version=1,
                bindings=[
                    dict(
                        provider="fixture",
                        account_id="a",
                        tool="pi",
                        session="s",
                        subscription_id="sub",
                        start_ts=1,
                        end_ts=20000,
                    )
                ],
            ),
            "host",
        )
        self.build()
        self.assertAlmostEqual(
            monetary.report(self.db, report_scope.Selection({"end": 20000}))["totals"][
                "effective_cost_usd"
            ],
            40,
        )

    def test_proration_uses_actual_calendar_month_lengths(self):
        def ts(s):
            return (
                datetime.datetime.fromisoformat(s).replace(tzinfo=datetime.timezone.utc).timestamp()
            )

        self.assertEqual(account_reports.accrued(500, ts("2026-02-01"), ts("2026-03-01")), 500)
        self.assertEqual(account_reports.accrued(500, ts("2026-02-01"), ts("2026-04-01")), 1000)


class RateCatalog(unittest.TestCase):
    def test_immutable_manifest_requires_matching_complete_rows(self):
        from unittest.mock import Mock

        card = dict(
            aliases={},
            entries=[dict(model="m", input=1, output=2, as_of="2026-10-26", source="fixture")],
        )
        rev = rate_catalog.revision(card)
        client = Mock()
        client.sql.side_effect = [[[rev, json.dumps(card)]], []]
        with self.assertRaisesRegex(ValueError, "incomplete"):
            rate_catalog.pull(client, "erg.telemetry")
        client.sql.side_effect = [[["wrong", json.dumps(card)]]]
        with self.assertRaisesRegex(ValueError, "digest"):
            rate_catalog.pull(client, "erg.telemetry")
        with self.assertRaises(ValueError):
            rate_catalog.validate(dict(entries=[dict(card["entries"][0], input=-1)]))

    def test_typed_row_edits_cannot_silently_disagree_with_offline_snapshot(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import Mock

        card = dict(
            aliases={},
            entries=[
                dict(
                    model="m",
                    channel="fixture",
                    input=1,
                    output=2,
                    as_of="2026-10-26",
                    source="fixture",
                )
            ],
        )
        rev = rate_catalog.revision(card)
        entry = card["entries"][0]
        row = [0, "m", "fixture", "2026-10-26", 1, 2, json.dumps(entry)]
        client = Mock()
        client.sql.side_effect = [[[rev, json.dumps(card)]], [row]]
        with tempfile.TemporaryDirectory() as temp, patch.object(
            rate_catalog, "directory", return_value=Path(temp)
        ):
            self.assertEqual(rate_catalog.pull(client, "erg.telemetry")["revision"], rev)
            before = (Path(temp) / "current.json").read_bytes()
            changed = list(row)
            changed[4] = 999
            client.sql.side_effect = [[[rev, json.dumps(card)]], [changed]]
            with self.assertRaisesRegex(ValueError, "differs"):
                rate_catalog.pull(client, "erg.telemetry")
            self.assertEqual((Path(temp) / "current.json").read_bytes(), before)
            self.assertEqual((Path(temp) / "current.json").stat().st_mode & 0o777, 0o600)
