"""Immutable UC rate catalogs; local reports use an atomic offline snapshot."""
import hashlib
import json
import math
import os
from pathlib import Path
import time
import tempfile
import urllib.parse
from . import collect,databricks


def directory():return Path(collect.STATE_DIR)/'rate-catalog'
def clean(card):return {k:v for k,v in card.items() if not k.startswith('_') and k!='catalog_revision'}
def revision(card):return hashlib.sha256(json.dumps(clean(card),sort_keys=True,separators=(',',':')).encode()).hexdigest()

def validate(card):
    from . import accounting
    if not isinstance(card,dict) or not isinstance(card.get('entries'),list) or not isinstance(card.get('aliases',{}),dict):raise ValueError('invalid rate catalog')
    if len(json.dumps(card,allow_nan=False).encode())>4*1024*1024:raise ValueError('rate catalog exceeds bound')
    if any(not isinstance(k,str) or not isinstance(v,str) or not k or not v for k,v in card.get('aliases',{}).items()):raise ValueError('invalid model aliases')
    for entry in card['entries']:
        if not isinstance(entry,dict) or not isinstance(entry.get('tiers',[]),list):raise ValueError('invalid model rate')
        if not all(isinstance(entry.get(k),str) and entry[k] for k in ('model','as_of','source')):raise ValueError('missing rate provenance')
        for price in [entry]+entry.get('tiers',[]):
            if not isinstance(price,dict):raise ValueError('invalid context tier')
            if price is not entry and (accounting.number(price.get('min_prompt_tokens')) is None or price['min_prompt_tokens']<0):raise ValueError('invalid context threshold')
            for k in ('input','output','cache_read','cache_write_5m','cache_write_1h','cache_write'):
                if k in price and (accounting.number(price[k]) is None or price[k]<0):raise ValueError('invalid rate')
        if any(k not in entry for k in ('input','output')):raise ValueError('input and output rates required')
    return card

def publish(client,target,card):
    """Only create application-owned tables in the explicitly selected schema."""
    card=validate(clean(card));rev=revision(card);ns=databricks.namespace(target)
    client.sql('CREATE TABLE IF NOT EXISTS '+ns+'.rate_catalog (revision STRING, ordinal INT, model STRING, channel STRING, as_of STRING, input_usd_per_million DOUBLE, output_usd_per_million DOUBLE, rate_json STRING) USING DELTA')
    client.sql('CREATE TABLE IF NOT EXISTS '+ns+'.rate_catalog_versions (revision STRING, published_at TIMESTAMP, catalog_json STRING) USING DELTA')
    rows=[dict(ordinal=i,model=e['model'],channel=e.get('channel',''),as_of=e['as_of'],input=e['input'],output=e['output'],spec=json.dumps(e,sort_keys=True)) for i,e in enumerate(card['entries'])]
    client.sql('''MERGE INTO %s.rate_catalog t USING (SELECT :r revision,v.* FROM explode(from_json(:rows,'ARRAY<STRUCT<ordinal:INT,model:STRING,channel:STRING,as_of:STRING,input:DOUBLE,output:DOUBLE,spec:STRING>>')) x(v)) s ON t.revision=s.revision AND t.ordinal=s.ordinal WHEN NOT MATCHED THEN INSERT (revision,ordinal,model,channel,as_of,input_usd_per_million,output_usd_per_million,rate_json) VALUES(s.revision,s.ordinal,s.model,s.channel,s.as_of,s.input,s.output,s.spec)'''%ns,[databricks.parameter('r',rev),databricks.parameter('rows',json.dumps(rows))])
    # Publish the manifest only after all immutable rows are visible.
    client.sql('MERGE INTO '+ns+'.rate_catalog_versions t USING (SELECT :r revision,:c catalog_json) s ON t.revision=s.revision WHEN NOT MATCHED THEN INSERT (revision,published_at,catalog_json) VALUES(s.revision,current_timestamp(),s.catalog_json)',[databricks.parameter('r',rev),databricks.parameter('c',json.dumps(card,sort_keys=True))])
    return rev

def pull(client,target):
    ns=databricks.namespace(target)
    rows=client.sql('SELECT revision,catalog_json FROM '+ns+'.rate_catalog_versions ORDER BY published_at DESC,revision LIMIT 1')
    if not rows:raise ValueError('rate catalog is empty')
    rev,raw=rows[0];card=validate(json.loads(raw))
    if revision(card)!=rev:raise ValueError('rate catalog digest mismatch')
    specs=client.sql('SELECT ordinal,model,channel,as_of,input_usd_per_million,output_usd_per_million,rate_json FROM '+ns+'.rate_catalog WHERE revision=:r ORDER BY ordinal',[databricks.parameter('r',rev)])
    if len(specs)!=len(card['entries']):raise ValueError('incomplete rate catalog revision')
    for i,row in enumerate(specs):
        e=card['entries'][i]
        expected=[i,e['model'],e.get('channel',''),e['as_of'],e['input'],e['output']]
        if len(row)!=7 or list(row[:6])!=expected or json.loads(row[6])!=e:raise ValueError('rate catalog row differs from manifest')
    directory().mkdir(mode=0o700,parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.catalog-',dir=str(directory()));tmp=Path(name)
    try:
        with os.fdopen(fd,'w') as f:
            os.chmod(tmp,0o600);json.dump(dict(card,catalog_revision=rev),f,sort_keys=True);f.flush();os.fsync(f.fileno())
        os.replace(tmp,directory()/'current.json')
    finally:
        if tmp.exists():tmp.unlink()
    return dict(revision=rev,models=len(specs))

def _host(value):
    if not isinstance(value,str):return None
    try:
        u=urllib.parse.urlsplit(value)
        if u.scheme!='https' or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in ('','/'):return None
        u.port  # Validate malformed/non-numeric ports without accepting new targets.
    except ValueError:return None
    return 'https://'+u.netloc.lower()


def _marker(value):
    root=directory();root.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,name=tempfile.mkstemp(prefix='.enabled-',dir=str(root));tmp=Path(name)
    try:
        with os.fdopen(fd,'w') as stream:
            os.fchmod(stream.fileno(),0o600);json.dump(value,stream,sort_keys=True);stream.flush();os.fsync(stream.fileno())
        os.replace(tmp,root/'enabled.json')
    finally:
        if tmp.exists():tmp.unlink()


def enable(profile,target,host):
    """Bind automatic pulls to the configured workspace and native profile."""
    databricks.namespace(target);host=_host(host)
    if not host or profile is not None and (not isinstance(profile,str) or not profile):raise ValueError('rate source requires a workspace and native profile')
    _marker(dict(version=1,namespace=target,host=host,profile=profile))


def refresh(client,target,host=None):
    """Opt-in source binding; at most one attempt per 15 minutes per worker."""
    marker=directory()/'enabled.json'
    try:
        if marker.stat().st_size>65536:return
        cfg=json.loads(marker.read_text())
    except (OSError,ValueError):return
    # Legacy namespace-only settings cannot select an arbitrary workspace. They
    # need explicit re-enablement against the configured reporting connection.
    if not isinstance(cfg,dict) or cfg.get('version')!=1 or cfg.get('namespace')!=target or not _host(host) or _host(cfg.get('host'))!=_host(host) or cfg.get('profile')!=getattr(client,'profile',None):return
    now=time.time();previous=cfg.get('last_attempt',0)
    if not isinstance(previous,(int,float)) or isinstance(previous,bool) or not math.isfinite(previous):return
    if now-previous<900:return
    cfg['last_attempt']=now;_marker(cfg)
    try:result=pull(client,target);cfg.update(result,last_success=now,error=None)
    except Exception as e:cfg['error']=type(e).__name__
    _marker(cfg)
