# Master Internet Edge Deployment

Status: application MVP exists; edge deployment and production approval pending.

Do not expose Setuora to the Internet by following the existing LAN guide. The
implemented application has a Master-only composition and Node Sync v1, but the
LAN setup:

- writes a Caddy site with `tls internal`;
- permits Windows ports 80/443 only from `LocalSubnet`;
- does not deploy the public edge, WireGuard, PostgreSQL, or operator VPN;
- has not passed the production security and two-node acceptance gates below.

Treat this as an infrastructure design and acceptance runbook. Replace every
example value, implement the missing controls, and obtain an independent review
before opening public traffic.

## Selected Topology

```text
Lite nodes
  -> outbound https://sync.example.com:443
  -> public Caddy edge VPS
  -> WireGuard private route
  -> Master private ingress (10.66.0.2:8443)
  -> Uvicorn (127.0.0.1:8000)

Operator
  -> operator VPN
  -> Master administrative UI

Master worker
  -> local/private PostgreSQL
  -> Tally 127.0.0.1:9000
```

The edge publishes only the Node Sync API. It must not proxy the Master
administrative surface, OpenAPI, static files, or arbitrary paths.

The edge public port number is 443:

- TCP 443: HTTPS;
- UDP 443: WireGuard when the edge is the tunnel endpoint.

The supplied Caddy example disables HTTP/3/QUIC to prevent Caddy from competing
with WireGuard for UDP 443. HTTPS continues over HTTP/1.1 and HTTP/2 on TCP 443.
Validate this split with the pinned Caddy build.

No other edge port remains publicly open. ACME therefore uses TLS-ALPN-01 on TCP
443 or an approved DNS challenge. If the environment cannot use UDP 443, select
a reviewed outbound HTTPS tunnel rather than opening Master services directly.

## Assumptions

- Master and Tally run on protected premises with supported operating systems.
- Lite nodes have outbound DNS, time synchronization, and TCP 443.
- The public DNS name is dedicated to Node Sync.
- Operators have a separate VPN path to the administration UI.
- The pilot is at most 50 nodes, below 5 sustained events/second, one Master web
  process, and one logical worker.
- PostgreSQL and formal migrations replace SQLite before production.
- A named operator owns edge, VPN, certificate, credential, backup, and incident
  response duties.

Breaking any assumption requires review against
[ADR-001](../architecture/adr-001-master-lite-control-plane.md).

## Production Gates

### Application boundary

- Master composition registers only monitoring, sync, reporting, and Tally
  control-plane routes; Lite mutation routes are absent.
- The Node Sync v1 endpoints and per-franchise authorization remain covered by
  contract tests.
- Browser sessions cannot authenticate to Node Sync; node keys cannot authenticate
  to browser/admin routes.
- Master trusts forwarded headers only from the fixed private edge address.
- FastAPI OpenAPI/docs are disabled in production or restricted to the operator
  VPN.
- Health endpoints reveal no secrets or business data.

### Data and workers

- Every event/domain record is associated with an enforced franchise identity.
- PostgreSQL is bound to loopback/private interfaces and uses a least-privilege
  service role.
- Formal versioned migrations have upgrade, rollback/forward-fix, and backup
  procedures.
- Event ingestion and commands retain database idempotency constraints; any
  decoupled projection or Tally workers use durable leases.
- Run one logical worker until database leases and duplicate-worker tests pass.
- Tally Stock Journal/Godown mapping for transfers is separately implemented and
  validated; no transfer Tally job exists in the MVP.

### Internet and identity

- Public DNS resolves only to the edge.
- A publicly trusted ACME certificate renews without opening a new permanent port.
- TLS 1.2+ is enforced; plaintext and invalid certificates fail closed.
- Node credentials are unique, revocable, rotated, and redacted from every log.
- Body, media type, method, path, and per-credential rate limits match the API
  contract.
- Administrative access is VPN-restricted and MFA-protected.
- Edge-to-Master access is limited by WireGuard peer and Master firewall to the
  single private ingress port.
- An external scan confirms that Tally, database, Uvicorn, SMB/RDP, and
  administrative HTTP routes are not publicly reachable.

### Secrets and host security

- `.env`, node-verifier material, database credentials, backup keys, WireGuard
  keys, and Caddy state are readable only by their service identity and approved
  administrators.
- Bootstrap credentials are removed or disabled after first use.
- Windows/Linux host patching, endpoint protection, disk encryption, and clock
  synchronization are monitored.
- Tally port `9000` is loopback-bound where possible or firewall-restricted to the
  Master service host.
- Uvicorn remains bound to `127.0.0.1`; it is never a WAN listener.

### Recovery and observability

- Database and configuration backups are encrypted before leaving Master.
- At least one backup copy is offsite and protected from the Master service
  account.
- A clean-host restore, credential recovery, WireGuard recovery, and Caddy
  certificate recovery drill has passed.
- Recovery point and recovery time objectives are recorded and measured.
- Alerts cover edge/API availability, authentication failures, last franchise
  contact, event rejection/backlog, command backlog, transfer exceptions,
  Tally queue age/failures, database health, disk, backup age, certificate expiry,
  and clock skew.
- Logs are structured, redacted, time-synchronized, access-controlled, and have a
  retention policy.

## Edge Preparation

1. Provision a minimal supported VPS with a static public address.
2. Create a dedicated DNS record such as `sync.example.com`.
3. Deny inbound traffic by default.
4. Allow TCP 443 for HTTPS and, when selected, UDP 443 for WireGuard.
5. Deny public SSH/RDP or restrict management to an operator VPN and named keys.
6. Install Caddy from a verified package source and pin the reviewed major/minor
   release. The example's `request_body` directive requires Caddy v2.10.0 or
   newer and remains an experimental Caddy feature, so validate it with the
   exact pinned build; check the
   [official directive documentation](https://caddyserver.com/docs/caddyfile/directives/request_body)
   during that review.
7. Store Caddy configuration and state outside the application checkout with
   least-privilege ownership.
8. Enable OS and Caddy security updates through a staged maintenance process.
9. Configure a WireGuard peer whose routes contain only the Master private ingress
   subnet, not the entire premises LAN.
10. Send edge logs and alerts to a destination that an edge compromise cannot
    silently erase.

Use
[`deployment/caddy/Caddyfile.master.example`](../../deployment/caddy/Caddyfile.master.example)
as a reviewed starting point. Replace every `.invalid` name and example address.
The file deliberately does not invent a stock-Caddy rate-limit directive; deploy
an approved Caddy module, a separate limiter, or application enforcement before
production.

## WireGuard Boundary

Example address plan:

```text
Edge WireGuard:   10.66.0.1/32
Master WireGuard: 10.66.0.2/32
Master ingress:   10.66.0.2:8443
```

Requirements:

- Master initiates/maintains the tunnel to `edge:443/udp`;
- `PersistentKeepalive` is set only when NAT traversal requires it;
- peer `AllowedIPs` are minimal `/32` routes;
- edge may route only to `10.66.0.2:8443`;
- Master firewall accepts that port only from `10.66.0.1`;
- no route from the edge reaches Tally, PostgreSQL, RDP, SMB, or the wider LAN;
- peer keys are unique and rotated under a documented break-glass procedure;
- tunnel-up is not treated as application health.

The private ingress can be a dedicated local reverse proxy that binds
`10.66.0.2:8443` and forwards to `127.0.0.1:8000`. It must preserve the public
Host value expected by `TRUSTED_HOSTS` and accept traffic only from the edge peer.

## Proxy Header Rules

At the edge:

- discard client-supplied `Forwarded`, `X-Forwarded-For`,
  `X-Forwarded-Host`, and `X-Forwarded-Proto`;
- generate fresh forwarded headers;
- preserve a validated `X-Request-Id`, or replace it with a UUID when absent or
  malformed;
- never forward Authorization to logs or error pages.

At Master:

- accept forwarded headers only from `10.66.0.1`;
- validate Host against the dedicated sync hostname;
- derive HTTPS origin only from the trusted edge;
- reject direct requests to the private ingress from any other tunnel peer.

Do not configure a global "trust all proxies" mode.

## Request and Rate Controls

The edge rejects:

- paths outside the exact `/api/v1/node`, `/api/v1/events`,
  `/api/v1/commands`, and `/api/v1/commands/<UUID>` surface;
- unsupported HTTP methods;
- non-JSON request bodies;
- any `Content-Encoding` on Node Sync requests;
- event requests without `Content-Length`;
- request bodies over 5,242,880 bytes;
- malformed Host and request IDs.

The application enforces the authenticated per-credential default of 120
requests per rolling 60 seconds and the body/item limits from the
[Node Sync v1 contract](../api/node-sync-v1.md). Its limiter is process-local,
so production also needs a reviewed shared or edge limiter. Edge IP throttling
is only coarse abuse control because carrier NAT can put many franchises behind
one IP.

Test the limiter for:

- multiple legitimate nodes behind one address;
- a revoked key retry loop;
- large-body slow uploads;
- burst retries after an outage;
- bypass attempts using spoofed forwarded headers.

## Administrative Isolation

Use a separate private hostname, for example `master-admin.internal`, reachable
only after operator VPN authentication. It must not resolve publicly to the edge
Node Sync virtual host.

Production approval requires:

- MFA for super-admin and operational accounts;
- named accounts, no shared admin login;
- shorter privileged sessions and invalidation after credential changes;
- rate limits by account and source, without allowing trivial account-lockout
  denial of service;
- administrative functions restricted to the private hostname;
- recovery tooling kept outside the browser application and available only to
  named operators;
- audit alerts for credential, role, recovery, Tally, and node registration
  changes.

## Database and Backup Deployment

PostgreSQL production requirements:

- database listens only on loopback/private Master;
- dedicated application, migration, backup, and monitoring roles;
- the runtime application role cannot alter schema;
- TLS is used if the connection leaves the host;
- connection pool and statement/lock timeouts are explicit;
- migrations run as a controlled release step, not concurrently in every web
  worker;
- backup includes database, migration version, non-secret deployment manifest,
  and separately protected configuration/keys;
- encrypted offsite copies support point-in-time objectives where required.

SQLite may be used only in the bounded pilot. For that exception:

- exactly one Master web process and one worker;
- verified SQLite backup API includes WAL state;
- lock wait and event acknowledgement latency are alerted;
- no assumption is made that a SQLite backup procedure is the final PostgreSQL
  runbook.

## Tally Boundary

- Tally remains private and is never addressed through the public edge.
- Master must store and enforce a reviewed franchise-to-Tally-company mapping;
  the MVP still has one active global company configuration.
- A received event may project successfully while its Tally job is pending,
  failed, or blocked.
- Stable remote IDs and recorded request/response metadata are used for
  reconciliation before retry after an ambiguous timeout.
- Raw Tally XML is restricted to privileged private UI/log storage and never
  returned to Lite.
- Transfers are not posted until real Stock Journal/Godown tests prove the
  voucher and inventory outcome.

## Observability Baseline

Minimum dashboards:

- requests, latency, status, bytes, and limiter outcomes at the edge;
- successful/failed authentication by franchise/key ID without secret material;
- last authenticated contact by franchise;
- franchise sequence cursor, oldest Lite outbox age, and event rejection;
- command delivery/ACK latency and unacknowledged/failed commands;
- transfers by state and oldest partial/exception;
- Tally queue depth/age/retry and acceptance;
- database latency/connections/locks/storage;
- worker last successful cycle, and lease owner after durable leases exist;
- backup result/age/size and last restore drill;
- certificate and credential expiry.

Alert routing must be tested, not only configured.

## Two-Node Pilot

Use two isolated Lite databases and a non-production Tally company. Pass all of
these scenarios before adding another franchise:

1. Normal inventory movement upload, projection, report, and eligible Tally
   post.
2. Lite offline for at least 24 hours, then ordered backlog replay.
3. Lost HTTP response followed by identical event retry.
4. Deliberate duplicate event.
5. Deliberate sequence gap and same-sequence/different-hash conflict.
6. Master database unavailable during upload; verify no false acknowledgement.
7. Edge unavailable and WireGuard flap.
8. Old, expired, invalid, and revoked node credential.
9. Current immediate key rotation and emergency revocation; test successor-key
   overlap separately after that production feature is implemented.
10. Source dispatch while destination is offline.
11. Partial receipt, invalid manifest item, duplicate scan/receipt, and late
    completion. Damaged/rejected exception states need their own implementation
    and acceptance case before they are offered.
12. Tally unavailable, slow, ambiguous timeout, and later recovery.
13. Master termination during the atomic event transaction and a restart after
    commit but before the response reaches Lite.
14. Worker restart between Tally request and recorded response.
15. Lite and Master upgrade across the oldest supported API version.
16. Encrypted offsite backup followed by clean-host restore.
17. Existing Lite inventory enrollment with a pre-existing serial collision and an
    explicitly approved empty-node baseline.
18. Invalid/inactive destination dispatch and its audited correction/cancel
    path; do not pass the pilot while that event can strand the ordered stream.
19. A serialized-item round trip A → B → A, including local product-identity
    collision rejection.
20. Verify the public edge rejects every non-Node-Sync path and that recovery
    operations require the isolated operator runbook.

Record evidence, timestamps, queue states, and reconciliation results.

## Weaker Direct-Port-Forward Fallback

Direct forwarding is not the default. It may be considered only after documenting
why the edge/tunnel cannot be operated.

Minimum controls:

- public DNS points to a static premises address;
- router forwards TCP 443 only to a dedicated Caddy listener;
- Caddy uses public ACME TLS, never the current `tls internal` example;
- Windows firewall allows only TCP 443 to Caddy;
- Uvicorn remains loopback; Tally/database/admin paths remain private;
- the public virtual host exposes only Node Sync API paths;
- endpoint hardening, patching, rate/body limits, logs, MFA/VPN admin isolation,
  external scan, backup, and incident-response gates all still pass.

The fallback increases premises-host exposure and recovery coupling. Approval
must name the risk owner and a trigger for moving back to the preferred edge.

## Rollback

An Internet rollout is rolled back when authentication, tenant isolation,
sequence safety, event durability, or private-service exposure cannot be proven.

Rollback actions are prepared before go-live:

- stop accepting new Node Sync traffic at the edge with a controlled `503` and
  `Retry-After`;
- keep Lite outboxes intact;
- do not revoke healthy node credentials merely to stop traffic;
- drain or freeze Master/Tally workers without deleting event journal rows;
- preserve database, edge, application, and Tally audit evidence;
- restore the last verified application/database pair only through the reviewed
  migration recovery procedure;
- compare each Lite outbox with `GET /api/v1/node` `last_sequence` and
  `next_sequence` before reopening.
