"""Shared project/folder allocation for usage, work activity and valuation."""
import json
import math
from . import project_scope,projects,rates,report_sqlite

LLM_COLS='hour machine tool project model kind fresh_in cache_read cache_write out reasoning calls requester folder value_low_usd value_high_usd priced_calls unpriced_calls reported_usd reported_calls'.split()
EDIT_COLS='hour machine tool project model doc added removed n folder'.split()
PROMPT_COLS='hour machine tool project model n chars words est_tokens folder'.split()
COMMIT_COLS='hour machine project agent model n added_code removed_code added_doc removed_doc files doc_files folder'.split()


def build(db,since=0,end=None,scope=None,card=None,on_call=None,tick=None,on_input=None):
    scope=scope or project_scope.Scope(db,2**63-1);card=card or rates.load(db);cells={k:{} for k in ('llm','edits','prompts','commits')};sessions={};dates=set();sources=set();observed_tools=set()
    def parts(row,value=None,is_call=False):
        observed_tools.add(row.get('tool','none'))
        result=scope.resolve(row);folder=result['folder']['folder_key']
        if tick:tick()
        if is_call and on_call:on_call(row,result,value)
        if row.get('_input') and on_input:on_input(row,result)
        if row.get('session'):
            key=(row['machine'],row['tool'],row['session'])
            item=sessions.setdefault(key,dict(machine=key[0],tool=key[1],session=key[2],project_keys=set(),folder_keys=set()))
            item['folder_keys'].add(folder);item['project_keys'].update(p['project_key'] for p in result['allocations'])
        for part in result['allocations']:yield part['project_key'],folder,part['units']/projects.UNITS
    def cell(kind,key,initial):
        if key not in cells[kind]:cells[kind][key]=initial
        return cells[kind][key]
    cols='uid ts machine tool session project model kind fresh_in cache_read cache_write out reasoning requester dup cache_write_5m cache_write_1h reported_cost_usd'.split()
    relation=report_sqlite.calls_select(cols,db=db)
    for values in db.execute('SELECT ts/3600,'+','.join(cols)+' FROM ('+relation+') WHERE ts>=? AND ts<?',(since,end if end is not None else 2**63-1)):
        hour=values[0];row=dict(zip(cols,values[1:]));value=rates.value(row,card)
        if value:dates.add(value['as_of']);sources.add(value['source'])
        for human,folder,w in parts(row,value,True):
            key=(hour,row['machine'],row['tool'],human,row['model'],row['kind'],row['requester'],folder)
            out=cell('llm',key,[*key[:6],0,0,0,0,0,0,row['requester'],folder,0,0,0,0,0,0])
            for i,name in enumerate(('fresh_in','cache_read','cache_write','out','reasoning'),6):out[i]+=(row[name] or 0)*w
            out[11]+=w
            if value:out[14]+=value['low_usd']*w;out[15]+=value['high_usd']*w;out[16]+=w
            else:out[17]+=w
            reported=row['reported_cost_usd']
            if isinstance(reported,(int,float)) and not isinstance(reported,bool) and math.isfinite(reported) and reported>=0:out[18]+=reported*w;out[19]+=w
    for kind,table,columns,metrics in (
        ('edits','edits','machine tool session project model doc ts added removed'.split(),('added','removed')),
        ('prompts','input_messages','machine tool session project model ts chars words est_tokens uid source_uid origin'.split(),('chars','words','est_tokens')),
        ('commits','work_commits','machine project agent model ts repo added_code removed_code added_doc removed_doc files doc_files'.split(),('added_code','removed_code','added_doc','removed_doc','files','doc_files'))):
        relation=table;params=(since,end if end is not None else 2**63-1)
        if end is not None and table in ('input_messages','work_commits'):
            if table=='input_messages':
                eligible="(tool<>'codex' OR COALESCE(provenance,'') IN ('codex_user_text','codex_user_event'))"
                relation="(WITH targets AS (SELECT DISTINCT tool target_tool,COALESCE(NULLIF(source_uid,''),uid) k FROM prompts WHERE ts>=? AND ts<? AND "+eligible+") SELECT * FROM (SELECT p.*,ROW_NUMBER() OVER(PARTITION BY p.tool,COALESCE(NULLIF(p.source_uid,''),p.uid) ORDER BY CASE WHEN origin IN ('human','automation') THEN 1 ELSE 0 END DESC,ts,machine,session) rk FROM targets t CROSS JOIN prompts p ON p.tool=t.target_tool AND COALESCE(NULLIF(p.source_uid,''),p.uid)=t.k WHERE "+eligible+") WHERE rk=1)"
            else:
                relation='(WITH targets AS (SELECT DISTINCT uid FROM commits WHERE ts>=? AND ts<?) SELECT * FROM (SELECT c.*,ROW_NUMBER() OVER(PARTITION BY c.uid ORDER BY machine) rk FROM targets t CROSS JOIN commits c ON c.uid=t.uid) WHERE rk=1)'
            params=(since,end)+params
        for values in db.execute('SELECT ts/3600,'+','.join(columns)+' FROM '+relation+' WHERE ts>=? AND ts<?',params):
            hour=values[0];row=dict(zip(columns,values[1:]));row.setdefault('tool',row.get('agent','none'));row.setdefault('session','')
            if kind=='prompts':row['_input']=True
            for human,folder,w in parts(row):
                if kind=='edits':
                    key=(hour,row['machine'],row['tool'],human,row['model'],row['doc'],folder)
                    out=cell(kind,key,[*key[:6],0,0,0,folder]);offset=6
                elif kind=='prompts':
                    key=(hour,row['machine'],row['tool'],human,row['model'],folder)
                    out=cell(kind,key,[*key[:5],0,0,0,0,folder]);offset=6
                else:
                    key=(hour,row['machine'],human,row['agent'],row['model'],folder)
                    out=cell(kind,key,[*key[:5],0,0,0,0,0,0,0,folder]);offset=6
                for i,name in enumerate(metrics,offset):out[i]+=(row[name] or 0)*w
                out[8 if kind=='edits' else 5]+=w
    result=dict(observed_tools=sorted(observed_tools),project_catalog=scope.config['projects'],folder_catalog=sorted(scope.folders.values(),key=lambda r:(r['name'],r['folder_key'])),project_revision=scope.config['revision'],
                project_allocation_scope='Temporal session → folder/agent project → human project; weighted equivalents, explicit unassigned remainder',
                cost_basis='Estimated standard API-equivalent value; native reported estimates separate; billed and subscription allocations unknown',
                rate_card_dates=sorted(dates),rate_sources=sorted(sources),
                session_allocations=[dict(item,project_keys=sorted(item['project_keys']),folder_keys=sorted(item['folder_keys'])) for item in sessions.values()])
    for kind,cols in (('llm',LLM_COLS),('edits',EDIT_COLS),('prompts',PROMPT_COLS),('commits',COMMIT_COLS)):
        result[{'edits':'edit','prompts':'prompt','commits':'commit'}.get(kind,kind)+'_cols']=cols
        result[kind]=[value for key,value in sorted(cells[kind].items(),key=lambda item:json.dumps(item[0],separators=(',',':')))]
    return result
