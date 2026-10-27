"""Versioned, content-free observation contract and durable export journal.

Rows are mutable snapshots identified independently of batch delivery. Each
changed snapshot gets a monotonically increasing local sequence. Export advances
its destination cursor only after a successful central MERGE.
"""
import hashlib
import json
import math
import time

VERSION = 1
FIELDS = {
    'llm': 'uid ts tool session project model kind client fresh_in cache_read cache_write out reasoning requester request_id response_id provider duration_ms reported_cost_usd reported_cost_source cache_write_5m cache_write_1h usage_kind',
    'project_context': 'uid ts tool session cwd repo',
    'session': 'uid ts last_ts tool session project kind parent_session forked_from surface agent_version provenance provider usage_status orchestrator conversation_id external_session summary_in summary_out summary_cache_read summary_cache_write',
    'source': 'uid ts tool files errors installed status linked missing detail',
    'edit': 'uid ts tool session project model doc added removed',
    'prompt': 'uid ts tool session project model chars words est_tokens origin provenance source_uid',
    'commit': 'uid ts repo project author added_code removed_code added_doc removed_doc files doc_files agent model',
    'metric': 'uid ts src m iv v pk',
    'note': 'uid ts k text',
}
from .work import EVENTS
FIELDS.update(EVENTS)

SCHEMA = '''
CREATE TABLE IF NOT EXISTS observations(
 event_id TEXT PRIMARY KEY, machine TEXT NOT NULL, kind TEXT NOT NULL,
 ts INTEGER NOT NULL, payload TEXT NOT NULL, content_hash TEXT NOT NULL,
 seq INTEGER NOT NULL UNIQUE);
CREATE INDEX IF NOT EXISTS observations_ts ON observations(ts);
CREATE INDEX IF NOT EXISTS observations_kind ON observations(kind);
CREATE TABLE IF NOT EXISTS export_cursors(destination TEXT PRIMARY KEY, seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS journal_clock(id INTEGER PRIMARY KEY CHECK(id=1),seq INTEGER NOT NULL);
INSERT OR IGNORE INTO journal_clock VALUES(1,0);
'''


def initialize(db):
    db.executescript(SCHEMA)


def normalize(machine, event):
    kind = event.get('t')
    if kind not in FIELDS:
        raise ValueError('unsupported observation type')
    payload = {k: event[k] for k in FIELDS[kind].split() if k in event}
    for k, value in payload.items():
        if value is not None and (not isinstance(value, (str, int, float, bool)) or
                                  isinstance(value, float) and not math.isfinite(value)):
            raise ValueError('invalid observation field: ' + k)
    ts = int(payload.get('ts') or 0)
    if ts <= 0:
        raise ValueError('observation requires a positive timestamp')
    uid = payload.get('uid')
    if not uid:
        if kind == 'metric':
            uid = '%s:%s:%s:%s' % (payload['src'], payload['m'], ts, payload['iv'])
        elif kind == 'note':
            uid = '%s:%s' % (payload['k'], ts)
        else:
            raise ValueError('observation requires a stable uid')
        payload['uid'] = uid
    key = json.dumps([VERSION, machine, kind, uid], separators=(',', ':'))
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return (hashlib.sha256(key.encode()).hexdigest(), machine, kind, ts, encoded,
            hashlib.sha256(encoded.encode()).hexdigest())


def record(db, machine, events):
    """Caller owns the transaction, shared with local projection/checkpoint."""
    changed = 0
    for event in events:
        row = normalize(machine, event)
        old = db.execute('SELECT payload,content_hash FROM observations WHERE event_id=?', (row[0],)).fetchone()
        if old:
            if event['t']=='llm':
                previous=json.loads(old[0]);current=json.loads(row[4])
                for field in ('provider','reported_cost_usd','reported_cost_source','cache_write_5m','cache_write_1h'):
                    if current.get(field) is None and previous.get(field) is not None:current[field]=previous[field]
                row=normalize(machine,dict(current,t='llm'))
            if event['t']=='prompt':
                previous=json.loads(old[0]);current=json.loads(row[4])
                if (previous.get('origin') in ('human','automation') and current.get('origin') not in ('human','automation')) or (previous.get('provenance') in ('codex_user_text','codex_user_event') and current.get('provenance') not in ('codex_user_text','codex_user_event')):
                    current['origin']=previous.get('origin');current['provenance']=previous.get('provenance');current['source_uid']=previous.get('source_uid')
                    row=normalize(machine,dict(current,t='prompt'))
            if event['t']=='session':
                previous=json.loads(old[0]);current=json.loads(row[4])
                for field in ('orchestrator','conversation_id','external_session','summary_in','summary_out','summary_cache_read','summary_cache_write'):
                    if not current.get(field) and previous.get(field): current[field]=previous[field]
                current['ts']=min(previous['ts'],current['ts'])
                current['last_ts']=max(previous.get('last_ts',previous['ts']),current.get('last_ts',current['ts']))
                statuses=(previous.get('usage_status'),current.get('usage_status'))
                current['usage_status']='observed' if 'observed' in statuses else 'summary_only' if 'summary_only' in statuses else 'unknown'
                if previous.get('provenance')!='omnigent_metadata' and current.get('provenance')=='omnigent_metadata':
                    for field in ('provenance','project','surface','provider','agent_version'): current[field]=previous.get(field,current.get(field))
                row=normalize(machine,dict(current,t='session'))
            if old[1] == row[5]:
                continue
            # A replay of an earlier streamed fragment cannot retract complete usage.
            if event['t'] == 'llm' and json.loads(old[0]).get('out', 0) > event.get('out', 0):
                continue
        db.execute('UPDATE journal_clock SET seq=seq+1 WHERE id=1')
        seq = db.execute('SELECT seq FROM journal_clock WHERE id=1').fetchone()[0]
        db.execute('INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?,?,?)', row + (seq,))
        changed += 1
    return changed


def pending(db, destination, limit=500):
    cursor = db.execute('SELECT seq FROM export_cursors WHERE destination=?', (destination,)).fetchone()
    return db.execute('SELECT event_id,machine,kind,ts,payload,content_hash,seq FROM observations WHERE seq>? ORDER BY seq LIMIT ?',
                      (cursor[0] if cursor else 0, limit)).fetchall()


def acknowledge(db, destination, seq):
    with db:
        db.execute('INSERT INTO export_cursors VALUES(?,?) ON CONFLICT(destination) DO UPDATE SET seq=MAX(seq,excluded.seq)',
                   (destination, seq))


def session_rows(db, since_ts=0, limit=200):
    """Backend-independent grouped session relation; bodies never enter it."""
    return [list(r) for r in db.execute('''SELECT machine,tool,session,project,kind,MIN(ts),MAX(last_ts),MAX(parent_session),
        MAX(surface),MAX(agent_version),MAX(usage_status),MAX(orchestrator),MAX(conversation_id),MAX(external_session),MAX(summary_in),MAX(summary_out),MAX(summary_cache_read),MAX(summary_cache_write) FROM report_sessions WHERE last_ts>=?
        GROUP BY machine,tool,session,project,kind ORDER BY MAX(last_ts) DESC LIMIT ?''', (since_ts, limit))]


def span(row):
    """OTel-compatible projection of a reconstructed observation.

    Stable synthetic IDs identify erg evidence, not a claimed gateway trace.
    Unknown call duration remains absent, rather than fabricated as zero.
    """
    event_id, machine, kind, ts, encoded = row[:5]
    p = json.loads(encoded)
    session = p.get('session') or p.get('uid')
    trace_id = hashlib.sha256(('erg:' + p.get('tool', '') + ':' + session).encode()).hexdigest()[:32]
    attributes = {'erg.schema_version': VERSION, 'erg.event_id': event_id,
                  'erg.provenance': 'reconstructed', 'host.name': machine,
                  'gen_ai.conversation.id': p.get('session'), 'erg.project': p.get('project'),
                  'gen_ai.request.model': p.get('model'), 'erg.observation.type': kind}
    if kind == 'llm':
        attributes.update({'gen_ai.operation.name': 'chat',
            'gen_ai.usage.input_tokens': sum(p.get(k) or 0 for k in ['fresh_in','cache_read','cache_write']),
            'gen_ai.usage.output_tokens': p.get('out'),
            'gen_ai.usage.reasoning.output_tokens': p.get('reasoning'),
            'gen_ai.agent.name': p.get('tool'),
            'gen_ai.usage.cache_read.input_tokens': p.get('cache_read'),
            'gen_ai.usage.cache_write.input_tokens': p.get('cache_write'),
            'gen_ai.response.id': p.get('response_id'), 'erg.request_id': p.get('request_id')})
    return dict(trace_id=trace_id, span_id=event_id[:16], name='invoke_agent' if kind == 'session' else 'chat',
                start_time_unix_nano=ts*10**9, end_time_unix_nano=p.get('last_ts', ts)*10**9 if kind == 'session' else None,
                attributes={k:v for k,v in attributes.items() if v is not None})


CALLS_SELECT = """WITH ranked AS (
 SELECT *,ROW_NUMBER() OVER(PARTITION BY tool,CASE WHEN tool='local' AND COALESCE(response_id,'')<>'' THEN response_id||':'||COALESCE(model,'') ELSE uid END ORDER BY out DESC,ts DESC,machine) AS capture_rank FROM llm WHERE COALESCE(usage_kind,CASE WHEN tool='pi' AND COALESCE(fresh_in,0)+COALESCE(cache_read,0)+COALESCE(cache_write,0)+COALESCE(out,0)=0 THEN 'unknown' ELSE 'observed' END)='observed'
), matched AS (
 SELECT request_id,model FROM ranked WHERE capture_rank=1 AND request_id IS NOT NULL AND request_id<>''
 GROUP BY request_id,model HAVING SUM(CASE WHEN tool='gateway' THEN 1 ELSE 0 END)=1
 AND SUM(CASE WHEN tool<>'gateway' THEN 1 ELSE 0 END)=1
), local_matched AS (
 SELECT machine,response_id,model FROM ranked WHERE capture_rank=1 AND response_id IS NOT NULL AND response_id<>''
 GROUP BY machine,response_id,model HAVING SUM(CASE WHEN tool='local' THEN 1 ELSE 0 END)=1
 AND SUM(CASE WHEN tool NOT IN ('local','gateway') THEN 1 ELSE 0 END)=1
)
SELECT r.* FROM ranked r WHERE capture_rank=1 AND
 (tool<>'local' OR NOT EXISTS(SELECT 1 FROM local_matched m WHERE m.machine=r.machine AND m.response_id=r.response_id AND m.model=r.model)) AND
 (tool<>'gateway' OR NOT EXISTS(SELECT 1 FROM matched m WHERE m.request_id=r.request_id AND m.model=r.model))"""
WORK_SELECT = """SELECT * FROM (SELECT *,ROW_NUMBER() OVER(PARTITION BY uid ORDER BY machine) AS sighting_rank FROM commits) WHERE sighting_rank=1"""


SESSIONS_SELECT = """SELECT s.* FROM sessions s WHERE (COALESCE(s.provenance,'')<>'omnigent_metadata'
 OR NOT EXISTS(SELECT 1 FROM sessions n WHERE n.machine=s.machine AND n.conversation_id=s.conversation_id
 AND COALESCE(n.provenance,'')<>'omnigent_metadata')) AND
 (s.tool<>'local' OR NOT EXISTS(SELECT 1 FROM llm l WHERE l.machine=s.machine AND l.tool='local' AND l.session=s.session)
 OR EXISTS(SELECT 1 FROM calls c WHERE c.machine=s.machine AND c.tool='local' AND c.session=s.session))"""
