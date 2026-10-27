"""Real SQLite parity, invalidation and publication tests for durable reports."""
import contextlib
import json
import math
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from erg import call_projection,project_dataset,project_reports,project_rollups,projects,report_sqlite,server,store


class DurableProjectRollups(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=os.path.join(self.temp.name,'ledger.db');self.db=store.connect(self.path)
        with self.db:project_rollups.initialize(self.db);project_rollups.put(self.db,enabled=1)
        config=projects.snapshot(self.db)
        config['projects']=[dict(project_key='a',name='A'),dict(project_key='b',name='B')]
        config['bindings']=[dict(binding_id='split',selectors={'agent_project':dict(machine='host',tool='pi',id='app')},allocations=[dict(project_key='a',units=600000),dict(project_key='b',units=200000)],valid_from=0)]
        projects.apply(self.db,config)
        self.card=dict(entries=[dict(model='model',as_of='2026-10-23',source='fixture',input=2,output=4,cache_read=.2,cache_write_5m=3,cache_write_1h=6,tiers=[dict(min_prompt_tokens=200000,input=5)])])
        self.prices=patch('erg.rates.load',return_value=self.card);self.prices.start();self.addCleanup(self.prices.stop);self.addCleanup(self.db.close);self.addCleanup(self.temp.cleanup)
    def add(self,uid='one',ts=3701,**kw):
        row=dict(t='llm',uid=uid,ts=ts,tool='pi',session='s',project='app',model='model',kind='interactive',fresh_in=100,cache_read=10,cache_write=2,out=20,reasoning=0,usage_kind='observed');row.update(kw)
        store.ingest(self.db,[json.dumps(row)],machine='host')
    def canonical(self):
        for _ in range(100):
            result=call_projection.run(self.db,seed_limit=10000,limit=10000,budget=1)
            if result['ready']:return
        self.fail('canonical cache did not warm')
    def build(self,tick=None):
        self.canonical();return project_rollups.rebuild(self.path,tick)
    def read(self,**filters):
        with contextlib.closing(store.open_readonly(self.path)) as db:
            db.execute('BEGIN');return project_rollups.costs(db,**filters)
    def parity(self,**filters):
        with contextlib.closing(store.open_readonly(self.path)) as db:
            db.execute('BEGIN');actual=project_rollups.costs(db,**filters)
            expected=project_reports.report(db,canonical_select=lambda cols:report_sqlite.calls_select(cols,db=db),**filters)
        for key in ('totals',):
            for metric in project_rollups.NUM:
                self.assertAlmostEqual(actual[key][metric],expected[key][metric],places=7,msg=metric)
        for field,key in [('projects','project_key'),('folders','folder_key'),('sessions','session')]:
            def grouped(rows):
                return {(r.get('project_key'),r[key]):r for r in rows}
            a=grouped(actual[field]);b=grouped(expected[field]);self.assertEqual(a.keys(),b.keys(),field)
            for k in a:
                for metric in project_rollups.NUM:self.assertAlmostEqual(a[k][metric],b[k][metric],places=7,msg=field+' '+metric)
        return actual
    def test_split_global_and_exact_partial_hour_costs_match_raw(self):
        self.add(kind=None);self.add('two',ts=3750,fresh_in=250000);self.add('later',ts=7300)
        self.build()
        for filters in ({},{'project_key':'a'},{'start':3710,'end':7301},{'start':3701,'end':3702},{'start':3600,'end':7200},{'tool':'claude'},{'tools':'@none'},{'requester':'me'},{'kinds':'interactive'},{'kinds':'@none'}):self.parity(**filters)
        with contextlib.closing(store.open_readonly(self.path)) as db:
            db.execute('BEGIN');data=project_rollups.dataset(db);reference=project_dataset.build(db)
            for field in ('llm','edits','prompts','commits'):self.assertEqual(data[field],reference[field])
    def test_correction_delete_replace_and_cross_hour_mirror_are_invalidated(self):
        self.add();self.build();self.add(ts=7350,out=90);self.build();self.parity()
        with self.db:self.db.execute("DELETE FROM llm WHERE uid='one'")
        self.build();self.parity()
        self.add('native',ts=3700,response_id='same');self.add('receipt',ts=7400,tool='local',response_id='same',project='other');self.build();self.parity()
        self.assertEqual(self.read()['totals']['request_equivalents'],1)
        with self.db:self.db.execute("DELETE FROM llm WHERE uid='native'")
        self.build();self.parity();self.assertEqual(self.read()['totals']['request_equivalents'],1)
    def test_late_context_policy_and_price_changes_rebuild_existing_hours(self):
        self.add();self.build()
        with self.db:projects.observe_context(self.db,'host','pi','s',3700,'ctx','/app')
        self.build();self.parity()
        cfg=projects.snapshot(self.db);cfg['bindings'][0]['allocations']=[dict(project_key='b',units=1000000)];projects.apply(self.db,cfg)
        self.build();self.parity();self.assertEqual(self.read()['projects'][0]['project_key'],'b')
        before=self.read()['totals']['known_value_low_usd'];self.card['entries'][0]['input']=8;self.card.pop('_prices',None);self.build();self.parity();self.assertGreater(self.read()['totals']['known_value_low_usd'],before)
    def test_readers_never_scan_sources_and_refresh_reuses_durable_rows(self):
        self.add();self.build()
        def auth(action,table,column,*rest):
            return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_READ and table in ('llm','call_winner_rows','prompts','edits','commits','project_contexts','work_contexts','history_events') else sqlite3.SQLITE_OK
        import sqlite3
        with contextlib.closing(store.open_readonly(self.path)) as db:
            db.execute('BEGIN');db.set_authorizer(auth)
            self.assertTrue(project_rollups.dataset(db)['llm']);self.assertTrue(project_rollups.costs(db)['available'])
        with patch.object(store,'open_readonly',side_effect=lambda *args:sqlite3.connect('file:'+self.path+'?mode=ro',uri=True)),patch.object(project_dataset,'build',side_effect=AssertionError('raw rebuild')),patch.object(project_reports,'report',side_effect=AssertionError('raw costs')):
            reports=server.Reports();self.assertEqual(reports.data(0,True)['project_revision'],1);self.assertTrue(reports.costs()['available'])
    def test_interrupted_build_keeps_previous_batch_and_pending_changes(self):
        self.add();self.build();before=self.read()['totals'];self.add('two',ts=7400);self.canonical()
        def abort(**kw):raise InterruptedError()
        with self.assertRaises(InterruptedError):project_rollups.rebuild(self.path,abort)
        self.assertEqual(self.read()['totals'],before);self.assertGreater(project_rollups.status(self.db)['pending_hours'],0)
        self.build();self.parity()
    def test_source_arriving_during_build_stays_queued(self):
        self.add();self.build();self.add('two');self.canonical();done=[]
        def change(**kw):
            if not done:
                done.append(1)
                with contextlib.closing(store.connect(self.path,initialize=False)) as db:store.ingest(db,[json.dumps(dict(t='llm',uid='three',ts=3710,tool='pi',session='s',project='app',model='model',fresh_in=50,out=1,usage_kind='observed'))],machine='host')
        project_rollups.rebuild(self.path,change)
        # Raw edits first dirty the winner cache; once winners advance, the hour
        # must be queued and the next published batch must match the oracle.
        self.build();self.parity();self.assertEqual(self.read()['totals']['request_equivalents'],3)

    def test_activity_and_noop_updates_only_dirty_changed_hours(self):
        self.add();self.build()
        with self.db:self.db.execute("UPDATE prompts SET ts=ts")
        self.assertEqual(project_rollups.status(self.db)['pending_hours'],0)
        for row in [dict(t='prompt',uid='input',ts=3720,tool='pi',session='s',project='app',chars=20,words=4,est_tokens=5),dict(t='edit',uid='edit',ts=3720,tool='pi',session='s',project='app',added=10,removed=1,doc=0),dict(t='commit',uid='commit',ts=3720,project='app',agent='pi',repo='github.com/a/app',added_code=5,removed_code=1,added_doc=0,removed_doc=0,files=1,doc_files=0)]:
            store.ingest(self.db,[json.dumps(row)],machine='host')
        self.build()
        with contextlib.closing(store.open_readonly(self.path)) as db:
            db.execute('BEGIN');actual=project_rollups.dataset(db);expected=project_dataset.build(db)
            for field in ('edits','prompts','commits'):self.assertEqual(actual[field],expected[field])
        self.assertEqual(project_rollups.status(self.db)['pending_hours'],0)
        with self.db:self.db.execute("UPDATE edits SET added=added WHERE uid='edit'")
        self.assertEqual(project_rollups.status(self.db)['pending_hours'],0)
        with self.db:self.db.execute("UPDATE edits SET ts=7400 WHERE uid='edit'")
        self.assertEqual(project_rollups.status(self.db)['pending_hours'],2)
        self.build()
        with contextlib.closing(store.open_readonly(self.path)) as db:
            db.execute('BEGIN');actual=project_rollups.dataset(db);expected=project_dataset.build(db)
            self.assertEqual(actual['edits'],expected['edits'])

    def test_gateway_mirror_reappears_when_native_identity_becomes_ambiguous(self):
        self.add('n1',request_id='request');self.add('g',ts=7300,tool='gateway',request_id='request');self.build();self.parity()
        self.assertEqual(self.read()['totals']['request_equivalents'],1)
        self.add('n2',ts=11000,request_id='request');self.build();self.parity()
        self.assertEqual(self.read()['totals']['request_equivalents'],3)

    def test_readonly_warming_never_triggers_raw_projection(self):
        self.add()
        with contextlib.closing(store.open_readonly(self.path)) as db:
            db.execute('BEGIN');self.assertFalse(project_rollups.dataset(db)['rollup_status']['ready']);self.assertFalse(project_rollups.costs(db)['available'])

    def test_prompt_and_commit_winner_moving_across_machines_and_hours(self):
        self.add();self.build()
        prompt=dict(t='prompt',uid='input',source_uid='global-input',ts=3720,tool='pi',session='s',project='app',origin='unknown',chars=10,words=2,est_tokens=3)
        commit=dict(t='commit',uid='sha',ts=3720,project='app',agent='pi',repo='github.com/a/app',added_code=3,removed_code=0,added_doc=0,removed_doc=0,files=1,doc_files=0)
        store.ingest(self.db,[json.dumps(prompt),json.dumps(commit)],machine='host');self.build()
        store.ingest(self.db,[json.dumps(dict(prompt,uid='better',ts=7400,origin='human')),json.dumps(dict(commit,ts=7400))],machine='aaa');self.build()
        with contextlib.closing(store.open_readonly(self.path)) as db:
            db.execute('BEGIN');a=project_rollups.dataset(db);b=project_dataset.build(db)
            self.assertEqual(a['prompts'],b['prompts']);self.assertEqual(a['commits'],b['commits'])
        store.ingest(self.db,[json.dumps(dict(prompt,uid='better',ts=7400,origin='human')),json.dumps(dict(commit,ts=7400))],machine='aaa')
        self.assertEqual(project_rollups.status(self.db)['pending_hours'],0)

    def test_winner_invalidation_uses_bounded_indexed_peer_probes(self):
        self.add();self.canonical()
        names=[r[1] for r in self.db.execute('PRAGMA table_info(call_winner_rows)')]
        original=self.db.execute('SELECT * FROM call_winner_rows LIMIT 1').fetchone();rows=[]
        for n in range(2000):
            row=list(original);row[names.index('key')]='unrelated:'+str(n);row[names.index('uid')]='unrelated:'+str(n);rows.append(row)
        with self.db:self.db.executemany('INSERT INTO call_winner_rows VALUES('+','.join('?' for _ in names)+')',rows)
        steps=[];self.db.set_progress_handler(lambda:steps.append(1) or 0,100)
        try:
            with self.db:self.db.execute("UPDATE call_winner_rows SET out=out+1 WHERE key='unrelated:0'")
        finally:self.db.set_progress_handler(None,0)
        self.assertLess(len(steps),30,'winner invalidation scanned unrelated identities')

    def test_context_time_bounds_and_work_changes_invalidate_shared_evidence(self):
        self.add();self.build()
        with self.db:projects.observe_context(self.db,'host','pi','s',1000000,'future','/future')
        self.assertEqual(project_rollups.status(self.db)['pending_hours'],0)
        with self.db:projects.observe_context(self.db,'host','pi','s',1000,'earlier','/app')
        self.build();self.parity()
        store.ingest(self.db,[json.dumps(dict(t='work_context',uid='ctx',ts=1000,tool='pi',session='s',project='app',repo='github.com/a/app',worktree='tree'))],machine='host');self.build();self.parity()
        with self.db:self.db.execute("UPDATE work_contexts SET branch='new-branch',head_sha='new-head' WHERE uid='ctx'")
        self.assertEqual(project_rollups.status(self.db)['pending_hours'],1)
        self.build();self.parity()
        with self.db:self.db.execute("UPDATE work_contexts SET ts=5000 WHERE uid='ctx'")
        self.assertEqual(project_rollups.status(self.db)['pending_hours'],1)
        self.build();self.parity()

    def test_default_near_core_worker_runs_without_legacy_limit_failure(self):
        stop=threading.Event();seen=[]
        def update(result):seen.append(result);stop.set()
        project_rollups.run_worker(self.path,stop,update)
        self.assertTrue(seen);self.assertFalse(seen[-1].get('last_error'))
        self.assertEqual(project_rollups.Pacer(duty=.9).allowance(),.9)
        for duty in (0,1.1):
            with self.assertRaises(ValueError):project_rollups.Pacer(duty=duty)

    def test_pacer_rechecks_live_allowance_while_waiting(self):
        wall=[0];allowance=[.02];waits=[]
        class Stop:
            def is_set(inner):return False
            def wait(inner,delay):
                waits.append(delay);self.assertEqual(len(waits),1)
                wall[0]+=delay;allowance[0]=.9
                return False
        with patch.object(project_rollups.time,'thread_time',side_effect=[0,.02,.02]),patch.object(project_rollups.time,'monotonic',side_effect=lambda:wall[0]):
            pacer=project_rollups.Pacer(Stop(),lambda:allowance[0]);pacer(force=True)
        self.assertEqual(waits,[.25])

    def test_reporting_catchup_ignores_small_transient_queues_and_bounds_pressure_probe(self):
        self.assertEqual(project_rollups.worker_allowance(self.db,.9),.9)
        with self.db:self.db.execute("INSERT INTO history_work_pending(event_id,seq,content_hash) VALUES('fixture',1,'hash')")
        self.assertEqual(project_rollups.worker_allowance(self.db,.9),.9)
        with self.db:self.db.executemany('INSERT INTO history_work_pending(event_id,seq,content_hash) VALUES(?,?,?)',[(str(n),n+2,'hash') for n in range(project_rollups.CORPUS_PRESSURE_ROWS)])
        self.assertEqual(project_rollups.worker_allowance(self.db,.9),.10)
        self.assertEqual(project_rollups.worker_allowance(self.db,.01),.01)
        with self.db:self.db.execute('DELETE FROM history_work_pending')
        self.assertEqual(project_rollups.worker_allowance(self.db,.9),.9)

    def test_scheduler_drains_old_hours_and_coalesces_only_recent_active_hour(self):
        now=360050;hour=int(now)//3600
        with self.db:
            self.db.execute('DELETE FROM project_rollup_dirty')
            project_rollups.put(self.db,updated_at=now-1)
            self.db.execute('INSERT INTO project_rollup_dirty VALUES(?,1)',(hour,))
        hot=project_rollups.worker_schedule(self.db,now)
        self.assertFalse(hot['publish']);self.assertEqual(hot['mode'],'coalescing')
        self.assertTrue(project_rollups.worker_schedule(self.db,now+5)['publish'])
        self.assertTrue(project_rollups.worker_schedule(self.db,now-10)['publish'],'clock rollback must not freeze publication')
        with self.db:self.db.execute('INSERT INTO project_rollup_dirty VALUES(?,1)',(hour-2,))
        cold=project_rollups.worker_schedule(self.db,now)
        self.assertTrue(cold['publish']);self.assertEqual(cold['max_hours'],4);self.assertEqual(cold['delay'],.05)
        with self.db:self.db.execute('DELETE FROM project_rollup_dirty')
        self.assertTrue(project_rollups.worker_schedule(self.db,now)['publish'],'metadata-only changes remain cheap and immediate')

    def test_background_gc_drains_retired_versions_and_stops_cleanly(self):
        self.add();self.build()
        for n in range(8):self.add(out=30+n);self.build()
        self.assertGreater(self.db.execute('SELECT count(*) FROM project_rollup_gc').fetchone()[0],1)
        stop=threading.Event();seen=[]
        def update(value):
            seen.append(value)
            if not self.db.execute('SELECT count(*) FROM project_rollup_gc').fetchone()[0]:stop.set()
            if len(seen)>20:stop.set()
        project_rollups.run_worker(self.path,stop,update,duty=.1)
        self.assertTrue(seen)
        self.assertEqual(self.db.execute('SELECT count(*) FROM project_rollup_gc').fetchone()[0],0)
        self.parity()
        self.assertEqual(self.db.execute('SELECT count(DISTINCT version) FROM project_rollup_cost_fine').fetchone()[0],1)

    def test_forced_refresh_reuses_immutable_allocation_and_invalidates_on_publish(self):
        self.add();self.add('other-hour',ts=7300);self.build()
        original=project_rollups.dataset;reads=[]
        def connection(*args):
            db=sqlite3.connect('file:'+self.path+'?mode=ro',uri=True);db.set_trace_callback(lambda sql:reads.append(sql) if sql.startswith('SELECT data FROM project_rollup_payload WHERE version=') else None);return db
        with patch.object(store,'open_readonly',side_effect=connection),patch.object(project_rollups,'dataset',wraps=original) as build:
            reports=server.Reports();a=reports.data(0,True);reports.data(0,True)
            self.assertEqual(build.call_count,1);self.assertEqual(len(reads),2)
            self.add('new',ts=7400);self.build();b=reports.data(0,True)
            self.assertEqual(build.call_count,2);self.assertEqual(len(reads),3,'unchanged hour was decoded again')
            self.assertGreater(sum(r[11] for r in b['llm']),sum(r[11] for r in a['llm']))

    def test_small_batches_publish_recent_hours_and_fair_oldest_debt(self):
        for n in range(6):self.add(str(n),ts=n*3600+1)
        self.build()
        for n in range(6):self.add(str(n),ts=n*3600+1,out=99)
        self.canonical()
        before=dict(self.db.execute('SELECT hour,version FROM project_rollup_heads'))
        project_rollups.rebuild(self.path,max_hours=3)
        after=dict(self.db.execute('SELECT hour,version FROM project_rollup_heads'))
        self.assertEqual({h for h in before if before[h]!=after[h]},{0,4,5})
        self.assertEqual(project_rollups.status(self.db)['pending_hours'],3)
        project_rollups.rebuild(self.path,max_hours=3);self.parity()
        self.assertEqual(project_rollups.status(self.db)['pending_hours'],0)
        cfg=projects.snapshot(self.db);cfg['bindings'][0]['allocations']=[dict(project_key='b',units=1000000)];projects.apply(self.db,cfg)
        project_rollups.rebuild(self.path,max_hours=3)
        self.assertEqual(project_rollups.status(self.db)['pending_hours'],0);self.parity()

    def test_freshness_is_pinned_source_time_and_reads_no_history(self):
        self.add();self.build()
        with self.db:self.db.execute('INSERT OR REPLACE INTO project_rollup_capture VALUES(1,?)',(json.dumps(dict(sampled_at=100,usage_pending=5,work_pending=9)),))
        def auth(action,table,column,*rest):
            return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_READ and table=='history_events' else sqlite3.SQLITE_OK
        self.db.set_authorizer(auth)
        try:s=project_rollups.status(self.db)
        finally:self.db.set_authorizer(None)
        self.assertLessEqual(s['recent_usage_source_at'],s['updated_at'])
        self.assertEqual(s['capture']['usage_pending'],5)

    def test_single_hour_turns_keep_recent_usage_moving_without_starving_oldest(self):
        for n in range(6):self.add(str(n),ts=n*3600+1)
        self.build()
        with self.db:
            project_rollups.put(self.db,batch_turn=0)
            self.db.executemany('INSERT INTO project_rollup_dirty VALUES(?,1)',[(n,) for n in range(6)])
        changed=[]
        for _ in range(3):
            before=dict(self.db.execute('SELECT hour,version FROM project_rollup_heads'))
            project_rollups.rebuild(self.path,max_hours=1)
            after=dict(self.db.execute('SELECT hour,version FROM project_rollup_heads'))
            changed.extend(h for h in before if before[h]!=after[h])
        self.assertEqual(changed,[5,4,0])

    def test_small_batches_keep_cross_hour_corrections_and_mirrors_atomic(self):
        self.add('moving',ts=3701);self.add('other',ts=18001);self.build()
        self.add('moving',ts=7301,out=99);self.canonical()
        project_rollups.rebuild(self.path,max_hours=1);self.parity()
        self.assertEqual(self.read()['totals']['request_equivalents'],2)
        self.add('native',ts=11001,response_id='mirror');self.build()
        self.add('local',ts=14601,tool='local',response_id='mirror');self.canonical()
        project_rollups.rebuild(self.path,max_hours=1);self.parity()
        self.assertEqual(self.read()['totals']['request_equivalents'],3)
        with self.db:self.db.execute("DELETE FROM llm WHERE uid='native'")
        self.canonical();project_rollups.rebuild(self.path,max_hours=1);self.parity()
        self.assertEqual(self.read()['totals']['request_equivalents'],3)

    def test_small_batches_keep_global_prompt_winner_moves_atomic(self):
        self.add();self.build()
        prompt=dict(t='prompt',uid='input',source_uid='same',ts=3701,tool='pi',session='s',project='app',origin='unknown',chars=3,words=1,est_tokens=1)
        store.ingest(self.db,[json.dumps(prompt)],machine='host');self.build()
        store.ingest(self.db,[json.dumps(dict(prompt,uid='better',ts=7301,origin='human'))],machine='other')
        project_rollups.rebuild(self.path,max_hours=1)
        with contextlib.closing(store.open_readonly(self.path)) as db:
            db.execute('BEGIN');self.assertEqual(project_rollups.dataset(db)['prompts'],project_dataset.build(db)['prompts'])

    def test_small_batches_keep_tool_corrections_atomic(self):
        self.add('same',ts=3701);self.build()
        self.add('same',ts=7301,tool='claude');self.canonical()
        project_rollups.rebuild(self.path,max_hours=1);self.parity()
        self.assertEqual(self.read()['totals']['request_equivalents'],1)

    def test_legacy_dirty_debt_migrates_atomically_before_small_batches(self):
        self.add('one',ts=3701);self.add('two',ts=7301);self.add('three',ts=11001);self.build()
        with self.db:
            for (name,) in self.db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'erg_rollup_dependency_%'").fetchall():self.db.execute('DROP TRIGGER '+name)
            self.db.execute('DROP TABLE project_rollup_dependencies')
        self.add('one',ts=3701,out=77);self.add('two',ts=7301,out=88);self.add('three',ts=11001,out=99)
        with self.db:project_rollups.initialize(self.db)
        self.assertTrue(project_rollups.status(self.db)['migration_pending'])
        self.canonical();project_rollups.rebuild(self.path,max_hours=1)
        self.assertEqual(project_rollups.status(self.db)['pending_hours'],0)
        self.assertFalse(project_rollups.status(self.db)['migration_pending']);self.parity()

    def test_new_cross_hour_mutation_survives_an_inflight_publication(self):
        self.add('moving',ts=3701);self.build()
        self.add('moving',ts=7301);self.canonical();changed=[]
        def change(**kwargs):
            if not changed:
                changed.append(True);self.add('moving',ts=3701,out=99);self.canonical()
        project_rollups.rebuild(self.path,change,max_hours=1)
        self.assertEqual(self.read()['totals']['request_equivalents'],1)
        self.assertGreater(self.db.execute('SELECT count(*) FROM project_rollup_dependencies').fetchone()[0],0)
        project_rollups.rebuild(self.path,max_hours=1);self.parity()
        self.assertEqual(self.read()['totals']['request_equivalents'],1)

    def test_dependency_uid_probe_is_a_bounded_covering_seek(self):
        plan=' '.join(r[3] for r in self.db.execute("EXPLAIN QUERY PLAN SELECT ts/3600 FROM llm WHERE tool=? AND uid=?",('pi','one')))
        self.assertIn('SEARCH',plan);self.assertIn('tool=? AND uid=?',plan);self.assertIn('COVERING',plan)

    def test_worker_checkpoints_are_throttled_between_publications(self):
        self.add();self.build();stop=threading.Event();seen=[]
        def update(value):
            if 'checkpoint' in value:
                seen.append(value)
                if len(seen)==3:stop.set()
        with patch.object(project_rollups,'Pacer',return_value=lambda **kwargs:None),patch.object(project_rollups.time,'monotonic',return_value=1000),patch.object(store,'passive_checkpoint',return_value=dict(state='ready',frames=0,checkpointed=0)) as checkpoint:
            project_rollups.run_worker(self.path,stop,update)
        self.assertEqual(checkpoint.call_count,1)
        self.assertEqual(len(seen),3)

    def test_invalidation_diagnostics_count_enqueued_hours_without_source_payload(self):
        self.add();self.canonical()
        rows=self.db.execute('SELECT source,queued_rows,max_batch FROM project_rollup_invalidations').fetchall()
        self.assertTrue(rows);self.assertTrue(all(n>0 and peak>0 for source,n,peak in rows))
        self.assertTrue(all(source.startswith(('erg_project_rollup_','erg_rollup_dependency_')) for source,n,peak in rows))
        self.build();self.parity()

    def test_invalidation_diagnostics_distinguish_historical_hours(self):
        self.add();self.canonical()
        rows=self.db.execute('SELECT source,historical_rows,oldest_hour FROM project_rollup_invalidations WHERE historical_rows>0').fetchall()
        self.assertTrue(rows);self.assertTrue(all(hour==1 for source,n,hour in rows))
        self.assertEqual(project_rollups.state(self.db).get('_dirty_cause'),'')

    def test_previous_hour_corrections_coalesce_with_active_hour(self):
        now=360050;hour=int(now)//3600
        with self.db:
            project_rollups.put(self.db,updated_at=now-1)
            self.db.execute('DELETE FROM project_rollup_dirty')
            self.db.execute('INSERT INTO project_rollup_dirty VALUES(?,1)',(hour-1,))
        plan=project_rollups.worker_schedule(self.db,now)
        self.assertFalse(plan['publish']);self.assertEqual(plan['max_hours'],2)
        self.assertTrue(project_rollups.worker_schedule(self.db,now+5)['publish'])

    def test_prompt_upsert_diagnostics_do_not_override_nested_conflict_policy(self):
        self.add();self.build()
        prompt=dict(t='prompt',uid='human',ts=3701,tool='pi',session='s',origin='human',provenance='user',text='fixture')
        for length in (7,11,19):
            prompt['chars']=length
            store.ingest(self.db,[json.dumps(prompt)],machine='host')
        self.assertEqual(self.db.execute("SELECT chars FROM prompts WHERE uid='human'").fetchone()[0],19)
        self.assertEqual(project_rollups.state(self.db).get('_dirty_cause'),'')
        self.build();self.parity()
