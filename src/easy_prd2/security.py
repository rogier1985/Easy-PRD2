from __future__ import annotations

import re
import secrets
from urllib.parse import urlparse

TOKEN_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,'\"]+"),
    re.compile(r"(?i)(\btoken\b\s*[:=]\s*)[^\s,'\"]+"),
)


def normalize_api_base(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ValueError("API URL is required")
    if any(character.isspace() for character in raw):
        raise ValueError("Enter a valid Rossum URL")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Enter a valid Rossum URL")
    path = parsed.path.rstrip("/")
    if not path.endswith("/api/v1"):
        if path.endswith("/api"):
            path += "/v1"
        else:
            path += "/api/v1"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}{path}"


def redact(value: object, secrets_to_remove: tuple[str, ...] = ()) -> str:
    text = str(value)
    for secret in secrets_to_remove:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    for pattern in TOKEN_PATTERNS:
        text = pattern.sub(r"\1[REDACTED]", text)
    return text


def new_secret() -> str:
    return secrets.token_urlsafe(32)
