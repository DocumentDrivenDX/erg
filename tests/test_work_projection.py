"""Forward work projection survives source pruning, restarts and source conflicts."""
import hashlib
import json
import sqlite3
import time
import unittest
import zlib
from unittest.mock import patch
from erg import history,history_projection,store,work,work_projection
import test_work_retained as fixtures


class WorkProjection(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.RetainedWork();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.db=self.f.db
    def drain(self,**kwargs):
        with patch.object(work,'capture_file',side_effect=AssertionError('native log reopened')),patch('erg.collect.transcript',side_effect=AssertionError('native log reopened')):
            for _ in range(30):
                result=work_projection.run(self.db,budget=10,**kwargs)
                if not result['pending'] or not result['processed']:return result
            self.fail('queue failed to drain')
    def replace(self,kind,change):
        row=self.db.execute('SELECT event_id,body FROM history_events WHERE kind=? ORDER BY seq LIMIT 1',(kind,)).fetchone()
        body=json.loads(zlib.decompress(row[1]));change(body)
        encoded=json.dumps(body,separators=(',',':')).encode()
        with self.db:self.db.execute('UPDATE history_events SET body=?,content_hash=? WHERE event_id=?',(zlib.compress(encoded),hashlib.sha256(encoded).hexdigest(),row[0]))
    def test_codex_pruning_restart_and_duplicate_delivery_preserve_usage_and_work(self):
        self.f.capture();self.f.native.unlink();protected=self.f.protected()
        first=work_projection.run(self.db,limit=3,budget=10);self.assertEqual(first['processed'],3)
        state=self.db.execute('SELECT value FROM history_work_context').fetchone()[0]
        self.assertIn('commit',json.loads(state)['_tools'])
        reopened=store.connect(str(self.f.path),initialize=False)
        try:
            with patch('erg.collect.transcript',side_effect=AssertionError('native reopened')):
                result=work_projection.run(reopened,budget=10)
            self.assertEqual(result['pending'],0)
        finally:reopened.close()
        self.assertEqual(self.f.protected(),protected)
        self.assertEqual(self.db.execute('SELECT count(*) FROM work_evidence').fetchone()[0],2)
        self.assertEqual(self.db.execute('SELECT sha FROM work_commit_evidence').fetchone()[0],self.f.sha)
        with self.db:self.db.execute('INSERT INTO history_work_pending(event_id,seq,content_hash) SELECT e.event_id,e.seq,e.content_hash FROM history_events e JOIN history_work_receipts r ON r.event_id=e.event_id')
        self.drain();self.assertEqual(self.db.execute('SELECT count(*) FROM work_evidence').fetchone()[0],2)
        self.assertEqual(work_projection.status(self.db)['states'],{'projected':6})
    def test_claude_uses_recorded_repository_and_matches_independent_native_reader(self):
        first=dict(type='user',timestamp=100,uuid='u',sessionId='s',cwd=str(self.f.repo),gitBranch='feature',message=dict(role='user',content='Task-ID: feature-1'))
        records=[first,dict(type='assistant',timestamp=101,uuid='a',sessionId='s',cwd=str(self.f.repo),gitBranch='feature',message=dict(role='assistant',content=[dict(type='tool_use',id='commit',name='Bash',input=dict(command='git commit -am work'))])),dict(type='user',timestamp=102,uuid='r',sessionId='s',cwd=str(self.f.repo),gitBranch='feature',message=dict(role='user',content=[dict(type='tool_result',tool_use_id='commit',content='[feature '+self.f.sha[:7]+'] work\n')]))]
        self.f.native.write_text(''.join(json.dumps(r)+'\n' for r in records));work.capture_file(self.db,'claude',str(self.f.native),True)
        self.f.capture(records,'claude');self.f.native.unlink();before=self.db.execute('SELECT * FROM work_evidence').fetchall()
        self.drain();self.assertEqual(self.db.execute('SELECT * FROM work_evidence').fetchall(),before)
        self.assertEqual(work_projection.status(self.db)['states'],{'projected':3})
    def test_metadata_only_repair_is_not_work_conflict_but_changed_task_is_unknown(self):
        self.f.capture();self.drain();protected=self.f.protected()
        self.replace('session_meta',lambda b:b['payload'].update(creator_account_id='new-owner',route={'provider':'gateway'}));self.drain()
        self.assertEqual(work_projection.status(self.db)['conflicting_sessions'],0)
        self.assertEqual(self.f.protected(),protected)
        self.replace('event_msg',lambda b:b['payload'].update(message='Task-ID: another-task'));self.drain()
        self.assertEqual(work_projection.status(self.db)['conflicting_sessions'],1)
        report=work.effectiveness(self.db)
        self.assertEqual(report['commit_attributed_calls'],0);self.assertEqual(report['automatic_evidence_count'],0)
        self.assertEqual(report['unattributed_reasons'],{'retained_work_source_conflict':1})
        self.assertEqual(self.db.execute('SELECT count(*) FROM work_evidence').fetchone()[0],2)  # Original evidence remains auditable.
    def test_expiration_protects_work_until_its_checkpoint_is_durable(self):
        self.f.capture();first=history.expire(self.db,200,True);self.assertGreater(first['protected_events'],0)
        self.drain();result=history.expire(self.db,200,True)
        self.assertEqual(result['protected_events'],0);self.assertEqual(result['expired_events']+first['expired_events'],7)
        self.assertEqual(self.db.execute('SELECT sha FROM work_commit_evidence').fetchone()[0],self.f.sha)
    def test_source_changed_during_parse_is_retried_without_advancing_checkpoint(self):
        self.f.capture();original=work.record_events;changed=[]
        def parse(*args,**kwargs):
            rows=original(*args,**kwargs)
            if not changed:
                changed.append(True);self.replace('session_meta',lambda b:b['payload']['git'].update(branch='new'))
            return rows
        with patch.object(work,'record_events',side_effect=parse):
            result=work_projection.run(self.db,budget=10)
        self.assertEqual(result['processed'],0);self.assertEqual(self.db.execute('SELECT count(*) FROM history_work_context').fetchone()[0],0)
        self.drain();self.assertEqual(work_projection.status(self.db)['conflicting_sessions'],0)
    def test_writer_failure_rolls_back_checkpoint_and_facts_then_retries(self):
        self.f.capture()
        with patch.object(store,'ingest',side_effect=RuntimeError('write failed')):
            with self.assertRaises(RuntimeError):work_projection.run(self.db,budget=10)
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(self.db.execute('SELECT count(*) FROM history_work_context').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM work_contexts').fetchone()[0],0)
        self.drain();self.assertEqual(work_projection.status(self.db)['pending'],0)
    def test_missing_header_is_explicit_and_does_not_guess_task_state(self):
        self.f.capture()
        with self.db:
            self.db.execute("DELETE FROM history_work_pending WHERE event_id IN (SELECT event_id FROM history_events WHERE kind='session_meta')")
            self.db.execute("DELETE FROM history_events WHERE kind='session_meta'")
        self.drain()
        status=work_projection.status(self.db)
        self.assertIn('session_header_not_projected',status['gaps'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM work_evidence').fetchone()[0],0)
    def test_no_historical_queue_fill_and_idle_run_has_no_writer(self):
        self.f.capture();self.drain()
        with self.db:self.db.execute('DELETE FROM history_work_receipts')
        work_projection.initialize(self.db)
        self.assertEqual(work_projection.status(self.db)['pending'],0)
        queries=[];self.db.set_trace_callback(queries.append)
        try:self.assertEqual(work_projection.run(self.db)['processed'],0)
        finally:self.db.set_trace_callback(None)
        self.assertFalse(any('BEGIN' in q.upper() for q in queries))
        for limit,budget in ((True,1),(0,1),(32,float('nan')),(32,0)):
            with self.assertRaises(ValueError):work_projection.run(self.db,limit,budget)

    def test_activation_ignores_pre_activation_metadata_repairs(self):
        self.f.capture()
        with self.db:
            self.db.execute('DELETE FROM history_work_pending')
            self.db.execute("DELETE FROM history_config WHERE key='work_projection_start_seq_v1'")
        work_projection.initialize(self.db)
        watermark=work_projection.status(self.db)['forward_start_seq']
        self.assertEqual(watermark,7)
        self.replace('session_meta',lambda b:b['payload'].update(creator_account_id='owner'))
        self.assertEqual(work_projection.status(self.db)['pending'],0)
        with self.db:history.put(self.db,'codex','s','p',200,'turn_context',dict(type='turn_context',timestamp=200,payload=dict(cwd=str(self.f.repo),model='model')),'new-turn')
        self.assertEqual(work_projection.status(self.db)['pending'],1)

    def test_recorded_repository_changed_during_plan_cannot_publish(self):
        first=dict(type='user',timestamp=100,uuid='u',sessionId='s',cwd=str(self.f.repo),gitBranch='feature',message=dict(role='user',content='Task-ID: feature-1'))
        self.f.native.write_text(json.dumps(first)+'\n');work.capture_file(self.db,'claude',str(self.f.native),True)
        self.f.capture([first],'claude');original=work.record_events
        def changed(*args,**kwargs):
            rows=original(*args,**kwargs)
            with self.db:self.db.execute("UPDATE work_contexts SET repo='github.com/acme/changed'")
            return rows
        with patch.object(work,'record_events',side_effect=changed):result=work_projection.run(self.db,budget=10)
        self.assertEqual(result['processed'],0);self.assertEqual(result['pending'],1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM history_work_context').fetchone()[0],0)

    def test_retry_dependency_query_does_not_scan_session_corpus_without_retries(self):
        self.f.capture()
        def steps():
            count=[0]
            self.db.set_progress_handler(lambda:count.__setitem__(0,count[0]+1) or 0,1)
            try:self.db.execute(work_projection.CANDIDATES_SQL,(int(time.time()),int(time.time()),32)).fetchall()
            finally:self.db.set_progress_handler(None,0)
            return count[0]
        small=steps()
        with self.db:
            for index in range(1500):history.put(self.db,'codex','s','p',200,'token_usage_record',dict(type='token_usage_record',timestamp=200,payload={}),str(index))
        large=steps()
        self.assertLessEqual(large,small+100)

    def test_retrying_prefix_blocks_its_session_without_blocking_other_sessions(self):
        self.f.capture()
        with self.db:
            self.db.execute("UPDATE history_work_pending SET retry_at=? WHERE seq=(SELECT min(seq) FROM history_work_pending)",(int(time.time())+60,))
            header=self.f.records()[0];header['payload']['id']='other'
            history.put(self.db,'codex','other','p',100,'session_meta',header,'other-header')
        result=work_projection.run(self.db,budget=10)
        self.assertEqual(result['processed'],1);self.assertEqual(result['pending'],6)
        self.assertEqual(self.db.execute('SELECT session FROM history_work_context').fetchall(),[('other',)])

    def test_known_gap_does_not_decode_more_bodies_or_invent_source_conflict(self):
        self.f.capture()
        with self.db:
            self.db.execute("DELETE FROM history_work_pending WHERE event_id IN (SELECT event_id FROM history_events WHERE kind='session_meta')")
            self.db.execute("DELETE FROM history_events WHERE kind='session_meta'")
        work_projection.run(self.db,limit=1,budget=10)
        with patch.object(work_projection.zlib,'decompress',side_effect=AssertionError('known gap decoded again')):
            self.drain()
        self.assertEqual(work_projection.status(self.db)['conflicting_sessions'],0)
        # Metadata repair of an unparsed record stays a known gap; it cannot
        # invalidate independently observed native evidence.
        self.replace('response_item',lambda b:b.update(creator_account_id='owner'))
        self.drain();self.assertEqual(work_projection.status(self.db)['conflicting_sessions'],0)

    def test_wall_wait_does_not_exhaust_small_cpu_budget(self):
        self.f.capture();counter=[100.0]
        def wall():counter[0]+=.001;return counter[0]
        with patch.object(work_projection.time,'monotonic',side_effect=wall),patch.object(work_projection.time,'thread_time',return_value=1):
            result=work_projection.run(self.db)
        self.assertEqual(result['pending'],0)
        self.assertEqual(result['processed'],6)

    def test_missing_codex_repository_metadata_waits_for_recorded_native_context(self):
        records=self.f.records();records[0]['payload'].pop('git');self.f.capture(records)
        with patch.object(work_projection.time,'time',return_value=101):
            first=work_projection.run(self.db,budget=10)
            self.assertEqual(first['errors'],1)
            self.assertEqual(self.db.execute('SELECT count(*) FROM history_work_context').fetchone()[0],0)
            work.capture_file(self.db,'codex',str(self.f.native),True)
            with self.db:self.db.execute('UPDATE history_work_pending SET retry_at=0')
            self.drain()
        self.assertEqual(work_projection.status(self.db)['pending'],0)
        self.assertEqual(work_projection.status(self.db)['conflicting_sessions'],0)
        self.assertEqual(work_projection.status(self.db)['states'],{'projected':6})

    def test_deleted_worktree_replay_matches_native_scope_and_facts(self):
        other=self.f.root/'other'
        self.f.git('-C',self.f.repo,'worktree','add','-b','other',other)
        visit=dict(type='response_item',timestamp=102,payload=dict(type='function_call',call_id='visit',name='functions.exec_command',arguments=json.dumps(dict(cmd='git status --short',workdir=str(other)))))
        done=dict(type='response_item',timestamp=102,payload=dict(type='function_call_output',call_id='visit',output=json.dumps(dict(output='',exit_code=0))))
        records=self.f.records();records[3:3]=[visit,done]
        self.f.capture(records);work.capture_file(self.db,'codex',str(self.f.native),True)
        expected=self.db.execute('SELECT * FROM work_evidence ORDER BY uid').fetchall()
        protected=self.f.protected()
        self.f.git('-C',self.f.repo,'worktree','remove','--force',other);self.f.native.unlink()
        self.drain()
        self.assertEqual(work_projection.status(self.db)['conflicting_sessions'],0)
        self.assertEqual(work_projection.status(self.db)['states'],{'projected':8})
        self.assertEqual(self.db.execute('SELECT * FROM work_evidence ORDER BY uid').fetchall(),expected)
        self.assertEqual(self.f.protected(),protected)
        from erg import work_retained
        repair=work_retained.replay(self.db,'codex','s',machine='host',budget=10)
        self.assertEqual(repair['conflicts'],0);self.assertEqual(repair['insertable'],0)

    def test_explicit_header_does_not_override_ambiguous_recorded_repository(self):
        state={};facts=[]
        for body in self.f.records():
            facts.extend(work.record_events('codex',body,state,str(self.f.native),0,include_metadata=False,include_prompts=False,repo_context=lambda cwd:dict(ambiguous=True)))
        self.assertFalse([row for row in facts if row['t'] in ('work_evidence','work_commit')])
        with self.db:
            rows=[work.context_event('codex','s',100,str(self.f.repo),repo=repo) for repo in ('github.com/acme/r','github.com/other/r')]
            store.ingest(self.db,map(json.dumps,rows),machine='host',transaction=False)
        witness=work_projection.observed_repository(self.db,dict(machine='host',tool='codex',session='s',ts=101),str(self.f.repo))
        self.assertEqual(witness,dict(ambiguous=True))

    def gap_prefix(self):
        self.f.capture()
        with self.db:
            self.db.execute("DELETE FROM history_work_pending WHERE event_id IN (SELECT event_id FROM history_events WHERE kind='session_meta')")
            self.db.execute("DELETE FROM history_events WHERE kind='session_meta'")
        work_projection.run(self.db,limit=1,budget=10)

    def test_gap_batch_matches_individual_receipts_without_body_or_fact_changes(self):
        self.gap_prefix();baseline=store.connect(str(self.f.root/'baseline'));self.addCleanup(baseline.close)
        self.db.backup(baseline)
        before=self.db.execute('SELECT * FROM history_events ORDER BY seq').fetchall()
        traces=[];self.db.set_trace_callback(traces.append)
        try:
            with patch.object(work_projection.time,'time',return_value=2000),patch.object(work_projection.zlib,'decompress',side_effect=AssertionError('gap body decoded')):
                actual=work_projection.run(self.db,budget=10)
                with patch.object(work_projection,'acknowledge_gap_prefix',return_value=None):expected=work_projection.run(baseline,budget=10)
        finally:self.db.set_trace_callback(None)
        self.assertEqual(actual['gap_batch_records'],4);self.assertEqual(actual['processed'],expected['processed'])
        self.assertEqual(sum(q=='BEGIN IMMEDIATE' for q in traces),1)
        for table in ('history_work_receipts','history_work_context','history_work_pending','work_contexts','work_evidence','work_commit_evidence','observations','prompts','llm'):
            self.assertEqual(self.db.execute('SELECT * FROM '+table+' ORDER BY 1,2').fetchall(),baseline.execute('SELECT * FROM '+table+' ORDER BY 1,2').fetchall(),table)
        self.assertEqual(self.db.execute('SELECT * FROM history_events ORDER BY seq').fetchall(),before)

    def test_gap_batch_is_bounded_even_with_large_caller_limit(self):
        self.gap_prefix()
        with self.db:
            for i in range(80):history.put(self.db,'codex','s','p',200,'event_msg',dict(type='event_msg',timestamp=200,payload=dict(type='user_message',message='hello')),str(i))
        before=work_projection.status(self.db)['pending'];result=work_projection.run(self.db,limit=1024,budget=10)
        self.assertEqual(result['gap_batch_records'],32);self.assertEqual(result['pending'],before-32)

    def test_gap_batch_writer_failure_rolls_back_all_receipts_and_progress(self):
        self.gap_prefix()
        before={table:self.db.execute('SELECT * FROM '+table+' ORDER BY 1').fetchall() for table in ('history_work_receipts','history_work_context','history_work_pending')}
        with self.db:self.db.execute("CREATE TRIGGER fail_gap_receipt BEFORE INSERT ON history_work_receipts WHEN NEW.event_id=(SELECT event_id FROM history_work_pending ORDER BY seq LIMIT 1 OFFSET 1) BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):work_projection.run(self.db,budget=10)
        self.assertFalse(self.db.in_transaction)
        for table,rows in before.items():self.assertEqual(self.db.execute('SELECT * FROM '+table+' ORDER BY 1').fetchall(),rows)
        with self.db:self.db.execute('DROP TRIGGER fail_gap_receipt')
        self.assertEqual(work_projection.run(self.db,budget=10)['gap_batch_records'],4)

    def test_gap_batch_rejects_checkpoint_change_before_commit(self):
        self.gap_prefix();original=work_projection.json.dumps;changed=[]
        def concurrent(value,*args,**kwargs):
            if isinstance(value,dict) and value.get('_work_gap')=='preceding_work_record_unavailable' and not changed:
                changed.append(True)
                with self.db:self.db.execute("UPDATE history_work_context SET value=?",(original(dict(_work_gap='other-writer')),))
            return original(value,*args,**kwargs)
        pending=work_projection.status(self.db)['pending']
        with patch.object(work_projection.json,'dumps',side_effect=concurrent):result=work_projection.run(self.db,budget=10)
        self.assertEqual(result['processed'],0);self.assertEqual(result['pending'],pending)
        self.assertEqual(json.loads(self.db.execute('SELECT value FROM history_work_context').fetchone()[0]),dict(_work_gap='other-writer'))

    def test_gap_batch_cannot_jump_over_a_new_earlier_source_revision(self):
        self.gap_prefix();original=work_projection.json.dumps;changed=[]
        def concurrent(value,*args,**kwargs):
            if isinstance(value,dict) and value.get('_work_gap')=='preceding_work_record_unavailable' and not changed:
                changed.append(True);self.replace('event_msg',lambda b:b['payload'].update(message='Task-ID: changed'))
            return original(value,*args,**kwargs)
        before=work_projection.status(self.db)['pending']
        with patch.object(work_projection.json,'dumps',side_effect=concurrent):result=work_projection.run(self.db,budget=10)
        self.assertEqual(result['processed'],0);self.assertEqual(result['pending'],before+1)
        self.assertEqual(self.db.execute('SELECT min(seq) FROM history_work_pending').fetchone()[0],2)

    def test_known_noop_shapes_keep_full_bodies_and_usage_without_work_enqueues(self):
        header=self.f.records()[0]
        with self.db:history.put(self.db,'codex','s','p',100,'session_meta',header,'header')
        bodies=[dict(type='response_item',timestamp=201,payload=dict(type='reasoning',summary='PRIVATE REASONING')),
                dict(type='response_item',timestamp=202,payload=dict(type='message',role='assistant',content=[dict(type='output_text',text='Task-ID: false-task PRIVATE TEXT')])),
                dict(type='event_msg',timestamp=203,payload=dict(type='agent_message',message='PRIVATE TEXT')),
                dict(type='event_msg',timestamp=204,payload=dict(type='agent_reasoning',text='PRIVATE REASONING')),
                dict(type='event_msg',timestamp=205,payload=dict(type='token_count',info=dict(total_token_usage=dict(input_tokens=30,cached_input_tokens=10,output_tokens=4,total_tokens=34)),rate_limits=dict(plan_type='pro',limit_id='codex',primary=dict(used_percent=90,window_minutes=300,resets_at=999))))]
        bodies.extend(dict(type='event_msg',timestamp=206+i,payload=dict(type=kind,message='Task-ID: not-a-task',item={'type':'function_call','name':'exec_command','arguments':'git commit'})) for i,kind in enumerate(('item_completed','thread_settings_applied','task_started','task_complete')))
        state={};work.record_events('codex',header,state,str(self.f.native),0,include_metadata=False,include_prompts=False)
        before=json.loads(json.dumps(state))
        with self.db:
            for i,body in enumerate(bodies):
                self.assertEqual(work.record_events('codex',body,state,str(self.f.native),i+1,include_metadata=False,include_prompts=False),[])
                history.put(self.db,'codex','s','p',body['timestamp'],body['type'],body,'noop'+str(i))
        self.assertEqual(state,before)
        self.assertEqual(work_projection.status(self.db)['pending'],1)
        self.assertEqual([e['body'] for e in history.events(self.db,'codex','s')['events']][1:],bodies)
        self.assertEqual(self.db.execute("SELECT count(*) FROM history_events WHERE projection_hint='codex_work_noop'").fetchone()[0],len(bodies))
        history_projection.run(self.db,budget=10)
        counter=history_projection.latest_counter(self.db,'codex','s');self.assertEqual((counter['input_tokens'],counter['output_tokens']),(30,4))
        self.assertEqual(self.db.execute("SELECT used_percent FROM quota_events WHERE window='primary'").fetchone()[0],90)
        self.assertEqual(self.db.execute('SELECT count(*) FROM calls').fetchone()[0],0)

    def test_tool_user_patch_and_unknown_shapes_still_enter_work_queue(self):
        bodies=[dict(type='response_item',timestamp=200,payload=dict(type='function_call',call_id='a',name='exec_command',arguments='{}')),
                dict(type='response_item',timestamp=201,payload=dict(type='function_call_output',call_id='a',output='done')),
                dict(type='response_item',timestamp=202,payload=dict(type='custom_tool_call',call_id='patch',name='apply_patch',input='*** Begin Patch\n*** End Patch')),
                dict(type='response_item',timestamp=203,payload=dict(type='message',role='user',content=[dict(text='Task-ID: real')])),
                dict(type='event_msg',timestamp=204,payload=dict(type='user_message',message='hello')),
                dict(type='response_item',timestamp=205,payload=dict(type='future_tool_event')),
                dict(type='event_msg',timestamp=207,payload=dict(type='future_lifecycle_event'))]
        with self.db:
            for i,body in enumerate(bodies):history.put(self.db,'codex','s','p',body['timestamp'],body['type'],body,str(i))
        self.assertEqual(work_projection.status(self.db)['pending'],7)
        self.assertEqual(self.db.execute("SELECT projection_hint FROM history_events WHERE ts=202").fetchone()[0],'codex_patch')
        old=dict(type='response_item',timestamp=206,payload=dict(type='message',role='assistant',content=[]))
        new=dict(type='response_item',timestamp=206,payload=dict(type='function_call',name='exec_command',call_id='changed',arguments='{}'))
        with self.db:history.put(self.db,'codex','s','p',206,'response_item',old,'mutable')
        self.assertEqual(work_projection.status(self.db)['pending'],7)
        with self.db:history.put(self.db,'codex','s','p',206,'response_item',new,'mutable')
        self.assertEqual(work_projection.status(self.db)['pending'],8)

    def test_noop_trigger_upgrade_preserves_existing_queue_and_boundary(self):
        for name in ('history_work_enqueue_insert','history_work_enqueue_update'):
            with self.db:self.db.execute('DROP TRIGGER '+name)
        legacy=work_projection.SCHEMA.replace("AND COALESCE(NEW.projection_hint,'')<>'codex_work_noop' ",'').replace("AND (COALESCE(NEW.projection_hint,'')<>'codex_work_noop' OR COALESCE(OLD.projection_hint,'')<>'codex_work_noop') ",'')
        self.db.executescript(legacy);self.db.commit()
        body=dict(type='response_item',timestamp=200,payload=dict(type='message',role='assistant',content=[]))
        with self.db:history.put(self.db,'codex','s','p',200,'response_item',body,'old')
        old=self.db.execute('SELECT * FROM history_work_pending').fetchall();boundary=work_projection.status(self.db)['forward_start_seq']
        work_projection.initialize(self.db)
        with self.db:history.put(self.db,'codex','s','p',201,'response_item',body,'new')
        self.assertEqual(self.db.execute('SELECT * FROM history_work_pending').fetchall(),old)
        self.assertEqual(work_projection.status(self.db)['forward_start_seq'],boundary)
        self.assertEqual(self.db.execute('SELECT count(*) FROM history_events').fetchone()[0],2)
        work_projection.initialize(self.db)
        self.assertEqual(self.db.execute('SELECT * FROM history_work_pending').fetchall(),old)

    def test_work_to_noop_mutation_is_queued_and_keeps_conflict_visible(self):
        header=self.f.records()[0]
        original=dict(type='response_item',timestamp=200,payload=dict(type='function_call',call_id='mutable',name='exec_command',arguments=json.dumps(dict(cmd='git status',workdir=str(self.f.repo)))))
        replacement=dict(type='response_item',timestamp=200,payload=dict(type='message',role='assistant',content=[]))
        with self.db:
            history.put(self.db,'codex','s','p',100,'session_meta',header,'header')
            history.put(self.db,'codex','s','p',200,'response_item',original,'mutable')
        self.drain()
        with self.db:history.put(self.db,'codex','s','p',200,'response_item',replacement,'mutable')
        self.assertEqual(work_projection.status(self.db)['pending'],1)
        self.drain();self.assertEqual(work_projection.status(self.db)['conflicting_sessions'],1)

    def test_prepares_ordered_work_outside_one_bounded_writer_transaction(self):
        self.f.capture();trace=[];original=work.record_events
        def parse(*args,**kwargs):
            self.assertFalse(self.db.in_transaction)
            return original(*args,**kwargs)
        self.db.set_trace_callback(trace.append)
        try:
            with patch.object(work,'record_events',side_effect=parse):r=work_projection.run(self.db,budget=10)
        finally:self.db.set_trace_callback(None)
        self.assertGreater(r['processed'],1)
        self.assertEqual(sum(q=='BEGIN IMMEDIATE' for q in trace),1)
        self.drain();self.assertEqual(work_projection.status(self.db)['pending'],0)


    def test_preexisting_conflict_batches_match_individual_unknown_receipts(self):
        self.f.capture()
        with self.db:self.db.execute("INSERT INTO history_work_conflicts VALUES('host','codex','s','conflicting_existing_work_fact')")
        baseline=sqlite3.connect(':memory:');self.addCleanup(baseline.close);self.db.backup(baseline)
        before=self.db.execute('SELECT * FROM history_events ORDER BY seq').fetchall()
        pending=work_projection.status(self.db)['pending'];trace=[]
        self.db.set_trace_callback(trace.append)
        try:
            with patch.object(work_projection.time,'time',return_value=1000),patch.object(work_projection.zlib,'decompress',side_effect=AssertionError('conflict body decoded')),patch.object(work,'record_events',side_effect=AssertionError('conflict parsed')):
                actual=work_projection.run(self.db,budget=10)
                for _ in range(pending):work_projection.run(baseline,limit=1,budget=10)
        finally:self.db.set_trace_callback(None)
        self.assertEqual(actual['processed'],pending);self.assertEqual(actual['pending'],0);self.assertEqual(actual['inserted'],0)
        self.assertEqual(sum(q=='BEGIN IMMEDIATE' for q in trace),1)
        for table in ('history_work_receipts','history_work_context','history_work_conflicts','work_contexts','work_evidence','work_commit_evidence','observations','prompts','llm'):
            self.assertEqual(sorted(self.db.execute('SELECT * FROM '+table)),sorted(baseline.execute('SELECT * FROM '+table)),table)
        self.assertEqual(self.db.execute('SELECT * FROM history_events ORDER BY seq').fetchall(),before)
        self.assertEqual(work_projection.status(self.db)['states'],{'conflicting':pending})

    def test_larger_conflict_batch_stays_bounded_and_rolls_back_atomically(self):
        self.f.capture()
        with self.db:
            self.db.execute("INSERT INTO history_work_conflicts VALUES('host','codex','s','conflicting_existing_work_fact')")
            for i in range(180):history.put(self.db,'codex','s','p',200+i,'event_msg',dict(type='event_msg',timestamp=200+i,payload=dict(type='user_message',message='private')),str(i))
        tables=('history_work_receipts','history_work_context','history_work_pending','work_contexts','work_evidence','history_events')
        before={table:self.db.execute('SELECT * FROM '+table+' ORDER BY 1').fetchall() for table in tables}
        with self.db:
            self.db.execute('CREATE TABLE work_batch_fault(event_id TEXT)')
            self.db.execute('INSERT INTO work_batch_fault SELECT event_id FROM history_work_pending ORDER BY seq LIMIT 1 OFFSET 70')
            self.db.execute("CREATE TRIGGER fail_large_work_batch BEFORE INSERT ON history_work_receipts WHEN NEW.event_id=(SELECT event_id FROM work_batch_fault) BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):work_projection.run(self.db,limit=1024,budget=10)
        self.assertFalse(self.db.in_transaction)
        for table,rows in before.items():self.assertEqual(self.db.execute('SELECT * FROM '+table+' ORDER BY 1').fetchall(),rows,table)
        with self.db:self.db.execute('DROP TRIGGER fail_large_work_batch')
        result=work_projection.run(self.db,limit=1024,budget=10)
        self.assertEqual(result['processed'],128);self.assertEqual(result['pending'],len(before['history_work_pending'])-128);self.assertEqual(result['inserted'],0)
        self.assertEqual(work_projection.status(self.db)['states'],{'conflicting':128})
        self.assertEqual(self.db.execute('SELECT * FROM history_events ORDER BY 1').fetchall(),before['history_events'])

    def test_demand_worker_drains_beyond_poll_batch_then_sleeps_with_exact_bytes(self):
        self.f.capture()
        with self.db:
            self.db.execute("INSERT INTO history_work_conflicts VALUES('host','codex','s','conflicting_existing_work_fact')")
            for i in range(260):history.put(self.db,'codex','s','p',200+i,'event_msg',dict(type='event_msg',timestamp=200+i,payload=dict(type='user_message',message='private')),str(i))
        count,size=self.db.execute('SELECT count(*),sum(e.bytes) FROM history_work_pending q JOIN history_events e USING(event_id)').fetchone()
        results=[]
        class Stop:
            stopped=False
            def is_set(inner):return inner.stopped
            def wait(inner,delay):
                self.assertFalse(self.db.in_transaction)
                if results[-1]['pending']:
                    self.assertLess(delay,2)
                else:
                    self.assertEqual(delay,2);inner.stopped=True
                return inner.stopped
        work_projection.run_worker(self.f.path,Stop(),results.append)
        self.assertGreater(len(results),1);self.assertEqual(sum(r['processed'] for r in results),count)
        self.assertEqual(results[-1]['processed_total'],count);self.assertEqual(results[-1]['processed_bytes_total'],size)
        self.assertEqual(results[-1]['pending'],0);self.assertEqual(results[-1]['mode'],'idle')
        self.assertTrue(all(r['processed']<=128 for r in results))
        self.assertEqual(self.db.execute('SELECT count(*) FROM work_evidence').fetchone()[0],0)

    def test_demand_worker_empty_and_retry_blocked_queues_wait_instead_of_spin(self):
        for pending in (0,1):
            waits=[];updates=[]
            class Stop:
                stopped=False
                def is_set(inner):return inner.stopped
                def wait(inner,delay):waits.append(delay);inner.stopped=True
            with patch.object(work_projection,'run',return_value=dict(processed=0,pending=pending,errors=pending,processed_bytes=0)) as run:
                work_projection.run_worker(self.f.path,Stop(),updates.append)
            self.assertEqual(run.call_count,1);self.assertEqual(waits,[2])
            self.assertEqual(updates[0]['mode'],'idle' if not pending else 'blocked')

    def test_demand_worker_failure_rolls_back_before_backoff(self):
        waits=[];updates=[]
        class Stop:
            stopped=False
            def is_set(inner):return inner.stopped
            def wait(inner,delay):waits.append(delay);inner.stopped=True
        def fail(db):
            db.execute("INSERT INTO history_config VALUES('uncommitted-worker-proof','private')")
            raise RuntimeError('private diagnostic must not be exposed')
        with patch.object(work_projection,'run',side_effect=fail):work_projection.run_worker(self.f.path,Stop(),updates.append)
        self.assertEqual(waits,[2]);self.assertEqual(updates[0]['error'],'RuntimeError')
        self.assertIsNone(self.db.execute("SELECT value FROM history_config WHERE key='uncommitted-worker-proof'").fetchone())

    def test_shared_worker_drains_usage_and_work_and_preserves_canonical_tokens(self):
        self.f.capture();before=self.f.protected();history_projection.requeue(self.db,tool='codex',session='s')
        expected=self.db.execute('SELECT sum(e.bytes) FROM history_pending q JOIN history_events e USING(event_id)').fetchone()[0]
        results=[]
        class Stop:
            stopped=False
            def is_set(inner):return inner.stopped
            def wait(inner,delay):
                if results[-1]['mode']=='idle':inner.stopped=True
        work_projection.run_worker(self.f.path,Stop(),results.append,include_usage=True)
        self.assertEqual(results[-1]['usage_projection']['pending'],0)
        self.assertEqual(results[-1]['pending'],0);self.assertEqual(results[-1]['edit_projection']['pending'],0)
        self.assertEqual(results[-1]['usage_processed_bytes_total'],expected)
        self.assertEqual(self.f.protected(),before)
        self.assertEqual(self.db.execute('SELECT count(*) FROM work_evidence').fetchone()[0],2)

    def test_managed_capture_delegates_work_without_inline_projection(self):
        self.f.capture();pending=work_projection.status(self.db)['pending']
        with patch.object(history,'config',return_value=dict(content=True,enrolled_at=1)),patch.object(work_projection,'run',side_effect=AssertionError('duplicate inline work projection')),patch.object(history_projection,'run',side_effect=AssertionError('duplicate inline usage projection')):
            result=history.scan(self.db,paths=[],scan_sqlite=False,project_work=False,project_usage=False)
        self.assertEqual(result['work_projection']['mode'],'independent_worker')
        self.assertEqual(result['work_projection']['pending'],pending)
        self.assertEqual(work_projection.status(self.db)['pending'],pending)

    def test_new_conflict_still_defers_later_prepared_facts(self):
        self.f.capture();work.capture_file(self.db,'codex',str(self.f.native),True)
        pending=work_projection.status(self.db)['pending']
        before=self.db.execute('SELECT * FROM work_evidence ORDER BY uid').fetchall()
        with patch.object(work_projection.work_retained,'compatible',return_value=False):first=work_projection.run(self.db,budget=10)
        self.assertEqual(first['processed'],1);self.assertEqual(first['pending'],pending-1)
        self.assertEqual(self.db.execute('SELECT * FROM work_evidence ORDER BY uid').fetchall(),before)
        second=work_projection.run(self.db,budget=10)
        self.assertEqual(second['pending'],0);self.assertEqual(second['processed'],pending-1);self.assertEqual(second['inserted'],0)
        self.assertEqual(work_projection.status(self.db)['states'],{'conflicting':pending})

    def test_batch_context_and_evidence_match_single_record_transactions(self):
        self.f.capture();baseline=sqlite3.connect(':memory:');self.addCleanup(baseline.close);self.db.backup(baseline)
        with patch.object(work_projection.time,'time',return_value=1000):
            for _ in range(20):
                if not work_projection.run(self.db,budget=10)['pending']:break
            for _ in range(20):
                if not work_projection.run(baseline,limit=1,budget=10)['pending']:break
        for table in ('work_contexts','work_evidence','work_commit_evidence','history_work_context','history_work_receipts'):
            self.assertEqual(sorted(self.db.execute('SELECT * FROM '+table)),sorted(baseline.execute('SELECT * FROM '+table)),table)


class WorkContextLookupEfficiency(unittest.TestCase):
    def test_scoped_index_reduces_witness_work_without_changing_ambiguity(self):
        db=sqlite3.connect(':memory:');self.addCleanup(db.close);work.initialize(db)
        cwd='/fixture/checkout';tree=hashlib.sha256(cwd.encode()).hexdigest()[:16]
        fields='machine,uid,tool,session,worktree,ts,capture_version,repo,branch,head_sha,task_id,scope_id'
        rows=[('host',str(i),'codex','session','unrelated',i,3,'other','main','head','task','scope') for i in range(1,1001)]
        rows += [('host','target','codex','session',tree,1,3,'repo','feature','sha','task','scope'),
                 ('other-host','foreign','codex','session',tree,1001,3,'foreign','main','head','task','scope'),
                 ('host','legacy','codex','session',tree,1002,2,'legacy','main','head','task','scope')]
        db.executemany('INSERT INTO work_contexts('+fields+') VALUES('+','.join('?' for _ in fields.split(','))+')',rows)
        db.commit();event=dict(machine='host',tool='codex',session='session',ts=2000)
        def lookup():
            return work_projection.observed_context(db,event,cwd),work_projection.observed_repository(db,event,cwd)
        def counted():
            steps=[0]
            def progress():steps[0]+=1;return 0
            db.set_progress_handler(progress,1)
            try:result=lookup()
            finally:db.set_progress_handler(None,0)
            return result,steps[0]
        db.execute('DROP INDEX work_context_witness');expected,original=counted()
        work.initialize(db);actual,indexed=counted()
        self.assertEqual(actual,expected);self.assertEqual(actual[0]['head_sha'],'sha')
        self.assertEqual(actual[1],dict(repo='repo'));self.assertLess(indexed,original//4)
        # The same latest timestamp can contain conflicting repository witnesses.
        # Index ordering must not choose one repository or hide ties.
        db.execute('INSERT INTO work_contexts('+fields+') VALUES('+','.join('?' for _ in fields.split(','))+')',('host','conflict','codex','session',tree,1,3,'conflicting','feature','sha','task','scope'))
        self.assertIsNone(work_projection.observed_context(db,event,cwd))
        self.assertEqual(work_projection.observed_repository(db,event,cwd),dict(ambiguous=True))
        work.initialize(db)
        self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name='work_context_witness'").fetchone()[0],1)
