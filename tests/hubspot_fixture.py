"""Loopback-only HubSpot CRM API double. No HubSpot account or credential is used.

It reproduces the documented request/response shapes used by the block:
CRM objects v3 search (filterGroups with the documented operators and limits,
one sort, query, properties, limit up to 200, `after` paging with `total` and
the 10,000-result window), read by ID or by a unique idProperty with
associations, batch create (201), batch update and upsert (200, `new` flags),
batch archive (204, recycle bin), HTTP 207 multi-status receipts with
`errors`, associations v4 default batches, the properties API, and the error
body {"status": "error", "message", "correlationId", "category"} including
429 with policyName. Records are stored with string property values, and
records hidden from the search index model its eventual consistency.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import itertools
import json
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit

# Built at run time so no fixture literal resembles a provider token format.
TOKEN = "-".join(("hubspot", "fixture", "token", "not", "a", "real", "one", "0123456789"))
EXPIRED = "-".join(("hubspot", "fixture", "token", "revoked", "0123456789"))
REF = "secret://workspace/hubspot_fixture"
CORRELATION = "fixture-correlation-0001"
MODIFIED = {"contacts": "lastmodifieddate", "companies": "hs_lastmodifieddate", "deals": "hs_lastmodifieddate",
            "tickets": "hs_lastmodifieddate", "notes": "hs_lastmodifieddate"}
SCHEMA = {
    "contacts": {"email": "string", "firstname": "string", "lastname": "string", "phone": "string",
                 "lifecyclestage": "enumeration", "hs_lead_status": "enumeration", "age": "number"},
    "companies": {"name": "string", "domain": "string", "industry": "enumeration", "external_id": "string",
                  "numberofemployees": "number"},
    "deals": {"dealname": "string", "amount": "number", "dealstage": "enumeration", "pipeline": "enumeration"},
    "tickets": {"subject": "string", "content": "string", "hs_pipeline_stage": "enumeration"},
    "notes": {"hs_note_body": "string", "hs_timestamp": "datetime"},
}
UNIQUE = {"contacts": {"email"}, "companies": {"external_id"}, "deals": set(), "tickets": set(), "notes": set()}
DEFAULT_PROPERTIES = {"contacts": ["firstname", "lastname", "email"], "companies": ["name", "domain"],
                      "deals": ["dealname", "amount", "dealstage"], "tickets": ["subject", "content"],
                      "notes": ["hs_note_body"]}
ASSOCIATION_TYPES = {("contacts", "companies"): 279, ("companies", "contacts"): 280, ("deals", "contacts"): 3,
                     ("contacts", "deals"): 4, ("deals", "companies"): 341, ("companies", "deals"): 342,
                     ("tickets", "contacts"): 16, ("contacts", "tickets"): 15, ("notes", "contacts"): 202,
                     ("contacts", "notes"): 201}
PEOPLE = [{"email": "ada@example.test", "firstname": "Ada", "lastname": "Lovelace", "lifecyclestage": "lead", "age": 36},
          {"email": "grace@example.test", "firstname": "Grace", "lastname": "Hopper", "lifecyclestage": "customer",
           "age": 85},
          {"email": "linus@example.test", "firstname": "Linus", "lastname": "Torvalds", "lifecyclestage": "lead"}]
COMPANIES = [{"name": "Analytical Engines", "domain": "engines.example.test", "external_id": "AE-1"}]
LIMIT_429 = {"status": "error", "message": "You have reached your ten_secondly_rolling limit.", "errorType": "RATE_LIMIT",
             "correlationId": CORRELATION, "policyName": "TEN_SECONDLY_ROLLING", "groupName": "fixture-group"}
DAILY_429 = {"status": "error", "message": "You have reached your daily limit.", "errorType": "RATE_LIMIT",
             "correlationId": CORRELATION, "policyName": "DAILY", "groupName": "fixture-group"}


def settings(api, **extra):
    """Block settings aimed at this fixture only."""
    return {"enabled": True, "secret_ref": REF, "api_base_url": api.base_url, "ca_file": api.ca_file, "timeout_sec": 20,
            **extra}


def error(category, message, **extra):
    return {"status": "error", "message": message, "correlationId": CORRELATION, "category": category, **extra}


def iso(ms):
    stamp = datetime.fromtimestamp(ms // 1000, tz=timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms % 1000:03d}Z"


def text(value):
    """HubSpot stores and returns property values as strings."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@contextmanager
def hubspot_api(tls=False):
    """Run the double on a dynamic loopback port; every owned thread is joined on exit."""
    with tempfile.TemporaryDirectory() as folder:
        directory = Path(folder)
        lock = threading.Lock()
        ids = itertools.count(1001)
        api = SimpleNamespace(calls=[], script={}, release=threading.Event(), closing=threading.Event(),
                              tokens={TOKEN}, holding=0, denied=set(), hidden=set(), last_ms=[0],
                              objects={kind: {} for kind in SCHEMA}, links=set())

        def now_ms():
            """Strictly increasing modification times, close to the wall clock."""
            with api.clock_lock:
                value = max(int(time.time() * 1000), api.last_ms[0] + 1)
                api.last_ms[0] = value
                return value

        api.clock_lock = threading.Lock()
        api.now_ms = now_ms

        def add(kind, properties, stamp=None):
            ident = str(next(ids))
            moment = stamp if stamp is not None else now_ms()
            api.objects[kind][ident] = {"id": ident, "properties": {key: text(value) for key, value in properties.items()},
                                        "created": moment, "updated": moment, "archived": False}
            return ident

        def touch(kind, ident, properties=None, stamp=None):
            item = api.objects[kind][ident]
            item["properties"].update({key: text(value) for key, value in (properties or {}).items()})
            item["updated"] = stamp if stamp is not None else now_ms()
            return item["updated"]

        api.add, api.touch = add, touch
        for person in PEOPLE:
            add("contacts", person)
        for company in COMPANIES:
            add("companies", company)

        def value_of(kind, item, name):
            if name == MODIFIED[kind] or name == "lastmodifieddate" or name == "hs_lastmodifieddate":
                return iso(item["updated"])
            if name in ("createdate", "hs_createdate"):
                return iso(item["created"])
            if name == "hs_object_id":
                return item["id"]
            return item["properties"].get(name)

        def public(kind, item, names=None, associations=None):
            # Requested properties (null when unset) or the defaults; the system properties always come back.
            chosen = list(dict.fromkeys((names or DEFAULT_PROPERTIES[kind]) + ["createdate", MODIFIED[kind], "hs_object_id"]))
            result = {"id": item["id"], "properties": {name: value_of(kind, item, name) for name in chosen},
                      "createdAt": iso(item["created"]), "updatedAt": iso(item["updated"]), "archived": item["archived"]}
            if associations:
                result["associations"] = {}
                for target in associations:
                    linked = sorted(to_id for (source, from_id, other, to_id) in api.links
                                    if source == kind and from_id == item["id"] and other == target)
                    if linked:
                        result["associations"][target] = {"results": [
                            {"id": to_id, "type": f"{kind[:-1]}_to_{target[:-1]}"} for to_id in linked]}
            return result

        def written(kind, item):
            names = sorted(set(item["properties"]) | {"createdate", MODIFIED[kind], "hs_object_id"})
            return public(kind, item, [name for name in names if value_of(kind, item, name) is not None])

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def reply(self, status, body, *, raw=None, content_type="application/json;charset=utf-8", headers=None):
                data = raw if raw is not None else (b"" if body is None else json.dumps(body).encode("utf-8"))
                try:
                    self.send_response(status)
                    if data:
                        self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Connection", "close")
                    for key, value in (headers or {}).items():
                        self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionError, ssl.SSLError):
                    pass
                finally:
                    self.close_connection = True

            def do_GET(self):
                self.dispatch("GET")

            def do_POST(self):
                self.dispatch("POST")

            def do_PATCH(self):
                self.dispatch("PATCH")

            def do_PUT(self):
                self.dispatch("PUT")

            def do_DELETE(self):
                self.dispatch("DELETE")

            def scripted(self, kind):
                """Apply one scripted action; return True when the reply has been handled."""
                queue = api.script.get(kind)
                if not queue:
                    return False
                action = queue.pop(0)
                if action == "pass":
                    return False
                if action == "hold":
                    with lock:
                        api.holding += 1
                    try:
                        api.release.wait(20)
                    finally:
                        with lock:
                            api.holding -= 1
                    return api.closing.is_set()
                if action == "lost":
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.close_connection = True
                    return True
                if action == "html502":
                    self.reply(502, None, raw=b"<html>Bad Gateway</html>", content_type="text/html")
                    return True
                if action == "oversized":
                    self.reply(200, {"total": 1, "results": [{"id": "1", "properties": {"note": "y" * 70000},
                                                              "createdAt": iso(0), "updatedAt": iso(0)}]})
                    return True
                if action == "gzip":
                    self.reply(200, None, raw=b"\x1f\x8b", headers={"Content-Encoding": "gzip"})
                    return True
                self.reply(action["status"], action["body"], headers=action.get("headers"))
                return True

            def dispatch(self, method):
                length = int(self.headers.get("Content-Length", "0"))
                assert 0 <= length <= 16 * 1024 * 1024
                raw = self.rfile.read(length)
                parts = urlsplit(self.path)
                query = parse_qs(parts.query, keep_blank_values=True)
                call = {"method": method, "path": parts.path, "query": query, "at": time.monotonic(),
                        "auth": self.headers.get("Authorization"), "accept_encoding": self.headers.get("Accept-Encoding"),
                        "content_type": self.headers.get("Content-Type"), "kind": None, "object_type": None, "body": None}
                api.calls.append(call)
                route = self.route(method, parts.path)
                if route is None:
                    self.reply(404, error("OBJECT_NOT_FOUND", "Unable to route " + method + " request: unknown path."))
                    return
                kind, objtype, tail = route
                call.update(kind=kind, object_type=objtype)
                if raw:
                    assert call["content_type"].startswith("application/json"), call["content_type"]
                    call["body"] = json.loads(raw)
                token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                if token not in api.tokens:
                    self.reply(401, error("INVALID_AUTHENTICATION", "Authentication credentials not found. This API "
                                          "supports OAuth 2.0 authentication."))
                    return
                if self.scripted(kind):
                    return
                if objtype not in SCHEMA:
                    self.reply(400, error("VALIDATION_ERROR", f"Unable to infer object type from: {objtype}"))
                    return
                if objtype in api.denied:
                    self.reply(403, error("MISSING_SCOPES", "This app hasn't been granted all required scopes to make "
                                          "this call.", errors=[{"message": "One or more of the following scopes are "
                                                                 "required.", "context": {"requiredGranularScopes":
                                                                 ["crm.objects." + objtype + ".read"]}}]))
                    return
                with lock:
                    getattr(self, "op_" + kind)(objtype, call, tail)

            @staticmethod
            def route(method, path):
                match = re.fullmatch(r"/crm/v3/objects/([a-z_]+)/search", path)
                if match and method == "POST":
                    return "search", match.group(1), None
                match = re.fullmatch(r"/crm/v3/objects/([a-z_]+)/batch/(create|update|upsert|archive)", path)
                if match and method == "POST":
                    return match.group(2), match.group(1), None
                match = re.fullmatch(r"/crm/v3/objects/([a-z_]+)/([^/]+)", path)
                if match and method == "GET":
                    return "get", match.group(1), unquote(match.group(2))
                match = re.fullmatch(r"/crm/v4/associations/([a-z_]+)/([a-z_]+)/batch/associate/default", path)
                if match and method == "POST":
                    return "associate", match.group(1), match.group(2)
                match = re.fullmatch(r"/crm/v3/properties/([a-z_]+)", path)
                if match and method == "GET":
                    return "properties", match.group(1), None
                return None

            def invalid(self, message):
                self.reply(400, error("VALIDATION_ERROR", message))

            # -- reads -----------------------------------------------------------

            def matches(self, objtype, item, condition):
                name, operator = condition.get("propertyName"), condition.get("operator")
                current = value_of(objtype, item, name)
                dated = name in ("createdate", "lastmodifieddate", "hs_lastmodifieddate", "hs_createdate")
                stamp = item["updated"] if name in ("lastmodifieddate", "hs_lastmodifieddate") else item["created"]

                def compare(raw):
                    if dated:
                        return stamp, int(raw)
                    if number(current) is not None and number(raw) is not None:
                        return number(current), number(raw)
                    return current or "", text(raw)

                if operator == "HAS_PROPERTY":
                    return current is not None
                if operator == "NOT_HAS_PROPERTY":
                    return current is None
                if operator in ("IN", "NOT_IN"):
                    found = text(current) in [text(entry) for entry in condition["values"]]
                    return found if operator == "IN" else not found
                if operator in ("CONTAINS_TOKEN", "NOT_CONTAINS_TOKEN"):
                    found = str(condition["value"]).strip("*").lower() in (current or "").lower()
                    return found if operator == "CONTAINS_TOKEN" else not found
                if current is None and not dated:
                    return operator == "NEQ"
                left, right = compare(condition["value"])
                if operator == "EQ":
                    return left == right
                if operator == "NEQ":
                    return left != right
                if operator == "BETWEEN":
                    _left, high = compare(condition["highValue"])
                    return right <= left <= high
                return {"LT": left < right, "LTE": left <= right, "GT": left > right, "GTE": left >= right}[operator]

            def op_search(self, objtype, call, _tail):
                body = call["body"] or {}
                limit = body.get("limit", 10)
                if type(limit) is not int or not 0 < limit <= 200:
                    self.invalid("limit must be between 1 and 200")
                    return
                groups = body.get("filterGroups", [])
                count = sum(len(group.get("filters", [])) for group in groups)
                if len(groups) > 5 or any(len(group.get("filters", [])) > 6 for group in groups) or count > 18:
                    self.invalid("Too many filters: at most 5 filterGroups of 6 filters and 18 filters in total.")
                    return
                if len(body.get("sorts", [])) > 1:
                    self.invalid("Only one sort is supported.")
                    return
                after = int(body.get("after", "0") or 0)
                if after + limit > 10000:
                    self.invalid("Paging beyond 10000 results is not supported.")
                    return
                for name in body.get("properties", []) + [f["propertyName"] for g in groups for f in g["filters"]]:
                    if (name not in SCHEMA[objtype] and name not in ("createdate", MODIFIED[objtype], "hs_object_id")):
                        self.invalid(f"Property {name} does not exist for {objtype}")
                        return
                items = [item for ident, item in api.objects[objtype].items()
                         if not item["archived"] and (objtype, ident) not in api.hidden]
                if groups:
                    items = [item for item in items if any(all(self.matches(objtype, item, condition)
                                                               for condition in group["filters"]) for group in groups)]
                phrase = body.get("query")
                if phrase:
                    items = [item for item in items if any(phrase.lower() in (value or "").lower()
                                                           for value in item["properties"].values())]
                items.sort(key=lambda item: int(item["id"]))
                for rule in body.get("sorts", []):
                    name = rule["propertyName"]
                    if name in ("lastmodifieddate", "hs_lastmodifieddate"):
                        key = lambda item: (item["updated"], int(item["id"]))
                    elif name in ("createdate", "hs_createdate"):
                        key = lambda item: (item["created"], int(item["id"]))
                    else:
                        key = lambda item, name=name: (value_of(objtype, item, name) or "", int(item["id"]))
                    items.sort(key=key, reverse=rule.get("direction") == "DESCENDING")
                chosen = items[after:after + limit]
                reply = {"total": len(items), "results": [public(objtype, item, body.get("properties")) for item in chosen]}
                if after + limit < len(items):
                    reply["paging"] = {"next": {"after": str(after + limit), "link": "?after=" + str(after + limit)}}
                self.reply(200, reply)

            def op_get(self, objtype, call, ident):
                query = call["query"]
                id_property = query.get("idProperty", [""])[0]
                names = [item for item in query.get("properties", [""])[0].split(",") if item]
                targets = [item for item in query.get("associations", [""])[0].split(",") if item]
                if id_property:
                    if id_property not in UNIQUE[objtype]:
                        self.invalid(f"{id_property} is not a unique property")
                        return
                    found = next((item for item in api.objects[objtype].values() if not item["archived"]
                                  and item["properties"].get(id_property) == ident), None)
                else:
                    found = api.objects[objtype].get(ident)
                    found = found if found is not None and not found["archived"] else None
                if found is None:
                    self.reply(404, error("OBJECT_NOT_FOUND", "Object not found.  objectId are usually numeric.",
                                          context={"id": [ident]}))
                    return
                self.reply(200, public(objtype, found, names or None, targets))

            def op_properties(self, objtype, _call, _tail):
                results = []
                for name, kind in SCHEMA[objtype].items():
                    entry = {"name": name, "label": name.replace("_", " ").title(), "type": kind,
                             "fieldType": "number" if kind == "number" else "text", "groupName": objtype[:-1] + "information",
                             "description": "", "options": [], "displayOrder": -1, "calculated": False,
                             "externalOptions": False, "hasUniqueValue": name in UNIQUE[objtype], "hidden": False,
                             "formField": True, "modificationMetadata": {"archivable": True, "readOnlyDefinition": False,
                                                                         "readOnlyValue": False}}
                    if kind == "enumeration":
                        entry["options"] = [{"label": "Lead", "value": "lead", "displayOrder": 0, "hidden": False},
                                            {"label": "Customer", "value": "customer", "displayOrder": 1, "hidden": False}]
                        entry["fieldType"] = "select"
                    results.append(entry)
                results.append({"name": MODIFIED[objtype], "label": "Last Modified Date", "type": "datetime",
                                "fieldType": "date", "groupName": objtype[:-1] + "information", "options": [],
                                "calculated": False, "hasUniqueValue": False, "hidden": False,
                                "modificationMetadata": {"archivable": False, "readOnlyDefinition": True,
                                                         "readOnlyValue": True}})
                self.reply(200, {"results": results})

            # -- writes ----------------------------------------------------------

            def checked(self, objtype, properties):
                """Return HubSpot-checked properties, or reply 400 and return None."""
                bad = [name for name in properties if name not in SCHEMA[objtype]]
                if bad:
                    detail = [{"isValid": False, "message": f'Property "{name}" does not exist', "error": "PROPERTY_DOESNT_EXIST",
                               "name": name} for name in bad]
                    self.invalid("Property values were not valid: " + json.dumps(detail))
                    return None
                for name, value in properties.items():
                    if SCHEMA[objtype][name] == "number" and value not in (None, "") and number(value) is None:
                        self.invalid("Property values were not valid: " + json.dumps(
                            [{"isValid": False, "message": f"{value} was not a valid number.", "error": "INVALID_FLOAT",
                              "name": name}]))
                        return None
                return properties

            def inputs(self, call):
                inputs = (call["body"] or {}).get("inputs")
                if not isinstance(inputs, list) or not inputs:
                    self.invalid("inputs is required")
                    return None
                if len(inputs) > 100:
                    self.invalid(f"Batch input of size {len(inputs)} exceeds the limit of 100.")
                    return None
                return inputs

            def duplicate(self, objtype, properties, ignore=None):
                for name in UNIQUE[objtype]:
                    value = properties.get(name)
                    for ident, item in api.objects[objtype].items():
                        if value is not None and ident != ignore and item["properties"].get(name) == text(value):
                            return ident
                return None

            def multi(self, objtype, results, missing):
                stamp = iso(now_ms())
                body = {"status": "COMPLETE", "results": results, "startedAt": stamp, "completedAt": stamp}
                if missing:
                    body.update(numErrors=1, errors=[{"status": "error", "category": "OBJECT_NOT_FOUND",
                                                      "message": "Could not get some " + objtype.upper()[:-1]
                                                      + " objects, they may be deleted or not exist.",
                                                      "context": {"ids": missing}}])
                self.reply(207 if missing else 200, body)

            def op_create(self, objtype, call, _tail):
                inputs = self.inputs(call)
                if inputs is None:
                    return
                prepared = []
                for entry in inputs:
                    properties = self.checked(objtype, entry.get("properties", {}))
                    if properties is None:
                        return
                    existing = self.duplicate(objtype, properties)
                    if existing:
                        self.reply(409, error("CONFLICT", f"{objtype[:-1].title()} already exists. Existing ID: {existing}"))
                        return
                    prepared.append(properties)
                created = [api.objects[objtype][add(objtype, properties)] for properties in prepared]
                stamp = iso(now_ms())
                self.reply(201, {"status": "COMPLETE", "results": [written(objtype, item) for item in created],
                                 "startedAt": stamp, "completedAt": stamp})

            def locate(self, objtype, entry):
                id_property = entry.get("idProperty")
                if id_property:
                    return next((ident for ident, item in api.objects[objtype].items()
                                 if not item["archived"] and item["properties"].get(id_property) == entry.get("id")), None)
                item = api.objects[objtype].get(str(entry.get("id")))
                return item["id"] if item is not None and not item["archived"] else None

            def op_update(self, objtype, call, _tail):
                inputs = self.inputs(call)
                if inputs is None:
                    return
                plan, missing = [], []
                for entry in inputs:
                    properties = self.checked(objtype, entry.get("properties", {}))
                    if properties is None:
                        return
                    ident = self.locate(objtype, entry)
                    if ident is None:
                        missing.append(str(entry.get("id")))
                    else:
                        plan.append((ident, properties))
                for ident, properties in plan:
                    touch(objtype, ident, properties)
                self.multi(objtype, [written(objtype, api.objects[objtype][ident]) for ident, _ in plan], missing)

            def op_upsert(self, objtype, call, _tail):
                inputs = self.inputs(call)
                if inputs is None:
                    return
                plan = []
                for entry in inputs:
                    id_property = entry.get("idProperty")
                    if id_property not in UNIQUE[objtype]:
                        self.invalid(f"{id_property} is not a unique property and cannot be used as idProperty")
                        return
                    properties = self.checked(objtype, entry.get("properties", {}))
                    if properties is None:
                        return
                    plan.append((self.locate(objtype, entry), {**properties, id_property: entry["id"]}))
                results = []
                for ident, properties in plan:
                    new = ident is None
                    if new:
                        ident = add(objtype, properties)
                    else:
                        touch(objtype, ident, properties)
                    results.append({**written(objtype, api.objects[objtype][ident]), "new": new})
                stamp = iso(now_ms())
                self.reply(200, {"status": "COMPLETE", "results": results, "startedAt": stamp, "completedAt": stamp})

            def op_archive(self, objtype, call, _tail):
                inputs = self.inputs(call)
                if inputs is None:
                    return
                for entry in inputs:
                    item = api.objects[objtype].get(str(entry.get("id")))
                    if item is not None:
                        item["archived"] = True
                        item["updated"] = now_ms()
                self.reply(204, None)

            def op_associate(self, objtype, call, target):
                if target not in SCHEMA:
                    self.invalid(f"Unable to infer object type from: {target}")
                    return
                inputs = self.inputs(call)
                if inputs is None:
                    return
                results, missing = [], []
                forward = ASSOCIATION_TYPES.get((objtype, target), 1)
                backward = ASSOCIATION_TYPES.get((target, objtype), 1)
                for entry in inputs:
                    left, right = str(entry["from"]["id"]), str(entry["to"]["id"])
                    if left not in api.objects[objtype] or right not in api.objects[target]:
                        missing.append(left if left not in api.objects[objtype] else right)
                        continue
                    api.links.update({(objtype, left, target, right), (target, right, objtype, left)})
                    results.append({"from": {"id": left}, "to": {"id": right},
                                    "associationSpec": {"associationCategory": "HUBSPOT_DEFINED",
                                                        "associationTypeId": forward}})
                    results.append({"from": {"id": right}, "to": {"id": left},
                                    "associationSpec": {"associationCategory": "HUBSPOT_DEFINED",
                                                        "associationTypeId": backward}})
                self.multi(target, results, missing)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        ca = ""
        if tls:
            assert shutil.which("openssl"), "OpenSSL is required for the disposable HTTPS fixture"
            certificate, key = directory / "ca.pem", directory / "key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-keyout", str(key), "-out", str(certificate), "-subj", "/CN=localhost",
                            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"], check=True, timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            ca = str(certificate)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        api.base_url = ("https" if tls else "http") + "://127.0.0.1:" + str(server.server_port)
        api.ca_file = ca
        api.kinds = lambda: [call["kind"] for call in api.calls]
        api.find = lambda kind, name, value: next(ident for ident, item in api.objects[kind].items()
                                                  if item["properties"].get(name) == value)
        try:
            yield api
        finally:
            api.closing.set()
            api.release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()
