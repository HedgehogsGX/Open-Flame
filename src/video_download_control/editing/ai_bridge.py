"""Core-side bridge to one hash-verified optional AI runtime.

Only validated request/result objects and progress codes cross the process
boundary. Model libraries, provider SDKs, and model weights stay outside the
core environment.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from threading import Lock
from typing import Any
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

    @staticmethod
    def _source_identity(payload: Mapping[str, object]) -> tuple[Path, int, str] | None:
        if "media_path" not in payload:
            return None
        path = Path(str(payload["media_path"]))
        try:
            size, digest = file_sha256(path, maximum=MAX_MEDIA_BYTES)
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
            provider, _model = self.runtime.verify_for_operation(
                provider_id, operation, model_id
            )
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
            scratch = Path(
                tempfile.mkdtemp(prefix=f"ai-{request_id[:8]}-", dir=self.work_root)
            )
            result_path = scratch / "result.json"
            request_path = scratch / "request.json"
            output_path = scratch / "output.wav"
            source_identity: tuple[Path, int, str] | None = None
            try:
                request_payload = dict(request["payload"])
                if operation == "synthesize":
                    if audio_destination is None:
                        raise AiBridgeError("ai_audio_destination_required")
                    request_payload["output_path"] = str(output_path)
                    request = validate_request({**request, "payload": request_payload})
                elif audio_destination is not None:
                    raise AiBridgeError("ai_audio_destination_invalid")
                source_identity = self._source_identity(request_payload)
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
                except (SubprocessPolicyError, CommandProcessError) as exc:
                    raise AiBridgeError("ai_worker_unavailable") from exc
                except SubprocessExecutionError as exc:
                    raise AiBridgeError("ai_worker_failed") from exc
                if progress_reader.failure is not None:
                    raise progress_reader.failure
                self._verify_source_after(source_identity)
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
                return result_payload
            except AiBridgeError:
                raise
            except (AiProtocolError, AiRuntimeError) as exc:
                raise AiBridgeError(getattr(exc, "code", "ai_bridge_failed")) from exc
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
    ) -> None:
        self.bridge = bridge
        self.provider_id = provider_id
        self.model_id = model_id

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
    ) -> None:
        self.bridge = bridge
        self.provider_id = provider_id
        self.model_id = model_id

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
    ) -> None:
        self.bridge = bridge
        self.provider_id = provider_id
        self.model_id = model_id
        self._voices: tuple[Voice, ...] | None = None
        self._voices_lock = Lock()

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
        allowed = {
            voice.id
            for voice in self.voices(options.language)
            if not voice.is_clone
        }
        if options.voice_id not in allowed:
            raise AiBridgeError("ai_voice_not_allowed")
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
]
