import base64
import hashlib
import io
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_client_package_builder_creates_safe_complete_platform_archives(tmp_path):
    version = "test-1.2.3"
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "build_client_packages.py"),
            "--version",
            version,
            "--output",
            str(tmp_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    linux_path = tmp_path / f"Setuora-Master-{version}-linux.run"
    windows_path = tmp_path / f"Setuora-Master-{version}-windows.cmd"
    checksum_path = tmp_path / f"Setuora-Master-{version}-SHA256SUMS.txt"
    assert str(linux_path) in result.stdout
    assert linux_path.is_file()
    assert windows_path.is_file()
    assert checksum_path.is_file()
    assert os.access(linux_path, os.X_OK)

    linux_root = "Setuora-Master-linux"
    linux_header, linux_payload = linux_path.read_bytes().split(b"\n__SETUORA_PAYLOAD_BELOW__\n", 1)
    assert linux_header.startswith(b"#!/usr/bin/env bash")
    with tarfile.open(fileobj=io.BytesIO(linux_payload), mode="r:gz") as archive:
        linux_members = {member.name: member for member in archive.getmembers()}
    assert f"{linux_root}/setuora" in linux_members
    assert f"{linux_root}/app/main.py" in linux_members
    assert f"{linux_root}/compose.yaml" in linux_members
    assert linux_members[f"{linux_root}/setuora"].mode == 0o755

    windows_root = "Setuora-Master-windows"
    windows_header, encoded_payload = windows_path.read_bytes().split(
        b"\n__SETUORA_PAYLOAD_BELOW__\n", 1
    )
    assert windows_header.startswith(b"@echo off")
    windows_payload = base64.b64decode(encoded_payload)
    with zipfile.ZipFile(io.BytesIO(windows_payload)) as archive:
        windows_members = set(archive.namelist())
    assert f"{windows_root}/setuora.ps1" in windows_members
    assert f"{windows_root}/app/main.py" in windows_members
    assert f"{windows_root}/compose.yaml" in windows_members

    for member_name in set(linux_members) | windows_members:
        parts = set(PurePosixPath(member_name).parts)
        assert ".env" not in parts
        assert ".git" not in parts
        assert "data" not in parts
        assert "__pycache__" not in parts

    expected_checksums = {
        linux_path.name: hashlib.sha256(linux_path.read_bytes()).hexdigest(),
        windows_path.name: hashlib.sha256(windows_path.read_bytes()).hexdigest(),
    }
    checksum_lines = checksum_path.read_text(encoding="utf-8").splitlines()
    actual_checksums = {
        file_name: checksum
        for checksum, file_name in (line.split("  ", 1) for line in checksum_lines)
    }
    assert actual_checksums == expected_checksums


def test_single_file_installers_detect_setup_or_update():
    linux = (PROJECT_ROOT / "client" / "linux" / "self-extract-header.sh").read_text(
        encoding="utf-8"
    )
    windows = (PROJECT_ROOT / "client" / "windows" / "self-extract-header.cmd").read_text(
        encoding="utf-8"
    )

    assert 'ACTION="update"' in linux
    assert 'ACTION="setup"' in linux
    assert '"$INSTALL_DIR/setuora" preflight' in linux
    assert '"$INSTALL_DIR/setuora" update' in linux
    assert '"$INSTALL_DIR/setuora" setup' in linux
    assert "$isUpdate = Test-Path" in windows
    assert "$launcher preflight" in windows
    assert "$launcher update" in windows
    assert "$launcher setup" in windows


def test_linux_single_file_runs_setup_then_update_without_real_docker(tmp_path):
    version = "integration-test"
    output_directory = tmp_path / "output"
    subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "build_client_packages.py"),
            "--version",
            version,
            "--output",
            str(output_directory),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_docker.chmod(0o755)
    fake_python = fake_bin / "python3.11"
    fake_python.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$SETUORA_TEST_LOG"\nexit 0\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    log_path = tmp_path / "commands.log"
    data_home = tmp_path / "data-home"
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "SETUORA_TEST_LOG": str(log_path),
        "XDG_DATA_HOME": str(data_home),
    }
    installer = output_directory / f"Setuora-Master-{version}-linux.run"

    subprocess.run(  # noqa: S603
        [str(installer)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    installation = data_home / "setuora" / "Setuora-Master-linux"
    assert (installation / "app" / "main.py").is_file()
    assert str(installation / "deploy.py") + " setup" in log_path.read_text(encoding="utf-8")

    (installation / ".env").write_text("SETUORA_APP_MODE=master\n", encoding="utf-8")
    subprocess.run(  # noqa: S603
        [str(installer)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    commands = log_path.read_text(encoding="utf-8")
    assert str(installation / "deploy.py") + " stop" in commands
    assert str(installation / "deploy.py") + " preflight" in commands
    assert str(installation / "deploy.py") + " update" in commands


def test_root_shortcuts_launch_the_newest_platform_installer():
    linux = (PROJECT_ROOT / "Linux — Setuora Master.run").read_text(encoding="utf-8")
    windows = (PROJECT_ROOT / "Windows — Setuora Master.cmd").read_text(encoding="utf-8")

    assert "dist/Setuora-Master-*-linux.run" in linux
    assert '-nt "$LATEST_INSTALLER"' in linux
    assert 'exec "$LATEST_INSTALLER"' in linux
    assert "Setuora-Master-*-windows.cmd" in windows
    assert "/o:-d" in windows
    assert 'call "%SETUORA_INSTALLER%"' in windows
