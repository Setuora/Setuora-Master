# Setuora Master Backup and Recovery

Master backup and recovery must preserve both database state and the sequence
relationship with every Lite node.

## Pilot backup contents

Protect:

- the verified SQLite database backup;
- `.env`, stored separately from the database;
- the deployed application revision;
- private proxy configuration and certificates;
- encrypted operational runbooks and credential records.

Do not copy a live SQLite file with an ordinary file-copy tool. Use the
application's verified backup operation or SQLite's backup API so committed WAL
state is included.

## Scheduled backups

The pilot creates verified backups in `data/backups/` by default. Each retained
file passes SQLite integrity and foreign-key checks.

Configure:

```text
AUTOMATIC_BACKUPS_ENABLED=true
BACKUP_DIRECTORY=./data/backups
BACKUP_INTERVAL_HOURS=24
BACKUP_RETENTION_COUNT=14
BACKUP_OFFSITE_DIRECTORY=<encrypted-off-machine-location>
```

An off-machine copy is required. Restrict access to backup operators and test
that retention cannot be silently disabled by the application service account.

## Recovery prerequisites

Before attempting recovery:

1. close the public Node Sync edge;
2. stop the Master application and workers;
3. preserve the failed database, logs, and application revision;
4. identify the last acknowledged sequence for every franchise;
5. select a verified database and matching application build;
6. prepare the node-cursor reconciliation record.

Replacing the database without cursor reconciliation can strand events already
acknowledged by Master and invalidate stock ownership.

## Controlled recovery

Recover only in an isolated environment:

1. install the recorded application revision on a clean host;
2. recover the environment configuration through the secret-management process;
3. load the verified database using the reviewed database procedure;
4. run schema migration checks;
5. verify database integrity and foreign keys;
6. compare every Master sequence cursor with the corresponding Lite outbox;
7. reconcile missing or divergent events through an audited process;
8. verify franchise, stock, transfer, command, and Tally queue invariants;
9. test login, health, backup creation, and a non-production Tally company;
10. reopen one pilot node, observe replay, then reopen the remaining nodes in
    controlled batches.

Never invent sequence values or delete an outbox to make reconciliation pass.

## Recovery drill acceptance

A drill passes only when:

- a clean host can run the recovered application;
- no acknowledged event is silently lost;
- duplicate replay remains idempotent;
- stock ownership and transfer states reconcile;
- queued Tally work does not double-post;
- old node credentials can be revoked;
- a new verified backup is created after acceptance;
- recovery time and recovery point objectives are recorded.

## Production

PostgreSQL production requires native encrypted backups, point-in-time recovery,
formal migrations, separate database roles, and repeated clean-host drills.
SQLite backup success is not production approval.
