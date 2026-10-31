"""Tiny HTTP client."""

from widget.parser import parse_url

DEFAULT_TIMEOUT = 10


class Client:
    def __init__(self, retries: int = 3):
        self.retries = retries

    def fetch(self, url: str, timeout: int = DEFAULT_TIMEOUT) -> bytes:
        parts = parse_url(url)
        if parts["scheme"] not in ("http", "https"):
            raise ValueError("unsupported scheme")
        return b""
