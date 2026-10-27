"""Edit-only upgrade replay preserves evidence, retention pins and live priority."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from erg import collect,edit_projection,history,history_projection,store,work_projection

class EditReplay(unittest.TestCase):
 def setUp(self):
  temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
  for name,value in [('HOME',str(self.root)),('STATE_DIR',str(self.root/'state')),('machine_name',lambda:'host')]:
   changed=patch.object(collect,name,value);changed.start();self.addCleanup(changed.stop)
  self.db=store.connect(str(self.root/'ledger'));self.addCleanup(self.db.close)
 def body(self,ident='write',text='one\ntwo\n'):
  return dict(type='assistant',sessionId='s',message=dict(id='response-'+ident,model='m',role='assistant',usage=dict(input_tokens=5,output_tokens=3),content=[dict(type='tool_use',name='Write',id=ident,input=dict(file_path='a.py',content=text))]))
 def legacy(self,body,ident='record'):
  with self.db:uid=history.put(self.db,'claude','s','project',100,'assistant',body,ident,role='assistant',source='/gone/native.jsonl')[0]
  history_projection.run(self.db)
  with self.db:
   self.db.execute('UPDATE history_projection_state SET version=1 WHERE event_id=?',(uid,))
   self.db.execute('DELETE FROM edits')
  return uid
 def seed(self):
  with self.db:self.db.execute("DELETE FROM history_config WHERE key='edit_projection_v2_seed'")
  history.initialize(self.db);self.db.commit()
 def test_replay_preserves_usage_identity_state_and_releases_expiration_pin(self):
  uid=self.legacy(self.body());self.seed()
  before=self.db.execute("SELECT event_id,payload,seq FROM observations WHERE kind<>'edit' ORDER BY event_id").fetchall()
  context=self.db.execute('SELECT * FROM history_projection_context').fetchall()
  self.assertEqual(history.expire(self.db,200,True)['protected_events'],1)
  with patch.object(collect,'log_paths',side_effect=AssertionError('source backfill')),patch.object(collect,'transcript',side_effect=AssertionError('source reopened')):
   self.assertEqual(edit_projection.run(self.db,budget=1)['processed'],1)
  self.assertEqual(self.db.execute('SELECT uid,added FROM edits').fetchone(),('c:write:0',2))
  self.assertEqual(self.db.execute("SELECT event_id,payload,seq FROM observations WHERE kind<>'edit' ORDER BY event_id").fetchall(),before)
  self.assertEqual(self.db.execute('SELECT * FROM history_projection_context').fetchall(),context)
  self.assertEqual(history.expire(self.db,200,True)['protected_events'],1)
  work_projection.run(self.db,budget=1)
  self.assertEqual(history.expire(self.db,200,True)['expired_events'],1)
  history.initialize(self.db);self.db.commit()
  self.assertEqual(edit_projection.run(self.db)['pending'],0)
 def test_changed_live_body_is_owned_by_main_projection(self):
  uid=self.legacy(self.body());self.seed()
  with self.db:history.put(self.db,'claude','s','project',100,'assistant',self.body(text='a\nb\nc\n'),'record',role='assistant')
  self.assertEqual(edit_projection.run(self.db)['processed'],0)
  self.assertEqual(self.db.execute('SELECT count(*) FROM history_edit_pending').fetchone()[0],1)
  history_projection.run(self.db)
  self.assertEqual(self.db.execute('SELECT added FROM edits').fetchone()[0],3)
  self.assertEqual(self.db.execute('SELECT count(*) FROM calls').fetchone()[0],1)
  self.assertEqual(self.db.execute('SELECT count(*) FROM history_edit_pending').fetchone()[0],0)
 def test_decode_compare_commit_rejects_concurrent_body_change(self):
  uid=self.legacy(self.body());self.seed();original=collect.claude_edit_events
  def changed(body,*args):
   rows=original(body,*args)
   with self.db:history.put(self.db,'claude','s','project',100,'assistant',self.body(text='a\nb\nc\nd\n'),'record',role='assistant')
   return rows
  with patch.object(collect,'claude_edit_events',side_effect=changed):self.assertEqual(edit_projection.run(self.db)['processed'],0)
  self.assertEqual(self.db.execute('SELECT count(*) FROM edits').fetchone()[0],0)
  history_projection.run(self.db)
  self.assertEqual(self.db.execute('SELECT added FROM edits').fetchone()[0],4)
 def test_failed_decode_attempts_obey_budget_and_remain_retryable(self):
  originals={}
  for i in range(3):
   body=self.body('write'+str(i));uid=self.legacy(body,str(i));originals[uid]=self.db.execute('SELECT body FROM history_events WHERE event_id=?',(uid,)).fetchone()[0]
  self.seed()
  with self.db:self.db.execute("UPDATE history_events SET body=x'00'")
  with patch.object(edit_projection.time,'thread_time',side_effect=[0,1,1.1]):
   result=edit_projection.run(self.db,budget=.005)
  self.assertEqual((result['processed'],result['errors'],result['pending']),(0,1,3))
  self.assertEqual(history_projection.status(self.db)['edit_retrying'],1)
  with self.db:
   for uid,body in originals.items():self.db.execute('UPDATE history_events SET body=? WHERE event_id=?',(body,uid))
   self.db.execute('UPDATE history_edit_pending SET retry_at=0')
  self.assertEqual(edit_projection.run(self.db,budget=1)['pending'],0)
 def test_unavailable_body_is_reported_without_inventing_edit_metrics(self):
  uid=self.legacy(self.body());self.seed()
  with self.db:self.db.execute('UPDATE history_events SET body=NULL,expired_at=101 WHERE event_id=?',(uid,))
  result=edit_projection.run(self.db)
  self.assertEqual(result['pending'],0)
  self.assertEqual(history_projection.status(self.db)['edit_unavailable'],1)
  self.assertEqual(self.db.execute('SELECT count(*) FROM edits').fetchone()[0],0)
