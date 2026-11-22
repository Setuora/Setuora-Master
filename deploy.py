"""Windows-native lifecycle helper for Setuora Master."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import re
import secrets
import shutil
import socket
import stat
import subprocess  # nosec B404
import sys
import tempfile
import time
import urllib.error
import urllib.request
import venv
from pathlib import Path
from xml.sax.saxutils import escape

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"
ENV_EXAMPLE_PATH = PROJECT_ROOT / ".env.example"
VENV_PATH = PROJECT_ROOT / ".venv"
WINDOWS_SCRIPTS = PROJECT_ROOT / "scripts" / "windows"
RUNNER_PATH = WINDOWS_SCRIPTS / "run-server.cmd"
TASK_NAME = "Setuora-Master"
TAILSCALE_URL_SETTING = "SETUORA_TAILSCALE_URL"
NODE_PATH = "/api/v1/"
DEFAULT_WEB_PORT = 8000
PORT_PATH = PROJECT_ROOT / "runtime-port.txt"
LAN_SETTING = "SETUORA_LAN_ACCESS"
LAN_FIREWALL_RULE = "Setuora-Master-LAN"
LAN_STATE_PATH = PROJECT_ROOT / "runtime-lan.txt"
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


class DeploymentError(RuntimeError):
    pass


class TailscaleUnavailable(DeploymentError):
    """Local app can run, but the private network is currently unavailable."""


def _run(
    command: list[str],
    *,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # nosec B603
        command,
        cwd=PROJECT_ROOT,
        check=check,
        text=True,
        capture_output=capture,
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
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1].replace(r"\"", '"').replace("\\\\", "\\")
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        values[key] = value
    return lines, values


def _web_port(values: dict[str, str] | None = None) -> int:
    if values is None:
        _, values = _read_env()
    try:
        port = int(values.get("SETUORA_WEB_PORT") or DEFAULT_WEB_PORT)
    except ValueError as exc:
        raise DeploymentError("SETUORA_WEB_PORT must be a TCP port number.") from exc
    if not 1024 <= port <= 65535:
        raise DeploymentError("SETUORA_WEB_PORT must be between 1024 and 65535.")
    return port


def _public_web_port() -> int:
    """Allow a normal desktop user to open the local console without reading .env."""
    try:
        return _web_port({"SETUORA_WEB_PORT": PORT_PATH.read_text(encoding="ascii").strip()})
    except FileNotFoundError:
        # Older installations have no public port file and always used 8000.
        return DEFAULT_WEB_PORT


def _node_target(port: int | None = None) -> str:
    return f"http://127.0.0.1:{port or _web_port()}{NODE_PATH}"


def _local_url(port: int | None = None) -> str:
    return f"http://127.0.0.1:{port or _web_port()}"


def _lan_enabled(values: dict[str, str] | None = None) -> bool:
    if values is None:
        _, values = _read_env()
    return values.get(LAN_SETTING, "false").strip().lower() == "true"


def _bind_host(values: dict[str, str] | None = None) -> str:
    return "0.0.0.0" if _lan_enabled(values) else "127.0.0.1"  # noqa: S104  # nosec B104


def _powershell_json(script: str) -> list[object]:
    result = _run(["powershell.exe", "-NoProfile", "-Command", script], capture=True, check=False)
    try:
        raw = json.loads(result.stdout or "[]")
    except ValueError:
        return []
    if raw is None:
        return []
    return raw if isinstance(raw, list) else [raw]


def _usable_lan_address(address: str) -> bool:
    parts = address.split(".")
    if len(parts) != 4 or not all(part.isdigit() and int(part) <= 255 for part in parts):
        return False
    first, second = int(parts[0]), int(parts[1])
    if first == 127 or (first == 169 and second == 254):
        return False
    # Tailscale addresses use the 100.64.0.0/10 range; Lite reaches Master there over HTTPS.
    return not (first == 100 and 64 <= second <= 127)


def _lan_addresses() -> list[str]:
    """Return this PC's usable LAN IPv4 addresses (no loopback, APIPA or Tailscale)."""
    items = _powershell_json(
        "ConvertTo-Json -Compress -InputObject @(Get-NetIPAddress -AddressFamily IPv4 "
        "-AddressState Preferred -ErrorAction SilentlyContinue | "
        "Select-Object -ExpandProperty IPAddress)"
    )
    return sorted({str(item).strip() for item in items if _usable_lan_address(str(item).strip())})


def _lan_urls(port: int | None = None) -> list[str]:
    return [f"http://{address}:{port or _web_port()}" for address in _lan_addresses()]


def _remove_lan_firewall_rule() -> None:
    script = (
        f"Get-NetFirewallRule -Name '{LAN_FIREWALL_RULE}' -ErrorAction SilentlyContinue "
        "| Remove-NetFirewallRule -ErrorAction Stop"
    )
    # Closing a rule that does not exist (or cannot be read) must not block Start or Stop.
    _run(["powershell.exe", "-NoProfile", "-Command", script], check=False, capture=True)


def _apply_lan_access() -> None:
    """Open or close local-network access to match SETUORA_LAN_ACCESS before the task runs."""
    _, values = _read_env()
    _remove_lan_firewall_rule()
    # Status runs without Administrator rights and cannot read .env, so publish the mode.
    LAN_STATE_PATH.write_text(f"{str(_lan_enabled(values)).lower()}\n", encoding="ascii")
    if not _lan_enabled(values):
        return
    port = _web_port(values)
    # Inbound only from the local subnet, and only on Private or Domain networks.
    script = (
        f"New-NetFirewallRule -Name '{LAN_FIREWALL_RULE}' "
        "-DisplayName 'Setuora Master (local network)' -Direction Inbound -Action Allow "
        f"-Protocol TCP -LocalPort {port} -RemoteAddress LocalSubnet "
        "-Profile Private,Domain -ErrorAction Stop | Out-Null"
    )
    _run(["powershell.exe", "-NoProfile", "-Command", script])
    trusted = [item.strip() for item in values.get("TRUSTED_HOSTS", "").split(",") if item.strip()]
    wanted = [*_lan_addresses(), socket.gethostname().strip().lower()]
    missing = [host for host in wanted if host and host not in trusted]
    if missing:
        _write_env({"TRUSTED_HOSTS": ",".join([*trusted, *missing])})


def _warn_if_public_network() -> None:
    names = _powershell_json(
        "ConvertTo-Json -Compress -InputObject @(Get-NetConnectionProfile "
        "-ErrorAction SilentlyContinue | Where-Object NetworkCategory -eq 'Public' | "
        "Select-Object -ExpandProperty Name)"
    )
    names = [str(item) for item in names if item]
    if names:
        print(
            "Note: Windows marks these networks as Public, so other PCs cannot connect: "
            + ", ".join(names)
            + ". Set the network to Private in Windows Settings > Network to allow LAN access."
        )


def _print_addresses() -> None:
    print(f"Local address (this computer only): {_local_url()}")
    if not _lan_enabled():
        print("LAN access is off; other computers cannot open Master.")
        return
    urls = _lan_urls()
    if urls:
        for url in urls:
            print(f"Other PCs on this network: {url}")
    else:
        print("LAN access is on, but no local network address was found.")
    _warn_if_public_network()


_TALLY_PROBE_SCRIPT = """import sys, urllib.request
from app.database import SessionLocal
from app.services.settings import get_all_settings
from app.services.tally_endpoint import build_tally_url
try:
    with SessionLocal() as db:
        settings = get_all_settings(db)
except Exception:
    print("unknown")
    sys.exit(0)
try:
    url = build_tally_url(settings)
except ValueError:
    print("unconfigured")
    sys.exit(0)
body = b"<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER></ENVELOPE>"
try:
    request = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(request, timeout=4) as response:
        response.read(256)
    print("ok " + url)
except Exception:
    print("down " + url)
"""


def _tally_gateway_state() -> str:
    """Return 'ok <url>', 'down <url>', 'unconfigured' or 'unknown' for Master's saved Tally gateway."""
    if not _venv_python().is_file():
        return "unknown"
    try:
        result = _run([str(_venv_python()), "-c", _TALLY_PROBE_SCRIPT], check=False, capture=True)
    except OSError:
        return "unknown"
    lines = (result.stdout or "").strip().splitlines()
    return lines[-1].strip() if result.returncode == 0 and lines else "unknown"


def _tailscale_ipv4() -> str:
    executable = _find_tailscale_executable()
    if not executable:
        return ""
    try:
        result = _run([executable, "ip", "-4"], check=False, capture=True)
    except OSError:
        return ""
    return (result.stdout or "").strip().splitlines()[0].strip() if (result.stdout or "").strip() else ""


def _print_startup_summary(port: int | None = None, lite_address: str = "") -> None:
    """Show this Master's addresses so a Lite PC can be linked, plus Tally reachability."""
    port = port or _web_port()
    if not lite_address:
        _, values = _read_env()
        lite_address = values.get(TAILSCALE_URL_SETTING, "").strip()
    lan_on = (
        LAN_STATE_PATH.is_file()
        and LAN_STATE_PATH.read_text(encoding="ascii").strip() == "true"
    )
    tally = _tally_gateway_state()
    line = "=" * 64
    print("")
    print(line)
    print("  SETUORA MASTER - CONNECTION DETAILS")
    print(line)
    print(f"  Computer name        : {socket.gethostname()}")
    print(f"  Console (this PC)    : {_local_url(port)}")
    lan_ips = _lan_addresses()
    print(f"  LAN IP address       : {', '.join(lan_ips) if lan_ips else 'not found'}")
    if lan_on:
        for address in lan_ips:
            print(f"  Console (LAN PCs)    : http://{address}:{port}")
    else:
        print("  Console (LAN PCs)    : off (menu option 4 turns it on)")
    tailscale_ip = _tailscale_ipv4()
    print(f"  Tailscale IP         : {tailscale_ip or 'not connected'}")
    if lite_address:
        print(f"  LITE MASTER ADDRESS  : {lite_address}")
        print(f"  Lite API endpoint    : {lite_address}/api/v1/")
    else:
        print("  LITE MASTER ADDRESS  : unavailable (Tailscale offline) - choose Start again")
    if tally.startswith("ok "):
        print(f"  Tally gateway        : answering at {tally[3:]}")
    elif tally.startswith("down "):
        print(f"  Tally gateway        : NOT answering at {tally[5:]} - open Tally and enable its HTTP/XML port")
    elif tally == "unconfigured":
        print("  Tally gateway        : not configured - set Tally host and port in Settings")
    else:
        print("  Tally gateway        : could not be checked")
    print(line)
    print("  To link a Lite: Master > Franchises > Copy connection details,")
    print("  then on Lite: Admin > Master connection > paste > Connect to Master.")
    print(line)
    print("")


def _save_web_port(port: int) -> None:
    _, values = _read_env()
    previous = _web_port(values)
    updates = {"SETUORA_WEB_PORT": str(port)}
    if previous != port:
        updates["SETUORA_PREVIOUS_WEB_PORT"] = str(previous)
    _write_env(updates)
    PORT_PATH.write_text(f"{port}\n", encoding="ascii")


def _previous_node_target() -> str | None:
    _, values = _read_env()
    previous = values.get("SETUORA_PREVIOUS_WEB_PORT")
    return _node_target(_web_port({"SETUORA_WEB_PORT": previous})) if previous else None


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
            key = stripped.split("=", 1)[0].strip().removeprefix("export ").strip()
            if key in pending:
                output.append(f"{key}={_format_env_value(pending.pop(key))}")
                continue
        output.append(raw_line)
    if pending:
        if output and output[-1]:
            output.append("")
        output.extend(f"{key}={_format_env_value(value)}" for key, value in pending.items())
    ENV_PATH.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    if sys.platform == "win32":
        _run(
            [
                "icacls.exe",
                str(ENV_PATH),
                "/inheritance:r",
                "/remove:g",
                "*S-1-1-0",
                "*S-1-5-11",
                "*S-1-5-32-545",
                "/grant:r",
                "*S-1-5-18:F",
                "*S-1-5-32-544:F",
            ]
        )


def _check_windows() -> None:
    if sys.platform != "win32":
        raise DeploymentError(
            "The active Setuora Master deployment supports Windows only. "
            "The previous Linux deployment is preserved under archive/."
        )


def _environment_issues(values: dict[str, str], *, has_application_data: bool) -> list[str]:
    issues: list[str] = []
    database_url = values.get("DATABASE_URL") or "sqlite:///./data/setuora.db"
    if not database_url.startswith("sqlite:///") or database_url == "sqlite:///:memory:":
        issues.append("DATABASE_URL must point to a persistent SQLite database for backups.")
    app_secret = values.get("APP_SECRET_KEY", "")
    if app_secret in PLACEHOLDER_SECRETS or len(app_secret) < 32:
        issues.append("APP_SECRET_KEY must contain at least 32 random characters.")

    password = values.get("BOOTSTRAP_ADMIN_PASSWORD", "")
    if not has_application_data and (password in UNSAFE_PASSWORDS or len(password) < 12):
        issues.append("BOOTSTRAP_ADMIN_PASSWORD must be unique and at least 12 characters.")

    if values.get("SETUORA_APP_MODE", "master").strip().lower() != "master":
        issues.append("SETUORA_APP_MODE must be master.")
    if values.get("SESSION_COOKIE_SECURE", "false").strip().lower() != "false":
        issues.append("SESSION_COOKIE_SECURE must be false for the loopback-only HTTP console.")

    trusted_hosts = {
        item.strip() for item in values.get("TRUSTED_HOSTS", "").split(",") if item.strip()
    }
    if not {"localhost", "127.0.0.1"}.issubset(trusted_hosts):
        issues.append("TRUSTED_HOSTS must include localhost and 127.0.0.1.")

    try:
        _web_port(values)
    except DeploymentError as exc:
        issues.append(str(exc))

    if values.get("SFTP_SYNC_ENABLED", "false").strip().lower() == "true":
        issues.append("SFTP_SYNC_ENABLED must be false for the central-Tally deployment.")

    if values.get("AUTOMATIC_BACKUPS_ENABLED", "true").strip().lower() != "true":
        issues.append("AUTOMATIC_BACKUPS_ENABLED must be true for production.")
    try:
        retention = int(values.get("BACKUP_RETENTION_COUNT", "2"))
    except ValueError:
        retention = 0
    if retention < 2:
        issues.append("BACKUP_RETENTION_COUNT must be at least 2.")
    return issues


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
        ENV_PATH.write_text(ENV_EXAMPLE_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    _, values = _read_env()
    updates: dict[str, str] = {}
    app_secret = values.get("APP_SECRET_KEY", "")
    if app_secret in PLACEHOLDER_SECRETS or len(app_secret) < 32:
        updates["APP_SECRET_KEY"] = secrets.token_urlsafe(48)

    database_exists = _has_application_data()
    password = values.get("BOOTSTRAP_ADMIN_PASSWORD", "")
    if not database_exists and (password in UNSAFE_PASSWORDS or len(password) < 12):
        password = _prompt_secret("First administrator password")
        if len(password) < 12:
            raise DeploymentError(
                "The first administrator password must be at least 12 characters."
            )
        updates["BOOTSTRAP_ADMIN_PASSWORD"] = password

    updates.update(
        {
            "SETUORA_APP_MODE": "master",
            "DATABASE_URL": values.get("DATABASE_URL") or "sqlite:///./data/setuora.db",
            "SESSION_COOKIE_SECURE": "false",
            "TRUSTED_HOSTS": values.get("TRUSTED_HOSTS") or "127.0.0.1,localhost",
            "SFTP_SYNC_ENABLED": "false",
            "SETUORA_WEB_PORT": values.get("SETUORA_WEB_PORT") or str(DEFAULT_WEB_PORT),
        }
    )
    _write_env(updates)
    PORT_PATH.write_text(f"{_web_port({**values, **updates})}\n", encoding="ascii")
    (PROJECT_ROOT / "data").mkdir(parents=True, exist_ok=True)
    (PROJECT_ROOT / "logs").mkdir(parents=True, exist_ok=True)
    _secure_private_storage()


def _secure_private_storage() -> None:
    """Keep the local database, backups, and logs readable only by the service and admins."""
    if sys.platform != "win32":
        return
    for directory in (PROJECT_ROOT / "data", PROJECT_ROOT / "logs"):
        _run(["icacls.exe", str(directory), "/grant", "*S-1-5-18:F", "*S-1-5-32-544:F", "/T"])
        _run(
            [
                "icacls.exe",
                str(directory),
                "/grant",
                "*S-1-5-18:(OI)(CI)F",
                "*S-1-5-32-544:(OI)(CI)F",
                "/T",
            ]
        )
        _run(["icacls.exe", str(directory), "/inheritance:r", "/T"])
        _run(
            [
                "icacls.exe",
                str(directory),
                "/remove:g",
                "*S-1-1-0",
                "*S-1-5-11",
                "*S-1-5-32-545",
                "/T",
            ]
        )


def _venv_python() -> Path:
    return VENV_PATH / "Scripts" / "python.exe"


def _install_runtime() -> None:
    if not _venv_python().is_file():
        venv.EnvBuilder(with_pip=True).create(VENV_PATH)
    _run(
        [
            str(_venv_python()),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--require-hashes",
            "-r",
            str(PROJECT_ROOT / "requirements-runtime.lock"),
        ]
    )


def _task(*arguments: str, check: bool = True, capture: bool = False):
    return _run(["schtasks.exe", *arguments], check=check, capture=capture)


def _task_xml() -> str:
    # Defaults stop a task after 72 hours and when a PC switches to battery.
    # Register explicit settings suitable for a continuously running server.
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers><BootTrigger>
    <Enabled>true</Enabled><ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </BootTrigger></Triggers>
  <Principals><Principal id="System">
    <UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel>
  </Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>10</Count></RestartOnFailure>
  </Settings>
  <Actions Context="System"><Exec>
    <Command>cmd.exe</Command>
    <Arguments>{escape(f'/d /s /c ""{RUNNER_PATH}""')}</Arguments>
    <WorkingDirectory>{escape(str(PROJECT_ROOT))}</WorkingDirectory>
  </Exec></Actions>
</Task>
"""


def _ensure_task() -> None:
    with tempfile.TemporaryDirectory(prefix="setuora-task-") as directory:
        task_path = Path(directory) / "task.xml"
        task_path.write_text(_task_xml(), encoding="utf-16")
        _task("/Create", "/TN", TASK_NAME, "/XML", str(task_path), "/F")


def _wait_for_health(timeout_seconds: int = 300) -> None:
    deadline = time.monotonic() + timeout_seconds
    url = _local_url() + "/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:  # noqa: S310  # nosec B310
                payload = json.load(response)
            if payload == {"status": "ok", "role": "master"}:
                return
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(2)
    raise DeploymentError(f"Setuora did not become healthy at {url}. Run `setuora.ps1 logs`.")


def _wait_for_stop(timeout_seconds: int = 30) -> None:
    # Windows can time out a loopback connection even when no process is listening.
    # Inspect actual listeners before replacing runtime files.
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not any(_is_own_listener(item) for item in _port_listeners()):
            return
        time.sleep(0.5)
    raise DeploymentError(
        f"Master still owns port {_web_port()} after stopping the task. "
        "Check the running Setuora process before updating files."
    )


def _port_listeners(port: int | None = None) -> list[dict[str, object]]:
    """Read TCP listeners on a candidate port, including wildcard binds."""
    port = port or _web_port()
    script = (
        f"$items = @(Get-NetTCPConnection -State Listen -LocalPort {port} "
        "-ErrorAction SilentlyContinue | ForEach-Object { "
        "$p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $_.OwningProcess) "
        "-ErrorAction SilentlyContinue; [pscustomobject]@{Pid=$_.OwningProcess; "
        "ExecutablePath=$p.ExecutablePath; CommandLine=$p.CommandLine} }); "
        "ConvertTo-Json -InputObject $items -Compress -Depth 3"
    )
    result = _run(["powershell.exe", "-NoProfile", "-Command", script], capture=True)
    try:
        listeners = json.loads(result.stdout or "[]")
    except ValueError as exc:
        raise DeploymentError(f"Windows could not identify the process using port {port}.") from exc
    if not isinstance(listeners, list) or any(not isinstance(item, dict) for item in listeners):
        raise DeploymentError(f"Windows returned invalid process details for port {port}.")
    return listeners


def _is_own_listener(listener: dict[str, object]) -> bool:
    expected = str(_venv_python().resolve()).replace("/", "\\").casefold()
    executable = str(listener.get("ExecutablePath") or "").replace("/", "\\").casefold()
    command = str(listener.get("CommandLine") or "").casefold()
    return (
        executable == expected
        and "-m uvicorn app.main:app" in command
        and ("--host 127.0.0.1" in command or "--host 0.0.0.0" in command)  # nosec B104
        and re.search(rf"(?<!\S)--port\s+{_web_port()}(?!\d)", command) is not None
    )


def _release_setuora_port() -> None:
    listeners = _port_listeners()
    owned = [item for item in listeners if _is_own_listener(item)]
    for pid in sorted({int(item["Pid"]) for item in owned}):
        print(f"Stopping orphaned Setuora Master process {pid} on port {_web_port()}.")
        _run(["taskkill.exe", "/PID", str(pid), "/F"])
    _wait_for_stop(timeout_seconds=10 if owned else 1)


def _can_bind_port(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((_bind_host(), port))
        return True
    except OSError:
        return False


def _reserved_sibling_ports() -> set[int]:
    """Keep Lite's saved app and Caddy ports free even while Lite is stopped."""
    shared = Path(os.environ.get("PROGRAMDATA") or "C:/ProgramData") / "Setuora"
    reserved: set[int] = set()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    for name in ("Setuora-Lite", "Setuora-Lite-windows"):
        root = shared / name
        path = root / ".runtime-ports.json"
        if not root.exists():
            continue
        if root.is_symlink() or getattr(root.lstat(), "st_file_attributes", 0) & reparse_flag:
            raise DeploymentError(f"Lite installation folder is linked: {root}")
        if not path.exists():
            continue
        if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & reparse_flag:
            raise DeploymentError(f"Lite port file is linked: {path}")
        if path.stat().st_size > 4096:
            raise DeploymentError(f"Lite port file is unexpectedly large: {path}")
        try:
            values = json.loads(path.read_text(encoding="ascii"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise DeploymentError(
                f"Lite port file is invalid: {path}. Choose Start in Lite's setuora.bat."
            ) from exc
        if not isinstance(values, dict):
            raise DeploymentError(
                f"Lite port file is invalid: {path}. Choose Start in Lite's setuora.bat."
            )
        for key in ("web_port", "caddy_port"):
            port = values.get(key)
            if type(port) is not int or not 1024 <= port <= 65535:
                raise DeploymentError(
                    f"Lite port file has an invalid {key}: {path}. Choose Start in Lite's setuora.bat."
                )
            reserved.add(port)
    return reserved


def _select_available_web_port() -> int:
    current = _web_port()
    reserved = _reserved_sibling_ports()
    candidates = [current, *(port for port in range(DEFAULT_WEB_PORT, 9000) if port != current)]
    for port in candidates:
        if port not in reserved and not _port_listeners(port) and _can_bind_port(port):
            if port != current:
                print(
                    f"Local port {current} is occupied or reserved; Setuora Master will use {port}."
                )
            _save_web_port(port)
            return port
    raise DeploymentError("No free local web port is available from 8000 through 8999.")


def _find_tailscale_executable() -> str | None:
    found = shutil.which("tailscale.exe")
    if found:
        return found
    candidate = (
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Tailscale" / "tailscale.exe"
    )
    if candidate.is_file():
        return str(candidate)
    return None


def _tailscale_executable() -> str:
    found = _find_tailscale_executable()
    if found:
        return found
    winget = shutil.which("winget.exe")
    if winget:
        print("Installing Tailscale with Windows Package Manager...")
        result = _run(
            [
                winget,
                "install",
                "--id",
                "Tailscale.Tailscale",
                "--exact",
                "--source",
                "winget",
                "--scope",
                "machine",
                "--accept-package-agreements",
                "--accept-source-agreements",
                "--disable-interactivity",
            ],
            check=False,
        )
        if result.returncode == 3010:
            raise DeploymentError(
                "Tailscale installed; Windows needs a restart. Reboot and choose Start again."
            )
        if result.returncode == 0:
            for _ in range(20):
                found = _find_tailscale_executable()
                if found:
                    return found
                time.sleep(1)
        print(
            "Windows Package Manager did not finish Tailscale installation; trying the signed official MSI."
        )

    architecture = "amd64" if platform.machine().lower() in {"amd64", "x86_64"} else "x86"
    url = f"https://pkgs.tailscale.com/stable/tailscale-setup-latest-{architecture}.msi"
    print("Downloading the signed Tailscale installer from pkgs.tailscale.com...")
    with tempfile.TemporaryDirectory(prefix="setuora-tailscale-") as directory:
        installer = Path(directory) / "tailscale.msi"
        with urllib.request.urlopen(url, timeout=60) as response, installer.open("wb") as target:  # nosec B310
            shutil.copyfileobj(response, target)
        signature_script = (
            "$signature = Get-AuthenticodeSignature -LiteralPath $args[0]; "
            "if ($signature.Status -ne 'Valid' -or "
            "$signature.SignerCertificate.Subject -notmatch '(^|,)\\s*CN=Tailscale Inc\\.?($|,)') "
            "{ exit 1 }"
        )
        signature_path = Path(directory) / "verify-signature.ps1"
        signature_path.write_text(signature_script, encoding="utf-8")
        signature = _run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(signature_path),
                str(installer),
            ],
            check=False,
        )
        if signature.returncode != 0:
            raise DeploymentError(
                "The downloaded Tailscale MSI lacks a valid Tailscale signature; installation stopped."
            )
        result = _run(["msiexec.exe", "/i", str(installer), "/quiet", "/norestart"], check=False)
        if result.returncode == 3010:
            raise DeploymentError(
                "Tailscale installed; Windows needs a restart. Reboot and choose Start again."
            )
        if result.returncode != 0:
            raise DeploymentError(f"Tailscale MSI installation failed (exit {result.returncode}).")
    for _ in range(20):
        found = _find_tailscale_executable()
        if found:
            return found
        time.sleep(1)
    raise DeploymentError("Tailscale was installed but its CLI was not found. Choose Start again.")


def _tailscale_json(executable: str, *arguments: str) -> dict[str, object]:
    result = _run([executable, *arguments], capture=True)
    payload = json.loads(result.stdout or "{}") or {}
    if not isinstance(payload, dict):
        raise DeploymentError("Tailscale returned unexpected status data.")
    return payload


def _tailscale_identity(executable: str) -> str:
    try:
        _run(["sc.exe", "config", "Tailscale", "start=", "auto"])
        _run(["sc.exe", "start", "Tailscale"], check=False)
    except subprocess.CalledProcessError as exc:
        raise TailscaleUnavailable(
            "Tailscale's Windows service is unavailable. Choose Start."
        ) from exc
    state = None
    for _ in range(20):
        try:
            state = _tailscale_json(executable, "status", "--json")
            break
        except (subprocess.CalledProcessError, ValueError):
            time.sleep(1)
    if state is None:
        raise TailscaleUnavailable(
            "Tailscale service did not become ready. Restart Windows and choose Start again."
        )
    if state.get("BackendState") != "Running":
        print("Sign in to Tailscale in the browser when prompted. Use the same account on Lite.")
        try:
            _run([executable, "up", "--unattended=true", "--timeout=5m"])
        except subprocess.CalledProcessError as exc:
            raise TailscaleUnavailable(
                "Tailscale sign-in is incomplete. Follow the link printed above, then choose Start."
            ) from exc
    else:
        # `set` changes only this preference; `up` can reject an existing device
        # with other custom flags unless every one is repeated.
        try:
            _run([executable, "set", "--unattended=true"])
        except subprocess.CalledProcessError as exc:
            raise TailscaleUnavailable(
                "Tailscale could not enable unattended mode. Choose Start."
            ) from exc
    try:
        state = _tailscale_json(executable, "status", "--json")
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise TailscaleUnavailable(
            "Tailscale disconnected during setup. Choose Start again."
        ) from exc
    name = str((state.get("Self") or {}).get("DNSName") or "").rstrip(".").lower()
    if (
        state.get("BackendState") != "Running"
        or not (state.get("Self") or {}).get("Online")
        or not name.endswith(".ts.net")
    ):
        raise TailscaleUnavailable(
            "Tailscale is not connected or MagicDNS is disabled. Enable MagicDNS in "
            "the Tailscale DNS admin page, then choose Start again."
        )
    return name


def _serve_handler(config: dict[str, object], host: str) -> tuple[object, bool]:
    web = config.get("Web") or {}
    site = web.get(f"{host}:443", {}) if isinstance(web, dict) else {}
    handlers = site.get("Handlers", {}) if isinstance(site, dict) else {}
    funnel = config.get("AllowFunnel") or {}
    public = bool(funnel.get(f"{host}:443")) if isinstance(funnel, dict) else False
    return handlers, public


def _check_serve_https_port(config: dict[str, object]) -> None:
    tcp = config.get("TCP") or {}
    if not isinstance(tcp, dict):
        raise DeploymentError("Tailscale Serve returned unexpected TCP route data.")
    for port, handler in tcp.items():
        if str(port).split(":")[-1] != "443":
            continue
        # Tailscale also records normal HTTPS Serve under TCP. A TCP forward
        # conflicts with the web route; the HTTPS marker does not.
        if (
            not isinstance(handler, dict)
            or handler.get("HTTPS") is not True
            or handler.get("TCPForward")
        ):
            raise DeploymentError("Tailscale port 443 is already used by a TCP route.")


def _ensure_private_serve(executable: str, host: str) -> None:
    try:
        config = _tailscale_json(executable, "serve", "status", "--json")
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise TailscaleUnavailable(
            "Tailscale Serve is offline. Retry when the tailnet is connected."
        ) from exc
    _check_serve_https_port(config)
    handlers, public = _serve_handler(config, host)
    if public:
        raise DeploymentError(
            "Tailscale Funnel is enabled on HTTPS port 443. Disable it before sharing Setuora privately."
        )
    if not isinstance(handlers, dict):
        raise DeploymentError("Tailscale Serve returned unexpected handler data.")
    target = _node_target()
    previous_target = _previous_node_target()
    for path, handler in handlers.items():
        proxy = handler.get("Proxy") if isinstance(handler, dict) else None
        if path == NODE_PATH and proxy in {target, previous_target}:
            continue
        if path == "/":
            # Lite may own the root route on a shared Tailscale computer.
            master_roots = {target.removesuffix(NODE_PATH)}
            if previous_target:
                master_roots.add(previous_target.removesuffix(NODE_PATH))
            if isinstance(proxy, str) and proxy.rstrip("/") in master_roots:
                raise DeploymentError("Tailscale Serve exposes the Master admin console at /.")
            continue
        if path.startswith(NODE_PATH) or NODE_PATH.startswith(path):
            raise DeploymentError(
                f"Tailscale Serve already uses overlapping path {path!r} on HTTPS port 443. "
                "Remove that mapping manually; Setuora will not overwrite it."
            )
    # Reapply our exact mapping in background mode so a prior foreground Serve
    # session cannot disappear when its terminal closes.
    serve = _run(
        [executable, "serve", "--bg", "--https=443", f"--set-path={NODE_PATH}", target],
        check=False,
    )
    if serve.returncode != 0:
        raise DeploymentError(
            "Tailscale could not enable private HTTPS Serve. Follow the approval link printed above "
            "to enable MagicDNS and HTTPS certificates, then choose Start again."
        )
    configured = _tailscale_json(executable, "serve", "status", "--json")
    actual, public = _serve_handler(configured, host)
    node = actual.get(NODE_PATH) if isinstance(actual, dict) else None
    if public or not isinstance(node, dict) or node.get("Proxy") != target:
        raise DeploymentError("Tailscale Serve did not retain the private Setuora API route.")


def _disable_own_serve() -> None:
    _, values = _read_env()
    address = values.get(TAILSCALE_URL_SETTING, "")
    if not address.startswith("https://"):
        return
    executable = _find_tailscale_executable()
    if not executable:
        print(
            "Tailscale is unavailable; stopping local Master. Its API route cannot be checked until Tailscale is restored."
        )
        return
    host = address.removeprefix("https://")
    try:
        config = _tailscale_json(executable, "serve", "status", "--json")
    except (subprocess.CalledProcessError, ValueError, OSError):
        print(
            "Tailscale is offline; stopping local Master. Recheck the private API route when Tailscale reconnects."
        )
        return
    handlers, _ = _serve_handler(config, host)
    node = handlers.get(NODE_PATH) if isinstance(handlers, dict) else None
    if isinstance(node, dict) and node.get("Proxy") in {_node_target(), _previous_node_target()}:
        _run([executable, "serve", "--https=443", f"--set-path={NODE_PATH}", "off"])
        config = _tailscale_json(executable, "serve", "status", "--json")
        handlers, _ = _serve_handler(config, host)
        if isinstance(handlers, dict) and NODE_PATH in handlers:
            raise DeploymentError(
                "Tailscale did not stop the Setuora API route; update was cancelled."
            )


def _verify_private_api(address: str) -> None:
    if not re.fullmatch(r"https://[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.ts\.net", address):
        raise DeploymentError("Tailscale returned an invalid private HTTPS address.")
    try:
        with urllib.request.urlopen(address + "/api/v1/node", timeout=15) as response:  # noqa: S310  # nosec B310
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (OSError, urllib.error.URLError) as exc:
        raise TailscaleUnavailable(
            f"Cannot reach private Master HTTPS address {address}: {exc}"
        ) from exc
    if status != 401:
        raise DeploymentError(
            f"Private HTTPS check for /api/v1/node returned {status}, expected 401. "
            "Check the Tailscale Serve route and access policy."
        )
    try:
        with urllib.request.urlopen(address + "/", timeout=15) as response:  # noqa: S310  # nosec B310
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (OSError, urllib.error.URLError) as exc:
        raise TailscaleUnavailable(
            f"Cannot reach private Master HTTPS address {address}: {exc}"
        ) from exc
    if status == 200:
        try:
            with urllib.request.urlopen(address + "/health", timeout=15) as response:  # noqa: S310  # nosec B310
                health = json.load(response)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise DeploymentError(
                "Private HTTPS root has no verified Lite health endpoint."
            ) from exc
        if health != {"status": "ok", "role": "lite"}:
            raise DeploymentError("Private HTTPS root exposes a service other than Setuora Lite.")
    elif status not in {404, 502, 503}:
        raise DeploymentError("Private HTTPS root returned an unexpected response.")


def _save_connection_address(address: str) -> None:
    # Fresh setup may launch deploy.py with system Python before the application
    # environment exists. Use the installed runtime for SQLAlchemy access.
    script = """import sys
from app.database import SessionLocal
from app.services.lite_connection import MASTER_URL_SETTING
from app.services.settings import get_setting, update_settings
address = sys.argv[1]
with SessionLocal() as db:
    current = get_setting(db, MASTER_URL_SETTING)
    if not current:
        update_settings(db, {MASTER_URL_SETTING: address})
    elif current != address:
        print(f"Franchises still uses {current}. Existing Lite connections may depend on it. "
              f"After moving them to Tailscale, save {address} in Franchises and issue replacement details.")
"""
    _run([str(_venv_python()), "-c", script, address])


def _configure_private_api(*, install: bool = True) -> None:
    executable = _tailscale_executable() if install else _find_tailscale_executable()
    if not executable:
        raise TailscaleUnavailable(
            "Tailscale is missing. Choose Start to restore private Lite access."
        )
    host = _tailscale_identity(executable)
    address = f"https://{host}"
    _, values = _read_env()
    trusted = [item.strip() for item in values.get("TRUSTED_HOSTS", "").split(",") if item.strip()]
    if host not in trusted:
        trusted.append(host)
        _write_env({"TRUSTED_HOSTS": ",".join(trusted)})
        # The app reads trusted hosts at startup.
        _task("/End", "/TN", TASK_NAME, check=False)
        try:
            _wait_for_stop(timeout_seconds=3)
        except DeploymentError:
            _release_setuora_port()
        _task("/Run", "/TN", TASK_NAME)
        _wait_for_health()
    _ensure_private_serve(executable, host)
    _verify_private_api(address)
    _write_env({"SETUORA_PREVIOUS_WEB_PORT": ""})
    _save_connection_address(address)
    _write_env({TAILSCALE_URL_SETTING: address})
    print(f"Private Master address for Lite connection details: {address}")


def _check_private_api_status() -> str:
    executable = _find_tailscale_executable()
    if not executable:
        raise DeploymentError(
            "Master is running locally, but Tailscale is not installed. Choose Start."
        )
    try:
        state = _tailscale_json(executable, "status", "--json")
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise TailscaleUnavailable("Master is running locally, but Tailscale is offline.") from exc
    host = str((state.get("Self") or {}).get("DNSName") or "").rstrip(".").lower()
    if (
        state.get("BackendState") != "Running"
        or not (state.get("Self") or {}).get("Online")
        or not host.endswith(".ts.net")
    ):
        raise DeploymentError(
            "Master is running locally, but Tailscale is disconnected. Choose Start."
        )
    try:
        config = _tailscale_json(executable, "serve", "status", "--json")
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise TailscaleUnavailable(
            "Master is running locally, but Tailscale Serve is offline."
        ) from exc
    _check_serve_https_port(config)
    handlers, public = _serve_handler(config, host)
    if public or not isinstance(handlers, dict):
        raise DeploymentError(
            "Master is running locally, but the private Tailscale Serve route is unavailable."
        )
    node = handlers.get(NODE_PATH)
    if not isinstance(node, dict) or node.get("Proxy") != _node_target(_public_web_port()):
        raise DeploymentError(
            "Master is running locally, but the private Tailscale Serve route is unavailable."
        )
    address = f"https://{host}"
    _verify_private_api(address)
    return address


def _has_application_data() -> bool:
    _, values = _read_env()
    url = values.get("DATABASE_URL") or "sqlite:///./data/setuora.db"
    if not url.startswith("sqlite:///"):
        return False
    database_path = Path(url.removeprefix("sqlite:///"))
    if not database_path.is_absolute():
        database_path = PROJECT_ROOT / database_path
    return database_path.is_file()


def preflight(_args: argparse.Namespace) -> None:
    _check_windows()
    if not ENV_PATH.exists():
        raise DeploymentError(".env is missing. Run `setuora.ps1 setup` first.")
    _, values = _read_env()
    issues = _environment_issues(values, has_application_data=_has_application_data())
    for required in (RUNNER_PATH, PROJECT_ROOT / "requirements-runtime.lock"):
        if not required.is_file():
            issues.append(f"Required deployment file is missing: {required.name}")
    if issues:
        raise DeploymentError("Preflight failed:\n- " + "\n- ".join(issues))
    print("Windows production configuration preflight passed without exposing secrets.")


def setup(_args: argparse.Namespace) -> None:
    _check_windows()
    _prepare_environment()
    preflight(_args)
    existing_task = _task("/Query", "/TN", TASK_NAME, check=False, capture=True).returncode == 0
    if existing_task:
        stop(_args)
    else:
        _release_setuora_port()
    _select_available_web_port()
    try:
        _install_runtime()
        _ensure_task()
    except (DeploymentError, subprocess.CalledProcessError, OSError) as exc:
        if existing_task:
            print("Setup repair failed while preparing the runtime. Attempting to restart Master.")
            try:
                start(_args)
            except (DeploymentError, subprocess.CalledProcessError, OSError) as restart_exc:
                raise DeploymentError(
                    f"Setup repair failed ({exc}); Master also could not restart ({restart_exc})."
                ) from exc
        raise
    _apply_lan_access()
    _task("/Run", "/TN", TASK_NAME)
    print(f"Waiting for Setuora Master to answer at {_local_url()} ...", flush=True)
    try:
        _wait_for_health()
        _write_env({"BOOTSTRAP_ADMIN_PASSWORD": ""})
        try:
            _configure_private_api()
        except TailscaleUnavailable as exc:
            # An existing Master keeps serving its console when the tailnet is offline.
            if not existing_task:
                raise
            print(f"Private Lite API is offline: {exc}")
    except (DeploymentError, subprocess.CalledProcessError, OSError):
        # Leave a failed Start stopped instead of half running.
        try:
            stop(_args)
        except (DeploymentError, subprocess.CalledProcessError, OSError) as stop_exc:
            print(f"Cleanup: {stop_exc}")
        raise
    print("Setuora Master is healthy on Windows.")
    if _lan_enabled():
        _warn_if_public_network()
    _print_startup_summary()


def start(_args: argparse.Namespace) -> None:
    _check_windows()
    preflight(_args)
    task = _task("/Query", "/TN", TASK_NAME, check=False, capture=True)
    if task.returncode != 0:
        raise DeploymentError("The Setuora background task is missing. Choose Start to repair it.")
    _disable_own_serve()
    _task("/End", "/TN", TASK_NAME, check=False)
    try:
        _wait_for_stop(timeout_seconds=3)
    except DeploymentError:
        _release_setuora_port()
    _select_available_web_port()
    # Stop disables the boot task; re-enable it before running.
    _task("/Change", "/TN", TASK_NAME, "/ENABLE")
    _apply_lan_access()
    _task("/Run", "/TN", TASK_NAME)
    print(f"Waiting for Setuora Master to answer at {_local_url()} ...", flush=True)
    _wait_for_health()
    try:
        _configure_private_api(install=False)
    except TailscaleUnavailable as exc:
        print(f"Private Lite API is offline: {exc}")
    print(f"Setuora Master is running at {_local_url()}.")
    if _lan_enabled():
        _warn_if_public_network()
    _print_startup_summary()


def stop(_args: argparse.Namespace) -> None:
    _check_windows()
    _disable_own_serve()
    # Disable first so the boot trigger or failure restart cannot race with
    # Stop, and a stopped Master stays stopped after Windows restarts.
    _task("/Change", "/TN", TASK_NAME, "/DISABLE", check=False)
    _task("/End", "/TN", TASK_NAME, check=False)
    _remove_lan_firewall_rule()
    try:
        _wait_for_stop(timeout_seconds=3)
    except DeploymentError:
        _release_setuora_port()
    print(
        "Setuora Master stopped and stays stopped until you choose Start. "
        "The database was preserved."
    )


def status(_args: argparse.Namespace) -> None:
    _check_windows()
    task = _task("/Query", "/TN", TASK_NAME, "/FO", "LIST", check=False, capture=True)
    if task.returncode == 0 and task.stdout.strip():
        print(task.stdout.strip())
    url = _local_url(_public_web_port()) + "/health"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:  # noqa: S310  # nosec B310
            payload = json.load(response)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise DeploymentError(
            "Setuora Master is not responding. Choose Start. "
            "If Start fails, check logs\\setuora.log."
        ) from exc
    if payload != {"status": "ok", "role": "master"}:
        raise DeploymentError(
            f"Port {_public_web_port()} is being used by a different application."
        )
    print("Setuora Master is running and its database is responding.")
    print(f"Open {_local_url(_public_web_port())} in your browser.")
    lan_public = LAN_STATE_PATH.is_file() and LAN_STATE_PATH.read_text(encoding="ascii").strip() == "true"
    if lan_public:
        for url in _lan_urls(_public_web_port()):
            print(f"Other PCs on this network: {url}")
    address = _check_private_api_status()
    print(f"Private Lite API is responding at {address}/api/v1/.")
    _print_startup_summary(_public_web_port(), address)


def logs(args: argparse.Namespace) -> None:
    _check_windows()
    log_path = PROJECT_ROOT / "logs" / "setuora.log"
    if not log_path.exists():
        raise DeploymentError("No server log exists yet. Start Setuora first.")
    command = [
        "powershell.exe",
        "-NoProfile",
        "Get-Content",
        str(log_path),
        "-Tail",
        str(args.tail),
    ]
    if args.follow:
        command.append("-Wait")
    _run(command)


def update(_args: argparse.Namespace) -> None:
    _check_windows()
    if not ENV_PATH.exists():
        raise DeploymentError("Run `setuora.ps1 setup` first.")
    preflight(_args)
    stop(_args)
    try:
        _install_runtime()
        _ensure_task()
    except (DeploymentError, subprocess.CalledProcessError, OSError) as exc:
        print(
            "Update failed while preparing the runtime. Attempting to restart the previous installation."
        )
        try:
            start(_args)
        except (DeploymentError, subprocess.CalledProcessError, OSError) as restart_exc:
            raise DeploymentError(
                f"Update failed ({exc}); the previous installation also could not restart ({restart_exc})."
            ) from exc
        raise
    start(_args)
    print(
        "Setuora Master was updated and its local server is healthy. Run Status to check the private Lite API."
    )


def run_server(_args: argparse.Namespace) -> None:
    """Start the scheduled server with the port selected during setup."""
    port = _web_port()
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        _bind_host(),
        "--port",
        str(port),
        "--workers",
        "1",
        "--no-access-log",
    ]
    raise SystemExit(_run(command, check=False).returncode)


def _set_lan_access(args: argparse.Namespace, enabled: bool) -> None:
    _check_windows()
    if not ENV_PATH.exists():
        raise DeploymentError("Run `setuora.ps1 setup` first.")
    _write_env({LAN_SETTING: "true" if enabled else "false"})
    if enabled:
        print("LAN access turned on. Restarting Setuora Master...")
    else:
        print("LAN access turned off. Restarting Setuora Master for this computer only...")
    start(args)


def lan_on(args: argparse.Namespace) -> None:
    _set_lan_access(args, True)


def lan_off(args: argparse.Namespace) -> None:
    _set_lan_access(args, False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deploy Setuora Master natively on Windows.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, function, help_text in (
        ("preflight", preflight, "validate the Windows production configuration"),
        ("setup", setup, "install, configure, start, and verify Setuora Master"),
        ("start", start, "start the Windows scheduled task"),
        ("stop", stop, "stop Setuora while preserving data"),
        ("status", status, "show the Windows scheduled task state"),
        ("update", update, "update dependencies and restart"),
        ("run-server", run_server, "run the scheduled local web server"),
        ("lan-on", lan_on, "allow other PCs on the local network to open Master"),
        ("lan-off", lan_off, "allow only this computer to open Master"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.set_defaults(function=function)
    logs_parser = subparsers.add_parser("logs", help="show the native Windows server log")
    logs_parser.add_argument("--tail", type=int, default=200)
    logs_parser.add_argument("--follow", action="store_true")
    logs_parser.set_defaults(function=logs)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.function(args)
    except (DeploymentError, subprocess.CalledProcessError, OSError) as exc:
        print(f"Deployment failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
