# Setuora Master Installation

The supported installation is the same on Linux and Windows: Docker Compose
runs Setuora Master and a Tailscale sidecar. Start with the complete
[universal deployment guide](universal-deployment.md).

## Host requirements

- Linux with Docker Engine and Compose v2, or Windows with Docker Desktop;
- outbound Internet access for image pulls and Tailscale;
- sufficient persistent storage for the database and verified backups;
- a Tailscale tagged auth key;
- access to Tally Prime on the Docker host or a protected LAN endpoint.

No public IP, public DNS, router port-forward, Caddy process, or operating-system
service installer is required.

Keep these endpoints private:

- Uvicorn: container network port `8000`, mapped only to host loopback;
- Tally gateway: port `9000` on the Docker host or protected LAN;
- SQLite/PostgreSQL and backup files;
- the Docker control socket.

## Install

Clone the repository and run:

```bash
python deploy.py setup
```

The command is identical in Linux shells and Windows PowerShell. If Python is
not installed on the host:

1. copy `.env.example` to `.env`;
2. replace the application secret, administrator password, and Tailscale key;
3. apply the example Tailscale policy;
4. run `docker compose up -d --build`;
5. run `docker compose exec tailscale tailscale status` to find the URL.

The application and Tailscale state live in persistent Docker volumes, not in
the container layers.

## Verify

```bash
python deploy.py status
```

The local health endpoint is:

```text
http://127.0.0.1:8000/health
```

Expected response:

```json
{ "status": "ok", "role": "master" }
```

Open the private HTTPS address printed by setup from an operator device admitted
to the tailnet. Verify secure cookies, login/logout, security headers, and role
restrictions.

## Updates

Before an update:

1. create and export a verified backup;
2. record the current application revision;
3. confirm the Lite compatibility window;
4. schedule a low-traffic maintenance window;
5. keep a tested rollback package.

After updating the source, run:

```bash
python deploy.py update
```

The command rebuilds the image and preserves `.env`, database, backups, and the
Tailscale identity.

## Production gates

The universal installer produces the bounded single-process SQLite pilot.
Before production scale, complete PostgreSQL migration, formal schema
migrations, shared rate limiting, worker leasing, encrypted offsite recovery,
monitoring, key-rotation runbooks, and every acceptance test in
[remote-franchise-connectivity.md](remote-franchise-connectivity.md).
