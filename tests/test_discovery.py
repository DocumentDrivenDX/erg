"""Discovery and mirror identity regressions; fixtures contain no real histories."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch
from erg import collect, store, unified

STAMP = '2026-10-17T00:00:00Z'


class Discovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {}, clear=True); self.env.start(); self.addCleanup(self.env.stop)
        self.hp = patch.object(collect, 'HOME', str(self.home)); self.hp.start(); self.addCleanup(self.hp.stop)

    def write(self, rel, events):
        p = self.home / rel; p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(''.join(json.dumps(e)+'\n' for e in events)); return p

    def capture(self, fn, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out): fn(0, *args)
        return [json.loads(x) for x in out.getvalue().splitlines()]

    def pi(self):
        return [dict(type='session', id='native-pi', timestamp=STAMP, cwd='/code/example', version=3),
            dict(type='model_change', id='model', timestamp=STAMP, provider='custom', modelId='model-a'),
            dict(type='message', id='u', timestamp=STAMP, message=dict(role='user',content='PRIVATE PROMPT')),
            dict(type='message', id='a', timestamp=STAMP, message=dict(role='assistant',content='PRIVATE RESPONSE',
                provider='custom',model='model-a',usage=dict(input=10,cacheRead=20,cacheWrite=3,output=7,reasoning=2))) ]

    def omni(self, external='native-pi'):
        p=self.home/'.omnigent/chat.db'; p.parent.mkdir(parents=True, exist_ok=True)
        db=sqlite3.connect(str(p))
        db.executescript('''CREATE TABLE conversations(id BLOB, created_at TEXT,updated_at TEXT,parent_conversation_id BLOB,workspace_id INT);
        CREATE TABLE omnigent_conversation_metadata(id BLOB,workspace_id INT,external_session_id TEXT,session_usage BLOB);
        CREATE TABLE conversation_labels(conversation_id BLOB,workspace_id INT,key TEXT,value TEXT);''')
        cid=bytes.fromhex('123456781234123412341234567890ab')
        db.execute('INSERT INTO conversations VALUES(?,?,?,?,?)',(cid,STAMP,STAMP,None,1))
        db.execute('INSERT INTO omnigent_conversation_metadata VALUES(?,?,?,?)',(cid,1,external,None))
        db.execute('INSERT INTO conversation_labels VALUES(?,?,?,?)',(cid,1,'omnigent.wrapper','pi-native-ui'))
        db.commit(); db.close(); return p

    def test_pi_custom_roots_and_provider_usage_without_content(self):
        p=self.write('custom/session.jsonl',self.pi())
        self.write('.config/erg/discovery.json',[])  # replace NDJSON with a config object
        (self.home/'.config/erg/discovery.json').write_text(json.dumps({'pi_session_dirs':[str(p.parent)]}))
        rows=self.capture(collect.collect_pi)
        call=next(x for x in rows if x['t']=='llm')
        self.assertEqual([call[k] for k in ['fresh_in','cache_read','cache_write','out','reasoning']],[10,20,3,7,2])
        self.assertEqual(call['provider'],'custom'); self.assertNotIn('PRIVATE',json.dumps(rows))

    def test_pi_missing_usage_and_malformed_lines_visible(self):
        events=self.pi(); events[-1]['message'].pop('usage')
        p=self.write('.pi/agent/sessions/p/s.jsonl',events)
        with p.open('a') as f:f.write('{truncated\n')
        rows=self.capture(collect.collect_pi)
        self.assertFalse(any(x['t']=='llm' for x in rows))
        self.assertEqual(next(x for x in rows if x['t']=='session')['usage_status'],'unknown')
        self.assertEqual(next(x for x in rows if x['t']=='source')['status'],'partial')

    def test_omnigent_pi_mirror_keeps_one_session_and_one_call(self):
        p=self.write('.pi/agent/sessions/p/s.jsonl',self.pi())
        self.write('.omnigent/pi-native/bridge/sessions/copy.jsonl',self.pi())
        dbpath=self.omni()
        rows=self.capture(collect.collect_pi)
        native={(x['tool'],x['session']):x for x in rows if x['t']=='session'}
        rows+=self.capture(collect.collect_omnigent,native)
        with store.connect(str(self.home/'erg.db')) as db:
            store.ingest(db,map(json.dumps,rows),machine='host')
            self.assertEqual(db.execute('SELECT COUNT(*),SUM(out) FROM calls').fetchone(),(1,7))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM report_sessions').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT orchestrator FROM sessions').fetchone()[0],'omnigent')
            # A subsequent unenriched native scan must not erase the association.
            store.ingest(db,map(json.dumps,self.capture(collect.collect_pi)),machine='host')
            self.assertEqual(db.execute('SELECT orchestrator FROM sessions').fetchone()[0],'omnigent')
        self.assertEqual(sqlite3.connect('file:'+str(dbpath)+'?mode=ro',uri=True).execute('SELECT COUNT(*) FROM conversations').fetchone()[0],1)

    def test_missing_native_history_is_visible_and_later_reconciled(self):
        self.omni(external=None)
        missing=self.capture(collect.collect_omnigent,{})
        meta=next(x for x in missing if x['t']=='session')
        self.assertEqual((meta['tool'],meta['usage_status']),('pi','unknown'))
        source=next(x for x in missing if x['t']=='source'); self.assertEqual(source['missing'],1)
        with store.connect(str(self.home/'erg.db')) as db:
            store.ingest(db,map(json.dumps,missing),machine='host')
            native=collect.session_record('pi','native',1792195200,orchestrator='omnigent',conversation_id=meta['conversation_id'],usage_status='observed')
            store.ingest(db,[json.dumps(native)],machine='host')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM report_sessions').fetchone()[0],1)
            self.assertEqual(store.dataset(db)['session_count'],1)

    def test_isolated_codex_and_claude_observer_paths(self):
        events=[dict(type='session_meta',timestamp=STAMP,payload=dict(id='codex-native',cwd='/code/p',source='cli')),
            dict(type='token_usage_record',timestamp=STAMP,payload=dict(response_id='r',usage=dict(input_tokens=10,output_tokens=3)))]
        self.write('.codex/sessions/original.jsonl',events)
        self.write('.omnigent/codex-native/bridge/codex-home/sessions/mirror.jsonl',events)
        extra=self.home/'elsewhere'
        self.write('elsewhere/sessions/new.jsonl',[dict(type='session_meta',timestamp=STAMP,payload=dict(id='other',source='cli'))])
        self.write('.omnigent/codex-native/other/state.json',[dict(codex_home=str(extra))])
        codex=self.capture(collect.collect_codex)
        transcript=self.write('isolated/claude.jsonl',[dict(type='assistant',timestamp=STAMP,sessionId='claude-native',message=dict(id='message',model='claude',usage=dict(input_tokens=2,output_tokens=4)))])
        self.write('.omnigent/claude-native/bridge/state.json',[dict(transcript_path=str(transcript))])
        claude=self.capture(collect.collect_claude)
        with store.connect(str(self.home/'erg.db')) as db:
            store.ingest(db,map(json.dumps,codex+claude),machine='host')
            self.assertEqual(db.execute('SELECT COUNT(*),SUM(out) FROM calls').fetchone(),(2,7))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM report_sessions').fetchone()[0],3)

    def test_existing_ledger_migrates_additive_discovery_fields(self):
        p=self.home/'old.db';db=sqlite3.connect(str(p))
        db.execute('CREATE TABLE sessions(machine TEXT,uid TEXT,PRIMARY KEY(machine,uid))');db.commit();db.close()
        with store.connect(str(p)) as db:
            self.assertIn('conversation_id',{r[1] for r in db.execute('PRAGMA table_info(sessions)')})
            self.assertEqual(store.dataset(db)['session_count'],0)

    def test_metadata_summary_is_retained_without_fabricating_calls(self):
        path=self.omni()
        db=sqlite3.connect(str(path))
        usage=b'\x00\x00'+json.dumps(dict(input_tokens=30,output_tokens=7,cache_read_input_tokens=20)).encode()
        db.execute('UPDATE omnigent_conversation_metadata SET session_usage=?',(usage,));db.commit();db.close()
        rows=self.capture(collect.collect_omnigent,{})
        session=next(x for x in rows if x['t']=='session')
        self.assertEqual((session['tool'],session['summary_out'],session['usage_status']),('pi',7,'summary_only'))
        self.assertFalse(any(x['t']=='llm' for x in rows))
        with store.connect(str(self.home/'erg.db')) as db:
            store.ingest(db,map(json.dumps,rows),machine='host')
            self.assertEqual(store.dataset(db)['capture_counts'],[['host','pi',1,0,1]])

    def test_complete_collector_links_native_sessions_before_metadata(self):
        self.write('.omnigent/pi-native/bridge/sessions/s.jsonl',self.pi())
        self.omni()
        env=dict(os.environ,HOME=str(self.home),ERG_STATE_DIR=str(self.home/'state'),PYTHONDONTWRITEBYTECODE='1')
        result=subprocess.run([sys.executable,collect.__file__,'collect','--days','40000','--no-git','--no-metrics'],
            env=env,capture_output=True,text=True,check=True)
        rows=[json.loads(line) for line in result.stdout.splitlines()]
        source=next(x for x in rows if x.get('t')=='source' and x['tool']=='omnigent')
        self.assertEqual((source['linked'],source['missing']),(1,0))
        self.assertNotIn('PRIVATE',result.stdout)
        with store.connect(str(self.home/'erg.db')) as db:
            store.ingest(db,result.stdout.splitlines())
            self.assertEqual(db.execute('SELECT COUNT(*),SUM(out) FROM calls').fetchone(),(1,7))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM report_sessions').fetchone()[0],1)

    def test_codex_cache_write_is_an_input_subset_and_mirrors_replay_once(self):
        events=[dict(type='session_meta',timestamp=STAMP,payload=dict(id='s',cwd='/code',source='cli')),
            dict(type='token_usage_record',timestamp=STAMP,payload=dict(response_id='r',usage=dict(input_tokens=100,cached_input_tokens=20,cache_write_input_tokens=70,output_tokens=5,total_tokens=105)))]
        self.write('.codex/sessions/a.jsonl',events)
        self.write('.omnigent/codex-native/w/codex-home/sessions/b.jsonl',events)
        rows=self.capture(collect.collect_codex)
        with store.connect(str(self.home/'db')) as db:
            for _ in range(2):store.ingest(db,map(json.dumps,rows),machine='host')
            self.assertEqual(db.execute('SELECT COUNT(*),SUM(fresh_in),SUM(cache_read),SUM(cache_write),SUM(fresh_in+cache_read+cache_write+out) FROM calls').fetchone(),(1,10,20,70,105))

    def test_empty_opencode_v2_schema_is_no_history_not_failure(self):
        root=self.home/'.local/share/opencode';root.mkdir(parents=True)
        db=sqlite3.connect(str(root/'opencode.db'))
        db.executescript('CREATE TABLE session_v2(id TEXT,parent_id TEXT,directory TEXT,time_created INT,time_updated INT); CREATE TABLE session_message(id TEXT,session_id TEXT,data TEXT);')
        db.close()
        rows=self.capture(collect.collect_opencode)
        source=next(r for r in rows if r['t']=='source')
        self.assertEqual((source['status'],source['errors']),('no_history',0))
    def test_unrecognized_opencode_message_shape_is_visible(self):
        root=self.home/'.local/share/opencode';root.mkdir(parents=True)
        db=sqlite3.connect(str(root/'opencode.db'))
        db.executescript('CREATE TABLE session_v2(id TEXT,parent_id TEXT,directory TEXT,time_created INT,time_updated INT); CREATE TABLE session_message(id TEXT,session_id TEXT,data TEXT);')
        db.execute('INSERT INTO session_message VALUES(?,?,?)',('m','s',json.dumps({'unknownEnvelope':{}})));db.commit();db.close()
        rows=self.capture(collect.collect_opencode)
        source=next(r for r in rows if r['t']=='source')
        self.assertEqual((source['status'],source['errors']),('partial',1))

    def test_opencode_native_message_id_is_not_a_server_response_id(self):
        root=self.home/'.local/share/opencode';root.mkdir(parents=True)
        db=sqlite3.connect(str(root/'opencode.db'))
        db.executescript('CREATE TABLE session(id TEXT,parent_id TEXT,directory TEXT,time_created INT,time_updated INT); CREATE TABLE message(id TEXT,session_id TEXT,data TEXT);')
        db.execute('INSERT INTO session VALUES(?,?,?,?,?)',('s',None,'/code/p',1792195200000,1792195200000))
        db.execute('INSERT INTO message VALUES(?,?,?)',('native-message','s',json.dumps({'role':'assistant','modelID':'m','tokens':{'input':10,'output':2},'time':{'completed':1792195200000}})));db.commit();db.close()
        call=next(r for r in self.capture(collect.collect_opencode) if r['t']=='llm')
        self.assertEqual(call['uid'],'opencode:native-message')
        self.assertEqual(call['response_id'],'')

    def test_omnigent_projection_cache_skips_decode_but_keeps_new_native_links(self):
        path=self.omni();cache={}
        usage=b'\x00\x00'+json.dumps(dict(input_tokens=30,output_tokens=7)).encode()
        snapshot=dict(runtime_config=dict(providers=dict(custom=dict(base_url='https://gateway.example/v1?token=PRIVATE',api_key='PRIVATE')),inference=dict(harnesses=dict(pi=dict(provider='custom')))))
        with sqlite3.connect(str(path)) as db:
            db.execute('ALTER TABLE omnigent_conversation_metadata ADD COLUMN inference_snapshot BLOB')
            db.execute('UPDATE omnigent_conversation_metadata SET session_usage=?,inference_snapshot=?',(usage,b'\x00\x00'+json.dumps(snapshot).encode()))
        with patch.object(collect,'omni_json',wraps=collect.omni_json) as decode,patch.object(collect.time,'time',return_value=1792195201):
            first=self.capture(collect.collect_omnigent,{},cache);self.assertEqual(decode.call_count,2)
            second=self.capture(collect.collect_omnigent,{},cache);self.assertEqual(decode.call_count,2)
            self.assertEqual(first,second);self.assertEqual(cache['cache_hits'],1)
            native={('pi','native-pi'):collect.session_record('pi','native-pi',1792195200,usage_status='observed')}
            linked=self.capture(collect.collect_omnigent,native,cache);self.assertEqual(decode.call_count,2)
            self.assertEqual(next(x for x in linked if x['t']=='session')['usage_status'],'observed')
            self.assertEqual(next(x for x in linked if x['t']=='source')['linked'],1)
            with sqlite3.connect(str(path)) as db:db.execute('UPDATE omnigent_conversation_metadata SET session_usage=?',(b'\x00\x00'+json.dumps(dict(output_tokens=9)).encode(),))
            updated=self.capture(collect.collect_omnigent,native,cache);self.assertEqual(decode.call_count,4)
            self.assertEqual(next(x for x in updated if x['t']=='session')['summary_out'],9)
        self.assertNotIn('PRIVATE',str(cache))
        self.assertNotIn('runtime_config',str(cache))
    def test_omnigent_projection_cache_never_hides_bad_metadata_and_prunes_deleted_rows(self):
        path=self.omni();cache={}
        with sqlite3.connect(str(path)) as db:db.execute('UPDATE omnigent_conversation_metadata SET session_usage=?',(b'bad json',))
        for _ in range(2):
            rows=self.capture(collect.collect_omnigent,{},cache)
            self.assertEqual(next(x for x in rows if x['t']=='source')['errors'],1)
            self.assertEqual(cache['cached_records'],0)
        with sqlite3.connect(str(path)) as db:db.execute('UPDATE omnigent_conversation_metadata SET session_usage=NULL')
        self.capture(collect.collect_omnigent,{},cache);self.assertEqual(cache['cached_records'],1)
        with sqlite3.connect(str(path)) as db:db.execute('DELETE FROM conversations')
        self.capture(collect.collect_omnigent,{},cache);self.assertEqual(cache['cached_records'],0)
