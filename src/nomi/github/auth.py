"""GitHub App authentication: a short-lived JWT exchanged for an installation token.

A GitHub App never holds a long-lived credential for a repository. It signs a
JWT with its private key (valid for at most ten minutes) and trades it for an
installation access token scoped to the repositories and permissions the
installer granted. The JWT is back-dated by 60 seconds and expires after nine
minutes so that clock skew between this machine and GitHub cannot make it
look issued-in-the-future or expired-too-late.
"""

from __future__ import annotations

import time

import httpx
import jwt

from nomi.github.client import DEFAULT_BASE_URL, GitHubClient, GitHubError

_CLOCK_SKEW_S = 60
_JWT_LIFETIME_S = 540


def make_app_jwt(app_id: str, private_key_pem: str, *, now: int | None = None) -> str:
    """Return an RS256 JWT identifying the GitHub App ``app_id``."""
    app_id = str(app_id).strip()
    if not app_id:
        raise ValueError("GitHub App id is empty; set NOMI_APP_ID")
    issued = int(time.time()) if now is None else now
    payload = {"iat": issued - _CLOCK_SKEW_S, "exp": issued + _JWT_LIFETIME_S, "iss": app_id}
    try:
        return jwt.encode(payload, private_key_pem, algorithm="RS256")
    except (ValueError, TypeError, jwt.PyJWTError) as exc:
        raise ValueError(
            "invalid GitHub App private key: expected the RSA PEM file downloaded from the "
            "App settings page (NOMI_APP_PRIVATE_KEY_PATH)"
        ) from exc


def get_installation_token(
    app_id: str,
    private_key_pem: str,
    installation_id: int,
    *,
    base_url: str = DEFAULT_BASE_URL,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Exchange an App JWT for an installation access token (valid for one hour)."""
    if isinstance(installation_id, bool) or not isinstance(installation_id, int):
        raise ValueError(f"installation id must be an integer, got {installation_id!r}")
    if installation_id < 1:
        raise ValueError(f"installation id must be positive, got {installation_id}")
    app_jwt = make_app_jwt(app_id, private_key_pem)
    with GitHubClient(app_jwt, base_url=base_url, transport=transport) as client:
        data = client.request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            what=f"GitHub App installation {installation_id}",
        )
    token = data.get("token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        raise GitHubError(f"GitHub returned no access token for installation {installation_id}")
    return token
