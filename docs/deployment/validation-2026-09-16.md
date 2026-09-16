# Setuora Lite and Master validation — 16 September 2026

For the latest Windows controls, simplified connection setup, test results, and
replacement pilot installers, see [setup validation](setup-validation-2026-09-16.md).

## Deployment decision

The code has been reviewed and corrected for **Tally only on Master**. The
deliverables are validation builds for a Windows pilot. Actual Windows install,
reboot, firewall, HTTPS, hardware, and real Tally acceptance remain necessary
before production use. No validation can promise that failures will never occur.

Use one independent Lite database per warehouse/franchise; staff PCs normally
share that Lite through a browser. Master and Lite use separate SQLite databases.
Master alone posts supported voucher XML through the central Tally gateway.
The old SFTP/franchise-Tally diagram is historical. Active synchronization uses
HTTPS events and commands, not debtor/creditor XML imports on client PCs.

## Corrected findings

| Finding                                                                       | Result                                                                                                                                                                                        |
| ----------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Pausing Lite transport could bypass the Master queue or skip events           | Lite retains supported operations in the durable outbox even while delivery is paused. An uninitialized node fails visibly instead of silently routing work to local Tally.                   |
| Sales discounts were missing from network events                              | Lite sends discounts; both editions preserve the submitted line discount so later product edits do not rewrite that discount.                                                                 |
| Zero line rates could fall back to the product default                        | Explicit zero and missing rates are distinguished. Master rejects non-finite numeric input.                                                                                                   |
| Franchise godown configuration was ignored when building vouchers             | Master stores the franchise godown on the inbound batch for voucher generation.                                                                                                               |
| Lost Tally replies could be retried after a successful import                 | Ambiguous replies, interrupted imports, and mixed success/error responses require operator reconciliation and pause further central imports. Definite connection refusal has bounded retries. |
| Lite recovery UI did not expose the existing retry handler                    | The blocked oldest event is visible and an authenticated retry route is available.                                                                                                            |
| A conflicting command replay could overwrite proof of an applied command      | The applied command journal survives the conflict; replay stays idempotent. Unsupported command schema versions are rejected.                                                                 |
| Truncated HTTP replies could leave delivery permanently blocked               | Retryable protocol failures remain queued without discarding the event.                                                                                                                       |
| Different underscore/hyphen franchise codes could generate the same QR prefix | The permanent franchise namespace preserves those distinct codes.                                                                                                                             |
| Lite settings still presented local Tally configuration                       | The Lite UI directs administrators to Master connection.                                                                                                                                      |
| Windows tasks used finite/default runtime and battery behavior                | Explicit continuous-runtime, battery, single-instance, and crash-restart task settings are generated.                                                                                         |
| Repair could overwrite a custom database path or LAN trusted hosts            | Existing paths and hosts are preserved; setup/update run preflight before runtime changes.                                                                                                    |
| Update could replace files while the old app was running                      | The deployment helper verifies the web port closes before proceeding; packaged payloads are staged before the existing app is stopped.                                                        |
| Installer elevation and Python discovery had inconsistent behavior            | Packaged elevation waits and propagates failure; launchers prefer the installed environment/Python 3.11 and Master supports Python installation through winget.                               |
| Installer quoting and application configuration parsing disagreed             | Double-quoted password/path escapes are read consistently, including quotes and backslashes.                                                                                                  |

Master also rejects inventory events whose product tax fields or Tally identity
conflict with the serial's recorded product, before committing stock changes or
queuing an incorrect voucher.

Final installer checks also fixed Python-version probing under Windows
PowerShell 5.1 and invalidated application bytecode caches after a stopped
upgrade. Reproducible ZIP timestamps previously allowed a same-size source
change to reuse stale compiled code. Cache cleanup is limited to the installed
application directory; it does not delete databases or configuration.

## Verification

Tests execute on Linux with isolated test databases. The Master full suite passed
**170 tests** and the Lite full suite passed **365 tests**. Lite's 102 warnings are deprecations from the existing test HTTP
client, not test failures. TestClient checks required execution outside this
environment's restrictive sandbox because the sandbox stalls its AnyIO event
loop. Full-suite commands from each edition directory:

```text
.venv/bin/python -m pytest -q
```

After the final launcher/cache changes, the package suites passed **6 checks
per edition** (`tests/test_client_packages.py`), including the four newly added
regressions. Master Ruff checks passed for app/deployment/tests; Lite changed
application files passed the Ruff F checks. Both working-tree diffs passed
whitespace checks.

Fresh-start checks exercise each real app lifespan twice, authenticate the
installer-escaped administrator password, request health/login/home, create and
verify a SQLite backup, and confirm the administrator survives restart. These
checks execute application code on Linux, not Windows Task Scheduler.

The cross-edition regression uses two independent persistent Lite databases and
the actual Master API handlers. Each Lite action runs in a new Python process.
It covers lost acknowledgement after commit, restart and duplicate delivery,
discounted sale/godown, two-item partial/full transfer receipt, command replay,
ownership, and command acknowledgements. Transport is simulated through the
Master ASGI client; no production server or Tally company is contacted.

## Windows validation packages

- `Setuora-Master/dist/Setuora-Master-1.0.0-validation-windows.cmd`
- `Setuora-Lite/dist/Setuora-Lite-1.0.0-validation-windows.cmd`

Each dist directory also contains a ZIP and checksum manifest. These are newly
built validation packages, not evidence of a completed installation on Windows.
Use the matching checksum manifest when copying packages to the target machines.

## Remaining deployment requirements

- Complete the [warehouse acceptance procedure](go-live-validation.md) on the
  intended Windows machines, with the actual Tally test company, scanners, and
  printers. Test realistic peak staff activity; automated tests are not a load
  capacity certification.
- Configure and verify the Master HTTPS proxy/certificate separately. The
  installer does not publish a ready-made public API endpoint.
- Upgrade **Master before Lite**. Older Master builds reject the new discount
  field. Check all blocked events and vouchers before and after the upgrade.
- Keep one packaged application process per installation. The central Tally
  import lock does not coordinate separately launched Master processes.
- The Tally retry scheduler does not guarantee strict chronological voucher
  order. Verify the intended purchase/sale sequence in the Tally test company,
  especially after outages; review earlier failed vouchers before releasing
  dependent accounting work. Lite event acceptance itself remains ordered.
- Use separate hosts/VMs for the packaged Master and Lite services: both use
  port 8000. Client PCs sharing a warehouse should use its Lite web address.
- Maintain off-machine Setuora backups and a separate supported Tally backup.
  Restore and reconcile accepted event sequences before resuming traffic.
  There is no complete automatic upgrade rollback.
- Watch pending events, review-required vouchers, backup age, disk space, and
  logs. Configure deployment log retention. A startup task has ten crash
  retries; persistent failures still require an operator.
- Active Tally vouchers are purchase, receive, sale, and sales return. Other
  stock events update Setuora network state; receipt review is a separate flow.
  The historical SFTP debtor/creditor delta-import architecture is not active.

No live deployment, production-data mutation, or real Tally import was performed
as part of this validation.
