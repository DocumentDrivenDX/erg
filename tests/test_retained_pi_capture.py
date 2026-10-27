"""Forward Pi projection must not depend on the redundant whole-file usage scraper."""
import contextlib
import signal
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from erg import collect,history,store

class RetainedPiCapture(unittest.TestCase):
 def test_service_captures_pi_without_legacy_usage_scraping(self):
  with tempfile.TemporaryDirectory() as temporary:
   root=Path(temporary);state=root/'state';state.mkdir();dbpath=root/'ledger'
   with patch.object(collect,'STATE_DIR',str(state)):
    db=store.connect(str(dbpath));history.enroll(db);db.close()
   logs=root/'.pi/agent/sessions';logs.mkdir(parents=True);now=int(time.time())+1
   records=[dict(type='session',id='proof',timestamp=now,cwd=str(root/'project')),
            dict(type='message',id='call',timestamp=now,message=dict(role='assistant',model='m',usage=dict(input=5,output=3)))]
   (logs/'session.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
   marker=root/'redundant-scraper';command_file=root/'scheduled-command.json'
   code="""from pathlib import Path
from erg import service,collect,account_probe,history
import json,time
original_popen=service.subprocess.Popen
def launch(argv,*args,**kwargs):
 if len(argv)>4 and argv[2:5]==['-m','erg.collect','collect']:Path(%r).write_text(json.dumps(argv))
 return original_popen(argv,*args,**kwargs)
service.subprocess.Popen=launch
original_scan=history.scan
failed=[False]
def capture(db,*args,**kwargs):
 if not failed[0]:
  failed[0]=True
  history.put(db,'pi','rollback-proof','p',int(time.time()),'message',dict(content='uncommitted'),'injected')
  raise ValueError('injected partial transaction')
 return original_scan(db,*args,**kwargs)
history.scan=capture
service.collect.take_sample=lambda: None
account_probe.poll=lambda db: 0
def forbidden(*args):
 Path(%r).write_text('called')
 raise AssertionError('redundant scraper')
collect.collect_pi=forbidden
service.run(2,1)
"""%(str(command_file),str(marker))
   env=dict(os.environ,HOME=str(root),ERG_STATE_DIR=str(state),ERG_DATABASE=str(dbpath),
     CODEX_HOME=str(root/'.codex'),CLAUDE_CONFIG_DIR=str(root/'.claude'),PI_CODING_AGENT_DIR=str(root/'.pi/agent'),
     PI_CODING_AGENT_SESSION_DIR=str(logs),OMNIGENT_DATA_DIR=str(root/'.omnigent'),
     ERG_DISCOVERY_FILE=str(root/'discovery.json'),PYTHONDONTWRITEBYTECODE='1')
   process=subprocess.Popen([sys.executable,'-B','-c',code],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,start_new_session=True)
   try:
    deadline=time.monotonic()+30;result=None
    while time.monotonic()<deadline:
     with sqlite3.connect(str(dbpath)) as db:result=db.execute("SELECT count(*),sum(fresh_in),sum(out) FROM calls WHERE tool='pi' AND session='proof'").fetchone()
     status_file=state/'collector-status.json'
     status=json.loads(status_file.read_text()) if status_file.exists() else {}
     if result==(1,5,3) and command_file.exists() and status.get('last_full'):break
     if process.poll() is not None:break
     time.sleep(.1)
    self.assertEqual(result,(1,5,3));self.assertFalse(marker.exists())
    with sqlite3.connect(str(dbpath)) as db:self.assertEqual(db.execute("SELECT count(*) FROM history_events WHERE session='rollback-proof'").fetchone()[0],0)
    self.assertIn('--retained',json.loads(command_file.read_text()))
    self.assertEqual(status.get('full_mode'),'periodic_retained_enrichment')
    self.assertIsNotNone(status.get('last_full'))
    self.assertIsNone(status.get('last_full_error'))
    workers=status['worker_cpu']['workers']
    self.assertGreaterEqual(workers['full_enrichment']['runs'],1)
    self.assertEqual(workers['full_enrichment']['errors'],0)
    self.assertGreaterEqual(workers['history_scan']['runs'],1)
    self.assertIn('excludes subprocess CPU',status['worker_cpu']['scope'])
    self.assertIsNone(status.get('last_metric_rollup_error'))
    self.assertTrue(status['metric_rollup_cache']['seed_complete'])
    self.assertIn('metric_rollups',workers)
    self.assertIn('title_refresh',workers)
    self.assertIsInstance(status['title_scan'],dict)
   finally:
    process.terminate()
    try:process.communicate(timeout=8)
    except subprocess.TimeoutExpired:
     with contextlib.suppress(ProcessLookupError):os.killpg(process.pid,signal.SIGKILL)
     process.communicate()
    finally:
     with contextlib.suppress(ProcessLookupError):os.killpg(process.pid,signal.SIGKILL)
