"""Repair metadata only on already-retained records with exact native UUIDs."""
import hashlib
import json
import os
from pathlib import Path
import time
import zlib
from . import collect,history


def lines_backwards(path):
    if str(path).endswith('.zst'):
        with collect.transcript(str(path)) as f:
            for line in f:
                if line.endswith(b'\n'):yield line
        return
    with open(path,'rb') as f:
        f.seek(0,2);position=f.tell();tail=b''
        if position:
            f.seek(position-1)
            if f.read(1)!=b'\n':
                discarded=0
                while position:
                    n=min(position,262144);position-=n;f.seek(position);chunk=f.read(n);discarded+=n
                    end=chunk.rfind(b'\n')
                    if end>=0:position+=end+1;break
                    if discarded>32*1024*1024:raise ValueError('oversized trailing record')
        while position:
            n=min(position,262144);position-=n;f.seek(position)
            parts=(f.read(n)+tail).split(b'\n');tail=parts[0]
            for line in reversed(parts[1:]):
                if line:yield line
            if len(tail)>32*1024*1024:raise ValueError('oversized source record')
        if tail:yield tail


def same_retained_fields(old,new):
    if not isinstance(old,dict) or not isinstance(new,dict):return old==new
    for key,value in old.items():
        if key not in new:return False
        # Content and usage are immutable for this operation: no new bodies or
        # later token counts can enter under the guise of metadata repair.
        if key in ('content','usage','payload'):
            if value!=new[key]:return False
        elif not same_retained_fields(value,new[key]):return False
    return True


def repair(db,apply=False,session='',budget=5):
    now=time.monotonic();groups={};result=dict(scope='Metadata on already-retained Claude records only; no history import',candidates=0,matched=0,repaired=0,unavailable=0,conflicts=0,deferred=0,applied=apply)
    query="SELECT event_id,machine,session,project,ts,kind,role,origin,source,content_hash,body FROM history_events WHERE tool='claude' AND capture_version<2 AND body IS NOT NULL AND expired_at IS NULL"
    params=[]
    if session:query+=' AND session=?';params.append(session)
    for row in db.execute(query,params):
        try:body=json.loads(zlib.decompress(row[10]))
        except (ValueError,zlib.error):continue
        uid=body.get('uuid')
        if not uid or body.get('type') not in ('user','assistant'):continue
        expected=hashlib.sha256(json.dumps([row[1],'claude',row[2],uid],separators=(',',':')).encode()).hexdigest()
        if expected!=row[0]:continue
        groups.setdefault(row[8],{})[uid]=(row,body);result['candidates']+=1
    roots=[os.path.realpath(p) for p in collect.claude_roots()];updates=[]
    for path,targets in groups.items():
        if time.monotonic()-now>budget:result['deferred']+=len(targets);continue
        real=os.path.realpath(path)
        if not real.endswith(('.jsonl','.jsonl.zst')) or not any(os.path.commonpath([real,r])==r for r in roots):
            result['unavailable']+=len(targets);continue
        remaining=dict(targets)
        try:
            for line in lines_backwards(real):
                if time.monotonic()-now>budget:break
                try:d=json.loads(line)
                except (ValueError,UnicodeDecodeError):continue
                uid=d.get('uuid') if isinstance(d,dict) else None
                if uid not in remaining:continue
                row,old=remaining.pop(uid);new=history.safe_event('claude',d)
                if not same_retained_fields(old,new):result['conflicts']+=1;continue
                if d.get('type')=='assistant' and not ((new.get('message') or {}).get('id') or new.get('requestId')):
                    result['conflicts']+=1;continue
                updates.append((row,new,uid));result['matched']+=1
                if not remaining:break
        except (OSError,ValueError,RuntimeError):result['unavailable']+=len(remaining);remaining={}
        result['deferred']+=len(remaining)
    if apply:
        for start in range(0,len(updates),100):
            with db:
                if not db.in_transaction:db.execute('BEGIN IMMEDIATE')
                for row,new,uid in updates[start:start+100]:
                    current=db.execute('SELECT content_hash,expired_at FROM history_events WHERE event_id=?',(row[0],)).fetchone()
                    if not current or current[0]!=row[9] or current[1] is not None:result['conflicts']+=1;continue
                    key,changed=history.put(db,'claude',row[2],row[3],row[4],row[5],new,uid,role=row[6],origin=row[7],source=row[8],machine=row[1])
                    if key!=row[0]:raise ValueError('metadata identity changed')
                    result['repaired']+=int(changed)
    return result
