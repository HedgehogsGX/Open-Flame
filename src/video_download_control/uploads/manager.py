"""Lazy Upload lifecycle shared by HTTP, workflows, and application shutdown."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING

from .contracts import UploadError

if TYPE_CHECKING:
    from .service import UploadService


class UploadManager:
    def __init__(
        self, root: Path, *, service_factory: Callable[[Path], UploadService]
    ) -> None:
        self.root = Path(root)
        self.service_factory = service_factory
        self.lock = RLock()
        self.service: UploadService | None = None
        self.closed = False

    def get(self) -> UploadService:
        with self.lock:
            if self.closed:
                raise UploadError("uploader_stopped")
            if self.service is None:
                candidate = self.service_factory(self.root)
                try:
                    candidate.start()
                except BaseException:
                    try:
                        candidate.stop()
                    except BaseException:
                        # Retain ownership when cleanup could not establish a
                        # stop. A later stop/recover must use the same instance.
                        self.service = candidate
                    raise
                self.service = candidate
            return self.service

    def stop(self) -> None:
        with self.lock:
            self.closed = True
            current = self.service
        if current is not None:
            current.stop()

    def recover(self) -> dict:
        with self.lock:
            if self.closed:
                raise UploadError("uploader_stopped")
            current = self.service
            if current is None:
                current = self.get()
            else:
                try:
                    current.start()
                except UploadError:
                    raise
                except Exception:
                    raise UploadError("scheduler_recovery_failed") from None
            return current.status()
