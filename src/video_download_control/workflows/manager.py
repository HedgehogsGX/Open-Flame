"""Lazy owner and bounded background reconciler for workflow records."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import Condition, Event, RLock, Thread, current_thread
from time import monotonic

from .contracts import WorkflowDomainAdapter
from .contracts import WorkflowError
from .service import WorkflowService


_MIN_RECONCILE_SECONDS = 0.75
_MAX_RECONCILE_SECONDS = 6.0


class WorkflowManager:
    def __init__(self, root: Path, adapter: WorkflowDomainAdapter) -> None:
        self.root = Path(root)
        self.adapter = adapter
        self._lock = RLock()
        self._changed = Condition(self._lock)
        self._wake = Event()
        self._stop = Event()
        self._service: WorkflowService | None = None
        self._thread: Thread | None = None
        self._worker_active = False
        self._active_operations = 0
        self._scan_cursor: tuple[str, str] | None = None

    def get(self) -> WorkflowService:
        with self._lock:
            if self._stop.is_set():
                raise WorkflowError("workflow_manager_stopped")
            if self._service is None:
                service = WorkflowService(self.root, self.adapter)
                try:
                    thread = Thread(
                        target=self._worker,
                        name="open-flame-workflow-worker",
                        daemon=True,
                    )
                    self._service = service
                    self._thread = thread
                    self._worker_active = True
                    thread.start()
                except BaseException:
                    # A failed launch must not publish an owner with no worker.
                    # Keep ownership if an interrupt arrived after the thread
                    # actually started; clearing it could create a second owner.
                    if self._thread is None or self._thread.ident is None:
                        self._service = None
                        self._thread = None
                        self._worker_active = False
                    self._changed.notify_all()
                    raise
            return self._service

    def invoke(self, method: str, *args, **kwargs):
        with self._changed:
            if self._stop.is_set():
                raise WorkflowError("workflow_manager_stopped")
            self._active_operations += 1
        try:
            service = self.get()
            return getattr(service, method)(*args, **kwargs)
        finally:
            with self._changed:
                self._active_operations -= 1
                self._changed.notify_all()

    def wake(self) -> None:
        self._wake.set()

    def resume_existing(self) -> bool:
        """Start reconciliation on app startup when a workflow store exists."""

        database = self.root / "workflows.sqlite3"
        if not database.exists() and not database.is_symlink():
            return False
        self.get()
        self._wake.set()
        return True

    def _worker(self) -> None:
        try:
            service = self.get()
            wait_seconds = _MIN_RECONCILE_SECONDS
            while not self._stop.is_set():
                scan_failed = False
                try:
                    active = service.active_page(self._scan_cursor, limit=100)
                except (WorkflowError, sqlite3.Error):
                    # A failed read has not dispatched any domain work, so the
                    # same worker can safely retry the scan.  Keeping the
                    # thread alive also avoids restarting it after an unknown
                    # exception, which could replay work whose remote outcome
                    # is not known.
                    active = []
                    scan_failed = True
                changed = False
                if not scan_failed:
                    if active:
                        tail = active[-1]
                        self._scan_cursor = (tail["created_at"], tail["id"])
                    else:
                        self._scan_cursor = None
                for item in active:
                    if self._stop.is_set():
                        break
                    try:
                        result = service.advance(item["id"])
                        changed = changed or result.get("revision") != item.get(
                            "revision"
                        )
                    except WorkflowError as exc:
                        if exc.code in {
                            "workflow_database_unavailable",
                            "workflow_manager_stopped",
                        }:
                            continue
                        try:
                            service.require_attention(
                                item["id"],
                                exc.code,
                                expected_revision=item["revision"],
                            )
                            changed = True
                        except (WorkflowError, sqlite3.Error):
                            pass
                    except sqlite3.Error:
                        continue
                    except Exception:
                        try:
                            service.require_attention(
                                item["id"],
                                "workflow_domain_failed",
                                expected_revision=item["revision"],
                            )
                            changed = True
                        except (WorkflowError, sqlite3.Error):
                            pass
                if changed:
                    wait_seconds = _MIN_RECONCILE_SECONDS
                elif not active and not scan_failed:
                    wait_seconds = _MAX_RECONCILE_SECONDS
                else:
                    wait_seconds = min(
                        _MAX_RECONCILE_SECONDS,
                        max(
                            _MIN_RECONCILE_SECONDS,
                            wait_seconds * 1.6,
                        ),
                    )
                if self._wake.wait(wait_seconds):
                    wait_seconds = _MIN_RECONCILE_SECONDS
                self._wake.clear()
        finally:
            with self._changed:
                self._worker_active = False
                self._changed.notify_all()

    def stop(self, *, timeout_seconds: float = 10.0) -> None:
        deadline = monotonic() + max(0.0, timeout_seconds)
        with self._changed:
            self._stop.set()
            self._wake.set()
            thread = self._thread
        if thread is current_thread():
            return
        if thread is not None:
            thread.join(timeout=max(0.0, deadline - monotonic()))
        with self._changed:
            while self._worker_active or self._active_operations:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return
                self._changed.wait(remaining)
