# Windows Backup and Restore

A recoverable Setuora Master installation needs a matched copy of:

- the verified SQLite backup;
- `.env` and the persistent application secret;
- `C:\ProgramData\Setuora\sftp`, including pending outbox/ack state;
- the list of SFTP-only Windows accounts and their franchise codes.

Use Setuora's Maintenance page or automatic backup worker for the database. Do
not copy a live SQLite file directly because committed WAL data may be omitted.
Keep encrypted copies on another machine or managed backup destination.

## Restore

1. Block inbound TCP 22 and stop Setuora.
2. Preserve the failed installation for audit.
3. Restore `.env`, the verified database, and the matching SFTP tree.
4. Run `setuora.ps1 preflight`.
5. Recreate any missing SFTP accounts with `setuora.ps1 sftp-add CODE`.
6. Start Setuora and verify `/health` locally.
7. Reconcile each franchise's `inbox`, `outbox`, and `ack` files. Do not remove
   a pending outbound XML unless the franchise confirms the Tally import.
8. Test a non-production exchange, then reopen TCP 22.

After recovery, verify database integrity, recent central party records, import
history, scheduled backups, Windows startup task state, OpenSSH chroot behavior,
and SFTP folder ACLs.
