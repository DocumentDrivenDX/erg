import datetime
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from erg import costs_bridge as bridge

class CostRefresh(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.patch=patch.object(bridge,'root',return_value=self.root);self.patch.start();self.addCleanup(self.patch.stop)
        (self.root/'refresh-command.json').write_text(json.dumps({'argv':['/usr/bin/python3','producer.py']}))

    def test_fresh_snapshot_does_not_launch_or_consume_cloud(self):
        (self.root/'latest.json').write_text(json.dumps({'captured_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat()}))
        with patch.object(bridge.subprocess,'Popen') as launch:
            self.assertEqual(bridge.request_refresh()['state'],'cached');launch.assert_not_called()
        self.assertIsNotNone(bridge.status()['last_success_at'])

    def test_stale_snapshot_launches_once_without_stealing_producer_lock(self):
        (self.root/'.refresh.lock').write_text('producer-owned')
        with patch.object(bridge.subprocess,'Popen') as launch:
            self.assertEqual(bridge.request_refresh()['state'],'queued')
            self.assertEqual(bridge.request_refresh()['state'],'cached')
            self.assertEqual(launch.call_count,1)
        self.assertEqual((self.root/'.refresh.lock').read_text(),'producer-owned')
        self.assertTrue((self.root/'.bridge-refresh.lock').exists())
