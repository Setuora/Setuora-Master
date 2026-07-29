import json
from pathlib import Path

import deploy

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_compose_is_cross_platform_and_uses_tailscale_without_caddy():
    compose = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "tailscale/tailscale:v1.98.9@sha256:" in compose
    assert 'TS_USERSPACE: "true"' in compose
    assert 'TS_ENABLE_HEALTH_CHECK: "true"' in compose
    assert "TS_SERVE_CONFIG:" in compose
    assert "network_mode: service:tailscale" in compose
    assert '"127.0.0.1:${SETUORA_LOCAL_PORT:-8000}:8000"' in compose
    assert "host.docker.internal:host-gateway" in compose
    assert "caddy" not in compose.lower()
    assert "windows" not in compose.lower()


def test_compose_persists_application_and_tailscale_state():
    compose = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "setuora-data:/srv/setuora/data" in compose
    assert "tailscale-state:/var/lib/tailscale" in compose
    assert 'TS_AUTH_ONCE: "true"' in compose
    assert "DATABASE_URL: sqlite:////srv/setuora/data/setuora.db" in compose
    assert "env_file:" not in compose
    assert compose.count("TAILSCALE_AUTH_KEY") == 1
    assert "condition: service_healthy" in compose


def test_tailscale_serve_terminates_https_and_proxies_only_to_private_app():
    config = json.loads(
        (PROJECT_ROOT / "deployment" / "tailscale" / "serve.json").read_text(encoding="utf-8")
    )

    assert config["TCP"]["443"] == {"HTTPS": True}
    handler = config["Web"]["${TS_CERT_DOMAIN}:443"]["Handlers"]["/"]
    assert handler == {"Proxy": "http://127.0.0.1:8000"}
    assert "AllowFunnel" not in config


def test_docker_build_does_not_copy_secrets_or_runtime_data():
    ignored = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert ".env" in ignored
    assert "data" in ignored
    assert ".git" in ignored


def test_deployment_helper_rewrites_settings_without_exposing_secrets(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "APP_SECRET_KEY=old\n"
        "TRUSTED_HOSTS=localhost\n"
        "# Preserve this comment\n"
        "TAILSCALE_AUTH_KEY=secret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(deploy, "ENV_PATH", env_path)

    deploy._write_env(
        {
            "APP_SECRET_KEY": "new-secret",
            "TRUSTED_HOSTS": "*.ts.net,localhost",
            "SESSION_COOKIE_SECURE": "true",
        }
    )

    result = env_path.read_text(encoding="utf-8")
    assert "APP_SECRET_KEY=new-secret" in result
    assert "TRUSTED_HOSTS=*.ts.net,localhost" in result
    assert "SESSION_COOKIE_SECURE=true" in result
    assert "# Preserve this comment" in result
    assert "TAILSCALE_AUTH_KEY=secret" in result


def test_deployment_helper_quotes_password_characters_for_compose(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(deploy, "ENV_PATH", env_path)

    password = 'spaces # quotes " and slash \\ stay intact'
    deploy._write_env({"BOOTSTRAP_ADMIN_PASSWORD": password})

    raw = env_path.read_text(encoding="utf-8")
    assert raw.startswith('BOOTSTRAP_ADMIN_PASSWORD="')
    assert deploy._read_env()[1]["BOOTSTRAP_ADMIN_PASSWORD"] == password


def test_deployment_pins_magic_dns_and_removes_bootstrap_key(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "TRUSTED_HOSTS=*.ts.net,localhost\nTAILSCALE_AUTH_KEY=tskey-auth-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(deploy, "ENV_PATH", env_path)
    monkeypatch.setattr(deploy, "_compose", lambda *args, **kwargs: None)
    monkeypatch.setattr(deploy, "_wait_for_health", lambda: None)

    deploy._pin_tailnet_hostname("setuora-master.example.ts.net")

    values = deploy._read_env()[1]
    assert values["BOOTSTRAP_ADMIN_PASSWORD"] == ""
    assert values["TAILSCALE_AUTH_KEY"] == ""
    assert values["TRUSTED_HOSTS"] == "127.0.0.1,localhost,setuora-master.example.ts.net"


def test_production_preflight_reports_missing_tailnet_enrollment():
    issues = deploy._environment_issues(
        {
            "APP_SECRET_KEY": "x" * 48,
            "BOOTSTRAP_ADMIN_PASSWORD": "strong-admin-password",
            "SESSION_COOKIE_SECURE": "true",
            "SETUORA_APP_MODE": "master",
            "TAILSCALE_TAG": "tag:setuora-master",
            "TRUSTED_HOSTS": "*.ts.net,127.0.0.1,localhost",
            "AUTOMATIC_BACKUPS_ENABLED": "true",
            "BACKUP_RETENTION_COUNT": "14",
        },
        has_application_data=False,
        has_tailnet_identity=False,
    )

    assert issues == ["Provide TAILSCALE_AUTH_KEY for first enrollment."]


def test_production_preflight_accepts_persisted_tailnet_identity():
    issues = deploy._environment_issues(
        {
            "APP_SECRET_KEY": "x" * 48,
            "BOOTSTRAP_ADMIN_PASSWORD": "strong-admin-password",
            "SESSION_COOKIE_SECURE": "true",
            "SETUORA_APP_MODE": "master",
            "TAILSCALE_TAG": "tag:setuora-master",
            "TRUSTED_HOSTS": "setuora-master.example.ts.net,127.0.0.1,localhost",
            "AUTOMATIC_BACKUPS_ENABLED": "true",
            "BACKUP_RETENTION_COUNT": "14",
        },
        has_application_data=True,
        has_tailnet_identity=True,
    )

    assert issues == []
