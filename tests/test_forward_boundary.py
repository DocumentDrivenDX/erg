import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime,timezone
from erg import store,history,delivery_journal as journal
from central.erg_central.receiver import deployment_binding,Forbidden
ID=dict(enrollment_id='forward',installation_id='install',destination_generation='gen')
def stamp(t):return datetime.fromtimestamp(t,timezone.utc).isoformat()
class ForwardBoundary(unittest.TestCase):
 def test_real_native_offsets_exclude_existing_and_late_retained_records_for_all_agents(self):
  for tool in ('claude','codex','pi'):
   with self.subTest(tool=tool),tempfile.TemporaryDirectory() as tmp:
    db=store.connect(str(Path(tmp)/'ledger.db'))
    try:
     path=Path(tmp)/'session.jsonl'
     if tool=='pi':head=dict(type='session',id='session',cwd='/new',timestamp=stamp(1000))
     elif tool=='codex':head=dict(type='session_meta',timestamp=stamp(1000),payload=dict(id='session',cwd='/new'))
     else:head=dict(type='user',sessionId='session',uuid='header',timestamp=stamp(1000),message=dict(role='user',content='header'))
     def event(ident,t):
      if tool=='codex':return dict(type='response_item',id=ident,timestamp=stamp(t),payload=dict(type='message',role='user',content=[dict(type='input_text',text=ident)]))
      return dict(type='message' if tool=='pi' else 'user',id=ident,uuid=ident,sessionId='session',timestamp=stamp(t),message=dict(role='user',content=ident))
     # Even future-dated retained records before the captured byte boundary stay excluded.
     path.write_text(json.dumps(head)+'\n'+json.dumps(event('old',9999))+'\n')
     s=path.stat();journal.enroll(db,ID,forward_only=True,forward_ts=2000,source_floors=[(str(path),s.st_ino,s.st_size)])
     with path.open('a') as f:f.write(json.dumps(event('new',2001))+'\n')
     with db:history.scan_file(db,tool,str(path),1)
     self.assertEqual(db.execute('SELECT count(*) FROM delivery_journal').fetchone()[0],1)
     late=Path(tmp)/'late.jsonl';late.write_text(json.dumps(head)+'\n'+json.dumps(event('late-old',1999))+'\n')
     with db:history.scan_file(db,tool,str(late),1)
     self.assertEqual(db.execute('SELECT count(*) FROM delivery_journal').fetchone()[0],1)
     while journal.prepare_one(db):pass
     row=db.execute('SELECT source_seq FROM delivery_journal').fetchone()[0]
     with db:db.execute("UPDATE history_events SET project='correction' WHERE seq=?",(row,))
     self.assertEqual(db.execute('SELECT revision FROM delivery_journal ORDER BY id').fetchall(),[(1,),(2,)])
    finally:db.close()
 def test_existing_rows_unknown_native_timestamps_and_synthetic_history_stay_out(self):
  with tempfile.TemporaryDirectory() as tmp:
   db=store.connect(str(Path(tmp)/'ledger.db'))
   try:
    with db:key,_=history.put(db,'pi','s','p',2001,'message',{},'old')
    journal.enroll(db,ID,forward_only=True,forward_ts=2000)
    with db:
     db.execute("UPDATE history_events SET project='later' WHERE event_id=?",(key,))
     history.put(db,'codex','s','p',2001,'event',{},'unknown',source_ts=0)
     history.put(db,'pi','s','p',2001,'message',{},'synthetic',origin='synthetic_history')
    self.assertEqual(db.execute('SELECT count(*) FROM delivery_journal').fetchone()[0],0)
   finally:db.close()
 def test_deployment_guard_denies_missing_or_other_machine_triple(self):
  real=dict(ID,synthetic_only=False)
  self.assertEqual(deployment_binding(real,'qualified',ID),real)
  for key in ID:
   wrong=dict(ID);wrong[key]='other'
   with self.assertRaises(Forbidden):deployment_binding(real,'qualified',wrong)
  with self.assertRaises(Forbidden):deployment_binding(real,'qualified',{})
  with self.assertRaises(Forbidden):deployment_binding(dict(real,synthetic_only=True),'qualified',ID)
