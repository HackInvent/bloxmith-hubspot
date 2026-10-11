"""FB1/FB2/FB3/FB4/FB5/FB7: pure settings, requests, bodies, batches, provider errors, watch state and block hooks.

No socket is opened and no provider is contacted; the wallet resolver is a local double.
"""

import json
from pathlib import Path
import sys
from types import MappingProxyType, SimpleNamespace
from urllib.parse import parse_qsl

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(Path(__file__).resolve().parent)]
from blocs.hubspot import logic, transport
from blocs.hubspot.block import HubSpotBlock
from bloxsmith_app.block_api import BlockRuntimeContext, BlockRuntimePreparationContext
from hubspot_fixture import REF, TOKEN

BASE_SETTINGS = {"enabled": True, "secret_ref": REF}
WRITER = {**BASE_SETTINGS, "allow_writes": True, "id_property": "email",
          "allowed_operations": ["search", "get", "create", "update", "upsert", "associate"]}
ARCHIVER = {**WRITER, "allow_delete": True, "allowed_operations": [*WRITER["allowed_operations"], "archive"]}
ALL = ["search", "get", "create", "update", "upsert", "associate", "archive", "list_properties", "watch"]


def rejected(call, code=None):
    """A contract violation must raise a coded error, never fall back silently."""
    try:
        call()
    except logic.HubSpotError as exc:
        assert code is None or exc.code == code, (code, exc.code, str(exc))
        assert TOKEN not in str(exc)
        return exc
    raise AssertionError("Invalid value accepted")


def settings_contract():
    """# - FB1 - remote access, writes, archive and watch stay off by default and every setting is bounded."""
    defaults = logic.configuration({})
    assert defaults["enabled"] is False and defaults["allow_writes"] is False and defaults["allow_delete"] is False
    assert defaults["watch"] is False and defaults["emit_existing"] is False
    assert defaults["api_base_url"] == logic.OFFICIAL_API == "https://api.hubapi.com"
    assert defaults["object_type"] == "contacts" and defaults["page_size"] == 100 and defaults["max_search_per_sec"] == 4
    assert defaults["max_requests_per_10s"] == 100 and defaults["watch_interval_sec"] == 300
    assert logic.selectable_operations(defaults) == ["search", "get", "list_properties"]
    assert set(HubSpotBlock().default_config()) == set(logic.DEFAULTS)
    config = logic.configuration({**ARCHIVER, "execution": {"inhibited": False}, "id_property": " email ",
                                  "object_type": "deals", "properties": ["dealname", "amount"]})
    assert config["id_property"] == "email" and config["object_type"] == "deals"
    assert logic.selectable_operations(config) == ARCHIVER["allowed_operations"]
    frozen = logic.configuration(MappingProxyType({**WRITER, "allowed_operations": ("create",),
                                                   "properties": ("email", "firstname")}))
    assert frozen["allowed_operations"] == ["create"] and frozen["properties"] == ["email", "firstname"]
    assert logic.selectable_operations(frozen) == ["search", "create"]
    watcher = logic.configuration({**BASE_SETTINGS, "watch": True, "watch_interval_sec": 60, "operation": "watch",
                                   "allowed_operations": ["watch"]})
    assert logic.selectable_operations(watcher) == ["watch"]
    for raw in ({"enabled": True}, {**BASE_SETTINGS, "secret_ref": ""}, {**BASE_SETTINGS, "secret_ref": TOKEN},
                {**BASE_SETTINGS, "secret_ref": "env://HUBSPOT"}, {**BASE_SETTINGS, "token": TOKEN},
                {**BASE_SETTINGS, "access_token": TOKEN}, {**BASE_SETTINGS, "api_key": TOKEN},
                {**BASE_SETTINGS, "object_type": "products"}, {**BASE_SETTINGS, "object_type": "0-1"},
                {**BASE_SETTINGS, "object_type": ""}, {**BASE_SETTINGS, "operation": "delete"},
                {**BASE_SETTINGS, "operation": "create"}, {**BASE_SETTINGS, "allowed_operations": ["upsert"]},
                {**BASE_SETTINGS, "allowed_operations": []}, {**BASE_SETTINGS, "allowed_operations": ["get", "get"]},
                {**BASE_SETTINGS, "allowed_operations": "search"}, {**WRITER, "allowed_operations": ["archive"]},
                {**BASE_SETTINGS, "allow_delete": True, "allowed_operations": ["archive"]},
                {**BASE_SETTINGS, "enabled": "yes"}, {**BASE_SETTINGS, "allow_writes": 1},
                {**BASE_SETTINGS, "watch": "true"}, {**BASE_SETTINGS, "emit_existing": 0},
                {**BASE_SETTINGS, "properties": "email"}, {**BASE_SETTINGS, "properties": ["email", "email"]},
                {**BASE_SETTINGS, "properties": ["bad name"]}, {**BASE_SETTINGS, "properties": [7]},
                {**BASE_SETTINGS, "properties": ["p" + str(index) for index in range(201)]},
                {**BASE_SETTINGS, "id_property": "e mail"}, {**BASE_SETTINGS, "id_property": 3},
                {**BASE_SETTINGS, "ca_file": "ca.pem"}, {**BASE_SETTINGS, "ca_file": "/etc/../ca.pem"},
                {**BASE_SETTINGS, "api_base_url": "http://api.hubapi.com"},
                {**BASE_SETTINGS, "api_base_url": "https://api.hubapi.com/crm/v3"},
                {**BASE_SETTINGS, "api_base_url": "https://user:pw@api.hubapi.com"},
                {**BASE_SETTINGS, "api_base_url": "https://x.test/?a=1"}, {**BASE_SETTINGS, "api_base_url": "ftp://api.hubapi.com"},
                {**BASE_SETTINGS, "api_base_url": "https://bad host"}, {**BASE_SETTINGS, "api_base_url": "http://example.test:8080"}):
        rejected(lambda raw=raw: logic.configuration(raw), "config")
    for key, (low, high) in logic.BOUNDS.items():
        for bad in (low - 1, high + 1, True, float(low), str(low)):
            rejected(lambda key=key, bad=bad: logic.configuration({**BASE_SETTINGS, key: bad}), "config")
        assert logic.configuration({**BASE_SETTINGS, key: low})[key] == low
        assert logic.configuration({**BASE_SETTINGS, key: high})[key] == high
    assert logic.BOUNDS["watch_interval_sec"][0] == 60, "The watch never polls more often than once a minute"
    assert logic.BOUNDS["page_size"] == (1, 200) and logic.BOUNDS["max_search_per_sec"] == (1, 5)
    assert logic.BOUNDS["max_requests_per_10s"] == (1, 190) and logic.BOUNDS["read_limit"][1] == logic.SEARCH_CAP
    origins = {"https://api.hubapi.com/": "https://api.hubapi.com", "http://127.0.0.1:8081": "http://127.0.0.1:8081",
               "http://localhost:9": "http://localhost:9", "http://[::1]:7": "http://[::1]:7",
               "https://Gateway.Example.test:8443": "https://gateway.example.test:8443"}
    for raw, expected in origins.items():
        assert logic.configuration({**BASE_SETTINGS, "api_base_url": raw})["api_base_url"] == expected


def credential_contract():
    """# - FB1 - # - FB4 - wallet values: bare token, {token} or {access_token}; never echoed."""
    assert logic.credentials(" " + TOKEN + "\n") == TOKEN
    assert logic.credentials({"token": TOKEN}) == TOKEN
    assert logic.credentials(json.dumps({"access_token": TOKEN})) == TOKEN
    for bad in ("", None, 12, {"token": ""}, {"token": "a b"}, {"token": TOKEN, "extra": "x"}, {"api_key": TOKEN},
                '{"token": ', json.dumps({"token": 5}), {"token": TOKEN, "access_token": TOKEN}, "x" * 9000):
        error = rejected(lambda bad=bad: logic.credentials(bad), "secret")
        assert TOKEN not in json.dumps(error.envelope())
    text = logic.redact("Bearer " + TOKEN + " again " + TOKEN, [TOKEN, ""])
    assert TOKEN not in text and text.count("***") == 2


def request_contract():
    """# - FB2 - # - FB3 - # - FB5 - one explicit request; the operation allowlist and write switches apply before the wallet."""
    reader = logic.configuration({**BASE_SETTINGS, "properties": ["email", "firstname"]})
    writer = logic.configuration(WRITER)
    archiver = logic.configuration(ARCHIVER)
    trigger = logic.parse_request("go", reader)
    assert trigger == {"operation": "search", "request_id": None, "object_type": "contacts", "filter_groups": [],
                       "sorts": [], "query": "", "properties": ["email", "firstname"], "max_records": 1000,
                       "page_size": 100, "after": None}
    searched = logic.parse_request({"request_id": "r-1", "object_type": "deals", "query": " acme ", "page_size": 2,
                                    "max_records": 5, "after": "200", "properties": ["dealname"],
                                    "filters": [{"propertyName": "amount", "operator": "GT", "value": 1000},
                                                {"propertyName": "dealstage", "operator": "IN", "values": ["won", "lost"]},
                                                {"propertyName": "closedate", "operator": "BETWEEN", "value": "1",
                                                 "highValue": "2"},
                                                {"propertyName": "hubspot_owner_id", "operator": "HAS_PROPERTY"}],
                                    "sorts": [{"propertyName": "amount", "direction": "DESCENDING"}]}, reader)
    assert searched["object_type"] == "deals" and searched["query"] == "acme" and searched["after"] == "200"
    assert searched["filter_groups"] == [{"filters": [
        {"propertyName": "amount", "operator": "GT", "value": 1000},
        {"propertyName": "dealstage", "operator": "IN", "values": ["won", "lost"]},
        {"propertyName": "closedate", "operator": "BETWEEN", "value": "1", "highValue": "2"},
        {"propertyName": "hubspot_owner_id", "operator": "HAS_PROPERTY"}]}]
    assert searched["sorts"] == [{"propertyName": "amount", "direction": "DESCENDING"}]
    assert (searched["max_records"], searched["page_size"], searched["properties"]) == (5, 2, ["dealname"])
    grouped = logic.parse_request({"filter_groups": [{"filters": [{"propertyName": "email", "operator": "EQ",
                                                                   "value": "a@example.test"}]},
                                                     {"filters": [{"propertyName": "phone", "operator": "EQ",
                                                                   "value": "1"}]}], "after": 100}, reader)
    assert len(grouped["filter_groups"]) == 2 and grouped["after"] == "100"
    assert logic.parse_request({"sorts": [{"propertyName": "createdate"}]}, reader)["sorts"][0]["direction"] == "ASCENDING"
    got = logic.parse_request({"operation": "get", "id": "1001", "associations": ["companies", "deals"]}, reader)
    assert got == {"operation": "get", "request_id": None, "object_type": "contacts", "id_property": None, "id": "1001",
                   "properties": ["email", "firstname"], "associations": ["companies", "deals"]}
    assert logic.parse_request({"operation": "get", "id": 1001}, reader)["id"] == "1001"
    by_email = logic.parse_request({"operation": "get", "id": "ada@example.test", "id_property": "email",
                                    "properties": ["lifecyclestage"]}, reader)
    assert by_email["id_property"] == "email" and by_email["id"] == "ada@example.test" and by_email["properties"] == ["lifecyclestage"]
    as_get = logic.configuration({**BASE_SETTINGS, "operation": "get"})
    assert logic.parse_request(" 1002\n", as_get)["id"] == "1002"
    assert logic.parse_request("1002", reader)["operation"] == "search", "a bare ID only addresses get or archive"
    created = logic.parse_request([{"properties": {"email": "b@example.test", "age": 3, "vip": True, "note": None}}],
                                  logic.configuration({**WRITER, "operation": "create"}))
    assert created["operation"] == "create" and created["records"] == [
        {"properties": {"email": "b@example.test", "age": 3, "vip": True, "note": None}}]
    assert logic.parse_request({"operation": "create", "record": {"properties": {}}}, writer)["records"] == [{"properties": {}}]
    updated = logic.parse_request({"operation": "update", "records": [{"id": "1001", "properties": {"phone": "1"}}]}, writer)
    assert updated["records"] == [{"id": "1001", "properties": {"phone": "1"}}] and updated["id_property"] is None
    keyed = logic.parse_request({"operation": "update", "id_property": "email",
                                 "record": {"id": "ada@example.test", "properties": {"phone": "2"}}}, writer)
    assert keyed["id_property"] == "email" and keyed["records"][0]["id"] == "ada@example.test"
    upsert = logic.parse_request({"operation": "upsert", "record": {"id": "c@example.test", "properties": {"firstname": "C"}}},
                                 writer)
    assert upsert["id_property"] == "email" and upsert["records"] == [{"id": "c@example.test", "properties": {"firstname": "C"}}]
    custom = logic.parse_request({"operation": "upsert", "object_type": "companies", "id_property": "external_id",
                                  "records": [{"id": "AE-1", "properties": {"name": "AE"}}]}, writer)
    assert custom["id_property"] == "external_id" and custom["object_type"] == "companies"
    linked = logic.parse_request({"operation": "associate", "to_object_type": "companies", "from_id": "1001", "to_id": 1004},
                                 writer)
    assert linked["pairs"] == [{"from_id": "1001", "to_id": "1004"}] and linked["to_object_type"] == "companies"
    many = logic.parse_request({"operation": "associate", "object_type": "deals", "to_object_type": "contacts",
                                "pairs": [{"from_id": "1", "to_id": "2"}, {"from_id": "1", "to_id": "3"}]}, writer)
    assert len(many["pairs"]) == 2 and many["object_type"] == "deals"
    assert logic.parse_request({"operation": "archive", "ids": ["1001", "1002"]}, archiver)["ids"] == ["1001", "1002"]
    assert logic.parse_request({"operation": "archive", "id": "1003"}, archiver)["ids"] == ["1003"]
    as_archive = logic.configuration({**ARCHIVER, "operation": "archive"})
    assert logic.parse_request("1003", as_archive)["ids"] == ["1003"]
    assert logic.parse_request({"operation": "list_properties", "object_type": "tickets"}, reader) == {
        "operation": "list_properties", "request_id": None, "object_type": "tickets"}
    watching = logic.configuration({**BASE_SETTINGS, "allowed_operations": ["watch"], "object_type": "deals"})
    assert logic.parse_request({"operation": "watch", "request_id": "w-1"}, watching) == {
        "operation": "watch", "request_id": "w-1", "object_type": "deals"}
    tiny = logic.configuration({**BASE_SETTINGS, "max_request_bytes": 1024})
    small = logic.configuration({**WRITER, "max_write_records": 3})
    no_key = logic.configuration({**WRITER, "id_property": ""})
    eq = {"propertyName": "email", "operator": "EQ", "value": "x"}
    for config, value, code in (
            (reader, {"operation": "create", "record": {"properties": {}}}, "operation_not_allowed"),
            (reader, {"operation": "watch"}, "operation_not_allowed"),
            (reader, {"operation": "drop"}, "invalid_input"), (reader, {"max_records": 1001}, "invalid_input"),
            (reader, {"max_records": 0}, "invalid_input"), (reader, {"page_size": 201}, "invalid_input"),
            (reader, {"page_size": True}, "invalid_input"), (reader, {"properties": []}, "invalid_input"),
            (reader, {"properties": "email"}, "invalid_input"), (reader, {"properties": ["email", "email"]}, "invalid_input"),
            (reader, {"object_type": "products"}, "invalid_input"), (reader, {"object_type": None}, "invalid_input"),
            (reader, {"filters": [eq], "filter_groups": [{"filters": [eq]}]}, "invalid_input"),
            (reader, {"filters": []}, "invalid_input"), (reader, {"filters": [eq] * 7}, "invalid_input"),
            (reader, {"filter_groups": [{"filters": [eq]}] * 6}, "invalid_input"),
            (reader, {"filter_groups": [{"filters": [eq] * 6}] * 3 + [{"filters": [eq]}]}, "invalid_input"),
            (reader, {"filter_groups": [{"filters": [eq], "extra": 1}]}, "invalid_input"),
            (reader, {"filters": [{"propertyName": "email", "operator": "LIKE", "value": "x"}]}, "invalid_input"),
            (reader, {"filters": [{"propertyName": "email", "operator": "EQ"}]}, "invalid_input"),
            (reader, {"filters": [{"propertyName": "email", "operator": "IN", "value": "x"}]}, "invalid_input"),
            (reader, {"filters": [{"propertyName": "email", "operator": "IN", "values": []}]}, "invalid_input"),
            (reader, {"filters": [{"propertyName": "email", "operator": "IN", "values": ["x"] * 101}]}, "invalid_input"),
            (reader, {"filters": [{"propertyName": "email", "operator": "BETWEEN", "value": "1"}]}, "invalid_input"),
            (reader, {"filters": [{"propertyName": "email", "operator": "HAS_PROPERTY", "value": "1"}]}, "invalid_input"),
            (reader, {"filters": [{"propertyName": "email", "operator": "EQ", "value": {"a": 1}}]}, "invalid_input"),
            (reader, {"filters": [{"propertyName": "bad name", "operator": "EQ", "value": "x"}]}, "invalid_input"),
            (reader, {"filters": [{"property": "email", "operator": "EQ", "value": "x"}]}, "invalid_input"),
            (reader, {"sorts": [{"propertyName": "a"}, {"propertyName": "b"}]}, "invalid_input"),
            (reader, {"sorts": [{"propertyName": "a", "direction": "asc"}]}, "invalid_input"),
            (reader, {"sorts": {"propertyName": "a"}}, "invalid_input"),
            (reader, {"query": "x" * 3001}, "invalid_input"), (reader, {"query": "a\x01"}, "invalid_input"),
            (reader, {"after": "has space"}, "invalid_input"), (reader, {"after": -1}, "invalid_input"),
            (reader, {"filterGroups": []}, "invalid_input"), (reader, {"id": "1"}, "invalid_input"),
            (reader, {"operation": "get"}, "invalid_input"), (reader, {"operation": "get", "id": "abc"}, "invalid_input"),
            (reader, {"operation": "get", "id": "0"}, "invalid_input"), (reader, {"operation": "get", "id": True}, "invalid_input"),
            (reader, {"operation": "get", "id": "a@b", "id_property": "e mail"}, "invalid_input"),
            (reader, {"operation": "get", "id": "", "id_property": "email"}, "invalid_input"),
            (reader, {"operation": "get", "id": "1", "associations": ["products"]}, "invalid_input"),
            (reader, {"operation": "get", "id": "1", "associations": ["deals", "deals"]}, "invalid_input"),
            (reader, {"operation": "list_properties", "properties": ["x"]}, "invalid_input"),
            (watching, {"operation": "watch", "object_type": "contacts"}, "invalid_input"),
            (reader, '{"max_records": 1, "max_records": 2}', "invalid_input"), (reader, "{broken", "invalid_input"),
            (reader, "[1]", "invalid_input"), (reader, {"request_id": "bad id"}, "invalid_input"),
            (writer, {"operation": "create"}, "invalid_input"), (writer, {"operation": "create", "records": []}, "invalid_input"),
            (writer, {"operation": "create", "records": [{"email": "flat"}]}, "invalid_input"),
            (writer, {"operation": "create", "records": [{"id": "1", "properties": {}}]}, "invalid_input"),
            (writer, {"operation": "create", "records": [{"properties": []}]}, "invalid_input"),
            (writer, {"operation": "create", "records": [{"properties": {"bad name": 1}}]}, "invalid_input"),
            (writer, {"operation": "create", "records": [{"properties": {"tags": ["a"]}}]}, "invalid_input"),
            (writer, {"operation": "create", "records": [{"properties": {"a": 1}}], "record": {"properties": {}}},
             "invalid_input"),
            (writer, {"operation": "create", "records": [{"properties": {"a": 1}}], "id_property": "email"}, "invalid_input"),
            (small, {"operation": "create", "records": [{"properties": {}}] * 4}, "too_many_records"),
            (writer, {"operation": "update", "records": [{"properties": {"a": 1}}]}, "invalid_input"),
            (writer, {"operation": "update", "records": [{"id": "x1", "properties": {"a": 1}}]}, "invalid_input"),
            (writer, {"operation": "update", "records": [{"id": "1001", "properties": {}}]}, "invalid_input"),
            (writer, {"operation": "update", "records": [{"id": "1001", "properties": {"a": 1}}] * 2}, "invalid_input"),
            (no_key, {"operation": "upsert", "records": [{"id": "a@example.test", "properties": {}}]}, "invalid_input"),
            (writer, {"operation": "upsert", "records": [{"id": "", "properties": {}}]}, "invalid_input"),
            (writer, {"operation": "upsert", "records": [{"properties": {"email": "x"}}]}, "invalid_input"),
            (writer, {"operation": "upsert", "id_property": 7, "records": [{"id": "x", "properties": {}}]}, "invalid_input"),
            (writer, {"operation": "associate", "from_id": "1", "to_id": "2"}, "invalid_input"),
            (writer, {"operation": "associate", "to_object_type": "companies"}, "invalid_input"),
            (writer, {"operation": "associate", "to_object_type": "companies", "pairs": []}, "invalid_input"),
            (writer, {"operation": "associate", "to_object_type": "companies", "pairs": [{"from_id": "1"}]}, "invalid_input"),
            (writer, {"operation": "associate", "to_object_type": "companies", "pairs": [{"from_id": "1", "to_id": "2"}] * 2},
             "invalid_input"),
            (writer, {"operation": "associate", "to_object_type": "companies", "from_id": "1", "to_id": "2",
                      "pairs": [{"from_id": "1", "to_id": "3"}]}, "invalid_input"),
            (small, {"operation": "associate", "to_object_type": "companies",
                     "pairs": [{"from_id": "1", "to_id": str(index)} for index in range(2, 6)]}, "too_many_records"),
            (writer, {"operation": "archive", "id": "1001"}, "operation_not_allowed"),
            (archiver, {"operation": "archive"}, "invalid_input"),
            (archiver, {"operation": "archive", "ids": ["1001", "1001"]}, "invalid_input"),
            (archiver, {"operation": "archive", "ids": ["1001"], "id": "1002"}, "invalid_input"),
            (archiver, {"operation": "archive", "ids": ["a@example.test"]}, "invalid_input"),
            (logic.configuration({**ARCHIVER, "max_write_records": 1}), {"operation": "archive", "ids": ["1", "2"]},
             "too_many_records"),
            (tiny, {"query": "x" * 2000}, "request_too_large"), (tiny, "y" * 2000, "request_too_large")):
        rejected(lambda config=config, value=value: logic.parse_request(value, config), code)
    # A tampered configuration (validation bypassed) still cannot write or archive.
    rejected(lambda: logic.parse_request({"operation": "create", "record": {"properties": {}}},
                                         {**writer, "allow_writes": False}), "writes_disabled")
    rejected(lambda: logic.parse_request({"operation": "archive", "id": "1001"}, {**archiver, "allow_delete": False}),
             "delete_disabled")


def encoding_contract():
    """# - FB2 - # - FB3 - HubSpot paths, query strings and bodies; batches of 100 sized before any write."""
    assert logic.objects_path("contacts", "search") == "/crm/v3/objects/contacts/search"
    assert logic.objects_path("contacts", "a b/c?@") == "/crm/v3/objects/contacts/a%20b%2Fc%3F%40"
    assert logic.properties_path("deals") == "/crm/v3/properties/deals"
    assert logic.association_path("contacts", "companies") == "/crm/v4/associations/contacts/companies/batch/associate/default"
    reader = logic.configuration({**BASE_SETTINGS, "properties": ["email"]})
    request = logic.parse_request({"operation": "get", "id": "ada@example.test", "id_property": "email",
                                   "associations": ["companies"], "properties": ["email", "phone"]}, reader)
    assert parse_qsl(logic.get_query(request)) == [("properties", "email,phone"), ("associations", "companies"),
                                                   ("idProperty", "email"), ("archived", "false")]
    plain = logic.parse_request({"operation": "get", "id": "1"}, logic.configuration(BASE_SETTINGS))
    assert logic.get_query(plain) == "archived=false"
    search = logic.parse_request({"filters": [{"propertyName": "email", "operator": "EQ", "value": "a@example.test"}],
                                  "sorts": [{"propertyName": "createdate", "direction": "DESCENDING"}], "query": "ada"},
                                 reader)
    assert json.loads(logic.search_body(search, 7, "14", 65536)) == {
        "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": "a@example.test"}]}],
        "limit": 7, "sorts": [{"propertyName": "createdate", "direction": "DESCENDING"}], "properties": ["email"],
        "query": "ada", "after": "14"}
    bare = logic.parse_request("go", logic.configuration(BASE_SETTINGS))
    assert json.loads(logic.search_body(bare, 100, None, 65536)) == {"filterGroups": [], "limit": 100}
    rejected(lambda: logic.search_body(search, 7, None, 64), "request_too_large")
    writer = logic.configuration({**ARCHIVER, "allowed_operations": ALL[:-1]})
    records = [{"properties": {"email": f"p{index}@example.test"}} for index in range(230)]
    created = logic.parse_request({"operation": "create", "records": records}, logic.configuration(
        {**writer, "max_write_records": 1000}))
    bodies = logic.write_bodies(created, writer)
    assert [len(json.loads(body)["inputs"]) for body in bodies] == [100, 100, 30]
    assert json.loads(bodies[0])["inputs"][0] == {"properties": {"email": "p0@example.test"}}
    assert logic.write_path(created) == "/crm/v3/objects/contacts/batch/create"
    upserted = logic.parse_request({"operation": "upsert", "records": [{"id": "a@example.test", "properties": {"x": "1"}}]},
                                   writer)
    assert json.loads(logic.write_bodies(upserted, writer)[0]) == {
        "inputs": [{"id": "a@example.test", "properties": {"x": "1"}, "idProperty": "email"}]}
    assert logic.write_path(upserted) == "/crm/v3/objects/contacts/batch/upsert"
    updated = logic.parse_request({"operation": "update", "records": [{"id": "5", "properties": {"x": 1}}]}, writer)
    assert json.loads(logic.write_bodies(updated, writer)[0]) == {"inputs": [{"id": "5", "properties": {"x": 1}}]}
    archived = logic.parse_request({"operation": "archive", "ids": ["5", "6"]}, writer)
    assert json.loads(logic.write_bodies(archived, writer)[0]) == {"inputs": [{"id": "5"}, {"id": "6"}]}
    assert logic.write_path(archived) == "/crm/v3/objects/contacts/batch/archive"
    linked = logic.parse_request({"operation": "associate", "to_object_type": "deals", "from_id": "5", "to_id": "9"}, writer)
    assert json.loads(logic.write_bodies(linked, writer)[0]) == {"inputs": [{"from": {"id": "5"}, "to": {"id": "9"}}]}
    assert logic.write_path(linked) == "/crm/v4/associations/contacts/deals/batch/associate/default"
    # Every batch body is built and sized before the first write: an oversized late batch refuses them all.
    late = [{"properties": {"n": "a"}}] * 100 + [{"properties": {"n": "x" * 3000}}]
    parsed = logic.parse_request({"operation": "create", "records": late}, logic.configuration(
        {**writer, "max_write_records": 1000}))
    assert len(logic.write_bodies(parsed, writer)) == 2
    rejected(lambda: logic.write_bodies(parsed, {**writer, "max_request_bytes": 3000}), "request_too_large")


def provider_contract():
    """# - FB4 - HubSpot error bodies map to stable codes; writes after 5xx are uncertain; 429 policies; nothing leaks."""
    secrets = [TOKEN]
    body = {"status": "error", "message": "Detail with " + TOKEN + "\n" + "x" * 400, "correlationId": "corr-1",
            "category": "SOME_CATEGORY"}
    for status, code in ((400, "bad_request"), (401, "unauthorized"), (403, "forbidden"), (404, "not_found"),
                         (409, "conflict"), (413, "provider_too_large"), (414, "provider_too_large"),
                         (418, "provider_error"), (422, "invalid_request"), (429, "rate_limited"),
                         (500, "provider_unavailable"), (502, "provider_unavailable"), (504, "provider_unavailable")):
        for mutation in (False, True):
            error = rejected(lambda status=status, mutation=mutation: logic.provider_failure(
                status, body, secrets, mutation=mutation, retry_after=7), code)
            envelope = error.envelope(operation="create", request_id=None)
            assert envelope["http_status"] == status and envelope["provider_category"] == "SOME_CATEGORY"
            assert envelope["correlation_id"] == "corr-1"
            assert len(envelope["provider_message"]) <= 200 and "\n" not in envelope["provider_message"]
            assert TOKEN not in json.dumps(envelope) and "request_id" not in envelope
            assert error.uncertain is (mutation and status >= 500)
            assert envelope.get("retry_after") == (7 if status == 429 else None)
    ten = rejected(lambda: logic.provider_failure(429, {**body, "policyName": "TEN_SECONDLY_ROLLING",
                                                        "errorType": "RATE_LIMIT"}, secrets, mutation=False), "rate_limited")
    assert ten.retry_after == 10, "Without Retry-After the ten-second window is awaited"
    assert rejected(lambda: logic.provider_failure(429, {"policyName": "SECONDLY"}, secrets, mutation=False),
                    "rate_limited").retry_after == 1
    assert rejected(lambda: logic.provider_failure(429, None, secrets, mutation=False), "rate_limited").retry_after == 2
    daily = rejected(lambda: logic.provider_failure(429, {"status": "error", "errorType": "RATE_LIMIT", "policyName": "DAILY",
                                                          "message": "daily"}, secrets, mutation=True), "daily_limit")
    assert daily.retry_after is None and daily.provider_category == "RATE_LIMIT" and not daily.uncertain
    bare = rejected(lambda: logic.provider_failure(502, None, secrets, mutation=False), "provider_unavailable")
    assert bare.provider_category is None and bare.provider_message is None
    raw = {"id": "1001", "properties": {"email": "a@example.test", "age": "3", "phone": None},
           "createdAt": "2026-01-02T10:00:00.000Z", "updatedAt": "2026-01-03T10:00:00.250Z", "archived": False,
           "associations": {"companies": {"results": [{"id": "1004", "type": "contact_to_company"},
                                                      {"id": 1004, "type": "contact_to_company_unlabeled"}]}}}
    assert logic.record(raw, "contacts") == {"id": "1001", "object_type": "contacts", "properties": raw["properties"],
                                             "created_at": "2026-01-02T10:00:00.000Z",
                                             "updated_at": "2026-01-03T10:00:00.250Z", "archived": False,
                                             "associations": {"companies": ["1004"]}}
    assert logic.record({"id": 7}, "deals")["id"] == "7" and logic.record({"id": "7"}, "deals")["properties"] == {}
    for bad in ({"id": "x1", "properties": {}}, {"properties": {}}, {"id": "1", "properties": []},
                {"id": "1", "properties": {"a": {"b": 1}}}, "x", {"id": True}):
        assert not rejected(lambda bad=bad: logic.record(bad, "contacts"), "protocol").uncertain
        assert rejected(lambda bad=bad: logic.record(bad, "contacts", mutation=True), "protocol").uncertain
    rejected(lambda: logic.record({**raw, "associations": {"companies": {"results": [{"id": "x"}]}}}, "contacts"), "protocol")
    items, after, total = logic.search_page({"total": 9, "results": [raw], "paging": {"next": {"after": "1"}}}, 1, "contacts")
    assert items[0]["id"] == "1001" and after == "1" and total == 9
    assert logic.search_page({"results": []}, 5, "contacts") == ([], None, None)
    assert logic.search_page({"results": [], "paging": {"next": {"after": 200}}}, 5, "contacts")[1] == "200"
    for bad in ({"results": [raw, raw]}, {"results": "x"}, {}, {"results": [], "total": -1},
                {"results": [], "paging": {"next": {"after": "a b"}}}, {"results": [], "paging": {"next": {}}},
                {"results": [], "paging": "x"}):
        rejected(lambda bad=bad: logic.search_page(bad, 1, "contacts"), "protocol")
    assert logic.capped("10000") and not logic.capped("9999") and not logic.capped(None) and not logic.capped("tok")
    receipt = {"status": "COMPLETE", "results": [{**raw, "new": True}, {"id": "1002", "properties": {}, "new": False}]}
    records, created = logic.batch_records(receipt, 2, "contacts", partial=False)
    assert [item["id"] for item in records] == ["1001", "1002"] and created == ["1001"]
    assert "new" not in records[0]
    for bad, size in (({"results": []}, 1), ({"results": [raw, raw]}, 1), ({}, 1), ({"results": [{"id": "x"}]}, 1)):
        assert rejected(lambda bad=bad, size=size: logic.batch_records(bad, size, "contacts", partial=False),
                        "protocol").uncertain
    assert logic.batch_records({"results": [raw]}, 2, "contacts", partial=True)[0][0]["id"] == "1001"
    pairs = [{"from_id": "1", "to_id": "2"}, {"from_id": "1", "to_id": "3"}]
    association = {"results": [{"from": {"id": "2"}, "to": {"id": "1"}, "associationSpec": {"associationTypeId": 280}},
                               {"from": {"id": "1"}, "to": {"id": "3"}, "associationSpec": {"associationTypeId": 279}}]}
    assert logic.associated(association, pairs, partial=False) == (pairs, [279, 280])
    assert rejected(lambda: logic.associated({"results": association["results"][:1]}, pairs, partial=False),
                    "protocol").uncertain
    assert logic.associated({"results": association["results"][:1]}, pairs, partial=True)[0] == pairs[:1]
    assert rejected(lambda: logic.associated({"results": [{"from": {"id": "x"}, "to": {"id": "1"}}]}, pairs, partial=True),
                    "protocol").uncertain
    errors = logic.item_errors({"errors": [{"category": "OBJECT_NOT_FOUND", "message": "gone " + TOKEN,
                                            "context": {"ids": ["9", 10, {"x": 1}]}}] * 30}, secrets)
    assert len(errors) == 20 and errors[0] == {"category": "OBJECT_NOT_FOUND", "message": "gone ***", "ids": ["9", "10"]}
    assert logic.item_errors({}, secrets) == []
    definition = logic.property_definition({"name": "email", "label": "Email", "type": "string", "fieldType": "text",
                                            "groupName": "contactinformation", "hasUniqueValue": True,
                                            "modificationMetadata": {"readOnlyValue": False},
                                            "options": [{"label": "A", "value": "a"}, {"label": "bad"}]})
    assert definition == {"name": "email", "label": "Email", "type": "string", "field_type": "text",
                          "group_name": "contactinformation", "description": None, "unique": True, "read_only": False,
                          "calculated": False, "hidden": False, "options": [{"label": "A", "value": "a"}]}
    rejected(lambda: logic.property_definition({"label": "x"}), "protocol")
    # Pacing: at most `rate` slots in any window per account key, shared by the process.
    key = ("general", "https://pace.test", "digest-a")
    assert [transport.pace_slot(key, 2, 10.0, now=100.0), transport.pace_slot(key, 2, 10.0, now=101.0)] == [0.0, 0.0]
    assert abs(transport.pace_slot(key, 2, 10.0, now=105.0) - 5.0) < 1e-9
    assert transport.pace_slot(key, 2, 10.0, now=110.01) == 0.0
    assert transport.pace_slot(("general", "https://pace.test", "digest-b"), 2, 10.0, now=110.02) == 0.0
    search_key = ("search", "https://pace.test", "digest-a")
    assert transport.pace_slot(search_key, 1, 1.0, now=200.0) == 0.0
    assert abs(transport.pace_slot(search_key, 1, 1.0, now=200.4) - 0.6) < 1e-9


def watch_state_contract():
    """# - FB5 - the persisted watch document holds cursors and digests only, and is validated before use."""
    assert logic.epoch_ms("2026-01-03T10:00:00.250Z") == 1767434400250
    assert logic.epoch_ms("2026-01-03T10:00:00Z") == 1767434400000
    assert logic.epoch_ms("2026-01-03T11:00:00.1+01:00") == 1767434400100
    for bad in (None, 5, "2026-01-03", "2026-01-03T10:00:00", "2026-13-03T10:00:00Z"):
        assert logic.epoch_ms(bad) is None
    assert logic.iso_ms(1767434400250) == "2026-01-03T10:00:00.250Z"
    config = logic.configuration({**BASE_SETTINGS, "object_type": "deals", "properties": ["dealname"]})
    key = logic.watch_key(config)
    assert len(key) == 24 and REF not in key and key != logic.watch_key({**config, "object_type": "tickets"})
    assert key != logic.watch_key({**config, "secret_ref": "secret://workspace/other"})
    assert logic.watch_properties(config) == ["dealname", "hs_lastmodifieddate"]
    assert logic.watch_properties(logic.configuration(BASE_SETTINGS)) == []
    assert logic.MODIFIED["contacts"] == "lastmodifieddate" and logic.MODIFIED["notes"] == "hs_lastmodifieddate"
    body = json.loads(logic.watch_body(config, 1767434400250, 50, "100", ["dealname", "hs_lastmodifieddate"]))
    assert body == {"filterGroups": [{"filters": [{"propertyName": "hs_lastmodifieddate", "operator": "GTE",
                                                   "value": "1767434400250"}]}],
                    "sorts": [{"propertyName": "hs_lastmodifieddate", "direction": "ASCENDING"}], "limit": 50,
                    "properties": ["dealname", "hs_lastmodifieddate"], "after": "100"}
    assert json.loads(logic.position_body(logic.configuration(BASE_SETTINGS))) == {
        "filterGroups": [], "sorts": [{"propertyName": "lastmodifieddate", "direction": "DESCENDING"}],
        "properties": ["lastmodifieddate"], "limit": 1}
    entry = {"floor": 10, "cursor": 20, "resume": {"lower": 11, "after": "200"},
             "seen": [[logic.seen_digest("1001", 20), 20]], "updated_at": 5}
    document = logic.with_watch(logic.empty_state(), key, entry)
    assert logic.state_document(json.loads(json.dumps(document))) == document
    for broken in ({"schema_version": 2, "watches": {}}, {"schema_version": 1}, {"schema_version": 1, "watches": [],},
                   {"schema_version": 1, "watches": {"x": entry}}, {"schema_version": 1, "watches": {key: {**entry, "extra": 1}}},
                   {"schema_version": 1, "watches": {key: {**entry, "cursor": 5}}},
                   {"schema_version": 1, "watches": {key: {**entry, "floor": -1}}},
                   {"schema_version": 1, "watches": {key: {**entry, "resume": {"lower": 1}}}},
                   {"schema_version": 1, "watches": {key: {**entry, "resume": {"lower": 1, "after": "a b"}}}},
                   {"schema_version": 1, "watches": {key: {**entry, "seen": [["zz", 1]]}}},
                   {"schema_version": 1, "watches": {key: {**entry, "seen": [[logic.seen_digest("1", 1), "1"]]}}},
                   {"schema_version": 1, "watches": {key: {**entry, "seen": [[logic.seen_digest("1", 1), 1]] * 4097}}},
                   {"schema_version": 1, "watches": {key: {**entry, "updated_at": "now"}}},
                   {"schema_version": 1, "watches": {format(index, "024x"): entry for index in range(9)}}):
        rejected(lambda broken=broken: logic.state_document(broken), "state_invalid")
    crowded = logic.empty_state()
    for index in range(10):
        crowded = logic.with_watch(crowded, format(index, "024x"), {**entry, "updated_at": index})
    assert len(crowded["watches"]) == 8 and format(9, "024x") in crowded["watches"] and format(0, "024x") not in crowded["watches"]


def block_contract():
    """# - FB1 - # - FB4 - # - FB5 - # - FB7 - block hooks fail before side effects, and surfaces stay bounded and translated."""
    block = HubSpotBlock()
    ports_in = tuple(SimpleNamespace(id=port["id"], name=port["name"]) for port in block.model["ports"]["inputs"])
    ports_out = tuple(SimpleNamespace(id=port["id"], name=port["name"]) for port in block.model["ports"]["outputs"])
    assert [port.name for port in ports_in] == ["request"]
    assert [port.name for port in ports_out] == ["records", "result", "changed", "error"]
    assert block.model["ports"]["inputs"][0]["execution_requirement"] == "not_required_for_execution"
    assert block.model["version"] == "0.1.0" and block.model["config"] == logic.DEFAULTS
    calls = []

    def resolver(ref):
        calls.append(ref)
        return "not json {" + TOKEN

    def run(config, inputs, services=None):
        context = BlockRuntimeContext(config={**block.default_config(), **config}, inputs=inputs, input_ports=ports_in,
                                      output_ports=ports_out, services={"resolve_secret": resolver, **(services or {})})
        result = block.execute_runtime(context)
        assert result.status in ("success", "skipped"), result
        return result, {output.port_name: json.loads(output.value) for output in result.outputs}

    assert run(BASE_SETTINGS, {})[0].status == "skipped"
    for config, inputs, code in (({**BASE_SETTINGS, "enabled": False}, {"request": "go"}, "disabled"),
                                 (BASE_SETTINGS, {"request": {"operation": "create", "record": {"properties": {}}}},
                                  "operation_not_allowed"),
                                 (BASE_SETTINGS, {"request": '{"max_records":'}, "invalid_input"),
                                 (WRITER, {"request": {"operation": "create", "records": [{"properties": {}}] * 101}},
                                  "too_many_records"),
                                 ({**BASE_SETTINGS, "allowed_operations": ["watch"]}, {"request": {"operation": "watch"}},
                                  "storage"),
                                 ({**BASE_SETTINGS, "secret_ref": TOKEN}, {"request": "go"}, "config")):
        result, outputs = run(config, inputs)
        assert set(outputs) == {"error"} and outputs["error"]["code"] == code, outputs
        assert result.metadata["hubspot"] == {"status": "error", "code": code, "uncertain": False}
        assert TOKEN not in json.dumps(outputs) and TOKEN not in " ".join(result.logs)
    assert not calls, "The wallet must not be read before settings, request and storage validation"
    result, outputs = run(BASE_SETTINGS, {"request": {"request_id": "req-1", "max_records": 3, "object_type": "deals"}})
    assert outputs["error"]["code"] == "secret" and outputs["error"]["request_id"] == "req-1" and calls == [REF]
    assert outputs["error"]["object_type"] == "deals" and outputs["error"]["operation"] == "search"
    assert TOKEN not in json.dumps(outputs) and result.logs == ["[hubspot-error] search: secret"]

    def prepared(config, mode="zeromq_active"):
        context = BlockRuntimePreparationContext.from_context(SimpleNamespace(config=config, runtime_mode=mode))
        return block.prepare_runtime(context)

    assert prepared(WRITER).listen_on_run is False
    assert prepared({**BASE_SETTINGS, "watch": True}).listen_on_run is True
    assert prepared({**BASE_SETTINGS, "watch": True}, "centralized").listen_on_run is False, "One Shot never listens"
    assert prepared({**BASE_SETTINGS, "enabled": False, "watch": True}).listen_on_run is False
    rejected(lambda: prepared({**BASE_SETTINGS, "secret_ref": TOKEN}), "config")
    rejected(lambda: prepared({**BASE_SETTINGS, "watch": True, "watch_interval_sec": 30}), "config")
    node = block.build_node_payload(node_id="crm", position={"x": 0, "y": 0})
    accepted = block.handle_ui_action(node=node, action="save_settings", values={"node_patch": {"config": WRITER}})
    operation = accepted["graph_operations"][-1]
    assert operation["op"] == "update_node_config" and operation["replace"] is True
    assert operation["config"]["allowed_operations"] == WRITER["allowed_operations"]
    assert operation["config"]["id_property"] == "email"
    refused = block.handle_ui_action(node=node, action="save_settings",
                                     values={"node_patch": {"config": {**BASE_SETTINGS, "operation": "create"}}})
    assert "allow_writes" in refused["error"] and "graph_operations" not in refused
    html = block.render_modal(node=node)["html"]
    assert 'data-block-config-field="secret_ref"' in html and 'data-hubspot-show="write" hidden' in html
    assert 'data-hubspot-show="watch" hidden' in html and 'data-block-value-type="json"' in html and "{{" not in html
    assert 'list="hubspot-crm-modal-id_property-names"' in html and '<option value="email"></option>' in html
    assert "data-hubspot-property-names=" in html and "dealname" in html, "the catalogue of every object travels along"
    writing = {**node, "config": {**node["config"], **WRITER, "watch": True, "object_type": "deals"}}
    panel = block.render_inspector_panel(node=writing)["html"]
    assert 'data-hubspot-show="write" hidden' not in panel and 'data-hubspot-show="watch" hidden' not in panel
    assert '<datalist id="hubspot-crm-inspector-id_property-names"' in panel
    options = panel.split('data-hubspot-property-names="', 1)[1].split('">', 1)[1].split("</datalist>", 1)[0]
    assert '<option value="dealname"></option>' in options and '<option value="email">' not in options
    card = block.render_node_card(node=node)["html"]
    assert card.count("Remote access off") == 1 and 'data-i18n="block.hubspot.card_disabled" data-i18n-params=""' in card
    reading = {**node, "config": {**node["config"], **BASE_SETTINGS}}
    assert "Read only · contacts · search" in block.render_node_card(node=reading)["html"]
    card = block.render_node_card(node={**node, "config": {**node["config"], **WRITER}})["html"]
    assert "Read and write · contacts · search" in card and 'data-i18n="block.hubspot.card_write"' in card
    archiving = {**node, "config": {**node["config"], **ARCHIVER, "operation": "archive", "object_type": "tickets"}}
    assert "Read, write and archive · tickets · archive" in block.render_node_card(node=archiving)["html"]
    assert "Watching deals · search" in block.render_node_card(node=writing)["html"]
    assert REF not in card
    french = block.translate("block.hubspot.card_write", {"operation": "upsert", "object_type": "contacts"}, lang="fr")
    assert french == "Lecture et écriture · contacts · upsert"
    assert block.translate("block.hubspot.card_watch", {"operation": "watch", "object_type": "deals"}, lang="fr") == \
        "Surveillance de deals · watch"


def main():
    settings_contract()
    credential_contract()
    request_contract()
    encoding_contract()
    provider_contract()
    watch_state_contract()
    block_contract()
    print("[ok] HubSpot settings, credentials, requests, bodies, batches, provider errors, watch state, pacing and hooks")


if __name__ == "__main__":
    main()
