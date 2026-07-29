# Setuora Master

Setuora Master is the central monitoring and Tally control plane for a Setuora
franchise network. Franchise nodes send durable, ordered events to Master over
Node Sync v1. Master projects network stock, coordinates inter-franchise
transfers, provides consolidated reports, and queues eligible accounting work
for the Tally instance on the protected Master network.

Master is not a franchise transaction-entry application. Operational capture,
label handling, and local inventory work remain in Setuora Lite.

> **Rollout status:** the Master application and Node Sync v1 are implemented for
> a controlled pilot. The public edge, PostgreSQL migration, franchise-aware
> Tally mapping, disaster-recovery drills, metrics, and multi-node acceptance
> testing remain production gates. Do not expose Uvicorn, Tally, the database,
> or the administrative UI directly to the Internet.

## Implemented Master capabilities

- authenticated franchise enrollment and revocable node credentials;
- ordered, idempotent event ingestion with franchise sequence enforcement;
- central product, serial, ownership, stock, and movement projections;
- dispatched, in-transit, partial, and completed transfer monitoring;
- durable commands polled by Lite nodes;
- franchise health, raw event, transfer, report, and Tally queue views;
- a central retry worker for eligible Tally work;
- role-based administration and verified SQLite backups for the pilot.

The Master application registers only its monitoring, administration, Tally,
maintenance, and Node Sync routes. Public API documentation is disabled.

## Architecture and protocol

- [Master/Lite architecture decision](docs/architecture/adr-001-master-lite-control-plane.md)
- [Master/Lite topology](docs/architecture/master-lite-topology.md)
- [Node Sync API v1](docs/api/node-sync-v1.md)
- [Internet-edge deployment](docs/deployment/master-internet-edge.md)
- [Public-edge Caddy example](deployment/caddy/Caddyfile.master.example)

Lite always initiates the connection:

```text
Setuora Lite
  -> outbound HTTPS 443
  -> public TLS edge
  -> WireGuard
  -> private Master ingress
  -> Uvicorn on 127.0.0.1:8000
  -> private database and Tally gateway
```

Only the exact Node Sync API paths belong on the public hostname. Master login,
reports, maintenance, static files, OpenAPI, the database, and Tally must remain
private.

## Pilot limits

The current SQLite foundation is limited to:

- at most 50 enrolled Lite nodes;
- less than 5 sustained events per second;
- one Master web process;
- one logical retry worker.

PostgreSQL, formal migrations, durable worker leasing, shared rate limiting, and
clean-host recovery testing are required before a production Internet rollout.

## Requirements

- Python 3.11;
- the hash-verified packages in `requirements.lock`;
- a writable `data/` directory;
- Tally Prime on the Master machine or a private LAN endpoint;
- a long application secret and a unique first-admin password;
- Windows administrator access when using the supplied service installer.

Tally port `9000` must never be reachable from the public Internet.

## Local setup

Create the environment:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r requirements.lock
cp .env.example .env
```

On Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --require-hashes -r requirements.lock
copy .env.example .env
```

Set at least:

```text
SETUORA_APP_MODE=master
APP_NAME=Setuora Master
APP_SECRET_KEY=replace-with-a-long-random-secret
BOOTSTRAP_ADMIN_USERNAME=admin
BOOTSTRAP_ADMIN_PASSWORD=replace-with-a-unique-password
DATABASE_URL=sqlite:///./data/setuora.db
SESSION_COOKIE_SECURE=false
TRUSTED_HOSTS=localhost,127.0.0.1
```

Use `SESSION_COOKIE_SECURE=true` when the private administrative site is served
over HTTPS. Add only reviewed public-sync and private-admin hostnames to
`TRUSTED_HOSTS`.

Start locally:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. The health endpoint should return:

```json
{"status":"ok","role":"master"}
```

Bootstrap credentials create only the first account in a new database. Changing
the environment values later does not change an existing user's password.

## Windows setup and lifecycle

`Setuora.exe` is the Windows setup and control utility:

```text
Setuora.exe setup --install-dir C:\Setuora-Master
Setuora.exe repair
Setuora.exe update
Setuora.exe start
Setuora.exe stop
```

Setup installs the pinned dependencies, writes a Master environment file,
configures the optional private-LAN HTTPS proxy, and can install the Windows
services. It does not deploy the public edge, WireGuard, PostgreSQL, an
administrative VPN, or production monitoring.

The updater refuses uncommitted source changes and preserves runtime data,
settings, and backups. See the [installation guide](docs/deployment/installation-guide.md)
and [Windows service guide](docs/deployment/windows-service.md).

## First administration

After the first login:

1. Create named `directors` accounts for consolidated monitoring.
2. Create named `admin` accounts only for operators who manage franchises,
   sensitive event details, or Tally.
3. Configure the private Tally company and gateway under `Settings`.
4. Verify the gateway and required masters under `Tally Check`.
5. Enroll each franchise and copy its one-time node credential directly into the
   corresponding Lite configuration.
6. Keep Tally posting disabled until the real company mapping is accepted.

Store node secrets in an approved secret manager. Master stores only hashes and
cannot display a secret again after enrollment or rotation.

## Tally control plane

Node Sync never calls Tally inside an upload request. Master commits the event
and its projection first, then the background worker handles eligible queued
work.

The current worker uses one active global company configuration. Do not enable
multi-franchise posting until each franchise has an approved company or Godown
mapping, stable idempotency behavior, and worker-lease acceptance tests.
Inter-franchise accounting also requires a validated Stock Journal design.

See the [Tally integration guide](docs/deployment/tally-integration-guide.md).

## Monitoring and reports

The Master console provides:

- franchise connectivity and sequence position;
- current network stock by franchise;
- network movement history;
- inter-franchise transfer status;
- consolidated movement totals and CSV export;
- the central Tally queue and attempt details.

Raw payload and Tally attempt details are restricted to administrative roles.

## Backups and recovery

The pilot creates verified SQLite backups on a schedule and supports a protected
backup download. Keep encrypted copies off the Master machine and protect the
environment file separately.

Recovery is an offline operator procedure. Close the public edge, recover a
matched application/database backup, reconcile every node cursor, verify network
ownership, and reopen uploads only after an audited acceptance check.

See the [backup and recovery guide](docs/deployment/backup-restore-guide.md).

## Internet deployment

The supported target is a public TLS edge connected to a private Master ingress
over WireGuard. The administrative site is reachable only through an
authenticated private network.

The included edge Caddyfile is a reviewed starting point, not a turnkey
deployment. Replace every placeholder, pin and validate the proxy version, add
shared rate limiting, and pass every acceptance test in the
[Internet-edge guide](docs/deployment/master-internet-edge.md).

## Validation

Run:

```bash
python -m pytest -q
python -m compileall -q app
```

For Windows pilot validation:

```powershell
.\deployment\windows\production_preflight.ps1 `
  -ProjectDir "C:\Setuora-Master" `
  -Address "master-admin.internal"
```

The Windows preflight validates only the private single-process pilot. It is not
approval for public exposure.

## Deployment guides

- [Installation](docs/deployment/installation-guide.md)
- [Private administrative HTTPS](docs/deployment/https-lan-guide.md)
- [Windows service](docs/deployment/windows-service.md)
- [Pilot release checklist](docs/deployment/production-release-checklist.md)
- [Backup and recovery](docs/deployment/backup-restore-guide.md)
- [Tally integration](docs/deployment/tally-integration-guide.md)
- [Internet edge](docs/deployment/master-internet-edge.md)

## Troubleshooting

If login fails, confirm that the application is using the expected database. A
bootstrap password does not overwrite an existing account.

If a franchise is offline, verify its system clock, public DNS, outbound TCP
443, API key, TLS trust, and the last acknowledged sequence shown in Master.

If Tally work remains queued, verify that Tally is open on the private gateway,
the selected company is correct, required masters are confirmed, and posting is
enabled only for an accepted test company.

If startup fails, check `.env`, Python 3.11, the dependency lock, directory
permissions, port `8000`, and the service error log.
