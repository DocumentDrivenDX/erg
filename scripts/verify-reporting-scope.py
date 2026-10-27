"""Read-only parity of indexed session evidence against pre-index eligibility.

Checks the entire enrolled scope by default, including already sent observations;
this must not become a trivial comparison of two empty pending queues. Prints
counts, timings and query plans, never identifiers, payloads or account values.
"""
import argparse
import contextlib
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from erg import store,report_delivery

REFERENCE_SESSION=frozenset(('project_context','llm','prompt','edit','session','work_context','work_evidence','work_link','identity','session_route','quota','session_cost','account_binding'))
REFERENCE_ACTIVITY=frozenset(('llm','prompt','edit'))
FORWARD_ONLY=REFERENCE_ACTIVITY|{'session','session_cost','quota'}

def literals(values):return ','.join("'"+v+"'" for v in sorted(values))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',default=store.DB_PATH)
    parser.add_argument('--limit',type=int,default=0,help='Optional session limit; zero checks the entire enrolled scope')
    parser.add_argument('--output')
    args=parser.parse_args()
    if args.limit<0:parser.error('--limit must be nonnegative')
    assert report_delivery.SESSION==REFERENCE_SESSION,'Session eligibility changed; review the reference contract'
    proof=dict(observed_at=time.time(),scope='Session observation eligibility, including already sent records; no capture, publication or export',runs={})
    with contextlib.closing(store.open_readonly(args.database)) as db:
        db.execute('BEGIN')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='delivery_observation_scope'").fetchone():raise SystemExit('Session index is not ready')
        floor=db.execute('SELECT forward_ts FROM delivery_config').fetchone()[0]
        high=db.execute('SELECT seq FROM journal_clock WHERE id=1').fetchone()[0]
        targets='SELECT machine,tool,session FROM delivery_report_scope ORDER BY machine,tool,session'+(' LIMIT '+str(args.limit) if args.limit else '')
        proof['sessions_checked']=db.execute('SELECT count(*) FROM ('+targets+')').fetchone()[0]
        if not proof['sessions_checked']:raise SystemExit('No enrolled sessions to qualify')
        prefix='WITH targets AS ('+targets+') '
        match="EXISTS(SELECT 1 FROM targets s WHERE s.machine=e.machine AND s.tool=json_extract(e.payload,'$.tool') AND s.session=json_extract(e.payload,'$.session'))"
        reference=prefix+"SELECT e.event_id,e.kind,e.ts FROM observations e INDEXED BY observations_kind WHERE e.kind IN ("+literals(REFERENCE_SESSION-REFERENCE_ACTIVITY)+") AND e.seq<=? AND "+match+" UNION ALL SELECT e.event_id,e.kind,e.ts FROM observations e INDEXED BY observations_ts WHERE e.kind IN ("+literals(REFERENCE_ACTIVITY)+") AND e.ts>=? AND e.seq<=? AND "+match
        indexed=prefix+"SELECT e.event_id,e.kind,e.ts FROM targets s CROSS JOIN observations e INDEXED BY delivery_observation_scope ON e.machine=s.machine AND CAST(json_extract(e.payload,'$.tool') AS TEXT)=s.tool AND CAST(json_extract(e.payload,'$.session') AS TEXT)=s.session WHERE e.kind IN ("+literals(REFERENCE_SESSION)+") AND e.seq<=? AND (e.kind NOT IN ("+literals(FORWARD_ONLY)+") OR e.ts>=?)"
        rows={}
        for name,sql,params in [('reference',reference,(high,floor,high)),('indexed',indexed,(high,floor))]:
            wall=time.monotonic();cpu=time.thread_time()
            rows[name]={ident for ident,kind,ts in db.execute(sql,params) if kind not in FORWARD_ONLY or ts>=floor}
            proof['runs'][name]=dict(rows=len(rows[name]),cpu_seconds=time.thread_time()-cpu,wall_seconds=time.monotonic()-wall,query_plan=[r[3] for r in db.execute('EXPLAIN QUERY PLAN '+sql,params)])
        proof.update(equal=rows['reference']==rows['indexed'],missing=len(rows['reference']-rows['indexed']),unexpected=len(rows['indexed']-rows['reference']))
    if args.output:Path(args.output).write_text(json.dumps(proof,indent=2)+'\n')
    print(json.dumps(proof,indent=2))
    if not proof['equal']:raise SystemExit(1)

if __name__=='__main__':main()
