"""Cached aggregate cost evidence. No billable refresh from ordinary status polls."""
import json
import datetime
import os
from pathlib import Path
import subprocess
import sys
import time
from . import collect


def root():return Path(collect.STATE_DIR)/'costs'
def read(name):
    try:
        p=root()/name
        if p.stat().st_size>1024*1024:raise ValueError('oversize aggregate snapshot')
        return json.loads(p.read_text())
    except (OSError,ValueError):return None

def scrub(value):
    if isinstance(value,dict):return {k:scrub(v) for k,v in value.items() if k.lower() not in ('token','access_token','client_secret','secret','password','error_message','authorization')}
    if isinstance(value,list):return [scrub(v) for v in value]
    return value

def snapshot_time():
    value=read('latest.json') or {}
    try:return datetime.datetime.fromisoformat(value['captured_at_utc'].replace('Z','+00:00')).timestamp()
    except (KeyError,ValueError,TypeError):return 0

def status():
    cfg=read('refresh-command.json');refresh=read('refresh-status.json') or {}
    snapshot=read('latest.json') or read('baseline.json')
    return dict(snapshot=scrub(snapshot),refresh=refresh,refresh_configured=bool(cfg and cfg.get('argv')),min_refresh_seconds=21600,
                last_success_at=snapshot_time() or None,next_refresh_at=max(snapshot_time(),refresh.get('requested_at',0))+21600,automatic_refresh=True,real_time_billing=False,scope='Measured upload quantities and posted cloud usage; billed and list-valued amounts are distinct')

def request_refresh():
    from .service import atomic_json
    cfg=read('refresh-command.json')
    if not cfg or not isinstance(cfg.get('argv'),list):return dict(state='not_configured')
    argv=cfg['argv']
    if not argv or len(argv)>24 or not all(isinstance(x,str) and len(x)<=4096 for x in argv):raise ValueError('invalid cost producer')
    previous=read('refresh-status.json') or {};now=time.time()
    if now-max(snapshot_time(),previous.get('requested_at',0))<max(21600,float(cfg.get('min_interval_seconds',21600))):return dict(state='cached',last_request=previous.get('requested_at'))
    root().mkdir(parents=True,exist_ok=True,mode=0o700)
    lock=root()/'.bridge-refresh.lock'
    try:
        if now-lock.stat().st_mtime>300:lock.unlink()
    except FileNotFoundError:pass
    try:fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    except FileExistsError:return dict(state='running')
    try:
        atomic_json(root()/'refresh-status.json',dict(state='queued',requested_at=now))
        subprocess.Popen([sys.executable,'-B','-m','erg.costs_bridge','run'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True,close_fds=True)
    except Exception:

        try:lock.unlink()
        except FileNotFoundError:pass
        raise
    return dict(state='queued')

def run():
    from .service import atomic_json
    previous=read('refresh-status.json') or {};cfg=read('refresh-command.json') or {}
    env={k:v for k,v in os.environ.items() if not k.startswith(('DATABRICKS_','TF_VAR_','OP_'))}
    try:
        p=subprocess.run(cfg['argv'],env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=180)
        result=dict(previous,state='complete' if p.returncode==0 else 'unavailable',returncode=p.returncode,completed_at=time.time())
    except Exception as error:result=dict(previous,state='unavailable',error=type(error).__name__,completed_at=time.time())
    finally:
        try:(root()/'.bridge-refresh.lock').unlink()
        except FileNotFoundError:pass
    atomic_json(root()/'refresh-status.json',result)

if __name__=='__main__':run()
