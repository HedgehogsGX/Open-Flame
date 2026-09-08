"""Core-side bridge to one hash-verified optional AI runtime.

Only validated request/result objects and progress codes cross the process
boundary. Model libraries, provider SDKs, and model weights stay outside the
core environment.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from threading import Lock
from typing import Any, Protocol
from uuid import uuid4

from ..subprocess_runner import (
    CommandCancelled,
    CommandOutputLimitExceeded,
    CommandProcessError,
    CommandSpec,
    CommandTimedOut,
    SecureSubprocessRunner,
    SubprocessExecutionError,
    SubprocessPolicyError,
)
from .ai import (
    CancelCallback,
    ProgressCallback,
    ProviderCapability,
    SpeechClip,
    SpeechOptions,
    TranscriptionOptions,
    TranslationItem,
    TranslationRevision,
    Voice,
)
from .ai_authorization import (
    AiAuthorizationError,
    AiOperationAuthorization,
    build_operation_authorization,
    require_authorization_match,
)
from .ai_protocol import (
    MAX_PROGRESS_LINE_BYTES,
    MAX_PROGRESS_LINES,
    MAX_REQUEST_BYTES,
    MAX_RESULT_BYTES,
    PROTOCOL_SCHEMA,
    AiProtocolError,
    file_sha256,
    operation_data_egress as protocol_operation_data_egress,
    read_json_file,
    validate_progress,
    validate_request,
    validate_result,
    write_json_atomic,
)
from .ai_runtime import (
    AiRuntime,
    AiRuntimeError,
    RuntimeModel,
    RuntimeProvider,
    load_ai_runtime,
)
from .timeline import TimelineCue


MAX_AUDIO_BYTES = 512 * 1024**2
MAX_MEDIA_BYTES = 16 * 1024**3
SecretResolver = Callable[[str], str | None]
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")


class AiBridgeError(RuntimeError):
    """A stable bridge/provider failure that never embeds provider output."""

    def __init__(self, code: str):
        self.code = (
            code
            if isinstance(code, str) and _CODE.fullmatch(code)
            else "ai_bridge_failed"
        )
        super().__init__(self.code)


class RemoteInvocationLifecycle(Protocol):
    """Persist one already-reserved remote invocation without seeing payloads."""

    def dispatch(self) -> None: ...

    def respond(self) -> None: ...

    def release(self, code: str) -> None: ...

    def mark_unknown(self, code: str) -> None: ...


RemoteInvocationFactory = Callable[[str], RemoteInvocationLifecycle]


class _InvocationGuard:
    """Close every reserved invocation according to the last durable boundary."""

    def __init__(self, lifecycle: RemoteInvocationLifecycle) -> None:
        if any(
            not callable(getattr(lifecycle, name, None))
            for name in ("dispatch", "respond", "release", "mark_unknown")
        ):
            raise AiBridgeError("ai_remote_ledger_invalid")
        self.lifecycle = lifecycle
        self.state = "reserved"

    @staticmethod
    def _call(lifecycle: RemoteInvocationLifecycle, name: str, *args: str) -> None:
        try:
            getattr(lifecycle, name)(*args)
        except AiBridgeError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", None)
            if not isinstance(code, str) or not _CODE.fullmatch(code):
                code = "ai_remote_ledger_unavailable"
            raise AiBridgeError(code) from exc

    def dispatch(self) -> None:
        if self.state != "reserved":
            raise AiBridgeError("ai_remote_ledger_conflict")
        self._call(self.lifecycle, "dispatch")
        self.state = "dispatched"

    def respond(self) -> None:
        if self.state != "dispatched":
            raise AiBridgeError("ai_remote_ledger_conflict")
        self._call(self.lifecycle, "respond")
        self.state = "responded"

    def release(self, code: str) -> None:
        if self.state == "reserved":
            self._call(self.lifecycle, "release", code)
            self.state = "released"

    def fail(self, code: str) -> str:
        if self.state == "reserved":
            self.release(code)
        elif self.state == "dispatched":
            self._call(self.lifecycle, "mark_unknown", code)
            self.state = "unknown"
        return self.state


def _request_fingerprint(request: Mapping[str, object]) -> str:
    """Hash the validated provider request without volatile local paths."""

    payload = request.get("payload")
    if not isinstance(payload, Mapping):
        raise AiBridgeError("ai_request_invalid")
    stable_payload = dict(payload)
    stable_payload.pop("media_path", None)
    stable_payload.pop("output_path", None)
    try:
        encoded = json.dumps(
            {
                "schema": request.get("schema"),
                "operation": request.get("operation"),
                "provider_id": request.get("provider_id"),
                "model_id": request.get("model_id"),
                "payload": stable_payload,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, UnicodeError) as exc:
        raise AiBridgeError("ai_request_invalid") from exc
    return hashlib.sha256(encoded).hexdigest()


def operation_data_egress(
    provider: RuntimeProvider, operation: str
) -> tuple[str, ...]:
    """Describe the user payload actually sent by this bridge operation."""

    if provider.kind not in {"http", "remote_plugin"}:
        return ()
    try:
        required = protocol_operation_data_egress(operation)
    except AiProtocolError as exc:
        raise AiBridgeError(exc.code) from exc
    if not set(required).issubset(provider.data_egress):
        raise AiBridgeError("ai_runtime_invalid")
    return required


def _plain_directory(path: Path, *, create: bool = False) -> Path:
    path = Path(os.path.abspath(path))
    try:
        if create:
            path.mkdir(parents=True, exist_ok=True)
        info = path.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & reparse
            or path.resolve(strict=True) != path
        ):
            raise AiBridgeError("ai_work_root_invalid")
    except AiBridgeError:
        raise
    except (OSError, RuntimeError) as exc:
        raise AiBridgeError("ai_work_root_invalid") from exc
    return path


def _remove_tree(path: Path, root: Path) -> None:
    """Best-effort cleanup without following child links or reparse points."""

    try:
        canonical_root = Path(os.path.abspath(root))
        candidate = Path(os.path.abspath(path))
        if candidate.parent != canonical_root:
            return
        info = candidate.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & reparse:
            candidate.unlink(missing_ok=True)
            return
        if not stat.S_ISDIR(info.st_mode):
            candidate.unlink(missing_ok=True)
            return
        for entry in os.scandir(candidate):
            child = Path(entry.path)
            child_info = child.lstat()
            if (
                stat.S_ISDIR(child_info.st_mode)
                and not stat.S_ISLNK(child_info.st_mode)
                and not getattr(child_info, "st_file_attributes", 0) & reparse
            ):
                _remove_tree_contents(child)
                child.rmdir()
            else:
                child.unlink(missing_ok=True)
        candidate.rmdir()
    except OSError:
        pass


def _remove_tree_contents(path: Path) -> None:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    for entry in os.scandir(path):
        child = Path(entry.path)
        try:
            info = child.lstat()
            if (
                stat.S_ISDIR(info.st_mode)
                and not stat.S_ISLNK(info.st_mode)
                and not getattr(info, "st_file_attributes", 0) & reparse
            ):
                _remove_tree_contents(child)
                child.rmdir()
            else:
                child.unlink(missing_ok=True)
        except OSError:
            continue


def _copy_verified_audio(
    source: Path,
    destination: Path,
    *,
    expected_size: int,
    expected_sha256: str,
) -> tuple[int, str]:
    destination = Path(destination)
    if not destination.is_absolute() or destination.suffix.lower() != ".wav":
        raise AiBridgeError("ai_audio_destination_invalid")
    parent = _plain_directory(destination.parent)
    if destination.exists() or destination.is_symlink():
        raise AiBridgeError("ai_audio_destination_exists")
    try:
        source_size, source_digest = file_sha256(source, maximum=MAX_AUDIO_BYTES)
    except AiProtocolError as exc:
        raise AiBridgeError("ai_audio_output_invalid") from exc
    if source_size != expected_size or source_digest != expected_sha256:
        raise AiBridgeError("ai_audio_output_changed")
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, raw = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".part", dir=parent
        )
        temporary = Path(raw)
        with os.fdopen(descriptor, "wb") as target, source.open("rb") as origin:
            descriptor = None
            shutil.copyfileobj(origin, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        copied_size, copied_digest = file_sha256(temporary, maximum=MAX_AUDIO_BYTES)
        if copied_size != expected_size or copied_digest != expected_sha256:
            raise AiBridgeError("ai_audio_output_changed")
        os.link(temporary, destination)
        return copied_size, copied_digest
    except FileExistsError:
        raise AiBridgeError("ai_audio_destination_exists") from None
    except AiBridgeError:
        raise
    except (OSError, AiProtocolError) as exc:
        raise AiBridgeError("ai_audio_destination_unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _copy_verified_input(
    source: Path,
    destination: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    maximum: int,
) -> None:
    """Freeze one already verified provider input inside the private scratch dir."""

    descriptor: int | None = None
    valid = False
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        with source.open("rb") as origin, os.fdopen(descriptor, "wb") as target:
            descriptor = None
            shutil.copyfileobj(origin, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        copied_size, copied_digest = file_sha256(destination, maximum=maximum)
        if copied_size != expected_size or copied_digest != expected_sha256:
            raise AiBridgeError("ai_source_changed")
        valid = True
    except AiBridgeError:
        raise
    except (OSError, AiProtocolError) as exc:
        raise AiBridgeError("ai_source_unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not valid:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass


class _ProgressReader:
    def __init__(
        self,
        request_id: str,
        callback: ProgressCallback,
    ) -> None:
        self.request_id = request_id
        self.callback = callback
        self.count = 0
        self.fraction = 0.0
        self.failure: AiBridgeError | None = None

    def __call__(self, line: bytes) -> None:
        if self.failure is not None:
            raise self.failure
        try:
            if len(line) > MAX_PROGRESS_LINE_BYTES or self.count >= MAX_PROGRESS_LINES:
                raise AiBridgeError("ai_worker_output_limit")
            value = json.loads(line.decode("utf-8"))
            progress = validate_progress(value, expected_request_id=self.request_id)
            fraction = float(progress["fraction"])
            if fraction < self.fraction:
                raise AiBridgeError("ai_progress_invalid")
            self.callback(fraction, str(progress["code"]))
            self.count += 1
            self.fraction = fraction
        except AiBridgeError as exc:
            self.failure = exc
            raise
        except (UnicodeError, json.JSONDecodeError, AiProtocolError) as exc:
            self.failure = AiBridgeError("ai_progress_invalid")
            raise self.failure from exc
        except Exception as exc:
            self.failure = AiBridgeError("ai_progress_callback_failed")
            raise self.failure from exc


class AiRuntimeBridge:
    """Run one operation at a time through a verified companion runtime."""

    def __init__(
        self,
        runtime_root: Path,
        work_root: Path,
        *,
        secret_resolver: SecretResolver | None = None,
        runner: SecureSubprocessRunner | None = None,
    ) -> None:
        self.runtime: AiRuntime = load_ai_runtime(runtime_root)
        self.work_root = _plain_directory(work_root, create=True)
        self.secret_resolver = secret_resolver
        auth_keys = frozenset(
            provider.auth_env
            for provider in self.runtime.providers
            if provider.auth_env is not None
        )
        self.runner = runner or SecureSubprocessRunner(
            allowed_executable_roots=(self.runtime.root,),
            allowed_environment_keys=auth_keys,
        )
        self._run_lock = Lock()

    def _environment(self, provider: RuntimeProvider) -> dict[str, str]:
        if provider.auth_env is None:
            return {}
        if self.secret_resolver is None:
            raise AiBridgeError("ai_provider_auth_missing")
        try:
            secret = self.secret_resolver(provider.id)
        except Exception as exc:
            raise AiBridgeError("ai_provider_auth_unavailable") from exc
        if (
            not isinstance(secret, str)
            or not secret
            or len(secret) > 16_384
            or any(ord(character) < 33 for character in secret)
        ):
            raise AiBridgeError("ai_provider_auth_missing")
        return {provider.auth_env: secret}

    def require_operation_authorization(
        self,
        operation: str,
        provider_id: str,
        model_id: str | None,
        expected_authorization: object,
    ) -> AiOperationAuthorization:
        """Re-hash one operation and match the exact persisted authorization.

        This check does not resolve credentials.  Callers can therefore place
        it immediately before provider setup without exposing a secret when a
        legacy or changed binding must be rejected.
        """

        try:
            provider, model = self.runtime.verify_for_operation(
                provider_id, operation, model_id
            )
            data_egress = operation_data_egress(provider, operation)
            current = build_operation_authorization(
                self.runtime,
                provider,
                model,
                operation,
                data_egress,
            )
            return require_authorization_match(expected_authorization, current)
        except AiAuthorizationError as exc:
            raise AiBridgeError(exc.code) from exc
        except AiRuntimeError as exc:
            raise AiBridgeError(exc.code) from exc

    @staticmethod
    def _source_identity(
        payload: Mapping[str, object], *, maximum: int = MAX_MEDIA_BYTES
    ) -> tuple[Path, int, str] | None:
        if "media_path" not in payload:
            return None
        path = Path(str(payload["media_path"]))
        try:
            size, digest = file_sha256(path, maximum=maximum)
        except AiProtocolError as exc:
            raise AiBridgeError("ai_source_unavailable") from exc
        if size != payload.get("media_size") or digest != payload.get("media_sha256"):
            raise AiBridgeError("ai_source_changed")
        return path, size, digest

    @staticmethod
    def _verify_source_after(identity: tuple[Path, int, str] | None) -> None:
        if identity is None:
            return
        path, expected_size, expected_digest = identity
        try:
            size, digest = file_sha256(path, maximum=MAX_MEDIA_BYTES)
        except AiProtocolError as exc:
            raise AiBridgeError("ai_source_changed") from exc
        if size != expected_size or digest != expected_digest:
            raise AiBridgeError("ai_source_changed")

    def run(
        self,
        *,
        operation: str,
        provider_id: str,
        model_id: str | None,
        payload: Mapping[str, object],
        progress: ProgressCallback,
        cancelled: CancelCallback,
        audio_destination: Path | None = None,
        expected_authorization: object = None,
        invocation_factory: RemoteInvocationFactory | None = None,
    ) -> dict[str, object]:
        """Run a validated operation and return its validated payload only."""

        if not callable(progress) or not callable(cancelled):
            raise AiBridgeError("ai_callback_invalid")
        request_id = uuid4().hex
        try:
            request = validate_request(
                {
                    "schema": PROTOCOL_SCHEMA,
                    "request_id": request_id,
                    "operation": operation,
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "payload": dict(payload),
                }
            )
            # Structural request validation can use the immutable in-memory
            # manifest.  The full artifact re-hash is performed at the final
            # authorization gate inside the run lock below.
            provider = self.runtime.provider(provider_id, operation)
            self.runtime.model(provider, model_id, operation)
            operation_data_egress(provider, operation)
        except AiProtocolError as exc:
            raise AiBridgeError(exc.code) from exc
        except AiRuntimeError as exc:
            raise AiBridgeError(exc.code) from exc
        if cancelled():
            raise AiBridgeError("ai_operation_canceled")
        with self._run_lock:
            if cancelled():
                raise AiBridgeError("ai_operation_canceled")
            authorization: AiOperationAuthorization | None = None
            if operation != "health":
                # This is the final core-side gate before a credential is
                # resolved or the isolated provider process can be started.
                authorization = self.require_operation_authorization(
                    operation,
                    provider_id,
                    model_id,
                    expected_authorization,
                )
                provider = self.runtime.provider(provider_id, operation)
            else:
                try:
                    provider, _model = self.runtime.verify_for_operation(
                        provider_id, operation, model_id
                    )
                except AiRuntimeError as exc:
                    raise AiBridgeError(exc.code) from exc
            scratch = Path(
                tempfile.mkdtemp(prefix=f"ai-{request_id[:8]}-", dir=self.work_root)
            )
            result_path = scratch / "result.json"
            request_path = scratch / "request.json"
            output_path = scratch / "output.wav"
            source_identity: tuple[Path, int, str] | None = None
            provider_input_identity: tuple[Path, int, str] | None = None
            invocation: _InvocationGuard | None = None
            try:
                request_payload = dict(request["payload"])
                if operation == "synthesize":
                    if audio_destination is None:
                        raise AiBridgeError("ai_audio_destination_required")
                    request_payload["output_path"] = str(output_path)
                    request = validate_request({**request, "payload": request_payload})
                elif audio_destination is not None:
                    raise AiBridgeError("ai_audio_destination_invalid")
                source_maximum = (
                    int(authorization.limits["max_audio_bytes"])
                    if operation == "transcribe" and authorization is not None
                    else MAX_MEDIA_BYTES
                )
                source_identity = self._source_identity(
                    request_payload,
                    maximum=source_maximum,
                )
                if operation == "transcribe" and source_identity is not None:
                    source_path, source_size, source_digest = source_identity
                    stable_input = scratch / "input.m4a"
                    _copy_verified_input(
                        source_path,
                        stable_input,
                        expected_size=source_size,
                        expected_sha256=source_digest,
                        maximum=source_maximum,
                    )
                    request_payload["media_path"] = str(stable_input)
                    provider_input_identity = (
                        stable_input,
                        source_size,
                        source_digest,
                    )
                    request = validate_request({**request, "payload": request_payload})
                request_fingerprint = _request_fingerprint(request)
                write_json_atomic(request_path, request, maximum=MAX_REQUEST_BYTES)
                progress_reader = _ProgressReader(request_id, progress)
                command = CommandSpec(
                    executable=self.runtime.python,
                    arguments=(
                        "-I",
                        "-B",
                        "-X",
                        f"pycache_prefix={scratch / 'pycache'}",
                        str(self.runtime.worker),
                        "--protocol",
                        str(self.runtime.protocol),
                        "--manifest",
                        str(self.runtime.root / "manifest.json"),
                        "--request",
                        str(request_path),
                        "--result",
                        str(result_path),
                    ),
                    cwd=scratch,
                    environment=self._environment(provider),
                    timeout_seconds=provider.timeout_seconds,
                    stdout_limit_bytes=(
                        MAX_PROGRESS_LINES * (MAX_PROGRESS_LINE_BYTES + 1)
                    ),
                    stderr_limit_bytes=64 * 1024,
                    stdout_line_observer=progress_reader,
                )
                # Scratch preparation and bounded input copying can take time.
                # Re-hash the exact runtime binding again immediately before
                # handing executable paths to the subprocess runner.
                if operation != "health":
                    self.require_operation_authorization(
                        operation,
                        provider_id,
                        model_id,
                        expected_authorization,
                    )
                else:
                    try:
                        self.runtime.verify_for_operation(
                            provider_id, operation, model_id
                        )
                    except AiRuntimeError as exc:
                        raise AiBridgeError(exc.code) from exc
                if operation != "health" and authorization is not None:
                    if authorization.execution == "remote":
                        if not callable(invocation_factory):
                            raise AiBridgeError("ai_remote_ledger_required")
                        try:
                            invocation = _InvocationGuard(
                                invocation_factory(request_fingerprint)
                            )
                        except AiBridgeError:
                            raise
                        except Exception as exc:
                            code = getattr(exc, "code", None)
                            if not isinstance(code, str) or not _CODE.fullmatch(code):
                                code = "ai_remote_ledger_unavailable"
                            raise AiBridgeError(code) from exc
                    elif invocation_factory is not None:
                        raise AiBridgeError("ai_remote_ledger_invalid")
                elif invocation_factory is not None:
                    raise AiBridgeError("ai_remote_ledger_invalid")
                if cancelled():
                    raise AiBridgeError("ai_operation_canceled")
                if invocation is not None:
                    invocation.dispatch()
                try:
                    command_result = self.runner.run(
                        command,
                        is_cancelled=cancelled,
                    )
                except CommandCancelled as exc:
                    raise AiBridgeError("ai_operation_canceled") from exc
                except CommandTimedOut as exc:
                    raise AiBridgeError("ai_operation_timeout") from exc
                except CommandOutputLimitExceeded as exc:
                    raise AiBridgeError("ai_worker_output_limit") from exc
                except SubprocessPolicyError as exc:
                    raise AiBridgeError("ai_worker_unavailable") from exc
                except CommandProcessError as exc:
                    raise AiBridgeError("ai_worker_unavailable") from exc
                except SubprocessExecutionError as exc:
                    raise AiBridgeError("ai_worker_failed") from exc
                if progress_reader.failure is not None:
                    raise progress_reader.failure
                self._verify_source_after(source_identity)
                self._verify_source_after(provider_input_identity)
                if operation != "health":
                    self.require_operation_authorization(
                        operation,
                        provider_id,
                        model_id,
                        expected_authorization,
                    )
                else:
                    try:
                        self.runtime.verify_for_operation(
                            provider_id, operation, model_id
                        )
                    except AiRuntimeError as exc:
                        raise AiBridgeError(exc.code) from exc
                try:
                    result = validate_result(
                        read_json_file(result_path, maximum=MAX_RESULT_BYTES),
                        expected_request_id=request_id,
                        expected_operation=operation,
                    )
                except AiProtocolError as exc:
                    raise AiBridgeError("ai_worker_result_invalid") from exc
                if (
                    result["provider_id"] != provider_id
                    or result["model_id"] != model_id
                ):
                    raise AiBridgeError("ai_worker_result_mismatch")
                if result["status"] == "error":
                    raise AiBridgeError(str(result["code"]))
                if command_result.returncode != 0:
                    raise AiBridgeError("ai_worker_failed")
                result_payload = dict(result["payload"])
                if operation == "health":
                    runtime = result_payload["runtime"]
                    if (
                        not isinstance(runtime, Mapping)
                        or runtime["implementation"]
                        != self.runtime.python_identity["implementation"]
                        or tuple(runtime["version"])
                        != tuple(self.runtime.python_identity["version"])
                        or runtime["bits"] != self.runtime.python_identity["bits"]
                    ):
                        raise AiBridgeError("ai_runtime_identity_mismatch")
                if operation == "translate":
                    expected_ids = [
                        item["id"] for item in request_payload["cues"]  # type: ignore[index]
                    ]
                    received_ids = [
                        item["segment_id"] for item in result_payload["items"]  # type: ignore[index]
                    ]
                    if received_ids != expected_ids:
                        raise AiBridgeError("ai_translation_alignment_invalid")
                if operation == "synthesize":
                    audio = result_payload["audio"]
                    if not isinstance(audio, Mapping) or Path(str(audio["path"])) != output_path:
                        raise AiBridgeError("ai_audio_output_invalid")
                    assert audio_destination is not None
                    size, digest = _copy_verified_audio(
                        output_path,
                        audio_destination,
                        expected_size=int(audio["size"]),
                        expected_sha256=str(audio["sha256"]),
                    )
                    audio_value = dict(audio)
                    audio_value.update(
                        {"path": str(audio_destination), "size": size, "sha256": digest}
                    )
                    result_payload["audio"] = audio_value
                if invocation is not None:
                    invocation.respond()
                return result_payload
            except AiBridgeError as exc:
                if invocation is not None:
                    try:
                        final_state = invocation.fail(exc.code)
                    except AiBridgeError as ledger_error:
                        raise ledger_error from exc
                    if final_state == "unknown":
                        raise AiBridgeError("ai_remote_result_unknown") from exc
                raise
            except (AiProtocolError, AiRuntimeError) as exc:
                code = getattr(exc, "code", "ai_bridge_failed")
                if invocation is not None:
                    try:
                        final_state = invocation.fail(code)
                    except AiBridgeError as ledger_error:
                        raise ledger_error from exc
                    if final_state == "unknown":
                        raise AiBridgeError("ai_remote_result_unknown") from exc
                raise AiBridgeError(code) from exc
            except BaseException as exc:
                if invocation is not None:
                    try:
                        final_state = invocation.fail("ai_bridge_failed")
                    except AiBridgeError as ledger_error:
                        raise ledger_error from exc
                    if final_state == "unknown":
                        raise AiBridgeError("ai_remote_result_unknown") from exc
                raise
            finally:
                _remove_tree(scratch, self.work_root)

    def health(
        self,
        provider_id: str,
        model_id: str | None = None,
        *,
        progress: ProgressCallback = lambda _fraction, _code: None,
        cancelled: CancelCallback = lambda: False,
    ) -> dict[str, object]:
        return self.run(
            operation="health",
            provider_id=provider_id,
            model_id=model_id,
            payload={},
            progress=progress,
            cancelled=cancelled,
        )


def _cue_value(cue: TimelineCue) -> dict[str, object]:
    return {
        "id": cue.id,
        "order": cue.order,
        "start_ms": cue.start_ms,
        "end_ms": cue.end_ms,
        "source_text": cue.source_text,
        "source_language": cue.source_language,
        "speaker_id": cue.speaker_id,
    }


class RuntimeTranscriptionProvider:
    def __init__(
        self,
        bridge: AiRuntimeBridge,
        provider_id: str,
        model_id: str | None,
        expected_authorization: object = None,
        invocation_factory: RemoteInvocationFactory | None = None,
    ) -> None:
        self.bridge = bridge
        self.provider_id = provider_id
        self.model_id = model_id
        self.expected_authorization = expected_authorization
        self.invocation_factory = invocation_factory

    def capability(self) -> ProviderCapability:
        provider = self.bridge.runtime.provider(self.provider_id, "transcribe")
        return ProviderCapability(
            operation="transcribe",
            label="自动听写",
            status="unverified",
            execution=(
                "remote"
                if provider.kind in {"http", "remote_plugin"}
                else "local"
            ),
            description="通过隔离 AI runtime 生成严格时间轴。",
            provider_id=provider.id,
            model_id=self.model_id,
            data_egress=operation_data_egress(provider, "transcribe"),
            requirements=("provider_health",),
            reason_code="provider_health_required",
        )

    def transcribe(
        self,
        media: Path,
        options: TranscriptionOptions,
        *,
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> tuple[TimelineCue, ...]:
        try:
            size, digest = file_sha256(Path(media), maximum=MAX_MEDIA_BYTES)
        except AiProtocolError as exc:
            raise AiBridgeError("ai_source_unavailable") from exc
        payload = self.bridge.run(
            operation="transcribe",
            provider_id=self.provider_id,
            model_id=self.model_id,
            payload={
                "media_path": str(Path(media).absolute()),
                "media_size": size,
                "media_sha256": digest,
                "options": {
                    "language": options.language,
                    "word_timestamps": options.word_timestamps,
                    "vad": options.vad,
                },
            },
            progress=progress,
            cancelled=cancelled,
            expected_authorization=self.expected_authorization,
            invocation_factory=self.invocation_factory,
        )
        language = str(payload["language"])
        return tuple(
            TimelineCue(
                id=str(item["id"]),
                order=int(item["order"]),
                start_ms=int(item["start_ms"]),
                end_ms=int(item["end_ms"]),
                source_text=str(item["source_text"]),
                source_language=language,
                speaker_id=(
                    None if item["speaker_id"] is None else str(item["speaker_id"])
                ),
            )
            for item in payload["cues"]  # type: ignore[union-attr]
        )


class RuntimeTranslationProvider:
    def __init__(
        self,
        bridge: AiRuntimeBridge,
        provider_id: str,
        model_id: str | None,
        expected_authorization: object = None,
        invocation_factory: RemoteInvocationFactory | None = None,
    ) -> None:
        self.bridge = bridge
        self.provider_id = provider_id
        self.model_id = model_id
        self.expected_authorization = expected_authorization
        self.invocation_factory = invocation_factory

    def capability(self) -> ProviderCapability:
        provider = self.bridge.runtime.provider(self.provider_id, "translate")
        return ProviderCapability(
            operation="translate",
            label="自动翻译",
            status="unverified",
            execution=(
                "remote"
                if provider.kind in {"http", "remote_plugin"}
                else "local"
            ),
            description="通过隔离 AI runtime 翻译并保持字幕段落对齐。",
            provider_id=provider.id,
            model_id=self.model_id,
            data_egress=operation_data_egress(provider, "translate"),
            requirements=("provider_health",),
            reason_code="provider_health_required",
        )

    def translate(
        self,
        cues: Sequence[TimelineCue],
        *,
        source_language: str,
        target_language: str,
        glossary: Sequence[tuple[str, str]],
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> TranslationRevision:
        payload = self.bridge.run(
            operation="translate",
            provider_id=self.provider_id,
            model_id=self.model_id,
            payload={
                "cues": [_cue_value(cue) for cue in cues],
                "source_language": source_language,
                "target_language": target_language,
                "glossary": [
                    {"source": source, "target": target}
                    for source, target in glossary
                ],
            },
            progress=progress,
            cancelled=cancelled,
            expected_authorization=self.expected_authorization,
            invocation_factory=self.invocation_factory,
        )
        revision = TranslationRevision(
            source_language=str(payload["source_language"]),
            target_language=str(payload["target_language"]),
            provider_id=self.provider_id,
            model_id=self.model_id or "",
            items=tuple(
                TranslationItem(
                    segment_id=str(item["segment_id"]),
                    target_text=str(item["target_text"]),
                )
                for item in payload["items"]  # type: ignore[union-attr]
            ),
        )
        revision.validate_against(cues)
        return revision


class RuntimeSpeechProvider:
    def __init__(
        self,
        bridge: AiRuntimeBridge,
        provider_id: str,
        model_id: str | None,
        expected_authorization: object = None,
        invocation_factory: Callable[[int], RemoteInvocationFactory] | None = None,
    ) -> None:
        self.bridge = bridge
        self.provider_id = provider_id
        self.model_id = model_id
        self.expected_authorization = expected_authorization
        self.invocation_factory = invocation_factory
        self._voices: tuple[Voice, ...] | None = None
        self._voices_lock = Lock()

    @property
    def authorization(self) -> AiOperationAuthorization:
        """Return the still-current authorization bound to this provider."""

        return self.bridge.require_operation_authorization(
            "synthesize",
            self.provider_id,
            self.model_id,
            self.expected_authorization,
        )

    def capability(self) -> ProviderCapability:
        provider = self.bridge.runtime.provider(self.provider_id, "synthesize")
        return ProviderCapability(
            operation="dub",
            label="自动 AI 配音",
            status="unverified",
            execution=(
                "remote"
                if provider.kind in {"http", "remote_plugin"}
                else "local"
            ),
            description="通过隔离 AI runtime 生成标准音色 WAV。",
            provider_id=provider.id,
            model_id=self.model_id,
            data_egress=operation_data_egress(provider, "synthesize"),
            requirements=("provider_health", "standard_voice_review"),
            reason_code="provider_health_required",
        )

    def voices(self, language: str) -> tuple[Voice, ...]:
        with self._voices_lock:
            if self._voices is None:
                # ``health`` may load a plugin or contact a remote provider.
                # Recheck the synthesis binding before that provider-specific
                # work begins. Cached metadata makes no provider call and does
                # not need another full artifact hash for every cue.
                self.authorization
                health = self.bridge.health(self.provider_id, self.model_id)
                self._voices = tuple(
                    Voice(
                        id=str(item["id"]),
                        label=str(item["label"]),
                        languages=tuple(str(value) for value in item["languages"]),
                        is_clone=bool(item["is_clone"]),
                    )
                    for item in health["voices"]  # type: ignore[union-attr]
                )
            return tuple(
                voice
                for voice in self._voices
                if language.casefold()
                in {value.casefold() for value in voice.languages}
            )

    def synthesize(
        self,
        text: str,
        output: Path,
        options: SpeechOptions,
        *,
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> SpeechClip:
        return self._synthesize(
            text,
            output,
            options,
            progress=progress,
            cancelled=cancelled,
            ordinal=None,
        )

    def synthesize_ledgered(
        self,
        text: str,
        output: Path,
        options: SpeechOptions,
        *,
        ordinal: int,
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> SpeechClip:
        """Synthesize one cue bound to its immutable original timeline ordinal."""

        if (
            isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or not 0 <= ordinal <= 999_999
        ):
            raise AiBridgeError("ai_remote_ledger_invalid")
        return self._synthesize(
            text,
            output,
            options,
            progress=progress,
            cancelled=cancelled,
            ordinal=ordinal,
        )

    def _synthesize(
        self,
        text: str,
        output: Path,
        options: SpeechOptions,
        *,
        progress: ProgressCallback,
        cancelled: CancelCallback,
        ordinal: int | None,
    ) -> SpeechClip:
        allowed = {
            voice.id
            for voice in self.voices(options.language)
            if not voice.is_clone
        }
        if options.voice_id not in allowed:
            raise AiBridgeError("ai_voice_not_allowed")
        invocation_factory = None
        if ordinal is not None and self.invocation_factory is not None:
            if not callable(self.invocation_factory):
                raise AiBridgeError("ai_remote_ledger_invalid")
            invocation_factory = self.invocation_factory(ordinal)
            if not callable(invocation_factory):
                raise AiBridgeError("ai_remote_ledger_invalid")
        payload = self.bridge.run(
            operation="synthesize",
            provider_id=self.provider_id,
            model_id=self.model_id,
            payload={
                "text": text,
                "output_path": str(Path(output).absolute()),
                "options": {
                    "voice_id": options.voice_id,
                    "language": options.language,
                    "rate": options.rate,
                    "style": options.style,
                },
            },
            progress=progress,
            cancelled=cancelled,
            audio_destination=Path(output),
            expected_authorization=self.expected_authorization,
            invocation_factory=invocation_factory,
        )
        audio = payload["audio"]
        return SpeechClip(
            path=Path(str(audio["path"])),  # type: ignore[index]
            duration_ms=int(audio["duration_ms"]),  # type: ignore[index]
            sample_rate=int(audio["sample_rate"]),  # type: ignore[index]
            channels=int(audio["channels"]),  # type: ignore[index]
            provider_id=self.provider_id,
            model_id=self.model_id or "",
        )


__all__ = [
    "AiBridgeError",
    "AiRuntimeBridge",
    "RuntimeSpeechProvider",
    "RuntimeTranscriptionProvider",
    "RuntimeTranslationProvider",
    "RemoteInvocationFactory",
    "RemoteInvocationLifecycle",
]
