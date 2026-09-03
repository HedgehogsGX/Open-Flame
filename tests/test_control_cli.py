from __future__ import annotations

from pathlib import Path

import pytest

import video_download_control.cli as cli_module
from video_download_control.config import Settings


def test_control_cli_disables_unstructured_uvicorn_access_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = (tmp_path / "data").resolve()
    settings = Settings(
        data_root=data_root,
        database_path=data_root / "control.sqlite3",
        host="127.0.0.1",
        port=8765,
    )
    captured: dict[str, object] = {}

    def fake_run(app, **kwargs: object) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(
        cli_module.Settings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(cli_module.uvicorn, "run", fake_run)

    cli_module.main()

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8765
    assert captured["reload"] is False
    assert captured["access_log"] is False
    assert captured["app"].state.runtime_logger.component == "control"
