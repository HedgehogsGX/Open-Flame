"""Explicit authorized loopback client for positive Download API scenarios.

Security rejection scenarios should construct TestClient directly and supply
their intentionally invalid headers. This helper does not patch TestClient.
"""
from contextlib import contextmanager
from fastapi.testclient import TestClient


@contextmanager
def download_client(app, **kwargs):
    kwargs.setdefault("base_url", "http://127.0.0.1")
    with TestClient(app, **kwargs) as client:
        session = client.get("/api/v1/session")
        assert session.status_code == 200, session.text
        client.headers["X-Download-CSRF"] = session.json()["csrf_token"]
        yield client
