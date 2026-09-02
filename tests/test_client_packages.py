import base64
import hashlib
import io
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_package_builder_creates_safe_complete_windows_installer(tmp_path):
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

    windows_path = tmp_path / f"Setuora-Master-{version}-windows.cmd"
    checksum_path = tmp_path / f"Setuora-Master-{version}-SHA256SUMS.txt"
    assert str(windows_path) in result.stdout
    assert windows_path.is_file()
    assert checksum_path.is_file()
    assert not (tmp_path / f"Setuora-Master-{version}-linux.run").exists()

    windows_root = "Setuora-Master-windows"
    windows_header, encoded_payload = windows_path.read_bytes().split(
        b"\n__SETUORA_PAYLOAD_BELOW__\n", 1
    )
    assert windows_header.startswith(b"@echo off")
    windows_payload = base64.b64decode(encoded_payload)
    with zipfile.ZipFile(io.BytesIO(windows_payload)) as archive:
        members = set(archive.namelist())
    assert f"{windows_root}/setuora.ps1" in members
    assert f"{windows_root}/app/main.py" in members
    assert f"{windows_root}/scripts/windows/configure-sftp.ps1" in members
    assert f"{windows_root}/scripts/windows/run-server.cmd" in members
    assert f"{windows_root}/compose.yaml" not in members
    assert f"{windows_root}/Dockerfile" not in members

    for member_name in members:
        parts = set(PurePosixPath(member_name).parts)
        assert ".env" not in parts
        assert ".git" not in parts
        assert "data" not in parts
        assert "__pycache__" not in parts
        assert "archive" not in parts

    checksum, filename = checksum_path.read_text(encoding="utf-8").strip().split("  ", 1)
    assert filename == windows_path.name
    assert checksum == hashlib.sha256(windows_path.read_bytes()).hexdigest()


def test_windows_installer_and_launcher_use_native_windows_services():
    header = (PROJECT_ROOT / "client" / "windows" / "self-extract-header.cmd").read_text(
        encoding="utf-8"
    )
    launcher = (PROJECT_ROOT / "client" / "windows" / "setuora.ps1").read_text(encoding="utf-8")

    assert "$env:ProgramData" in header
    assert "-Verb RunAs" in header
    assert "$launcher preflight" in header
    assert "$launcher update" in header
    assert "$launcher setup" in header
    assert '"sftp-install"' in launcher
    assert '"sftp-add"' in launcher
    assert "configure-sftp.ps1" in launcher
    assert "docker" not in launcher.lower()
