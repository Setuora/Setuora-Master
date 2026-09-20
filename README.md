# Setuora Master

Setuora Master is the central service for the franchise network. It owns the consolidated Setuora database and is the only Setuora application that connects to Tally. Tally remains a separate application; Setuora sends supported voucher XML through Tally's HTTP/XML gateway and never edits Tally company files directly.

## Current architecture

```text
Lite A ── HTTPS events/commands ──┐
Lite B ── HTTPS events/commands ──┼── Master node API and database
Lite C ── HTTPS events/commands ──┘              │
                                        durable pending voucher queue
                                                 │
                                        central Tally XML gateway
```

Each Lite has a unique franchise identity and bearer credential. Master checks event order and identity, applies each event idempotently, and records network inventory and transfers. Purchase, receive, sale, and sales return events become pending Tally batches. A single Master retry worker processes pending supported vouchers and records success or failure. Acknowledging an event to Lite means Master accepted it; Tally may still be pending.

Master allocates new franchise QR serials and queues their product and serial data for the assigned Lite. Lite receives the records through its normal command poll, stores them locally, and can then use the labels in franchise workflows. Commands remain queued while Lite is offline and are acknowledged after Lite applies them. Generate labels for a franchise in Master after its connection has been set up.

To issue QRs, open **Franchises → Create QR codes** for the target franchise. Enter its Lite product code and quantity; if Master has not seen that product yet, also enter its product details. The allocation page shows delivery status and printable labels. Use **Franchises → Replace a QR code** for damaged labels; Master reserves the new serial, Lite applies the replacement, and Master records the completion event. Historical Lite stock is imported only during the initial inventory enrollment.

Before upgrading an existing network to this QR workflow, let every Lite deliver its pending events from locally created QR labels. Master now rejects new purchase or stock events for serials it did not issue, apart from each node's initial historical inventory enrollment. An offline Lite with older unsent QR events needs to reconnect and drain that queue before the upgrade.

The former SFTP debtor/creditor worker, which assumed Tally at each franchise, is not started in this deployment. SFTP source and scripts remain for migration reference.

## Network boundary

The Windows application binds to an available loopback port, starting with `127.0.0.1:8000`. Setup saves the selected port and reuses it on updates; if another application occupies it, Setup / repair selects a new free port. Open the installed `setuora.bat` and choose **Open in browser** for the current address. Setup installs or finds Tailscale, connects this computer to the tailnet, and uses private Tailscale Serve to publish only `/api/v1/` over HTTPS. The admin console and Tally port stay local. Each Lite makes outbound HTTPS requests; no public Lite port or purchased domain is required.

## Windows installation

For a fresh x64 Windows 10 or 11 computer, give the client just [install-master.bat](install-master.bat). They should double-click it, approve the Administrator prompt, choose a first administrator password when asked, and complete the Tailscale browser sign-in. The batch file downloads the current bootstrap from the public Setuora GitHub repository, installs Git and Python if needed, fetches the latest `main` source into `C:\ProgramData\Setuora\Setuora-Master`, installs the locked runtime, creates a Windows boot task, initializes the SQLite database, enables automatic local backups, and verifies the local server and private API. Double-click the same file later to fetch a safe fast-forward update and repair setup. The computer needs internet access during installation and must stay powered on for the service and Tally connection.

This Git installation uses its own folder. If the older packaged `Setuora-Master-windows` installation exists, use that installation's release updater instead of running this batch file. Master and Lite can run on the same computer with separate databases and Windows tasks; Tailscale Serve gives Lite `/` and Master `/api/v1/`. The current locked runtime supports x64; Windows ARM64 and 32-bit are not supported. Windows 10 production hosts need active Extended Security Updates or a supported LTSC lifecycle.

The client still completes the Tally company and gateway settings in the local console and adds each franchise. Tailscale sign-in and any tailnet HTTPS or MagicDNS approval require the account owner. The first administrator password is entered locally and never printed by setup. Automatic SQLite backups stay on this computer by default; a lost computer also loses those copies. An off-machine backup destination can be added in Settings later.

### Packaged release installer

Build and run the Windows installer as Administrator:

```powershell
py -3.11 scripts\build_client_packages.py --version 1.0.0
```

The installer places the application under `C:\ProgramData\Setuora\Setuora-Master-windows`, registers a startup task, and verifies the selected local port. Open `setuora.bat` for the current address. Then:

1. Configure the central Tally company and HTTP/XML gateway in Master's settings. Confirm the exact masters through **Tally Check** before enabling supported voucher sync.
2. Complete the Tailscale browser sign-in if prompted. Sign Lite computers into the same tailnet. Tailscale may ask a tailnet administrator to enable MagicDNS and HTTPS certificates. Setup checks the private HTTPS node endpoint and confirms the admin console is not shared. It prints the `https://<machine>.<tailnet>.ts.net` address.
3. Open **Franchises** (`/franchises`). Setup saves the private Master address there if no different address is already configured. If an old address is present, move existing Lite connections first, then save the new address and issue replacement details. Add each franchise with its permanent code and Tally godown. Adding a franchise creates its first credential and displays its setup details; select **Copy connection details** and give them to that Lite administrator securely.
4. On the matching Lite, open **Admin → Master connection** (`/master-connection`), paste the copied details, and select **Connect to Master**. Lite verifies the server and franchise, initializes its inventory baseline once, and starts synchronization. Monitor inbound events, pending vouchers, and failed Tally attempts on Master. Keep Tally only at Master.

Double-click `setuora.bat` in either a source checkout or an installed copy to use the same controls menu. Closing the menu leaves Setuora running. Setup, start, stop, update, and configuration checks (`preflight`) request Windows Administrator approval and show an interactive console for prompts and errors. An installed copy's update action asks you to choose its downloaded Windows `.cmd` installer; a source checkout uses Git and requires a clean worktree and a fast-forward update.

Tailscale Serve persists across reboots. Start and update restore the Setuora API route; stop and update remove only Setuora's own route while leaving the Tailscale network service online. Setup selects another local port if the current one belongs to another application and leaves that process untouched. The Windows controls still require acceptance testing on the actual deployment machines.

Back up the Master database and its configuration. The central Tally company needs its own supported backup process.

## Development

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --require-hashes -r requirements.lock
copy .env.example .env
python -m pytest -q
```

See [central Tally topology](docs/architecture/central-tally-topology.md) and [installation guide](docs/deployment/installation-guide.md). The SFTP/Tally documents describe the previous franchise-Tally deployment.

Before live deployment, complete the [warehouse acceptance procedure](docs/deployment/go-live-validation.md). Upgrade Master before Lite when installing the discount-aware event schema. Uncertain Tally imports pause the central queue for operator reconciliation.
