"""Adapt only approved infra bindings to an exact synthetic corpus allowlist.

Local output only; infrastructure owns publishing to the private control container.
Never reads credentials or a native session ledger.
"""
import argparse
import json
import os
from pathlib import Path


def build(bindings,manifest):
    if manifest.get('actual_session_data') is not False or manifest.get('kind')!='erg-synthetic-qualification-v1':
        raise ValueError('synthetic manifest required')
    if 'erg_enrollment_bindings' in bindings:
        bindings=bindings['erg_enrollment_bindings']['value']
    identity=manifest['identity']; key=identity['enrollment_id']
    if set(bindings)!={key}:raise ValueError('registry build requires exactly the approved synthetic enrollment')
    row=bindings[key]
    if any(row.get(k)!=identity[k] for k in ('enrollment_id','installation_id','destination_generation')):
        raise ValueError('infra approved identity differs from manifest')
    if row.get('active') is not True:raise ValueError('synthetic enrollment not active')
    digests=manifest['allowed_batch_sha256']
    if sorted(digests)!=sorted(b['sha256'] for b in manifest['batches']):raise ValueError('manifest digest mismatch')
    for digest in digests:
        if len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):raise ValueError('invalid manifest digest')
    result=dict(identity,tenant_id=row['tenant_id'],principal_id=row['principal_object_id'],
                client_id=row['authorized_client_id'],managed_device_id=row['intune_device_id'],
                user_object_id=row['user_object_id'],active=True,synthetic_only=True,allowed_batch_sha256=digests,
                certificate_expires_at=row['certificate_expires_at'])
    if any(not isinstance(result[k],str) or not result[k] for k in ('tenant_id','principal_id','client_id','managed_device_id','user_object_id')):
        raise ValueError('incomplete approved principal binding')
    return dict(registry_revision=1,enrollments=[result])


def main():
    p=argparse.ArgumentParser();p.add_argument('--bindings',type=Path,required=True);p.add_argument('--manifest',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();result=build(json.loads(args.bindings.read_text()),json.loads(args.manifest.read_text()))
    descriptor=os.open(str(args.output),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w') as f:json.dump(result,f,indent=2);f.write('\n')
    print(json.dumps(dict(output=str(args.output),enrollments=1,synthetic_only=True,approved_batches=len(result['enrollments'][0]['allowed_batch_sha256']))))

if __name__=='__main__':main()
