from pathlib import Path

import deploy

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_active_deployment_is_windows_native_and_tailscale_free():
    assert not (PROJECT_ROOT / "compose.yaml").exists()
    assert not (PROJECT_ROOT / "Dockerfile").exists()
    assert not (PROJECT_ROOT / "deployment" / "tailscale").exists()

    deployment = (PROJECT_ROOT / "deploy.py").read_text(encoding="utf-8").lower()
    sftp = (PROJECT_ROOT / "scripts" / "windows" / "configure-sftp.ps1").read_text(encoding="utf-8")
    assert "schtasks.exe" in deployment
    assert "docker" not in deployment
    assert "tailscale" not in deployment
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


def test_production_preflight_requires_sftp_and_loopback_console():
    issues = deploy._environment_issues(
        {
            "APP_SECRET_KEY": "x" * 48,
            "BOOTSTRAP_ADMIN_PASSWORD": "strong-admin-password",
            "SESSION_COOKIE_SECURE": "false",
            "SETUORA_APP_MODE": "master",
            "TRUSTED_HOSTS": "127.0.0.1,localhost",
            "SETUORA_WEB_PORT": "8000",
            "SFTP_SYNC_ENABLED": "true",
            "SFTP_EXCHANGE_ROOT": "C:/ProgramData/Setuora/sftp",
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
            "SFTP_SYNC_ENABLED": "false",
            "AUTOMATIC_BACKUPS_ENABLED": "true",
            "BACKUP_RETENTION_COUNT": "14",
        },
        has_application_data=False,
    )
    assert "SESSION_COOKIE_SECURE must be false for the loopback-only HTTP console." in issues
    assert "TRUSTED_HOSTS must include localhost and 127.0.0.1." in issues
    assert "SFTP_EXCHANGE_ROOT must be configured." in issues
    assert "SFTP_SYNC_ENABLED must be true for the Windows server deployment." in issues


def test_source_checkout_batch_controller_owns_waited_elevation():
    controller = (PROJECT_ROOT / "setuora.bat").read_text(encoding="utf-8")
    setup = (PROJECT_ROOT / "scripts" / "setup.bat").read_text(encoding="utf-8")
    update = (PROJECT_ROOT / "scripts" / "update.bat").read_text(encoding="utf-8")

    assert 'set "ROOT_DIR=%~dp0"' in controller
    assert all(
        f'if /I "%~1"=="{command}"' in controller
        for command in ("setup", "start", "stop", "update", "help")
    )
    assert "Setup / repair" in controller
    assert "Invalid choice" in controller
    assert "completed successfully" in controller
    assert "-Verb RunAs -Wait -PassThru" in controller
    assert "$process.ExitCode" in controller
    assert "ELEVATED_REENTRY" in controller
    assert "if defined CLI_MODE endlocal & exit /b" not in controller
    assert "if defined CLI_MODE goto cli_exit" in controller
    assert ":cli_exit\nendlocal & exit /b %EXIT_CODE%" in controller
    assert "pause\ngoto menu\n\n:cli_exit" in controller

    assert "Start-Process" not in setup
    assert "requires Administrator privileges" in setup
    assert "Python.Python.3.11" in setup
    assert "%ProgramFiles%\\Python%%V\\python.exe" in setup
    assert "%LOCALAPPDATA%\\Programs\\Python\\Python%%V\\python.exe" in setup
    assert '"%DEPLOY_SCRIPT%" setup' in setup

    assert "status --porcelain --untracked-files=all" in update
    assert "merge-base --is-ancestor" in update
    assert "merge --ff-only" in update
