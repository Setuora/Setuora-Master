# Windows SFTP Release Checklist

- [ ] Windows updates and OpenSSH Server updates are current.
- [ ] TCP 22 is reachable externally and restricted by firewall source ranges
      where practical.
- [ ] The admin console listens only on `127.0.0.1:8000`.
- [ ] Every franchise has a distinct SFTP-only Windows account and code.
- [ ] Chroot, inbox, outbox, ack, processed, and failed ACLs were tested.
- [ ] A partial upload is ignored until renamed to `.xml`.
- [ ] Duplicate XML is idempotent.
- [ ] Invalid and oversized XML moves to `failed`.
- [ ] A pending outbound file blocks the next inbound file.
- [ ] A matching acknowledgement unblocks the next exchange.
- [ ] A real Tally debtor/creditor export-import round trip passed.
- [ ] SQLite backup verification and off-machine recovery passed.
- [ ] Public exposure excludes port 8000, Tally 9000, database, and backups.
