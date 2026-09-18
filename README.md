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

The former SFTP debtor/creditor worker, which assumed Tally at each franchise, is not started in this deployment. SFTP source and scripts remain for migration reference.

## Network boundary

The Windows application binds to `127.0.0.1:8000`. Setup installs or finds Tailscale, connects this computer to the tailnet, and uses private Tailscale Serve to publish only `/api/v1/` over HTTPS. The admin console and Tally port stay local. Each Lite makes outbound HTTPS requests; no public Lite port or purchased domain is required.

## Windows installation

Build and run the Windows installer as Administrator:

```powershell
py -3.11 scripts\build_client_packages.py --version 1.0.0
```

The installer places the application under `C:\ProgramData\Setuora\Setuora-Master-windows`, registers a startup task, and verifies `http://127.0.0.1:8000/health`. Then:

1. Configure the central Tally company and HTTP/XML gateway in Master's settings. Confirm the exact masters through **Tally Check** before enabling supported voucher sync.
2. Complete the Tailscale browser sign-in if prompted. Sign Lite computers into the same tailnet. Tailscale may ask a tailnet administrator to enable MagicDNS and HTTPS certificates. Setup checks the private HTTPS node endpoint and confirms the admin console is not shared. It prints the `https://<machine>.<tailnet>.ts.net` address.
3. Open **Franchises** (`/franchises`). Setup saves the private Master address there if no different address is already configured. If an old address is present, move existing Lite connections first, then save the new address and issue replacement details. Add each franchise with its permanent code and Tally godown. Adding a franchise creates its first credential and displays its setup details; select **Copy connection details** and give them to that Lite administrator securely.
4. On the matching Lite, open **Admin → Master connection** (`/master-connection`), paste the copied details, and select **Connect to Master**. Lite verifies the server and franchise, initializes its inventory baseline once, and starts synchronization. Monitor inbound events, pending vouchers, and failed Tally attempts on Master. Keep Tally only at Master.

Double-click `setuora.bat` in either a source checkout or an installed copy to use the same controls menu. Closing the menu leaves Setuora running. Setup, start, stop, update, and configuration checks (`preflight`) request Windows Administrator approval and show an interactive console for prompts and errors. An installed copy's update action asks you to choose its downloaded Windows `.cmd` installer; a source checkout uses Git and requires a clean worktree and a fast-forward update.

Tailscale Serve persists across reboots. Start and update restore the Setuora API route; stop and update remove only Setuora's own route while leaving the Tailscale network service online. Setup refuses to terminate an unrelated process on port 8000 or replace an overlapping Tailscale Serve route. The Windows controls still require acceptance testing on the actual deployment machines.

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
