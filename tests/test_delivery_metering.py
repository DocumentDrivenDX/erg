import tempfile
import unittest
from pathlib import Path
from erg import store,history,delivery_journal as journal,delivery_client as client
from central.erg_central.receiver import Receiver,FileStore
class DeliveryMetering(unittest.TestCase):
 def test_exact_replay_counts_two_requests_but_one_unique_payload(self):
  with tempfile.TemporaryDirectory() as tmp:
   db=store.connect(str(Path(tmp)/'db'))
   try:
    identity=dict(enrollment_id='e',installation_id='i',destination_generation='g');journal.enroll(db,identity)
    with db:history.put(db,'pi','s','p',1,'message',{'text':'fixture'},'id')
    journal.prepare_one(db);batch,raw,sha=journal.next_batch(db)
    receiver=Receiver(FileStore(Path(tmp)/'spool'))
    class API:
     def submit(self,body,digest):return receiver.accept(body,digest,dict(identity,active=True))
    one=client.send_once(db,API(),batch,raw,sha);two=client.send_once(db,API(),batch,raw,sha)
    self.assertEqual(one,two)
    self.assertEqual(db.execute('SELECT sum(attempts),sum(accepted_responses),sum(offered_body_bytes) FROM delivery_http_daily').fetchone(),(2,2,2*len(raw)))
    self.assertEqual(db.execute('SELECT sum(batches),sum(items),sum(payload_bytes) FROM delivery_unique_daily').fetchone(),(1,1,len(raw)))
   finally:db.close()
