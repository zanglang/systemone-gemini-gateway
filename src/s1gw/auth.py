"""Optional inbound bearer-token authentication."""

from __future__ import annotations

import hmac


def extract_bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def is_authorized(header: str | None, keys: list[str]) -> bool:
    """True when auth is disabled (no keys) or the bearer token matches a key."""
    if not keys:
        return True
    token = extract_bearer(header)
    if token is None:
        return False
    # Compare against every key so timing does not reveal which one matched.
    matches = [hmac.compare_digest(token.encode(), key.encode()) for key in keys]
    return any(matches)
