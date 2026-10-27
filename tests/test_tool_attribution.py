import json
import subprocess
import unittest
from unittest.mock import patch

import test_effectiveness as fixtures

URL = fixtures.URL
from erg import work, work_tools  # noqa: E402 - checkout/bootstrap imports follow path and schema setup


class ToolAttribution(unittest.TestCase):
    ingest = fixtures.Effectiveness.ingest
    call = fixtures.Effectiveness.call
    pr = fixtures.Effectiveness.pr
    ctx = fixtures.Effectiveness.ctx

    def setUp(self):
        fixtures.Effectiveness.setUp(self)
        self.repo = self.home / "repo"
        self.repo.mkdir()
        for args in [
            ("init", "-b", "feature"),
            ("config", "user.email", "test@example.com"),
            ("config", "user.name", "Test"),
            ("remote", "add", "origin", "https://github.com/acme/r.git"),
            ("commit", "--allow-empty", "-m", "fixture"),
        ]:
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)
        self.sha = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def evidence(self, url=URL, scope="scope", sha="", ts=30, repo="github.com/acme/r"):
        self.ingest(
            dict(
                t="work_evidence",
                capture_version=5,
                uid="e" + url + sha,
                ts=ts,
                tool="codex",
                session="s",
                repo=repo,
                scope_id=scope,
                task_id="",
                sha=sha,
                pr_url="" if sha else url,
                provenance="tool_commit" if sha else "tool_pr_create",
            )
        )

    def scoped(self, scope="scope", ts=1, branch="feature"):
        return work.context_event(
            "codex",
            "s",
            ts,
            str(self.repo),
            repo="github.com/acme/r",
            branch=branch,
            scope_id=scope,
        )

    def test_tool_commit_links_pre_pr_work_by_verified_membership(self):
        self.pr()
        self.ingest(
            dict(t="pr_commit", uid="real", ts=100, pr_url=URL, sha=self.sha),
            self.scoped(),
            self.call(ts=2),
        )
        self.evidence(sha=self.sha)
        r = work.effectiveness(self.db)
        self.assertEqual(r["attributed_calls"], 1)
        self.assertEqual(r["pre_pr_calls"], 1)
        self.assertEqual(r["prs"][0]["evidence"], ["tool_commit"])

    def test_created_pr_links_precreation_but_not_other_task_or_after_merge(self):
        self.pr()
        self.ingest(
            self.scoped(),
            self.call(ts=2),
            self.scoped("other", ts=40),
            self.call(uid="other", ts=45),
            self.call(uid="late", ts=60),
        )
        self.evidence()
        self.assertEqual(work.effectiveness(self.db)["attributed_calls"], 1)

    def test_conflicting_prs_in_one_scope_stay_unknown(self):
        self.pr()
        self.pr("https://github.com/acme/r/pull/2")
        self.ingest(self.scoped(), self.call(ts=2))
        self.evidence()
        self.evidence("https://github.com/acme/r/pull/2")
        self.assertEqual(work.effectiveness(self.db)["unattributed_reasons"], {"ambiguous": 1})

    def test_unknown_membership_wrong_repo_and_postclose_evidence_do_not_link(self):
        self.pr()
        self.ingest(self.scoped(), self.call(ts=2))
        self.evidence(sha=self.sha)
        self.evidence(repo="github.com/other/r", ts=60)
        self.assertEqual(work.effectiveness(self.db)["attributed_calls"], 0)

    def _capture(self, tool, command, output, failed=False, wrapper=False):
        path = self.home / (tool + ".jsonl")
        start = "2026-10-17T00:00:00Z"
        if tool == "claude":
            common = dict(timestamp=start, sessionId="s", cwd=str(self.repo), gitBranch="feature")
            records = [
                dict(
                    common,
                    type="assistant",
                    message=dict(
                        content=[
                            dict(type="tool_use", id="id", name="Bash", input=dict(command=command))
                        ]
                    ),
                ),
                dict(
                    common,
                    type="user",
                    message=dict(
                        content=[
                            dict(
                                type="tool_result",
                                tool_use_id="id",
                                content=output,
                                is_error=failed,
                            )
                        ]
                    ),
                ),
            ]
        else:
            records = [
                dict(
                    type="session_meta",
                    timestamp=start,
                    payload=dict(
                        id="s",
                        cwd=str(self.repo),
                        git=dict(repository_url="https://github.com/acme/r", branch="feature"),
                    ),
                )
            ]
            name = "exec" if wrapper else "exec_command"
            args = (
                "text(await tools.exec_command("
                + json.dumps(dict(cmd=command, workdir=str(self.repo)))
                + "));"
                if wrapper
                else json.dumps(dict(cmd=command, workdir=str(self.repo)))
            )
            records += [
                dict(
                    type="response_item",
                    timestamp=start,
                    payload=dict(
                        type="custom_tool_call" if wrapper else "function_call",
                        call_id="id",
                        name=name,
                        input=args,
                        arguments=args,
                    ),
                ),
                dict(
                    type="response_item",
                    timestamp=start,
                    payload=dict(
                        type="custom_tool_call_output" if wrapper else "function_call_output",
                        call_id="id",
                        output=json.dumps(dict(output=output, exit_code=1 if failed else 0)),
                    ),
                ),
            ]
        path.write_text(json.dumps(records[0]) + "\n")
        work.capture_file(self.db, tool, str(path), True)
        with path.open("a") as f:
            for r in records[1:]:
                f.write(json.dumps(r) + "\n")
        work.capture_file(self.db, tool, str(path), True)
        work.capture_file(self.db, tool, str(path), True)
        return self.db.execute("SELECT provenance,sha,pr_url FROM work_evidence").fetchall()

    def test_claude_paired_commit_result_resolves_local_git_and_replays_once(self):
        r = self._capture(
            "claude",
            'git commit -m "SECRET MESSAGE"',
            "[feature " + self.sha[:8] + "] SECRET BODY\n",
        )
        self.assertEqual(r, [("tool_commit", self.sha, "")])
        self.assertNotIn(
            "SECRET", str(self.db.execute("SELECT payload FROM observations").fetchall())
        )
        self.assertNotIn(
            "SECRET", str(self.db.execute("SELECT context FROM native_offsets").fetchall())
        )

    def test_codex_wrapped_pr_creation_paired_across_tail_restart(self):
        self.assertEqual(
            self._capture("codex", 'gh pr create --title "SECRET"', URL, wrapper=True),
            [("tool_pr_create", "", URL)],
        )

    def test_failed_commit_and_readonly_pr_view_are_not_creation_evidence(self):
        self.assertEqual(
            self._capture(
                "codex", "git commit -m fixture", "[feature " + self.sha[:8] + "] fixture\n", True
            ),
            [],
        )
        self.assertEqual(self._capture("claude", "gh pr view", URL), [])

    def test_unpaired_output_and_fabricated_git_object_do_not_link(self):
        self.assertEqual(
            self._capture("claude", "git commit -m fixture", "[feature deadbeef] fixture\n"), []
        )

    def test_cross_repository_and_multi_command_wrappers_are_rejected(self):
        state = dict(cwd=str(self.repo), repo="github.com/acme/r")
        self.assertIsNone(
            work_tools.invocation(
                "exec", 'await tools.exec_command({cmd:"git commit",workdir:"/missing"});', state
            )
        )
        self.assertIsNone(
            work_tools.invocation(
                "exec",
                'await tools.exec_command({cmd:"git commit"});await tools.exec_command({cmd:"gh pr create"});',
                state,
            )
        )

    def test_scoped_context_wins_over_legacy_context_at_same_timestamp(self):
        self.pr()
        self.ingest(self.ctx(), dict(self.scoped(ts=10), head_sha="abc"), self.call())
        self.evidence()
        self.assertEqual(work.effectiveness(self.db)["attributed_calls"], 1)

    def test_obsolete_force_push_membership_does_not_allocate(self):
        self.pr()
        self.ingest(
            dict(
                t="pull_request",
                uid=URL,
                ts=101,
                url=URL,
                repo="github.com/acme/r",
                number=1,
                state="open",
                created_ts=5,
                branch="feature",
                head_sha="new",
            )
        )
        self.ingest(self.ctx(), self.call())
        self.assertEqual(work.effectiveness(self.db)["attributed_calls"], 0)

    def test_injected_tool_history_is_not_creation_evidence(self):
        state = dict(
            cwd=str(self.repo),
            repo="github.com/acme/r",
            session="s",
            started=100,
            _scope_id="scope",
        )
        payload = dict(
            type="function_call",
            call_id="old",
            name="exec_command",
            arguments=json.dumps(dict(cmd="gh pr create")),
            internal_chat_message_metadata_passthrough=dict(create_time=1),
        )
        self.assertEqual(
            work_tools.observe("codex", dict(type="response_item"), payload, state, 100), []
        )
        self.assertFalse(state.get("_tools"))

    def test_multiline_commands_and_async_result_keep_sanitized_intent(self):
        state = dict(cwd=str(self.repo), repo="github.com/acme/r", session="s", _scope_id="scope")
        hint = work_tools.invocation(
            "exec_command", dict(cmd="git add .\ngit commit -m SECRET"), state
        )
        self.assertEqual(hint["kinds"], ["commit"])
        state["_shells"] = {"123": dict(hint, scope_id="scope", session="s", task_id="")}
        args = json.dumps(dict(session_id=123))
        work_tools.observe(
            "codex",
            dict(type="response_item"),
            dict(type="function_call", call_id="poll", name="write_stdin", arguments=args),
            state,
            20,
        )
        result = work_tools.observe(
            "codex",
            dict(type="response_item"),
            dict(
                type="function_call_output",
                call_id="poll",
                output=json.dumps(
                    dict(exit_code=0, output="[feature " + self.sha[:8] + "] SECRET\n")
                ),
            ),
            state,
            21,
        )
        self.assertEqual(result[0]["sha"], self.sha)
        self.assertNotIn("SECRET", json.dumps(state))

    def test_evidence_pr_discovery_is_scoped_and_bounded(self):
        self.evidence(sha=self.sha)
        other = "https://github.com/elsewhere/r/pull/9"
        with patch.object(
            work, "gh_json", return_value=[[dict(html_url=URL), dict(html_url=other)]]
        ) as gh:
            numbers, errors = work.evidence_pulls(self.db, "github.com/acme/r", 1)
            self.assertEqual(numbers, [1])
            self.assertFalse(errors)
            self.assertIn("/commits/" + self.sha + "/pulls", gh.call_args.args[1])
            work.evidence_pulls(self.db, "github.com/acme/r", 1)
            self.assertEqual(gh.call_count, 1)

    def test_structured_exec_results_with_headers_are_unwrapped(self):
        wrapped = "Script completed\nOutput:\n" + json.dumps(
            dict(exit_code=0, output="[feature " + self.sha[:8] + "] fixture\n")
        )
        self.assertEqual(work_tools._texts(wrapped), ["[feature " + self.sha[:8] + "] fixture\n"])
        self.assertEqual(
            work_tools._texts(
                "Process exited with code 1\nOutput:\n[feature " + self.sha[:8] + "] fixture\n"
            ),
            [],
        )

    def test_checkout_starts_new_scope_without_reassigning_prior_work(self):
        state = dict(
            cwd=str(self.repo),
            repo="github.com/acme/r",
            session="s",
            branch="main",
            _scope_id="old",
        )
        work_tools.observe(
            "codex",
            dict(type="response_item"),
            dict(
                type="function_call",
                call_id="switch",
                name="exec_command",
                arguments=json.dumps(dict(cmd="git switch feature")),
            ),
            state,
            20,
        )
        rows = work_tools.observe(
            "codex",
            dict(type="response_item"),
            dict(
                type="function_call_output",
                call_id="switch",
                output="Switched to branch 'feature'\n",
            ),
            state,
            21,
        )
        self.assertEqual(rows[0]["branch"], "feature")
        self.assertNotEqual(rows[0]["scope_id"], "old")
        self.assertEqual(rows[0]["scope_id"], work.scope_context(state, 22))

    def test_different_worktree_of_same_repository_is_not_the_native_scope(self):
        other = self.home / "other"
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "add", "-b", "other", str(other)],
            check=True,
            capture_output=True,
        )
        self.assertIsNotNone(
            work_tools.invocation(
                "exec_command",
                dict(cmd="git commit", workdir=str(other)),
                dict(cwd=str(self.repo), repo="github.com/acme/r"),
            )
        )
        subdir = self.repo / "src"
        subdir.mkdir()
        self.assertIsNotNone(
            work_tools.invocation(
                "exec_command",
                dict(cmd="git commit", workdir=str(subdir)),
                dict(cwd=str(self.repo)),
            )
        )

    def test_deleted_native_worktree_uses_same_origin_objects_without_current_head_guess(self):
        missing = str(self.home / "deleted")
        state = dict(cwd=missing, repo="github.com/acme/r")
        with patch.object(work_tools, "object_repository", return_value=[str(self.repo)]):
            hint = work_tools.invocation("exec_command", dict(cmd="git commit -m fixture"), state)
        self.assertEqual(hint["repo"], "github.com/acme/r")
        self.assertEqual(hint["object_cwd"], str(self.repo))
        self.assertEqual(hint["cwd"], missing)

    def test_wrapped_async_result_keeps_executor_session_correlation(self):
        state = dict(cwd=str(self.repo), repo="github.com/acme/r", session="s", _scope_id="scope")
        work_tools.observe(
            "codex",
            dict(type="response_item"),
            dict(
                type="custom_tool_call",
                call_id="a",
                name="exec",
                input='text(await tools.exec_command({cmd:"git commit -m fixture"}));',
            ),
            state,
            20,
        )
        output = "Script completed\nOutput:\n" + json.dumps(
            dict(session_id=77, exit_code=None, output="")
        )
        work_tools.observe(
            "codex",
            dict(type="response_item"),
            dict(type="custom_tool_call_output", call_id="a", output=output),
            state,
            21,
        )
        self.assertIn("77", state["_shells"])

    def test_explicit_tool_workdir_starts_scope_without_claiming_earlier_context(self):
        other = self.home / "other"
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "add", "-b", "other", str(other)],
            check=True,
            capture_output=True,
        )
        state = dict(
            cwd=str(self.repo),
            _native_cwd=str(self.repo),
            repo="github.com/acme/r",
            session="s",
            _scope_id="old",
        )
        rows = work_tools.observe(
            "codex",
            dict(type="response_item"),
            dict(
                type="function_call",
                call_id="read",
                name="exec_command",
                arguments=json.dumps(dict(cmd="git status --short", workdir=str(other))),
            ),
            state,
            10,
        )
        self.assertEqual(rows[0]["provenance"], "tool_workdir")
        self.assertNotEqual(rows[0]["scope_id"], "old")
        self.assertEqual(state["_native_cwd"], str(self.repo))

    def test_heredoc_contents_cannot_be_misread_as_executed_git_commands(self):
        self.assertIsNone(
            work_tools.invocation(
                "exec_command",
                dict(cmd="cat <<'EOF'\ngit commit -m fake\nEOF"),
                dict(cwd=str(self.repo), repo="github.com/acme/r"),
            )
        )

    def test_recorded_scope_does_not_require_existing_directory_or_current_origin(self):
        missing = str(self.home / "removed-worktree")
        state = dict(
            cwd=str(self.repo), _native_cwd=str(self.repo), repo="github.com/acme/r", session="s"
        )
        with patch.object(
            work, "git_context", side_effect=AssertionError("current origin used as history")
        ):
            hint = work_tools.invocation(
                "exec_command",
                dict(cmd="git status --short", workdir=missing),
                state,
                repo_context=lambda cwd: dict(repo="github.com/acme/r"),
            )
            self.assertEqual(
                (hint["scope_cwd"], hint["repo"], hint["kinds"]), (missing, "github.com/acme/r", [])
            )
            self.assertIsNone(
                work_tools.invocation(
                    "exec_command",
                    dict(cmd="git status --short", workdir=str(self.repo)),
                    state,
                    repo_context=lambda cwd: {},
                )
            )
            self.assertIsNone(
                work_tools.invocation(
                    "exec_command",
                    dict(cmd="git status --short", workdir=missing),
                    state,
                    repo_context=lambda cwd: dict(repo="github.com/other/r"),
                )
            )

    def test_recorded_scope_with_ambiguous_object_locations_does_not_prove_commit(self):
        missing = str(self.home / "removed-worktree")
        state = dict(
            cwd=missing,
            _native_cwd=missing,
            repo="github.com/acme/r",
            session="s",
            _scope_id="scope",
        )
        with patch.object(
            work_tools,
            "object_repository",
            return_value=[str(self.repo), str(self.home / "another")],
        ):
            rows = work_tools.observe(
                "codex",
                dict(type="response_item"),
                dict(
                    type="function_call",
                    call_id="commit",
                    name="exec_command",
                    arguments=json.dumps(dict(cmd="git commit -m work")),
                ),
                state,
                20,
                repo_context=lambda cwd: dict(repo="github.com/acme/r"),
            )
        self.assertFalse(rows)
        rows = work_tools.observe(
            "codex",
            dict(type="response_item"),
            dict(
                type="function_call_output",
                call_id="commit",
                output="[feature " + self.sha[:8] + "] work\n",
            ),
            state,
            21,
            repo_context=lambda cwd: dict(repo="github.com/acme/r"),
        )
        self.assertFalse(rows)
