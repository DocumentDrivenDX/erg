"""Persistent synthetic-only cloud qualification. Never opens the operator ledger.

Prepare pins exact batch bytes before enrollment. Submit needs the native Keychain
helper and the published synthetic registry. Verify only reconciles HTTPS receipts;
production SQL/hash/access evidence is supplied independently by infrastructure.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from erg import store,history,delivery_journal as journal,delivery_protocol as wire,delivery_client as client


def write(path,value):
    temporary=path.with_suffix('.tmp')
    temporary.write_bytes(wire.encode(value));os.chmod(temporary,0o600);os.replace(str(temporary),str(path))


def prepare(state):
    if state.exists():raise ValueError('new isolated qualification directory required')
    state.mkdir(parents=True,mode=0o700);os.chmod(state,0o700)
    identity=dict(enrollment_id='erg-synthetic-'+uuid.uuid4().hex[:16],installation_id=str(uuid.uuid4()),destination_generation=str(uuid.uuid4()))
    write(state/'identity-proposal.json',identity)
    db=store.connect(str(state/'synthetic.db'))
    try:
        journal.enroll(db,identity)
        for tool in ('claude','codex','pi'):
            for role in ('user','assistant'):
                content='SYNTHETIC ERG QUALIFICATION: '+tool+' '+role
                body=dict(type='message',message=dict(role=role,content=content))
                if role=='assistant':body['message']['usage']=dict(input_tokens=17,output_tokens=4)
                with db:history.put(db,tool,'synthetic-'+tool,'/synthetic/erg',int(time.time()),'message',body,role,role=role,machine='synthetic-qualification')
        with db:
            key,_=history.put(db,'pi','synthetic-chunked','/synthetic/erg',int(time.time()),'message',dict(content='SYNTHETIC-LARGE-'+('x'*2500000)),'large',machine='synthetic-qualification')
            # Same source seq, new immutable revision; both must survive delivery.
            db.execute("UPDATE history_events SET project='/synthetic/revised' WHERE event_id=?",(key,))
        while journal.prepare_one(db):pass
        batches=[]
        # Pin every exact batch, without marking any of them accepted.
        while db.execute('SELECT 1 FROM delivery_items WHERE batch_id IS NULL LIMIT 1').fetchone():
            # Future timestamp makes prior pinned batches temporarily ineligible.
            batch=journal.next_batch(db,now=0)
            if not batch:raise ValueError('failed to pin synthetic batch')
            ident,raw,sha=batch
            with db:db.execute('UPDATE delivery_batches SET retry_at=1 WHERE batch_id=?',(ident,))
            folder=state/'batches';folder.mkdir(mode=0o700,exist_ok=True)
            body_path=folder/(ident+'.json');body_path.write_bytes(raw);os.chmod(body_path,0o600)
            batches.append(dict(batch_id=ident,sha256=sha,bytes=len(raw),items=len(wire.decode(raw)['items'])))
        with db:db.execute('UPDATE delivery_batches SET retry_at=0')
        expected=[]
        for _,item in db.execute('SELECT delivery_seq,item FROM delivery_items ORDER BY delivery_seq'):
            v=wire.decode(item)
            key=(v['record_id'],v['record_revision'],v['record_sha256'])
            if key not in expected:expected.append(key)
        manifest=dict(kind='erg-synthetic-qualification-v1',identity=identity,batches=batches,
                      allowed_batch_sha256=[b['sha256'] for b in batches],
                      expected_revisions=[dict(record_id=k[0],record_revision=k[1],record_sha256=k[2]) for k in expected],
                      source_events=db.execute('SELECT COUNT(*) FROM history_events').fetchone()[0],
                      delivery_items=db.execute('SELECT COUNT(*) FROM delivery_items').fetchone()[0],
                      has_chunked_records=True,actual_session_data=False)
        write(state/'manifest.json',manifest)
        return dict(state_directory=str(state),manifest=str(state/'manifest.json'),identity_proposal=str(state/'identity-proposal.json'),
                    batches=len(batches),delivery_items=manifest['delivery_items'],network_requests=0,actual_session_data=False)
    finally:db.close()


def load(state):
    manifest=json.loads((state/'manifest.json').read_text())
    if manifest.get('kind')!='erg-synthetic-qualification-v1' or manifest.get('actual_session_data') is not False:
        raise ValueError('not an approved isolated synthetic state')
    if (state/'synthetic.db').resolve()==Path(store.DB_PATH).resolve():raise ValueError('refusing operator ledger')
    return manifest


def transport(state,config):
    required=('tenant_id','client_id','audience','broker_origin','key_tag')
    if any(not config.get(k) for k in required):raise ValueError('missing enrollment deployment output')
    helper=state/'erg-enrollment-helper';cert=state/'key'/'certificate.pem'
    argv=[str(helper),'token',config['key_tag'],str(cert),config['tenant_id'],config['client_id'],config['audience']]
    return client.HTTPS(config['broker_origin'],lambda:client.helper_token(argv))


def verify_receipt_snapshot(api, manifest, bodies):
    """Verify every pinned acceptance receipt, including pagination and scope."""
    expected = {}
    for batch in manifest['batches']:
        value = wire.validate_batch(bodies[batch['batch_id']], batch['sha256'])
        expected[batch['batch_id']] = (batch['sha256'], [i['delivery_seq'] for i in value['items']])
    if not 1 <= len(expected) <= 500: raise ValueError('bounded pinned receipt set required')
    first = api.status(limit=1); pages = [first]; seen = {}; cursors = set()
    page = first
    while True:
        for key in ('enrollment_id', 'destination_generation'):
            if page.get(key) != manifest['identity'][key]: raise ValueError('receipt page scope mismatch')
        for key in ('materialized_through', 'projected_through', 'publication_state'):
            if page.get(key) != first.get(key): raise ValueError('receipt snapshot progress changed')
        for receipt in page['receipts']:
            ident = receipt.get('batch_id')
            if ident in seen or ident not in expected: raise ValueError('duplicate or unexpected acceptance receipt')
            if any(receipt.get(k) != manifest['identity'][k] for k in ('enrollment_id', 'destination_generation')):
                raise ValueError('acceptance receipt scope mismatch')
            sha, sequences = expected[ident]
            if (type(receipt.get('protocol_version')) is not int or receipt.get('protocol_version') != 1 or
                receipt.get('state') != 'accepted' or
                not isinstance(receipt.get('accepted_delivery_seqs'), list) or
                any(type(seq) is not int for seq in receipt['accepted_delivery_seqs'])):
                raise ValueError('invalid acceptance receipt state or sequence type')
            if receipt.get('batch_sha256') != sha or receipt.get('accepted_delivery_seqs') != sequences:
                raise ValueError('acceptance receipt digest or sequence mismatch')
            seen[ident] = sequences
        cursor = page.get('next_cursor')
        if not cursor: break
        if cursor in cursors or len(pages) >= len(expected): raise ValueError('receipt pagination exceeded pinned corpus')
        cursors.add(cursor); page = api.status(limit=1, cursor=cursor); pages.append(page)
    if set(seen) != set(expected): raise ValueError('missing pinned acceptance receipt')
    sequences = [seq for values in seen.values() for seq in values]
    if len(sequences) != manifest['delivery_items'] or len(set(sequences)) != len(sequences):
        raise ValueError('synthetic receipt coverage mismatch')
    return first, pages, dict(verified=True, batches=len(seen), delivery_items=len(sequences),
                             pages=len(pages), exact_digests_and_sequences=True)


def execute(state,config,action):
    manifest=load(state)
    if config.get('identity')!=manifest['identity'] or config.get('synthetic_only') is not True:
        raise ValueError('approved synthetic enrollment does not match manifest')
    api=transport(state,config)
    db=store.connect(str(state/'synthetic.db'),initialize=False)
    try:
        if action=='submit':
            accepted=[]
            for b in manifest['batches']:
                row=db.execute('SELECT body,sha256 FROM delivery_batches WHERE batch_id=?',(b['batch_id'],)).fetchone()
                if row is None or row[1]!=b['sha256'] or wire.digest(row[0])!=b['sha256']:raise ValueError('synthetic batch changed')
                receipt=api.submit(row[0],row[1]);journal.accept(db,b['batch_id'],receipt)
                # Exact lost-response replay is a second real HTTP request.
                replay=api.submit(row[0],row[1])
                if replay!=receipt:raise ValueError('replay receipt changed')
                accepted.append(dict(batch_id=b['batch_id'],accepted_items=len(receipt['accepted_delivery_seqs'])))
            result=dict(action=action,accepted=accepted,local=journal.status(db),actual_session_data=False)
        else:
            bodies={b['batch_id']:(state/'batches'/(b['batch_id']+'.json')).read_bytes() for b in manifest['batches']}
            remote,pages,proof=verify_receipt_snapshot(api,manifest,bodies)
            journal.reconcile_status(db,remote)
            result=dict(action=action,remote=remote,remote_pages=pages,receipt_verification=proof,local=journal.status(db),actual_session_data=False)
        write(state/(action+'-evidence.json'),result)
        return result
    finally:db.close()


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','submit','verify'])
    p.add_argument('--state',required=True,type=Path);p.add_argument('--config',type=Path)
    args=p.parse_args();state=args.state.expanduser().resolve()
    if args.action=='prepare':result=prepare(state)
    else:
        if not args.config:p.error('deployment config required; it contains no token/private key')
        result=execute(state,json.loads(args.config.read_text()),args.action)
    print(json.dumps(result,indent=2))

if __name__=='__main__':main()
