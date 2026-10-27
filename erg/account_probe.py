"""Bounded read-only Codex account RPC. No inference, login, reset or credential output."""
import json
import os
import selectors
import shutil
import subprocess
import time
from pathlib import Path
from . import accounting,collect


def read(home,timeout=12):
    binary=shutil.which('codex')
    if not binary:return None
    env=dict(os.environ,CODEX_HOME=str(home));proc=None
    try:
        proc=subprocess.Popen([binary,'app-server','--stdio','-c','analytics.enabled=false'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env)
        def send(message):proc.stdin.write((json.dumps(message)+'\n').encode());proc.stdin.flush()
        send(dict(id=0,method='initialize',params=dict(clientInfo=dict(name='erg_local_monitor',version='0.1.0'))))
        results={};failed=set();buffer=b'';deadline=time.monotonic()+timeout
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout,selectors.EVENT_READ)
            while time.monotonic()<deadline and (1 not in results or 2 not in results):
                if not selector.select(min(1,max(0,deadline-time.monotonic()))):continue
                chunk=os.read(proc.stdout.fileno(),65536)
                if not chunk:break
                buffer+=chunk
                if len(buffer)>2*1024*1024:raise RuntimeError('account response exceeded bound')
                while b'\n' in buffer:
                    raw,buffer=buffer.split(b'\n',1)
                    try:message=json.loads(raw)
                    except ValueError:continue
                    ident=message.get('id')
                    if ident==0:
                        if 'error' in message:return None
                        send(dict(method='initialized',params={}))
                        send(dict(id=1,method='account/read',params=dict(refreshToken=False)))
                        send(dict(id=2,method='account/rateLimits/read'))
                    elif ident in (1,2):
                        results[ident]=message.get('result') or {}
                        if 'error' in message:failed.add(ident)
                    elif ident is not None and message.get('method'):
                        # Do not service refresh, login or any other server request.
                        send(dict(id=ident,error=dict(code=-32601,message='Read-only monitor')))
        if not results:return None
        result=dict(account=results.get(1,{}).get('account'),limits=results.get(2,{}),_complete=1 in results and 2 in results and not failed)
        if result['_complete'] and (result['account'] or {}).get('type') in ('chatgpt','chatgptAuthTokens'):
            from . import codex_billing
            result['billing']=codex_billing.read(home,result['limits'].get('accountId') or '')
        return result
    except (OSError,subprocess.SubprocessError,ValueError):return None
    finally:
        if proc:
            if proc.stdin:proc.stdin.close()
            if proc.poll() is None:proc.terminate()
            try:proc.wait(timeout=2)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()


def events(home,result,ts):
    if not result:return []
    account=result.get('account') or {};kind=account.get('type')
    if kind not in ('chatgpt','chatgptAuthTokens'):return []
    limits=result.get('limits') or {}
    account=dict(account,accountId=limits.get('accountId') or account.get('accountId'))
    fields=accounting.identity_fields(account,'openai')
    if not fields['account_key']:return []
    installation=accounting.digest('codex',str(home));state=dict(session='',_installation=installation,_provider='openai',_account_key=fields['account_key'],_subscription_key=fields['subscription_key'],_plan=fields['plan'])
    rows=[dict(t='identity',uid=accounting.digest(str(home),ts,fields),ts=ts,tool='codex',session='',installation=installation,provider='openai',provenance='codex_account_read',**fields)]
    buckets=limits.get('rateLimitsByLimitId')
    if not isinstance(buckets,dict):buckets={'codex':limits.get('rateLimits')}
    for name,bucket in buckets.items():
        if not isinstance(bucket,dict):continue
        rate=dict(limit_id=bucket.get('limitId') or name,plan_type=bucket.get('planType') or fields['plan'],rate_limit_reached_type=bucket.get('rateLimitReachedType'))
        for window in ('primary','secondary'):
            w=bucket.get(window)
            if isinstance(w,dict):rate[window]=dict(used_percent=w.get('usedPercent'),window_minutes=w.get('windowDurationMins'),resets_at=w.get('resetsAt'))
        rows.extend(accounting.observe('codex',{},dict(provider='openai',rate_limits=rate),state,ts,str(home)))
    credits=limits.get('rateLimitResetCredits')
    if isinstance(credits,dict) and accounting.number(credits.get('availableCount')) is not None:
        rows.append(dict(t='quota',uid=accounting.digest('codex',installation,ts,'credits'),ts=ts,tool='codex',session='',installation=installation,provider='openai',account_key=fields['account_key'],subscription_key=fields['subscription_key'],plan=fields['plan'],limit_id='codex',window='reset_credits',reset_credits_available=credits['availableCount'],event='reset_credit_snapshot',provenance='codex_rate_limits_read'))
    for row in rows:
        if row['t']=='quota':row['provenance']='codex_rate_limits_read'
    from . import codex_billing
    rows.extend(codex_billing.events(home,result.get('billing')))
    return rows


def poll(db):
    from . import store,account_probe_context as context
    default=Path(os.environ.get('CODEX_HOME') or Path(collect.HOME)/'.codex')
    homes={default}
    for root in collect.native_roots('codex'):
        root=Path(root)
        if root.name in ('sessions','archived_sessions'):homes.add(root.parent)
    selected=[]
    for home in sorted(homes):
        if home!=default and not (home/'auth.json').exists():
            settings=context.settings(home)
            if not home.is_dir() or not (settings and settings['cli_auth_credentials_store'] in ('keyring','auto') or context.managed(home)):continue
        selected.append(home)
    initial={home:context.snapshot(home) for home in selected}
    cache={};captured=0;stats=dict(discovered_homes=len(homes),eligible_homes=len(selected),rpc_calls=0,shared_auth_snapshots=0,account_profiles_observed=0)
    try:
        for home in selected:
            before=initial[home];cached=cache.get(before[0]) if before else None
            shared=False;rows=[]
            if cached:
                source,result,stamp=cached
                if context.snapshot(home)==before and context.snapshot(source)==initial[source]:
                    rows=[r for r in events(home,result,stamp) if r['t']=='identity']
                    for row in rows:row.update(provenance='codex_shared_account_read',scope='shared_auth_snapshot')
                    # Preserve the timestamp of the actual RPC, and never turn
                    # alias installation membership into another quota event.
                    if context.snapshot(home)==before and context.snapshot(source)==initial[source]:shared=True
            if not shared:
                stats['rpc_calls']+=1;result=read(home);stamp=int(time.time());rows=events(home,result,stamp)
                if (before and result and result.get('_complete') is True and rows
                    and context.snapshot(home)==before):cache[before[0]]=(home,result,stamp)
            elif rows:stats['shared_auth_snapshots']+=1
            if rows:
                for row in rows:
                    if row.get('provenance')=='codex_reset_history_api':
                        previous=db.execute('SELECT ts FROM quota_events WHERE uid=? LIMIT 1',(row['uid'],)).fetchone()
                        if previous:row['ts']=previous[0]
                    if row['t']=='quota' and row.get('used_percent') is not None and row.get('resets_at'):
                        old=db.execute('SELECT used_percent,resets_at FROM quota_events WHERE account_key=? AND limit_id=? AND window=? ORDER BY ts DESC LIMIT 1',(row['account_key'],row['limit_id'],row['window'])).fetchone()
                        if old and old[1] and row['resets_at']>old[1] and row['used_percent']<old[0]:row['event']='reset_observed'
                store.ingest(db,map(json.dumps,rows),machine=collect.machine_name());captured+=1
        stats['account_profiles_observed']=captured
        return captured
    finally:poll.last_stats=dict(stats,account_profiles_observed=captured)
