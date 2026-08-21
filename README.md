# Setuora Master

Setuora Master is the central Windows server for two-way Tally debtor and
creditor synchronization. Every franchise exports Tally XML and transfers it
to an isolated SFTP folder. Setuora imports those masters into its central
database and publishes a consolidated Tally XML file back to that franchise.

The active deployment is Windows-only. It uses Windows OpenSSH for SFTP and a
Windows startup task for the Python application. Docker and Tailscale are not
part of the active setup. Their previous files are retained under `archive/`.

## Data flow

```text
Franchise Tally
  -> SFTP /inbox
  -> Setuora Master validates and upserts Sundry Debtors/Creditors
  -> SFTP /outbox/setuora-...xml
  -> Franchise imports the XML into Tally
  -> Franchise uploads /ack/<same-file-stem>.ack
  -> Setuora archives the completed exchange and accepts the next upload
```

Only `LEDGER` masters whose parent is `Sundry Debtors` or `Sundry Creditors`
are synchronized. XML is parsed with entity expansion disabled, upload size is
bounded, imports are idempotent by SHA-256, and files are moved to `processed`
or `failed` for audit. A franchise is paused while an unacknowledged file is in
its `outbox`, preventing overlapping Tally imports.

## Windows server requirements

- Windows Server 2019 or newer, or Windows 10/11 Pro for a pilot;
- Python 3.11 or newer available on `PATH`;
- Administrator access during setup;
- a static public IP or router/NAT mapping for TCP 22;
- Tally Prime at each franchise.

The admin console is bound to `127.0.0.1:8000`; use the server desktop or a
separately reviewed HTTPS reverse proxy to access it. Do not publish port 8000,
the SQLite database, backups, or Tally port 9000 to the Internet.

## Install

Build the single Windows installer:

```powershell
py -3.11 scripts\build_client_packages.py --version 1.0.0
```

Copy `dist\Setuora-Master-1.0.0-windows.cmd` to the Windows server and run it.
The installer requests Administrator access, installs under
`C:\ProgramData\Setuora\Setuora-Master-windows`, creates a private virtual
environment, enables Windows OpenSSH, registers the Setuora startup task, and
waits for `http://127.0.0.1:8000/health`.

## Add a franchise

First enroll the same permanent code in Setuora's **Franchises** page. Codes
must be 1–20 characters using letters, digits, `_`, or `-`. Then open an
elevated PowerShell window:

```powershell
& "C:\ProgramData\Setuora\Setuora-Master-windows\setuora.ps1" sftp-add BLR-01
```

The command securely prompts for the SFTP password and creates an SFTP-only,
chrooted local Windows account. Its username is the lowercase franchise code.

Give the franchise:

- server public IP and TCP port 22;
- the lowercase username and generated password;
- upload folder `/inbox`;
- download folder `/outbox`;
- acknowledgement folder `/ack`.

Franchises should upload to a temporary non-XML filename, then rename it to
`.xml` after transfer completes. After importing an outbound file successfully,
they upload an empty `.ack` file with exactly the same stem.

## Daily operations

```powershell
$setuora = "C:\ProgramData\Setuora\Setuora-Master-windows\setuora.ps1"
& $setuora status
& $setuora logs --follow
& $setuora stop
& $setuora start
```

See [Windows installation](docs/deployment/installation-guide.md),
[SFTP/Tally operations](docs/deployment/tally-integration-guide.md), and
[backup recovery](docs/deployment/backup-restore-guide.md).

## Development and validation

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --require-hashes -r requirements.lock
copy .env.example .env
python -m pytest -q
python -m ruff check app tests scripts deploy.py
python -m ruff format --check app tests scripts deploy.py
```
