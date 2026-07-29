# Private Administrative HTTPS

This guide covers the private Master administrative site. It is separate from
the public Node Sync hostname.

## Boundary

```text
Administrator
  -> authenticated private network
  -> private HTTPS proxy
  -> Uvicorn 127.0.0.1:8000
```

The private proxy may serve login, monitoring, reports, Tally settings, and
maintenance. It must not be reachable from the public Internet.

The public edge has a different configuration and publishes only the Node Sync
API. See [master-internet-edge.md](master-internet-edge.md) and
[`Caddyfile.master.example`](../../deployment/caddy/Caddyfile.master.example).

## Automated private proxy

`Setuora.exe setup` can install Caddy, generate an internal-certificate
configuration, and create a local-subnet firewall rule. Use this only on a
trusted management network.

Setup also writes:

```text
SESSION_COOKIE_SECURE=true
TRUSTED_HOSTS=<private-admin-host>,127.0.0.1,localhost,testserver
```

Review `TRUSTED_HOSTS`; do not use wildcards.

## Certificate handling

The automated private proxy uses Caddy's internal certificate authority. Install
only its public root certificate on managed administrator devices. Never copy
the Caddy state directory because it contains private keys.

Prefer an organization-managed certificate and DNS name when available.

## Firewall

- Keep Uvicorn bound to loopback.
- Permit private HTTPS only from the management subnet or VPN.
- Deny public access to the administrative hostname.
- Never expose Tally or the database.
- Keep the Caddy administration endpoint on loopback.

## Validation

From an approved administrator device:

1. verify the certificate chain and hostname;
2. sign in and sign out;
3. confirm secure session cookies;
4. confirm security headers;
5. verify that an unapproved network cannot connect.

From the public Internet, verify that the private administrative hostname is not
reachable and that the public sync hostname rejects every non-Node-Sync path.

## Important distinction

The internal-certificate setup is convenient for the bounded pilot. It does not
provide publicly trusted TLS for Lite nodes and must not be adapted into a
direct Internet port-forward.
