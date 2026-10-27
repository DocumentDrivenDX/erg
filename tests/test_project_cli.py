"""Exercise the actual CLI workflow without assigning a real user's projects."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from erg import cli, server, store


class ProjectCLI(unittest.TestCase):
    def test_create_export_preview_apply_retry_and_cache_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = str(root / "erg.db")
            original = store.connect
            readonly = store.open_readonly
            with original(path) as db:
                store.ingest(
                    db,
                    [
                        json.dumps(
                            dict(
                                t="llm",
                                uid="a",
                                ts=100,
                                tool="pi",
                                session="s",
                                model="unknown",
                                fresh_in=4,
                                out=3,
                            )
                        )
                    ],
                    machine="host",
                )

            def command(*args):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    cli.main(["projects"] + list(args))
                return json.loads(output.getvalue())

            with patch.object(store, "connect", side_effect=lambda: original(path)), patch.object(
                store, "open_readonly", side_effect=lambda: readonly(path)
            ):
                created = command("create", "--name", "A project")
                key = created["project_key"]
                config = command("export")
                self.assertEqual(config["revision"], 1)
                config["bindings"] = [
                    dict(
                        binding_id="fixture",
                        selectors=dict(session=dict(machine="host", tool="pi", id="s")),
                        allocations=[dict(project_key=key, units=1000000)],
                        valid_from=0,
                    )
                ]
                file = root / "projects.json"
                file.write_text(json.dumps(config))
                reporter = server.Reports()
                before = reporter.costs(start=0, end=200)
                preview = command("preview", "--file", str(file), "--end", "200")
                self.assertFalse(preview["applied"])
                self.assertEqual(preview["after"][0]["project_key"], key)
                self.assertEqual(command("list")["bindings"], [])
                applied = command("import", "--file", str(file), "--apply")
                self.assertTrue(applied["changed"])
                self.assertFalse(command("import", "--file", str(file), "--apply")["changed"])
                after = reporter.costs(start=0, end=200)
                self.assertEqual(before["totals"], after["totals"])
                self.assertEqual(after["projects"][0]["project_key"], key)
                self.assertNotEqual(before["revision"], after["revision"])
