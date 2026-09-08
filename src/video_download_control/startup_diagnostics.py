"""Closed, persistent local failure diagnostics independent of the business root.

Only a fixed enum crosses this boundary. No command line, exception, path,
Cookie value, environment value or captured stderr is accepted as record data.
Records are closed before reporting success; no power-loss durability is claimed.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from enum import Enum
from pathlib import Path

from . import __version__
from .runtime_logging import (
    LOCAL_FAILURE_DIAGNOSTIC_CONTEXT,
    RuntimeLogConfig,
    RuntimeLogger,
    _reject_link_components,
    _require_plain_file,
)


class DiagnosticCode(Enum):
    INVALID_ARGUMENTS = "invalid_arguments"
    LOCAL_PORT_UNAVAILABLE = "local_port_unavailable"
    LOCAL_TOOLCHAIN_UNAVAILABLE = "local_toolchain_unavailable"
    LOCAL_COOKIE_CONFIG_INVALID = "local_cookie_config_invalid"
    LOCAL_DEPENDENCIES_UNAVAILABLE = "local_dependencies_unavailable"
    LOCAL_APP_FAILED = "local_app_failed"
    INTERNAL_ERROR = "internal_error"
    SETUP_PREREQUISITE_UNAVAILABLE = "setup_prerequisite_unavailable"
    SETUP_ENVIRONMENT_UNAVAILABLE = "setup_environment_unavailable"
    SETUP_DEPENDENCIES_FAILED = "setup_dependencies_failed"
    SETUP_TOOLCHAIN_FAILED = "setup_toolchain_failed"
    SETUP_AI_RUNTIME_FAILED = "setup_ai_runtime_failed"
    SETUP_BUSY = "setup_busy"
    SETUP_INTERRUPTED = "setup_interrupted"


_NEXT_STEPS = {
    DiagnosticCode.INVALID_ARGUMENTS: "启动参数无效；请运行 --help 检查参数后重试。",
    DiagnosticCode.LOCAL_PORT_UNAVAILABLE: "请关闭占用端口的程序，或使用 --port 指定其他端口后重试。",
    DiagnosticCode.LOCAL_TOOLCHAIN_UNAVAILABLE: "请安装锁定工具包，或使用 --tool-root 指定已安装的有效工具目录后重试。",
    DiagnosticCode.LOCAL_COOKIE_CONFIG_INVALID: "请检查 --cookie-config 的只读 JSON 配置；如需匿名启动，请移除该参数后重试。",
    DiagnosticCode.LOCAL_DEPENDENCIES_UNAVAILABLE: "请修复本地 Python 环境，并按照 README 安装项目依赖后重试。",
    DiagnosticCode.LOCAL_APP_FAILED: "请检查运行环境；若已有运行日志，请保留日志并根据错误码排查。",
    DiagnosticCode.INTERNAL_ERROR: "请检查运行环境；若已有运行日志，请保留日志并根据错误码排查。",
    DiagnosticCode.SETUP_PREREQUISITE_UNAVAILABLE: "安装前置条件不满足；请按 README 检查 Python 与安装工具，再运行 Setup-Open-Flame.cmd。",
    DiagnosticCode.SETUP_ENVIRONMENT_UNAVAILABLE: "本地 Python 环境不可用；安装器不会覆盖未知环境。请先保留现有内容，按 README 检查或修复后再运行 Setup-Open-Flame.cmd。",
    DiagnosticCode.SETUP_DEPENDENCIES_FAILED: "项目依赖安装失败；请按 README 检查网络与依赖配置，再运行 Setup-Open-Flame.cmd。",
    DiagnosticCode.SETUP_TOOLCHAIN_FAILED: "下载工具包安装或校验失败；请按 README 检查网络与工具包来源，再运行 Setup-Open-Flame.cmd。",
    DiagnosticCode.SETUP_AI_RUNTIME_FAILED: "AI runtime 安装或校验失败；安装器不会覆盖已有 runtime。请停止应用，保留或重命名已有目录，并按 AI runtime 指南检查本地 CPython 压缩包后重试。",
    DiagnosticCode.SETUP_BUSY: "源码环境正被安装器或应用占用，或其锁文件不可用；请先正常停止相关程序，再检查目录权限并重试 Setup-Open-Flame.cmd 或启动器，详见 README。",
    DiagnosticCode.SETUP_INTERRUPTED: "安装已取消；已有数据保留。可重新运行 Setup-Open-Flame.cmd，详见 README。",
}
_DIAGNOSTIC_MAX_BYTES = 256 * 1024
_DIAGNOSTIC_BACKUP_COUNT = 3
_LOCK_TIMEOUT_SECONDS = 1.0
_THREAD_LOCK = threading.Lock()


def build_failure_payload(code: DiagnosticCode) -> dict[str, str]:
    """Build fixed operator guidance without accepting caller-supplied fields."""

    if type(code) is not DiagnosticCode:
        raise ValueError("invalid diagnostic code")
    site, log_status = LOCAL_FAILURE_DIAGNOSTIC_CONTEXT[code.value]
    return {
        "status": "error",
        "error_code": code.value,
        "failure_site": site,
        "log_status": log_status,
        "next_step": _NEXT_STEPS[code],
    }


def _diagnostic_directory() -> Path:
    raw = os.environ.get("LOCALAPPDATA")
    if not isinstance(raw, str) or not raw or os.name != "nt":
        raise OSError("diagnostic directory unavailable")
    base = Path(raw)
    if (
        not base.is_absolute()
        or base == Path(base.anchor)
        or Path(os.path.abspath(base)) != base
        or raw.startswith("\\\\")
        or any(
            part.endswith((" ", ".")) or ":" in part
            or any(ord(character) < 32 for character in part)
            for part in base.parts[1:]
        )
    ):
        raise OSError("diagnostic directory unavailable")
    _reject_link_components(base)
    info = base.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise OSError("diagnostic directory unavailable")
    directory = base / "Open-Flame" / "diagnostics"
    _reject_link_components(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    _reject_link_components(directory)
    return directory


@contextmanager
def _exclusive_diagnostic_writer(directory: Path) -> Iterator[None]:
    """Bounded thread and Windows process locks cover one rotation + append."""

    import msvcrt

    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    if not _THREAD_LOCK.acquire(timeout=_LOCK_TIMEOUT_SECONDS):
        raise OSError("diagnostic writer busy")
    descriptor: int | None = None
    locked = False
    try:
        path = directory / ".launch-diagnostics.lock"
        _reject_link_components(path)
        if path.exists():
            _require_plain_file(path)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_BINARY, 0o600)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_size != 0
            or getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        ):
            raise OSError("diagnostic lock unavailable")
        while True:
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                locked = True
                break
            except OSError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OSError("diagnostic writer busy") from None
                time.sleep(min(0.01, remaining))
        yield
    finally:
        try:
            if locked and descriptor is not None:
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        finally:
            try:
                if descriptor is not None:
                    os.close(descriptor)
            finally:
                _THREAD_LOCK.release()


def persist_diagnostic(code: DiagnosticCode) -> bool:
    """Persist one bounded record; saving errors never replace the real failure."""

    if type(code) is not DiagnosticCode:
        return False
    try:
        payload = build_failure_payload(code)
        directory = _diagnostic_directory()
        with _exclusive_diagnostic_writer(directory):
            logger = RuntimeLogger(
                component="launch-diagnostics",
                config=RuntimeLogConfig(
                    directory=directory,
                    max_bytes=_DIAGNOSTIC_MAX_BYTES,
                    backup_count=_DIAGNOSTIC_BACKUP_COUNT,
                ),
            )
            return logger.emit(
                "local_app.failure_reported",
                level="ERROR",
                app_version=__version__,
                diagnostic_code=code.value,
                failure_site=payload["failure_site"],
                log_status=payload["log_status"],
            )
    except Exception:  # noqa: BLE001 - diagnostic failure must stay secondary
        return False


def emit_failure(code: DiagnosticCode) -> dict[str, str]:
    """Save fixed guidance and emit one safe JSON object to stderr."""

    payload = build_failure_payload(code)
    payload["diagnostic_status"] = "saved" if persist_diagnostic(code) else "unavailable"
    try:
        sys.stderr.write(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        )
    except UnicodeEncodeError:
        # Preserve the original record and guidance on legacy Windows streams.
        # Escaping JSON changes its byte representation, not its parsed values.
        sys.stderr.write(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            + "\n"
        )
    return payload


__all__ = ["DiagnosticCode", "build_failure_payload", "emit_failure", "persist_diagnostic"]
