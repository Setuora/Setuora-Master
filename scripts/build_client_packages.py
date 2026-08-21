"""Build the shareable Windows Setuora Master installer."""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import stat
import tempfile
import textwrap
import zipfile
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "dist"
RELEASE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

COMMON_FILES = (
    ".env.example",
    "deploy.py",
    "requirements-runtime.lock",
    "client/CLIENT-README.md",
)
COMMON_DIRECTORIES = (
    "app",
    "docs/deployment",
    "scripts/windows",
)
WINDOWS_FILES = {
    "client/windows/setuora.ps1": "setuora.ps1",
}
WINDOWS_HEADER = PROJECT_ROOT / "client/windows/self-extract-header.cmd"


def _common_payload() -> dict[str, Path]:
    payload = {name: PROJECT_ROOT / name for name in COMMON_FILES}
    for directory_name in COMMON_DIRECTORIES:
        directory = PROJECT_ROOT / directory_name
        for source in sorted(directory.rglob("*")):
            if source.is_file() and "__pycache__" not in source.parts:
                payload[source.relative_to(PROJECT_ROOT).as_posix()] = source
    return payload


def _payload(platform_files: dict[str, str]) -> dict[str, Path]:
    payload = _common_payload()
    payload["CLIENT-README.md"] = payload.pop("client/CLIENT-README.md")
    for source_name, archive_name in platform_files.items():
        payload[archive_name] = PROJECT_ROOT / source_name
    return payload


def _release_text(version: str, platform: str) -> bytes:
    return (
        f"Setuora Master\nVersion: {version}\nPlatform: {platform}\n"
        "Persistent data is stored under C:\\ProgramData\\Setuora.\n"
    ).encode()


def _validate_payload(payload: dict[str, Path]) -> None:
    forbidden = {".env", ".git", "data", "__pycache__"}
    for archive_name, source in payload.items():
        parts = set(PurePosixPath(archive_name).parts)
        if parts & forbidden:
            raise ValueError(f"Refusing to package private or generated path: {archive_name}")
        if not source.is_file():
            raise FileNotFoundError(source)


def _write_zip(path: Path, root_name: str, payload: dict[str, Path], version: str) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for archive_name, source in sorted(payload.items()):
            info = zipfile.ZipInfo(f"{root_name}/{archive_name}", (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, source.read_bytes())

        info = zipfile.ZipInfo(f"{root_name}/RELEASE.txt", (1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = (stat.S_IFREG | 0o644) << 16
        archive.writestr(info, _release_text(version, "Windows"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_package(version: str, output_directory: Path = DEFAULT_OUTPUT) -> Path:
    if not RELEASE_NAME.fullmatch(version):
        raise ValueError(
            "Version may contain only letters, numbers, dots, underscores, and hyphens."
        )

    output_directory.mkdir(parents=True, exist_ok=True)
    windows_payload = _payload(WINDOWS_FILES)
    _validate_payload(windows_payload)

    windows_root = "Setuora-Master-windows"
    windows_path = output_directory / f"Setuora-Master-{version}-windows.cmd"

    with tempfile.TemporaryDirectory(prefix="setuora-package-") as temporary_directory:
        temporary_path = Path(temporary_directory)
        windows_payload_path = temporary_path / "payload.zip"
        _write_zip(windows_payload_path, windows_root, windows_payload, version)

        encoded_payload = base64.b64encode(windows_payload_path.read_bytes()).decode("ascii")
        wrapped_payload = "\n".join(textwrap.wrap(encoded_payload, width=76)) + "\n"
        windows_path.write_bytes(WINDOWS_HEADER.read_bytes() + wrapped_payload.encode("ascii"))

    checksum_path = output_directory / f"Setuora-Master-{version}-SHA256SUMS.txt"
    checksum_path.write_text(
        f"{_sha256(windows_path)}  {windows_path.name}\n",
        encoding="utf-8",
    )
    return windows_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        default=os.environ.get("SETUORA_RELEASE_VERSION", "pilot"),
        help="release label used in package names (default: pilot)",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    windows_path = build_package(args.version, args.output.resolve())
    print(windows_path)
    print(windows_path.parent / f"Setuora-Master-{args.version}-SHA256SUMS.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
