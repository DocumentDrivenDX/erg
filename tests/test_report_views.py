"""Wire views preserve visible metrics without shipping unrelated history."""

import copy
import json
import unittest
from unittest.mock import Mock, patch

from erg import report_views, server


class ReportViews(unittest.TestCase):
    def data(self):
        return dict(
            available=True,
            generated=100,
            cost_provenance="unknown",
            sessions=[dict(tool="codex", session="s", calls=3, actual_billed_usd=None)],
            accounts=[
                dict(
                    account_key="a",
                    ts=1,
                    provider="provider",
                    plan="pro",
                    email_hint="hint",
                    provenance="native",
                )
            ],
            prs=[
                dict(
                    url="https://example.invalid/r/pull/1",
                    total_tokens=12,
                    human_inputs=None,
                    cost_usd=None,
                )
            ],
            commits=[dict(sha="abc", actual_billed_usd=None)],
            total_calls=3,
            attributed_calls=1,
            unattributed_calls=2,
            valuation=dict(known_value_low_usd=1, known_value_high_usd=2, actual_billed_usd=None),
            subscription_periods=[dict(subscription_key="period", subscription_spend_usd=None)],
            quota_latest=[dict(used_percent=None)],
            quota_counts=dict(reset_observed=2),
        )

    def test_views_preserve_their_metrics_and_full_endpoint_without_mutation(self):
        full = self.data()
        original = copy.deepcopy(full)
        self.assertIs(report_views.project(full, "full"), full)
        accounts = report_views.project(full, "accounts")
        work = report_views.project(full, "work")
        capture = report_views.project(full, "capture")
        for key in ("valuation", "subscription_periods", "quota_latest", "quota_counts"):
            self.assertEqual(accounts[key], full[key])
        for key in ("prs", "commits", "total_calls", "attributed_calls", "unattributed_calls"):
            self.assertEqual(work[key], full[key])
        self.assertEqual(capture["sessions"], full["sessions"])
        self.assertEqual(capture["subscription_period_count"], 1)
        self.assertFalse({"accounts", "sessions", "prs", "commits"} & accounts.keys())
        self.assertFalse({"accounts", "sessions", "quota_latest"} & work.keys())
        self.assertFalse({"accounts", "prs", "commits"} & capture.keys())
        self.assertEqual(full, original)

    def test_registry_keeps_latest_nonempty_fields_and_their_actual_sources(self):
        records = [
            dict(
                account_key="a",
                provider="one",
                ts=10,
                plan="pro",
                email_hint="first",
                provenance="first",
                subscription_key="old-subscription",
            ),
            dict(
                account_key="a", provider="one", ts=30, plan="", email_hint="", provenance="latest"
            ),
            dict(
                account_key="a",
                provider="one",
                ts=20,
                plan="team",
                email_hint="second",
                provenance="middle",
            ),
            dict(
                account_key="a",
                provider="one",
                ts=20,
                plan="conflicting-tie",
                email_hint="tie",
                provenance="tie",
            ),
            dict(
                account_key="b",
                provider="two",
                ts=15,
                plan="",
                email_hint="second",
                provenance="other",
            ),
        ]
        before = copy.deepcopy(records)
        registry = report_views.account_registry(records)
        self.assertEqual(len(registry), 2)
        a = registry[0]
        self.assertEqual((a["plan"], a["plan_ts"], a["plan_source"]), ("team", 20, "middle"))
        self.assertEqual(
            (a["email_hint"], a["email_hint_ts"], a["email_hint_source"]), ("second", 20, "middle")
        )
        self.assertEqual((a["ts"], a["provenance"], a["identity_record_count"]), (30, "latest", 4))
        self.assertNotIn(
            "subscription_key", a
        )  # Registry hints cannot assign a billing subscription.
        self.assertEqual(records, before)

    def test_wire_views_reuse_full_snapshot_and_accounts_refresh_invalidates_it(self):
        reports = server.Reports()
        data = self.data()
        with patch.object(server.store, "open_readonly", return_value=Mock()), patch(
            "erg.work.effectiveness", return_value=data
        ) as build, patch("erg.account_reports.report", return_value=data) as accounts:
            reports.encoded_effectiveness()  # A fresh full report supplies every view.
            old = reports.encoded_effectiveness(view="work")
            self.assertIs(old, reports.encoded_effectiveness(view="work"))
            reports.encoded_effectiveness(view="accounts")
            reports.encoded_effectiveness(view="capture")
            self.assertEqual(build.call_count, 1)
            accounts.assert_not_called()
            self.assertEqual(len(reports.wire_work_views), 3)
            data["total_calls"] = 4
            reports.encoded_effectiveness(refresh=True, view="accounts")
            self.assertEqual(reports.wire_work_views, {})
            self.assertIsNone(reports.work_cache)
            self.assertEqual(accounts.call_count, 1)
            self.assertEqual(
                json.loads(reports.encoded_effectiveness(view="work"))["total_calls"], 4
            )
            self.assertEqual(build.call_count, 2)
            self.assertIsNotNone(reports.accounts_cache)
            with self.assertRaises(ValueError):
                reports.encoded_effectiveness(view="unbounded-private-view")
            self.assertEqual(build.call_count, 2)
        self.assertEqual(server.Reports().wire_work_views, {})

    def test_cold_accounts_cache_avoids_work_and_expires_without_hiding_failures(self):
        reports = server.Reports()
        data = self.data()
        with patch.object(server.store, "open_readonly", return_value=Mock()), patch(
            "erg.work.effectiveness"
        ) as full, patch("erg.account_reports.report", return_value=data) as build, patch(
            "erg.server.time.monotonic", return_value=100
        ) as clock:
            first = reports.encoded_effectiveness(view="accounts")
            self.assertIs(first, reports.encoded_effectiveness(view="accounts"))
            self.assertEqual(build.call_count, 1)
            full.assert_not_called()
            clock.return_value = 999
            self.assertIs(first, reports.encoded_effectiveness(view="accounts"))
            clock.return_value = 1000
            reports.encoded_effectiveness(view="accounts")
            self.assertEqual(build.call_count, 2)
            build.side_effect = RuntimeError("failed")
            with self.assertRaises(RuntimeError):
                reports.encoded_effectiveness(refresh=True, view="accounts")
            self.assertIsNone(reports.accounts_cache)
            self.assertIsNone(reports.work_cache)
            full.assert_not_called()

    def test_unavailable_backend_does_not_expose_cached_native_evidence(self):
        reports = server.Reports(backend="sqlite")
        reports.backend = "databricks"
        for view in ("work", "accounts", "capture"):
            value = json.loads(reports.encoded_effectiveness(view=view))
            self.assertFalse(value["available"])
            self.assertNotIn("sessions", value)
        self.assertEqual(reports.wire_work_views, {})

    def test_http_views_share_snapshot_and_keep_validation_separate_from_backend_failure(self):
        import contextlib
        import io
        import threading
        import urllib.error
        import urllib.request

        created = []
        ready = threading.Event()
        real = server.http.server.ThreadingHTTPServer
        reports = server.Reports()
        data = self.data()

        def make(address, handler):
            listener = real(address, handler)
            created.append(listener)
            ready.set()
            return listener

        with patch.object(server, "Reports", return_value=reports), patch.object(
            server.http.server, "ThreadingHTTPServer", side_effect=make
        ), patch.object(server.store, "open_readonly", return_value=Mock()), patch(
            "erg.work.effectiveness", return_value=data
        ) as build, patch(
            "erg.account_reports.report", return_value=data
        ) as account_build, contextlib.redirect_stdout(io.StringIO()):
            thread = threading.Thread(target=server.serve, args=(0,), daemon=True)
            thread.start()
            try:
                self.assertTrue(ready.wait(3))
                base = "http://127.0.0.1:" + str(created[0].server_port)

                def get(path):
                    with urllib.request.urlopen(base + path, timeout=5) as response:
                        return json.load(response)

                with self.assertRaises(urllib.error.HTTPError) as error:
                    get("/api/effectiveness?view=private-unknown")
                self.assertEqual(error.exception.code, 400)
                build.assert_not_called()
                account = get("/api/effectiveness?view=accounts")
                self.assertNotIn("sessions", account)
                self.assertEqual(get("/api/effectiveness?view=work")["prs"], data["prs"])
                self.assertEqual(
                    get("/api/effectiveness?view=capture")["sessions"], data["sessions"]
                )
                self.assertEqual(get("/api/effectiveness")["accounts"], data["accounts"])
                self.assertEqual(build.call_count, 2)
                account_build.side_effect = ValueError("PRIVATE BACKEND DETAIL")
                with self.assertRaises(urllib.error.HTTPError) as error:
                    get("/api/effectiveness?view=accounts&refresh=1")
                self.assertEqual(error.exception.code, 503)
                self.assertNotIn("PRIVATE", error.exception.read().decode())
            finally:
                if created:
                    created[0].shutdown()
                thread.join(3)

    def test_cold_work_uses_focused_cache_and_refresh_does_not_restore_old_result(self):
        reports = server.Reports()
        data = self.data()
        with patch.object(server.store, "open_readonly", return_value=Mock()), patch(
            "erg.work.effectiveness", return_value=data
        ) as build, patch("erg.account_reports.report") as accounts, patch(
            "erg.server.time.monotonic", return_value=100
        ) as clock:
            first = reports.encoded_effectiveness(view="work")
            self.assertIs(first, reports.encoded_effectiveness(view="work"))
            self.assertEqual(build.call_args.kwargs, dict(view="work"))
            accounts.assert_not_called()
            self.assertIsNone(reports.work_cache)
            self.assertIsNotNone(reports.work_view_cache)
            clock.return_value = 1000
            reports.encoded_effectiveness(view="work")
            self.assertEqual(build.call_count, 2)
            build.side_effect = RuntimeError("failed")
            with self.assertRaises(RuntimeError):
                reports.encoded_effectiveness(refresh=True, view="work")
            self.assertIsNone(reports.work_view_cache)
            self.assertEqual(reports.report_replies, {})
