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

## Products from Tally

An administrator opens **Products → Import from Tally**, ticks stock items from the active Tally company, and ticks the franchises that should receive them. Master reads the items again on import, creates each product under the franchise's code, and queues a `PRODUCT_UPSERT` command for that Lite. A product that already exists for the franchise, by code or by Tally stock item name, is linked to Tally instead of duplicated. Items without a GST rate on the stock item itself cannot be imported, because Master will not guess a tax rate.

Live sync re-reads the Tally item master on the schedule set on the Products page (default every 15 minutes; 0 turns it off). It matches each imported product by Tally GUID, then by name, and updates the product name, Tally stock item name, HSN, unit and GST rate. Every change queues another `PRODUCT_UPSERT` command. Pricing stays as each Lite set it. An item removed from Tally is reported as missing and the product is left unchanged.

Lite freezes product details into its queued events. When live sync changes a product, Master records the previous details and still accepts events that carry them, so events queued before Lite applied the change are not rejected. Lite stops at the first command it cannot apply, so Master sends `PRODUCT_UPSERT` only to a Lite that declares support in its command poll. Until a franchise's Lite is updated, those commands wait in its queue.
