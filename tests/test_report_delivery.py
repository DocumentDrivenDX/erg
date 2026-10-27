import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from erg import store,history,delivery_journal as journal,delivery_protocol as wire,report_delivery as reports,cloud_dashboard

class ReportingDelivery(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.db=store.connect(self.root/'source.db');self.cloud=store.connect(self.root/'cloud.db');self.addCleanup(self.db.close);self.addCleanup(self.cloud.close)
  self.identity=dict(enrollment_id='test',installation_id='install',destination_generation='gen')
  journal.enroll(self.db,self.identity,forward_only=True,source_floors=[],forward_ts=100)
  reports.initialize(self.db);reports.initialize(self.db);cloud_dashboard.initialize(self.cloud,self.identity)
  with self.db:history.put(self.db,'pi','s','app',200,'session',dict(type='session',id='s'),'native',machine='host')
 def ingest(self,kind,uid,ts,**kw):
  store.ingest(self.db,[json.dumps(dict(t=kind,uid=uid,ts=ts,**kw))],machine='host')
 def test_only_forward_activity_and_scoped_reporting_metadata_are_sent_idempotently(self):
  self.ingest('llm','new',250,tool='pi',session='s',project='app',model='m',fresh_in=10,out=2)
  self.ingest('llm','old',50,tool='pi',session='s',fresh_in=900,out=900)
  self.ingest('llm','other',250,tool='pi',session='unrelated',fresh_in=900,out=900)
  self.ingest('work_context','ctx',50,tool='pi',session='s',repo='https://github.com/o/r',branch='b')
  self.ingest('pull_request','pr',40,repo='https://github.com/o/r',url='https://github.com/o/r/pull/1',title='Scoped PR',state='merged')
  self.ingest('pull_request','private',300,repo='https://github.com/other/repo',title='Must not export')
  self.ingest('identity','owner',250,tool='pi',session='s',account_key='account',provider='p')
  self.ingest('subscription_charge','charge',20,account_key='account',usd=20,provider='p')
  self.ingest('subscription_charge','unrelated-charge',300,account_key='other',usd=999)
  result=reports.publish(self.db,budget=5);self.assertEqual(result['queued'],5)
  self.assertEqual(reports.publish(self.db,budget=5)['queued'],0)
  while journal.prepare_one(self.db):pass
  rows=self.db.execute('SELECT delivery_seq,item FROM delivery_items ORDER BY delivery_seq').fetchall()
  cloud_dashboard.apply_page(self.cloud,[(r[0],r[1].decode()) for r in rows])
  self.assertEqual(self.cloud.execute('SELECT uid,fresh_in,out FROM llm').fetchall(),[('new',10,2)])
  self.assertEqual(self.cloud.execute('SELECT title FROM pull_requests').fetchall(),[('Scoped PR',)])
  self.assertEqual(self.cloud.execute('SELECT usd FROM subscription_charge_events').fetchall(),[(20,)])
  self.assertEqual(self.cloud.execute('SELECT count(*) FROM history_events').fetchone()[0],1)
  self.assertEqual(self.cloud.execute('SELECT count(*) FROM delivery_journal').fetchone()[0],0)
  cloud_dashboard.apply_page(self.cloud,[(r[0],r[1].decode()) for r in rows])
  self.assertEqual(self.cloud.execute('SELECT count(*) FROM llm').fetchone()[0],1)
 def test_changed_observation_gets_new_revision_without_duplicate_usage(self):
  self.ingest('llm','call',250,tool='pi',session='s',project='app',model='m',fresh_in=10,out=2)
  reports.publish(self.db,budget=5)
  self.ingest('llm','call',250,tool='pi',session='s',project='app',model='m',fresh_in=10,out=3)
  reports.publish(self.db,budget=5)
  revisions=self.db.execute("SELECT revision FROM delivery_journal WHERE snapshot='v1' AND source_seq=0").fetchall();self.assertEqual(len(revisions),2);self.assertLess(revisions[0][0],revisions[1][0])
  while journal.prepare_one(self.db):pass
  cloud_dashboard.apply_page(self.cloud,[(seq,item.decode()) for seq,item in self.db.execute('SELECT delivery_seq,item FROM delivery_items ORDER BY delivery_seq')])
  self.assertEqual(self.cloud.execute('SELECT count(*),sum(out) FROM llm').fetchone(),(1,3))
 def test_noncanonical_fields_and_digest_fail_closed(self):
  self.ingest('llm','call',250,tool='pi',session='s',fresh_in=10,out=2,client_secret='do-not-copy')
  reports.publish(self.db,budget=5)
  import zlib
  body=json.loads(zlib.decompress(self.db.execute('SELECT body FROM delivery_journal WHERE source_seq=0').fetchone()[0]))
  self.assertNotIn('client_secret',body['payload'])
  body['content_hash']='0'*64
  with self.assertRaises(ValueError):reports.apply(self.cloud,dict(origin='derived_reporting',kind='usage_evidence',record_kind='usage_evidence',body=body))

 def test_bad_source_is_quarantined_without_blocking_other_evidence(self):
  self.ingest('llm','bad',250,tool='pi',session='s',fresh_in=1,out=2)
  self.ingest('llm','good',251,tool='pi',session='s',fresh_in=3,out=4)
  with self.db:self.db.execute("UPDATE observations SET content_hash=? WHERE kind='llm' AND ts=250",('0'*64,))
  result=reports.publish(self.db,budget=5)
  self.assertEqual(result['queued'],1);self.assertEqual(result['errors'],1)
  self.assertEqual(reports.publish(self.db,budget=5)['queued'],0)

 def test_preboundary_activity_in_existing_seed_is_still_excluded(self):
  for kind in ('llm','prompt','edit','session','session_cost','quota'):
   self.ingest(kind,'old-'+kind,50,tool='pi',session='s',summary_in=999,summary_out=999)
  self.ingest('work_context','current-context',50,tool='pi',session='s',branch='feature')
  with self.db:
   self.db.execute('INSERT OR IGNORE INTO delivery_report_seed(event_id) SELECT event_id FROM observations')
  result=reports.publish(self.db,budget=5)
  self.assertEqual(result['queued'],1)
  self.assertEqual(self.db.execute("SELECT role FROM delivery_snapshots WHERE origin='derived_reporting'").fetchall(),[('work_context',)])

 def test_import_excludes_legacy_preboundary_summary_and_charge(self):
  for kind in ('session','session_cost'):
   self.ingest(kind,'legacy-'+kind,50,tool='pi',session='s',summary_in=999,summary_out=999)
  config=self.db.execute('SELECT enrollment_id,installation_id,destination_generation,forward_ts FROM delivery_config').fetchone()
  for row in self.db.execute('SELECT event_id,machine,kind,ts,payload,content_hash,seq FROM observations').fetchall():
   with self.db:reports.enqueue(self.db,config,row)
  while journal.prepare_one(self.db):pass
  cloud_dashboard.initialize(self.cloud,self.identity,forward_ts=100)
  cloud_dashboard.apply_page(self.cloud,[(seq,item.decode()) for seq,item in self.db.execute('SELECT delivery_seq,item FROM delivery_items ORDER BY delivery_seq')])
  self.assertEqual(self.cloud.execute('SELECT count(*) FROM observations').fetchone()[0],0)
  self.assertEqual(self.cloud.execute('SELECT cursor FROM cloud_config').fetchone()[0],3)

 def test_priority_headers_and_contiguous_trace_replay_share_deduplication(self):
  self.ingest('llm','call',250,tool='pi',session='s',fresh_in=10,out=2)
  self.ingest('work_context','ctx',50,tool='pi',session='s',repo='https://github.com/o/r')
  self.ingest('pull_request','pr',50,repo='https://github.com/o/r',url='https://github.com/o/r/pull/1',title='Verified title')
  self.ingest('identity','owner',250,tool='pi',session='s',account_key='acct')
  reports.publish(self.db,budget=5)
  while journal.prepare_one(self.db):pass
  rows=[(seq,item.decode()) for seq,item in self.db.execute('SELECT delivery_seq,item FROM delivery_items ORDER BY delivery_seq')]
  headers=[r for r in rows if wire.decode(r[1].encode())['record_kind'] in ('work_evidence','account_evidence')]
  self.assertEqual(cloud_dashboard.apply_page(self.cloud,headers,metadata=True),3)
  self.assertEqual(self.cloud.execute('SELECT cursor FROM cloud_config').fetchone()[0],0)
  self.assertEqual(self.cloud.execute('SELECT count(*) FROM history_events').fetchone()[0],0)
  self.assertEqual(self.cloud.execute('SELECT title FROM pull_requests').fetchall(),[('Verified title',)])
  with self.assertRaises(ValueError):cloud_dashboard.apply_page(self.cloud,rows[:1],metadata=True)
  self.assertEqual(cloud_dashboard.apply_page(self.cloud,rows),len(rows))
  self.assertEqual(cloud_dashboard.apply_page(self.cloud,rows),0)
  self.assertEqual(self.cloud.execute('SELECT count(*) FROM identity_events').fetchone()[0],1)
  self.assertEqual(self.cloud.execute('SELECT count(*) FROM llm').fetchone()[0],1)
  altered=wire.decode(headers[0][1].encode());altered['record_sha256']='0'*64
  with self.assertRaises(ValueError):cloud_dashboard.apply_page(self.cloud,[(headers[0][0],wire.encode(altered).decode())],metadata=True)

 def test_context_anchor_and_updates_preserve_forward_allocation_without_old_bodies(self):
  from erg import projects,project_scope
  cfg=projects.snapshot(self.db);cfg['projects']=[{'project_key':'human','name':'Human'}];cfg['bindings']=[dict(binding_id='folder',selectors={'directory':{'machine':'host','root':'/work/app'}},allocations=[{'project_key':'human','units':1000000}],valid_from=0)];projects.apply(self.db,cfg);cloud_dashboard.sync_policy(self.cloud,self.root/'source.db')
  with self.db:
   projects.observe_context(self.db,'host','pi','s',50,'old-anchor','/work/app')
   projects.observe_context(self.db,'host','pi','unrelated',50,'unrelated','/private/other')
  result=reports.publish(self.db,budget=5);self.assertEqual(result['queued'],1)
  while journal.prepare_one(self.db):pass
  rows=[(seq,item.decode()) for seq,item in self.db.execute('SELECT delivery_seq,item FROM delivery_items ORDER BY delivery_seq')]
  cloud_dashboard.apply_page(self.cloud,rows[1:],metadata=True)
  call=dict(machine='host',tool='pi',session='s',ts=250,project='app')
  self.assertEqual(project_scope.Scope(self.cloud,300).resolve(call)['allocations'],[{'project_key':'human','units':1000000}])
  self.assertEqual(self.cloud.execute('SELECT cwd FROM project_contexts').fetchall(),[('/work/app',)])
  self.assertEqual(self.cloud.execute('SELECT count(*) FROM history_events').fetchone()[0],0)
  with self.db:projects.observe_context(self.db,'host','pi','s',300,'new-anchor','/work/new')
  self.assertEqual(reports.publish(self.db,budget=5)['queued'],1)
  self.assertEqual(reports.publish(self.db,budget=5)['queued'],0)

 def test_live_metadata_changes_do_not_wait_for_seed_backlog(self):
  events=[json.dumps(dict(t='prompt',uid='seed-'+str(i),ts=200,tool='pi',session='s',chars=1)) for i in range(600)]
  store.ingest(self.db,events,machine='host')
  self.assertEqual(reports.publish(self.db,budget=5)['queued'],512)
  self.assertGreater(self.db.execute('SELECT count(*) FROM delivery_report_seed').fetchone()[0],0)
  self.ingest('work_context','live',300,tool='pi',session='s',repo='https://github.com/o/r')
  result=reports.publish(self.db,budget=5)
  self.assertEqual(result['queued'],1)
  self.assertEqual(self.db.execute("SELECT count(*) FROM delivery_snapshots WHERE role='work_context'").fetchone()[0],1)
  self.assertGreater(result['seed_pending'],0)
  self.assertEqual(self.db.execute('SELECT source_seq FROM delivery_report_progress').fetchone()[0],self.db.execute('SELECT seq FROM journal_clock').fetchone()[0])

 def test_bootstrap_order_and_context_anchor_use_indexes(self):
  plan=self.db.execute('EXPLAIN QUERY PLAN SELECT o.event_id FROM delivery_report_seed s INDEXED BY delivery_report_seed_order JOIN observations o ON o.event_id=s.event_id ORDER BY s.source_seq LIMIT 4000').fetchall()
  self.assertFalse(any('TEMP B-TREE' in r[-1] for r in plan))
  plan=self.db.execute("EXPLAIN QUERY PLAN SELECT min(ts) FROM delivery_snapshots WHERE machine=? AND tool=? AND session=? AND COALESCE(origin,'')<>'derived_reporting'",('host','pi','s')).fetchall()
  self.assertTrue(any('delivery_snapshot_scope' in r[-1] for r in plan))

 def test_updated_runtime_can_retry_canonical_quarantined_metadata_without_bypass(self):
  self.ingest('identity','id',200,tool='pi',session='s',account_key='account',provider='fixture')
  row=self.db.execute("SELECT event_id,seq FROM observations WHERE kind='identity'").fetchone()
  with self.db:self.db.execute('INSERT INTO delivery_report_errors VALUES(?,?,?)',(row[0],row[1],'ValueError'))
  self.assertEqual(reports.retry_errors(self.db),1)
  for _ in range(4):reports.publish(self.db,budget=5)
  self.assertEqual(self.db.execute('SELECT count(*) FROM delivery_report_errors').fetchone()[0],0)
  self.assertEqual(self.db.execute('SELECT count(*) FROM delivery_report_sent WHERE event_id=?',(row[0],)).fetchone()[0],1)

  # A newer runtime may already have delivered the exact revision while the
  # prior runtime's error marker survived; verified journal evidence clears it.
  with self.db:self.db.execute('INSERT INTO delivery_report_errors VALUES(?,?,?)',(row[0],row[1],'ValueError'))
  self.assertEqual(reports.retry_errors(self.db),1)
  self.assertEqual(self.db.execute('SELECT count(*) FROM delivery_report_errors').fetchone()[0],0)
