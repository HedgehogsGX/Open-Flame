from __future__ import annotations

from pathlib import Path

import pytest

from video_download_control.api import create_app
from video_download_control.config import Settings
from video_download_control.runtime_logging import (
    DEFAULT_RUNTIME_LOG_BACKUP_COUNT,
    DEFAULT_RUNTIME_LOG_MAX_BYTES,
    RuntimeLogConfigurationError,
)


def _settings(tmp_path: Path, *, host: str) -> Settings:
    data_root = tmp_path / "data"
    return Settings(
        data_root=data_root,
        database_path=data_root / "control.sqlite3",
        host=host,
    )


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_unauthenticated_control_plane_allows_only_named_loopback_hosts(
    tmp_path: Path, host: str
) -> None:
    assert _settings(tmp_path, host=host).host == host


@pytest.mark.parametrize(
    "host", ["0.0.0.0", "::", "192.168.1.10", "example.com", "localhost."]
)
def test_settings_fail_closed_for_non_loopback_bind_hosts(
    tmp_path: Path, host: str
) -> None:
    with pytest.raises(ValueError, match="loopback"):
        _settings(tmp_path, host=host)


def test_api_revalidates_bind_host_at_startup(tmp_path: Path) -> None:
    settings = _settings(tmp_path, host="127.0.0.1")
    object.__setattr__(settings, "host", "0.0.0.0")

    with pytest.raises(ValueError, match="loopback"):
        create_app(settings)


def test_environment_configuration_rejects_public_bind_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VDC_HOST", "0.0.0.0")

    with pytest.raises(ValueError, match="loopback"):
        Settings.from_env()


@pytest.mark.parametrize("raw", ["1", "true", "YES", "on"])
def test_x_graph_v2_route_gate_requires_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("VDC_ENABLE_X_GRAPH_V2", raw)

    assert Settings.from_env().x_graph_v2_enabled is True


@pytest.mark.parametrize("raw", ["0", "false", "NO", "off"])
def test_x_graph_v2_route_gate_accepts_explicit_disable(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("VDC_ENABLE_X_GRAPH_V2", raw)

    assert Settings.from_env().x_graph_v2_enabled is False


def test_x_graph_v2_route_gate_fails_closed_for_ambiguous_environment_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VDC_ENABLE_X_GRAPH_V2", "maybe")

    with pytest.raises(ValueError, match="VDC_ENABLE_X_GRAPH_V2"):
        Settings.from_env()


def test_short_link_resolution_is_default_off_and_paths_do_not_enable_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "VDC_SHORT_LINK_TRANSPORT_SOCKET", str(tmp_path / "resolver.sock")
    )
    monkeypatch.setenv(
        "VDC_SHORT_LINK_ATTESTATION_KEY_FILE", str(tmp_path / "attest.key")
    )

    settings = Settings.from_env()

    assert settings.short_link_resolution_enabled is False
    assert settings.short_link_transport_socket == (tmp_path / "resolver.sock")
    assert settings.short_link_attestation_key_file == (tmp_path / "attest.key")


def test_short_link_resolution_gate_requires_both_security_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VDC_ENABLE_SHORT_LINK_RESOLUTION", "1")

    with pytest.raises(ValueError, match="requires both"):
        Settings.from_env()

    monkeypatch.setenv(
        "VDC_SHORT_LINK_TRANSPORT_SOCKET", str(tmp_path / "resolver.sock")
    )
    with pytest.raises(ValueError, match="requires both"):
        Settings.from_env()

    monkeypatch.setenv(
        "VDC_SHORT_LINK_ATTESTATION_KEY_FILE", str(tmp_path / "attest.key")
    )
    settings = Settings.from_env()
    assert settings.short_link_resolution_enabled is True


def test_short_link_resolution_gate_rejects_ambiguous_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VDC_ENABLE_SHORT_LINK_RESOLUTION", "auto")

    with pytest.raises(ValueError, match="VDC_ENABLE_SHORT_LINK_RESOLUTION"):
        Settings.from_env()


def test_short_link_security_paths_must_not_alias(tmp_path: Path) -> None:
    shared = (tmp_path / "shared").resolve()
    with pytest.raises(ValueError, match="different paths"):
        Settings(
            data_root=(tmp_path / "data").resolve(),
            database_path=(tmp_path / "data" / "control.sqlite3").resolve(),
            short_link_resolution_enabled=True,
            short_link_transport_socket=shared,
            short_link_attestation_key_file=shared,
        )


def test_short_link_security_paths_are_hidden_from_settings_repr(
    tmp_path: Path,
) -> None:
    socket_path = (tmp_path / "sensitive-socket-name.sock").resolve()
    key_path = (tmp_path / "sensitive-key-name.key").resolve()
    settings = Settings(
        data_root=(tmp_path / "data").resolve(),
        database_path=(tmp_path / "data" / "control.sqlite3").resolve(),
        short_link_resolution_enabled=True,
        short_link_transport_socket=socket_path,
        short_link_attestation_key_file=key_path,
    )

    rendered = repr(settings)
    assert str(socket_path) not in rendered
    assert str(key_path) not in rendered
    assert "sensitive-socket-name" not in rendered
    assert "sensitive-key-name" not in rendered


def test_short_link_environment_paths_reject_relative_or_parent_traversal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VDC_SHORT_LINK_TRANSPORT_SOCKET", "resolver.sock")

    with pytest.raises(ValueError, match="VDC_SHORT_LINK_TRANSPORT_SOCKET"):
        Settings.from_env()

    monkeypatch.setenv(
        "VDC_SHORT_LINK_TRANSPORT_SOCKET",
        str(tmp_path / "nested" / ".." / "resolver.sock"),
    )
    with pytest.raises(ValueError, match="normalized"):
        Settings.from_env()


def test_short_link_runtime_builder_masks_key_path_and_invalid_file(
    tmp_path: Path,
) -> None:
    private_name = "must-not-appear-attestation.key"
    key_path = (tmp_path / private_name).resolve()
    key_path.write_bytes(b"too-short")
    key_path.chmod(0o600)
    settings = Settings(
        data_root=(tmp_path / "data").resolve(),
        database_path=(tmp_path / "data" / "control.sqlite3").resolve(),
        short_link_resolution_enabled=True,
        short_link_transport_socket=(tmp_path / "resolver.sock").resolve(),
        short_link_attestation_key_file=key_path,
    )

    with pytest.raises(ValueError) as caught:
        create_app(settings)

    assert str(caught.value) == "short-link security configuration is invalid"
    assert private_name not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_short_link_runtime_builder_is_not_activated_by_paths_alone(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_root=(tmp_path / "data").resolve(),
        database_path=(tmp_path / "data" / "control.sqlite3").resolve(),
        short_link_transport_socket=(tmp_path / "missing.sock").resolve(),
        short_link_attestation_key_file=(tmp_path / "missing.key").resolve(),
    )

    app = create_app(settings)

    assert app.state.batch_service.short_link_resolver is None


def test_runtime_log_configuration_has_bounded_safe_defaults(tmp_path: Path) -> None:
    settings = _settings(tmp_path, host="127.0.0.1")

    assert settings.runtime_log_level == "INFO"
    assert settings.runtime_log_max_bytes == DEFAULT_RUNTIME_LOG_MAX_BYTES
    assert settings.runtime_log_backup_count == DEFAULT_RUNTIME_LOG_BACKUP_COUNT


def test_runtime_log_environment_configuration_is_normalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VDC_RUNTIME_LOG_LEVEL", "debug")
    monkeypatch.setenv("VDC_RUNTIME_LOG_MAX_BYTES", "4096")
    monkeypatch.setenv("VDC_RUNTIME_LOG_BACKUP_COUNT", "7")

    settings = Settings.from_env()

    assert settings.runtime_log_level == "DEBUG"
    assert settings.runtime_log_max_bytes == 4096
    assert settings.runtime_log_backup_count == 7


@pytest.mark.parametrize("level", ["TRACE", "verbose", "", "INFO\nSECRET"])
def test_runtime_log_environment_rejects_invalid_levels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VDC_RUNTIME_LOG_LEVEL", level)

    with pytest.raises(RuntimeLogConfigurationError, match="level"):
        Settings.from_env()


@pytest.mark.parametrize("value", ["0", "1023", "1073741825"])
def test_runtime_log_environment_rejects_unsafe_max_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VDC_RUNTIME_LOG_MAX_BYTES", value)

    with pytest.raises(RuntimeLogConfigurationError, match="max bytes"):
        Settings.from_env()


@pytest.mark.parametrize("value", ["0", "21", "999"])
def test_runtime_log_environment_rejects_unsafe_backup_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VDC_RUNTIME_LOG_BACKUP_COUNT", value)

    with pytest.raises(RuntimeLogConfigurationError, match="backup count"):
        Settings.from_env()


@pytest.mark.parametrize(
    "name",
    ["VDC_RUNTIME_LOG_MAX_BYTES", "VDC_RUNTIME_LOG_BACKUP_COUNT"],
)
def test_runtime_log_environment_rejects_non_integer_numeric_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(name, "not-an-integer")

    with pytest.raises(ValueError):
        Settings.from_env()


def test_tool_root_is_optional_and_hidden_from_settings_repr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert Settings.from_env().tool_root is None

    tool_root = (tmp_path / "private-tool-root").resolve()
    monkeypatch.setenv("VDC_TOOL_ROOT", str(tool_root))

    settings = Settings.from_env()

    assert settings.tool_root == tool_root
    assert str(tool_root) not in repr(settings)


@pytest.mark.parametrize("raw", ["tools", "nested/../tools"])
def test_tool_root_environment_rejects_relative_or_non_normalized_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.chdir(tmp_path)
    if raw.startswith("nested"):
        raw = str(tmp_path / raw)
    monkeypatch.setenv("VDC_TOOL_ROOT", raw)

    with pytest.raises(ValueError, match="VDC_TOOL_ROOT"):
        Settings.from_env()


def test_direct_settings_reject_relative_tool_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="tool root"):
        Settings(
            data_root=(tmp_path / "data").resolve(),
            database_path=(tmp_path / "data" / "control.sqlite3").resolve(),
            tool_root=Path("relative-tools"),
        )
