"""Work uses the same temporal project shares as Usage and project costs."""

import json
import unittest
from unittest.mock import patch

import test_effectiveness as fixtures

from erg import project_dataset, project_reports, projects, report_scope, server, store, work


class ScopedWork(unittest.TestCase):
    setUp = fixtures.Effectiveness.setUp
    ingest = fixtures.Effectiveness.ingest
    call = fixtures.Effectiveness.call
    pr = fixtures.Effectiveness.pr
    ctx = fixtures.Effectiveness.ctx

    def configure(self, units=600000):
        config = projects.snapshot(self.db)
        config["projects"] = [dict(project_key="p", name="Product")]
        config["bindings"] = [
            dict(
                binding_id="session",
                selectors=dict(session=dict(machine="host", tool="codex", id="s")),
                allocations=[dict(project_key="p", units=units)],
                valid_from=0,
            )
        ]
        projects.apply(self.db, config)

    def card(self):
        return dict(
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

    def test_time_project_and_input_shares_match_usage_and_costs(self):
        self.configure()
        self.pr()
        self.ingest(
            self.ctx(),
            self.call(),
            self.call(uid="outside", ts=40),
            dict(
                t="prompt",
                uid="human",
                ts=15,
                tool="codex",
                session="s",
                project="r",
                origin="human",
                provenance="codex_user_event",
            ),
            dict(
                t="prompt",
                uid="late",
                ts=40,
                tool="codex",
                session="s",
                project="r",
                origin="human",
                provenance="codex_user_event",
            ),
        )
        select = report_scope.Selection(dict(start=1, end=30, project_key="p"))
        with patch.object(work.rates, "load", return_value=self.card()):
            result = work.effectiveness(self.db, view="work", selection=select)
            usage = project_dataset.build(self.db)
            cost = project_reports.report(self.db, 1, 30, project_key="p")
        pr = result["prs"][0]
        self.assertEqual(result["total_calls"], 0.6)
        self.assertEqual(pr["calls"], 0.6)
        self.assertEqual(pr["human_inputs"], 0.6)
        self.assertEqual(pr["observed_input_events"], 1)
        self.assertEqual(pr["total_tokens"], cost["totals"]["tokens"])
        self.assertAlmostEqual(
            pr["valuation"]["known_value_low_usd"], cost["totals"]["known_value_low_usd"]
        )
        self.assertEqual(pr["valuation"]["priced_calls"], 0.6)
        self.assertEqual(result["valuation"], pr["valuation"])
        self.assertIsNone(pr["cost_usd"])
        self.assertEqual(result["report_scope"]["project_key"], "p")
        self.assertIn("Whole-PR diff", result["limitations"][-1])
        cols = usage["llm_cols"]
        rows = [dict(zip(cols, row)) for row in usage["llm"]]
        self.assertEqual(sum(row["calls"] for row in rows if row["project"] == "p"), 1.2)

    def test_requester_and_folder_scope_cannot_reassign_other_calls(self):
        self.configure(1000000)
        self.pr()
        self.ingest(
            self.ctx(),
            self.call(requester="operator"),
            self.call(uid="other", requester="different"),
        )
        with self.db:
            self.db.executemany(
                "UPDATE llm SET requester=? WHERE uid=?",
                [("operator", "call"), ("different", "other")],
            )
        scope = report_scope.Selection(
            dict(start=1, end=30, project_key="p", requester="me"), ["operator"]
        )
        first = work.effectiveness(self.db, view="work", selection=scope)
        self.assertEqual(first["total_calls"], 1)
        folder = first["folder_catalog"][0]["folder_key"]
        scoped = work.effectiveness(
            self.db,
            view="work",
            selection=report_scope.Selection(dict(start=1, end=30, folder_key=folder)),
        )
        self.assertEqual(scoped["total_calls"], 2)
        absent = work.effectiveness(
            self.db,
            view="work",
            selection=report_scope.Selection(dict(start=1, end=30, folder_key="folder:absent")),
        )
        self.assertEqual(absent["total_calls"], 0)
        with self.assertRaises(ValueError):
            work.effectiveness(self.db, selection=scope)

    def test_project_revision_invalidates_usage_and_scoped_work_caches(self):
        self.configure()
        self.pr()
        self.ingest(self.ctx(), self.call())
        reports = server.Reports()
        with patch.object(
            store,
            "open_readonly",
            side_effect=lambda: store.connect(str(self.home / "erg.db"), initialize=False),
        ), patch.object(store, "my_identities", return_value=[]):
            first = reports.data(0)
            before = json.loads(reports.encoded_scoped_work(dict(start=1, end=30, project_key="p")))
            config = projects.snapshot(self.db)
            config["bindings"][0]["allocations"][0]["units"] = 1000000
            projects.apply(self.db, config)
            second = reports.data(0)
            after = json.loads(reports.encoded_scoped_work(dict(start=1, end=30, project_key="p")))
        self.assertNotEqual(first["project_revision"], second["project_revision"])
        self.assertEqual(before["total_calls"], 0.6)
        self.assertEqual(after["total_calls"], 1)
        self.assertEqual(
            first["llm"][0][11], 0.4
        )  # unassigned remainder is a separate conserved row
        self.assertEqual(sum(row[11] for row in second["llm"]), 1)
