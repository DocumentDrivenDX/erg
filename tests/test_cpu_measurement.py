"""A reproducible observer must reject restarted processes and counter resets."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('erg_cpu_measurement',Path(__file__).resolve().parents[1]/'scripts/measure-background-cpu.py')
probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)

class CPUProbe(unittest.TestCase):
    def state(self,wall,cpu,pid=1,identity='same'):
        return dict(wall=wall,components={'collector':dict(sampled_at=wall,cpu_seconds=cpu,pid=pid,identity=identity)})
    def test_cpu_percent_uses_one_core_and_invalidates_restarts_or_resets(self):
        before=self.state(100,3);after=self.state(200,3.75)
        self.assertEqual(probe.interval(before,after)['combined_percent'],.75)
        for invalid in (self.state(200,4,pid=2),self.state(200,4,identity='restarted'),self.state(100,4),self.state(200,2)):
            self.assertIsNone(probe.interval(before,invalid)['combined_percent'])
    def test_unix_cpu_time_includes_days_and_fractional_seconds(self):
        self.assertEqual(probe.process_seconds('1-02:03:04.5'),93784.5)
        self.assertEqual(probe.process_seconds('02:03.5'),123.5)
    def test_saved_evidence_is_private_and_existing_run_cannot_be_replaced(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'evidence.json';probe.save(path,dict(state='complete'))
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            with self.assertRaises(ValueError):probe.run(8787,60,50,str(path))
            self.assertEqual(json.loads(path.read_text()),dict(state='complete'))
