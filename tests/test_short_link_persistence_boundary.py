from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from video_download_control.api import create_app
from video_download_control.backup import (
    BACKUP_MANIFEST_NAME,
    DATABASE_PAYLOAD_PATH,
    create_backup,
)
from video_download_control.config import Settings
from video_download_control.database import Database
from video_download_control.domain import Platform, SourceType
from video_download_control.normalization import NormalizedURL
from video_download_control.short_links import ShortLinkResolution

USER_SUBMITTED_SHORT_URL = "https://t.co/persist-user-input?campaign=visible"
RESOLVER_INPUT_URL = "https://t.co/persist-user-input"
SAFE_CANONICAL_URL = "https://x.com/i/status/700099"
SIGNED_LOCATION_SENTINEL = "signed-location-sentinel-must-never-persist"
SIGNED_FINAL_LOCATION = (
    "https://x.com/user/status/700099"
    f"?signature={SIGNED_LOCATION_SENTINEL}&expires=9999999999"
)


class _ResolverReturningSignedLocation:
    def resolve(
        self, submitted_url: str, *, timeout_seconds: float
    ) -> ShortLinkResolution:
        assert submitted_url == RESOLVER_INPUT_URL
        assert 0 < timeout_seconds <= 15
        return ShortLinkResolution(
            normalized=NormalizedURL(
                submitted_url=SIGNED_FINAL_LOCATION,
                canonical_url=SAFE_CANONICAL_URL,
                platform=Platform.X,
                source_type=SourceType.X_POST,
                source_id="700099",
            ),
            platform=Platform.X,
            redirect_count=1,
            policy_hosts=("t.co", "x.com"),
        )


def _sqlite_dump(database_path: Path) -> str:
    with Database(database_path).connect() as connection:
        return "\n".join(connection.iterdump())


def _assert_tree_excludes_token(root: Path, token: bytes) -> None:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            assert token not in path.read_bytes(), (
                f"resolver-only signed Location leaked to {path.relative_to(root)}"
            )


def test_signed_final_location_never_crosses_api_database_or_backup_boundary(
    settings: Settings,
    tmp_path: Path,
) -> None:
    enabled_settings = replace(
        settings,
        short_link_resolution_enabled=True,
        short_link_transport_socket=settings.data_root / "short-link.sock",
        short_link_attestation_key_file=settings.data_root / "short-link.key",
    )
    app = create_app(
        enabled_settings,
        short_link_resolver=_ResolverReturningSignedLocation(),
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        created = client.post(
            "/api/v1/batches",
            json={"inputs": [USER_SUBMITTED_SHORT_URL]},
        )
        assert created.status_code == 201
        fetched = client.get(f"/api/v1/batches/{created.json()['id']}")
        assert fetched.status_code == 200

    for response in (created, fetched):
        assert SIGNED_LOCATION_SENTINEL not in response.text
        assert SIGNED_FINAL_LOCATION not in response.text
        assert response.json()["inputs"][0]["submitted_url"] == (
            USER_SUBMITTED_SHORT_URL
        )
        assert response.json()["inputs"][0]["canonical_url"] == SAFE_CANONICAL_URL

    live_database_dump = _sqlite_dump(settings.database_path)
    assert USER_SUBMITTED_SHORT_URL in live_database_dump
    assert SAFE_CANONICAL_URL in live_database_dump
    assert SIGNED_LOCATION_SENTINEL not in live_database_dump
    assert SIGNED_FINAL_LOCATION not in live_database_dump

    backup_root = tmp_path / "short-link-persistence-backup"
    create_backup(
        source_data_root=settings.data_root,
        source_database_path=settings.database_path,
        backup_target=backup_root,
    )

    manifest_text = (backup_root / BACKUP_MANIFEST_NAME).read_text("utf-8")
    assert SIGNED_LOCATION_SENTINEL not in manifest_text
    assert SIGNED_FINAL_LOCATION not in manifest_text

    payload_root = backup_root / "payload"
    sentinel_bytes = SIGNED_LOCATION_SENTINEL.encode()
    _assert_tree_excludes_token(payload_root, sentinel_bytes)

    backup_database = backup_root.joinpath(*DATABASE_PAYLOAD_PATH.split("/"))
    backup_database_dump = _sqlite_dump(backup_database)
    assert USER_SUBMITTED_SHORT_URL in backup_database_dump
    assert SAFE_CANONICAL_URL in backup_database_dump
    assert SIGNED_LOCATION_SENTINEL not in backup_database_dump
    assert SIGNED_FINAL_LOCATION not in backup_database_dump
