#!/usr/bin/env python3
"""Audit a static TEMP call-winner read path against the portable canonical oracle.

This is a benchmark, not a production cache: it has no incremental maintenance or
readiness protocol. The main ledger is opened read-only and pinned in one snapshot.
Only function timings, counts and content-free row digests leave the connection.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from erg import report_sqlite,store,unified

CACHED_SELECT = """SELECT w.* FROM prototype_call_winners w WHERE
 (tool<>'local' OR NOT (w.response_id IS NOT NULL AND w.response_id<>'' AND
 (SELECT count(*) FROM prototype_call_winners n WHERE n.machine=w.machine AND n.response_id=w.response_id AND n.model=w.model AND n.tool NOT IN ('local','gateway'))=1 AND
 (SELECT count(*) FROM prototype_call_winners l WHERE l.machine=w.machine AND l.response_id=w.response_id AND l.model=w.model AND l.tool='local')=1)) AND
 (tool<>'gateway' OR NOT (w.request_id IS NOT NULL AND w.request_id<>'' AND
 (SELECT count(*) FROM prototype_call_winners g WHERE g.request_id=w.request_id AND g.model=w.model AND g.tool='gateway')=1 AND
 (SELECT count(*) FROM prototype_call_winners n WHERE n.request_id=w.request_id AND n.model=w.model AND n.tool<>'gateway')=1))"""

def seed(db):
    ranked=unified.CALLS_SELECT.split('), matched AS (',1)[0]+') SELECT * FROM ranked WHERE capture_rank=1'
    db.execute('CREATE TEMP TABLE prototype_call_winners AS '+ranked)
    db.execute('CREATE INDEX temp.prototype_request ON prototype_call_winners(request_id,model,tool)')
    db.execute('CREATE INDEX temp.prototype_response ON prototype_call_winners(machine,response_id,model,tool)')


def run(database):
    db=sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True,timeout=30)
    try:
        db.execute('PRAGMA temp_store=FILE');db.execute('BEGIN')
        start=time.process_time();seed(db);seed_cpu=time.process_time()-start
        fields=','.join(r[1] for r in db.execute('PRAGMA table_info(calls)'))
        def signature(query):
            start=time.process_time();h=hashlib.sha256();n=0
            for row in db.execute('SELECT '+fields+' FROM ('+query+') ORDER BY tool,uid,machine'):
                h.update(json.dumps(row,separators=(',',':')).encode());h.update(b'\n');n+=1
            return dict(cpu_seconds=time.process_time()-start,rows=n,sha256=h.hexdigest())
        original=signature(unified.CALLS_SELECT);projected=signature(CACHED_SELECT)
        if (original['rows'],original['sha256'])!=(projected['rows'],projected['sha256']):raise AssertionError('Canonical row comparison failed')
        rollup='SELECT ts/3600,machine,tool,project,model,kind,SUM(fresh_in),SUM(cache_read),SUM(cache_write),SUM(out),SUM(reasoning),COUNT(*),requester FROM ({}) WHERE ts>=0 AND dup=0 GROUP BY 1,2,3,4,5,6,13'
        def measure(query):
            start=time.process_time();rows=db.execute(rollup.format(query)).fetchall();return time.process_time()-start,rows
        old,expected=measure(unified.CALLS_SELECT)
        cols='ts machine tool project model kind fresh_in cache_read cache_write out reasoning requester dup'.split()
        narrow,reference=measure(report_sqlite.calls_select(cols));new,actual=measure(CACHED_SELECT)
        if expected!=actual or expected!=reference:raise AssertionError('Hourly aggregate comparison failed')
        return dict(seed_cpu_seconds=seed_cpu,full_row_comparison=dict(original=original,prototype=projected),hourly_rollup_cpu_seconds=dict(portable=old,deployed_narrow=narrow,prototype=new),hourly_groups=len(actual),all_canonical_columns_rows_and_groups_identical=True,
            scope='Static read-path prototype on one real read-only main-ledger transaction; SQLite TEMP writes only; no live schema/runtime changes, upstream history import or central export',
            limitations='No incremental maintenance or readiness protocol; safe comparison requires the pinned source snapshot; not deployable or whole-tool CPU acceptance',whole_tool_cpu_acceptance=False)
    finally:db.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',default=store.DB_PATH);parser.add_argument('--output',required=True)
    args=parser.parse_args()
    if os.path.lexists(args.output):raise ValueError('Choose a new evidence path')
    result=run(args.database)
    fd=os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as output:json.dump(result,output,indent=2);output.write('\n')
    print(json.dumps(result,indent=2))

if __name__=='__main__':main()
