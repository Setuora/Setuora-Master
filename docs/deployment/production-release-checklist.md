# Master Pilot Release Checklist

This checklist validates the private, single-process, cross-platform pilot.
Passing it does not approve anonymous public exposure or production scale.

## Automated checks

Run from the repository root:

```bash
python -m ruff check .
python -m ruff format --check .
npm ci --ignore-scripts
npm run format:check
python -m pytest -q
python -m compileall -q app deploy.py
docker compose config --quiet
python deploy.py preflight
python deploy.py status
```

## Required manual checks

- [ ] `/health` returns `status=ok` and `role=master`.
- [ ] Docker publishes `8000` only on `127.0.0.1`.
- [ ] Tailscale Serve provides valid HTTPS at the expected `*.ts.net` name.
- [ ] Master is tagged `tag:setuora-master`.
- [ ] Lite nodes are tagged `tag:setuora-lite`.
- [ ] Grants allow Lite and operator access only to Master `tcp:443`.
- [ ] No Lite-to-Lite grant exists.
- [ ] Tailscale Funnel is disabled.
- [ ] Tally, the database, backups, and Docker socket are not advertised.
- [ ] First-admin, Tailscale, and node credentials are stored securely.
- [ ] Every enrolled franchise has a unique identity and node credential.
- [ ] Key issue, rotation, revocation, and incident procedures are rehearsed.
- [ ] Backup retention and encrypted offsite copy are verified.
- [ ] A clean-host recovery drill has passed.
- [ ] Master and Lite versions are inside the supported compatibility window.
- [ ] Event replay, duplicate delivery, sequence gaps, and node outage recovery
      have passed with two isolated Lite databases on different networks.
- [ ] Transfer partial-receipt and repeated-command cases have passed.
- [ ] Tally posting is disabled unless the real company mapping is accepted.
- [ ] Logs and metrics do not record API keys or raw authorization headers.

## Production gate

Before production scale, also complete PostgreSQL, formal migrations, shared
rate limiting, alerting, worker leases, load/outage testing, encrypted offsite
recovery, and rollback testing described in
[remote-franchise-connectivity.md](remote-franchise-connectivity.md).
