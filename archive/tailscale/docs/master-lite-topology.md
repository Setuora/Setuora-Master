# Master/Lite Deployment Topology

Status: application MVP and private cross-network deployment implemented;
production scale remains gated.

This document describes the deployment and trust boundaries selected by
[ADR-001](adr-001-master-lite-control-plane.md). Linux and Windows use the same
Docker Compose stack. Tailscale provides private reachability and HTTPS without
an inbound public listener.

## System context

```text
┌──────────────────────── Franchise A LAN ────────────────────────┐
│ Phones/browsers -> Setuora Lite A -> SQLite + durable outbox    │
│                         tag:setuora-lite                         │
└──────────────────────────────┬──────────────────────────────────┘
                               │ outbound encrypted connection
┌──────────────────────── Franchise B LAN ────────────────────────┐
│ Phones/browsers -> Setuora Lite B -> SQLite + durable outbox    │
│                         tag:setuora-lite                         │
└──────────────────────────────┬──────────────────────────────────┘
                               │ outbound encrypted connection
                               v
                    ┌────────────────────────┐
                    │ Private Tailscale net  │
                    │ - device identities    │
                    │ - grants               │
                    │ - MagicDNS + HTTPS     │
                    └───────────┬────────────┘
                                │ tcp:443 to tag:setuora-master
                                v
┌──────────────────────── Master premises ─────────────────────────┐
│ Tailscale Serve -> Setuora Master modular monolith               │
│                    │          │                 │                 │
│                    │          │                 └-> Reports/UI    │
│                    │          └-> durable Tally queue -> Tally   │
│                    └-> central database                          │
│                                                                  │
│ Operator tailnet device -> HTTPS UI -> application login         │
└──────────────────────────────────────────────────────────────────┘
```

Master and Lite make outbound connections. Separate ISPs, dynamic addresses,
NAT, and franchise firewalls therefore need no inbound rules. Tailscale Serve,
not Funnel, exposes Master only inside the authenticated tailnet.

## Responsibilities

### Setuora Lite

Lite is the franchise transaction system:

- provides local operational capture and inventory pages;
- commits an outbound event with each local business transaction;
- uploads events sequentially and retries without user intervention;
- polls and acknowledges durable commands;
- continues local work during a Master or WAN outage under documented rules.

Lite does not accept inbound WAN traffic and does not connect to Tally.

### Tailscale transport

Tailscale:

- admits devices with individual or tagged device identities;
- encrypts traffic between sites;
- provides the private Master DNS name and HTTPS certificate;
- applies grants such as Lite/operator to Master `tcp:443`;
- relays encrypted traffic when direct connectivity is unavailable.

Tailscale is not the business authorization layer. A device admitted as
`tag:setuora-lite` must still present its own Setuora node API credential.

### Setuora Master

Master:

- authenticates franchise installations and durably receives events;
- maintains per-franchise sequence cursors and network stock projections;
- coordinates transfer commands and item-level receipts;
- exposes consolidated reports to authenticated operators;
- persists eligible Tally work and processes it outside upload requests;
- records configuration and administrative changes.

Master never initiates inbound connections to Lite nodes.

### Database and Tally

The database accepts connections only from Master. SQLite is restricted to the
bounded single-process pilot; PostgreSQL and formal migrations remain a
production gate.

Tally remains inside the Master trust boundary. Port `9000` is never published
by Compose or advertised through Tailscale. The Dockerized app reaches Tally
through `host.docker.internal` on a Windows host or an approved private LAN
address.

## Trust boundaries

| Boundary               | Authentication                           | Encryption                  | Authorization                         |
| ---------------------- | ---------------------------------------- | --------------------------- | ------------------------------------- |
| Lite device to tailnet | Tailscale device identity/tag            | WireGuard                   | Grant to Master `tcp:443`             |
| Lite request to Master | Per-installation Setuora credential      | Tailnet HTTPS               | Assigned franchise only               |
| Operator to admin UI   | Tailscale user/device plus Setuora login | Tailnet HTTPS               | Setuora roles and permissions         |
| Master to database     | Local service identity                   | Local/private transport     | Application process only              |
| Master to Tally        | Host/LAN firewall                        | Private transport           | Configured company and access rules   |
| Backup destination     | Operator/backup identity                 | Encrypted storage/transport | Restricted and audited restore access |

Device access and application access are independent. Revoking one does not
automatically revoke the other; incident response rotates both when compromise
is possible.

## Network exposure

| Component              | Direction              |              Port | Source                  | Purpose                        |
| ---------------------- | ---------------------- | ----------------: | ----------------------- | ------------------------------ |
| Master Tailscale Serve | Tailnet inbound        |           TCP 443 | Approved Lite/operators | HTTPS API and authenticated UI |
| Master host loopback   | Local only             |          TCP 8000 | Host health tooling     | Uvicorn health/troubleshooting |
| Tally                  | Local/private only     |          TCP 9000 | Master application      | Tally XML gateway              |
| Database               | Container/private only | database-specific | Master application      | Persistence                    |
| Lite                   | Outbound               |       dynamic/443 | Lite host               | Tailscale and Node Sync        |

There is no public listener, port-forward, or Lite-to-Lite grant. Uvicorn,
Tally, the database, backups, and Docker control endpoints never appear on the
tailnet or Internet.

## Primary data flows

### Transaction upload

1. Lite commits the transaction, stock change, audit data, and outbox event.
2. The uploader sends its oldest frozen event over the private HTTPS URL.
3. Tailscale grants the device a path to Master.
4. Master authenticates the Setuora node credential and validates franchise,
   schema, sequence, and payload limits.
5. Master atomically commits the event, projections, commands, Tally queue work,
   and franchise cursor, then returns `200`.
6. The independent Master worker later processes eligible Tally work.

An event acknowledgement proves the Master projection committed. It does not
prove Tally completion.

### Command polling

1. Lite calls `GET /api/v1/commands?limit=100`.
2. Master returns the oldest unacknowledged commands at least once.
3. Lite commits the local effect and result.
4. Lite acknowledges with `PATCH /api/v1/commands/<command_id>`.
5. Master retains terminal command history for audit.

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

Ownership changes only after Master accepts the destination receipt.

## Failure behavior

| Failure                                 | Required behavior                                                  |
| --------------------------------------- | ------------------------------------------------------------------ |
| Lite loses Internet                     | Local transaction/outbox persist; upload resumes with backoff      |
| Tailscale relay/direct path unavailable | Lite remains local; no outbox row is discarded                     |
| Upload response is lost                 | Lite retries; Master returns the stored idempotent acknowledgement |
| Event sequence has a gap                | Master returns expected sequence; Lite repairs order               |
| Same sequence has different content     | Master returns `409`; no overwrite occurs                          |
| Tally unavailable                       | Events remain projected; Tally work retries independently          |
| Node credential revoked                 | Master rejects sync; Lite retains local data                       |
| Tailscale device revoked                | Network connection fails before reaching Master                    |
| Master database unavailable             | Master cannot acknowledge or buffer events                         |

## Pilot limits and production gates

The SQLite pilot remains limited to 50 nodes, fewer than 5 sustained events per
second, one Master process, and one logical worker. Measure upload latency,
backlogs, SQLite lock waits, franchise last-seen time, Tally queue age, transfer
age, backup age, and disk use.

Tailscale replaces public-ingress infrastructure; it does not remove the gates
for PostgreSQL, formal migrations, shared rate limiting, worker leasing,
monitoring, recovery drills, key operations, or real-company Tally validation.

## References

- [Universal deployment](../deployment/universal-deployment.md)
- [Remote franchise connectivity](../deployment/remote-franchise-connectivity.md)
- [Node Sync v1 contract](../api/node-sync-v1.md)
- [Tailscale policy example](../../deployment/tailscale/policy.hujson.example)
