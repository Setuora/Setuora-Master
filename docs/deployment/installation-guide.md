# Windows Server Installation

1. Install Python 3.11 or newer and select **Add Python to PATH**.
2. Confirm that the server has a static public IP, or forward TCP 22 from the
   router to the server.
3. Run the Setuora Windows `.cmd` installer and approve the Administrator
   prompt.
4. Enter a unique first-administrator password.
5. Open `http://127.0.0.1:8000` on the server and sign in.
6. Enroll each franchise, then run `setuora.ps1 sftp-add CODE` in elevated
   PowerShell to create its isolated SFTP account.
7. Test SFTP from an external network before exchanging real Tally data.

Setup uses the Windows built-in OpenSSH capability and firewall rule. Setuora
runs as `SYSTEM` through the `Setuora-Master` startup task. Runtime files are:

- application: `C:\ProgramData\Setuora\Setuora-Master-windows`;
- exchange: `C:\ProgramData\Setuora\sftp`;
- database/backups: application `data` directory;
- log: application `logs\setuora.log`.

Keep the admin console on loopback. If remote administration is required, add
a separately reviewed HTTPS reverse proxy with authentication and update secure
cookie/host settings; SFTP exposure does not authorize publishing the console.
