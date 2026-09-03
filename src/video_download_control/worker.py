from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock, Thread
from time import perf_counter

from . import __version__
from .adapters.base import (
    AdapterContext,
    AdapterFailure,
    AdapterNetworkMode,
    DownloadAdapter,
    DownloadRequest,
    NetworkExecutionGuard,
    ProbeItem,
    ProbeRequest,
    ProducedFile,
    ProgressUpdate,
)
from .assets import (
    MAX_CAPTIONS_PER_ASSET,
    MAX_THUMBNAILS_PER_ASSET,
    MAX_TOTAL_CAPTIONS_PER_DOWNLOAD,
    MAX_TOTAL_THUMBNAILS_PER_DOWNLOAD,
    AssetStore,
    AssetValidationError,
    AuxiliaryFile,
    MediaVerifier,
)
from .domain import ErrorCode, JobStatus, SourceType
from .graph import GraphValidationError, XAttachmentProbeItem
from .retry_policy import RetryAction, RetryContext, RetryPolicy
from .runtime_logging import RuntimeLogger, safe_exception_type
from .worker_repository import JobLease, LostLease, WorkerRepository

_WORKER_LOG_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _safe_log_token(value: object, *, fallback: str) -> str:
    candidate = str(value)
    return candidate if _WORKER_LOG_ID.fullmatch(candidate) else fallback


def _elapsed_ms(started: float) -> float:
    return min(max((perf_counter() - started) * 1000.0, 0.0), 86_400_000.0)


@dataclass(frozen=True, slots=True)
class WorkerRunResult:
    job_id: str
    attempt_id: str
    status: str
    error_code: ErrorCode | None = None


class _LeaseHeartbeat:
    def __init__(
        self,
        repository: WorkerRepository,
        lease: JobLease,
        clock: Callable[[], datetime],
        *,
        lease_seconds: int,
        interval_seconds: float,
        on_failure: Callable[[Exception], None] | None = None,
    ) -> None:
        self._repository = repository
        self._lease = lease
        self._clock = clock
        self._lease_seconds = lease_seconds
        self._interval_seconds = interval_seconds
        self._stop = Event()
        self._state_lock = Lock()
        self._cancelled = False
        self._failure: Exception | None = None
        self._failure_reported = False
        self._on_failure = on_failure
        self._thread = Thread(
            target=self._run,
            name=f"lease-heartbeat-{lease.attempt_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()

    def raise_if_failed(self) -> None:
        with self._state_lock:
            failure = self._failure
        if failure is not None:
            raise failure

    def pulse(self) -> bool:
        self.raise_if_failed()
        try:
            cancelled = self._repository.heartbeat(
                self._lease,
                now=self._clock(),
                lease_seconds=self._lease_seconds,
            )
        except Exception as exc:
            report_failure = False
            with self._state_lock:
                if self._failure is None:
                    self._failure = exc
                if not self._failure_reported:
                    self._failure_reported = True
                    report_failure = True
            self._stop.set()
            if report_failure and self._on_failure is not None:
                try:
                    self._on_failure(exc)
                except Exception:  # noqa: BLE001,S110 - observer cannot affect lease
                    # Runtime logging is observational and cannot affect lease
                    # ownership or Worker failure semantics.
                    pass
            raise
        with self._state_lock:
            self._cancelled = self._cancelled or cancelled
            return self._cancelled

    def is_cancelled(self) -> bool:
        with self._state_lock:
            if self._cancelled:
                return True
        return self.pulse()

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                self.pulse()
            except Exception:  # noqa: BLE001 - heartbeat failure is stored by pulse
                return


class Worker:
    """Runs one injected adapter without owning network or subprocess policy.

    Adapter construction and real-network feature gates stay in the CLI layer.
    Graph jobs additionally require an adapter-declared exact-selector
    capability; the production candidate deliberately does not declare it.
    """

    def __init__(
        self,
        *,
        worker_id: str,
        repository: WorkerRepository,
        adapter: DownloadAdapter,
        asset_store: AssetStore,
        verifier: MediaVerifier,
        retry_policy: RetryPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        lease_seconds: int = 60,
        heartbeat_interval_seconds: float | None = None,
        attempt_timeout_seconds: float = 3600.0,
        max_items_per_source: int = 20,
        skip_unsupported_graph_jobs: bool = False,
        network_guard: NetworkExecutionGuard | None = None,
        runtime_logger: RuntimeLogger | None = None,
    ) -> None:
        if lease_seconds < 10:
            raise ValueError("lease_seconds must be at least 10")
        heartbeat_interval = (
            max(1.0, lease_seconds / 3)
            if heartbeat_interval_seconds is None
            else heartbeat_interval_seconds
        )
        if not 0 < heartbeat_interval < lease_seconds:
            raise ValueError(
                "heartbeat_interval_seconds must be positive and shorter than the lease"
            )
        if attempt_timeout_seconds <= 0:
            raise ValueError("attempt_timeout_seconds must be positive")
        if not 1 <= max_items_per_source <= 50:
            raise ValueError("max_items_per_source must be between 1 and 50")
        if not isinstance(skip_unsupported_graph_jobs, bool):
            raise ValueError("graph skip policy must be boolean")
        try:
            network_mode = AdapterNetworkMode(adapter.network_mode)
        except (AttributeError, ValueError) as exc:
            raise ValueError("adapter must declare a valid network_mode") from exc
        if network_mode is not AdapterNetworkMode.OFFLINE and network_guard is None:
            raise ValueError(
                "networked adapter requires a deployment-owned network guard"
            )
        self.worker_id = worker_id
        self.repository = repository
        self.adapter = adapter
        self.asset_store = asset_store
        self.verifier = verifier
        self.retry_policy = retry_policy or RetryPolicy()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.lease_seconds = lease_seconds
        self.heartbeat_interval_seconds = heartbeat_interval
        self.attempt_timeout_seconds = attempt_timeout_seconds
        self.max_items_per_source = max_items_per_source
        self.skip_unsupported_graph_jobs = skip_unsupported_graph_jobs
        self.network_mode = network_mode
        self.network_guard = network_guard
        self.runtime_logger = runtime_logger
        self._log_worker_id = _safe_log_token(
            worker_id, fallback="worker-id-unavailable"
        )
        self._paused = False
        self._pause_error_code: ErrorCode | None = None

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def pause_error_code(self) -> ErrorCode | None:
        return self._pause_error_code

    def run_once(self) -> WorkerRunResult | None:
        cycle_started = perf_counter()
        if self.network_mode is not AdapterNetworkMode.OFFLINE:
            assert self.network_guard is not None
            try:
                self.network_guard.assert_ready(adapter_name=self.adapter.name)
            except Exception as exc:
                self._emit(
                    "worker.cycle_failed",
                    level="ERROR",
                    worker_id=self._log_worker_id,
                    exception_type=safe_exception_type(exc),
                    duration_ms=_elapsed_ms(cycle_started),
                )
                raise
        try:
            queue_control = self.repository.get_queue_control()
        except Exception as exc:
            self._emit(
                "worker.cycle_failed",
                level="ERROR",
                worker_id=self._log_worker_id,
                exception_type=safe_exception_type(exc),
                duration_ms=_elapsed_ms(cycle_started),
            )
            raise
        if queue_control["paused"]:
            was_paused = self._paused
            self._paused = True
            reason = queue_control.get("reason")
            try:
                self._pause_error_code = ErrorCode(str(reason))
            except ValueError:
                self._pause_error_code = ErrorCode.WORKER_INTERNAL
            if not was_paused:
                self._emit(
                    "worker.queue_paused",
                    level="WARNING",
                    worker_id=self._log_worker_id,
                    error_code=self._pause_error_code.value,
                    failure_site="queue_pause",
                )
            return None
        self._paused = False
        self._pause_error_code = None
        try:
            self.repository.reconcile_attempt_directories(
                list_attempt_ids=self.asset_store.list_managed_attempt_ids,
                cleanup_attempt=self.asset_store.cleanup_attempt_identity,
            )
            lease = self.repository.claim_next(
                worker_id=self.worker_id,
                adapter=self.adapter.name,
                adapter_version=self.adapter.version,
                now=self.clock(),
                lease_seconds=self.lease_seconds,
                remove_pending_asset=self.asset_store.remove_pending_asset,
                supports_exact_selector=getattr(
                    self.adapter, "supports_exact_selector", False
                ),
                skip_unsupported_graph_jobs=self.skip_unsupported_graph_jobs,
            )
        except OSError as exc:
            # Recovery cleanup is part of claiming.  A filesystem denial must
            # fail closed: its DB transaction has rolled back and the durable
            # intent remains for a later retry.
            self._emit(
                "worker.cycle_failed",
                level="ERROR",
                worker_id=self._log_worker_id,
                exception_type=safe_exception_type(exc),
                duration_ms=_elapsed_ms(cycle_started),
            )
            self._emit_cleanup_failure(
                None,
                operation="reconciliation",
                exception=exc,
            )
            self._pause(ErrorCode.STORAGE_ERROR, exception=exc)
            return None
        except Exception as exc:
            self._emit(
                "worker.cycle_failed",
                level="ERROR",
                worker_id=self._log_worker_id,
                exception_type=safe_exception_type(exc),
                duration_ms=_elapsed_ms(cycle_started),
            )
            raise
        if lease is None:
            return None

        self._emit(
            "worker.job_claimed",
            worker_id=self._log_worker_id,
            job_id=lease.job_id,
            attempt_id=lease.attempt_id,
            attempt_no=lease.attempt_no,
            run_generation=lease.run_generation,
            platform=lease.platform.value,
            source_type=lease.source_type.value,
            job_kind=lease.job_kind,
            adapter=lease.adapter,
        )
        self._emit_phase(lease, "preparing")

        attempt_paths = None
        staged_assets = []
        durable_intent_ids: set[str] = set()
        assets_registered = False
        heartbeat = _LeaseHeartbeat(
            self.repository,
            lease,
            self.clock,
            lease_seconds=self.lease_seconds,
            interval_seconds=self.heartbeat_interval_seconds,
            on_failure=lambda exc: self._emit(
                "worker.heartbeat_failed",
                level="ERROR",
                worker_id=self._log_worker_id,
                job_id=lease.job_id,
                attempt_id=lease.attempt_id,
                exception_type=safe_exception_type(exc),
                failure_site="heartbeat",
            ),
        )
        heartbeat.start()
        try:
            attempt_paths = self.asset_store.prepare_attempt(
                lease.job_id, lease.attempt_id
            )
            context = AdapterContext(
                worker_id=self.worker_id,
                attempt_id=lease.attempt_id,
                temporary_dir=attempt_paths.root,
                deadline_at=self.clock()
                + timedelta(seconds=self.attempt_timeout_seconds),
            )
            if lease.source_type == SourceType.X_ATTACHMENT:
                if (
                    lease.job_kind != "download"
                    or lease.selector_key is None
                    or lease.expected_media_key is None
                    or lease.expected_media_kind is None
                ):
                    raise AssetValidationError("attachment download target is invalid")
                expected_item_count = 1
                sanitized_source: dict[str, object] = {}
                probe_items = {
                    lease.expected_media_key: ProbeItem(
                        canonical_url=lease.canonical_url,
                        media_key=lease.expected_media_key,
                        media_kind=lease.expected_media_kind,
                    )
                }
            else:
                self._emit_phase(lease, "probing")
                probe = self.adapter.probe(
                    ProbeRequest(
                        job_id=lease.job_id,
                        canonical_url=lease.canonical_url,
                        platform=lease.platform,
                        source_type=lease.source_type,
                        credential_ref=lease.credential_ref,
                        max_items=self.max_items_per_source,
                    ),
                    context,
                )
                heartbeat.raise_if_failed()
                if heartbeat.is_cancelled():
                    heartbeat.stop()
                    self.repository.finish_canceled(
                        lease,
                        now=self.clock(),
                        remove_pending_asset=self.asset_store.remove_pending_asset,
                    )
                    return self._finish_result(
                        lease,
                        result_status="canceled",
                        started=cycle_started,
                    )
                if probe.expected_item_count != len(probe.items) or not probe.items:
                    raise AssetValidationError("probe item count mismatch")
                if probe.expected_item_count > self.max_items_per_source:
                    raise AssetValidationError(
                        "probe item count exceeds the configured source limit"
                    )
                if lease.job_kind == "discover":
                    graph_items = tuple(
                        XAttachmentProbeItem(
                            stable_key=item.stable_key,
                            selector_key=item.selector_key,
                            expected_media_key=item.media_key,
                            media_kind=item.media_kind,
                        )
                        for item in probe.items
                    )
                    heartbeat.stop()
                    self._emit_phase(lease, "discovery")
                    discovery_status = self.repository.commit_discovery(
                        lease,
                        probe_items=graph_items,
                        discovery_snapshot_hash=probe.discovery_snapshot_hash,
                        sanitized_source=probe.sanitized_source,
                        now=self.clock(),
                    )
                    return self._finish_result(
                        lease,
                        result_status=discovery_status.value,
                        started=cycle_started,
                    )
                expected_item_count = probe.expected_item_count
                sanitized_source = dict(probe.sanitized_source)
                probe_items = {item.media_key: item for item in probe.items}
                if len(probe_items) != len(probe.items):
                    raise AssetValidationError("probe media keys must be unique")
                self.repository.record_probe(
                    lease,
                    expected_item_count=expected_item_count,
                    discovery_snapshot_hash=probe.discovery_snapshot_hash,
                    sanitized_source=sanitized_source,
                    now=self.clock(),
                )
            heartbeat.raise_if_failed()
            if heartbeat.is_cancelled():
                heartbeat.stop()
                self.repository.finish_canceled(
                    lease,
                    now=self.clock(),
                    remove_pending_asset=self.asset_store.remove_pending_asset,
                )
                return self._finish_result(
                    lease,
                    result_status="canceled",
                    started=cycle_started,
                )
            self._emit_phase(lease, "downloading")
            self.repository.transition(
                lease,
                status=JobStatus.DOWNLOADING,
                progress=0.05,
                now=self.clock(),
                lease_seconds=self.lease_seconds,
            )

            def report_progress(update: ProgressUpdate) -> None:
                heartbeat.raise_if_failed()
                if update.fraction is None:
                    heartbeat.pulse()
                    return
                bounded = min(max(update.fraction, 0.0), 1.0)
                self.repository.transition(
                    lease,
                    status=JobStatus.DOWNLOADING,
                    progress=0.05 + (bounded * 0.75),
                    now=self.clock(),
                    lease_seconds=self.lease_seconds,
                )

            download = self.adapter.download(
                DownloadRequest(
                    job_id=lease.job_id,
                    source_item_id=lease.source_item_id,
                    canonical_url=lease.canonical_url,
                    platform=lease.platform,
                    source_type=lease.source_type,
                    output_dir=attempt_paths.output,
                    expected_media_keys=tuple(probe_items),
                    selector_key=lease.selector_key,
                    credential_ref=lease.credential_ref,
                ),
                context,
                report_progress,
                heartbeat,
            )
            heartbeat.raise_if_failed()
            if heartbeat.is_cancelled():
                heartbeat.stop()
                self.repository.finish_canceled(
                    lease,
                    now=self.clock(),
                    remove_pending_asset=self.asset_store.remove_pending_asset,
                )
                return self._finish_result(
                    lease,
                    result_status="canceled",
                    started=cycle_started,
                )
            if len(download.files) != expected_item_count:
                raise AssetValidationError("downloaded original count mismatch")
            produced_by_key = {item.media_key: item for item in download.files}
            if len(produced_by_key) != len(download.files) or set(
                produced_by_key
            ) != set(probe_items):
                raise AssetValidationError("download media keys do not match probe")
            ordinals = {item.ordinal for item in download.files}
            if ordinals != set(range(len(download.files))):
                raise AssetValidationError(
                    "download ordinals must be unique and contiguous"
                )
            if any(
                produced_by_key[key].media_kind != probe_items[key].media_kind
                for key in produced_by_key
            ):
                raise AssetValidationError("download media kinds do not match probe")
            if any(item.role != "original" for item in download.files):
                raise AssetValidationError("download originals must use original role")
            thumbnails_by_key = self._group_auxiliary_files(
                download.thumbnails,
                owners=probe_items,
                kind="thumbnail",
                media_kind="image",
                per_asset_limit=MAX_THUMBNAILS_PER_ASSET,
                total_limit=MAX_TOTAL_THUMBNAILS_PER_DOWNLOAD,
            )
            captions_by_key = self._group_auxiliary_files(
                download.captions,
                owners=probe_items,
                kind="caption",
                media_kind="text",
                per_asset_limit=MAX_CAPTIONS_PER_ASSET,
                total_limit=MAX_TOTAL_CAPTIONS_PER_DOWNLOAD,
            )
            self._validate_unique_produced_paths(
                (*download.files, *download.thumbnails, *download.captions)
            )
            self._validate_output_inventory(
                attempt_paths.output,
                (*download.files, *download.thumbnails, *download.captions),
            )
            self._emit_phase(lease, "verifying")
            self.repository.transition(
                lease,
                status=JobStatus.VERIFYING,
                progress=0.85,
                now=self.clock(),
                lease_seconds=self.lease_seconds,
            )
            for produced in sorted(download.files, key=lambda item: item.ordinal):
                staged = self.asset_store.stage_file(
                    attempt=attempt_paths,
                    produced_path=produced.path,
                    media_key=produced.media_key,
                    media_kind=produced.media_kind,
                    role=produced.role,
                    ordinal=produced.ordinal,
                    sanitized_source=sanitized_source,
                    producer={
                        "adapter": self.adapter.name,
                        "adapter_version": self.adapter.version,
                        "worker": "video-download-control",
                        "worker_version": __version__,
                    },
                    verifier=self.verifier,
                    job_id=lease.job_id,
                    attempt_id=lease.attempt_id,
                    source_item_id=lease.source_item_id,
                    auxiliary_files=(
                        *thumbnails_by_key[produced.media_key],
                        *captions_by_key[produced.media_key],
                    ),
                )
                staged_assets.append(staged)
                heartbeat.raise_if_failed()
                if heartbeat.is_cancelled():
                    heartbeat.stop()
                    self.repository.finish_canceled(
                        lease,
                        now=self.clock(),
                        remove_pending_asset=self.asset_store.remove_pending_asset,
                    )
                    durable_intent_ids.clear()
                    return self._finish_result(
                        lease,
                        result_status="canceled",
                        started=cycle_started,
                    )
                self.repository.record_asset_commit_intent(
                    lease,
                    asset=staged,
                    now=self.clock(),
                )
                durable_intent_ids.add(staged.asset_id)
            heartbeat.stop()
            self._emit_phase(lease, "committing")
            final_status = self.repository.finalize_asset_commit_intents(
                lease,
                assets=staged_assets,
                publish_staged=self.asset_store.publish_staged,
                remove_pending_asset=self.asset_store.remove_pending_asset,
                now=self.clock(),
            )
            assets_registered = final_status == JobStatus.READY
            durable_intent_ids.clear()
            return self._finish_result(
                lease,
                result_status=final_status.value,
                started=cycle_started,
                asset_count=(
                    len(staged_assets) if final_status == JobStatus.READY else 0
                ),
            )
        except LostLease:
            heartbeat.stop()
            return self._finish_result(
                lease,
                result_status="lost_lease",
                started=cycle_started,
                error_code=ErrorCode.WORKER_LOST,
            )
        except AdapterFailure as exc:
            heartbeat.stop()
            return self._handle_failure(
                lease,
                error_code=exc.code,
                diagnostic=exc.diagnostic,
                retry_after_seconds=exc.retry_after,
                started=cycle_started,
            )
        except AssetValidationError as exc:
            heartbeat.stop()
            return self._handle_failure(
                lease,
                error_code=ErrorCode.VALIDATION_FAILED,
                diagnostic=str(exc),
                started=cycle_started,
            )
        except GraphValidationError as exc:
            heartbeat.stop()
            return self._handle_failure(
                lease,
                error_code=ErrorCode.VALIDATION_FAILED,
                diagnostic=str(exc),
                started=cycle_started,
            )
        except OSError as exc:
            heartbeat.stop()
            self._pause(ErrorCode.STORAGE_ERROR, lease=lease, exception=exc)
            return self._handle_failure(
                lease,
                error_code=ErrorCode.STORAGE_ERROR,
                diagnostic=f"storage operation failed ({type(exc).__name__})",
                started=cycle_started,
            )
        except Exception as exc:  # noqa: BLE001 - injected adapter safety boundary
            heartbeat.stop()
            return self._handle_failure(
                lease,
                error_code=ErrorCode.WORKER_INTERNAL,
                diagnostic=f"unhandled {type(exc).__name__}",
                started=cycle_started,
            )
        finally:
            heartbeat.stop()
            if not assets_registered:
                for staged in staged_assets:
                    if staged.asset_id in durable_intent_ids:
                        continue
                    try:
                        self.asset_store.remove_pending_asset(
                            staged.asset_id,
                            staged.attempt_id,
                        )
                    except (OSError, AssetValidationError) as exc:
                        self._emit_cleanup_failure(
                            lease,
                            operation="pending_asset",
                            exception=exc,
                        )
                        self._pause(
                            ErrorCode.STORAGE_ERROR,
                            lease=lease,
                            exception=exc,
                        )
            if attempt_paths is not None:
                try:
                    self.asset_store.cleanup_attempt(attempt_paths)
                except (OSError, AssetValidationError) as exc:
                    self._emit_cleanup_failure(
                        lease,
                        operation="attempt_directory",
                        exception=exc,
                    )
                    self._pause(
                        ErrorCode.STORAGE_ERROR,
                        lease=lease,
                        exception=exc,
                    )

    @staticmethod
    def _group_auxiliary_files(
        files: Sequence[ProducedFile],
        *,
        owners: dict[str, object],
        kind: str,
        media_kind: str,
        per_asset_limit: int,
        total_limit: int,
    ) -> dict[str, list[AuxiliaryFile]]:
        if len(files) > total_limit:
            raise AssetValidationError("auxiliary artifact count exceeds limit")
        ordinals = {item.ordinal for item in files}
        if ordinals != set(range(len(files))):
            raise AssetValidationError(
                "auxiliary ordinals must be unique and contiguous"
            )
        grouped: dict[str, list[AuxiliaryFile]] = {
            media_key: [] for media_key in owners
        }
        for produced in files:
            if produced.media_key not in owners:
                raise AssetValidationError("auxiliary artifact has no original owner")
            if produced.role != kind or produced.media_kind != media_kind:
                raise AssetValidationError("auxiliary artifact contract mismatch")
            group = grouped[produced.media_key]
            if len(group) >= per_asset_limit:
                raise AssetValidationError("auxiliary artifact count exceeds limit")
            group.append(
                AssetStore.describe_auxiliary_file(
                    path=produced.path,
                    kind=kind,
                    ordinal=produced.ordinal,
                )
            )
        return grouped

    @staticmethod
    def _validate_unique_produced_paths(files: Sequence[ProducedFile]) -> None:
        seen: set[object] = set()
        for produced in files:
            try:
                resolved = produced.path.resolve(strict=True)
                info = resolved.stat()
            except OSError as exc:
                raise AssetValidationError(
                    "produced file is missing or unreadable"
                ) from exc
            identity = (info.st_dev, info.st_ino)
            if identity in seen:
                raise AssetValidationError("produced file path is duplicated")
            seen.add(identity)

    @staticmethod
    def _validate_output_inventory(
        output_dir: Path,
        files: Sequence[ProducedFile],
    ) -> None:
        """Reject unreported downloader output, including fetched siblings."""

        try:
            expected = {item.path.resolve(strict=True) for item in files}
            observed: set[object] = set()
            for root, directories, filenames in os.walk(
                output_dir,
                topdown=True,
                followlinks=False,
            ):
                root_path = Path(root)
                for directory in directories:
                    if (root_path / directory).is_symlink():
                        raise AssetValidationError(
                            "adapter output contains an untrusted link"
                        )
                for filename in filenames:
                    candidate = root_path / filename
                    if candidate.is_symlink():
                        raise AssetValidationError(
                            "adapter output contains an untrusted link"
                        )
                    observed.add(candidate.resolve(strict=True))
        except AssetValidationError:
            raise
        except OSError as exc:
            raise AssetValidationError(
                "adapter output inventory is unreadable"
            ) from exc
        if observed != expected:
            raise AssetValidationError("adapter output inventory is not exact")

    def _emit(self, event: str, *, level: str = "INFO", **fields: object) -> None:
        if self.runtime_logger is None:
            return
        try:
            self.runtime_logger.emit(event, level=level, **fields)
        except Exception:  # noqa: BLE001 - logging must remain observational
            # An injected logging implementation must remain observational.
            return

    def _emit_phase(self, lease: JobLease, phase: str) -> None:
        self._emit(
            "worker.job_phase",
            worker_id=self._log_worker_id,
            job_id=lease.job_id,
            attempt_id=lease.attempt_id,
            phase=phase,
        )

    def _emit_cleanup_failure(
        self,
        lease: JobLease | None,
        *,
        operation: str,
        exception: BaseException,
    ) -> None:
        fields: dict[str, object] = {
            "worker_id": self._log_worker_id,
            "error_code": ErrorCode.STORAGE_ERROR.value,
            "exception_type": safe_exception_type(exception),
            "failure_site": operation,
            "cleanup_operation": operation,
        }
        if lease is not None:
            fields["job_id"] = lease.job_id
            fields["attempt_id"] = lease.attempt_id
        self._emit("worker.cleanup_failed", level="ERROR", **fields)

    def _finish_result(
        self,
        lease: JobLease,
        *,
        result_status: str,
        started: float,
        error_code: ErrorCode | None = None,
        asset_count: int = 0,
    ) -> WorkerRunResult:
        if result_status in {"failed", "lost_lease"}:
            level = "ERROR"
        elif error_code is not None or result_status in {"canceled", "queued"}:
            level = "WARNING"
        else:
            level = "INFO"
        fields: dict[str, object] = {
            "worker_id": self._log_worker_id,
            "job_id": lease.job_id,
            "attempt_id": lease.attempt_id,
            "result_status": result_status,
            "asset_count": asset_count,
            "duration_ms": _elapsed_ms(started),
        }
        if error_code is not None:
            fields["error_code"] = error_code.value
        self._emit("worker.job_finished", level=level, **fields)
        return WorkerRunResult(
            lease.job_id,
            lease.attempt_id,
            result_status,
            error_code,
        )

    def _pause(
        self,
        error_code: ErrorCode,
        *,
        lease: JobLease | None = None,
        exception: BaseException | None = None,
    ) -> None:
        self._paused = True
        self._pause_error_code = error_code
        pause_failure: BaseException | None = None
        try:
            self.repository.pause_queue(reason=error_code, now=self.clock())
        except Exception as exc:  # noqa: BLE001 - queue stays locally fail-closed
            # Keep this process fail-closed even if the control database is
            # itself the storage component that became unavailable.
            pause_failure = exc
        fields: dict[str, object] = {
            "worker_id": self._log_worker_id,
            "error_code": error_code.value,
            "failure_site": "queue_pause",
        }
        if lease is not None:
            fields["job_id"] = lease.job_id
            fields["attempt_id"] = lease.attempt_id
        observed_exception = pause_failure or exception
        if observed_exception is not None:
            fields["exception_type"] = safe_exception_type(observed_exception)
        self._emit("worker.queue_paused", level="ERROR", **fields)

    def _handle_failure(
        self,
        lease: JobLease,
        *,
        error_code: ErrorCode,
        diagnostic: str,
        retry_after_seconds: float | None = None,
        started: float,
    ) -> WorkerRunResult:
        try:
            attempts = self.repository.attempts_for(
                lease.job_id,
                run_generation=lease.run_generation,
            )
            attempts_on_adapter = sum(
                attempt["adapter"] == lease.adapter for attempt in attempts
            )
            decision = self.retry_policy.decide(
                error_code,
                RetryContext(
                    total_attempts=len(attempts),
                    attempts_on_current_adapter=attempts_on_adapter,
                    fallback_available=False,
                    retry_after_seconds=retry_after_seconds,
                ),
            )
            self._emit(
                "worker.retry_decided",
                level="WARNING",
                worker_id=self._log_worker_id,
                job_id=lease.job_id,
                attempt_id=lease.attempt_id,
                error_code=error_code.value,
                retry_action=decision.action.value,
                retry_delay_ms=min(
                    max(round(decision.delay_seconds * 1000), 0),
                    1_000_000_000,
                ),
            )
            retry_at = (
                self.clock() + timedelta(seconds=decision.delay_seconds)
                if decision.action == RetryAction.RETRY
                else None
            )
            status = self.repository.finish_failure(
                lease,
                error_code=error_code,
                diagnostic=diagnostic,
                now=self.clock(),
                retry_at=retry_at,
                remove_pending_asset=self.asset_store.remove_pending_asset,
            )
        except Exception as exc:
            self._emit(
                "worker.cycle_failed",
                level="ERROR",
                worker_id=self._log_worker_id,
                exception_type=safe_exception_type(exc),
                duration_ms=_elapsed_ms(started),
            )
            raise
        final_error = None if status == JobStatus.CANCELED else error_code
        return self._finish_result(
            lease,
            result_status=status.value,
            started=started,
            error_code=final_error,
        )
