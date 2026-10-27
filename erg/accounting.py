"""Observed identity, entitlement and quota history; charges are independent ledger data."""
import bisect
from functools import lru_cache
import hashlib
import json
import math
import time

EVENTS={
 'identity':'uid ts tool session installation provider account_key account_id_hash email_hash email_hint organization_hash plan subscription_key provenance scope monthly_usd price_currency billing_interval price_provenance billing_status',
 'session_route':'uid ts tool session installation provider endpoint_host endpoint_path transport workspace_key region profile account_key account_provider billing_mode scope request_uid provenance',
 'quota':'uid ts tool session installation provider account_key subscription_key plan limit_id window used_percent window_minutes resets_at reset_credits_available event provenance occurred_at',
 'session_cost':'uid ts tool session usd complete provenance',
 'subscription_charge':'uid ts provider account_key subscription_key period_start period_end usd amount currency source_charge_key provenance charge_kind reset_at',
 'account_binding':'uid ts tool session installation start_ts end_ts provider account_key subscription_key plan provenance',
}
TABLES={k:k+'_events' for k in EVENTS}
NUMERIC=set('occurred_at monthly_usd reset_at amount ts reset_credits_available used_percent window_minutes resets_at usd complete period_start period_end start_ts end_ts'.split())


def digest(*parts):return hashlib.sha256(json.dumps(parts,separators=(',',':')).encode()).hexdigest()

def number(value):return value if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) else None

def text(value):return value.strip()[:256] if isinstance(value,str) else ''


def identity_fields(value,provider):
    account=text(value.get('account_id') or value.get('accountId') or value.get('accountUuid'))
    email=text(value.get('email') or value.get('emailAddress') or value.get('user_email')).lower()
    email=email if '@' in email and ' ' not in email else ''
    ah=digest(provider,account) if account else '';eh=digest(provider,email) if email else ''
    return dict(account_key=ah or eh,account_id_hash=ah,email_hash=eh,
                email_hint=(email[:1]+'***@'+email.split('@',1)[1]) if email else '',
                organization_hash=digest(provider,text(value.get('organization_id') or value.get('organizationUuid') or value.get('orgId'))) if value.get('organization_id') or value.get('organizationUuid') or value.get('orgId') else '',
                plan=text(value.get('plan_type') or value.get('planType') or value.get('plan') or value.get('subscriptionType')),
                subscription_key=digest(provider,text(value.get('subscription_id'))) if value.get('subscription_id') else '')


def observe(tool,d,p,state,ts,path):
    """Only allowlisted metadata in native logs; never inspect auth/token files."""
    if tool=='codex' and ts<state.get('started',0):return []
    provider=text(p.get('model_provider') or p.get('provider') or state.get('_provider') or tool)
    state['_provider']=provider
    if '_installation' not in state:state['_installation']=digest(tool,path.split('/sessions/',1)[0].split('/projects/',1)[0])
    installation=state['_installation']
    session=state.get('session') or d.get('sessionId') or p.get('session_id') or ''
    base=dict(ts=ts,tool=tool,session=session,installation=installation,provider=provider)
    rows=[]
    values=[p]
    for name in ('account','account_info','metadata','auth','oauthAccount'):
        if isinstance(p.get(name),dict):values.append(p[name])
    if tool=='claude' and isinstance(d.get('rateLimitInfo'),dict):values.append(d['rateLimitInfo'])
    for value in values:
        fields=identity_fields(value,provider)
        if fields['account_key']:
            state['_account_key']=fields['account_key']
            if fields['subscription_key']:state['_subscription_key']=fields['subscription_key']
            if fields['plan']:state['_plan']=fields['plan']
            row=dict(base,t='identity',**fields,provenance='native_metadata');signature=digest(fields)
            if state.get('_identity_signature')!=signature:
                row['uid']=digest(tool,session,ts,fields);rows.append(row);state['_identity_signature']=signature
    rate=p.get('rate_limits') or d.get('rateLimitInfo')
    if isinstance(rate,dict):
        plan=text(rate.get('plan_type') or state.get('_plan'));state['_plan']=plan
        limit=text(rate.get('limit_id') or rate.get('rateLimitType') or 'unknown')
        # A plan is an observed entitlement label, not a unique account or a charge.
        for name in ('primary','secondary'):
            window=rate.get(name)
            if not isinstance(window,dict):continue
            used=number(window.get('used_percent'));minutes=number(window.get('window_minutes'));reset=number(window.get('resets_at'))
            if used is None or used<0:continue
            row=dict(base,t='quota',account_key=state.get('_account_key',''),subscription_key=state.get('_subscription_key',''),plan=plan,limit_id=limit,window=name,used_percent=used,window_minutes=minutes,resets_at=reset,event='snapshot',provenance='native_rate_limits')
            key=limit+':'+name;signature=digest({k:v for k,v in row.items() if k not in ('ts','session')})
            previous=state.setdefault('_quota',{}).get(key)
            if previous and reset and previous.get('reset') and reset>previous['reset'] and used<previous['used']:row['event']='reset_observed'
            if used>=100:row['event']='window_exhausted'
            if not previous or previous['signature']!=signature:
                row['uid']=digest(tool,session,ts,signature);rows.append(row)
                state['_quota'][key]=dict(signature=signature,reset=reset,used=used)
        if rate.get('status') in ('allowed','allowed_warning','rejected'):
            row=dict(base,t='quota',uid=digest(tool,session,ts,rate.get('status'),rate.get('resetsAt')),account_key=state.get('_account_key',''),subscription_key=state.get('_subscription_key',''),plan=plan,limit_id=text(rate.get('rateLimitType') or 'unknown'),window=text(rate.get('rateLimitType') or 'unknown'),used_percent=number(rate.get('usedPercent')),window_minutes=None,resets_at=number(rate.get('resetsAt')),event='limit_reached' if rate['status']=='rejected' else 'snapshot',provenance='native_rate_limits');rows.append(row)
        reached=rate.get('rate_limit_reached_type') or rate.get('spend_control_reached')
        if reached:
            row=dict(base,t='quota',uid=digest(tool,session,ts,'limit_reached'),account_key=state.get('_account_key',''),subscription_key=state.get('_subscription_key',''),plan=plan,limit_id=limit,window='unknown',used_percent=None,window_minutes=None,resets_at=None,event='limit_reached',provenance='native_rate_limits');rows.append(row)
    code=p.get('code') or (p.get('error') or {}).get('code') if isinstance(p.get('error'),dict) else p.get('code')
    info=p.get('codex_error_info')
    if isinstance(info,dict):info=next(iter(info),'')
    if isinstance(info,str) and info.lower().replace('_','') in ('usagelimitexceeded','ratelimitexceeded','insufficientquota'):code='usage_limit_reached'
    if tool=='codex' and d.get('type')=='event_msg' and p.get('type')=='error':
        message=p.get('message') or ''
        if isinstance(message,str) and message.startswith(("You've hit your usage limit",'You’ve hit your usage limit','Rate limit reached')):code='usage_limit_reached'

    if p.get('type') in ('usage_limit_reached','rate_limit_reached','rate_limit_exceeded') or code in ('usage_limit_reached','rate_limit_exceeded','insufficient_quota'):
        rows.append(dict(base,t='quota',uid=digest(tool,session,ts,'error'),account_key=state.get('_account_key',''),subscription_key=state.get('_subscription_key',''),plan=state.get('_plan',''),limit_id='unknown',window='unknown',event='limit_reached',provenance='native_error_code'))
    return rows


def reported_cost(d,mtime):
    amount=number(d.get('totalCostUSD'))
    if d.get('type')=='cost-state' and amount is not None and amount>=0 and d.get('sessionId'):
        return dict(t='session_cost',uid='claude:'+d['sessionId'],ts=int(mtime),tool='claude',session=d['sessionId'],usd=amount,complete=int(not d.get('hasUnknownModelCost',True)),provenance='agent_reported_estimate')


def rows(db,table):
    cur=db.execute('SELECT * FROM '+table);keys=[d[0] for d in cur.description]
    result={}
    for values in cur:
        row=dict(zip(keys,values));old=result.get(row['uid'])
        if old is None or row['ts']>old['ts']:result[row['uid']]=row
        elif old['ts']==row['ts'] and table=='subscription_charge_events':
            fields=('provider','account_key','subscription_key','period_start','period_end','usd','amount','currency')
            if any(old.get(k)!=row.get(k) for k in fields):old['usd']=None;old['conflict']=True
    return list(result.values())


def resolver(db):
    from . import routes
    route_at=routes.route_resolver(db)
    identities=rows(db,'identity_events');parent={}
    def root(k):
        while parent.get(k,k)!=k:k=parent[k]
        return k
    # Email alone cannot merge distinct provider accounts sharing an address.
    email_accounts={}
    for r in identities:
        if r['email_hash'] and r['account_id_hash']:email_accounts.setdefault(r['email_hash'],set()).add(r['account_id_hash'])
    ambiguous_emails={e for e,ids in email_accounts.items() if len(ids)>1}
    # Exact identifiers co-observed on one native record can connect aliases.
    for r in identities:
        keys=[k for k in (r['account_key'],r['account_id_hash'],r['email_hash']) if k and k not in ambiguous_emails]
        if keys:
            target=root(keys[0])
            for k in keys:parent[root(k)]=target
    bindings=rows(db,'account_binding_events');by_session={}
    for r in identities:by_session.setdefault((r['tool'],r['session']),[]).append(r)
    for values in by_session.values():values.sort(key=lambda r:r['ts'])
    def select(tool,session,ts,installation=''):
        choices=[r for r in bindings if r['tool']==tool and (r['session']==session or not r['session'] and installation and r['installation']==installation) and (not r['start_ts'] or ts>=r['start_ts']) and (not r['end_ts'] or ts<r['end_ts'])]
        if not choices:
            records=by_session.get((tool,session),[])
            values=[r for r in records if r['ts']<=ts and r.get('scope')!='session_owner']
            if values:
                stamp=values[-1]['ts'];choices=[r for r in values if r['ts']==stamp]
            else:choices=[r for r in records if r.get('scope')=='session_owner']
        unique={(root(r['account_key']),r['subscription_key'] or '',r['provider']) for r in choices if r['account_key'] and r['account_key'] not in ambiguous_emails}
        if len(unique)!=1:return None
        key,sub,provider=next(iter(unique))
        return dict(account_key=key,subscription_key=sub,provider=provider,plan=choices[-1].get('plan') or '',provenance=choices[-1]['provenance'],billing_evidence=choices[-1]['provenance'] not in ('codex_creator_account','claude_session_owner','profile_snapshot','codex_account_read','codex_shared_account_read','codex_billing_api'))
    @lru_cache(maxsize=4096)
    def boundaries(tool,session,installation):
        stamps={r['ts'] for r in by_session.get((tool,session),[]) if r.get('scope')!='session_owner'}
        for row in bindings:
            if row['tool']==tool and (row['session']==session or not row['session'] and installation and row['installation']==installation):
                stamps.update(row[field] for field in ('start_ts','end_ts') if row[field])
        return sorted(stamps)
    @lru_cache(maxsize=4096)
    def cached(tool,session,installation,interval):
        stamps=boundaries(tool,session,installation)
        return select(tool,session,stamps[interval-1] if interval else float('-inf'),installation)
    def resolve(tool,session,ts,installation='',request_uid=''):
        # Explicit bindings remain authoritative. Request principals must never
        # enter the interval cache: separate calls can share the same timestamp.
        bound=[r for r in bindings if r['tool']==tool and (r['session']==session or not r['session'] and installation and r['installation']==installation) and (not r['start_ts'] or ts>=r['start_ts']) and (not r['end_ts'] or ts<r['end_ts'])]
        if not bound:
            route,error=route_at(tool,session,ts,request_uid)
            if route and route.get('account_key') and route.get('account_provider'):
                key=root(route['account_key']);provider=route['account_provider']
                observed=[r for r in identities if root(r['account_key'])==key and r['provider']==provider and r['ts']<=ts]
                latest=max(observed,key=lambda r:r['ts']) if observed else {}
                return dict(account_key=key,subscription_key=latest.get('subscription_key') or '',provider=provider,plan=latest.get('plan') or '',provenance='session_route_principal',billing_evidence=True)
        interval=bisect.bisect_right(boundaries(tool,session,installation),ts)
        value=cached(tool,session,installation,interval)
        # Each caller owns its result; a mutation must not alter later billing.
        if value is not None:
            value=dict(value)
            if not bound:
                # A conflicting route or a different service cannot borrow the
                # owner's subscription, even when an earlier auth hook matched.
                if error=='conflicting_session_routes' or route and (route.get('billing_mode') in ('local','gateway','api') or route.get('account_provider') and route['account_provider']!=value['provider'] or route.get('provider') and route['provider'] not in (value['provider'],'anthropic' if value['provider']=='anthropic' else 'openai')):
                    value['billing_evidence']=False
        return value
    resolve.cache_info=cached.cache_info
    return resolve,root,identities


def import_metadata(db,data,machine):
    from . import store
    if not isinstance(data,dict) or data.get('version')!=1:raise ValueError('expected version 1 metadata object')
    out=[];now=int(time.time())
    for kind,key in [('identity','identities'),('account_binding','bindings'),('subscription_charge','charges')]:
        for item in data.get(key,[]):
            if not isinstance(item,dict):raise ValueError('expected metadata rows')
            provider=text(item.get('provider'));fields=identity_fields(item,provider)
            if not fields['account_key'] and item.get('account_key'):
                import re
                key=item['account_key']
                if not isinstance(key,str) or not re.fullmatch('[a-f0-9]{64}',key):raise ValueError('invalid account key')
                known=db.execute('SELECT 1 FROM identity_events WHERE account_key=? AND provider=? UNION SELECT 1 FROM session_route_events WHERE account_key=? AND account_provider=? LIMIT 1',(key,provider,key,provider)).fetchone()
                if not known:raise ValueError('account key is not observed for this provider')
                fields['account_key']=key
            if not provider or not fields['account_key']:raise ValueError('provider plus observed account key, account_id or email required')
            base=dict(t=kind,ts=now,provider=provider,account_key=fields['account_key'],provenance='operator_metadata')
            if kind=='identity':
                base.update(fields,tool=text(item.get('tool')),session=text(item.get('session')),installation=text(item.get('installation')))
                if 'monthly_usd' in item:
                    price=number(item['monthly_usd'])
                    if price is None or price<0:raise ValueError('invalid monthly plan price')
                    observed=item.get('observed_at',now)
                    if isinstance(observed,bool) or not isinstance(observed,int) or not 0<observed<=now:raise ValueError('invalid plan observation time')
                    base.update(ts=observed,scope='account_plan',monthly_usd=price,price_currency='USD',billing_interval='month',price_provenance=text(item.get('price_provenance') or 'operator_observed_plan_price'))
            if kind=='account_binding':
                start=item.get('start_ts');end=item.get('end_ts')
                if not isinstance(start,int) or not isinstance(end,int) or end<=start:raise ValueError('binding requires explicit nonempty start_ts/end_ts interval')
                if not item.get('session') and not item.get('installation'):raise ValueError('binding requires session or installation')
                base.update(tool=text(item.get('tool')),session=text(item.get('session')),installation=text(item.get('installation')),start_ts=start,end_ts=end,subscription_key=fields['subscription_key'],plan=fields['plan'])
            if kind=='subscription_charge':
                currency=text(item.get('currency') or 'USD').upper()
                amount=number(item.get('amount',item.get('usd')));start=item.get('period_start');end=item.get('period_end')
                if amount is None or amount<0 or isinstance(start,bool) or isinstance(end,bool) or not isinstance(start,int) or not isinstance(end,int) or end<=start or not item.get('charge_id') or not fields['subscription_key']:raise ValueError('charge requires id, subscription_id, USD amount and explicit period bounds')
                kind_value=item.get('charge_kind','subscription');reset=item.get('reset_at')
                if kind_value not in ('subscription','purchased_reset','overage'):raise ValueError('invalid charge kind')
                if reset is not None and (isinstance(reset,bool) or not isinstance(reset,int) or not start<=reset<end):raise ValueError('reset must be inside billing period')
                base.update(charge_kind=kind_value,reset_at=reset)
                base.update(subscription_key=fields['subscription_key'],period_start=start,period_end=end,usd=amount if currency=='USD' else None,amount=amount,currency=currency,source_charge_key=digest(provider,text(item['charge_id'])))
            base['uid']=digest(kind,provider,fields['account_key'],item.get('charge_id') or {k:v for k,v in base.items() if k!='ts'})
            if kind=='identity' and 'monthly_usd' in base:base['uid']=digest(kind,provider,fields['account_key'],base['ts'],{k:v for k,v in base.items() if k not in ('uid','ts')})
            out.append(base)
    store.ingest(db,map(json.dumps,out),machine=machine)
    return len(out)


def capture_metadata(db):
    """Discover non-secret account profile metadata; never read credential files.

    Current profile snapshots populate the registry, not historical session links.
    """
    from pathlib import Path
    from . import collect,store
    db.execute('CREATE TABLE IF NOT EXISTS account_metadata_offsets(path TEXT PRIMARY KEY,mtime REAL)')
    paths=[(Path(collect.HOME)/'.claude.json','claude','anthropic','oauthAccount')]
    for path,tool,provider,key in paths:
        try:
            stamp=path.stat().st_mtime
            old=db.execute('SELECT mtime FROM account_metadata_offsets WHERE path=?',(str(path),)).fetchone()
            if old and old[0]==stamp:continue
            data=json.loads(path.read_text());value=data.get(key) or {}
            if not isinstance(value,dict):continue
            fields=identity_fields(value,provider)
            if not fields['account_key']:continue
            fields['plan']=text(value.get('seatTier') or value.get('subscriptionType') or value.get('billingType'))
            row=dict(t='identity',uid=digest(str(path),stamp,fields),ts=int(time.time()),tool=tool,session='',installation=digest(tool,str(Path(collect.HOME)/'.claude')),provider=provider,provenance='profile_snapshot',**fields)
            with db:
                store.ingest(db,[json.dumps(row)],machine=collect.machine_name(),transaction=False)
                db.execute('INSERT OR REPLACE INTO account_metadata_offsets VALUES(?,?)',(str(path),stamp))
        except (OSError,ValueError):continue


def capture_codex_creators(db):
    """Use exact creator IDs from local thread metadata; creator != proven payer."""
    import sqlite3
    from pathlib import Path
    from . import collect,store
    homes={Path(collect.HOME)/'.codex'}
    for root in collect.native_roots('codex'):
        root=Path(root)
        if root.name in ('sessions','archived_sessions'):homes.add(root.parent)
    events=[]
    for home in homes:
        for path in home.glob('state*.sqlite'):
            try:
                source=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True)
                columns={r[1] for r in source.execute('PRAGMA table_info(threads)')}
                if not {'id','model_provider','created_at','creator_account_id'}.issubset(columns):source.close();continue
                for session,provider,ts,account in source.execute("SELECT id,model_provider,created_at,creator_account_id FROM threads WHERE creator_account_id IS NOT NULL AND creator_account_id<>''"):
                    # Codex creator IDs identify the app owner even for custom routes.
                    # Their namespace stays OpenAI; they never identify the route payer.
                    fields=identity_fields(dict(account_id=account),'openai')
                    events.append(dict(t='identity',uid=digest('creator',session,account),ts=int(ts),tool='codex',session=session,installation=digest('codex',str(home)),provider='openai',provenance='codex_creator_account',**fields))
                source.close()
            except (OSError,ValueError,sqlite3.Error):continue
    if events:store.ingest(db,map(json.dumps,events),machine=collect.machine_name())


def claude_session_start(payload,db):
    """SessionStart uses the caller's auth context; never infer from model names."""
    import os
    import subprocess
    from . import store,collect,routes
    if not isinstance(payload,dict) or not text(payload.get('session_id')):return 0
    session=text(payload['session_id']);ts=int(time.time())
    route=routes.context_route('claude',session,os.environ,ts)
    if route:store.ingest(db,[json.dumps(route)],machine=collect.machine_name())
    if os.environ.get('ANTHROPIC_BASE_URL'):return 0
    try:
        result=subprocess.run(['claude','auth','status','--json'],capture_output=True,text=True,timeout=5)
        if result.returncode:return 0
        status=json.loads(result.stdout)
        if not status.get('loggedIn') or status.get('authMethod') not in ('oauth','oauthToken','claudeAi','claude.ai'):return 0
        fields=identity_fields(status,'anthropic')
        if not fields['account_key']:return 0
        ts=int(time.time());session=text(payload['session_id'])
        row=dict(t='identity',uid=digest('claude-hook',session,ts,fields),ts=ts,tool='claude',session=session,provider='anthropic',installation='',provenance='claude_session_start',**fields)
        route=routes.event('claude',session,ts,dict(provider='anthropic',billing_mode='subscription'),'claude_session_auth')
        store.ingest(db,map(json.dumps,[row,route]),machine=collect.machine_name());return 1
    except (OSError,ValueError,subprocess.SubprocessError):return 0
