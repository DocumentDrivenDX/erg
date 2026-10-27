"""Work evidence must be recoverable after native pruning without guessing billing."""
import contextlib
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from erg import collect,history,history_projection,store,work,work_retained


@unittest.skipUnless(shutil.which('git'),'Git objects required for evidence verification')
class RetainedWork(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name).resolve()
        for name,value in [('HOME',str(self.root)),('STATE_DIR',str(self.root/'state')),('machine_name',lambda:'host')]:
            p=patch.object(collect,name,value);p.start();self.addCleanup(p.stop)
        self.path=self.root/'ledger';self.db=store.connect(self.path);self.addCleanup(self.db.close)
        self.repo=self.root/'repo';self.git('init','--quiet',self.repo)
        self.git('-C',self.repo,'config','user.name','Fixture');self.git('-C',self.repo,'config','user.email','fixture@example.invalid')
        self.git('-C',self.repo,'remote','add','origin','https://github.com/acme/r')
        self.git('-C',self.repo,'checkout','--quiet','-b','feature')
        self.git('-C',self.repo,'commit','--quiet','--allow-empty','-m','base');self.base=self.git('-C',self.repo,'rev-parse','HEAD')
        (self.repo/'code.py').write_text('value = 1\n');self.git('-C',self.repo,'add','code.py');self.git('-C',self.repo,'commit','--quiet','-m','work')
        self.sha=self.git('-C',self.repo,'rev-parse','HEAD');self.native=self.root/'native.jsonl'
    def git(self,*args):return subprocess.run(['git',*map(str,args)],capture_output=True,text=True,check=True).stdout.strip()
    def records(self):
        def call(ident,command,ts):return dict(type='response_item',timestamp=ts,payload=dict(type='function_call',call_id=ident,name='functions.exec_command',arguments=json.dumps(dict(cmd=command,workdir=str(self.repo)))))
        def output(ident,text,ts):return dict(type='response_item',timestamp=ts,payload=dict(type='function_call_output',call_id=ident,output=json.dumps(dict(output=text,exit_code=0))))
        return [dict(type='session_meta',timestamp=100,payload=dict(id='s',cwd=str(self.repo),git=dict(repository_url='https://github.com/acme/r',branch='feature',commit_hash=self.base))),
                dict(type='event_msg',timestamp=101,payload=dict(type='user_message',message_id='input',message='Task-ID: feature-1\nSECRET HUMAN TEXT')),
                dict(type='token_usage_record',timestamp=102,payload=dict(response_id='response',usage=dict(input_tokens=10,output_tokens=3))),
                call('commit','git commit -am work',103),output('commit','[feature '+self.sha[:7]+'] work\n',104),
                call('pr','gh pr create --title fixture',105),output('pr','https://github.com/acme/r/pull/12\n',106)]
    def capture(self,records=None,tool='codex'):
        records=records or self.records();self.native.write_text(''.join(json.dumps(r)+'\n' for r in records))
        history.scan_file(self.db,tool,str(self.native),1);history_projection.run(self.db,budget=10)
    def protected(self):
        return self.db.execute("SELECT kind,event_id,payload FROM observations WHERE kind NOT IN ('work_context','work_evidence','work_commit') ORDER BY event_id").fetchall()
    def replay(self,tool='codex',**kwargs):
        with patch.object(collect,'transcript',side_effect=AssertionError('upstream transcript reopened')),patch.object(work,'capture_file',side_effect=AssertionError('native parser reopened')):
            return work_retained.replay(self.db,tool,'s',**kwargs)
    def test_pruned_codex_repairs_commit_and_pre_pr_attribution_once_without_usage_changes(self):
        records=self.records()+[dict(type='token_usage_record',timestamp=107,payload=dict(response_id='after',usage=dict(input_tokens=4,output_tokens=2)))]
        self.capture(records);self.native.unlink();before=self.protected()
        url='https://github.com/acme/r/pull/12'
        store.ingest(self.db,map(json.dumps,[dict(t='pull_request',uid=url,url=url,repo='github.com/acme/r',number=12,ts=200,created_ts=108,state='open',branch='feature',head_sha=self.sha),dict(t='pr_commit',uid=url+':'+self.sha,pr_url=url,ts=200,sha=self.sha)]),machine='host')
        before=self.protected();preview=self.replay()
        self.assertTrue(preview['can_apply']);self.assertEqual(preview['insertable'],5)
        self.assertEqual(self.db.execute('SELECT count(*) FROM work_evidence').fetchone()[0],0)
        applied=self.replay(apply=True);again=self.replay(apply=True)
        self.assertEqual((applied['inserted'],again['inserted']),(5,0));self.assertEqual(self.protected(),before)
        self.assertEqual(self.db.execute('SELECT count(*) FROM native_offsets').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT sha,added_code FROM work_commit_evidence').fetchone(),(self.sha,1))
        report=work.effectiveness(self.db)
        self.assertEqual((report['total_calls'],report['commit_attributed_calls'],report['attributed_calls'],report['pre_pr_calls']),(2,1,2,2))
        self.assertEqual(report['commit_unattributed_reasons']['no_later_commit_evidence'],1)
        self.assertIsNone(report['sessions'][0]['actual_billed_usd'])
    def test_existing_native_work_matches_replay_and_conflict_is_never_overwritten(self):
        self.capture();work.capture_file(self.db,'codex',str(self.native),True);self.native.unlink()
        result=self.replay(apply=True);self.assertEqual((result['inserted'],result['conflicts']),(0,0))
        with self.db:self.db.execute("UPDATE work_commit_evidence SET provenance='git_history'")
        self.assertEqual(self.replay(apply=True)['conflicts'],0)
        self.assertEqual(self.db.execute('SELECT provenance FROM work_commit_evidence').fetchone()[0],'git_history')
        with self.db:self.db.execute("UPDATE work_evidence SET provenance='conflicting-source' WHERE sha<>''")
        result=self.replay(apply=True);self.assertFalse(result['can_apply']);self.assertFalse(result['applied']);self.assertEqual(result['conflicts'],1)
        self.assertEqual(self.db.execute("SELECT provenance FROM work_evidence WHERE sha<>''").fetchone()[0],'conflicting-source')
    def test_missing_header_expired_body_and_limits_remain_explicit_unknowns(self):
        self.capture();self.native.unlink()
        self.assertIn('event_limit_exceeded',self.replay(max_events=2)['gaps'])
        self.assertIn('body_byte_limit_exceeded',self.replay(max_bytes=1)['gaps'])
        with self.db:self.db.execute("DELETE FROM history_events WHERE kind='session_meta'")
        result=self.replay(apply=True);self.assertFalse(result['applied']);self.assertIn('session_header_not_retained',result['gaps'])
        with self.db:self.db.execute('UPDATE history_events SET body=NULL,expired_at=200')
        result=self.replay(apply=True);self.assertFalse(result['applied']);self.assertIn('body_unavailable',result['gaps'])
    def test_claude_repair_uses_recorded_repository_not_current_configuration(self):
        first=dict(type='user',timestamp=100,uuid='u',sessionId='s',cwd=str(self.repo),gitBranch='feature',message=dict(role='user',content='Task-ID: feature-1\nSECRET HUMAN TEXT'))
        self.native.write_text(json.dumps(first)+'\n');work.capture_file(self.db,'claude',str(self.native),True)
        records=[first,dict(type='assistant',timestamp=101,uuid='a',sessionId='s',cwd=str(self.repo),gitBranch='feature',message=dict(role='assistant',id='response',model='m',usage=dict(input_tokens=5,output_tokens=3),content=[dict(type='tool_use',id='commit',name='Bash',input=dict(command='git commit -am work'))])),
                 dict(type='user',timestamp=102,uuid='r',sessionId='s',cwd=str(self.repo),gitBranch='feature',message=dict(role='user',content=[dict(type='tool_result',tool_use_id='commit',content='[feature '+self.sha[:7]+'] work\n')]))]
        self.capture(records,'claude');self.native.unlink();before=self.protected()
        result=self.replay('claude',apply=True)
        self.assertTrue(result['applied']);self.assertEqual(self.db.execute("SELECT count(*) FROM work_evidence WHERE tool='claude' AND sha=?",(self.sha,)).fetchone()[0],1)
        self.assertEqual(self.protected(),before)
    def test_current_repository_alone_is_not_historical_claude_context(self):
        records=[dict(type='user',timestamp=100,uuid='u',sessionId='s',cwd=str(self.repo),gitBranch='feature',message=dict(role='user',content='Task-ID: task'))]
        self.capture(records,'claude');self.native.unlink();result=self.replay('claude')
        self.assertIn('historical_repository_not_recorded',result['gaps']);self.assertEqual(result['insertable'],0)
    def test_replay_does_not_hold_writer_and_source_mutation_prevents_apply(self):
        self.capture();self.native.unlink();original=work.record_events;changed=[]
        def mutate(*args,**kwargs):
            with contextlib.closing(sqlite3.connect(str(self.path),timeout=0)) as other:
                other.execute('BEGIN IMMEDIATE')
                if not changed:
                    other.execute('UPDATE history_events SET expired_at=200,body=NULL WHERE kind=\'session_meta\'');other.commit();changed.append(True)
                else:other.rollback()
            return original(*args,**kwargs)
        with patch.object(work,'record_events',side_effect=mutate):
            with self.assertRaisesRegex(RuntimeError,'changed during replay'):self.replay(apply=True)
        self.assertEqual(self.db.execute('SELECT count(*) FROM work_evidence').fetchone()[0],0)
    def test_preview_is_read_only_and_does_not_print_retained_text(self):
        self.capture();self.native.unlink()
        with contextlib.closing(sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True)) as reader:
            result=work_retained.replay(reader,'codex','s')
        self.assertTrue(result['can_apply']);self.assertNotIn('SECRET',json.dumps(result))

    def test_cli_preview_and_apply_share_the_retained_path_without_disclosing_text(self):
        self.capture();self.native.unlink()
        from erg import cli
        import io
        readonly=store.open_readonly
        for apply in (False,True):
            output=io.StringIO()
            factory=(lambda: sqlite3.connect(str(self.path))) if apply else (lambda:readonly(str(self.path)))
            with patch.object(cli.store,'connect' if apply else 'open_readonly',side_effect=factory),contextlib.redirect_stdout(output):
                args=['work','replay-retained','--tool','codex','--session','s','--machine','host']+(['--apply'] if apply else [])
                cli.main(args)
            result=json.loads(output.getvalue());self.assertEqual(result['applied'],apply)
            self.assertNotIn('SECRET',output.getvalue())
    def test_invalid_bounds_and_active_transactions_are_rejected_before_replay(self):
        for kwargs in (dict(max_events=0),dict(max_events=True),dict(budget=float('nan')),dict(budget=float('inf'))):
            with self.assertRaisesRegex(ValueError,'bounds'):self.replay(**kwargs)
        self.db.execute('BEGIN')
        with self.assertRaisesRegex(ValueError,'idle connection'):self.replay()
        self.db.rollback()

    def test_late_header_does_not_validate_earlier_unscoped_records(self):
        records=self.records();self.capture(records[1:]+records[:1]);self.native.unlink()
        result=self.replay(apply=True)
        self.assertFalse(result['applied']);self.assertIn('session_header_not_retained',result['gaps'])
    def test_conflicting_native_session_or_timestamp_cannot_create_work(self):
        body=dict(type='session_meta',timestamp=100,payload=dict(id='other',cwd=str(self.repo)))
        with self.db:history.put(self.db,'codex','s','p',100,'session_meta',body,'header')
        result=self.replay(apply=True)
        self.assertFalse(result['applied']);self.assertIn('native_session_identity_conflict',result['gaps'])
        with self.db:self.db.execute('DELETE FROM history_events')
        body['payload']['id']='s'
        with self.db:history.put(self.db,'codex','s','p',101,'session_meta',body,'header')
        result=self.replay(apply=True)
        self.assertFalse(result['applied']);self.assertIn('native_timestamp_conflict',result['gaps'])

    def test_unpaired_tools_stay_partial_and_do_not_invent_commit_evidence(self):
        records=[r for r in self.records() if not (r.get('payload',{}).get('type')=='function_call' and r['payload'].get('call_id')=='commit')]
        self.capture(records);self.native.unlink();result=self.replay(apply=True)
        self.assertTrue(result['applied']);self.assertEqual(result['coverage'],'partial')
        self.assertEqual(result['gaps']['tool_call_not_retained'],1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM work_evidence WHERE sha<>''").fetchone()[0],0)
    def test_malformed_work_record_is_a_gap_without_mutation_or_text_disclosure(self):
        body=dict(type='session_meta',timestamp=100,payload=dict(id='s',cwd=str(self.repo),git=['SECRET']))
        with self.db:history.put(self.db,'codex','s','p',100,'session_meta',body,'header')
        result=self.replay(apply=True)
        self.assertFalse(result['applied']);self.assertEqual(result['gaps']['invalid_work_record'],1)
        self.assertNotIn('SECRET',json.dumps(result))
