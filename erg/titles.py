"""Private display metadata; titles never determine identity or attribution."""
import contextlib
import glob
import hashlib
import math
import json
from pathlib import Path
import sqlite3
import time
from . import collect

SCHEMA='''CREATE TABLE IF NOT EXISTS session_titles(machine TEXT,tool TEXT,session TEXT,title TEXT,source TEXT,
 priority INT,updated_at INT,PRIMARY KEY(machine,tool,session));'''


def initialize(db):db.executescript(SCHEMA)


def put(db,tool,session,title,source,priority=20,updated_at=None):
    if not isinstance(title,str) or not title.strip() or not session:return
    title=' '.join(title.split())[:4000]
    db.execute('''INSERT INTO session_titles VALUES(?,?,?,?,?,?,?) ON CONFLICT(machine,tool,session)
     DO UPDATE SET title=excluded.title,source=excluded.source,priority=excluded.priority,updated_at=excluded.updated_at
     WHERE excluded.priority>session_titles.priority OR
     (excluded.priority=session_titles.priority AND excluded.updated_at>=session_titles.updated_at AND
      (excluded.updated_at>session_titles.updated_at OR excluded.title IS NOT session_titles.title OR excluded.source IS NOT session_titles.source))''',
     (collect.machine_name(),tool,session,title,source,priority,int(updated_at or time.time())))


def native(db,tool,session,record):
    title=None
    if tool=='claude' and record.get('type')=='custom-title':title=record.get('customTitle')
    if tool=='pi' and record.get('type')=='session_info':title=record.get('name')
    if title:put(db,tool,session,title,'native_title',40,collect.parse_ts(record.get('timestamp')))


def rows(db):
    return [dict(zip(('machine','tool','session','title','source'),r)) for r in db.execute('SELECT machine,tool,session,title,source FROM session_titles')]


def refresh(db,cache=None):
    """Read metadata outside writer locks; acknowledge progress after commit."""
    cache={} if cache is None else cache
    errors=read_sources=unchanged=read_rows=incremental_sources=0
    updates={};active=set();pending=[];now=time.monotonic()
    def signature(path):
        result=[]
        for item in (path,Path(str(path)+'-wal')):
            try:
                stat=item.stat();result.append((stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns))
            except FileNotFoundError:result.append(None)
        return tuple(result)
    def offer(tool,sid,title,source,priority,updated):
        if isinstance(title,str) and title.strip() and sid:
            pending.append((tool,sid,' '.join(title.split())[:4000],source,priority,collect.parse_ts(updated)))
    def read(path,reader):
        nonlocal errors,read_sources,unchanged
        active.add(str(path))
        try:
            stamp=signature(path);previous=cache.get(str(path))
            if not isinstance(previous,dict):previous=None
            full=not previous or now-previous['full_at']>=300
            if previous and previous['signature']==stamp and not full:
                unchanged+=1;return
            old_stamp=previous['signature'] if previous else None
            same_file=bool(old_stamp and old_stamp[0] and stamp[0] and old_stamp[0][:2]==stamp[0][:2] and stamp[0][2]>=old_stamp[0][2])
            before=errors;meta,complete=reader(path,previous.get('meta',{}) if previous and same_file and not full else {})
            read_sources+=1
            if errors==before:updates[str(path)]=dict(signature=stamp,checked_at=now,full_at=now if complete else previous['full_at'],meta=meta)
        except (OSError,sqlite3.Error,ValueError,OverflowError):errors+=1
    def codex_db(path,previous):
        nonlocal read_rows,incremental_sources
        with contextlib.closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1)) as source:
            source.execute('BEGIN')
            cols={r[1] for r in source.execute('PRAGMA table_info(threads)')}
            if not {'id','title'}<=cols:return {},True
            stamp='updated_at_ms' if 'updated_at_ms' in cols else 'updated_at' if 'updated_at' in cols else None
            incremental=stamp is not None and previous.get('stamp')==stamp and isinstance(previous.get('watermark'),(int,float))
            fields='id,title,'+('name' if 'name' in cols else 'NULL')+','+('updated_at' if 'updated_at' in cols else stamp or '0')+','+(stamp or '0')
            sql='SELECT '+fields+' FROM threads';params=()
            if incremental:sql+=' WHERE '+stamp+'>=?';params=(previous['watermark'],);incremental_sources+=1
            watermark=previous['watermark'] if incremental else None;numeric=True
            for sid,title,name,updated,water in source.execute(sql,params):
                read_rows+=1;offer('codex',sid,name or title,'codex_thread_metadata',50 if name else 20,updated)
                if isinstance(water,(int,float)) and math.isfinite(water):watermark=max(watermark,water) if watermark is not None else water
                else:numeric=False
            return dict(stamp=stamp,watermark=watermark if numeric else None),not incremental
    def codex_index(path,previous):
        nonlocal errors,read_rows,incremental_sources
        offset=previous.get('offset',0)
        # Size-preserving rewrites cannot be mistaken for an empty append.
        incremental=bool(offset and path.stat().st_size>offset)
        with path.open('rb') as stream:
            if incremental:
                stream.seek(max(0,offset-128));tail=stream.read(min(128,offset))
                incremental=hashlib.sha256(tail).hexdigest()==previous.get('tail_hash')
            if incremental:stream.seek(offset);incremental_sources+=1
            else:stream.seek(0)
            while True:
                line=stream.readline()
                if not line:break
                read_rows+=1
                try:record=json.loads(line)
                except ValueError:errors+=1;continue
                if isinstance(record,dict):offer('codex',record.get('id'),record.get('thread_name'),'codex_session_index',30,record.get('updated_at'))
            offset=stream.tell();stream.seek(max(0,offset-128));tail=stream.read(min(128,offset))
        return dict(offset=offset,tail_hash=hashlib.sha256(tail).hexdigest()),not incremental
    def omnigent_db(path,previous):
        nonlocal read_rows
        with contextlib.closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1)) as source:
            source.execute('BEGIN')
            cols={r[1] for r in source.execute('PRAGMA table_info(conversations)')}
            if {'id','title','updated_at'}<=cols:
                names={r[0].lower().replace('-',''):(r[1],r[2]) for r in source.execute('SELECT hex(id),title,updated_at FROM conversations') if r[1]}
                read_rows+=len(names)
                for tool,sid,cid in db.execute("SELECT tool,session,conversation_id FROM report_sessions WHERE machine=? AND conversation_id IS NOT NULL",(collect.machine_name(),)):
                    match=names.get(cid.lower().replace('-',''))
                    if match:offer(tool,sid,match[0],'omnigent_conversation_title',10,match[1])
        return {},True
    homes={str(Path(p).parent) for p in collect.native_roots('codex')}
    for home in sorted(homes):
        for path in glob.glob(str(Path(home)/'state_*.sqlite')):read(Path(path),codex_db)
        path=Path(home)/'session_index.jsonl'
        if path.exists():read(path,codex_index)
    path=Path(collect.omni_home())/'chat.db'
    if path.exists():read(path,omnigent_db)
    # Source reads, parsing and directory discovery cannot hold the ledger writer.
    with db:
        for row in pending:put(db,*row)
    cache.update(updates)
    for key in list(cache):
        if key not in active:cache.pop(key,None)
    return dict(errors=errors,read_sources=read_sources,unchanged_sources=unchanged,read_rows=read_rows,incremental_sources=incremental_sources)
