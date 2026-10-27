"""Databricks SQL transport, idempotent snapshot sync and report backend.

Uses the existing Databricks CLI login locally. In Databricks Apps it uses
platform-injected OAuth credentials in memory; secrets never enter records.
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import gateway, unified, work


def namespace(value):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*', value):
        raise ValueError('namespace must be catalog.schema with simple identifiers')
    return '.'.join('`%s`' % p for p in value.split('.'))


class Client:
    def __init__(self, profile=None, warehouse=None, access_token=None):
        self.profile = profile
        self.warehouse = warehouse or os.environ.get('ERG_WAREHOUSE_ID')
        self.access_token = access_token
        self.token = None
        self.expires = 0
        self.lock = threading.Lock()

    def api(self, method, path, body=None):
        if self.profile:
            return gateway._api(self.profile, method.lower(), path, body)
        host = os.environ['DATABRICKS_HOST'].rstrip('/')
        if not host.startswith('https://'):
            host = 'https://' + host
        with self.lock:
            if self.access_token:
                self.token = self.access_token
            elif time.time() >= self.expires:
                encoded = urllib.parse.urlencode({'grant_type':'client_credentials', 'scope':'all-apis'}).encode()
                import base64
                basic = base64.b64encode((os.environ['DATABRICKS_CLIENT_ID'] + ':' + os.environ['DATABRICKS_CLIENT_SECRET']).encode()).decode()
                req = urllib.request.Request(host+'/oidc/v1/token', data=encoded, headers={'Authorization':'Basic '+basic})
                with urllib.request.urlopen(req, timeout=30) as r:
                    t = json.load(r)
                self.token, self.expires = t['access_token'], time.time() + int(t.get('expires_in',3600)) - 60
        req=urllib.request.Request(host+path, data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization':'Bearer '+self.token, 'Content-Type':'application/json'}, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            # Do not log arbitrary response bodies: SQL diagnostics can echo parameters.
            raise RuntimeError('Databricks HTTP %s for %s' % (e.code, path)) from None

    def sql(self, statement, parameters=()):
        st=self.api('POST','/api/2.0/sql/statements',dict(warehouse_id=self.warehouse, statement=statement,
            parameters=list(parameters), wait_timeout='50s', on_wait_timeout='CONTINUE', disposition='INLINE',format='JSON_ARRAY'))
        sid=st['statement_id']; deadline=time.monotonic()+600
        while st['status']['state'] in ('PENDING','RUNNING'):
            if time.monotonic() >= deadline:
                self.api('POST','/api/2.0/sql/statements/'+sid+'/cancel')
                raise TimeoutError('Databricks statement exceeded 600 seconds')
            time.sleep(2)
            st=self.api('GET','/api/2.0/sql/statements/'+sid)
        if st['status']['state'] != 'SUCCEEDED':
            error=st['status'].get('error',{})
            if 'PERMISSION_DENIED' in error.get('message',''):
                raise RuntimeError('Databricks SQL permission denied; check namespace privileges and warehouse access')
            raise RuntimeError('Databricks SQL failed (%s)' % error.get('error_code',st['status']['state']))
        if (st.get('manifest') or {}).get('truncated'):raise RuntimeError('Databricks result exceeded inline limits; narrow filters')
        rows=list((st.get('result') or {}).get('data_array') or [])
        nxt=(st.get('result') or {}).get('next_chunk_index')
        while nxt is not None:
            part=self.api('GET','/api/2.0/sql/statements/%s/result/chunks/%s'%(sid,nxt))
            rows.extend(part.get('data_array') or []);nxt=part.get('next_chunk_index')
        cols=((st.get('manifest') or {}).get('schema') or {}).get('columns',[])
        for row in rows:
            for i,col in enumerate(cols):
                if row[i] is None:
                    continue
                typ=col.get('type_name','')
                if typ in ('INT','BIGINT','SMALLINT','TINYINT','LONG'):
                    row[i]=int(row[i])
                elif typ in ('FLOAT','DOUBLE','DECIMAL'):
                    row[i]=float(row[i])
        return rows


def parameter(name,value):
    typ='BOOLEAN' if isinstance(value,bool) else 'INT' if isinstance(value,int) and -(2**31)<=value<2**31 else 'BIGINT' if isinstance(value,int) else 'DOUBLE' if isinstance(value,float) else 'STRING'
    return {'name':name,'value':str(value).lower() if isinstance(value,bool) else str(value),'type':typ}


def provision(client, target):
    """Create only erg-owned objects in an existing, explicitly selected schema."""
    ns=namespace(target)
    client.sql('''CREATE TABLE IF NOT EXISTS %s.erg_observations(
      event_id STRING, machine STRING, kind STRING, ts BIGINT, payload STRING,
      content_hash STRING, seq BIGINT, schema_version INT) USING DELTA''' % ns)
    from . import store
    numeric=set('ts last_ts fresh_in cache_read cache_write out reasoning doc added removed chars words est_tokens added_code removed_code added_doc removed_doc files doc_files ncpu last_sync last_perf dup errors installed linked missing summary_in summary_out summary_cache_read summary_cache_write'.split())
    numeric.update(work.NUMERIC);numeric.update(('cache_write_5m','cache_write_1h'))
    for kind,table in store.TABLE.items():
        cols=list(store.COLS[kind])
        if kind=='llm':
            cols=list(dict.fromkeys(cols+['requester','dup','request_id','response_id']))
        select=['machine']
        for col in cols:
            if col=='dup':
                select.append('0 AS dup');continue
            value="get_json_object(payload,'$.%s')"%col
            if col=='installed':
                value="CASE WHEN %s='true' THEN 1 ELSE 0 END"%value
            elif col in ('usd','amount','used_percent','reported_cost_usd'):
                value='CAST(%s AS DOUBLE)'%value
            elif col in numeric:
                value='CAST(%s AS BIGINT)'%value
            elif col=='requester':
                value="coalesce(%s,'')"%value
            select.append(value+' AS `'+col+'`')
        client.sql('CREATE OR REPLACE VIEW %s.erg_%s AS SELECT %s FROM %s.erg_observations WHERE kind=\'%s\''%
                   (ns,table,','.join(select),ns,kind))
    local_cols=list(dict.fromkeys(store.COLS['llm']+['requester','dup','request_id','response_id']))
    aliases={
        'uid':"concat('g:',coalesce(invocation_id,request_id))",'ts':'unix_timestamp(event_time)',
        'tool':"'gateway'",'session':"coalesce(session_metadata.client_session_id,'')",
        'project':"'(gateway)'",'model':"coalesce(destination_model,endpoint_name,'unknown')",
        'kind':"'main'",'client':"coalesce(session_metadata.coding_agent,'gateway')",
        'fresh_in':'greatest(coalesce(input_tokens,0)-coalesce(token_details.cache_read_input_tokens,0)-coalesce(token_details.cache_creation_input_tokens,0),0)',
        'cache_read':'coalesce(token_details.cache_read_input_tokens,0)',
        'cache_write':'coalesce(token_details.cache_creation_input_tokens,0)',
        'out':'coalesce(output_tokens,0)', 'reasoning':'coalesce(token_details.output_reasoning_tokens,0)',
        'requester':"coalesce(requester,'')",'dup':'0','request_id':'request_id','response_id':"''"}
    for col in local_cols:aliases.setdefault(col,'NULL')
    # Reuse the typed local view, then union live gateway accounting with explicit columns.
    # A separate local view avoids a self-referencing replacement of erg_llm.
    local_projection=['machine']
    for col in local_cols:
        v="get_json_object(payload,'$.%s')"%col
        if col=='reported_cost_usd':v='CAST('+v+' AS DOUBLE)'
        elif col in numeric: v='CAST('+v+' AS BIGINT)'
        if col=='dup': v='0'
        if col in ('requester','request_id','response_id'): v="coalesce("+v+",'')"
        local_projection.append(v+' AS `'+col+'`')
    client.sql("CREATE OR REPLACE VIEW "+ns+".erg_local_llm AS SELECT "+','.join(local_projection)+" FROM "+ns+".erg_observations WHERE kind='llm' AND coalesce(get_json_object(payload,'$.tool'),'')<>'gateway'")
    gateway_projection=["'databricks' AS machine"]+[aliases[c]+' AS `'+c+'`' for c in local_cols]
    client.sql("CREATE OR REPLACE VIEW "+ns+".erg_llm AS SELECT * FROM "+ns+".erg_local_llm UNION ALL SELECT "+','.join(gateway_projection)+" FROM system.ai_gateway.usage WHERE event_time>=current_timestamp()-INTERVAL 400 DAYS AND coalesce(total_tokens,0)>0 AND coalesce(status_code,200)<400")
    for kind,table,fields in [('metric','metrics','src m iv v pk'),('note','notes','k text')]:
        expr=['machine','ts']
        for col in fields.split():
            val="get_json_object(payload,'$.%s')"%col
            if col in ('v','pk'):val='CAST(%s AS DOUBLE)'%val
            if col=='iv':val='CAST(%s AS BIGINT)'%val
            expr.append(val+' AS '+col)
        client.sql('CREATE OR REPLACE VIEW %s.erg_%s AS SELECT %s FROM %s.erg_observations WHERE kind=\'%s\''%(ns,table,','.join(expr),ns,kind))
    client.sql('CREATE OR REPLACE VIEW %s.erg_machines AS SELECT DISTINCT machine FROM %s.erg_observations'%(ns,ns))
    for name,statement in [('calls',unified.CALLS_SELECT),('work_commits',unified.WORK_SELECT),('report_sessions',unified.SESSIONS_SELECT),('input_messages',work.INPUTS_SELECT)]:
        for old in ['llm','commits','sessions','prompts','calls']:
            statement=re.sub(r'\bFROM\s+'+old+r'\b','FROM '+ns+'.erg_'+old,statement)
        client.sql('CREATE OR REPLACE VIEW '+ns+'.erg_'+name+' AS '+statement)
    # An OTel-compatible projection alongside raw source evidence, never fake inference payloads.
    client.sql('''CREATE OR REPLACE VIEW %s.erg_spans AS SELECT event_id AS record_id,
      timestamp_seconds(ts) AS time, CAST(timestamp_seconds(ts) AS DATE) AS date,
      'erg' AS service_name,
      substring(sha2(concat('erg:',coalesce(get_json_object(payload,'$.tool'),''),':',coalesce(get_json_object(payload,'$.session'),event_id)),256),1,32) AS trace_id,
      substring(event_id,1,16) AS span_id, CAST(NULL AS STRING) AS parent_span_id,
      CASE WHEN kind='session' THEN 'invoke_agent' ELSE 'chat' END AS name,
      CASE WHEN kind='session' THEN 'SPAN_KIND_INTERNAL' ELSE 'SPAN_KIND_CLIENT' END AS kind,
      ts*1000000000 AS start_time_unix_nano,
      CASE WHEN kind='session' THEN CAST(get_json_object(payload,'$.last_ts') AS BIGINT)*1000000000 ELSE NULL END AS end_time_unix_nano,
      parse_json(to_json(named_struct(
        'gen_ai.conversation.id',get_json_object(payload,'$.session'),
        'gen_ai.operation.name',CASE WHEN kind='session' THEN 'invoke_agent' ELSE 'chat' END,
        'gen_ai.request.model',get_json_object(payload,'$.model'),
        'gen_ai.usage.input_tokens',CAST(get_json_object(payload,'$.fresh_in') AS BIGINT)+CAST(get_json_object(payload,'$.cache_read') AS BIGINT)+CAST(get_json_object(payload,'$.cache_write') AS BIGINT),
        'gen_ai.usage.output_tokens',CAST(get_json_object(payload,'$.out') AS BIGINT),
        'gen_ai.usage.reasoning.output_tokens',CAST(get_json_object(payload,'$.reasoning') AS BIGINT),
        'gen_ai.agent.name',get_json_object(payload,'$.tool'),
        'gen_ai.usage.cache_read.input_tokens',CAST(get_json_object(payload,'$.cache_read') AS BIGINT),
        'gen_ai.usage.cache_write.input_tokens',CAST(get_json_object(payload,'$.cache_write') AS BIGINT),
        'gen_ai.response.id',get_json_object(payload,'$.response_id'),
        'erg.project',get_json_object(payload,'$.project'),
        'erg.provenance','reconstructed','erg.timing','observation_time',
        'erg.event_id',event_id,'host.name',machine))) AS attributes, schema_version
      FROM %s.erg_observations WHERE kind IN ('session','llm')'''%(ns,ns))


    client.sql("""CREATE OR REPLACE VIEW %s.erg_inference_calls AS SELECT
      nullif(request_id,'') AS request_id,uid AS invocation_id,timestamp_seconds(ts) AS event_time,
      CAST(NULL AS INT) AS status_code,CAST(NULL AS BIGINT) AS latency_ms,
      CAST(NULL AS STRING) AS request,CAST(NULL AS STRING) AS response,
      model AS destination_model,requester,'erg-1' AS schema_version,
      session AS client_session_id,tool AS coding_agent,project,machine,
      fresh_in,cache_read,cache_write,out,reasoning FROM %s.erg_calls"""%(ns,ns))
    client.sql("""CREATE OR REPLACE VIEW %s.erg_hourly_usage AS SELECT
      CAST(floor(ts/3600) AS BIGINT) AS hour,machine,tool,project,model,kind,requester,
      COUNT(*) AS calls,COUNT(DISTINCT session) AS sessions,
      SUM(fresh_in) AS fresh_in,SUM(cache_read) AS cache_read,SUM(cache_write) AS cache_write,
      SUM(out) AS out,SUM(reasoning) AS reasoning FROM %s.erg_calls GROUP BY 1,2,3,4,5,6,7"""%(ns,ns))
    client.sql("""CREATE OR REPLACE VIEW %s.erg_daily_usage AS SELECT
      CAST(timestamp_seconds(hour*3600) AS DATE) AS date,machine,tool,project,model,kind,requester,
      SUM(calls) AS calls,SUM(fresh_in) AS fresh_in,SUM(cache_read) AS cache_read,
      SUM(cache_write) AS cache_write,SUM(out) AS out,SUM(reasoning) AS reasoning
      FROM %s.erg_hourly_usage GROUP BY 1,2,3,4,5,6,7"""%(ns,ns))


def sync(db, client, target, batch_size=5000, max_batches=None, replay=False):
    ns=namespace(target)
    table=client.api('GET','/api/2.1/unity-catalog/tables/'+target+'.erg_observations')
    destination='databricks-table:'+table['table_id']
    if replay:
        with db: db.execute('INSERT OR REPLACE INTO export_cursors VALUES(?,0)',(destination,))
    total=batches=0
    while max_batches is None or batches<max_batches:
        rows=unified.pending(db,destination,batch_size)
        if not rows:
            break
        data=[dict(zip(['event_id','machine','kind','ts','payload','content_hash','seq'],r)) for r in rows]
        body=json.dumps(data,separators=(',',':'))
        while len(body.encode())>2*1024*1024 and len(rows)>1:
            rows=rows[:len(rows)//2];data=data[:len(rows)]
            body=json.dumps(data,separators=(',',':'))
        if len(body.encode())>2*1024*1024:
            raise ValueError('one observation exceeds the 2 MiB delivery limit')
        stmt='''MERGE INTO %s.erg_observations t USING
          (SELECT e.*, %d AS schema_version FROM
           (SELECT explode(from_json(:batch,'ARRAY<STRUCT<event_id:STRING,machine:STRING,kind:STRING,ts:BIGINT,payload:STRING,content_hash:STRING,seq:BIGINT>>')) AS e)) s
          ON t.event_id=s.event_id
          WHEN MATCHED AND s.seq>=t.seq AND s.content_hash<>t.content_hash THEN UPDATE SET *
          WHEN NOT MATCHED THEN INSERT *'''%(ns,unified.VERSION)
        client.sql(stmt,[parameter('batch',body)])
        unified.acknowledge(db,destination,rows[-1][-1])
        total+=len(rows);batches+=1
        print("export progress: %d observations, %d batches"%(total,batches),flush=True)
    return total


class Result(list):
    def fetchone(self):
        return self[0] if self else None


class ReportDB:
    """Small SELECT-only adapter for the shared rollup queries."""
    def __init__(self,client,target):
        self.client=client;self.ns=namespace(target)

    def execute(self,sql,args=()):
        if not sql.lstrip().upper().startswith('SELECT'):
            raise ValueError('report backend is read-only')
        tables=['llm','calls','work_commits','edits','prompts','commits','metrics','notes','machines','sessions','sources','report_sessions','input_messages']
        for table in tables:
            sql=re.sub(r'\b(FROM|JOIN)\s+'+table+r'\b',lambda m:m[1]+' '+self.ns+'.erg_'+table,sql,flags=re.I)
        sql=sql.replace('ts/3600','CAST(floor(ts/3600) AS BIGINT)')
        # SQLite permits a bare value alongside MAX; central SQL requires an explicit aggregate.
        sql=sql.replace("SELECT CAST(floor(ts/3600) AS BIGINT),machine,text FROM", "SELECT CAST(floor(ts/3600) AS BIGINT),machine,max_by(text,ts) FROM")
        sql=sql.replace(' HAVING ts=MAX(ts)','')
        parameters=[]
        for i,value in enumerate(args):
            sql=sql.replace('?',':p'+str(i),1)
            parameters.append(parameter('p'+str(i),value))
        return Result(self.client.sql(sql,parameters))
