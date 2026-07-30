# Setuora Master

Setuora Master is the central monitoring and Tally control plane for a Setuora
franchise network. Franchise nodes send durable, ordered events to Master over
Node Sync v1. Master projects network stock, coordinates inter-franchise
transfers, provides consolidated reports, and queues eligible accounting work
for the Tally instance on the protected Master network.

Master is not a franchise transaction-entry application. Operational capture,
label handling, and local inventory work remain in Setuora Lite.

> **Rollout status:** the Master application and Node Sync v1 are implemented for
> a controlled pilot. The universal deployment uses a private Tailscale network;
> PostgreSQL migration, franchise-aware Tally mapping, disaster-recovery drills,
> metrics, and multi-node acceptance testing remain production gates. Do not
> expose Uvicorn, Tally, or the database directly to the public Internet.

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
- [Universal Linux/Windows deployment](docs/deployment/universal-deployment.md)
- [Shareable client installer packages](docs/deployment/client-packages.md)
- [Remote franchise connectivity](docs/deployment/remote-franchise-connectivity.md)

Lite always initiates the connection:

```text
Setuora Lite
  -> outbound Tailscale/WireGuard connection
  -> private tailnet HTTPS 443
  -> Tailscale Serve
  -> Uvicorn in the private container network
  -> private database and Tally gateway
```

There is no public listener, public DNS requirement, router port-forward, or
Caddy dependency. Tailscale grants determine which devices can reach Master;
Setuora node credentials independently bind every sync request to one franchise.
Tally and the database remain private.

## Pilot limits

The current SQLite foundation is limited to:

- at most 50 enrolled Lite nodes;
- less than 5 sustained events per second;
- one Master web process;
- one logical retry worker.

PostgreSQL, formal migrations, durable worker leasing, shared rate limiting, and
clean-host recovery testing are required before a production Internet rollout.

## Requirements

For the recommended deployment on either Linux or Windows:

- Docker Engine with Compose v2, or Docker Desktop;
- a Tailscale account with HTTPS and MagicDNS enabled;
- one-off, non-ephemeral, pre-authorized auth key tagged
  `tag:setuora-master`;
- Tally Prime on the Docker host or a private LAN endpoint;
- a unique first-administrator password.

Python 3.11 is needed only for local development and for the optional
cross-platform `deploy.py` helper.

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
{ "status": "ok", "role": "master" }
```

Bootstrap credentials create only the first account in a new database. Changing
the environment values later does not change an existing user's password.

## Universal self-hosted deployment

The same deployment is used on Linux and Windows. It runs Setuora and Tailscale
as Compose services, uses persistent Docker volumes, and publishes port `8000`
only on host loopback for health checks.

```bash
python deploy.py setup
```

The helper securely prompts for the first administrator password and the
tagged Tailscale auth key, builds the application, waits for health, and prints
the private HTTPS URL. Lifecycle commands are also identical:

```bash
python deploy.py start
python deploy.py status
python deploy.py logs
python deploy.py update
python deploy.py stop
```

If Python is unavailable on the host, fill `.env` from `.env.example` and use
`docker compose up -d --build`; query the URL with
`docker compose exec tailscale tailscale status`.

Copy [`deployment/tailscale/policy.hujson.example`](deployment/tailscale/policy.hujson.example)
into the Tailscale access-control editor after replacing the operator address.
Install Tailscale on every Lite host, tag it `tag:setuora-lite`, and configure
its `MASTER_URL` with the HTTPS URL printed by setup. See the
[universal deployment guide](docs/deployment/universal-deployment.md).

## Shareable Linux and Windows packages

Create client-ready single-file installers from a reviewed release:

```bash
python scripts/build_client_packages.py --version 1.0.0
```

This writes one self-extracting Linux `.run` file, one double-clickable Windows
`.cmd` file, and SHA-256 checksums to `dist/`. Each platform file installs or
updates the complete application and excludes `.env`, credentials, databases,
backups, and Tailscale state. See the
[client package guide](docs/deployment/client-packages.md).

For local use, the project root contains stable shortcuts named
`Linux — Setuora Master.run` and `Windows — Setuora Master.cmd`. Each shortcut
automatically launches the newest matching installer from `dist/`.

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

Recovery is an offline operator procedure. Revoke or stop tailnet access,
recover a matched application/database backup, reconcile every node cursor,
verify network ownership, and reopen uploads only after an audited acceptance
check.

See the [backup and recovery guide](docs/deployment/backup-restore-guide.md).

## Remote franchise connectivity

Tailscale Serve terminates HTTPS inside the private tailnet. Master and every
Lite node make outbound connections, so different ISPs, carrier NAT, dynamic
addresses, and separate franchise networks do not require inbound firewall
rules. This is deliberately Tailscale **Serve**, not Funnel: unauthenticated
public Internet clients cannot reach Setuora.

Network admission is not a franchise identity. Each Lite installation must also
use the one-time Setuora node credential issued from Master's Franchises page.
See the [remote-connectivity guide](docs/deployment/remote-franchise-connectivity.md).

## Validation

Run:

```bash
python -m pytest -q
python -m compileall -q app
docker compose config --quiet
```

## Deployment guides

- [Universal Linux/Windows deployment](docs/deployment/universal-deployment.md)
- [Shareable client installer packages](docs/deployment/client-packages.md)
- [Installation](docs/deployment/installation-guide.md)
- [Pilot release checklist](docs/deployment/production-release-checklist.md)
- [Backup and recovery](docs/deployment/backup-restore-guide.md)
- [Tally integration](docs/deployment/tally-integration-guide.md)
- [Remote franchise connectivity](docs/deployment/remote-franchise-connectivity.md)

## Troubleshooting

If login fails, confirm that the application is using the expected database. A
bootstrap password does not overwrite an existing account.

If a franchise is offline, verify its system clock, Tailscale status and grants,
the private `*.ts.net` URL, its Setuora node API key, and the last acknowledged
sequence shown in Master.

If Tally work remains queued, verify that Tally is open on the private gateway,
the selected company is correct, required masters are confirmed, and posting is
enabled only for an accepted test company.

If startup fails, run `python deploy.py status` and `python deploy.py logs`.
Check Docker, the tagged Tailscale auth key, HTTPS/MagicDNS enablement, `.env`,
the dependency lock, and free disk space.
