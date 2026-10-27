import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from erg import accounting, collect, routes, store, work


class RouteEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.db = store.connect(str(self.home / "ledger"))
        self.addCleanup(self.db.close)

    def ingest(self, *rows):
        store.ingest(self.db, map(json.dumps, rows), machine="host")

    def call(self, uid="call", ts=100, tool="claude", session="s"):
        return dict(
            t="llm", uid=uid, ts=ts, tool=tool, session=session, model="unknown", fresh_in=1, out=1
        )

    def test_precomputed_routes_preserve_conflicts_and_request_isolation(self):
        self.ingest(
            routes.event(
                "pi", "s", 10, dict(provider="custom", base_url="https://one.example/v1"), "native"
            ),
            routes.event(
                "pi",
                "s",
                12,
                dict(
                    provider="custom", base_url="https://request.example/v1", request_uid="request"
                ),
                "native",
            ),
        )
        for host in ("a.example", "b.example"):
            self.ingest(
                routes.event(
                    "pi",
                    "s",
                    20,
                    dict(provider="custom", base_url="https://" + host + "/v1"),
                    "native",
                )
            )
        resolve = routes.route_resolver(self.db)
        self.assertEqual(resolve("pi", "s", 5), (None, "route_not_recorded_at_call"))
        override, error = resolve("pi", "s", 15, "request")
        self.assertIsNone(error)
        self.assertEqual(override["endpoint_host"], "request.example")
        base, error = resolve("pi", "s", 15)
        self.assertEqual(base["endpoint_host"], "one.example")
        base["endpoint_host"] = "changed-by-caller"
        self.assertEqual(resolve("pi", "s", 15)[0]["endpoint_host"], "one.example")
        self.assertEqual(resolve("pi", "s", 25, "request"), (None, "conflicting_session_routes"))

    def test_untimestamped_bridge_owner_is_session_scoped_and_not_billing(self):
        path = self.home / "log.jsonl"
        owner = dict(
            type="bridge-session",
            sessionId="s",
            ownerAccountUuid="PRIVATE_ID",
            ownerOrganizationUuid="PRIVATE_ORG",
        )
        path.write_text(json.dumps(owner) + "\n")
        work.capture_file(self.db, "claude", path, True)
        self.ingest(self.call())
        s = work.effectiveness(self.db)["sessions"][0]
        self.assertEqual(s["accounts"], [accounting.digest("anthropic", "PRIVATE_ID")])
        self.assertIsNone(s["billing_account"])
        self.assertIn("owner_identity_is_not_billing_evidence", s["unknown_reasons"])
        self.assertNotIn(
            "PRIVATE", str(self.db.execute("select payload from observations").fetchall())
        )
        work.capture_file(self.db, "claude", path, True)
        self.assertEqual(self.db.execute("select count(*) from identity_events").fetchone()[0], 1)

    def test_artifact_reactor_account_is_not_session_owner(self):
        self.assertEqual(
            routes.native(
                "claude",
                dict(type="artifact-autoreact-ledger", sessionId="s", accountUuid="other"),
                {},
                None,
                {},
            ),
            [],
        )

    def test_conflicting_session_owners_remain_unknown(self):
        for owner in ("one", "two"):
            self.ingest(
                *routes.native(
                    "claude",
                    dict(type="bridge-session", sessionId="s", ownerAccountUuid=owner),
                    {},
                    None,
                    {},
                )
            )
        self.ingest(self.call())
        s = work.effectiveness(self.db)["sessions"][0]
        self.assertEqual(s["accounts"], [])
        self.assertIn("conflicting_owner_ids", s["unknown_reasons"])

    def test_current_configuration_never_backfills_route_or_payer(self):
        self.ingest(
            self.call(),
            routes.event(
                "claude",
                "s",
                150,
                dict(provider="custom", base_url="https://gateway.example/v1", region="r"),
                "configuration",
                scope="configuration",
            ),
        )
        s = work.effectiveness(self.db)["sessions"][0]
        self.assertEqual(s["route_call_coverage"]["endpoint_known_calls"], 0)
        self.assertIsNone(s["billing_account"])
        self.assertIn("current_configuration_is_not_historical_route_proof", s["unknown_reasons"])

    def test_route_change_and_partial_coverage_do_not_rewrite_prior_calls(self):
        self.ingest(
            self.call(ts=10),
            self.call("new", 30),
            routes.event(
                "claude",
                "s",
                20,
                dict(provider="arbitrary", base_url="http://localhost:9001/v1"),
                "lifecycle",
            ),
        )
        s = work.effectiveness(self.db)["sessions"][0]
        self.assertEqual(s["route_call_coverage"]["endpoint_known_calls"], 1)
        self.assertIn("partial_session_time_endpoint_coverage", s["unknown_reasons"])

    def test_conflicting_same_time_routes_are_unknown(self):
        for host in ("one.example", "two.example"):
            self.ingest(
                routes.event(
                    "claude",
                    "s",
                    50,
                    dict(provider="custom", base_url="https://" + host + "/v1"),
                    "lifecycle",
                )
            )
        self.ingest(self.call())
        s = work.effectiveness(self.db)["sessions"][0]
        self.assertEqual(s["route_call_coverage"]["endpoint_known_calls"], 0)
        self.assertIn("conflicting_session_routes", s["unknown_reasons"])

    def test_endpoint_sanitization_drops_credentials_query_and_opaque_path(self):
        self.assertEqual(routes.endpoint("https://user:SECRET@host.example/v1"), {})
        self.assertEqual(
            routes.endpoint("https://host.example/token/SECRET?token=PRIVATE#PRIVATE"),
            dict(endpoint_host="host.example", endpoint_path="", transport="https"),
        )
        row = routes.event(
            "pi",
            "s",
            1,
            dict(
                provider="custom",
                base_url="https://host.example/v1?token=SECRET",
                account_id="PRIVATE",
                account_provider="provider",
            ),
            "native",
        )
        self.assertNotIn("SECRET", str(row))
        self.assertNotIn("PRIVATE", str(row))
        self.assertEqual(row["endpoint_path"], "/v1")

    def test_gateway_claude_hook_preserves_route_but_never_borrows_oauth_identity(self):
        env = dict(
            ANTHROPIC_BASE_URL="https://gateway.example/ai-gateway/anthropic/v1?token=SECRET",
            DATABRICKS_HOST="https://gateway.example",
            DATABRICKS_CONFIG_PROFILE="arbitrary-profile",
        )
        with patch.dict(os.environ, env, clear=True), patch("subprocess.run") as run:
            self.assertEqual(
                accounting.claude_session_start(dict(session_id="s", prompt="PRIVATE"), self.db), 0
            )
            run.assert_not_called()
        row = accounting.rows(self.db, "session_route_events")[0]
        self.assertEqual(row["profile"], "arbitrary-profile")
        self.assertNotIn("SECRET", str(row))
        self.assertEqual(accounting.rows(self.db, "identity_events"), [])

    def test_dynamic_wrapper_config_is_observed_once_and_never_executes_auth(self):
        bridge = self.home / "codex-native/w"
        home = bridge / "codex-home"
        home.mkdir(parents=True)
        (bridge / "state.json").write_text(json.dumps(dict(thread_id="s", codex_home=str(home))))
        (home / "config.toml").write_text(
            '[model_providers]\nmy_provider = {base_url="https://arbitrary.example/v1", auth={command="SECRET"}}\n'
        )
        self.ingest(
            dict(
                t="session",
                uid="s",
                tool="codex",
                session="s",
                provider="my_provider",
                ts=1,
                last_ts=2,
            )
        )
        with patch.object(collect, "omni_home", return_value=str(self.home)), patch.object(
            routes.profiles, "cache", (0, []), create=True
        ), patch("subprocess.run") as run:
            self.assertEqual(routes.capture_wrappers(self.db), 1)
            self.assertEqual(routes.capture_wrappers(self.db), 0)
            run.assert_not_called()
        r = accounting.rows(self.db, "session_route_events")[0]
        self.assertEqual(r["scope"], "configuration")
        self.assertEqual(r["endpoint_host"], "arbitrary.example")
        self.assertNotIn("SECRET", str(r))

    def test_native_pi_provider_does_not_erase_matching_lifecycle_endpoint(self):
        self.ingest(
            self.call(tool="pi", uid="pi:s:entry"),
            routes.event(
                "pi",
                "s",
                50,
                dict(provider="custom", base_url="http://localhost:9001/v1"),
                "pi_lifecycle",
            ),
        )
        d = dict(
            type="message",
            id="entry",
            message=dict(role="assistant", provider="custom", usage=dict(input=1)),
        )
        self.ingest(*routes.native("pi", d, d, 100, dict(session="s")))
        self.assertEqual(
            work.effectiveness(self.db)["sessions"][0]["route_call_coverage"][
                "endpoint_known_calls"
            ],
            1,
        )

    def test_backfill_metadata_is_idempotent_and_does_not_reingest_calls(self):
        path = self.home / "log.jsonl"
        path.write_text(
            json.dumps(dict(type="bridge-session", sessionId="s", ownerAccountUuid="owner")) + "\n"
        )
        with patch.object(collect, "claude_roots", return_value=[str(path)]), patch.object(
            collect, "native_roots", return_value=[]
        ), patch.object(collect, "pi_roots", return_value=[]):
            self.assertEqual(routes.backfill(self.db)["errors"], 0)
            routes.backfill(self.db)
        self.assertEqual(self.db.execute("select count(*) from identity_events").fetchone()[0], 1)
        self.assertEqual(self.db.execute("select count(*) from llm").fetchone()[0], 0)

    def test_saved_omnigent_snapshot_uses_session_config_not_current_profile(self):
        snapshot = dict(
            runtime_config=dict(
                providers={
                    "custom-local": dict(
                        kind="local",
                        openai=dict(base_url="http://localhost:8000/v1", api_key="SECRET"),
                    )
                },
                inference=dict(harnesses={"pi-native": dict(provider="custom-local")}),
            )
        )
        rows = routes.omnigent_snapshot("pi", "s", 10, snapshot)
        self.assertEqual(rows[0]["endpoint_host"], "localhost:8000")
        self.assertEqual(rows[0]["billing_mode"], "local")
        self.assertNotIn("SECRET", str(rows))
        snapshot["runtime_config"]["providers"]["custom-local"] = dict(
            kind="databricks", profile="regional-profile"
        )
        row = routes.omnigent_snapshot("pi", "s", 10, snapshot)[0]
        self.assertEqual(row["profile"], "regional-profile")
        self.assertNotIn("endpoint_host", row)
        self.assertNotIn("region", row)

    def test_gateway_service_classification_keeps_config_scope_and_payer_unknown(self):
        config = routes.event(
            "codex",
            "s",
            150,
            dict(
                provider="dynamic-alias",
                base_url="https://workspace.example/v1",
                workspace_id="123",
            ),
            "omnigent_active_configuration",
            scope="configuration",
        )
        native = routes.event(
            "codex", "s", 50, dict(provider="dynamic-alias"), "native_session_metadata"
        )
        self.ingest(config, native, self.call(tool="codex"))
        s = work.effectiveness(self.db)["sessions"][0]
        self.assertEqual(
            s["inference_route"]["evidence"], "native_provider_and_current_configuration"
        )
        self.assertEqual(s["route_call_coverage"]["endpoint_known_calls"], 0)
        self.assertIsNone(s["billing_account"])
        self.assertIn("session_time_endpoint_not_recorded", s["unknown_reasons"])
        self.ingest(
            routes.event(
                "pi",
                "pi-session",
                50,
                dict(base_url="https://workspace.example/v1"),
                "pi_lifecycle",
            ),
            self.call(tool="pi", session="pi-session", uid="pi-call"),
        )
        s = next(s for s in work.effectiveness(self.db)["sessions"] if s["tool"] == "pi")
        self.assertEqual(s["inference_route"]["evidence"], "session_endpoint")
        self.assertIsNone(s["billing_account"])

    def test_gateway_classification_rejects_conflicting_current_endpoints(self):
        self.ingest(
            self.call(tool="codex"),
            routes.event(
                "codex", "s", 50, dict(provider="dynamic-alias"), "native_session_metadata"
            ),
        )
        for host in ("one.example", "two.example"):
            self.ingest(
                routes.event(
                    "codex",
                    "s",
                    150,
                    dict(provider="dynamic-alias", base_url="https://" + host, workspace_id="123"),
                    "omnigent_active_configuration",
                    scope="configuration",
                )
            )
        self.assertIsNone(work.effectiveness(self.db)["sessions"][0]["inference_route"])

    def test_billing_uses_request_principal_without_leaking_across_calls(self):
        owner = accounting.identity_fields(dict(account_id="owner"), "openai")
        self.ingest(
            dict(
                t="identity",
                uid="owner",
                ts=1,
                tool="codex",
                session="s",
                provider="openai",
                provenance="codex_creator_account",
                scope="session_owner",
                **owner,
            )
        )
        self.ingest(
            routes.event(
                "codex",
                "s",
                50,
                dict(
                    provider="gateway",
                    account_id="payer",
                    account_provider="databricks",
                    request_uid="paid",
                ),
                "native",
                scope="request",
            )
        )
        resolve, _, _ = accounting.resolver(self.db)
        a = resolve("codex", "s", 100, request_uid="paid")
        self.assertTrue(a["billing_evidence"])
        self.assertEqual(a["provider"], "databricks")
        self.assertEqual(a["account_key"], accounting.digest("databricks", "payer"))
        self.assertFalse(resolve("codex", "s", 100, request_uid="other")["billing_evidence"])

    def test_billing_configuration_payer_is_not_historical_evidence(self):
        self.ingest(
            routes.event(
                "codex",
                "s",
                1,
                dict(provider="openai", account_id="payer", account_provider="openai"),
                "config",
                scope="configuration",
            )
        )
        resolve, _, _ = accounting.resolver(self.db)
        self.assertIsNone(resolve("codex", "s", 100))

    def test_billing_route_change_does_not_reuse_previous_route_principal(self):
        self.ingest(
            routes.event(
                "pi",
                "s",
                10,
                dict(provider="one", account_id="first", account_provider="openai"),
                "native",
            ),
            routes.event(
                "pi", "s", 20, dict(provider="two", base_url="https://gateway.example/v1"), "native"
            ),
        )
        resolve, _, _ = accounting.resolver(self.db)
        self.assertTrue(resolve("pi", "s", 15)["billing_evidence"])
        self.assertIsNone(resolve("pi", "s", 25))

    def test_conflicting_route_payers_remain_unknown(self):
        for payer in ("one", "two"):
            self.ingest(
                routes.event(
                    "codex",
                    "s",
                    10,
                    dict(provider="openai", account_id=payer, account_provider="openai"),
                    "native",
                )
            )
        resolve, _, _ = accounting.resolver(self.db)
        self.assertIsNone(resolve("codex", "s", 100))

    def test_request_route_switch_cannot_inherit_another_endpoint_payer(self):
        self.ingest(
            routes.event(
                "pi",
                "s",
                10,
                dict(
                    provider="custom",
                    base_url="https://one.example/v1",
                    account_id="payer",
                    account_provider="openai",
                ),
                "native",
            )
        )
        self.ingest(
            routes.event(
                "pi",
                "s",
                20,
                dict(provider="custom", base_url="https://two.example/v1", request_uid="moved"),
                "native",
            )
        )
        resolve, _, _ = accounting.resolver(self.db)
        self.assertIsNone(resolve("pi", "s", 30, request_uid="moved"))
        self.assertTrue(resolve("pi", "s", 30)["billing_evidence"])

    def test_duplicate_request_principals_conflict_without_last_writer_wins(self):
        for payer in ("one", "two"):
            self.ingest(
                routes.event(
                    "pi",
                    "s",
                    10,
                    dict(
                        provider="custom",
                        account_id=payer,
                        account_provider="openai",
                        request_uid="ambiguous",
                    ),
                    "native",
                )
            )
        resolve, _, _ = accounting.resolver(self.db)
        self.assertIsNone(resolve("pi", "s", 30, request_uid="ambiguous"))
