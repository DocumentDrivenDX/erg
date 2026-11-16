"""Read numbered journal pages without listing the historical spool."""

import concurrent.futures
import time
from pathlib import Path

from erg import delivery_protocol as wire

from .discovery import MAX_CANDIDATES, MAX_STATE_BYTES, pending_marker
from .journal import PAGE_SIZE, decode_page, page_key, valid_marker
from .receiver import Conflict

CURSOR = "processor-journal-cursor.json"
MAX_PAGES_PER_RUN = 64


def load_cursor(path, root):
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_STATE_BYTES + 1)
    except FileNotFoundError:
        return dict(version=1, root=str(root), page=0, offset=0, retry=[])
    if len(raw) > MAX_STATE_BYTES:
        raise Conflict("acceptance cursor exceeds bound")
    state = wire.decode(raw)
    if (
        not isinstance(state, dict)
        or set(state) != {"version", "root", "page", "offset", "retry"}
        or state["version"] != 1
        or state["root"] != str(root)
        or type(state["page"]) is not int
        or not 0 <= state["page"] < 2**63
        or type(state["offset"]) is not int
        or not 0 <= state["offset"] < PAGE_SIZE
        or not isinstance(state["retry"], list)
        or len(state["retry"]) > MAX_CANDIDATES
        or not all(map(valid_marker, state["retry"]))
    ):
        raise Conflict("invalid acceptance cursor")
    return state


def read_page(root, number):
    try:
        with (root / page_key(number)).open("rb") as handle:
            raw = handle.read(128 * 1024 + 1)
    except FileNotFoundError:
        return []
    if len(raw) > 128 * 1024:
        raise Conflict("acceptance journal page exceeds bound")
    return decode_page(raw)


def journal_markers(root, control, limit, started, seconds, staged, write):
    target = Path(control) / CURSOR
    state = load_cursor(target, root)
    end = started + seconds
    selected = []
    retry = dict.fromkeys(state["retry"])
    metrics = dict(mode="acceptance_journal", pages=0, entries=0, budget_exhausted=False)
    if limit < 1:
        return selected, metrics

    def inspect(relative):
        try:
            return pending_marker(root, control, staged, relative), False
        except FileNotFoundError:
            return None, True

    def consume(pool, paths):
        for relative, (row, unpublished) in zip(paths, pool.map(inspect, paths)):
            if unpublished or row is not None:
                retry[relative] = None
            else:
                retry.pop(relative, None)
            if row is not None:
                selected.append(row)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        retry_offset = 0
        while retry_offset < len(state["retry"]) and len(selected) < limit:
            if time.monotonic() >= end:
                metrics["budget_exhausted"] = True
                break
            width = min(8, limit - len(selected))
            pending = state["retry"][retry_offset : retry_offset + width]
            consume(pool, pending)
            retry_offset += len(pending)
        while len(selected) < limit and len(retry) < MAX_CANDIDATES:
            if time.monotonic() >= end or metrics["pages"] >= MAX_PAGES_PER_RUN:
                metrics["budget_exhausted"] = True
                break
            rows = read_page(root, state["page"])
            metrics["pages"] += 1
            offset = state["offset"]
            if len(rows) < offset:
                raise Conflict("acceptance journal page regressed")
            if len(rows) == offset:
                break
            width = min(limit - len(selected), MAX_CANDIDATES - len(retry))
            candidates = rows[offset : offset + width]
            fresh = [row for row in dict.fromkeys(candidates) if row not in retry]
            consume(pool, fresh)
            metrics["entries"] += len(candidates)
            offset += len(candidates)
            state["page"] += offset == PAGE_SIZE
            state["offset"] = offset % PAGE_SIZE
            if offset == len(rows) and len(rows) < PAGE_SIZE:
                break
    state["retry"] = list(retry)
    if len(wire.encode(state)) > MAX_STATE_BYTES:
        raise Conflict("acceptance cursor exceeds bound")
    write(target, state)
    metrics.update(
        selected=len(selected),
        unresolved=len(retry),
        cursor_page=state["page"],
        cursor_offset=state["offset"],
        retry_full=len(retry) == MAX_CANDIDATES,
    )
    selected.sort(key=lambda row: (min(row[1]["accepted_delivery_seqs"]), str(row[0])))
    return selected, metrics
