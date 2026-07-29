# Setuora Master Windows Service

The Windows pilot runs one Uvicorn process with the in-process Tally retry and
backup workers. Do not install multiple application instances against SQLite.

## Automated installation

Run:

```powershell
.\Setuora.exe setup --install-dir C:\Setuora-Master
```

The setup utility can install the application and private Caddy services with
automatic startup and recovery.

## Manual NSSM installation

After creating `.venv` and `.env`, run from an Administrator PowerShell:

```powershell
.\deployment\windows\install_service.ps1 `
  -ProjectDir "C:\Setuora-Master" `
  -NssmPath "C:\Tools\nssm\nssm.exe" `
  -Port 8000
```

The service command is:

```text
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The helper runs the application as `NT AUTHORITY\LocalService`, grants read
access to the application, and grants write access only to `data\` and `logs\`.

The internal service name remains `SetuoraQrTallyBridge` for upgrade
compatibility with existing installations. It does not change the Master
application boundary.

## Logs

```text
logs\setuora-out.log
logs\setuora-err.log
```

Logs rotate at the configured size. Protect them because operational errors can
contain business identifiers. They must never contain node secrets or
authorization headers.

## Controls

```powershell
.\Setuora.exe start
.\Setuora.exe stop
.\Setuora.exe repair
```

The executable requests elevation when service control requires it.

## Private proxy

The optional local Caddy service provides private administrative HTTPS only.
Keep it dependent on the application service and restrict its firewall rule to
the management network.

The public Node Sync edge is a separate host and configuration. See
[master-internet-edge.md](master-internet-edge.md).

## Verification

After installation:

1. verify both services use automatic startup;
2. verify the application service uses `LocalService`;
3. restart Windows;
4. confirm `http://127.0.0.1:8000/health` reports `role=master`;
5. confirm the private HTTPS health endpoint;
6. confirm the administrative site is unreachable from unapproved networks;
7. create a verified backup;
8. inspect service logs for restart loops.

Run the [pilot release checklist](production-release-checklist.md) before
acceptance.
