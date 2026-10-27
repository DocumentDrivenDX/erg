"""Shared evidence reports must agree with published usage, without raw reads."""
import contextlib
import json
import sqlite3
import unittest
from unittest.mock import patch
from tests import test_project_rollups as fixtures
from erg import account_reports,project_rollups,report_evidence,report_scope,store,work,server


class EvidenceReports(unittest.TestCase):
    setUp=fixtures.DurableProjectRollups.setUp
    add=fixtures.DurableProjectRollups.add
    canonical=fixtures.DurableProjectRollups.canonical
    build=fixtures.DurableProjectRollups.build
    read=fixtures.DurableProjectRollups.read
    # Inherit the fixture helpers, not its independent tests.
    def event(self,**row):store.ingest(self.db,[json.dumps(row)],machine='host')
    def report(self,view='work',**filters):
        with contextlib.closing(store.open_readonly(self.path)) as db:
            db.execute('BEGIN');return report_evidence.report(db,view,report_scope.Selection(filters))
    def test_all_scopes_match_costs_and_raw_work(self):
        self.add();self.add('two',ts=7350,out=90);self.add('receipt',ts=3750,tool='local',response_id='mirror');self.add('native',ts=3751,response_id='mirror')
        for f in ({},{'project_key':'a'},{'project_key':'b'},{'project_key':'@unassigned'},{'start':3710,'end':7400},{'tool':'local'},{'kinds':'@none'},{'tools':'@none'}):
            self.build();cost=self.read(**f)
            for view in ('work','accounts'):
                report=self.report(view,**f);self.assertAlmostEqual(report['total_calls'],cost['totals']['request_equivalents']);self.assertAlmostEqual(report['valuation']['known_value_low_usd'],cost['totals']['known_value_low_usd']);self.assertAlmostEqual(report['reported_call_count'],cost['totals']['reported_equivalents'])
            raw=work.effectiveness(self.db,view='work',selection=report_scope.Selection(f));self.assertAlmostEqual(raw['total_calls'],self.report(**f)['total_calls'])
    def test_late_pr_and_commit_evidence_updates_pre_pr_usage_and_inputs(self):
        self.add();self.build();self.assertEqual(self.report()['attributed_calls'],0)
        url='https://github.com/a/app/pull/1';sha='a'*40
        self.event(t='work_context',uid='ctx',ts=3600,tool='pi',session='s',repo='github.com/a/app',branch='feature',scope_id='scope',capture_version=5)
        self.event(t='pull_request',uid='pr',ts=8000,url=url,repo='github.com/a/app',number=1,branch='feature',state='merged',created_ts=7000,merged_ts=8000,title='Title',added_code=50)
        self.event(t='pr_commit',uid='pc',ts=8000,pr_url=url,sha=sha)
        self.event(t='work_commit',uid='wc',ts=4000,repo='github.com/a/app',sha=sha,provenance='tool')
        self.event(t='work_evidence',uid='we',ts=4000,tool='pi',session='s',repo='github.com/a/app',scope_id='scope',sha=sha,capture_version=5,provenance='tool_commit')
        self.event(t='prompt',uid='input',ts=3700,tool='pi',session='s',project='app',origin='human',source_uid='input',chars=4,words=1)
        self.build();a=self.report(project_key='a');raw=work.effectiveness(self.db,view='work',selection=report_scope.Selection({'project_key':'a'}))
        self.assertEqual(a['prs'][0]['title'],'Title');self.assertAlmostEqual(a['pre_pr_calls'],.6);self.assertAlmostEqual(a['commit_attributed_calls'],.6);self.assertAlmostEqual(a['prs'][0]['human_inputs'],.6)
        for k in ('total_calls','attributed_calls','pre_pr_calls','commit_attributed_calls'):self.assertAlmostEqual(a[k],raw[k])
        with self.db:self.db.execute("DELETE FROM work_evidence WHERE uid='we'")
        self.build();self.assertEqual(self.report()['attributed_calls'],0)
    def test_account_owner_is_not_billing_and_charges_stay_whole_period(self):
        self.add();self.build();self.assertEqual(self.report('accounts')['account_usage'][0]['account_key'],'@unknown')
        self.event(t='identity',uid='owner',ts=3600,tool='pi',session='s',provider='provider',account_key='account',subscription_key='sub',provenance='claude_session_owner',scope='session_owner')
        self.event(t='subscription_charge',uid='charge',ts=8000,provider='provider',account_key='account',subscription_key='sub',period_start=0,period_end=9000,usd=100,amount=100,currency='USD',provenance='operator_metadata')
        self.build();a=self.report('accounts',project_key='a');self.assertEqual(a['account_usage'][0]['account_key'],'account');self.assertEqual(a['subscription_periods'][0]['calls'],0)
        self.event(t='account_binding',uid='binding',ts=8000,tool='pi',session='s',provider='provider',account_key='account',subscription_key='sub',start_ts=0,end_ts=9000,provenance='operator_metadata')
        self.build();a=self.report('accounts',project_key='a');self.assertAlmostEqual(a['subscription_periods'][0]['calls'],.6);self.assertEqual(a['subscription_periods'][0]['subscription_spend_usd'],100);self.assertIsNone(a['account_usage'][0]['actual_billed_usd'])
        raw=account_reports.report(self.db);all_report=self.report('accounts');self.assertAlmostEqual(raw['subscription_periods'][0]['calls'],all_report['subscription_periods'][0]['calls'])
        self.event(t='subscription_charge',uid='charge',ts=9000,provider='provider',account_key='account',subscription_key='sub',period_start=0,period_end=9000,usd=-10,amount=-10,currency='USD',provenance='operator_metadata')
        self.build();self.assertIsNone(self.report('accounts')['subscription_periods'][0]['known_value_per_subscription_dollar_low'])
    def test_source_free_cold_read_and_atomic_interruption(self):
        self.add();self.build();before=self.report();self.add('two',ts=7400);self.canonical()
        with self.assertRaises(InterruptedError):project_rollups.rebuild(self.path,lambda **kw:(_ for _ in ()).throw(InterruptedError()))
        self.assertEqual(self.report()['total_calls'],before['total_calls']);self.build()
        def auth(action,table,*rest):
            forbidden={'llm','call_winner_rows','prompts','work_contexts','work_evidence','identity_events','subscription_charge_events','quota_events','history_events'}
            return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_READ and table in forbidden else sqlite3.SQLITE_OK
        with contextlib.closing(store.open_readonly(self.path)) as db:
            db.execute('BEGIN');db.set_authorizer(auth)
            for v in ('work','accounts'):self.assertTrue(report_evidence.report(db,v,report_scope.Selection())['available'])
        with patch.object(store,'open_readonly',side_effect=lambda *args:sqlite3.connect('file:'+self.path+'?mode=ro',uri=True)),patch.object(work,'effectiveness',side_effect=AssertionError('raw work')),patch.object(account_reports,'report',side_effect=AssertionError('raw accounts')):
            reports=server.Reports()
            for v in ('work','accounts'):self.assertEqual(json.loads(reports.encoded_effectiveness(True,v))['total_calls'],2)
    def test_quota_and_titles_refresh_metadata_without_dirtying_usage(self):
        self.add();self.build();heads=list(self.db.execute('SELECT * FROM project_rollup_heads'))
        self.event(t='quota',uid='quota',ts=8000,tool='pi',session='s',provider='p',limit_id='l',window='w',event='limit_reached',provenance='fixture')
        self.assertEqual(project_rollups.status(self.db)['pending_hours'],0);self.build();self.assertEqual(heads,list(self.db.execute('SELECT * FROM project_rollup_heads')));self.assertEqual(self.report('accounts')['quota_counts']['limit_reached'],1)

    def test_identity_invalidations_only_rebuild_related_sessions(self):
        self.add();self.add('other',ts=7350,session='other');self.build()
        heads=dict(self.db.execute('SELECT hour,version FROM project_rollup_heads'))
        self.event(t='identity',uid='owner',ts=3600,tool='pi',session='s',provider='p',account_key='account',provenance='session_auth')
        self.assertEqual(list(self.db.execute('SELECT hour FROM project_rollup_dirty')),[(1,)])
        self.build();after=dict(self.db.execute('SELECT hour,version FROM project_rollup_heads'));self.assertEqual(heads[2],after[2]);self.assertNotEqual(heads[1],after[1])
        before=dict(self.db.execute('SELECT hour,version FROM project_rollup_heads'))
        self.event(t='identity',uid='owner',ts=3600,tool='pi',session='s',provider='p',account_key='account',provenance='session_auth')
        self.assertEqual(project_rollups.status(self.db)['pending_hours'],0)
        self.assertEqual(before,dict(self.db.execute('SELECT hour,version FROM project_rollup_heads')))

    def test_parent_operator_link_invalidates_child_and_unknowns_stay_unknown(self):
        self.add(session='child')
        self.event(t='session',uid='child',ts=3600,tool='pi',session='child',parent_session='parent',project='app')
        self.build();self.assertEqual(self.report()['attributed_calls'],0)
        url='https://github.com/a/app/pull/8'
        self.event(t='work_link',uid='link',ts=8000,tool='pi',session='parent',pr_url=url,start_ts=0,end_ts=9000,active=1,provenance='operator')
        self.assertEqual(project_rollups.status(self.db)['pending_hours'],1)
        self.build();r=self.report();self.assertEqual(r['attributed_calls'],1);self.assertEqual(r['prs'][0]['evidence'],['parent_operator']);self.assertIsNone(r['prs'][0]['human_inputs']);self.assertIsNone(r['prs'][0]['cost_usd'])

    def test_repeated_account_observations_do_not_rebuild_shared_account_history(self):
        self.add();self.add('other',ts=7350,session='other')
        for sid in ('s','other'):
            self.event(t='identity',uid='owner-'+sid,ts=3600,tool='pi',session=sid,provider='p',account_key='a',account_id_hash='id',email_hash='email',provenance='auth')
        self.build()
        self.event(t='identity',uid='new-observation',ts=3700,tool='pi',session='s',provider='p',account_key='a',account_id_hash='id',email_hash='email',provenance='auth')
        self.assertEqual(list(self.db.execute('SELECT hour FROM project_rollup_dirty')),[(1,)])
        self.build();self.assertEqual(self.report('accounts')['total_calls'],2)

    def test_real_title_rows_publish_without_event_uid(self):
        self.add();self.build();heads=list(self.db.execute('SELECT * FROM project_rollup_heads'))
        with self.db:self.db.execute("INSERT INTO session_titles VALUES('host','pi','s','Real title','native',40,8000)")
        self.assertEqual(project_rollups.status(self.db)['pending_hours'],0)
        self.build();self.assertEqual(heads,list(self.db.execute('SELECT * FROM project_rollup_heads')))
        metadata=json.loads(project_rollups.state(self.db)['evidence_metadata']);self.assertEqual(metadata['session_titles'][0]['title'],'Real title')

    def test_worker_retries_schema_writer_contention(self):
        import threading
        self.add();self.canonical();stop=threading.Event();initialize=project_rollups.initialize;attempts=[]
        def retry(db):
            attempts.append(1)
            if len(attempts)==1:raise sqlite3.OperationalError('database is locked')
            return initialize(db)
        with patch.object(project_rollups,'initialize',side_effect=retry),patch.object(project_rollups,'Pacer',return_value=lambda **kwargs:None):
            project_rollups.run_worker(self.path,stop,update=lambda value:stop.set())
        self.assertEqual(len(attempts),2);self.assertTrue(self.report()['available'])

    def test_unrelated_commit_and_future_context_do_not_rebuild_usage(self):
        self.add();self.build()
        self.event(t='work_commit',uid='unrelated',ts=8000,repo='github.com/other/repo',sha='b'*40,provenance='tool')
        self.assertEqual(project_rollups.status(self.db)['pending_hours'],0)
        self.event(t='work_context',uid='future',ts=1000000,tool='pi',session='s',repo='github.com/a/app',branch='future',capture_version=5)
        self.assertEqual(project_rollups.status(self.db)['pending_hours'],0)
        self.event(t='pull_request',uid='unlinked',ts=8000,url='https://github.com/other/repo/pull/9',repo='github.com/other/repo',number=9,state='open',branch='other',title='Unlinked title')
        self.assertEqual(project_rollups.status(self.db)['pending_hours'],0)
        self.build();self.assertEqual(self.report()['prs'][0]['title'],'Unlinked title')

    def test_service_history_projection_has_bounded_background_budget(self):
        from erg import history
        with patch.object(history,'config',return_value=dict(content=True,enrolled_at=0)),patch('erg.history_projection.run',return_value=dict(processed=0,pending=0,errors=0)) as run,patch('erg.edit_projection.run',return_value={}),patch('erg.work_projection.run',return_value=dict(pending=0)),patch.object(history,'link_inferences'),patch.object(history,'consume_outbox'):
            history.scan(self.db,paths=[],scan_sqlite=False,projection_budget=.06)
            run.assert_called_once_with(self.db,limit=128,budget=.06,cpu_budget=True)

    def test_repeated_session_observation_uses_an_exact_identity_seek(self):
        with self.db:
            self.db.executemany("INSERT INTO sessions(machine,uid,tool,session,parent_session) VALUES('host',?,'pi',?,'')",(('pi:'+str(i),str(i)) for i in range(1000)))
        steps=[];self.db.set_progress_handler(lambda:steps.append(1) or 0,100)
        try:
            with self.db:self.db.execute("INSERT INTO sessions(machine,uid,tool,session,parent_session,last_ts) VALUES('host','pi:500','pi','500','',8000) ON CONFLICT(machine,uid) DO UPDATE SET last_ts=excluded.last_ts")
        finally:self.db.set_progress_handler(None,0)
        self.assertLess(len(steps),10,'unchanged session metadata scanned unrelated sessions')

    def test_commit_object_metadata_refreshes_without_rebuilding_attributed_calls(self):
        sha='c'*40
        self.add();self.event(t='work_context',uid='ctx',ts=3600,tool='pi',session='s',repo='github.com/a/app',scope_id='scope',capture_version=5)
        self.event(t='work_evidence',uid='fact',ts=4000,tool='pi',session='s',repo='github.com/a/app',scope_id='scope',sha=sha,capture_version=5,provenance='tool_commit')
        self.build();heads=list(self.db.execute('SELECT * FROM project_rollup_heads'))
        self.assertEqual(self.report()['commit_attributed_calls'],0)
        self.event(t='work_commit',uid='object',ts=4000,repo='github.com/a/app',sha=sha,added_code=12,files=1,provenance='git_object')
        self.assertEqual(project_rollups.status(self.db)['pending_hours'],0);self.build()
        self.assertEqual(heads,list(self.db.execute('SELECT * FROM project_rollup_heads')))
        self.assertEqual(self.report()['commit_attributed_calls'],1);self.assertEqual(self.report()['commits'][0]['added_code'],12)
        self.event(t='work_commit',uid='object',ts=4000,repo='github.com/a/app',sha=sha,added_code=20,files=1,provenance='git_history')
        self.build();self.assertEqual(heads,list(self.db.execute('SELECT * FROM project_rollup_heads')));self.assertEqual(self.report()['commits'][0]['added_code'],20)
        with self.db:self.db.execute("DELETE FROM work_commit_evidence WHERE uid='object'")
        self.build();self.assertEqual(heads,list(self.db.execute('SELECT * FROM project_rollup_heads')));self.assertEqual(self.report()['commit_attributed_calls'],0)

    def test_work_facts_invalidate_only_their_scope_including_pre_commit_calls(self):
        self.add('first',ts=3700);self.add('second',ts=7300)
        for uid,stamp,scope in [('one',3600,'one'),('two',7200,'two')]:
            self.event(t='work_context',uid=uid,ts=stamp,tool='pi',session='s',repo='github.com/a/app',scope_id=scope,capture_version=5)
        self.build()
        self.event(t='work_evidence',uid='fact',ts=5000,tool='pi',session='s',repo='github.com/a/app',scope_id='one',sha='d'*40,capture_version=5,provenance='tool_commit')
        self.assertEqual(list(self.db.execute('SELECT hour FROM project_rollup_dirty')),[(1,)])
        self.build()
        # Replacement must invalidate both the retracted scope and its new scope.
        self.event(t='work_evidence',uid='fact',ts=8000,tool='pi',session='s',repo='github.com/a/app',scope_id='two',sha='d'*40,capture_version=5,provenance='tool_commit')
        self.assertEqual(list(self.db.execute('SELECT hour FROM project_rollup_dirty ORDER BY hour')),[(1,),(2,)])
        self.build();raw=work.effectiveness(self.db,view='work',selection=report_scope.Selection())
        self.assertEqual(self.report()['commit_attributed_calls'],raw['commit_attributed_calls'])
