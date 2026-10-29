"""Resumable Files API metadata discovery for wide Unity Catalog volumes.

Only the central adapter imports the Databricks SDK. No body is opened here.
A persisted retry list keeps selected work reachable across crashes and budget
expiry; terminal receipts, rather than discovery position, acknowledge work.
"""

import concurrent.futures
import time
from pathlib import Path
from urllib.parse import quote

from erg import delivery_protocol as wire

from .receiver import Conflict, namespace

PAGE_SIZE = 1000
MAX_STATE_BYTES = 2 * 1024 * 1024


def volume_page(directory, token):
    from databricks.sdk import WorkspaceClient

    query = {"page_size": PAGE_SIZE}
    if token is not None:
        query["page_token"] = token
    # Runtime identity already reads these same private volumes through FUSE.
    # Keep it separate from the narrowly scoped Zerobus credential.
    return WorkspaceClient().api_client.do(
        "GET", "/api/2.0/fs/directories" + quote(str(directory), safe="/"), query=query
    )


def relative_path(value):
    if (
        not isinstance(value, str)
        or len(value) > 2048
        or value.startswith("/")
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise Conflict("invalid paged discovery path")
    return value


def frame(path):
    return dict(path=path, token=None, entries=None, next=None)


def load_state(path, root):
    if not path.exists():
        return dict(version=1, root=str(root), stack=[frame("")], retry=[])
    if path.stat().st_size > MAX_STATE_BYTES:
        raise Conflict("paged discovery state exceeds bound")
    value = wire.decode(path.read_bytes())
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or value.get("root") != str(root)
        or not isinstance(value.get("stack"), list)
        or len(value["stack"]) > 8
        or not isinstance(value.get("retry"), list)
        or len(value["retry"]) > 512
    ):
        raise Conflict("invalid paged discovery state")
    for item in value["retry"]:
        relative_path(item)
    for item in value["stack"]:
        if not isinstance(item, dict) or set(item) != {"path", "token", "entries", "next"}:
            raise Conflict("invalid paged discovery frame")
        if item["path"] != "":
            relative_path(item["path"])
        for key in ("token", "next"):
            if item[key] is not None and (not isinstance(item[key], str) or len(item[key]) > 16384):
                raise Conflict("invalid directory page token")
        if item["entries"] is not None:
            if not isinstance(item["entries"], list) or len(item["entries"]) > PAGE_SIZE:
                raise Conflict("invalid directory page entries")
            for entry in item["entries"]:
                validate_entry(entry)
    return value


def validate_entry(entry):
    if (
        not isinstance(entry, dict)
        or set(entry) != {"name", "is_directory"}
        or not isinstance(entry["is_directory"], bool)
        or "/" in relative_path(entry["name"])
    ):
        raise Conflict("invalid directory entry")


def paged_markers(root, control, limit, started, max_seconds, staged, write, page=volume_page):
    """Advance within a wide directory without listing it in full through FUSE."""
    path = Path(control) / "processor-discovery-pages.json"
    state = load_state(path, root)
    metrics = dict(pages=0, entries=0, selected=0, sweep_complete=False, budget_exhausted=False)
    selected = []
    retry = []
    waiting = []
    limit = min(512, limit)
    if limit < 1:
        return [], metrics

    def read(relative):
        marker = root / relative
        parts = relative.split("/")
        if len(parts) == 5 and parts[2] == "batches":
            if parts[3] in staged["scopes"].get("/".join(parts[:2]), {}):
                return None
        if marker.stat().st_size > 65536:
            raise Conflict("oversized spool object")
        receipt = wire.decode(marker.read_bytes())
        expected = namespace(receipt) + "/batches/" + receipt["batch_id"] + "/accepted.json"
        if expected != relative:
            raise Conflict("accepted marker path identity conflict")
        output = Path(control) / namespace(receipt) / "batches" / (receipt["batch_id"] + ".json")
        if output.exists():
            previous = wire.decode(output.read_bytes())
            if previous.get("batch_sha256") != receipt["batch_sha256"]:
                raise Conflict("control receipt conflict")
            if previous.get("items_staged") is True:
                return None
        if (output.parent.parent / "quarantine" / output.name).exists():
            return None
        return marker, receipt

    def consume(paths):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            for relative, row in zip(paths, pool.map(read, paths)):
                if row is not None:
                    selected.append(row)
                    retry.append(relative)

    consume(state["retry"])
    while state["stack"] and len(selected) + len(waiting) < limit:
        if time.monotonic() - started >= max_seconds:
            metrics["budget_exhausted"] = True
            break
        current = state["stack"][-1]
        if current["entries"] is None:
            response = page(root / current["path"], current["token"])
            entries = response.get("contents", [])
            token = response.get("next_page_token")
            if not isinstance(entries, list) or len(entries) > PAGE_SIZE:
                raise Conflict("directory page exceeds bound")
            if token is not None and (not isinstance(token, str) or len(token) > 16384):
                raise Conflict("invalid directory page token")
            if token and token == current["token"]:
                raise Conflict("directory pagination did not advance")
            current["entries"] = [
                dict(name=e.get("name"), is_directory=e.get("is_directory")) for e in entries
            ]
            for entry in current["entries"]:
                validate_entry(entry)
            current["next"] = token or None
            metrics["pages"] += 1
        if not current["entries"]:
            if current["next"] is None:
                state["stack"].pop()
            else:
                current.update(token=current["next"], entries=None, next=None)
            continue
        entry = current["entries"].pop(0)
        metrics["entries"] += 1
        relative = "/".join(filter(None, (current["path"], entry["name"])))
        parts = relative.split("/")
        if not entry["is_directory"]:
            continue
        # Receiver layout is fixed; never descend into bodies or arbitrary files.
        if len(parts) == 1 and parts[0] == "v1":
            state["stack"].append(frame(relative))
        elif len(parts) == 2 and parts[0] == "v1" and wire.HEX.fullmatch(parts[1]):
            state["stack"].append(frame(relative))
        elif len(parts) == 3 and parts[2] == "batches":
            state["stack"].append(frame(relative))
        elif len(parts) == 4 and parts[2] == "batches":
            if parts[3] not in staged["scopes"].get("/".join(parts[:2]), {}):
                # A batch directory can exist before the immutable marker is
                # published. Missing markers are retried on the next full sweep.
                marker = root / relative / "accepted.json"
                if marker.exists() and relative + "/accepted.json" not in retry:
                    waiting.append(relative + "/accepted.json")
                    if len(waiting) == 8:
                        consume(waiting)
                        waiting.clear()
    consume(waiting)
    metrics["sweep_complete"] = not state["stack"]
    if not state["stack"]:
        state["stack"] = [frame("")]
    state["retry"] = retry
    if len(wire.encode(state)) > MAX_STATE_BYTES:
        raise Conflict("paged discovery state exceeds bound")
    write(path, state)
    metrics["selected"] = len(selected)
    metrics["state"] = (
        "work_found"
        if selected
        else ("sweep_complete" if metrics["sweep_complete"] else "scan_in_progress")
    )
    selected.sort(key=lambda row: (min(row[1]["accepted_delivery_seqs"]), str(row[0])))
    return selected, metrics
