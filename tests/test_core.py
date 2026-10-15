import json, os, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from erg import collect, store


class Collector(unittest.TestCase):
    def test_project_of(self):
        h = collect.HOME
        self.assertEqual(collect.project_of(h + "/Projects/foo/.claude/worktrees/x"), "foo")
        self.assertEqual(collect.project_of(h + "/.codex/worktrees/ab12/bar"), "bar")
        self.assertEqual(collect.project_of(h + "/Projects/baz/sub"), "baz")
        self.assertEqual(collect.project_of(None), "(unknown)")

    def test_patch_stats(self):
        patch = "*** Begin Patch\n*** Update File: a.py\n@@\n-old\n+new1\n+new2\n*** Add File: d.md\n+x\n*** End Patch\n"
        self.assertEqual(list(collect.patch_stats(patch)), [("a.py", 2, 1), ("d.md", 1, 0)])

    def test_doc_classification(self):
        self.assertTrue(collect.is_doc("docs/x.md"))
        self.assertFalse(collect.is_doc("src/x.py"))
        self.assertTrue(collect.SKIP_FILE.search("package-lock.json"))
        self.assertTrue(collect.SKIP_FILE.search("data/big.csv"))
        self.assertFalse(collect.SKIP_FILE.search("src/main.py"))

    def test_parse_ts(self):
        self.assertEqual(collect.parse_ts("1970-01-02T00:00:00.000Z"), 86400)


class Store(unittest.TestCase):
    def setUp(self):
        self.db = store.connect(os.path.join(tempfile.mkdtemp(), "t.db"))

    def llm(self, uid, out, ts=3600, tool="claude"):
        return json.dumps({"t": "llm", "uid": uid, "ts": ts, "tool": tool, "session": "s", "project": "p", "model": "m",
                           "kind": "main", "client": "", "fresh_in": 1, "cache_read": 2, "cache_write": 3, "out": out, "reasoning": 0})

    def test_streamed_duplicate_keeps_most_complete(self):
        store.ingest(self.db, [self.llm("a", 5)], machine="m1")
        store.ingest(self.db, [self.llm("a", 50)], machine="m1")
        store.ingest(self.db, [self.llm("a", 7)], machine="m1")
        self.assertEqual(self.db.execute("SELECT COUNT(*), MAX(out) FROM llm").fetchone(), (1, 50))

    def test_machines_are_separate_keys(self):
        store.ingest(self.db, [self.llm("a", 5)], machine="m1")
        store.ingest(self.db, [self.llm("a", 5)], machine="m2")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM llm").fetchone()[0], 2)

    def test_gateway_duplicate_marked(self):
        store.ingest(self.db, [self.llm("a", 100, ts=1000)], machine="local")
        gw = {"uid": "g:1", "ts": 1060, "tool": "gateway", "session": "", "requester": "u", "project": "p", "model": "m",
              "kind": "main", "client": "claude-code", "fresh_in": 0, "cache_read": 2, "cache_write": 0, "out": 100, "reasoning": 0}
        other = dict(gw, uid="g:2", out=999)
        self.assertEqual(store.ingest_gateway(self.db, [gw, other], "databricks"), 1)
        ds = store.dataset(self.db)
        self.assertEqual(sum(r[9] for r in ds["llm"]), 100 + 999)  # duplicate excluded

    def test_rollup_prefers_finest_covering_tier_and_priority(self):
        ev = [{"t": "metric", "src": "istat", "m": "cpu", "ts": 3600 * 5 + i * 60, "iv": 60, "v": 10.0, "pk": None} for i in range(60)]
        ev += [{"t": "metric", "src": "istat", "m": "cpu", "ts": 3600 * 5, "iv": 3600, "v": 90.0, "pk": None},
               {"t": "metric", "src": "sampler", "m": "cpu", "ts": 3600 * 5, "iv": 30, "v": 1.0, "pk": None}]
        store.ingest(self.db, [json.dumps(e) for e in ev], machine="m")
        rows = [r for r in store.rollup_metrics(self.db) if r[2] == "cpu"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0][3], rows[0][5]), (10.0, "istat"))


if __name__ == "__main__":
    unittest.main()
