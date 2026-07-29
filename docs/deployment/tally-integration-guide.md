# Master Tally Integration

Setuora Master is the only Setuora edition that may communicate with Tally. Lite
nodes send events to Master; they do not receive Tally network access or
credentials.

## Network boundary

- Run Tally on the Master host or a protected LAN endpoint.
- Bind or firewall the XML gateway so only Master can reach it.
- Never publish port `9000`.
- Keep the database and Tally data directory private.

The default private endpoint is:

```text
http://127.0.0.1:9000
```

## Configure the pilot company

1. Open `Settings`.
2. Configure the private Tally host, port, company, voucher types, ledgers, tax
   ledgers, round-off ledger, and retry interval.
3. Keep posting disabled.
4. Open `Tally Check`.
5. Load the available Tally companies and choose the non-production test
   company.
6. Compare every required master name exactly and record the confirmations.
7. Enable posting only after a controlled event produces the expected voucher
   and inventory effect.

Changing the active company disables posting until the new company is checked.

## Queue behavior

Node Sync commits an accepted event and its Master projections before returning
success. Eligible accounting work is placed in the central queue. The background
worker posts it later and records request, response, retry, and error details.

Node uploads never wait for Tally. A Tally outage therefore does not roll back an
accepted network event.

The exact event types and current queue effects are defined in
[Node Sync API v1](../api/node-sync-v1.md).

## Franchise mapping gate

The pilot worker currently uses one active global company configuration. That is
not sufficient for unrestricted multi-franchise accounting.

Before enabling network-wide posting, implement and validate:

- an authoritative franchise-to-company or franchise-to-Godown mapping;
- reviewed voucher and ledger behavior for every franchise;
- stable accounting idempotency keys;
- durable worker leases;
- duplicate-post reconciliation;
- a correction process for rejected accounting data;
- Stock Journal behavior for inter-franchise transfers.

Until those controls pass, use the Tally queue for a controlled test company or
monitoring only.

## Operational checks

When work remains queued:

1. confirm Tally is open;
2. test the private gateway;
3. confirm the selected company;
4. review `Tally Check`;
5. inspect the queue item and its latest attempt;
6. correct configuration before retrying.

Do not edit an accepted network event to make accounting succeed. Apply a
reviewed correction through an auditable domain operation.

## Security

- Restrict raw XML and attempt details to administrators.
- Redact credentials from logs and support bundles.
- Back up Tally and Master independently.
- Test reconciliation after timeout, process termination, and ambiguous gateway
  responses.
