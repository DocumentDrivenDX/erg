"""Bounded, derived hourly host metrics; raw rows remain the reporting oracle."""
import sqlite3
import time

RAW_SQL='''SELECT machine,src,m,ts/3600,iv,COUNT(*),SUM(v),MAX(COALESCE(pk,v))
 FROM metrics WHERE ts>=? GROUP BY 1,2,3,4,5'''


def dimensions(prefix):
    fields=[prefix+'.ts/3600']+[prefix+'.'+c for c in ('machine','src','m','iv')]
    # SQL literals frame NULLs and embedded separators without an identifier hash.
    key="||','||".join('quote('+c+')' for c in fields)
    return key+','+','.join(fields)


def initialize(db):
    for sql in (
        'CREATE TABLE IF NOT EXISTS metric_rollup_state(key TEXT PRIMARY KEY,value INT)',
        'CREATE TABLE IF NOT EXISTS metric_rollup_dirty(key TEXT PRIMARY KEY,hour INT,machine TEXT,src TEXT,m TEXT,iv INT)',
        'CREATE INDEX IF NOT EXISTS metric_rollup_dirty_hour ON metric_rollup_dirty(hour)',
        'CREATE TABLE IF NOT EXISTS metric_hour_tiers(key TEXT PRIMARY KEY,hour INT,machine TEXT,src TEXT,m TEXT,iv INT,n INT,total REAL,peak REAL)',
        "CREATE INDEX IF NOT EXISTS metric_rollup_noninteger_ts ON metrics(ts) WHERE typeof(ts) NOT IN ('integer','null')",
        'CREATE INDEX IF NOT EXISTS metric_hour_tiers_hour ON metric_hour_tiers(hour)'):
        db.execute(sql)
    for action,prefix in (('INSERT','NEW'),('DELETE','OLD')):
        db.execute('CREATE TRIGGER IF NOT EXISTS erg_metric_rollup_'+action.lower()+' AFTER '+action+' ON metrics BEGIN INSERT INTO metric_rollup_dirty VALUES('+dimensions(prefix)+') ON CONFLICT(key) DO NOTHING; END')
    db.execute('CREATE TRIGGER IF NOT EXISTS erg_metric_rollup_update AFTER UPDATE ON metrics BEGIN INSERT INTO metric_rollup_dirty VALUES('+dimensions('OLD')+') ON CONFLICT(key) DO NOTHING; INSERT INTO metric_rollup_dirty VALUES('+dimensions('NEW')+') ON CONFLICT(key) DO NOTHING; END')
    if not db.execute("SELECT 1 FROM metric_rollup_state WHERE key='seed_bound'").fetchone():
        bound=db.execute('SELECT COALESCE(MAX(rowid),0) FROM metrics').fetchone()[0]
        db.executemany('INSERT INTO metric_rollup_state VALUES(?,?)',[('seed_bound',bound),('seed_cursor',0),('seed_complete',int(bound==0)),('compatible',1)])


def bounds(hour):
    # Preserve SQLite's existing integer division for pre-epoch samples too.
    if hour>0:return hour*3600,(hour+1)*3600
    if hour<0:return (hour-1)*3600+1,hour*3600+1
    return -3599,3600


def run(db,seed_limit=5000,limit=256,budget=.005):
    if seed_limit<1 or limit<1 or budget<0:raise ValueError("require positive batch limits and nonnegative CPU budget")
    start=time.thread_time();seeded=processed=0
    state=dict(db.execute('SELECT key,value FROM metric_rollup_state'))
    compatible=not db.execute("SELECT 1 FROM metrics WHERE typeof(ts) NOT IN ('integer','null') LIMIT 1").fetchone()
    if state['compatible']!=int(compatible):
        with db:
            if not db.in_transaction:db.execute('BEGIN IMMEDIATE')
            compatible=not db.execute("SELECT 1 FROM metrics WHERE typeof(ts) NOT IN ('integer','null') LIMIT 1").fetchone()
            db.execute("UPDATE metric_rollup_state SET value=? WHERE key='compatible'",(int(compatible),))
    if not state['seed_complete']:
        with db:
            if not db.in_transaction:db.execute('BEGIN IMMEDIATE')
            # Re-read progress under the writer lock; a second worker cannot
            # acknowledge past another worker's uncommitted seed batch.
            current=dict(db.execute('SELECT key,value FROM metric_rollup_state'))
            last,count=db.execute('SELECT max(rowid),count(*) FROM (SELECT rowid FROM metrics WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT ?)',(current['seed_cursor'],current['seed_bound'],seed_limit)).fetchone()
            if count:
                db.execute('INSERT OR IGNORE INTO metric_rollup_dirty SELECT '+dimensions('s')+' FROM metrics s WHERE s.rowid>? AND s.rowid<=?',(current['seed_cursor'],last))
                db.execute("UPDATE metric_rollup_state SET value=? WHERE key='seed_cursor'",(last,));seeded=count
            else:db.execute("UPDATE metric_rollup_state SET value=1 WHERE key='seed_complete'")
    if time.thread_time()-start<budget and db.execute('SELECT 1 FROM metric_rollup_dirty LIMIT 1').fetchone():
        with db:
            if not db.in_transaction:db.execute('BEGIN IMMEDIATE')
            while processed<limit and time.thread_time()-start<budget:
                item=db.execute('SELECT key,hour,machine,src,m,iv FROM metric_rollup_dirty ORDER BY hour LIMIT 1').fetchone()
                if not item:break
                key,hour,machine,src,metric,iv=item
                if hour is not None and not isinstance(hour,int):
                    db.execute('DELETE FROM metric_hour_tiers WHERE key=?',(key,))
                    db.execute('DELETE FROM metric_rollup_dirty WHERE key=?',(key,));processed+=1;continue
                where='machine IS ? AND src IS ? AND m IS ? AND iv IS ?';args=(machine,src,metric,iv)
                if hour is None:where+=' AND ts IS NULL'
                else:
                    low,high=bounds(hour);where+=' AND ts>=? AND ts<?';args+=(low,high)
                n,total,peak=db.execute('SELECT count(*),sum(v),max(coalesce(pk,v)) FROM metrics WHERE '+where,args).fetchone()
                if n:db.execute('INSERT OR REPLACE INTO metric_hour_tiers VALUES(?,?,?,?,?,?,?,?,?)',item+(n,total,peak))
                else:db.execute('DELETE FROM metric_hour_tiers WHERE key=?',(key,))
                db.execute('DELETE FROM metric_rollup_dirty WHERE key=?',(key,));processed+=1
    state=dict(db.execute('SELECT key,value FROM metric_rollup_state'))
    pending=db.execute('SELECT count(*) FROM metric_rollup_dirty').fetchone()[0]
    return dict(seeded=seeded,seed_cursor=state['seed_cursor'],seed_bound=state['seed_bound'],seed_complete=bool(state['seed_complete']),pending=pending,processed=processed,compatible=bool(state['compatible']),ready=bool(state['seed_complete'] and state['compatible'] and not pending),cpu_seconds=round(time.thread_time()-start,6))


def tiers(db,since=0):
    """Use only proven-current cache rows; otherwise stream the raw relation."""
    own=isinstance(db,sqlite3.Connection) and not db.in_transaction;cursor=None
    if own:db.execute('BEGIN')
    try:
        ready=False
        if isinstance(db,sqlite3.Connection) and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metric_rollup_state'").fetchone():
            state=dict(db.execute('SELECT key,value FROM metric_rollup_state'))
            hour=int(since/3600)
            ready=state.get('seed_complete') and state.get('compatible') and not db.execute("SELECT 1 FROM metrics WHERE typeof(ts) NOT IN ('integer','null') LIMIT 1").fetchone() and not db.execute('SELECT 1 FROM metric_rollup_dirty WHERE hour>=? LIMIT 1',(hour,)).fetchone()
        if not ready:cursor=db.execute(RAW_SQL,(since,))
        else:
            hour=int(since/3600);low,high=bounds(hour)
            if since==low:
                cursor=db.execute('SELECT machine,src,m,hour,iv,n,total,peak FROM metric_hour_tiers WHERE hour>=? ORDER BY 1,2,3,4,5',(hour,))
            else:
                cursor=db.execute('''SELECT * FROM (
 SELECT machine,src,m,hour,iv,n,total,peak FROM metric_hour_tiers WHERE hour>?
 UNION ALL SELECT machine,src,m,ts/3600,iv,count(*),sum(v),max(coalesce(pk,v))
 FROM metrics WHERE ts>=? AND ts<? GROUP BY 1,2,3,4,5) ORDER BY 1,2,3,4,5''',(hour,since,high))
        yield from cursor
    finally:
        if cursor is not None and hasattr(cursor,'close'):cursor.close()
        if own:db.rollback()
