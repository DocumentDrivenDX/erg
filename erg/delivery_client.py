"""Independent bounded delivery worker. No cloud SDK or collector/report imports."""
import json
import random
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from . import delivery_journal as journal
from . import delivery_protocol as wire


class DeliveryError(ValueError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def endpoint(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or
        parsed.query or parsed.fragment or parsed.path not in ('', '/')):
        raise DeliveryError('delivery requires an HTTPS origin')
    return url.rstrip('/')


def helper_token(argv):
    """Managed helper writes only its access token to stdout; never use a shell."""
    if not isinstance(argv, list) or not argv or any(not isinstance(v, str) for v in argv):
        raise DeliveryError('invalid token helper')
    # Keep credentials in memory, cap output while reading, and enforce a deadline.
    import os
    import selectors
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True)
    except OSError:
        raise DeliveryError('token helper unavailable') from None
    raw = b''; deadline = time.monotonic() + 10
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise DeliveryError('token helper timed out')
            chunk = os.read(process.stdout.fileno(), 16385 - len(raw))
            if not chunk: break
            raw += chunk
            if len(raw) > 16384: raise DeliveryError('oversized token helper output')
        try: result = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired: raise DeliveryError('token helper timed out') from None
        if result or not raw: raise DeliveryError('token helper failed')
    finally:
        selector.close(); process.stdout.close()
        if process.poll() is None: process.kill(); process.wait()
    try: token = raw.decode('ascii').strip()
    except UnicodeError: raise DeliveryError('invalid token helper output') from None
    if not token or any(v.isspace() for v in token):
        raise DeliveryError('invalid token helper output')
    return token


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Authorization must never be forwarded to a redirect destination.
        return None


class HTTPS:
    def __init__(self, origin, token_provider):
        self.origin = endpoint(origin); self.token_provider = token_provider
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, path, data=None, digest=None):
        if not path.startswith('/v1/') or '://' in path:raise DeliveryError('invalid broker route')
        headers={'Authorization':'Bearer '+self.token_provider()}
        if data is not None:headers['Content-Type']='application/json'
        if digest is not None:headers['X-Erg-Batch-SHA256']=digest
        request=urllib.request.Request(self.origin+path,data=data,headers=headers,method='POST' if data is not None else 'GET')
        expected=202 if data is not None else 200
        try:
            with self.opener.open(request,timeout=30) as response:
                if response.status!=expected:raise DeliveryError('unexpected broker response')
                maximum=65536 if data is not None else 8*1024*1024
                body=response.read(maximum+1)
        except urllib.error.HTTPError as error:
            raise DeliveryError('broker HTTP %d'%error.code,code=error.code) from None
        except (OSError,urllib.error.URLError):raise DeliveryError('broker unavailable') from None
        if len(body)>maximum:raise DeliveryError('oversized broker response')
        return wire.decode(body)

    def submit(self,raw,digest):
        return self.request('/v1/batches',data=raw,digest=digest)

    def status(self,batch_id=None,limit=100,cursor=None):
        query=dict(limit=limit)
        if batch_id is not None:query['batch_id']=batch_id
        if cursor is not None:query['cursor']=cursor
        return self.request('/v1/delivery/status?'+urllib.parse.urlencode(query))


def send_once(db,transport,batch_id,raw,sha):
    """Durable attempt accounting; bytes offered are never billed/wire bytes."""
    now=time.time();day=int(now)//86400;started=time.monotonic()
    with db:
        attempt=db.execute('INSERT INTO delivery_http_attempts(ts,batch_id,body_bytes,outcome) VALUES(?,?,?,?)',(int(now),batch_id,len(raw),'started')).lastrowid
        db.execute('INSERT INTO delivery_http_daily(day,attempts,offered_body_bytes) VALUES(?,1,?) ON CONFLICT(day) DO UPDATE SET attempts=attempts+1,offered_body_bytes=offered_body_bytes+excluded.offered_body_bytes',(day,len(raw)))
    code=None
    try:
        receipt=transport.submit(raw,sha);code=202
        journal.accept(db,batch_id,receipt)
        with db:
            db.execute("UPDATE delivery_http_attempts SET http_status=202,outcome='accepted',elapsed_ms=? WHERE id=?",((time.monotonic()-started)*1000,attempt))
            db.execute('UPDATE delivery_http_daily SET accepted_responses=accepted_responses+1,elapsed_ms=elapsed_ms+? WHERE day=?',((time.monotonic()-started)*1000,day))
        return receipt
    except Exception as error:
        with db:db.execute("UPDATE delivery_http_attempts SET http_status=?,outcome='failed',elapsed_ms=? WHERE id=?",(getattr(error,'code',code),(time.monotonic()-started)*1000,attempt))
        raise


def step(db, transport, now=None):
    now = time.time() if now is None else now
    local = journal.status(db)
    if local.get('blocked_reason'): return 'blocked'
    batch = journal.next_batch(db, now)
    if not batch:
        return 'prepared' if journal.prepare_one(db) else 'idle'
    batch_id, raw, sha = batch
    try:
        send_once(db,transport,batch_id,raw,sha)
        return 'accepted'
    except (DeliveryError, wire.ProtocolError) as error:
        count = db.execute('SELECT attempts FROM delivery_batches WHERE batch_id=?', (batch_id,)).fetchone()[0] + 1
        delay = min(300, 2 ** min(count, 8)) * random.uniform(0.8, 1.2)
        blocked = getattr(error, 'code', None) in (403,409,413)
        with db:
            if getattr(error, 'code', None) == 403:
                db.execute("UPDATE delivery_config SET blocked_reason='enrollment rejected; requalification required'")
            db.execute("UPDATE delivery_batches SET attempts=?,retry_at=?,blocked=?,last_error='delivery not acknowledged' WHERE batch_id=?",
                       (count, now + delay, int(blocked), batch_id))
        return 'blocked' if blocked else 'retry'


def run_worker(db, transport, stop, progress=None):
    """Own connection in calling thread; productive work has no fixed poll delay."""
    while not stop.is_set():
        state = step(db, transport)
        if progress: progress(dict(state=state))
        if state in ('idle', 'retry', 'blocked'): stop.wait(2)
