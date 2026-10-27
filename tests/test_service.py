"""Durability, native identity and user-service lifecycle integration tests."""
import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from erg import collect, service, store

# Capture the tool selected by CI before each fixture clears its environment.
NODE = shutil.which('node')


class Continuous(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name);self.state=self.home/'state'
        self.env=patch.dict(os.environ,{'HOME':str(self.home),'ERG_STATE_DIR':str(self.state)},clear=True)
        self.env.start();self.addCleanup(self.env.stop)
        self.hp=patch.object(collect,'HOME',str(self.home));self.hp.start();self.addCleanup(self.hp.stop)
        self.sp=patch.object(collect,'STATE_DIR',str(self.state));self.sp.start();self.addCleanup(self.sp.stop)

    def node(self,code,*args):
        binary=NODE or shutil.which('node',path='/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin')
        if not binary:self.skipTest('Node required for Pi extension fixture')
        return subprocess.run([binary,'--input-type=module','-e',code,*map(str,args)],capture_output=True,text=True,check=True)

    def test_hook_survives_pruning_and_native_replay_counts_once(self):
        hook=Path(collect.__file__).parent/'hooks/pi.mjs'
        # Node's dynamic import keeps the fixture independent of installed Pi/LLM providers.
        self.node('''
const {install}=await import(process.argv[1]);
const handlers=new Map();install({on:(name,fn)=>handlers.set(name,fn)},{stateDir:process.argv[2]});
const message={role:'assistant',model:'m',provider:'custom',content:'SECRET BODY',auth:'SECRET KEY',
 usage:{input:10,output:7,cacheRead:20,cacheWrite:3,reasoning:2,cost:'SECRET COST'}};
const entry={type:'message',id:'native-entry',timestamp:'2026-10-17T12:00:00Z',message};
const manager={getHeader:()=>({timestamp:'2026-10-17T12:00:00Z',version:3}),getSessionId:()=> 'native-session',
 getCwd:()=>'/code/p',getEntries:()=>[entry]};
const ctx={sessionManager:manager,ui:{notify:()=>{throw Error('unexpected capture error')}}};
handlers.get('turn_end')({},ctx);handlers.get('session_shutdown')({},ctx);
''',hook.as_uri(),self.state)
        snapshot=next((self.state/'pi-snapshots').glob('*.jsonl'))
        self.assertEqual(snapshot.stat().st_mode&0o777,0o600)
        self.assertNotIn('SECRET',snapshot.read_text())
        native=self.home/'.omnigent/pi-native/bridge/sessions/original.jsonl';native.parent.mkdir(parents=True)
        native.write_text(snapshot.read_text())
        with store.connect(str(self.home/'erg.db')) as db:
            service.fast_capture(db,0)
            self.assertEqual(db.execute('SELECT COUNT(*),SUM(out) FROM calls').fetchone(),(1,7))
            native.unlink()  # Omnigent prunes the complete native history.
            service.fast_capture(db,0)
            self.assertEqual(db.execute('SELECT COUNT(*),SUM(out) FROM calls').fetchone(),(1,7))
            payloads=[r[0] for r in db.execute('SELECT payload FROM observations')]
            self.assertNotIn('SECRET',json.dumps(payloads))
            self.assertEqual(db.execute('SELECT uid FROM calls').fetchone()[0],'pi:native-session:native-entry')

    def test_hook_write_failure_warns_without_breaking_agent(self):
        self.state.write_text('not a directory')
        hook=Path(collect.__file__).parent/'hooks/pi.mjs'
        result=self.node('''
const {install}=await import(process.argv[1]);const handlers=new Map();
install({on:(name,fn)=>handlers.set(name,fn)},{stateDir:process.argv[2]});
let warnings=0;const ctx={sessionManager:{getHeader:()=>({timestamp:'2026-10-17T00:00:00Z'}),
 getSessionId:()=> 's',getCwd:()=>'/code/p',getEntries:()=>[]},ui:{notify:()=>warnings++}};
handlers.get('session_start')({},ctx);handlers.get('session_shutdown')({},ctx);console.log(warnings);
''',hook.as_uri(),self.state)
        self.assertEqual(result.stdout.strip(),'1')

    def test_pi_lifecycle_captures_runtime_route_without_secrets_or_backdating(self):
        hook=Path(collect.__file__).parent/'hooks/pi.mjs'
        self.node('''
const {install}=await import(process.argv[1]);const handlers=new Map();
install({on:(name,fn)=>handlers.set(name,fn)},{stateDir:process.argv[2]});
const entry={type:'message',id:'old',timestamp:'2020-01-01T00:00:00Z',message:{role:'assistant',provider:'custom',usage:{input:1,output:2}}};
const ctx={model:{provider:'custom',baseUrl:'http://localhost:9876/v1?token=SECRET',apiKey:'SECRET',headers:{Authorization:'SECRET'}},
 sessionManager:{getHeader:()=>({timestamp:'2020-01-01T00:00:00Z'}),getSessionId:()=> 's',getCwd:()=>'/code',getEntries:()=>[entry]}};
handlers.get('before_agent_start')({},ctx);handlers.get('turn_end')({},ctx);
''',hook.as_uri(),self.state)
        snapshot=next((self.state/'pi-snapshots').glob('*.jsonl'));self.assertNotIn('SECRET',snapshot.read_text())
        rows=[json.loads(line) for line in snapshot.read_text().splitlines()]
        route=next(r for r in rows if r['type']=='erg_route');self.assertEqual(route['route']['base_url'],'http://localhost:9876/v1')
        self.assertGreater(collect.parse_ts(route['timestamp']),collect.parse_ts('2020-01-01T00:00:00Z'))
        with store.connect(str(self.home/'db')) as db:
            service.fast_capture(db,0)
            from erg import work
            s=work.effectiveness(db)['sessions'][0]
            self.assertEqual(s['route_call_coverage']['endpoint_known_calls'],0)

    def test_hook_install_preserves_settings_and_is_idempotent(self):
        settings=self.home/'.pi/agent/settings.json';settings.parent.mkdir(parents=True)
        original={'theme':'dark','extensions':['/existing/extension.mjs']};settings.write_text(json.dumps(original))
        runtime=self.state/'runtime/erg/hooks';runtime.mkdir(parents=True)
        shutil.copy2(str(Path(collect.__file__).parent/'hooks/pi.mjs'),str(runtime/'pi.mjs'))
        hook=service.install_pi_hook(self.state)
        service.install_pi_hook(self.state)
        actual=json.loads(settings.read_text())
        self.assertEqual(actual,dict(original,extensions=['/existing/extension.mjs',hook]))
        self.assertEqual(len(list(settings.parent.glob('settings.json.erg-backup-*'))),1)
        self.assertEqual(settings.stat().st_mode&0o777,0o600)
        result=self.node("const loader=await import(process.argv[1]);let n=0;loader.default({on:()=>n++});console.log(n);",Path(hook).as_uri())
        self.assertEqual(result.stdout.strip(),'10')

    def test_launchd_install_uses_runtime_and_replaces_known_sampler(self):
        dbpath=self.home/'erg.db'
        agents=self.home/'Library/LaunchAgents';agents.mkdir(parents=True)
        sampler=agents/'dev.ddx.erg.sampler.plist'
        sampler.write_bytes(plistlib.dumps({'Label':'dev.ddx.erg.sampler','ProgramArguments':['python',str(self.state.resolve()/'collect.py'),'sample','--loop','30']}))
        with patch.object(service.sys,'platform','darwin'),patch.object(store,'DB_PATH',str(dbpath.resolve())),patch.object(service.subprocess,'run',return_value=subprocess.CompletedProcess([],0)) as run:
            result=service.install(interval=900,poll=2)
        config=plistlib.loads(Path(result['plist']).read_bytes())
        self.assertEqual(config['ProcessType'],'Standard');self.assertTrue(config['LowPriorityBackgroundIO']);self.assertNotIn('LowPriorityIO',config)
        self.assertEqual(config['Umask'],0o077)
        self.assertIn('--poll',config['ProgramArguments'])
        self.assertNotIn('--push',config['ProgramArguments'])
        self.assertEqual(config['EnvironmentVariables']['ERG_DATABASE'],str(dbpath.resolve()))
        self.assertTrue((self.state/'runtime/erg/hooks/pi.mjs').exists())
        self.assertTrue(Path(result['cli']).is_file());self.assertEqual(Path(result['cli']).stat().st_mode&0o777,0o700)
        self.assertFalse(sampler.exists());self.assertTrue(sampler.with_suffix('.plist.disabled').exists())
        self.assertEqual(run.call_args.args[0][1],'bootstrap')

    def test_real_service_heartbeats_and_exits_cleanly(self):
        env=dict(os.environ,PATH='/opt/homebrew/bin:/usr/bin:/bin',PYTHONDONTWRITEBYTECODE='1',
                 PYTHONPATH=str(Path(collect.__file__).parent.parent),ERG_DATABASE=str(self.home/'erg.db'))
        proc=subprocess.Popen([sys.executable,'-B','-c','from erg import service; import time; service.collect.take_sample=lambda: time.sleep(5); service.run(30,1)'],
                              env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
        try:
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                if service.status().get('last_fast'):break
                if proc.poll() is not None:self.fail('service exited before heartbeat: '+proc.stderr.read())
                time.sleep(.1)
            self.assertTrue(service.status()['running']);self.assertIsNotNone(service.status()['last_fast'])
            proc.terminate();proc.communicate(timeout=12)
            self.assertEqual(proc.returncode,0)
            self.assertFalse(service.status()['running'])
        finally:
            with contextlib.suppress(ProcessLookupError):os.killpg(proc.pid,signal.SIGKILL)
            proc.communicate()

    def test_status_reports_capture_errors_as_degraded(self):
        service.atomic_json(self.state/'collector-status.json',dict(pid=os.getpid(),heartbeat=time.time(),poll=2,last_fast_error='OperationalError'))
        value=service.status();self.assertTrue(value['running']);self.assertEqual(value['state'],'degraded')

    def test_concurrent_hook_writers_keep_distinct_durable_snapshots(self):
        hook=Path(collect.__file__).parent/'hooks/pi.mjs'
        self.node('''
const {install}=await import(process.argv[1]);
for(const n of [2,1]){
 const handlers=new Map();install({on:(name,fn)=>handlers.set(name,fn)},{stateDir:process.argv[2]});
 const entries=Array.from({length:n},(_,i)=>({type:'message',id:'entry'+i,timestamp:'2026-10-17T00:00:00Z',
  message:{role:'assistant',model:'m',provider:'p',usage:{input:1,output:2}}}));
 handlers.get('session_shutdown')({}, {sessionManager:{getHeader:()=>({timestamp:'2026-10-17T00:00:00Z'}),
  getSessionId:()=> 'shared',getCwd:()=>'/code/p',getEntries:()=>entries}});
}
''',hook.as_uri(),self.state)
        self.assertEqual(len(list((self.state/'pi-snapshots').glob('*.jsonl'))),2)
        with store.connect(str(self.home/'erg.db')) as db:
            service.fast_capture(db,0)
            self.assertEqual(db.execute('SELECT COUNT(*),SUM(out) FROM calls').fetchone(),(2,4))

    def test_launchd_bootstrap_retries_teardown_race(self):
        with patch.object(service.subprocess,'run',side_effect=[subprocess.CompletedProcess([],5),subprocess.CompletedProcess([],0)]) as run,patch.object(service.time,'sleep') as sleep:
            service.bootstrap('gui/501','/fixture/collector.plist')
        self.assertEqual(run.call_count,2);sleep.assert_called_once_with(.5)

    def test_native_usage_subprocess_refresh_is_replay_safe_and_does_not_sample(self):
        native=self.home/'.codex/sessions/test.jsonl';native.parent.mkdir(parents=True)
        native.write_text('\n'.join(json.dumps(r) for r in [
            dict(type='session_meta',timestamp='2026-10-17T00:00:00Z',payload=dict(id='s',cwd='/code',source='cli')),
            dict(type='token_usage_record',timestamp='2026-10-17T00:00:01Z',payload=dict(response_id='r',usage=dict(input_tokens=100,cache_write_input_tokens=90,output_tokens=2)))])+'\n')
        with store.connect(str(self.home/'db')) as db:
            for _ in range(2):self.assertEqual(service.native_usage_capture(db,0),0)
            self.assertEqual(db.execute('select count(*),sum(fresh_in+cache_read+cache_write+out) from calls').fetchone(),(1,102))
            self.assertEqual(db.execute('select count(*) from metrics').fetchone()[0],0)
        self.assertFalse((self.state/'perf.ndjson').exists())

    def test_optional_enrichment_yields_to_backlog_without_disabling_source_adapters(self):
        healthy=dict(usage_backlog=0,work_projection=dict(pending=0))
        self.assertFalse(service.defer_optional_enrichment(healthy,True))
        self.assertTrue(service.defer_optional_enrichment(dict(healthy,usage_backlog=1001),True))
        self.assertTrue(service.defer_optional_enrichment(dict(healthy,work_projection=dict(pending=10001)),True))
        self.assertTrue(service.defer_optional_enrichment({},True))
        self.assertFalse(service.defer_optional_enrichment({},False))
        cmd=service.full_command(123,True,True)
        self.assertIn('--retained',cmd);self.assertIn('--no-git',cmd);self.assertIn('--no-metrics',cmd)
        self.assertEqual(cmd[cmd.index('--perf-since')+1],'123')
        self.assertNotIn('--no-git',service.full_command(123,False,True))
        self.assertNotIn('--no-git',service.full_command(123,True,False))
        service.atomic_json(self.state/'collector-status.json',dict(heartbeat=time.time(),poll=2,enrichment_deferred_reason='projection_backlog'))
        observed=service.status()
        self.assertEqual(observed['capture_state'],'running');self.assertEqual(observed['enrichment_state'],'deferred')

    def test_pr_enrichment_failure_does_not_mislabel_capture_health(self):
        service.atomic_json(self.state/'collector-status.json',dict(heartbeat=time.time(),poll=2,last_pr_error='metadata_fetch_failed'))
        value=service.status()
        self.assertEqual(value['state'],'degraded');self.assertEqual(value['capture_state'],'running')
        self.assertEqual(value['enrichment_state'],'degraded')
        service.atomic_json(self.state/'collector-status.json',dict(heartbeat=time.time(),poll=2,last_pr_error=None,last_pr_refresh=time.time()))
        value=service.status();self.assertEqual(value['state'],'running');self.assertEqual(value['enrichment_state'],'running')

    def test_fresh_heartbeat_cannot_hide_stalled_native_capture_worker(self):
        service.atomic_json(self.state/'collector-status.json',dict(heartbeat=time.time(),started=time.time()-200,poll=2,last_usage=time.time()-150))
        value=service.status();self.assertTrue(value['running']);self.assertEqual(value['capture_state'],'degraded')
        self.assertEqual(value['workers']['usage']['state'],'stale')
