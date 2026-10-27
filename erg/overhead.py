"""Cumulative CPU accounting; sampling does not claim a hardware-enforced cap."""

import contextlib
import os
import threading
import time


def reading():
    times = os.times()
    return dict(
        sampled_at=time.time(),
        monotonic=time.monotonic(),
        process_cpu_seconds=time.process_time(),
        reaped_children_cpu_seconds=times.children_user + times.children_system,
        scope="Process plus waited-for children; live children must be accounted at measurement boundaries",
    )


def waited_children():
    """A process-wide counter, never ownership by the calling worker thread."""
    if os.name != "posix":
        return None
    try:
        value = os.times()
        return value.children_user + value.children_system
    except (OSError, AttributeError):
        return None


class WorkerCPU:
    """Fixed-name cumulative diagnostics, never a CPU cap or a full process sum."""

    def __init__(self, names):
        self.names = frozenset(names)
        self.lock = threading.Lock()
        self.values = {}
        self.child_windows = {}
        self.active = {}

    @contextlib.contextmanager
    def measure(self, name):
        if name not in self.names:
            raise ValueError("unknown worker CPU label")
        cpu = time.thread_time()
        wall = time.monotonic()
        failed = False
        child = waited_children()
        token = object()
        with self.lock:
            overlap = bool(self.active)
            for active in self.active.values():
                active[0] = True
            self.active[token] = [overlap]
        try:
            yield
        except BaseException:
            failed = True
            raise
        finally:
            elapsed = max(0, time.thread_time() - cpu)
            duration = max(0, time.monotonic() - wall)
            after = waited_children()
            with self.lock:
                overlap = self.active.pop(token)[0]
                window = self.child_windows.setdefault(
                    name,
                    dict(
                        runs=0,
                        overlapped_runs=0,
                        samples=0,
                        unknown_runs=0,
                        waited_children_cpu_seconds_delta=0.0,
                    ),
                )
                window["runs"] += 1
                window["overlapped_runs"] += int(overlap)
                if child is not None and after is not None and after >= child:
                    window["samples"] += 1
                    window["waited_children_cpu_seconds_delta"] += after - child
                else:
                    window["unknown_runs"] += 1
                row = self.values.setdefault(
                    name,
                    dict(runs=0, errors=0, cpu_seconds=0.0, wall_seconds=0.0, max_wall_seconds=0.0),
                )
                row["runs"] += 1
                row["errors"] += int(failed)
                row["cpu_seconds"] += elapsed
                row["wall_seconds"] += duration
                row["max_wall_seconds"] = max(row["max_wall_seconds"], duration)

    def call(self, name, function, *args, **kwargs):
        with self.measure(name):
            return function(*args, **kwargs)

    def snapshot(self):
        with self.lock:
            return dict(
                scope="Measured collector thread CPU only; excludes subprocess CPU, watcher callbacks and uninstrumented code. Use cpu_accounting for process totals.",
                workers={k: dict(v) for k, v in self.values.items()},
                child_window_scope="Process-wide waited-child CPU deltas during each phase, not ownership. Concurrent phases may include the same child CPU; do not sum windows or add them to cpu_accounting. Live children are excluded until reaped. Unknown/reset counters are not zero observations.",
                child_cpu_windows={k: dict(v) for k, v in self.child_windows.items()},
            )
