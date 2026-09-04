from __future__ import annotations

import math
import os
import re
import signal
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from .runtime_logging import RuntimeLogger, safe_exception_type


class SubprocessPolicyError(ValueError):
    pass


class SubprocessExecutionError(RuntimeError):
    pass


class CommandProcessError(SubprocessExecutionError):
    """The runner could not spawn or control its child process."""


class CommandTimedOut(SubprocessExecutionError):
    pass


class CommandOutputLimitExceeded(SubprocessExecutionError):
    pass


class CommandCancelled(SubprocessExecutionError):
    pass


@dataclass(frozen=True, slots=True)
class CommandSpec:
    executable: Path = field(repr=False)
    arguments: tuple[str, ...] = field(default=(), repr=False)
    cwd: Path | None = field(default=None, repr=False)
    environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    timeout_seconds: float = 300.0
    stdout_limit_bytes: int = 1024 * 1024
    stderr_limit_bytes: int = 1024 * 1024
    # Observers receive complete lines without their CR/LF terminator. They run
    # on the caller thread and only see bytes already admitted by the bounded
    # output buffer. Their identity is deliberately absent from repr/equality.
    stdout_line_observer: Callable[[bytes], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    stderr_line_observer: Callable[[bytes], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    duration_seconds: float


class _BoundedReader:
    def __init__(self, stream, limit: int) -> None:
        self.stream = stream
        self.limit = limit
        self.buffer = bytearray()
        self.exceeded = threading.Event()
        self._lock = threading.Lock()

    def run(self) -> None:
        read = getattr(self.stream, "read1", self.stream.read)
        try:
            while chunk := read(64 * 1024):
                with self._lock:
                    remaining = self.limit - len(self.buffer)
                    if remaining > 0:
                        self.buffer.extend(chunk[:remaining])
                    exceeded = len(chunk) > remaining
                if exceeded:
                    self.exceeded.set()
        finally:
            self.stream.close()

    def read_from(self, offset: int) -> tuple[bytes, int]:
        with self._lock:
            end = len(self.buffer)
            return bytes(self.buffer[offset:end]), end

    def result(self) -> bytes:
        with self._lock:
            return bytes(self.buffer)


class _LineObserverCursor:
    """Incrementally split one bounded byte stream into universal-newline lines."""

    def __init__(
        self,
        reader: _BoundedReader,
        observer: Callable[[bytes], None] | None,
    ) -> None:
        self.reader = reader
        self.observer = observer
        self.offset = 0
        self.pending = bytearray()

    def drain(self, *, final: bool = False) -> None:
        if self.observer is None:
            return
        chunk, self.offset = self.reader.read_from(self.offset)
        self.pending.extend(chunk)
        lines: list[bytes] = []
        start = 0
        index = 0
        while index < len(self.pending):
            current = self.pending[index]
            if current == 10:  # LF
                lines.append(bytes(self.pending[start:index]))
                index += 1
                start = index
                continue
            if current == 13:  # CR or CRLF
                if index + 1 == len(self.pending) and not final:
                    break
                lines.append(bytes(self.pending[start:index]))
                index += 1
                if index < len(self.pending) and self.pending[index] == 10:
                    index += 1
                start = index
                continue
            index += 1
        if start:
            del self.pending[:start]
        if final and self.pending:
            lines.append(bytes(self.pending))
            self.pending.clear()
        for line in lines:
            self.observer(line)


class SecureSubprocessRunner:
    """Run one absolute executable with bounded output and tree termination.

    This runner is intentionally transport-agnostic. It constrains process
    execution but does not prove network isolation; networked downloaders must
    additionally run inside the separately enforced egress boundary.
    """

    _HOST_ENVIRONMENT_KEYS = (
        "SystemRoot",
        "WINDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "TZ",
    )
    _ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    _HARD_DENIED_ENVIRONMENT_KEYS = frozenset(
        {
            "PATH",
            "PATHEXT",
            "PYTHONPATH",
            "PYTHONHOME",
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "DYLD_INSERT_LIBRARIES",
            "SSLKEYLOGFILE",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
        }
    )

    def __init__(
        self,
        *,
        allowed_executable_roots: Sequence[Path] | None = None,
        allowed_environment_keys: frozenset[str] = frozenset(),
        poll_interval_seconds: float = 0.02,
        runtime_logger: RuntimeLogger | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        if (
            isinstance(poll_interval_seconds, bool)
            or not isinstance(poll_interval_seconds, (int, float))
            or not math.isfinite(poll_interval_seconds)
            or poll_interval_seconds <= 0
        ):
            raise ValueError("poll_interval_seconds must be a finite positive number")
        self.allowed_executable_roots = (
            tuple(path.resolve() for path in allowed_executable_roots)
            if allowed_executable_roots is not None
            else None
        )
        self.allowed_environment_keys = frozenset(
            key.upper() for key in allowed_environment_keys
        )
        self.poll_interval_seconds = poll_interval_seconds
        self.runtime_logger = runtime_logger
        # One event can cover every command belonging to a concurrent Worker,
        # including probes and verifiers that have no per-job callback. It is a
        # one-way stop signal; this runner never resets another caller's stop.
        self.stop_event = stop_event if stop_event is not None else threading.Event()

    def run(
        self,
        spec: CommandSpec,
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> CommandResult:
        subprocess_id = uuid4().hex
        started = time.monotonic()
        try:
            executable = self._validate_executable(spec.executable)
            cwd = self._validate_cwd(spec.cwd)
            self._validate_limits(spec)
            self._validate_observers(spec)
            arguments = self._validate_arguments(spec.arguments)
            environment = self._build_environment(executable, spec.environment)
        except OSError as exc:
            failure = SubprocessPolicyError(
                "subprocess policy could not be validated"
            )
            self._emit(
                "subprocess.failed",
                level="ERROR",
                subprocess_id=subprocess_id,
                duration_ms=(time.monotonic() - started) * 1000,
                exception_type=safe_exception_type(failure),
                failure_site="policy_validation",
            )
            raise failure from exc
        except (TypeError, ValueError) as exc:
            self._emit(
                "subprocess.failed",
                level="ERROR",
                subprocess_id=subprocess_id,
                duration_ms=(time.monotonic() - started) * 1000,
                exception_type=safe_exception_type(exc),
                failure_site="policy_validation",
            )
            raise

        executable_name = executable.name.lower()
        if self.stop_event.is_set():
            failure = CommandCancelled("subprocess was cancelled before launch")
            self._emit(
                "subprocess.failed",
                level="WARNING",
                subprocess_id=subprocess_id,
                executable=executable_name,
                argument_count=len(arguments),
                duration_ms=(time.monotonic() - started) * 1000,
                exception_type=safe_exception_type(failure),
                failure_site="process_spawn",
            )
            raise failure
        self._emit(
            "subprocess.started",
            subprocess_id=subprocess_id,
            executable=executable_name,
            argument_count=len(arguments),
            timeout_seconds=spec.timeout_seconds,
        )

        popen_options: dict[str, object] = {
            "cwd": str(cwd) if cwd is not None else None,
            "env": environment,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "shell": False,
            "close_fds": True,
        }
        if os.name == "nt":
            popen_options["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        else:
            popen_options["start_new_session"] = True

        try:
            process = subprocess.Popen([str(executable), *arguments], **popen_options)
        except Exception as exc:
            failure = CommandProcessError(
                "subprocess could not be spawned under policy"
            )
            self._emit(
                "subprocess.failed",
                level="ERROR",
                subprocess_id=subprocess_id,
                executable=executable_name,
                argument_count=len(arguments),
                duration_ms=(time.monotonic() - started) * 1000,
                exception_type=safe_exception_type(failure),
                failure_site="process_spawn",
            )
            raise failure from exc
        except BaseException as exc:
            self._emit(
                "subprocess.failed",
                level="ERROR",
                subprocess_id=subprocess_id,
                executable=executable_name,
                argument_count=len(arguments),
                duration_ms=(time.monotonic() - started) * 1000,
                exception_type=safe_exception_type(exc),
                failure_site="process_spawn",
            )
            raise
        reader_threads: list[threading.Thread] = []
        try:
            assert process.stdout is not None
            assert process.stderr is not None
            stdout_reader = _BoundedReader(process.stdout, spec.stdout_limit_bytes)
            stderr_reader = _BoundedReader(process.stderr, spec.stderr_limit_bytes)
            stdout_thread = threading.Thread(target=stdout_reader.run, daemon=True)
            reader_threads.append(stdout_thread)
            stderr_thread = threading.Thread(target=stderr_reader.run, daemon=True)
            reader_threads.append(stderr_thread)
            stdout_thread.start()
            stderr_thread.start()
            stdout_observer = _LineObserverCursor(
                stdout_reader,
                spec.stdout_line_observer,
            )
            stderr_observer = _LineObserverCursor(
                stderr_reader,
                spec.stderr_line_observer,
            )
        except BaseException as exc:
            # Popen already owns a live process even if a reader cannot start.
            # Termination precedes joining so a reader blocked on EOF wakes up;
            # the never-started reader's stream must also be closed explicitly.
            self._best_effort_shutdown(process)
            self._best_effort_join(
                *(thread for thread in reader_threads if thread.ident is not None)
            )
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except BaseException:
                        pass
            setup_failure = (
                CommandProcessError("subprocess output readers could not be started")
                if isinstance(exc, Exception)
                else exc
            )
            return_code = (
                {"return_code": process.returncode}
                if isinstance(process.returncode, int)
                else {}
            )
            self._emit(
                "subprocess.failed",
                level="ERROR",
                subprocess_id=subprocess_id,
                executable=executable_name,
                argument_count=len(arguments),
                duration_ms=(time.monotonic() - started) * 1000,
                exception_type=safe_exception_type(setup_failure),
                failure_site="process_spawn",
                **return_code,
            )
            if setup_failure is exc:
                raise
            raise setup_failure from exc

        failure: SubprocessExecutionError | None = None
        failure_site = "process_wait"
        try:
            while self._poll_process(process) is None:
                failure_site = "process_wait"
                if stdout_reader.exceeded.is_set() or stderr_reader.exceeded.is_set():
                    failure = CommandOutputLimitExceeded(
                        "subprocess output exceeded configured byte limit"
                    )
                    break
                if self.stop_event.is_set() or (
                    is_cancelled is not None and is_cancelled()
                ):
                    failure = CommandCancelled("subprocess was cancelled")
                    break
                if time.monotonic() - started >= spec.timeout_seconds:
                    failure = CommandTimedOut("subprocess deadline exceeded")
                    break
                failure_site = "output_observer"
                stdout_observer.drain()
                stderr_observer.drain()
                failure_site = "process_wait"
                time.sleep(self.poll_interval_seconds)
        except BaseException as exc:
            self._best_effort_shutdown(process)
            self._best_effort_join(stdout_thread, stderr_thread)
            return_code = (
                {"return_code": process.returncode}
                if isinstance(process.returncode, int)
                else {}
            )
            self._emit(
                "subprocess.failed",
                level="ERROR",
                subprocess_id=subprocess_id,
                executable=executable_name,
                argument_count=len(arguments),
                duration_ms=(time.monotonic() - started) * 1000,
                exception_type=safe_exception_type(exc),
                failure_site=failure_site,
                **return_code,
            )
            raise

        try:
            if failure is not None:
                self._terminate_process_tree(process)
            self._wait_for_process(process)
        except CommandProcessError as exc:
            self._best_effort_shutdown(process)
            self._best_effort_join(stdout_thread, stderr_thread)
            return_code = (
                {"return_code": process.returncode}
                if isinstance(process.returncode, int)
                else {}
            )
            self._emit(
                "subprocess.failed",
                level="ERROR",
                subprocess_id=subprocess_id,
                executable=executable_name,
                argument_count=len(arguments),
                duration_ms=(time.monotonic() - started) * 1000,
                exception_type=safe_exception_type(exc),
                failure_site="process_wait",
                **return_code,
            )
            raise
        stdout_thread.join()
        stderr_thread.join()
        if failure is None and (
            stdout_reader.exceeded.is_set() or stderr_reader.exceeded.is_set()
        ):
            failure = CommandOutputLimitExceeded(
                "subprocess output exceeded configured byte limit"
            )
        if failure is None:
            try:
                stdout_observer.drain(final=True)
                stderr_observer.drain(final=True)
            except BaseException as exc:
                self._emit(
                    "subprocess.failed",
                    level="ERROR",
                    subprocess_id=subprocess_id,
                    executable=executable_name,
                    argument_count=len(arguments),
                    return_code=process.returncode,
                    duration_ms=(time.monotonic() - started) * 1000,
                    exception_type=safe_exception_type(exc),
                    failure_site="output_observer",
                )
                raise
        if failure is not None:
            self._emit(
                "subprocess.failed",
                level="WARNING",
                subprocess_id=subprocess_id,
                executable=executable_name,
                argument_count=len(arguments),
                return_code=process.returncode,
                duration_ms=(time.monotonic() - started) * 1000,
                exception_type=safe_exception_type(failure),
                failure_site="process_wait",
            )
            raise failure
        result = CommandResult(
            returncode=process.returncode,
            stdout=stdout_reader.result(),
            stderr=stderr_reader.result(),
            duration_seconds=time.monotonic() - started,
        )
        self._emit(
            "subprocess.completed",
            subprocess_id=subprocess_id,
            executable=executable_name,
            argument_count=len(arguments),
            return_code=result.returncode,
            stdout_bytes=len(result.stdout),
            stderr_bytes=len(result.stderr),
            duration_ms=result.duration_seconds * 1000,
        )
        return result

    def _emit(self, event: str, *, level: str = "INFO", **fields: object) -> None:
        if self.runtime_logger is not None:
            self.runtime_logger.emit(event, level=level, **fields)

    def _validate_executable(self, path: Path) -> Path:
        if not path.is_absolute():
            raise SubprocessPolicyError("executable path must be absolute")
        if path.is_symlink():
            raise SubprocessPolicyError("executable symlinks are not accepted")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise SubprocessPolicyError("executable does not exist") from exc
        info = resolved.stat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        attributes = getattr(info, "st_file_attributes", 0)
        if not resolved.is_file() or (reparse and attributes & reparse):
            raise SubprocessPolicyError("executable must be a regular file")
        if self.allowed_executable_roots is not None and not any(
            resolved.is_relative_to(root) for root in self.allowed_executable_roots
        ):
            raise SubprocessPolicyError("executable is outside the allowed roots")
        return resolved

    @staticmethod
    def _validate_cwd(path: Path | None) -> Path | None:
        if path is None:
            return None
        if not path.is_absolute():
            raise SubprocessPolicyError("working directory must be absolute")
        if path.is_symlink():
            raise SubprocessPolicyError("working directory symlinks are not accepted")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise SubprocessPolicyError("working directory does not exist") from exc
        if not resolved.is_dir():
            raise SubprocessPolicyError("working directory must be a directory")
        return resolved

    @staticmethod
    def _validate_limits(spec: CommandSpec) -> None:
        if (
            isinstance(spec.timeout_seconds, bool)
            or not isinstance(spec.timeout_seconds, (int, float))
            or not math.isfinite(spec.timeout_seconds)
            or spec.timeout_seconds <= 0
        ):
            raise SubprocessPolicyError(
                "timeout_seconds must be a finite positive number"
            )
        output_limits = (spec.stdout_limit_bytes, spec.stderr_limit_bytes)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in output_limits
        ):
            raise SubprocessPolicyError("output limits must be non-negative integers")

    @staticmethod
    def _validate_observers(spec: CommandSpec) -> None:
        if any(
            observer is not None and not callable(observer)
            for observer in (
                spec.stdout_line_observer,
                spec.stderr_line_observer,
            )
        ):
            raise SubprocessPolicyError("output observers must be callable")

    @staticmethod
    def _validate_arguments(arguments: Sequence[str]) -> tuple[str, ...]:
        accepted: list[str] = []
        for argument in arguments:
            if not isinstance(argument, str):
                raise SubprocessPolicyError("all subprocess arguments must be strings")
            if "\x00" in argument:
                raise SubprocessPolicyError("subprocess arguments may not contain NUL")
            accepted.append(argument)
        return tuple(accepted)

    def _build_environment(
        self,
        executable: Path,
        requested: Mapping[str, str],
    ) -> dict[str, str]:
        environment = {
            key: value
            for key in self._HOST_ENVIRONMENT_KEYS
            if (value := os.environ.get(key)) is not None
        }
        environment["PATH"] = str(executable.parent)
        for key, value in requested.items():
            normalized = key.upper()
            if (
                normalized in self._HARD_DENIED_ENVIRONMENT_KEYS
                or normalized.endswith("_PROXY")
                or "COOKIE" in normalized
                or "AUTHORIZATION" in normalized
            ):
                raise SubprocessPolicyError(
                    f"environment key is forbidden at the runner boundary: {normalized}"
                )
            if normalized not in self.allowed_environment_keys:
                raise SubprocessPolicyError(
                    f"environment key is not allow-listed: {normalized}"
                )
            if (
                not self._ENVIRONMENT_NAME.fullmatch(key)
                or "\x00" in value
                or len(value) > 4096
            ):
                raise SubprocessPolicyError("invalid subprocess environment entry")
            environment[key] = value
        return environment

    @staticmethod
    def _poll_process(process: subprocess.Popen[bytes]) -> int | None:
        try:
            return process.poll()
        except Exception as exc:
            raise CommandProcessError(
                "subprocess status could not be inspected"
            ) from exc

    @classmethod
    def _terminate_process_tree(cls, process: subprocess.Popen[bytes]) -> None:
        try:
            cls._terminate_tree(process)
        except Exception as exc:
            raise CommandProcessError(
                "subprocess tree could not be terminated"
            ) from exc

    @staticmethod
    def _wait_for_process(process: subprocess.Popen[bytes]) -> None:
        try:
            process.wait()
        except Exception as exc:
            raise CommandProcessError(
                "subprocess completion could not be observed"
            ) from exc

    @classmethod
    def _best_effort_shutdown(cls, process: subprocess.Popen[bytes]) -> None:
        try:
            cls._terminate_tree(process)
        except BaseException:
            pass
        try:
            process.wait()
        except BaseException:
            pass

    @staticmethod
    def _best_effort_join(*threads: threading.Thread) -> None:
        for thread in threads:
            try:
                thread.join()
            except BaseException:
                pass

    @staticmethod
    def _terminate_tree(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            system_root = Path(os.environ.get("SystemRoot", r"C:\\Windows"))
            taskkill = system_root / "System32" / "taskkill.exe"
            if taskkill.is_file():
                subprocess.run(
                    [
                        str(taskkill),
                        "/PID",
                        str(process.pid),
                        "/T",
                        "/F",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            if process.poll() is None:
                process.kill()
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
