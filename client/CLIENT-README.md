# Setuora Master Windows Package

The `.cmd` file is a self-extracting Windows installer and updater. Run it on
the Windows server. It requests Administrator access and installs Setuora under
`C:\ProgramData\Setuora\Setuora-Master-windows`.

Requirements are Windows Server 2019+ (or Windows 10/11 Pro for a pilot),
Python 3.11+, Internet access during the initial dependency installation, and
network access to the central Tally HTTP/XML gateway.

Setup creates a Python virtual environment, registers Setuora as a startup
task, starts it, and verifies the local health endpoint. It does not expose a
public service or install Docker.

Configure a reviewed HTTPS reverse proxy that exposes only `/api/v1` to Lite
servers, with working public DNS and a valid certificate. Keep the Master
admin console and central Tally gateway private. The installer does not
configure this public HTTPS access.

Open **Franchises** (`/franchises`) in the Master web console and save the
public Master HTTPS address once. Add each franchise with its permanent code
and Tally godown. Master creates its first credential automatically. Select
**Copy connection details** and transfer the copied JSON securely to that
Lite administrator. On Lite, paste it into **Admin → Master connection** and
select **Connect to Master**. Lite verifies the connection, initializes its
inventory baseline once, and starts synchronization. Tally runs only at Master.

Double-click
`C:\ProgramData\Setuora\Setuora-Master-windows\setuora.bat` for the controls
menu: browser, start/stop, status, setup/repair, update, logs, and configuration
checks. Closing the menu leaves Setuora running. Actions requiring
Administrator access open a visible console after Windows approval.

Choose **Install downloaded update** and select a newer Master `.cmd`
installer, or run that installer directly. The updater preserves `.env`, the
database, and backups. The HTTPS proxy is configured and backed up separately.

See the [installation guide](docs/deployment/installation-guide.md).
