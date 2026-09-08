"""Isolated AI worker entrypoint.

The worker is copied into the optional runtime and executed in isolated Python
mode. Heavy provider modules are imported only after the request and runtime
provider selection pass the dependency-free protocol checks. Provider stdout
and stderr are discarded; stdout is reserved for bounded progress JSON Lines.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib
import importlib.util
import json
import os
import platform
import struct
import sys
import urllib.error
import urllib.request
import wave
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any, Callable, Mapping


_MAX_HTTP_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_HTTP_MEDIA_BYTES = 32 * 1024 * 1024
_MAX_AUDIO_BYTES = 512 * 1024**2
_PROVIDER_FIELDS = {
    "id",
    "kind",
    "operations",
    "model_ids",
    "entrypoint",
    "endpoint",
    "auth_env",
    "timeout_seconds",
    "data_egress",
    "artifact_paths",
    "config",
}
_MANIFEST_FIELDS = {
    "schema",
    "protocol_schema",
    "runtime_id",
    "runtime_version",
    "platform",
    "python",
    "worker",
    "protocol",
    "providers",
    "models",
    "artifacts",
}


def _arguments(argv: list[str]) -> dict[str, Path]:
    if len(argv) != 8:
        raise ValueError
    expected = ("--protocol", "--manifest", "--request", "--result")
    result: dict[str, Path] = {}
    for index, flag in enumerate(expected):
        offset = index * 2
        if argv[offset] != flag:
            raise ValueError
        value = Path(argv[offset + 1])
        if not value.is_absolute():
            raise ValueError
        result[flag[2:]] = value
    if len(set(result.values())) != len(result):
        raise ValueError
    return result


def _load_protocol(path: Path) -> ModuleType:
    if path.name != "ai_protocol.py":
        raise ValueError
    spec = importlib.util.spec_from_file_location("_open_flame_ai_protocol", path)
    if spec is None or spec.loader is None:
        raise ValueError
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _exact(value: object, fields: set[str]) -> Mapping[str, Any]:
    if (
        not isinstance(value, Mapping)
        or any(not isinstance(key, str) for key in value)
        or set(value) != fields
    ):
        raise ValueError
    return value


def _runtime_path(root: Path, relative: object, protocol: ModuleType) -> Path:
    canonical = protocol.relative_runtime_path(relative)
    path = root.joinpath(*PurePosixPath(canonical).parts)
    if path.resolve(strict=True) != path:
        raise ValueError
    return path


class WorkerFailure(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _provider(
    manifest_path: Path,
    worker_path: Path,
    protocol_path: Path,
    request: Mapping[str, object],
    protocol: ModuleType,
) -> tuple[Mapping[str, Any], Mapping[str, Any], Path]:
    manifest = _exact(
        protocol.read_json_file(manifest_path, maximum=4 * 1024 * 1024),
        _MANIFEST_FIELDS,
    )
    root = manifest_path.parent
    if (
        manifest["schema"] != protocol.RUNTIME_MANIFEST_SCHEMA
        or manifest["protocol_schema"] != protocol.PROTOCOL_SCHEMA
        or manifest["runtime_id"] != protocol.RUNTIME_ID
        or manifest["runtime_version"] != protocol.RUNTIME_VERSION
        or _runtime_path(root, manifest["worker"], protocol) != worker_path
        or _runtime_path(root, manifest["protocol"], protocol) != protocol_path
    ):
        raise ValueError
    providers = manifest["providers"]
    if not isinstance(providers, list):
        raise ValueError
    matches = [
        _exact(item, _PROVIDER_FIELDS)
        for item in providers
        if isinstance(item, Mapping) and item.get("id") == request["provider_id"]
    ]
    if len(matches) != 1:
        raise WorkerFailure("ai_provider_not_found")
    provider = matches[0]
    operation = request["operation"]
    operations = provider["operations"]
    if not isinstance(operations, list) or (
        operation != "health" and operation not in operations
    ):
        raise WorkerFailure("ai_provider_operation_unsupported")
    model_id = request["model_id"]
    model_ids = provider["model_ids"]
    if not isinstance(model_ids, list) or (
        model_id is None and model_ids
    ) or (
        model_id is not None and model_id not in model_ids
    ):
        raise WorkerFailure("ai_model_not_found")
    models = manifest["models"]
    if not isinstance(models, list):
        raise ValueError
    if model_id is None:
        model: Mapping[str, Any] = {}
    else:
        found = [
            _exact(item, {"id", "operations", "revision", "license", "artifact_paths"})
            for item in models
            if isinstance(item, Mapping) and item.get("id") == model_id
        ]
        if len(found) != 1:
            raise WorkerFailure("ai_model_not_found")
        model = found[0]
        if operation != "health" and operation not in model["operations"]:
            raise WorkerFailure("ai_model_operation_unsupported")
    return provider, model, root


class ProgressEmitter:
    def __init__(self, protocol: ModuleType, request_id: str, stream) -> None:
        self.protocol = protocol
        self.request_id = request_id
        self.stream = stream
        self.count = 0
        self.last_fraction = 0.0

    def __call__(self, fraction: float, code: str) -> None:
        if self.count >= self.protocol.MAX_PROGRESS_LINES:
            raise WorkerFailure("ai_progress_limit_exceeded")
        value = self.protocol.validate_progress(
            {
                "schema": self.protocol.PROTOCOL_SCHEMA,
                "type": "progress",
                "request_id": self.request_id,
                "fraction": fraction,
                "code": code,
            },
            expected_request_id=self.request_id,
        )
        current = float(value["fraction"])
        if current < self.last_fraction:
            raise WorkerFailure("ai_progress_invalid")
        encoded = self.protocol.canonical_json_bytes(value)
        if len(encoded) > self.protocol.MAX_PROGRESS_LINE_BYTES:
            raise WorkerFailure("ai_progress_limit_exceeded")
        self.stream.buffer.write(encoded + b"\n")
        self.stream.flush()
        self.last_fraction = current
        self.count += 1


def _safe_plugin_context(
    provider: Mapping[str, Any],
    model: Mapping[str, Any],
    root: Path,
    protocol: ModuleType,
) -> dict[str, object]:
    model_paths = [
        str(_runtime_path(root, path, protocol))
        for path in model.get("artifact_paths", [])
    ]
    return {
        "provider_id": provider["id"],
        "model_id": model.get("id"),
        "model_revision": model.get("revision"),
        "model_paths": model_paths,
        "config": dict(provider["config"]),
    }


def _plugin_call(
    provider: Mapping[str, Any],
    model: Mapping[str, Any],
    root: Path,
    request: Mapping[str, object],
    progress: Callable[[float, str], None],
    protocol: ModuleType,
) -> Mapping[str, object]:
    entrypoint = protocol.validate_entrypoint(provider["entrypoint"])
    module_name, attribute = entrypoint.split(":", 1)
    with open(os.devnull, "w", encoding="utf-8") as discarded:
        with contextlib.redirect_stdout(discarded), contextlib.redirect_stderr(discarded):
            module = importlib.import_module(module_name)
            handler = getattr(module, attribute)
            if not callable(handler):
                raise TypeError
            value = handler(
                operation=request["operation"],
                payload=request["payload"],
                context=_safe_plugin_context(provider, model, root, protocol),
                progress=progress,
            )
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise WorkerFailure("ai_provider_output_invalid")
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_media_for_http(payload: Mapping[str, object]) -> dict[str, object]:
    path = Path(str(payload["media_path"]))
    expected_size = int(payload["media_size"])
    if expected_size > _MAX_HTTP_MEDIA_BYTES:
        raise WorkerFailure("ai_http_media_too_large")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise WorkerFailure("ai_source_unavailable") from exc
    if len(data) != expected_size or hashlib.sha256(data).hexdigest() != payload["media_sha256"]:
        raise WorkerFailure("ai_source_changed")
    return {
        "media_base64": base64.b64encode(data).decode("ascii"),
        "media_size": expected_size,
        "media_sha256": payload["media_sha256"],
        "options": payload["options"],
    }


def _http_call(
    provider: Mapping[str, Any],
    request: Mapping[str, object],
    protocol: ModuleType,
) -> Mapping[str, object]:
    operation = str(request["operation"])
    egress = provider["data_egress"]
    if not isinstance(egress, list):
        raise WorkerFailure("ai_http_policy_invalid")
    payload = request["payload"]
    if not isinstance(payload, Mapping):
        raise WorkerFailure("ai_protocol_invalid")
    if operation == "transcribe":
        if "video" not in egress and "audio" not in egress:
            raise WorkerFailure("ai_media_egress_blocked")
        wire_payload = _read_media_for_http(payload)
    elif operation == "translate":
        if "text" not in egress:
            raise WorkerFailure("ai_text_egress_blocked")
        wire_payload = payload
    elif operation == "synthesize":
        if "text" not in egress or "audio" not in egress:
            raise WorkerFailure("ai_speech_egress_blocked")
        wire_payload = {"text": payload["text"], "options": payload["options"]}
    else:
        wire_payload = {}
    outbound = protocol.canonical_json_bytes(
        {
            "schema": protocol.PROTOCOL_SCHEMA,
            "request_id": request["request_id"],
            "operation": operation,
            "provider_id": request["provider_id"],
            "model_id": request["model_id"],
            "payload": wire_payload,
        }
    )
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Open-Flame-AI-Runtime/1",
    }
    auth_env = provider["auth_env"]
    if auth_env is not None:
        token = os.environ.get(str(auth_env), "")
        if not token or len(token) > 16_384 or any(ord(character) < 33 for character in token):
            raise WorkerFailure("ai_provider_auth_missing")
        headers["Authorization"] = "Bearer " + token
    request_object = urllib.request.Request(
        str(provider["endpoint"]), data=outbound, headers=headers, method="POST"
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(
            request_object, timeout=float(provider["timeout_seconds"])
        ) as response:
            if response.status != 200 or response.headers.get_content_type() != "application/json":
                raise WorkerFailure("ai_http_response_invalid")
            response_payload = response.read(_MAX_HTTP_RESPONSE_BYTES + 1)
    except WorkerFailure:
        raise
    except (OSError, TimeoutError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        raise WorkerFailure("ai_http_request_failed") from exc
    if not response_payload or len(response_payload) > _MAX_HTTP_RESPONSE_BYTES:
        raise WorkerFailure("ai_http_response_invalid")
    try:
        value = json.loads(response_payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise WorkerFailure("ai_http_response_invalid") from exc
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise WorkerFailure("ai_http_response_invalid")
    if operation == "synthesize":
        if set(value) != {"audio_base64"} or not isinstance(value["audio_base64"], str):
            raise WorkerFailure("ai_http_response_invalid")
        try:
            audio = base64.b64decode(value["audio_base64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise WorkerFailure("ai_http_response_invalid") from exc
        if not audio or len(audio) > _MAX_AUDIO_BYTES:
            raise WorkerFailure("ai_http_response_invalid")
        output = Path(str(payload["output_path"]))
        try:
            with output.open("xb") as handle:
                handle.write(audio)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise WorkerFailure("ai_audio_output_unavailable") from exc
        return {}
    return value


def _verify_source(request: Mapping[str, object], protocol: ModuleType) -> None:
    if request["operation"] != "transcribe":
        return
    payload = request["payload"]
    if not isinstance(payload, Mapping):
        raise WorkerFailure("ai_protocol_invalid")
    try:
        size, digest = protocol.file_sha256(
            Path(str(payload["media_path"])), maximum=16 * 1024**3
        )
    except protocol.AiProtocolError as exc:
        raise WorkerFailure("ai_source_unavailable") from exc
    if size != payload["media_size"] or digest != payload["media_sha256"]:
        raise WorkerFailure("ai_source_changed")


def _wav_result(path: Path, protocol: ModuleType) -> dict[str, object]:
    try:
        size, digest = protocol.file_sha256(path, maximum=_MAX_AUDIO_BYTES)
        with wave.open(str(path), "rb") as stream:
            channels = stream.getnchannels()
            sample_rate = stream.getframerate()
            frames = stream.getnframes()
            sample_width = stream.getsampwidth()
            compression = stream.getcomptype()
        if (
            channels not in {1, 2}
            or sample_rate not in {24_000, 44_100, 48_000}
            or frames <= 0
            or sample_width not in {2, 3, 4}
            or compression != "NONE"
        ):
            raise WorkerFailure("ai_audio_output_invalid")
        duration_ms = max(1, round(frames * 1000 / sample_rate))
    except WorkerFailure:
        raise
    except (OSError, EOFError, wave.Error, protocol.AiProtocolError) as exc:
        raise WorkerFailure("ai_audio_output_invalid") from exc
    return {
        "audio": {
            "path": str(path),
            "size": size,
            "sha256": digest,
            "duration_ms": duration_ms,
            "sample_rate": sample_rate,
            "channels": channels,
            "container": "wav",
        }
    }


def _runtime_identity() -> dict[str, object]:
    return {
        "implementation": platform.python_implementation().lower(),
        "version": [sys.version_info.major, sys.version_info.minor, sys.version_info.micro],
        "bits": struct.calcsize("P") * 8,
    }


def _success(
    request: Mapping[str, object],
    provider: Mapping[str, Any],
    provider_payload: Mapping[str, object],
    protocol: ModuleType,
) -> dict[str, object]:
    operation = str(request["operation"])
    if operation == "health":
        if set(provider_payload) != {"voices"}:
            raise WorkerFailure("ai_provider_output_invalid")
        payload: dict[str, object] = {
            "operations": list(provider["operations"]),
            "voices": provider_payload["voices"],
            "runtime": _runtime_identity(),
        }
    elif operation == "synthesize":
        payload_value = request["payload"]
        if not isinstance(payload_value, Mapping) or provider_payload:
            raise WorkerFailure("ai_provider_output_invalid")
        payload = _wav_result(Path(str(payload_value["output_path"])), protocol)
    else:
        payload = dict(provider_payload)
    return protocol.validate_result(
        {
            "schema": protocol.PROTOCOL_SCHEMA,
            "request_id": request["request_id"],
            "operation": operation,
            "status": "ok",
            "code": "",
            "provider_id": request["provider_id"],
            "model_id": request["model_id"],
            "payload": payload,
        },
        expected_request_id=str(request["request_id"]),
        expected_operation=operation,
    )


def _error(
    protocol: ModuleType,
    *,
    request_id: str,
    operation: str,
    provider_id: str | None,
    model_id: str | None,
    code: str,
) -> dict[str, object]:
    try:
        return protocol.validate_result(
            {
                "schema": protocol.PROTOCOL_SCHEMA,
                "request_id": request_id,
                "operation": operation,
                "status": "error",
                "code": code,
                "provider_id": provider_id,
                "model_id": model_id,
                "payload": {},
            },
            expected_request_id=request_id,
            expected_operation=operation,
        )
    except Exception:
        return {
            "schema": protocol.PROTOCOL_SCHEMA,
            "request_id": request_id,
            "operation": operation,
            "status": "error",
            "code": "ai_worker_failed",
            "provider_id": None,
            "model_id": None,
            "payload": {},
        }


def main(argv: list[str] | None = None) -> int:
    result_path: Path | None = None
    protocol: ModuleType | None = None
    request_id = "0" * 32
    operation = "health"
    provider_id: str | None = None
    model_id: str | None = None
    try:
        arguments = _arguments(list(sys.argv[1:] if argv is None else argv))
        result_path = arguments["result"]
        protocol = _load_protocol(arguments["protocol"])
        request = protocol.validate_request(
            protocol.read_json_file(arguments["request"], maximum=protocol.MAX_REQUEST_BYTES)
        )
        request_id = str(request["request_id"])
        operation = str(request["operation"])
        provider_id = str(request["provider_id"])
        model_id = None if request["model_id"] is None else str(request["model_id"])
        provider, model, root = _provider(
            arguments["manifest"],
            Path(__file__).resolve(strict=True),
            arguments["protocol"].resolve(strict=True),
            request,
            protocol,
        )
        _verify_source(request, protocol)
        progress = ProgressEmitter(protocol, request_id, sys.__stdout__)
        progress(0.0, "provider_starting")
        if provider["kind"] == "plugin":
            provider_payload = _plugin_call(
                provider, model, root, request, progress, protocol
            )
        elif provider["kind"] == "http":
            provider_payload = _http_call(provider, request, protocol)
        else:
            raise WorkerFailure("ai_provider_invalid")
        _verify_source(request, protocol)
        progress(1.0, "provider_complete")
        result = _success(request, provider, provider_payload, protocol)
        protocol.write_json_atomic(result_path, result, maximum=protocol.MAX_RESULT_BYTES)
        return 0
    except BaseException as exc:
        if protocol is None or result_path is None:
            return 2
        code = (
            exc.code
            if isinstance(exc, WorkerFailure)
            else exc.code
            if isinstance(exc, protocol.AiProtocolError)
            else "ai_worker_failed"
        )
        try:
            protocol.write_json_atomic(
                result_path,
                _error(
                    protocol,
                    request_id=request_id,
                    operation=operation,
                    provider_id=provider_id,
                    model_id=model_id,
                    code=code,
                ),
                maximum=protocol.MAX_RESULT_BYTES,
            )
        except BaseException:
            return 2
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
