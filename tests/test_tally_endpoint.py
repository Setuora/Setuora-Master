import pytest

from app.services.tally_endpoint import build_tally_url, read_tally_response


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", "http://127.0.0.1:9000"),
        ("host.docker.internal", "http://host.docker.internal:9000"),
        ("[fd7a:115c:a1e0::1]", "http://[fd7a:115c:a1e0::1]:9000"),
    ],
)
def test_build_tally_url_accepts_supported_hosts(host, expected):
    assert build_tally_url({"tally_host": host, "tally_port": "9000"}) == expected


@pytest.mark.parametrize(
    ("host", "port"),
    [
        ("http://example.com", "9000"),
        ("example.com/path", "9000"),
        ("example.com", "0"),
        ("example.com", "65536"),
        ("example.com", "not-a-port"),
    ],
)
def test_build_tally_url_rejects_url_injection_and_invalid_ports(host, port):
    with pytest.raises(ValueError):
        build_tally_url({"tally_host": host, "tally_port": port})


def test_read_tally_response_rejects_oversized_body():
    class Response:
        def read(self, size):
            return b"x" * size

    with pytest.raises(ValueError, match="exceeds"):
        read_tally_response(Response(), maximum_bytes=8)
