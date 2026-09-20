# Warehouse deployment and acceptance

The active topology is **Lite → HTTPS → Master → central Tally XML gateway**.
Tally runs only at Master. Setuora stores its own SQLite databases and never
opens Tally company files. The older SFTP diagram with a Tally installation at
each franchise is not the deployment procedure for these builds.

## Installation layout

- Install Master once on the central server. Open its local admin console from
  `setuora.bat`; setup prints and saves the selected port. Keep its database on a local persistent disk.
- Install one Lite server for each independent warehouse/franchise. Staff PCs
  in that warehouse open the same Lite server in a browser, using separate user
  accounts. They do not need independent databases or Tally installations.
- A genuinely independent Lite installation needs its own permanent franchise
  code and credential. Do not clone a configured Lite database onto another
  simultaneously active machine, or share an SQLite file over a network drive.
- Master and Lite may share a Windows computer. Setup saves separate available
  loopback ports for the services. Verify each app through its own `setuora.bat`
  menu and check both private Tailscale routes.
- Use the packaged single application process. Tally import serialization is
  in-process; do not add extra Uvicorn workers or a second Master process against
  the same company/database.

## Before staff use the system

1. Back up any existing Setuora database and configuration and the central Tally
   company. Preserve existing logs. New schema columns are added at startup;
   preserve the pre-upgrade backup for rollback.
2. Install or upgrade Master first, then Lite. The updated Lite sends a discount
   field that older Master builds reject. Keep Lite delivery paused until Master
   is upgraded. Use Python 3.11; if Windows Package Manager is unavailable, install
   Python before running the installer. Installation requires access to the
   dependency package sources; these are not fully offline installers.
3. Run the installed `setuora.ps1 preflight`, `status`, and `logs` commands from
   an Administrator PowerShell terminal in the installation directory. Confirm
   `/health` reports the expected role. Log in with the installation password.
4. Give Lite a stable LAN address/DNS name and list the exact client-facing
   hostname or IP in `TRUSTED_HOSTS`. Test from a staff PC, not just the server.
   The default firewall rule applies to the Private profile. A domain-managed
   warehouse needs its administrator to provide the appropriate scoped Domain
   firewall rule. Never forward Lite's local web port or Tally port 9000 publicly.
5. Complete Tailscale setup on Master and Lite in the same tailnet. Setup
   publishes only Master's `/api/v1/` endpoints through private HTTPS Serve.
   Verify from a Lite machine that `/api/v1/node` returns 401 without a
   credential, then connect with the issued credential. Confirm `/`,
   `/maintenance`, `/settings`, database files, and the Tally gateway are
   inaccessible through the private address.
6. Enroll each Lite code and credential in Master. Configure each franchise's
   Tally godown before sending transactions. On Lite enter the exact HTTPS
   origin, initialize inventory once, and run Sync now. Confirm the baseline in
   Master before starting normal stock operations.
7. On Master configure the exact central company, voucher types, stock items,
   parties, tax ledgers, and godowns. Use Tally Check and start with a disposable
   test company. Set up Tally's own backup independently of Setuora's backup.
8. Configure and create an off-machine Setuora backup on both servers, then
   verify restoration on a separate test machine. Startup tasks run as SYSTEM:
   a UNC backup destination must permit the machine/service identity. A drive
   mapped only in an administrator's desktop session is insufficient.

## Acceptance on the actual Windows machines

Record the installed versions, Windows versions, Tally version, staff count,
and results. Complete these checks before switching to live transactions.

| Scenario                                                        | Required result                                                                                                        |
| --------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| Reboot each server without signing in                           | Correct app becomes healthy and staff PCs reconnect.                                                                   |
| Open Lite from every intended staff PC                          | Login, permissions, scan input, labels, and printing work with the actual hardware.                                    |
| Purchase, receive, discounted sale, sales return                | Master accepts each event once; Tally shows the correct company, party, amount, tax, stock item, and franchise godown. |
| Disconnect Lite from Master; submit stock work and a receipt    | Local data and pending events remain after restarting Lite; reconnecting drains the queue in order.                    |
| Pause background delivery in Lite; perform an allowed operation | The event still enters the local durable queue and sends after delivery resumes.                                       |
| Stop Tally; submit a supported voucher                          | Master retains the event and voucher; stock work remains available on Lite.                                            |
| Lose a Tally response or interrupt Master during import         | The uncertain voucher requires review; further central imports pause until reconciled.                                 |
| Dispatch two items, receive one then the other                  | Correct source/destination ownership and partial/full receipt state on both Lites and Master.                          |
| Scan the same serial simultaneously from two staff PCs          | Only one conflicting stock operation succeeds.                                                                         |
| Upgrade an existing populated installation                      | Database, connection identity, secrets, backups, and custom LAN hosts survive.                                         |
| Restore a test backup                                           | SQLite integrity and event sequences agree; verify Tally state separately before resuming delivery.                    |

Run the intended peak staff workload using realistic inventory size. Automated
tests are not a capacity benchmark or a substitute for scanners, printers,
Windows tasks/firewall, real certificates, and the installed Tally version.

## Recovery and daily operation

**Lite accepted by Master** means the event is durable on Master. It does not
mean the voucher is already imported into Tally. Check Master → Tally queue for
the accounting result. Purchase/receive/sale/sales-return events generate
supported vouchers; other inventory events and receipt review do not implicitly
create all possible Tally voucher types.

For a blocked Lite queue, inspect the oldest unsent event on Master connection,
resolve its reported identity/data/configuration issue, and use Retry. Do not
delete outbox rows, change event IDs, or reset the franchise sequence.

For a Master voucher marked `REVIEW_REQUIRED`, open its detail and inspect the
company in Tally. If the voucher exists, record its verified reference. Only if
you have confirmed it does not exist, confirm absence to resume the frozen XML
request. Do not repeatedly import the XML manually to clear the queue.

Restoring only one database to an older point can make event sequences disagree
or resurrect a voucher already in Tally. Stop delivery and reconcile Master,
Lite, and Tally histories before resuming. See the backup guides. Installers
preserve data but do not provide automatic rollback of an entire failed upgrade.

Check failed/review vouchers, oldest pending event age, offline nodes, backup
age, free disk space, and logs daily. The startup task retries crashes ten times
at one-minute intervals; persistent failures require operator attention. Arrange
log rotation and a UPS for the actual deployment.
