"""View-specific wire shapes; full evidence and valuation stay in the read model."""
VIEWS=frozenset(('full','work','accounts','capture'))
COMMON=('monetary','available','generated','definition_version','cost_provenance')
WORK=('valuation','project_catalog','folder_catalog','project_revision','observed_tools','report_scope','reported_call_count','reported_call_value','input_count_basis','prs','commits','commit_attributed_calls','total_calls','attributed_calls','pre_pr_calls','unattributed_calls',
      'automatic_evidence_count','unattributed_reasons','definitions','limitations','unattributed_sessions','task_contexts','unclassified_codex_role_records')
ACCOUNTS=('valuation','subscription_periods','quota_latest','reset_history','quota_counts','account_registry','account_usage','billing_scope','registry_scope')


def account_registry(records):
    """Match the UI's latest nonempty observed fields, preserving field provenance."""
    accounts={}
    for record in records:
        key=record['account_key'];previous=accounts.get(key)
        if previous is None:
            accounts[key]={field:record.get(field) for field in ('account_key','provider','email_hint','plan','ts','provenance','billing_status')}
            accounts[key].update(email_hint_ts=record['ts'],plan_ts=record['ts'],plan_source=record['provenance'],identity_record_count=1,billing_status_ts=record['ts'] if record.get('billing_status') else 0)
            continue
        previous['identity_record_count']+=1
        for field in ('email_hint','plan'):
            if record.get(field) and (not previous.get(field) or record['ts']>(previous.get(field+'_ts') or 0)):
                previous[field]=record[field];previous[field+'_ts']=record['ts'];previous[field+'_source']=record['provenance']
        if record['ts']>previous['ts']:
            previous['ts']=record['ts'];previous['provenance']=record['provenance']
        if record.get('billing_status') and record['ts']>=previous.get('billing_status_ts',0):
            previous['billing_status']=record['billing_status'];previous['billing_status_ts']=record['ts']
    return list(accounts.values())


def project(data,view):
    if view not in VIEWS:raise ValueError('invalid report view')
    if view=='full' or data.get('available') is False:return data
    fields=COMMON+(WORK if view=='work' else ACCOUNTS if view=='accounts' else ('sessions',))
    result={key:data[key] for key in fields if key in data};result['view']=view
    if 'rollup_status' in data:
        for key in ('rollup_status','project_catalog','folder_catalog','project_revision','report_scope','observed_tools','reported_call_count','reported_call_value','total_calls'):
            if key in data:result[key]=data[key]
    if view=='accounts':
        records=data.get('accounts',[])
        if 'account_registry' not in result:result.update(account_registry=account_registry(records),identity_record_count=len(records),registry_scope='Latest nonempty observed identity fields; not subscription or billing proof')
    elif view=='capture':result['subscription_period_count']=len(data.get('subscription_periods',[]))
    return result
