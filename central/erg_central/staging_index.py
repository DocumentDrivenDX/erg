"""Incremental completion cache; terminal receipts remain authoritative.

Keep the existing index as an immutable migration base. New completions update
only their digest-prefix shards instead of rewriting the entire legacy index.
"""

import concurrent.futures
from pathlib import Path

from erg import delivery_protocol as wire

from .receiver import Conflict, namespace


def load_shards(control, result):
    paths = list((Path(control) / "processor-staged-shards").glob("*.json"))
    if len(paths) > 16:
        raise Conflict("too many staged index shards")

    def read(path):
        if len(path.stem) != 1 or any(c not in "0123456789abcdef" for c in path.stem):
            raise Conflict("invalid staged shard name")
        if path.stat().st_size > 16 * 1024 * 1024:
            raise Conflict("staged shard exceeds bound")
        value = wire.decode(path.read_bytes())
        if (
            not isinstance(value, dict)
            or value.get("version") != 1
            or not isinstance(value.get("scopes"), dict)
        ):
            raise Conflict("invalid staged shard")
        for space, batches in value["scopes"].items():
            if (
                not isinstance(space, str)
                or not space.startswith("v1/")
                or not wire.HEX.fullmatch(space[3:])
            ):
                raise Conflict("invalid staged shard scope")
            if not isinstance(batches, dict):
                raise Conflict("invalid staged shard batches")
            for ident, sha in batches.items():
                if (
                    not isinstance(ident, str)
                    or not ident
                    or "/" in ident
                    or not isinstance(sha, str)
                    or not wire.HEX.fullmatch(sha)
                    or sha[:1] != path.stem
                ):
                    raise Conflict("invalid staged shard batch")
        return value

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for value in pool.map(read, paths):
            for space, batches in value["scopes"].items():
                target = result["scopes"].setdefault(space, {})
                for ident, sha in batches.items():
                    if ident in target and target[ident] != sha:
                        raise Conflict("staged shard receipt conflict")
                    target[ident] = sha
    return result


def persist(index, control, receipts, write):
    touched = set()
    for receipt in receipts:
        batches = index["scopes"].setdefault(namespace(receipt), {})
        ident, sha = receipt["batch_id"], receipt["batch_sha256"]
        if ident in batches and batches[ident] != sha:
            raise Conflict("staged index receipt conflict")
        batches[ident] = sha
        touched.add(sha[:1])
    for prefix in sorted(touched):
        path = Path(control) / "processor-staged-shards" / (prefix + ".json")
        value = dict(version=1, scopes={})
        # Existing shards were validated at sweep startup. The one-writer job
        # updates only these small deltas; the legacy migration base stays put.
        if path.exists():
            if path.stat().st_size > 16 * 1024 * 1024:
                raise Conflict("staged shard exceeds bound")
            value = wire.decode(path.read_bytes())
        for receipt in receipts:
            if receipt["batch_sha256"][:1] == prefix:
                value["scopes"].setdefault(namespace(receipt), {})[receipt["batch_id"]] = receipt[
                    "batch_sha256"
                ]
        if len(wire.encode(value)) > 16 * 1024 * 1024:
            raise Conflict("staged shard exceeds bound")
        write(path, value)
