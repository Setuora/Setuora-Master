# Master Windows installation

## Give the client one file

1. Use an x64 Windows 11 computer, or Windows 10 with active Extended Security Updates or a supported LTSC lifecycle. Master and Lite may share a computer; each keeps its own database, background task, and loopback port. Leave the computer powered on, connected to the internet, and able to reach the central Tally HTTP/XML gateway.
2. Copy `install-master.bat` to the Master computer. Double-click it and approve the Administrator prompt. It downloads the bootstrap from the public `Setuora/Setuora-Master` GitHub `main` branch, installs Git and Python when missing, downloads the latest source into `C:\ProgramData\Setuora\Setuora-Master`, installs the app, initializes its SQLite database and local backups, and starts its Windows boot task. Internet access to GitHub, python.org, package downloads, and Tailscale is required. If the GitHub repository is private, a single anonymous batch file cannot fetch it; provide the client an authenticated repository checkout or a packaged release installer instead.
3. Enter a unique administrator password of at least 12 characters when prompted. Complete Tailscale browser sign-in when prompted. If Tailscale asks the tailnet owner to approve MagicDNS or HTTPS certificates, complete that approval and double-click `install-master.bat` again. Setup verifies the selected local admin port and the private Master API. The admin console stays on this computer.
4. Open the installed `setuora.bat` and choose **Open in browser**. Sign in as `admin` with that password. Configure the Tally company, HTTP/XML gateway, voucher and ledger mappings, and master readiness. Use **Tally Check** before enabling supported voucher sync.
5. Open **Franchises** and create each franchise with its permanent code, name, location, and Tally godown. Copy its connection details and give them securely to that Lite administrator. On Lite, paste them into **Admin → Master connection** and connect. Verify inbound events and Tally results.

Double-click the same `install-master.bat` later to fetch a fast-forward update, make a verified SQLite snapshot before changing code, and run setup again. The updater refuses local code changes and a different Git origin or branch. It preserves `.env`, the database, backups, and logs. For the local controls menu, open `C:\ProgramData\Setuora\Setuora-Master\setuora.bat`. A source update cannot be rolled back automatically after a schema migration; retain the pre-update backup and previous source commit for recovery. Automatic backups are local by default, so machine loss also loses those copies. Set an off-machine backup destination in Settings when available.

To remove Master, open its installed `setuora.bat` and choose **Remove this installation**. The removal stops its own task and private API route, then keeps a final recovery backup outside the deleted application folder. It leaves a coinstalled Lite in place. Save the printed recovery location before closing the window.

The installer makes the application code readable but not writable by standard Windows users. It limits `.env`, the SQLite database, backups, and logs to SYSTEM and Administrators. Open the control menu normally; Windows asks for Administrator approval for setup, update, logs, and service controls.

## Packaged release installation

1. Prepare an x64 Windows 11 computer, or Windows 10 computer with active Extended Security Updates or a supported LTSC lifecycle, that can reach the one central Tally HTTP/XML gateway. Give the computer a durable database and backup location.
2. Run the `Setuora-Master-<version>-windows.cmd` installer as Administrator. It registers a Windows startup task and checks the selected local port. The application stays bound to loopback.
3. Configure Tally on Master: the company, gateway host and port, voucher and ledger mappings, and master readiness. Enable only the supported voucher types after a successful Tally Check.
4. Setup installs Tailscale through Windows Package Manager when available, or downloads and verifies Tailscale's signed MSI when it is not. When it prints a sign-in URL, complete the browser login. A tailnet administrator may need to enable MagicDNS and HTTPS certificates through the approval link printed by Tailscale; then run Setup / repair again. Setup enables unattended mode and publishes only `/api/v1/` through private Tailscale Serve. It checks that `/api/v1/node` requires authentication and the admin console is unavailable through that address. Keep Master and each Lite in the same tailnet.
5. Open **Franchises** (`/franchises`). Setup saves the `https://<machine>.<tailnet>.ts.net` address there when no different address is already configured. If another address is set, move existing Lite connections to Tailscale first and then save the new address. Add a franchise with its permanent code, name, location, and Tally godown. Master creates the first node credential automatically. Select **Copy connection details** and transfer the copied JSON securely to that franchise's Lite administrator. The credential is shown only when issued; do not paste it into an email, public link, or support log.
6. On that Lite, open **Admin → Master connection** (`/master-connection`), paste the setup details, and select **Connect to Master**. This checks the credential and franchise identity before saving, initializes the local inventory baseline once, and sends the first updates. Verify inbound events, network stock, pending Tally batches, and Tally results on Master. Tally remains only at Master.

The copied connection details contain a credential. If they are lost or need replacement, use the franchise's connection replacement action and confirm the change. Its previous credential stops working; reconnect the existing Lite with the replacement details instead of creating a second franchise or resetting its database.

## Windows controls and updates

Double-click `setuora.bat` in the source checkout or installed folder. Both show the same menu for opening the browser, starting or stopping Setuora, checking status, setup/repair, updates, logs, and configuration checks. Closing this menu leaves the application running.

Setup, start, stop, update, and `preflight` request Administrator approval through Windows UAC. Complete password prompts and read errors in the visible Administrator console. Cancelling the approval does not complete the action.

For an installed copy, choose **Install downloaded update** and select the downloaded `Setuora-Master-<version>-windows.cmd` installer. For a source checkout, **Update from Git** requires a clean worktree and permits only a fast-forward update from its origin branch; it does not discard local changes. Back up the database and configuration before upgrading.

The installer uses Tailscale's private DNS and HTTPS certificates; no public DNS purchase or inbound router port is needed. During a Git or installer update, Master stops its task and its own `/api/v1/` Serve route before replacing files, then starts and verifies both again. If Tailscale is temporarily offline, the local server can still stop and update; **Status** reports private API readiness after reconnection. It never resets other Serve routes or stops the Tailscale daemon. If another process owns Master's saved local port, setup selects a free port without stopping that process. If an older deployment had a public SFTP rule or a franchise-Tally exchange, retire that rule after migrating and verifying all nodes. Do not delete historical exchange files or credentials until they have been reviewed and backed up.

Follow the [warehouse acceptance procedure](go-live-validation.md) before live use.
Native Windows launcher, UAC, startup-task, and firewall behavior must be verified on the actual machines; automated application tests do not certify those operating-system functions.
Upgrade Master before Lite when deploying the updated discount-aware event schema.
