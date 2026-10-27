"""Forward, checkpointed work evidence from retained bodies, without native reads.

Native work collection remains an independent source during parity verification.
Only absent compatible work facts are inserted. Changed work-bearing source data
is a persistent attribution conflict, never silently replayed into newer state.
"""
import contextlib
import hashlib
import json
import math
import time
import zlib
from . import collect,store,work,work_retained

SCHEMA='''
INSERT OR IGNORE INTO history_config(key,value) SELECT 'work_projection_start_seq_v1',CAST(coalesce(max(seq),0) AS TEXT) FROM history_events;
CREATE TABLE IF NOT EXISTS history_work_pending(event_id TEXT PRIMARY KEY,seq INT,content_hash TEXT,attempts INT DEFAULT 0,retry_at INT DEFAULT 0);
CREATE INDEX IF NOT EXISTS history_work_pending_order ON history_work_pending(seq);
CREATE INDEX IF NOT EXISTS history_work_pending_retry ON history_work_pending(retry_at) WHERE retry_at>0;
CREATE TABLE IF NOT EXISTS history_work_receipts(event_id TEXT PRIMARY KEY,content_hash TEXT,semantic_hash TEXT,state TEXT,reason TEXT,processed_at INT);
CREATE TABLE IF NOT EXISTS history_work_context(machine TEXT,tool TEXT,session TEXT,seq INT,value TEXT,PRIMARY KEY(machine,tool,session));
CREATE TABLE IF NOT EXISTS history_work_conflicts(machine TEXT,tool TEXT,session TEXT,reason TEXT,PRIMARY KEY(machine,tool,session));
CREATE TRIGGER IF NOT EXISTS history_work_enqueue_insert AFTER INSERT ON history_events
 WHEN NEW.body IS NOT NULL AND COALESCE(NEW.projection_hint,'')<>'codex_work_noop' AND ((NEW.tool='claude' AND NEW.kind IN ('user','assistant')) OR (NEW.tool='codex' AND NEW.kind IN ('session_meta','turn_context','response_item','event_msg'))) BEGIN
 INSERT INTO history_work_pending(event_id,seq,content_hash) VALUES(NEW.event_id,NEW.seq,NEW.content_hash)
 ON CONFLICT(event_id) DO UPDATE SET seq=excluded.seq,content_hash=excluded.content_hash,attempts=0,retry_at=0; END;
CREATE TRIGGER IF NOT EXISTS history_work_enqueue_update AFTER UPDATE OF body,content_hash ON history_events
 WHEN NEW.body IS NOT NULL AND (COALESCE(NEW.projection_hint,'')<>'codex_work_noop' OR COALESCE(OLD.projection_hint,'')<>'codex_work_noop') AND ((NEW.tool='claude' AND NEW.kind IN ('user','assistant')) OR (NEW.tool='codex' AND NEW.kind IN ('session_meta','turn_context','response_item','event_msg'))) AND NEW.seq>CAST((SELECT value FROM history_config WHERE key='work_projection_start_seq_v1') AS INT) AND (OLD.content_hash<>NEW.content_hash OR OLD.body IS NULL) BEGIN
 INSERT INTO history_work_pending(event_id,seq,content_hash) VALUES(NEW.event_id,NEW.seq,NEW.content_hash)
 ON CONFLICT(event_id) DO UPDATE SET seq=excluded.seq,content_hash=excluded.content_hash,attempts=0,retry_at=0; END;
'''



COLUMNS='event_id seq machine tool session ts source capture_version content_hash expired_at present bytes'.split()
CANDIDATES_SQL=('SELECT '+','.join('e.body IS NOT NULL AS present' if c=='present' else 'e.'+c for c in COLUMNS)+',q.content_hash '
    'FROM history_work_pending q CROSS JOIN history_events e ON e.event_id=q.event_id '
    'WHERE q.retry_at<=? AND NOT EXISTS('
    'SELECT 1 FROM history_work_pending prior INDEXED BY history_work_pending_retry '
    'CROSS JOIN history_events previous ON previous.event_id=prior.event_id '
    'WHERE prior.retry_at>0 AND prior.retry_at>? AND prior.seq<q.seq '
    'AND previous.machine=e.machine AND previous.tool=e.tool AND previous.session=e.session) '
    'ORDER BY q.seq LIMIT ?')


def initialize(db):
    # Triggers cover only future inserts/changes: no historical corpus queue fill.
    obsolete=[]
    for name in ('history_work_enqueue_insert','history_work_enqueue_update'):
        old=db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",(name,)).fetchone()
        required=('codex_work_noop','OLD.projection_hint') if name.endswith('update') else ('codex_work_noop',)
        if old and any(marker not in old[0] for marker in required):obsolete.append(name)
    drops=''.join('DROP TRIGGER IF EXISTS '+name+';\n' for name in obsolete)
    db.executescript('BEGIN IMMEDIATE;\n'+drops+SCHEMA+'\nCOMMIT;')


def semantic_hash(tool,body):
    """Owner/quota/route metadata repairs cannot invalidate unrelated work facts."""
    value={k:body[k] for k in ('type','timestamp','sessionId','agentId','isSidechain','cwd','gitBranch','isMeta','ordinal') if k in body}
    if tool=='codex':
        payload=body.get('payload') or {}
        value['payload']={k:payload[k] for k in ('type','id','session_id','cwd','git','source','parent_thread_id','thread_source','model','call_id','name','arguments','input','output','role','content','internal_chat_message_metadata_passthrough','message','message_id') if k in payload}
    elif isinstance(body.get('message'),dict):
        value['message']={k:body['message'][k] for k in ('id','role','content') if k in body['message']}
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def observed_context(db,event,cwd,extra=()):
    key=hashlib.sha256((cwd or '').encode()).hexdigest()[:16]
    cursor=db.execute("SELECT repo,branch,head_sha,task_id,scope_id,ts FROM work_contexts WHERE machine=? AND tool=? AND session=? AND worktree=? AND ts<=? AND capture_version>=3 ORDER BY ts DESC LIMIT 32",(event['machine'],event['tool'],event['session'],key,event['ts']))
    rows=cursor.fetchall()
    if extra:
        rows+= [tuple(r.get(k,'') for k in ('repo','branch','head_sha','task_id','scope_id','ts')) for r in extra if (r['machine'],r['tool'],r['session'],r['worktree'])==(event['machine'],event['tool'],event['session'],key) and r['ts']<=event['ts']]
        rows=sorted(rows,key=lambda r:r[-1],reverse=True)[:32]
    if not rows:return None
    if len(rows)==32 and rows[-1][-1]==rows[0][-1]:return None
    latest=[r for r in rows if r[-1]==rows[0][-1]]
    if len({r[:-1] for r in latest})!=1:return None
    repo,branch,head,task,scope,_=latest[0]
    return dict(repo=repo,branch=branch,head_sha=head,task_id=task,scope_id=scope) if repo else None



def observed_repository(db,event,cwd,extra=()):
    key=hashlib.sha256((cwd or '').encode()).hexdigest()[:16]
    params=(event['machine'],event['tool'],event['session'],key,event['ts'])
    stamp=db.execute('SELECT max(ts) FROM work_contexts WHERE machine=? AND tool=? AND session=? AND worktree=? AND ts<=? AND capture_version>=3',params).fetchone()[0]
    staged=[r for r in extra if (r['machine'],r['tool'],r['session'],r['worktree'])==(event['machine'],event['tool'],event['session'],key) and r['ts']<=event['ts']]
    staged_stamp=max((r['ts'] for r in staged),default=None)
    if stamp is None and staged_stamp is None:return {}
    latest=max(t for t in (stamp,staged_stamp) if t is not None)
    repos=db.execute('SELECT DISTINCT repo FROM work_contexts WHERE machine=? AND tool=? AND session=? AND worktree=? AND ts=? AND capture_version>=3 LIMIT 2',params[:-1]+(latest,)).fetchall()
    repos=list({r[0] for r in repos}|{r['repo'] for r in staged if r['ts']==latest})
    return dict(repo=repos[0]) if len(repos)==1 and repos[0] else dict(ambiguous=True) if len(repos)>1 else {}


def plan(db,event,body,state,witnesses=None,staged=()):
    if not isinstance(body,dict):raise ValueError('invalid_work_body')
    if (event['capture_version'] or 0)<2:return [],'native_flags_not_retained'
    stamp=collect.parse_ts(body.get('timestamp'))
    if not stamp or stamp!=event['ts']:return [],'native_timestamp_not_retained'
    witnesses=witnesses if witnesses is not None else {}
    def witnessed(cwd):
        if cwd not in witnesses:witnesses[cwd]=(observed_context(db,event,cwd),observed_repository(db,event,cwd))
        return (observed_context(db,event,cwd,staged),observed_repository(db,event,cwd,staged)) if staged else witnesses[cwd]
    tool=event['tool'];sid=event['session'];payload=body.get('payload') or {}
    if state.get('_work_gap'):return [],'preceding_work_record_unavailable'
    if tool=='codex' and not state:
        if body.get('type')!='session_meta':return [],'session_header_not_projected'
        if not isinstance(payload,dict) or (payload.get('id') or payload.get('session_id'))!=sid:return [],'native_session_identity_conflict'
    if tool=='claude' and not state:
        cwd=body.get('cwd');context,repo=witnessed(cwd)
        if not repo:return [],'recorded_repository_context_unavailable'
        if context:
            state.update(session=sid,cwd=cwd,_native_cwd=cwd,**{k:v for k,v in context.items() if k!='scope_id'})
            state['_scope_id']=context['scope_id'];state['_scope_key']=json.dumps([sid,cwd,state['repo'],state['branch'],state['task_id']])
    def repository(cwd):return witnessed(cwd)[1]
    rows=work.record_events(tool,body,state,event['source'] or '',0,include_metadata=False,include_prompts=False,repo_context=repository)
    if state.get('session')!=sid:return [],'native_session_identity_conflict'
    rows=[r for r in rows if r['t'] in work_retained.KINDS]
    if any(r['t']=='work_context' and not r.get('repo') for r in rows):return [],'recorded_repository_context_unavailable'
    return rows,''


def status(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='history_work_pending'").fetchone():
        return dict(available=False,reason='Forward work projection has not been initialized')
    return dict(available=True,forward_start_seq=int(db.execute("SELECT value FROM history_config WHERE key='work_projection_start_seq_v1'").fetchone()[0]),pending=db.execute('SELECT count(*) FROM history_work_pending').fetchone()[0],
                retrying=db.execute('SELECT count(*) FROM history_work_pending WHERE attempts>0').fetchone()[0],
                states=dict(db.execute('SELECT state,count(*) FROM history_work_receipts GROUP BY state')),
                gaps=dict(db.execute("SELECT reason,count(*) FROM history_work_receipts WHERE state<>'projected' GROUP BY reason")),
                conflicting_sessions=db.execute('SELECT count(*) FROM history_work_conflicts').fetchone()[0],
                scope='Forward retained Claude/Codex records; independent native work reader remains enabled; no historical queue fill')



def acknowledge_gap_prefix(db,candidates,now):
    """Checkpoint a bounded contiguous unknown prefix; never derive new facts.

    None means ordinary parsing is required. Zero means a writer changed the
    prepared prefix, so the next cycle must select it again. Git/body processing
    is excluded from this small metadata transaction.
    """
    if len(candidates)<2:return None
    first=candidates[0];key=(first['machine'],first['tool'],first['session'])
    old=db.execute('SELECT seq,value FROM history_work_context WHERE machine=? AND tool=? AND session=?',key).fetchone()
    if not old:return None
    state=json.loads(old[1])
    if not state.get('_work_gap'):return None
    if db.execute('SELECT 1 FROM history_work_conflicts WHERE machine=? AND tool=? AND session=?',key).fetchone():return None
    prefix=[]
    for event in candidates[:32]:
        if (event['machine'],event['tool'],event['session'])!=key or event['seq']<=old[0]:break
        prefix.append(event)
    if len(prefix)<2:return None
    ids=[e['event_id'] for e in prefix];marks=','.join('?' for _ in ids)
    receipts={r[0] for r in db.execute('SELECT event_id FROM history_work_receipts WHERE event_id IN ('+marks+')',ids)}
    prefix=prefix[:next((i for i,e in enumerate(prefix) if e['event_id'] in receipts),len(prefix))]
    if len(prefix)<2:return None
    ids=[e['event_id'] for e in prefix];marks=','.join('?' for _ in ids)
    # Checking the global pending prefix is bounded by 32, independent of the
    # corpus/session length. It also catches earlier metadata revisions/retries
    # appearing after candidate selection, without a per-session history scan.
    query=('SELECT q.event_id,e.seq,e.machine,e.tool,e.session,e.content_hash,e.expired_at,q.content_hash '
           'FROM history_work_pending q CROSS JOIN history_events e ON e.event_id=q.event_id ORDER BY q.seq LIMIT ?')
    expected=[(e['event_id'],e['seq'],e['machine'],e['tool'],e['session'],e['content_hash'],e['expired_at'],e['queued_hash']) for e in prefix]
    if db.execute(query,(len(prefix),)).fetchall()!=expected:return None
    state['_work_gap']='preceding_work_record_unavailable'
    encoded_state=json.dumps(state)
    with db:
        db.execute('BEGIN IMMEDIATE')
        if (db.execute('SELECT seq,value FROM history_work_context WHERE machine=? AND tool=? AND session=?',key).fetchone()!=old
            or db.execute('SELECT 1 FROM history_work_conflicts WHERE machine=? AND tool=? AND session=?',key).fetchone()
            or db.execute(query,(len(prefix),)).fetchall()!=expected
            or db.execute('SELECT 1 FROM history_work_receipts WHERE event_id IN ('+marks+') LIMIT 1',ids).fetchone()
            or db.execute('SELECT 1 FROM history_work_pending WHERE event_id IN ('+marks+') AND retry_at>? LIMIT 1',ids+[now]).fetchone()):return 0
        db.executemany('INSERT INTO history_work_receipts VALUES(?,?,?,?,?,?)',
                       [(e['event_id'],e['content_hash'],'','gap','preceding_work_record_unavailable',now) for e in prefix])
        db.execute('INSERT OR REPLACE INTO history_work_context VALUES(?,?,?,?,?)',key+(prefix[-1]['seq'],encoded_state))
        db.executemany('DELETE FROM history_work_pending WHERE event_id=? AND content_hash=?',[(e['event_id'],e['queued_hash']) for e in prefix])
    return len(prefix)


def run(db,limit=128,budget=.04):
    if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=1024 or isinstance(budget,bool) or not isinstance(budget,(int,float)) or not math.isfinite(budget) or budget<=0:raise ValueError('invalid work projection bounds')
    if db.in_transaction:raise ValueError('work projection requires an idle connection')
    started=time.monotonic();cpu=time.thread_time();now=int(time.time());processed=errors=inserted=attempted=processed_bytes=0
    cols=COLUMNS
    cursor=db.execute(CANDIDATES_SQL,(now,now,limit))
    candidates=[]
    for values in cursor:
        event=dict(zip(cols,values[:-1]));event['queued_hash']=values[-1];candidates.append(event)
    cursor.close()
    batch=acknowledge_gap_prefix(db,candidates,now)
    if batch is not None:
        return dict(processed=batch,inserted=0,errors=0,pending=db.execute('SELECT count(*) FROM history_work_pending').fetchone()[0],gap_batch_records=batch,processed_bytes=sum(e['bytes'] or 0 for e in candidates[:batch]),cpu_seconds=round(time.thread_time()-cpu,6))
    deferred=set();prepared=[];checkpoints={};conflicts={};staged=[]
    for event in candidates:
        if attempted and (time.monotonic()-started>=max(.05,budget) or time.thread_time()-cpu>=budget):break
        key=(event['machine'],event['tool'],event['session'])
        if key in deferred:continue
        attempted+=1
        if key not in checkpoints:checkpoints[key]=db.execute('SELECT seq,value FROM history_work_context WHERE machine=? AND tool=? AND session=?',key).fetchone()
        old=checkpoints[key]
        receipt=db.execute('SELECT semantic_hash,state,reason FROM history_work_receipts WHERE event_id=?',(event['event_id'],)).fetchone()
        if key not in conflicts:conflicts[key]=db.execute('SELECT reason FROM history_work_conflicts WHERE machine=? AND tool=? AND session=?',key).fetchone()
        blocked=conflicts[key]
        reason='';rows=[];state=json.loads(old[1]) if old else {};semantic='';failure=False;witnesses={}
        try:
            if blocked:reason=blocked[0];semantic=receipt[0] if receipt else ''
            elif receipt and receipt[1]=='gap' and not receipt[0]:reason=receipt[2]
            elif state.get('_work_gap') and not receipt:reason='preceding_work_record_unavailable'
            else:
                if event['expired_at'] or not event['present']:raise ValueError('body_unavailable_for_work_projection')
                packed=db.execute('SELECT body FROM history_events WHERE event_id=? AND content_hash=?',(event['event_id'],event['content_hash'])).fetchone()
                if not packed or not packed[0]:raise ValueError('body_changed_before_work_projection')
                body=json.loads(zlib.decompress(packed[0]));semantic=semantic_hash(event['tool'],body)
                if receipt and receipt[1]!='retrying' and receipt[0]==semantic:reason=receipt[2]
                elif receipt and receipt[1]!='retrying' and receipt[0]!=semantic:reason='work_source_changed_after_projection'
                elif old and event['seq']<=old[0] and not receipt:reason='work_source_out_of_order'
                else:rows,reason=plan(db,event,body,state,witnesses,staged)
            # The independent native context reader may be one tick behind.
            if reason=='recorded_repository_context_unavailable' and not old and time.time()-event['ts']<120:failure=True
        except (ValueError,TypeError,KeyError,AttributeError,zlib.error):reason='invalid_or_unavailable_work_body';failure=True
        # Parse a bounded ordered prefix without a writer lock. Later records
        # see the preceding planned checkpoint, exactly as after separate commits.
        prepared.append((event,key,old,receipt,reason,rows,state,semantic,failure,witnesses))
        if failure:deferred.add(key)
        elif not receipt or receipt[1]=='retrying':
            next_state=json.loads(old[1]) if reason and old else {} if reason else state
            if reason:next_state['_work_gap']=reason
            checkpoints[key]=(event['seq'],json.dumps(next_state))
        if not failure and not reason:
            for row in rows:
                if row['t']=='work_context' and work_retained.existing(db,event['machine'],row) is None:
                    staged.append(dict(row,machine=event['machine']))
        if len(prepared)>=128:break
    deferred=set()
    # Only validated SQLite writes occur under the bounded batch transaction.
    # A failed comparison defers the rest of that session, preserving its order.
    if prepared:
      with db:
        db.execute('BEGIN IMMEDIATE')
        # Validate recorded witnesses before our own writes add new contexts.
        # The writer lock keeps external evidence stable throughout this batch.
        for event,key,old,receipt,reason,rows,state,semantic,failure,witnesses in prepared:
            if db.execute('SELECT reason FROM history_work_conflicts WHERE machine=? AND tool=? AND session=?',key).fetchone()!=conflicts[key]:deferred.add(key)
            if db.execute('SELECT semantic_hash,state,reason FROM history_work_receipts WHERE event_id=?',(event['event_id'],)).fetchone()!=receipt:deferred.add(key)
            if any((observed_context(db,event,cwd),observed_repository(db,event,cwd))!=value for cwd,value in witnesses.items()):deferred.add(key)
        for event,key,old,receipt,reason,rows,state,semantic,failure,witnesses in prepared:
            if key in deferred:continue
            current=db.execute('SELECT content_hash,expired_at FROM history_events WHERE event_id=?',(event['event_id'],)).fetchone()
            queued=db.execute('SELECT content_hash FROM history_work_pending WHERE event_id=?',(event['event_id'],)).fetchone()
            checkpoint=db.execute('SELECT seq,value FROM history_work_context WHERE machine=? AND tool=? AND session=?',key).fetchone()
            if current!=(event['content_hash'],event['expired_at']) or not queued or queued[0]!=event['queued_hash'] or checkpoint!=old:
                deferred.add(key);continue
            additions=[]
            if not reason:
                for row in rows:
                    existing=work_retained.existing(db,event['machine'],row)
                    if existing is None:additions.append(row)
                    elif not work_retained.compatible(existing,row):reason='conflicting_existing_work_fact';break
            conflict=reason in ('work_source_changed_after_projection','work_source_out_of_order','conflicting_existing_work_fact')
            if conflict:
                db.execute('INSERT OR REPLACE INTO history_work_conflicts VALUES(?,?,?,?)',key+(reason,))
            if failure:
                errors+=1;deferred.add(key)
                db.execute('UPDATE history_work_pending SET attempts=attempts+1,retry_at=? WHERE event_id=?',(now+30,event['event_id']))
                db.execute('INSERT OR REPLACE INTO history_work_receipts VALUES(?,?,?,?,?,?)',(event['event_id'],event['content_hash'],semantic,'retrying',reason,now))
                continue
            if not reason:
                if additions:store.ingest(db,map(json.dumps,additions),machine=event['machine'],transaction=False)
                inserted+=len(additions)
            if not receipt or receipt[1]=='retrying':
                if reason:
                    state=json.loads(old[1]) if old else {}
                    state['_work_gap']=reason
                db.execute('INSERT OR REPLACE INTO history_work_context VALUES(?,?,?,?,?)',key+(event['seq'],json.dumps(state)))
            db.execute('INSERT OR REPLACE INTO history_work_receipts VALUES(?,?,?,?,?,?)',(event['event_id'],event['content_hash'],semantic,'conflicting' if conflict else 'gap' if reason else 'projected',reason,now))
            db.execute('DELETE FROM history_work_pending WHERE event_id=? AND content_hash=?',(event['event_id'],event['queued_hash']))
            processed+=1;processed_bytes+=event['bytes'] or 0
            # A newly discovered conflict invalidates later prepared facts. An
            # already blocked session prepared no facts and may acknowledge its
            # entire validated prefix without one-record-per-poll throttling.
            if conflict and not conflicts[key]:deferred.add(key)
    return dict(processed=processed,inserted=inserted,errors=errors,pending=db.execute('SELECT count(*) FROM history_work_pending').fetchone()[0],processed_bytes=processed_bytes,cpu_seconds=round(time.thread_time()-cpu,6))



def run_worker(database,stop,update=None,measure=None,poll=2,duty=.90,include_usage=False):
    """Drain on demand; leave capture headroom and never hold a lock while waiting.

    CPU pacing is an average thread budget during catch-up, not an OS hard cap.
    Empty/retry-blocked queues sleep; successful batches have no fixed poll delay.
    """
    if poll<=0 or not 0<duty<=1:raise ValueError('invalid work worker bounds')
    processed=processed_bytes=usage_total=usage_bytes=0
    with contextlib.closing(store.connect(path=database,initialize=False,autocheckpoint=False)) as db:
        while not stop.is_set():
            cpu=time.thread_time();wall=time.monotonic()
            usage=edits=None;phase='work'
            try:
                if include_usage:
                    from . import history_projection,edit_projection
                    phase='usage'
                    usage=measure('usage_projection',history_projection.run,db,limit=128,budget=.06,cpu_budget=True) if measure else history_projection.run(db,limit=128,budget=.06,cpu_budget=True)
                    phase='edits'
                    edits=measure('edit_projection',edit_projection.run,db) if measure else edit_projection.run(db)
                phase='work'
                result=measure('work_projection',run,db) if measure else run(db)
            except Exception as error:
                db.rollback()
                result=dict(processed=0,processed_bytes=0,errors=1,pending=None,error=type(error).__name__,error_stage=phase)
            if usage is not None:
                usage_total+=usage['processed'];usage_bytes+=usage.get('processed_bytes',0)
                result.update(usage_projection=usage,edit_projection=edits,usage_processed_total=usage_total,usage_processed_bytes_total=usage_bytes)
            processed+=result['processed'];processed_bytes+=result.get('processed_bytes',0)
            stages=[result]+[stage for stage in (usage,edits) if stage is not None]
            progress=sum(stage['processed'] for stage in stages)
            pending=any(stage.get('pending') is None or stage['pending']>0 for stage in stages)
            result.update(processed_total=processed,processed_bytes_total=processed_bytes,
                          mode='catchup' if progress and pending else 'idle' if not pending else 'blocked')
            if update:update(result)
            if not progress or not pending:
                stop.wait(poll)
            else:
                delay=(time.thread_time()-cpu)/duty-(time.monotonic()-wall)
                if delay>0:stop.wait(delay)
