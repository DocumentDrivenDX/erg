import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from erg import collect, store, unified, work

URL = "https://github.com/acme/r/pull/1"


class Effectiveness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.db = store.connect(str(self.home / "erg.db"))
        self.addCleanup(self.db.close)
        self.machine = patch.object(collect, "machine_name", return_value="host")
        self.machine.start()
        self.addCleanup(self.machine.stop)

    def ingest(self, *rows):
        store.ingest(self.db, map(json.dumps, rows), machine="host")

    def call(self, **kw):
        return dict(
            dict(
                t="llm",
                uid="call",
                ts=20,
                tool="codex",
                session="s",
                project="r",
                model="m",
                fresh_in=10,
                cache_read=2,
                cache_write=3,
                out=5,
                reasoning=1,
            ),
            **kw,
        )

    def pr(self, url=URL):
        self.ingest(
            dict(
                t="pull_request",
                uid=url,
                ts=100,
                url=url,
                repo="github.com/acme/r",
                number=1,
                state="merged",
                created_ts=5,
                merged_ts=50,
                closed_ts=50,
                branch="feature",
                head_sha="abc",
                added_code=12,
                removed_code=4,
                files_complete=1,
            ),
            dict(t="pr_commit", uid=url + ":abc", ts=100, pr_url=url, sha="abc"),
        )

    def ctx(self, **kw):
        return work.context_event(
            "codex",
            "s",
            10,
            "/code/r",
            repo="github.com/acme/r",
            branch="feature",
            head_sha=kw.get("head", "abc"),
            task_id="task-1",
        )

    def test_focused_work_view_matches_full_for_known_unknown_and_conflicting_work(self):
        from erg import report_views

        card = dict(
            entries=[
                dict(
                    model="m",
                    as_of="fixture",
                    source="fixture",
                    input=1,
                    output=2,
                    cache_read=0.1,
                    cache_write_5m=1.25,
                    cache_write_1h=2,
                )
            ]
        )
        self.pr()
        context = self.ctx()
        context["scope_id"] = "scope"
        self.ingest(
            context,
            self.call(),
            self.call(uid="unknown", session="missing"),
            dict(
                t="prompt",
                uid="human",
                ts=15,
                tool="codex",
                session="s",
                origin="human",
                provenance="codex_user_event",
            ),
            dict(
                t="work_commit",
                uid="commit",
                ts=25,
                repo="github.com/acme/r",
                sha="abc",
                added_code=2,
                removed_code=1,
                files=1,
                provenance="native_tool",
            ),
            dict(
                t="work_evidence",
                uid="fact",
                ts=25,
                tool="codex",
                session="s",
                repo="github.com/acme/r",
                scope_id="scope",
                sha="abc",
                pr_url="",
                provenance="native_tool",
                capture_version=5,
            ),
        )

        def parity():
            with patch.object(work.time, "time", return_value=10000), patch.object(
                work.rates, "load", return_value=card
            ):
                full = work.effectiveness(self.db)
                with patch.object(
                    work.accounting, "resolver", side_effect=AssertionError("unused account fold")
                ), patch.object(
                    work.routes, "route_resolver", side_effect=AssertionError("unused route fold")
                ), patch(
                    "erg.session_usage.new_row", side_effect=AssertionError("unused session fold")
                ):
                    partial = work.effectiveness(self.db, view="work")
                self.assertEqual(
                    report_views.project(partial, "work"), report_views.project(full, "work")
                )
            return partial

        result = parity()
        self.assertEqual(result["prs"][0]["calls"], 1)
        self.assertEqual(result["commits"][0]["calls"], 1)
        self.assertEqual(result["prs"][0]["valuation"]["priced_calls"], 1)
        self.assertIsNone(result["prs"][0]["cost_usd"])
        # A second PR with the same branch/head cannot become uniquely attributable.
        self.pr("https://github.com/acme/r/pull/2")
        parity()
        # Retained-source conflicts suppress automatic facts in both calculations.
        self.db.execute(
            "INSERT INTO history_work_conflicts VALUES('host','codex','s','work_source_changed_after_projection')"
        )
        self.db.commit()
        result = parity()
        self.assertEqual(result["attributed_calls"], 0)
        with self.assertRaises(ValueError):
            work.effectiveness(self.db, view="private-unknown")

    def test_precomputed_context_conflict_does_not_leak_across_intervals(self):
        self.pr()
        self.ingest(
            self.ctx(),
            self.ctx(head="conflicting"),
            self.call(uid="ambiguous", ts=20),
            work.context_event(
                "codex",
                "s",
                30,
                "/code/r",
                repo="github.com/acme/r",
                branch="feature",
                head_sha="abc",
                task_id="task-1",
            ),
            self.call(uid="later", ts=40),
        )
        result = work.effectiveness(self.db)
        self.assertEqual(result["attributed_calls"], 1)
        self.assertEqual(result["unattributed_reasons"], {"ambiguous": 1})
        self.assertEqual(result["prs"][0]["calls"], 1)

    def test_verified_commit_and_branch_allocate_once_cost_unknown(self):
        self.pr()
        self.ingest(self.ctx(), self.call(), self.call())
        self.ingest(
            dict(
                t="prompt",
                uid="input",
                ts=15,
                tool="codex",
                session="s",
                project="r",
                chars=4,
                words=1,
                est_tokens=1,
                origin="unknown",
                provenance="codex_user_event",
            )
        )
        report = work.effectiveness(self.db)
        r = report["prs"][0]
        self.assertEqual(
            (report["total_calls"], report["attributed_calls"], r["calls"], r["out"]), (1, 1, 1, 5)
        )
        self.assertIsNone(r["cost_usd"])
        self.assertIsNone(r["human_inputs"])
        self.assertEqual(r["observed_inputs"], 1)
        self.assertEqual(r["tasks"], ["task-1"])
        self.assertEqual(r["total_tokens"], 20)

    def test_branch_without_commit_evidence_stays_unattributed(self):
        self.pr()
        self.ingest(self.ctx(head="unrelated"), self.call())
        r = work.effectiveness(self.db)
        self.assertEqual(r["unattributed_calls"], 1)
        self.assertEqual(r["prs"][0]["usage_status"], "unknown")

    def test_shared_branch_commit_is_ambiguous_and_never_double_charged(self):
        self.pr()
        self.pr("https://github.com/acme/r/pull/2")
        self.ingest(self.ctx(), self.call())
        r = work.effectiveness(self.db)
        self.assertEqual(r["attributed_calls"], 0)
        self.assertEqual(r["unattributed_reasons"]["ambiguous"], 1)

    def test_operator_link_scope_and_overlapping_conflicts(self):
        self.ingest(self.call())
        work.bind(self.db, "codex", "s", URL, "task-x", 10, 30)
        r = work.effectiveness(self.db)["prs"][0]
        self.assertEqual(r["calls"], 1)
        self.assertEqual(r["state"], "unknown")
        self.assertIsNone(r["added_code"])
        work.bind(self.db, "codex", "s", "https://github.com/acme/r/pull/2", "", 20, 30)
        r = work.effectiveness(self.db)
        self.assertEqual(r["attributed_calls"], 0)

    def test_pre_creation_and_post_merge_work_not_guessed(self):
        self.pr()
        self.ingest(self.ctx(), self.call(ts=60), self.call(uid="before", ts=4))
        self.assertEqual(work.effectiveness(self.db)["unattributed_calls"], 2)

    def test_subagent_inherits_explicit_parent_scope(self):
        work.bind(self.db, "codex", "s", URL, "task", 10, 30)
        self.ingest(
            collect.session_record("codex", "child", 10, parent_session="s"),
            self.call(session="child"),
        )
        r = work.effectiveness(self.db)["prs"][0]
        self.assertEqual(r["calls"], 1)
        self.assertEqual(r["evidence"], ["parent_operator"])

    def test_native_tail_privacy_replay_partial_line_and_task_context(self):
        path = self.home / "native.jsonl"
        events = [
            dict(
                type="session_meta",
                timestamp="2026-10-17T00:00:00Z",
                payload=dict(
                    id="s",
                    cwd="/code/r",
                    git=dict(
                        repository_url="https://user:SECRET@github.com/acme/r.git",
                        branch="feature",
                        commit_hash="abc",
                    ),
                ),
            ),
            dict(
                type="response_item",
                timestamp="2026-10-17T00:00:01Z",
                payload=dict(
                    type="message",
                    role="user",
                    internal_chat_message_metadata_passthrough=dict(
                        content_item_kinds=["user.text"]
                    ),
                    content=[dict(text="Task-ID: task-x\nSECRET HUMAN BODY")],
                ),
            ),
        ]
        path.write_text("".join(json.dumps(e) + "\n" for e in events) + '{"incomplete"')
        work.capture_file(self.db, "codex", str(path), True)
        before = self.db.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]
        work.capture_file(self.db, "codex", str(path), True)
        self.assertEqual(before, 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM prompts").fetchone()[0], 1)
        payloads = [r[0] for r in self.db.execute("SELECT payload FROM observations")]
        self.assertNotIn("SECRET", json.dumps(payloads))
        self.assertIn("task-x", json.dumps(payloads))
        self.assertEqual(
            self.db.execute("SELECT MAX(repo) FROM work_contexts").fetchone()[0],
            "github.com/acme/r",
        )

    def test_native_checkpoint_and_events_rollback_together(self):
        path = self.home / "native.jsonl"
        path.write_text(
            json.dumps(
                dict(
                    type="user",
                    timestamp="2026-10-17T00:00:00Z",
                    uuid="u",
                    sessionId="s",
                    message=dict(content="SECRET"),
                )
            )
            + "\n"
        )
        with patch.object(unified, "record", side_effect=ValueError("failure")):
            with self.assertRaises(ValueError):
                work.capture_file(self.db, "claude", str(path), True)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM prompts").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM native_offsets").fetchone()[0], 0)

    def test_human_origin_survives_unenriched_native_replay_in_ledger(self):
        base = dict(
            t="prompt",
            uid="u",
            ts=20,
            tool="pi",
            session="s",
            project="r",
            chars=4,
            words=1,
            est_tokens=1,
        )
        self.ingest(
            dict(base, origin="human", provenance="pi_lifecycle"),
            dict(base, origin="unknown", provenance="native_user_message"),
        )
        self.assertEqual(self.db.execute("SELECT origin FROM prompts").fetchone()[0], "human")
        self.assertEqual(
            json.loads(
                self.db.execute("SELECT payload FROM observations WHERE kind='prompt'").fetchone()[
                    0
                ]
            )["origin"],
            "human",
        )

    def test_pi_lifecycle_retains_human_sizes_without_text(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node required")
        state = self.home / "state"
        module = (Path(collect.__file__).parent / "hooks/pi.mjs").as_uri()
        code = """const {install}=await import(process.argv[1]);const h=new Map();install({on:(k,v)=>h.set(k,v)},{stateDir:process.argv[2]});
  h.get('input')({text:'SECRET HUMAN INPUT',source:'interactive'});
  const ctx={sessionManager:{getHeader:()=>({timestamp:'2026-10-17T00:00:00Z'}),getSessionId:()=> 's',getCwd:()=>'/code/r',getEntries:()=>[{type:'message',id:'u',timestamp:'2026-10-17T00:00:01Z',message:{role:'user',content:'SECRET HUMAN INPUT'}}]}};
  h.get('turn_end')({},ctx);"""
        subprocess.run(
            [node, "--input-type=module", "-e", code, module, str(state)],
            check=True,
            capture_output=True,
        )
        path = next((state / "pi-snapshots").glob("*.jsonl"))
        self.assertNotIn("SECRET", path.read_text())
        out = io.StringIO()
        with patch.dict(os.environ, {"ERG_STATE_DIR": str(state)}), patch.object(
            collect, "HOME", str(self.home)
        ), contextlib.redirect_stdout(out):
            collect.collect_pi(0)
        self.ingest(*map(json.loads, out.getvalue().splitlines()))
        r = self.db.execute("SELECT chars,words,origin FROM prompts").fetchone()
        self.assertEqual(r, (18, 3, "human"))

    def test_retracted_operator_link_returns_usage_to_unknown(self):
        self.ingest(self.call())
        work.bind(self.db, "codex", "s", URL)
        self.assertEqual(work.effectiveness(self.db)["attributed_calls"], 1)
        self.assertEqual(work.unlink(self.db, "codex", "s", URL), 1)
        self.assertEqual(work.effectiveness(self.db)["unattributed_calls"], 1)

    def test_pr_refresh_uses_final_filtered_diff_and_verified_merge(self):
        detail = dict(
            html_url=URL,
            number=1,
            created_at="2026-10-17T00:00:00Z",
            closed_at="2026-10-17T01:00:00Z",
            merged_at="2026-10-17T01:00:00Z",
            state="closed",
            head=dict(ref="feature", sha="abc"),
            changed_files=3,
        )
        responses = [
            [detail],
            [[dict(sha="abc")]],
            [
                [
                    dict(filename="src/a.py", additions=12, deletions=4),
                    dict(filename="README.md", additions=100, deletions=0),
                    dict(filename="package-lock.json", additions=1000, deletions=0),
                ]
            ],
            detail,
        ]
        with patch.object(work, "gh_json", side_effect=responses):
            r = work.refresh(self.db, ["github.com/acme/r"])
        self.assertEqual(r["prs"], 1)
        pr = work.effectiveness(self.db)["prs"][0]
        self.assertEqual((pr["state"], pr["added_code"], pr["removed_code"]), ("merged", 12, 4))
        self.assertIsNone(pr["cost_usd"])
        self.assertEqual(pr["usage_status"], "unknown")

    def test_pr_refresh_rejects_unbounded_limits_before_remote_calls(self):
        with patch.object(work, "gh_json") as remote:
            for limit in (0, -1, 101, True, 1.5):
                with self.assertRaisesRegex(ValueError, "between 1 and 100"):
                    work.refresh(self.db, ["github.com/acme/r"], limit=limit)
            remote.assert_not_called()

    def test_pr_metadata_page_reuses_known_prs_without_expanding_recent_scope(self):
        updated = "2026-10-17T00:00:00Z"
        stamp = collect.parse_ts(updated)
        for number in (1, 99):
            url = "https://github.com/acme/r/pull/" + str(number)
            self.ingest(
                dict(
                    t="pull_request",
                    uid=url,
                    ts=100,
                    url=url,
                    repo="github.com/acme/r",
                    number=number,
                    state="open",
                    remote_updated_ts=stamp,
                )
            )
        listed = [dict(number=n, updated_at=updated) for n in (1, 2, 99)]

        def fetch(repo, endpoint, paginate=False):
            if "/pulls?" not in endpoint:
                raise AssertionError("redundant individual PR fetch")
            count = int(endpoint.rsplit("per_page=", 1)[1])
            return listed[:count]

        with patch.object(work, "gh_json", side_effect=fetch) as remote:
            result = work.refresh(self.db, ["github.com/acme/r"], limit=1)
        self.assertEqual(result["errors"], [])
        self.assertEqual(remote.call_count, 1)
        self.assertEqual(
            self.db.execute("SELECT number FROM pull_requests ORDER BY number").fetchall(),
            [(1,), (99,)],
        )

    def test_known_pr_outside_metadata_page_keeps_individual_fallback(self):
        updated = "2026-10-17T00:00:00Z"
        stamp = collect.parse_ts(updated)
        for number in (1, 200):
            url = "https://github.com/acme/r/pull/" + str(number)
            self.ingest(
                dict(
                    t="pull_request",
                    uid=url,
                    ts=100,
                    url=url,
                    repo="github.com/acme/r",
                    number=number,
                    state="open",
                    remote_updated_ts=stamp,
                )
            )

        def fetch(repo, endpoint, paginate=False):
            if "/pulls?" in endpoint:
                return [dict(number=1, updated_at=updated)]
            if endpoint.endswith("/pulls/200"):
                return dict(number=200, updated_at=updated)
            raise AssertionError("unexpected changed diff fetch")

        with patch.object(work, "gh_json", side_effect=fetch) as remote:
            result = work.refresh(self.db, ["github.com/acme/r"], limit=1)
        self.assertEqual(result["errors"], [])
        self.assertEqual(remote.call_count, 2)
        self.assertEqual(result["prs"], 0)

    def test_claude_sidechain_inputs_not_counted_as_human(self):
        p = self.home / "child.jsonl"
        p.write_text(
            json.dumps(
                dict(
                    type="user",
                    timestamp="2026-10-17T00:00:00Z",
                    isSidechain=True,
                    sessionId="parent",
                    agentId="child",
                    uuid="u",
                    message=dict(content="automated task"),
                )
            )
            + "\n"
        )
        work.capture_file(self.db, "claude", str(p), True)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM prompts").fetchone()[0], 0)

    @unittest.skipUnless(shutil.which("zstd"), "zstd needed for archived context fixture")
    def test_compressed_codex_context_inputs_replay_once(self):
        events = [
            dict(
                type="session_meta",
                timestamp="2026-10-17T00:00:00Z",
                payload=dict(
                    id="s",
                    git=dict(
                        branch="feature",
                        repository_url="https://github.com/acme/r",
                        commit_hash="abc",
                    ),
                ),
            ),
            dict(
                type="response_item",
                timestamp="2026-10-17T00:00:01Z",
                payload=dict(
                    type="message",
                    role="user",
                    internal_chat_message_metadata_passthrough=dict(
                        content_item_kinds=["user.text"]
                    ),
                    content=[dict(text="hello")],
                ),
            ),
        ]
        data = "".join(json.dumps(x) + "\n" for x in events).encode()
        packed = subprocess.run(
            ["zstd", "-q", "-c"], input=data, capture_output=True, check=True
        ).stdout
        path = self.home / "a.jsonl.zst"
        path.write_bytes(packed)
        work.capture_file(self.db, "codex", str(path), True)
        work.capture_file(self.db, "codex", str(path), True)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM prompts").fetchone()[0], 1)

    def test_codex_injected_context_and_replayed_history_are_not_human_inputs(self):
        path = self.home / "context.jsonl"
        records = [
            dict(type="session_meta", timestamp="2026-10-17T00:00:10Z", payload=dict(id="s"))
        ]
        for kind, created in [
            ("agents_md.instructions", 1792195211),
            ("user.text", 1792195000),
            ("user.text", 1792195211),
        ]:
            records.append(
                dict(
                    type="response_item",
                    timestamp="2026-10-17T00:00:11Z",
                    payload=dict(
                        type="message",
                        role="user",
                        id=kind + str(created),
                        internal_chat_message_metadata_passthrough=dict(
                            content_item_kinds=[kind], create_time=created
                        ),
                        content=[dict(text="SECRET")],
                    ),
                )
            )
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        work.capture_file(self.db, "codex", str(path), True)
        self.assertEqual(
            self.db.execute("SELECT COUNT(*),MAX(origin) FROM input_messages").fetchone(),
            (1, "human"),
        )

    def test_unclassified_codex_role_counts_are_not_used_in_effectiveness(self):
        self.ingest(
            self.call(),
            dict(
                t="prompt",
                uid="legacy-role",
                ts=20,
                tool="codex",
                session="s",
                chars=2000,
                words=300,
                est_tokens=500,
                provenance="native_user_message",
                origin="unknown",
            ),
        )
        work.bind(self.db, "codex", "s", URL)
        r = work.effectiveness(self.db)
        self.assertIsNone(r["prs"][0]["observed_inputs"])
        self.assertEqual(r["unclassified_codex_role_records"], 1)

    def test_dataset_columnar_fields_match_row_widths(self):
        self.ingest(
            dict(
                t="prompt",
                uid="p",
                ts=20,
                tool="pi",
                session="s",
                chars=10,
                words=2,
                est_tokens=3,
                origin="unknown",
            )
        )
        data = store.dataset(self.db)
        for prefix in ["llm", "prompt", "edit", "commit", "metric", "session", "source"]:
            rows = data.get(prefix + "s", data.get(prefix, []))
            for row in rows:
                self.assertEqual(len(row), len(data[prefix + "_cols"]), prefix)

    def test_missing_remote_commit_remains_unattributed_and_retries_after_backoff(self):
        self.ingest(
            dict(
                t="work_evidence",
                uid="missing",
                tool="codex",
                session="s",
                repo="github.com/acme/r",
                sha="abc",
                ts=10,
                provenance="native_tool",
            )
        )
        with patch.object(
            work, "gh_json", side_effect=work.MetadataFetchError(422, "commit_not_found")
        ) as gh:
            numbers, errors = work.evidence_pulls(self.db, "github.com/acme/r", 1)
            self.assertEqual(numbers, [])
            self.assertEqual(errors, [])
            self.assertEqual(
                self.db.execute("select reason from work_lookup_gaps").fetchone()[0],
                "commit_not_found",
            )
            self.assertIsNone(self.db.execute("select * from work_lookups").fetchone())
            work.evidence_pulls(self.db, "github.com/acme/r", 1)
            self.assertEqual(gh.call_count, 1)
        self.db.execute("update work_lookup_gaps set checked_ts=0")
        self.db.commit()
        with patch.object(work, "gh_json", return_value=[[dict(html_url=URL)]]):
            numbers, errors = work.evidence_pulls(self.db, "github.com/acme/r", 1)
        self.assertEqual(numbers, [1])
        self.assertEqual(errors, [])
        self.assertEqual(self.db.execute("select count(*) from work_lookup_gaps").fetchone()[0], 0)

    def test_authentication_and_server_errors_remain_refresh_failures(self):
        self.ingest(
            dict(
                t="work_evidence",
                uid="e",
                tool="codex",
                session="s",
                repo="github.com/acme/r",
                sha="abc",
                ts=10,
                provenance="native_tool",
            )
        )
        for code in (401, 403, 429, 500, 422):
            with patch.object(
                work, "gh_json", side_effect=work.MetadataFetchError(code, "request_failed")
            ):
                numbers, errors = work.evidence_pulls(self.db, "github.com/acme/r", 1)
            self.assertEqual(numbers, [])
            self.assertEqual(len(errors), 1)
        self.assertEqual(self.db.execute("select count(*) from work_lookup_gaps").fetchone()[0], 0)

    def test_gh_failure_categories_do_not_leak_cli_output(self):
        response = subprocess.CompletedProcess(
            [],
            1,
            stdout=json.dumps([dict(status="422", message="No commit found for SHA: PRIVATE")]),
            stderr="SECRET",
        )
        with patch.object(work.subprocess, "run", return_value=response):
            with self.assertRaises(work.MetadataFetchError) as caught:
                work.gh_json("github.com/acme/r", "test")
        self.assertEqual(caught.exception.reason, "commit_not_found")
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertNotIn("SECRET", str(caught.exception))
