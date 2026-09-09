"""Shared loopback request and CSRF boundary for local HTTP surfaces."""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def _origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        ):
            return None
        return (
            parsed.scheme,
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
        )
    except ValueError:
        return None


def install_local_http_guard(
    app: FastAPI,
    *,
    protects_path: Callable[[str], bool],
    csrf_header: str,
    forbidden_detail: str,
    requires_csrf: Callable[[str, str], bool],
    preserve_existing_no_store: bool = False,
) -> str:
    """Install one local surface boundary and return its process-local token."""

    nonce = secrets.token_urlsafe(32)

    @app.middleware("http")
    async def local_http_boundary(request: Request, call_next):
        path = request.url.path
        if not protects_path(path):
            return await call_next(request)
        hosts = request.headers.getlist("host")
        expected = (
            _origin(request.url.scheme + "://" + hosts[0]) if len(hosts) == 1 else None
        )
        origins = request.headers.getlist("origin")
        fetch_sites = request.headers.getlist("sec-fetch-site")
        safe = (
            expected is not None
            and len(origins) <= 1
            and len(fetch_sites) <= 1
            and (not origins or _origin(origins[0]) == expected)
            and (not fetch_sites or fetch_sites[0] in {"same-origin", "none"})
        )
        if requires_csrf(request.method, path):
            tokens = request.headers.getlist(csrf_header)
            safe = (
                safe
                and len(tokens) == 1
                and tokens[0].isascii()
                and hmac.compare_digest(tokens[0], nonce)
            )
        if not safe:
            return JSONResponse({"detail": forbidden_detail}, status_code=403)
        response = await call_next(request)
        cache_control = response.headers.get("cache-control", "")
        has_no_store = any(
            directive.strip().lower() == "no-store"
            for directive in cache_control.split(",")
        )
        if not preserve_existing_no_store or not has_no_store:
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    return nonce
