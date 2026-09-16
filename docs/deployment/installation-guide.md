# Master Windows installation

1. Prepare a Windows Server 2019+ or newer server that can reach the one central Tally HTTP/XML gateway. Give the server a durable database and backup location.
2. Run the `Setuora-Master-<version>-windows.cmd` installer as Administrator. It installs the service and checks `http://127.0.0.1:8000/health`. The application stays bound to loopback.
3. Configure Tally on Master: the company, gateway host and port, voucher and ledger mappings, and master readiness. Enable only the supported voucher types after a successful Tally Check.
4. Install a reviewed HTTPS reverse proxy with a valid certificate. Route the authenticated `/api/v1` node endpoints to `127.0.0.1:8000`; do not publish the admin console, SQLite files, backups, or Tally port. Ensure the proxy preserves `Authorization` and request bodies. If it preserves the public `Host` header, add that DNS name to `TRUSTED_HOSTS` in Master's `.env` and restart Master.
5. Test `/api/v1/node` using a credential from an external network, then enroll each franchise in **Franchises** and issue a separate node credential. Record the code and HTTPS origin for each Lite administrator.
6. Initialize inventory on each Lite and verify inbound events, network stock, pending Tally batches, and Tally results on Master.

The installer does not configure a reverse proxy or expose SFTP. If an older deployment had a public SFTP rule or a franchise-Tally exchange, retire that rule after migrating and verifying all nodes. Do not delete historical exchange files or credentials until they have been reviewed and backed up.
