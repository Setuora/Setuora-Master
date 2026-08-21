# Franchise Tally XML Exchange

## Franchise upload

In Tally Prime, export accounting masters as XML. The file may contain other
masters, but Setuora imports only ledgers directly under **Sundry Debtors** or
**Sundry Creditors**. Upload the file to `/inbox` using SFTP. Use a `.part`
suffix while transferring and rename to `.xml` only after upload completes.

## Master processing

Setuora validates the file, rejects unsafe/oversized XML, computes its SHA-256,
and idempotently updates the central party table. The newest accepted record for
a ledger name wins. The original file is moved to `processed/inbound`; invalid
files are moved to `failed` and recorded in the database.

Setuora then creates a complete Tally **All Masters** import envelope in
`/outbox`. Ledger entries use the official minimal `NAME` and `PARENT` fields,
plus supported mailing, contact, GST, and opening-balance fields when present.
Closing balance is retained centrally for reporting but is not written as a
master field because Tally derives it from transactions.

## Franchise download and acknowledgement

1. Download the XML from `/outbox`.
2. Back up the Tally company.
3. Import the file as **Masters** and choose the reviewed existing-master
   behavior (normally **Modify with new data** for two-way synchronization).
4. Review Tally's Exceptions report.
5. Only after a successful import, upload an empty file to `/ack` using the
   outbound filename with `.xml` replaced by `.ack`.

While an XML file remains unacknowledged in `/outbox`, Setuora does not process
another upload for that franchise.
