from __future__ import annotations

import json
import multiprocessing
import signal
import socket
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

import video_download_control.local_app as local_app_module
from video_download_control import __version__
from video_download_control.candidate_cookies import CookieSource
from video_download_control.database import SCHEMA_VERSION
from video_download_control.domain import Platform


PRODUCT_IDENTITY = f"{__version__}+build.sha256." + ("a" * 64)


def _block_on_command_pipe(command_connection, ready_connection) -> None:
    ready_connection.send_bytes(b"R")
    ready_connection.close()
    try:
        command_connection.recv_bytes()
    except (EOFError, OSError):
        pass
    finally:
        command_connection.close()


class _FakeConnection:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.closed = False

    def send_bytes(self, payload: bytes) -> None:
        self.sent.append(payload)

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self, *, name: str, pid: int) -> None:
        self.name = name
        self.pid = pid
        self.exitcode: int | None = None

    def is_alive(self) -> bool:
        return True


class _FakeContext:
    def __init__(self) -> None:
        self.pipes: list[tuple[_FakeConnection, _FakeConnection]] = []
        self.processes: list[_FakeProcess] = []

    def Pipe(self, *, duplex: bool):
        assert duplex is False
        pipe = (_FakeConnection(), _FakeConnection())
        self.pipes.append(pipe)
        return pipe

    def Process(self, *, name: str, **_kwargs) -> _FakeProcess:
        process = _FakeProcess(name=name, pid=4000 + len(self.processes))
        self.processes.append(process)
        return process


class _FakeListener:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeJob:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def emit(self, event: str, **fields: object) -> bool:
        self.events.append((event, fields))
        return True


def _default_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> local_app_module.LocalAppConfig:
    local_appdata = (tmp_path / "local-appdata").resolve()
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    return local_app_module.LocalAppConfig.from_environment()


def _health(*, paused: bool = False) -> dict[str, object]:
    return {
        "status": "degraded" if paused else "ok",
        "database": "ok",
        "schema_version": SCHEMA_VERSION,
        "worker": "paused" if paused else "external_status_unknown",
        "detail": "queue_paused:storage_error" if paused else None,
    }


def _capability() -> dict[str, object]:
    return {
        "current_product_identity": PRODUCT_IDENTITY,
        "implementations": [],
        "evidence": [],
        "decisions": [],
        "evidence_total": 0,
        "decision_total": 0,
        "evidence_truncated": False,
        "decision_truncated": False,
    }


def test_default_config_uses_one_localappdata_tree_and_ignores_cwd_and_vdc_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_appdata = (tmp_path / "local-appdata").resolve()
    cwd = (tmp_path / "unrelated-cwd").resolve()
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    monkeypatch.setenv("VDC_DATA_ROOT", str((tmp_path / "ambient-data").resolve()))
    monkeypatch.setenv(
        "VDC_DATABASE_PATH",
        str((tmp_path / "ambient.sqlite3").resolve()),
    )
    monkeypatch.setenv("VDC_TOOL_ROOT", str((tmp_path / "ambient-tools").resolve()))

    config = local_app_module.LocalAppConfig.from_environment()

    assert config.app_root == local_appdata / "Open-Flame" / "video-download-control"
    assert config.data_root == config.app_root / "data"
    assert config.database_path == config.data_root / "control.sqlite3"
    assert config.tool_root == config.app_root / "runtime-tools" / "windows-x64"
    assert not config.data_root.is_relative_to(cwd)
    assert not config.tool_root.is_relative_to(config.data_root)
    assert not config.data_root.is_relative_to(config.tool_root)
    assert not config.app_root.exists()


@pytest.mark.parametrize(
    "mutation",
    (
        lambda config, tmp_path: replace(config, app_root=Path("relative-app")),
        lambda config, tmp_path: replace(config, data_root=Path("relative-data")),
        lambda config, tmp_path: replace(
            config,
            database_path=(tmp_path / "outside.sqlite3").resolve(),
        ),
        lambda config, tmp_path: replace(
            config,
            tool_root=config.data_root / "nested-tools",
        ),
        lambda config, tmp_path: replace(
            config,
            data_root=config.tool_root / "nested-data",
        ),
    ),
)
def test_config_rejects_relative_divergent_or_overlapping_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation,
) -> None:
    config = _default_config(tmp_path, monkeypatch)

    with pytest.raises(local_app_module.LocalAppError):
        mutation(config, tmp_path)


@pytest.mark.parametrize(
    "path",
    (
        Path("C:\\"),
        Path(r"\\server\share\private"),
        Path(r"\\?\C:\private"),
        Path(r"\\.\private"),
        Path("C:\\safe\\trailing."),
        Path("C:\\safe\\trailing "),
        Path("C:\\safe\\CON"),
        Path("C:\\safe\\prn.txt"),
        Path("C:\\safe\\AUX.bin"),
        Path("C:\\safe\\nul.json"),
        *(Path(f"C:\\safe\\COM{index}.txt") for index in range(1, 10)),
        *(Path(f"C:\\safe\\LPT{index}.txt") for index in range(1, 10)),
        Path("C:\\safe\\CON\\child.txt"),
    ),
)
def test_windows_local_path_rejects_root_unc_device_unsafe_or_reserved_components(
    path: Path,
) -> None:
    with pytest.raises(local_app_module.LocalAppError):
        local_app_module._validate_windows_local_path(path, "test path")


def test_windows_local_path_accepts_a_normal_absolute_path() -> None:
    path = Path("C:\\safe-root\\plain-directory\\file.json")

    assert local_app_module._validate_windows_local_path(path, "test path") == path


@pytest.mark.parametrize("field", ("data_root", "tool_root"))
def test_config_rejects_cookie_config_overlapping_runtime_trees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    config = _default_config(tmp_path, monkeypatch)
    cookie_config = getattr(config, field) / "private-cookie-config.json"

    with pytest.raises(local_app_module.LocalAppError):
        replace(config, cookie_config_path=cookie_config)


@pytest.mark.parametrize("field", ("data_root", "tool_root"))
def test_config_rejects_cookie_source_overlapping_runtime_trees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    config = _default_config(tmp_path, monkeypatch)
    source = CookieSource(
        platform=Platform.DOUYIN,
        credential_ref="douyin-primary",
        path=getattr(config, field) / "private.cookies.txt",
    )

    with pytest.raises(local_app_module.LocalAppError):
        replace(config, cookie_sources=(source,))


def _ready_message() -> dict[str, object]:
    return {
        "protocol": 1,
        "status": "ready",
        "role": "worker",
        "phase": "worker_ready",
        "pid": 4242,
        "run_id": "1234567890abcdef1234567890abcdef",
        "app_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "product_identity": PRODUCT_IDENTITY,
    }


def test_ready_message_requires_one_exact_correlated_payload() -> None:
    expected = {
        "protocol": 1,
        "status": "ready",
        "role": "worker",
        "phase": "worker_ready",
        "pid": 4242,
        "run_id": "1234567890abcdef1234567890abcdef",
        "app_version": __version__,
        "product_identity": PRODUCT_IDENTITY,
        "schema_version": SCHEMA_VERSION,
    }

    assert (
        local_app_module._validate_ready_message(
            expected,
            role="worker",
            phase="worker_ready",
            pid=4242,
            run_id="1234567890abcdef1234567890abcdef",
            product_identity=PRODUCT_IDENTITY,
        )
        == expected
    )


@pytest.mark.parametrize(
    "message",
    (
        None,
        {},
        {**_ready_message(), "unexpected": "field"},
        {key: value for key, value in _ready_message().items() if key != "run_id"},
        {**_ready_message(), "run_id": "UPPERCASE-OR-NOT-HEX"},
        {**_ready_message(), "pid": True},
        {**_ready_message(), "schema_version": True},
    ),
)
def test_ready_message_rejects_non_mapping_missing_extra_or_invalid_fields(
    message: object,
) -> None:
    with pytest.raises(local_app_module.LocalAppError):
        local_app_module._validate_ready_message(
            message,
            role="worker",
            phase="worker_ready",
            pid=4242,
            run_id="1234567890abcdef1234567890abcdef",
            product_identity=PRODUCT_IDENTITY,
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("protocol", 2),
        ("role", "control"),
        ("status", "starting"),
        ("phase", "worker_preflight_ready"),
        ("pid", 4243),
        ("run_id", "fedcba0987654321fedcba0987654321"),
        ("app_version", "999.0.0"),
        (
            "product_identity",
            f"{__version__}+build.sha256." + ("b" * 64),
        ),
        ("schema_version", 9),
    ),
)
def test_ready_message_rejects_wrong_child_or_build_identity(
    field: str,
    replacement: object,
) -> None:
    expected = _ready_message()
    received = {**expected, field: replacement}

    with pytest.raises(local_app_module.LocalAppError):
        local_app_module._validate_ready_message(
            received,
            role="worker",
            phase="worker_ready",
            pid=4242,
            run_id="1234567890abcdef1234567890abcdef",
            product_identity=PRODUCT_IDENTITY,
        )


def test_control_health_accepts_ready_and_storage_paused_queue() -> None:
    assert local_app_module._validate_control_health(
        _health(),
        _capability(),
        schema_version=SCHEMA_VERSION,
        product_identity=PRODUCT_IDENTITY,
    ) is None
    assert local_app_module._validate_control_health(
        _health(paused=True),
        _capability(),
        schema_version=SCHEMA_VERSION,
        product_identity=PRODUCT_IDENTITY,
    ) is None


@pytest.mark.parametrize(
    ("health", "capability"),
    (
        ({**_health(), "database": "error", "status": "degraded"}, _capability()),
        ({**_health(), "schema_version": 9}, _capability()),
        (
            {**_health(), "status": "degraded", "detail": "missing_tables:batches"},
            _capability(),
        ),
        (
            {
                **_health(paused=True),
                "detail": "queue_paused:operator-supplied-text",
            },
            _capability(),
        ),
        (
            _health(),
            {
                **_capability(),
                "current_product_identity": f"{__version__}+build.sha256."
                + ("b" * 64),
            },
        ),
        (_health(), {}),
    ),
)
def test_control_health_rejects_database_schema_build_and_unknown_degradation(
    health: dict[str, object],
    capability: dict[str, object],
) -> None:
    with pytest.raises(local_app_module.LocalAppError):
        local_app_module._validate_control_health(
            health,
            capability,
            schema_version=SCHEMA_VERSION,
            product_identity=PRODUCT_IDENTITY,
        )


def test_child_environment_is_rebuilt_from_a_small_allowlist(
    tmp_path: Path,
) -> None:
    inherited = {
        "SystemRoot": r"C:\Windows",
        "WINDIR": r"C:\Windows",
        "TEMP": str((tmp_path / "temp").resolve()),
        "TMP": str((tmp_path / "tmp").resolve()),
        "PATH": r"C:\attacker",
        "PYTHONPATH": r"C:\injected",
        "PYTHONHOME": r"C:\injected-home",
        "HTTP_PROXY": "http://private.invalid",
        "HTTPS_PROXY": "http://private.invalid",
        "ALL_PROXY": "http://private.invalid",
        "NO_PROXY": "*",
        "SSLKEYLOGFILE": str((tmp_path / "tls.keys").resolve()),
        "BROWSER": r"C:\private-browser.exe",
        "AUTHORIZATION": "Bearer private",
        "COOKIE_SECRET": "private-cookie",
        "API_TOKEN": "private-token",
        "CLIENT_SECRET": "private-secret",
        "vdc_data_root": str((tmp_path / "wrong-data").resolve()),
        "VDC_DATABASE_PATH": str((tmp_path / "wrong.sqlite3").resolve()),
        "VDC_ENABLE_LOCAL_REAL_WORKER": "ambient-wrong-value",
    }

    environment = local_app_module._sanitized_child_environment(
        inherited,
        enable_worker=True,
    )

    assert environment["SystemRoot"] == inherited["SystemRoot"]
    assert environment["WINDIR"] == inherited["WINDIR"]
    assert environment["TEMP"] == inherited["TEMP"]
    assert environment["TMP"] == inherited["TMP"]
    assert environment["VDC_ENABLE_LOCAL_REAL_WORKER"] == "1"
    normalized = {key.upper() for key in environment}
    assert "PATH" not in normalized
    assert "PYTHONPATH" not in normalized
    assert "PYTHONHOME" not in normalized
    assert "HTTP_PROXY" not in normalized
    assert "HTTPS_PROXY" not in normalized
    assert "ALL_PROXY" not in normalized
    assert "NO_PROXY" not in normalized
    assert "SSLKEYLOGFILE" not in normalized
    assert "BROWSER" not in normalized
    assert "AUTHORIZATION" not in normalized
    assert all("COOKIE" not in key for key in normalized)
    assert all("TOKEN" not in key for key in normalized)
    assert all("SECRET" not in key for key in normalized)
    assert not {
        "VDC_DATA_ROOT",
        "VDC_DATABASE_PATH",
        "VDC_TOOL_ROOT",
        "VDC_HOST",
        "VDC_PORT",
    } & normalized
    assert sum(
        key.upper() == "VDC_ENABLE_LOCAL_REAL_WORKER" for key in environment
    ) == 1

    control_environment = local_app_module._sanitized_child_environment(
        inherited,
        enable_worker=False,
    )
    assert {key.upper() for key in control_environment} == {
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
    }
    assert "VDC_ENABLE_LOCAL_REAL_WORKER" not in {
        key.upper() for key in control_environment
    }


def test_reserve_loopback_socket_rejects_an_occupied_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        port = int(occupied.getsockname()[1])

        with pytest.raises(local_app_module.LocalAppError):
            with local_app_module._reserve_loopback_socket(port):
                raise AssertionError("an occupied port must never be reserved")


def test_non_windows_host_is_rejected_before_any_runtime_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _default_config(tmp_path, monkeypatch)

    def reject_host() -> None:
        raise local_app_module.LocalAppError("local app requires Windows")

    def unexpected(*_args, **_kwargs):
        raise AssertionError("no runtime side effect is allowed on a non-Windows host")

    monkeypatch.setattr(local_app_module, "_require_windows_host", reject_host)
    for name in (
        "_exclusive_local_app",
        "_reserve_loopback_socket",
        "_spawn_child",
        "_open_browser",
    ):
        monkeypatch.setattr(local_app_module, name, unexpected, raising=False)

    with pytest.raises(local_app_module.LocalAppError, match="Windows"):
        local_app_module.run_local_app(config)

    assert not config.app_root.exists()
    assert not config.database_path.exists()


def test_occupied_port_fails_before_database_child_or_browser_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _default_config(tmp_path, monkeypatch)
    spawned = False
    opened = False

    def unexpected_spawn(*_args, **_kwargs):
        nonlocal spawned
        spawned = True
        raise AssertionError("an occupied port must prevent child startup")

    def unexpected_browser(*_args, **_kwargs):
        nonlocal opened
        opened = True
        raise AssertionError("an occupied port must prevent browser startup")

    monkeypatch.setattr(local_app_module, "_require_windows_host", lambda: None)
    monkeypatch.setattr(
        local_app_module,
        "_spawn_child",
        unexpected_spawn,
        raising=False,
    )
    monkeypatch.setattr(
        local_app_module,
        "_open_browser",
        unexpected_browser,
        raising=False,
    )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        occupied_config = replace(
            config,
            port=int(occupied.getsockname()[1]),
            allow_direct_network=True,
        )

        with pytest.raises(local_app_module.LocalAppError):
            local_app_module.run_local_app(occupied_config)

    assert spawned is False
    assert opened is False
    assert not occupied_config.database_path.exists()


def test_missing_tool_root_fails_before_creating_the_application_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(
        _default_config(tmp_path, monkeypatch),
        allow_direct_network=True,
    )
    monkeypatch.setattr(local_app_module, "_require_windows_host", lambda: None)
    monkeypatch.setattr(
        local_app_module,
        "_reserve_loopback_socket",
        lambda _port: _FakeListener(),
    )

    with pytest.raises(local_app_module.LocalAppError, match="toolchain"):
        local_app_module.run_local_app(config)

    assert not config.app_root.exists()
    assert not config.database_path.exists()


def _patch_supervisor_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_ready_phase: str | None = None,
    fail_control_verification: int | None = None,
    fail_claim_gate_phase: str | None = None,
) -> tuple[list[str], _FakeContext]:
    order: list[str] = []
    context = _FakeContext()
    verify_count = 0

    class ClaimGate:
        def prepare_claim_gate(self, *, run_id: str, worker_id: str, now):
            del now
            assert len(run_id) == 32
            assert worker_id == "local-app-worker"
            order.append("gate:prepare")
            if fail_claim_gate_phase == "prepare":
                raise RuntimeError("SENTINEL-PRIVATE-PREPARE-ERROR")
            return {"accepting_claims": False}

        def activate_claim_gate(self, *, run_id: str, worker_id: str, now):
            del now
            assert len(run_id) == 32
            assert worker_id == "local-app-worker"
            order.append("gate:activate")
            if fail_claim_gate_phase == "activate":
                raise RuntimeError("SENTINEL-PRIVATE-ACTIVATE-ERROR")
            return {"accepting_claims": True}

    @contextmanager
    def exclusive(_app_root: Path):
        order.append("lock")
        yield

    def start_child(process, **_kwargs) -> None:
        order.append(f"start:{process.name}")

    def await_ready(_connection, _process, *, phase: str, **_kwargs):
        order.append(f"ready:{phase}")
        if phase == fail_ready_phase:
            raise local_app_module.LocalAppError("synthetic readiness failure")
        return {}

    def verify_control(
        _port: int,
        _product_identity: str,
        **_kwargs,
    ) -> None:
        nonlocal verify_count
        verify_count += 1
        order.append(f"verify:{verify_count}")
        if verify_count == fail_control_verification:
            raise local_app_module.LocalAppError("synthetic control failure")

    def open_browser(_url: str) -> None:
        order.append("browser")

    def cleanup(**_kwargs) -> bool:
        order.append("cleanup")
        return False

    def interrupt_running_loop(_seconds: float) -> None:
        order.append("running")
        raise KeyboardInterrupt

    monkeypatch.setattr(local_app_module, "_require_windows_host", lambda: None)
    monkeypatch.setattr(
        local_app_module,
        "_require_plain_directory_tree",
        lambda _path: None,
    )
    monkeypatch.setattr(local_app_module, "_prepare_layout", lambda _config: None)
    monkeypatch.setattr(
        local_app_module,
        "current_product_identity",
        lambda: PRODUCT_IDENTITY,
    )
    monkeypatch.setattr(local_app_module, "_exclusive_local_app", exclusive)
    monkeypatch.setattr(
        local_app_module,
        "_runtime_logger",
        lambda *_args, **_kwargs: _FakeLogger(),
    )
    monkeypatch.setattr(
        local_app_module,
        "_reserve_loopback_socket",
        lambda _port: _FakeListener(),
    )
    monkeypatch.setattr(local_app_module, "WindowsKillOnCloseJob", _FakeJob)
    monkeypatch.setattr(
        local_app_module.multiprocessing,
        "get_context",
        lambda _method: context,
    )
    monkeypatch.setattr(local_app_module, "_start_owned_child", start_child)
    monkeypatch.setattr(local_app_module, "_await_ready", await_ready)
    monkeypatch.setattr(local_app_module, "_verify_control", verify_control)
    monkeypatch.setattr(
        local_app_module,
        "_supervisor_claim_gate_repository",
        lambda _config: ClaimGate(),
    )
    monkeypatch.setattr(local_app_module, "_open_browser", open_browser)
    monkeypatch.setattr(local_app_module, "_cleanup_children", cleanup)
    monkeypatch.setattr(local_app_module.time, "sleep", interrupt_running_loop)
    return order, context


def test_check_only_never_releases_claim_gate_or_opens_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = replace(
        _default_config(tmp_path, monkeypatch),
        allow_direct_network=True,
        check_only=True,
        open_browser=True,
    )
    order, context = _patch_supervisor_lifecycle(monkeypatch)

    assert local_app_module.run_local_app(config) == 0

    assert json.loads(capsys.readouterr().out) == {"status": "checked"}
    assert "ready:control_ready" in order
    assert "ready:worker_preflight_ready" in order
    assert "ready:worker_ready" not in order
    assert "gate:activate" not in order
    assert order.index("verify:1") < order.index("gate:prepare")
    assert order.index("gate:prepare") < order.index("ready:worker_preflight_ready")
    assert "browser" not in order
    assert context.pipes[3][1].sent == [
        local_app_module._COMMAND_LAUNCH,
        local_app_module._COMMAND_CHECK_COMPLETE,
    ]
    assert order[-1] == "cleanup"


def test_check_only_verification_failure_never_sends_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(
        _default_config(tmp_path, monkeypatch),
        allow_direct_network=True,
        check_only=True,
    )
    order, context = _patch_supervisor_lifecycle(
        monkeypatch,
        fail_control_verification=2,
    )

    with pytest.raises(local_app_module.LocalAppError):
        local_app_module.run_local_app(config)

    assert context.pipes[3][1].sent == [local_app_module._COMMAND_LAUNCH]
    assert order[-1] == "cleanup"


def test_browser_opens_only_after_worker_ready_and_final_control_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = replace(
        _default_config(tmp_path, monkeypatch),
        allow_direct_network=True,
        open_browser=True,
    )
    order, context = _patch_supervisor_lifecycle(monkeypatch)

    assert local_app_module.run_local_app(config) == 0

    assert json.loads(capsys.readouterr().out) == {
        "status": "ready",
        "url": config.url,
    }
    assert order.index("ready:control_ready") < order.index("verify:1")
    assert order.index("verify:1") < order.index("gate:prepare")
    assert order.index("gate:prepare") < order.index("ready:worker_preflight_ready")
    assert order.index("ready:worker_preflight_ready") < order.index("verify:2")
    assert order.index("verify:2") < order.index("gate:activate")
    assert order.index("gate:activate") < order.index("ready:worker_ready")
    assert order.index("ready:worker_ready") < order.index("verify:3")
    assert order.index("verify:3") < order.index("browser")
    assert context.pipes[3][1].sent == [
        local_app_module._COMMAND_LAUNCH,
        local_app_module._COMMAND_CLAIM,
    ]
    assert order[-1] == "cleanup"


def test_supervisor_logs_claim_gate_prepare_and_activation_without_claim_run_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(
        _default_config(tmp_path, monkeypatch),
        allow_direct_network=True,
        open_browser=False,
    )
    _patch_supervisor_lifecycle(monkeypatch)
    logger = _FakeLogger()
    cleanup_arguments: dict[str, object] = {}

    def cleanup(**kwargs) -> bool:
        cleanup_arguments.update(kwargs)
        return False

    monkeypatch.setattr(
        local_app_module,
        "_runtime_logger",
        lambda *_args, **_kwargs: logger,
    )
    monkeypatch.setattr(local_app_module, "_cleanup_children", cleanup)

    assert local_app_module.run_local_app(config) == 0

    gate_events = [
        (event, fields)
        for event, fields in logger.events
        if event.startswith("local_app.claim_gate_")
    ]
    assert gate_events == [
        (
            "local_app.claim_gate_prepared",
            {"worker_id": config.worker_id, "gate_state": "prepared"},
        ),
        (
            "local_app.claim_gate_activated",
            {"worker_id": config.worker_id, "gate_state": "active"},
        ),
    ]
    assert all("run_id" not in fields for _event, fields in gate_events)
    assert cleanup_arguments["runtime_logger"] is logger
    assert cleanup_arguments["worker_id"] == config.worker_id


@pytest.mark.parametrize(
    ("failure_phase", "expected_events"),
    (
        (
            "prepare",
            [
                (
                    "local_app.claim_gate_prepared",
                    {
                        "level": "ERROR",
                        "worker_id": "local-app-worker",
                        "gate_state": "prepare_failed",
                    },
                )
            ],
        ),
        (
            "activate",
            [
                (
                    "local_app.claim_gate_prepared",
                    {
                        "worker_id": "local-app-worker",
                        "gate_state": "prepared",
                    },
                ),
                (
                    "local_app.claim_gate_activated",
                    {
                        "level": "ERROR",
                        "worker_id": "local-app-worker",
                        "gate_state": "activate_failed",
                    },
                ),
            ],
        ),
    ),
)
def test_supervisor_logs_bounded_claim_gate_database_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
    expected_events: list[tuple[str, dict[str, object]]],
) -> None:
    config = replace(
        _default_config(tmp_path, monkeypatch),
        allow_direct_network=True,
        open_browser=False,
    )
    _patch_supervisor_lifecycle(
        monkeypatch,
        fail_claim_gate_phase=failure_phase,
    )
    logger = _FakeLogger()
    monkeypatch.setattr(
        local_app_module,
        "_runtime_logger",
        lambda *_args, **_kwargs: logger,
    )

    with pytest.raises(local_app_module.LocalAppError):
        local_app_module.run_local_app(config)

    gate_events = [
        (event, fields)
        for event, fields in logger.events
        if event.startswith("local_app.claim_gate_")
    ]
    assert gate_events == expected_events
    assert "SENTINEL-PRIVATE" not in json.dumps(logger.events)


def test_no_open_browser_keeps_the_same_readiness_and_cleanup_sequence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = replace(
        _default_config(tmp_path, monkeypatch),
        allow_direct_network=True,
        open_browser=False,
    )
    order, context = _patch_supervisor_lifecycle(monkeypatch)

    assert local_app_module.run_local_app(config) == 0

    assert json.loads(capsys.readouterr().out) == {
        "status": "ready",
        "url": config.url,
    }
    assert "ready:worker_ready" in order
    assert "verify:3" in order
    assert "browser" not in order
    assert context.pipes[3][1].sent == [
        local_app_module._COMMAND_LAUNCH,
        local_app_module._COMMAND_CLAIM,
    ]
    assert order[-1] == "cleanup"


@pytest.mark.parametrize(
    ("fail_ready_phase", "fail_control_verification"),
    (
        ("control_ready", None),
        ("worker_preflight_ready", None),
        (None, 1),
        ("worker_ready", None),
        (None, 2),
        (None, 3),
    ),
)
def test_startup_failure_never_opens_browser_and_always_cleans_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_ready_phase: str | None,
    fail_control_verification: int | None,
) -> None:
    config = replace(
        _default_config(tmp_path, monkeypatch),
        allow_direct_network=True,
        open_browser=True,
    )
    order, _context = _patch_supervisor_lifecycle(
        monkeypatch,
        fail_ready_phase=fail_ready_phase,
        fail_control_verification=fail_control_verification,
    )

    with pytest.raises(local_app_module.LocalAppError):
        local_app_module.run_local_app(config)

    assert "browser" not in order
    assert order[-1] == "cleanup"


@pytest.mark.parametrize(
    ("completion_received", "expected_reason"),
    (
        (True, "check_complete"),
        (False, "supervisor_shutdown"),
    ),
)
def test_worker_check_child_requires_explicit_completion_before_logging_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    completion_received: bool,
    expected_reason: str,
) -> None:
    config = replace(
        _default_config(tmp_path, monkeypatch),
        allow_direct_network=True,
        check_only=True,
    )
    connection = _FakeConnection()
    command_connection = _FakeConnection()
    logger = _FakeLogger()
    run_once_calls = 0
    commands: list[bytes] = []

    class Worker:
        def run_once(self):
            nonlocal run_once_calls
            run_once_calls += 1
            raise AssertionError("check-only Worker must never claim a queue item")

    @contextmanager
    def exclusive(_data_root: Path):
        yield

    monkeypatch.setattr(
        local_app_module,
        "_apply_child_environment",
        lambda *_args, **_kwargs: None,
    )
    def wait_for_command(_connection, command: bytes) -> bool:
        commands.append(command)
        return (
            command == local_app_module._COMMAND_LAUNCH
            or completion_received
        )

    monkeypatch.setattr(
        local_app_module,
        "_wait_for_child_command",
        wait_for_command,
    )
    monkeypatch.setattr(local_app_module, "_exclusive_local_worker", exclusive)
    monkeypatch.setattr(
        local_app_module,
        "_local_worker_runtime_logger",
        lambda _config: logger,
    )
    monkeypatch.setattr(local_app_module, "build_local_worker", lambda *_a, **_k: Worker())
    monkeypatch.setattr(
        local_app_module,
        "current_product_identity",
        lambda: PRODUCT_IDENTITY,
    )

    local_app_module._worker_child_main(
        config,
        connection,
        command_connection,
        "1234567890abcdef1234567890abcdef",
        PRODUCT_IDENTITY,
    )

    assert run_once_calls == 0
    assert commands == [
        local_app_module._COMMAND_LAUNCH,
        local_app_module._COMMAND_CHECK_COMPLETE,
    ]
    assert len(connection.sent) == 1
    assert json.loads(connection.sent[0])["phase"] == "worker_preflight_ready"
    assert connection.closed is True
    assert command_connection.closed is True
    assert [event for event, _fields in logger.events] == [
        "worker.preflight_started",
        "worker.preflight_succeeded",
        "worker.stopped",
    ]
    assert logger.events[-1][1]["reason"] == expected_reason


def test_idle_worker_stops_on_command_eof_without_starting_a_second_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(
        _default_config(tmp_path, monkeypatch),
        allow_direct_network=True,
        poll_interval_seconds=0.1,
    )
    connection = _FakeConnection()
    command_connection = _FakeConnection()
    logger = _FakeLogger()
    run_once_calls = 0
    commands: list[bytes] = []
    channel_states = iter((False, True))

    class Worker:
        def claim_once(self, **_kwargs):
            nonlocal run_once_calls
            run_once_calls += 1
            return None

    @contextmanager
    def exclusive(_data_root: Path):
        yield

    monkeypatch.setattr(
        local_app_module,
        "_apply_child_environment",
        lambda *_args, **_kwargs: None,
    )

    def wait_for_command(_connection, command: bytes) -> bool:
        commands.append(command)
        return True

    monkeypatch.setattr(
        local_app_module,
        "_wait_for_child_command",
        wait_for_command,
    )
    monkeypatch.setattr(
        local_app_module,
        "_command_channel_closed",
        lambda *_args, **_kwargs: next(channel_states),
    )
    monkeypatch.setattr(local_app_module, "_exclusive_local_worker", exclusive)
    monkeypatch.setattr(
        local_app_module,
        "_local_worker_runtime_logger",
        lambda _config: logger,
    )
    monkeypatch.setattr(
        local_app_module,
        "build_local_worker",
        lambda *_args, **_kwargs: Worker(),
    )
    monkeypatch.setattr(
        local_app_module,
        "current_product_identity",
        lambda: PRODUCT_IDENTITY,
    )

    local_app_module._worker_child_main(
        config,
        connection,
        command_connection,
        "abcdef1234567890abcdef1234567890",
        PRODUCT_IDENTITY,
    )

    assert run_once_calls == 1
    assert commands == [
        local_app_module._COMMAND_LAUNCH,
        local_app_module._COMMAND_CLAIM,
    ]
    assert [json.loads(payload)["phase"] for payload in connection.sent] == [
        "worker_preflight_ready",
        "worker_ready",
    ]
    assert [event for event, _fields in logger.events] == [
        "worker.preflight_started",
        "worker.preflight_succeeded",
        "worker.started",
        "worker.stopped",
    ]
    assert logger.events[-1][1]["reason"] == "supervisor_shutdown"
    assert connection.closed is True
    assert command_connection.closed is True


def test_cleanup_requests_worker_before_control_without_forcing_owned_job() -> None:
    order: list[str] = []

    class Command:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            order.append(f"close:{self.name}")

    class Process:
        def __init__(self, name: str) -> None:
            self.name = name
            self.pid = 1234
            self.alive = True

        def join(self, _timeout: float) -> None:
            order.append(f"join:{self.name}")
            self.alive = False

        def is_alive(self) -> bool:
            return self.alive

    class Job:
        def terminate(self) -> None:
            order.append("job:terminate")

    forced = local_app_module._cleanup_children(
        worker=Process("worker"),
        control=Process("control"),
        worker_command=Command("worker-command"),
        control_command=Command("control-command"),
        job=Job(),
        timeout_seconds=10.0,
    )

    assert forced is False
    assert order == [
        "close:worker-command",
        "join:worker",
        "close:control-command",
        "join:control",
    ]


def test_cleanup_commits_claim_gate_stop_before_closing_worker_channel() -> None:
    order: list[str] = []
    logger = _FakeLogger()

    class Gate:
        def stop_claim_gate(self, *, run_id: str, now) -> bool:
            del now
            assert run_id == "e" * 32
            order.append("gate:stop")
            return True

    class Command:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            order.append(f"close:{self.name}")

    class Job:
        def terminate(self) -> None:
            order.append("job:terminate")

    forced = local_app_module._cleanup_children(
        worker=None,
        control=None,
        worker_command=Command("worker-command"),
        control_command=Command("control-command"),
        job=Job(),
        timeout_seconds=10.0,
        claim_gate_repository=Gate(),
        claim_gate_run_id="e" * 32,
        runtime_logger=logger,
        worker_id="local-app-worker",
    )

    assert forced is False
    assert order == [
        "gate:stop",
        "close:worker-command",
        "close:control-command",
    ]
    assert logger.events == [
        (
            "local_app.claim_gate_stopped",
            {"worker_id": "local-app-worker", "gate_state": "stopped"},
        )
    ]


def test_cleanup_logs_when_a_newer_run_already_fenced_the_claim_gate() -> None:
    logger = _FakeLogger()

    class Gate:
        def stop_claim_gate(self, **_kwargs) -> bool:
            return False

    forced = local_app_module._cleanup_children(
        worker=None,
        control=None,
        worker_command=_FakeConnection(),
        control_command=_FakeConnection(),
        job=_FakeJob(),
        timeout_seconds=10.0,
        claim_gate_repository=Gate(),
        claim_gate_run_id="e" * 32,
        runtime_logger=logger,
        worker_id="local-app-worker",
    )

    assert forced is False
    assert logger.events == [
        (
            "local_app.claim_gate_stopped",
            {
                "level": "WARNING",
                "worker_id": "local-app-worker",
                "gate_state": "fenced",
            },
        )
    ]


def test_cleanup_kills_owned_job_before_pipe_close_when_gate_stop_fails() -> None:
    order: list[str] = []
    logger = _FakeLogger()

    class Gate:
        def stop_claim_gate(self, **_kwargs) -> bool:
            order.append("gate:stop")
            raise RuntimeError("synthetic gate failure")

    class Command:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            order.append(f"close:{self.name}")

    class Job:
        def terminate(self) -> None:
            order.append("job:terminate")

    forced = local_app_module._cleanup_children(
        worker=None,
        control=None,
        worker_command=Command("worker-command"),
        control_command=Command("control-command"),
        job=Job(),
        timeout_seconds=10.0,
        claim_gate_repository=Gate(),
        claim_gate_run_id="f" * 32,
        runtime_logger=logger,
        worker_id="local-app-worker",
    )

    assert forced is True
    assert order == [
        "gate:stop",
        "job:terminate",
        "close:worker-command",
        "close:control-command",
    ]
    assert logger.events == [
        (
            "local_app.claim_gate_stopped",
            {
                "level": "ERROR",
                "worker_id": "local-app-worker",
                "gate_state": "stop_failed",
            },
        ),
        (
            "local_app.forced_shutdown",
            {
                "level": "WARNING",
                "cause": "gate_stop_failed",
                "failure_site": "claim_gate_stop",
            },
        ),
    ]


def test_cleanup_logs_child_timeout_without_exception_text() -> None:
    order: list[str] = []
    logger = _FakeLogger()

    class Process:
        pid = 1234

        def is_alive(self) -> bool:
            return True

        def join(self, _timeout: float) -> None:
            order.append("join")

        def terminate(self) -> None:
            order.append("process:terminate")

    class Job:
        def terminate(self) -> None:
            order.append("job:terminate")

    forced = local_app_module._cleanup_children(
        worker=Process(),
        control=None,
        worker_command=_FakeConnection(),
        control_command=_FakeConnection(),
        job=Job(),
        timeout_seconds=0.0,
        runtime_logger=logger,
        worker_id="local-app-worker",
    )

    assert forced is True
    assert order == ["join", "job:terminate", "join"]
    assert logger.events == [
        (
            "local_app.forced_shutdown",
            {
                "level": "WARNING",
                "cause": "child_timeout",
                "failure_site": "child_shutdown",
            },
        )
    ]


def test_cleanup_closes_the_command_channel_for_an_already_dead_child() -> None:
    order: list[str] = []

    class Command:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            order.append(f"close:{self.name}")

    class Process:
        def __init__(self, name: str, *, alive: bool) -> None:
            self.name = name
            self.pid = 1234
            self.alive = alive

        def join(self, _timeout: float) -> None:
            order.append(f"join:{self.name}")
            self.alive = False

        def is_alive(self) -> bool:
            return self.alive

    class Job:
        def terminate(self) -> None:
            order.append("job:terminate")

    forced = local_app_module._cleanup_children(
        worker=Process("worker", alive=False),
        control=Process("control", alive=True),
        worker_command=Command("worker-command"),
        control_command=Command("control-command"),
        job=Job(),
        timeout_seconds=10.0,
    )

    assert forced is False
    assert order == [
        "close:worker-command",
        "join:worker",
        "close:control-command",
        "join:control",
    ]


def test_abruptly_terminated_command_receiver_cannot_block_cleanup() -> None:
    context = multiprocessing.get_context("spawn")
    command_receive, command_send = context.Pipe(duplex=False)
    ready_receive, ready_send = context.Pipe(duplex=False)
    process = context.Process(
        target=_block_on_command_pipe,
        args=(command_receive, ready_send),
    )
    process.start()
    command_receive.close()
    ready_send.close()
    try:
        assert ready_receive.poll(5.0)
        assert ready_receive.recv_bytes() == b"R"
        process.terminate()
        process.join(5.0)
        assert not process.is_alive()

        started = time.monotonic()
        forced = local_app_module._cleanup_children(
            worker=process,
            control=None,
            worker_command=command_send,
            control_command=_FakeConnection(),
            job=_FakeJob(),
            timeout_seconds=1.0,
        )

        assert forced is False
        assert time.monotonic() - started < 1.0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5.0)
        ready_receive.close()
        command_send.close()
        process.close()


@pytest.mark.skipif(
    not hasattr(signal, "SIGBREAK"),
    reason="Windows console-break signal is unavailable",
)
def test_supervisor_translates_sigbreak_and_restores_the_previous_handler() -> None:
    sigbreak = signal.SIGBREAK
    previous = signal.getsignal(sigbreak)

    with local_app_module._supervisor_signal_handlers():
        assert signal.getsignal(sigbreak) is signal.default_int_handler

    assert signal.getsignal(sigbreak) == previous
