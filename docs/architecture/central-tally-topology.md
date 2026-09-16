# Central Tally topology

One Setuora Master serves many Lite franchises. Master keeps its own database and owns the only Setuora connection to Tally's HTTP/XML gateway. It does not read or modify Tally company data files.

```text
Lite A ── HTTPS event queue ──┐
Lite B ── HTTPS event queue ──┼── Master database ── pending voucher queue ── Tally XML gateway
Lite C ── HTTPS event queue ──┘         │
            ▲                          └── franchise command queues
            └──────── HTTPS command polling ────────────
```

Master authenticates a separate node credential for each franchise. It stores accepted events idempotently and enforces franchise ordering. Stock transfers are delivered to the destination Lite as commands. Purchase, receive, sale, and sales return events create pending Tally batches; other supported inventory events update Master network state without a Tally voucher.

The central Tally worker processes supported vouchers one import at a time in the packaged single-process deployment. A definite failure leaves a pending or failed voucher. An ambiguous import response or interrupted import becomes `REVIEW_REQUIRED` and pauses the central queue until an administrator verifies the voucher in Tally. Acceptance of an event through `/api/v1/events` is not a Tally success acknowledgement. Tally runs at Master, so no XML is sent back to Lite for local Tally import.

The Master application listens on loopback. A reviewed HTTPS reverse proxy must publish only the authenticated `/api/v1` node API to remote Lite servers. Keep the admin console and Tally gateway private. The previous SFTP debtor/creditor topology in this directory is historical and is not active.
