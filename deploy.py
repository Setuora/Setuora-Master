"""Cross-platform Setuora Master deployment helper.

The same commands run on Linux and Windows. Docker Compose owns the application
and Tailscale processes, so this script does not install system services or
modify the host firewall.
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import secrets
import shutil
import subprocess  # nosec B404
import sys
import time
import urllib.error
import urllib.request
from contextlib import suppress
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"
ENV_EXAMPLE_PATH = PROJECT_ROOT / ".env.example"
COMPOSE_FILE = PROJECT_ROOT / "compose.yaml"
UNSAFE_PASSWORDS = {
    "",
    "admin123",
    "change-this-password",
    "change-this-before-first-start",
}
PLACEHOLDER_SECRETS = {
    "",
    "dev-change-me",
    "change-this-before-production",
    "replace-with-a-long-random-secret",
}
PLAIN_ENV_VALUE = re.compile(r"^[A-Za-z0-9_./,:*?=@+%-]+$")
DNS_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


class DeploymentError(RuntimeError):
    pass


def _run(
    command: list[str],
    *,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    # The command is always an argument list and shell execution is disabled.
    return subprocess.run(  # noqa: S603  # nosec B603
        command,
        cwd=PROJECT_ROOT,
        check=check,
        text=True,
        capture_output=capture,
    )


def _compose(*arguments: str, check: bool = True, capture: bool = False):
    return _run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), *arguments],
        check=check,
        capture=capture,
    )


def _read_env() -> tuple[list[str], dict[str, str]]:
    if not ENV_PATH.exists():
        return [], {}
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1].replace(r"\\", "\\").replace(r"\"", '"')
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        values[key] = value
    return lines, values


def _format_env_value(value: str) -> str:
    if value and PLAIN_ENV_VALUE.fullmatch(value):
        return value
    escaped = value.replace("\\", r"\\").replace('"', r"\"")
    return f'"{escaped}"'


def _write_env(updates: dict[str, str]) -> None:
    lines, _ = _read_env()
    pending = dict(updates)
    output: list[str] = []
    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key.startswith("export "):
                key = key.removeprefix("export ").strip()
            if key in pending:
                output.append(f"{key}={_format_env_value(pending.pop(key))}")
                continue
        output.append(raw_line)
    if pending:
        if output and output[-1]:
            output.append("")
        output.extend(f"{key}={_format_env_value(value)}" for key, value in pending.items())
    ENV_PATH.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    with suppress(OSError):
        ENV_PATH.chmod(0o600)


def _check_prerequisites() -> None:
    if shutil.which("docker") is None:
        raise DeploymentError(
            "Docker was not found. Install Docker Engine (Linux) or Docker "
            "Desktop (Windows), then run this command again."
        )
    result = _run(["docker", "compose", "version"], check=False, capture=True)
    if result.returncode:
        raise DeploymentError("Docker Compose v2 is required (`docker compose`).")
    result = _run(["docker", "info"], check=False, capture=True)
    if result.returncode:
        raise DeploymentError("Docker is installed but its engine is not running.")


def _environment_issues(
    values: dict[str, str],
    *,
    has_application_data: bool,
    has_tailnet_identity: bool,
) -> list[str]:
    issues: list[str] = []
    app_secret = values.get("APP_SECRET_KEY", "")
    if app_secret in PLACEHOLDER_SECRETS or len(app_secret) < 32:
        issues.append("APP_SECRET_KEY must contain at least 32 random characters.")

    password = values.get("BOOTSTRAP_ADMIN_PASSWORD", "")
    if not has_application_data and (password in UNSAFE_PASSWORDS or len(password) < 12):
        issues.append("BOOTSTRAP_ADMIN_PASSWORD must be unique and at least 12 characters.")

    auth_key = values.get("TAILSCALE_AUTH_KEY", "")
    if auth_key and not auth_key.startswith(("tskey-auth-", "tskey-client-")):
        issues.append("TAILSCALE_AUTH_KEY has an unsupported format.")
    if not auth_key and not has_tailnet_identity:
        issues.append("Provide TAILSCALE_AUTH_KEY for first enrollment.")

    if values.get("SESSION_COOKIE_SECURE", "").strip().lower() != "true":
        issues.append("SESSION_COOKIE_SECURE must be true.")
    if values.get("SETUORA_APP_MODE", "master").strip().lower() != "master":
        issues.append("SETUORA_APP_MODE must be master.")
    if not values.get("TAILSCALE_TAG", "tag:setuora-master").startswith("tag:"):
        issues.append("TAILSCALE_TAG must start with tag:.")

    trusted_hosts = {
        item.strip() for item in values.get("TRUSTED_HOSTS", "").split(",") if item.strip()
    }
    if not {"localhost", "127.0.0.1"}.issubset(trusted_hosts):
        issues.append("TRUSTED_HOSTS must include localhost and 127.0.0.1.")
    if not any(host == "*.ts.net" or host.endswith(".ts.net") for host in trusted_hosts):
        issues.append("TRUSTED_HOSTS must include the bootstrap wildcard or exact MagicDNS name.")

    try:
        local_port = int(values.get("SETUORA_LOCAL_PORT", "8000"))
    except ValueError:
        local_port = 0
    if not 1 <= local_port <= 65535:
        issues.append("SETUORA_LOCAL_PORT must be a number from 1 to 65535.")

    if values.get("AUTOMATIC_BACKUPS_ENABLED", "true").strip().lower() != "true":
        issues.append("AUTOMATIC_BACKUPS_ENABLED must be true for production.")
    try:
        retention = int(values.get("BACKUP_RETENTION_COUNT", "14"))
    except ValueError:
        retention = 0
    if retention < 2:
        issues.append("BACKUP_RETENTION_COUNT must be at least 2.")
    return issues


def _has_named_volume(name: str) -> bool:
    result = _run(
        ["docker", "volume", "inspect", name],
        check=False,
        capture=True,
    )
    return result.returncode == 0


def _prompt_secret(label: str) -> str:
    if not sys.stdin.isatty():
        raise DeploymentError(
            f"{label} is missing. Set it in .env before running setup non-interactively."
        )
    first = getpass.getpass(f"{label}: ").strip()
    second = getpass.getpass(f"Confirm {label}: ").strip()
    if first != second:
        raise DeploymentError(f"{label} values did not match.")
    return first


def _prepare_environment() -> None:
    if not ENV_PATH.exists():
        shutil.copyfile(ENV_EXAMPLE_PATH, ENV_PATH)
        with suppress(OSError):
            ENV_PATH.chmod(0o600)

    _, values = _read_env()
    updates: dict[str, str] = {}

    app_secret = values.get("APP_SECRET_KEY", "")
    if app_secret in PLACEHOLDER_SECRETS or len(app_secret) < 32:
        updates["APP_SECRET_KEY"] = secrets.token_urlsafe(48)

    password = values.get("BOOTSTRAP_ADMIN_PASSWORD", "")
    if password in UNSAFE_PASSWORDS or len(password) < 12:
        password = _prompt_secret("First administrator password")
        if len(password) < 12:
            raise DeploymentError(
                "The first administrator password must be at least 12 characters."
            )
        updates["BOOTSTRAP_ADMIN_PASSWORD"] = password

    tailscale_key = values.get("TAILSCALE_AUTH_KEY", "")
    if not tailscale_key:
        tailscale_key = _prompt_secret("Tailscale tagged auth key")
        if not tailscale_key.startswith(("tskey-auth-", "tskey-client-")):
            raise DeploymentError("That does not look like a Tailscale auth key.")
        updates["TAILSCALE_AUTH_KEY"] = tailscale_key

    trusted_hosts = {
        item.strip() for item in values.get("TRUSTED_HOSTS", "").split(",") if item.strip()
    }
    trusted_hosts.update({"localhost", "127.0.0.1", "*.ts.net"})
    updates["TRUSTED_HOSTS"] = ",".join(sorted(trusted_hosts))
    updates["SETUORA_APP_MODE"] = "master"
    updates["SESSION_COOKIE_SECURE"] = "true"
    updates["TAILSCALE_HOSTNAME"] = values.get("TAILSCALE_HOSTNAME") or "setuora-master"
    updates["TAILSCALE_TAG"] = values.get("TAILSCALE_TAG") or "tag:setuora-master"

    _write_env(updates)


def _wait_for_health(timeout_seconds: int = 120) -> None:
    _, values = _read_env()
    port = values.get("SETUORA_LOCAL_PORT", "8000")
    deadline = time.monotonic() + timeout_seconds
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        try:
            # This URL is built locally with a fixed HTTP scheme and loopback host.
            with urllib.request.urlopen(url, timeout=3) as response:  # nosec B310
                payload = json.load(response)
            if payload == {"status": "ok", "role": "master"}:
                return
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(2)
    raise DeploymentError(f"Setuora did not become healthy at {url}. Run `python deploy.py logs`.")


def _tailscale_dns_name() -> str | None:
    result = _compose(
        "exec",
        "-T",
        "tailscale",
        "tailscale",
        "status",
        "--json",
        check=False,
        capture=True,
    )
    if result.returncode:
        return None
    try:
        return json.loads(result.stdout)["Self"]["DNSName"].rstrip(".")
    except (KeyError, TypeError, ValueError):
        return None


def _wait_for_tailscale(timeout_seconds: int = 90) -> str:
    deadline = time.monotonic() + timeout_seconds
    last_status = ""
    while time.monotonic() < deadline:
        dns_name = _tailscale_dns_name()
        serve = _compose(
            "exec",
            "-T",
            "tailscale",
            "tailscale",
            "serve",
            "status",
            check=False,
            capture=True,
        )
        last_status = (serve.stdout or serve.stderr).strip()
        if (
            dns_name
            and serve.returncode == 0
            and "https://" in last_status
            and "proxy http://127.0.0.1:8000" in last_status
        ):
            return dns_name
        time.sleep(2)
    detail = f" Last status: {last_status}" if last_status else ""
    raise DeploymentError(
        "Tailscale did not publish the private HTTPS endpoint. Confirm that the "
        "auth key owns the configured tag and that MagicDNS/HTTPS are enabled."
        f"{detail}"
    )


def _pin_tailnet_hostname(dns_name: str) -> None:
    if not DNS_NAME.fullmatch(dns_name):
        raise DeploymentError("Tailscale returned an invalid MagicDNS hostname.")
    _, values = _read_env()
    trusted_hosts = {
        item.strip()
        for item in values.get("TRUSTED_HOSTS", "").split(",")
        if item.strip() and item.strip() != "*.ts.net"
    }
    trusted_hosts.update({"localhost", "127.0.0.1", dns_name})
    _write_env(
        {
            "TRUSTED_HOSTS": ",".join(sorted(trusted_hosts)),
            # The persistent Tailscale state is the long-lived identity. Do not
            # retain the bootstrap credential after successful enrollment.
            "TAILSCALE_AUTH_KEY": "",
            # The application stores only the password hash after bootstrap.
            "BOOTSTRAP_ADMIN_PASSWORD": "",
        }
    )
    _compose("up", "-d", "--no-deps", "--force-recreate", "setuora")
    _wait_for_health()


def preflight(_args: argparse.Namespace) -> None:
    _check_prerequisites()
    if not ENV_PATH.exists():
        raise DeploymentError(".env is missing. Run `python deploy.py setup` first.")
    _, values = _read_env()
    issues = _environment_issues(
        values,
        has_application_data=_has_named_volume("setuora-master_setuora-data"),
        has_tailnet_identity=_has_named_volume("setuora-master_tailscale-state"),
    )
    compose = _compose("config", "--quiet", check=False, capture=True)
    if compose.returncode:
        issues.append("Docker Compose configuration is invalid.")
    if issues:
        raise DeploymentError("Preflight failed:\n- " + "\n- ".join(issues))
    print("Production configuration preflight passed without exposing secrets.")


def setup(_args: argparse.Namespace) -> None:
    _check_prerequisites()
    _prepare_environment()
    _compose("up", "-d", "--build", "--remove-orphans")
    _wait_for_health()
    dns_name = _wait_for_tailscale()
    _pin_tailnet_hostname(dns_name)
    print("Setuora Master is healthy.")
    print(f"Open https://{dns_name}")
    print(f"Configure each Lite node with MASTER_URL=https://{dns_name}")


def start(_args: argparse.Namespace) -> None:
    _check_prerequisites()
    if not ENV_PATH.exists():
        raise DeploymentError("Run `python deploy.py setup` first.")
    _compose("up", "-d", "--remove-orphans")
    _wait_for_health()
    dns_name = _wait_for_tailscale()
    _pin_tailnet_hostname(dns_name)
    print(f"Setuora Master is running at https://{dns_name}.")


def stop(_args: argparse.Namespace) -> None:
    _check_prerequisites()
    _compose("down")
    print("Setuora Master stopped. Database and Tailscale identity volumes were preserved.")


def status(_args: argparse.Namespace) -> None:
    _check_prerequisites()
    _compose("ps")
    dns_name = _tailscale_dns_name()
    if dns_name:
        print(f"Tailnet URL: https://{dns_name}")


def logs(args: argparse.Namespace) -> None:
    _check_prerequisites()
    services = [args.service] if args.service else []
    _compose("logs", "--follow", "--tail", str(args.tail), *services)


def update(_args: argparse.Namespace) -> None:
    _check_prerequisites()
    if not ENV_PATH.exists():
        raise DeploymentError("Run `python deploy.py setup` first.")
    _compose("pull", "tailscale")
    _compose("up", "-d", "--build", "--remove-orphans")
    _wait_for_health()
    dns_name = _wait_for_tailscale()
    _pin_tailnet_hostname(dns_name)
    print("Setuora Master containers were rebuilt and are healthy.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deploy Setuora Master identically on Linux and Windows."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, function, help_text in (
        ("preflight", preflight, "validate production configuration without starting"),
        ("setup", setup, "configure, build, start, and verify the deployment"),
        ("start", start, "start an existing deployment"),
        ("stop", stop, "stop containers while preserving data"),
        ("status", status, "show container state and the tailnet URL"),
        ("update", update, "rebuild the current source and restart"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.set_defaults(function=function)
    logs_parser = subparsers.add_parser("logs", help="follow container logs")
    logs_parser.add_argument("service", nargs="?", choices=("setuora", "tailscale"))
    logs_parser.add_argument("--tail", type=int, default=200)
    logs_parser.set_defaults(function=logs)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.function(args)
    except (DeploymentError, subprocess.CalledProcessError) as exc:
        print(f"Deployment failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
