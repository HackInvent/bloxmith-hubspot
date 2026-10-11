"""FB2-FB6: loopback HubSpot protocol, pages, batches, pacing, errors, watch, listener, cancellation and both runtimes.

Every provider exchange targets the loopback double in hubspot_fixture.py. The
wallet and its obviously fake token exist only in disposable test storage.
"""

import json
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(Path(__file__).resolve().parent)]
from blocs.hubspot import listener, logic, operations, storage, transport, watch
from blocs.hubspot.block import HubSpotBlock
from block_test_packages import install_test_package, prepare_release_run, surface_payload
from hubspot_fixture import DAILY_429, EXPIRED, LIMIT_429, REF, TOKEN, error, hubspot_api, iso, settings
from ui_smoke_common import (create_project_api, create_run_api, data_edge, display_node, graph_payload, http_json,
                             isolated_server, stop_run_api, text_node, wait_for_run_predicate, wait_for_run_terminal)

ALL = ["search", "get", "create", "update", "upsert", "associate", "archive", "list_properties", "watch"]


def config_of(api, **extra):
    return logic.configuration(settings(api, **{"max_search_per_sec": 5, **extra}))


def run(config, value, token=TOKEN, cancel=lambda: False, storage_dir=None, now_ms=None):
    """Execute one request through the real session and operations."""
    session = transport.Session(config, token, cancel)
    return operations.run(session, logic.parse_request(value, config), config, storage_dir=storage_dir, now_ms=now_ms)


def failure(call, code):
    try:
        call()
    except logic.HubSpotError as exc:
        assert exc.code == code, (code, exc.code, str(exc))
        assert TOKEN not in json.dumps(exc.envelope())
        return exc
    raise AssertionError("Expected " + code)


def until(predicate, timeout=10.0, label="condition not reached"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(label)


def names(result):
    return [item["properties"].get("firstname") for item in result["records"]]


def deals(api, count=25):
    return [api.add("deals", {"dealname": f"Deal {index:02d}", "amount": index * 10, "dealstage": "open"})
            for index in range(1, count + 1)]


def read_protocol():
    """# - FB2 - # - FB4 - filters, query, one sort, properties, exact after cursors, search window, get and properties."""
    with hubspot_api() as api:
        config = config_of(api)
        read = run(config, "go")
        assert names(read) == ["Ada", "Grace", "Linus"]
        ada = read["records"][0]
        assert ada["id"] == "1001" and ada["object_type"] == "contacts" and ada["archived"] is False
        assert set(ada["properties"]) == {"firstname", "lastname", "email", "createdate", "lastmodifieddate", "hs_object_id"}
        assert ada["properties"]["email"] == "ada@example.test" and ada["updated_at"] == ada["properties"]["lastmodifieddate"]
        assert read["result"] == {"request_id": None, "operation": "search", "object_type": "contacts", "count": 3,
                                  "total": 3, "truncated": False, "stop_reason": "end", "after": None, "pages": 1,
                                  "requests": 1}
        call = api.calls[-1]
        assert call["method"] == "POST" and call["path"] == "/crm/v3/objects/contacts/search"
        assert call["body"] == {"filterGroups": [], "limit": 100}
        assert call["auth"] == "Bearer " + TOKEN and call["accept_encoding"] == "identity"
        lead = {"propertyName": "lifecyclestage", "operator": "EQ", "value": "lead"}
        assert names(run(config, {"filters": [lead]})) == ["Ada", "Linus"]
        assert api.calls[-1]["body"]["filterGroups"] == [{"filters": [lead]}]
        assert names(run(config, {"filters": [lead, {"propertyName": "age", "operator": "GT", "value": 30}]})) == ["Ada"]
        either = run(config, {"filter_groups": [{"filters": [{"propertyName": "firstname", "operator": "EQ", "value": "Ada"}]},
                                                {"filters": [{"propertyName": "firstname", "operator": "IN",
                                                              "values": ["Linus", "Nobody"]}]}]})
        assert names(either) == ["Ada", "Linus"]
        assert names(run(config, {"filters": [{"propertyName": "age", "operator": "HAS_PROPERTY"}]})) == ["Ada", "Grace"]
        assert names(run(config, {"sorts": [{"propertyName": "firstname", "direction": "DESCENDING"}]})) == ["Linus", "Grace", "Ada"]
        assert api.calls[-1]["body"]["sorts"] == [{"propertyName": "firstname", "direction": "DESCENDING"}]
        assert names(run(config, {"query": "hopper"})) == ["Grace"] and api.calls[-1]["body"]["query"] == "hopper"
        slim = run(config, {"properties": ["email"], "max_records": 1})
        assert set(slim["records"][0]["properties"]) == {"email", "createdate", "lastmodifieddate", "hs_object_id"}
        assert api.calls[-1]["body"]["properties"] == ["email"] and api.calls[-1]["body"]["limit"] == 1
        assert run(config_of(api, properties=["phone"]), {"max_records": 1})["records"][0]["properties"]["phone"] is None
        # Pagination: pages of 10, a shorter last page for max_records, and an exact continuation cursor.
        ids = deals(api)
        pages = config_of(api, object_type="deals", page_size=10)
        before = len(api.calls)
        first = run(pages, {"max_records": 15, "request_id": "p-1"})
        assert [call["body"]["limit"] for call in api.calls[before:]] == [10, 5]
        assert [item["id"] for item in first["records"]] == ids[:15] and first["records"][0]["object_type"] == "deals"
        result = first["result"]
        assert result["stop_reason"] == "limit" and result["truncated"] and result["pages"] == 2 and result["total"] == 25
        assert result["after"] == "15" and result["request_id"] == "p-1"
        rest = run(pages, {"after": result["after"]})
        assert [item["id"] for item in rest["records"]] == ids[15:], "no record skipped or repeated"
        assert rest["result"]["stop_reason"] == "end" and rest["result"]["after"] is None
        assert api.calls[-1]["body"]["after"] == "15"
        capped = run(config_of(api, object_type="deals", page_size=10, max_pages=2), "go")
        assert capped["result"]["count"] == 20 and capped["result"]["stop_reason"] == "max_pages"
        assert capped["result"]["after"] == "20" and capped["result"]["truncated"]
        assert [item["id"] for item in run(pages, {"after": "20"})["records"]] == ids[20:]
        assert run(pages, {"max_records": 25})["result"]["stop_reason"] == "end"
        # The output limit keeps whole pages only, so the cursor re-reads the page that did not fit.
        limit = logic.OUTPUT_LIMIT
        try:
            logic.OUTPUT_LIMIT = 2600
            sized = run(config_of(api, object_type="deals", page_size=2), "go")
            count = sized["result"]["count"]
            assert sized["result"]["stop_reason"] == "output_limit" and count % 2 == 0 and 2 <= count < 25
            following = run(config_of(api, object_type="deals", page_size=2), {"after": sized["result"]["after"]})
            assert following["records"][0]["id"] == ids[count]
            logic.OUTPUT_LIMIT = 50
            failure(lambda: run(pages, "go"), "output_too_large")
        finally:
            logic.OUTPUT_LIMIT = limit
        # HubSpot search stops at 10,000 results: the block reports it instead of asking for an invalid page.
        page = {"total": 25000, "results": [], "paging": {"next": {"after": "10000"}}}
        api.script["search"] = [{"status": 200, "body": page}]
        windowed = run(pages, {"after": "9990"})
        assert windowed["result"]["stop_reason"] == "search_cap" and windowed["result"]["after"] is None
        assert windowed["result"]["truncated"] and windowed["result"]["total"] == 25000
        # get by ID or by a unique property, with associations; list_properties.
        one = run(config, {"operation": "get", "id": "1002", "request_id": "g-1"})
        assert one["records"][0]["properties"]["firstname"] == "Grace" and one["result"]["id"] == "1002"
        assert api.calls[-1]["path"] == "/crm/v3/objects/contacts/1002" and api.calls[-1]["query"] == {"archived": ["false"]}
        api.links.update({("contacts", "1001", "companies", "1004"), ("companies", "1004", "contacts", "1001")})
        keyed = run(config, {"operation": "get", "id": "ada@example.test", "id_property": "email",
                             "associations": ["companies", "deals"], "properties": ["lifecyclestage"]})
        assert keyed["records"][0]["id"] == "1001" and keyed["records"][0]["associations"] == {"companies": ["1004"]}
        assert keyed["records"][0]["properties"]["lifecyclestage"] == "lead" and keyed["result"]["id_property"] == "email"
        assert api.calls[-1]["path"] == "/crm/v3/objects/contacts/ada%40example.test"
        assert api.calls[-1]["query"]["idProperty"] == ["email"] and api.calls[-1]["query"]["associations"] == ["companies,deals"]
        missing = failure(lambda: run(config, {"operation": "get", "id": "999999"}), "not_found")
        assert missing.provider_category == "OBJECT_NOT_FOUND" and missing.http_status == 404 and not missing.uncertain
        assert missing.correlation_id == "fixture-correlation-0001"
        failure(lambda: run(config, {"operation": "get", "id": "nobody@example.test", "id_property": "email"}), "not_found")
        unknown = failure(lambda: run(config, {"properties": ["nope"]}), "bad_request")
        assert unknown.provider_category == "VALIDATION_ERROR" and "nope" in unknown.provider_message
        api.denied.add("tickets")
        scoped = failure(lambda: run(config, {"object_type": "tickets"}), "forbidden")
        assert scoped.provider_category == "MISSING_SCOPES" and scoped.http_status == 403
        schema = run(config, {"operation": "list_properties", "request_id": "s-1"})
        email = next(item for item in schema["records"] if item["name"] == "email")
        assert email["unique"] is True and email["type"] == "string" and email["read_only"] is False
        stage = next(item for item in schema["records"] if item["name"] == "lifecyclestage")
        assert stage["options"] == [{"label": "Lead", "value": "lead"}, {"label": "Customer", "value": "customer"}]
        assert [item["name"] for item in schema["records"]] == sorted(item["name"] for item in schema["records"])
        assert schema["result"]["unique_properties"] == ["email"] and schema["result"]["request_id"] == "s-1"
        assert api.calls[-1]["path"] == "/crm/v3/properties/contacts" and api.calls[-1]["method"] == "GET"
        assert run(config, {"operation": "list_properties", "object_type": "companies"})["result"]["unique_properties"] == \
            ["external_id"]
    print("[ok] HubSpot search filters, sorts, query, pages and cursors, search window, get, associations and properties",
          flush=True)


def write_protocol():
    """# - FB3 - create, update, upsert, associate and archive in batches of 100; partial progress, never replayed."""
    with hubspot_api() as api:
        config = config_of(api, allow_writes=True, allow_delete=True, id_property="email", allowed_operations=ALL,
                           max_write_records=1000)
        many = [{"properties": {"email": f"p{index}@example.test", "firstname": f"P{index}", "age": index}}
                for index in range(230)]
        before = len(api.calls)
        created = run(config, {"operation": "create", "records": many, "request_id": "c-1"})
        calls = api.calls[before:]
        assert [call["kind"] for call in calls] == ["create"] * 3
        assert [len(call["body"]["inputs"]) for call in calls] == [100, 100, 30]
        assert all(call["path"] == "/crm/v3/objects/contacts/batch/create" and call["method"] == "POST" for call in calls)
        result = created["result"]
        assert result["count"] == 230 and result["batches"] == 3 and result["request_id"] == "c-1"
        assert result["ids"] == [item["id"] for item in created["records"]] and len(set(result["ids"])) == 230
        assert created["records"][0]["properties"]["age"] == "0", "HubSpot stores property values as text"
        conflict = failure(lambda: run(config, {"operation": "create", "record": {"properties": {"email": "ada@example.test"}}}),
                           "conflict")
        assert conflict.provider_category == "CONFLICT" and "1001" in conflict.provider_message
        assert conflict.envelope()["completed_batches"] == 0 and conflict.envelope()["committed_ids"] == []
        invalid = failure(lambda: run(config, {"operation": "create", "record": {"properties": {"nope": "1"}}}), "bad_request")
        assert "PROPERTY_DOESNT_EXIST" in invalid.provider_message
        failure(lambda: run(config, {"operation": "create", "record": {"properties": {"age": "old"}}}), "bad_request")
        updated = run(config, {"operation": "update", "records": [{"id": "1001", "properties": {"phone": "555"}}]})
        assert api.calls[-1]["path"] == "/crm/v3/objects/contacts/batch/update"
        assert api.calls[-1]["body"] == {"inputs": [{"id": "1001", "properties": {"phone": "555"}}]}
        assert updated["records"][0]["properties"]["phone"] == "555" and updated["result"]["ids"] == ["1001"]
        by_email = run(config, {"operation": "update", "id_property": "email",
                                "record": {"id": "grace@example.test", "properties": {"phone": "777"}}})
        assert by_email["result"]["ids"] == ["1002"] and by_email["result"]["id_property"] == "email"
        assert api.calls[-1]["body"]["inputs"][0]["idProperty"] == "email"
        upsert = run(config, {"operation": "upsert", "records": [
            {"id": "ada@example.test", "properties": {"lifecyclestage": "customer"}},
            {"id": "new@example.test", "properties": {"firstname": "New"}}]})
        assert api.calls[-1]["path"] == "/crm/v3/objects/contacts/batch/upsert"
        assert api.calls[-1]["body"]["inputs"][0] == {"id": "ada@example.test", "properties": {"lifecyclestage": "customer"},
                                                      "idProperty": "email"}
        assert upsert["result"]["updated_ids"] == ["1001"] and len(upsert["result"]["created_ids"]) == 1
        fresh = upsert["result"]["created_ids"][0]
        assert api.objects["contacts"][fresh]["properties"]["email"] == "new@example.test"
        assert api.objects["contacts"]["1001"]["properties"]["lifecyclestage"] == "customer"
        company = run(config, {"operation": "upsert", "object_type": "companies", "id_property": "external_id",
                               "record": {"id": "AE-1", "properties": {"industry": "lead"}}})
        assert company["result"]["updated_ids"] == ["1004"] and company["records"][0]["object_type"] == "companies"
        refused = failure(lambda: run(config, {"operation": "upsert", "id_property": "firstname",
                                               "record": {"id": "Ada", "properties": {}}}), "bad_request")
        assert "unique" in refused.provider_message
        linked = run(config, {"operation": "associate", "to_object_type": "companies",
                              "pairs": [{"from_id": "1002", "to_id": "1004"}, {"from_id": fresh, "to_id": "1004"}]})
        assert api.calls[-1]["path"] == "/crm/v4/associations/contacts/companies/batch/associate/default"
        assert api.calls[-1]["body"] == {"inputs": [{"from": {"id": "1002"}, "to": {"id": "1004"}},
                                                    {"from": {"id": fresh}, "to": {"id": "1004"}}]}
        assert linked["result"]["association_type_ids"] == [279, 280] and len(linked["result"]["pairs"]) == 2
        assert "records" not in linked and linked["result"]["count"] == 2
        assert run(config, {"operation": "get", "id": "1002", "associations": ["companies"]})["records"][0]["associations"] == \
            {"companies": ["1004"]}
        archived = run(config, {"operation": "archive", "ids": ["1003"]})
        assert api.calls[-1]["path"] == "/crm/v3/objects/contacts/batch/archive" and api.calls[-1]["body"] == {"inputs": [{"id": "1003"}]}
        assert archived["result"]["archived_ids"] == ["1003"] and "records" not in archived
        assert api.objects["contacts"]["1003"]["archived"] is True, "archived records stay in HubSpot's recycle bin"
        failure(lambda: run(config, {"operation": "get", "id": "1003"}), "not_found")
        assert "Linus" not in names(run(config, {"query": "linus"}))
        # HTTP 207: part of a batch applied; later batches are not sent.
        before = len(api.calls)
        partial = failure(lambda: run(config, {"operation": "update", "records": [
            {"id": "1001", "properties": {"phone": "1"}}, {"id": "999999", "properties": {"phone": "2"}}]}), "partial_failure")
        envelope = partial.envelope()
        assert envelope["http_status"] == 207 and envelope["committed_ids"] == ["1001"] and not envelope["uncertain"]
        assert envelope["completed_batches"] == 0 and envelope["total_batches"] == 1
        assert envelope["item_errors"] == [{"category": "OBJECT_NOT_FOUND", "message": envelope["item_errors"][0]["message"],
                                            "ids": ["999999"]}]
        assert len(api.calls) == before + 1 and api.objects["contacts"]["1001"]["properties"]["phone"] == "1"
        dangling = failure(lambda: run(config, {"operation": "associate", "to_object_type": "companies",
                                                "pairs": [{"from_id": "1001", "to_id": "1004"},
                                                          {"from_id": "1001", "to_id": "999998"}]}), "partial_failure")
        assert dangling.envelope()["committed_ids"] == ["1001-1004"]
        # A refused second batch leaves the first one written and says so.
        api.script["create"] = ["pass", {"status": 400, "body": error("VALIDATION_ERROR", "Property values were not valid")}]
        before_ids = set(api.objects["contacts"])
        late = [{"properties": {"email": f"late{index}@example.test"}} for index in range(150)]
        rejected = failure(lambda: run(config, {"operation": "create", "records": late}), "bad_request")
        added = set(api.objects["contacts"]) - before_ids
        assert rejected.envelope()["completed_batches"] == 1 and rejected.envelope()["total_batches"] == 2
        assert set(rejected.envelope()["committed_ids"]) == added and len(added) == 100
        assert "committed_ids" in rejected.envelope()["message"]
        # A lost connection on a later batch is uncertain and never replayed.
        api.script["update"] = ["pass", "lost"]
        targets = [{"id": ident, "properties": {"phone": "9"}} for ident in result["ids"][:150]]
        before = len(api.calls)
        lost = failure(lambda: run(config, {"operation": "update", "records": targets}), "network")
        assert lost.uncertain and lost.envelope()["completed_batches"] == 1 and len(lost.envelope()["committed_ids"]) == 100
        assert api.kinds()[before:] == ["update", "update"]
        api.script["archive"] = [{"status": 200, "body": {"status": "COMPLETE"}}]
        odd = failure(lambda: run(config, {"operation": "archive", "id": "1002"}), "protocol")
        assert odd.uncertain, "an archive receipt other than 204 is uncertain"
        # The write switches are enforced before any request.
        before = len(api.calls)
        failure(lambda: run(config_of(api), {"operation": "create", "record": {"properties": {}}}), "operation_not_allowed")
        failure(lambda: logic.parse_request({"operation": "archive", "id": "1001"}, {**config, "allow_delete": False}),
                "delete_disabled")
        assert len(api.calls) == before
    print("[ok] HubSpot batched create, update, upsert, associate, archive, 207 receipts and partial progress", flush=True)


def pacing_protocol():
    """# - FB4 - at most max_requests_per_10s per token in any window and max_search_per_sec searches, process-wide."""
    with hubspot_api() as api:
        window = transport.GENERAL_WINDOW
        try:
            # The ten-second window is shortened to one second here; F5.01 checks the ten-second arithmetic.
            transport.GENERAL_WINDOW = 1.0
            config = config_of(api, max_requests_per_10s=3, allowed_operations=["search", "get"])
            started = time.monotonic()
            for _ in range(7):
                run(config, {"operation": "get", "id": "1001"})
            assert time.monotonic() - started >= 1.8
            times = [call["at"] for call in api.calls]
            assert all(times[index + 3] - times[index] >= 0.8 for index in range(len(times) - 3)), times
            # Two sessions with the same token share one window; another token has its own.
            api.tokens.add(EXPIRED)
            before, results = len(api.calls), []
            workers = [threading.Thread(target=lambda: results.append(run(config, {"operation": "get", "id": "1001"})))
                       for _ in range(6)]
            started = time.monotonic()
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(30)
            assert len(results) == 6 and time.monotonic() - started >= 0.8
            times = sorted(call["at"] for call in api.calls[before:])
            assert all(times[index + 3] - times[index] >= 0.8 for index in range(len(times) - 3)), times
            started = time.monotonic()
            run(config, {"operation": "get", "id": "1001"}, token=EXPIRED)
            assert time.monotonic() - started < 0.5, "another token is paced separately"
        finally:
            transport.GENERAL_WINDOW = window
        searching = config_of(api, max_search_per_sec=2)
        before = len(api.calls)
        started = time.monotonic()
        for _ in range(5):
            run(searching, "go")
        assert time.monotonic() - started >= 1.8
        times = [call["at"] for call in api.calls[before:]]
        assert all(times[index + 2] - times[index] >= 0.9 for index in range(len(times) - 2)), times
    print("[ok] HubSpot ten-second and search pacing per token", flush=True)


def error_protocol():
    """# - FB4 - 401/403/429 policies/5xx, lost connections, oversized bodies, cancellation, deadlines and TLS."""
    with hubspot_api() as api:
        config = config_of(api, allow_writes=True, allowed_operations=["search", "create"], max_retry_wait_sec=2)
        create = {"operation": "create", "record": {"properties": {"firstname": "Z"}}}
        expired = failure(lambda: run(config, "go", EXPIRED), "unauthorized")
        assert expired.provider_category == "INVALID_AUTHENTICATION" and expired.http_status == 401 and not expired.uncertain
        # 429: Retry-After is honoured within bounds for reads and writes; the ten-second policy waits its window.
        api.script["search"] = [{"status": 429, "body": LIMIT_429, "headers": {"Retry-After": "1"}}]
        started = time.monotonic()
        assert run(config, "go")["result"]["requests"] == 2 and time.monotonic() - started >= 1
        api.script["create"] = [{"status": 429, "body": LIMIT_429, "headers": {"Retry-After": "1"}}]
        assert run(config, create)["result"]["count"] == 1 and api.kinds()[-2:] == ["create", "create"]
        api.script["search"] = [{"status": 429, "body": LIMIT_429}]
        limited = failure(lambda: run(config, "go"), "rate_limited")
        assert limited.retry_after == 10 and limited.attempts == 1 and limited.provider_category == "RATE_LIMIT"
        api.script["search"] = [{"status": 429, "body": DAILY_429, "headers": {"Retry-After": "1"}}]
        before = len(api.calls)
        daily = failure(lambda: run(config, "go"), "daily_limit")
        assert daily.attempts == 1 and len(api.calls) == before + 1, "the daily limit is never retried"
        api.script["search"] = [{"status": 429, "body": LIMIT_429, "headers": {"Retry-After": "1"}}] * 3
        assert failure(lambda: run(config_of(api, max_retries=0), "go"), "rate_limited").attempts == 1
        api.script["search"] = []
        # Reads retry 5xx and lost connections; writes never replay an ambiguous outcome.
        api.script["search"] = ["html502", "lost"]
        before = len(api.calls)
        assert run(config, "go")["result"]["count"] == 3 + 1 and api.kinds()[before:] == ["search"] * 3
        for scripted, code in (({"status": 503, "body": error("INTERNAL_ERROR", "Try again later.")}, "provider_unavailable"),
                               ("html502", "provider_unavailable"), ("lost", "network"), ("oversized", "protocol"),
                               ("gzip", "protocol")):
            api.script["create"] = [scripted]
            before = len(api.calls)
            outcome = failure(lambda: run(config_of(api, allow_writes=True, allowed_operations=["create"],
                                                    max_response_bytes=65536), create), code)
            assert outcome.uncertain and api.kinds()[before:] == ["create"], scripted
        api.script["search"] = ["oversized"]
        assert not failure(lambda: run(config_of(api, max_response_bytes=65536, max_retries=0), "go"), "protocol").uncertain
        api.script["search"] = [{"status": 400, "body": error("VALIDATION_ERROR", "Invalid input JSON")}]
        assert failure(lambda: run(config, "go"), "bad_request").provider_category == "VALIDATION_ERROR"
        api.script["create"] = [{"status": 413, "body": error("VALIDATION_ERROR", "Request entity too large")}]
        assert not failure(lambda: run(config, create), "provider_too_large").uncertain
        # Cancellation and the deadline shut the socket down while HubSpot holds the request.
        api.script["create"] = ["hold"]
        started = time.monotonic()
        cancelled = failure(lambda: run(config, create, cancel=lambda: api.holding >= 1), "cancelled")
        assert cancelled.uncertain and time.monotonic() - started < 2.5
        api.release.set()
        time.sleep(0.2)
        api.release.clear()
        api.script["search"] = ["hold"]
        started = time.monotonic()
        quiet = failure(lambda: run(config, "go", cancel=lambda: api.holding >= 1), "cancelled")
        assert not quiet.uncertain and time.monotonic() - started < 2.5
        api.release.set()
        time.sleep(0.2)
        api.release.clear()
        api.script["create"] = ["hold"]
        started = time.monotonic()
        timed = failure(lambda: run(config_of(api, allow_writes=True, allowed_operations=["create"], timeout_sec=5), create),
                        "timeout")
        assert timed.uncertain and 4 <= time.monotonic() - started < 8
        api.release.set()
        time.sleep(0.2)
        assert not [thread for thread in threading.enumerate() if thread.name == transport.THREAD_NAME]
        assert not failure(lambda: run(config_of(api, api_base_url="http://127.0.0.1:9", max_retries=0), "go"),
                           "network").uncertain
        before = len(api.calls)
        failure(lambda: run(config, "go", cancel=lambda: True), "cancelled")
        assert len(api.calls) == before
    with hubspot_api(tls=True) as api:
        assert run(config_of(api), "go")["result"]["count"] == 3
        before = len(api.calls)
        untrusted = failure(lambda: run(config_of(api, ca_file=""), "go"), "tls")
        assert not untrusted.uncertain and len(api.calls) == before
    print("[ok] HubSpot errors, 429 policies, retries, uncertainty, cancellation, deadline and TLS", flush=True)


def watch_protocol():
    """# - FB5 - modification cursor: no flood on first start, each change once, bounded polls, lookback and One Shot poll."""
    with hubspot_api() as api, tempfile.TemporaryDirectory() as folder:
        node = Path(folder).resolve()
        config = config_of(api, watch=True, page_size=5, watch_max_items=5, allowed_operations=["search", "watch"])

        def poll(document, conf=config):
            return watch.poll(transport.Session(conf, TOKEN), conf, document, api.now_ms())

        newest = max(item["updated"] for item in api.objects["contacts"].values())
        before = len(api.calls)
        document, found, summary = poll(logic.empty_state())
        assert found == [] and summary == {"object_type": "contacts", "count": 0, "pages": 1, "skipped": 0, "started": True,
                                           "stop_reason": "end", "caught_up": True, "cursor": iso(newest)}
        position, first = api.calls[before], api.calls[before + 1]
        assert position["body"] == json.loads(logic.position_body(config)), "the start position comes from HubSpot's clock"
        assert first["body"]["filterGroups"] == [{"filters": [{"propertyName": "lastmodifieddate", "operator": "GTE",
                                                               "value": str(newest + 1)}]}]
        assert first["body"]["sorts"] == [{"propertyName": "lastmodifieddate", "direction": "ASCENDING"}]
        entry = document["watches"][logic.watch_key(config)]
        assert entry["floor"] == newest == entry["cursor"] and entry["seen"] == []
        stamp = api.touch("contacts", "1001", {"phone": "1"})
        document, found, summary = poll(document)
        assert [(item["id"], item["change"]) for item in found] == [("1001", "updated")] and not summary["started"]
        assert found[0]["updated_at"] == iso(stamp) and found[0]["object_type"] == "contacts"
        document, found, summary = poll(document)
        assert found == [] and summary["skipped"] == 1, "the lookback re-reads a change that is already handed over"
        created = api.add("contacts", {"email": "watch@example.test", "firstname": "Watch"})
        api.touch("contacts", "1001", {"phone": "2"})
        document, found, _summary = poll(document)
        assert [(item["id"], item["change"]) for item in found] == [(created, "created"), ("1001", "updated")]
        # A burst is handed over in bounded polls, each change exactly once.
        burst = [api.add("contacts", {"email": f"b{index}@example.test"}) for index in range(12)]
        batches = []
        for _ in range(3):
            document, found, summary = poll(document)
            batches.append([item["id"] for item in found])
        assert [len(batch) for batch in batches] == [5, 5, 2] and sum(batches, []) == burst and summary["caught_up"]
        assert summary["stop_reason"] == "end"
        # The search index is eventually consistent: a change indexed late is still found in the lookback window.
        late = api.add("contacts", {"email": "late@example.test"})
        api.hidden.add(("contacts", late))
        early = api.touch("contacts", "1002", {"phone": "3"})
        document, found, _summary = poll(document)
        assert [item["id"] for item in found] == ["1002"] and api.objects["contacts"][late]["updated"] < early
        api.hidden.clear()
        document, found, _summary = poll(document)
        assert [item["id"] for item in found] == [late]
        # emit_existing: a fresh cursor hands over the recent changes once, bounded per poll.
        recent = config_of(api, watch=True, emit_existing=True, existing_since_hours=1, page_size=50, watch_max_items=50)
        visible = sorted((item["updated"], ident) for ident, item in api.objects["contacts"].items() if not item["archived"])
        existing, found, summary = poll(logic.empty_state(), conf=recent)
        assert [item["id"] for item in found] == [ident for _stamp, ident in visible] and summary["started"]
        assert poll(existing, conf=recent)[1] == []
        # max_pages: the next poll resumes the same query instead of re-reading skipped pages.
        ids = deals(api, 6)
        narrow = config_of(api, watch=True, emit_existing=True, object_type="deals", page_size=2, max_pages=1,
                           watch_max_items=100)
        state, handed = logic.empty_state(), []
        for _ in range(8):
            state, found, summary = poll(state, conf=narrow)
            handed.extend(item["id"] for item in found)
        assert handed == ids, handed
        assert any(call["body"].get("after") for call in api.calls[-8:]), "a skipped window is resumed with after"
        # More than 10,000 changes in the window: the cursor moves past the records read instead of looping.
        cursor_before = state["watches"][logic.watch_key(narrow)]["cursor"]
        flood = api.objects["deals"][ids[-1]]["updated"] + 5000
        body = {"total": 20000, "results": [{"id": "77", "properties": {"dealname": "Flood"}, "createdAt": iso(flood),
                                             "updatedAt": iso(flood), "archived": False}],
                "paging": {"next": {"after": "10000"}}}
        api.script["search"] = [{"status": 200, "body": body}]
        state, found, summary = poll(state, conf=narrow)
        assert summary["stop_reason"] == "search_cap" and [item["id"] for item in found] == ["77"]
        assert state["watches"][logic.watch_key(narrow)]["cursor"] == flood > cursor_before
        # One Shot or a request: one poll with the cursor persisted in node storage under a lock.
        first = run(config, {"operation": "watch", "request_id": "w-1"}, storage_dir=lambda: node, now_ms=api.now_ms())
        assert first["changed"] == [] and first["result"]["started"] and first["result"]["source"] == "request"
        assert first["result"]["request_id"] == "w-1" and first["result"]["operation"] == "watch"
        api.touch("contacts", "1001", {"phone": "4"})
        second = run(config, {"operation": "watch"}, storage_dir=lambda: node)
        assert [item["id"] for item in second["changed"]] == ["1001"] and not second["result"]["started"]
        assert run(config, {"operation": "watch"}, storage_dir=lambda: node)["changed"] == []
        state_text = (node / storage.STATE_NAME).read_text()
        assert TOKEN not in state_text and REF not in state_text and "example.test" not in state_text and '"id"' not in state_text
        holder = storage.WatchStore(node)
        assert holder.acquire()
        try:
            failure(lambda: run(config, {"operation": "watch"}, storage_dir=lambda: node), "busy")
        finally:
            holder.close()
        failure(lambda: run(config, {"operation": "watch"}), "storage")
        (node / storage.STATE_NAME).write_text('{"schema_version": 1, "watches": {"x": 1}}')
        failure(lambda: run(config, {"operation": "watch"}, storage_dir=lambda: node), "state_invalid")
    print("[ok] HubSpot watch cursor, no flood, bounded polls, lookback, resume, search window, One Shot poll and lock",
          flush=True)


def listener_protocol():
    """# - FB5 - the Active Runtime watcher persists before hand-off, never replays after restart and stops promptly."""
    with hubspot_api() as api, tempfile.TemporaryDirectory() as folder:
        node = Path(folder).resolve()
        # The 60 s minimum is a settings rule; the watcher object itself is driven faster here.
        fast = {**config_of(api, watch=True), "watch_interval_sec": 0.3}

        def start(emit, secret=lambda: json.dumps({"token": TOKEN}), directory=lambda: str(node)):
            stop = threading.Event()
            watcher = listener.ChangeWatcher(fast, resolve_secret=secret, storage_dir=directory, emit=emit, stop=stop.is_set)
            thread = threading.Thread(target=watcher.run, name="hubspot-test-watcher")
            thread.start()
            return stop, thread

        def finish(stop, thread):
            started = time.monotonic()
            stop.set()
            thread.join(3)
            assert not thread.is_alive() and time.monotonic() - started < 1.5, "The watcher must stop promptly"

        emitted = []
        stop, thread = start(lambda kind, value, stats: emitted.append((kind, value, stats)))
        try:
            until(lambda: any(kind == "status" for kind, _v, _s in emitted), label="first poll missing")
            assert not any(kind == "changed" for kind, _v, _s in emitted), "The first start never floods"
            api.touch("contacts", "1002", {"phone": "live"})
            until(lambda: any(kind == "changed" for kind, _v, _s in emitted), label="change not handed over")
            kind, (records, result), stats = next(item for item in emitted if item[0] == "changed")
            assert [item["id"] for item in records] == ["1002"] and result["source"] == "watch" and result["count"] == 1
            assert result["operation"] == "watch" and stats["state"] == "watching" and stats["changed"] == 1
            time.sleep(1)
            assert sum(1 for item in emitted if item[0] == "changed") == 1, "Each change is handed over once"
        finally:
            finish(stop, thread)
        stamp = api.touch("contacts", "1003", {"phone": "stopped"})
        emitted.clear()
        stop, thread = start(lambda kind, value, stats: emitted.append((kind, value, stats)))
        try:
            until(lambda: any(kind == "changed" for kind, _v, _s in emitted), label="restart lost a change")
            records = next(v for k, v, _s in emitted if k == "changed")[0]
            assert [item["id"] for item in records] == ["1003"] and records[0]["updated_at"] == iso(stamp)
            # A request poll while the listener holds the cursor is refused, not interleaved.
            failure(lambda: run(logic.configuration({**fast, "watch_interval_sec": 60, "allowed_operations": ["watch"]}),
                                {"operation": "watch"}, storage_dir=lambda: node), "busy")
        finally:
            finish(stop, thread)
        # A refused hand-off restores the stored cursor: the next Run hands the same change over again.
        api.touch("contacts", "1001", {"phone": "refused"})
        refused = []

        def refusing(kind, value, stats):
            refused.append(kind)
            if kind == "changed":
                raise RuntimeError("runtime_listeners.queue_full")

        stop, thread = start(refusing)
        try:
            until(lambda: "changed" in refused, label="refused hand-off not attempted")
        finally:
            finish(stop, thread)
        emitted.clear()
        stop, thread = start(lambda kind, value, stats: emitted.append((kind, value, stats)))
        try:
            until(lambda: any(kind == "changed" for kind, _v, _s in emitted), label="refused change lost")
            assert [item["id"] for item in next(v for k, v, _s in emitted if k == "changed")[0]] == ["1001"]
        finally:
            finish(stop, thread)
        # Failures are reported once per distinct code; Stop interrupts a held poll at once.
        errors = []
        stop, thread = start(lambda kind, value, stats: errors.append((kind, value)), secret=lambda: "{broken")
        try:
            until(lambda: errors, label="secret error missing")
            time.sleep(1)
            assert [value["code"] for kind, value in errors if kind == "error"] == ["secret"] and len(errors) == 1
            assert errors[0][1]["source"] == "watch" and errors[0][1]["operation"] == "watch"
        finally:
            finish(stop, thread)
        errors.clear()
        stop, thread = start(lambda kind, value, stats: errors.append((kind, value)),
                             directory=lambda: (_ for _ in ()).throw(RuntimeError("block_storage.missing_scope")))
        try:
            until(lambda: errors, label="storage error missing")
            assert errors[0][1]["code"] == "storage" and errors[0][1]["source"] == "watch"
        finally:
            finish(stop, thread)
        errors.clear()
        api.tokens.discard(TOKEN)
        stop, thread = start(lambda kind, value, stats: errors.append((kind, value)))
        try:
            until(lambda: errors, label="auth error missing")
            assert errors[0][1]["code"] == "unauthorized" and TOKEN not in json.dumps(errors)
        finally:
            finish(stop, thread)
            api.tokens.add(TOKEN)
        api.script["search"] = ["hold"]
        stop, thread = start(lambda kind, value, stats: None)
        try:
            until(lambda: api.holding == 1, label="held poll not reached")
        finally:
            finish(stop, thread)
            api.release.set()
        time.sleep(0.2)
        assert not [item for item in threading.enumerate() if item.name == transport.THREAD_NAME]
    print("[ok] HubSpot watcher hand-off, restart without replay, refused hand-off, errors and Stop", flush=True)


def value(data, port):
    raw = data.get("output_values", {}).get("crm:" + str(port), {}).get("value")
    return json.loads(raw) if raw else None


def wallet(server, secret):
    http_json(server.base_url, "/api/application/secrets/init", method="POST", payload={"password": "disposable-hubspot-fixture"})
    http_json(server.base_url, "/api/application/secrets", method="POST",
              payload={"name": "hubspot_fixture", "value": secret})


def graph_runs():
    """# - FB5 - # - FB6 - real graphs in One Shot and Active Runtime for bundled, managed and linked packages, with prompt Stop."""
    block = HubSpotBlock()
    for mode in ("centralized", "zeromq_active"):
        for origin in (None, "managed", "linked"):
            full = origin is None
            with hubspot_api() as api, isolated_server() as server:
                node = block.build_node_payload(node_id="crm", position={"x": 380, "y": 160}, config_overrides=settings(
                    api, allow_writes=True, allowed_operations=["search", "create", "watch"], watch=True,
                    emit_existing=True, existing_since_hours=1, max_search_per_sec=5, timeout_sec=20))
                if origin:
                    model = install_test_package(server, "hubspot", origin=origin)
                    node["block_version"] = model["version"]
                    for surface in ("modal", "inspector_panel", "node_card"):
                        html = surface_payload(server, model, node, surface=surface)["html"]
                        assert "{{" not in html and TOKEN not in html
                        if surface != "node_card":
                            assert 'data-block-config-field="allowed_operations"' in html
                wallet(server, json.dumps({"token": TOKEN}) if full else TOKEN)
                sinks = [display_node("records", "Records", 760, 20), display_node("result", "Result", 760, 160),
                         display_node("changed", "Changed", 760, 300), display_node("problem", "Problem", 760, 440)]
                edges = [data_edge("o1", "crm", 1, "records", 1), data_edge("o2", "crm", 2, "result", 1),
                         data_edge("o3", "crm", 3, "changed", 1), data_edge("o4", "crm", 4, "problem", 1)]

                def graph(text):
                    return graph_payload("HubSpot", [text_node("source", "Source", text, 60, 160), node, *sinks],
                                         [data_edge("in", "source", 1, "crm", 1), *edges])

                if mode == "centralized":
                    def once(document, expect="success", project=None):
                        project = project or create_project_api(server, document=document)["project"]
                        created = create_run_api(server, document, project_id=project["graph_id"], runtime_mode=mode)
                        data = wait_for_run_terminal(server, created["run_id"], timeout_sec=40)
                        assert data["status"] == expect, (origin, data.get("status"), data.get("logs"))
                        assert TOKEN not in json.dumps(data)
                        return data, project

                    data, _ = once(graph(json.dumps({"request_id": "q-1", "max_records": 2})))
                    assert [item["properties"]["firstname"] for item in value(data, 1)] == ["Ada", "Grace"]
                    assert value(data, 2)["request_id"] == "q-1" and value(data, 2)["truncated"] is True
                    data, _ = once(graph(json.dumps({"operation": "create", "record": {"properties": {"firstname": "Graph"}}})))
                    assert value(data, 2)["count"] == 1 and value(data, 1)[0]["properties"]["firstname"] == "Graph"
                    # One Shot never starts the watcher: a watch request polls once per run, without replay across runs.
                    document = graph(json.dumps({"operation": "watch"}))
                    data, project = once(document)
                    assert len(value(data, 3)) == 4 and value(data, 2)["started"] and value(data, 2)["source"] == "request"
                    api.touch("contacts", "1002", {"phone": "between"})
                    data, _ = once(document, project=project)
                    assert [item["id"] for item in value(data, 3)] == ["1002"] and not value(data, 2)["started"]
                    if full:
                        before = len(api.calls)
                        data, _ = once(graph(json.dumps({"operation": "archive", "id": "1001"})))
                        assert value(data, 4)["code"] == "operation_not_allowed" and len(api.calls) == before
                        assert data["results"]["crm"]["hubspot"]["code"] == "operation_not_allowed"
                        http_json(server.base_url, "/api/application/secrets/lock", method="POST", payload={})
                        data, _ = once(graph("go"))
                        assert value(data, 4)["code"] == "secret" and len(api.calls) == before
                        http_json(server.base_url, "/api/application/secrets/unlock", method="POST",
                                  payload={"password": "disposable-hubspot-fixture"})
                        # Stop while HubSpot holds a search: the run ends promptly and nothing is retried.
                        api.script["search"] = ["hold"]
                        document = graph("go")
                        project = create_project_api(server, document=document)["project"]
                        rid = create_run_api(server, document, project_id=project["graph_id"], runtime_mode=mode)["run_id"]
                        until(lambda: api.holding == 1, 20, "held search not reached")
                        held, started = len(api.calls), time.monotonic()
                        stop_run_api(server, rid)
                        stopped = wait_for_run_terminal(server, rid, timeout_sec=20)
                        assert time.monotonic() - started < 10, stopped.get("logs")
                        assert stopped["status"] == "cancelled", (stopped["status"], value(stopped, 4), stopped.get("logs"))
                        assert value(stopped, 1) is None and value(stopped, 2) is None, "a stopped search publishes nothing"
                        assert len(api.calls) == held, "the held search is neither retried nor followed by another request"
                        assert "shutdown_timeout" not in json.dumps(stopped.get("logs"))
                        api.release.set()
                else:
                    document = graph("")
                    project = create_project_api(server, document=document)["project"]

                    def publish(rid, text):
                        http_json(server.base_url, f"/api/runs/{rid}/active/control", method="POST", payload={
                            "action": "publish_output", "node_id": "source", "port_id": 1, "value": text,
                            "content_type": "text/plain"})

                    def wait(rid, predicate, label):
                        data = wait_for_run_predicate(server, rid, lambda item: item.get("status") == "failed" or predicate(item),
                                                      label, timeout_sec=30)
                        assert data.get("status") != "failed", data.get("logs")
                        assert TOKEN not in json.dumps(data)
                        return data

                    def stop(rid):
                        started = time.monotonic()
                        stop_run_api(server, rid)
                        stopped = wait_for_run_terminal(server, rid, timeout_sec=20)
                        assert time.monotonic() - started < 10, stopped.get("logs")
                        assert "shutdown_timeout" not in json.dumps(stopped.get("logs"))
                        return stopped

                    def watched(item):
                        # The framework records one emission port by port (records, result, changed, error):
                        # wait for the changed port as well, not only for the result that precedes it.
                        result = value(item, 2) or {}
                        return (result.get("source") == "watch" and result.get("operation") == "watch"
                                and value(item, 3) is not None)

                    rid = prepare_release_run(server, project["graph_id"], document)["run_id"]
                    http_json(server.base_url, f"/api/runs/{rid}/play", method="POST", payload={})
                    try:
                        data = wait(rid, watched, "watched changes missing")
                        assert [item["id"] for item in value(data, 3)] == ["1001", "1002", "1003"]
                        publish(rid, json.dumps({"request_id": "live-1", "max_records": 3}))
                        data = wait(rid, lambda item: (value(item, 2) or {}).get("request_id") == "live-1", "search missing")
                        assert [item["id"] for item in value(data, 1)] == ["1001", "1002", "1003"]
                        publish(rid, json.dumps({"request_id": "live-2", "operation": "create",
                                                 "records": [{"properties": {"firstname": "Live"}}]}))
                        data = wait(rid, lambda item: (value(item, 2) or {}).get("request_id") == "live-2", "create missing")
                        assert value(data, 2)["count"] == 1 and value(data, 1)[0]["properties"]["firstname"] == "Live"
                        live = value(data, 1)[0]["id"]
                        if full:
                            publish(rid, json.dumps({"request_id": "live-3", "operation": "watch"}))
                            data = wait(rid, lambda item: (value(item, 4) or {}).get("request_id") == "live-3", "busy missing")
                            assert value(data, 4)["code"] == "busy", "The listener owns the cursor while the Run is loaded"
                    finally:
                        stop(rid)
                    if full:
                        stamp = api.touch("contacts", "1003", {"phone": "between runs"})
                        rid = prepare_release_run(server, project["graph_id"], document)["run_id"]
                        http_json(server.base_url, f"/api/runs/{rid}/play", method="POST", payload={})
                        try:
                            data = wait(rid, watched, "restart lost a change")
                            changed = value(data, 3)
                            # Changes made after the last poll (the created record, then the touched one), nothing replayed.
                            assert [item["id"] for item in changed] == [live, "1003"], "A restart never replays"
                            assert changed[0]["change"] == "created" and changed[1]["updated_at"] == iso(stamp)
                            api.script["search"] = ["hold"]
                            publish(rid, json.dumps({"request_id": "live-4"}))
                            until(lambda: api.holding == 1, 20, "held search not reached")
                        finally:
                            stop(rid)
                            api.release.set()
                assert TOKEN not in "\n".join(server.logs)
            print("[ok] HubSpot graph " + mode + " " + str(origin), flush=True)


def main():
    read_protocol()
    write_protocol()
    pacing_protocol()
    error_protocol()
    watch_protocol()
    listener_protocol()
    graph_runs()


if __name__ == "__main__":
    main()
