"""Shared account valuation and subscription rules, independent of work attribution."""
import calendar
import datetime
import time
from . import accounting,rates


def periods(db,account_root,identities=None):
    charges_by_key={}
    for charge in accounting.rows(db,'subscription_charge_events'):
        key=(charge['provider'],account_root(charge['account_key']),charge.get('source_charge_key') or charge['uid'])
        old=charges_by_key.get(key)
        if old is None or charge['ts']>old['ts']:charges_by_key[key]=charge
        elif old['ts']==charge['ts'] and any(old.get(k)!=charge.get(k) for k in ('subscription_key','period_start','period_end','usd','amount','currency')):old['usd']=None;old['conflict']=True
    charges=list(charges_by_key.values());periods={}
    for charge in charges:
        key=(account_root(charge['account_key']),charge['subscription_key'],charge['period_start'],charge['period_end'])
        if key not in periods:periods[key]=dict(account_key=key[0],subscription_key=key[1],period_start=key[2],period_end=key[3],subscription_spend_usd=0.0,unknown_charges=0,currency_amounts={},charge_provenance=set(),valuation=rates.empty(),calls=0,base_subscription_usd=0.,purchased_reset_usd=0.,overage_usd=0.,charge_events=[])
        if charge['usd'] is None:periods[key]['unknown_charges']+=1
        else:
            periods[key]['subscription_spend_usd']+=charge['usd']
            field={'purchased_reset':'purchased_reset_usd','overage':'overage_usd'}.get(charge.get('charge_kind'),'base_subscription_usd')
            periods[key][field]+=charge['usd']
        periods[key]['charge_events'].append({k:charge.get(k) for k in ('source_charge_key','charge_kind','reset_at','usd','provenance')})
        periods[key]['charge_provenance'].add(charge['provenance'])
        if not charge.get('conflict') and charge.get('amount') is not None:periods[key]['currency_amounts'][charge['currency']]=periods[key]['currency_amounts'].get(charge['currency'],0)+charge['amount']
    # Plan-price observations accrue from their actual observation time. They
    # are estimates, never invoice rows or invented billing anniversary dates.
    plans={}
    for i in accounting.rows(db,'identity_events') if identities is None else identities:
        amount=i.get('monthly_usd')
        if accounting.number(amount) is None or amount<0 or i.get('price_currency')!='USD' or i.get('billing_interval')!='month':continue
        plans.setdefault(account_root(i['account_key']),[]).append(i)
    for account,observations in plans.items():
        ordered=sorted(observations,key=lambda i:(i['ts'],i['uid']));observations=[]
        for i in ordered:
            if observations and all(i.get(k)==observations[-1].get(k) for k in ('monthly_usd','plan','subscription_key','billing_interval','price_currency')):continue
            observations.append(i)
        for n,i in enumerate(observations):
            end=observations[n+1]['ts'] if n+1<len(observations) else 2**62
            if end<=i['ts']:continue
            key=(account,i.get('subscription_key') or 'observed-plan:'+account,i['ts'],end)
            periods[key]=dict(account_key=account,subscription_key=key[1],period_start=key[2],period_end=end,subscription_spend_usd=None,unknown_charges=0,currency_amounts={},charge_provenance={i.get('price_provenance') or i['provenance']},valuation=rates.empty(),calls=0,base_subscription_usd=None,purchased_reset_usd=0.,overage_usd=0.,charge_events=[],expense_basis='observed_monthly_plan_price_accrual',monthly_usd=i['monthly_usd'],observed_plan=i['plan'])
    return periods


def accrued(monthly,start,end):
    """Prorate by actual UTC calendar-month length without a billing anchor."""
    amount=0.
    while start<end:
        d=datetime.datetime.fromtimestamp(start,datetime.timezone.utc)
        next_month=datetime.datetime(d.year+int(d.month==12),d.month%12+1,1,tzinfo=datetime.timezone.utc).timestamp()
        stop=min(end,next_month);amount+=monthly*(stop-start)/(calendar.monthrange(d.year,d.month)[1]*86400);start=stop
    return amount


def allocate(periods,ts,valuation,account):
    if account:
        eligible=[(k,p) for k,p in periods.items() if k[0]==account['account_key'] and (p.get('expense_basis') or not account['subscription_key'] or k[1]==account['subscription_key']) and k[2]<=ts<k[3]]
        # Two subscriptions/overlapping periods are not a unique billing assignment.
        recorded=[v for v in eligible if not v[1].get('expense_basis')]
        if recorded:eligible=recorded
        if len(eligible)==1 and account['billing_evidence']:
            p=eligible[0][1];p['calls']+=1;rates.add(p['valuation'],valuation)


def finish(db,account_root,identities,global_value,periods,quota=None):
    for period in periods.values():
        period['valuation']=rates.finish(period['valuation']);v=period['valuation'];spend=period['subscription_spend_usd']
        period['charge_provenance']=sorted(period['charge_provenance'])
        if period['unknown_charges']:period['subscription_spend_usd']=None;spend=0
        period['known_value_per_subscription_dollar_low']=v['known_value_low_usd']/spend if spend is not None and spend>0 and v['priced_calls'] else None
        period['known_value_per_subscription_dollar_high']=v['known_value_high_usd']/spend if spend is not None and spend>0 and v['priced_calls'] else None
        period['ratio_coverage']='observed_lower_bound' if v['priced_calls'] else 'unknown'
    summary=quota if quota is not None else quota_summary(db,account_root)
    return dict(definition_version='erg.work-effectiveness.v3',cost_provenance='unknown',generated=int(time.time()),
                valuation=rates.finish(global_value),subscription_periods=list(periods.values()),
                accounts=[dict(i,account_key=account_root(i['account_key'])) for i in identities],**summary)


def quota_summary(db,account_root):
    from . import quota_projection
    summary=quota_projection.summary(db,account_root)
    return raw_quota_summary(db,account_root) if summary is None else summary

def raw_quota_summary(db,account_root):
    quotas=accounting.rows(db,'quota_events');latest={}
    history={}
    for q in quotas:
        if q.get('occurred_at') and q['window']=='reset_history':
            key=(q['provider'],account_root(q['account_key']),q['uid'])
            history.setdefault(key,q)
    quotas=[q for q in quotas if q['window']!='reset_history']+list(history.values())
    for q in quotas:
        key=(account_root(q['account_key']) if q['account_key'] else q['tool']+':'+q['session'],q['limit_id'],q['window'])
        if key not in latest or q['ts']>latest[key]['ts']:latest[key]=q
    return dict(quota_latest=sorted((q for q in latest.values() if q['window']!='reset_history'),key=lambda r:-r['ts'])[:200],
                reset_history=sorted((q for q in quotas if q.get('occurred_at') and q['window']=='reset_history'),key=lambda r:-r['occurred_at'])[:200],
                quota_counts={event:sum(q['event']==event for q in quotas) for event in ('snapshot','window_exhausted','limit_reached','reset_observed','reset_credit_used','reset_credit_granted','reset_credit_purchased')})


def report(db):
    """Fold canonical calls with payer routes, without session, prompt or Git work."""
    from . import session_usage
    card=rates.load(db);resolve,root,identities=accounting.resolver(db)
    subscription_periods=periods(db,root,identities);value=rates.empty()
    cols=session_usage.CALL_COLS
    import sqlite3
    from . import report_sqlite
    call_sql=report_sqlite.calls_select(cols,db=db) if isinstance(db,sqlite3.Connection) else 'SELECT '+','.join(cols)+' FROM calls'
    for values in db.execute(call_sql):
        call=dict(zip(cols,values));valuation=rates.value(call,card)
        rates.add(value,valuation)
        # With no charge periods there is no billing allocation to resolve.
        if subscription_periods:
            allocate(subscription_periods,call['ts'],valuation,resolve(call['tool'],call['session'],call['ts'],request_uid=call.get('uid','')))
    return finish(db,root,identities,value,subscription_periods)
