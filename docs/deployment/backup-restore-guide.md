# Master backup and recovery

Back up the Master SQLite database, `.env`, backup settings, and HTTPS proxy configuration together. The database contains franchise credentials, accepted event sequences, network stock, command queues, and pending Tally batches. Keep verified off-machine copies. Back up the central Tally company separately using its supported procedure; Setuora does not copy or edit Tally company files.

Before restoring, stop Master, note each Lite's last acknowledged event sequence, and preserve current database and logs for review. Restore a consistent Master database and configuration, then check franchise identities, pending commands, pending Tally batches, and central Tally state before resuming Lite traffic. A Master restore behind an already acknowledged Lite sequence requires operator reconciliation to avoid missing accepted events or duplicate vouchers.

The previous SFTP exchange tree and accounts are migration artifacts in the central-Tally deployment. Preserve them if a historical franchise-Tally deployment still needs audit or recovery.
