"""Accounts-only computation preserves the full report's billing evidence rules."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from erg import account_reports, accounting, report_views, routes, store, work


class AccountReports(unittest.TestCase):
    def test_full_report_parity_and_no_work_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            db = store.connect(str(Path(directory) / "ledger"))
            self.addCleanup(db.close)

            def ingest(*records, machine="host"):
                store.ingest(db, map(json.dumps, records), machine=machine)

            def call(uid, session="s", ts=100, model="gpt-6.1-sol"):
                return dict(
                    t="llm",
                    uid=uid,
                    tool="codex",
                    session=session,
                    ts=ts,
                    model=model,
                    fresh_in=10,
                    cache_read=20,
                    cache_write=0,
                    out=5,
                    reasoning=2,
                )

            fields = accounting.identity_fields(
                dict(account_id="account", email="fixture@example.invalid"), "openai"
            )
            owner = dict(
                t="identity",
                uid="owner",
                ts=1,
                tool="codex",
                session="owner",
                provider="openai",
                provenance="codex_creator_account",
                scope="session_owner",
                **fields,
            )
            native = dict(owner, uid="native", session="s", scope="", provenance="native_metadata")
            ingest(
                owner,
                native,
                call("one"),
                call("unpriced", model="unknown"),
                call("owner-call", session="owner"),
                call("unassigned", session="unknown"),
            )
            ingest(call("one"), machine="mirror")

            def compare():
                with patch("erg.account_reports.time.time", return_value=12345):
                    full = work.effectiveness(db)
                    full["available"] = True
                    expected = report_views.project(full, "accounts")
                    queries = []
                    db.set_trace_callback(queries.append)
                    try:
                        with patch(
                            "erg.session_usage.add_call", side_effect=AssertionError("session fold")
                        ), patch(
                            "erg.work_intervals.resolver", side_effect=AssertionError("work fold")
                        ):
                            actual = account_reports.report(db)
                    finally:
                        db.set_trace_callback(None)
                    actual["available"] = True
                    self.assertEqual(report_views.project(actual, "accounts"), expected)
                    sql = " ".join(queries).lower()
                    for table in (
                        "work_contexts",
                        "work_evidence",
                        "work_commit_evidence",
                        "pull_requests",
                        "input_messages",
                        "session_routes",
                        "report_sessions",
                        "session_cost_events",
                    ):
                        self.assertNotIn(table, sql)
                    return actual

            self.assertEqual(compare()["valuation"]["priced_calls"], 3)
            charge = dict(
                charge_id="invoice",
                provider="openai",
                account_id="account",
                subscription_id="sub",
                period_start=1,
                period_end=200,
                usd=20,
            )
            accounting.import_metadata(db, dict(version=1, charges=[charge]), "host")
            r = compare()
            self.assertEqual(r["subscription_periods"][0]["calls"], 2)
            # Mirrored charges, ambiguous overlaps, unknown FX and contradictory
            # invoice revisions must retain exactly the existing semantics.
            event = accounting.rows(db, "subscription_charge_events")[0]
            event.pop("machine")
            event["t"] = "subscription_charge"
            ingest(event, machine="mirror")
            self.assertEqual(compare()["subscription_periods"][0]["subscription_spend_usd"], 20)
            accounting.import_metadata(
                db,
                dict(
                    version=1, charges=[dict(charge, charge_id="other", subscription_id="second")]
                ),
                "host",
            )
            self.assertTrue(all(p["calls"] == 0 for p in compare()["subscription_periods"]))
            accounting.import_metadata(
                db,
                dict(
                    version=1,
                    charges=[
                        dict(
                            charge,
                            charge_id="euro",
                            subscription_id="euro",
                            usd=None,
                            amount=30,
                            currency="EUR",
                        )
                    ],
                ),
                "host",
            )
            self.assertTrue(
                any(
                    p["currency_amounts"] == {"EUR": 30} and p["subscription_spend_usd"] is None
                    for p in compare()["subscription_periods"]
                )
            )
            event["usd"] = 99
            ingest(event, machine="conflicting-mirror")
            self.assertTrue(
                any(
                    p["subscription_spend_usd"] is None
                    for p in compare()["subscription_periods"]
                    if p["subscription_key"] == event["subscription_key"]
                )
            )

    def test_route_payer_matches_shared_account_reporting(self):
        with tempfile.TemporaryDirectory() as directory:
            db = store.connect(str(Path(directory) / "ledger"))
            self.addCleanup(db.close)
            payer = accounting.identity_fields(dict(account_id="payer"), "openai")
            records = [
                dict(
                    t="identity",
                    uid="payer",
                    ts=1,
                    tool="codex",
                    session="",
                    provider="openai",
                    provenance="codex_account_read",
                    **payer,
                ),
                routes.event(
                    "codex",
                    "s",
                    2,
                    dict(
                        provider="openai",
                        account_provider="openai",
                        account_id="payer",
                        request_uid="paid",
                    ),
                    "native",
                ),
                dict(
                    t="llm",
                    uid="paid",
                    tool="codex",
                    session="s",
                    ts=100,
                    model="gpt-6.1-sol",
                    fresh_in=10,
                    out=5,
                ),
                dict(
                    t="llm",
                    uid="other",
                    tool="codex",
                    session="s",
                    ts=100,
                    model="gpt-6.1-sol",
                    fresh_in=10,
                    out=5,
                ),
            ]
            store.ingest(db, map(json.dumps, records), machine="host")
            accounting.import_metadata(
                db,
                dict(
                    version=1,
                    charges=[
                        dict(
                            charge_id="invoice",
                            provider="openai",
                            account_id="payer",
                            subscription_id="sub",
                            period_start=1,
                            period_end=200,
                            usd=20,
                        )
                    ],
                ),
                "host",
            )
            self.assertEqual(account_reports.report(db)["subscription_periods"][0]["calls"], 1)
            self.assertEqual(work.effectiveness(db)["subscription_periods"][0]["calls"], 1)
