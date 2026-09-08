"""Strict inspection for the optional, isolated AI runtime.

This module never imports an inference library and never installs anything.
An installed runtime is accepted only when its exact manifest, complete file
inventory, provider declarations, and artifact hashes satisfy the bundled
lock contract.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import urlsplit

from .ai_protocol import (
    MAX_PROGRESS_LINE_BYTES,
    MAX_PROGRESS_LINES,
    MAX_REQUEST_BYTES,
    MAX_RESULT_BYTES,
    PROTOCOL_SCHEMA,
    RUNTIME_ID,
    RUNTIME_MANIFEST_SCHEMA,
    RUNTIME_VERSION,
    TASK_OPERATIONS,
    AiProtocolError,
    canonical_json_bytes,
    read_json_file,
    relative_runtime_path,
    validate_entrypoint,
)


LOCK_SCHEMA = 2
LOCK_PATH = Path(__file__).with_name("ai-runtime-lock.json")
MANIFEST_NAME = "manifest.json"
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_RUNTIME_FILES = 200_000
MAX_ARTIFACT_BYTES = 64 * 1024**3
MAX_RUNTIME_BYTES = 256 * 1024**3
MAX_PROVIDER_CONFIG_BYTES = 64 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,159}$")
_ENVIRONMENT_NAME = re.compile(r"^OPEN_FLAME_AI_[A-Z0-9_]{1,60}$")
_SENSITIVE_CONFIG_KEY = re.compile(
    r"(?:secret|token|password|credential|authorization|cookie|api.?key)",
    re.IGNORECASE,
)
_EGRESS_KINDS = frozenset({"text", "audio", "video"})


class AiRuntimeError(RuntimeError):
    """Stable runtime failure with no path, provider payload, or secret."""

    _CODES = frozenset(
        {
            "ai_runtime_missing",
            "ai_runtime_invalid",
            "ai_runtime_changed",
            "ai_runtime_unsupported",
            "ai_provider_not_found",
            "ai_provider_operation_unsupported",
            "ai_model_not_found",
            "ai_model_operation_unsupported",
        }
    )

    def __init__(self, code: str):
        self.code = code if code in self._CODES else "ai_runtime_invalid"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class RuntimeArtifact:
    relative_path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class RuntimeModel:
    id: str
    operations: tuple[str, ...]
    revision: str
    license: str
    artifact_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuntimeProvider:
    id: str
    kind: str
    operations: tuple[str, ...]
    model_ids: tuple[str, ...]
    entrypoint: str | None
    endpoint: str | None
    auth_env: str | None
    timeout_seconds: float
    data_egress: tuple[str, ...]
    artifact_paths: tuple[str, ...]
    config: Mapping[str, object]

    def manifest_value(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "operations": list(self.operations),
            "model_ids": list(self.model_ids),
            "entrypoint": self.entrypoint,
            "endpoint": self.endpoint,
            "auth_env": self.auth_env,
            "timeout_seconds": self.timeout_seconds,
            "data_egress": list(self.data_egress),
            "artifact_paths": list(self.artifact_paths),
            "config": dict(self.config),
        }


@dataclass(frozen=True, slots=True)
class AiRuntime:
    root: Path
    platform: str
    python: Path
    worker: Path
    protocol: Path
    python_identity: Mapping[str, object]
    artifacts: Mapping[str, RuntimeArtifact]
    providers: tuple[RuntimeProvider, ...]
    models: tuple[RuntimeModel, ...]
    manifest_sha256: str

    def provider(self, provider_id: str, operation: str) -> RuntimeProvider:
        provider = next((item for item in self.providers if item.id == provider_id), None)
        if provider is None:
            raise AiRuntimeError("ai_provider_not_found")
        if operation != "health" and operation not in provider.operations:
            raise AiRuntimeError("ai_provider_operation_unsupported")
        return provider

    def model(
        self,
        provider: RuntimeProvider,
        model_id: str | None,
        operation: str,
    ) -> RuntimeModel | None:
        if model_id is None:
            if provider.model_ids:
                raise AiRuntimeError("ai_model_not_found")
            return None
        if model_id not in provider.model_ids:
            raise AiRuntimeError("ai_model_not_found")
        model = next((item for item in self.models if item.id == model_id), None)
        if model is None:
            raise AiRuntimeError("ai_model_not_found")
        if operation != "health" and operation not in model.operations:
            raise AiRuntimeError("ai_model_operation_unsupported")
        return model

    def verify_for_operation(
        self,
        provider_id: str,
        operation: str,
        model_id: str | None,
    ) -> tuple[RuntimeProvider, RuntimeModel | None]:
        """Re-hash every executable/provider/model byte needed by one call."""

        provider = self.provider(provider_id, operation)
        model = self.model(provider, model_id, operation)
        manifest_path = self.root / MANIFEST_NAME
        try:
            manifest_payload = _read_plain_bytes(manifest_path, MAX_MANIFEST_BYTES)
        except (OSError, AiRuntimeError):
            raise AiRuntimeError("ai_runtime_changed") from None
        if hashlib.sha256(manifest_payload).hexdigest() != self.manifest_sha256:
            raise AiRuntimeError("ai_runtime_changed")
        required = {
            _relative_to_root(self.root, self.python),
            _relative_to_root(self.root, self.worker),
            _relative_to_root(self.root, self.protocol),
            *provider.artifact_paths,
        }
        if model is not None:
            required.update(model.artifact_paths)
        for relative in sorted(required):
            artifact = self.artifacts.get(relative)
            if artifact is None:
                raise AiRuntimeError("ai_runtime_changed")
            _verify_artifact(self.root, artifact)
        return provider, model

    def status(self) -> dict[str, object]:
        return {
            "ready": True,
            "code": "ready",
            "runtime_id": RUNTIME_ID,
            "runtime_version": RUNTIME_VERSION,
            "protocol_schema": PROTOCOL_SCHEMA,
            "platform": self.platform,
            "providers": [
                {
                    "id": provider.id,
                    "kind": provider.kind,
                    "operations": list(provider.operations),
                    "model_ids": list(provider.model_ids),
                    "data_egress": list(provider.data_egress),
                }
                for provider in self.providers
            ],
            "models": [
                {
                    "id": model.id,
                    "operations": list(model.operations),
                    "revision": model.revision,
                    "license": model.license,
                }
                for model in self.models
            ],
        }


def default_ai_runtime_root(data_root: Path) -> Path:
    data_root = Path(data_root)
    return data_root.with_name(data_root.name + "-ai-runtime")


def _exact(value: object, keys: set[str]) -> Mapping[str, Any]:
    if (
        not isinstance(value, Mapping)
        or any(not isinstance(key, str) for key in value)
        or set(value) != keys
    ):
        raise AiRuntimeError("ai_runtime_invalid")
    return value


def _token(value: object) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise AiRuntimeError("ai_runtime_invalid")
    return value


def _string(value: object, *, maximum: int, required: bool = True) -> str:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise AiRuntimeError("ai_runtime_invalid")
    normalized = value.strip()
    if required and not normalized:
        raise AiRuntimeError("ai_runtime_invalid")
    return normalized


def _integer(value: object, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise AiRuntimeError("ai_runtime_invalid")
    return value


def _number(value: object, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AiRuntimeError("ai_runtime_invalid")
    converted = float(value)
    if not math.isfinite(converted) or not minimum <= converted <= maximum:
        raise AiRuntimeError("ai_runtime_invalid")
    return converted


def _digest(value: object) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise AiRuntimeError("ai_runtime_invalid")
    return value


def _unique_tokens(value: object, *, allowed: frozenset[str] | None = None) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) != len(set(value)):
        raise AiRuntimeError("ai_runtime_invalid")
    result = tuple(_token(item) for item in value)
    if allowed is not None and any(item not in allowed for item in result):
        raise AiRuntimeError("ai_runtime_invalid")
    return result


def _runtime_path(value: object) -> str:
    try:
        return relative_runtime_path(value)
    except AiProtocolError as exc:
        raise AiRuntimeError("ai_runtime_invalid") from exc


def _runtime_paths(value: object, *, nonempty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) != len(set(value)):
        raise AiRuntimeError("ai_runtime_invalid")
    result = tuple(_runtime_path(item) for item in value)
    if nonempty and not result:
        raise AiRuntimeError("ai_runtime_invalid")
    return result


def _platform_id() -> str:
    machine = platform.machine().lower()
    if machine not in {"amd64", "x86_64", "arm64", "aarch64"}:
        raise AiRuntimeError("ai_runtime_unsupported")
    architecture = "arm64" if machine in {"arm64", "aarch64"} else "x64"
    if os.name == "nt":
        return f"windows-{architecture}"
    if platform.system().lower() == "linux":
        return f"linux-{architecture}"
    if platform.system().lower() == "darwin":
        return f"darwin-{architecture}"
    raise AiRuntimeError("ai_runtime_unsupported")


def _plain_directory(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise AiRuntimeError("ai_runtime_missing") from None
    except OSError as exc:
        raise AiRuntimeError("ai_runtime_invalid") from exc
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & reparse
    ):
        raise AiRuntimeError("ai_runtime_invalid")
    return info


def _signature(info: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        getattr(info, "st_file_attributes", 0),
    )


def _read_plain_bytes(path: Path, maximum: int) -> bytes:
    before = path.lstat()
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or getattr(before, "st_file_attributes", 0) & reparse
        or before.st_nlink != 1
        or not 0 < before.st_size <= maximum
    ):
        raise AiRuntimeError("ai_runtime_invalid")
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if _signature(opened) != _signature(before):
            raise AiRuntimeError("ai_runtime_changed")
        payload = handle.read(maximum + 1)
        finished = os.fstat(handle.fileno())
    after = path.lstat()
    if (
        len(payload) != before.st_size
        or len(payload) > maximum
        or _signature(finished) != _signature(before)
        or _signature(after) != _signature(before)
    ):
        raise AiRuntimeError("ai_runtime_changed")
    return payload


def _hash_runtime_file(path: Path, expected_size: int) -> str:
    before = path.lstat()
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or getattr(before, "st_file_attributes", 0) & reparse
        or before.st_nlink != 1
        or before.st_size != expected_size
    ):
        raise AiRuntimeError("ai_runtime_invalid")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if _signature(opened) != _signature(before):
            raise AiRuntimeError("ai_runtime_changed")
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
        finished = os.fstat(handle.fileno())
    after = path.lstat()
    if _signature(finished) != _signature(before) or _signature(after) != _signature(before):
        raise AiRuntimeError("ai_runtime_changed")
    return digest.hexdigest()


def _artifact_path(root: Path, relative: str) -> Path:
    path = root.joinpath(*PurePosixPath(relative).parts)
    try:
        if path.resolve(strict=True) != path:
            raise AiRuntimeError("ai_runtime_invalid")
    except (OSError, RuntimeError) as exc:
        raise AiRuntimeError("ai_runtime_invalid") from exc
    return path


def _relative_to_root(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise AiRuntimeError("ai_runtime_invalid") from exc


def _verify_artifact(root: Path, artifact: RuntimeArtifact) -> None:
    try:
        actual = _hash_runtime_file(
            _artifact_path(root, artifact.relative_path),
            artifact.size,
        )
    except OSError as exc:
        raise AiRuntimeError("ai_runtime_changed") from exc
    if actual != artifact.sha256:
        raise AiRuntimeError("ai_runtime_changed")


def _load_lock() -> Mapping[str, Any]:
    try:
        value = read_json_file(LOCK_PATH, maximum=64 * 1024)
    except AiProtocolError as exc:
        raise AiRuntimeError("ai_runtime_invalid") from exc
    lock = _exact(
        value,
        {
            "schema",
            "runtime_manifest_schema",
            "protocol_schema",
            "runtime_id",
            "runtime_version",
            "python",
            "supported_platforms",
            "limits",
        },
    )
    python_value = _exact(
        lock["python"], {"implementation", "major", "minors", "bits"}
    )
    limits = _exact(
        lock["limits"],
        {
            "request_bytes",
            "result_bytes",
            "progress_line_bytes",
            "progress_lines",
            "http_response_bytes",
        },
    )
    if (
        lock["schema"] != LOCK_SCHEMA
        or lock["runtime_manifest_schema"] != RUNTIME_MANIFEST_SCHEMA
        or lock["protocol_schema"] != PROTOCOL_SCHEMA
        or lock["runtime_id"] != RUNTIME_ID
        or lock["runtime_version"] != RUNTIME_VERSION
        or python_value
        != {
            "implementation": "cpython",
            "major": 3,
            "minors": [12, 13],
            "bits": 64,
        }
        or limits
        != {
            "request_bytes": MAX_REQUEST_BYTES,
            "result_bytes": MAX_RESULT_BYTES,
            "progress_line_bytes": MAX_PROGRESS_LINE_BYTES,
            "progress_lines": MAX_PROGRESS_LINES,
            "http_response_bytes": MAX_RESULT_BYTES,
        }
    ):
        raise AiRuntimeError("ai_runtime_invalid")
    _unique_tokens(lock["supported_platforms"])
    return lock


def _safe_config(value: object, *, depth: int = 0) -> object:
    if depth > 6:
        raise AiRuntimeError("ai_runtime_invalid")
    if value is None or type(value) in {bool, int}:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AiRuntimeError("ai_runtime_invalid")
        return value
    if isinstance(value, str):
        return _string(value, maximum=4_000, required=False)
    if isinstance(value, list):
        if len(value) > 256:
            raise AiRuntimeError("ai_runtime_invalid")
        return [_safe_config(item, depth=depth + 1) for item in value]
    if isinstance(value, Mapping):
        if len(value) > 256 or any(not isinstance(key, str) for key in value):
            raise AiRuntimeError("ai_runtime_invalid")
        result: dict[str, object] = {}
        for key, item in value.items():
            if not _TOKEN.fullmatch(key) or _SENSITIVE_CONFIG_KEY.search(key):
                raise AiRuntimeError("ai_runtime_invalid")
            result[key] = _safe_config(item, depth=depth + 1)
        return result
    raise AiRuntimeError("ai_runtime_invalid")


def _https_endpoint(value: object) -> str:
    text = _string(value, maximum=2_000)
    try:
        parsed = urlsplit(text)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise AiRuntimeError("ai_runtime_invalid")
        _ = parsed.port
    except ValueError as exc:
        raise AiRuntimeError("ai_runtime_invalid") from exc
    return text


def _provider(value: object) -> RuntimeProvider:
    row = _exact(
        value,
        {
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
        },
    )
    kind = row["kind"]
    if kind not in {"plugin", "remote_plugin", "http"}:
        raise AiRuntimeError("ai_runtime_invalid")
    operations = _unique_tokens(row["operations"], allowed=TASK_OPERATIONS)
    if not operations:
        raise AiRuntimeError("ai_runtime_invalid")
    model_ids = _unique_tokens(row["model_ids"])
    artifact_paths = _runtime_paths(
        row["artifact_paths"], nonempty=kind in {"plugin", "remote_plugin"}
    )
    entrypoint = row["entrypoint"]
    endpoint = row["endpoint"]
    auth_env = row["auth_env"]
    if kind in {"plugin", "remote_plugin"}:
        try:
            entrypoint = validate_entrypoint(entrypoint)
        except AiProtocolError as exc:
            raise AiRuntimeError("ai_runtime_invalid") from exc
        if endpoint is not None:
            raise AiRuntimeError("ai_runtime_invalid")
        if kind == "plugin" and auth_env is not None:
            raise AiRuntimeError("ai_runtime_invalid")
        if kind == "remote_plugin" and (
            not isinstance(auth_env, str)
            or not _ENVIRONMENT_NAME.fullmatch(auth_env)
        ):
            raise AiRuntimeError("ai_runtime_invalid")
    else:
        if entrypoint is not None:
            raise AiRuntimeError("ai_runtime_invalid")
        endpoint = _https_endpoint(endpoint)
        if auth_env is not None and (
            not isinstance(auth_env, str) or not _ENVIRONMENT_NAME.fullmatch(auth_env)
        ):
            raise AiRuntimeError("ai_runtime_invalid")
    egress = _unique_tokens(row["data_egress"], allowed=_EGRESS_KINDS)
    if kind == "plugin" and egress:
        raise AiRuntimeError("ai_runtime_invalid")
    if kind in {"remote_plugin", "http"} and not egress:
        raise AiRuntimeError("ai_runtime_invalid")
    config = _safe_config(row["config"])
    if not isinstance(config, Mapping) or len(canonical_json_bytes(config)) > MAX_PROVIDER_CONFIG_BYTES:
        raise AiRuntimeError("ai_runtime_invalid")
    return RuntimeProvider(
        id=_token(row["id"]),
        kind=kind,
        operations=operations,
        model_ids=model_ids,
        entrypoint=entrypoint,
        endpoint=endpoint,
        auth_env=auth_env,
        timeout_seconds=_number(row["timeout_seconds"], 1.0, 86_400.0),
        data_egress=egress,
        artifact_paths=artifact_paths,
        config=dict(config),
    )


def _model(value: object) -> RuntimeModel:
    row = _exact(value, {"id", "operations", "revision", "license", "artifact_paths"})
    operations = _unique_tokens(row["operations"], allowed=TASK_OPERATIONS)
    if not operations:
        raise AiRuntimeError("ai_runtime_invalid")
    return RuntimeModel(
        id=_token(row["id"]),
        operations=operations,
        revision=_string(row["revision"], maximum=240),
        license=_string(row["license"], maximum=240),
        artifact_paths=_runtime_paths(row["artifact_paths"], nonempty=True),
    )


def _artifact(relative: object, value: object) -> RuntimeArtifact:
    path = _runtime_path(relative)
    row = _exact(value, {"size", "sha256"})
    return RuntimeArtifact(
        relative_path=path,
        size=_integer(row["size"], 0, MAX_ARTIFACT_BYTES),
        sha256=_digest(row["sha256"]),
    )


def _inventory(root: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    count = 0
    for current, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        _plain_directory(current_path)
        for name in directory_names:
            count += 1
            if count > MAX_RUNTIME_FILES:
                raise AiRuntimeError("ai_runtime_invalid")
            directory = current_path / name
            _plain_directory(directory)
            directories.add(directory.relative_to(root).as_posix())
        for name in file_names:
            count += 1
            if count > MAX_RUNTIME_FILES:
                raise AiRuntimeError("ai_runtime_invalid")
            path = current_path / name
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError as exc:
                raise AiRuntimeError("ai_runtime_invalid") from exc
            _runtime_path(relative)
            files.add(relative)
    return files, directories


def load_ai_runtime(root: Path) -> AiRuntime:
    """Load and fully hash one exact installed runtime, or fail closed."""

    lock = _load_lock()
    root = Path(os.path.abspath(root))
    _plain_directory(root)
    try:
        canonical_root = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AiRuntimeError("ai_runtime_invalid") from exc
    if canonical_root != root:
        raise AiRuntimeError("ai_runtime_invalid")
    manifest_path = root / MANIFEST_NAME
    try:
        manifest_bytes = _read_plain_bytes(manifest_path, MAX_MANIFEST_BYTES)
        manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
    except FileNotFoundError:
        raise AiRuntimeError("ai_runtime_missing") from None
    except AiRuntimeError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AiRuntimeError("ai_runtime_invalid") from exc
    value = _exact(
        manifest,
        {
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
        },
    )
    current_platform = _platform_id()
    supported = set(lock["supported_platforms"])
    if current_platform not in supported:
        raise AiRuntimeError("ai_runtime_unsupported")
    if (
        value["schema"] != RUNTIME_MANIFEST_SCHEMA
        or value["protocol_schema"] != PROTOCOL_SCHEMA
        or value["runtime_id"] != RUNTIME_ID
        or value["runtime_version"] != RUNTIME_VERSION
        or value["platform"] != current_platform
    ):
        raise AiRuntimeError("ai_runtime_invalid")

    python_value = _exact(value["python"], {"path", "implementation", "version", "bits"})
    version = python_value["version"]
    if (
        python_value["implementation"] != "cpython"
        or not isinstance(version, list)
        or len(version) != 3
        or any(type(item) is not int or item < 0 for item in version)
        or version[0] != lock["python"]["major"]
        or version[1] not in lock["python"]["minors"]
        or python_value["bits"] != 64
    ):
        raise AiRuntimeError("ai_runtime_invalid")
    python_relative = _runtime_path(python_value["path"])
    worker_relative = _runtime_path(value["worker"])
    protocol_relative = _runtime_path(value["protocol"])

    raw_artifacts = value["artifacts"]
    if not isinstance(raw_artifacts, Mapping) or not raw_artifacts:
        raise AiRuntimeError("ai_runtime_invalid")
    artifacts: dict[str, RuntimeArtifact] = {}
    total_bytes = 0
    for relative, raw in raw_artifacts.items():
        artifact = _artifact(relative, raw)
        if artifact.relative_path in artifacts:
            raise AiRuntimeError("ai_runtime_invalid")
        total_bytes += artifact.size
        if total_bytes > MAX_RUNTIME_BYTES:
            raise AiRuntimeError("ai_runtime_invalid")
        artifacts[artifact.relative_path] = artifact
    required = {python_relative, worker_relative, protocol_relative}
    if not required.issubset(artifacts):
        raise AiRuntimeError("ai_runtime_invalid")

    raw_providers = value["providers"]
    raw_models = value["models"]
    if not isinstance(raw_providers, list) or not raw_providers or len(raw_providers) > 64:
        raise AiRuntimeError("ai_runtime_invalid")
    if not isinstance(raw_models, list) or len(raw_models) > 256:
        raise AiRuntimeError("ai_runtime_invalid")
    providers = tuple(_provider(item) for item in raw_providers)
    models = tuple(_model(item) for item in raw_models)
    if len({item.id for item in providers}) != len(providers):
        raise AiRuntimeError("ai_runtime_invalid")
    if len({item.id for item in models}) != len(models):
        raise AiRuntimeError("ai_runtime_invalid")
    model_map = {item.id: item for item in models}
    for provider in providers:
        if any(model_id not in model_map for model_id in provider.model_ids):
            raise AiRuntimeError("ai_runtime_invalid")
        if any(
            not set(model_map[model_id].operations).intersection(provider.operations)
            for model_id in provider.model_ids
        ):
            raise AiRuntimeError("ai_runtime_invalid")
        if any(path not in artifacts for path in provider.artifact_paths):
            raise AiRuntimeError("ai_runtime_invalid")
    for model in models:
        if any(path not in artifacts for path in model.artifact_paths):
            raise AiRuntimeError("ai_runtime_invalid")

    files, _directories = _inventory(root)
    if files != set(artifacts) | {MANIFEST_NAME}:
        raise AiRuntimeError("ai_runtime_invalid")
    for artifact in artifacts.values():
        _verify_artifact(root, artifact)

    python_path = _artifact_path(root, python_relative)
    worker_path = _artifact_path(root, worker_relative)
    protocol_path = _artifact_path(root, protocol_relative)
    return AiRuntime(
        root=root,
        platform=current_platform,
        python=python_path,
        worker=worker_path,
        protocol=protocol_path,
        python_identity={
            "implementation": "cpython",
            "version": tuple(version),
            "bits": 64,
        },
        artifacts=artifacts,
        providers=providers,
        models=models,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )


def inspect_ai_runtime(root: Path) -> dict[str, object]:
    try:
        return load_ai_runtime(root).status()
    except AiRuntimeError as exc:
        return {
            "ready": False,
            "code": exc.code,
            "runtime_id": RUNTIME_ID,
            "runtime_version": RUNTIME_VERSION,
            "protocol_schema": PROTOCOL_SCHEMA,
            "providers": [],
            "models": [],
        }


__all__ = [
    "AiRuntime",
    "AiRuntimeError",
    "RuntimeArtifact",
    "RuntimeModel",
    "RuntimeProvider",
    "default_ai_runtime_root",
    "inspect_ai_runtime",
    "load_ai_runtime",
]
