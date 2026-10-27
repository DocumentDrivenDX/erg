"""Bounded read-only snapshots of OpenCode's mutable SQLite transcript rows.

This adapter preserves observed native projections, not an unobserved mutation log.
It never derives usage from row counts or session-level cumulative token summaries.
"""
import contextlib
import json
from pathlib import Path
import sqlite3
import time
from . import collect,history

PARSER_VERSION=2


def signature(path):
    result=[]
    for name in (str(path),str(path)+'-wal'):
        try:
            s=Path(name).stat();result.append([s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns])
        except FileNotFoundError:result.append(None)
    return result


def checkpoint(db,path,context,state,changed=0,errors=0):
    now=int(time.time())
    db.execute('''INSERT INTO history_sources(path,tool,inode,offset,context,size,mtime,first_seen,last_seen,events,errors,state)
     VALUES(?,'opencode','sqlite',0,?,0,0,?,?,?, ?,?) ON CONFLICT(path) DO UPDATE SET
     context=excluded.context,last_seen=excluded.last_seen,events=coalesce(events,0)+excluded.events,
     errors=excluded.errors,state=excluded.state''',
     (str(path),json.dumps(context),now,now,changed,errors,state))


def scan_file(db,path,enrolled,limit=500):
    path=Path(path);key=str(path);sig=signature(path)
    previous=db.execute('SELECT context,state,last_seen FROM history_sources WHERE path=?',(key,)).fetchone()
    context=json.loads(previous[0]) if previous and previous[0] else {}
    if previous and context.get('signature')==sig and context.get('parser_version')==PARSER_VERSION and previous[1]=='captured':
        if int(time.time())-(previous[2] or 0)>=history.HEARTBEAT_INTERVAL:
            with db:checkpoint(db,path,context,'captured')
        return dict(events=0,errors=0,rows=0)
    # Finish one bounded traversal even while the source changes; a changed
    # fingerprint causes another traversal after completion. IDs use native PKs.
    replaced=context.get('signature') and context['signature'][0] and sig[0] and context['signature'][0][:2]!=sig[0][:2]
    if not previous or previous[1]!='pending' or replaced or context.get('parser_version')!=PARSER_VERSION:
        context=dict(signature=sig,table=0,after='',rows_checked=0,enrolled_at=enrolled,parser_version=PARSER_VERSION)
    changed=checked=0;errors=context.get('errors',0);state='captured'
    try:
        with contextlib.closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1)) as source:
            source.execute('PRAGMA query_only=ON');source.execute('BEGIN')
            tables={r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            sessions=[name for name in ('session','session_v2') if name in tables]
            variants=[name for name in ('message','session_message','part') if name in tables]
            if not sessions or not variants:raise ValueError('unsupported SQLite transcript schema')
            while context['table']<len(variants) and checked<limit:
                table=variants[context['table']]
                targets={r[2] for r in source.execute('PRAGMA foreign_key_list('+table+')') if r[3]=='session_id' and r[2] in sessions}
                preferred='session_v2' if table=='session_message' else 'session'
                session=next(iter(targets)) if len(targets)==1 else preferred if preferred in sessions else sessions[0]
                scols={r[1] for r in source.execute('PRAGMA table_info('+session+')')}
                if not {'id','directory'}<=scols:raise ValueError('unsupported SQLite session schema')
                cols={r[1] for r in source.execute('PRAGMA table_info('+table+')')}
                if not {'id','session_id','data','time_created'}<=cols:raise ValueError('unsupported SQLite record schema')
                updated='m.time_updated' if 'time_updated' in cols else 'm.time_created'
                typ='m.type' if 'type' in cols else "'"+table+"'"
                title='s.title' if 'title' in scols else 'NULL'
                parent='m.message_id' if 'message_id' in cols else 'NULL'
                sequence='m.seq' if 'seq' in cols else 'NULL'
                records=source.execute('SELECT m.id,m.session_id,m.time_created,'+updated+',s.directory,'+typ+','+title+','+parent+','+sequence+' FROM '+table+' m LEFT JOIN '+session+' s ON s.id=m.session_id WHERE m.id>? ORDER BY m.id LIMIT ?',
                  (context['after'],limit-checked+1)).fetchall()
                remaining=limit-checked;more=len(records)>remaining;records=records[:remaining]
                page_changed=0
                with db:
                    for rid,sid,created,updated,directory,kind,title_value,parent_id,sequence_value in records:
                        checked+=1;context['after']=rid;context['rows_checked']+=1
                        ts=collect.parse_ts(updated) or collect.parse_ts(created)
                        if ts is None:errors+=1;continue
                        from . import titles
                        titles.put(db,'opencode',sid,title_value,'opencode_session_metadata',20,ts)
                        if ts<enrolled:continue  # Inspect timestamps, not historical bodies.
                        if not sid or directory is None:errors+=1;continue
                        raw,size=source.execute('SELECT CASE WHEN length(CAST(data AS BLOB))<=33554432 THEN data END,length(data) FROM '+table+' WHERE id=?',(rid,)).fetchone()
                        if raw is None:errors+=1;continue
                        try:payload=json.loads(raw)
                        except (ValueError,TypeError):payload={'unparsed_content':raw};errors+=1
                        if isinstance(payload,dict):
                            payload={k:v for k,v in payload.items() if k not in ('headers','authorization','apiKey','accessToken','refreshToken','credentials')}
                        body=dict(type='sqlite_record',table=table,id=rid,session_id=sid,record_type=kind,
                                  source_created_at=created,source_updated_at=updated,parent_message_id=parent_id,native_sequence=sequence_value,data=payload)
                        role=payload.get('role','') if isinstance(payload,dict) else ''
                        _,fresh=history.put(db,'opencode',sid,collect.project_of(directory),ts,'sqlite_record',body,
                          table+':'+rid,role=role if isinstance(role,str) else '',origin='native_projection',source=key)
                        page_changed+=int(fresh)
                    if more:state='pending'
                    else:context['table']+=1;context['after']=''
                    context['errors']=errors
                    checkpoint(db,path,context,'pending' if context['table']<len(variants) else 'captured',page_changed,errors)
                    changed+=page_changed
                if more:break
            if context['table']<len(variants):state='pending'
    except (OSError,sqlite3.Error,ValueError,TypeError):
        with db:checkpoint(db,path,context,'source_error',0,max(1,errors))
        return dict(events=changed,errors=max(1,errors),rows=checked)
    if errors:state='parse_error'
    with db:checkpoint(db,path,context,state,0,errors)
    return dict(events=changed,errors=errors,rows=checked)


def scan(db,enrolled):
    total=dict(events=0,errors=0,files=0,rows=0)
    for root in collect.opencode_roots():
        path=Path(root)/'opencode.db'
        if not path.exists():continue
        result=scan_file(db,path,enrolled)
        total['files']+=1
        for k in ('events','errors','rows'):total[k]+=result[k]
    return total
