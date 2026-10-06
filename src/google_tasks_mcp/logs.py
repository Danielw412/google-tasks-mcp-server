"""Logging setup with a last-line-of-defence redaction pass.

The code never logs credentials on purpose; this formatter additionally scrubs
anything shaped like a Google token, client secret, OpenAI key, or an OAuth
query parameter from every formatted line (including tracebacks).
"""

from __future__ import annotations

import logging
import re

_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"ya29\.[0-9A-Za-z_\-.]+"), "ya29.<redacted>"),
    (re.compile(r"1//[0-9A-Za-z_\-]{10,}"), "1//<redacted>"),
    (re.compile(r"GOCSPX-[0-9A-Za-z_\-]+"), "GOCSPX-<redacted>"),
    (re.compile(r"\bsk-[0-9A-Za-z_\-]{16,}"), "sk-<redacted>"),
    (
        re.compile(
            r"(?i)\b(code|state|code_verifier|refresh_token|access_token|id_token|client_secret|token)"
            r"(=|\"\s*:\s*\"|'\s*:\s*')([^&\s\"',}]+)"
        ),
        r"\1\2<redacted>",
    ),
    (re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"), r"\1<redacted>"),
]


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Request lines can carry OAuth query strings; the HTTP client loggers add nothing we need.
    for noisy in ("httpx2", "httpcore2", "httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
