"""SQLite reference reducer for shared synthetic replay/fault fixtures.

Not a substitute for the Delta job or central/local reporting parity acceptance.
"""
from erg import delivery_protocol as wire
from .receiver import Conflict, namespace

SCHEMA = '''
CREATE TABLE IF NOT EXISTS canonical_items(namespace TEXT,seq INTEGER,record_id TEXT,revision INTEGER,item BLOB,
 materialized INTEGER DEFAULT 0,disposition TEXT,PRIMARY KEY(namespace,seq));
CREATE INDEX IF NOT EXISTS canonical_record_items ON canonical_items(namespace,record_id,revision);
CREATE TABLE IF NOT EXISTS canonical_revisions(namespace TEXT,id TEXT,revision INTEGER,
 sha256 TEXT,payload BLOB,PRIMARY KEY(namespace,id,revision));
CREATE TABLE IF NOT EXISTS canonical_heads(namespace TEXT,id TEXT,revision INTEGER,
 kind TEXT,payload BLOB,PRIMARY KEY(namespace,id));
CREATE TABLE IF NOT EXISTS canonical_progress(namespace TEXT PRIMARY KEY,materialized INTEGER,projected INTEGER);
CREATE TABLE IF NOT EXISTS canonical_errors(namespace TEXT,id TEXT,revision INTEGER,
 reason TEXT,PRIMARY KEY(namespace,id,revision));
'''


class Canonical:
    def __init__(self, db):
        self.db = db
        db.executescript(SCHEMA)

    def apply(self, value):
        space = namespace(value)
        records = set()
        with self.db:
            for item in value['items']:
                raw = wire.encode(item)
                old = self.db.execute('SELECT item FROM canonical_items WHERE namespace=? AND seq=?', (space, item['delivery_seq'])).fetchone()
                if old and old[0] != raw: raise Conflict('sequence conflict')
                self.db.execute('INSERT OR IGNORE INTO canonical_items(namespace,seq,record_id,revision,item) VALUES(?,?,?,?,?)', (space, item['delivery_seq'], item['record_id'], item['record_revision'], raw))
                records.add((item['record_id'], item['record_revision']))
            for ident, revision in records:
                items = [wire.decode(r[0]) for r in self.db.execute(
                    'SELECT item FROM canonical_items WHERE namespace=? AND record_id=? AND revision=? LIMIT 129', (space, ident, revision))]
                if len(items) > 128: raise Conflict('record multiplicity exceeded')
                try: raw = wire.reconstruct(items, as_bytes=True)
                except wire.ProtocolError as error:
                    if str(error) == 'incomplete record': continue
                    self.db.execute('INSERT OR REPLACE INTO canonical_errors VALUES(?,?,?,?)', (space, ident, revision, 'record conflict or corruption'))
                    continue
                payload = wire.decode(raw); sha = wire.digest(raw)
                old = self.db.execute('SELECT sha256 FROM canonical_revisions WHERE namespace=? AND id=? AND revision=?', (space, ident, revision)).fetchone()
                if old and old[0] != sha:
                    self.db.execute('INSERT OR REPLACE INTO canonical_errors VALUES(?,?,?,?)', (space, ident, revision, 'revision digest conflict'))
                    continue
                self.db.execute('INSERT OR IGNORE INTO canonical_revisions VALUES(?,?,?,?,?)', (space, ident, revision, sha, raw))
                self.db.execute('''INSERT INTO canonical_heads VALUES(?,?,?,?,?)
                  ON CONFLICT(namespace,id) DO UPDATE SET revision=excluded.revision,kind=excluded.kind,payload=excluded.payload
                  WHERE excluded.revision>canonical_heads.revision''', (space, ident, revision, payload['record_kind'], raw))
                # Only raw session content is explicitly not an accounting projection.
                # All other kinds remain unprojected until their real reducers exist.
                disposition = ('not_applicable:raw-session-v1' if payload['record_kind'] in ('session_event', 'content_tombstone') else None)
                for item in items:
                    self.db.execute('UPDATE canonical_items SET materialized=1,disposition=? WHERE namespace=? AND seq=?',
                                    (disposition, space, item['delivery_seq']))
        return self.status(value)

    def status(self, identity):
        space = namespace(identity)
        previous = self.db.execute('SELECT materialized,projected FROM canonical_progress WHERE namespace=?', (space,)).fetchone() or (0,0)
        materialized, projected = previous
        for field in ('materialized','projected'):
            cursor = materialized if field == 'materialized' else projected
            rows = self.db.execute('SELECT seq,materialized,disposition FROM canonical_items WHERE namespace=? AND seq>? ORDER BY seq LIMIT 4096', (space,cursor))
            for seq, complete, disposition in rows:
                if seq != cursor + 1 or not complete or (field == 'projected' and not disposition): break
                cursor = seq
            if field == 'materialized': materialized = cursor
            else: projected = cursor
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO canonical_progress VALUES(?,?,?)', (space,materialized,projected))
        return dict(enrollment_id=identity['enrollment_id'], destination_generation=identity['destination_generation'],
                    materialized_through=materialized, projected_through=projected)
