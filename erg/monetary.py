"""One allocation of recorded period expense for all shared dashboard views.

The denominator is the whole published observed corpus, never the UI selection.
This is an allocation estimate, not a claim to know provider-invoiced request cost.
"""
import json
import time
from . import project_rollups,report_evidence,report_scope,account_reports


def empty():return dict(known_effective_cost_usd=0.,effective_cost_calls=0.,missing_effective_cost_calls=0.,rack_rate_low_usd=0.,rack_rate_high_usd=0.,rack_rate_missing_calls=0.,missing_reasons={})

def finish(row):
    row=dict(row);row['effective_cost_usd']=row['known_effective_cost_usd'] if not row['missing_effective_cost_calls'] else None
    row['effective_cost_basis']='recorded_period_expense_allocated_by_observed_rack_value_midpoint'
    row['coverage_scope']='Observed corpus only; incomplete account/machine coverage changes allocations. Current periods are provisional.'
    return row


def report(db,selection):
    s=project_rollups.status(db)
    if not s.get('ready'):return dict(available=False,reason='Reporting facts are warming')
    metadata=json.loads(project_rollups.state(db).get('evidence_metadata','{}'));periods=metadata.get('subscription_periods',[])
    index={json.dumps([p[k] for k in ('account_key','subscription_key','period_start','period_end')],separators=(',',':')):p for p in periods}
    denominators={}
    if periods:
        # Published heads pin both numerator and denominator. Parent facts are
        # unsplit, so project shares cannot inflate the whole-period denominator.
        sql="""SELECT json_extract(c.evidence,'$.period'),SUM((c.known_value_low_usd+c.known_value_high_usd)/2),SUM(c.unpriced_equivalents)
        FROM project_rollup_evidence_hour c JOIN project_rollup_heads h ON h.hour=c.hour AND h.version=c.version
        WHERE c.human='' AND json_extract(c.evidence,'$.input')=0 GROUP BY 1"""
        denominators={k:(v,u) for k,v,u in db.execute(sql) if k}
    buckets={};selected_project=selection.values['project_key']
    for values in project_rollups.query(db,selection,'evidence',report_evidence.DIMS,report_evidence.METRICS):
        r=dict(zip(report_evidence.DIMS+report_evidence.METRICS+['first_ts','last_ts'],values));e=json.loads(r['evidence'])
        if e['input']:continue
        parent=r['human']==selected_project if selected_project else r['human']==''
        account=e.get('account') or {};pid=e.get('period');period=index.get(pid);denom,missing=denominators.get(pid,(0,0));cost=None;reason=None
        if not account or not account.get('billing_evidence'):reason='billing_account_unverified'
        elif not period:reason='subscription_charge_missing_or_ambiguous'
        elif period['subscription_spend_usd'] is None and not period.get('expense_basis'):reason='charge_currency_or_amount_unresolved'
        elif missing or r['unpriced_equivalents']:reason='rack_rate_resolution_required'
        elif not denom:reason='billing_period_has_no_valued_usage'
        else:
            expense=account_reports.accrued(period['monthly_usd'],period['period_start'],min(time.time(),period['period_end'])) if period.get('expense_basis') else period['subscription_spend_usd']
            cost=expense*((r['known_value_low_usd']+r['known_value_high_usd'])/2)/denom
        keys=[]
        if parent:
            keys=[('tool',r['tool']),('machine',r['machine']),('model',r['model']),('kind',r['kind']),('total',''),('session',json.dumps([r[k] for k in ('machine','tool','session')])),('account',account.get('account_key') or '@unknown')]
            if e.get('pr'):keys.append(('pr',e['pr']))
            if e.get('commit'):keys.append(('commit',json.dumps(e['commit'])))
        if r['human']:keys.extend([('project',r['human']),('folder',json.dumps([r['human'],r['folder']]))])
        for key in keys:
            b=buckets.setdefault(key,empty());b['first_ts']=min(b.get('first_ts',r['first_ts']),r['first_ts']);b['last_ts']=max(b.get('last_ts',r['last_ts']),r['last_ts']);b['rack_rate_low_usd']+=r['known_value_low_usd'];b['rack_rate_high_usd']+=r['known_value_high_usd'];b['rack_rate_missing_calls']+=r['unpriced_equivalents']
            if cost is None:
                b['missing_effective_cost_calls']+=r['request_equivalents'];b['missing_reasons'][reason]=b['missing_reasons'].get(reason,0)+r['request_equivalents']
            else:b['known_effective_cost_usd']+=cost;b['effective_cost_calls']+=r['request_equivalents']
    return dict(available=True,totals=finish(buckets.get(('total',''),empty())),rows=[dict(kind=k,key=v,**finish(b)) for (k,v),b in buckets.items() if k!='total'],project_revision=s['project_revision'],subscription_periods=[dict(p,plan_accrued_estimate_usd=account_reports.accrued(p['monthly_usd'],p['period_start'],min(time.time(),p['period_end'])) if p.get('expense_basis') else None,observed_period_rack_midpoint_usd=denominators.get(k,(0,0))[0],observed_period_missing_rate_calls=denominators.get(k,(0,0))[1]) for k,p in index.items()])


def enrich(db,data,selection,view):
    if data.get('available') is False:return data
    money=report(db,selection);data['monetary']=money
    if not money.get('available'):return data
    index={(r['kind'],r['key']):r for r in money['rows']}
    def apply(row,kind,key):
        value=index.get((kind,key))
        if value:row.update({k:v for k,v in value.items() if k not in ('kind','key')})
    if view=='costs':
        data['totals'].update(money['totals'])
        for row in data['projects']:apply(row,'project',row['project_key'])
        for row in data['folders']:apply(row,'folder',json.dumps([row['project_key'],row['folder_key']]))
        for row in data['sessions']:apply(row,'session',json.dumps([row[k] for k in ('machine','tool','session')]))
    elif view=='work':
        for row in data.get('prs',[]):apply(row,'pr',row['url'])
        for row in data.get('commits',[]):apply(row,'commit',json.dumps([row['repo'],row['sha']]))
    elif view=='accounts':
        periods={(p['account_key'],p['subscription_key'],p['period_start'],p['period_end']):p for p in money['subscription_periods']}
        for period in data.get('subscription_periods',[]):
            extra=periods.get(tuple(period[k] for k in ('account_key','subscription_key','period_start','period_end')), {})
            period['plan_accrued_estimate_usd']=extra.get('plan_accrued_estimate_usd')
            expense=period['plan_accrued_estimate_usd'] if period.get('expense_basis') else period['subscription_spend_usd']
            for bound in ('low','high'):
                period['known_value_per_subscription_dollar_'+bound]=period['valuation']['known_value_'+bound+'_usd']/expense if expense and period['valuation']['priced_calls'] else None
        for row in data.get('account_usage',[]):apply(row,'account',row['account_key'])
    return data
