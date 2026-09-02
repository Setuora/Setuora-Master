# Setuora Master

> Central Windows service for controlled Tally master exchange across franchises.

## Overview

Setuora Master imports Tally debtor and creditor XML from each franchise, stores
the consolidated master data, and publishes a franchise-specific XML file for
import back into Tally Prime.

Each franchise uses an isolated SFTP account and exchange directory. The web
console is intended for administration from the server itself.

## Status

The supported deployment is Windows-only. It uses Windows OpenSSH for SFTP and
a Windows startup task for the Python application. Docker and Tailscale are not
part of the active setup; their previous implementation files are retained in
`archive/` for reference.

## Features

- Isolated SFTP inbox, outbox, acknowledgement, processed, and failed areas for
  each franchise
- Tally `LEDGER` synchronization for `Sundry Debtors` and `Sundry Creditors`
- Idempotent imports identified by SHA-256
- Franchise-specific consolidated Tally XML output
- Acknowledgement gating that prevents overlapping outbound imports
- Bounded XML uploads and parsing with entity expansion disabled
- Auditable processing and failure history
- Local administration console and franchise enrollment
- Verified SQLite backups with configurable retention and off-machine copies

## Architecture

```text
Franchise Tally
  -> SFTP /inbox
  -> Setuora Master validates and upserts debtors/creditors
  -> SFTP /outbox/setuora-...xml
  -> Franchise imports the XML into Tally
  -> Franchise uploads /ack/<same-file-stem>.ack
  -> Setuora archives the exchange and accepts the next upload
```

Only supported `LEDGER` masters are synchronized. A franchise remains paused
while an unacknowledged file is present in its outbox.

## Repository Structure

```text
Setuora-Master/
|-- app/                         FastAPI application and processing services
|-- client/windows/              Windows client lifecycle assets
|-- deployment/                  Active deployment support files
|-- docs/                        Architecture and operating guides
|-- scripts/                     Packaging and Windows administration scripts
|-- tests/                       Pytest coverage
|-- archive/                     Unsupported historical deployment code
|-- data/                        Local runtime data, ignored by Git
|-- deploy.py                    Deployment lifecycle entry point
|-- requirements.lock            Hash-verified development dependency lock
|-- requirements-runtime.lock    Hash-verified runtime dependency lock
`-- .env.example                 Configuration template
```

## Requirements

- Windows Server 2019 or newer, or Windows 10/11 Pro for a pilot
- Python 3.11 or newer available on `PATH`
- Administrator access during setup
- A static public IP or router/NAT mapping for TCP 22
- Tally Prime at each franchise

The admin console binds to `127.0.0.1:8000`. Access it from the server desktop
or through a separately reviewed HTTPS reverse proxy.

## Installation

Build the Windows installer from the repository root:

```powershell
py -3.11 scripts\build_client_packages.py --version 1.0.0
```

Copy `dist\Setuora-Master-1.0.0-windows.cmd` to the Windows server and run it as
an administrator. The installer:

- installs to `C:\ProgramData\Setuora\Setuora-Master-windows`;
- creates a private virtual environment;
- enables and configures Windows OpenSSH;
- registers the Setuora startup task; and
- waits for `http://127.0.0.1:8000/health`.

Complete the [production release checklist](docs/deployment/production-release-checklist.md)
before live use.

### Windows source-checkout controls

For a Windows source checkout used for development or direct server setup, run
`setuora.bat` from the repository root (not the generated release installer).
Run it without an argument for a menu, or use one of these commands:

```bat
setuora.bat setup
setuora.bat start
setuora.bat stop
setuora.bat update
```

Run `setuora.bat setup` first; it requests Administrator approval and installs
Python 3.11 with Windows Package Manager when needed. Run `setuora.bat update`
elevated as well because it manages the native task lifecycle. These wrappers
find the checkout's `deploy.py`; they are source-checkout/developer controls.
For a production packaged
installation, continue to use the installer procedure above and its installed
`setuora.ps1` lifecycle script.

## Configuration

Copy `.env.example` to `.env` for development. Production setup creates the
deployed configuration. Important settings include:

- `APP_SECRET_KEY` and the one-time bootstrap administrator credentials
- `DATABASE_URL` and backup directories
- `SFTP_EXCHANGE_ROOT`, polling interval, settle time, and maximum XML size
- `TRUSTED_HOSTS` and the local web port
- login throttling and backup retention

Do not reuse placeholder secrets. Changing bootstrap values after the database
exists does not change an existing account.

## Usage

Enroll the permanent franchise code in the **Franchises** page, then create its
SFTP-only account from an elevated PowerShell window:

```powershell
& "C:\ProgramData\Setuora\Setuora-Master-windows\setuora.ps1" sftp-add BLR-01
```

Franchise codes must be 1-20 characters and contain only letters, digits, `_`,
or `-`. Give the franchise its server address, TCP port, lowercase username,
generated password, and these folders:

- upload: `/inbox`
- download: `/outbox`
- acknowledgement: `/ack`

Upload to a temporary non-XML name and rename it to `.xml` only after transfer
completes. After a successful Tally import, upload an empty `.ack` file with the
same stem as the outbound XML.

## Operations

Use the installed lifecycle script for routine administration:

```powershell
$setuora = "C:\ProgramData\Setuora\Setuora-Master-windows\setuora.ps1"
& $setuora status
& $setuora logs --follow
& $setuora stop
& $setuora start
```

Back up the database and configuration on a reviewed schedule. Follow the
[backup recovery guide](docs/deployment/backup-restore-guide.md) before replacing
or restoring runtime data.

## Development

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --require-hashes -r requirements.lock
copy .env.example .env
uvicorn app.main:app --reload
```

The development console is available at `http://127.0.0.1:8000`.

## Testing

Run the full validation suite before packaging a release:

```powershell
python -m pytest -q
python -m ruff check app tests scripts deploy.py
python -m ruff format --check app tests scripts deploy.py
```

## Security

- Do not publish port 8000, the SQLite database, backups, or Tally port 9000.
- Expose only the reviewed SFTP endpoint required by franchises.
- Keep every franchise in its own chrooted, SFTP-only Windows account.
- Protect `.env`, backup files, SFTP credentials, and the server host.
- Preserve upload limits, safe XML parsing, idempotency, and acknowledgement
  gating when changing the exchange flow.

## Documentation

- [Windows installation](docs/deployment/installation-guide.md)
- [SFTP and Tally operations](docs/deployment/tally-integration-guide.md)
- [Backup and recovery](docs/deployment/backup-restore-guide.md)
- [Client packaging](docs/deployment/client-packages.md)
- [Production release checklist](docs/deployment/production-release-checklist.md)
- [SFTP/Tally topology](docs/architecture/sftp-tally-topology.md)

## Troubleshooting

- **Health check fails:** confirm the startup task is running, port 8000 is
  available locally, and the application logs contain no configuration error.
- **SFTP login fails:** confirm OpenSSH is running, TCP 22 reaches the server,
  and the lowercase franchise account matches the enrolled code.
- **An upload is not processed:** confirm the completed file has a `.xml`
  extension, is within the size limit, and is in `/inbox`.
- **No new outbound file appears:** check whether the previous file is still
  awaiting its matching acknowledgement in `/ack`.
- **An import fails:** inspect the franchise's failed area and application logs,
  correct the XML or Tally master data, and follow the operations guide.
