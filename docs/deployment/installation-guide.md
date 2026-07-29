# Setuora Master Installation

This guide installs the private Master application. It does not deploy the
public Node Sync edge, WireGuard, PostgreSQL, an administrative VPN, or Tally.

## Target host

Use a dedicated Windows 11 or supported Windows Server machine on the protected
Tally network. The pilot requires Python 3.11, Git, sufficient disk space for
the database and verified backups, and administrator access for service setup.

Keep these endpoints private:

- Uvicorn: `127.0.0.1:8000`
- Tally gateway: normally `127.0.0.1:9000`
- SQLite or PostgreSQL
- administrative HTTPS

Only the separate public edge may accept Internet traffic, and it forwards only
the exact Node Sync paths described in the
[Internet-edge guide](master-internet-edge.md).

## Automated Windows installation

Run from an Administrator terminal:

```powershell
.\Setuora.exe setup --install-dir C:\Setuora-Master
```

The utility:

- installs Git and Python 3.11 when approved and required;
- downloads the Setuora Master repository;
- creates the virtual environment;
- installs `requirements.lock` with hash verification;
- creates or preserves `.env`, `data\`, and `logs\`;
- forces `SETUORA_APP_MODE=master`;
- runs import and dependency checks;
- can configure private-LAN HTTPS;
- can install the automatic Windows services.

The generated private Caddy configuration is not the public edge. Skip it when
an approved private reverse proxy already exists:

```powershell
.\Setuora.exe setup --with-caddy=false
```

## Manual installation

```powershell
git clone https://github.com/Setuora/Setuora-Master.git C:\Setuora-Master
cd C:\Setuora-Master
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock
copy .env.example .env
```

Set unique secrets and reviewed hostnames in `.env`:

```text
SETUORA_APP_MODE=master
APP_NAME=Setuora Master
APP_SECRET_KEY=<long-random-secret>
BOOTSTRAP_ADMIN_USERNAME=admin
BOOTSTRAP_ADMIN_PASSWORD=<unique-first-login-password>
DATABASE_URL=sqlite:///./data/setuora.db
SESSION_COOKIE_SECURE=true
TRUSTED_HOSTS=master-admin.internal,sync.example.com,127.0.0.1,localhost
```

Do not place secrets in source control or support messages.

## Start and verify

```powershell
.\scripts\start_setuora.bat
```

Verify locally:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

Expected result:

```json
{"status":"ok","role":"master"}
```

Confirm that the administrative site is reachable only from the protected
management network and that public requests to non-Node-Sync paths are rejected
at the edge.

## Service installation

The automated setup can install the services. For a manual NSSM installation,
follow [windows-service.md](windows-service.md).

## Updates

```powershell
.\Setuora.exe update
```

The updater requires a clean source checkout, verifies the downloaded revision,
installs pinned dependencies, runs validation, and preserves `.env`, databases,
and backups.

Before an update:

1. create and export a verified backup;
2. record the current application revision;
3. confirm the Lite compatibility window;
4. schedule a low-traffic maintenance window;
5. keep a tested rollback package.

## Production gates

The Windows installer produces only the bounded single-process pilot. Before an
Internet rollout, complete PostgreSQL migration, formal schema migrations,
shared rate limiting, worker leasing, encrypted offsite recovery, monitoring,
key-rotation runbooks, and every acceptance test in
[master-internet-edge.md](master-internet-edge.md).
