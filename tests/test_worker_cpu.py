"""Worker diagnostics must attribute thread CPU without inventing a process cap."""
import threading
import unittest
from unittest.mock import patch
from erg import overhead

class WorkerCPU(unittest.TestCase):
    def test_records_cpu_and_wait_separately_and_returns_results(self):
        counter=overhead.WorkerCPU(('capture',))
        with patch.object(overhead.time,'thread_time',side_effect=[2,2.25]),patch.object(overhead.time,'monotonic',side_effect=[10,15]):
            self.assertEqual(counter.call('capture',lambda:42),42)
        row=counter.snapshot()['workers']['capture']
        self.assertEqual(row,dict(runs=1,errors=0,cpu_seconds=.25,wall_seconds=5,max_wall_seconds=5))
        row['runs']=900
        self.assertEqual(counter.snapshot()['workers']['capture']['runs'],1)
        self.assertIn('excludes subprocess CPU',counter.snapshot()['scope'])
    def test_failed_work_is_measured_without_retaining_error_text(self):
        counter=overhead.WorkerCPU(('capture',))
        with patch.object(overhead.time,'thread_time',side_effect=[2,2.5]),patch.object(overhead.time,'monotonic',side_effect=[10,11]):
            with self.assertRaisesRegex(ValueError,'PRIVATE'):
                with counter.measure('capture'):raise ValueError('PRIVATE')
        data=counter.snapshot();self.assertEqual(data['workers']['capture']['errors'],1)
        self.assertNotIn('PRIVATE',str(data))
        with self.assertRaises(ValueError):counter.call('PRIVATE',lambda:None)
        self.assertEqual(len(counter.snapshot()['workers']),1)
    def test_concurrent_workers_do_not_lose_updates_or_mix_cpu_clocks(self):
        counter=overhead.WorkerCPU(('capture','context'))
        clock=threading.local()
        def tick():
            clock.value=getattr(clock,'value',0)+1;return clock.value
        def run(name):
            for _ in range(100):counter.call(name,lambda:None)
        with patch.object(overhead.time,'thread_time',side_effect=tick):
            threads=[threading.Thread(target=run,args=(name,)) for name in ('capture','context') for _ in range(3)]
            for thread in threads:thread.start()
            for thread in threads:thread.join()
        for row in counter.snapshot()['workers'].values():
            self.assertEqual(row['runs'],300);self.assertEqual(row['cpu_seconds'],300)

    def test_child_windows_mark_nested_overlap_and_cannot_be_summed_as_ownership(self):
        counter=overhead.WorkerCPU(('capture','context'))
        with patch.object(overhead,'waited_children',side_effect=[1,2,3,4]):
            with counter.measure('capture'):
                with counter.measure('context'):pass
        data=counter.snapshot();a=data['child_cpu_windows']['capture'];b=data['child_cpu_windows']['context']
        self.assertEqual((a['waited_children_cpu_seconds_delta'],b['waited_children_cpu_seconds_delta']),(3,1))
        self.assertEqual((a['overlapped_runs'],b['overlapped_runs']),(1,1))
        self.assertIn('do not sum',data['child_window_scope'])
        self.assertEqual(a['samples'],1);self.assertEqual(a['unknown_runs'],0)
        a['runs']=999;self.assertEqual(counter.snapshot()['child_cpu_windows']['capture']['runs'],1)
    def test_unavailable_or_reset_child_counters_stay_unknown_and_do_not_break_work(self):
        counter=overhead.WorkerCPU(('capture',))
        with patch.object(overhead,'waited_children',side_effect=[None,None,5,2]):
            self.assertEqual(counter.call('capture',lambda:42),42)
            with self.assertRaises(ValueError):
                with counter.measure('capture'):raise ValueError('PRIVATE')
        data=counter.snapshot();row=data['child_cpu_windows']['capture']
        self.assertEqual((row['runs'],row['samples'],row['unknown_runs']),(2,0,2))
        self.assertEqual(row['waited_children_cpu_seconds_delta'],0)
        self.assertNotIn('PRIVATE',str(data));self.assertEqual(counter.active,{})
    def test_child_counter_failures_are_optional_diagnostics(self):
        with patch.object(overhead.os,'name','nt'):self.assertIsNone(overhead.waited_children())
        with patch.object(overhead.os,'name','posix'),patch.object(overhead.os,'times',side_effect=OSError):self.assertIsNone(overhead.waited_children())

    def test_service_registers_every_literal_worker_cpu_label(self):
        import ast
        from pathlib import Path
        from erg import service
        tree=ast.parse(Path(service.__file__).read_text())
        allowed=set();used=set()
        for node in ast.walk(tree):
            if not isinstance(node,ast.Call) or not isinstance(node.func,ast.Attribute):continue
            if node.func.attr=='WorkerCPU':allowed.update(ast.literal_eval(node.args[0]))
            if node.func.attr=='call' and isinstance(node.func.value,ast.Name) and node.func.value.id=='cpu':
                if node.args and isinstance(node.args[0],ast.Constant):used.add(node.args[0].value)
        self.assertTrue(used);self.assertFalse(used-allowed,'Unregistered worker CPU labels prevent the worker from running')
