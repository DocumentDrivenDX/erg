"""Pinned native worker admission around the pure Erg codec.

The archive builder supplies runtime_pins.py. A successful native fact proves
these checks executed; configuration failures are never declared data conflicts.
"""

import hashlib
import importlib
import importlib.metadata
import platform
from pathlib import Path

from .ldp_codec import checked_item as core_checked_item
from .ldp_codec import reconstruct_revision as core_reconstruct_revision
from .receiver import Conflict  # noqa: F401 -- declared stage rejection type

_VERIFIED = set()


def verify_runtime(pins=None):
    if pins is None:
        from .runtime_pins import PINS

        pins = PINS
    key = repr(pins)
    if key in _VERIFIED:
        return
    if (pins.get("machine") and platform.machine() != pins["machine"]) or (
        pins.get("machines") and platform.machine() not in pins["machines"]
    ):
        raise RuntimeError("ERG_LDP_RUNTIME_ARCHITECTURE_MISMATCH")
    actual = dict(python=platform.python_version())
    for name in pins["packages"]:
        actual[name] = importlib.metadata.version(name)
    if actual != dict(python=pins["python"], **pins["packages"]):
        raise RuntimeError("ERG_LDP_RUNTIME_VERSION_MISMATCH")
    for name, expected in pins.get("module_versions", {}).items():
        if importlib.import_module(name).__version__ != expected:
            raise RuntimeError("ERG_LDP_RUNTIME_VERSION_MISMATCH")
    for name, expected in pins["source_sha256"].items():
        module = importlib.import_module(name)
        if hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() != expected:
            raise RuntimeError("ERG_LDP_RUNTIME_SOURCE_MISMATCH")
    for package, files in pins.get("distribution_files", {}).items():
        distribution = importlib.metadata.distribution(package)
        for relative, expected in files.items():
            if Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise RuntimeError("ERG_LDP_RUNTIME_ARTIFACT_PATH_INVALID")
            path = Path(distribution.locate_file(relative))
            if isinstance(expected, dict):
                expected = expected.get(platform.machine())
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise RuntimeError("ERG_LDP_RUNTIME_BINARY_MISMATCH")
    _VERIFIED.add(key)


def checked_item(row, identity):
    verify_runtime()
    return core_checked_item(row, identity)


def reconstruct_revision(items):
    verify_runtime()
    return core_reconstruct_revision(items)
