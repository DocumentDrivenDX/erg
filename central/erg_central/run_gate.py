"""Decide whether a durable journal has unprocessed work before starting compute."""

from erg import delivery_protocol as wire

from .journal import PAGE_SIZE, decode_page, page_key
from .receiver import Conflict

CURSOR = "processor-journal-cursor.json"


def pending(store, root):
    """Read bounded journal metadata and receipts. Missing state requires a run."""
    raw = store.read_control(CURSOR)
    if raw is None:
        return True
    state = wire.decode(raw)
    if (
        not isinstance(state, dict)
        or state.get("version") != 1
        or state.get("root") != root
        or type(state.get("page")) is not int
        or state["page"] < 0
        or type(state.get("offset")) is not int
        or not 0 <= state["offset"] < PAGE_SIZE
        or not isinstance(state.get("retry"), list)
        or len(state["retry"]) > 4096
    ):
        raise Conflict("invalid gate cursor")
    rows = decode_page(store.get(page_key(state["page"])))
    if len(rows) > state["offset"]:
        return True
    if len(rows) < state["offset"]:
        raise Conflict("journal page regressed")
    for marker in state["retry"]:
        from .journal import valid_marker

        if not valid_marker(marker):
            raise Conflict("invalid gate marker")
        space, batch = marker.rsplit("/batches/", 1)
        batch_id = batch.split("/")[0]
        receipt = store.read_control(space + "/batches/" + batch_id + ".json")
        if receipt is not None:
            value = wire.decode(receipt)
            if value.get("items_staged") is True:
                continue
        if store.read_control(space + "/quarantine/" + batch_id + ".json") is None:
            return True
    return False
