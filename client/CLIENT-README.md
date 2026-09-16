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
servers. Keep the Master admin console and central Tally gateway private. In
the web console, enroll each franchise and issue a separate node credential.
Each Lite needs its franchise code, Master HTTPS origin, and credential.

Run a newer `.cmd` installer to update. The updater preserves `.env`, the
database, and backups. The HTTPS proxy has a separate configuration and
backup lifecycle.
