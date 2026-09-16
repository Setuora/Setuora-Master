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

The Windows application binds to `127.0.0.1:8000`. Keep the admin console and Tally port private. Expose only the authenticated `/api/v1` node endpoints to Lite servers through a reviewed HTTPS reverse proxy with a valid certificate. Each Lite makes outbound HTTPS requests; no public Lite port is required. The installer does not set up the reverse proxy.

## Windows installation

Build and run the Windows installer as Administrator:

```powershell
py -3.11 scripts\build_client_packages.py --version 1.0.0
```

The installer places the application under `C:\ProgramData\Setuora\Setuora-Master-windows`, registers a startup task, and verifies `http://127.0.0.1:8000/health`. Then:

1. Configure the central Tally company and HTTP/XML gateway in Master's settings. Confirm the exact masters through **Tally Check** before enabling supported voucher sync.
2. Configure the HTTPS reverse proxy for `/api/v1` and test it from an external network.
3. Open **Franchises** (`/franchises`) and save the public Master HTTPS address once. Add each franchise with its permanent code and Tally godown. Adding a franchise creates its first credential and displays its setup details; select **Copy connection details** and give them to that Lite administrator securely.
4. On the matching Lite, open **Admin → Master connection** (`/master-connection`), paste the copied details, and select **Connect to Master**. Lite verifies the server and franchise, initializes its inventory baseline once, and starts synchronization. Monitor inbound events, pending vouchers, and failed Tally attempts on Master. Keep Tally only at Master.

Double-click `setuora.bat` in either a source checkout or an installed copy to use the same controls menu. Closing the menu leaves Setuora running. Setup, start, stop, update, and configuration checks (`preflight`) request Windows Administrator approval and show an interactive console for prompts and errors. An installed copy's update action asks you to choose its downloaded Windows `.cmd` installer; a source checkout uses Git and requires a clean worktree and a fast-forward update.

Saving the Master address does not provision public DNS, HTTPS, or a certificate. Complete that network setup separately before connecting Lite. The Windows controls still require acceptance testing on the actual deployment machines.

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
