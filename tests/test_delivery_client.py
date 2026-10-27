"""Demand-driven transport, retry and status invariants; no external HTTP."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
from erg import store,history,delivery_journal as journal,delivery_client as client
from erg.delivery_inventory import inventory

IDENTITY=dict(enrollment_id='fixture',installation_id='install',destination_generation='gen')

class Client(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'local.db';self.db=store.connect(str(self.path));self.addCleanup(self.db.close)
        journal.enroll(self.db,IDENTITY)
        with self.db:history.put(self.db,'pi','session','folder',1000,'message',dict(content='synthetic fixture'),'entry',machine='machine')

    def test_offline_retry_preserves_batch_and_uses_deadline(self):
        transport=Mock();transport.submit.side_effect=client.DeliveryError('offline')
        self.assertEqual(client.step(self.db,transport,now=1),'prepared')
        self.assertEqual(client.step(self.db,transport,now=1),'retry')
        original=self.db.execute('SELECT body FROM delivery_batches').fetchone()[0]
        self.assertEqual(client.step(self.db,transport,now=1),'idle')
        self.assertEqual(transport.submit.call_count,1)
        self.assertEqual(self.db.execute('SELECT body FROM delivery_batches').fetchone()[0],original)
        self.assertEqual(client.step(self.db,transport,now=1000),'retry')
        self.assertEqual(transport.submit.call_args_list[0],transport.submit.call_args_list[1])

    def test_revoked_enrollment_stops_all_submissions(self):
        transport=Mock();transport.submit.side_effect=client.DeliveryError('revoked',code=403)
        client.step(self.db,transport,now=1)
        self.assertEqual(client.step(self.db,transport,now=1),'blocked')
        with self.db:history.put(self.db,'pi','session','folder',1001,'message',dict(content='next'),'entry2',machine='machine')
        self.assertEqual(client.step(self.db,transport,now=10000),'blocked')
        self.assertEqual(transport.submit.call_count,1)

    def test_status_cannot_jump_unaccepted_items_or_change_generation(self):
        journal.prepare_one(self.db)
        with self.assertRaises(ValueError):journal.reconcile_status(self.db,dict(IDENTITY,materialized_through=1,projected_through=1))
        with self.assertRaises(ValueError):journal.reconcile_status(self.db,dict(IDENTITY,destination_generation='other',materialized_through=0,projected_through=0))

    def test_inventory_is_read_only_and_keeps_unknown_costs_unknown(self):
        before=self.db.total_changes
        result=inventory(self.path,sample_limit=0)
        self.assertEqual(result['counts']['events'],1)
        self.assertIsNone(result['estimated_initial_contract_bytes'])
        self.assertIsNone(result['costs']['zerobus'])
        self.assertEqual(self.db.total_changes,before)
        with self.assertRaises(ValueError):inventory(self.path,batch_rows=10001)

    def test_helper_token_is_bounded_and_never_uses_shell(self):
        self.assertEqual(client.helper_token([sys.executable,'-c',"print('synthetic-token')"]),'synthetic-token')
        with self.assertRaises(client.DeliveryError):client.helper_token([sys.executable,'-c',"print('x'*20000)"])
        for value in ('http://localhost:1','https://user:password@example.invalid','https://example.invalid/path','https://example.invalid/?token=x'):
            with self.assertRaises(client.DeliveryError):client.endpoint(value)
