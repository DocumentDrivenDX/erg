import copy
from pathlib import Path
import tempfile
import unittest
from erg import delivery_protocol as wire
from central.erg_central.receiver import FileStore,Receiver,Forbidden,namespace
from central.erg_central.status import delivery_status

IDENTITY=dict(enrollment_id='fixture-enrollment',installation_id='fixture-install',destination_generation='fixture-gen',active=True)

class Status(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=FileStore(Path(self.tmp.name)/'spool');self.receiver=Receiver(self.store)

    def batch(self,seq):
        payload=dict(record_id='fixture-'+str(seq),record_revision=1,record_kind='session_event',body={'content':'synthetic'})
        raw=wire.batch({k:IDENTITY[k] for k in wire.ENROLLMENT},wire.record_items(payload,seq))
        return raw,wire.digest(raw)

    def test_synthetic_allowlist_is_enforced_before_durable_content(self):
        raw,sha=self.batch(1)
        restricted=dict(IDENTITY,synthetic_only=True,allowed_batch_sha256=[])
        with self.assertRaises(Forbidden):self.receiver.accept(raw,sha,restricted)
        self.assertEqual(list(self.store.keys('v1/')),[])
        self.receiver.accept(raw,sha,dict(restricted,allowed_batch_sha256=[sha]))
        self.assertEqual(len(list(self.receiver.batches())),1)

    def test_pagination_is_pinned_and_does_not_cross_identity(self):
        for seq in (1,2,3):
            raw,sha=self.batch(seq);self.receiver.accept(raw,sha,IDENTITY)
        first=delivery_status(self.store,IDENTITY,limit=1)
        self.assertEqual(first['publication_state'],'pending')
        self.assertIsNone(first['missing_ranges'])
        self.assertIsNotNone(first['next_cursor'])
        raw,sha=self.batch(4);self.receiver.accept(raw,sha,IDENTITY)
        receipts=list(first['receipts']);cursor=first['next_cursor']
        while cursor:
            page=delivery_status(self.store,IDENTITY,limit=1,cursor=cursor)
            receipts.extend(page['receipts']);cursor=page['next_cursor']
        self.assertEqual(len(receipts),3)
        self.assertEqual(len({r['batch_id'] for r in receipts}),3)
        with self.assertRaises(Forbidden):delivery_status(self.store,dict(IDENTITY,enrollment_id='other'),limit=1,cursor=first['next_cursor'])
        with self.assertRaises(wire.ProtocolError):delivery_status(self.store,IDENTITY,limit=2,cursor=first['next_cursor'])

    def test_progress_is_scoped_and_never_fabricated(self):
        key=namespace(IDENTITY)+'/status.json'
        self.store.put_control_once(key,wire.encode(dict(enrollment_id=IDENTITY['enrollment_id'],destination_generation=IDENTITY['destination_generation'],materialized_through=5,projected_through=3)))
        value=delivery_status(self.store,IDENTITY)
        self.assertEqual(value['materialized_through'],5)
        self.assertEqual(value['projected_through'],3)
        self.assertEqual(value['receipts'],[])
        self.assertEqual(delivery_status(self.store,dict(IDENTITY,enrollment_id='other'))['materialized_through'],0)
        with self.assertRaises(Forbidden):delivery_status(self.store,dict(IDENTITY,active=False))

    def test_direct_batch_lookup_cannot_read_other_scope(self):
        raw,sha=self.batch(1);receipt=self.receiver.accept(raw,sha,IDENTITY)
        self.assertEqual(delivery_status(self.store,IDENTITY,batch_id=receipt['batch_id'])['receipts'],[receipt])
        self.assertEqual(delivery_status(self.store,dict(IDENTITY,destination_generation='other'),batch_id=receipt['batch_id'])['receipts'],[])
        for bad in ('../../registry','bad-id'):
            with self.assertRaises(wire.ProtocolError):delivery_status(self.store,IDENTITY,batch_id=bad)

    def test_corrupt_cursor_and_invalid_bounds_are_rejected(self):
        for cursor in ('%%%','e30','x'*3000):
            with self.assertRaises((wire.ProtocolError,Forbidden)):delivery_status(self.store,IDENTITY,cursor=cursor)
        for limit in (0,501,True):
            with self.assertRaises(wire.ProtocolError):delivery_status(self.store,IDENTITY,limit=limit)
