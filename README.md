# HubSpot

Search, create, update and associate HubSpot CRM records. The block works on
contacts, companies, deals, tickets and notes through the HubSpot CRM API with a
private app access token kept in the BloxSmith wallet. Records travel as plain
JSON (`id`, `properties`, timestamps), writes go out in batches of up to 100, and
an optional watch emits records modified since a persisted cursor. Remote access
stays off until you enable it, nothing is written until you also enable writes,
and archiving records needs a third, separate switch.

<!-- block-metadata:start -->
[![Block version: 0.1.0](https://img.shields.io/badge/block-0.1.0-blue)](model.json)
[![BloxSmith compatibility: 1.0.9](https://img.shields.io/badge/BloxSmith-1.0.9-brightgreen)](compatibility.json)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

Verified BloxSmith versions: **1.0.9** (bundled-block tests; see [test evidence](compatibility.json)).
<!-- block-metadata:end -->

[![HubSpot — search, write and associate CRM records](media/thumbnail.webp)](media/cover.png)

## When to use it

- Look up a contact by email, or search deals by stage, amount or owner.
- Create leads from a form, a chat or an e-mail, without duplicates: upsert by
  `email` (or another unique property), so a repeated run updates the record.
- Update a deal stage or a ticket status from a workflow step.
- Link a contact to its company, or a note to a contact, with HubSpot's default
  association type.
- React to CRM changes: in Active Runtime the watch emits records modified since
  the last poll, at most once a minute.
- Discover the property names of an object, custom ones included.

## Ports

| Direction | Port | Value |
| --- | --- | --- |
| Input | `request` | A JSON object with the operation parameters, a JSON array of records, a record ID, or any other text as a plain trigger |
| Output | `records` | search, get, create, update, upsert: a JSON array of records; list_properties: the property definitions |
| Output | `result` | Operation receipt: counts, IDs, `after` cursor, batches, request ID; also the summary of each watch poll |
| Output | `changed` | watch: a JSON array of the records modified since the previous poll |
| Output | `error` | Machine `code`, English `message`, `uncertain` flag, provider details and batch progress |

Each fresh request runs exactly one operation. A retained value is never sent
again, so a replayed or forced activation does not repeat a write. Business
failures are published on `error` and the run continues; they never stop an
Active Runtime worker. The `request` input is optional, so a node that only
watches needs no input wire.

### `request` input

| Field | Operations | Meaning |
| --- | --- | --- |
| `operation` | all | `search`, `get`, `create`, `update`, `upsert`, `associate`, `archive`, `list_properties` or `watch`; defaults to the `operation` setting and must be allowed |
| `request_id` | all | Optional correlation ID (1–128 of `A-Za-z0-9._:-`), echoed in `result` and `error` |
| `object_type` | all but `watch` | `contacts`, `companies`, `deals`, `tickets` or `notes`; defaults to the `object_type` setting |
| `filters` | `search` | One group of filters combined with AND (see below) |
| `filter_groups` | `search` | Up to 5 groups combined with OR: `[{"filters": [...]}, ...]` |
| `sorts` | `search` | At most one rule: `[{"propertyName": "createdate", "direction": "DESCENDING"}]` |
| `query` | `search` | Full-text search over HubSpot's default searchable properties |
| `properties` | `search`, `get` | Property names to return; defaults to the `properties` setting |
| `max_records` | `search` | Records to return, 1 to `read_limit` |
| `page_size` | `search` | Records per page, 1–200; defaults to the `page_size` setting |
| `after` | `search` | The `after` of a previous result, to continue reading |
| `id` | `get`, `archive` | A record ID; for `get` with `id_property`, the value of that unique property |
| `id_property` | `get`, `update`, `upsert` | Unique property used as the key, such as `email` (upsert defaults to the `id_property` setting) |
| `associations` | `get` | Object types whose associated record IDs are returned, up to 5 |
| `records` / `record` | `create`, `update`, `upsert` | Array of records, or one record (see below) |
| `to_object_type` | `associate` | Object type of the records to link to |
| `pairs` / `from_id` + `to_id` | `associate` | Array of `{"from_id", "to_id"}`, or one pair |
| `ids` | `archive` | Array of record IDs |

Unknown fields are refused. A JSON array is a shortcut for `{"records": [...]}`.
A bare record ID such as `1001` is a shortcut for `{"id": ...}` when the default
operation is `get` or `archive`. Other text (for example `go` from a trigger) runs
the default operation without parameters, which is useful for `search` and `watch`.

Filters keep HubSpot's own shape, so an example from the HubSpot documentation can
be pasted as is:

```json
{"operation": "search", "object_type": "deals",
 "filters": [{"propertyName": "dealstage", "operator": "IN", "values": ["appointmentscheduled", "qualifiedtobuy"]},
             {"propertyName": "amount", "operator": "GTE", "value": 1000}],
 "sorts": [{"propertyName": "amount", "direction": "DESCENDING"}],
 "properties": ["dealname", "amount", "dealstage"]}
```

Operators: `EQ`, `NEQ`, `LT`, `LTE`, `GT`, `GTE`, `BETWEEN` (`value` and
`highValue`), `IN` and `NOT_IN` (`values`, up to 100), `HAS_PROPERTY`,
`NOT_HAS_PROPERTY`, `CONTAINS_TOKEN`, `NOT_CONTAINS_TOKEN`. HubSpot's limits are
checked before anything is sent: at most 5 groups, 6 filters per group and 18
filters in total, and one sort rule.

Records to write:

```json
{"operation": "create", "records": [{"properties": {"email": "ada@example.com", "firstname": "Ada"}}]}
{"operation": "update", "records": [{"id": "1001", "properties": {"lifecyclestage": "customer"}}]}
{"operation": "update", "id_property": "email", "record": {"id": "ada@example.com", "properties": {"phone": "+44 20 0000 0000"}}}
{"operation": "upsert", "id_property": "email", "records": [{"id": "ada@example.com", "properties": {"firstname": "Ada"}}]}
{"operation": "associate", "object_type": "contacts", "to_object_type": "companies", "pairs": [{"from_id": "1001", "to_id": "2001"}]}
{"operation": "archive", "object_type": "deals", "ids": ["3001", "3002"]}
```

Property values are text, numbers, booleans or `null` (HubSpot stores them as text;
multiple checkboxes use `;`-separated text). `create` refuses an `id`; `update`
needs one per record, refuses duplicates and at least one property; `upsert` needs
`id_property` and an `id` (the key value) per record. HubSpot creates the missing
records of an upsert with the key property set to that value.

### `records` output

```json
[
  {"id": "1001", "object_type": "contacts",
   "properties": {"email": "ada@example.com", "firstname": "Ada", "createdate": "2026-01-02T10:00:00.000Z",
                  "lastmodifieddate": "2026-01-03T09:30:00.250Z", "hs_object_id": "1001"},
   "created_at": "2026-01-02T10:00:00.000Z", "updated_at": "2026-01-03T09:30:00.250Z", "archived": false}
]
```

`get` with `associations` adds `"associations": {"companies": ["2001"]}`.
`list_properties` returns `{"name", "label", "type", "field_type", "group_name",
"description", "unique", "read_only", "calculated", "hidden", "options"}` per
property, sorted by name.

### `changed` output

The same records, oldest change first, with `"change": "created"` when the record
was created after the previous poll position and `"updated"` otherwise.

### `result` output

- `search`: `{"request_id", "operation", "object_type", "count", "total",
  "truncated", "stop_reason", "after", "pages", "requests"}`. `stop_reason` is
  `end`, `limit` (`max_records`), `max_pages`, `output_limit` (16 MiB of records)
  or `search_cap` (HubSpot's 10,000-result window). Pass `after` back to continue
  exactly where the read stopped; `search_cap` has no continuation: narrow the filters.
- `get`: `id`, `id_property` and `count` (1).
- `create`, `update`: `count`, `ids`, `batches` (and `id_property` when used).
- `upsert`: the same plus `created_ids` and `updated_ids`.
- `associate`: `count`, `to_object_type`, `pairs`, `association_type_ids`, `batches`.
- `archive`: `count`, `archived_ids`, `batches`.
- `list_properties`: `count` and `unique_properties` (the names usable as `id_property`).
- `watch`: `count`, `pages`, `skipped`, `started`, `stop_reason`, `caught_up`,
  `cursor` and `source` (`watch` for the Run listener, `request` for a request).

All receipts carry `request_id`, `operation`, `object_type` and `requests` (HTTP
requests used, retries included). They never contain the token.

### `error` output

`{"code", "message", "uncertain", "operation", "request_id", "object_type"}` plus,
when HubSpot provides them, `http_status`, `retry_after`, `provider_category` (for
example `VALIDATION_ERROR`, `OBJECT_NOT_FOUND`, `MISSING_SCOPES`), `provider_message`
(bounded, credentials removed), `correlation_id` (to quote to HubSpot support) and
`attempts`. Writes add `completed_batches`, `total_batches` and `committed_ids`:
batches before the failing one **are written** and are listed there.
`uncertain: true` means the failing batch itself may have been applied: read the
records before sending it again.

| Code | Meaning |
| --- | --- |
| `config`, `disabled`, `secret`, `invalid_input`, `request_too_large` | Refused locally; nothing sent |
| `operation_not_allowed`, `writes_disabled`, `delete_disabled` | The operation is not enabled in the properties |
| `too_many_records` | More records or pairs than `max_write_records`; nothing written |
| `partial_failure` | HTTP 207: part of a batch was applied; `committed_ids` lists it, `item_errors` the refused inputs; later batches were not sent |
| `bad_request` (400), `unauthorized` (401), `forbidden` (403), `not_found` (404), `conflict` (409), `provider_too_large` (413/414), `invalid_request` (422), `rate_limited` (429), `daily_limit` (429, daily), `provider_error` (other) | HubSpot refused the request |
| `provider_unavailable` (5xx), `network`, `timeout`, `cancelled`, `protocol` | Transport outcome; `uncertain` tells whether a write may have been applied |
| `output_too_large` | One page alone exceeds the 16 MiB output limit; lower `page_size` or ask for fewer properties |
| `storage`, `busy`, `state_invalid` | The watch cursor could not be used: no project storage, another watcher holds it, or the stored state is unreadable |
| `tls` | Certificate verification failed; nothing sent |

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Remote access switch. While off, nothing reaches HubSpot and the wallet is not read |
| `secret_ref` | empty | Wallet reference such as `secret://workspace/hubspot` (see Authentication) |
| `object_type` | `contacts` | Default object; the watch always follows this one |
| `operation` | `search` | Operation used when a request names none; always allowed |
| `allowed_operations` | `["search", "get", "list_properties"]` | Other operations a request may select |
| `properties` | `[]` | Property names returned by default; empty returns HubSpot's default properties |
| `read_limit` | 1000 | Maximum records returned by one search, 1–10000 |
| `allow_writes` | `false` | Required when `create`, `update`, `upsert`, `associate` or `archive` is selectable |
| `id_property` | empty | Default unique property for `upsert`, for example `email` |
| `max_write_records` | 100 | Records or pairs per write request, 1–1000, sent in batches of 100 |
| `allow_delete` | `false` | Additional switch required for `archive` |
| `watch` | `false` | Run the change watch in Active Runtime |
| `watch_interval_sec` | 300 | Seconds between two polls of the Run listener, 60–86400 |
| `emit_existing` | `false` | On the first start, emit the records modified in the recent window instead of only recording the position |
| `existing_since_hours` | 24 | Recent window for `emit_existing`, 1–720 hours |
| `watch_max_items` | 100 | Changes emitted per poll, 1–1000; more wait for the next poll |
| `page_size` | 100 | `limit` per search request, 1–200 |
| `max_pages` | 20 | Pages per search or watch poll, 1–100 |
| `timeout_sec` | 60 | Deadline for one request or poll, pages, batches, pacing and retries included, 5–300 |
| `max_requests_per_10s` | 100 | Pacing per access token in this process, 1–190 |
| `max_search_per_sec` | 4 | Separate pacing for search requests, 1–5 |
| `max_retries` | 2 | Retries, 0–5 (see Limits) |
| `max_retry_wait_sec` | 15 | Longest wait before a retry, 1–120 |
| `max_request_bytes` | 1048576 | Bound for the incoming request and each JSON body sent, 1 KiB–8 MiB |
| `max_response_bytes` | 8388608 | Bound for each HubSpot response, 64 KiB–32 MiB |
| `api_base_url` | `https://api.hubapi.com` | API origin; plain HTTP only for a loopback test host |
| `ca_file` | empty | Optional absolute PEM file for a privately signed HTTPS origin |

The settings are validated on Apply and again before a Run starts. A write
operation listed in `operation` or `allowed_operations` is refused unless
`allow_writes` is on, and `archive` also needs `allow_delete`. Restart a loaded Run
to use changed settings. The `Upsert key property` field suggests HubSpot's standard
property names for the selected object; custom properties are discovered with
`list_properties`.

## Authentication

In HubSpot, create a private app (Settings › Integrations › Private Apps) with the
CRM scopes the node needs, for example `crm.objects.contacts.read`,
`crm.objects.contacts.write`, `crm.objects.companies.read`, `crm.objects.deals.read`,
`tickets` and `crm.schemas.contacts.read` for `list_properties`. Store its access
token in an unlocked wallet secret, then configure only its reference, for example
`secret://workspace/hubspot`. The wallet value is the bare token text,
`{"token": "YOUR_TOKEN"}` or `{"access_token": "YOUR_OAUTH_ACCESS_TOKEN"}`; these are
placeholders, not credentials. An OAuth access token works as long as it is valid;
the block does not refresh it. Legacy API keys are not supported.

## Example

1. Create a private app with `crm.objects.contacts.read` and
   `crm.objects.contacts.write`, and store its token in the wallet as `hubspot`.
2. Set `enabled = true` and `secret_ref = secret://workspace/hubspot`.
3. Wire a Text block containing
   `{"filters": [{"propertyName": "email", "operator": "EQ", "value": "ada@example.com"}]}`
   to `request` and a Display to `records`: One Shot returns the matching contact.
4. To record leads without duplicates, enable `allow_writes`, add `upsert` to
   `allowed_operations`, set `id_property = email` and send
   `{"operation": "upsert", "record": {"id": "ada@example.com", "properties": {"firstname": "Ada"}}}`.
5. To react to changes, enable `watch`, wire `changed` to the next step and start
   an Active Runtime Run. The first start records the current position; each later
   modification arrives once.

## How the operations work

- `search` sends `POST /crm/v3/objects/{objectType}/search` with `filterGroups`,
  `sorts`, `query`, `properties`, `limit` and `after`, page after page. The last
  page asks only for the records still needed, so the returned `after` continues
  exactly. Reading stops at the last page, at `max_records`, at `max_pages`, at
  16 MiB of records or at HubSpot's 10,000-result window, and keeps whole pages.
- `get` calls `GET /crm/v3/objects/{objectType}/{id}` with `properties`,
  `associations`, `idProperty` and `archived=false`.
- `create`, `update` and `upsert` send `POST /crm/v3/objects/{objectType}/batch/create`,
  `/batch/update` and `/batch/upsert` with up to 100 `inputs` per request. Updates
  change only the given properties.
- `associate` sends `POST /crm/v4/associations/{fromObjectType}/{toObjectType}/batch/associate/default`,
  which links each pair with HubSpot's default (unlabeled) association type.
- `archive` sends `POST /crm/v3/objects/{objectType}/batch/archive` (HTTP 204).
  HubSpot moves archived records to its recycle bin, where they can be restored
  for 90 days. The permanent GDPR deletion endpoint is never used.
- `list_properties` calls `GET /crm/v3/properties/{objectType}?archived=false`.
- `watch` searches records whose modification property (`lastmodifieddate` for
  contacts, `hs_lastmodifieddate` for the other objects) is at or after the cursor,
  oldest first, and persists the new cursor in the node storage before the records
  are handed over.

Every write request is validated and sized before the first batch is sent.

## Watch

- **Active Runtime only** for continuous watching: with `watch` on, the Run starts a
  listener that polls every `watch_interval_sec` (at least 60 s) until Stop. Stop
  ends a pending poll at once.
- **One Shot** never starts the listener: a `watch` request performs a single poll
  and returns. Successive Runs of the same project continue from the stored cursor.
- **First start:** without `emit_existing`, the position is the modification time of
  the most recently modified record, read from HubSpot (so the local clock does not
  matter), and nothing is emitted. With `emit_existing`, records modified in the last
  `existing_since_hours` are emitted once, `watch_max_items` per poll.
- **No replay after a restart:** the cursor and the digests of the changes already
  handed over are written atomically to the node storage before the hand-off. If the
  runtime refuses the hand-off, the previous cursor is restored and the changes come
  again on the next Run.
- **Eventual consistency:** HubSpot's search index lags behind writes, so each poll
  re-reads the last two minutes and skips the changes already handed over. A later
  modification of the same record is a new change.
- **Bounds:** each poll reads at most `max_pages` pages and emits at most
  `watch_max_items` changes; the next poll continues where it stopped, resuming the
  same search when a whole window was already handed over.
- While the Run listener holds the cursor, a `watch` request returns `busy` instead
  of polling concurrently. The stored state holds no token, wallet reference or
  property value: only times, a search position and short digests.
- There is no webhook and no inbound listener; the watch only makes outbound requests.

## Limits and warnings

- **Batches are not atomic as a whole.** A request with more than 100 inputs is sent
  as several requests; if one fails, earlier batches stay written and the error lists
  them in `committed_ids`. HubSpot may also apply part of a single batch (HTTP 207):
  the block then stops and reports `partial_failure`.
- The block does not rely on the order of a batch receipt; match created records by
  their properties, not by their position in `records`.
- Requests made with one token are paced to `max_requests_per_10s` (private apps get
  100 per 10 seconds or more depending on the plan) and searches to
  `max_search_per_sec`, within this process and across nodes. Several Active Runtime
  workers each pace themselves; HubSpot then answers 429, which is honoured.
- Reads are retried after HTTP 429, 5xx or a lost connection. Writes are retried only
  after HTTP 429 (HubSpot did not process them); a write that fails after it was sent
  is reported with `uncertain: true` and never replayed. `Retry-After` is honoured up
  to `max_retry_wait_sec`; without it, the window of the limit HubSpot names is used
  (10 s for the ten-second limit, 1 s for the per-second limit, 2 s otherwise). The
  daily limit (`daily_limit`) is never retried.
- Search returns at most 10,000 results per query and reflects writes after a short
  delay. Batch archive answers HTTP 204 without per-record results, so `archived_ids`
  lists the IDs sent, not a confirmation that each one existed.
- Under a burst of more than 10,000 changes within two minutes, or more than 4,096
  changes handed over within that window, a watch poll moves past what it read and
  may miss or repeat some changes; lower the interval or narrow the object.
- No HTTP proxy support; the host needs direct outbound HTTPS to `api.hubapi.com`.
- Property values read from HubSpot are untrusted data. Do not feed them to tools that
  execute commands.

## Dependencies

Python standard library only (`http.client`, `ssl`, `json`, `fcntl`); no
`requirements.txt`. Linux/POSIX is required for the watch state lock.

## Security

- The wallet value is read at run time, only after settings, the request and the
  watch storage are validated. The token never appears in settings, outputs, logs,
  run metadata, the watch state or error messages; provider messages are bounded and
  credentials are removed. Pacing identifies an account by a digest of the token
  kept in memory only.
- TLS certificates and host names are always verified; there is no insecure mode.
- Redirects, cookies, proxies and compressed responses are not accepted.
- An `api_base_url` other than the official origin receives the token: change it
  only for a trusted gateway or a local test double.
- The block opens no listening port; it only makes outbound requests.

## HubSpot API usage

HubSpot CRM API **v3** and associations API **v4** on `https://api.hubapi.com`,
bearer authentication with a private app access token:

- `POST /crm/v3/objects/{objectType}/search`
- `GET /crm/v3/objects/{objectType}/{objectId}` (`properties`, `associations`, `idProperty`, `archived`)
- `POST /crm/v3/objects/{objectType}/batch/create`, `/batch/update`, `/batch/upsert`, `/batch/archive`
- `POST /crm/v4/associations/{fromObjectType}/{toObjectType}/batch/associate/default`
- `GET /crm/v3/properties/{objectType}`

Errors are read from `{"status": "error", "message", "correlationId", "category"}`;
429 bodies are classified by `policyName` (`TEN_SECONDLY_ROLLING`, `SECONDLY`, `DAILY`).

**Real-provider qualification is pending.** All tests use a loopback double that
reproduces these documented request and response shapes; no HubSpot account, token or
portal was contacted. In particular, the exact HTTP 207 receipts for missing IDs, the
direction of the records returned by the default-association batch, the search index
delay and the 429 bodies follow the documentation and have not been checked against
the live service.

## Framing decisions

- Five standard objects only (`contacts`, `companies`, `deals`, `tickets`, `notes`);
  custom objects and engagement types other than notes are not addressed.
- Writes always use the batch endpoints, even for one record, so every write has the
  same receipt, the same batch progress and the same 207 handling.
- `upsert` needs an explicit unique property (`id_property`, for example `email`);
  `list_properties` shows which properties are unique.
- `archive` is the only removal and moves records to HubSpot's recycle bin; it needs
  both `allow_writes` and `allow_delete`. GDPR deletion is deliberately absent.
- `associate` uses default association types only; association labels are not managed.
- Filters, sorts and property names keep HubSpot's own names (`propertyName`,
  `operator`, `highValue`) so documented examples work unchanged; request fields of
  the block itself are snake_case like the other BloxSmith connectors.
- The watch uses the search API on the object's modification property with a
  two-minute lookback and digests, and takes its start position from HubSpot rather
  than from the local clock. Its minimum interval is 60 seconds. It follows the
  configured object only, so one cursor belongs to one node, token reference and object.
- The settings form offers static suggestions of standard property names. It cannot
  list custom properties: the framework gives package UI requests no wallet access,
  so the form never calls HubSpot; run `list_properties` in a graph instead.
- Pacing is process-wide per token, as HubSpot's limits are per account.
- The API origin is fixed but overridable for tests; plain HTTP is accepted only for
  loopback hosts.

## Tests

`tests/F5.01_hubspot.py` checks settings, credentials, request contracts (filters,
operators and HubSpot's limits), bodies and batch sizing, provider error mapping and
429 policies, record normalization, pacing windows, the watch state document and
block hooks without a socket. `tests/F5.02_hubspot_runtime.py` runs the real client
against the loopback double (`tests/hubspot_fixture.py`, HTTP and HTTPS): filters,
query, sort, properties, pages and exact `after` cursors, the 10,000-result window,
get by ID or email with associations, list_properties, batched create, update,
upsert, associate and archive, 207 receipts, partial batch failures, ten-second and
search pacing, 400/401/403/404/409/413/429/5xx, lost connections, oversized and
compressed bodies, cancellation and the deadline; the watch (no flood, each change
once, bursts, late-indexed changes, resume, restart without replay, refused hand-off,
lock); then real graphs in One Shot and Active Runtime for bundled, managed and
linked packages, including Stop while a request is pending.
`tests/F8.01_hubspot_ui.py` covers the English and French forms at desktop and mobile
widths. They run in the private BloxSmith workspace against a pinned framework; the
initial development version does not imply a release.

## License

Block code: Apache-2.0. HubSpot is an external service governed by its own terms.
No HubSpot SDK, logo, account data or credential is bundled.
