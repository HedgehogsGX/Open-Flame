"""Own the editing service, worker, activity lease, and cancellation lifecycle."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from threading import Condition, Event, RLock, Thread, current_thread
from time import monotonic
from typing import Any

from ..uploads.activity_lock import UploadActivityBusy, UploadActivityLease
from .ai import default_capabilities
from .ai_authorization import (
    AiAuthorizationError,
    parse_operation_authorization,
    require_authorization_match,
)
from .ai_bridge import AiBridgeError
from .ai_execution import AiTaskExecutor
from .ai_render import AiRenderProcessor
from .ai_runtime import default_ai_runtime_root
from .contracts import EditingError, MediaProcessor, recipe_from_mapping
from .service import EditingService, render_ordinary_plan

_PROGRESS_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")


class EditingManager:
    """Lazily own one editing service and one bounded local worker."""

    def __init__(
        self,
        root: Path,
        processor_factory: Callable[[], MediaProcessor] | None,
        *,
        ai_runtime_root: Path | None = None,
        _service_factory: Callable[..., EditingService] | None = None,
    ) -> None:
        self.root = root
        self.processor_factory = processor_factory
        self._service_factory = (
            EditingService if _service_factory is None else _service_factory
        )
        self.ai_runtime_root = Path(
            ai_runtime_root if ai_runtime_root is not None else default_ai_runtime_root(root)
        )
        self._ai_executor = AiTaskExecutor(
            self.ai_runtime_root, self.root / "ai-work"
        )
        self._lock = RLock()
        self._state_changed = Condition(self._lock)
        self._wake = Event()
        self._stop = Event()
        self._current_cancel: Event | None = None
        self._current_plan_id: str | None = None
        self._current_ai_cancel: Event | None = None
        self._current_ai_task_id: str | None = None
        self._current_ai_progress: tuple[float, str] | None = None
        self._service: EditingService | None = None
        self._thread: Thread | None = None
        self._worker_active = False
        self._active_operations = 0
        self._activity_lease: UploadActivityLease | None = None

    @property
    def media_ready(self) -> bool:
        return self.processor_factory is not None

    def get(self) -> EditingService:
        with self._lock:
            if self._stop.is_set():
                raise EditingError("editing_manager_stopped")
            if self._service is not None:
                return self._service
            try:
                lease = UploadActivityLease.acquire(self.root, exclusive=True)
            except UploadActivityBusy:
                raise EditingError("editing_worker_busy") from None
            try:
                processor = (
                    self.processor_factory() if self.processor_factory else None
                )
                service = self._service_factory(
                    self.root,
                    processor=processor,
                    recover_interrupted=False,
                )
                service.recover_interrupted(cleanup_orphans=True)
                self._activity_lease = lease
                self._service = service
                thread = Thread(
                    target=self._worker,
                    name="open-flame-editing-worker",
                    daemon=True,
                )
                self._thread = thread
                self._worker_active = True
                thread.start()
            except BaseException:
                self._service = None
                self._thread = None
                self._worker_active = False
                self._activity_lease = None
                lease.release()
                raise
            return self._service

    def _begin_operation(self) -> EditingService:
        with self._lock:
            if self._stop.is_set():
                raise EditingError("editing_manager_stopped")
            self._active_operations += 1
            try:
                return self.get()
            except BaseException:
                self._active_operations -= 1
                self._state_changed.notify_all()
                raise

    def _take_quiescent_lease_locked(self) -> UploadActivityLease | None:
        if (
            not self._stop.is_set()
            or self._worker_active
            or self._active_operations
        ):
            return None
        lease = self._activity_lease
        self._activity_lease = None
        return lease

    def _finish_operation(self) -> None:
        with self._state_changed:
            self._active_operations -= 1
            lease = self._take_quiescent_lease_locked()
            self._state_changed.notify_all()
        if lease is not None:
            lease.release()

    def invoke(self, method: str, *args, **kwargs):
        """Run one service operation while retaining the root activity lease."""

        service = self._begin_operation()
        try:
            return getattr(service, method)(*args, **kwargs)
        finally:
            self._finish_operation()

    def wake(self) -> None:
        self._wake.set()

    def cancel(self, plan_id: str) -> dict[str, Any]:
        service = self._begin_operation()
        try:
            result = service.cancel_plan(plan_id)
            with self._lock:
                if self._current_plan_id == plan_id and self._current_cancel is not None:
                    self._current_cancel.set()
            self._wake.set()
            return result
        finally:
            self._finish_operation()

    def runtime_status(self) -> dict[str, object]:
        result = self._ai_executor.runtime_status()
        with self._lock:
            active = (
                None
                if self._current_ai_task_id is None
                else {
                    "task_id": self._current_ai_task_id,
                    "progress": (
                        None
                        if self._current_ai_progress is None
                        else {
                            "fraction": self._current_ai_progress[0],
                            "code": self._current_ai_progress[1],
                        }
                    ),
                }
            )
        return {**result, "active_task": active}

    def capabilities(self) -> list[dict[str, object]]:
        defaults = [
            item.to_dict() for item in default_capabilities(media_ready=self.media_ready)
        ]
        local = [item for item in defaults if item["operation"] in {"segment", "cover"}]
        return [*local, *self._ai_executor.capabilities()]

    @staticmethod
    def _as_editing_error(
        error: AiBridgeError | AiAuthorizationError,
    ) -> EditingError:
        return EditingError(error.code)

    def validate_ai_operation(
        self,
        operation: str,
        provider_id: str,
        model_id: str,
        stored_authorization: object,
        *,
        expected_authorization_sha256: str,
        voice_id: str | None = None,
    ) -> None:
        """Match a frozen workflow authorization against the current runtime."""

        try:
            current = self._ai_executor.operation_authorization(
                operation,
                provider_id,
                model_id,
                voice_id=voice_id,
            )
            require_authorization_match(
                stored_authorization,
                current,
                expected_sha256=expected_authorization_sha256,
            )
        except (AiBridgeError, AiAuthorizationError) as error:
            raise self._as_editing_error(error) from None

    def create_ai_task(
        self,
        project_id: str,
        operation: str,
        provider_id: str,
        model_id: str,
        options: dict[str, object],
        idempotency_key: str,
        *,
        source_revision_id: str | None = None,
        expected_authorization_sha256: str | None = None,
    ) -> dict[str, Any]:
        if expected_authorization_sha256 is None:
            raise EditingError("ai_authorization_binding_required")
        try:
            authorization = self._ai_executor.operation_authorization(
                operation, provider_id, model_id
            )
            require_authorization_match(
                authorization,
                authorization,
                expected_sha256=expected_authorization_sha256,
            )
        except (AiBridgeError, AiAuthorizationError) as error:
            raise self._as_editing_error(error) from None
        return self.invoke(
            "create_ai_task",
            project_id,
            operation,
            provider_id,
            model_id,
            options,
            idempotency_key,
            source_revision_id=source_revision_id,
            authorization=authorization.to_dict(),
        )

    def confirm_ai_task(
        self,
        task_id: str,
        *,
        expected_request_sha256: str | None = None,
        expected_authorization_sha256: str | None = None,
        ai_data_egress_accepted: bool = False,
    ) -> dict[str, Any]:
        service = self._begin_operation()
        try:
            task = service.ai_task(task_id)
            if expected_authorization_sha256 is None:
                raise EditingError("invalid_ai_task_confirmation")
            try:
                current = self._ai_executor.operation_authorization(
                    task["operation"], task["provider"], task["model"]
                )
                authorization = require_authorization_match(
                    task.get("authorization"),
                    current,
                    expected_sha256=expected_authorization_sha256,
                )
            except (AiBridgeError, AiAuthorizationError) as error:
                raise self._as_editing_error(error) from None
            if type(ai_data_egress_accepted) is not bool:
                raise EditingError("invalid_ai_task_confirmation")
            if authorization.execution == "remote" and not ai_data_egress_accepted:
                raise EditingError("ai_data_egress_confirmation_required")
            if expected_request_sha256 is None:
                raise EditingError("invalid_ai_task_confirmation")
            result = service.confirm_ai_task(
                task_id,
                expected_request_sha256=expected_request_sha256,
            )
            self._wake.set()
            return result
        finally:
            self._finish_operation()

    def confirm_plan(
        self,
        plan_id: str,
        *,
        expected_recipe_sha256: str | None = None,
        expected_authorization_sha256: str | None = None,
        ai_data_egress_accepted: bool = False,
    ) -> dict[str, Any]:
        service = self._begin_operation()
        try:
            plan = service.plan(plan_id)
            recipe = recipe_from_mapping(plan["recipe"])
            if recipe.dubbing.enabled:
                if expected_authorization_sha256 is None:
                    raise EditingError("invalid_plan_confirmation")
                try:
                    authorization = parse_operation_authorization(
                        recipe.dubbing.authorization
                    )
                    current = self._ai_executor.operation_authorization(
                        "synthesize",
                        recipe.dubbing.provider,
                        recipe.dubbing.model,
                    )
                    require_authorization_match(
                        authorization,
                        current,
                        expected_sha256=expected_authorization_sha256,
                    )
                except (AiBridgeError, AiAuthorizationError) as error:
                    raise self._as_editing_error(error) from None
                if authorization.execution == "remote" and not ai_data_egress_accepted:
                    raise EditingError("ai_data_egress_confirmation_required")
            result = service.confirm_plan(
                plan_id,
                expected_recipe_sha256=expected_recipe_sha256,
                ai_data_egress_accepted=ai_data_egress_accepted,
            )
            self._wake.set()
            return result
        finally:
            self._finish_operation()

    def cancel_ai_tasks(self, task_ids: Sequence[str]) -> list[dict[str, Any]]:
        service = self._begin_operation()
        try:
            result = service.cancel_ai_tasks(task_ids)
            self._signal_ai_cancellations(result)
            return result
        finally:
            self._finish_operation()

    def cancel_ai_project_tasks(self, project_id: str) -> list[dict[str, Any]]:
        service = self._begin_operation()
        try:
            result = service.cancel_ai_project_tasks(project_id)
            self._signal_ai_cancellations(result)
            return result
        finally:
            self._finish_operation()

    def cancel_workflow_request_artifacts(
        self,
        project_request_key: str,
        plan_request_key: str,
        *,
        expected_project_id: str | None,
        expected_name: str,
        expected_source_asset_id: str,
        expected_recipe: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Atomically discover workflow editing artifacts and signal active workers."""

        service = self._begin_operation()
        try:
            result = service.cancel_workflow_request_artifacts(
                project_request_key,
                plan_request_key,
                expected_project_id=expected_project_id,
                expected_name=expected_name,
                expected_source_asset_id=expected_source_asset_id,
                expected_recipe=expected_recipe,
            )
            plan = result.get("plan")
            if isinstance(plan, Mapping):
                plan_id = plan.get("id")
                with self._lock:
                    if (
                        self._current_plan_id == plan_id
                        and self._current_cancel is not None
                    ):
                        self._current_cancel.set()
            tasks = result.get("ai_tasks")
            if isinstance(tasks, Sequence) and not isinstance(tasks, (str, bytes)):
                self._signal_ai_cancellations(tasks)
            else:
                self._wake.set()
            return result
        finally:
            self._finish_operation()

    def _signal_ai_cancellations(self, result: Sequence[Mapping[str, Any]]) -> None:
        result_ids = {
            task.get("id") for task in result if isinstance(task, Mapping)
        }
        with self._lock:
            if (
                self._current_ai_task_id in result_ids
                and self._current_ai_cancel is not None
            ):
                self._current_ai_cancel.set()
        self._wake.set()

    def cancel_ai_task(self, task_id: str) -> dict[str, Any]:
        return self.cancel_ai_tasks((task_id,))[0]

    def _worker(self) -> None:
        try:
            self._worker_loop()
        except EditingError:
            if not self._stop.is_set():
                raise
        finally:
            with self._state_changed:
                self._worker_active = False
                lease = self._take_quiescent_lease_locked()
                self._state_changed.notify_all()
            if lease is not None:
                lease.release()

    def _worker_loop(self) -> None:
        service = self.get()
        while not self._stop.is_set():
            ai_claim = None
            claim_ai_task = getattr(service, "claim_next_ai_task", None)
            try:
                if callable(claim_ai_task):
                    ai_claim = claim_ai_task()
            except EditingError:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            if ai_claim is not None:
                self._process_ai_claim(service, ai_claim)
                continue

            if getattr(service, "processor", None) is None:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            claim = None
            try:
                claim = service.claim_next_plan()
            except EditingError:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            if claim is None:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            plan_id, token = claim["id"], claim["claim_token"]
            cancel_event = Event()
            with self._lock:
                self._current_plan_id = plan_id
                self._current_cancel = cancel_event
                if self._stop.is_set():
                    cancel_event.set()
            # A cancellation can land after the database claim but before the
            # in-memory event is published.  Reconcile the durable state once
            # the event is visible; later cancellations set the event directly.
            try:
                if service.plan_cancellation_requested(plan_id, token):
                    cancel_event.set()
            except EditingError:
                pass
            try:
                assert service.processor is not None
                source, source_size, source_sha256 = service.source_identity_for_plan(
                    plan_id
                )
                recipe = recipe_from_mapping(claim["recipe"])
                output_dir = service.output_dir_for_plan(plan_id, token)
                if recipe.translation.enabled or recipe.dubbing.enabled:
                    speech_provider = None
                    if recipe.dubbing.enabled:
                        speech_provider = self._ai_executor.speech_provider(
                            recipe.dubbing.provider,
                            recipe.dubbing.model,
                            recipe.dubbing.authorization,
                            service=service,
                            render_plan_id=plan_id,
                            owner_claim_token=token,
                        )
                    result = AiRenderProcessor(service.processor).render(
                        source,
                        output_dir,
                        recipe,
                        timeline=service.approved_timeline_for_plan(plan_id),
                        speech_provider=speech_provider,
                        cancel_event=cancel_event,
                        expected_source_size=source_size,
                        expected_source_sha256=source_sha256,
                    )
                else:
                    result = render_ordinary_plan(
                        service.processor,
                        source,
                        output_dir,
                        recipe,
                        cancel_event=cancel_event,
                        expected_source_size=source_size,
                        expected_source_sha256=source_sha256,
                    )
                service.complete_plan(plan_id, token, result)
            except AiBridgeError as error:
                try:
                    service.fail_plan(
                        plan_id,
                        token,
                        error.code,
                        canceled=cancel_event.is_set(),
                    )
                except EditingError:
                    pass
            except EditingError as error:
                try:
                    service.fail_plan(
                        plan_id,
                        token,
                        error.code,
                        canceled=cancel_event.is_set(),
                    )
                except EditingError:
                    pass
            except Exception:
                try:
                    service.fail_plan(
                        plan_id,
                        token,
                        "processor_failed",
                        canceled=cancel_event.is_set(),
                    )
                except EditingError:
                    pass
            finally:
                with self._lock:
                    self._current_plan_id = None
                    self._current_cancel = None

    def _process_ai_claim(
        self, service: EditingService, claim: dict[str, Any]
    ) -> None:
        task_id = claim.get("id")
        token = claim.get("claim_token")
        if not isinstance(task_id, str) or not isinstance(token, str):
            return
        cancel_event = Event()
        with self._lock:
            self._current_ai_task_id = task_id
            self._current_ai_cancel = cancel_event
            self._current_ai_progress = None
            if self._stop.is_set():
                cancel_event.set()

        def cancelled() -> bool:
            if cancel_event.is_set():
                return True
            try:
                if service.ai_task_cancellation_requested(task_id, token):
                    cancel_event.set()
            except (EditingError, sqlite3.Error):
                cancel_event.set()
            return cancel_event.is_set()

        def progress(fraction: float, code: str) -> None:
            if (
                isinstance(fraction, bool)
                or not isinstance(fraction, (int, float))
                or not 0.0 <= float(fraction) <= 1.0
                or not isinstance(code, str)
                or not _PROGRESS_CODE.fullmatch(code)
            ):
                raise AiBridgeError("ai_progress_invalid")
            with self._lock:
                if self._current_ai_task_id == task_id:
                    self._current_ai_progress = (float(fraction), code)

        try:
            if cancelled():
                raise AiBridgeError("ai_operation_canceled")
            self._ai_executor.execute(
                service,
                claim,
                progress=progress,
                cancelled=cancelled,
                cancel_event=cancel_event,
            )
        except AiBridgeError as error:
            try:
                service.fail_ai_task(
                    task_id,
                    token,
                    error.code,
                    canceled=(cancelled() or error.code == "ai_operation_canceled"),
                )
            except EditingError:
                pass
        except EditingError:
            try:
                service.fail_ai_task(
                    task_id,
                    token,
                    "ai_execution_failed",
                    canceled=cancelled(),
                )
            except EditingError:
                pass
        except Exception:
            try:
                service.fail_ai_task(
                    task_id,
                    token,
                    "ai_execution_failed",
                    canceled=cancelled(),
                )
            except EditingError:
                pass
        finally:
            with self._lock:
                if self._current_ai_task_id == task_id:
                    self._current_ai_task_id = None
                    self._current_ai_cancel = None
                    self._current_ai_progress = None

    def stop(self, *, timeout_seconds: float = 10) -> None:
        deadline = monotonic() + max(0.0, timeout_seconds)
        with self._state_changed:
            self._stop.set()
            self._wake.set()
            if self._current_cancel is not None:
                self._current_cancel.set()
            if self._current_ai_cancel is not None:
                self._current_ai_cancel.set()
            thread = self._thread
        if thread is current_thread():
            return
        if thread is not None:
            thread.join(timeout=max(0.0, deadline - monotonic()))
        with self._state_changed:
            while self._worker_active or self._active_operations:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return
                self._state_changed.wait(timeout=remaining)
            lease = self._take_quiescent_lease_locked()
        if lease is not None:
            lease.release()

    def resolve_output(self, output_id: str) -> tuple[Path, str, str]:
        service = self._begin_operation()
        try:
            record = service.asset(output_id)
            if record["kind"] not in {"segment", "dubbed_video"}:
                raise EditingError("edit_output_not_video")
            return service.asset_path(output_id), record["sha256"], record["name"]
        finally:
            self._finish_operation()

    def resolve_cover(self, output_id: str) -> tuple[Path, str, str]:
        service = self._begin_operation()
        try:
            record = service.asset(output_id)
            if record["kind"] != "cover":
                raise EditingError("edit_output_not_cover")
            return service.asset_path(output_id), record["sha256"], record["name"]
        finally:
            self._finish_operation()


__all__ = ["EditingManager"]
