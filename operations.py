"""The nine HubSpot operations, built from Session calls and pure logic.

Searches go through pages bounded by page_size, max_pages, max_records, the
10,000-result search window and the output size limit, and always return an
exact `after` cursor when more records remain. Writes are validated and sized
completely before the first request, then sent in batches of at most 100
inputs; a failure reports the batches already committed.
"""

import time

from . import logic, storage, watch


def identity(request):
    return {"request_id": request["request_id"], "operation": request["operation"],
            "object_type": request["object_type"]}


def search(session, request, config, **_):
    """Read pages until the end, max_records, max_pages, the search window or the output limit."""
    kind, limit = request["object_type"], request["max_records"]
    path = logic.objects_path(kind, "search")
    records, weight, pages, total = [], 2, 0, None
    after, stop, next_after = request["after"], "end", None
    while True:
        if pages >= config["max_pages"]:
            stop, next_after = "max_pages", after
            break
        # The last page asks only for the records still needed, so the returned cursor continues exactly.
        size = min(request["page_size"], limit - len(records))
        body = session.call("POST", path, None, logic.search_body(request, size, after, config["max_request_bytes"]),
                            mutation=False, search=True)
        pages += 1
        items, following, reported = logic.search_page(body, size, kind)
        total = reported if reported is not None else total
        page_weight = sum(logic.size_of(item) for item in items)
        if weight + page_weight > logic.OUTPUT_LIMIT:
            if not records:
                logic.fail("output_too_large")
            stop, next_after = "output_limit", after
            break
        records.extend(items)
        weight += page_weight
        after = following
        if after is None:
            break
        if logic.capped(after):
            stop = "search_cap"
            break
        if len(records) >= limit:
            stop, next_after = "limit", after
            break
    result = {**identity(request), "count": len(records), "total": total, "truncated": stop != "end",
              "stop_reason": stop, "after": next_after, "pages": pages, "requests": session.requests}
    return {"records": records, "result": result}


def get(session, request, config, **_):
    kind = request["object_type"]
    body = session.call("GET", logic.objects_path(kind, request["id"]), logic.get_query(request), mutation=False)
    item = logic.record(body, kind)
    if request["id_property"] is None and item["id"] != request["id"]:
        logic.fail("protocol")
    result = {**identity(request), "id": item["id"], "id_property": request["id_property"], "count": 1,
              "requests": session.requests}
    return {"records": [item], "result": result}


def list_properties(session, request, config, **_):
    kind = request["object_type"]
    body = session.call("GET", logic.properties_path(kind), "archived=false", mutation=False)
    results = body.get("results")
    if not isinstance(results, list):
        logic.fail("protocol")
    definitions = [logic.property_definition(item) for item in results]
    definitions.sort(key=lambda item: item["name"])
    result = {**identity(request), "count": len(definitions),
              "unique_properties": [item["name"] for item in definitions if item["unique"]],
              "requests": session.requests}
    return {"records": definitions, "result": result}


def committed(exc, done, total, ids):
    """Attach batch progress to a failure; earlier batches stay written and are listed."""
    exc.progress = {"completed_batches": done, "total_batches": total, "committed_ids": list(ids)}
    if done:
        exc.args = (str(exc) + " " + logic.MESSAGES["partial"],)
    return exc


def partial(done, total, ids, errors):
    """An HTTP 207 receipt: some inputs of this batch were applied, the others refused."""
    exc = logic.HubSpotError("partial_failure", http_status=207)
    exc.progress = {"completed_batches": done, "total_batches": total, "committed_ids": list(ids),
                    "item_errors": errors}
    return exc


def write(session, request, config, **_):
    """create, update, upsert, associate and archive through HubSpot batch endpoints, 100 inputs per request."""
    operation, kind = request["operation"], request["object_type"]
    path, bodies = logic.write_path(request), logic.write_bodies(request, config)
    units = request.get("records") or request.get("pairs") or request.get("ids")
    chunks = logic.batches(units)
    records, ids, created, updated, done_pairs, types = [], [], [], [], [], set()
    for index, (chunk, body) in enumerate(zip(chunks, bodies)):
        try:
            status, receipt = session.request("POST", path, None, body, mutation=True, empty=operation == "archive")
            multi = status == 207
            if operation == "archive":
                if status != 204:
                    logic.fail("protocol", logic.MESSAGES["protocol_uncertain"], uncertain=True)
                ids.extend(chunk)
                continue
            if operation == "associate":
                pairs, found = logic.associated(receipt, chunk, partial=multi)
                done_pairs.extend(pairs)
                types.update(found)
                ids.extend(f"{pair['from_id']}-{pair['to_id']}" for pair in pairs)
            else:
                items, new = logic.batch_records(receipt, len(chunk), kind, partial=multi)
                if operation == "update" and request["id_property"] is None:
                    sent = {item["id"] for item in chunk}
                    if any(item["id"] not in sent for item in items):
                        logic.fail("protocol", logic.MESSAGES["protocol_uncertain"], uncertain=True)
                records.extend(items)
                ids.extend(item["id"] for item in items)
                if operation == "upsert":
                    created.extend(new)
                    updated.extend(item["id"] for item in items if item["id"] not in new)
            if multi:
                raise partial(index, len(chunks), ids, logic.item_errors(receipt, session.secrets))
        except logic.HubSpotError as exc:
            if exc.code == "partial_failure":
                raise
            raise committed(exc, index, len(chunks), ids) from None
    result = {**identity(request), "count": len(units), "batches": len(chunks), "requests": session.requests}
    if operation == "archive":
        result["archived_ids"] = ids
        return {"result": result}
    if operation == "associate":
        result.update(to_object_type=request["to_object_type"], pairs=done_pairs, association_type_ids=sorted(types))
        return {"result": result}
    result["ids"] = ids
    if request.get("id_property"):
        result["id_property"] = request["id_property"]
    if operation == "upsert":
        result.update(created_ids=created, updated_ids=updated)
    return {"records": records, "result": result}


def run_watch(session, request, config, storage_dir=None, now_ms=None, **_):
    """One Shot (or a request) runs one bounded poll; the listener runs the same poll on its interval."""
    store = storage.WatchStore(storage.storage_directory(storage_dir))
    if not store.acquire():
        logic.fail("busy")
    try:
        document = store.load()
        updated, changed, summary = watch.poll(session, config, document,
                                               now_ms if now_ms is not None else int(time.time() * 1000))
        store.save(updated)
    finally:
        store.close()
    return {"changed": changed, "result": {"request_id": request["request_id"], "operation": "watch", **summary,
                                           "source": "request", "requests": session.requests}}


HANDLERS = {"search": search, "get": get, "list_properties": list_properties, "create": write, "update": write,
            "upsert": write, "associate": write, "archive": write, "watch": run_watch}


def run(session, request, config, **extra):
    return HANDLERS[request["operation"]](session, request, config, **extra)
