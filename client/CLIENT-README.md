# Setuora Master Client Package

The Linux and Windows Setuora installers are each delivered as one
self-extracting file. The same file handles a new installation and an update:

- when Setuora is not installed, it extracts, configures, starts, and verifies
  the complete application;
- when Setuora is already installed, it stops the application, replaces the
  application files, runs preflight checks, updates, restarts, and verifies it.

## Before installation

The client computer needs:

- a 64-bit Linux host with Docker Engine and Compose v2, or Windows 10/11 with
  Docker Desktop configured to use Linux containers;
- Python 3.11 or newer;
- outbound Internet access during installation and updates;
- a one-off, pre-authorized, non-ephemeral Tailscale auth key tagged
  `tag:setuora-master`;
- the first administrator password (minimum 12 characters).

Enable MagicDNS and HTTPS in Tailscale and apply the included
`deployment/tailscale/policy.hujson.example` policy before setup.

## Linux

Make the downloaded file executable and run it:

```bash
chmod +x Setuora-Master-<version>-linux.run
./Setuora-Master-<version>-linux.run
```

Setuora is installed under
`${XDG_DATA_HOME:-$HOME/.local/share}/setuora/Setuora-Master-linux`. For daily
administration:

```bash
~/.local/share/setuora/Setuora-Master-linux/setuora status
~/.local/share/setuora/Setuora-Master-linux/setuora logs
```

## Windows

Start Docker Desktop and double-click
`Setuora-Master-<version>-windows.cmd`. Windows may ask you to confirm that you
trust the local script. Setuora is installed under
`%LOCALAPPDATA%\Setuora\Setuora-Master-windows`. The installer prompts securely
for the administrator password and Tailscale key.

Daily commands can be run from PowerShell:

```powershell
& "$env:LOCALAPPDATA\Setuora\Setuora-Master-windows\setuora.ps1" status
& "$env:LOCALAPPDATA\Setuora\Setuora-Master-windows\setuora.ps1" logs
```

## Install an update

1. Export a verified backup from Setuora's **Maintenance** page.
2. Run the newer `.run` file on Linux or `.cmd` file on Windows.
3. The installer detects and updates the existing installation automatically.
4. Sign in and verify Tally connectivity and franchise status.

The single-file updater preserves `.env`, the database, backups, and the
Tailscale device identity. Never run
`docker compose down --volumes`; that command deletes persistent data.

For complete deployment and troubleshooting information, see
`docs/deployment/client-packages.md`.
