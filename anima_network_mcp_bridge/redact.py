"""Never echo agent tokens in errors or logs."""

from __future__ import annotations

import re

# Product tokens look like anagt_<urlsafe>; also redact Bearer blobs.
_TOKEN_RE = re.compile(
    r"(?i)\b(anagt_[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._~+/=-]{8,})\b"
)


def redact_secrets(text: str, *, extra: str | None = None) -> str:
    """Strip known token shapes (and an explicit plaintext if provided)."""
    if not text:
        return text
    out = text
    if extra:
        out = out.replace(extra, "[REDACTED_TOKEN]")
    return _TOKEN_RE.sub("[REDACTED_TOKEN]", out)
