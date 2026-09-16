"""Connection details shown once when an administrator enrolls a Lite server."""

import json
import re
from urllib.parse import urlsplit

MASTER_URL_SETTING = "lite_connection_master_url"
_DNS_NAME = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
_CODE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,19}$")
_PLACEHOLDERS = {
    "CHANGE-ME",
    "CHANGEME",
    "DEFAULT",
    "FRANCHISE",
    "FRANCHISE-CODE",
    "LITE",
    "MASTER",
    "SETUORA",
    "YOUR-FRANCHISE",
    "YOUR-FRANCHISE-CODE",
}


def normalize_master_url(value: str) -> str:
    value = value.strip()
    message = "Enter the Master HTTPS address, such as https://master.example.com, without a path or port."
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").rstrip(".").lower()
        if (
            parsed.scheme != "https"
            or not _DNS_NAME.fullmatch(host)
            or parsed.port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or any(char.isspace() for char in value)
        ):
            raise ValueError(message)
    except ValueError:
        raise ValueError(message) from None
    return f"https://{host}"


def validate_franchise_code(value: str) -> str:
    code = value.strip().upper()
    if not _CODE.fullmatch(code) or code in _PLACEHOLDERS:
        raise ValueError(
            "Choose a permanent franchise code of 1-20 letters, numbers, hyphens or "
            "underscores, such as BLR-01. Do not use a placeholder such as LITE."
        )
    return code


def connection_details(*, master_url: str, franchise_code: str, node_credential: str) -> str:
    return json.dumps(
        {
            "format": "setuora-lite-connection",
            "version": 1,
            "master_url": normalize_master_url(master_url),
            "franchise_code": franchise_code,
            "node_credential": node_credential,
        },
        indent=2,
    )
