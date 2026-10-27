"""Read-only full-corpus parity qualification for the local History index.

Run: python3 scripts/verify-history-browse.py [--database PATH] [--output PATH]
This deliberately performs an expensive raw comparison; it never initializes a
ledger, reads transcript bodies, or enables capture/export.
"""
import argparse,contextlib,hashlib,json,time,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from erg import store,history,history_browse,project_scope,report_scope
parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--database',default=store.DB_PATH);parser.add_argument('--output');args=parser.parse_args()
proof={'observed_at':time.time(),'runs':{}}
version=history_browse.VERSION
with contextlib.closing(store.open_readonly(args.database)) as db:
 db.execute('BEGIN');upper=db.execute('SELECT max(seq) FROM history_events').fetchone()[0]
 end=(int(time.time())//3600+1)*3600;selection=report_scope.Selection(dict(start=0,end=end))
 links={r[0]:(r[1],r[2]) for r in db.execute('SELECT local_session,tool,session FROM history_links')}
 proof['through_seq']=upper;proof['index_status']=history_browse.status(db)
 if not proof['index_status'].get('ready_hours'):raise SystemExit('Browse index has no ready hours; wait for background catch-up')
 for mode in ('indexed','raw'):
  hierarchy=project_scope.Scope(db,end);stats=dict(indexed_hours=0,raw_hours=0);total={}
  wall=time.monotonic();cpu=time.thread_time()
  iterator=history_browse.metadata(db,selection,upper,hierarchy,links,stats) if mode=='indexed' else history._allocated_metadata(db,selection,upper,hierarchy,links)
  for row,allocation in iterator:
   key=(row['machine'],row['tool'],row['session'],allocation['folder']['folder_key'],tuple((p['project_key'],p['units']) for p in allocation['allocations']))
   item=total.setdefault(key,[0,0,row['ts'],row['last_ts'],row['seq']]);item[0]+=row['count'];item[1]+=row['expired'];item[2]=min(item[2],row['ts']);item[3]=max(item[3],row['last_ts']);item[4]=max(item[4],row['seq'])
  raw=json.dumps(sorted(total.items()),separators=(',',':')).encode()
  proof['runs'][mode]={'wall_seconds':time.monotonic()-wall,'cpu_seconds':time.thread_time()-cpu,'facts':len(total),'physical_events':sum(r[0] for r in total.values()),'sha256':hashlib.sha256(raw).hexdigest(),'hours':stats}

 assert proof['runs']['indexed']['hours']['indexed_hours']>0,'No indexed hours were exercised'
 proof['all_retained_facts_match']=proof['runs']['indexed']['sha256']==proof['runs']['raw']['sha256'];assert proof['all_retained_facts_match']
 outputs=[]
 for mode in ('indexed','raw'):
  with patch.object(history_browse,'VERSION',version if mode=='indexed' else -1):data=history.scoped_sessions(db,selection.values,limit=100,through_seq=upper)
  data.pop('browse_index');outputs.append(data)
 proof['full_page_response_match']=outputs[0]==outputs[1]
 assert proof['full_page_response_match'],'Full page metadata or folder catalogs differ'
 proof['distinct_sessions']=len({k[:3] for k in total})
if args.output:Path(args.output).write_text(json.dumps(proof,indent=2)+'\n')
print(json.dumps(proof,indent=2))
