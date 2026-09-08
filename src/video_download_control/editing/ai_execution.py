"""Execute persisted transcription and translation tasks through the AI runtime.

This module is the narrow control-process seam between ``EditingService`` and
the optional, hash-verified companion runtime.  Persisted tasks cover
transcription and translation; speech providers are resolved only after an
approved translation timeline has been frozen into a render plan.
"""
from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from threading import Event, Lock
from typing import Any

from .ai import TranscriptionOptions
from .ai_bridge import (
    AiBridgeError,
    AiRuntimeBridge,
    RuntimeSpeechProvider,
    RuntimeTranscriptionProvider,
    RuntimeTranslationProvider,
    operation_data_egress,
)
from .ai_runtime import AiRuntimeError, inspect_ai_runtime
from .contracts import EditingError
from .service import EditingService
from .timeline import TimelineCue, TimelineError


ProgressCallback = Callable[[float, str], None]
CancelCallback = Callable[[], bool]
_TASK_OPERATIONS = frozenset({"transcribe", "translate"})
_SUPPORTED_OPERATIONS = _TASK_OPERATIONS | {"synthesize"}
_AUTH_ENVIRONMENT = re.compile(r"^OPEN_FLAME_AI_[A-Z0-9_]{1,63}$")


def _task_text(task: Mapping[str, Any], field: str) -> str:
    value = task.get(field)
    if not isinstance(value, str) or not value:
        raise AiBridgeError("ai_task_invalid")
    return value


def _task_options(task: Mapping[str, Any]) -> Mapping[str, Any]:
    value = task.get("options")
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise AiBridgeError("ai_task_invalid")
    return value


def _timeline_cues(value: Mapping[str, Any]) -> tuple[TimelineCue, ...]:
    raw_cues = value.get("cues")
    if not isinstance(raw_cues, list) or not raw_cues:
        raise AiBridgeError("ai_source_timeline_invalid")
    cues: list[TimelineCue] = []
    try:
        for raw in raw_cues:
            if not isinstance(raw, Mapping):
                raise AiBridgeError("ai_source_timeline_invalid")
            cues.append(
                TimelineCue(
                    id=str(raw["id"]),
                    order=int(raw["order"]),
                    start_ms=int(raw["start_ms"]),
                    end_ms=int(raw["end_ms"]),
                    source_text=str(raw["source_text"]),
                    source_language=str(raw["source_language"]),
                    speaker_id=(
                        None
                        if raw.get("speaker_id") is None
                        else str(raw["speaker_id"])
                    ),
                )
            )
    except AiBridgeError:
        raise
    except (KeyError, TypeError, ValueError, TimelineError) as exc:
        raise AiBridgeError("ai_source_timeline_invalid") from exc
    if [cue.order for cue in cues] != list(range(len(cues))):
        raise AiBridgeError("ai_source_timeline_invalid")
    return tuple(cues)


class AiTaskExecutor:
    """Lazily load one verified runtime and execute claimed AI tasks serially."""

    def __init__(self, runtime_root: Path, work_root: Path) -> None:
        self.runtime_root = Path(runtime_root)
        self.work_root = Path(work_root)
        self._bridge: AiRuntimeBridge | None = None
        self._bridge_lock = Lock()

    def _resolve_secret(self, provider_id: str) -> str | None:
        """Resolve only the verified provider's namespaced environment key."""

        bridge = self._bridge
        if bridge is None:
            return None
        provider = next(
            (item for item in bridge.runtime.providers if item.id == provider_id),
            None,
        )
        if (
            provider is None
            or provider.auth_env is None
            or not _AUTH_ENVIRONMENT.fullmatch(provider.auth_env)
        ):
            return None
        return os.environ.get(provider.auth_env)

    @staticmethod
    def _credential_code(provider: object) -> str | None:
        auth_env = getattr(provider, "auth_env", None)
        if auth_env is None:
            return None
        if not isinstance(auth_env, str) or not _AUTH_ENVIRONMENT.fullmatch(auth_env):
            return "ai_provider_auth_environment_invalid"
        secret = os.environ.get(auth_env)
        if (
            not isinstance(secret, str)
            or not secret
            or len(secret) > 16_384
            or any(ord(character) < 33 for character in secret)
        ):
            return "ai_provider_auth_missing"
        return None

    def _get_bridge(self) -> AiRuntimeBridge:
        with self._bridge_lock:
            if self._bridge is not None:
                return self._bridge
            try:
                bridge = AiRuntimeBridge(
                    self.runtime_root,
                    self.work_root,
                    secret_resolver=self._resolve_secret,
                )
            except AiBridgeError:
                raise
            except AiRuntimeError as exc:
                raise AiBridgeError(exc.code) from exc
            except (OSError, RuntimeError) as exc:
                raise AiBridgeError("ai_runtime_invalid") from exc
            self._bridge = bridge
            return bridge

    def runtime_status(self) -> dict[str, object]:
        """Report integrity separately from provider execution health.

        Reading and hashing a manifest does not prove that a provider can load
        its model or reach its remote service, so ``ready`` remains false until
        a separately authorised health/execution path has succeeded.
        """

        inspected = inspect_ai_runtime(self.runtime_root)
        manifest_verified = inspected.get("ready") is True
        return {
            "ready": False,
            "code": (
                "provider_health_required"
                if manifest_verified
                else str(inspected.get("code", "ai_runtime_invalid"))
            ),
            "integrity": "verified" if manifest_verified else "blocked",
            "provider_health": "unverified" if manifest_verified else "blocked",
            "runtime_id": inspected.get("runtime_id"),
            "runtime_version": inspected.get("runtime_version"),
            "protocol_schema": inspected.get("protocol_schema"),
            "platform": inspected.get("platform"),
            "providers": inspected.get("providers", []),
            "models": inspected.get("models", []),
        }

    def capabilities(self) -> list[dict[str, object]]:
        """Return only manifest-verified provider/model combinations.

        A listed combination is deliberately ``unverified`` rather than
        ``ready``.  The runtime verifies every required byte here, while the
        actual provider/model health is established only by execution.
        """

        try:
            bridge = self._get_bridge()
        except AiBridgeError as exc:
            return [
                {
                    "operation": "dub" if operation == "synthesize" else operation,
                    "label": (
                        "自动听写"
                        if operation == "transcribe"
                        else "自动翻译"
                        if operation == "translate"
                        else "自动 AI 配音"
                    ),
                    "status": "blocked",
                    "execution": "local",
                    "description": "隔离 AI runtime 当前不可用。",
                    "requirements": ["ai_runtime", "model"],
                    "provider_id": None,
                    "model_id": None,
                    "data_egress": [],
                    "reason_code": exc.code,
                }
                for operation in ("transcribe", "translate", "synthesize")
            ]

        values: list[dict[str, object]] = []
        failure_codes: dict[str, str] = {}
        for provider in bridge.runtime.providers:
            for operation in ("transcribe", "translate", "synthesize"):
                if operation not in provider.operations:
                    continue
                # Persisted AI tasks require an explicit model id.  Providers
                # without one remain runtime-internal until that contract is
                # extended; advertising them here would create an unusable UI.
                if not provider.model_ids:
                    continue
                model_ids: Sequence[str | None] = provider.model_ids
                for model_id in model_ids:
                    try:
                        verified_provider, _model = bridge.runtime.verify_for_operation(
                            provider.id, operation, model_id
                        )
                        data_egress = operation_data_egress(
                            verified_provider, operation
                        )
                    except (AiRuntimeError, AiBridgeError) as exc:
                        failure_codes.setdefault(operation, exc.code)
                        continue
                    credential_code = self._credential_code(verified_provider)
                    capability: dict[str, object] = {
                            "operation": (
                                "dub" if operation == "synthesize" else operation
                            ),
                            "label": (
                                "自动听写"
                                if operation == "transcribe"
                                else "自动翻译"
                                if operation == "translate"
                                else "自动 AI 配音"
                            ),
                            "status": (
                                "blocked" if credential_code else "unverified"
                            ),
                            "execution": (
                                "remote"
                                if provider.kind in {"http", "remote_plugin"}
                                else "local"
                            ),
                            "description": (
                                "运行时和模型完整性已验证；实际 provider 健康尚待任务执行验证。"
                            ),
                            "requirements": (
                                ["provider_secret", "provider_health"]
                                if credential_code
                                else ["provider_health"]
                            ),
                            "provider_id": provider.id,
                            "model_id": model_id,
                            "data_egress": list(data_egress),
                            "reason_code": (
                                credential_code or "provider_health_required"
                            ),
                        }
                    if operation == "synthesize":
                        configured_voices = provider.config.get(
                            "standard_voice_ids", []
                        )
                        if isinstance(configured_voices, list) and all(
                            isinstance(voice, str)
                            and re.fullmatch(
                                r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,159}", voice
                            )
                            for voice in configured_voices
                        ):
                            capability["voices"] = [
                                {
                                    "id": voice,
                                    "label": voice.capitalize(),
                                    "is_clone": False,
                                }
                                for voice in configured_voices
                            ]
                    values.append(capability)
        represented = {str(item["operation"]) for item in values}
        for operation in ("transcribe", "translate", "synthesize"):
            public_operation = "dub" if operation == "synthesize" else operation
            if public_operation in represented:
                continue
            values.append(
                {
                    "operation": public_operation,
                    "label": (
                        "自动听写"
                        if operation == "transcribe"
                        else "自动翻译"
                        if operation == "translate"
                        else "自动 AI 配音"
                    ),
                    "status": "blocked",
                    "execution": "local",
                    "description": "运行时中没有通过完整性验证的 provider/model 组合。",
                    "requirements": ["ai_runtime", "model"],
                    "provider_id": None,
                    "model_id": None,
                    "data_egress": [],
                    "reason_code": failure_codes.get(
                        operation, "ai_provider_operation_unsupported"
                    ),
                }
            )
        return values

    def verify_operation(
        self, operation: str, provider_id: str, model_id: str
    ) -> None:
        if operation not in _SUPPORTED_OPERATIONS:
            raise AiBridgeError("ai_operation_unsupported")
        try:
            provider, _model = self._get_bridge().runtime.verify_for_operation(
                provider_id, operation, model_id
            )
        except AiRuntimeError as exc:
            raise AiBridgeError(exc.code) from exc
        operation_data_egress(provider, operation)
        credential_code = self._credential_code(provider)
        if credential_code is not None:
            raise AiBridgeError(credential_code)

    def speech_provider(
        self, provider_id: str, model_id: str
    ) -> RuntimeSpeechProvider:
        self.verify_operation("synthesize", provider_id, model_id)
        return RuntimeSpeechProvider(self._get_bridge(), provider_id, model_id)

    def execute(
        self,
        service: EditingService,
        task: Mapping[str, Any],
        *,
        progress: ProgressCallback,
        cancelled: CancelCallback,
        cancel_event: Event | None = None,
    ) -> dict[str, Any]:
        """Execute and atomically persist one already-claimed task result."""

        operation = _task_text(task, "operation")
        if operation not in _TASK_OPERATIONS:
            raise AiBridgeError("ai_operation_unsupported")
        task_id = _task_text(task, "id")
        claim_token = _task_text(task, "claim_token")
        project_id = _task_text(task, "project_id")
        provider_id = _task_text(task, "provider")
        model_id = _task_text(task, "model")
        options = _task_options(task)
        bridge = self._get_bridge()

        try:
            if operation == "transcribe":
                if task.get("source_revision_id") is not None:
                    raise AiBridgeError("ai_task_invalid")
                if service.processor is None:
                    raise AiBridgeError("processor_not_configured")
                provider = RuntimeTranscriptionProvider(
                    bridge, provider_id, model_id
                )
                source, source_size, source_sha256 = service.project_source_identity(
                    project_id
                )
                with tempfile.TemporaryDirectory(
                    prefix=f"transcribe-{task_id[:8]}-",
                    dir=self.work_root,
                    ignore_cleanup_errors=True,
                ) as temporary:
                    clip_start_ms = options.get("clip_start_ms")
                    clip_end_ms = options.get("clip_end_ms")
                    media = service.processor.prepare_transcription_audio(
                        source,
                        Path(temporary) / "source-audio.m4a",
                        expected_source_size=source_size,
                        expected_source_sha256=source_sha256,
                        clip_start_ms=clip_start_ms,
                        clip_end_ms=clip_end_ms,
                        cancel_event=cancel_event,
                    )
                    result = provider.transcribe(
                        media,
                        TranscriptionOptions(
                            language=options.get("language"),
                            word_timestamps=options.get("word_timestamps", True),
                            vad=options.get("vad", True),
                        ),
                        progress=progress,
                        cancelled=cancelled,
                    )
                if clip_start_ms is not None:
                    assert isinstance(clip_end_ms, int)
                    duration_ms = clip_end_ms - clip_start_ms
                    bounded: list[TimelineCue] = []
                    for cue in result:
                        if cue.start_ms >= duration_ms:
                            continue
                        end_ms = min(cue.end_ms, duration_ms)
                        if end_ms <= cue.start_ms:
                            continue
                        bounded.append(
                            TimelineCue(
                                id=cue.id,
                                order=len(bounded),
                                start_ms=cue.start_ms + clip_start_ms,
                                end_ms=end_ms + clip_start_ms,
                                source_text=cue.source_text,
                                source_language=cue.source_language,
                                speaker_id=cue.speaker_id,
                            )
                        )
                    if not bounded:
                        raise AiBridgeError("ai_transcription_empty")
                    result = tuple(bounded)
                language = result[0].source_language if result else None
                return service.complete_ai_task(
                    task_id, claim_token, result, language=language
                )

            source_revision_id = _task_text(task, "source_revision_id")
            timeline = service.timeline(source_revision_id)
            cues = _timeline_cues(timeline)
            raw_glossary = options.get("glossary", [])
            if not isinstance(raw_glossary, list):
                raise AiBridgeError("ai_task_invalid")
            glossary: list[tuple[str, str]] = []
            for item in raw_glossary:
                if not isinstance(item, list) or len(item) != 2:
                    raise AiBridgeError("ai_task_invalid")
                glossary.append((str(item[0]), str(item[1])))
            source_language = _task_text(options, "source_language")
            target_language = _task_text(options, "target_language")
            provider = RuntimeTranslationProvider(bridge, provider_id, model_id)
            result = provider.translate(
                cues,
                source_language=source_language,
                target_language=target_language,
                glossary=glossary,
                progress=progress,
                cancelled=cancelled,
            )
            return service.complete_ai_task(task_id, claim_token, result)
        except AiBridgeError:
            raise
        except EditingError as exc:
            if exc.code in {"stale_ai_claim", "ai_task_not_found"}:
                raise
            if exc.code in {
                "processor_not_configured",
                "ai_transcription_audio_unavailable",
                "ai_transcription_media_too_large",
                "editing_storage_full",
                "editing_storage_unavailable",
                "source_asset_changed",
            }:
                raise AiBridgeError(exc.code) from exc
            raise AiBridgeError("ai_source_unavailable") from exc
        except (TimelineError, TypeError, ValueError) as exc:
            raise AiBridgeError("ai_worker_result_invalid") from exc


__all__ = ["AiTaskExecutor"]
