"""The targeted session relation must equal the portable canonical-call oracle."""
import json
import random
import tempfile
import unittest
from erg import collect,store,unified,work

class TargetedSessionReporting(unittest.TestCase):
 def database(self):
  tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
  db=store.connect(tmp.name+'/ledger');self.addCleanup(db.close);return db
 def assert_parity(self,db):
  cols='machine,uid'
  actual=set(db.execute('SELECT '+cols+' FROM report_sessions'))
  expected=set(db.execute('SELECT '+cols+' FROM ('+unified.SESSIONS_SELECT+')'))
  self.assertEqual(actual,expected)
 def test_targeted_relation_matches_oracle_for_replay_ambiguity_and_unknowns(self):
  db=self.database()
  for seed in range(16):
   randomizer=random.Random(seed)
   with db:
    db.execute('DELETE FROM sessions');db.execute('DELETE FROM llm')
    for machine in ('a','b','c'):
     for tool in ('claude','codex','pi','gateway','local'):
      for sid in ('s1','s2','metadata'):
       db.execute('INSERT INTO sessions(machine,uid,tool,session,provenance,conversation_id) VALUES(?,?,?,?,?,?)',(machine,tool+':'+sid,tool,sid,'omnigent_metadata' if sid=='metadata' else 'native','conversation' if sid=='metadata' else None))
    for i in range(400):
     machine=randomizer.choice(('a','b','c'));tool=randomizer.choice(('claude','codex','pi','gateway','local'))
     row=[machine,tool+':'+str(randomizer.randrange(35)),randomizer.choice((1,2,3)),tool,randomizer.choice(('s1','s2')),randomizer.choice((None,'','m','m:n','n')),randomizer.choice((0,1,5)),0,0,randomizer.choice((None,0,1,7)),randomizer.choice((None,'observed','unknown','synthetic_history')),randomizer.choice((None,'','r','r:','r:m','r:m:n'))]
     db.execute('INSERT OR REPLACE INTO llm(machine,uid,ts,tool,session,model,fresh_in,cache_read,cache_write,out,usage_kind,response_id) VALUES('+','.join('?' for _ in row)+')',row)
   self.assert_parity(db)
 def test_exact_matching_does_not_follow_losing_native_sighting(self):
  db=self.database()
  for machine,events in [('a',[
   collect.session_record('local','receipt',10),dict(t='llm',uid='receipt',tool='local',session='receipt',ts=10,model='m',fresh_in=1,out=1,response_id='response'),
   dict(t='llm',uid='native',tool='codex',session='s',ts=10,model='m',fresh_in=1,out=1,response_id='response')]),
   ('b',[dict(t='llm',uid='native',tool='codex',session='s',ts=11,model='m',fresh_in=1,out=2,response_id='different')])]:
   store.ingest(db,map(json.dumps,events),machine=machine)
  self.assert_parity(db);self.assertEqual(db.execute('SELECT tool,session FROM report_sessions').fetchall(),[('local','receipt')])
 def test_unrelated_native_history_does_not_drive_session_query_work(self):
  db=self.database()
  with db:
   db.executemany("INSERT INTO llm(machine,uid,ts,tool,session,model,fresh_in,out,response_id) VALUES('host',?,1,'codex','s','m',1,1,?)",[(str(i),'response-'+str(i)) for i in range(5000)])
   db.execute("INSERT INTO llm(machine,uid,ts,tool,session,model,fresh_in,out,response_id) VALUES('host','receipt',1,'local','receipt','m',1,1,'response-1')")
   db.execute("INSERT INTO sessions(machine,uid,tool,session) VALUES('host','receipt','local','receipt')")
  ticks=[]
  def budget():ticks.append(1);return len(ticks)>50
  db.set_progress_handler(budget,100)
  try:self.assertEqual(db.execute('SELECT count(*) FROM report_sessions').fetchone()[0],0)
  finally:db.set_progress_handler(None,0)

 def test_input_ranking_uses_eligible_index_without_sorting_excluded_history(self):
  db=self.database()
  plan=str(db.execute('EXPLAIN QUERY PLAN SELECT count(*) FROM input_messages').fetchall())
  self.assertIn('prompts_capture_ranking',plan)
  self.assertNotIn('USE TEMP B-TREE FOR ORDER BY',plan)

 def test_effectiveness_preserves_global_input_dedup_and_parent_links(self):
  db=self.database();url='https://github.com/acme/r/pull/1'
  rows=[collect.session_record('codex','child',10,parent_session='parent'),
        dict(t='prompt',uid='earlier',tool='codex',session='unknown',ts=10,source_uid='shared',origin='human',provenance='codex_user_text',chars=4,words=1,est_tokens=1),
        dict(t='prompt',uid='losing',tool='codex',session='assigned',ts=11,source_uid='shared',origin='human',provenance='codex_user_text',chars=4,words=1,est_tokens=1),
        dict(t='prompt',uid='child',tool='codex',session='child',ts=12,origin='human',provenance='codex_user_text',chars=4,words=1,est_tokens=1),
        dict(t='prompt',uid='direct',tool='codex',session='assigned',ts=13,origin='unknown',provenance='codex_user_event',chars=4,words=1,est_tokens=1)]
  store.ingest(db,map(json.dumps,rows),machine='host')
  work.bind(db,'codex','parent',url,'',1,100);work.bind(db,'codex','assigned',url,'',1,100)
  actual=work.effectiveness(db)
  self.assertEqual(actual['prs'][0]['observed_inputs'],2)
  self.assertEqual(actual['prs'][0]['known_human_inputs'],1)
  self.assertIsNone(actual['prs'][0]['human_inputs'])


class NarrowCallReporting(unittest.TestCase):
 database=TargetedSessionReporting.database
 def test_narrow_columns_preserve_global_winners_and_matching_multiplicity(self):
  from collections import Counter
  from erg import report_sqlite
  db=self.database()
  projections=[['uid','tool','machine','session','out','capture_rank'],['ts','machine','tool','project','model','kind','fresh_in','cache_read','cache_write','out','reasoning','requester','dup']]
  for seed in range(12):
   rng=random.Random(seed)
   with db:
    db.execute('DELETE FROM llm')
    for i in range(240):
     row=[rng.choice(('a','b','c')),rng.choice(('codex','claude','pi','local','gateway')),str(rng.randrange(25)),rng.randrange(1,8),rng.choice(('s','child','other')),rng.choice((None,'','m','m:n','n')),rng.choice((None,'','r','r:m','r:m:n')),rng.choice((None,'','request','other')),rng.choice((None,0,1,5)),rng.choice((None,0,2)),rng.choice((None,'observed','unknown','synthetic_history'))]
     db.execute('INSERT OR REPLACE INTO llm(machine,tool,uid,ts,session,model,response_id,request_id,out,fresh_in,usage_kind) VALUES('+','.join('?' for _ in row)+')',row)
   for fields in projections:
    expected=Counter(db.execute('SELECT '+','.join(fields)+' FROM ('+unified.CALLS_SELECT+')'))
    actual=Counter(db.execute(report_sqlite.calls_select(fields)))
    self.assertEqual(actual,expected,(seed,fields))
 def test_projection_validation_rejects_expressions_and_keeps_match_keys_internal(self):
  from erg import report_sqlite
  db=self.database()
  for fields in ([],['uid','uid'],['uid; DROP TABLE llm'],['a.uid'],'uid',[None],[['uid']]):
   with self.assertRaises(ValueError):report_sqlite.calls_select(fields)
  query=report_sqlite.calls_select(['uid'])
  self.assertEqual([c[0] for c in db.execute(query).description],['uid'])
