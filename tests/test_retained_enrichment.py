"""Periodic enrichment must reuse durable captures without dropping legacy sources."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from erg import collect,history,history_projection,store

class RetainedEnrichment(unittest.TestCase):
 def setUp(self):
  temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name);self.path=self.root/'ledger'
  for name,value in [('HOME',str(self.root)),('STATE_DIR',str(self.root/'state')),('machine_name',lambda:'host')]:
   changed=patch.object(collect,name,value);changed.start();self.addCleanup(changed.stop)
  self.db=store.connect(str(self.path));self.addCleanup(self.db.close);history.enroll(self.db)
 def test_cli_reuses_captured_context_and_keeps_other_adapters(self):
  cwd=str(self.root/'external-project')
  with self.db:
   history.put(self.db,'codex','s','project',100,'session_meta',dict(type='session_meta',payload=dict(id='s',cwd=cwd)),'header')
   self.db.execute("INSERT INTO history_sources(path,tool,errors,state) VALUES('missing/native','claude',2,'source_error')")
  output=io.StringIO()
  with patch.object(collect.sys,'argv',['collect.py','collect','--retained','--retained-database',str(self.path),'--no-metrics','--roots','']),contextlib.redirect_stdout(output),contextlib.ExitStack() as stack:
   for name in ('collect_claude','collect_codex','collect_pi','collect_omnigent'):
    stack.enter_context(patch.object(collect,name,side_effect=AssertionError('reopened covered native history')))
   copilot=stack.enter_context(patch.object(collect,'collect_copilot'));opencode=stack.enter_context(patch.object(collect,'collect_opencode'))
   stack.enter_context(patch.object(collect,'emit_perf'))
   find=stack.enter_context(patch.object(collect,'find_repos',return_value=[cwd]));git=stack.enter_context(patch.object(collect,'collect_git'))
   collect.main()
   copilot.assert_called_once();opencode.assert_called_once();find.assert_called_once_with({cwd},[])
   self.assertEqual(git.call_args.args[0],[cwd])
  source=next(row for row in map(json.loads,output.getvalue().splitlines()) if row.get('t')=='source' and row['tool']=='claude')
  self.assertEqual((source['files'],source['errors'],source['status']),(1,2,'partial'))
  self.assertIn('retained checkpoints',source['detail'])
 def test_counter_only_usage_stays_retained_without_fabricated_requests(self):
  records=[dict(type='session_meta',payload=dict(id='s',cwd='/code/project')),
           dict(type='turn_context',payload=dict(model='m')),
           dict(type='event_msg',payload=dict(type='token_count',info=dict(total_token_usage=dict(input_tokens=50,output_tokens=30,total_tokens=80),last_token_usage=dict(input_tokens=40,output_tokens=20))))]
  with self.db:
   for i,record in enumerate(records):history.put(self.db,'codex','s','project',100+i,record['type'],record,str(i))
  history_projection.run(self.db)
  with contextlib.redirect_stdout(io.StringIO()):collect.collect_retained(str(self.path))
  self.assertEqual(history_projection.latest_counter(self.db,'codex','s')['total_tokens'],80)
  self.assertEqual(self.db.execute('SELECT count(*) FROM calls').fetchone()[0],0)
  self.assertEqual(self.db.execute('SELECT count(*) FROM history_events WHERE body IS NOT NULL').fetchone()[0],3)
 def test_retained_mode_requires_both_content_opt_in_and_enrolled_ledger(self):
  config=self.root/'state/capture.json';settings=json.loads(config.read_text());settings['content']=False;config.write_text(json.dumps(settings))
  with self.assertRaisesRegex(RuntimeError,'content capture'):collect.collect_retained(str(self.path))
  settings['content']=True;config.write_text(json.dumps(settings))
  with self.db:self.db.execute("DELETE FROM history_config WHERE key='enrolled_at'")
  with self.assertRaisesRegex(RuntimeError,'not enrolled'):collect.collect_retained(str(self.path))
 def test_unenrolled_legacy_cli_keeps_original_collectors(self):
  with patch.object(collect.sys,'argv',['collect.py','collect','--no-metrics','--no-git']),contextlib.redirect_stdout(io.StringIO()),contextlib.ExitStack() as stack:
   funcs=[stack.enter_context(patch.object(collect,name)) for name in ('collect_claude','collect_codex','collect_pi','collect_omnigent','collect_copilot','collect_opencode')]
   retained=stack.enter_context(patch.object(collect,'collect_retained',side_effect=AssertionError('unexpected retained mode')))
   stack.enter_context(patch.object(collect,'emit_perf'));collect.main();retained.assert_not_called()
   for fn in funcs:fn.assert_called_once()
