"""Private forward-only native history, source checkpoints and capture evidence."""
import collections
import hashlib
import json
import os
from pathlib import Path
import time
import zlib
from . import collect

SCHEMA='''
CREATE TABLE IF NOT EXISTS history_metadata_mirrors(event_id TEXT PRIMARY KEY,content_hash TEXT,source_hash TEXT);
CREATE TABLE IF NOT EXISTS history_config(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE IF NOT EXISTS history_sources(path TEXT PRIMARY KEY,tool TEXT,inode TEXT,offset INT,context TEXT,
 size INT,mtime REAL,first_seen INT,last_seen INT,events INT,errors INT,state TEXT);
CREATE INDEX IF NOT EXISTS history_source_errors ON history_sources(errors) WHERE errors>0;
CREATE INDEX IF NOT EXISTS history_source_recent ON history_sources(mtime DESC);
CREATE TABLE IF NOT EXISTS history_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT UNIQUE,machine TEXT,
 tool TEXT,session TEXT,project TEXT,ts INT,kind TEXT,role TEXT,origin TEXT,source TEXT,body BLOB,content_hash TEXT,
 bytes INT,expires_at INT,expired_at INT);
CREATE INDEX IF NOT EXISTS history_session ON history_events(tool,session,seq);
CREATE INDEX IF NOT EXISTS history_time ON history_events(ts,project,tool);
CREATE TABLE IF NOT EXISTS history_usage_snapshots(event_id TEXT PRIMARY KEY,machine TEXT,tool TEXT,session TEXT,ts INT,input_tokens INT,output_tokens INT,cache_read INT,cache_write INT,total_tokens INT);
CREATE INDEX IF NOT EXISTS history_usage_snapshot_session ON history_usage_snapshots(tool,session,ts);
CREATE TABLE IF NOT EXISTS history_projection_usage(machine TEXT,tool TEXT,uid TEXT,value TEXT,PRIMARY KEY(machine,tool,uid));
CREATE TABLE IF NOT EXISTS history_projection_messages(machine TEXT,tool TEXT,session TEXT,native_id TEXT,role TEXT,ts INT,PRIMARY KEY(machine,tool,session,native_id));
CREATE TABLE IF NOT EXISTS history_projection_parts(machine TEXT,tool TEXT,session TEXT,parent_id TEXT,part_id TEXT,event_id TEXT,chars INT,words INT,est_tokens INT,PRIMARY KEY(machine,tool,session,part_id));
CREATE INDEX IF NOT EXISTS history_projection_parent ON history_projection_parts(machine,tool,session,parent_id);
CREATE TABLE IF NOT EXISTS history_pending(event_id TEXT PRIMARY KEY,content_hash TEXT,enqueued_at INT,attempts INT DEFAULT 0,retry_at INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS history_projection_state(event_id TEXT PRIMARY KEY,content_hash TEXT,version INT,state TEXT,reason TEXT,observations INT,processed_at INT);
CREATE TABLE IF NOT EXISTS history_projection_context(machine TEXT,tool TEXT,session TEXT,value TEXT,PRIMARY KEY(machine,tool,session));
CREATE TABLE IF NOT EXISTS history_edit_pending(event_id TEXT PRIMARY KEY,content_hash TEXT,seq INTEGER,attempts INTEGER DEFAULT 0,retry_at INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS history_edit_pending_order ON history_edit_pending(seq);
CREATE INDEX IF NOT EXISTS history_edit_pending_errors ON history_edit_pending(attempts) WHERE attempts>0;
CREATE TABLE IF NOT EXISTS history_edit_errors(event_id TEXT PRIMARY KEY,reason TEXT);
CREATE TABLE IF NOT EXISTS history_links(local_session TEXT PRIMARY KEY,tool TEXT,session TEXT,response_id TEXT,linked_at INT);
CREATE TABLE IF NOT EXISTS local_inferences(invocation_id TEXT PRIMARY KEY,request_id TEXT,response_id TEXT,
 event_time INT,session TEXT,project TEXT,destination TEXT,model TEXT,status_code INT,execution_duration_ms INT,
 time_to_first_byte_ms INT,input_tokens INT,output_tokens INT,cache_read INT,cache_write INT,usage_known INT,
 state TEXT,error TEXT,request_event_id TEXT,response_event_id TEXT);
CREATE TRIGGER IF NOT EXISTS history_enqueue_insert AFTER INSERT ON history_events
 WHEN NEW.body IS NOT NULL BEGIN
 INSERT INTO history_pending(event_id,content_hash,enqueued_at) VALUES(NEW.event_id,NEW.content_hash,CAST(strftime('%s','now') AS INT))
 ON CONFLICT(event_id) DO UPDATE SET content_hash=excluded.content_hash,enqueued_at=excluded.enqueued_at,attempts=0,retry_at=0;
 END;
CREATE TRIGGER IF NOT EXISTS history_enqueue_update AFTER UPDATE OF body,content_hash ON history_events
 WHEN NEW.body IS NOT NULL AND (OLD.content_hash<>NEW.content_hash OR OLD.body IS NULL) BEGIN
 INSERT INTO history_pending(event_id,content_hash,enqueued_at) VALUES(NEW.event_id,NEW.content_hash,CAST(strftime('%s','now') AS INT))
 ON CONFLICT(event_id) DO UPDATE SET content_hash=excluded.content_hash,enqueued_at=excluded.enqueued_at,attempts=0,retry_at=0;
 END;
CREATE TRIGGER IF NOT EXISTS history_expired_update BEFORE UPDATE OF body ON history_events
 WHEN OLD.expired_at IS NOT NULL AND NEW.body IS NOT NULL BEGIN SELECT RAISE(IGNORE); END;
CREATE TRIGGER IF NOT EXISTS history_expired_insert BEFORE INSERT ON history_events
 WHEN NEW.body IS NOT NULL AND EXISTS(SELECT 1 FROM history_events WHERE event_id=NEW.event_id AND expired_at IS NOT NULL)
 BEGIN SELECT RAISE(IGNORE); END;
'''


def initialize(db):
    db.executescript(SCHEMA)
    columns={r[1] for r in db.execute('PRAGMA table_info(history_events)')}
    if 'capture_version' not in columns:db.execute('ALTER TABLE history_events ADD COLUMN capture_version INT DEFAULT 1')
    if 'projection_hint' not in columns:db.execute('ALTER TABLE history_events ADD COLUMN projection_hint TEXT')
    pending_columns={r[1] for r in db.execute('PRAGMA table_info(history_pending)')}
    for name in ('attempts','retry_at'):
        if name not in pending_columns:db.execute('ALTER TABLE history_pending ADD COLUMN '+name+' INT DEFAULT 0')
    for name in ('source_offset','source_inode','source_ts'):
        if name not in columns:db.execute('ALTER TABLE history_events ADD COLUMN '+name+' INTEGER')
    pending_columns={r[1] for r in db.execute('PRAGMA table_info(history_pending)')}
    if 'seq' not in pending_columns:db.execute('ALTER TABLE history_pending ADD COLUMN seq INTEGER')
    db.execute('CREATE INDEX IF NOT EXISTS history_pending_sequence ON history_pending(seq,event_id,retry_at)')
    db.execute("""CREATE TRIGGER IF NOT EXISTS history_pending_sequence_insert AFTER INSERT ON history_pending BEGIN
      UPDATE history_pending SET seq=(SELECT seq FROM history_events WHERE event_id=NEW.event_id) WHERE event_id=NEW.event_id; END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS history_pending_sequence_update AFTER UPDATE OF event_id,content_hash ON history_pending BEGIN
      UPDATE history_pending SET seq=(SELECT seq FROM history_events WHERE event_id=NEW.event_id) WHERE event_id=NEW.event_id; END""")
    db.execute('UPDATE history_pending SET seq=(SELECT seq FROM history_events WHERE event_id=history_pending.event_id) WHERE seq IS NULL')
    if not db.execute("SELECT 1 FROM history_config WHERE key='projection_seed_v1'").fetchone():
        db.execute('INSERT OR IGNORE INTO history_pending(event_id,content_hash,enqueued_at) SELECT event_id,content_hash,? FROM history_events WHERE body IS NOT NULL',(int(time.time()),))
        db.execute("INSERT INTO history_config VALUES('projection_seed_v1','1')")


    if not db.execute("SELECT 1 FROM history_config WHERE key='edit_projection_v2_seed'").fetchone():
        db.execute("""INSERT OR IGNORE INTO history_edit_pending(event_id,content_hash,seq)
          SELECT e.event_id,e.content_hash,e.seq FROM history_events e JOIN history_projection_state s ON s.event_id=e.event_id
          WHERE COALESCE(s.version,0)<2 AND e.body IS NOT NULL AND
          ((e.tool='claude' AND e.kind='assistant') OR (e.tool='codex' AND e.kind='response_item' AND COALESCE(e.role,'')<>'user'))""")
        db.execute("INSERT INTO history_config VALUES('edit_projection_v2_seed','1')")



def config():
    try:return json.loads(Path(collect.STATE_DIR,'capture.json').read_text())
    except FileNotFoundError:return {}


def enroll(db):
    """The operator opted in. Historical files become baseline checkpoints."""
    from .service import atomic_json
    current=config()
    current.update(content=True,enrolled_at=current.get('enrolled_at') or int(time.time()))
    atomic_json(Path(collect.STATE_DIR,'capture.json'),current)
    with db:db.execute('INSERT OR IGNORE INTO history_config VALUES(?,?)',('enrolled_at',str(current['enrolled_at'])))
    return current


def stamp(d):return collect.parse_ts(d.get('timestamp')) or 0


def identity(tool,d,context,path):
    kind=d.get('type','unknown');p=d.get('payload') or {}
    if tool=='pi' and kind=='session':context.update(session=d.get('id'),cwd=d.get('cwd'),started=stamp(d))
    if tool=='codex' and kind=='session_meta':context.update(session=p.get('id') or p.get('session_id'),cwd=p.get('cwd'),started=stamp(d))
    if tool=='codex' and kind=='turn_context':context['cwd']=p.get('cwd') or context.get('cwd')
    if tool=='copilot':
        data=d.get('data') or {}
        if kind=='session.start':
            context.update(session=data.get('sessionId') or Path(path).parent.name,started=stamp(d),cwd=(data.get('context') or {}).get('cwd') or data.get('cwd'))
        elif kind=='session.context_changed':context['cwd']=data.get('cwd') or context.get('cwd')
        context.setdefault('session',Path(path).parent.name)
    if tool=='claude':
        sid=d.get('sessionId') or context.get('session') or Path(path).name.split('.jsonl')[0]
        if d.get('isSidechain') or '/subagents/' in str(path):sid=d.get('agentId') or Path(path).name.split('.jsonl')[0]
        context.update(session=sid,cwd=d.get('cwd') or context.get('cwd'))
    return context.get('session')


def safe_route(value):
    from . import routes
    if not isinstance(value,dict):return {}
    result={k:value[k] for k in ('provider','region','profile','billing_mode','account_id','account_provider','workspace_id','request_uid') if isinstance(value.get(k),str)}
    endpoint=routes.endpoint(value.get('base_url') or value.get('baseUrl'))
    if endpoint:result['base_url']=endpoint['transport']+'://'+endpoint['endpoint_host']+endpoint['endpoint_path']
    return result


def safe_event(tool,d):
    """Native transcript bodies, with metadata/auth envelopes explicitly limited."""
    allowed=('type','id','uuid','parentUuid','timestamp','sessionId','agentId','isSidechain','cwd','version',
             'role','content','message','payload','usage','provider','modelId','parentId','parentSession','stopReason','isError',
             'requestId','entrypoint','gitBranch','ordinal','isMeta','totalCostUSD','hasUnknownModelCost',
             'ownerAccountUuid','ownerOrganizationUuid','creator_account_id')
    out={k:d[k] for k in allowed if k in d}
    if isinstance(out.get('message'),dict):
        m=out['message'];out['message']={k:m[k] for k in ('id','role','content','usage','model','provider','timestamp','usageProvenance','inputStats','responseId','requestId','toolCallId','toolName','isError','stopReason') if k in m}
    if d.get('type')=='turn_context':
        payload=d.get('payload') or {};out['payload']={k:payload[k] for k in ('turn_id','cwd','model','effort','summary') if k in payload}
    if d.get('type')=='session_meta':
        p=d.get('payload') or {};out['payload']={k:p[k] for k in ('id','session_id','cwd','source','originator','model_provider','git','timestamp','parent_thread_id','thread_source','forked_from_id','cli_version','creator_account_id') if k in p}
        if isinstance(p.get('route'),dict):out['payload']['route']=safe_route(p['route'])
    if tool=='copilot' and isinstance(d.get('data'),dict):
        out['data']={k:v for k,v in d['data'].items() if k not in ('headers','authorization','apiKey','accessToken','refreshToken','credentials')}
    if tool=='claude' and d.get('type') in ('user','assistant'):out['isMeta']=bool(d.get('isMeta'))
    if isinstance(d.get('rateLimitInfo'),dict):out['rateLimitInfo']={k:v for k,v in d['rateLimitInfo'].items() if k in ('rateLimitType','status','resetsAt','usedPercent','utilization','overageStatus','overageResetsAt','overageDisabledReason') and isinstance(v,(str,int,float,bool))}
    if d.get('type')=='erg_route' and isinstance(d.get('route'),dict):out['route']=safe_route(d['route'])
    # Top-level credentials and HTTP headers never enter history. Content itself
    # is intentionally preserved: the operator explicitly enabled private bodies.
    return out


def projection_hint(tool,kind,body):
    payload=body.get('payload') if isinstance(body,dict) else None
    if tool=='codex' and isinstance(payload,dict):
        if (kind=='response_item' and (payload.get('type')=='reasoning' or (payload.get('type')=='message' and payload.get('role')=='assistant'))
            or kind=='event_msg' and payload.get('type') in ('token_count','agent_message','agent_reasoning','item_completed','thread_settings_applied','task_started','task_complete')):
            return 'codex_work_noop'
    if tool=='codex' and kind=='response_item' and isinstance(payload,dict) and payload.get('type') in ('custom_tool_call','function_call'):
        text=payload.get('input') or payload.get('arguments') or ''
        if isinstance(text,str) and 'Begin Patch' in text:return 'codex_patch'
    return ''


def put(db,tool,session,project,ts,kind,body,entry_id,role='',origin='native',source='',machine=None,source_offset=None,source_inode=None,source_ts=None,event_id=None):
    machine=machine or collect.machine_name()
    key=event_id or hashlib.sha256(json.dumps([machine,tool,session,entry_id],separators=(',',':')).encode()).hexdigest()
    encoded=json.dumps(body,ensure_ascii=False,separators=(',',':')).encode();digest=hashlib.sha256(encoded).hexdigest()
    previous=db.execute('SELECT content_hash,expired_at FROM history_events WHERE event_id=?',(key,)).fetchone()
    if previous and not previous[1] and previous[0]==digest and source_offset is not None:
        db.execute('UPDATE history_events SET source=?,source_offset=?,source_inode=?,source_ts=? WHERE event_id=? AND source_offset IS NULL',(source,source_offset,source_inode,source_ts,key))
    if previous and (previous[1] or previous[0]==digest):return key,False
    if previous and isinstance(body,dict) and isinstance(body.get('message'),dict) and 'content' not in body['message']:
        source_hash=digest
        cached=db.execute('SELECT content_hash,source_hash FROM history_metadata_mirrors WHERE event_id=?',(key,)).fetchone()
        if cached==(previous[0],source_hash):return key,False
        stored=db.execute('SELECT body FROM history_events WHERE event_id=?',(key,)).fetchone()
        old=json.loads(zlib.decompress(stored[0])) if stored and stored[0] else {}
        if isinstance(old.get('message'),dict) and 'content' in old['message']:
            # Lifecycle summaries enrich a retained native envelope. They must
            # not erase parent/tool/stop metadata or itemized native usage merely
            # because the smaller mirror omits those fields.
            message=dict(old['message'],**body['message'])
            previous_usage=old['message'].get('usage');incoming_usage=body['message'].get('usage')
            if isinstance(previous_usage,dict) and isinstance(incoming_usage,dict):
                usage=dict(previous_usage,**incoming_usage)
                if isinstance(previous_usage.get('cost'),dict) and isinstance(incoming_usage.get('cost'),dict):
                    usage['cost']=dict(previous_usage['cost'],**incoming_usage['cost'])
                message['usage']=usage
            body=dict(old,**body);body['message']=message
            encoded=json.dumps(body,ensure_ascii=False,separators=(',',':')).encode();digest=hashlib.sha256(encoded).hexdigest()
            # The hint is valid only for this exact canonical body. Native body
            # changes invalidate it without rewriting or deleting another BLOB.
            db.execute('INSERT INTO history_metadata_mirrors VALUES(?,?,?) ON CONFLICT(event_id) DO UPDATE SET content_hash=excluded.content_hash,source_hash=excluded.source_hash',(key,digest,source_hash))
    if previous and previous[0]==digest:return key,False
    db.execute("""INSERT INTO history_events(event_id,machine,tool,session,project,ts,kind,role,origin,source,body,content_hash,bytes,capture_version,projection_hint,source_offset,source_inode,source_ts)
     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET body=excluded.body,content_hash=excluded.content_hash,
     bytes=excluded.bytes,role=excluded.role,origin=excluded.origin,capture_version=excluded.capture_version,projection_hint=excluded.projection_hint,
     ts=CASE WHEN excluded.origin='native_projection' THEN MAX(history_events.ts,excluded.ts) ELSE history_events.ts END""",
     (key,machine,tool,session,project,int(ts),kind,role,origin,source,zlib.compress(encoded,1),digest,len(encoded),2,projection_hint(tool,kind,body),source_offset,source_inode,source_ts))
    project_context(db,machine,tool,session,ts,key,body)
    return key,True


def project_context(db,machine,tool,session,ts,uid,body):
    from . import projects
    if not isinstance(body,dict):return
    value=body.get('payload') if tool=='codex' else body
    if not isinstance(value,dict):return
    repo=(value.get('git') or {}).get('repository_url') if isinstance(value.get('git'),dict) else None
    try:repo=projects.repository(repo) if repo else ''
    except ValueError:repo=''
    projects.observe_context(db,machine,tool,session,ts,uid,value.get('cwd'),repo)


def index_project_contexts(db):
    """Index metadata from already-retained events, without importing source history."""
    changed=0
    with db:
        for machine,tool,sid,ts,uid,body in db.execute("SELECT machine,tool,session,ts,event_id,body FROM history_events WHERE body IS NOT NULL AND (kind IN ('session','session_meta','turn_context') OR tool='claude')"):
            project_context(db,machine,tool,sid,ts,uid,json.loads(zlib.decompress(body)));changed+=1
    return dict(examined=changed,scope='Retained local metadata only')


def _header(tool,path):
    context={}
    try:
        with collect.transcript(path) as f:
            for i,line in enumerate(f):
                try:d=json.loads(line)
                except ValueError:continue
                identity(tool,d,context,path)
                if context.get('session') or i>=31:break
    except (OSError,RuntimeError):pass
    return context


_UNREAD=object()
HEARTBEAT_INTERVAL=60


def scan_file(db,tool,path,enrolled,limit=1000,previous=_UNREAD,touches=None,stats=None):
    stat=os.stat(path);inode=str(stat.st_dev)+':'+str(stat.st_ino);now=int(time.time())
    if previous is _UNREAD:
        previous=db.execute('SELECT inode,offset,context,size,mtime,events,errors,last_seen FROM history_sources WHERE path=?',(str(path),)).fetchone()
    if previous and previous[0]==inode and previous[3]==stat.st_size and previous[4]==stat.st_mtime:
        if stats is not None:
            stats['unchanged_files']+=1;stats['source_errors']+=previous[6] or 0
        if now-(previous[7] or 0)>=HEARTBEAT_INTERVAL:
            if touches is not None:touches.append((now,str(path)))
            else:
                with db:db.execute('UPDATE history_sources SET last_seen=? WHERE path=?',(now,str(path)))
        return 0
    context=json.loads(previous[2]) if previous and previous[2] else {}
    compressed=str(path).endswith('.zst')
    offset=previous[1] if previous and previous[0]==inode and (compressed or previous[1]<=stat.st_size) else 0
    events=previous[5] if previous else 0;errors=previous[6] if previous else 0
    # Baseline pre-enrollment files without reading their content into the corpus.
    baseline=not previous and stat.st_mtime<enrolled
    if baseline:offset=0 if compressed else stat.st_size
    changed=0;state='baseline' if baseline else 'captured';seen=0
    if not baseline:
        if not context.get('session'):context=_header(tool,path)
        stream=collect.transcript(path) if compressed else open(path,'r',encoding='utf-8')
        with stream as f:
            if not compressed:f.seek(offset)
            else:
                for _ in range(offset):
                    if not f.readline():break
            with db:
                while seen<limit:
                    start=offset;line=f.readline(32*1024*1024)
                    if not line:break
                    if not line.endswith(b'\n' if isinstance(line,bytes) else '\n'):
                        state='partial_record';break
                    seen+=1
                    offset=offset+1 if compressed else f.tell()
                    try:d=json.loads(line)
                    except ValueError:
                        errors+=1;state='parse_error';continue
                    if not isinstance(d,dict):errors+=1;state='parse_error';continue
                    sid=identity(tool,d,context,path)
                    if not sid:state='session_identity_missing';continue
                    from . import titles
                    titles.native(db,tool,sid,d)
                    ts=stamp(d)
                    # Old snapshots may contain entire historical sessions. Capture
                    # only post-enrollment entries, preserving new-session context.
                    if ts<enrolled and (context.get('started') or 0)<enrolled:continue
                    safe=safe_event(tool,d);kind=d.get('type') or 'unknown';p=d.get('payload') or {};m=d.get('message') or {}
                    role=m.get('role') or p.get('role') or d.get('role') or ('user' if tool=='copilot' and kind=='user.message' else 'assistant' if tool=='copilot' and kind=='assistant.message' else '')
                    entry=d.get('id') or d.get('uuid')
                    # Content identity deduplicates no-ID Codex events across copied
                    # native/archived files; call IDs alone are not tool-result IDs.
                    if not entry:entry=hashlib.sha256(json.dumps(safe,sort_keys=True).encode()).hexdigest()
                    origin='synthetic_history' if m.get('timestamp')==0 or m.get('usageProvenance')=='synthetic_history' else 'native'
                    _,fresh=put(db,tool,sid,collect.project_of(context.get('cwd')),ts or now,kind,safe,str(entry),role,origin,str(path),source_offset=start,source_inode=stat.st_ino,source_ts=ts)
                    changed+=fresh;events+=fresh
                if seen==limit:state='pending'
                db.execute('''INSERT INTO history_sources VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
                 inode=excluded.inode,offset=excluded.offset,context=excluded.context,size=excluded.size,mtime=excluded.mtime,
                 last_seen=excluded.last_seen,events=excluded.events,errors=excluded.errors,state=excluded.state''',
                 (str(path),tool,inode,offset,json.dumps(context),stat.st_size if state!='pending' else -1,stat.st_mtime,now,now,events,errors,state))
                if stats is not None:stats['source_errors']+=errors
                return changed
    with db:db.execute('INSERT OR REPLACE INTO history_sources VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (str(path),tool,inode,offset,json.dumps(context),stat.st_size,stat.st_mtime,now,now,events,errors,state))
    return changed


def scan(db,budget=15,paths=None,scan_sqlite=True,projection_budget=None,project_work=True,project_usage=True):
    settings=config()
    if not settings.get('content'):return dict(enabled=False,events=0,errors=0)
    started=time.monotonic();cpu_started=time.thread_time()
    enrolled=int(settings['enrolled_at']);deadline=started+budget;changed=errors=0
    stats=dict(unchanged_files=0,source_errors=0);touches=[]
    full=paths is None
    discovery_errors=[];discovery_error_count=0;inventory=None
    def discovery_error(tool,path,operation,number):
        nonlocal discovery_error_count
        discovery_error_count+=1
        if len(discovery_errors)<100:discovery_errors.append(dict(tool=tool,path=path,state='discovery_error',operation=operation,errno=number))
    if full:
        # A cursor rotates full reconciliation so large histories cannot starve Pi.
        paths=[]
        for tool,roots in [('pi',collect.pi_roots()+[str(Path(collect.STATE_DIR,'pi-content-outbox'))]),('claude',collect.claude_roots()),('codex',collect.native_roots('codex')),('copilot',[str(Path(collect.HOME,'.copilot','session-state'))])]:
            paths.extend((tool,p) for p in collect.log_paths(roots,on_error=lambda path,operation,number,tool=tool:discovery_error(tool,path,operation,number)))
    paths=list(dict.fromkeys(paths))
    inventory_seconds=time.monotonic()-started
    query='SELECT path,inode,offset,context,size,mtime,events,errors,last_seen FROM history_sources'
    # Targeted event batches need only their own progress, not the entire inventory.
    if not full and paths:
        rows=db.execute(query+' WHERE path IN ('+','.join('?' for _ in paths)+')',[p for _,p in paths])
    elif full:rows=db.execute(query)
    else:rows=()
    previous={row[0]:row[1:] for row in rows}
    saved=db.execute("SELECT value FROM history_config WHERE key='scan_cursor'").fetchone() if full else None
    cursor=int(saved[0]) if saved else 0
    checked=0
    for i in range(len(paths)):
        if time.monotonic()>deadline:break
        tool,path=paths[(cursor+i)%len(paths)]
        checked+=1
        try:changed+=scan_file(db,tool,path,enrolled,previous=previous.get(str(path)),touches=touches,stats=stats)
        except (OSError,ValueError,RuntimeError):
            errors+=1
            with db:
                db.execute('''INSERT INTO history_sources(path,tool,first_seen,last_seen,events,errors,state)
                 VALUES(?,?,?,?,0,1,'source_error') ON CONFLICT(path) DO UPDATE SET
                 state='source_error',errors=coalesce(errors,0)+1,last_seen=excluded.last_seen''',
                 (str(path),tool,int(time.time()),int(time.time())))
    if touches:
        with db:db.executemany('UPDATE history_sources SET last_seen=? WHERE path=?',touches)
    from . import history_opencode
    sqlite_result=history_opencode.scan(db,enrolled) if scan_sqlite else dict(events=0,errors=0,files=0)
    changed+=sqlite_result['events'];errors+=sqlite_result['errors']
    consume_outbox(db)
    from . import history_projection
    projection=(history_projection.run(db) if projection_budget is None else history_projection.run(db,limit=128,budget=projection_budget,cpu_budget=True)) if project_usage else dict(processed=0,errors=0,pending=db.execute('SELECT count(*) FROM history_pending').fetchone()[0],mode='independent_worker')
    from . import edit_projection
    edit_projection_result=edit_projection.run(db) if project_usage else dict(processed=0,errors=0,pending=db.execute('SELECT count(*) FROM history_edit_pending').fetchone()[0],mode='independent_worker')
    from . import work_projection
    work_projection_result=work_projection.run(db) if project_work else dict(processed=0,errors=0,pending=db.execute('SELECT count(*) FROM history_work_pending').fetchone()[0],mode='independent_worker')
    from . import project_rollups
    project_rollups.capture_progress(db,projection,work_projection_result)
    link_inferences(db)
    if full:
        from . import store
        from . import source_audit
        audit_paths=list(paths)
        for root in collect.opencode_roots():
            path=Path(root)/'opencode.db'
            if path.exists():audit_paths.append(('opencode',str(path)))
        inventory=source_audit.record(db,paths=audit_paths,discovery_errors=discovery_errors,discovery_error_count=discovery_error_count,started_at=int(time.time()-(time.monotonic()-started)))
        source_rows=list(map(json.dumps,collect.retained_source_rows(db)));machine=collect.machine_name()
        with db:
            db.execute('INSERT OR REPLACE INTO history_config VALUES(?,?)',('scan_cursor',str((cursor+checked)%len(paths) if paths else 0)))
            store.ingest(db,source_rows,machine=machine,transaction=False)
    return dict(enabled=True,events=changed,errors=errors+stats['source_errors'],source_error_count=db.execute('SELECT coalesce(sum(errors),0) FROM history_sources WHERE errors>0').fetchone()[0],files=len(paths)+sqlite_result['files'],projection=projection,edit_projection=edit_projection_result,work_projection=work_projection_result,
                source_inventory=inventory,reconciled=full,deferred_paths=paths[checked:] if not full else [],checked_files=checked,unchanged_files=stats['unchanged_files'],heartbeat_writes=len(touches),
                inventory_seconds=round(inventory_seconds,4),scan_seconds=round(time.monotonic()-started,4),cpu_seconds=round(time.thread_time()-cpu_started,4))


def overview(db):
    enabled=config().get('content',False)
    states=dict(db.execute('SELECT state,count(*) FROM history_sources GROUP BY state'))
    bounds=db.execute('SELECT count(*),sum(bytes),min(ts),max(ts) FROM history_events WHERE expired_at IS NULL').fetchone()
    from . import history_projection,work_projection
    from . import source_audit
    return dict(source_inventory=source_audit.status(db),work_projection=work_projection.status(db),enabled=enabled,enrolled_at=config().get('enrolled_at'),source_states=states,events=bounds[0],projection=history_projection.status(db),
      source_errors=db.execute('SELECT coalesce(sum(errors),0) FROM history_sources').fetchone()[0],
      sources_needing_attention=db.execute("SELECT count(*) FROM history_sources WHERE coalesce(errors,0)>0 OR state NOT IN ('baseline','captured','consumed')").fetchone()[0],
      pending_inferences=db.execute("SELECT count(*) FROM local_inferences WHERE state<>'complete'").fetchone()[0],
      endpoint_enrollments=[{k:e.get(k) for k in ('name','upstream','port')} for e in config().get('endpoints',[])],
      projects=[r[0] for r in db.execute('SELECT DISTINCT project FROM history_events ORDER BY project LIMIT 1000')],
      original_bytes=bounds[1] or 0,first_event=bounds[2],last_event=bounds[3],
      scope='Discovered sources from enrollment onward; bypassed endpoints and unknown roots are not certified',
      unmatched_sessions=db.execute('''SELECT count(*) FROM report_sessions s WHERE s.last_ts>=? AND NOT EXISTS(
       SELECT 1 FROM history_events h WHERE h.tool=s.tool AND h.session=s.session)''',(config().get('enrolled_at') or int(time.time()),)).fetchone()[0])


def sessions(db,tool='',session='',project='',start=0,end=0,after=0,limit=50):
    limit=max(1,min(int(limit),100));where=['1=1'];params=[]
    for col,value in [('tool',tool),('session',session),('project',project)]:
        if value:where.append(col+'=?');params.append(value)
    if start:where.append('ts>=?');params.append(int(start))
    if end:where.append('ts<=?');params.append(int(end))
    where.append("NOT (tool='local' AND session IN (SELECT local_session FROM history_links))")
    rows=db.execute('''SELECT tool,session,project,min(ts),max(ts),count(*),max(seq),sum(CASE WHEN expired_at IS NOT NULL THEN 1 ELSE 0 END)
      FROM history_events WHERE '''+' AND '.join(where)+' GROUP BY tool,session,project HAVING max(seq)>? ORDER BY max(seq) LIMIT ?',params+[int(after),limit+1]).fetchall()
    more=len(rows)>limit;rows=rows[:limit]
    from . import titles
    names={(r['tool'],r['session']):r['title'] for r in titles.rows(db)}
    return dict(sessions=[dict(dict(zip(('tool','session','project','first_ts','last_ts','events','cursor','expired_events'),r)),title=names.get((r[0],r[1]))) for r in rows],
                more=more,next_cursor=rows[-1][6] if more else None,limit=limit)


def scoped_sessions(db,filters=None,after=0,limit=50,through_seq=None):
    """Browse retained metadata using the universal hierarchy, never session bodies.

    A high-water seq pins pagination against concurrent appends. One native
    session is one row even if its captured context spans several folders.
    Event counts are physical records, not additive across split human projects.
    """
    owned=not db.in_transaction
    if owned:db.execute('BEGIN')
    try:return _scoped_sessions(db,filters,after,limit,through_seq)
    finally:
        if owned:db.rollback()


def _history_metadata(db,selection,upper):
    """Push identity filters into indexed native/local branches, then compact
    records sharing the exact inputs to temporal project allocation. Bodies are
    never selected; no new on-disk projection or duplicate event store is needed.
    """
    v=selection.values
    clock='e.ts>=? AND e.ts<? AND e.seq<=?'
    bounds=[v['start'],v['end'],upper]
    def identity(alias):
        clauses=[];params=[]
        for name in ('tool','session'):
            if v[name]:clauses.append(alias+'.'+name+'=?');params.append(v[name])
        if selection.tools is not None:
            if selection.tools:
                tools=sorted(selection.tools);clauses.append(alias+'.tool IN ('+','.join('?' for _ in tools)+')');params.extend(tools)
            else:clauses.append('0')
        return clauses,params
    native,params=identity('e');linked,link_params=identity('l')
    machine=['e.machine=?'] if v['machine'] else []
    machine_params=[v['machine']] if v['machine'] else []
    columns='e.seq,e.machine,e.tool,e.session,e.project,e.ts,e.expired_at'
    branches=['SELECT '+columns+' FROM history_events e WHERE '+clock+''.join(' AND '+c for c in machine+native)+" AND NOT(e.tool='local' AND EXISTS(SELECT 1 FROM history_links l WHERE l.local_session=e.session))",
              'SELECT '+columns+" FROM history_events e JOIN history_links l ON l.local_session=e.session WHERE e.tool='local' AND "+clock+''.join(' AND '+c for c in machine+linked)]
    query='SELECT MAX(seq),machine,tool,session,project,ts,COUNT(*),SUM(expired_at IS NOT NULL) FROM ('+' UNION ALL '.join(branches)+') GROUP BY machine,tool,session,project,ts'
    return db.execute(query,bounds+machine_params+params+bounds+machine_params+link_params)


def _allocated_metadata(db,selection,upper,hierarchy,links):
    # Allocation is constant until a recorded context or binding validity edge.
    # Cache only within this pinned read; retain exact physical counts/timestamps.
    from bisect import bisect_right
    policy_edges=sorted({edge for rule in hierarchy.config['bindings'] if rule['enabled']
                         for edge in (rule['valid_from'],rule['valid_to']) if edge is not None})
    allocation_cache={}
    for seq,machine,tool,sid,project,ts,count,expired in _history_metadata(db,selection,upper):
        if tool=='local' and sid in links:tool,sid=links[sid]
        row=dict(machine=machine,tool=tool,session=sid,project=project,ts=ts)
        if not selection.accepts(row):continue
        key=(machine,tool,sid)
        epoch=tuple(bisect_right(hierarchy.contexts[source].get(key,([],[]))[0],ts)
                    for source in ('native','work'))
        allocation_key=(key,project,epoch,bisect_right(policy_edges,ts))
        resolved=allocation_cache.get(allocation_key)
        if resolved is None:
            resolved=hierarchy.weight(row,selection.values['project_key'],selection.values['folder_key'])
            if len(allocation_cache)>=4096:allocation_cache.pop(next(iter(allocation_cache)))
            allocation_cache[allocation_key]=resolved
        units,allocation=resolved
        if not units:continue
        yield dict(seq=seq,machine=machine,tool=tool,session=sid,project=project,ts=ts,last_ts=ts,count=count,expired=expired),allocation


def _scoped_sessions(db,filters,after,limit,through_seq):
    from . import project_scope,report_scope
    selection=report_scope.Selection(filters)
    after=int(after);limit=max(1,min(int(limit),100))
    upper=db.execute('SELECT coalesce(max(seq),0) FROM history_events').fetchone()[0] if through_seq is None else int(through_seq)
    if not 0<=after<=2**63-1 or not 0<=upper<=2**63-1:raise ValueError('Invalid history cursor')
    hierarchy=project_scope.Scope(db,selection.values['end'])
    target=selection.values['project_key']
    if target and target!='@unassigned' and target not in hierarchy.catalog:raise ValueError('Unknown human project')
    links={r[0]:(r[1],r[2]) for r in db.execute('SELECT local_session,tool,session FROM history_links')}
    grouped={}
    bounds=(selection.values['start'],selection.values['end'],upper)
    # The tool picker describes all retained tools in the time window, even when
    # a session/tool filter narrows the rows. This query uses the covering time index.
    observed_tools={r[0] for r in db.execute('SELECT DISTINCT tool FROM history_events WHERE ts>=? AND ts<? AND seq<=?',bounds)}
    if 'local' in observed_tools:
        observed_tools.remove('local')
        for sid, in db.execute("SELECT DISTINCT session FROM history_events WHERE tool='local' AND ts>=? AND ts<? AND seq<=?",bounds):
            observed_tools.add(links.get(sid,('local',sid))[0])
    from . import history_browse
    browse_stats=dict(indexed_hours=0,raw_hours=0)
    for row,allocation in history_browse.metadata(db,selection,upper,hierarchy,links,browse_stats):
        seq,machine,tool,sid,ts,count,expired=(row[k] for k in ('seq','machine','tool','session','ts','count','expired'))
        key=(machine,tool,sid)
        item=grouped.setdefault(key,dict(machine=machine,tool=tool,session=sid,first_ts=ts,last_ts=row['last_ts'],events=0,expired_events=0,cursor=seq,folder_keys=set(),project_keys=set()))
        item['first_ts']=min(item['first_ts'],ts);item['last_ts']=max(item['last_ts'],row['last_ts']);item['events']+=count;item['expired_events']+=expired;item['cursor']=max(item['cursor'],seq)
        item['folder_keys'].add(allocation['folder']['folder_key']);item['project_keys'].update(p['project_key'] for p in allocation['allocations'])
    rows=sorted((r for r in grouped.values() if r['cursor']>after),key=lambda r:(r['cursor'],r['machine'],r['tool'],r['session']))
    more=len(rows)>limit;rows=rows[:limit]
    names={(r[0],r[1],r[2]):r[3] for r in db.execute('SELECT machine,tool,session,title FROM session_titles')}
    for row in rows:
        row['title']=names.get((row['machine'],row['tool'],row['session']))
        row['folder_keys']=sorted(row['folder_keys']);row['project_keys']=sorted(row['project_keys'])
        row['folder_names']=[hierarchy.folders[key]['name'] for key in row['folder_keys']]
        row['project']='; '.join(row['folder_names'])
    return dict(browse_index=browse_stats,observed_tools=sorted(observed_tools),sessions=rows,more=more,next_cursor=rows[-1]['cursor'] if more else None,through_seq=upper,limit=limit,project_catalog=hierarchy.config['projects'],folder_catalog=sorted(hierarchy.folders.values(),key=lambda f:f['folder_key']),project_revision=hierarchy.config['revision'],scope=selection.values,
                count_basis='Physical matching records and distinct native sessions; split-project counts are not additive; opening a session shows full retained history')


def events(db,tool,session,after=0,limit=50):
    limit=max(1,min(int(limit),200))
    rows=db.execute("SELECT seq,ts,kind,role,origin,body,expired_at FROM history_events WHERE ((tool=? AND session=?) OR (tool='local' AND session IN (SELECT local_session FROM history_links WHERE tool=? AND session=?))) AND seq>? ORDER BY seq LIMIT ?",
      (tool,session,tool,session,int(after),limit+1)).fetchall();more=len(rows)>limit;rows=rows[:limit]
    return dict(events=[dict(seq=r[0],ts=r[1],kind=r[2],role=r[3],origin=r[4],expired_at=r[6],body=json.loads(zlib.decompress(r[5])) if r[5] else None) for r in rows],
                more=more,next_cursor=rows[-1][0] if more else None)


def expire(db,before,apply=False):
    """Preview/expire only fully projected and centrally materialized content."""
    delivery='0'
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_journal'").fetchone():
        delivery="""EXISTS(SELECT 1 FROM delivery_journal j WHERE j.event_id=e.event_id AND j.body IS NOT NULL
          AND (j.prepared<>1 OR EXISTS(SELECT 1 FROM delivery_items i WHERE i.journal_id=j.id AND i.materialized=0)))"""
    pending=' OR '.join('EXISTS(SELECT 1 FROM '+table+' p WHERE p.event_id=e.event_id)' for table in
                        ('history_pending','history_edit_pending','history_work_pending'))+' OR '+delivery
    def inventory():
        return db.execute('SELECT count(*),coalesce(sum(bytes),0),coalesce(sum(CASE WHEN '+pending+
                          ' THEN 1 ELSE 0 END),0) FROM history_events e WHERE ts<? AND expired_at IS NULL', (int(before),)).fetchone()
    expired=0
    if apply:
        with db:
            if not db.in_transaction:db.execute('BEGIN IMMEDIATE')
            n,size,protected=inventory()
            expired=db.execute('UPDATE history_events AS e SET body=NULL,expired_at=? WHERE ts<? AND expired_at IS NULL AND NOT ('+pending+')',
                               (int(time.time()),int(before))).rowcount
    else:n,size,protected=inventory()
    return dict(events=n,uncompressed_bytes=size,protected_events=protected,expired_events=expired,applied=bool(apply),before=int(before))


def reconciliation(db,limit=200):
    """Coverage and canonical usage must describe the same ledger snapshot."""
    owned=not db.in_transaction
    if owned:db.execute('BEGIN')
    try:return _reconciliation(db,limit)
    finally:
        if owned:db.rollback()


def _reconciliation(db,limit=200):
    since=config().get('enrolled_at') or int(time.time())
    # A linked server receipt is evidence for the native session, not a second
    # unmatched session. Keep raw events intact while reconciling the read model.
    links={r[0]:(r[1],r[2]) for r in db.execute('SELECT local_session,tool,session FROM history_links')}
    hist={}; source_sessions={}; problems=[]
    for tool,sid,n,first,last,content,expired in db.execute("""SELECT tool,session,count(*),min(ts),max(ts),
     sum(CASE WHEN role<>'' OR kind IN ('response_item','inference_request','inference_response','tool.execution_start','tool.execution_complete','sqlite_record') THEN 1 ELSE 0 END),
     sum(CASE WHEN expired_at IS NOT NULL THEN 1 ELSE 0 END) FROM history_events GROUP BY tool,session"""):
        key=links.get(sid,(tool,sid)) if tool=='local' else (tool,sid)
        item=hist.setdefault(key,dict(events=0,content_events=0,expired_events=0,first_ts=first,last_ts=last))
        item['events']+=n;item['content_events']+=content;item['expired_events']+=expired
        item['first_ts']=min(first,item['first_ts']);item['last_ts']=max(last,item['last_ts'])
    event_sources={}
    for source,tool,sid in db.execute('SELECT DISTINCT source,tool,session FROM history_events'):
        event_sources.setdefault(source,set()).add((tool,sid))
    for path,tool,context,offset,size,state,errors,last_seen in db.execute('SELECT path,tool,context,offset,size,state,errors,last_seen FROM history_sources'):
        ctx=json.loads(context) if context else {};sid=ctx.get('session')
        problem=bool(errors or state not in ('baseline','captured','consumed'))
        if problem:problems.append(dict(tool=tool,path=path,state=state,errors=errors or 0,offset=offset,size=size,last_seen=last_seen))
        keys={(tool,sid)} if sid else event_sources.get(path,set())
        for source_tool,source_sid in keys:
            key=links.get(source_sid,(source_tool,source_sid)) if source_tool=='local' else (source_tool,source_sid)
            source_sessions.setdefault(key,[]).append(dict(state=state,errors=errors or 0,checkpoint_current=not problem))
    from . import report_sqlite
    relation=report_sqlite.calls_select(['tool','session','ts'],db=db)
    usage={(r[0],r[1]):r[2] for r in db.execute("SELECT tool,session,count(*) FROM ("+relation+") WHERE ts>=? GROUP BY tool,session",(since,))}
    found={}
    for tool,sid,project,last,usage_status,orchestrator in db.execute('SELECT tool,session,project,last_ts,usage_status,orchestrator FROM report_sessions WHERE last_ts>=?',(since,)):
        found[(tool,sid)]=dict(tool=tool,session=sid,project=project,last_ts=last,orchestrator=orchestrator,usage_status=usage_status)
    for key,item in hist.items():
        found.setdefault(key,dict(tool=key[0],session=key[1],last_ts=item['last_ts'],usage_status='not_observed'))
    for key,item in found.items():
        h=hist.get(key,{});sources=source_sessions.get(key,[])
        item.update(archived_events=h.get('events',0),content_events=h.get('content_events',0),expired_events=h.get('expired_events',0),metered_calls=usage.get(key,0),source_checkpoints=sources)
        if any(not x['checkpoint_current'] for x in sources):state='source_needs_attention'
        elif not h:state='history_not_observed_since_enrollment'
        elif not h['content_events']:state='metadata_only'
        elif h['expired_events']:state='archived_with_expired_content'
        else:state='archived'
        item['history_state']=state
    reasons=collections.Counter(r['history_state'] for r in found.values())
    from . import source_audit
    return dict(source_inventory=source_audit.status(db),enrolled_at=since,sessions=sorted(found.values(),key=lambda r:r['last_ts'],reverse=True)[:limit],
                total=len(found),limit=limit,counts=dict(reasons),source_problems=problems[:limit],source_problem_count=len(problems),
                adapter_coverage=[dict(tool=r[0],executable_on_collector_path=bool(r[1]),source_data_observed=bool(r[4]),usage_status=r[2],last_inventory=r[3],history_adapter=('sqlite_projection' if r[0]=='opencode' else 'native_jsonl' if r[0] in ('claude','codex','pi','copilot') else 'linked_metadata' if r[0]=='omnigent' else 'not_implemented'),reason=('Read-only mutable SQLite projections; intermediate unobserved mutations are unknown' if r[0]=='opencode' else 'Native polling plus durable lifecycle outbox' if r[0]=='pi' else 'Native persisted event polling; no missing usage inferred' if r[0] in ('claude','codex','copilot') else 'Orchestrator metadata links to native traces' if r[0]=='omnigent' else 'Usage adapter exists; full-history archiving is not implemented')) for r in db.execute('SELECT tool,installed,status,ts,files FROM sources')],
                unsupported_sources=[dict(tool=r[0],status=r[1],errors=r[2]) for r in db.execute("SELECT tool,status,errors FROM sources WHERE status='partial' OR errors>0")],
                proof_scope='Archived means records exist; checkpoint health covers discovered sources only and does not prove every external producer persisted every event.',
                bypass_warning='Local clients bypassing enrolled capture listeners have no server receipt; endpoint configuration is not proof that every client uses it.')


def inference_rows(db,after=0,limit=100):
    """Owned Unity-inference-compatible rows plus namespaced provenance extensions."""
    import datetime
    fields=[r[1] for r in db.execute('PRAGMA table_info(local_inferences)')]
    rows=db.execute('SELECT * FROM local_inferences WHERE event_time>=? ORDER BY event_time,invocation_id LIMIT ?',(int(after),min(int(limit),500))).fetchall()
    result=[]
    for values in rows:
        r=dict(zip(fields,values));payloads=[];expired=False
        for key in ('request_event_id','response_event_id'):
            h=db.execute('SELECT body,expired_at FROM history_events WHERE event_id=?',(r[key],)).fetchone()
            expired|=bool(h and h[1]);payloads.append(json.loads(zlib.decompress(h[0])) if h and h[0] else None)
        result.append(dict(request_id='erg:'+r['request_id'],invocation_id=r['invocation_id'],
          request_tags={'erg.session_id':r['session'],'erg.project':r['project']},
          event_time=datetime.datetime.fromtimestamp(r['event_time'],datetime.timezone.utc).isoformat(),
          status_code=r['status_code'],sampling_fraction=1.0,latency_ms=r['execution_duration_ms'],
          time_to_first_byte_ms=r['time_to_first_byte_ms'],request=json.dumps(payloads[0]) if payloads[0] is not None else None,
          response=payloads[1].get('content') if payloads[1] else None,destination_type='ERG_LOCAL_MODEL',
          destination_name=r['destination'],destination_model=r['model'],requester=None,schema_version='erg-inference-v1',
          logging_error_codes=(['ERG_CONTENT_EXPIRED'] if expired else [])+([r['error']] if r['error'] else []),
          erg={'native_response_id':r['response_id'],'session_id':r['session'],'project':r['project'],
               'usage_known':bool(r['usage_known']),'fresh_input_tokens':r['input_tokens'],
               'output_tokens':r['output_tokens'],'cache_read_tokens':r['cache_read'],'cache_write_tokens':r['cache_write'],'state':r['state']}))
    return result



def link_inferences(db):
    # Materialize receipts before writes, then use one short write transaction.
    # An active read cursor must not span commits from another capture worker.
    receipts=db.execute("SELECT session,response_id,model FROM local_inferences WHERE response_id IS NOT NULL AND response_id<>''").fetchall()
    if not receipts:return
    with db:
        if not db.in_transaction:db.execute('BEGIN IMMEDIATE')
        for local_session,response_id,model in receipts:
            matches=db.execute("SELECT DISTINCT tool,session FROM llm WHERE machine=? AND tool NOT IN ('local','gateway') AND response_id=? AND model=? AND response_id<>''",(collect.machine_name(),response_id,model)).fetchall()
            current=db.execute('SELECT tool,session,response_id FROM history_links WHERE local_session=?',(local_session,)).fetchone()
            if len(matches)==1:
                wanted=(matches[0][0],matches[0][1],response_id)
                if current!=wanted:db.execute('INSERT OR REPLACE INTO history_links VALUES(?,?,?,?,?)',(local_session,*wanted,int(time.time())))
            elif current:db.execute('DELETE FROM history_links WHERE local_session=?',(local_session,))



def consume_outbox(db):
    root=Path(collect.STATE_DIR,'pi-content-outbox').resolve()
    aliases={str(root),os.path.abspath(os.path.join(collect.STATE_DIR,'pi-content-outbox'))}
    predicates=[];params=[]
    for alias in aliases:predicates.append('(path>=? AND path<?)');params.extend((alias+os.sep,alias+'0'))
    for path, in db.execute("SELECT path FROM history_sources WHERE state='captured' AND errors=0 AND ("+' OR '.join(predicates)+')',params):
        p=Path(path)
        if p.parent.resolve()!=root:continue
        try:p.unlink()
        except FileNotFoundError:pass
        with db:db.execute("UPDATE history_sources SET state='consumed' WHERE path=?",(path,))
