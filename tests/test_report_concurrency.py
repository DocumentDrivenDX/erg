"""Ready replies do not queue behind report builds; refresh cannot revive old data."""

import json
import threading
import unittest
from unittest.mock import Mock, patch

from erg import server


class ReportConcurrency(unittest.TestCase):
    def data(self, revision=1):
        return dict(
            available=True,
            generated=revision,
            valuation=dict(priced_calls=revision),
            accounts=[],
            subscription_periods=[],
            quota_latest=[],
            quota_counts={},
            sessions=[],
            prs=[],
            commits=[],
            total_calls=revision,
        )

    def run_thread(self, fn):
        done = threading.Event()
        values = []
        errors = []

        def run():
            try:
                values.append(fn())
            except BaseException as error:
                errors.append(error)
            finally:
                done.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, done, values, errors

    def finish(self, task):
        thread, done, values, errors = task
        self.assertTrue(done.wait(5), "request did not finish")
        thread.join(1)
        if errors:
            raise errors[0]
        return values[0]

    def test_cached_accounts_returns_while_another_request_holds_build_lock(self):
        reports = server.Reports()
        holding = threading.Event()
        release = threading.Event()
        with patch.object(server.store, "open_readonly", return_value=Mock()), patch(
            "erg.account_reports.report", return_value=self.data()
        ) as build:
            expected = reports.encoded_effectiveness(view="accounts")

            def other_request():
                with reports.lock:
                    holding.set()
                    self.assertTrue(release.wait(5))

            blocker = self.run_thread(other_request)
            self.assertTrue(holding.wait(3))
            request = self.run_thread(lambda: reports.encoded_effectiveness(view="accounts"))
            try:
                # Event ordering, not a submillisecond timing budget: the other
                # request must still hold the build lock when this one completes.
                self.assertTrue(request[1].wait(3), "cached request waited for build lock")
                self.assertFalse(blocker[1].is_set())
                self.assertIs(self.finish(request), expected)
                self.assertEqual(build.call_count, 1)
            finally:
                release.set()
                self.finish(blocker)
                self.finish(request)

    def test_simultaneous_cold_requests_compute_once(self):
        reports = server.Reports()
        entered = threading.Event()
        release = threading.Event()
        start = threading.Barrier(7)

        def build(db):
            entered.set()
            self.assertTrue(release.wait(5))
            return self.data()

        def request():
            start.wait(5)
            return reports.encoded_effectiveness(view="accounts")

        with patch.object(server.store, "open_readonly", return_value=Mock()), patch(
            "erg.account_reports.report", side_effect=build
        ) as compute:
            tasks = [self.run_thread(request) for _ in range(6)]
            try:
                start.wait(5)
                self.assertTrue(entered.wait(3))
                release.set()
                replies = [self.finish(task) for task in tasks]
                self.assertTrue(all(reply is replies[0] for reply in replies))
                self.assertEqual(compute.call_count, 1)
                self.assertEqual(len(reports.report_replies), 1)
            finally:
                release.set()
                for task in tasks:
                    task[0].join(5)

    def test_refresh_during_build_prevents_old_publication(self):
        reports = server.Reports()
        old_entered = threading.Event()
        old_release = threading.Event()
        refresh_entered = threading.Event()
        refresh_release = threading.Event()
        invalidated = threading.Event()

        def full(db, **kwargs):
            old_entered.set()
            self.assertTrue(old_release.wait(5))
            return self.data(1)

        def account(db):
            refresh_entered.set()
            self.assertTrue(refresh_release.wait(5))
            return self.data(2)

        invalidate = reports.invalidate_report_replies

        def observed_invalidate():
            invalidate()
            invalidated.set()

        with patch.object(server.store, "open_readonly", return_value=Mock()), patch(
            "erg.work.effectiveness", side_effect=full
        ), patch("erg.account_reports.report", side_effect=account), patch.object(
            reports, "invalidate_report_replies", side_effect=observed_invalidate
        ):
            original = self.run_thread(lambda: reports.encoded_effectiveness(view="work"))
            self.assertTrue(old_entered.wait(3))
            refresh = self.run_thread(
                lambda: reports.encoded_effectiveness(refresh=True, view="accounts")
            )
            try:
                self.assertTrue(invalidated.wait(3))
                old_release.set()
                self.assertEqual(json.loads(self.finish(original))["total_calls"], 1)
                self.assertTrue(refresh_entered.wait(3))
                # The original caller can finish, but its old snapshot must not
                # become available to a later caller after refresh invalidation.
                self.assertIsNone(reports.ready_report_reply("work"))
                self.assertEqual(reports.report_replies, {})
                refresh_release.set()
                self.assertEqual(json.loads(self.finish(refresh))["generated"], 2)
                self.assertEqual(json.loads(reports.ready_report_reply("accounts"))["generated"], 2)
                self.assertIsNone(reports.work_cache)
            finally:
                old_release.set()
                refresh_release.set()
                original[0].join(5)
                refresh[0].join(5)

    def test_direct_refresh_invalidates_published_views_and_failures_stay_visible(self):
        reports = server.Reports()
        with patch.object(server.store, "open_readonly", return_value=Mock()), patch(
            "erg.work.effectiveness", return_value=self.data(1)
        ) as compute:
            for view in ("full", "work", "accounts", "capture"):
                reports.encoded_effectiveness(view=view)
            self.assertEqual(len(reports.report_replies), 4)
            compute.return_value = self.data(2)
            reports.effectiveness(refresh=True)
            self.assertEqual(reports.report_replies, {})
            self.assertEqual(json.loads(reports.encoded_effectiveness(view="work"))["generated"], 2)
            compute.side_effect = RuntimeError("failed")
            with self.assertRaises(RuntimeError):
                reports.encoded_effectiveness(refresh=True, view="full")
            self.assertEqual(reports.report_replies, {})
            self.assertIsNone(reports.work_cache)

    def test_native_cached_evidence_is_not_returned_for_other_backend(self):
        reports = server.Reports()
        with patch.object(server.store, "open_readonly", return_value=Mock()), patch(
            "erg.work.effectiveness", return_value=self.data()
        ):
            for view in ("full", "work", "accounts", "capture"):
                reports.encoded_effectiveness(view=view)
            reports.backend = "databricks"
            for view in ("full", "work", "accounts", "capture"):
                data = json.loads(reports.encoded_effectiveness(view=view))
                self.assertFalse(data["available"])
                self.assertNotIn("valuation", data)

    def test_http_cached_accounts_completes_during_cold_work_build(self):
        import contextlib
        import io
        import urllib.request

        reports = server.Reports()
        created = []
        ready = threading.Event()
        full_entered = threading.Event()
        full_release = threading.Event()
        real = server.http.server.ThreadingHTTPServer

        def listener(address, handler):
            result = real(address, handler)
            created.append(result)
            ready.set()
            return result

        def build(db, **kwargs):
            full_entered.set()
            self.assertTrue(full_release.wait(5))
            return self.data(2)

        with patch.object(server, "Reports", return_value=reports), patch.object(
            server.http.server, "ThreadingHTTPServer", side_effect=listener
        ), patch.object(server.store, "open_readonly", return_value=Mock()), patch(
            "erg.account_reports.report", return_value=self.data(1)
        ), patch("erg.work.effectiveness", side_effect=build), contextlib.redirect_stdout(
            io.StringIO()
        ):
            runner = threading.Thread(target=server.serve, args=(0,), daemon=True)
            runner.start()
            tasks = []
            try:
                self.assertTrue(ready.wait(3))
                base = "http://127.0.0.1:" + str(created[0].server_port)

                def get(view):
                    with urllib.request.urlopen(
                        base + "/api/effectiveness?view=" + view, timeout=5
                    ) as reply:
                        return reply.read()

                expected = get("accounts")
                full = self.run_thread(lambda: get("work"))
                tasks.append(full)
                self.assertTrue(full_entered.wait(3))
                cached = self.run_thread(lambda: get("accounts"))
                tasks.append(cached)
                self.assertTrue(cached[1].wait(3), "HTTP cache queued behind full report")
                self.assertEqual(self.finish(cached), expected)
                self.assertFalse(full[1].is_set())
                full_release.set()
                self.assertEqual(json.loads(self.finish(full))["generated"], 2)
            finally:
                full_release.set()
                for task in tasks:
                    task[0].join(5)
                if created:
                    created[0].shutdown()
                runner.join(3)
