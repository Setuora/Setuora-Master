# Remote Franchise Connectivity

Setuora's supported remote pilot path is the private Tailscale deployment in
[universal-deployment.md](universal-deployment.md). Caddy, a public edge VPS,
public DNS, inbound NAT, and host-specific service installers are not part of
this path.

## Boundary

```text
Lite nodes on separate franchise networks
  -> outbound encrypted Tailscale connection
  -> Master private HTTPS endpoint
  -> authenticated Node Sync v1

Operators on approved devices
  -> outbound encrypted Tailscale connection
  -> Master private HTTPS endpoint
  -> Setuora login and role checks

Master
  -> private database
  -> local/private Tally gateway
```

Tailscale solves cross-network reachability. It does not determine which
franchise owns an event. Master still authenticates every Node Sync request with
the per-installation bearer credential and applies the franchise authorization,
ordering, idempotency, and body limits documented in
[Node Sync v1](../api/node-sync-v1.md).

## Why this works outside the LAN

Master and Lite nodes initiate outbound connections, so ordinary dynamic IP
addresses, carrier NAT, and unrelated ISP networks do not require inbound
firewall changes. Tailscale Serve gives Master a private, certificate-validated
HTTPS name in the tailnet. Encrypted relays can carry traffic when peer-to-peer
connectivity is unavailable.

This is not anonymous public access. Every Lite host and operator device must be
admitted to the tailnet and allowed by grants. Do not enable Funnel as a
shortcut for a franchise that has not installed Tailscale.

## Required controls

- Master uses `tag:setuora-master`; Lite installations use
  `tag:setuora-lite`.
- Grants allow Lite and operator sources to Master `tcp:443` only.
- No grant permits Lite-to-Lite traffic.
- Every franchise has a distinct Setuora node credential.
- Tailscale state and Setuora credentials are stored and rotated separately.
- Docker publishes Uvicorn only to host loopback.
- Tally `9000`, SQLite/PostgreSQL, backups, and Docker control sockets are never
  advertised to the tailnet or Internet.
- Secure cookies and exact MagicDNS trusted-host validation remain enabled.
- Rate limits, redacted logs, alerts, encrypted offsite backups, and restore
  drills remain mandatory operational controls.

The reviewed example policy is
[`deployment/tailscale/policy.hujson.example`](../../deployment/tailscale/policy.hujson.example).

## Acceptance test

Before enabling real franchise synchronization:

1. start Master with `python deploy.py setup`;
2. verify its health through the printed HTTPS URL from an operator device;
3. verify an untagged/unauthorized device cannot connect;
4. connect two isolated Lite test nodes from different networks;
5. issue a different Setuora node credential to each;
6. upload heartbeats and representative events;
7. confirm each event is attributed to the correct franchise;
8. test duplicate delivery, a sequence gap, credential revocation, offline
   recovery, command polling, and a partial transfer receipt;
9. verify neither Lite node can reach the other;
10. verify Tally and database ports are unreachable from both Lite nodes;
11. export and restore a verified backup on a clean host.

## Production gates

Tailscale removes the public-ingress and certificate-management burden; it does
not remove the application's production gaps. PostgreSQL and formal migrations,
durable worker leasing, multi-node load/failure testing, per-franchise Tally
mapping, monitoring and alerting, key-rotation drills, and clean-host recovery
remain required before moving beyond the bounded SQLite pilot.
