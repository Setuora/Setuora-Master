# ADR-001: Master/Lite Control Plane and Store-and-Forward Synchronization

- Status: Accepted; MVP implemented, production approval pending
- Date: 2026-07-29
- Owners: Setuora engineering and operations
- Scope: `Setuora-Master` and `Setuora-Lite`

## Decision Summary

Setuora will be split into two deployment roles built as modular monoliths:

- **Setuora Lite** runs inside each franchise LAN. It owns franchise operational
  capture, item-label handling, and local inventory. It remains useful during an
  Internet outage.
- **Setuora Master** runs beside, or on the same protected premises as, Tally. It
  ingests franchise events, maintains the cross-franchise monitoring read model,
  coordinates inter-franchise transfers, generates consolidated reports, and is
  the only role allowed to enqueue and post transactions to Tally.

Lite communicates with Master through outbound HTTPS only. Delivery is
at-least-once using a durable Lite outbox and a durable Master event journal.
Stable event identifiers, per-franchise sequence numbers, canonical payload
hashes, and database constraints make retries idempotent.

The two composition roots, Node Sync v1, franchise ownership model, monitoring
console, and two-phase transfer MVP are implemented. The deployment remains a
single-process SQLite pilot: the Internet edge, PostgreSQL migrations,
production credential operations, per-franchise Tally mapping, and operational
failure tests are not complete.

## Context

The inherited application was designed for one LAN server and roughly ten users.
It uses SQLite, server-rendered FastAPI pages, an in-process Tally retry loop, and
a local Caddy certificate. That remains a useful implementation foundation:

- serial-level inventory and transaction history;
- franchise operational capture and item-label handling;
- reports and exports;
- Tally XML generation, a stable Tally `REMOTEID`, and retry history;
- role checks, CSRF origin checks, security headers, and verified SQLite backups.

The new operating model changes the trust and consistency boundary:

- multiple franchise databases can be online, offline, or on different releases;
- a source franchise can dispatch stock while the destination is offline;
- Master is reachable beyond the franchise LANs;
- only Master may talk to Tally;
- serialized-item identifiers must be unique across every franchise;
- monitoring must distinguish the source franchise and current stock owner.

A synchronous request/response design cannot make all franchise databases commit
atomically. The architecture therefore makes partial progress visible and
reconcilable instead of pretending that a distributed transaction is atomic.

## Decision

### 1. Keep two modular monoliths

Master and Lite remain independently deployable FastAPI applications. Each
application has one composition root and one primary database. Shared protocol
schemas may be published as a small versioned package or generated contract, but
neither application imports the other's router layer or database models.

This avoids the operational cost of many services while creating a hard edition
boundary:

| Capability | Lite | Master |
|---|---:|---:|
| Franchise operational capture | Yes | No |
| Item-label generation and printing | Yes | No |
| Local inventory and offline operation | Yes | No |
| Franchise event upload and command polling | Yes | API counterpart |
| Cross-franchise monitoring and reports | Local view | Consolidated view |
| Transfer coordination | Dispatch/receive actions | Authoritative coordinator |
| Tally posting | No | Yes |

Master must not merely hide Lite pages in navigation. Its composition root must
not register franchise operational mutation routes, and edition-boundary tests
must assert that those routes are unavailable.

### 2. Use a durable inbox/outbox protocol

A Lite business transaction and its outbound event are committed in the same
local database transaction. One logical uploader sends contiguous event
sequences to the versioned Node Sync API and retains each immutable row after
Master confirms success.

In the implemented v1 MVP, Master stores the canonical event journal, applies
the inventory/transfer projection, queues any resulting command or Tally batch,
and advances the franchise cursor in one transaction before returning `200`.
A malformed or semantically invalid request advances nothing, and the whole
HTTP batch rolls back. This gives exact idempotency and atomic projection for a
single process, but it does not yet provide a quarantined receive cursor
independent of an apply cursor. A decoupled leased projector may be introduced
for production scale only through a versioned compatibility decision.

Commands use the reverse store-and-forward path. Lite polls Master, applies a
command locally, and posts an idempotent acknowledgement. Master never initiates
an inbound connection to a Lite node.

The normative wire contract is
[`docs/api/node-sync-v1.md`](../api/node-sync-v1.md).

### 3. Make franchise ownership explicit

Master records a stable `franchise_id` on every ingested event. The MVP models
one `FranchiseNode` identity and one ordered stream per franchise installation;
multiple credentials for it share that cursor. Domain ownership and read models
carry franchise foreign keys; free-text warehouse labels are not a tenancy
boundary.

At minimum, the production schema needs durable identities for:

- franchise installation registrations;
- node credentials and rotation history;
- event journal and per-franchise sequence cursor;
- command outbox and acknowledgements;
- products and global serial identities;
- inventory ownership and append-only stock movements;
- transfers and item-level receipts/exceptions;
- Tally company mapping, posting outbox, and posting attempts.

Database constraints, not only application checks, enforce uniqueness and
idempotency.

### 4. Reserve a global serialized-item namespace

Only Lite creates serialized-item identifiers. The implemented format for newly
generated identifiers is:

```text
<FRANCHISE_CODE>-<SERIAL_PREFIX>-<six-digit local sequence>
```

For example:

```text
FR01-SG020-000041
```

`FRANCHISE_CODE` is normalized uppercase and must be uniquely assigned and never
reused. Lite rejects identifier generation when sync is enabled without an
assigned code. Master enforces one global serial identity and rejects a serial
already owned by a different origin; it does not enforce the string grammar.

Manual, imported, replacement, and pre-existing values can bypass the generated
format. A collision scan, explicit registration/import policy, and controls
over manual values are therefore production gates. The label payload remains
only the serial string; it contains no credential or public URL.

### 5. Model transfers as two-phase, partially completable workflows

A transfer is not one cross-database transaction.

1. Source Lite validates and locks stock, commits a dispatch transaction, and
   emits `TRANSFER_DISPATCHED`.
2. Master durably records the dispatch, marks the items `IN_TRANSIT`, and creates
   a `TRANSFER_AVAILABLE` command for destination Lite.
3. Destination Lite polls, receives any physically present subset, commits the
   local receipt, and emits `TRANSFER_RECEIVED`.
4. Master applies each item receipt idempotently and moves ownership only for
   accepted items, then queues `TRANSFER_RECEIPT` for the source.

The visible states are:

```text
DRAFT -> DISPATCHED -> PARTIALLY_RECEIVED -> RECEIVED
             items: IN_TRANSIT
```

Draft editing and multiple partial receipt events are implemented. Dispatched
items do not silently reappear at the source. Missing, rejected, damaged,
cancellation, and reversal workflows still require explicit exception states
and are production follow-up; operators must not repair them by editing stock
history.

### 6. Centralize Tally posting behind a durable queue

Only Master creates Tally posting work. The MVP mirrors eligible franchise
events as `PENDING_SYNC` batches in the existing durable attempt/retry path and
does not call Tally in a Node Sync request. It still reads one active company
configuration.

Production requires each job to be tied to its franchise, mapped Tally company,
source event, and stable idempotency key. Every eligible movement-event mapping
must be validated per company before activation. Inter-franchise movement is a
separate gate: Stock Journal, source/destination Godown, voucher type, and
accounting semantics must be proven against representative Tally data. No
transfer Tally job is implemented; monitoring must not be mistaken for posting.

### 7. Use the public-edge/private-Master deployment boundary

The preferred topology is:

```text
Lite --outbound HTTPS--> public TLS edge VPS
                         --WireGuard--> private Master ingress
                                         -> Uvicorn on loopback
                                         -> database on private/loopback
                                         -> Tally on loopback/private LAN
```

The administrative UI is reachable only through an operator VPN. The public
edge serves only the Node Sync API. Tally port `9000`, the database port, Uvicorn,
and Master maintenance pages are never exposed to the public Internet.

See
[`docs/architecture/master-lite-topology.md`](master-lite-topology.md) and
[`docs/deployment/master-internet-edge.md`](../deployment/master-internet-edge.md).

### 8. Treat SQLite as a bounded pilot exception

SQLite is allowed for a small, single-process Master pilot only when all of these
assumptions remain true:

- no more than 50 registered Lite nodes;
- sustained aggregate ingestion remains below 5 events/second;
- exactly one Master web process and one logical background worker are active;
- backup and restore drills meet the agreed recovery objectives;
- lock waits, ingest latency, and event backlog remain within alert thresholds.

PostgreSQL plus formal versioned migrations is a production gate, even if the
pilot traffic stays below those numbers. A second web replica, a second worker,
or any production Internet rollout also ends the SQLite exception.

## Options Considered

### Shared central database for all franchises

Rejected. It would make Lite dependent on Internet availability, expose the
central database trust boundary to remote sites, and prevent reliable local
operation during outages.

### Master calling inbound Lite APIs

Rejected. Franchise routers and firewalls should not expose Lite, and sleeping or
offline nodes make synchronous orchestration brittle. Command polling preserves
the outbound-only rule.

### Synchronous Lite request directly posting to Tally

Rejected. A browser transaction would inherit WAN and Tally latency, retries could
double-post, and Lite would need Tally credentials and network access. Durable
Master queuing isolates that failure domain.

### Kafka/RabbitMQ and multiple microservices from the first release

Deferred. A broker could help at higher throughput, but it adds deployment,
backup, monitoring, and operator complexity that is not justified by the pilot
assumption. The inbox/outbox tables preserve a migration path to a broker later.

### Direct port forwarding to the Master Windows server

Allowed only as a documented weaker fallback. It removes the separate edge blast
radius and puts the premises host directly on the Internet. It requires explicit
security review, public ACME TLS, firewall verification, administrative VPN
isolation, and rollback approval.

### SQLite for permanent production

Rejected. SQLite's single-writer model and the current in-process workers do not
provide an acceptable long-term concurrency, migration, or failover boundary for
an Internet-facing multi-franchise control plane.

## Consequences

### Benefits

- Lite remains useful when Master, Tally, or the WAN is unavailable.
- No franchise needs an inbound Internet firewall rule.
- Duplicate delivery is safe and traceable.
- Partial transfers and Tally delays are visible instead of hidden.
- Master has one place for franchise monitoring, policy, and Tally audit history.
- The modular-monolith shape keeps the first production deployment operable by a
  small team.

### Costs and risks

- Monitoring is eventually consistent, not instantaneous.
- Conflict and exception reconciliation become explicit product features.
- Two independent release trains require compatibility tests and a supported
  upgrade window.
- Per-node credentials, global namespaces, and clock health require operational
  ownership.
- Master needs production-grade migrations, encrypted backups, metrics, and an
  on-call response for an Internet-facing service.
- The implemented edition split still needs deployment, migration, and
  operational qualification before either edition is production-ready.

## Production Gates

The architecture is not approved for production until all of the following are
demonstrated:

- edition composition tests prove Master has no franchise operational mutations
  and Lite has no Tally posting path;
- Node Sync v1 authentication, rotation, idempotency, sequence conflict, payload
  bounds, and audit tests pass;
- franchise isolation is enforced by database keys and authorization tests;
- PostgreSQL and formal forward/rollback migration procedures are operational;
- the public edge has trusted TLS, body/rate limits, hardened proxy headers, and
  only the approved public port;
- administrative access is VPN-isolated and protected by MFA;
- secrets have least-privilege filesystem ACLs;
- encrypted offsite backup and bare-machine restore drills pass;
- metrics and alerts cover last franchise contact, backlog, processing failures,
  Tally queue, disk, database, backup age, and certificate expiry;
- durable worker leases prevent duplicate processing;
- Stock Journal/Godown behavior is validated in Tally before transfer posting;
- a two-node pilot passes the failure scenarios listed in the deployment guide.

## Revisit Triggers

Re-open this decision when any of these conditions occurs:

- more than 50 active Lite nodes;
- sustained ingestion at or above 5 events/second, or bursts that violate the
  agreed acknowledgement latency;
- a requirement for more than one region or active-active Master;
- more than one web or worker replica;
- command or event backlog exceeds the recovery objective after an outage;
- transfer exception/conflict rate requires automated reconciliation;
- Tally throughput becomes the dominant queue delay;
- a broker, partitioned event log, or independent reporting store becomes
  operationally cheaper than the database inbox/outbox;
- legal, audit, or retention requirements change the data-isolation boundary.

## Validation and Follow-up

Every implementation phase must update this ADR or create a superseding ADR when
it changes a trust boundary, delivery guarantee, data owner, or production gate.
The Node Sync contract receives contract tests in both repositories, and release
qualification must exercise the oldest and newest supported Lite versions
against the candidate Master.
