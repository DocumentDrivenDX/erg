import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from erg import accounting, collect, rates, store, work


class Accounting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.db = store.connect(str(self.path / "db"))
        self.addCleanup(self.db.close)

    def ingest(self, *rows):
        store.ingest(self.db, map(json.dumps, rows), machine="host")

    def call(self, **kw):
        return dict(
            dict(
                t="llm",
                uid="call",
                ts=100,
                tool="codex",
                session="s",
                project="p",
                model="gpt-6.1-sol",
                fresh_in=10,
                cache_read=20,
                cache_write=0,
                out=5,
                reasoning=2,
            ),
            **kw,
        )

    def identity(self, ts=1, account="a", **kw):
        fields = accounting.identity_fields(
            dict(account_id=account, email="same@example.com"), "openai"
        )
        return dict(
            dict(
                t="identity",
                uid="id" + account + str(ts),
                ts=ts,
                tool="codex",
                session="s",
                provider="openai",
                provenance="native_metadata",
                **fields,
            ),
            **kw,
        )

    def test_session_token_totals_and_peak_request_are_different_and_not_truncated(self):
        self.ingest(self.call(), self.call(uid="second", fresh_in=30, cache_read=50, out=10))
        report = work.effectiveness(self.db)
        s = report["sessions"][0]
        self.assertEqual(
            (
                s["total_tokens"],
                s["largest_request_input_tokens"],
                s["largest_request_total_tokens"],
            ),
            (125, 80, 90),
        )
        self.assertEqual(s["reasoning"], 4)
        self.assertIsNone(s["actual_billed_usd"])
        self.assertEqual(s["valuation"]["priced_calls"], 2)

    def test_rate_valuation_has_explicit_asof_and_long_context_tier(self):
        card = rates.load()
        base = self.call(fresh_in=300000, cache_read=0, out=100000)
        v = rates.value(base, card)
        self.assertAlmostEqual(v["usd"], 2.7)
        self.assertEqual(v["basis"], "estimated_standard_api_equivalent")
        self.assertIsNone(rates.value(self.call(model="codex-auto-review"), card))

    def test_cache_write_ttl_unknown_is_range_not_invented_exact_cost(self):
        card = rates.load()
        call = self.call(
            model="claude-opus-5-5", fresh_in=0, cache_read=0, cache_write=1000000, out=0
        )
        v = rates.value(call, card)
        self.assertEqual((v["low_usd"], v["high_usd"], v["usd"]), (5, 8, None))
        call.update(cache_write_5m=1000000, cache_write_1h=0)
        self.assertEqual(rates.value(call, card)["usd"], 5)

    def test_compiled_rates_preserve_strict_boundaries_aliases_and_authored_tier_order(self):
        card = dict(
            aliases={"alias": "model"},
            entries=[
                dict(model="model", input=100, output=100, as_of="2025-01-01", source="old"),
                dict(
                    model="model",
                    input=1,
                    output=2,
                    cache_read=0.1,
                    cache_write_5m=3,
                    cache_write_1h=5,
                    as_of="2026-01-01",
                    source="current",
                    tiers=[
                        dict(min_prompt_tokens=20, input=7, cache_write=11),
                        dict(min_prompt_tokens=10, input=4, output=6),
                        dict(min_prompt_tokens=20, output=9),
                    ],
                ),
            ],
        )
        # Reversed calls and fractional boundaries prevent a prior call's tier from
        # leaking into another call. At >20, the later >10 input rule still wins.
        for tokens, input_rate, output_rate in [
            (21, 4, 9),
            (10, 1, 2),
            (20, 4, 6),
            (10.5, 4, 6),
            (0, 1, 2),
            (20.5, 4, 9),
        ]:
            value = rates.value(
                self.call(model="alias", fresh_in=tokens, cache_read=0, out=2), card
            )
            self.assertAlmostEqual(value["usd"], (tokens * input_rate + 2 * output_rate) / 1e6)
            self.assertEqual((value["as_of"], value["source"]), ("2026-01-01", "current"))
        self.assertIsNone(rates.value(self.call(model="absent"), card))
        self.assertIsNone(rates.value(self.call(model="alias", fresh_in=-1, cache_read=0), card))

    def test_compiled_tiers_keep_per_call_cache_ttl_and_missing_rates_unknown(self):
        card = dict(
            aliases={},
            entries=[
                dict(
                    model="m",
                    input=1,
                    output=2,
                    cache_write_5m=3,
                    cache_write_1h=5,
                    as_of="2026",
                    source="fixture",
                    tiers=[dict(min_prompt_tokens=20, cache_write=7)],
                )
            ],
        )
        call = self.call(model="m", fresh_in=0, cache_read=0, cache_write=20, out=0)
        self.assertEqual(
            (rates.value(call, card)["low_usd"], rates.value(call, card)["high_usd"]),
            (60 / 1e6, 100 / 1e6),
        )
        call.update(cache_write_5m=20, cache_write_1h=0)
        self.assertEqual(rates.value(call, card)["usd"], 60 / 1e6)
        call.update(cache_write=21, cache_write_5m=21)
        self.assertEqual(rates.value(call, card)["usd"], 147 / 1e6)
        call.update(cache_read=1)
        self.assertIsNone(rates.value(call, card))
        other = dict(
            aliases={}, entries=[dict(model="m", input=9, output=2, as_of="2026", source="other")]
        )
        self.assertEqual(
            rates.value(self.call(model="m", fresh_in=1, cache_read=0, out=0), other)["usd"],
            9 / 1e6,
        )

    def test_unknown_prices_do_not_become_zero_cost(self):
        self.ingest(self.call(model="unknown"))
        v = work.effectiveness(self.db)["valuation"]
        self.assertEqual(v["coverage"], "unknown")
        self.assertIsNone(v["api_equivalent_usd"])
        self.assertEqual(v["unpriced_calls"], 1)

    def test_email_does_not_merge_two_distinct_account_ids(self):
        self.ingest(self.identity(account="a"), self.identity(account="b", ts=2))
        resolve, root, _ = accounting.resolver(self.db)
        self.assertNotEqual(
            root(accounting.identity_fields(dict(account_id="a"), "openai")["account_key"]),
            root(accounting.identity_fields(dict(account_id="b"), "openai")["account_key"]),
        )
        self.assertEqual(resolve("codex", "s", 1)["account_key"], self.identity()["account_key"])
        self.assertEqual(
            resolve("codex", "s", 3)["account_key"], self.identity(account="b")["account_key"]
        )

    def test_account_interval_cache_preserves_owner_fallback_and_conflicting_boundaries(self):
        owner = self.identity(
            ts=100, account="owner", scope="session_owner", provenance="codex_creator_account"
        )
        self.ingest(
            owner,
            self.identity(ts=10, account="first"),
            self.identity(ts=20, account="second"),
            self.identity(ts=20, account="conflict"),
        )
        resolve, _, _ = accounting.resolver(self.db)
        for ts, expected in [
            (25, None),
            (19, "first"),
            (20, None),
            (9.5, "owner"),
            (10, "first"),
            (20.5, None),
            (1, "owner"),
        ]:
            value = resolve("codex", "s", ts)
            self.assertEqual(
                value["account_key"] if value else None,
                self.identity(account=expected)["account_key"] if expected else None,
            )
            if expected == "owner":
                self.assertFalse(value["billing_evidence"])
        self.assertGreater(resolve.cache_info().hits, 0)

    def test_account_interval_cache_respects_binding_edges_and_installation_scope(self):
        self.ingest(
            self.identity(
                account="owner", scope="session_owner", provenance="codex_creator_account"
            )
        )
        base = dict(
            t="account_binding",
            ts=1,
            tool="codex",
            provider="openai",
            provenance="operator",
            subscription_key="subscription",
        )
        for uid, session, installation, start, end, account in [
            ("session", "s", "", 5, 10, "bound"),
            ("overlap", "s", "", 8, 12, "conflict"),
            ("installation", "", "other", 0, 0, "installed"),
        ]:
            self.ingest(
                dict(
                    base,
                    uid=uid,
                    session=session,
                    installation=installation,
                    start_ts=start,
                    end_ts=end,
                    account_key=account,
                )
            )
        resolve, _, _ = accounting.resolver(self.db)
        for ts, expected in [
            (10, "conflict"),
            (4, "owner"),
            (5, "bound"),
            (8, None),
            (7.5, "bound"),
            (12, "owner"),
            (9.9, None),
        ]:
            value = resolve("codex", "s", ts)
            key = self.identity(account="owner")["account_key"] if expected == "owner" else expected
            self.assertEqual(value["account_key"] if value else None, key)
        self.assertIsNone(resolve("codex", "unmatched", 6))
        self.assertEqual(resolve("codex", "unmatched", 6, "other")["account_key"], "installed")
        self.assertIsNone(resolve("codex", "unmatched", 6, "different"))
        value = resolve("codex", "s", 6)
        value.update(account_key="corrupted", billing_evidence=False)
        self.assertEqual(resolve("codex", "s", 7)["account_key"], "bound")
        self.assertTrue(resolve("codex", "s", 7)["billing_evidence"])

    def test_account_interval_cache_is_bounded_for_unmatched_sessions(self):
        resolve, _, _ = accounting.resolver(self.db)
        for i in range(4200):
            self.assertIsNone(resolve("codex", "unmatched-" + str(i), 1))
        self.assertLessEqual(resolve.cache_info().currsize, 4096)

    def test_native_quota_changes_resets_and_limit_hits_are_durable_without_identity(self):
        state = dict(session="s", started=1)

        def event(ts, used, reset):
            return accounting.observe(
                "codex",
                {},
                dict(
                    rate_limits=dict(
                        plan_type="pro",
                        limit_id="codex",
                        primary=dict(used_percent=used, window_minutes=300, resets_at=reset),
                    )
                ),
                state,
                ts,
                "/profiles/user/sessions/log.jsonl",
            )

        first = event(1, 90, 100)
        self.ingest(*first)
        self.assertEqual(event(2, 90, 100), [])
        self.ingest(*event(3, 100, 100), *event(4, 2, 200))
        rows = accounting.rows(self.db, "quota_events")
        self.assertEqual(
            {r["event"] for r in rows}, {"snapshot", "window_exhausted", "reset_observed"}
        )
        self.assertTrue(all(not r["account_key"] for r in rows))
        self.assertEqual(len(rows), 3)

    def test_identity_capture_hashes_email_and_rejects_secret_fields(self):
        state = dict(session="s")
        rows = accounting.observe(
            "codex",
            {},
            dict(
                provider="openai",
                account=dict(account_id="acct", email="person@example.com", access_token="SECRET"),
                rate_limits=dict(plan_type="new-plan"),
            ),
            state,
            1,
            "/profile/sessions/a",
        )
        self.ingest(*rows)
        payload = str(self.db.execute("select payload from observations").fetchall())
        self.assertNotIn("SECRET", payload)
        self.assertNotIn("person@example.com", payload)
        self.assertIn("p***@example.com", payload)

    def test_metadata_import_is_idempotent_and_charge_not_multiplied_by_calls(self):
        data = dict(
            version=1,
            bindings=[
                dict(
                    provider="openai",
                    account_id="a",
                    subscription_id="sub",
                    tool="codex",
                    session="s",
                    start_ts=1,
                    end_ts=200,
                )
            ],
            charges=[
                dict(
                    charge_id="invoice-1",
                    provider="openai",
                    account_id="a",
                    subscription_id="sub",
                    period_start=1,
                    period_end=200,
                    usd=20,
                )
            ],
        )
        accounting.import_metadata(self.db, data, "host")
        accounting.import_metadata(self.db, data, "host")
        self.ingest(self.call(), self.call(uid="second"))
        r = work.effectiveness(self.db)
        p = r["subscription_periods"][0]
        self.assertEqual((p["subscription_spend_usd"], p["calls"]), (20, 2))
        self.assertGreater(p["known_value_per_subscription_dollar_low"], 0)
        self.assertIsNone(r["sessions"][0]["actual_billed_usd"])
        self.assertEqual(
            self.db.execute("select count(*) from subscription_charge_events").fetchone()[0], 1
        )

    def test_overlapping_subscription_periods_stay_unassigned(self):
        self.ingest(self.identity(), self.call())
        data = dict(
            version=1,
            charges=[
                dict(
                    charge_id="invoice" + s,
                    provider="openai",
                    account_id="a",
                    subscription_id=s,
                    period_start=1,
                    period_end=200,
                    usd=20,
                )
                for s in ("one", "two")
            ],
        )
        accounting.import_metadata(self.db, data, "host")
        r = work.effectiveness(self.db)
        self.assertTrue(all(p["calls"] == 0 for p in r["subscription_periods"]))

    def test_agent_reported_session_value_is_not_billed_spend(self):
        cost = accounting.reported_cost(
            dict(type="cost-state", sessionId="s", totalCostUSD=4.25, hasUnknownModelCost=False),
            123,
        )
        self.ingest(self.call(tool="claude"), cost)
        s = work.effectiveness(self.db)["sessions"][0]
        self.assertEqual(s["reported_session_value"]["usd"], 4.25)
        self.assertIsNone(s["actual_billed_usd"])

    def test_future_profile_snapshot_does_not_backdate_account_assignment(self):
        self.ingest(self.identity(ts=200), self.call(ts=100))
        self.assertEqual(work.effectiveness(self.db)["sessions"][0]["accounts"], [])

    def test_subscription_plan_alone_is_not_account_identity(self):
        rows = accounting.observe(
            "codex",
            {},
            dict(rate_limits=dict(plan_type="pro", primary=dict(used_percent=1, resets_at=100))),
            dict(session="s"),
            1,
            "/profile/sessions/a",
        )
        self.assertFalse(any(r["t"] == "identity" for r in rows))

    def test_metadata_import_rejects_unbounded_or_incomplete_charge(self):
        with self.assertRaises(ValueError):
            accounting.import_metadata(
                self.db,
                dict(version=1, charges=[dict(provider="openai", account_id="a", usd=20)]),
                "host",
            )

    def test_charge_mirrors_do_not_double_count_and_conflicts_stay_unknown(self):
        d = dict(
            version=1,
            charges=[
                dict(
                    charge_id="invoice",
                    provider="openai",
                    account_id="a",
                    subscription_id="sub",
                    period_start=1,
                    period_end=200,
                    usd=20,
                )
            ],
        )
        accounting.import_metadata(self.db, d, "host")
        row = accounting.rows(self.db, "subscription_charge_events")[0]
        row.pop("machine")
        row["t"] = "subscription_charge"
        store.ingest(self.db, [json.dumps(row)], machine="mirror")
        self.assertEqual(
            work.effectiveness(self.db)["subscription_periods"][0]["subscription_spend_usd"], 20
        )
        row["usd"] = 30
        store.ingest(self.db, [json.dumps(row)], machine="mirror")
        self.assertIsNone(
            work.effectiveness(self.db)["subscription_periods"][0]["subscription_spend_usd"]
        )

    def test_commit_work_exists_without_any_pr_and_batched_commits_are_unknown(self):
        self.ingest(
            work.context_event("codex", "s", 1, "/repo", repo="github.com/a/r", scope_id="scope"),
            self.call(ts=5),
            self.call(uid="later", ts=15),
        )
        for sha, ts in [("a" * 40, 10), ("b" * 40, 20)]:
            self.ingest(
                dict(
                    t="work_commit",
                    uid=sha,
                    ts=ts,
                    repo="github.com/a/r",
                    sha=sha,
                    added_code=1,
                    removed_code=0,
                    files=1,
                ),
                dict(
                    t="work_evidence",
                    uid=sha,
                    ts=ts,
                    tool="codex",
                    session="s",
                    repo="github.com/a/r",
                    sha=sha,
                    scope_id="scope",
                    capture_version=5,
                    provenance="tool_commit",
                ),
            )
        r = work.effectiveness(self.db)
        self.assertEqual(r["commit_attributed_calls"], 2)
        self.assertEqual(r["attributed_calls"], 0)
        self.assertTrue(all(c["calls"] == 1 for c in r["commits"]))
        self.ingest(
            dict(
                t="work_evidence",
                uid="another",
                ts=10,
                tool="codex",
                session="s",
                repo="github.com/a/r",
                sha="c" * 40,
                scope_id="scope",
                capture_version=5,
                provenance="tool_commit",
            )
        )
        r = work.effectiveness(self.db)
        self.assertEqual(r["commit_unattributed_reasons"]["ambiguous_commit_batch"], 1)

    def test_provider_account_probe_joins_exact_id_email_and_retains_reset_credit_count(self):
        from erg import account_probe

        result = dict(
            account=dict(type="chatgpt", email="person@example.com", planType="new-plan"),
            limits=dict(
                accountId="acct",
                rateLimitsByLimitId={
                    "codex": dict(
                        primary=dict(usedPercent=99, windowDurationMins=300, resetsAt=500)
                    )
                },
                rateLimitResetCredits=dict(availableCount=2, credits=[dict(description="SECRET")]),
            ),
        )
        events = account_probe.events(self.path, result, 1)
        self.ingest(*events)
        report = work.effectiveness(self.db)
        self.assertEqual(report["accounts"][0]["plan"], "new-plan")
        self.assertEqual(len(report["quota_latest"]), 2)
        self.assertNotIn(
            "SECRET", str(self.db.execute("select payload from observations").fetchall())
        )
        self.assertNotIn(
            "person@example.com",
            str(self.db.execute("select payload from observations").fetchall()),
        )

    def test_creator_account_is_not_proven_subscription_billing(self):
        self.ingest(self.identity(provenance="codex_creator_account"), self.call())
        d = dict(
            version=1,
            charges=[
                dict(
                    charge_id="invoice",
                    provider="openai",
                    account_id="a",
                    subscription_id="sub",
                    period_start=1,
                    period_end=200,
                    usd=20,
                )
            ],
        )
        accounting.import_metadata(self.db, d, "host")
        report = work.effectiveness(self.db)
        self.assertEqual(len(report["sessions"][0]["accounts"]), 1)
        self.assertEqual(report["subscription_periods"][0]["calls"], 0)

    def test_claude_lifecycle_uses_actual_session_auth_and_ignores_prompt(self):
        status = json.dumps(
            dict(
                loggedIn=True,
                authMethod="oauth",
                account_id="acct",
                email="person@example.com",
                subscriptionType="max",
            )
        )
        with patch.dict("os.environ", {"ANTHROPIC_BASE_URL": ""}), patch(
            "subprocess.run", return_value=type("R", (), dict(returncode=0, stdout=status))()
        ):
            self.assertEqual(
                accounting.claude_session_start(dict(session_id="s", prompt="SECRET"), self.db), 1
            )
        payload = str(self.db.execute("select payload from observations").fetchall())
        self.assertNotIn("SECRET", payload)
        resolve, _, _ = accounting.resolver(self.db)
        self.assertTrue(resolve("claude", "s", 2**40)["billing_evidence"])

    def test_account_metadata_layer_charge_replay_does_not_backfill_ownership(self):
        self.ingest(self.call())
        accounting.import_metadata(
            self.db,
            dict(
                version=1,
                charges=[
                    dict(
                        charge_id="invoice",
                        provider="openai",
                        account_id="a",
                        subscription_id="sub",
                        period_start=1,
                        period_end=200,
                        usd=20,
                    )
                ],
            ),
            "host",
        )
        self.assertEqual(work.effectiveness(self.db)["subscription_periods"][0]["calls"], 0)

    def test_reported_native_cost_requires_finite_nonnegative_number(self):
        self.assertEqual(collect.reported_cost(dict(cost=dict(total=1.25))), 1.25)
        self.assertIsNone(collect.reported_cost(dict(cost=dict(total="SECRET"))))
        self.assertIsNone(collect.reported_cost(dict(cost=dict(total=-2))))

    def test_non_usd_charge_preserves_currency_without_inventing_conversion(self):
        accounting.import_metadata(
            self.db,
            dict(
                version=1,
                charges=[
                    dict(
                        charge_id="invoice",
                        provider="openai",
                        account_id="a",
                        subscription_id="sub",
                        period_start=1,
                        period_end=200,
                        amount=20,
                        currency="EUR",
                    )
                ],
            ),
            "host",
        )
        p = work.effectiveness(self.db)["subscription_periods"][0]
        self.assertIsNone(p["subscription_spend_usd"])
        self.assertEqual(p["currency_amounts"], {"EUR": 20})

    def test_session_detail_cap_does_not_truncate_token_summary(self):
        from erg.server import Reports

        for i in range(1001):
            self.ingest(self.call(uid="call" + str(i)))
        with patch.object(store, "open_readonly", return_value=self.db):
            r = Reports().session("codex", "s")
        self.assertEqual(len(r["calls"]), 1000)
        self.assertTrue(r["calls_truncated"])
        self.assertEqual(r["metrics"]["total_tokens"], 35035)

    def test_claude_hook_install_preserves_user_settings_and_is_idempotent(self):
        from erg import service

        path = self.path / ".claude" / "settings.json"
        path.parent.mkdir()
        path.write_text(
            json.dumps(
                dict(
                    custom="keep",
                    hooks=dict(
                        SessionStart=[dict(hooks=[dict(type="command", command="user-hook")])]
                    ),
                )
            )
        )
        with patch.object(collect, "HOME", str(self.path)), patch.dict(
            "os.environ", {}, clear=True
        ):
            service.install_claude_identity_hook(self.path / "state")
            service.install_claude_identity_hook(self.path / "state")
        d = json.loads(path.read_text())
        self.assertEqual(d["custom"], "keep")
        self.assertEqual(len(d["hooks"]["SessionStart"]), 2)
        self.assertEqual(d["hooks"]["SessionStart"][0]["hooks"][0]["command"], "user-hook")

    def test_local_creator_metadata_is_hashed_and_custom_provider_not_billing_account(self):
        import sqlite3

        home = self.path / ".codex"
        home.mkdir()
        db = sqlite3.connect(str(home / "state_5.sqlite"))
        db.execute(
            "create table threads(id TEXT,model_provider TEXT,created_at INT,creator_account_id TEXT)"
        )
        db.executemany(
            "insert into threads values(?,?,?,?)",
            [("s", "openai", 1, "SECRET_ID"), ("gateway", "Databricks", 1, "SECRET_ID")],
        )
        db.commit()
        db.close()
        with patch.object(collect, "HOME", str(self.path)), patch.object(
            collect, "native_roots", return_value=[str(home / "sessions")]
        ):
            accounting.capture_codex_creators(self.db)
        ids = accounting.rows(self.db, "identity_events")
        self.assertEqual(len(ids), 2)
        self.assertEqual({r["session"] for r in ids}, {"s", "gateway"})
        self.assertNotIn("SECRET_ID", str(ids))
        self.assertFalse(accounting.resolver(self.db)[0]("codex", "gateway", 2)["billing_evidence"])

    def test_invoice_ids_are_namespaced_by_account(self):
        for account in ("a", "b"):
            accounting.import_metadata(
                self.db,
                dict(
                    version=1,
                    charges=[
                        dict(
                            charge_id="001",
                            provider="openai",
                            account_id=account,
                            subscription_id=account,
                            period_start=1,
                            period_end=200,
                            usd=20,
                        )
                    ],
                ),
                "host",
            )
        periods = work.effectiveness(self.db)["subscription_periods"]
        self.assertEqual(len(periods), 2)
        self.assertEqual(sum(p["subscription_spend_usd"] for p in periods), 40)

    def test_structured_native_limit_error_is_captured_without_message_body(self):
        state = dict(session="s")
        payload = dict(
            type="error",
            message="You've hit your usage limit. SECRET",
            codex_error_info="UsageLimitExceeded",
        )
        rows = accounting.observe(
            "codex", dict(type="event_msg"), payload, state, 1, "/profile/sessions/a"
        )
        self.assertEqual(rows[0]["event"], "limit_reached")
        self.assertNotIn("SECRET", str(rows))

    def test_claude_identity_hook_migrates_async_to_bounded_synchronous(self):
        from erg import service

        path = self.path / ".claude" / "settings.json"
        path.parent.mkdir()
        with patch.object(collect, "HOME", str(self.path)), patch.dict(
            "os.environ", {}, clear=True
        ):
            service.install_claude_identity_hook(self.path / "state")
            settings = json.loads(path.read_text())
            h = settings["hooks"]["SessionStart"][0]["hooks"][0]
            h["async"] = True
            h.pop("timeout", None)
            path.write_text(json.dumps(settings))
            service.install_claude_identity_hook(self.path / "state")
            service.install_claude_identity_hook(self.path / "state")
        settings = json.loads(path.read_text())
        self.assertEqual(len(settings["hooks"]["SessionStart"]), 1)
        h = settings["hooks"]["SessionStart"][0]["hooks"][0]
        self.assertNotIn("async", h)
        self.assertEqual(h["timeout"], 8)

    def test_report_registry_canonicalizes_verified_email_alias_without_duplicate_account(self):
        self.ingest(self.identity(), self.call())
        fields = accounting.identity_fields(dict(email="same@example.com", plan="team"), "openai")
        self.ingest(
            dict(
                t="identity",
                uid="email-observation",
                ts=50,
                tool="codex",
                session="s",
                provider="openai",
                provenance="native_metadata",
                **fields,
            )
        )
        report = work.effectiveness(self.db)
        self.assertEqual(len({r["account_key"] for r in report["accounts"]}), 1)
        self.assertEqual(report["sessions"][0]["observed_plans"], ["team"])
