"""Durable batch acceptance core; adapters supply authenticated enrollment/store."""

import os
import tempfile
import time
from pathlib import Path

from erg import delivery_protocol as wire


class Conflict(ValueError):
    pass


class Forbidden(ValueError):
    pass


class FileStore:
    """Private local proof adapter, with immutable atomic create + directory fsync."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.stat().st_mode & 0o077:
            raise ValueError("spool directory must be private")

    def _path(self, key):
        if key.startswith("/") or ".." in key.split("/"):
            raise ValueError("invalid storage key")
        return self.root / key

    def get(self, key):
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            return None

    def put_once(self, key, raw):
        path = self._path(key)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=str(path.parent))
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, str(path))
            except FileExistsError:
                if path.read_bytes() != raw:
                    raise Conflict("immutable key conflict")
            directory = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            os.unlink(temporary)

    def read_control(self, key):
        return self.get("control/" + key)

    def put_control_once(self, key, raw):
        self.put_once("control/" + key, raw)

    def versioned(self, key):
        raw = self.get(key)
        return raw, wire.digest(raw) if raw is not None else None

    def compare_swap(self, key, expected, raw):
        import fcntl

        path = self._path(key)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        temporary = None
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            old, version = self.versioned(key)
            if version != expected:
                return False
            descriptor, temporary = tempfile.mkstemp(dir=str(path.parent))
            with os.fdopen(descriptor, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, str(path))
            temporary = None
            directory = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return True
        finally:
            if temporary is not None:
                os.unlink(temporary)
            os.close(lock)

    def keys(self, prefix):
        path = self._path(prefix)
        if not path.exists():
            return
        for entry in sorted(path.rglob("*")):
            if entry.is_file():
                yield str(entry.relative_to(self.root))


def namespace(identity):
    """Opaque identities never become filesystem/container paths."""
    return "v1/" + wire.digest(
        wire.encode([identity[k] for k in ("enrollment_id", "destination_generation")])
    )


class Receiver:
    def __init__(self, store):
        self.store = store
        self.metrics = {}

    def accept(self, raw, digest, identity):
        started = time.monotonic()
        self.metrics = dict(
            batch_bytes=len(raw),
            claim_items=0,
            claim_shards=0,
            claim_reads=0,
            claim_writes=0,
            claim_cas_conflicts=0,
            replay=False,
            body_put_attempts=0,
            accepted_marker_reads=0,
            accepted_marker_put_attempts=0,
        )
        value = wire.validate_batch(raw, digest)
        if identity.get("active") is not True or any(
            identity.get(k) != value[k] for k in wire.ENROLLMENT
        ):
            raise Forbidden("enrollment mismatch or revoked")
        prefix = namespace(value)
        root = prefix + "/batches/" + value["batch_id"]
        self.metrics["accepted_marker_reads"] += 1
        committed = self.store.get(root + "/accepted.json")
        if committed is not None:
            previous = wire.decode(committed)
            if previous["batch_sha256"] != digest:
                raise Conflict("batch digest conflict")
            self.metrics.update(replay=True, elapsed_seconds=time.monotonic() - started)
            return previous
        if identity.get("synthetic_only") is True:
            approved = identity.get("allowed_batch_sha256")
            if not isinstance(approved, list) or digest not in approved:
                raise Forbidden("batch outside approved synthetic manifest")
        # Order matters: body first, immutable sequence claims, accepted marker last.
        # A crash can strand staging, but never an acknowledged unavailable body.
        self.metrics["body_put_attempts"] += 1
        self.store.put_once(root + "/body.json", raw)
        # 512 immutable bindings per bounded CAS shard. A crash can reserve some
        # shards, but cannot acknowledge until all claims and the marker commit.
        shards = {}
        for item in value["items"]:
            number = (item["delivery_seq"] - 1) // 512
            shards.setdefault(number, {})[str(item["delivery_seq"])] = wire.digest(
                wire.encode(item)
            )
        self.metrics.update(claim_items=len(value["items"]), claim_shards=len(shards))
        for number, claims in shards.items():
            key = prefix + "/sequence-shards/%020d.json" % number
            for attempt in range(8):
                old, version = self.store.versioned(key)
                self.metrics["claim_reads"] += 1
                current = wire.decode(old) if old is not None else {}
                if (
                    not isinstance(current, dict)
                    or len(current) > 512
                    or any(
                        not k.isdigit()
                        or len(k) > 19
                        or str(int(k)) != k
                        or not 1 <= int(k) <= 2**63 - 1
                        or (int(k) - 1) // 512 != number
                        or not isinstance(v, str)
                        or not wire.HEX.fullmatch(v)
                        for k, v in current.items()
                    )
                ):
                    raise Conflict("invalid claim index")
                for seq, sha in claims.items():
                    if seq in current and current[seq] != sha:
                        raise Conflict("immutable sequence conflict")
                if all(seq in current for seq in claims):
                    break
                updated = dict(current, **claims)
                if self.store.compare_swap(key, version, wire.encode(updated)):
                    self.metrics["claim_writes"] += 1
                    break
                self.metrics["claim_cas_conflicts"] += 1
            else:
                raise RuntimeError("claim contention; retry identical batch")
        receipt = dict(
            protocol_version=1,
            batch_id=value["batch_id"],
            batch_sha256=digest,
            enrollment_id=value["enrollment_id"],
            destination_generation=value["destination_generation"],
            state="accepted",
            accepted_delivery_seqs=[i["delivery_seq"] for i in value["items"]],
            materialized_through=0,
            projected_through=0,
        )
        self.metrics["accepted_marker_put_attempts"] += 1
        self.store.put_once(root + "/accepted.json", wire.encode(receipt))
        self.metrics["elapsed_seconds"] = time.monotonic() - started
        return receipt

    def receipts(self, identity):
        if identity.get("active") is not True:
            raise Forbidden("revoked enrollment")
        prefix = namespace(identity) + "/batches/"
        for key in self.store.keys(prefix):
            if key.endswith("/accepted.json"):
                yield wire.decode(self.store.get(key))

    def batches(self):
        """Independent spool scan; never relies on a delivered notification."""
        for key in self.store.keys("v1/"):
            if not key.endswith("/accepted.json"):
                continue
            receipt = wire.decode(self.store.get(key))
            body = self.store.get(key[: -len("accepted.json")] + "body.json")
            if body is None:
                raise Conflict("accepted body unavailable")
            value = wire.validate_batch(body, receipt["batch_sha256"])
            if value["batch_id"] != receipt["batch_id"]:
                raise Conflict("receipt identity mismatch")
            yield value, receipt


def deployment_binding(identity, policy, bindings):
    """Exact deployment cohort, independently of registry authentication."""
    if policy not in ("synthetic-only", "qualified"):
        raise ValueError("invalid enrollment policy")
    if policy == "synthetic-only":
        if identity.get("synthetic_only") is not True:
            raise Forbidden("synthetic enrollment required")
    else:
        if identity.get("synthetic_only") is not False:
            raise Forbidden("real forward enrollment required")
        if any(not bindings.get(k) or identity.get(k) != bindings[k] for k in wire.ENROLLMENT):
            raise Forbidden("machine enrollment mismatch")
    return identity
