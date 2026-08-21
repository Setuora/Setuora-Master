# Windows SFTP/Tally Topology

Setuora Master is the central database and synchronization worker running on a
Windows server. Windows OpenSSH is the only Internet-facing component.

```text
Franchise A Tally --SFTP--> /franchises/A/inbox
Franchise B Tally --SFTP--> /franchises/B/inbox
Franchise C Tally --SFTP--> /franchises/C/inbox
                              |
                              v
                     Setuora Master worker
                              |
                     central debtor/creditor DB
                              |
          /franchises/<code>/outbox --SFTP--> franchise Tally
```

Each Windows local account is forced to `internal-sftp`, chrooted to one
franchise directory, and denied shells, forwarding, and tunnels. The chroot
root is administrator-owned. The franchise can write only `inbox` and `ack`,
and can only read `outbox`; `processed` and `failed` remain server-only.

Setuora processes at most one inbound XML per franchise before waiting for an
acknowledgement of the generated outbound file. This implements the operational
gate: synchronization stops until the downloaded XML has been imported into
Tally successfully.
