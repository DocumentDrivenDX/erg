"""Atomic forward-only capture journal; transport never runs inside capture."""
import json
import time
import zlib
import uuid
from . import delivery_protocol as wire

FIELDS = ('event_id', 'machine', 'tool', 'session', 'project', 'ts', 'kind',
          'role', 'origin', 'source', 'body', 'content_hash', 'bytes',
          'capture_version', 'expires_at', 'expired_at', 'source_offset', 'source_inode', 'source_ts')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS delivery_config(singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 enrollment_id TEXT NOT NULL,installation_id TEXT NOT NULL,destination_generation TEXT NOT NULL,
 floor INTEGER NOT NULL,next_sequence INTEGER NOT NULL DEFAULT 1,blocked_reason TEXT);
CREATE TABLE IF NOT EXISTS delivery_heads(event_id TEXT PRIMARY KEY,revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS delivery_journal(id INTEGER PRIMARY KEY AUTOINCREMENT,
 enrollment_id TEXT NOT NULL,installation_id TEXT NOT NULL,destination_generation TEXT NOT NULL,
 event_id TEXT NOT NULL,revision INTEGER NOT NULL,source_seq INTEGER NOT NULL,snapshot TEXT NOT NULL,
 body BLOB,captured_at INTEGER NOT NULL,prepared INTEGER NOT NULL DEFAULT 0,
 error TEXT,UNIQUE(enrollment_id,destination_generation,event_id,revision));
CREATE INDEX IF NOT EXISTS delivery_event_revisions ON delivery_journal(event_id,prepared,id);
CREATE INDEX IF NOT EXISTS delivery_prepare ON delivery_journal(prepared,id);
CREATE TABLE IF NOT EXISTS delivery_items(enrollment_id TEXT NOT NULL,destination_generation TEXT NOT NULL,
 delivery_seq INTEGER NOT NULL,journal_id INTEGER NOT NULL,item BLOB NOT NULL,
 batch_id TEXT,accepted INTEGER NOT NULL DEFAULT 0,materialized INTEGER NOT NULL DEFAULT 0,
 projected INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(enrollment_id,destination_generation,delivery_seq));
CREATE INDEX IF NOT EXISTS delivery_item_materialization ON delivery_items(journal_id,materialized);
CREATE INDEX IF NOT EXISTS delivery_unsent ON delivery_items(batch_id,delivery_seq);
CREATE TABLE IF NOT EXISTS delivery_batches(batch_id TEXT PRIMARY KEY,
 enrollment_id TEXT NOT NULL,destination_generation TEXT NOT NULL,body BLOB NOT NULL,sha256 TEXT NOT NULL,
 created_at INTEGER NOT NULL,accepted INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,
 retry_at REAL NOT NULL DEFAULT 0,blocked INTEGER NOT NULL DEFAULT 0,last_error TEXT);
CREATE INDEX IF NOT EXISTS delivery_batch_retry ON delivery_batches(accepted,retry_at,created_at);
CREATE TABLE IF NOT EXISTS delivery_http_attempts(id INTEGER PRIMARY KEY,ts INTEGER,batch_id TEXT,body_bytes INTEGER,http_status INTEGER,outcome TEXT,elapsed_ms REAL);
CREATE TABLE IF NOT EXISTS delivery_http_daily(day INTEGER PRIMARY KEY,attempts INTEGER DEFAULT 0,accepted_responses INTEGER DEFAULT 0,offered_body_bytes INTEGER DEFAULT 0,elapsed_ms REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS delivery_unique_daily(day INTEGER PRIMARY KEY,batches INTEGER DEFAULT 0,items INTEGER DEFAULT 0,payload_bytes INTEGER DEFAULT 0);

'''


def initialize(db):
    """Inactive until explicit enrollment; no retained history is queued."""
    db.executescript(SCHEMA)
    columns={r[1] for r in db.execute('PRAGMA table_info(delivery_config)')}
    if 'forward_ts' not in columns:db.execute('ALTER TABLE delivery_config ADD COLUMN forward_ts INTEGER NOT NULL DEFAULT 0')
    for flag in ('accepted','materialized','projected'):
        db.execute('CREATE INDEX IF NOT EXISTS delivery_pending_'+flag+' ON delivery_items(delivery_seq) WHERE '+flag+'=0')
    db.execute('CREATE TABLE IF NOT EXISTS delivery_source_floors(path TEXT PRIMARY KEY,inode INTEGER,offset INTEGER)')
    # JSON1 is deliberately unnecessary on portable SQLite runtimes.
    # A SQL trigger snapshots each scalar as an ordered SQL-quoted array instead.
    columns = ','.join('NEW.' + c for c in FIELDS if c != 'body')
    # Snapshot stored as columns in a side table, rather than calling Python in SQL.
    definitions = ','.join(c + (' INTEGER' if c in ('ts', 'bytes', 'capture_version', 'expires_at', 'expired_at', 'source_offset', 'source_inode', 'source_ts') else ' TEXT') for c in FIELDS if c != 'body')
    db.execute('CREATE TABLE IF NOT EXISTS delivery_snapshots(journal_id INTEGER PRIMARY KEY,' + definitions + ')')
    have={r[1] for r in db.execute('PRAGMA table_info(delivery_snapshots)')}
    for c in FIELDS:
        if c!='body' and c not in have:db.execute('ALTER TABLE delivery_snapshots ADD COLUMN '+c+' INTEGER')
    names = ','.join(c for c in FIELDS if c != 'body')
    actions = '''
      INSERT INTO delivery_heads VALUES(NEW.event_id,1)
        ON CONFLICT(event_id) DO UPDATE SET revision=revision+1;
      INSERT INTO delivery_journal(enrollment_id,installation_id,destination_generation,
        event_id,revision,source_seq,snapshot,body,captured_at)
      SELECT enrollment_id,installation_id,destination_generation,NEW.event_id,
        (SELECT revision FROM delivery_heads WHERE event_id=NEW.event_id),NEW.seq,'v1',NEW.body,
        CAST(strftime('%%s','now') AS INTEGER) FROM delivery_config WHERE singleton=1;
      INSERT INTO delivery_snapshots(journal_id,%s) VALUES(last_insert_rowid(),%s);
    ''' % (names, columns)
    when = '''EXISTS(SELECT 1 FROM delivery_config c WHERE singleton=1 AND NEW.seq>floor
      AND (c.forward_ts=0 OR (NEW.ts>=c.forward_ts AND (NEW.source_ts IS NULL OR NEW.source_ts>=c.forward_ts)
      AND NEW.origin<>'synthetic_history' AND NOT EXISTS(SELECT 1 FROM delivery_source_floors f
        WHERE f.path=NEW.source AND (NEW.source_offset IS NULL OR NEW.source_inode IS NULL OR
          (NEW.source_inode=f.inode AND NEW.source_offset<f.offset))))))'''
    db.execute('SAVEPOINT delivery_trigger_upgrade')
    for name in ('delivery_capture_insert','delivery_capture_update'):db.execute('DROP TRIGGER IF EXISTS '+name)
    db.execute('CREATE TRIGGER IF NOT EXISTS delivery_capture_insert AFTER INSERT ON history_events WHEN ' + when + ' BEGIN ' + actions + ' END')
    changed = ' OR '.join('NEW.%s IS NOT OLD.%s' % (c, c) for c in FIELDS)
    db.execute('CREATE TRIGGER IF NOT EXISTS delivery_capture_update AFTER UPDATE ON history_events WHEN ' + when + ' AND (' + changed + ') BEGIN ' + actions + ' END')
    db.execute('RELEASE delivery_trigger_upgrade')
    # Defense in depth: expiration cannot erase unpublished revisions.
    db.execute('''CREATE TRIGGER IF NOT EXISTS delivery_protect_expiry BEFORE UPDATE OF body ON history_events
      WHEN OLD.body IS NOT NULL AND NEW.body IS NULL AND EXISTS(
        SELECT 1 FROM delivery_journal j WHERE j.event_id=OLD.event_id AND j.body IS NOT NULL
        AND (j.prepared<>1 OR EXISTS(SELECT 1 FROM delivery_items i WHERE i.journal_id=j.id AND i.materialized=0)))
      BEGIN SELECT RAISE(ABORT,'delivery pending materialization'); END''')


def enroll(db, enrollment, forward_only=False, source_floors=None, forward_ts=None):
    """Called only with a qualified server generation; never invent one on reinstall."""
    if set(enrollment) != set(wire.ENROLLMENT):
        raise wire.ProtocolError('invalid enrollment fields')
    # Reuse the same identity validation as the actual wire.
    payload = dict(record_id='validation', record_revision=1, record_kind='session_event')
    wire.batch(enrollment, wire.record_items(payload, 1))
    with db:
        if not db.in_transaction:db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT enrollment_id,installation_id,destination_generation FROM delivery_config').fetchone()
        if old:
            if tuple(old) != tuple(enrollment[k] for k in wire.ENROLLMENT):
                raise ValueError('generation replacement requires explicit recovery workflow')
            return
        floor = db.execute('SELECT COALESCE(MAX(seq),0) FROM history_events').fetchone()[0]
        boundary=(int(time.time())+1 if forward_ts is None else int(forward_ts)) if forward_only else 0
        if forward_only and boundary<=0:raise ValueError('positive forward boundary required')
        for path,inode,offset in source_floors or []:
            db.execute('INSERT INTO delivery_source_floors VALUES(?,?,?)',(path,inode,offset))
        db.execute('INSERT INTO delivery_config(singleton,enrollment_id,installation_id,destination_generation,floor,forward_ts) VALUES(1,?,?,?,?,?)',
                   tuple(enrollment[k] for k in wire.ENROLLMENT) + (floor,boundary))


def _inflate(raw):
    decoder = zlib.decompressobj()
    body = decoder.decompress(raw, wire.MAX_RECORD + 1)
    if len(body) > wire.MAX_RECORD or decoder.unconsumed_tail or not decoder.eof:
        raise wire.ProtocolError('unsupported compressed record size or encoding')
    return wire.decode(body)


def prepare_one(db):
    """Decode outside writer transaction; publish sequences in one short commit."""
    row = db.execute('SELECT id,revision,source_seq,body,captured_at FROM delivery_journal WHERE prepared=0 ORDER BY id LIMIT 1').fetchone()
    if not row: return False
    ident, revision, source_seq, body, captured_at = row
    cursor = db.execute('SELECT * FROM delivery_snapshots WHERE journal_id=?', (ident,))
    snapshot = dict(zip((c[0] for c in cursor.description), cursor.fetchone()))
    snapshot.pop('journal_id')
    payload = dict(snapshot, record_id=snapshot['event_id'], record_revision=revision,
                   record_kind='content_tombstone' if snapshot['expired_at'] is not None else 'session_event',
                   source_seq=source_seq, captured_at=captured_at,
                   body=None)
    if snapshot.get('origin')=='derived_reporting':payload['record_kind']=snapshot['kind'];payload['source_seq']=None
    try:
        payload['body'] = _inflate(body) if body is not None else None
        # Validate before acquiring a write lock.
        wire.record_items(payload, 1)
    except (wire.ProtocolError,zlib.error,TypeError):
        with db: db.execute("UPDATE delivery_journal SET prepared=-1,error='unsupported record' WHERE id=? AND prepared=0", (ident,))
        return True
    try:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT prepared FROM delivery_journal WHERE id=?', (ident,)).fetchone()[0] != 0:
            db.rollback(); return True
        config = db.execute('SELECT enrollment_id,destination_generation,next_sequence FROM delivery_config').fetchone()
        items = wire.record_items(payload, config[2])
        for item in items:
            db.execute('INSERT INTO delivery_items(enrollment_id,destination_generation,delivery_seq,journal_id,item) VALUES(?,?,?,?,?)',
                       (config[0], config[1], item['delivery_seq'], ident, wire.encode(item)))
        db.execute('UPDATE delivery_config SET next_sequence=next_sequence+?', (len(items),))
        db.execute('UPDATE delivery_journal SET prepared=1 WHERE id=?', (ident,))
        db.commit()
    except Exception:
        db.rollback(); raise
    return True


def next_batch(db, now=None):
    """Persist exact bytes before network IO. Lost responses retry identical bytes."""
    now = time.time() if now is None else now
    existing = db.execute('SELECT batch_id,body,sha256 FROM delivery_batches WHERE accepted=0 AND blocked=0 AND retry_at<=? ORDER BY created_at,batch_id LIMIT 1', (now,)).fetchone()
    if existing: return tuple(existing)
    config = db.execute('SELECT enrollment_id,installation_id,destination_generation FROM delivery_config').fetchone()
    if not config: return None
    enrollment = dict(zip(wire.ENROLLMENT, config))
    rows = db.execute('SELECT delivery_seq,item FROM delivery_items WHERE batch_id IS NULL AND enrollment_id=? AND destination_generation=? ORDER BY delivery_seq LIMIT ?',
                      (config[0], config[2], wire.MAX_ITEMS)).fetchall()
    if not rows: return None
    items = []; selected = []; batch_id = str(uuid.uuid4())
    base = dict(enrollment, protocol_version=1, batch_id=batch_id, items=[])
    size = len(wire.encode(base))
    for seq, raw in rows:
        cost = len(raw) + (1 if items else 0)
        if size + cost > wire.MAX_BATCH: break
        items.append(wire.decode(raw)); selected.append(seq); size += cost
    if not items: raise wire.ProtocolError('item exceeds batch size')
    body = wire.batch(enrollment, items, batch_id)
    batch_id = wire.decode(body)['batch_id']; sha = wire.digest(body)
    with db:
        db.execute('INSERT INTO delivery_batches(batch_id,enrollment_id,destination_generation,body,sha256,created_at) VALUES(?,?,?,?,?,?)',
                   (batch_id, config[0], config[2], body, sha, int(now)))
        for seq in selected:
            result = db.execute('UPDATE delivery_items SET batch_id=? WHERE enrollment_id=? AND destination_generation=? AND delivery_seq=? AND batch_id IS NULL',
                                (batch_id, config[0], config[2], seq))
            if result.rowcount != 1: raise ValueError('concurrent batch allocation')
    return batch_id, body, sha


def accept(db, batch_id, receipt):
    row = db.execute('SELECT body,sha256 FROM delivery_batches WHERE batch_id=?', (batch_id,)).fetchone()
    if not row: raise wire.ProtocolError('unknown batch receipt')
    envelope = wire.decode(row[0])
    expected = [i['delivery_seq'] for i in envelope['items']]
    if not isinstance(receipt, dict): raise wire.ProtocolError('invalid receipt')
    if (type(receipt.get('protocol_version')) is not int or receipt.get('protocol_version') != 1 or receipt.get('state') != 'accepted' or
        receipt.get('batch_id') != batch_id or receipt.get('batch_sha256') != row[1] or
        any(receipt.get(k) != envelope[k] for k in ('enrollment_id', 'destination_generation')) or
        receipt.get('accepted_delivery_seqs') != expected or
        any(type(v) is not int for v in receipt.get('accepted_delivery_seqs', []))):
        raise wire.ProtocolError('receipt mismatch')
    with db:
        updated=db.execute('UPDATE delivery_batches SET accepted=1,last_error=NULL WHERE batch_id=? AND accepted=0', (batch_id,))
        if updated.rowcount:
            db.execute('INSERT INTO delivery_unique_daily VALUES(?,?,?,?) ON CONFLICT(day) DO UPDATE SET batches=batches+1,items=items+excluded.items,payload_bytes=payload_bytes+excluded.payload_bytes',(int(time.time())//86400,1,len(expected),len(row[0])))
        db.execute('UPDATE delivery_items SET accepted=1 WHERE batch_id=?', (batch_id,))


def status(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_config'").fetchone():
        return dict(state='disabled', pending_revisions=0)
    config = db.execute('SELECT enrollment_id,installation_id,destination_generation,floor FROM delivery_config').fetchone()
    if not config: return dict(state='disabled', pending_revisions=0)
    result = dict(zip(wire.ENROLLMENT + ('forward_floor',), config), state='enrolled')
    result['forward_started_at']=db.execute('SELECT forward_ts FROM delivery_config').fetchone()[0]
    result['source_boundaries']=db.execute('SELECT COUNT(*) FROM delivery_source_floors').fetchone()[0]
    result['blocked_reason'] = db.execute('SELECT blocked_reason FROM delivery_config').fetchone()[0]
    result['pending_revisions'] = db.execute('SELECT COUNT(*) FROM delivery_journal WHERE prepared<>1').fetchone()[0]
    result['blocked_batches'] = db.execute('SELECT COUNT(*) FROM delivery_batches WHERE blocked=1 AND accepted=0').fetchone()[0]
    result['blocked_revisions'] = db.execute('SELECT COUNT(*) FROM delivery_journal WHERE prepared<0').fetchone()[0]
    result['pending_items'] = db.execute('SELECT COUNT(*) FROM delivery_items WHERE accepted=0').fetchone()[0]
    for field in ('accepted', 'materialized', 'projected'):
        # A cursor cannot jump a gap. Next_sequence is the allocated high-water mark.
        gap = db.execute('SELECT MIN(delivery_seq) FROM delivery_items WHERE ' + field + '=0').fetchone()[0]
        high = db.execute('SELECT next_sequence-1 FROM delivery_config').fetchone()[0]
        result[field + '_through'] = gap - 1 if gap is not None else high
    return result


def reconcile_status(db, receipt):
    """Apply a verified own-enrollment status, only across locally accepted items."""
    local = status(db)
    if not isinstance(receipt, dict) or any(receipt.get(k) != local.get(k) for k in ('enrollment_id','destination_generation')):
        raise wire.ProtocolError('status enrollment mismatch')
    materialized = receipt.get('materialized_through'); projected = receipt.get('projected_through')
    if (type(materialized) is not int or type(projected) is not int or
        not 0 <= projected <= materialized <= local['accepted_through'] or
        materialized < local['materialized_through'] or projected < local['projected_through']):
        raise wire.ProtocolError('invalid status progress')
    with db:
        db.execute('UPDATE delivery_items SET materialized=1 WHERE accepted=1 AND materialized=0 AND delivery_seq<=?', (materialized,))
        db.execute('UPDATE delivery_items SET projected=1 WHERE materialized=1 AND projected=0 AND delivery_seq<=?', (projected,))
