# Master Windows installation

1. Prepare a Windows Server 2019+ or newer server that can reach the one central Tally HTTP/XML gateway. Give the server a durable database and backup location.
2. Run the `Setuora-Master-<version>-windows.cmd` installer as Administrator. It registers a Windows startup task and checks `http://127.0.0.1:8000/health`. The application stays bound to loopback.
3. Configure Tally on Master: the company, gateway host and port, voucher and ledger mappings, and master readiness. Enable only the supported voucher types after a successful Tally Check.
4. Setup installs Tailscale through Windows Package Manager when available, or downloads and verifies Tailscale's signed MSI when it is not. When it prints a sign-in URL, complete the browser login. A tailnet administrator may need to enable MagicDNS and HTTPS certificates through the approval link printed by Tailscale; then run Setup / repair again. Setup enables unattended mode and publishes only `/api/v1/` through private Tailscale Serve. It checks that `/api/v1/node` requires authentication and the admin console is unavailable through that address. Keep Master and each Lite in the same tailnet.
5. Open **Franchises** (`/franchises`). Setup saves the `https://<machine>.<tailnet>.ts.net` address there when no different address is already configured. If another address is set, move existing Lite connections to Tailscale first and then save the new address. Add a franchise with its permanent code, name, location, and Tally godown. Master creates the first node credential automatically. Select **Copy connection details** and transfer the copied JSON securely to that franchise's Lite administrator. The credential is shown only when issued; do not paste it into an email, public link, or support log.
6. On that Lite, open **Admin → Master connection** (`/master-connection`), paste the setup details, and select **Connect to Master**. This checks the credential and franchise identity before saving, initializes the local inventory baseline once, and sends the first updates. Verify inbound events, network stock, pending Tally batches, and Tally results on Master. Tally remains only at Master.

The copied connection details contain a credential. If they are lost or need replacement, use the franchise's connection replacement action and confirm the change. Its previous credential stops working; reconnect the existing Lite with the replacement details instead of creating a second franchise or resetting its database.

## Windows controls and updates

Double-click `setuora.bat` in the source checkout or installed folder. Both show the same menu for opening the browser, starting or stopping Setuora, checking status, setup/repair, updates, logs, and configuration checks. Closing this menu leaves the application running.

Setup, start, stop, update, and `preflight` request Administrator approval through Windows UAC. Complete password prompts and read errors in the visible Administrator console. Cancelling the approval does not complete the action.

For an installed copy, choose **Install downloaded update** and select the downloaded `Setuora-Master-<version>-windows.cmd` installer. For a source checkout, **Update from Git** requires a clean worktree and permits only a fast-forward update from its origin branch; it does not discard local changes. Back up the database and configuration before upgrading.

The installer uses Tailscale's private DNS and HTTPS certificates; no public DNS purchase or inbound router port is needed. During a Git or installer update, Master stops its task and its own `/api/v1/` Serve route before replacing files, then starts and verifies both again. If Tailscale is temporarily offline, the local server can still stop and update; **Status** reports private API readiness after reconnection. It never resets other Serve routes or stops the Tailscale daemon. If another process owns port 8000, setup reports it instead of terminating it. If an older deployment had a public SFTP rule or a franchise-Tally exchange, retire that rule after migrating and verifying all nodes. Do not delete historical exchange files or credentials until they have been reviewed and backed up.

Follow the [warehouse acceptance procedure](go-live-validation.md) before live use.
Native Windows launcher, UAC, startup-task, and firewall behavior must be verified on the actual machines; automated application tests do not certify those operating-system functions.
Upgrade Master before Lite when deploying the updated discount-aware event schema.
