# Setuora Windows setup: client handoff

Use **x64 Windows 11** computers for Master and Lite. They may share one computer
when appropriate; setup assigns separate local ports. Windows 10 requires active [Extended
Security Updates](https://learn.microsoft.com/en-us/windows/whats-new/extended-security-updates)
or a supported LTSC edition. Keep each computer powered on and
connected to the internet. Have an Administrator account and access to the same
Tailscale network ready.

## 1. Install Master first

Give the central office this repository. They double-click `setuora.bat`, choose
**Start**, approve the Windows Administrator prompt, choose the first
Setuora administrator password, and complete the Tailscale sign-in link if shown.
If Tailscale asks the network owner to approve the device, MagicDNS, or HTTPS,
complete that approval and choose **Start** again.

Start opens the Master console in the browser. Set up the existing Tally
company and gateway in Setuora, then add each franchise. Copy its connection
details and share them privately with that franchise's Lite administrator.
Tally itself is a separately licensed application and is not installed by Start.

## 2. Install Lite at each location

Give that location the [Setuora-Lite](https://github.com/Setuora/Setuora-Lite) repository. They
double-click `setuora.bat`, choose **Start**, approve the Administrator prompt, choose the
first Setuora administrator password, and sign in to the **same Tailscale network**.
Follow any approval link and choose **Start** again if setup asks.

Start prints a private **https://...ts.net** Lite address. Staff computers
must join that Tailscale network and open this HTTPS address to use Lite, including
the camera scanner. In Lite, open **Admin → Master connection**, paste the
connection details copied from Master, and select **Connect to Master**. Confirm
that the first inventory baseline appears on Master before normal stock work.

## Later

Choose **Start** in either product's `setuora.bat` to get the latest `main`
branch and repair setup. Start keeps local code changes and a different
installation type untouched; it does not erase the database. **Stop** keeps the
product stopped, even after a restart, until Start.

To remove either product, open that product's installed `setuora.bat` and choose
**Uninstall**. Each removal stops that product and keeps a final
recovery bundle in `C:\ProgramData\Setuora\Recovery\Master` or
`C:\ProgramData\Setuora\Recovery\Lite`. Keep the recovery folder until the
data is no longer needed. A coinstalled product remains in place.

SQLite databases and automatic backups are created locally. You chose local
backups for now; copies on the same computer are lost if that computer or disk is
lost. An off-machine backup folder can be configured later in Setuora Settings.

**Release step for the developer:** Publish the new bootstrap and setup changes
to both public GitHub `main` branches before handing over a BAT by itself. A BAT
outside this workspace downloads its bootstrap from GitHub. Test one fresh
Windows 10/11 installation and one reboot on representative client computers
before live use.
