"""Fixed package-local UI assets shared by the download and upload pages."""
from __future__ import annotations

import base64
import hashlib
from functools import lru_cache
from importlib.resources import files


_ASSETS = {
    "open-flame.css": "text/css; charset=utf-8",
    "open-flame-shell.js": "text/javascript; charset=utf-8",
}


@lru_cache(maxsize=len(_ASSETS))
def ui_asset(name: str) -> tuple[bytes, str]:
    """Return one allowlisted package asset and its exact media type."""
    try:
        media_type = _ASSETS[name]
    except KeyError as exc:
        raise ValueError("unknown_ui_asset") from exc
    payload = files("video_download_control").joinpath("static", name).read_bytes()
    if not payload:
        raise RuntimeError("empty_ui_asset")
    return payload, media_type


def page_content_security_policy(html: str) -> str:
    """Bind the one package-owned inline business script to a self-only page."""
    marker = "<script>"
    if html.count(marker) != 1 or html.count("</script>") != 2:
        raise ValueError("unexpected_page_script_layout")
    script = html.split(marker, 1)[1].split("</script>", 1)[0].encode("utf-8")
    digest = base64.b64encode(hashlib.sha256(script).digest()).decode("ascii")
    return "; ".join(
        (
            "default-src 'none'",
            "base-uri 'none'",
            "connect-src 'self'",
            "form-action 'self'",
            "frame-ancestors 'none'",
            "img-src 'self' blob:",
            f"script-src 'self' 'sha256-{digest}'",
            "style-src 'self'",
        )
    )
