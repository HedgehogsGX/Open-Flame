"""Central redaction boundary for diagnostics persisted or exposed by the app."""

from __future__ import annotations

import re


MAX_DIAGNOSTIC_LENGTH = 4096
REDACTED = "[REDACTED]"

_SENSITIVE_FIELD_NAMES = (
    "proxy-authorization",
    "authorization",
    "set-cookie",
    "cookie",
    "access_token",
    "refresh_token",
    "auth_token",
    "id_token",
    "x-amz-security-token",
    "x-amz-signature",
    "x-amz-credential",
    "x-goog-signature",
    "x-goog-credential",
    "api_key",
    "apikey",
    "signature",
    "credential",
    "session_id",
    "sessionid",
    "password",
    "passwd",
    "secret",
    "token",
    "sig",
)
_FIELD_PATTERN = "(?:" + "|".join(
    re.escape(name) for name in _SENSITIVE_FIELD_NAMES
) + ")"

_HEADER_RE = re.compile(
    rf"(?im)(?P<prefix>^[ \t]*{_FIELD_PATTERN}[ \t]*:[ \t]*)[^\r\n]*"
)
_QUOTED_ASSIGNMENT_RE = re.compile(
    rf"(?P<prefix>(?<![\w-])(?P<keyquote>['\"]?){_FIELD_PATTERN}"
    rf"(?P=keyquote)(?![\w-])\s*[:=]\s*)"
    rf"(?P<quote>['\"])(?P<value>(?:\\.|(?!(?P=quote)).)*)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)
_AUTH_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>(?<![\w-])(?:proxy-)?authorization(?![\w-])\s*[:=]\s*)"
    r"(?:(?:basic|bearer|digest)\s+)?[^\s,;}\]]+",
    re.IGNORECASE,
)
_UNQUOTED_ASSIGNMENT_RE = re.compile(
    rf"(?P<prefix>(?<![\w-]){_FIELD_PATTERN}(?![\w-])\s*[:=]\s*)"
    r"[^\s,;&}]+",
    re.IGNORECASE,
)
_URL_USERINFO_RE = re.compile(
    r"(?P<scheme>https?://)[^/@\s:]+:[^/@\s]+@", re.IGNORECASE
)


def sanitize_diagnostic(
    diagnostic: str, *, max_length: int = MAX_DIAGNOSTIC_LENGTH
) -> str:
    """Redact common credentials, then bound the value for safe persistence.

    Redaction intentionally happens before truncation so a sensitive value that
    crosses the storage boundary cannot be partially retained.
    """

    if max_length < 0:
        raise ValueError("max_length must be non-negative")

    value = str(diagnostic)
    value = _URL_USERINFO_RE.sub(rf"\g<scheme>{REDACTED}@", value)
    value = _HEADER_RE.sub(rf"\g<prefix>{REDACTED}", value)

    def redact_quoted(match: re.Match[str]) -> str:
        quote = match.group("quote")
        return f"{match.group('prefix')}{quote}{REDACTED}{quote}"

    value = _QUOTED_ASSIGNMENT_RE.sub(redact_quoted, value)
    value = _AUTH_ASSIGNMENT_RE.sub(rf"\g<prefix>{REDACTED}", value)
    value = _UNQUOTED_ASSIGNMENT_RE.sub(
        rf"\g<prefix>{REDACTED}", value
    )
    return value[:max_length]
