# Universal Linux and Windows Deployment

Setuora Master has one supported self-hosted deployment model for Linux and
Windows: Docker Compose plus a Tailscale sidecar. It does not require Caddy,
public DNS, a static IP address, router port-forwarding, or Windows services.

```text
Franchise Lite (tag:setuora-lite)
  -> outbound Tailscale/WireGuard
  -> private HTTPS :443
  -> Tailscale Serve (tag:setuora-master)
  -> Setuora :8000 in the shared container network
  -> persistent database / private Tally gateway
```

Tailscale usually connects peers directly and can relay encrypted traffic when
NAT or firewall conditions prevent a direct path. All sites initiate outbound
connections. Setuora remains self-hosted on the Master machine; Tailscale
coordinates device identity and connectivity.

This deployment uses Tailscale Serve, not Funnel. It is reachable from approved
tailnet devices outside the local network, but it is not published to anonymous
Internet clients.

## Prerequisites

Install:

- Linux: Docker Engine and the Compose v2 plugin;
- Windows: Docker Desktop using Linux containers;
- Python 3.11 only if using `deploy.py` (otherwise use Compose directly).

In the Tailscale admin console:

1. enable MagicDNS and HTTPS certificates;
2. copy
   [`deployment/tailscale/policy.hujson.example`](../../deployment/tailscale/policy.hujson.example)
   into the access-control editor;
3. replace `operator@example.com` with the real operator account;
4. generate a one-off, non-ephemeral, pre-authorized auth key tagged
   `tag:setuora-master`.

Generate a separate tagged key for each Lite installation or use an approved
automated key-provisioning process. Lite keys use `tag:setuora-lite`. Do not
share the Master key with franchises.

## Recommended setup

Clone the repository, open a terminal in its root, and run the same command on
Linux or Windows:

```bash
python deploy.py setup
```

The helper:

- verifies that Docker and Compose are running;
- creates `.env` when needed;
- generates a strong application secret;
- securely prompts for the first administrator password and Tailscale key;
- builds and starts both containers;
- verifies the local health endpoint;
- replaces the bootstrap trusted-host wildcard with the exact MagicDNS name;
- removes the enrollment key and bootstrap password from `.env` after both
  identities are persisted;
- prints the private HTTPS `*.ts.net` URL.

The prompt does not put either secret in shell history. `.env` is excluded from
Git and the Docker build context.

For non-interactive setup, prepare `.env` first:

```text
APP_SECRET_KEY=<at-least-32-random-characters>
BOOTSTRAP_ADMIN_PASSWORD=<unique-password-at-least-12-characters>
TAILSCALE_AUTH_KEY=<one-off-tagged-non-ephemeral-key>
TAILSCALE_HOSTNAME=setuora-master
TAILSCALE_TAG=tag:setuora-master
TRUSTED_HOSTS=*.ts.net,127.0.0.1,localhost
SESSION_COOKIE_SECURE=true
```

Then run:

```bash
docker compose up -d --build
docker compose exec tailscale tailscale status
```

## Daily lifecycle

These commands are identical on both operating systems:

```bash
python deploy.py status
python deploy.py preflight
python deploy.py logs
python deploy.py logs setuora
python deploy.py logs tailscale
python deploy.py stop
python deploy.py start
python deploy.py update
```

`stop` preserves the application data and Tailscale identity volumes. Never run
`docker compose down --volumes` during normal operation because that deletes
both persistent volumes.

Compose binds `SETUORA_LOCAL_PORT` to `127.0.0.1` only. It exists for local
health and troubleshooting; remote users must use the HTTPS Tailscale URL.

## Connect a franchise

Each franchise needs two independent credentials:

1. a Tailscale device identity tagged `tag:setuora-lite`, which grants network
   access to Master port `443`;
2. the one-time Setuora node API key issued for that franchise from
   **Master → Franchises**, which binds sync data to exactly that franchise.

On the Lite host:

1. install Tailscale or its approved container sidecar;
2. authenticate it with that installation's Lite key;
3. confirm it can resolve and reach the Master `*.ts.net` address;
4. set `MASTER_URL=https://<master-name>.<tailnet>.ts.net`;
5. set the Setuora node API key;
6. start Lite sync and verify that the franchise becomes online in Master.

Lite requires no inbound port. The example grants contain no Lite-to-Lite rule,
so franchise devices cannot connect to one another.

## Tally connectivity

Never publish Tally port `9000` through Compose or Tailscale.

- Tally on a Windows Docker host: configure Setuora's Tally host as
  `host.docker.internal` and port `9000`.
- Tally on another protected LAN host: use that host's private address and
  restrict its firewall to the Master host.
- Linux Docker host: `host.docker.internal` maps to Docker's host gateway, but
  the Tally service must accept that private bridge connection. Prefer a
  dedicated private endpoint rather than widening a loopback-only listener.

Run the gateway and master checks in Setuora before enabling posting. Node Sync
requests never call Tally inline.

## Persistence and backup

The Compose volumes are:

- `setuora-master_setuora-data`: SQLite database, application secret, schema
  safety copies, and automatic backups;
- `setuora-master_tailscale-state`: the stable Tailscale device identity.

Use the authenticated Maintenance page to create and download a verified
database backup. Keep encrypted off-machine copies of the backup and `.env`.
The database backup alone does not contain application or Tailscale credentials.

To inspect backups without publishing the data volume:

```bash
docker compose exec setuora ls -la /srv/setuora/data/backups
```

Complete a clean-host restore drill before production use.

## Security boundary

- Do not enable Tailscale Funnel for this deployment.
- Do not map container port `8000` to `0.0.0.0`.
- Do not advertise Tally or database ports.
- Keep the example grants deny-by-default; do not restore Tailscale's
  allow-everything starter policy.
- Give each person an individual Setuora account and each franchise an
  individual Setuora node credential.
- Rotate a compromised Tailscale key/device and its Setuora node credential;
  they protect different boundaries.
- Tailscale access does not replace application login, roles, audit logs,
  backups, monitoring, or PostgreSQL production work.

## Troubleshooting

Check the whole stack:

```bash
python deploy.py status
python deploy.py logs
```

If Setuora is healthy locally but unavailable remotely:

1. run `docker compose exec tailscale tailscale status`;
2. confirm HTTPS and MagicDNS are enabled in the tailnet;
3. confirm Master has `tag:setuora-master`;
4. confirm the client has `tag:setuora-lite` or is in the operator group;
5. verify the grants allow source to `tag:setuora-master` on `tcp:443`;
6. confirm `TRUSTED_HOSTS` includes Master's exact MagicDNS name;
7. check both systems' clocks.

If Tailscale reports an authentication error and its identity volume cannot be
recovered, generate a new one-off, non-ephemeral tagged key, replace
`TAILSCALE_AUTH_KEY` in `.env`, and recreate only that identity after following
the incident procedure. Do not delete the application data volume.
