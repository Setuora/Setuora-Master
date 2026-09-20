# Setuora Windows setup: client handoff

Use **x64 Windows 11** computers for Master and Lite. They may share one computer
when appropriate; setup assigns separate local ports. Windows 10 requires active [Extended
Security Updates](https://learn.microsoft.com/en-us/windows/whats-new/extended-security-updates)
or a supported LTSC edition. Keep each computer powered on and
connected to the internet. Have an Administrator account and access to the same
Tailscale network ready.

## 1. Install Master first

Give the central office [install-master.bat](../../install-master.bat).
They double-click it, approve the Windows Administrator prompt, choose the first
Setuora administrator password, and complete the Tailscale sign-in link if shown.
If Tailscale asks the network owner to approve the device, MagicDNS, or HTTPS,
complete that approval and double-click the BAT again.

On the Master computer, open `setuora.bat` and choose **Open in browser**. Set up the existing Tally
company and gateway in Setuora, then add each franchise. Copy its connection
details and share them privately with that franchise's Lite administrator.
Tally itself is a separately licensed application and is not installed by the BAT.

## 2. Install Lite at each location

Give that location [install-lite.bat](https://github.com/Setuora/Setuora-Lite/blob/main/install-lite.bat). They
double-click it, approve the Administrator prompt, choose the first Setuora
administrator password, and sign in to the **same Tailscale network**. Follow any
approval link and rerun the BAT if setup asks.

The installer prints a private **https://...ts.net** Lite address. Staff computers
must join that Tailscale network and open this HTTPS address to use Lite, including
the camera scanner. In Lite, open **Admin → Master connection**, paste the
connection details copied from Master, and select **Connect to Master**. Confirm
that the first inventory baseline appears on Master before normal stock work.

## Later

Double-click the same product BAT on its own computer to get the latest
`main` branch and repair setup. The BAT refuses local code changes or a different
installation type; it does not erase the database. The installed `setuora.bat`
provides daily controls and status.

To remove either product, open that product's installed `setuora.bat` and choose
**Remove this installation**. Each removal stops that product and keeps a final
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
