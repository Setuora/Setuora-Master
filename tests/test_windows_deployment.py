from pathlib import Path

import deploy

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_active_deployment_is_windows_native_with_private_tailscale_serve():
    assert not (PROJECT_ROOT / "compose.yaml").exists()
    assert not (PROJECT_ROOT / "Dockerfile").exists()
    assert not (PROJECT_ROOT / "deployment" / "tailscale").exists()

    deployment = (PROJECT_ROOT / "deploy.py").read_text(encoding="utf-8").lower()
    sftp = (PROJECT_ROOT / "scripts" / "windows" / "configure-sftp.ps1").read_text(encoding="utf-8")
    assert "schtasks.exe" in deployment
    assert "docker" not in deployment
    assert '"serve", "--bg", "--https=443"' in deployment
    assert 'node_path = "/api/v1/"' in deployment
    assert '"funnel"' not in deployment
    assert "OpenSSH.Server" in sftp
    assert "ForceCommand internal-sftp" in sftp
    assert "ChrootDirectory" in sftp
    assert "AllowTcpForwarding no" in sftp


def test_linux_and_previous_private_network_assets_are_archived():
    assert (PROJECT_ROOT / "archive" / "linux" / "Linux — Setuora Master.run").is_file()
    assert (PROJECT_ROOT / "archive" / "linux" / "client" / "linux" / "setuora").is_file()
    assert (PROJECT_ROOT / "archive" / "linux" / "container-deployment" / "compose.yaml").is_file()
    assert (
        PROJECT_ROOT / "archive" / "tailscale" / "deployment" / "tailscale" / "serve.json"
    ).is_file()


def test_deployment_helper_rewrites_settings_without_exposing_secrets(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "APP_SECRET_KEY=old\nTRUSTED_HOSTS=localhost\n# Preserve this comment\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(deploy, "ENV_PATH", env_path)

    deploy._write_env(
        {
            "APP_SECRET_KEY": "new-secret",
            "TRUSTED_HOSTS": "127.0.0.1,localhost",
            "SESSION_COOKIE_SECURE": "false",
        }
    )

    result = env_path.read_text(encoding="utf-8")
    assert "APP_SECRET_KEY=new-secret" in result
    assert "TRUSTED_HOSTS=127.0.0.1,localhost" in result
    assert "SESSION_COOKIE_SECURE=false" in result
    assert "# Preserve this comment" in result


def test_deployment_helper_quotes_password_characters(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(deploy, "ENV_PATH", env_path)

    password = 'spaces # quotes " and slash \\ stay intact'
    deploy._write_env({"BOOTSTRAP_ADMIN_PASSWORD": password})

    raw = env_path.read_text(encoding="utf-8")
    assert raw.startswith('BOOTSTRAP_ADMIN_PASSWORD="')
    assert deploy._read_env()[1]["BOOTSTRAP_ADMIN_PASSWORD"] == password


def test_production_preflight_rejects_legacy_sftp_and_requires_loopback_console():
    issues = deploy._environment_issues(
        {
            "APP_SECRET_KEY": "x" * 48,
            "BOOTSTRAP_ADMIN_PASSWORD": "strong-admin-password",
            "SESSION_COOKIE_SECURE": "false",
            "SETUORA_APP_MODE": "master",
            "TRUSTED_HOSTS": "127.0.0.1,localhost",
            "SETUORA_WEB_PORT": "8000",
            "SFTP_SYNC_ENABLED": "false",
            "AUTOMATIC_BACKUPS_ENABLED": "true",
            "BACKUP_RETENTION_COUNT": "14",
        },
        has_application_data=False,
    )
    assert issues == []

    issues = deploy._environment_issues(
        {
            "APP_SECRET_KEY": "x" * 48,
            "BOOTSTRAP_ADMIN_PASSWORD": "strong-admin-password",
            "SESSION_COOKIE_SECURE": "true",
            "SETUORA_APP_MODE": "master",
            "TRUSTED_HOSTS": "localhost",
            "SFTP_SYNC_ENABLED": "true",
            "AUTOMATIC_BACKUPS_ENABLED": "true",
            "BACKUP_RETENTION_COUNT": "14",
        },
        has_application_data=False,
    )
    assert "SESSION_COOKIE_SECURE must be false for the loopback-only HTTP console." in issues
    assert "TRUSTED_HOSTS must include localhost and 127.0.0.1." in issues
    assert "SFTP_SYNC_ENABLED must be false for the central-Tally deployment." in issues


def test_batch_and_packaged_controls_share_safe_interactive_elevation():
    controller = (PROJECT_ROOT / "setuora.bat").read_text(encoding="utf-8")
    launcher = (PROJECT_ROOT / "client/windows/setuora.ps1").read_text(encoding="utf-8")
    finder = (PROJECT_ROOT / "scripts/find_python.bat").read_text(encoding="utf-8")

    assert 'set "ROOT_DIR=%~dp0"' in controller
    assert "%ROOT_DIR%setuora.ps1" in controller
    assert "%ROOT_DIR%client\\windows\\setuora.ps1" in controller
    assert "endlocal & exit /b %EXIT_CODE%" in controller
    assert "set /p" not in controller.lower()
    assert "$selection = Read-Host" in launcher
    assert "switch ($selection)" in launcher
    assert "-Verb RunAs -Wait -PassThru" in launcher
    assert "$process.ExitCode" in launcher
    assert "SETUORA_ELEVATED_LOG" not in launcher
    assert "RedirectStandardOutput" not in launcher
    assert "No action was completed" in launcher
    assert "if ($PauseAfter)" in launcher
    assert (
        '$Action -in @("setup", "start", "stop", "preflight", "update", "update-runtime"'
        in launcher
    )
    for action in (
        "open",
        "status",
        "logs",
        "preflight",
        "setup",
        "start",
        "stop",
        "update",
        "help",
    ):
        assert f'"{action}"' in launcher
    assert "Show-SetuoraMenu" in launcher
    assert "Install downloaded update" in launcher
    assert "$ApplicationRoot\\.venv\\Scripts\\python.exe" in launcher
    assert finder.index("..\\.venv\\Scripts\\python.exe") < finder.index("py -3.11")
    for script_name, action in (
        ("setup.bat", "setup"),
        ("start_setuora.bat", "start"),
        ("stop_setuora.bat", "stop"),
        ("update.bat", "update"),
    ):
        wrapper = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")
        assert f'call "%~dp0..\\setuora.bat" {action} %*' in wrapper
        assert "endlocal & exit /b %EXIT_CODE%" in wrapper


def test_source_update_stops_before_mutating_application_files():
    launcher = (PROJECT_ROOT / "client/windows/setuora.ps1").read_text(encoding="utf-8")
    update = launcher[
        launcher.index("function Update-SetuoraSource") : launcher.index(
            "function Install-SetuoraUpdate"
        )
    ]
    assert "--porcelain" in update and "--untracked-files=all" in update
    assert "--is-ancestor" in update
    assert update.index('"fetch"') < update.index('Invoke-SetuoraDeployment "stop"')
    assert update.index('Invoke-SetuoraDeployment "stop"') < update.index('"merge", "--ff-only"')
    assert update.index('"merge", "--ff-only"') < update.index('Invoke-SetuoraDeployment "update"')


def _valid_environment():
    return {
        "APP_SECRET_KEY": "x" * 48,
        "BOOTSTRAP_ADMIN_PASSWORD": "unique-admin-password",
        "SETUORA_APP_MODE": "master",
        "SESSION_COOKIE_SECURE": "false",
        "TRUSTED_HOSTS": "127.0.0.1,localhost",
        "SETUORA_WEB_PORT": "8000",
        "AUTOMATIC_BACKUPS_ENABLED": "true",
        "BACKUP_RETENTION_COUNT": "14",
    }


def test_task_registration_survives_continuous_runtime_battery_and_crashes(tmp_path, monkeypatch):
    from xml.etree import ElementTree

    runner = tmp_path / "warehouse & office" / "run-server.cmd"
    monkeypatch.setattr(deploy, "RUNNER_PATH", runner)
    monkeypatch.setattr(deploy, "PROJECT_ROOT", runner.parent)
    registrations = []

    def register(*arguments):
        assert arguments[:3] == ("/Create", "/TN", deploy.TASK_NAME)
        task_path = Path(arguments[arguments.index("/XML") + 1])
        registrations.append(ElementTree.fromstring(task_path.read_text(encoding="utf-16")))

    monkeypatch.setattr(deploy, "_task", register)
    deploy._ensure_task()
    task = registrations[0]
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert task.findtext("t:Triggers/t:BootTrigger/t:Enabled", namespaces=ns) == "true"
    assert task.findtext("t:Principals/t:Principal/t:UserId", namespaces=ns) == "S-1-5-18"
    for key, expected in {
        "ExecutionTimeLimit": "PT0S",
        "DisallowStartIfOnBatteries": "false",
        "StopIfGoingOnBatteries": "false",
        "MultipleInstancesPolicy": "IgnoreNew",
        "StartWhenAvailable": "true",
        "RestartOnFailure/Interval": "PT1M",
        "RestartOnFailure/Count": "10",
    }.items():
        xpath = "t:Settings/" + "/".join(f"t:{part}" for part in key.split("/"))
        assert task.findtext(xpath, namespaces=ns) == expected
    assert task.findtext("t:Actions/t:Exec/t:Command", namespaces=ns) == "cmd.exe"
    assert task.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns) == (
        f'/d /s /c ""{runner}""'
    )
    assert task.findtext("t:Actions/t:Exec/t:WorkingDirectory", namespaces=ns) == str(runner.parent)


def test_invalid_setup_and_update_fail_before_install_or_stop(tmp_path, monkeypatch):
    import argparse

    import pytest

    env_path = tmp_path / ".env"
    env_path.write_text("APP_SECRET_KEY=invalid\n", encoding="utf-8")
    monkeypatch.setattr(deploy, "ENV_PATH", env_path)
    monkeypatch.setattr(deploy, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(deploy, "_check_windows", lambda: None)
    monkeypatch.setattr(deploy, "_prepare_environment", lambda: None)
    mutations = []
    monkeypatch.setattr(deploy, "_install_runtime", lambda: mutations.append("install"))
    monkeypatch.setattr(deploy, "stop", lambda _: mutations.append("stop"))
    for operation in (deploy.setup, deploy.update):
        with pytest.raises(deploy.DeploymentError, match="Preflight failed"):
            operation(argparse.Namespace())
    assert mutations == []


def test_failed_runtime_update_attempts_to_restore_previous_master(tmp_path, monkeypatch):
    import argparse

    import pytest

    monkeypatch.setattr(deploy, "_check_windows", lambda: None)
    monkeypatch.setattr(deploy, "ENV_PATH", tmp_path / ".env")
    deploy.ENV_PATH.write_text("configured=true\n", encoding="utf-8")
    monkeypatch.setattr(deploy, "preflight", lambda _: None)
    events = []
    monkeypatch.setattr(deploy, "stop", lambda _: events.append("stop"))
    monkeypatch.setattr(deploy, "start", lambda _: events.append("restart"))

    def install_failure():
        events.append("install")
        raise OSError("network outage")

    monkeypatch.setattr(deploy, "_install_runtime", install_failure)
    monkeypatch.setattr(deploy, "_ensure_task", lambda: events.append("task"))
    with pytest.raises(OSError, match="network outage"):
        deploy.update(argparse.Namespace())
    assert events == ["stop", "install", "restart"]


def test_failed_setup_repair_attempts_to_restart_existing_master(monkeypatch):
    import argparse
    import subprocess

    import pytest

    monkeypatch.setattr(deploy, "_check_windows", lambda: None)
    monkeypatch.setattr(deploy, "_prepare_environment", lambda: None)
    monkeypatch.setattr(deploy, "preflight", lambda _: None)
    monkeypatch.setattr(
        deploy, "_task", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0)
    )
    events = []
    monkeypatch.setattr(deploy, "stop", lambda _: events.append("stop"))
    monkeypatch.setattr(deploy, "start", lambda _: events.append("restart"))

    def install_failure():
        events.append("install")
        raise OSError("dependency download failed")

    monkeypatch.setattr(deploy, "_install_runtime", install_failure)
    with pytest.raises(OSError, match="dependency download failed"):
        deploy.setup(argparse.Namespace())
    assert events == ["stop", "install", "restart"]


def test_repair_preserves_existing_database_and_host_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(deploy, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setenv("COMPUTERNAME", "warehouse-pc")
    values = _valid_environment()
    values.update(
        {
            "DATABASE_URL": "sqlite:///./warehouse/custom.db",
            "TRUSTED_HOSTS": "127.0.0.1,localhost,warehouse.example,192.168.1.50",
            "BOOTSTRAP_ADMIN_PASSWORD": "",
        }
    )
    database = tmp_path / "warehouse" / "custom.db"
    database.parent.mkdir()
    database.write_bytes(b"existing database content")
    deploy._write_env(values)

    def unexpected_password_prompt(_):
        raise AssertionError("An existing database must not request another bootstrap password")

    monkeypatch.setattr(deploy, "_prompt_secret", unexpected_password_prompt)
    deploy._prepare_environment()
    stored = deploy._read_env()[1]
    assert stored["DATABASE_URL"] == values["DATABASE_URL"]
    assert stored["APP_SECRET_KEY"] == values["APP_SECRET_KEY"]
    assert {"warehouse.example", "192.168.1.50", "localhost", "127.0.0.1"}.issubset(
        stored["TRUSTED_HOSTS"].split(",")
    )
    assert database.read_bytes() == b"existing database content"
    assert deploy._has_application_data()


def test_preflight_rejects_wrong_port_and_nonpersistent_database():
    values = _valid_environment()
    values.update({"SETUORA_WEB_PORT": "9000", "DATABASE_URL": "sqlite:///:memory:"})
    issues = deploy._environment_issues(values, has_application_data=False)
    assert "SETUORA_WEB_PORT must remain 8000 for the Windows service." in issues
    assert "DATABASE_URL must point to a persistent SQLite database for backups." in issues


def test_stop_waits_until_web_process_releases_port(monkeypatch):
    import argparse

    events = []
    monkeypatch.setattr(deploy, "_check_windows", lambda: None)
    monkeypatch.setattr(deploy, "_disable_own_serve", lambda: None)
    monkeypatch.setattr(deploy, "_task", lambda *args, **kwargs: events.append("task-end"))
    attempts = iter([[{"Pid": 42}], []])

    def listeners():
        events.append("check-port")
        return next(attempts)

    monkeypatch.setattr(deploy, "_port_listeners", listeners)
    monkeypatch.setattr(deploy.time, "sleep", lambda _: None)
    deploy.stop(argparse.Namespace())
    assert events == ["task-end", "check-port", "check-port"]


def test_no_listener_is_free_even_when_loopback_connections_time_out(monkeypatch):
    monkeypatch.setattr(deploy, "_port_listeners", lambda: [])
    deploy._wait_for_stop(timeout_seconds=1)


def test_stop_does_not_report_success_when_process_keeps_running(monkeypatch):
    import argparse

    import pytest

    monkeypatch.setattr(deploy, "_check_windows", lambda: None)
    monkeypatch.setattr(deploy, "_disable_own_serve", lambda: None)
    monkeypatch.setattr(deploy, "_task", lambda *args, **kwargs: None)
    times = iter([0, 31])
    monkeypatch.setattr(deploy.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(
        deploy,
        "_port_listeners",
        lambda: [{"Pid": 88, "ExecutablePath": "C:\\Other\\python.exe", "CommandLine": "other"}],
    )
    with pytest.raises(deploy.DeploymentError, match="belongs to another process"):
        deploy.stop(argparse.Namespace())


def test_port_recovery_only_terminates_exact_master_venv_process(tmp_path, monkeypatch):
    import pytest

    monkeypatch.setattr(deploy, "VENV_PATH", tmp_path / ".venv")
    expected = str(deploy._venv_python().resolve())
    owned = {
        "Pid": 123,
        "ExecutablePath": expected,
        "CommandLine": f'"{expected}" -m uvicorn app.main:app --host 127.0.0.1 --port 8000',
    }
    assert deploy._is_own_listener(owned)
    assert not deploy._is_own_listener({**owned, "ExecutablePath": str(tmp_path / "other.exe")})
    assert not deploy._is_own_listener({**owned, "CommandLine": "python -m http.server 8000"})

    calls = []
    monkeypatch.setattr(deploy, "_run", lambda command, **kwargs: calls.append(command))
    monkeypatch.setattr(deploy, "_wait_for_stop", lambda **kwargs: calls.append("released"))
    monkeypatch.setattr(deploy, "_port_listeners", lambda: [owned])
    deploy._release_setuora_port()
    assert calls == [["taskkill.exe", "/PID", "123", "/F"], "released"]

    calls.clear()
    monkeypatch.setattr(
        deploy,
        "_port_listeners",
        lambda: [owned, {**owned, "Pid": 456, "ExecutablePath": "C:/Other/python.exe"}],
    )
    with pytest.raises(deploy.DeploymentError, match="belongs to another process"):
        deploy._release_setuora_port()
    assert calls == []


def test_private_serve_refuses_funnel_and_overlapping_routes(monkeypatch):
    import pytest

    host = "master.example.ts.net"
    calls = []
    monkeypatch.setattr(deploy, "_run", lambda command, **kwargs: calls.append(command))

    config = {"AllowFunnel": {f"{host}:443": True}}
    monkeypatch.setattr(deploy, "_tailscale_json", lambda *args: config)
    with pytest.raises(deploy.DeploymentError, match="Funnel"):
        deploy._ensure_private_serve("tailscale.exe", host)
    assert calls == []

    config = {"Web": {f"{host}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9000"}}}}}
    with pytest.raises(deploy.DeploymentError, match="overlapping path"):
        deploy._ensure_private_serve("tailscale.exe", host)
    assert calls == []


def test_private_serve_adds_only_node_api_and_checks_persisted_route(monkeypatch):
    import subprocess

    host = "master.example.ts.net"
    before = {"Web": {f"{host}:443": {"Handlers": {"/other": {"Proxy": "http://127.0.0.1:9000"}}}}}
    after = {
        "Web": {
            f"{host}:443": {
                "Handlers": {
                    "/other": {"Proxy": "http://127.0.0.1:9000"},
                    deploy.NODE_PATH: {"Proxy": deploy.NODE_TARGET},
                }
            }
        }
    }
    states = iter([before, after])
    calls = []
    monkeypatch.setattr(deploy, "_tailscale_json", lambda *args: next(states))
    monkeypatch.setattr(
        deploy,
        "_run",
        lambda command, **kwargs: calls.append(command) or subprocess.CompletedProcess(command, 0),
    )
    deploy._ensure_private_serve("tailscale.exe", host)
    assert calls == [
        [
            "tailscale.exe",
            "serve",
            "--bg",
            "--https=443",
            "--set-path=/api/v1/",
            "http://127.0.0.1:8000/api/v1/",
        ]
    ]


def test_private_api_probe_requires_auth_and_blocks_console(monkeypatch):
    import urllib.error

    import pytest

    statuses = iter([401, 404])

    def fetch(url, timeout):
        raise urllib.error.HTTPError(url, next(statuses), "expected", {}, None)

    monkeypatch.setattr(deploy.urllib.request, "urlopen", fetch)
    deploy._verify_private_api("https://master.example.ts.net")

    statuses = iter([200])
    with pytest.raises(deploy.DeploymentError, match="expected 401"):
        deploy._verify_private_api("https://master.example.ts.net")


def test_existing_tailscale_session_sets_unattended_without_redoing_up(monkeypatch):
    states = iter(
        [
            {
                "BackendState": "Running",
                "Self": {"DNSName": "master.example.ts.net.", "Online": True},
            },
            {
                "BackendState": "Running",
                "Self": {"DNSName": "master.example.ts.net.", "Online": True},
            },
        ]
    )
    calls = []
    monkeypatch.setattr(deploy, "_tailscale_json", lambda *args: next(states))
    monkeypatch.setattr(deploy, "_run", lambda command, **kwargs: calls.append(command))
    assert deploy._tailscale_identity("tailscale.exe") == "master.example.ts.net"
    assert calls[-1] == ["tailscale.exe", "set", "--unattended=true"]


def test_private_status_fails_when_route_is_missing(monkeypatch):
    import pytest

    monkeypatch.setattr(deploy, "_find_tailscale_executable", lambda: "tailscale.exe")
    states = iter(
        [
            {
                "BackendState": "Running",
                "Self": {"DNSName": "master.example.ts.net.", "Online": True},
            },
            {"Web": {}},
        ]
    )
    monkeypatch.setattr(deploy, "_tailscale_json", lambda *args: next(states))
    with pytest.raises(deploy.DeploymentError, match="Serve route is unavailable"):
        deploy._check_private_api_status()


def test_stop_removes_only_its_private_serve_mapping(monkeypatch):
    host = "master.example.ts.net"
    before = {
        "Web": {
            f"{host}:443": {
                "Handlers": {
                    deploy.NODE_PATH: {"Proxy": deploy.NODE_TARGET},
                    "/other": {"Proxy": "http://127.0.0.1:9000"},
                }
            }
        }
    }
    after = {
        "Web": {
            f"{host}:443": {
                "Handlers": {
                    "/other": {"Proxy": "http://127.0.0.1:9000"},
                }
            }
        }
    }
    states = iter([before, after])
    calls = []
    monkeypatch.setattr(
        deploy, "_read_env", lambda: ([], {deploy.TAILSCALE_URL_SETTING: f"https://{host}"})
    )
    monkeypatch.setattr(deploy, "_find_tailscale_executable", lambda: "tailscale.exe")
    monkeypatch.setattr(deploy, "_tailscale_json", lambda *args: next(states))
    monkeypatch.setattr(deploy, "_run", lambda command, **kwargs: calls.append(command))
    deploy._disable_own_serve()
    assert calls == [["tailscale.exe", "serve", "--https=443", "--set-path=/api/v1/", "off"]]


def test_offline_tailscale_does_not_block_local_stop_or_start(monkeypatch, capsys):
    import argparse
    import subprocess

    monkeypatch.setattr(
        deploy,
        "_read_env",
        lambda: ([], {deploy.TAILSCALE_URL_SETTING: "https://master.example.ts.net"}),
    )
    monkeypatch.setattr(deploy, "_find_tailscale_executable", lambda: "tailscale.exe")

    def offline(*args):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(deploy, "_tailscale_json", offline)
    monkeypatch.setattr(deploy, "_check_windows", lambda: None)
    monkeypatch.setattr(deploy, "preflight", lambda _: None)
    calls = []
    monkeypatch.setattr(
        deploy,
        "_task",
        lambda *args, **kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0),
    )
    monkeypatch.setattr(deploy, "_wait_for_stop", lambda **kwargs: None)
    monkeypatch.setattr(deploy, "_wait_for_health", lambda **kwargs: None)
    monkeypatch.setattr(
        deploy,
        "_configure_private_api",
        lambda **kwargs: (_ for _ in ()).throw(deploy.TailscaleUnavailable("offline")),
    )

    deploy.stop(argparse.Namespace())
    deploy.start(argparse.Namespace())
    assert ("/End", "/TN", deploy.TASK_NAME) in calls
    assert ("/Run", "/TN", deploy.TASK_NAME) in calls
    assert "Private Lite API is offline" in capsys.readouterr().out


def test_tailscale_msi_fallback_checks_signature_before_install(monkeypatch):
    import io
    import subprocess

    import pytest

    installed = {"value": False}
    calls = []
    urls = []
    monkeypatch.setattr(
        deploy,
        "_find_tailscale_executable",
        lambda: "tailscale.exe" if installed["value"] else None,
    )
    monkeypatch.setattr(deploy.shutil, "which", lambda _: None)
    monkeypatch.setattr(deploy.platform, "machine", lambda: "AMD64")

    def download(url, timeout):
        urls.append(url)
        return io.BytesIO(b"signed MSI fixture")

    monkeypatch.setattr(deploy.urllib.request, "urlopen", download)

    def invoke(command, **kwargs):
        calls.append(command)
        if command[0] == "powershell.exe":
            return subprocess.CompletedProcess(command, 1)
        if command[0] == "msiexec.exe":
            installed["value"] = True
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(deploy, "_run", invoke)
    with pytest.raises(deploy.DeploymentError, match="lacks a valid Tailscale signature"):
        deploy._tailscale_executable()
    assert urls == ["https://pkgs.tailscale.com/stable/tailscale-setup-latest-amd64.msi"]
    assert not any(command[0] == "msiexec.exe" for command in calls)

    calls.clear()

    def verified(command, **kwargs):
        calls.append(command)
        if command[0] == "msiexec.exe":
            installed["value"] = True
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(deploy, "_run", verified)
    assert deploy._tailscale_executable() == "tailscale.exe"
    assert [command[0] for command in calls] == ["powershell.exe", "msiexec.exe"]


def test_setup_repair_stops_existing_task_before_replacing_runtime(monkeypatch):
    import argparse
    import subprocess

    events = []
    monkeypatch.setattr(deploy, "_check_windows", lambda: None)
    monkeypatch.setattr(deploy, "_prepare_environment", lambda: None)
    monkeypatch.setattr(deploy, "preflight", lambda _: None)
    monkeypatch.setattr(
        deploy, "_task", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0)
    )
    monkeypatch.setattr(deploy, "stop", lambda _: events.append("stop"))
    monkeypatch.setattr(deploy, "_install_runtime", lambda: events.append("install"))
    monkeypatch.setattr(deploy, "_ensure_task", lambda: None)
    monkeypatch.setattr(deploy, "_wait_for_health", lambda: events.append("healthy"))
    monkeypatch.setattr(deploy, "_write_env", lambda _: None)
    monkeypatch.setattr(deploy, "_configure_private_api", lambda: None)
    if hasattr(deploy, "_configure_private_firewall"):
        monkeypatch.setattr(deploy, "_configure_private_firewall", lambda: None)
    deploy.setup(argparse.Namespace())
    assert events == ["stop", "install", "healthy"]


def test_application_reads_installer_password_escaping(tmp_path, monkeypatch):
    import os

    from app.config import _load_env_file

    key = "SETUORA_DEPLOYMENT_ESCAPE_TEST"
    for value in (
        'Password with "quotes" and \\ slash # spaces café',
        r"Literal\n\t\U0001f600 and escaped ending\\",
        r"Combined slash and quote: \"",
    ):
        monkeypatch.delenv(key, raising=False)
        fixture = tmp_path / "password.env"
        fixture.write_text(f"{key}={deploy._format_env_value(value)}\n", encoding="utf-8")
        _load_env_file(fixture)
        assert os.environ[key] == value
    monkeypatch.delenv(key, raising=False)


def test_fresh_application_start_login_backup_and_restart(tmp_path):
    import os
    import subprocess
    import sys

    password = 'Warehouse-2026 "secure" \\ pass'
    env_fixture = tmp_path / "bootstrap.env"
    env_fixture.write_text(
        f"BOOTSTRAP_ADMIN_PASSWORD={deploy._format_env_value(password)}\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "SETUORA_APP_MODE": "master",
            "SETUORA_ALLOW_LEGACY_TEST_MODE": "false",
            "APP_SECRET_KEY": "isolated-startup-secret-" + "x" * 48,
            "DATABASE_URL": f"sqlite:///{tmp_path / 'fresh.db'}",
            "BOOTSTRAP_ADMIN_USERNAME": "warehouse-admin",
            "BOOTSTRAP_ADMIN_PASSWORD": "",
            "SESSION_COOKIE_SECURE": "false",
            "TRUSTED_HOSTS": "127.0.0.1,localhost,testserver",
            "AUTOMATIC_BACKUPS_ENABLED": "false",
            "BACKUP_DIRECTORY": str(tmp_path / "backups"),
            "BACKUP_SETTINGS_FILE": str(tmp_path / "backup-settings.env"),
            "BACKUP_OFFSITE_DIRECTORY": "",
            "MASTER_SYNC_ENABLED": "false",
            "MASTER_CONNECTION_SETTINGS_FILE": str(tmp_path / "master-connection.env"),
            "SFTP_SYNC_ENABLED": "false",
            "SFTP_CONNECTION_SETTINGS_FILE": str(tmp_path / "sftp-connection.env"),
            "SMOKE_BOOTSTRAP_ENV": str(env_fixture),
            "SMOKE_BOOTSTRAP_PASSWORD": password,
        }
    )
    script = r"""
import os
import sqlite3
from pathlib import Path

from app.config import _load_env_file, get_settings

os.environ.pop("BOOTSTRAP_ADMIN_PASSWORD", None)
_load_env_file(Path(os.environ["SMOKE_BOOTSTRAP_ENV"]))
get_settings.cache_clear()

from fastapi.testclient import TestClient
from app.main import app
from app.services.backup import create_scheduled_backup, verify_sqlite_backup

for startup in range(2):
    if startup:
        # A repaired/restarted installation no longer retains its bootstrap password.
        os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = ""
        get_settings.cache_clear()
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200, health.text
        assert health.json() == {"status": "ok", "role": os.environ["SETUORA_APP_MODE"]}
        login_page = client.get("/login")
        assert login_page.status_code == 200, login_page.text
        login = client.post(
            "/login",
            data={
                "username": "warehouse-admin",
                "password": os.environ["SMOKE_BOOTSTRAP_PASSWORD"],
            },
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )
        assert login.status_code == 303, login.text
        assert "httponly" in login.headers["set-cookie"].lower()
        home = client.get("/")
        assert home.status_code == 200, home.text
        backup = create_scheduled_backup()
        verify_sqlite_backup(backup.path)
        with sqlite3.connect(backup.path) as saved:
            assert saved.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert saved.execute("PRAGMA foreign_key_check").fetchall() == []
            assert saved.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
print("fresh startup, login, verified backup, and restart passed")
"""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "fresh startup, login, verified backup, and restart passed" in result.stdout


def test_status_checks_running_application_without_reading_admin_secrets(monkeypatch, capsys):
    import argparse
    import io
    import json
    import subprocess

    monkeypatch.setattr(deploy, "_check_windows", lambda: None)
    monkeypatch.setattr(
        deploy, "_task", lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, stdout="")
    )

    def protected_env():
        raise PermissionError("A regular user cannot read .env")

    monkeypatch.setattr(deploy, "_read_env", protected_env)
    monkeypatch.setattr(
        deploy, "_check_private_api_status", lambda: "https://master.example.ts.net"
    )
    monkeypatch.setattr(
        deploy.urllib.request,
        "urlopen",
        lambda *args, **kwargs: io.BytesIO(json.dumps({"status": "ok", "role": "master"}).encode()),
    )
    deploy.status(argparse.Namespace())
    output = capsys.readouterr().out
    assert "database is responding" in output
    assert "https://master.example.ts.net/api/v1/" in output


def test_status_reports_stopped_server_instead_of_claiming_it_is_running(monkeypatch):
    import argparse
    import subprocess

    import pytest

    monkeypatch.setattr(deploy, "_check_windows", lambda: None)
    monkeypatch.setattr(
        deploy, "_task", lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, stdout="")
    )

    def unavailable(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(deploy.urllib.request, "urlopen", unavailable)
    with pytest.raises(deploy.DeploymentError, match="Choose Start"):
        deploy.status(argparse.Namespace())


def test_start_recommends_repair_when_background_task_is_missing(monkeypatch):
    import argparse
    import subprocess

    import pytest

    monkeypatch.setattr(deploy, "_check_windows", lambda: None)
    monkeypatch.setattr(deploy, "preflight", lambda _: None)
    calls = []

    def missing_task(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, stdout="")

    monkeypatch.setattr(deploy, "_task", missing_task)
    with pytest.raises(deploy.DeploymentError, match="Choose Setup / repair"):
        deploy.start(argparse.Namespace())
    assert len(calls) == 1
    assert calls[0][0] == "/Query"
