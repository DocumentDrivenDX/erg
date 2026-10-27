"""Synthetic capture-to-canonical proof; never opens the operator's local corpus."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from erg import store, history, delivery_journal as journal
from central.erg_central.receiver import Receiver, FileStore
from central.erg_central.canonical import Canonical


def prove():
    identity = dict(enrollment_id='synthetic-device',installation_id='synthetic-install',destination_generation='synthetic-generation')
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='erg-delivery-proof-') as temporary:
        root = Path(temporary)
        local = store.connect(str(root/'local.db'))
        central = sqlite3.connect(str(root/'canonical.db'))
        try:
            journal.enroll(local,identity)
            for tool in ('claude','codex','pi'):
                source = dict(type='message',role='user',content='Synthetic proof for '+tool, authorization='fixture-secret-excluded')
                body = history.safe_event(tool,source)
                assert 'authorization' not in body
                with local: history.put(local,tool,'synthetic-'+tool,'synthetic-project',int(time.time()),'message',body,'entry',machine='synthetic-host')
            while journal.prepare_one(local): pass
            receiver = Receiver(FileStore(root/'spool')); canonical = Canonical(central)
            batches = 0; metrics=[]
            while True:
                batch = journal.next_batch(local)
                if not batch: break
                ident, raw, sha = batch
                receipt = receiver.accept(raw,sha,dict(identity,active=True))
                initial_metrics=dict(receiver.metrics)
                # Duplicate retry after a lost response has identical acceptance.
                assert receipt == receiver.accept(raw,sha,dict(identity,active=True))
                metrics.append(dict(first_acceptance=initial_metrics,replay=dict(receiver.metrics)))
                journal.accept(local,ident,receipt); batches += 1
            accepted = journal.status(local)
            assert accepted['accepted_through'] == 3 and accepted['materialized_through'] == 0
            # Fresh scan, no notification state survives: all accepted bodies recover.
            for value,_ in Receiver(FileStore(root/'spool')).batches():
                canonical.apply(value); canonical.apply(value)
            remote = canonical.status(identity)
            journal.reconcile_status(local,remote)
            heads = central.execute('SELECT payload FROM canonical_heads').fetchall()
            assert len(heads) == 3
            for row in heads:
                record = json.loads(row[0]); assert record['body']['content'] == 'Synthetic proof for '+record['tool']
            status = journal.status(local)
            assert status['materialized_through'] == 3
            return dict(mode='local synthetic only',tools=['claude','codex','pi'],captured_events=3,
                canonical_events=len(heads),duplicate_canonical_events=0,batches=batches,broker_metrics=metrics,
                accepted_through=status['accepted_through'],materialized_through=status['materialized_through'],
                projected_through=status['projected_through'],
                projection_policy='raw-session-v1 not_applicable; no inference/cost projection fabricated',
                assertions=['native body round trip','credential envelope exclusion','exact retry acknowledgement',
                            'independent spool scan','canonical replay invariance','own-generation receipt reconciliation'],
                not_proven=['live Entra enrollment','Azure/UC private route','Zerobus visibility','Delta execution',
                            'central reporting parity','10x central throughput','fleet deployment'],
                elapsed_seconds=time.monotonic()-started)
        finally: local.close(); central.close()


if __name__ == '__main__': print(json.dumps(prove(),indent=2))
