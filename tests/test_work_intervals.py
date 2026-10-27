"""Attribution caching must preserve every boundary and conservative unknown."""
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch
from erg import collect,store,work,work_intervals


class AttributionIntervals(unittest.TestCase):
    def test_exact_and_fractional_boundaries_preserve_inclusive_rules(self):
        def pr(m,t,s,ts):return ('pr','operator',{'task'}) if 10<=ts<=20 else (None,'unknown',set())
        def commit(m,t,s,ts):return (('repo','sha'),'verified') if ts<=15 else (None,'unknown')
        key=('host','codex','s');contexts={key:[dict(ts=0,repo='repo',branch='branch',scope_id='scope')]}
        resolve=work_intervals.resolver(pr,commit,contexts,{}, {},{('repo','branch'):['pr']},{'pr':dict(created_ts=10,merged_ts=20)},{},{key+('scope',):[15]})
        for ts in (12,13,20,20.5,21,20,10,9.5,15,15.01,0,-1):
            actual=resolve(*key,ts);expected=(pr(*key,ts),commit(*key,ts))
            self.assertEqual(actual,expected,ts)
        self.assertIsInstance(resolve(*key,12)[0][2],frozenset)
        self.assertGreater(resolve.cache_info()['hits'],0)
    def test_parent_cycles_empty_sessions_and_cache_eviction_preserve_link_edges(self):
        for sid in ('child','',None):
            key=('host','codex',sid)
            links={('codex',sid):[dict(start_ts=5,end_ts=8)]}
            def pr(m,t,s,ts):return ('pr','operator',set()) if 5<=ts<=8 else (None,'unknown',set())
            resolve=work_intervals.resolver(pr,lambda *args:(None,'unknown'),{},links,{('codex','child'):'parent',('codex','parent'):'child'},{},{},{},{},maxsize=2)
            for ts in (1,6,7,8,8.01,20,1,5):self.assertEqual(resolve(*key,ts)[0],pr(*key,ts))
            self.assertLessEqual(resolve.cache_info()['entries'],2)
    def test_full_reports_match_uncached_resolvers_across_mixed_work_histories(self):
        with tempfile.TemporaryDirectory() as folder:
            db=store.connect(Path(folder)/'ledger');self.addCleanup(db.close)
            rng=random.Random(9814);events=[]
            times=[-1,0,4.5,5,5.5,9.5,10,10.5,14,14.1,18,18.1,25,28,30,30.5,32,35,35.5,42,50,50.5,60]
            for i in range(8):
                sid='s'+str(i);child='child'+str(i);urls=['https://github.com/acme/r/pull/'+str(3*i+j+1) for j in range(3)]
                for j,url in enumerate(urls):
                    events.append(dict(t='pull_request',uid=url,ts=100,url=url,repo='github.com/acme/r',number=3*i+j+1,state='merged',created_ts=(5,10,20)[j],merged_ts=(35,50,60)[j],closed_ts=(35,50,60)[j],branch='branch-'+str(i),head_sha='abc',added_code=10,removed_code=1,files_complete=1))
                    for sha in rng.sample(('a','b','c'),2):events.append(dict(t='pr_commit',uid=url+sha,ts=100,pr_url=url,sha=sha))
                events.append(collect.session_record('codex',child,0,parent_session=sid))
                for ts,scope,sha in ((0,'first','a'),(18,'second','b'),(42,'third','c')):
                    events.append(work.context_event('codex',sid,ts,'/code/r',repo='github.com/acme/r',branch='branch-'+str(i),head_sha=sha,scope_id=scope,task_id='task-'+scope))
                if i%2:events.append(work.context_event('codex',sid,18,'/code/r',repo='github.com/other/r',branch='different',head_sha='conflict',scope_id='second'))
                for scope,ts in (('first',14),('second',32)):
                    events.append(dict(t='work_evidence',uid=sid+scope,ts=ts,tool='codex',session=sid,repo='github.com/acme/r',scope_id=scope,sha=rng.choice(('a','b','c')),provenance='tool_commit',capture_version=5))
                for target,start,end,url in ((sid,5,10,urls[0]),(sid,25,30,urls[1]),(child,28,32,urls[2])):
                    events.append(dict(t='work_link',uid=target+str(start),ts=100,tool='codex',session=target,pr_url=url,start_ts=start,end_ts=end,task_id='explicit',active=1,provenance='operator'))
                for n in range(60):
                    ts=rng.choice(times);target=rng.choice((sid,child))
                    events.append(dict(t='llm',uid=sid+'call'+str(n),ts=ts,tool='codex',session=target,project='r',model='gpt-5',fresh_in=10,cache_read=2,cache_write=0,out=3,reasoning=1))
                    events.append(dict(t='prompt',uid=sid+'input'+str(n),ts=ts,tool='codex',session=target,project='r',chars=4,words=1,est_tokens=1,origin=rng.choice(('human','unknown')),provenance='codex_user_event'))
            for event in events:
                for field in ('ts','last_ts','created_ts','merged_ts','closed_ts','start_ts','end_ts'):
                    if event.get(field) is not None:event[field]+=100
            store.ingest(db,map(json.dumps,events),machine='host')
            with patch.object(work.time,'time',return_value=10000):cached=work.effectiveness(db)
            def uncached(pr,commit,*args,**kwargs):
                return lambda *key:(pr(*key),commit(*key))
            with patch.object(work_intervals,'resolver',side_effect=uncached),patch.object(work.time,'time',return_value=10000):oracle=work.effectiveness(db)
            self.assertEqual(cached,oracle)
            self.assertGreater(cached['unattributed_calls'],0)
            self.assertGreater(cached['attributed_calls'],0)
