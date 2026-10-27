"""Host monitoring must not spawn whole-machine process sampling."""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from erg import collect


class HostCounters(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "Mach counters require macOS")
    def test_native_cpu_counters_advance_without_a_subprocess(self):
        with patch.object(collect.subprocess, "run", side_effect=AssertionError("spawned helper")):
            before = collect.darwin_cpu_ticks()
            time.sleep(0.03)
            after = collect.darwin_cpu_ticks()
        self.assertGreater(after[1], before[1])
        self.assertGreaterEqual(after[0], before[0])
        self.assertLessEqual(after[0], after[1])

    def test_darwin_sampler_uses_ticks_and_keeps_unavailable_cpu_unknown(self):
        with patch.object(collect.platform, "system", return_value="Darwin"), patch.object(
            collect, "sh", return_value=""
        ) as shell, patch.object(collect, "darwin_cpu_ticks", return_value=(20, 100)):
            self.assertEqual(collect.raw_counters()["cpu_raw"], (20, 100))
            self.assertFalse(any("top " in call.args[0] for call in shell.call_args_list))
        with patch.object(collect.platform, "system", return_value="Darwin"), patch.object(
            collect, "sh", return_value=""
        ), patch.object(collect, "darwin_cpu_ticks", side_effect=OSError):
            self.assertNotIn("cpu_raw", collect.raw_counters())

    def test_sample_uses_interval_ticks_and_rejects_reset(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state"
            out = Path(folder) / "out"
            with patch.object(collect, "STATE_DIR", folder), patch.object(
                collect, "PERF_STATE", str(state)
            ), patch.object(collect, "PERF_FILE", str(out)):
                for prior, current, expected in [
                    ((20, 100), (30, 140), 25),
                    ((20, 100), (1, 2), None),
                    ((20, 100), (1, 140), None),
                ]:
                    state.write_text(json.dumps(dict(ts=10, cpu_raw=prior)))
                    with patch.object(
                        collect, "raw_counters", return_value=dict(ts=20, cpu_raw=current)
                    ):
                        self.assertEqual(collect.take_sample()["cpu"], expected)

    def test_network_sample_uses_numeric_output_without_name_resolution(self):
        output = "Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll\nen0 1500 <Link#1> aa:bb 2 0 100 3 0 200 0\n"
        commands = []

        def shell(cmd, *args):
            commands.append(cmd)
            return output if cmd == "netstat -ibn" else ""

        with patch.object(collect.platform, "system", return_value="Darwin"), patch.object(
            collect, "sh", side_effect=shell
        ), patch.object(collect, "darwin_cpu_ticks", return_value=(20, 100)):
            result = collect.raw_counters()
        self.assertEqual((result["net_rx_b"], result["net_tx_b"]), (100, 200))
        self.assertIn("netstat -ibn", commands)
        self.assertNotIn("netstat -ib", commands)
