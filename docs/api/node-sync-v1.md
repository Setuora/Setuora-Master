# Setuora Node Sync API v1

Status: implemented MVP contract; production Internet rollout is not approved.

This is the exact wire contract currently shared by `Setuora-Master` and
`Setuora-Lite`. Lite makes outbound HTTPS requests, uploads its durable ordered
event stream, polls durable Master commands, and acknowledges a command only
after applying it locally. Master never opens a connection to Lite.

The API is usable for a controlled single-process pilot. TLS edge deployment,
credential operations, PostgreSQL migration, distributed rate limiting, and
failure/soak testing remain production gates. See
[ADR-001](../architecture/adr-001-master-lite-control-plane.md) and the
[remote-connectivity runbook](../deployment/remote-franchise-connectivity.md).

## Contract Summary

- Base path: `/api/v1`
- Media type sent by Lite: `application/json`
- Authentication: one Bearer node API key per franchise installation
- Delivery: at least once
- Ordering: strictly increasing, gap-free sequence per authenticated franchise
- Event request: atomic; either all new events and domain effects commit or none
- Event success: `200 OK` after the events and their current Master-side effects
  commit
- Command delivery: at least once until acknowledged
- Schema version: integer `1` in every event
- Unknown JSON fields: rejected

An accepted event proves that Master committed the event journal, inventory or
transfer effect, and any resulting command or mirrored batch in one database
transaction. It does **not** prove that Tally accepted a voucher.

## Transport and Authentication

Production and remote pilot traffic must use HTTPS with normal hostname and
certificate verification. Lite refuses to start enabled synchronization unless
`MASTER_URL` begins with `https://`. The supported deployment uses Tailscale
Serve to terminate a certificate for Master's private `*.ts.net` name. Tailscale
Funnel is not enabled.

Every request uses:

```http
Authorization: Bearer setuora-node.<key_id>.<secret>
Accept: application/json
```

Requests with a JSON body also use:

```http
Content-Type: application/json
```

The application currently validates the JSON bytes rather than rejecting on
media type alone. Lite must still send `application/json` for event uploads and
command acknowledgements.

Master generates `key_id` and `secret` with cryptographically secure random
Base64URL values. It returns the complete key once and stores only the SHA-256
digest of the secret. A credential is bound to one `FranchiseNode`; payload
fields cannot select or override that franchise.

The implemented administrative workflow can:

- enroll an active franchise and issue its first key;
- rotate a key, which immediately revokes every prior active key for that
  franchise;
- deactivate a franchise, which also revokes its active credentials.

The current rotation has no overlap window. Coordinate the Lite configuration
change as a short maintenance action. A safer tested successor-key overlap,
expiry policy, credential audit export, and restore/revocation procedure remain
production gates.

Authentication failures use these codes:

| HTTP | Code                 | Meaning                                              |
| ---: | -------------------- | ---------------------------------------------------- |
|  401 | `AUTH_REQUIRED`      | Missing or non-Bearer Authorization header           |
|  401 | `INVALID_CREDENTIAL` | Malformed key, unknown key ID, or wrong secret       |
|  401 | `CREDENTIAL_REVOKED` | Credential was revoked                               |
|  401 | `CREDENTIAL_EXPIRED` | Optional expiry has passed                           |
|  403 | `NODE_INACTIVE`      | Credential is valid but its franchise is inactive    |
|  429 | `RATE_LIMITED`       | Credential exceeded the configured application limit |

`401` responses include `WWW-Authenticate: Bearer`.

The default application limiter is 120 requests per 60 seconds per credential.
It is in-process memory: it resets on restart and is not shared by multiple
processes. One Master process is therefore an MVP constraint; a distributed or
edge-enforced policy is a production gate.

## Common Response Envelope

Every application-generated Node Sync response, including errors, has exactly
these top-level fields:

```json
{
  "data": {},
  "error": null,
  "request_id": "0190f73c-23ec-7ab8-8e2d-05db49fca1f0"
}
```

On error, `data` is `null`:

```json
{
  "data": null,
  "error": {
    "code": "SEQUENCE_GAP",
    "message": "Expected sequence 41, received 43.",
    "details": {
      "expected_sequence": 41
    }
  },
  "request_id": "0190f73c-23ec-7ab8-8e2d-05db49fca1f0"
}
```

`error.details` is present only when useful. The same request ID is returned in
the `X-Request-ID` response header. A client may supply `X-Request-ID`; Master
uses at most its first 128 characters. Otherwise Master generates a UUID.

Request ID is for correlation, not event idempotency.

## Upload Events

### `POST /api/v1/events`

The request object contains only `events`:

```json
{
  "events": [
    {
      "event_id": "7b4a9d62-1322-4ff3-b23f-8815c005c557",
      "sequence": 41,
      "schema_version": 1,
      "type": "SALE",
      "occurred_at": "2026-07-29T10:15:30+00:00",
      "reference": "SAL-FR01-20260729-0007",
      "actor": "sales.operator",
      "items": [
        {
          "serial_number": "FR01-SG020-000041",
          "product_code": "SG020",
          "product_name": "Example Product",
          "tally_stock_item_name": "Example Product 100 g",
          "hsn": "21069099",
          "gst_rate": 5.0,
          "unit": "Pcs",
          "rate": 1000.0,
          "status": "IN_STOCK",
          "product_batch_number": "B2407",
          "mfg_date": "2026-06-01",
          "expiry_date": "2027-06-01",
          "warehouse": "MAIN"
        }
      ],
      "party_name": "Example Customer",
      "party_state": "Karnataka",
      "party_gst_registration_type": "Regular",
      "party_gst_name": "Example Customer",
      "party_gstin": "29ABCDE1234F1Z5",
      "gst_treatment": "INTRASTATE",
      "gst_cgst_rate": 2.5,
      "gst_sgst_rate": 2.5,
      "gst_igst_rate": 0.0,
      "reason_code": null,
      "destination_franchise_code": null,
      "transfer_id": null
    }
  ]
}
```

Optional fields may be omitted or set to `null`. `items` defaults to an empty
array, which is valid only for `HEARTBEAT`.

### Request bounds

Defaults are configurable on Master and must remain aligned with the edge:

| Bound                          |                 Default |
| ------------------------------ | ----------------------: |
| Encoded request body           | 5,242,880 bytes (5 MiB) |
| Events per request             |                1 to 100 |
| Items in one event             |              0 to 5,000 |
| Total items across the request |                   5,000 |

`Content-Length` is required for this endpoint. A missing header returns
`411 CONTENT_LENGTH_REQUIRED`, a non-integer returns
`400 INVALID_CONTENT_LENGTH`, and a value below one returns
`400 BODY_REQUIRED`. Middleware checks the declared length and the router also
checks the bytes actually read. Oversize bodies return `413 BODY_TOO_LARGE`;
excessive item counts return `413 TOO_MANY_ITEMS`.

Lite's current worker intentionally uploads one durable event per request even
though the server accepts a larger batch. The headers
`X-Setuora-Event-Id`, `X-Setuora-Event-Hash`, and `X-Setuora-Sequence` are sent
for diagnostics, but the current Master contract derives identity and the
canonical SHA-256 hash from the validated JSON body. Those headers are not
authentication or idempotency inputs.

### Event fields

| Field                         | Required      | Constraint                                          |
| ----------------------------- | ------------- | --------------------------------------------------- |
| `event_id`                    | yes           | UUID; immutable idempotency identity                |
| `sequence`                    | yes           | integer `>= 1`; next contiguous franchise sequence  |
| `schema_version`              | no            | defaults to and must equal `1`                      |
| `type`                        | yes           | one of the event types below                        |
| `occurred_at`                 | yes           | ISO 8601 datetime with an explicit UTC offset       |
| `reference`                   | no            | string, at most 180 characters                      |
| `actor`                       | no            | string, at most 120 characters                      |
| `items`                       | conditionally | array; shape below                                  |
| `party_name`                  | no            | string, at most 180 characters                      |
| `party_state`                 | no            | string, at most 80 characters                       |
| `party_gst_registration_type` | no            | string, at most 40 characters                       |
| `party_gst_name`              | no            | string, at most 180 characters                      |
| `party_gstin`                 | no            | string, at most 20 characters                       |
| `gst_treatment`               | no            | string, at most 40 characters                       |
| `gst_cgst_rate`               | no            | number from 0 through 100                           |
| `gst_sgst_rate`               | no            | number from 0 through 100                           |
| `gst_igst_rate`               | no            | number from 0 through 100                           |
| `reason_code`                 | no            | string, at most 80 characters                       |
| `destination_franchise_code`  | dispatch only | string, at most 40 characters; normalized uppercase |
| `transfer_id`                 | receipt only  | UUID                                                |

Strings are trimmed. Unknown fields are rejected.

### Item fields

Every non-heartbeat event requires at least one item. A serial may appear only
once within an event.

| Field                   | Required | Constraint                                                    |
| ----------------------- | -------- | ------------------------------------------------------------- |
| `serial_number`         | yes      | non-empty string, at most 140 characters                      |
| `product_code`          | yes      | non-empty string, at most 80 characters; normalized uppercase |
| `product_name`          | yes      | non-empty string, at most 180 characters                      |
| `tally_stock_item_name` | yes      | non-empty string, at most 180 characters                      |
| `hsn`                   | yes      | string, at most 40 characters                                 |
| `gst_rate`              | yes      | number from 0 through 100                                     |
| `unit`                  | yes      | non-empty string, at most 40 characters                       |
| `rate`                  | yes      | number `>= 0`                                                 |
| `status`                | yes      | non-empty string, at most 40 characters; normalized uppercase |
| `product_batch_number`  | no       | string, at most 80 characters                                 |
| `mfg_date`              | no       | ISO date                                                      |
| `expiry_date`           | no       | ISO date; cannot precede `mfg_date`                           |
| `warehouse`             | no       | string, at most 80 characters                                 |

Money, rates, quantities, and GST values are JSON numbers in this MVP schema.
Each serialized item represents one QR/serial and quantity one.

### Event types and committed effects

| Type                  | Current Master effect                                                                                                                                                        |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `STOCK_SNAPSHOT`      | Enrolls new `GENERATED`/`IN_STOCK` serials or updates metadata for an owned serial at the same authoritative status; `QR_REPLACEMENT` is the only snapshot status transition |
| `PURCHASE`            | Moves `GENERATED`/`PURCHASE_RETURN` to `IN_STOCK`; creates a mirrored `PENDING_SYNC` batch                                                                                   |
| `RECEIVE`             | Same stock transition and Tally eligibility as `PURCHASE`                                                                                                                    |
| `SALE`                | Moves available stock to `SOLD`; creates a mirrored `PENDING_SYNC` batch                                                                                                     |
| `SALES_RETURN`        | Moves `SOLD` to `IN_STOCK`, or `DAMAGED` for reason `DAMAGED`/`EXPIRED`; creates a mirrored `PENDING_SYNC` batch                                                             |
| `PURCHASE_RETURN`     | Moves available stock to `PURCHASE_RETURN`; mirrored batch is currently `CLOSED`                                                                                             |
| `ISSUE`               | Moves `IN_STOCK` to `ISSUED`; mirrored batch is currently `CLOSED`                                                                                                           |
| `AUDIT`               | Records an audit transaction without changing the authoritative state; mirrored batch is `CLOSED`                                                                            |
| `TRANSFER_DISPATCHED` | Validates source ownership and destination, moves items to `IN_TRANSIT`, creates the transfer, and queues `TRANSFER_AVAILABLE`                                               |
| `TRANSFER_RECEIVED`   | Accepts a destination subset, transfers ownership, sets `PARTIALLY_RECEIVED` or `RECEIVED`, and queues `TRANSFER_RECEIPT`                                                    |
| `HEARTBEAT`           | Advances sequence and last-seen time; `items` must be empty                                                                                                                  |

`STOCK_SNAPSHOT` is additive for the listed serials. New serials may be enrolled
only as `GENERATED` or `IN_STOCK`. Existing serials must remain at Master's
authoritative status; the event may update batch/date/warehouse metadata but
cannot resurrect `SOLD`, `ISSUED`, damaged, invalid, or in-transit stock.
`reason_code=QR_REPLACEMENT` is a separately validated two-item operation: one
owned replaceable serial becomes `INVALID`, one globally unknown replacement
inherits its product and origin, and both history rows are recorded atomically.
Product identity is checked against the origin franchise.

Omitting a serial does not delete it, mark it missing, or relinquish ownership.
Lite provides a one-time, pre-first-event baseline for active `GENERATED` and
`IN_STOCK` serials, chunked at 5,000 items with
`reason_code=INITIAL_ENROLLMENT`. It is not a historical transaction backfill.
The MVP does not run an automatic full-database snapshot or periodic heartbeat
producer; submitted QR assignments create targeted snapshots, and normal
authenticated requests update last-seen time.

`TRANSFER_DISPATCHED` requires `destination_franchise_code` and `reference`.
`TRANSFER_RECEIVED` requires `transfer_id`. All other non-heartbeat events also
require at least one item.

`PURCHASE`, `RECEIVE`, `SALE`, and `SALES_RETURN` are placed in the existing
Master Tally retry queue as `PENDING_SYNC`; no Node Sync HTTP request calls Tally
inline. Multi-franchise company mapping and real-company XML validation are
still rollout gates, so a `PENDING_SYNC` result is not permission to enable
production Tally posting.

### Sequence and idempotency rules

Master authenticates the franchise first, then enforces:

- global uniqueness of `event_id`;
- uniqueness of `(franchise, sequence)`;
- the next new sequence must equal `last_sequence + 1`;
- a previously accepted `event_id` must have the identical canonical validated
  body and belong to the same franchise.

Master stores a canonical JSON representation of the validated event and its
SHA-256 hash. Retrying the same event returns its original acknowledgement. The
acknowledgement still says `ACCEPTED`; v1 does not emit a separate `DUPLICATE`
status.

Conflicts return:

| HTTP | Code                        | Condition                                 |
| ---: | --------------------------- | ----------------------------------------- |
|  403 | `EVENT_FORBIDDEN`           | Event ID belongs to another franchise     |
|  409 | `EVENT_ID_CONFLICT`         | Same event ID, different canonical body   |
|  409 | `SEQUENCE_CONFLICT`         | Sequence already belongs to another event |
|  409 | `SEQUENCE_GAP`              | New sequence is greater than expected     |
|  409 | `SEQUENCE_STALE`            | New sequence is lower than expected       |
|  409 | `CONCURRENT_EVENT_CONFLICT` | Concurrent writer changed the stream      |

Gap/stale errors include `details.expected_sequence`. A batch may contain an
identical duplicate prefix followed by new contiguous events. Any validation,
ordering, ownership, or domain failure rolls back every new event and effect in
that HTTP request.

### Success response

```json
{
  "data": {
    "acknowledgements": [
      {
        "event_id": "7b4a9d62-1322-4ff3-b23f-8815c005c557",
        "sequence": 41,
        "type": "SALE",
        "status": "ACCEPTED",
        "received_at": "2026-07-29T10:15:31.120000+00:00",
        "result": {
          "batch_id": 812,
          "batch_number": "NET-FR01-000000000041",
          "batch_status": "PENDING_SYNC",
          "item_count": 1
        }
      }
    ],
    "last_sequence": 41
  },
  "error": null,
  "request_id": "0190f73c-23ec-7ab8-8e2d-05db49fca1f0"
}
```

`result` depends on event type. It is useful for audit but is not a replacement
for consolidated report or Tally status APIs.

## Inspect Authenticated Node State

### `GET /api/v1/node`

This endpoint identifies the franchise bound to the supplied credential and
reports its current stream cursor:

```json
{
  "data": {
    "public_id": "8242df08-a2fe-4b58-a436-35284347b05f",
    "code": "FR01",
    "name": "EXAMPLE FRANCHISE",
    "location": "BENGALURU",
    "active": true,
    "tally_godown_name": "FR01 GODOWN",
    "last_sequence": 41,
    "next_sequence": 42,
    "last_seen_at": "2026-07-29T10:16:00.000000+00:00"
  },
  "error": null,
  "request_id": "6e41ca60-16da-4087-ae40-fb25e64332e7"
}
```

This is the v1 cursor-recovery endpoint. There is no `/state` endpoint in the
implemented contract. Lite's MVP uploader preserves strict order and blocks
behind its oldest failed event; automated gap repair after database restore is a
remaining acceptance gate.

## Poll Commands

### `GET /api/v1/commands?limit=100`

`limit` is optional and must be an integer from 1 through 100. Master returns the
oldest unacknowledged commands for the authenticated franchise:

```json
{
  "data": {
    "commands": [
      {
        "command_id": "96f97a77-c724-4940-a6eb-cf3c2bbf0634",
        "type": "TRANSFER_AVAILABLE",
        "payload": {
          "transfer": {
            "transfer_uuid": "da5ca7b5-4de9-447f-a021-6206bff33ff7",
            "source_franchise_code": "FR01",
            "destination_franchise_code": "FR02",
            "status": "DISPATCHED",
            "dispatched_at": "2026-07-29T11:00:00+00:00",
            "notes": "da5ca7b5-4de9-447f-a021-6206bff33ff7"
          },
          "items": [
            {
              "manifest_serial_number": "FR01-SG020-000041",
              "product": {
                "product_code": "SG020",
                "product_name": "Example Product",
                "hsn": "21069099",
                "gst_rate": 5.0,
                "unit": "Pcs",
                "default_rate": 1000.0,
                "tally_stock_item_name": "Example Product 100 g"
              },
              "serial": {
                "serial_number": "FR01-SG020-000041",
                "status": "IN_STOCK",
                "product_batch_number": "B2407",
                "mfg_date": "2026-06-01",
                "expiry_date": "2027-06-01",
                "warehouse": "MAIN"
              }
            }
          ]
        },
        "created_at": "2026-07-29T11:00:01.000000+00:00",
        "acknowledged_at": null
      }
    ]
  },
  "error": null,
  "request_id": "fe55bcf1-f797-4efc-83b9-234f5764c653"
}
```

Current command types are:

- `TRANSFER_AVAILABLE`: destination Lite creates an inbound transfer task and
  materializes manifest serials with `IN_TRANSIT` status, so they are not
  saleable;
- `TRANSFER_RECEIPT`: source Lite records a partial/full destination receipt and
  makes received source serial rows inactive. If the same physical QR later
  returns through another Master-authorized transfer, Lite may reactivate only
  an inactive `IN_TRANSIT` row tied to completed outbound history and an
  identical product identity.

There is no cursor parameter in v1. Unacknowledged commands are redelivered in
database order. Lite stores each command by `command_id`, verifies an identical
payload on redelivery, applies it transactionally, and only then acknowledges
it. Unsupported or locally invalid commands remain unacknowledged and visible
as failures; v1 has no rejected/deferred acknowledgement body.

## Acknowledge a Command

### `PATCH /api/v1/commands/{command_id}`

Recommended request:

```json
{
  "acknowledged": true
}
```

The application also accepts an empty body. When a body is provided it may
contain only `acknowledged`, whose value must be `true`.

Success returns the command with `acknowledged_at` populated:

```json
{
  "data": {
    "command": {
      "command_id": "96f97a77-c724-4940-a6eb-cf3c2bbf0634",
      "type": "TRANSFER_AVAILABLE",
      "payload": {},
      "created_at": "2026-07-29T11:00:01.000000+00:00",
      "acknowledged_at": "2026-07-29T11:00:05.000000+00:00"
    }
  },
  "error": null,
  "request_id": "a3ee3707-aeed-46da-9942-d53d111ed8e8"
}
```

Acknowledging the same command again is idempotent and preserves the first
acknowledgement time.

| HTTP | Code                 | Condition                                |
| ---: | -------------------- | ---------------------------------------- |
|  403 | `COMMAND_FORBIDDEN`  | Command belongs to a different franchise |
|  404 | `COMMAND_NOT_FOUND`  | UUID is valid but command does not exist |
|  422 | `INVALID_IDENTIFIER` | Path value is not a UUID                 |
|  422 | `VALIDATION_ERROR`   | Acknowledgement body is invalid          |

## Validation and Domain Errors

Malformed strict JSON returns `422 VALIDATION_ERROR` with a list of field,
message, and validation type entries. Domain processing may also return:

- `INVALID_STOCK_STATUS`
- `STOCK_UNKNOWN`
- `STOCK_OWNERSHIP_CONFLICT`
- `STOCK_NOT_AVAILABLE`
- `INVALID_STOCK_TRANSITION`
- `DESTINATION_NOT_FOUND`
- `INVALID_TRANSFER`
- `TRANSFER_REFERENCE_CONFLICT`
- `TRANSFER_ID_CONFLICT`
- `TRANSFER_NOT_FOUND`
- `TRANSFER_FORBIDDEN`
- `TRANSFER_ALREADY_RECEIVED`
- `TRANSFER_ITEM_CONFLICT`
- `TRANSFER_ITEM_ALREADY_RECEIVED`
- `TRANSFER_STOCK_CONFLICT`
- `UNSUPPORTED_EVENT`

Domain conflicts normally use `409`; missing transfer/command rows use `404`;
invalid destinations or event values use `422`; cross-franchise access uses
`403`. Unexpected failures return `500 INTERNAL_ERROR` with no exception detail.

## Lite Retry Rules in the MVP

Lite freezes the canonical `{ "events": [...] }` request and SHA-256 at local
commit time. Its SQLite AUTOINCREMENT outbox ID is the event sequence.

The current worker:

1. selects the oldest event that is not `SENT`;
2. sends that one event;
3. validates that a 2xx response acknowledges the exact event UUID and
   sequence and advances the Master cursor, then marks it `SENT`;
4. stops at the first blocked event, so later sequences cannot overtake it;
5. recovers a stale `SENDING` row after at least 60 seconds;
6. retries `408`, `425`, `429`, 5xx, network failures, credential responses
   `401`/`403`, and ordering responses `409`/`412`;
7. waits 60 seconds for ordering conflicts and uses an exponential delay capped
   at 2,048 seconds for other retryable failures;
8. leaves validation/body failures (`400`, `411`, `413`, `415`, `422`) and
   other non-retryable 4xx responses stopped for operator correction.

The worker validates the acknowledgement body before advancing. It still does
not honor `Retry-After` or add jitter. An admin can integrity-check and
reschedule the same frozen event, but cannot edit or skip it; cursor-assisted
restore recovery, invalid-business-event correction/cancellation, and durable
multi-process worker leases remain acceptance or production gates.

## Compatibility

Breaking wire changes require a new base path such as `/api/v2`. Additive event
types or optional fields still require coordinated Master/Lite contract tests
because all current v1 models reject unknown fields. A deployed Lite must not
send a schema other than `1`.

Before upgrading either side, test:

- a non-empty Lite outbox;
- duplicate delivery after a lost response;
- sequence gap and conflict behavior;
- an unacknowledged command already applied locally;
- partial transfer receipt;
- credential revocation;
- rollback with the existing SQLite schema.

## Production Gaps

The implemented API is not, by itself, approval to expose Master to the
Internet. Production still requires:

- reviewed Tailscale tags/grants, HTTPS, device lifecycle, and denial tests;
- PostgreSQL and formal migrations;
- tested credential provisioning, successor overlap, expiry, audit, and
  emergency revocation procedures;
- a shared or edge rate limiter and abuse monitoring;
- distributed worker leases before adding any process or worker replica;
- acceptance of the one-time available-stock baseline, pre-existing serial collision
  handling, historical sold/issued migration where required, and an explicit
  empty-baseline enrollment option;
- automated restore/cursor reconciliation and replay tests;
- validated destination selection plus cancel/correction recovery for a
  rejected dispatch event;
- penetration, load, outage, lost-response, and two-node transfer testing;
- per-franchise Tally company/Godown mapping and real-company voucher validation;
- redacted structured logs, metrics, alerts, encrypted offsite backups, and a
  clean-host restore drill.
