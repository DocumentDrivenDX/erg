#!/usr/bin/env python3
"""Verify a real installation from an isolated home/cwd with synthetic data only.

Run with the installed environment's Python and -I. No build/runtime dependency,
agent credential, existing ledger, source checkout import or cloud call is needed.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

PROBE = r'''
import importlib.metadata as metadata
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import erg
from erg import collect, history, rate_catalog, rates, store

package = Path(erg.__file__).resolve().parent
assert 'site-packages' in str(package), package
assert metadata.version('erg') == '0.1.0rc1'
assert not metadata.requires('erg'), metadata.requires('erg')
assert (package/'static/index.html').is_file()
assert (package/'static/databricks.html').is_file()
assert (package/'hooks/pi.mjs').is_file()
card = rates.load()
rate_catalog.validate(card)
assert card['entries'] and card['source_sha256']
# Import every installed runtime module; cloud SDKs must stay behind explicit use.
for path in package.glob('*.py'):
    if path.stem != '__init__':
        __import__('erg.' + path.stem)
command = [sys.executable, '-I', '-B', '-m', 'erg.cli']
entry = Path(sys.executable).parent/'erg'
assert entry.is_file()
subprocess.run([str(entry), '--help'], check=True, capture_output=True, timeout=30)
subprocess.run(command + ['sources'], check=True, capture_output=True, timeout=30)
# Exercise the one-shot entrypoint first on an empty installation.
subprocess.run(command + ['collect'], check=True, capture_output=True, timeout=60)
assert not history.config().get('content')
# A native Pi fixture exercises the installed portable collector and store.
stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
logs = Path(collect.HOME)/'.pi/agent/sessions'
logs.mkdir(parents=True, exist_ok=True)
events = [dict(type='session', id='smoke-session', timestamp=stamp, cwd='/code/example', version=3),
    dict(type='message', id='user', timestamp=stamp, message=dict(role='user', content='SYNTHETIC_PRIVATE_PROMPT')),
    dict(type='message', id='assistant', timestamp=stamp, message=dict(role='assistant', content='SYNTHETIC_PRIVATE_RESPONSE', provider='fixture', model='fixture-model', usage=dict(input=10, cacheRead=20, cacheWrite=3, output=7)))]
(logs/'smoke.jsonl').write_text(''.join(json.dumps(event)+'\n' for event in events))
captured = subprocess.run([sys.executable, '-I', '-B', str(package/'collect.py'), 'collect', '--days', '1', '--no-git', '--no-metrics'], check=True, capture_output=True, text=True, timeout=60)
assert 'SYNTHETIC_PRIVATE_' not in captured.stdout
with store.connect() as db:
    store.ingest(db, captured.stdout.splitlines())
    store.ingest(db, captured.stdout.splitlines())
    assert db.execute('SELECT count(*), sum(out) FROM calls').fetchone() == (1, 7)
    assert db.execute('SELECT count(*) FROM history_events WHERE body IS NOT NULL').fetchone()[0] == 0
    data = store.dataset(db)
    assert data['llm'], data.keys()
# Loopback server smoke: UI assets, health, reports and privacy defaults.
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
server = subprocess.Popen(command + ['serve', '--port', str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
try:
    base = 'http://127.0.0.1:' + str(port)
    deadline = time.monotonic()+30
    while True:
        try:
            with urllib.request.urlopen(base+'/healthz', timeout=2) as response:
                assert json.load(response)['status'] == 'ok'
            break
        except (urllib.error.URLError, TimeoutError):
            if server.poll() is not None:
                raise AssertionError(server.stderr.read().decode())
            if time.monotonic() > deadline:
                raise AssertionError('reporter did not start')
            time.sleep(.1)
    for path in ('/', '/api/data', '/api/history/status', '/api/delivery/status'):
        with urllib.request.urlopen(base+path, timeout=30) as response:
            raw = response.read()
            assert response.status == 200 and raw, path
            assert b'SYNTHETIC_PRIVATE_' not in raw, path
            if path == '/':
                assert raw == (package/'static/index.html').read_bytes()
finally:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill();server.wait(timeout=10)
    server.stderr.close()
print(json.dumps(dict(version=metadata.version('erg'), python=sys.version.split()[0], installed=True, runtime_dependencies=[], synthetic_calls=1, output_tokens=7, bodies_retained=0, http=True, bundled_rates=len(card['entries']))))
'''


def main():
    with tempfile.TemporaryDirectory(prefix='erg-install-smoke-') as temp:
        # Deliberate subprocess isolation: never inherit agent/cloud discovery or
        # credentials from the operator's home, PATH or ERG_* environment.
        env = {'HOME': temp, 'PATH': str(Path(sys.executable).parent)+os.pathsep+'/usr/bin:/bin',
               'ERG_HOME': str(Path(temp)/'ledger'), 'ERG_STATE_DIR': str(Path(temp)/'state'),
               'ERG_DATABASE': str(Path(temp)/'ledger/erg-v2.db'),
               'PYTHONDONTWRITEBYTECODE': '1', 'LANG': 'en_US.UTF-8'}
        subprocess.run([sys.executable, '-I', '-B', '-c', PROBE], cwd=temp, env=env, check=True, timeout=180)


if __name__ == '__main__':
    main()
