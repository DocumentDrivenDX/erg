"""Opt-in loopback inference capture; request bodies are durable before forwarding."""

import contextlib
import http.client
import http.server
import json
import time
import uuid
from urllib.parse import urlsplit

from . import collect, history, store


def usage_from(response):
    """Normalize terminal OpenAI, Responses and Anthropic usage without guessing."""
    documents = []
    try:
        documents = [json.loads(response)]
    except (ValueError, UnicodeDecodeError):
        for line in response.splitlines():
            if line.startswith(b"data:"):
                try:
                    documents.append(json.loads(line[5:].strip()))
                except (ValueError, UnicodeDecodeError):
                    pass
    values = {}
    response_id = model = ""
    for d in documents:
        if not isinstance(d, dict):
            continue
        wrapped = (
            d.get("response")
            if isinstance(d.get("response"), dict)
            else d.get("message")
            if isinstance(d.get("message"), dict)
            else d
        )
        response_id = wrapped.get("id") or response_id
        model = wrapped.get("model") or model
        usage = wrapped.get("usage") or d.get("usage")
        if not isinstance(usage, dict):
            continue
        values.update(usage)
    if not any(k in values for k in ("input_tokens", "prompt_tokens")) or not any(
        k in values for k in ("output_tokens", "completion_tokens")
    ):
        return dict(known=False, response_id=response_id, model=model)
    inp = values.get("input_tokens", values.get("prompt_tokens", 0))
    out = values.get("output_tokens", values.get("completion_tokens", 0))
    cached = values.get("cache_read_input_tokens", 0) or (
        values.get("prompt_tokens_details") or values.get("input_tokens_details") or {}
    ).get("cached_tokens", 0)
    written = values.get("cache_creation_input_tokens", 0) or (
        values.get("prompt_tokens_details") or values.get("input_tokens_details") or {}
    ).get("cache_write_tokens", 0)
    # Anthropic input excludes caches; OpenAI-style input includes them.
    fresh = (
        inp
        if "cache_read_input_tokens" in values or "cache_creation_input_tokens" in values
        else max(0, inp - cached - written)
    )
    return dict(
        known=True,
        response_id=response_id,
        model=model,
        fresh_in=fresh,
        cache_read=cached,
        cache_write=written,
        out=out,
    )


def record_request(db, invocation, body, endpoint, session="", project=""):
    now = int(time.time())
    session = session or "inference:" + invocation
    project = project or "(direct local inference)"
    request_id = invocation
    event, _ = history.put(
        db,
        "local",
        session,
        project,
        now,
        "inference_request",
        body,
        invocation + ":request",
        origin="server_capture",
        source=endpoint,
    )
    db.execute(
        "INSERT INTO local_inferences(invocation_id,request_id,event_time,session,project,destination,model,state,request_event_id) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            invocation,
            request_id,
            now,
            session,
            project,
            endpoint,
            body.get("model") or "",
            "pending",
            event,
        ),
    )
    return session, project


def finish(db, invocation, response, status, elapsed, ttfb, error=None):
    row = db.execute(
        "SELECT session,project,event_time,destination,model FROM local_inferences WHERE invocation_id=?",
        (invocation,),
    ).fetchone()
    session, project, ts, endpoint, requested = row
    u = usage_from(response)
    response_id = u["response_id"]
    model = u["model"] or requested
    event, _ = history.put(
        db,
        "local",
        session,
        project,
        int(time.time()),
        "inference_response",
        dict(status_code=status, error=error, content=response.decode("utf-8", errors="replace")),
        invocation + ":response",
        origin="server_capture",
        source=endpoint,
    )
    db.execute(
        """UPDATE local_inferences SET response_id=?,model=?,status_code=?,execution_duration_ms=?,time_to_first_byte_ms=?,
     input_tokens=?,output_tokens=?,cache_read=?,cache_write=?,usage_known=?,state=?,error=?,response_event_id=? WHERE invocation_id=?""",
        (
            response_id,
            model,
            status,
            elapsed,
            ttfb,
            u.get("fresh_in"),
            u.get("out"),
            u.get("cache_read"),
            u.get("cache_write"),
            int(u["known"] and not error),
            "complete" if not error else "incomplete",
            error,
            event,
            invocation,
        ),
    )
    # The local call uses the actual response identity. Agent capture can then
    # reconcile the same invocation without counting the server receipt twice.
    rows = [
        collect.session_record(
            "local",
            session,
            ts,
            project,
            surface="local_proxy",
            usage_status="observed" if u["known"] else "unknown",
        )
    ]
    rows[0]["project"] = project
    if u["known"] and not error:
        rows.append(
            dict(
                t="llm",
                uid="local:" + invocation,
                ts=ts,
                tool="local",
                session=session,
                project=project,
                kind="main",
                client="local_proxy",
                provider="local",
                model=model,
                response_id=response_id,
                request_id="",
                usage_kind="observed",
                **{k: u[k] for k in ("fresh_in", "out", "cache_read", "cache_write")},
            )
        )
    store.ingest(db, map(json.dumps, rows), machine=collect.machine_name(), transaction=False)


def make_server(upstream, port=0, host="127.0.0.1"):
    target = urlsplit(upstream)
    if (
        target.scheme != "http"
        or target.hostname not in ("localhost", "127.0.0.1", "::1")
        or target.username
        or target.password
        or target.query
        or target.fragment
    ):
        raise ValueError("local capture upstream must be a loopback HTTP URL without credentials")
    if host not in ("127.0.0.1", "::1"):
        raise ValueError("local capture listener must bind loopback")
    if port and port == (target.port or 80):
        raise ValueError("capture and upstream ports must differ")
    # Initialize once before accepting requests, not at every streaming checkpoint.
    with contextlib.closing(store.connect()) as db:
        database = next(row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main")
    if not database:
        raise ValueError("local capture requires a persistent ledger")

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def proxy(self):
            if not self.path.startswith("/v1/") or self.path.startswith("//"):
                self.send_error(404)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400, "Invalid Content-Length")
                return
            if size < 0 or size > 32 * 1024 * 1024:
                self.send_error(413)
                return
            if self.headers.get("Transfer-Encoding"):
                self.send_error(411)
                return
            raw = self.rfile.read(size)
            invocation = uuid.uuid4().hex
            recorded = False
            status = None
            ttfb = None
            result = bytearray()
            started = time.monotonic()
            error = None
            sent = False
            capture_db = None
            try:
                if self.command == "POST":
                    body = json.loads(raw)
                    if not isinstance(body, dict):
                        raise ValueError("JSON object required")
                    capture_db = store.connect(database, initialize=False)
                    with capture_db:
                        record_request(
                            capture_db,
                            invocation,
                            body,
                            upstream,
                            self.headers.get("X-Erg-Session-Id", ""),
                            self.headers.get("X-Erg-Project", ""),
                        )
                    recorded = True
                connection = http.client.HTTPConnection(
                    target.hostname, target.port or 80, timeout=300
                )
                headers = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower()
                    not in (
                        "host",
                        "connection",
                        "content-length",
                        "transfer-encoding",
                        "x-erg-project",
                        "x-erg-session-id",
                    )
                }
                headers["Content-Length"] = str(len(raw))
                headers["Connection"] = "close"
                connection.request(
                    self.command, (target.path.rstrip("/") + self.path), raw, headers
                )
                response = connection.getresponse()
                status = response.status
                ttfb = int((time.monotonic() - started) * 1000)
                self.send_response(status)
                for k, v in response.getheaders():
                    if k.lower() not in ("connection", "transfer-encoding", "content-length"):
                        self.send_header(k, v)
                self.send_header("Connection", "close")
                self.send_header("X-Erg-Invocation-Id", invocation)
                self.end_headers()
                sent = True
                self.close_connection = True
                content_type = response.getheader("Content-Type", "")
                last_checkpoint = time.monotonic()
                checkpoint_offset = 0
                while True:
                    chunk = (
                        response.readline(65536)
                        if "text/event-stream" in content_type
                        else response.read(65536)
                    )
                    if not chunk:
                        break
                    result.extend(chunk)
                    if len(result) > 64 * 1024 * 1024:
                        raise ValueError("response capture exceeds 64 MiB limit")
                    if recorded and time.monotonic() - last_checkpoint >= 1:
                        with capture_db:
                            history.put(
                                capture_db,
                                "local",
                                self.headers.get("X-Erg-Session-Id") or "inference:" + invocation,
                                self.headers.get("X-Erg-Project") or "(direct local inference)",
                                int(time.time()),
                                "inference_stream_checkpoint",
                                dict(
                                    content=result[checkpoint_offset:].decode(
                                        "utf-8", errors="replace"
                                    )
                                ),
                                invocation + ":stream:" + str(checkpoint_offset),
                                origin="server_capture",
                                source=upstream,
                            )
                        checkpoint_offset = len(result)
                        last_checkpoint = time.monotonic()
                    self.wfile.write(chunk)
                    self.wfile.flush()
                connection.close()
            except Exception as exc:
                error = type(exc).__name__
                if not sent:
                    self.send_error(503, "Local capture/upstream unavailable")
            finally:
                if recorded:
                    try:
                        with capture_db:
                            finish(
                                capture_db,
                                invocation,
                                bytes(result),
                                status,
                                int((time.monotonic() - started) * 1000),
                                ttfb,
                                error,
                            )
                    except Exception:
                        # The durable pending receipt remains an explicit gap.
                        pass
                if capture_db is not None:
                    capture_db.close()

        do_POST = proxy
        do_GET = proxy

    server = http.server.ThreadingHTTPServer((host, int(port)), Handler)
    server.daemon_threads = True
    return server


def serve(upstream, port):
    server = make_server(upstream, port)
    print(
        "erg local inference capture: http://127.0.0.1:%s -> %s" % (server.server_port, upstream),
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
