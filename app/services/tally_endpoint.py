from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlunsplit

_HOSTNAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?$")
MAX_TALLY_RESPONSE_BYTES = 20 * 1024 * 1024


def build_tally_url(settings: dict[str, str]) -> str:
    """Build a fixed-scheme Tally URL while rejecting URL syntax in host settings."""

    host = settings.get("tally_host", "").strip()
    port_text = settings.get("tally_port", "").strip()
    if not host or not port_text:
        raise ValueError("Tally host and port are not configured.")

    unwrapped_host = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        address = ipaddress.ip_address(unwrapped_host)
    except ValueError:
        if not _HOSTNAME.fullmatch(unwrapped_host):
            raise ValueError("Tally host must be an IP address or hostname.") from None
        network_location = unwrapped_host
    else:
        network_location = f"[{address.compressed}]" if address.version == 6 else address.compressed

    try:
        port = int(port_text)
    except ValueError:
        raise ValueError("Tally port must be a number from 1 to 65535.") from None
    if not 1 <= port <= 65535:
        raise ValueError("Tally port must be a number from 1 to 65535.")

    return urlunsplit(("http", f"{network_location}:{port}", "", "", ""))


def read_tally_response(response, *, maximum_bytes: int = MAX_TALLY_RESPONSE_BYTES) -> str:
    body = response.read(maximum_bytes + 1)
    if len(body) > maximum_bytes:
        raise ValueError(f"Tally response exceeds the {maximum_bytes}-byte limit.")
    return body.decode("utf-8", errors="replace")
