# Windows Installer Package

Build a deterministic Windows self-extracting installer with:

```powershell
py -3.11 scripts\build_client_packages.py --version 1.0.0
```

The output contains the reviewed application, Windows lifecycle/SFTP scripts,
runtime lock file, and deployment documentation. It excludes `.env`, databases,
backups, credentials, caches, and generated SFTP data. A SHA-256 checksum file
is written beside the installer.

Running a newer installer stops the startup task, replaces application files,
reinstalls locked dependencies, and starts the task again. Persistent files and
Windows accounts are preserved.
