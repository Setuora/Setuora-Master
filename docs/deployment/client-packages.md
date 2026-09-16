# Windows Installer Package

Build a deterministic Windows self-extracting installer with:

```powershell
py -3.11 scripts\build_client_packages.py --version 1.0.0
```

The output contains the reviewed application and Windows lifecycle scripts. Legacy SFTP scripts may be packaged for migration reference,
runtime lock file, and deployment documentation. It excludes `.env`, databases,
backups, credentials, caches, and generated legacy SFTP data. A SHA-256 checksum file
is written beside the installer.

Running a newer installer stops the startup task, replaces application files,
reinstalls locked dependencies, and starts the task again. Persistent files and
Windows accounts are preserved.
