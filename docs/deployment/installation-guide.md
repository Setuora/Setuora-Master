# Master Windows installation

1. Prepare a Windows Server 2019+ or newer server that can reach the one central Tally HTTP/XML gateway. Give the server a durable database and backup location.
2. Run the `Setuora-Master-<version>-windows.cmd` installer as Administrator. It registers a Windows startup task and checks `http://127.0.0.1:8000/health`. The application stays bound to loopback.
3. Configure Tally on Master: the company, gateway host and port, voucher and ledger mappings, and master readiness. Enable only the supported voucher types after a successful Tally Check.
4. Install a reviewed HTTPS reverse proxy with a valid certificate. Route the authenticated `/api/v1` node endpoints to `127.0.0.1:8000`; do not publish the admin console, SQLite files, backups, or Tally port. Ensure the proxy preserves `Authorization` and request bodies. If it preserves the public `Host` header, add that DNS name to `TRUSTED_HOSTS` in Master's `.env` and restart Master.
5. Open **Franchises** (`/franchises`) and save the public Master HTTPS address once. Use the address Lite machines can reach, not the local admin address. Add a franchise with its permanent code, name, location, and Tally godown. Master creates the first node credential automatically. Select **Copy connection details** and transfer the copied JSON securely to that franchise's Lite administrator. The credential is shown only when issued; do not paste it into an email, public link, or support log.
6. On that Lite, open **Admin → Master connection** (`/master-connection`), paste the setup details, and select **Connect to Master**. This checks the credential and franchise identity before saving, initializes the local inventory baseline once, and sends the first updates. Verify inbound events, network stock, pending Tally batches, and Tally results on Master. Tally remains only at Master.

The copied connection details contain a credential. If they are lost or need replacement, use the franchise's connection replacement action and confirm the change. Its previous credential stops working; reconnect the existing Lite with the replacement details instead of creating a second franchise or resetting its database.

## Windows controls and updates

Double-click `setuora.bat` in the source checkout or installed folder. Both show the same menu for opening the browser, starting or stopping Setuora, checking status, setup/repair, updates, logs, and configuration checks. Closing this menu leaves the application running.

Setup, start, stop, update, and `preflight` request Administrator approval through Windows UAC. Complete password prompts and read errors in the visible Administrator console. Cancelling the approval does not complete the action.

For an installed copy, choose **Install downloaded update** and select the downloaded `Setuora-Master-<version>-windows.cmd` installer. For a source checkout, **Update from Git** requires a clean worktree and permits only a fast-forward update from its origin branch; it does not discard local changes. Back up the database and configuration before upgrading.

The installer and saved connection address do not configure public DNS, HTTPS certificates, or a reverse proxy, and do not expose SFTP. These network requirements must be completed separately before Lite can connect. If an older deployment had a public SFTP rule or a franchise-Tally exchange, retire that rule after migrating and verifying all nodes. Do not delete historical exchange files or credentials until they have been reviewed and backed up.

Follow the [warehouse acceptance procedure](go-live-validation.md) before live use.
Native Windows launcher, UAC, startup-task, and firewall behavior must be verified on the actual machines; automated application tests do not certify those operating-system functions.
Upgrade Master before Lite when deploying the updated discount-aware event schema.
