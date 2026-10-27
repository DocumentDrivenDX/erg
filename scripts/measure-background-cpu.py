#!/usr/bin/env python3
"""Observe an existing local erg installation; never start or restart capture."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from erg import service,store


def process_seconds(stamp):
    days,clock=stamp.split('-',1) if '-' in stamp else ('0',stamp)
    return int(days)*86400+sum(float(n)*60**i for i,n in enumerate(reversed(clock.split(':'))))


def snapshot(port):
    status=service.status()
    if not status.get('running'):raise RuntimeError('collector is not running with a fresh heartbeat')
    with urllib.request.urlopen('http://127.0.0.1:'+str(port)+'/healthz',timeout=8) as response:health=json.load(response)
    rows={}
    for line in subprocess.check_output(['ps','-axo','pid=,ppid=,time='],text=True).splitlines():
        pid,parent,stamp=line.split();rows[int(pid)]=(int(parent),process_seconds(stamp))
    components={}
    for name,state in [('collector',status),('reporter',health)]:
        pid=state['pid']
        if pid not in rows:raise RuntimeError(name+' process disappeared during observation')
        ids={pid}
        while True:
            extra={p for p,(parent,cpu) in rows.items() if parent in ids}-ids
            if not extra:break
            ids|=extra
        children={p:rows[p][1] for p in ids-{pid}};a=state['cpu_accounting']
        components[name]=dict(pid=pid,identity=state.get('started') if name=='collector' else state.get('instance'),sampled_at=a['sampled_at'],cpu_seconds=a['process_cpu_seconds']+a['reaped_children_cpu_seconds']+sum(children.values()),process_cpu_seconds=a['process_cpu_seconds'],reaped_children_cpu_seconds=a['reaped_children_cpu_seconds'],live_children=children)
    with sqlite3.connect('file:'+str(store.DB_PATH)+'?mode=ro',uri=True,timeout=8) as db:
        seq=db.execute('SELECT max(seq) FROM history_events').fetchone()[0]
        pending=db.execute('SELECT count(*) FROM history_pending').fetchone()[0]
    return dict(wall=time.time(),components=components,capture_state=status.get('capture_state'),file_events=status.get('file_events'),history_scan=status.get('history_scan'),last_seq=seq,pending=pending,
                last_full=status.get('last_full'),last_full_error=status.get('last_full_error'),next_full_at=status.get('next_full_at'),last_pr_refresh=status.get('last_pr_refresh'),last_pr_error=status.get('last_pr_error'),worker_cpu=status.get('worker_cpu'),metric_rollup_cache=status.get('metric_rollup_cache'),title_scan=status.get('title_scan'),request_cpu=health.get('request_cpu'))


def interval(a,b):
    result={}
    for name,start in a['components'].items():
        end=b['components'][name];duration=end['sampled_at']-start['sampled_at'];cpu=end['cpu_seconds']-start['cpu_seconds']
        stable=start['pid']==end['pid'] and start.get('identity')==end.get('identity')
        result[name]=100*cpu/duration if stable and duration>0 and cpu>=0 else None
    return dict(component_percent=result,combined_percent=sum(result.values()) if all(v is not None for v in result.values()) else None,wall_seconds=b['wall']-a['wall'])


def save(path,report):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with tempfile.NamedTemporaryFile(mode='w',dir=path.parent,prefix=path.name+'.',delete=False) as output:
        temporary=output.name
        try:
            json.dump(report,output,indent=2);output.flush();os.fsync(output.fileno())
        except BaseException:
            os.unlink(temporary);raise
    try:os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def run(port,duration,period,output):
    if os.path.lexists(output):raise ValueError('choose a new output path; existing evidence is not overwritten')
    report=dict(probe_pid=os.getpid(),state='running',planned_seconds=duration,samples=[],whole_tool_acceptance=False,
                scope='Collector/reporter plus reaped and boundary-live helper CPU. Excludes browser renderer, agent-host hooks, observer and kernel work. Asynchronous collector heartbeat boundaries have timestamp skew. Cold reports, migration work and live capture inside the window are included.')
    try:
        report['samples'].append(snapshot(port));save(output,report)
        deadline=time.monotonic()+duration
        while time.monotonic()<deadline:
            time.sleep(min(period,max(0,deadline-time.monotonic())))
            report['samples'].append(snapshot(port));report['measured']=interval(report['samples'][0],report['samples'][-1]);save(output,report)
            print(json.dumps(report['measured']),flush=True)
        report['state']='complete'
    except KeyboardInterrupt:report['state']='interrupted'
    except Exception as error:
        report.update(state='observation_failed',error=type(error).__name__);raise
    finally:
        samples=report['samples']
        report['scheduled_full_observed']=bool(samples and samples[-1]['last_full'] and samples[-1]['last_full']>=samples[0]['wall'])
        save(output,report)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8787)
    parser.add_argument('--duration',type=float,default=1000)
    parser.add_argument('--period',type=float,default=50)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    if not 1<=args.port<=65535 or args.duration<60 or not 1<=args.period<=50:parser.error('require a valid port, duration >=60 seconds and period 1..50 seconds')
    result=run(args.port,args.duration,args.period,args.output)
    print(json.dumps(dict(state=result['state'],scheduled_full_observed=result['scheduled_full_observed'],output=args.output)),flush=True)

if __name__=='__main__':main()
