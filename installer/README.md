# Setuora Master Windows Installer

`Setuora.exe` is the Windows control utility for a Setuora Master pilot host. It
provides setup, repair, update, start, and stop commands. Setup requests
administrator access, installs Git for Windows when needed, clones or updates
the official Setuora Master repository, and launches `scripts\setup.bat`.

The setup workflow installs Python when needed, creates the virtual environment,
installs Python dependencies, creates or preserves `.env` and application data,
runs validation, forces `SETUORA_APP_MODE=master`, configures optional private
HTTPS, and installs the Windows services. It does not deploy the public Node
Sync edge, WireGuard, PostgreSQL, an administrative VPN, or Tally. Repair
rebuilds a damaged virtual environment, reinstalls verified dependencies,
repairs service startup, preserves settings and data, runs the full tests, and
starts the services.

## Build

From this directory with Go installed:

```text
go test ./...
set GOOS=windows
set GOARCH=amd64
go build -trimpath -ldflags="-s -w" -o ..\Setuora.exe .
```

Linux/macOS can cross-compile it with:

```bash
GOOS=windows GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o ../Setuora.exe .
```

The installer requires internet access. The generated executable is unsigned;
Windows SmartScreen may therefore ask the operator to confirm that it should run.

## Options

```text
Setuora.exe setup --install-dir C:\Setuora-Master --branch main
Setuora.exe setup --with-caddy=false
Setuora.exe repair
Setuora.exe update
Setuora.exe start
Setuora.exe stop
```

Updates refuse uncommitted source changes. If a clean installed checkout has
diverged from official release history, its prior commit is preserved on a
timestamped `setuora-backup/...` branch before the verified release is applied.
Application data, settings, and backups are not changed.
