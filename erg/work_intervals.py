"""Reuse attribution only inside intervals where every governing fact is stable."""

import bisect
import math
from collections import OrderedDict


def resolver(pr, commit, contexts, links, parents, by_branch, prs, strong, anchors, maxsize=4096):
    if maxsize < 1:
        raise ValueError("positive attribution cache size required")
    boundaries = {}
    cache = OrderedDict()
    stats = dict(hits=0, misses=0)

    def edges(key):
        if key in boundaries:
            return boundaries[key]
        values = set()
        machine, tool, sid = key

        def add(stamp):
            if isinstance(stamp, (int, float)) and math.isfinite(stamp):
                values.add(stamp)

        # Explicit links can be inherited through multiple parent sessions.
        current = sid
        seen = set()
        while current not in seen:
            seen.add(current)
            for link in links.get((tool, current), ()):
                add(link.get("start_ts"))
                add(link.get("end_ts"))
            current = parents.get((tool, current))
            if not current:
                break
        scopes = set()
        branches = set()
        urls = set()
        for context in contexts.get(key, ()):
            add(context["ts"])
            scopes.add(key + (context.get("scope_id"),))
            branches.add((context.get("repo"), context.get("branch")))
        for scope in scopes:
            for stamp in anchors.get(scope, ()):
                add(stamp)
            urls.update(strong.get(scope, {}))
        for branch in branches:
            urls.update(by_branch.get(branch, ()))
        for url in urls:
            p = prs.get(url, {})
            add(p.get("created_ts"))
            add(p.get("merged_ts") or p.get("closed_ts"))
        boundaries[key] = sorted(values)
        return boundaries[key]

    def resolve(machine, tool, session, ts):
        if not isinstance(ts, (int, float)) or not math.isfinite(ts):
            return pr(machine, tool, session, ts), commit(machine, tool, session, ts)
        key = (machine, tool, session)
        stamps = edges(key)
        # Distinguish the exact boundary from both adjacent intervals. This
        # preserves inclusive closures and also works for fractional timestamps.
        interval = key + (bisect.bisect_left(stamps, ts), bisect.bisect_right(stamps, ts))
        if interval in cache:
            stats["hits"] += 1
            cache.move_to_end(interval)
            return cache[interval]
        stats["misses"] += 1
        url, why, tasks = pr(machine, tool, session, ts)
        result = ((url, why, frozenset(tasks)), commit(machine, tool, session, ts))
        cache[interval] = result
        if len(cache) > maxsize:
            cache.popitem(last=False)
        return result

    resolve.cache_info = lambda: dict(
        stats, entries=len(cache), sessions=len(boundaries), maxsize=maxsize
    )
    return resolve
