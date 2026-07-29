# Master/Lite Deployment Topology

Status: application MVP implemented; Internet topology not deployed or approved.

This document describes the deployment and trust boundaries selected by
[ADR-001](adr-001-master-lite-control-plane.md). Master/Lite composition, Node
Sync v1, monitoring, and transfers exist in the repositories. This does not
replace the current LAN installation guides or prove that a public edge is
operational. Installed LAN Caddy still uses an internal certificate, and the
Windows setup still opens ports only to `LocalSubnet`.

## System Context

```text
┌──────────────────────── Franchise A LAN ────────────────────────┐
│ Phones/browsers -> Setuora Lite A -> SQLite + durable outbox    │
└──────────────────────────────┬──────────────────────────────────┘
                               │ outbound HTTPS only
┌──────────────────────── Franchise B LAN ────────────────────────┐
│ Phones/browsers -> Setuora Lite B -> SQLite + durable outbox    │
└──────────────────────────────┬──────────────────────────────────┘
                               │ outbound HTTPS only
                               v
                    ┌────────────────────────┐
                    │ Public TLS edge VPS    │
                    │ - DNS/ACME certificate │
                    │ - API allowlist        │
                    │ - request limits       │
                    │ - access audit         │
                    └───────────┬────────────┘
                                │ WireGuard private address
                                v
┌──────────────────────── Master premises ─────────────────────────┐
│ Private ingress -> Setuora Master modular monolith               │
│                    │          │                 │                 │
│                    │          │                 └-> Reports/UI    │
│                    │          └-> durable Tally queue -> Tally   │
│                    └-> central database                          │
│                                                                  │
│ Operator VPN -> administrative UI                                │
└──────────────────────────────────────────────────────────────────┘
```

The public edge and Master are separate failure and trust domains. Compromise of
the edge must not grant direct access to Tally, the database, Windows
administration, or backup files.

## Container Responsibilities

### Setuora Lite

Lite is the franchise transaction system:

- provides franchise operational capture, item-label handling, and local
  inventory pages;
- stores local business transactions in its own database;
- commits an outbound event with the local business transaction;
- uploads events sequentially and retries without user intervention;
- polls commands and records command acknowledgements;
- locks dispatched transfer items until Master resolves their state;
- continues local operation during a Master or WAN outage within documented
  offline business rules.

Lite does not accept inbound WAN traffic, and Lite mode does not register or run
direct Tally posting.

### Public TLS edge

The edge is an intentionally narrow Internet surface:

- terminates publicly trusted TLS for the Node Sync hostname;
- routes only `/api/v1/node`, `/api/v1/events`, and `/api/v1/commands*` to
  Master;
- rejects unsupported methods, paths, media types, and oversized bodies;
- applies coarse IP controls and the approved request-rate policy;
- writes redacted structured access logs;
- forwards traffic through WireGuard to a fixed Master private address;
- does not expose the administrative UI.

Node identity and authorization remain application responsibilities; a network
tunnel alone is not a franchise credential.

### Setuora Master

Master is the control plane:

- authenticates franchise installations and durably receives events;
- maintains the per-franchise sequence cursor;
- atomically projects accepted events into franchise inventory and monitoring
  read models in the v1 request;
- coordinates transfer commands and item-level receipts;
- exposes reports and operational status to authorized operators;
- shows eligible mirrored batches in a central Tally queue and records posting
  attempts;
- records security, configuration, and administrative changes.

Master does not originate franchise operational transactions and does not
directly open connections to Lite nodes.

### Database

The database is private and accepts connections only from Master. PostgreSQL plus
formal migrations is required for production. SQLite may be used for the bounded
single-process pilot defined in ADR-001, but it must not be used to justify a
multi-process or production rollout.

### Tally connector

The connector runs within the Master trust boundary. Tally port `9000` is bound
to loopback where possible or restricted by host firewall to the Master service
host. The connector consumes persisted pending batches; Node Sync requests never
post to Tally inline. Per-franchise company mapping, durable multi-worker
leasing, and reconciliation remain production gates.

Inter-franchise transfers remain blocked from Tally until Stock Journal and
source/destination Godown mappings have been validated against the real Tally
companies.

## Trust Boundaries

| Boundary | Authentication | Encryption | Authorization |
|---|---|---|---|
| Lite to public edge | Per-installation credential | Public TLS | Installation may act only for its assigned franchise |
| Edge to Master | WireGuard peer keys plus restricted private listener | WireGuard | Edge peer may reach only Master ingress |
| Operator to admin UI | Operator VPN and application login | VPN plus HTTPS | Role/permission checks; MFA is a production gate |
| Master to database | Service credential | Local/private TLS as supported | Dedicated least-privilege database role |
| Master to Tally | Host firewall and Tally configuration | Private HTTP unless Tally supports stronger transport | Per-franchise/company mapping in Master |
| Backup destination | Backup service identity | Encrypted backup artifact and secure transport | Restore access restricted and audited |

No proxy-supplied client identity is trusted unless the request arrived from the
fixed edge tunnel address. The edge must replace, not append blindly to,
forwarded host/protocol/client headers.

## Network Exposure

Preferred public surface:

| Host | Direction | Port | Source | Purpose |
|---|---|---:|---|---|
| Edge VPS | Inbound | TCP 443 | Internet | Node Sync HTTPS |
| Edge VPS | Inbound | UDP 443 | Master/operator VPN peers | WireGuard on the same public port number |
| Master premises | Inbound over WireGuard | TCP 8443 | Edge tunnel IP only | Private reverse-proxy ingress |
| Master premises | Local only | TCP 8000 | Private ingress process | Uvicorn |
| Master premises | Local/private only | TCP 9000 | Master Tally worker | Tally XML gateway |
| Master premises | Local/private only | Database-specific | Master service role | PostgreSQL |
| Lite premises | Outbound | TCP 443 | Lite service | Node Sync |

If UDP 443 cannot be used in the operating environment, choose a reviewed
outbound tunnel that runs over HTTPS. Do not solve that constraint by exposing
Uvicorn, Tally, or the database.

The supplied Caddy edge example disables HTTP/3/QUIC so WireGuard can own UDP
443 while Caddy owns TCP 443 for HTTP/1.1 and HTTP/2. Validate that protocol
split with the pinned Caddy build before rollout.

Port 80 is not required to remain public. ACME must use TLS-ALPN-01 on 443 or an
approved DNS challenge when the edge firewall permits only 443.

## Primary Data Flows

### Transaction upload

1. A Lite user completes a local transaction.
2. Lite commits the transaction, stock change, audit data, and outbox event
   atomically.
3. The uploader sends its oldest frozen event; the API also accepts up to 100
   contiguous events in one request.
4. Master validates node identity, franchise binding, schema, sequence, and
   payload limits.
5. Master commits the event journal, domain/read-model effects, command rows,
   eligible `PENDING_SYNC` batch, and sequence cursor atomically, then returns
   `200`.
6. The existing Tally worker later posts eligible batches and records attempts,
   after the rollout's company mapping is approved.

An event acknowledgement proves the current Master projection committed. It
does not prove Tally completion.

### Command polling

1. Lite calls `GET /api/v1/commands?limit=100`.
2. Master returns the oldest unacknowledged commands at least once.
3. Lite commits the local effect and command result before acknowledging.
4. Lite sends `PATCH /api/v1/commands/<command_id>` with
   `{"acknowledged":true}`.
5. Master retains terminal command history for audit.

The MVP has no command cursor or rejected/deferred acknowledgement status.

### Inter-franchise transfer

```text
Source Lite        Master                         Destination Lite
    | dispatch       |                                  |
    |--------------->| durable IN_TRANSIT               |
    |                 |---- receive command (polled) --->|
    |                 |<--- partial/full receipt event ---|
    |                 | update item ownership/state      |
    |<-- status on next poll/report ---------------------|
```

Master never reports an item as destination stock merely because a receive
command was sent. Ownership changes only after an accepted destination receipt.

## Failure Behavior

| Failure | Required behavior |
|---|---|
| Lite loses Internet | Local transaction and outbox persist; upload resumes with backoff |
| Master/edge unavailable | Lite remains local; no outbox rows are discarded |
| Upload response is lost | Lite retries; Master returns the identical stored `ACCEPTED` acknowledgement without reapplying |
| Event sequence has a gap | Master returns expected sequence; Lite stops later uploads and repairs order |
| Same sequence has different content | Master returns `409`; no automatic overwrite, and Lite blocks later events |
| Destination receives only some items | Transfer becomes `PARTIALLY_RECEIVED`; remaining items stay in transit/exception |
| Tally unavailable | Accepted business events remain projected; Tally jobs retry independently |
| Worker crashes after external Tally post | Stable remote ID and durable lease support safe reconciliation before retry |
| Master database unavailable | Edge returns `503`; it must not acknowledge or buffer unaudited business events |
| Credential revoked | Edge may connect, but Master returns `401`; Lite retains local data and blocks upload behind the failed event |
| Clock skew | Timezone-less event timestamps fail validation; broader skew detection and alerting remain a gate |

## Deployment Variants

### Preferred: edge VPS plus WireGuard

Advantages:

- premises Master has no WAN port-forward;
- public certificate and Internet logs are isolated from Tally;
- the edge can be rebuilt without moving the Master database;
- only a narrow private ingress is reachable over the tunnel.

Costs:

- one additional host and tunnel to monitor;
- edge and Master deployments must coordinate proxy-header trust;
- WireGuard key rotation and recovery need runbooks.

### Weaker fallback: direct public 443 to Master Caddy

This is allowed only when a static public address is available and a security
review accepts the larger blast radius. Requirements include:

- public DNS and ACME certificate; never `tls internal`;
- NAT/firewall exposes only TCP 443 to Caddy;
- Uvicorn remains on `127.0.0.1`;
- admin routes are VPN-restricted separately from the Node Sync path;
- Tally and database ports remain private;
- host patching, endpoint protection, rate/body limits, access logging, and
  external vulnerability checks pass before rollout.

The direct variant does not remove application-level node authentication or any
production gate.

## Capacity and Revisit Points

Pilot assumptions are no more than 50 nodes, less than 5 sustained events/second,
one Master process, and one logical worker. Measure:

- p50/p95/p99 durable event acknowledgement latency;
- event and command backlog age/count;
- database lock/transaction time;
- event rejection and sequence-conflict rate;
- per-franchise last authenticated request age;
- Tally queue age, retry count, and acceptance rate;
- transfer time in each state;
- backup age, restore duration, disk use, and certificate expiry.

Crossing an ADR-001 revisit trigger requires an architecture review before
increasing capacity or adding replicas.

## Production Readiness References

- [Node Sync v1 contract](../api/node-sync-v1.md)
- [Master Internet edge guide](../deployment/master-internet-edge.md)
- [Master edge Caddy example](../../deployment/caddy/Caddyfile.master.example)
- [Lite node synchronization design](../../../Setuora-Lite/docs/architecture/lite-node-sync.md)
- Current local HTTPS guide:
  [`docs/deployment/https-lan-guide.md`](../deployment/https-lan-guide.md)
- Current Windows service:
  [`deployment/windows/install_service.ps1`](../../deployment/windows/install_service.ps1)

The last two references document implemented LAN foundations. They are not an
Internet rollout procedure.
