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
NODE_TARGET = "http://127.0.0.1:8000/api/v1/"
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
        web_port = int(values.get("SETUORA_WEB_PORT", "8000"))
    except ValueError:
        web_port = 0
    if web_port != 8000:
        issues.append("SETUORA_WEB_PORT must remain 8000 for the Windows service.")

    if values.get("SFTP_SYNC_ENABLED", "false").strip().lower() == "true":
        issues.append("SFTP_SYNC_ENABLED must be false for the central-Tally deployment.")

    if values.get("AUTOMATIC_BACKUPS_ENABLED", "true").strip().lower() != "true":
        issues.append("AUTOMATIC_BACKUPS_ENABLED must be true for production.")
    try:
        retention = int(values.get("BACKUP_RETENTION_COUNT", "14"))
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
            "SETUORA_WEB_PORT": values.get("SETUORA_WEB_PORT") or "8000",
        }
    )
    _write_env(updates)
    (PROJECT_ROOT / "data").mkdir(parents=True, exist_ok=True)
    (PROJECT_ROOT / "logs").mkdir(parents=True, exist_ok=True)
    _secure_private_storage()


def _secure_private_storage() -> None:
    """Keep the local database, backups, and logs readable only by the service and admins."""
    if sys.platform != "win32":
        return
    for directory in (PROJECT_ROOT / "data", PROJECT_ROOT / "logs"):
        _run(
            [
                "icacls.exe",
                str(directory),
                "/inheritance:r",
                "/remove:g",
                "*S-1-1-0",
                "*S-1-5-11",
                "*S-1-5-32-545",
                "/grant:r",
                "*S-1-5-18:(OI)(CI)F",
                "*S-1-5-32-544:(OI)(CI)F",
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


def _wait_for_health(timeout_seconds: int = 120) -> None:
    _, values = _read_env()
    port = values.get("SETUORA_WEB_PORT", "8000")
    deadline = time.monotonic() + timeout_seconds
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:  # nosec B310
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
        if not _port_listeners():
            return
        time.sleep(0.5)
    raise DeploymentError(
        "Port 8000 is still in use after stopping the task. "
        "Check the running Setuora process before updating files."
    )


def _port_listeners() -> list[dict[str, object]]:
    """Read all Windows TCP listeners on our fixed port, including wildcard binds."""
    script = (
        "$items = @(Get-NetTCPConnection -State Listen -LocalPort 8000 "
        "-ErrorAction SilentlyContinue | ForEach-Object { "
        "$p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $_.OwningProcess) "
        "-ErrorAction Stop; [pscustomobject]@{Pid=$_.OwningProcess; "
        "ExecutablePath=$p.ExecutablePath; CommandLine=$p.CommandLine} }); "
        "ConvertTo-Json -InputObject $items -Compress -Depth 3"
    )
    result = _run(["powershell.exe", "-NoProfile", "-Command", script], capture=True)
    try:
        listeners = json.loads(result.stdout or "[]")
    except ValueError as exc:
        raise DeploymentError("Windows could not identify the process using port 8000.") from exc
    if not isinstance(listeners, list) or any(not isinstance(item, dict) for item in listeners):
        raise DeploymentError("Windows returned invalid process details for port 8000.")
    return listeners


def _is_own_listener(listener: dict[str, object]) -> bool:
    expected = str(_venv_python().resolve()).replace("/", "\\").casefold()
    executable = str(listener.get("ExecutablePath") or "").replace("/", "\\").casefold()
    command = str(listener.get("CommandLine") or "").casefold()
    return (
        executable == expected
        and "-m uvicorn app.main:app" in command
        and "--host 127.0.0.1" in command
        and "--port 8000" in command
    )


def _release_setuora_port() -> None:
    listeners = _port_listeners()
    if any(not _is_own_listener(item) for item in listeners):
        raise DeploymentError(
            "Port 8000 belongs to another process. Close that application or choose a "
            "different computer; Setuora will not terminate it."
        )
    for pid in sorted({int(item["Pid"]) for item in listeners}):
        print(f"Stopping orphaned Setuora Master process {pid} on port 8000.")
        _run(["taskkill.exe", "/PID", str(pid), "/F"])
    _wait_for_stop(timeout_seconds=10 if listeners else 1)


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
                "Tailscale installed; Windows needs a restart. Reboot and run Setup / repair again."
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
                "Tailscale installed; Windows needs a restart. Reboot and run Setup / repair again."
            )
        if result.returncode != 0:
            raise DeploymentError(f"Tailscale MSI installation failed (exit {result.returncode}).")
    for _ in range(20):
        found = _find_tailscale_executable()
        if found:
            return found
        time.sleep(1)
    raise DeploymentError(
        "Tailscale was installed but its CLI was not found. Run Setup / repair again."
    )


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
            "Tailscale's Windows service is unavailable. Run Setup / repair."
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
            "Tailscale service did not become ready. Restart Windows and run Setup / repair again."
        )
    if state.get("BackendState") != "Running":
        print("Sign in to Tailscale in the browser when prompted. Use the same account on Lite.")
        try:
            _run([executable, "up", "--unattended=true", "--timeout=5m"])
        except subprocess.CalledProcessError as exc:
            raise TailscaleUnavailable(
                "Tailscale sign-in is incomplete. Follow the link printed above, then run Setup / repair."
            ) from exc
    else:
        # `set` changes only this preference; `up` can reject an existing device
        # with other custom flags unless every one is repeated.
        try:
            _run([executable, "set", "--unattended=true"])
        except subprocess.CalledProcessError as exc:
            raise TailscaleUnavailable(
                "Tailscale could not enable unattended mode. Run Setup / repair."
            ) from exc
    try:
        state = _tailscale_json(executable, "status", "--json")
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise TailscaleUnavailable(
            "Tailscale disconnected during setup. Retry Setup / repair."
        ) from exc
    name = str((state.get("Self") or {}).get("DNSName") or "").rstrip(".").lower()
    if (
        state.get("BackendState") != "Running"
        or not (state.get("Self") or {}).get("Online")
        or not name.endswith(".ts.net")
    ):
        raise TailscaleUnavailable(
            "Tailscale is not connected or MagicDNS is disabled. Enable MagicDNS in "
            "the Tailscale DNS admin page, then run Setup / repair again."
        )
    return name


def _serve_handler(config: dict[str, object], host: str) -> tuple[object, bool]:
    web = config.get("Web") or {}
    site = web.get(f"{host}:443", {}) if isinstance(web, dict) else {}
    handlers = site.get("Handlers", {}) if isinstance(site, dict) else {}
    funnel = config.get("AllowFunnel") or {}
    public = bool(funnel.get(f"{host}:443")) if isinstance(funnel, dict) else False
    return handlers, public


def _ensure_private_serve(executable: str, host: str) -> None:
    try:
        config = _tailscale_json(executable, "serve", "status", "--json")
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise TailscaleUnavailable(
            "Tailscale Serve is offline. Retry when the tailnet is connected."
        ) from exc
    handlers, public = _serve_handler(config, host)
    if public:
        raise DeploymentError(
            "Tailscale Funnel is enabled on HTTPS port 443. Disable it before sharing Setuora privately."
        )
    if not isinstance(handlers, dict):
        raise DeploymentError("Tailscale Serve returned unexpected handler data.")
    for path, handler in handlers.items():
        if path == NODE_PATH and isinstance(handler, dict) and handler.get("Proxy") == NODE_TARGET:
            continue
        if path == "/" or path.startswith(NODE_PATH) or NODE_PATH.startswith(path):
            raise DeploymentError(
                f"Tailscale Serve already uses overlapping path {path!r} on HTTPS port 443. "
                "Remove that mapping manually; Setuora will not overwrite it."
            )
    # Reapply our exact mapping in background mode so a prior foreground Serve
    # session cannot disappear when its terminal closes.
    serve = _run(
        [executable, "serve", "--bg", "--https=443", f"--set-path={NODE_PATH}", NODE_TARGET],
        check=False,
    )
    if serve.returncode != 0:
        raise DeploymentError(
            "Tailscale could not enable private HTTPS Serve. Follow the approval link printed above "
            "to enable MagicDNS and HTTPS certificates, then run Setup / repair again."
        )
    configured = _tailscale_json(executable, "serve", "status", "--json")
    actual, public = _serve_handler(configured, host)
    node = actual.get(NODE_PATH) if isinstance(actual, dict) else None
    if public or not isinstance(node, dict) or node.get("Proxy") != NODE_TARGET:
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
    if isinstance(node, dict) and node.get("Proxy") == NODE_TARGET:
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
    checks = (("/api/v1/node", 401), ("/", 404))
    for path, expected in checks:
        try:
            with urllib.request.urlopen(address + path, timeout=15) as response:  # noqa: S310  # nosec B310
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except (OSError, urllib.error.URLError) as exc:
            raise TailscaleUnavailable(
                f"Cannot reach private Master HTTPS address {address}: {exc}"
            ) from exc
        if status != expected:
            raise DeploymentError(
                f"Private HTTPS check for {path} returned {status}, expected {expected}. "
                "Check the Tailscale Serve route and access policy."
            )


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
            "Tailscale is missing. Run Setup / repair to restore private Lite access."
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
    _save_connection_address(address)
    _write_env({TAILSCALE_URL_SETTING: address})
    print(f"Private Master address for Lite connection details: {address}")


def _check_private_api_status() -> str:
    executable = _find_tailscale_executable()
    if not executable:
        raise DeploymentError(
            "Master is running locally, but Tailscale is not installed. Choose Setup / repair."
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
            "Master is running locally, but Tailscale is disconnected. Choose Setup / repair."
        )
    try:
        config = _tailscale_json(executable, "serve", "status", "--json")
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise TailscaleUnavailable(
            "Master is running locally, but Tailscale Serve is offline."
        ) from exc
    handlers, public = _serve_handler(config, host)
    if public or not isinstance(handlers, dict):
        raise DeploymentError(
            "Master is running locally, but the private Tailscale Serve route is unavailable."
        )
    node = handlers.get(NODE_PATH)
    if not isinstance(node, dict) or node.get("Proxy") != NODE_TARGET:
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
    _task("/Run", "/TN", TASK_NAME)
    _wait_for_health()
    _write_env({"BOOTSTRAP_ADMIN_PASSWORD": ""})
    _configure_private_api()
    print("Setuora Master is healthy on Windows.")
    print("Admin console: http://127.0.0.1:8000")


def start(_args: argparse.Namespace) -> None:
    _check_windows()
    preflight(_args)
    task = _task("/Query", "/TN", TASK_NAME, check=False, capture=True)
    if task.returncode != 0:
        raise DeploymentError(
            "The Setuora background task is missing. Choose Setup / repair first."
        )
    _task("/End", "/TN", TASK_NAME, check=False)
    try:
        _wait_for_stop(timeout_seconds=3)
    except DeploymentError:
        _release_setuora_port()
    _task("/Run", "/TN", TASK_NAME)
    _wait_for_health()
    try:
        _configure_private_api(install=False)
    except TailscaleUnavailable as exc:
        print(f"Private Lite API is offline: {exc}")
    print("Setuora Master is running at http://127.0.0.1:8000.")


def stop(_args: argparse.Namespace) -> None:
    _check_windows()
    _disable_own_serve()
    _task("/End", "/TN", TASK_NAME, check=False)
    try:
        _wait_for_stop(timeout_seconds=3)
    except DeploymentError:
        _release_setuora_port()
    print("Setuora Master stopped. The database was preserved.")


def status(_args: argparse.Namespace) -> None:
    _check_windows()
    task = _task("/Query", "/TN", TASK_NAME, "/FO", "LIST", check=False, capture=True)
    if task.returncode == 0 and task.stdout.strip():
        print(task.stdout.strip())
    url = "http://127.0.0.1:8000/health"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:  # nosec B310
            payload = json.load(response)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise DeploymentError(
            "Setuora Master is not responding. Choose Start, or Setup / repair for a new installation. "
            "If Start fails, choose View recent logs."
        ) from exc
    if payload != {"status": "ok", "role": "master"}:
        raise DeploymentError("Port 8000 is being used by a different application.")
    print("Setuora Master is running and its database is responding.")
    print("Open http://127.0.0.1:8000 in your browser.")
    address = _check_private_api_status()
    print(f"Private Lite API is responding at {address}/api/v1/.")


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
