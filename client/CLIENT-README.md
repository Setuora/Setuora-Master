# Setuora Master Windows Package

The `.cmd` file is a self-extracting Windows installer and updater. Run it on
the Windows server. It requests Administrator access and installs Setuora under
`C:\ProgramData\Setuora\Setuora-Master-windows`.

Requirements are Windows Server 2019+ (or Windows 10/11 Pro for a pilot),
Python 3.11+, Internet access during the initial dependency installation, and a
public/NAT-forwarded TCP 22 endpoint.

Setup enables Windows OpenSSH SFTP, creates a Python virtual environment,
registers Setuora as a startup task, starts it, and verifies the local health
endpoint. It does not install Docker or a private-network client.

After enrolling a franchise in the web console, create its isolated SFTP
account from elevated PowerShell:

```powershell
& "C:\ProgramData\Setuora\Setuora-Master-windows\setuora.ps1" sftp-add BLR-01
```

Run a newer `.cmd` installer to update. The updater preserves `.env`, the
database, backups, SFTP folders, and Windows SFTP accounts.
