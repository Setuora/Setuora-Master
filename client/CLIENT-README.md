# Setuora Master Windows Package

The `.cmd` file is a self-extracting Windows installer and updater. Run it on
the Windows server. It requests Administrator access and installs Setuora under
`C:\ProgramData\Setuora\Setuora-Master-windows`.

Requirements are x64 Windows 10 or 11 (Windows 10 needs active security updates or a supported LTSC lifecycle),
Internet access during the initial dependency installation, and
network access to the central Tally HTTP/XML gateway.

Setup creates a Python virtual environment, registers Setuora as a startup
task, starts it, and verifies the local health endpoint. It does not expose a
public service or install Docker.
Setup selects an unused local port if 8000 is occupied and saves that port for
later starts and updates. Master and Lite can share a computer.

Setup installs or finds Tailscale and configures private HTTPS access to only
`/api/v1/`. Complete the Tailscale browser login when prompted, and sign Lite
computers into the same tailnet. Tailscale may ask a tailnet administrator to
enable MagicDNS and HTTPS certificates. The admin console and central Tally
gateway remain local.

Open **Franchises** (`/franchises`) in the Master web console. Setup saves its
private Master HTTPS address when no different address is already configured.
Add each franchise with its permanent code
and Tally godown. Master creates its first credential automatically. Select
**Copy connection details** and transfer the copied JSON securely to that
Lite administrator. On Lite, paste it into **Admin → Master connection** and
select **Connect to Master**. Lite verifies the connection, initializes its
inventory baseline once, and starts synchronization. Tally runs only at Master.

Double-click
`C:\ProgramData\Setuora\Setuora-Master-windows\setuora.bat` for the controls
menu: **Start**, **Stop** and **Uninstall**. Start repairs and runs Master,
then opens the browser at the saved local port. Stop keeps Master stopped,
even after Windows restarts, until you choose Start. Uninstall keeps a final
recovery backup. Each action opens a visible Administrator console after
Windows approval. Closing the menu leaves Master in its current state.

To update, run the newer Master `.cmd` installer directly, or run
`setuora.bat update` and select it. The updater preserves `.env`, the
database, and backups. The updater restores and verifies the private API route.

See the [installation guide](docs/deployment/installation-guide.md).
