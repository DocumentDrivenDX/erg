"""Reporting survives native pruning, replay, mutation and projection failures."""
import json
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from erg import collect,history,history_projection as projection,local_proxy,store

class DurableProjection(unittest.TestCase):
 def setUp(self):
  tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
  for obj,key,value in ((collect,'HOME',str(self.root)),(collect,'STATE_DIR',str(self.root/'state')),(collect,'machine_name',lambda:'host')):
   p=patch.object(obj,key,value);p.start();self.addCleanup(p.stop)
  self.db=store.connect(str(self.root/'erg.db'));self.addCleanup(self.db.close)
 def put(self,tool,body,ident='event',session='s',role='',kind=None,ts=100):
  with self.db:return history.put(self.db,tool,session,'project',ts,kind or body.get('type','message'),body,ident,role=role,source='/removed/native.jsonl')[0]
 def project(self):return projection.run(self.db,limit=1000,budget=10)
 def pi(self,output=3,**extra):
  message=dict(role='assistant',model='m',provider='custom',responseId='r',usage=dict(input=5,output=output,cacheRead=2,cacheWrite=1));message.update(extra)
  return dict(type='message',id='a',timestamp=100,message=message)
 def test_batch_reuses_context_with_session_isolation_and_durable_restart(self):
  for sid,model in [('s','first'),('other','second')]:
   self.put('pi',dict(type='model_change',modelId=model),ident='model',session=sid)
  for sid in ('s','other','s','other'):
   ident='message-'+str(self.db.execute('SELECT count(*) FROM history_events').fetchone()[0])
   self.put('pi',dict(type='message',id=ident,message=dict(role='assistant',usage=dict(input=2,output=1))),ident=ident,session=sid)
  statements=[];self.db.set_trace_callback(statements.append)
  try:self.assertEqual(self.project()['errors'],0)
  finally:self.db.set_trace_callback(None)
  reads=[s for s in statements if s.startswith('SELECT value FROM history_projection_context')]
  self.assertEqual(len(reads),2)
  self.assertEqual(self.db.execute('SELECT session,model,count(*) FROM calls GROUP BY session,model ORDER BY session').fetchall(),[('other','second',2),('s','first',2)])
  self.put('pi',dict(type='message',id='later',message=dict(role='assistant',usage=dict(input=3,output=2))),ident='later')
  self.project()
  self.assertEqual(self.db.execute("SELECT model FROM calls WHERE uid='pi:s:later'").fetchone()[0],'first')
 def test_failed_plan_cannot_poison_later_context_in_same_batch(self):
  self.put('pi',dict(type='model_change',modelId='good'),ident='good')
  self.put('pi',dict(type='model_change',modelId='poison'),ident='bad')
  self.put('pi',dict(type='message',id='after',message=dict(role='assistant',usage=dict(input=2,output=1))),ident='after')
  original=projection.plan
  def fail(db,event,body,state):
   result=original(db,event,body,state)
   if body.get('modelId')=='poison':raise ValueError('failed after state mutation')
   return result
  with patch.object(projection,'plan',side_effect=fail):result=self.project()
  self.assertEqual((result['processed'],result['errors'],result['pending']),(2,1,1))
  self.assertEqual(self.db.execute("SELECT model FROM calls WHERE uid='pi:s:after'").fetchone()[0],'good')
  state=json.loads(self.db.execute("SELECT value FROM history_projection_context WHERE tool='pi' AND session='s'").fetchone()[0])
  self.assertEqual(state['model'],'good')
 def test_large_pending_queue_reads_only_an_ordered_prefix(self):
  with self.db:
   self.db.executemany("INSERT INTO history_events(event_id,machine,tool,session,kind,body,content_hash,bytes) VALUES(?,'host','pi','s','session',x'7b7d','hash',2)",[(str(i),) for i in range(1000)])
   self.db.execute('DELETE FROM history_pending')
   self.db.executemany("INSERT INTO history_pending(event_id,content_hash,retry_at) VALUES(?,'hash',0)",[(str(i),) for i in reversed(range(1000))])
  steps=[];self.db.set_progress_handler(lambda:steps.append(1) or 0,100)
  try:
   cols,cursor=projection.candidate_cursor(self.db,8,100)
   rows=list(cursor);cursor.close()
  finally:self.db.set_progress_handler(None,0)
  self.assertEqual([r[0] for r in rows],[str(i) for i in range(8)])
  self.assertLess(len(steps),30,'queue prefix sorted unrelated pending rows')
  with self.db:self.db.execute('UPDATE history_pending SET seq=NULL')
  history.initialize(self.db)
  self.assertEqual(self.db.execute('SELECT count(*) FROM history_pending q JOIN history_events e ON e.event_id=q.event_id WHERE q.seq=e.seq').fetchone()[0],1000)
 def test_cpu_budget_preserves_progress_across_io_wait(self):
  import time
  for i in range(4):self.put('pi',dict(type='session',id='s'),ident=str(i))
  def io(*args):time.sleep(.05);return [],''
  with patch.object(projection,'plan',side_effect=io):result=projection.run(self.db,limit=4,budget=.1,cpu_budget=True)
  self.assertEqual(result['processed'],4);self.assertEqual(result['pending'],0)

 def test_empty_projection_queue_does_not_walk_retained_corpus(self):
  with self.db:
   self.db.executemany("INSERT INTO history_events(event_id,machine,tool,session,kind,body,content_hash,bytes) VALUES(?,'host','pi','s','session',x'00','hash',1)",[(str(i),) for i in range(2000)])
   self.db.execute('DELETE FROM history_pending')
  ticks=[]
  def budget():
   ticks.append(1)
   return len(ticks)>10
  self.db.set_progress_handler(budget,100)
  try:self.assertEqual(self.project(),dict(processed=0,errors=0,pending=0))
  finally:self.db.set_progress_handler(None,0)
 def test_sparse_projection_queue_preserves_native_order_and_retry_deadlines(self):
  first=self.put('pi',dict(type='session',id='s',cwd='/first'),ident='first')
  second=self.put('pi',dict(type='session',id='s',cwd='/second'),ident='second')
  third=self.put('pi',dict(type='session',id='s',cwd='/future'),ident='third')
  with self.db:
   self.db.execute('DELETE FROM history_pending')
   for ident in (third,second,first):
    self.db.execute('INSERT INTO history_pending(event_id,content_hash,retry_at) SELECT event_id,content_hash,? FROM history_events WHERE event_id=?',(9999999999 if ident==third else 0,ident))
  result=self.project();self.assertEqual((result['processed'],result['pending']),(2,1))
  context=json.loads(self.db.execute("SELECT value FROM history_projection_context WHERE tool='pi' AND session='s'").fetchone()[0])
  self.assertEqual(context['cwd'],'/second')
 def test_pruned_source_usage_rebuild_reuses_native_id_and_preserves_cost_unknown(self):
  self.put('pi',self.pi());self.assertEqual(self.project()['errors'],0)
  self.assertEqual(self.db.execute('select uid,fresh_in,out from calls').fetchone(),('pi:s:a',5,3))
  with self.db:self.db.execute('delete from llm')
  projection.requeue(self.db,tool='pi',session='s');self.project()
  self.assertEqual(self.db.execute('select count(*),sum(fresh_in+cache_read+cache_write+out) from calls').fetchone(),(1,11))
  projection.requeue(self.db);self.project()
  self.assertEqual(self.db.execute('select count(*) from calls').fetchone()[0],1)
 def test_mutated_event_queues_latest_revision_and_does_not_add_a_call(self):
  self.put('pi',self.pi(1));self.project();self.put('pi',self.pi(9));self.put('pi',self.pi(12));self.project()
  self.assertEqual(self.db.execute('select count(*),sum(out) from calls').fetchone(),(1,12))
  self.assertEqual(projection.status(self.db)['pending'],0)
 def test_synthetic_history_with_positive_usage_is_still_not_metered(self):
  self.put('pi',self.pi(100,usageProvenance='synthetic_history'));self.project()
  self.assertEqual(self.db.execute('select count(*) from calls').fetchone()[0],0)
  self.put('pi',dict(type='message',id='u',message=dict(role='user',timestamp=0,content='restored')),ident='u');self.project()
  self.assertEqual(self.db.execute('select count(*) from input_messages').fetchone()[0],0)
 def test_claude_ids_and_flags_survive_capture_before_native_pruning(self):
  path=self.root/'session.jsonl'
  records=[dict(type='user',uuid='u',sessionId='s',cwd='/code/project',timestamp=100,message=dict(role='user',content='Human or automation')),
           dict(type='assistant',uuid='fragment',requestId='request',sessionId='s',cwd='/code/project',timestamp=101,message=dict(id='response',role='assistant',model='m',usage=dict(input_tokens=5,output_tokens=3,cache_read_input_tokens=2,cache_creation_input_tokens=1)))]
  path.write_text(''.join(json.dumps(r)+'\n' for r in records));history.scan_file(self.db,'claude',str(path),1);path.unlink();self.project()
  self.assertEqual(self.db.execute('select uid,response_id,request_id from calls').fetchone(),('c:response','response','request'))
  self.assertEqual(self.db.execute('select uid,origin from input_messages').fetchone(),('c:u','unknown'))
 def test_old_stripped_claude_identity_stays_unknown_without_a_duplicate(self):
  store.ingest(self.db,[json.dumps(dict(t='llm',uid='c:real',tool='claude',session='s',ts=100,model='m',fresh_in=5,out=3))],machine='host')
  eid=self.put('claude',dict(type='assistant',uuid='fragment',message=dict(role='assistant',model='m',usage=dict(input_tokens=5,output_tokens=3))))
  with self.db:self.db.execute('update history_events set capture_version=1 where event_id=?',(eid,))
  self.project();self.assertEqual(self.db.execute('select count(*) from calls').fetchone()[0],1)
  self.assertEqual(projection.status(self.db)['gaps'],{'native_message_id_not_retained':1})
 def test_codex_explicit_usage_matches_native_uid_and_cache_subsets(self):
  self.put('codex',dict(type='turn_context',payload=dict(turn_id='turn',model='m',cwd='/code/project')),ident='context')
  record=dict(type='token_usage_record',payload=dict(response_id='r',turn_id='turn',usage=dict(input_tokens=100,cached_input_tokens=20,cache_write_input_tokens=70,output_tokens=5)))
  self.put('codex',record);self.project()
  self.assertEqual(self.db.execute('select uid,model,fresh_in,cache_read,cache_write,out from calls').fetchone(),('x:r','m',10,20,70,5))
  projection.requeue(self.db);self.project();self.assertEqual(self.db.execute('select count(*) from calls').fetchone()[0],1)
 def test_cumulative_codex_counter_does_not_invent_request_identity(self):
  self.put('codex',dict(type='event_msg',payload=dict(type='token_count',info=dict(total_token_usage=dict(total_tokens=100),last_token_usage=dict(input_tokens=90,output_tokens=10)))));self.project()
  self.assertEqual(self.db.execute('select count(*) from calls').fetchone()[0],0)
  self.assertEqual(projection.latest_counter(self.db,'codex','s')['total_tokens'],100)
  self.assertIsNone(projection.latest_counter(self.db,'codex','s')['request_count'])
  self.assertEqual(projection.status(self.db)['gaps'],{})
 def test_projection_failure_rolls_back_partial_observations_and_does_not_starve_other_records(self):
  self.put('pi',self.pi());original=store.ingest
  def fail(db,rows,**kwargs):
   original(db,list(rows)[:1],**kwargs);raise ValueError('injected')
  with patch.object(store,'ingest',side_effect=fail):self.assertEqual(self.project()['errors'],1)
  self.assertEqual(self.db.execute('select count(*) from calls').fetchone()[0],0)
  self.assertEqual(self.db.execute('select count(*) from observations').fetchone()[0],0)
  self.put('pi',dict(type='message',id='b',message=dict(role='assistant',model='m',usage=dict(input=2,output=1))),ident='b')
  self.project();self.assertEqual(self.db.execute('select uid from calls').fetchall(),[('pi:s:b',)])
  self.assertEqual(projection.status(self.db)['pending'],1)
 def test_expiration_pins_unprojected_records_then_releases_them(self):
  self.put('pi',self.pi())
  result=history.expire(self.db,200,True);self.assertEqual((result['protected_events'],result['expired_events']),(1,0))
  self.assertIsNotNone(history.events(self.db,'pi','s')['events'][0]['body'])
  self.project();self.assertEqual(history.expire(self.db,200,True)['expired_events'],1)
  self.assertIsNone(history.events(self.db,'pi','s')['events'][0]['body'])
  self.assertEqual(self.db.execute('select count(*) from calls').fetchone()[0],1)
 def test_opencode_parts_aggregate_one_unknown_origin_input_after_out_of_order_parent(self):
  part=dict(type='sqlite_record',table='part',id='p',parent_message_id='u',data=dict(type='text',text='hello world'))
  self.put('opencode',part,ident='part:p');self.project()
  self.put('opencode',dict(type='sqlite_record',table='message',id='u',data=dict(role='user')),ident='message:u',role='user');self.project()
  self.assertEqual(self.db.execute('select count(*),sum(chars) from input_messages').fetchone(),(1,11))
  part['data']['text']='hello world again';self.put('opencode',part,ident='part:p');self.project()
  self.assertEqual(self.db.execute('select count(*),sum(chars) from input_messages').fetchone(),(1,17))
  self.assertEqual(self.db.execute('select origin from input_messages').fetchone()[0],'unknown')
 def test_local_receipt_rebuild_keeps_exact_native_server_reconciliation(self):
  with self.db:
   local_proxy.record_request(self.db,'inv',dict(model='m'),'ds4')
   local_proxy.finish(self.db,'inv',json.dumps(dict(id='r',model='m',usage=dict(prompt_tokens=8,completion_tokens=3))).encode(),200,10,2)
  with self.db:self.db.execute("delete from llm where tool='local'")
  self.put('pi',self.pi());self.project();history.link_inferences(self.db)
  self.assertEqual(self.db.execute('select count(*) from llm').fetchone()[0],2)
  self.assertEqual(self.db.execute('select count(*) from calls').fetchone()[0],1)
  self.assertEqual(self.db.execute('select count(*) from history_links').fetchone()[0],1)

 def test_claude_usage_fragments_merge_after_pruning_and_keep_one_call(self):
  first=dict(type='assistant',uuid='first',message=dict(id='r',role='assistant',model='m',usage=dict(input_tokens=7,cache_read_input_tokens=2)))
  self.put('claude',first,ident='first');self.project()
  self.assertEqual(self.db.execute('select count(*) from calls').fetchone()[0],0)
  last=dict(type='assistant',uuid='last',message=dict(id='r',role='assistant',model='m',usage=dict(output_tokens=5)))
  self.put('claude',last,ident='last');self.project()
  self.assertEqual(self.db.execute('select count(*),sum(fresh_in),sum(cache_read),sum(out) from calls').fetchone(),(1,7,2,5))
  self.assertNotIn('usage_fields_incomplete',projection.status(self.db)['gaps'])
  projection.requeue(self.db);self.project()
  self.assertEqual(self.db.execute('select count(*),sum(fresh_in),sum(out) from calls').fetchone(),(1,7,5))
 def test_native_claude_fragment_merge_matches_corpus_and_does_not_zero_omitted_input(self):
  path=self.root/'.claude/projects/p/s.jsonl';path.parent.mkdir(parents=True)
  rows=[dict(type='assistant',uuid='a',timestamp=100,sessionId='s',cwd='/code/p',message=dict(id='r',role='assistant',model='m',usage=dict(input_tokens=7))),dict(type='assistant',uuid='b',timestamp=101,sessionId='s',cwd='/code/p',message=dict(id='r',role='assistant',model='m',usage=dict(output_tokens=5)))]
  path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
  import contextlib,io
  out=io.StringIO()
  with contextlib.redirect_stdout(out):collect.collect_claude(0)
  calls=[json.loads(r) for r in out.getvalue().splitlines() if json.loads(r)['t']=='llm']
  self.assertEqual([(r['uid'],r['fresh_in'],r['out']) for r in calls],[('c:r',7,5)])
 def test_copilot_missing_input_usage_does_not_become_zero(self):
  self.put('copilot',dict(type='assistant.usage',id='call',data=dict(outputTokens=5)));self.project()
  self.assertEqual(self.db.execute('select count(*) from calls').fetchone()[0],0)
  self.assertIn('usage_fields_incomplete',projection.status(self.db)['gaps'])

 def test_usage_replay_does_not_replace_verified_work_context(self):
  from erg import work
  url='https://github.com/acme/r/pull/1'
  rows=[dict(t='pull_request',uid=url,ts=100,repo='github.com/acme/r',number=1,url=url,state='open',created_ts=1,branch='feature',head_sha='abc'),dict(t='pr_commit',uid=url+':abc',ts=100,pr_url=url,sha='abc'),work.context_event('codex','s',100,'/code/r',repo='github.com/acme/r',branch='feature',head_sha='abc',scope_id='verified'),dict(t='llm',uid='x:r',ts=110,tool='codex',session='s',model='m',fresh_in=10,out=5)]
  store.ingest(self.db,map(json.dumps,rows),machine='host')
  self.assertEqual(work.effectiveness(self.db)['attributed_calls'],1)
  self.put('codex',dict(type='session_meta',payload=dict(id='s',cwd='/code/r',git=dict(repository_url='https://github.com/acme/r',branch='other'))),ident='header',ts=101)
  self.put('codex',dict(type='token_usage_record',payload=dict(response_id='r',model='m',usage=dict(input_tokens=10,output_tokens=5))),ts=110)
  self.project()
  self.assertEqual(self.db.execute('select count(*) from work_contexts').fetchone()[0],1)
  self.assertEqual(work.effectiveness(self.db)['attributed_calls'],1)

 def test_route_and_quota_metadata_survive_without_transport_credentials(self):
  body=history.safe_event('codex',dict(type='session_meta',timestamp=100,payload=dict(id='s',model_provider='custom',route=dict(provider='custom',base_url='https://gateway.example/v1?secret=NO',headers={'Authorization':'NO'}),creator_account_id='owner')))
  self.assertEqual(body['payload']['route']['base_url'],'https://gateway.example/v1')
  self.assertNotIn('NO',json.dumps(body))
  self.put('codex',body,ident='header');self.project()
  self.assertEqual(self.db.execute('select endpoint_host from session_route_events').fetchone()[0],'gateway.example')
  self.assertEqual(self.db.execute('select count(*) from identity_events').fetchone()[0],1)

 def test_metadata_only_projection_preserves_session_and_machine_facts(self):
  rows=[dict(t='hello',machine='host',os='Darwin',ncpu=8),dict(t='session',uid='pi:s',tool='pi',session='s',ts=50,last_ts=100,project='project',kind='subagent',surface='native-ui',parent_session='parent',forked_from='fork',agent_version='1.2.3',provider='provider',orchestrator='wrapper',conversation_id='conversation',usage_status='unknown')]
  store.ingest(self.db,map(json.dumps,rows),machine='host')
  self.put('pi',self.pi());self.project()
  self.assertEqual(self.db.execute('select kind,surface,parent_session,forked_from,agent_version,provider,orchestrator,conversation_id from sessions').fetchone(),('subagent','native-ui','parent','fork','1.2.3','provider','wrapper','conversation'))
  self.assertEqual(self.db.execute('select os,ncpu from machines').fetchone(),('Darwin',8))

 def test_database_boundary_queues_legacy_writer_and_blocks_expired_resurrection(self):
  import hashlib,zlib
  body=json.dumps(self.pi()).encode();digest=hashlib.sha256(body).hexdigest()
  with self.db:
   self.db.execute('INSERT INTO history_events(event_id,machine,tool,session,project,ts,kind,role,origin,source,body,content_hash,bytes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',('legacy','host','pi','s','project',100,'message','assistant','native','/gone',zlib.compress(body),digest,len(body)))
  self.assertEqual(projection.status(self.db)['pending'],1)
  self.project();history.expire(self.db,200,True)
  with self.db:
   self.db.execute('UPDATE history_events SET body=?,content_hash=? WHERE event_id=?',(zlib.compress(body),digest,'legacy'))
  self.assertIsNone(self.db.execute('select body from history_events').fetchone()[0])
  self.assertEqual(projection.status(self.db)['pending'],0)

 def test_exact_metadata_repair_does_not_import_history_or_duplicate_calls(self):
  from erg import history_repair
  native=dict(type='assistant',uuid='fragment',sessionId='s',timestamp=100,message=dict(id='r',role='assistant',model='m',content='RETAINED',usage=dict(input_tokens=5,output_tokens=3)))
  path=self.root/'.claude/projects/p/s.jsonl';path.parent.mkdir(parents=True);path.write_text(json.dumps(native)+'\n')
  old=history.safe_event('claude',native);old['message'].pop('id');old.pop('isMeta')
  with self.db:
   eid,_=history.put(self.db,'claude','s','project',100,'assistant',old,'fragment',role='assistant',source=str(path))
   self.db.execute('update history_events set capture_version=1 where event_id=?',(eid,))
  self.project();self.assertIn('native_message_id_not_retained',projection.status(self.db)['gaps'])
  preview=history_repair.repair(self.db);self.assertEqual((preview['matched'],preview['repaired']),(1,0))
  self.assertEqual(history_repair.repair(self.db,True)['repaired'],1)
  self.project();self.assertEqual(self.db.execute('select uid,fresh_in,out from calls').fetchone(),('c:r',5,3))
  self.assertEqual(len(history.events(self.db,'claude','s')['events']),1)
  self.assertEqual(history.events(self.db,'claude','s')['events'][0]['body']['message']['content'],'RETAINED')
 def test_metadata_repair_refuses_changed_content(self):
  from erg import history_repair
  native=dict(type='assistant',uuid='fragment',sessionId='s',timestamp=100,message=dict(id='r',role='assistant',model='m',content='LATER BODY',usage=dict(input_tokens=5,output_tokens=3)))
  path=self.root/'.claude/projects/p/s.jsonl';path.parent.mkdir(parents=True);path.write_text(json.dumps(native)+'\n')
  old=history.safe_event('claude',native);old['message'].pop('id');old['message']['content']='RETAINED'
  with self.db:
   eid,_=history.put(self.db,'claude','s','project',100,'assistant',old,'fragment',source=str(path))
   self.db.execute('update history_events set capture_version=1 where event_id=?',(eid,))
  result=history_repair.repair(self.db,True);self.assertEqual((result['repaired'],result['conflicts']),(0,1))
  self.assertEqual(history.events(self.db,'claude','s')['events'][0]['body']['message']['content'],'RETAINED')
 def test_metadata_repair_ignores_incomplete_trailing_source_records(self):
  from erg import history_repair
  path=self.root/'lines.jsonl';path.write_bytes(b'{"uuid":"committed"}\n{"uuid":"not-committed"}')
  self.assertEqual([json.loads(r)['uuid'] for r in history_repair.lines_backwards(path)],['committed'])

 def test_native_header_metadata_survives_without_a_second_usage_scrape(self):
  self.put('codex',dict(type='session_meta',payload=dict(id='s',cwd='/code',cli_version='1.2.3',forked_from_id='parent',originator='native-app',model_provider='custom-provider')),ident='header')
  self.project()
  self.assertEqual(self.db.execute('select agent_version,forked_from,surface,provider from sessions').fetchone(),('1.2.3','parent','native-app','custom-provider'))

 def test_native_edit_parity_survives_pruning_and_retained_rebuild(self):
  claude=[dict(type='assistant',sessionId='s',timestamp=100,cwd='/code/project',message=dict(id='response',role='assistant',model='m',usage=dict(input_tokens=5,output_tokens=3),content=[dict(type='tool_use',id='write',name='Write',input=dict(file_path='/code/project/a.py',content='one\ntwo\n'))]))]
  patch_text='*** Begin Patch\n*** Add File: /code/project/b.py\n+line\n*** Update File: README.md\n@@\n-old\n+new\n*** End Patch\n'
  codex=[dict(type='session_meta',timestamp=100,payload=dict(id='s',cwd='/code/project')),
         dict(type='turn_context',timestamp=100,payload=dict(model='m')),
         dict(type='response_item',timestamp=101,payload=dict(type='custom_tool_call',call_id='patch',input=patch_text))]
  expected=[]
  for tool,records,fn,root_function in [('claude',claude,collect.collect_claude,'claude_roots'),('codex',codex,collect.collect_codex,'native_roots')]:
   path=self.root/(tool+'.jsonl');path.write_text(''.join(json.dumps(r)+'\n' for r in records));output=io.StringIO()
   with patch.object(collect,root_function,return_value=[str(path)]),contextlib.redirect_stdout(output):fn(0)
   expected.extend(json.loads(line) for line in output.getvalue().splitlines() if json.loads(line).get('t')=='edit')
   history.scan_file(self.db,tool,str(path),1);path.unlink()
  self.assertEqual(self.project()['errors'],0)
  columns=store.COLS['edit']
  actual=[dict(zip(columns,row)) for row in self.db.execute('SELECT '+','.join(columns)+' FROM edits ORDER BY uid')]
  wanted=[{k:r[k] for k in columns} for r in sorted(expected,key=lambda r:r['uid'])]
  self.assertEqual(actual,wanted);self.assertEqual(len(actual),3)
  with self.db:self.db.execute('DELETE FROM edits')
  projection.requeue(self.db);self.project()
  self.assertEqual([dict(zip(columns,row)) for row in self.db.execute('SELECT '+','.join(columns)+' FROM edits ORDER BY uid')],wanted)
 def test_unrelated_large_codex_tool_payload_is_archived_without_projection_decode(self):
  body=dict(type='response_item',payload=dict(type='function_call',call_id='read',name='exec_command',arguments='opaque '*100000))
  self.put('codex',body,kind='response_item')
  with patch.object(projection.zlib,'decompress',side_effect=AssertionError('decoded unused tool body')):self.assertEqual(self.project()['errors'],0)
  self.assertEqual(self.db.execute('SELECT count(*) FROM edits').fetchone()[0],0)
  self.assertEqual(history.events(self.db,'codex','s')['events'][0]['body'],body)
 def test_older_retained_patch_without_hint_replays_and_indexes_its_hint(self):
  body=dict(type='response_item',payload=dict(type='function_call',call_id='patch',arguments='*** Begin Patch\n*** Add File: file.py\n+line\n*** End Patch\n'))
  eid=self.put('codex',body,kind='response_item')
  with self.db:self.db.execute('UPDATE history_events SET projection_hint=NULL WHERE event_id=?',(eid,))
  self.assertEqual(self.project()['errors'],0)
  self.assertEqual(self.db.execute('SELECT uid,added FROM edits').fetchone(),('x:patch:0',1))
  self.assertEqual(self.db.execute('SELECT projection_hint FROM history_events WHERE event_id=?',(eid,)).fetchone()[0],'codex_patch')

 def test_less_enriched_patch_replay_preserves_model_in_projection_and_journal(self):
  self.put('codex',dict(type='turn_context',payload=dict(model='m')),ident='context')
  patch=dict(type='response_item',payload=dict(type='custom_tool_call',call_id='patch',input='*** Begin Patch\n*** Add File: file.py\n+line\n*** End Patch\n'))
  event=self.put('codex',patch,kind='response_item');self.project()
  with self.db:
   self.db.execute('DELETE FROM history_projection_context')
   self.db.execute('INSERT INTO history_pending(event_id,content_hash) SELECT event_id,content_hash FROM history_events WHERE event_id=?',(event,))
  self.project()
  self.assertEqual(self.db.execute('SELECT model FROM edits').fetchone()[0],'m')
  self.assertEqual(json.loads(self.db.execute("SELECT payload FROM observations WHERE kind='edit'").fetchone()[0])['model'],'m')

 def test_unchanged_mirror_does_not_read_or_decode_archived_body(self):
  body=self.pi(content='retained content');self.put('pi',body)
  queries=[];self.db.set_trace_callback(queries.append)
  try:
   with patch.object(history.zlib,'decompress',side_effect=AssertionError('decoded unchanged body')):
    with self.db:self.assertFalse(history.put(self.db,'pi','s','project',100,'message',body,'event')[1])
  finally:self.db.set_trace_callback(None)
  self.assertFalse(any(q.startswith('SELECT body') for q in queries))
  snapshot=self.pi();snapshot['message']['usage']['output']=4
  self.put('pi',snapshot)
  value=history.events(self.db,'pi','s')['events'][0]['body']
  self.assertEqual(value['message']['content'],'retained content')
  self.assertEqual(value['message']['usage']['output'],4)

 def test_metadata_mirror_hash_avoids_blob_reads_and_invalidates_on_body_change(self):
  full=self.pi(content='retained content');event=self.put('pi',full)
  snapshot=self.pi();self.put('pi',snapshot)
  queries=[];self.db.set_trace_callback(queries.append)
  with patch.object(history.zlib,'decompress',side_effect=AssertionError('unchanged mirror decoded body')):
   self.put('pi',snapshot)
  self.assertFalse(any(q.startswith('SELECT body FROM history_events') for q in queries))
  self.db.set_trace_callback(None)
  changed=self.pi(content='changed native body');self.put('pi',changed)
  decode=history.zlib.decompress
  with patch.object(history.zlib,'decompress',wraps=decode) as unpack:
   self.put('pi',snapshot);self.assertEqual(unpack.call_count,1)
  body=history.events(self.db,'pi','s')['events'][0]['body']
  self.assertEqual(body['message']['content'],'changed native body')
  enriched=self.pi();enriched['message']['usage']['output']=17;self.put('pi',enriched)
  body=history.events(self.db,'pi','s')['events'][0]['body']
  self.assertEqual(body['message']['usage']['output'],17)
  self.assertEqual(body['message']['content'],'changed native body')

 def test_metadata_mirror_cache_rolls_back_with_the_body_and_projection_queue(self):
  self.put('pi',self.pi(content='retained content'));self.project()
  snapshot=self.pi();snapshot['message']['usage']['output']=19
  with self.assertRaises(ValueError):
   with self.db:
    history.put(self.db,'pi','s','project',100,'message',snapshot,'event')
    raise ValueError('injected')
  self.assertEqual(self.db.execute('SELECT count(*) FROM history_metadata_mirrors').fetchone()[0],0)
  self.assertEqual(self.db.execute('SELECT count(*) FROM history_pending').fetchone()[0],0)
  self.assertNotEqual(history.events(self.db,'pi','s')['events'][0]['body']['message']['usage']['output'],19)

 def test_metadata_mirror_preserves_native_envelope_and_itemized_usage(self):
  body=self.pi(content=[dict(type='text',text='retained')])
  body['parentId']='native-parent';body['message']['stopReason']='toolUse';body['message']['toolCallId']='tool-id'
  body['message']['usage']['cost']=dict(input=.1,output=.2,total=.3)
  body['message']['usage']['nativeDetail']=42
  self.put('pi',body)
  mirror=self.pi();mirror['message']['usage']['output']=8;mirror['message']['usage']['cost']=dict(total=.4)
  self.put('pi',mirror)
  result=history.events(self.db,'pi','s')['events'][0]['body']
  self.assertEqual(result['parentId'],'native-parent')
  self.assertEqual(result['message']['stopReason'],'toolUse');self.assertEqual(result['message']['toolCallId'],'tool-id')
  self.assertEqual(result['message']['usage']['cost'],dict(input=.1,output=.2,total=.4))
  self.assertEqual(result['message']['usage']['nativeDetail'],42)
  self.assertEqual(result['message']['usage']['output'],8)
  self.assertEqual(result['message']['content'],body['message']['content'])
  with patch.object(history.zlib,'decompress',side_effect=AssertionError('unchanged enriched mirror decoded')):self.put('pi',mirror)

 def test_metadata_mirror_cannot_erase_native_synthetic_history_marker(self):
  body=self.pi(content='injected history');body['message']['timestamp']=0
  self.put('pi',body);self.put('pi',self.pi());self.project()
  result=history.events(self.db,'pi','s')['events'][0]['body']
  self.assertEqual(result['message']['timestamp'],0)
  self.assertEqual(self.db.execute("SELECT count(*) FROM calls WHERE tool='pi' AND session='s'").fetchone()[0],0)
