"""Bounded acceptance journal pages shared by the broker and processor."""

import re

from erg import delivery_protocol as wire

from .receiver import Conflict

PREFIX = "v1/processor-journal"
ACTIVATION = "processor-journal-active.json"
PAGE_SIZE = 128
MARKER = re.compile(
    r"v1/[0-9a-f]{64}/batches/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/accepted\.json"
)


def page_key(number):
    return "%s/%020d.json" % (PREFIX, number)


def valid_marker(value):
    return isinstance(value, str) and MARKER.fullmatch(value) is not None


def decode_page(raw):
    rows = wire.decode(raw) if raw is not None else []
    if not isinstance(rows, list) or len(rows) > PAGE_SIZE or not all(map(valid_marker, rows)):
        raise Conflict("invalid acceptance journal page")
    return rows


def append(store, marker):
    """Publish before acceptance; a full page is immutable and seals its offsets."""
    if not valid_marker(marker):
        raise Conflict("invalid acceptance journal marker")
    head = PREFIX + "/head.json"
    for _ in range(16):
        raw, head_version = store.versioned(head)
        number = wire.decode(raw) if raw is not None else 0
        if type(number) is not int or not 0 <= number < 2**63 - 1:
            raise Conflict("invalid acceptance journal head")
        key = page_key(number)
        raw_page, version = store.versioned(key)
        rows = decode_page(raw_page)
        if marker in rows:
            return
        if len(rows) == PAGE_SIZE:
            store.compare_swap(head, head_version, wire.encode(number + 1))
        elif store.compare_swap(key, version, wire.encode([*rows, marker])):
            return
    raise RuntimeError("acceptance journal contention; retry identical batch")
