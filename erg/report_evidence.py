"""Work and account facts published with Usage/Costs, never on HTTP reads."""
import copy
import json
import math
import os
import time
import uuid
from . import accounting,account_reports,projects,rates,report_views,work,titles

DIMS='human machine tool session folder reason binding_refs model kind requester evidence'.split()
METRICS='distinct_calls request_equivalents tokens priced_equivalents unpriced_equivalents known_value_low_usd known_value_high_usd reported_equivalents known_reported_usd fresh_in cache_read cache_write out reasoning input_equivalents input_events human_inputs unknown_inputs'.split()
FIELDS=['version','hour','ts']+DIMS+METRICS+['first_ts','last_ts']


def initialize(db,trigger):
    ddl=','.join(c+(' REAL' if c in METRICS+['first_ts','last_ts'] else ' INTEGER' if c in ('hour','ts') else ' TEXT') for c in FIELDS)
    for suffix in ('fine','hour'):
        table='project_rollup_evidence_'+suffix
        db.execute('CREATE TABLE IF NOT EXISTS '+table+'('+ddl+')')
        db.execute('CREATE INDEX IF NOT EXISTS '+table+'_version ON '+table+'(version,hour)')
        db.execute('CREATE INDEX IF NOT EXISTS '+table+'_scope ON '+table+'(human,hour)')
    for key in ('evidence_account_serial','evidence_quota_serial','evidence_title_serial'):
        db.execute("INSERT INTO project_rollup_state VALUES(?, '0') ON CONFLICT(key) DO NOTHING",(key,))
    db.execute("INSERT INTO project_rollup_state VALUES('preparation_generation',?) ON CONFLICT(key) DO NOTHING",(uuid.uuid4().hex,))
    # Metadata changes reproject affected session hours, including work performed
    # before the PR/commit was created. No-op native upserts generate no work.
    local=('work_contexts','work_evidence','work_links','history_work_conflicts','sessions','account_binding_events','session_route_events')
    global_tables=('pull_requests','pr_commits','subscription_charge_events')
    work_metadata_only=('work_commit_evidence',)
    identity_tables=('identity_events',)
    metadata_only=('quota_events','session_titles')
    for table in local+global_tables+identity_tables+metadata_only+work_metadata_only:
        cols=[r[1] for r in db.execute('PRAGMA table_info('+table+')')]
        if not cols:continue
        if table=='sessions':cols=[c for c in ('machine','tool','session','parent_session') if c in cols]
        for event in ('INSERT','UPDATE','DELETE'):
            aliases=('OLD',) if event=='DELETE' else ('NEW',) if event=='INSERT' else ('OLD','NEW')
            when=''
            if event=='UPDATE':when=' WHEN '+' OR '.join("NULLIF(OLD.parent_session,'') IS NOT NULLIF(NEW.parent_session,'')" if table=='sessions' and c=='parent_session' else 'OLD.'+c+' IS NOT NEW.'+c for c in cols)
            # BEFORE INSERT covers SQLite REPLACE, whose delete trigger may not
            # run. Exact existing rows do not invalidate the projection.
            if event=='INSERT':when=' WHEN NOT EXISTS(SELECT 1 FROM '+table+' p WHERE '+' AND '.join("NULLIF(p.parent_session,'') IS NULLIF(NEW.parent_session,'')" if table=='sessions' and c=='parent_session' else 'p.'+c+' IS NEW.'+c for c in cols)+')'
            body="INSERT INTO project_rollup_state VALUES('evidence_serial','1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1; "
            component='account' if table in ('identity_events','account_binding_events','session_route_events','subscription_charge_events') else 'quota' if table=='quota_events' else 'title' if table=='session_titles' else None
            if component:
                body+="INSERT INTO project_rollup_state VALUES('evidence_"+component+"_serial','1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1; "
            if table not in metadata_only:
                body+="INSERT INTO project_rollup_state VALUES('evidence_work_serial','1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1; "
                if table in work_metadata_only:
                    pass  # Commit-object statistics live in shared report metadata.
                elif table=='work_evidence':
                    for alias in aliases:
                        seeds='SELECT '+','.join(alias+'.'+c+' '+c for c in ('machine','tool','session','scope_id'))
                        if event=='INSERT':seeds+=' UNION SELECT machine,tool,session,scope_id FROM work_evidence WHERE machine=NEW.machine AND uid=NEW.uid'
                        # Facts apply to the recorded scope, including calls
                        # before the commit/PR. Context intervals, not fact time,
                        # bound the affected calls and human inputs.
                        for source in ('call_winner_rows','prompts'):
                            body+='INSERT INTO project_rollup_dirty SELECT DISTINCT r.ts/3600,1 FROM ('+seeds+') s CROSS JOIN work_contexts c ON c.machine=s.machine AND c.tool=s.tool AND c.session=s.session AND c.scope_id IS s.scope_id CROSS JOIN '+source+' r ON r.machine=c.machine AND r.tool=c.tool AND r.session=c.session AND r.ts>=c.ts AND r.ts<COALESCE((SELECT min(n.ts) FROM work_contexts n WHERE n.machine=c.machine AND n.tool=c.tool AND n.session=c.session AND n.ts>c.ts),9223372036854775807) WHERE 1 ON CONFLICT(hour) DO UPDATE SET serial=serial+1; '
                elif table in identity_tables:

                    for alias in aliases:
                        seeds='SELECT '+','.join(alias+'.'+c+' '+c for c in ('tool','session','account_key','account_id_hash','email_hash'))
                        if event=='INSERT':seeds+=' UNION SELECT tool,session,account_key,account_id_hash,email_hash FROM identity_events WHERE machine=NEW.machine AND uid=NEW.uid'
                        same='SELECT 1 FROM identity_events z WHERE '+ ' AND '.join('z.'+c+' IS '+alias+'.'+c for c in ('account_key','account_id_hash','email_hash'))
                        if event=='DELETE':same+=' AND NOT(z.machine=OLD.machine AND z.uid=OLD.uid)'
                        novelty='NOT EXISTS('+same+')'
                        if event=='UPDATE':novelty='('+ ' OR '.join('OLD.'+c+' IS NOT NEW.'+c for c in ('account_key','account_id_hash','email_hash'))+')'
                        if event=='INSERT':novelty='('+novelty+' OR EXISTS(SELECT 1 FROM identity_events prior WHERE prior.machine=NEW.machine AND prior.uid=NEW.uid AND ('+' OR '.join('prior.'+c+' IS NOT NEW.'+c for c in ('account_key','account_id_hash','email_hash'))+')))'
                        candidates='WITH seeds AS ('+seeds+") SELECT i.tool,i.session FROM seeds s JOIN identity_events i ON (length(s.account_key)>0 AND i.account_key=s.account_key) OR (length(s.account_id_hash)>0 AND i.account_id_hash=s.account_id_hash) OR (length(s.email_hash)>0 AND i.email_hash=s.email_hash) WHERE "+novelty+" UNION SELECT tool,session FROM seeds"
                        for source in ('call_winner_rows','prompts'):
                            body+='INSERT INTO project_rollup_dirty SELECT DISTINCT r.ts/3600,1 FROM ('+candidates+') i CROSS JOIN '+source+' r ON r.tool=i.tool AND r.session=i.session WHERE i.session IS NOT NULL AND length(i.session)>0 ON CONFLICT(hour) DO UPDATE SET serial=serial+1; '
                elif table in ('pull_requests','pr_commits'):
                    for alias in aliases:
                        url=alias+('.url' if table=='pull_requests' else '.pr_url')
                        branches='SELECT repo,branch FROM pull_requests WHERE url='+url
                        if table=='pull_requests':branches+=' UNION SELECT '+alias+'.repo,'+alias+'.branch'
                        candidates='SELECT c.tool,c.session FROM work_contexts c JOIN ('+branches+') p ON c.repo=p.repo AND c.branch=p.branch UNION SELECT tool,session FROM work_evidence WHERE pr_url='+url+' OR (repo,sha) IN (SELECT p.repo,pc.sha FROM pull_requests p JOIN pr_commits pc ON pc.pr_url=p.url WHERE p.url='+url+') UNION SELECT tool,session FROM work_links WHERE pr_url='+url
                        if table=='pr_commits':candidates+=' UNION SELECT tool,session FROM work_evidence WHERE sha='+alias+'.sha'
                        for source in ('call_winner_rows','prompts'):
                            body+='INSERT INTO project_rollup_dirty SELECT DISTINCT r.ts/3600,1 FROM ('+candidates+') i CROSS JOIN '+source+' r ON r.tool=i.tool AND r.session=i.session WHERE 1 ON CONFLICT(hour) DO UPDATE SET serial=serial+1; '
                elif table in global_tables or table=='sessions' and 'parent_session' not in cols:
                    body+='INSERT INTO project_rollup_dirty SELECT hour,1 FROM project_rollup_heads WHERE 1 ON CONFLICT(hour) DO UPDATE SET serial=serial+1; '
                else:
                    for alias in aliases:
                        seeds='SELECT '+alias+'.tool tool,'+alias+'.session session'
                        if event=='INSERT' and 'uid' in cols and 'machine' in cols:seeds+=' UNION SELECT tool,session FROM '+table+' WHERE machine=NEW.machine AND uid=NEW.uid'
                        candidates='WITH RECURSIVE affected(tool,session) AS ('+seeds+' UNION SELECT s.tool,s.session FROM sessions s JOIN affected a ON s.tool=a.tool AND s.parent_session=a.session) SELECT tool,session FROM affected'
                        for source in ('call_winner_rows','prompts'):
                            body+='INSERT INTO project_rollup_dirty SELECT DISTINCT r.ts/3600,1 FROM ('+candidates+') i CROSS JOIN '+source+' r ON r.tool=i.tool AND r.session=i.session WHERE '+('r.ts>='+alias+'.ts' if table=='work_contexts' else '1')+' ON CONFLICT(hour) DO UPDATE SET serial=serial+1; '
            trigger(db,'evidence_'+table+'_'+event.lower(),('BEFORE INSERT' if event=='INSERT' else 'AFTER '+event)+' ON '+table+when,body)
    db.execute("CREATE INDEX IF NOT EXISTS sessions_evidence_identity ON sessions(machine,tool,session,NULLIF(parent_session,''))")
    db.execute('CREATE INDEX IF NOT EXISTS sessions_evidence_parent ON sessions(tool,parent_session,session)')
    db.execute('CREATE INDEX IF NOT EXISTS context_evidence_branch ON work_contexts(repo,branch,tool,session)')
    db.execute('CREATE INDEX IF NOT EXISTS context_evidence_scope ON work_contexts(machine,tool,session,scope_id,ts)')
    db.execute('CREATE INDEX IF NOT EXISTS work_evidence_sha ON work_evidence(repo,sha,tool,session)')
    for field in ('account_key','account_id_hash','email_hash'):db.execute('CREATE INDEX IF NOT EXISTS identity_evidence_'+field+' ON identity_events('+field+',tool,session)')
    db.execute('CREATE INDEX IF NOT EXISTS winner_evidence_session ON call_winner_rows(tool,session,ts)')
    db.execute('CREATE INDEX IF NOT EXISTS prompt_evidence_session ON prompts(tool,session,ts)')


class PreparationCache:
    """One in-memory snapshot per component; never persisted or shared by workers."""
    def __init__(self):self.owner=None;self.values={};self.hits={};self.misses={}
    def bind(self,db,versions):
        required=('preparation_generation','evidence_account_serial','evidence_quota_serial','evidence_title_serial')
        if not all(key in versions for key in required):return lambda name,key,build:build()
        try:
            files=[]
            for _,name,path in db.execute('PRAGMA database_list'):
                if name!='main':continue
                stat=os.stat(path) if path else None
                files.append((name,path,stat.st_dev if stat else None,stat.st_ino if stat else None))
            files=tuple(files)
        except OSError:
            self.values.clear();self.owner=None;return lambda name,key,build:build()
        owner=(files,versions['preparation_generation'],db.execute('PRAGMA schema_version').fetchone()[0])
        if owner!=self.owner:self.values.clear();self.owner=owner
        def get(name,key,build):
            cached=self.values.get(name)
            if cached is not None and cached[0]==key:
                self.hits[name]=self.hits.get(name,0)+1;return cached[1]
            value=build();self.values[name]=(key,value);self.misses[name]=self.misses.get(name,0)+1;return value
        return get
    def stats(self):return dict(components=len(self.values),hits=dict(self.hits),misses=dict(self.misses))


def prepare(db,tick=None,previous=None,cache=None,versions=None):
    if cache is not None:
        from . import project_rollups
        versions=project_rollups.state(db) if versions is None else versions
        get=cache.bind(db,versions)
    else:versions={};get=lambda name,key,build:build()
    def work_snapshot():
        resolve,prs,commits,contexts,facts,commit_prs,facts_by_url,objects=work.prepare_attribution(db,tick)
        metadata=dict(prs=list(prs.values()),commits=[dict(c,prs=sorted(commit_prs.get((c['repo'],c['sha']),()))) for c in objects('work_commit_evidence')],automatic_evidence_count=len(facts),facts_by_url=facts_by_url)
        return resolve,metadata
    if previous is None:resolve,work_metadata=get('work',versions.get('evidence_work_serial'),work_snapshot)
    else:resolve=None;work_metadata={key:previous[key] for key in ('prs','commits','automatic_evidence_count','facts_by_url')}
    def account_snapshot():
        account,root,identities=accounting.resolver(db)
        periods=account_reports.periods(db,root,identities)
        registry=report_views.account_registry([dict(i,account_key=root(i['account_key'])) for i in identities])
        return account,root,periods,registry
    account,root,original_periods,registry=get('account',versions.get('evidence_account_serial'),account_snapshot)
    # finish() adds presentation fields. Cached period inputs must remain pristine.
    periods=copy.deepcopy(original_periods)
    from . import quota_projection
    ready=quota_projection.ready(db) if quota_projection.available(db) else False
    quota=get('quota',(versions.get('evidence_quota_serial'),versions.get('evidence_account_serial'),ready),lambda:account_reports.quota_summary(db,root))
    metadata=account_reports.finish(db,root,[],rates.empty(),periods,quota=quota)
    metadata.pop('accounts');metadata.update(account_registry=registry,**work_metadata)
    metadata['session_titles']=get('title',versions.get('evidence_title_serial'),lambda:titles.rows(db))
    return resolve,account,periods,metadata


class Builder:
    def __init__(self,prepared,version,hour,writer):
        self.resolve,self.account,self.periods,self.metadata=prepared
        self.version=version;self.hour=hour;self.writer=writer;self.pending=[];self.cells={};self.prs={p['url']:p for p in self.metadata['prs']}
    def add(self,call,allocation,value=None,is_input=False):
        pr,commit=self.resolve(call['machine'],call['tool'],call['session'],call['ts'])
        url,why,tasks=pr;ck,commit_why=commit
        account=self.account(call['tool'],call['session'],call['ts'],request_uid=call.get('uid','')) if not is_input else None
        eligible=[json.dumps([p[k] for k in ('account_key','subscription_key','period_start','period_end')],separators=(',',':')) for p in self.periods.values() if account and account['billing_evidence'] and p['account_key']==account['account_key'] and (p.get('expense_basis') or not account['subscription_key'] or p['subscription_key']==account['subscription_key']) and p['period_start']<=call['ts']<p['period_end']]
        recorded=[key for key in eligible if not self.periods[tuple(json.loads(key))].get('expense_basis')]
        if recorded:eligible=recorded
        period=eligible[0] if len(eligible)==1 else None
        evidence=json.dumps(dict(rate_asof=value['as_of'] if value else None,rate_source=value['source'] if value else None,pr=url,why=why,tasks=sorted(tasks),commit=ck,commit_why=commit_why,account=account,period=period,input=is_input,pre_pr=bool(url and self.prs.get(url,{}).get('created_ts') and call['ts']<self.prs[url]['created_ts'])),sort_keys=True,separators=(',',':'))
        for part in [dict(project_key='',units=projects.UNITS)]+allocation['allocations']:
            w=part['units']/projects.UNITS
            dims=[part['project_key'],call['machine'],call['tool'],call['session'],allocation['folder']['folder_key'],allocation['reason'] or '',json.dumps(allocation['binding_refs']),call.get('model'),call.get('kind'),call.get('requester'),evidence]
            reported=call.get('reported_cost_usd');known=isinstance(reported,(int,float)) and not isinstance(reported,bool) and math.isfinite(reported) and reported>=0
            metrics=([0]*14+[w,1,w if call.get('origin')=='human' else 0,w if call.get('origin') not in ('human','automation') else 0]) if is_input else [1,w,sum(call.get(k) or 0 for k in ('fresh_in','cache_read','cache_write','out'))*w,w if value else 0,0 if value else w,(value['low_usd'] if value else 0)*w,(value['high_usd'] if value else 0)*w,w if known else 0,(reported if known else 0)*w]+[(call.get(k) or 0)*w for k in ('fresh_in','cache_read','cache_write','out','reasoning')]+[0]*4
            self.pending.append([self.version,self.hour,call['ts']]+dims+metrics+[call['ts'],call['ts']])
            key=tuple(dims);cell=self.cells.setdefault(key,[0.0]*len(METRICS)+[call['ts'],call['ts']])
            for i,v in enumerate(metrics):cell[i]+=v
            cell[-2]=min(cell[-2],call['ts']);cell[-1]=max(cell[-1],call['ts'])
        if len(self.pending)>=256:self.flush()
    def flush(self):
        if self.pending:
            with self.writer:self.writer.executemany('INSERT INTO project_rollup_evidence_fine VALUES('+','.join('?' for _ in FIELDS)+')',self.pending)
            self.pending.clear()
    def finish(self):
        self.flush()
        with self.writer:self.writer.executemany('INSERT INTO project_rollup_evidence_hour VALUES('+','.join('?' for _ in FIELDS)+')',([self.version,self.hour,self.hour*3600]+list(key)+values for key,values in self.cells.items()))


def valuation(row,dates=(),sources=()):
    return dict(priced_calls=row['priced_equivalents'],unpriced_calls=row['unpriced_equivalents'],known_value_low_usd=row['known_value_low_usd'],known_value_high_usd=row['known_value_high_usd'],rate_card_dates=set(dates),rate_sources=set(sources))


def add_value(target,value):
    for k in ('priced_calls','unpriced_calls','known_value_low_usd','known_value_high_usd'):target[k]+=value[k]
    for k in ('rate_card_dates','rate_sources'):target[k].update(value[k])


def report(db,view,selection):
    from . import project_rollups
    s=project_rollups.status(db)
    if not s['enabled']:return None
    state=project_rollups.state(db)
    if not s['ready'] or not state.get('evidence_metadata'):return dict(available=False,reason='Shared reporting facts are warming in the background',rollup_status=s)
    metadata=json.loads(state['evidence_metadata'])
    # Reuse the exact SQL selection and edge-hour planner used by Costs.
    rows=project_rollups.query(db,selection,'evidence',DIMS,METRICS,parent_only=True)
    catalog=json.loads(state['catalog']);key=selection.values['project_key']
    if key and key!='@unassigned' and key not in {p['project_key'] for p in catalog}:raise ValueError('Unknown human project')
    cost_meta=project_rollups.dataset_metadata(db)
    dates=cost_meta['rate_card_dates'];sources=cost_meta['rate_sources'];value=rates.empty()
    prs={};commits={};unknown={};commit_unknown={};missing={};accounts={};total=0;reported=0;reported_usd=0
    titles={(r['machine'],r['tool'],r['session']):r['title'] for r in metadata['session_titles']}
    pr_meta={p['url']:p for p in metadata['prs']};commit_meta={(c['repo'],c['sha']):c for c in metadata['commits']}
    periods=metadata['subscription_periods']
    period_index={json.dumps([p[k] for k in ('account_key','subscription_key','period_start','period_end')],separators=(',',':')):p for p in periods}
    for p in periods:p.update(calls=0,valuation=rates.empty())
    def bucket(e):
        url=e['pr']
        if url not in prs:
            p=pr_meta.get(url,{})
            prs[url]=dict(url=url,repo=p.get('repo',''),number=p.get('number',0),title=p.get('title'),state=p.get('state','unknown'),metadata_asof=p.get('ts'),calls=0,pre_pr_calls=0,fresh_in=0,cache_read=0,cache_write=0,out=0,reasoning=0,total_tokens=0,valuation=rates.empty(),session_refs={},tasks=set(),evidence=set(),evidence_refs=[],observed_inputs=0,observed_input_events=0,known_human_inputs=0,unknown_origin_inputs=0,input_sessions=set(),added_code=p.get('added_code') if p.get('state')=='merged' else None,removed_code=p.get('removed_code') if p.get('state')=='merged' else None,cost_usd=None,cost_provenance='unknown',tokens_per_dollar=None)
        return prs[url]
    if view=='work':
        for url in pr_meta:bucket(dict(pr=url))
    tools=set()
    for values in rows:
        r=dict(zip(DIMS+METRICS+['first_ts','last_ts'],values));e=json.loads(r['evidence']);w=r['request_equivalents'];v=valuation(r,[e['rate_asof']] if e.get('rate_asof') else [],[e['rate_source']] if e.get('rate_source') else []);sk=(r['machine'],r['tool'],r['session']);tools.add(r['tool'])
        add_value(value,v);total+=w;reported+=r['reported_equivalents'];reported_usd+=r['known_reported_usd']
        if view=='accounts':
            if e['input']:continue
            a=e['account'];ak=a['account_key'] if a else '@unknown'
            ar=accounts.setdefault(ak,dict(account_key=ak,provider=a.get('provider','') if a else '',calls=0,tokens=0,valuation=rates.empty(),reported_call_count=0,reported_call_value=0,reason='No unambiguous session-time account evidence' if not a else 'Observed account; billing assignment requires independent evidence'))
            ar['calls']+=w;ar['tokens']+=r['tokens'];ar['reported_call_count']+=r['reported_equivalents'];ar['reported_call_value']+=r['known_reported_usd'];add_value(ar['valuation'],v)
            if e['period'] in period_index:
                p=period_index[e['period']];p['calls']+=w;add_value(p['valuation'],v)
            continue
        if e['pr']:
            p=bucket(e);p['tasks'].update(e['tasks']);p['evidence'].add(e['why'])
            if e['input']:
                p['observed_inputs']+=r['input_equivalents'];p['observed_input_events']+=r['input_events'];p['known_human_inputs']+=r['human_inputs'];p['unknown_origin_inputs']+=r['unknown_inputs'];p['input_sessions'].add(sk)
            else:
                p['calls']+=w;p['total_tokens']+=r['tokens'];p['pre_pr_calls']+=w if e['pre_pr'] else 0;add_value(p['valuation'],v);p['session_refs'][sk]=dict(machine=sk[0],tool=sk[1],session=sk[2],title=titles.get(sk))
                for k in ('fresh_in','cache_read','cache_write','out','reasoning'):p[k]+=r[k]
        elif not e['input']:
            unknown[e['why']]=unknown.get(e['why'],0)+w
            m=missing.setdefault(sk,dict(machine=sk[0],tool=sk[1],session=sk[2],calls=0,reasons=set()));m['calls']+=w;m['reasons'].add(e['why'])
        ck=tuple(e['commit']) if e['commit'] else None
        if not e['input']:
            if ck in commit_meta:
                c=commits.setdefault(ck,dict(commit_meta[ck],calls=0,total_tokens=0,out=0,sessions=set(),linked_sessions=set(),valuation=rates.empty(),actual_billed_usd=None));c['calls']+=w;c['total_tokens']+=r['tokens'];c['out']+=r['out'];c['sessions'].add((sk[1],sk[2]));c['linked_sessions'].add((sk[1],sk[2]));add_value(c['valuation'],v)
            else:
                why='missing_commit_metadata' if ck else e['commit_why'];commit_unknown[why]=commit_unknown.get(why,0)+w
    common=dict(available=True,generated=int(s['updated_at']),definition_version='erg.incremental-evidence.v1',cost_provenance='unknown',valuation=rates.finish(value),project_catalog=catalog,folder_catalog=cost_meta['folder_catalog'],project_revision=s['project_revision'],observed_tools=sorted(tools),report_scope=selection.values,rollup_status=s,reported_call_count=reported,reported_call_value=reported_usd,total_calls=total)
    if view=='accounts':
        for a in accounts.values():a['valuation']=rates.finish(a['valuation']);a['actual_billed_usd']=None
        for p in periods:
            p['valuation']=rates.finish(p['valuation']);spend=p['subscription_spend_usd'];v=p['valuation']
            for bound in ('low','high'):p['known_value_per_subscription_dollar_'+bound]=v['known_value_'+bound+'_usd']/spend if spend is not None and spend>0 and v['priced_calls'] else None
            p['ratio_coverage']='selected_usage_over_full_period_charge' if v['priced_calls'] else 'unknown'
        return dict(common,account_registry=metadata['account_registry'],account_usage=list(accounts.values()),subscription_periods=periods,quota_latest=metadata['quota_latest'],reset_history=metadata.get('reset_history',[]),quota_counts=metadata['quota_counts'],billing_scope='Token usage follows selected filters. Charges cover whole billing periods; quota/identity evidence is account-wide. Charges are not allocated project expense.',registry_scope='Latest observed identity metadata; not billing proof')
    for p in prs.values():
        p['evidence_refs']=[f for f in metadata.get('facts_by_url',{}).get(p['url'],[]) if (f['machine'],f['tool'],f['session']) in p['session_refs']][:100]
        p['sessions']=len(p['session_refs']);p['session_refs']=list(p['session_refs'].values());p['tasks']=sorted(p['tasks']);p['evidence']=sorted(p['evidence']);p['input_sessions_observed']=len(p.pop('input_sessions'));p['observed_inputs']=p['observed_inputs'] if p['observed_input_events'] else None;p['human_inputs']=p['known_human_inputs'] if p['observed_input_events'] and not p['unknown_origin_inputs'] else None;p['input_coverage']='partial' if p['input_sessions_observed']<p['sessions'] or p['unknown_origin_inputs'] else 'observed_only' if p['observed_input_events'] else 'unknown';p['accepted_code_per_million_tokens']=p['added_code']*1e6/p['total_tokens'] if p['added_code'] is not None and p['total_tokens'] else None;p['valuation']=rates.finish(p['valuation']);p['usage_status']='partial' if p['calls'] else 'unknown';p['attribution']='allocated_observations_only' if p['calls'] else 'unknown'
    for c in commits.values():
        for k in ('sessions','linked_sessions'):c[k]=sorted(c[k])
        c['valuation']=rates.finish(c['valuation'])
    for m in missing.values():m['reasons']=sorted(m['reasons'])
    return dict(common,prs=sorted(prs.values(),key=lambda p:-p['calls']),commits=sorted(commits.values(),key=lambda c:-c['calls']),commit_attributed_calls=sum(c['calls'] for c in commits.values()),commit_unattributed_reasons=commit_unknown,attributed_calls=total-sum(unknown.values()),unattributed_calls=sum(unknown.values()),unattributed_reasons=unknown,pre_pr_calls=sum(p['pre_pr_calls'] for p in prs.values()),unattributed_sessions=sorted(missing.values(),key=lambda m:-m['calls'])[:100],task_contexts=[],automatic_evidence_count=metadata['automatic_evidence_count'],unclassified_codex_role_records=None,input_count_basis='Allocated input equivalents; physical input events are separate',definitions='Accepted = merged. Whole-PR diff lines are independent of selected usage; no AI authorship is inferred.',limitations=['Missing or conflicting work/account evidence remains unknown. Subscription fees and actual billed spend are separate from reference value.'])
