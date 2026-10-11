"""Changed-record polling through the CRM search API, bounded per poll.

A watch keeps three positions in node storage:

- `floor`: records modified at or before it existed when the watch started and
  are never handed over. On a first start it is the modification time of the
  most recently modified record, read from HubSpot (so the local clock does
  not matter); with emit_existing it is `existing_since_hours` before now.
- `cursor`: the newest modification time already handed over.
- `seen`: digests of (record ID, modification time) handed over within the
  lookback window.

Each poll searches records modified at or after `cursor - LOOKBACK_MS`, oldest
first, because the search index is eventually consistent. A change already in
`seen` is skipped; a later modification of the same record is a new change.
Nothing is persisted here: the caller stores the returned document before it
hands the records over, so a restart never replays them.
"""

from . import logic


def start_floor(session, config, now_ms):
    """The watch start position: HubSpot's newest modification time, or the emit_existing window."""
    if config["emit_existing"]:
        return max(0, now_ms - config["existing_since_hours"] * 3600000)
    kind = config["object_type"]
    body = session.call("POST", logic.objects_path(kind, "search"), None, logic.position_body(config),
                        mutation=False, search=True)
    records, _after, _total = logic.search_page(body, 1, kind)
    if not records:
        return 0
    stamp = logic.epoch_ms(records[0]["updated_at"])
    if stamp is None:
        logic.fail("protocol")
    return stamp


def poll(session, config, document, now_ms):
    """One bounded poll; returns (new document, changed records, summary)."""
    kind = config["object_type"]
    key = logic.watch_key(config)
    entry = document["watches"].get(key)
    started = entry is None
    if entry is None:
        floor = start_floor(session, config, now_ms)
        entry = {"floor": floor, "cursor": floor, "resume": None, "seen": [], "updated_at": now_ms // 1000}
    floor, cursor = entry["floor"], entry["cursor"]
    lower = max(floor + 1, cursor - logic.LOOKBACK_MS)
    resume = entry["resume"]
    after = resume["after"] if resume is not None and resume["lower"] == lower else None
    seen = {digest: stamp for digest, stamp in entry["seen"]}
    size = config["page_size"]
    properties = logic.watch_properties(config)
    found, pages, skipped, stop, last = [], 0, 0, "end", None
    path = logic.objects_path(kind, "search")
    while stop == "end":
        if pages >= config["max_pages"]:
            stop = "max_pages"
            break
        body = session.call("POST", path, None, logic.watch_body(config, lower, size, after, properties),
                            mutation=False, search=True)
        pages += 1
        records, following, _total = logic.search_page(body, size, kind)
        for item in records:
            stamp = logic.epoch_ms(item["updated_at"])
            if stamp is None:
                logic.fail("protocol")
            last = stamp if last is None or stamp > last else last
            digest = logic.seen_digest(item["id"], stamp)
            if stamp <= floor or digest in seen:
                skipped += 1
                continue
            created = logic.epoch_ms(item["created_at"])
            seen[digest] = stamp
            found.append({**item, "change": "created" if created is not None and created > cursor else "updated",
                          "modified_ms": stamp})
            if len(found) >= config["watch_max_items"]:
                stop = "max_items"
                break
        if stop != "end":
            break
        if following is None:
            break
        if logic.capped(following):
            stop = "search_cap"
            break
        after = following
    newest = max([cursor] + [item["modified_ms"] for item in found])
    following_resume = None
    if stop == "max_pages":
        # The next poll continues this exact query instead of re-reading pages it already skipped.
        following_resume = {"lower": lower, "after": after}
    elif stop == "search_cap" and last is not None:
        # More than 10,000 changes in the window: move past the records read, which were all handed or skipped.
        newest = max(newest, last)
    kept = sorted(((digest, stamp) for digest, stamp in seen.items() if stamp >= newest - logic.LOOKBACK_MS),
                  key=lambda pair: (pair[1], pair[0]))[-logic.SEEN_LIMIT:]
    updated = {"floor": floor, "cursor": newest, "resume": following_resume, "seen": [list(pair) for pair in kept],
               "updated_at": now_ms // 1000}
    found.sort(key=lambda item: (item["modified_ms"], item["id"]))
    changed = [{key: value for key, value in item.items() if key != "modified_ms"} for item in found]
    summary = {"object_type": kind, "count": len(changed), "pages": pages, "skipped": skipped, "started": started,
               "stop_reason": stop, "caught_up": stop == "end", "cursor": logic.iso_ms(newest) if newest else None}
    return logic.with_watch(document, key, updated), changed, summary
