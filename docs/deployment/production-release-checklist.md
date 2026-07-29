# Master Pilot Release Checklist

This checklist validates the private, single-process Windows pilot. Passing it
does not approve public exposure.

## Automated check

Run on the target server:

```powershell
.\deployment\windows\production_preflight.ps1 `
  -ProjectDir "C:\Setuora-Master" `
  -Address "master-admin.internal"
```

The script checks the Master environment identity, pinned dependencies, clean
source state, Windows services, private Caddy configuration, security headers,
test suite, health response, and a verified SQLite backup.

## Required manual checks

- [ ] `/health` returns `status=ok` and `role=master`.
- [ ] Uvicorn listens only on loopback.
- [ ] Tally and the database are private.
- [ ] The administrative site is limited to the management network.
- [ ] First-admin and node credentials are stored securely.
- [ ] Every enrolled franchise has a unique identity.
- [ ] Key issue, rotation, revocation, and incident procedures are rehearsed.
- [ ] Backup retention and encrypted offsite copy are verified.
- [ ] A clean-host recovery drill has passed.
- [ ] Master and Lite versions are inside the supported compatibility window.
- [ ] Event replay, duplicate delivery, sequence gaps, and node outage recovery
      have passed with two isolated Lite databases.
- [ ] Transfer partial-receipt and repeated-command cases have passed.
- [ ] Tally posting is disabled unless the real company mapping is accepted.
- [ ] Logs and metrics do not record API keys or raw authorization headers.

## Public-edge gate

Before any Internet rollout, also complete every control and acceptance scenario
in [master-internet-edge.md](master-internet-edge.md), including PostgreSQL,
formal migrations, WireGuard isolation, shared rate limiting, alerting, worker
leases, external scanning, and rollback testing.
