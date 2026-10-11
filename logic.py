"""Pure HubSpot CRM API contracts: settings, requests, batches, watch state, responses and errors.

The block uses the CRM objects API v3 (search, read, batch create/update/upsert/
archive, properties) and the associations API v4 (default association batches).
No network, filesystem, framework or third-party import happens here. Every
function is deterministic, so the policy can be checked directly by the tests.
"""

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import math
import re
from urllib.parse import quote, urlencode, urlsplit

OFFICIAL_API = "https://api.hubapi.com"
OBJECT_TYPES = ("contacts", "companies", "deals", "tickets", "notes")
OPERATIONS = ("search", "get", "create", "update", "upsert", "associate", "archive", "list_properties", "watch")
WRITES = frozenset(("create", "update", "upsert", "associate", "archive"))
READS = ("search", "get", "list_properties", "watch")
BATCH_SIZE = 100  # HubSpot accepts at most 100 inputs per CRM batch request.
SEARCH_LIMIT = 200  # HubSpot returns at most 200 records per search page.
SEARCH_CAP = 10000  # HubSpot search paging stops at 10,000 results per query.
MAX_FILTER_GROUPS = 5
MAX_FILTERS_PER_GROUP = 6
MAX_FILTERS = 18
MAX_IN_VALUES = 100
MAX_PROPERTIES = 200
MAX_ASSOCIATIONS = 5
QUERY_LIMIT = 3000
OUTPUT_LIMIT = 16777216
LOOKBACK_MS = 120000  # The search index is eventually consistent: each watch poll re-reads two minutes.
SEEN_LIMIT = 4096
MAX_WATCHES = 8
# Contacts keep their modification time in lastmodifieddate; the other objects in hs_lastmodifieddate.
MODIFIED = {"contacts": "lastmodifieddate", "companies": "hs_lastmodifieddate", "deals": "hs_lastmodifieddate",
            "tickets": "hs_lastmodifieddate", "notes": "hs_lastmodifieddate"}
# Without Retry-After, a 429 is retried after the window of the limit HubSpot names.
RATE_WAIT = {"TEN_SECONDLY_ROLLING": 10, "SECONDLY": 1}
RATE_WAIT_DEFAULT = 2
OPERATORS = ("EQ", "NEQ", "LT", "LTE", "GT", "GTE", "BETWEEN", "IN", "NOT_IN", "HAS_PROPERTY", "NOT_HAS_PROPERTY",
             "CONTAINS_TOKEN", "NOT_CONTAINS_TOKEN")
# Standard HubSpot property names offered as suggestions in the settings; custom ones come from list_properties.
SUGGESTED_PROPERTIES = {
    "contacts": ("email", "firstname", "lastname", "phone", "company", "website", "jobtitle", "lifecyclestage",
                 "hs_lead_status", "hubspot_owner_id", "hs_object_id", "createdate", "lastmodifieddate"),
    "companies": ("name", "domain", "industry", "phone", "city", "country", "numberofemployees", "annualrevenue",
                  "lifecyclestage", "hubspot_owner_id", "hs_object_id", "createdate", "hs_lastmodifieddate"),
    "deals": ("dealname", "amount", "dealstage", "pipeline", "closedate", "hubspot_owner_id", "hs_object_id",
              "createdate", "hs_lastmodifieddate"),
    "tickets": ("subject", "content", "hs_pipeline", "hs_pipeline_stage", "hs_ticket_priority", "hubspot_owner_id",
                "hs_object_id", "createdate", "hs_lastmodifieddate"),
    "notes": ("hs_note_body", "hs_timestamp", "hubspot_owner_id", "hs_object_id", "hs_lastmodifieddate"),
}
DEFAULTS = {
    "enabled": False, "secret_ref": "", "object_type": "contacts", "operation": "search",
    "allowed_operations": ["search", "get", "list_properties"], "properties": [], "allow_writes": False,
    "allow_delete": False, "id_property": "", "max_write_records": 100, "read_limit": 1000, "page_size": 100,
    "max_pages": 20, "watch": False, "watch_interval_sec": 300, "emit_existing": False, "existing_since_hours": 24,
    "watch_max_items": 100, "timeout_sec": 60, "max_requests_per_10s": 100, "max_search_per_sec": 4,
    "max_retries": 2, "max_retry_wait_sec": 15, "max_request_bytes": 1048576, "max_response_bytes": 8388608,
    "api_base_url": OFFICIAL_API, "ca_file": "",
}
FRAMEWORK_KEYS = {"execution", "runtime_path", "runtime_path_label"}
BOUNDS = {
    "max_write_records": (1, 1000), "read_limit": (1, 10000), "page_size": (1, SEARCH_LIMIT), "max_pages": (1, 100),
    "watch_interval_sec": (60, 86400), "existing_since_hours": (1, 720), "watch_max_items": (1, 1000),
    "timeout_sec": (5, 300), "max_requests_per_10s": (1, 190), "max_search_per_sec": (1, 5), "max_retries": (0, 5),
    "max_retry_wait_sec": (1, 120), "max_request_bytes": (1024, 8388608), "max_response_bytes": (65536, 33554432),
}
BOOLEANS = ("enabled", "allow_writes", "allow_delete", "watch", "emit_existing")
SECRET_REF = re.compile(r"secret://(?:workspace/[A-Za-z0-9_.-]{1,80}|project/[A-Za-z0-9_.-]{1,80}/[A-Za-z0-9_.-]{1,80})\Z")
OBJECT_ID = re.compile(r"[0-9]{1,20}\Z")
PROPERTY = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
TOKEN_TEXT = re.compile(r"[\x21-\x7e]{1,8192}\Z")
AFTER_TEXT = re.compile(r"[A-Za-z0-9_=-]{1,256}\Z")
HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
TIMESTAMP = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})\Z")
VALUE_LIMIT = 65536
UNIQUE_LIMIT = 255
REQUEST_KEYS = {
    "search": {"operation", "request_id", "object_type", "filters", "filter_groups", "sorts", "query", "properties",
               "max_records", "page_size", "after"},
    "get": {"operation", "request_id", "object_type", "id", "id_property", "properties", "associations"},
    "create": {"operation", "request_id", "object_type", "records", "record"},
    "update": {"operation", "request_id", "object_type", "records", "record", "id_property"},
    "upsert": {"operation", "request_id", "object_type", "records", "record", "id_property"},
    "associate": {"operation", "request_id", "object_type", "to_object_type", "pairs", "from_id", "to_id"},
    "archive": {"operation", "request_id", "object_type", "ids", "id"},
    "list_properties": {"operation", "request_id", "object_type"},
    "watch": {"operation", "request_id"},
}

MESSAGES = {
    "config": "Invalid HubSpot settings.",
    "disabled": "Remote access is disabled; enable the block explicitly before contacting HubSpot.",
    "secret": "Unlock the wallet and store a HubSpot private app access token, or JSON with token, under secret_ref.",
    "invalid_input": "The request does not follow the documented contract; nothing was sent.",
    "operation_not_allowed": "This operation is not listed in allowed_operations; nothing was sent.",
    "writes_disabled": "Writes are disabled; enable allow_writes in the properties; nothing was written.",
    "delete_disabled": "archive needs allow_delete in the properties; nothing was archived.",
    "too_many_records": "The request has more records than max_write_records; nothing was written.",
    "request_too_large": "The request exceeds max_request_bytes; nothing was sent.",
    "output_too_large": "One page of records exceeds the 16 MiB output limit; lower page_size or ask for fewer properties.",
    "storage": "Block storage is unavailable; the watch needs a project-scoped run.",
    "busy": "Another watch of this node holds the watch state; nothing was polled.",
    "state_invalid": "The stored watch state is unreadable; remove it from the node storage to start again.",
    "bad_request": "HubSpot rejected the request (HTTP 400), for example an invalid property value; nothing was changed.",
    "unauthorized": "HubSpot rejected the access token (HTTP 401); check or replace the wallet secret.",
    "forbidden": "The private app lacks a required scope for this object or operation (HTTP 403).",
    "not_found": "The object or record was not found (HTTP 404); check object_type, IDs and id_property.",
    "conflict": "HubSpot reported a conflict (HTTP 409), for example a record that already exists; nothing was changed.",
    "provider_too_large": "HubSpot refused the request as too large (HTTP 413/414); nothing was changed.",
    "invalid_request": "HubSpot could not process the request (HTTP 422); nothing was changed.",
    "rate_limited": "HubSpot rate limit reached (HTTP 429); retry after the indicated delay.",
    "daily_limit": "The HubSpot daily API limit of this account is exhausted (HTTP 429); retry after midnight in the account time zone.",
    "provider_error": "HubSpot returned an unexpected error status; nothing is assumed changed.",
    "provider_unavailable": "HubSpot is temporarily unavailable (HTTP 5xx); nothing was written.",
    "provider_unavailable_uncertain": "HubSpot answered HTTP 5xx after the write was sent; the change is uncertain.",
    "network": "The connection to HubSpot failed before the request was complete; nothing was changed.",
    "network_uncertain": "The connection failed after the write was sent; the change is uncertain.",
    "tls": "TLS verification failed for the HubSpot API origin; nothing was sent.",
    "timeout": "The operation exceeded timeout_sec before any write was complete; nothing was changed.",
    "timeout_uncertain": "The operation exceeded timeout_sec after the write was sent; the change is uncertain.",
    "cancelled": "The operation was cancelled after the write was sent; the change is uncertain.",
    "cancelled_before_send": "The operation was cancelled before the next request was sent.",
    "protocol": "HubSpot returned an invalid, compressed or oversized response.",
    "protocol_uncertain": "HubSpot accepted the write but returned an unexpected receipt; the change is likely.",
    "partial_failure": "HubSpot applied only part of a batch (HTTP 207); item_errors lists the refused inputs.",
    "partial": "Earlier batches were already written; committed_ids lists them.",
    "internal": "The HubSpot operation failed without a complete result.",
}


class HubSpotError(ValueError):
    """A content-free diagnostic with a stable machine code and an English message."""

    def __init__(self, code, detail=None, *, uncertain=False, http_status=None, retry_after=None,
                 provider_category=None, provider_message=None, correlation_id=None, retryable=False):
        super().__init__(detail or MESSAGES.get(code, MESSAGES["internal"]))
        self.code, self.uncertain, self.retryable = code, bool(uncertain), bool(retryable)
        self.http_status, self.retry_after = http_status, retry_after
        self.provider_category, self.provider_message = provider_category, provider_message
        self.correlation_id = correlation_id
        self.attempts = 0
        self.progress = None

    def envelope(self, **context):
        """Return the public error document; callers add identifiers only, never property values or tokens."""
        result = {"code": self.code, "message": str(self), "uncertain": self.uncertain}
        for key, value in (("http_status", self.http_status), ("retry_after", self.retry_after),
                           ("provider_category", self.provider_category), ("provider_message", self.provider_message),
                           ("correlation_id", self.correlation_id)):
            if value is not None:
                result[key] = value
        if self.attempts:
            result["attempts"] = self.attempts
        if self.progress:
            result.update(self.progress)
        for key, value in context.items():
            if value is not None:
                result[key] = value
        return result


def fail(code, detail=None, **extra):
    """Raise a structured failure."""
    raise HubSpotError(code, detail, **extra)


def thaw(value):
    """Turn immutable preparation snapshots back into plain JSON containers."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(item) for item in value]
    return value


def encoded(value, limit, code="invalid_input"):
    """Serialize strict finite JSON with bounded depth, element count and bytes."""
    pending, count = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > 24 or count > 2000000:
            fail(code, "JSON exceeds its depth or element limit.")
        if isinstance(item, dict):
            if any(type(key) is not str for key in item):
                fail(code, "JSON object keys must be strings.")
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend((child, depth + 1) for child in item)
        elif item is not None and type(item) not in (str, int, float, bool):
            fail(code, "A strict JSON value is required.")
        elif type(item) is float and not math.isfinite(item):
            fail(code, "Non-finite numbers are not accepted.")
    try:
        data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        fail(code, "Finite UTF-8 JSON is required.")
    if len(data) > limit:
        fail("request_too_large" if code == "invalid_input" else code, "JSON exceeds its byte limit.")
    return data


def strict_json(text, limit, code="invalid_input"):
    """Parse JSON text, rejecting duplicate keys, non-finite numbers and oversized input."""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8", errors="strict")
        except UnicodeError:
            fail(code, "UTF-8 JSON is required.")
    if not isinstance(text, str):
        fail(code, "JSON text is required.")
    if len(text.encode("utf-8", errors="replace")) > limit:
        fail("request_too_large" if code == "invalid_input" else code, "JSON exceeds its byte limit.")

    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                fail(code, "Duplicate JSON property.")
            result[key] = item
        return result

    def constant(_name):
        fail(code, "Non-finite numbers are not accepted.")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except HubSpotError:
        raise
    except (ValueError, RecursionError):
        fail(code, "Malformed JSON.")
    encoded(value, limit, code)
    return value


def boolean(value, name, code="config"):
    if type(value) is not bool:
        fail(code, f"{name} must be a boolean.")
    return value


def integer(value, name, low, high, code="config"):
    if type(value) is not int or not low <= value <= high:
        fail(code, f"{name} must be an integer between {low} and {high}.")
    return value


def path_text(value, name):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        fail("config", f"{name} must be a bounded path without control characters.")
    value = value.strip()
    if value and (not value.startswith("/") or ".." in value.split("/")):
        fail("config", f"{name} must be empty or an absolute path without '..' components.")
    return value


def is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def api_origin(value, name):
    """Accept an HTTPS origin; plain HTTP only for a loopback host such as a local test double."""
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or any(ord(char) <= 32 or ord(char) >= 127 for char in value):
        fail("config", f"{name} must be an ASCII URL without spaces.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        fail("config", f"{name} has an invalid host or port.")
    host = (parsed.hostname or "").lower()
    if (parsed.scheme not in ("https", "http") or not host or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or "?" in value or "#" in value
            or parsed.path not in ("", "/") or "\\" in value or "%" in host):
        fail("config", f"{name} must be a bare https:// origin without credentials, path, query or fragment.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if len(host) > 253 or host.endswith(".") or any(not HOST_LABEL.fullmatch(label) for label in host.split(".")):
            fail("config", f"{name} has an invalid host name.")
    if port is not None and not 1 <= port <= 65535:
        fail("config", f"{name} has an invalid port.")
    if parsed.scheme == "http" and not is_loopback(host):
        fail("config", f"Plain HTTP is accepted for {name} only with a loopback test host; use HTTPS.")
    netloc = "[" + host + "]" if ":" in host else host
    return parsed.scheme + "://" + netloc + (":" + str(port) if port is not None else "")


def visible(text):
    """No control characters (C0, DEL, C1) in values shown or sent as identifiers."""
    return not any(ord(char) < 32 or 127 <= ord(char) < 160 for char in text)


def property_name(value, label, code="invalid_input"):
    """A HubSpot internal property name such as email, hs_lead_status or a custom unique property."""
    if not isinstance(value, str) or not PROPERTY.fullmatch(value):
        fail(code, f"{label} must be a HubSpot internal property name (letters, digits, _ . : -; at most 128).")
    return value


def property_names(value, label, code, low=0, high=MAX_PROPERTIES):
    if not isinstance(value, list) or not low <= len(value) <= high:
        fail(code, f"{label} must be a JSON array of {low}-{high} property names.")
    names = [property_name(item, label, code) for item in value]
    if len(set(names)) != len(names):
        fail(code, f"{label} must not repeat a property.")
    return names


def object_type(value, label="object_type", code="invalid_input"):
    if value not in OBJECT_TYPES:
        fail(code, f"{label} must be one of: " + ", ".join(OBJECT_TYPES) + ".")
    return value


def operations_list(value):
    if (not isinstance(value, list) or not 1 <= len(value) <= len(OPERATIONS)
            or any(item not in OPERATIONS for item in value) or len(set(value)) != len(value)):
        fail("config", "allowed_operations must list distinct values among " + ", ".join(OPERATIONS) + ".")
    return value


def configuration(raw):
    """Pure settings validation; no wallet, filesystem or network access."""
    raw = thaw(raw)
    if not isinstance(raw, Mapping):
        fail("config", "Settings must be a JSON object.")
    unknown = set(raw) - set(DEFAULTS) - FRAMEWORK_KEYS
    if unknown:
        fail("config", "Unknown setting: " + sorted(unknown)[0][:64] + ". Credentials belong in the wallet.")
    config = {**{key: thaw(value) for key, value in DEFAULTS.items()},
              **{key: value for key, value in raw.items() if key in DEFAULTS}}
    for key in BOOLEANS:
        boolean(config[key], key)
    for key, (low, high) in BOUNDS.items():
        integer(config[key], key, low, high)
    ref = config["secret_ref"]
    if not isinstance(ref, str) or len(ref) > 256:
        fail("config", "secret_ref must be a wallet reference such as secret://workspace/hubspot.")
    ref = ref.strip()
    if ref and not SECRET_REF.fullmatch(ref):
        fail("config", "secret_ref must be a wallet reference such as secret://workspace/hubspot, never a token.")
    config["secret_ref"] = ref
    config["object_type"] = object_type(config["object_type"], code="config")
    if config["operation"] not in OPERATIONS:
        fail("config", "operation must be one of: " + ", ".join(OPERATIONS) + ".")
    config["allowed_operations"] = operations_list(config["allowed_operations"])
    selectable = set(config["allowed_operations"]) | {config["operation"]}
    if selectable & WRITES and not config["allow_writes"]:
        fail("config", "create, update, upsert, associate and archive need allow_writes; enable it or remove them.")
    if "archive" in selectable and not config["allow_delete"]:
        fail("config", "archive needs allow_delete; enable it or remove archive.")
    config["properties"] = property_names(config["properties"], "properties", "config")
    if not isinstance(config["id_property"], str):
        fail("config", "id_property must be text.")
    config["id_property"] = config["id_property"].strip()
    if config["id_property"]:
        property_name(config["id_property"], "id_property", "config")
    config["ca_file"] = path_text(config["ca_file"], "ca_file")
    config["api_base_url"] = api_origin(config["api_base_url"], "api_base_url")
    if config["enabled"] and not config["secret_ref"]:
        fail("config", "Remote access requires secret_ref, a wallet reference holding the HubSpot access token.")
    return config


def selectable_operations(config):
    return [item for item in OPERATIONS if item == config["operation"] or item in config["allowed_operations"]]


def credentials(value):
    """Validate the wallet value: a bare token, {"token": ...} or {"access_token": ...}; return the token."""
    secret = value
    if isinstance(secret, str):
        text = secret.strip()
        if text.startswith("{"):
            try:
                secret = strict_json(text, 32768, "secret")
            except HubSpotError:
                fail("secret")
        else:
            secret = {"token": text}
    if not isinstance(secret, Mapping) or set(secret) not in ({"token"}, {"access_token"}):
        fail("secret")
    token = next(iter(secret.values()))
    if not isinstance(token, str) or not TOKEN_TEXT.fullmatch(token):
        fail("secret")
    return token


def redact(text, secrets):
    """Mask every credential value wherever it could appear."""
    text = "" if text is None else str(text)
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "***")
    return text


def clean(value, limit):
    """Bounded single-line provider text with control characters removed."""
    if not isinstance(value, str):
        return None
    value = "".join(" " if ord(char) < 32 or ord(char) == 127 else char for char in value).strip()
    return value[:limit] if value else None


# ---------------------------------------------------------------------------
# Requests


def identifier(value):
    if value is None:
        return None
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        fail("invalid_input", "request_id needs 1-128 ASCII letters, digits or ._:- characters.")
    return value


def object_id(value, label):
    """A HubSpot record ID: the decimal digits HubSpot returns as text (a number is accepted)."""
    if type(value) is int and 0 < value < 10 ** 20:
        value = str(value)
    if not isinstance(value, str) or not OBJECT_ID.fullmatch(value) or value.strip("0") == "":
        fail("invalid_input", f"{label} must be a HubSpot record ID made of digits.")
    return value


def unique_value(value, label):
    """A value of a unique property (for example an email address) used as a record key."""
    if not isinstance(value, str) or not value.strip() or len(value) > UNIQUE_LIMIT or not visible(value):
        fail("invalid_input", f"{label} must be text of 1-{UNIQUE_LIMIT} characters without control characters.")
    return value


def record_key(value, label, id_property):
    """With id_property the key is that property's value; otherwise a HubSpot record ID."""
    return unique_value(value, label) if id_property else object_id(value, label)


def document(value, config):
    """Read a JSON object or array; a bare record ID or other text is a trigger for the configured operation."""
    if isinstance(value, Mapping):
        result = thaw(value)
        encoded(result, config["max_request_bytes"])
        return result
    if isinstance(value, (list, tuple)):
        result = thaw(value)
        encoded(result, config["max_request_bytes"])
        return {"records": result}
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="strict")
        except UnicodeError:
            fail("invalid_input", "The request must be UTF-8 text or JSON.")
    if not isinstance(value, str):
        fail("invalid_input", "The request must be UTF-8 text or JSON.")
    text = value.strip()
    if text.startswith("{") or text.startswith("["):
        parsed = strict_json(text, config["max_request_bytes"])
        if isinstance(parsed, list):
            return {"records": parsed}
        if not isinstance(parsed, dict):
            fail("invalid_input", "A value starting with { must be one JSON object.")
        return parsed
    if len(value.encode("utf-8", errors="replace")) > config["max_request_bytes"]:
        fail("request_too_large")
    if OBJECT_ID.fullmatch(text) and config["operation"] in ("get", "archive"):
        return {"id": text}
    return {}


def scalar(value, label):
    """A filter or property value: text, a finite number or a boolean, sent as HubSpot expects."""
    if type(value) is bool or type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            fail("invalid_input", f"{label} must be finite.")
        return value
    if not isinstance(value, str) or len(value) > VALUE_LIMIT:
        fail("invalid_input", f"{label} must be text, a number or a boolean.")
    return value


def search_filter(item, label):
    """One HubSpot filter: {propertyName, operator, value | values | value + highValue}."""
    if not isinstance(item, dict) or "propertyName" not in item or "operator" not in item:
        fail("invalid_input", f"{label} must be an object with propertyName and operator.")
    if not set(item) <= {"propertyName", "operator", "value", "values", "highValue"}:
        fail("invalid_input", f"{label} accepts only propertyName, operator, value, values and highValue.")
    operator = item["operator"]
    if operator not in OPERATORS:
        fail("invalid_input", f"{label}.operator must be one of: " + ", ".join(OPERATORS) + ".")
    result = {"propertyName": property_name(item["propertyName"], label + ".propertyName"), "operator": operator}
    given = set(item) - {"propertyName", "operator"}
    if operator in ("HAS_PROPERTY", "NOT_HAS_PROPERTY"):
        expected = set()
    elif operator in ("IN", "NOT_IN"):
        expected = {"values"}
    elif operator == "BETWEEN":
        expected = {"value", "highValue"}
    else:
        expected = {"value"}
    if given != expected:
        names = ", ".join(sorted(expected)) or "no value"
        fail("invalid_input", f"{label} with {operator} needs {names}.")
    if "values" in item:
        values = item["values"]
        if not isinstance(values, list) or not 1 <= len(values) <= MAX_IN_VALUES:
            fail("invalid_input", f"{label}.values must be a JSON array of 1-{MAX_IN_VALUES} values.")
        result["values"] = [scalar(entry, label + ".values") for entry in values]
    for key in ("value", "highValue"):
        if key in item:
            result[key] = scalar(item[key], f"{label}.{key}")
    return result


def filter_groups(data):
    """filters (one AND group) or filter_groups (OR of AND groups), within HubSpot's documented limits."""
    if "filters" in data and "filter_groups" in data:
        fail("invalid_input", "Give either filters or filter_groups, not both.")
    if "filters" in data:
        groups = [{"filters": data["filters"]}]
    else:
        groups = data.get("filter_groups", [])
    if not isinstance(groups, list) or len(groups) > MAX_FILTER_GROUPS:
        fail("invalid_input", f"filter_groups must be a JSON array of at most {MAX_FILTER_GROUPS} groups.")
    result, total = [], 0
    for index, group in enumerate(groups):
        if not isinstance(group, dict) or set(group) != {"filters"} or not isinstance(group["filters"], list):
            fail("invalid_input", f"filter_groups[{index}] must be an object with a filters array.")
        filters = group["filters"]
        if not 1 <= len(filters) <= MAX_FILTERS_PER_GROUP:
            fail("invalid_input", f"filter_groups[{index}] needs 1-{MAX_FILTERS_PER_GROUP} filters.")
        total += len(filters)
        result.append({"filters": [search_filter(item, f"filter_groups[{index}].filters[{position}]")
                                   for position, item in enumerate(filters)]})
    if total > MAX_FILTERS:
        fail("invalid_input", f"A search accepts at most {MAX_FILTERS} filters in total.")
    return result


def sort_spec(value):
    """HubSpot search accepts one sort rule: [{propertyName, direction}]."""
    if not isinstance(value, list) or len(value) > 1:
        fail("invalid_input", "sorts must be a JSON array of at most one {propertyName, direction} object.")
    result = []
    for item in value:
        if not isinstance(item, dict) or "propertyName" not in item or not set(item) <= {"propertyName", "direction"}:
            fail("invalid_input", "sorts[0] must be an object with propertyName and an optional direction.")
        direction = item.get("direction", "ASCENDING")
        if direction not in ("ASCENDING", "DESCENDING"):
            fail("invalid_input", "sorts[0].direction must be ASCENDING or DESCENDING.")
        result.append({"propertyName": property_name(item["propertyName"], "sorts[0].propertyName"),
                       "direction": direction})
    return result


def query_text(value):
    if not isinstance(value, str) or len(value) > QUERY_LIMIT or not visible(value):
        fail("invalid_input", f"query must be text of at most {QUERY_LIMIT} characters without control characters.")
    return value.strip()


def properties_object(value, label):
    """A record's properties: an object keyed by property names; values are text, numbers, booleans or null."""
    if not isinstance(value, dict):
        fail("invalid_input", f"{label} must be a JSON object keyed by property names.")
    result = {}
    for key, item in value.items():
        property_name(key, f"{label} key")
        result[key] = None if item is None else scalar(item, f"{label}.{key}")
    return result


def listed(data, plural, single, label):
    """Read a plural array or a single value of a request, never both."""
    items, one = data.get(plural), data.get(single)
    if items is not None and one is not None:
        fail("invalid_input", f"Give either {plural} or {single}, not both.")
    if one is not None:
        items = [one]
    if not isinstance(items, list) or not items:
        fail("invalid_input", f"{plural} must be a non-empty JSON array of {label}.")
    return items


def write_records(data, operation, config, id_property):
    """Validate the records of a create, update or upsert request."""
    records = listed(data, "records", "record", "{\"properties\": {...}} objects")
    if len(records) > config["max_write_records"]:
        fail("too_many_records")
    keyed = operation in ("update", "upsert")
    allowed = {"id", "properties"} if keyed else {"properties"}
    shape = '{"id": "...", "properties": {...}}' if keyed else '{"properties": {...}}'
    result, seen = [], set()
    for index, item in enumerate(records):
        label = f"records[{index}]"
        if not isinstance(item, dict) or "properties" not in item or not set(item) <= allowed or (keyed and "id" not in item):
            fail("invalid_input", f"{label} must be an object shaped {shape}.")
        properties = properties_object(item["properties"], label + ".properties")
        if not keyed:
            result.append({"properties": properties})
            continue
        ident = record_key(item["id"], label + ".id", id_property)
        if ident in seen:
            fail("invalid_input", f"{label}.id repeats a record of this request; nothing was written.")
        seen.add(ident)
        if operation == "update" and not properties:
            fail("invalid_input", f"{label}.properties must name at least one property to update.")
        result.append({"id": ident, "properties": properties})
    return result


def association_pairs(data, config):
    if "pairs" in data and ("from_id" in data or "to_id" in data):
        fail("invalid_input", "Give either pairs or from_id and to_id, not both.")
    pairs = [{"from_id": data.get("from_id"), "to_id": data.get("to_id")}] if "pairs" not in data else data["pairs"]
    if not isinstance(pairs, list) or not pairs:
        fail("invalid_input", "pairs must be a non-empty JSON array of {\"from_id\", \"to_id\"} objects.")
    if len(pairs) > config["max_write_records"]:
        fail("too_many_records")
    result, seen = [], set()
    for index, item in enumerate(pairs):
        if not isinstance(item, dict) or set(item) != {"from_id", "to_id"}:
            fail("invalid_input", f"pairs[{index}] must be an object with from_id and to_id.")
        pair = (object_id(item["from_id"], f"pairs[{index}].from_id"), object_id(item["to_id"], f"pairs[{index}].to_id"))
        if pair in seen:
            fail("invalid_input", f"pairs[{index}] repeats a pair of this request; nothing was associated.")
        seen.add(pair)
        result.append({"from_id": pair[0], "to_id": pair[1]})
    return result


def archive_ids(data, config):
    ids = listed(data, "ids", "id", "record IDs")
    if len(ids) > config["max_write_records"]:
        fail("too_many_records")
    result = [object_id(item, f"ids[{index}]") for index, item in enumerate(ids)]
    if len(set(result)) != len(result):
        fail("invalid_input", "ids must not repeat a record; nothing was archived.")
    return result


def parse_request(value, config):
    """Return one normalized operation request; enforce the operation allowlist before any wallet access."""
    data = document(value, config)
    operation = data.get("operation", config["operation"])
    if operation not in OPERATIONS:
        fail("invalid_input", "operation must be one of: " + ", ".join(OPERATIONS) + ".")
    request = {"operation": operation, "request_id": identifier(data.get("request_id"))}
    if operation not in selectable_operations(config):
        fail("operation_not_allowed")
    if operation in WRITES and not config["allow_writes"]:
        fail("writes_disabled")
    if operation == "archive" and not config["allow_delete"]:
        fail("delete_disabled")
    unknown = set(data) - REQUEST_KEYS[operation]
    if unknown:
        fail("invalid_input", "Unknown field for " + operation + ": " + sorted(unknown)[0][:64] + ".")
    request["object_type"] = object_type(data.get("object_type", config["object_type"]))
    if operation == "watch":
        return request
    if operation == "search":
        request["filter_groups"] = filter_groups(data)
        request["sorts"] = sort_spec(data.get("sorts", []))
        request["query"] = query_text(data.get("query", ""))
        request["properties"] = (property_names(data["properties"], "properties", "invalid_input", 1)
                                 if "properties" in data else list(config["properties"]))
        request["max_records"] = integer(data.get("max_records", config["read_limit"]), "max_records", 1,
                                         config["read_limit"], "invalid_input")
        request["page_size"] = integer(data.get("page_size", config["page_size"]), "page_size", 1, SEARCH_LIMIT,
                                       "invalid_input")
        after = data.get("after")
        if type(after) is int and after >= 0:
            after = str(after)
        if after is not None and (not isinstance(after, str) or not AFTER_TEXT.fullmatch(after)):
            fail("invalid_input", "after must be the paging cursor returned by a previous search result.")
        request["after"] = after
        return request
    if operation == "get":
        id_property = data.get("id_property", "")
        if id_property:
            property_name(id_property, "id_property")
        elif id_property != "":
            fail("invalid_input", "id_property must be a property name or empty.")
        request["id_property"] = id_property or None
        request["id"] = record_key(data.get("id"), "id", id_property)
        request["properties"] = (property_names(data["properties"], "properties", "invalid_input", 1)
                                 if "properties" in data else list(config["properties"]))
        associations = data.get("associations", [])
        if (not isinstance(associations, list) or len(associations) > MAX_ASSOCIATIONS
                or len(set(map(str, associations))) != len(associations)):
            fail("invalid_input", f"associations must be a JSON array of at most {MAX_ASSOCIATIONS} distinct object types.")
        request["associations"] = [object_type(item, "associations[]") for item in associations]
        return request
    if operation == "list_properties":
        return request
    if operation == "archive":
        request["ids"] = archive_ids(data, config)
        return request
    if operation == "associate":
        request["to_object_type"] = object_type(data.get("to_object_type"), "to_object_type")
        request["pairs"] = association_pairs(data, config)
        return request
    id_property = None
    if operation in ("update", "upsert"):
        id_property = data.get("id_property", config["id_property"] if operation == "upsert" else "")
        if id_property:
            property_name(id_property, "id_property")
        elif id_property != "":
            fail("invalid_input", "id_property must be a property name or empty.")
        id_property = id_property or None
        if operation == "upsert" and not id_property:
            fail("invalid_input", "upsert needs id_property, a unique property such as email, in the request or the properties.")
        request["id_property"] = id_property
    request["records"] = write_records(data, operation, config, id_property)
    return request


# ---------------------------------------------------------------------------
# Paths, query strings and request bodies


def objects_path(kind, *suffix):
    """/crm/v3/objects/<type>[/<suffix>]: every dynamic segment is percent-encoded."""
    return "/".join(["", "crm", "v3", "objects", kind] + [quote(item, safe="") for item in suffix])


def properties_path(kind):
    return "/crm/v3/properties/" + kind


def association_path(from_type, to_type):
    return f"/crm/v4/associations/{from_type}/{to_type}/batch/associate/default"


def get_query(request):
    pairs = []
    if request["properties"]:
        pairs.append(("properties", ",".join(request["properties"])))
    if request["associations"]:
        pairs.append(("associations", ",".join(request["associations"])))
    if request["id_property"]:
        pairs.append(("idProperty", request["id_property"]))
    pairs.append(("archived", "false"))
    return urlencode(pairs)


def search_body(request, size, after, limit):
    """The JSON body of POST /crm/v3/objects/<type>/search, sized before it is sent."""
    body = {"filterGroups": [{"filters": [dict(item) for item in group["filters"]]} for group in request["filter_groups"]],
            "limit": size}
    if request["sorts"]:
        body["sorts"] = [dict(item) for item in request["sorts"]]
    if request["properties"]:
        body["properties"] = list(request["properties"])
    if request["query"]:
        body["query"] = request["query"]
    if after is not None:
        body["after"] = after
    return encoded(body, limit, "request_too_large")


def batches(items, size=BATCH_SIZE):
    return [items[index:index + size] for index in range(0, len(items), size)]


def write_inputs(request):
    """HubSpot batch inputs for create, update, upsert, archive and associate."""
    operation = request["operation"]
    if operation == "archive":
        return [{"id": ident} for ident in request["ids"]]
    if operation == "associate":
        return [{"from": {"id": pair["from_id"]}, "to": {"id": pair["to_id"]}} for pair in request["pairs"]]
    if operation == "create":
        return [{"properties": item["properties"]} for item in request["records"]]
    inputs = []
    for item in request["records"]:
        entry = {"id": item["id"], "properties": item["properties"]}
        if request["id_property"]:
            entry["idProperty"] = request["id_property"]
        inputs.append(entry)
    return inputs


def write_bodies(request, config):
    """Build and size every batch body before the first write, so a late batch cannot fail locally."""
    return [encoded({"inputs": chunk}, config["max_request_bytes"], "request_too_large")
            for chunk in batches(write_inputs(request))]


def write_path(request):
    operation = request["operation"]
    if operation == "associate":
        return association_path(request["object_type"], request["to_object_type"])
    return objects_path(request["object_type"], "batch", operation)


# ---------------------------------------------------------------------------
# Provider responses


def provider_detail(body):
    """HubSpot error bodies: {"status": "error", "message", "correlationId", "category", "policyName"?}."""
    if not isinstance(body, dict):
        return None, None, None, None
    category = body.get("category") or body.get("errorType")
    return (category if isinstance(category, str) else None, body.get("message"),
            body.get("correlationId") if isinstance(body.get("correlationId"), str) else None,
            body.get("policyName") if isinstance(body.get("policyName"), str) else None)


def map_status(status):
    return {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "conflict",
            413: "provider_too_large", 414: "provider_too_large", 422: "invalid_request", 429: "rate_limited"}.get(
                status, "provider_unavailable" if status >= 500 else "provider_error")


def rate_wait(policy, retry_after):
    """Retry-After when HubSpot sends it, otherwise the window of the named limit."""
    if retry_after is not None:
        return retry_after
    return RATE_WAIT.get(policy or "", RATE_WAIT_DEFAULT)


def provider_failure(status, body, secrets, *, mutation, retry_after=None):
    """Raise the coded error for a non-2xx HubSpot response."""
    category, message, correlation, policy = provider_detail(body)
    code = map_status(status)
    if code == "rate_limited" and (policy or "").upper() == "DAILY":
        code = "daily_limit"
    uncertain = mutation and code == "provider_unavailable"
    fail(code, MESSAGES["provider_unavailable_uncertain"] if uncertain else None, uncertain=uncertain,
         http_status=status, retry_after=rate_wait(policy, retry_after) if code == "rate_limited" else None,
         provider_category=clean(category, 64), provider_message=clean(redact(message, secrets), 200),
         correlation_id=clean(correlation, 64))


def epoch_ms(text):
    """Milliseconds since the epoch of a HubSpot ISO-8601 timestamp, or None."""
    if not isinstance(text, str):
        return None
    match = TIMESTAMP.fullmatch(text)
    if not match:
        return None
    try:
        zone = match.group(8)
        stamp = datetime.fromisoformat(text[:19] + ("+00:00" if zone == "Z" else zone))
    except ValueError:
        return None
    fraction = (match.group(7) or "0")[:3].ljust(3, "0")
    return int(stamp.timestamp()) * 1000 + int(fraction)


def iso_ms(value):
    """ISO-8601 UTC text with milliseconds for an epoch-millisecond value."""
    stamp = datetime.fromtimestamp(value // 1000, tz=timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S") + f".{value % 1000:03d}Z"


def text_or_none(value, limit=64):
    return value[:limit] if isinstance(value, str) else None


def associations_of(value):
    """{"companies": {"results": [{"id", "type"}]}} -> {"companies": ["id", ...]} (distinct, in order)."""
    if value is None:
        return None
    if not isinstance(value, dict):
        fail("protocol")
    result = {}
    for kind, entry in value.items():
        items = entry.get("results") if isinstance(entry, dict) else None
        if not isinstance(kind, str) or not isinstance(items, list):
            fail("protocol")
        ids = []
        for item in items:
            ident = str(item.get("id")) if isinstance(item, dict) and isinstance(item.get("id"), (str, int)) else ""
            if not OBJECT_ID.fullmatch(ident):
                fail("protocol")
            if ident not in ids:
                ids.append(ident)
        result[kind[:64]] = ids
    return result


def record(item, kind, *, mutation=False):
    """Normalize one HubSpot object to {id, object_type, properties, created_at, updated_at, archived}."""
    ident = item.get("id") if isinstance(item, dict) else None
    if isinstance(ident, int) and not isinstance(ident, bool):
        ident = str(ident)
    properties = item.get("properties", {}) if isinstance(item, dict) else None
    if (not isinstance(ident, str) or not OBJECT_ID.fullmatch(ident) or not isinstance(properties, dict)
            or any(not isinstance(key, str) or (value is not None and type(value) not in (str, int, float, bool))
                   for key, value in properties.items())):
        fail("protocol", MESSAGES["protocol_uncertain"] if mutation else None, uncertain=mutation)
    result = {"id": ident, "object_type": kind, "properties": properties,
              "created_at": text_or_none(item.get("createdAt")), "updated_at": text_or_none(item.get("updatedAt")),
              "archived": item.get("archived") is True}
    associations = associations_of(item.get("associations"))
    if associations is not None:
        result["associations"] = associations
    return result


def search_page(body, size, kind):
    """One search page: at most `size` records, the reported total and an optional `after` cursor."""
    results = body.get("results") if isinstance(body, dict) else None
    if not isinstance(results, list) or len(results) > size:
        fail("protocol")
    total = body.get("total")
    if total is not None and (type(total) is not int or total < 0):
        fail("protocol")
    paging = body.get("paging")
    after = None
    if paging is not None:
        if not isinstance(paging, dict):
            fail("protocol")
        following = paging.get("next")
        if following is not None:
            after = following.get("after") if isinstance(following, dict) else None
            if type(after) is int and after >= 0:
                after = str(after)
            if not isinstance(after, str) or not AFTER_TEXT.fullmatch(after):
                fail("protocol")
    return [record(item, kind) for item in results], after, total


def capped(after):
    """True when the next search page would start beyond HubSpot's 10,000-result window."""
    return after is not None and after.isdigit() and int(after) >= SEARCH_CAP


def item_errors(body, secrets):
    """Sanitized errors of an HTTP 207 batch receipt: category, bounded message and the input keys involved."""
    errors = body.get("errors") if isinstance(body, dict) else None
    result = []
    for entry in errors if isinstance(errors, list) else []:
        if not isinstance(entry, dict):
            continue
        context = entry.get("context") if isinstance(entry.get("context"), dict) else {}
        ids = context.get("ids") if isinstance(context.get("ids"), list) else []
        result.append({"category": clean(entry.get("category"), 64),
                       "message": clean(redact(entry.get("message"), secrets), 200),
                       "ids": [clean(str(item), 255) for item in ids[:10] if isinstance(item, (str, int))]})
        if len(result) >= 20:
            break
    return result


def batch_records(body, chunk_size, kind, *, partial):
    """The records of a batch receipt; a complete receipt returns one record per input."""
    results = body.get("results") if isinstance(body, dict) else None
    if not isinstance(results, list) or len(results) > chunk_size or (not partial and len(results) != chunk_size):
        fail("protocol", MESSAGES["protocol_uncertain"], uncertain=True)
    records = [record(item, kind, mutation=True) for item in results]
    created = [item["id"] for item, raw in zip(records, results) if raw.get("new") is True]
    return records, created


def associated(body, pairs, *, partial):
    """Default-association receipts: every requested pair must be reported, in either direction."""
    results = body.get("results") if isinstance(body, dict) else None
    if not isinstance(results, list):
        fail("protocol", MESSAGES["protocol_uncertain"], uncertain=True)
    found, types = set(), set()
    for item in results:
        source = item.get("from") if isinstance(item, dict) else None
        target = item.get("to") if isinstance(item, dict) else None
        spec = item.get("associationSpec") if isinstance(item, dict) else None
        left = str(source.get("id")) if isinstance(source, dict) else ""
        right = str(target.get("id")) if isinstance(target, dict) else ""
        if not OBJECT_ID.fullmatch(left) or not OBJECT_ID.fullmatch(right):
            fail("protocol", MESSAGES["protocol_uncertain"], uncertain=True)
        found.update(((left, right), (right, left)))
        if isinstance(spec, dict) and type(spec.get("associationTypeId")) is int:
            types.add(spec["associationTypeId"])
    done = [pair for pair in pairs if (pair["from_id"], pair["to_id"]) in found]
    if not partial and len(done) != len(pairs):
        fail("protocol", MESSAGES["protocol_uncertain"], uncertain=True)
    return done, sorted(types)


def property_definition(item):
    """Normalize one property definition of GET /crm/v3/properties/<type>."""
    if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
        fail("protocol")
    metadata = item.get("modificationMetadata") if isinstance(item.get("modificationMetadata"), dict) else {}
    options = []
    for option in item.get("options") or []:
        if isinstance(option, dict) and isinstance(option.get("value"), str):
            options.append({"label": text_or_none(option.get("label"), 255), "value": option["value"][:255]})
    return {"name": item["name"][:128], "label": text_or_none(item.get("label"), 255),
            "type": text_or_none(item.get("type")), "field_type": text_or_none(item.get("fieldType")),
            "group_name": text_or_none(item.get("groupName"), 128),
            "description": text_or_none(item.get("description"), 1000),
            "unique": item.get("hasUniqueValue") is True, "read_only": metadata.get("readOnlyValue") is True,
            "calculated": item.get("calculated") is True, "hidden": item.get("hidden") is True, "options": options[:500]}


def size_of(item):
    return len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1


# ---------------------------------------------------------------------------
# Watch state


def watch_key(config):
    """One cursor per API origin, wallet reference and object type; the document never holds the reference."""
    material = "|".join((config["api_base_url"], config["secret_ref"], config["object_type"]))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def seen_digest(ident, modified):
    return hashlib.sha256(f"{ident}|{modified}".encode("ascii")).hexdigest()[:16]


def empty_state():
    return {"schema_version": 1, "watches": {}}


def millis(value):
    return type(value) is int and 0 <= value < 10 ** 15


def state_document(value):
    """Validate a stored watch document: cursors, a resume position, seen digests and timestamps only."""
    if not isinstance(value, dict) or value.get("schema_version") != 1 or set(value) != {"schema_version", "watches"}:
        fail("state_invalid")
    watches = value["watches"]
    if not isinstance(watches, dict) or len(watches) > MAX_WATCHES:
        fail("state_invalid")
    for key, entry in watches.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{24}", key) or not isinstance(entry, dict)
                or set(entry) != {"floor", "cursor", "resume", "seen", "updated_at"}):
            fail("state_invalid")
        if not millis(entry["floor"]) or not millis(entry["cursor"]) or entry["cursor"] < entry["floor"]:
            fail("state_invalid")
        resume = entry["resume"]
        if resume is not None and (not isinstance(resume, dict) or set(resume) != {"lower", "after"}
                                   or not millis(resume["lower"]) or not isinstance(resume["after"], str)
                                   or not AFTER_TEXT.fullmatch(resume["after"])):
            fail("state_invalid")
        seen = entry["seen"]
        if (not isinstance(seen, list) or len(seen) > SEEN_LIMIT
                or any(not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str)
                       or not re.fullmatch(r"[0-9a-f]{16}", item[0]) or not millis(item[1]) for item in seen)):
            fail("state_invalid")
        if type(entry["updated_at"]) is not int or entry["updated_at"] < 0:
            fail("state_invalid")
    return value


def with_watch(document, key, entry):
    """Return a new document holding this watch; at most eight watches are retained."""
    watches = dict(document["watches"])
    watches[key] = entry
    if len(watches) > MAX_WATCHES:
        for old in sorted(watches, key=lambda item: watches[item]["updated_at"])[:len(watches) - MAX_WATCHES]:
            if old != key:
                watches.pop(old)
    return {"schema_version": 1, "watches": watches}


def watch_body(config, lower, size, after, properties):
    """Records modified at or after `lower`, oldest first, on the object's modification property."""
    prop = MODIFIED[config["object_type"]]
    body = {"filterGroups": [{"filters": [{"propertyName": prop, "operator": "GTE", "value": str(lower)}]}],
            "sorts": [{"propertyName": prop, "direction": "ASCENDING"}], "limit": size}
    if properties:
        body["properties"] = properties
    if after is not None:
        body["after"] = after
    return encoded(body, 65536, "request_too_large")


def position_body(config):
    """The most recently modified record: its time is the watch start position on HubSpot's clock."""
    prop = MODIFIED[config["object_type"]]
    return encoded({"filterGroups": [], "sorts": [{"propertyName": prop, "direction": "DESCENDING"}],
                    "properties": [prop], "limit": 1}, 65536, "request_too_large")


def watch_properties(config):
    """Requested properties plus the modification property, so a change is always dated."""
    names = list(config["properties"])
    prop = MODIFIED[config["object_type"]]
    if names and prop not in names:
        names.append(prop)
    return names
