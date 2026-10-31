"""URL parsing helpers."""

from urllib.parse import urlsplit


def parse_url(url: str) -> dict:
    """Split a URL into its parts. Raises ValueError on empty input."""
    if not url:
        raise ValueError("empty url")
    parts = urlsplit(url)
    if not parts.scheme:
        raise ValueError(f"missing scheme in {url!r}")
    return {"scheme": parts.scheme, "host": parts.hostname, "path": parts.path}


def normalize_host(host: str) -> str:
    return host.strip().lower()
