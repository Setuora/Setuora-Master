# Shareable Linux and Windows Client Packages

Setuora Master releases are delivered as one self-contained file for each
operating system:

- `Setuora-Master-<version>-linux.run`
- `Setuora-Master-<version>-windows.cmd`

Each self-extracting installer contains the complete application, Docker Compose
deployment, Tailscale policy, documentation, and lifecycle launcher. The same
file handles both first-time setup and later updates. It does not contain
`.env`, database files, credentials, backups, or Tailscale device state.

## Build the packages

From a reviewed release checkout:

```bash
python scripts/build_client_packages.py --version 1.0.0
```

The two single-file installers and `SHA256SUMS.txt` are written to `dist/`. Send
only the installer matching the client's operating system (and optionally the
checksum). Build from a clean, tagged revision and retain that exact installer
for rollback.

The repository root also provides `Linux — Setuora Master.run` and
`Windows — Setuora Master.cmd` as stable local shortcuts. They find and launch
the newest matching versioned installer in `dist/`.

## Client installation

The client must first install:

- Docker Engine with Compose v2 on Linux, or Docker Desktop using Linux
  containers on Windows;
- Python 3.11 or newer.

The package deliberately does not silently install or elevate third-party host
software. Docker installation can require a reboot, changes privileged system
services, and has organization-specific licensing and hardening requirements.

On Linux, run:

```bash
chmod +x Setuora-Master-<version>-linux.run
./Setuora-Master-<version>-linux.run
```

On Windows, start Docker Desktop and double-click
`Setuora-Master-<version>-windows.cmd`.

Setup prompts securely for the first administrator password and tagged
Tailscale key. It creates `.env`, builds the application, starts both services,
waits for health, enrolls Tailscale, removes bootstrap credentials from `.env`,
and prints the private HTTPS URL. The application is extracted to a stable
per-user installation directory:

- Linux:
  `${XDG_DATA_HOME:-$HOME/.local/share}/setuora/Setuora-Master-linux`;
- Windows: `%LOCALAPPDATA%\Setuora\Setuora-Master-windows`.

## Client updates

Before updating, export a verified database backup from **Maintenance** and
retain the current installer. Run the newer single file exactly as for the
initial installation:

```bash
./Setuora-Master-<new-version>-linux.run
```

or double-click the newer Windows `.cmd` file. It detects `.env`, stops the
existing deployment, safely replaces application files, runs production
preflight checks, pulls the pinned Tailscale image, rebuilds Setuora, and waits
for application and private HTTPS health.

Docker's fixed Compose project name keeps these named volumes stable across
versions:

- `setuora-master_setuora-data`
- `setuora-master_tailscale-state`

Never distribute a package copied from an active installation folder without
first checking it for `.env`, database files, backups, or credentials. The
official package builder selects only allowlisted release files.
