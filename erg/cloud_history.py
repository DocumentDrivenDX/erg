"""Read-only, parameterized Unity Catalog session browser using operator CLI auth."""
import configparser
import json
import os
from pathlib import Path
import time
from . import databricks


def connection():
    from .delivery_service import configuration
    cfg=configuration();cloud=cfg['cloud'];profiles=configparser.ConfigParser();profiles.read(Path.home()/'.databrickscfg')
    if profiles.get(cloud['profile'],'host',fallback='').rstrip('/')!=cloud['host'].rstrip('/'):
        raise ValueError('reader profile host differs from configured production host')
    return databricks.Client(cloud['profile'],cloud['warehouse']),databricks.namespace(cloud['namespace']),cfg


def filters(args,cfg,require_session=False):
    allowed={'tool','session','project','machine','enrollment_id','destination_generation','start','end','after','limit'}
    if set(args)-allowed or any(len(v)!=1 for v in args.values()):raise ValueError('unsupported cloud filter')
    values={k:v[0] for k,v in args.items()};limit=int(values.get('limit',50))
    if not 1<=limit<=100:raise ValueError('limit must be1..100')
    where=["record_kind='session_event'"];params=[]
    for key in ('tool','session','project','machine'):
        value=values.get(key)
        if value:
            if len(value)>4096:raise ValueError('oversized filter')
            where.append("get_json_object(payload_json,'$.%s')=:%s"%(key,key));params.append(databricks.parameter(key,value))
    enrollment=values.get('enrollment_id') or cfg['identity']['enrollment_id']
    where.append('enrollment_id=:enrollment');params.append(databricks.parameter('enrollment',enrollment))
    if values.get('destination_generation'):
        where.append('destination_generation=:generation');params.append(databricks.parameter('generation',values['destination_generation']))
    if require_session:
        if not values.get('tool') or not values.get('session'):raise ValueError('tool and session required')
    else:
        start=int(values.get('start') or time.time()-7*86400);end=int(values.get('end') or time.time()+60)
        if not 0<=start<end:raise ValueError('invalid time range')
        where.append("CAST(get_json_object(payload_json,'$.ts') AS BIGINT)>=:start AND CAST(get_json_object(payload_json,'$.ts') AS BIGINT)<:end")
        params.extend([databricks.parameter('start',start),databricks.parameter('end',end)])
    after=int(values.get('after',0))
    if not 0<=after<=2**63-1:raise ValueError('invalid cursor')
    return ' AND '.join(where),params,limit,after


def sessions(args):
    client,ns,cfg=connection();where,params,limit,after=filters(args,cfg)
    if after>100000:raise ValueError('narrow filters before paging further')
    sql="""SELECT enrollment_id,destination_generation,
      get_json_object(payload_json,'$.machine') AS machine,get_json_object(payload_json,'$.tool') AS tool,
      get_json_object(payload_json,'$.session') AS session,
      max_by(get_json_object(payload_json,'$.project'),CAST(get_json_object(payload_json,'$.source_seq') AS BIGINT)) AS project,
      MIN(CAST(get_json_object(payload_json,'$.ts') AS BIGINT)) AS first_ts,
      MAX(CAST(get_json_object(payload_json,'$.ts') AS BIGINT)) AS last_ts,COUNT(*) AS events
      FROM %s.current_records WHERE %s GROUP BY 1,2,3,4,5 ORDER BY last_ts DESC,tool,session LIMIT %d OFFSET %d"""%(ns,where,limit+1,after)
    rows=client.sql(sql,params);more=len(rows)>limit;rows=rows[:limit]
    cols='enrollment_id destination_generation machine tool session project first_ts last_ts events'.split()
    return dict(backend='databricks',sessions=[dict(zip(cols,row)) for row in rows],more=more,next_cursor=after+len(rows) if more else None,
                namespace=cfg['cloud']['namespace'],host=cfg['cloud']['host'],reporting_scope='Forward session traces from Unity Catalog; local usage and costs remain separately labelled')


def events(args):
    client,ns,cfg=connection();where,params,limit,after=filters(args,cfg,True)
    where+=" AND CAST(get_json_object(payload_json,'$.source_seq') AS BIGINT)>:after";params.append(databricks.parameter('after',after))
    rows=client.sql("SELECT payload_json FROM %s.current_records WHERE %s ORDER BY CAST(get_json_object(payload_json,'$.source_seq') AS BIGINT),record_id LIMIT %d"%(ns,where,limit+1),params)
    more=len(rows)>limit;values=[json.loads(row[0]) for row in rows[:limit]]
    return dict(backend='databricks',events=values,more=more,next_cursor=values[-1]['source_seq'] if more and values else None)
