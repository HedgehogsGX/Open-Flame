"""Durable, explicit upload submission, separate from download recovery.

An interrupted upload may already exist on the platform. Such an operation is
never retried automatically. The immutable local draft remains reviewable.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .contracts import BackendResult, PLATFORMS, UploadBackend, UploadError, UploadRequest
from .login_progress import validate_update
from .schema import UploadSchemaError, initialize_upload_schema, validate_upload_schema

MAX_SOURCE_BYTES = 2 * 1024**3
TITLE_LIMITS = {"bilibili": 80, "douyin": 30, "tencent": 100}
_ID = re.compile(r"^[0-9a-f]{32}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_PAGE_CURSOR = re.compile(r"^([0-3]):([1-9][0-9]*)$")
_JOB_PRIORITY = "CASE WHEN j.state='running' THEN 0 WHEN j.state='queued' THEN 1 WHEN j.state IN ('draft','unknown','failed','canceled') THEN 2 ELSE 3 END"
_SOURCE_PRIORITY = ("CASE WHEN EXISTS(SELECT 1 FROM jobs active WHERE active.source_id=s.id "
                    "AND active.state='running') THEN 0 "
                    "WHEN EXISTS(SELECT 1 FROM jobs queued WHERE queued.source_id=s.id "
                    "AND queued.state='queued') THEN 1 "
                    "WHEN EXISTS(SELECT 1 FROM jobs actionable WHERE actionable.source_id=s.id "
                    "AND actionable.state IN ('draft','unknown','failed','canceled')) THEN 2 ELSE 3 END")


def default_upload_root(data_root: Path) -> Path:
    """Keep secrets outside the downloader's recursively copied backup root."""
    return data_root.with_name(data_root.name + "-uploads")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _plain(path: Path, *, directory: bool = False) -> os.stat_result:
    info = path.lstat()
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & reparse
        or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
        or (not directory and info.st_nlink != 1)):
        raise UploadError("unsafe_upload_file")
    return info


def _signature(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise UploadError("invalid_identifier")
    return value


def _text(value: str, maximum: int, *, required: bool = False) -> str:
    if (not isinstance(value, str) or len(value) > maximum
        or any(ord(c) < 32 and c not in "\n\t\r" for c in value)):
        raise UploadError("invalid_metadata")
    if required and not value.strip():
        raise UploadError("invalid_metadata")
    return value.strip()


class _SchedulerLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def acquire(self) -> bool:
        if self.path.exists():
            _plain(self.path)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None


class UploadService:
    def __init__(self, root: Path, backend: UploadBackend | None = None):
        self.root = Path(os.path.abspath(root))
        # Every existing ancestor must be a plain directory, including junctions.
        for ancestor in reversed((self.root, *self.root.parents)):
            if ancestor.exists():
                _plain(ancestor, directory=True)
        database_path = self.root / "uploads.sqlite3"
        if database_path.exists():
            _plain(database_path)
            try:
                validate_upload_schema(database_path)
            except UploadSchemaError:
                raise UploadError("upload_schema_unsupported") from None
        else:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            _plain(self.root, directory=True)
            try:
                initialize_upload_schema(database_path)
            except UploadSchemaError:
                raise UploadError("upload_schema_unsupported") from None
        for name in ("media", "incoming", "private"):
            path = self.root / name
            path.mkdir(exist_ok=True, mode=0o700)
            _plain(path, directory=True)
        self.database_path = database_path
        if backend is None:
            from .backend import SauBackend
            backend = SauBackend(self.root)
        self.backend = backend
        self._shutdown = threading.Event()
        self._wake = threading.Event()
        self._operation_stop = threading.Event()
        self._active_id: str | None = None
        self._active_guard = threading.RLock()
        self._login_presentations: dict[str, dict] = {}
        self._thread: threading.Thread | None = None
        self._lock = _SchedulerLock(self.root / ".worker.lock")
        self._scheduler_state = "stopped"
        self._scheduler_code = ""

    @contextmanager
    def _db(self, *, timeout: float = 10):
        db = sqlite3.connect(self.database_path, timeout=timeout)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                yield db
        finally:
            db.close()

    def start(self) -> None:
        with self._active_guard:
            if self._thread is not None and self._thread.is_alive():
                if self._scheduler_state != "faulted":
                    return
                self._thread.join(timeout=1)
                if self._thread.is_alive():
                    raise UploadError("upload_worker_stopping")
            if not self._lock.acquire():
                self._scheduler_state = "standby"
                self._scheduler_code = "scheduler_owned_by_other_instance"
                return  # Another application owns execution; read/queue still work.
            try:
                was_faulted = self._scheduler_state == "faulted"
                self._login_presentations.clear()
                self._recover_interrupted_records()
                self._shutdown.clear()
                self._scheduler_state = "running"
                self._scheduler_code = "scheduler_recovered" if was_faulted else ""
                self._thread = threading.Thread(target=self._run, name="open-flame-uploads", daemon=True)
                self._thread.start()
            except BaseException as exc:
                self._scheduler_state = "faulted"
                self._scheduler_code = ("scheduler_database_unavailable"
                                        if isinstance(exc, sqlite3.Error) else "scheduler_failed")
                self._lock.release()
                raise

    def stop(self) -> None:
        self._shutdown.set()
        with self._active_guard:
            self._operation_stop.set()
            self._login_presentations.clear()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=20)
            if thread.is_alive():
                raise UploadError("upload_worker_stopping")
        with self._active_guard:
            if self._scheduler_state != "faulted":
                self._scheduler_state = "stopped"
                self._scheduler_code = ""

    def status(self) -> dict:
        return {"backend": self.backend.inspect(),
                "worker_running": bool(self._thread and self._thread.is_alive()
                                       and self._scheduler_state != "faulted"),
                "scheduler_state": self._scheduler_state,
                "scheduler_code": self._scheduler_code,
                "platforms": [{"id": p, "name": name, "title_limit": TITLE_LIMITS[p],
                               "modes": ["publish", "draft"] if p == "tencent" else ["publish"]}
                              for p, name in PLATFORMS.items()],
                "max_source_bytes": MAX_SOURCE_BYTES}

    def accounts(self) -> list[dict]:
        with self._db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM accounts ORDER BY created_at,id")]

    def add_account(self, platform: str, name: str) -> dict:
        if platform not in PLATFORMS:
            raise UploadError("unsupported_upload_platform")
        name = _text(name, 60, required=True)
        account_id = uuid4().hex
        try:
            with self._db() as db:
                db.execute("INSERT INTO accounts(id,platform,name,created_at) VALUES(?,?,?,?)", (account_id, platform, name, _now()))
                return dict(db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone())
        except sqlite3.IntegrityError:
            raise UploadError("account_name_exists") from None

    def account_action(self, account_id: str, action: str) -> dict:
        _identifier(account_id)
        if action not in ("login", "check"):
            raise UploadError("invalid_account_action")
        if (action == "login" and callable(getattr(self.backend, "login_interactive", None))
                and not (self._thread and self._thread.is_alive() and self._lock.handle)):
            raise UploadError("login_owned_by_other_instance")
        if not self.backend.inspect().get("ready"):
            raise UploadError("runtime_missing")
        operation_id, now = uuid4().hex, _now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT id FROM accounts WHERE id=?", (account_id,)).fetchone() is None:
                raise UploadError("account_not_found")
            if db.execute("SELECT id FROM operations WHERE account_id=? AND state IN ('queued','running')", (account_id,)).fetchone():
                raise UploadError("account_operation_active")
            db.execute("INSERT INTO operations(id,account_id,action,created_at,updated_at) VALUES(?,?,?,?,?)", (operation_id, account_id, action, now, now))
            db.execute("UPDATE accounts SET auth_state='checking',code='' WHERE id=?", (account_id,))
            if action == "login":
                # A new login may select a different real platform account.
                # Previous queued approvals must not follow that new session.
                db.execute("UPDATE jobs SET state='draft',code='account_session_changed',updated_at=? WHERE account_id=? AND state='queued'", (now, account_id))
            result = dict(db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone())
        self._wake.set()
        return result

    def operations(self) -> list[dict]:
        with self._db() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM operations ORDER BY created_at DESC,rowid DESC LIMIT 100")]
        with self._active_guard:
            for row in rows:
                view = self._login_view(row)
                row.update(login_phase=view.get("phase", row["state"]),
                           qr_available=bool(view.get("png")), qr_revision=view.get("revision", 0),
                           expires_at=view.get("expires_at"))
        return rows

    def _login_view(self, row: dict) -> dict:
        view = self._login_presentations.get(row["id"], {})
        if (row["action"] != "login" or row["state"] != "running"
                or row["code"] == "cancellation_requested" or self._active_id != row["id"]
                or self._operation_stop.is_set() or self._shutdown.is_set()):
            self._login_presentations.pop(row["id"], None)
            return {}
        if view.get("deadline") is not None and view["deadline"] <= time.monotonic():
            view.update(phase="expired", png=None)
        return view

    def login_qr(self, operation_id: str) -> bytes:
        _identifier(operation_id)
        with self._active_guard, self._db() as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                raise UploadError("operation_not_found")
            view = self._login_view(dict(row))
            if not view.get("png") or view.get("phase") != "waiting_scan":
                raise UploadError("login_qr_unavailable")
            return view["png"]

    def _record_login(self, row: dict, stop: threading.Event, phase, png=None, expires_at=None) -> None:
        phase, png, expires_at = validate_update(phase, png, expires_at)
        with self._active_guard:
            if (self._active_id != row["id"] or self._operation_stop is not stop
                    or stop.is_set() or self._shutdown.is_set()):
                return
            with self._db() as db:
                current = db.execute("SELECT * FROM operations WHERE id=?", (row["id"],)).fetchone()
            if (current is None or current["state"] != "running"
                    or current["code"] == "cancellation_requested"
                    or current["account_id"] != row["account_id"] or current["action"] != "login"):
                return
            previous = self._login_presentations.get(row["id"], {})
            if (previous.get("phase") == phase and previous.get("png") == png
                    and previous.get("expires_at") == expires_at):
                return
            operation_deadline = previous.get("operation_deadline", time.monotonic() + 600)
            deadline = min(operation_deadline, time.monotonic() + max(0, expires_at - time.time())) if expires_at is not None else min(operation_deadline, time.monotonic() + 300)
            image_hash = hashlib.sha256(png).hexdigest() if png else None
            seen = dict(previous.get("seen", {}))
            if image_hash:
                if image_hash in seen:
                    old_deadline, old_expiry = seen[image_hash]
                    deadline, expires_at = min(old_deadline, deadline), old_expiry
                elif len(seen) >= 16:
                    raise ValueError("too_many_login_updates")
                seen[image_hash] = (deadline, expires_at)
            self._login_presentations[row["id"]] = {"phase": phase, "png": png,
                "expires_at": expires_at, "deadline": deadline, "image_hash": image_hash,
                "operation_deadline": operation_deadline, "seen": seen,
                "revision": previous.get("revision", 0) + 1}

    def cancel_operation(self, operation_id: str) -> dict:
        _identifier(operation_id)
        with self._active_guard, self._db() as db:
            self._login_presentations.pop(operation_id, None)
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                raise UploadError("operation_not_found")
            if row["state"] == "queued":
                db.execute("UPDATE operations SET state='canceled',code='canceled',updated_at=? WHERE id=?", (_now(), operation_id))
                db.execute("UPDATE accounts SET auth_state='unchecked',code='canceled' WHERE id=?", (row["account_id"],))
            elif row["state"] == "running":
                db.execute("UPDATE operations SET code='cancellation_requested' WHERE id=?", (operation_id,))
                if self._active_id == operation_id:
                    self._operation_stop.set()
            return dict(db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone())

    @staticmethod
    def _page_key(cursor: str | None, limit: int) -> tuple[int, int] | None:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise UploadError("invalid_page_limit")
        if cursor is None:
            return None
        if not isinstance(cursor, str) or not (match := _PAGE_CURSOR.fullmatch(cursor)):
            raise UploadError("invalid_page_cursor")
        rowid = int(match.group(2))
        if rowid > 9_223_372_036_854_775_807:
            raise UploadError("invalid_page_cursor")
        return int(match.group(1)), rowid

    def source_page(self, *, cursor: str | None = None, limit: int = 50) -> dict:
        key = self._page_key(cursor, limit)
        query = ("SELECT * FROM (SELECT s.*,s.rowid AS _page_rowid," + _SOURCE_PRIORITY
                 + " AS _page_bucket FROM sources s) page")
        parameters: list[int] = []
        if key is not None:
            query += " WHERE (_page_bucket>? OR (_page_bucket=? AND _page_rowid<?))"
            parameters.extend((key[0], key[0], key[1]))
        query += " ORDER BY _page_bucket,_page_rowid DESC LIMIT ?"
        parameters.append(limit + 1)
        with self._db() as db:
            rows = list(db.execute(query, parameters))
        selected = rows[:limit]
        next_cursor = None
        if len(rows) > limit and selected:
            next_cursor = f"{selected[-1]['_page_bucket']}:{selected[-1]['_page_rowid']}"
        return {"items": [self._source_public(row) for row in selected],
                "next_cursor": next_cursor}

    def sources(self) -> list[dict]:
        return self.source_page(limit=200)["items"]

    def source(self, source_id: str) -> dict:
        with self._db() as db:
            row = db.execute("SELECT * FROM sources WHERE id=?", (_identifier(source_id),)).fetchone()
        if row is None:
            raise UploadError("source_not_found")
        return self._source_public(row)

    @staticmethod
    def _source_public(row) -> dict:
        return {key: row[key] for key in ("id", "name", "size", "sha256", "created_at")}

    def import_source(self, path: Path, name: str, expected_sha256: str | None = None) -> dict:
        name = _text(name, 180, required=True)
        if any(char in name for char in '/\\:\x00') or name in (".", ".."):
            raise UploadError("invalid_source_name")
        suffix = Path(name).suffix.lower()
        if suffix not in (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"):
            raise UploadError("unsupported_video_type")
        try:
            before = _plain(path)
        except OSError:
            raise UploadError("source_unavailable") from None
        if not 0 < before.st_size <= MAX_SOURCE_BYTES:
            raise UploadError("source_size_invalid")
        if shutil.disk_usage(self.root).free < before.st_size + 64 * 1024**2:
            raise UploadError("upload_storage_full")
        source_id = uuid4().hex
        target = self.root / "media" / f"{source_id}{suffix}"
        _plain(target.parent, directory=True)
        digest, total = hashlib.sha256(), 0
        try:
            with path.open("rb") as src, target.open("xb") as dst:
                if _signature(os.fstat(src.fileno())) != _signature(before):
                    raise UploadError("source_changed")
                while chunk := src.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_SOURCE_BYTES:
                        raise UploadError("source_size_invalid")
                    dst.write(chunk)
                    digest.update(chunk)
                if _signature(os.fstat(src.fileno())) != _signature(before):
                    raise UploadError("source_changed")
                dst.flush()
                os.fsync(dst.fileno())
            if total != before.st_size or _signature(_plain(path)) != _signature(before):
                raise UploadError("source_changed")
            if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
                raise UploadError("source_hash_mismatch")
            with self._db() as db:
                db.execute("INSERT INTO sources VALUES(?,?,?,?,?,?)", (source_id, name, suffix, total, digest.hexdigest(), _now()))
                return self._source_public(db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone())
        except BaseException:
            if target.exists():
                target.unlink()
            raise

    def _source_path(self, source_id: str) -> Path:
        _identifier(source_id)
        with self._db() as db:
            row = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if row is None:
            raise UploadError("source_not_found")
        if row["suffix"] not in (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"):
            raise UploadError("source_changed")
        _plain(self.root / "media", directory=True)
        path = self.root / "media" / f"{source_id}{row['suffix']}"
        try:
            before = _plain(path)
            if before.st_size != row["size"]:
                raise UploadError("source_changed")
            with path.open("rb") as handle:
                if _signature(os.fstat(handle.fileno())) != _signature(before):
                    raise UploadError("source_changed")
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
                if _signature(os.fstat(handle.fileno())) != _signature(before):
                    raise UploadError("source_changed")
            if digest != row["sha256"] or _signature(_plain(path)) != _signature(before):
                raise UploadError("source_changed")
        except OSError:
            raise UploadError("source_unavailable") from None
        return path

    @staticmethod
    def _job_public(row) -> dict:
        result = dict(row)
        result.pop("_page_bucket", None)
        result.pop("_page_rowid", None)
        result["tags"] = json.loads(result["tags"])
        return result

    @staticmethod
    def _job_query() -> str:
        return ("SELECT j.*,a.platform,a.name AS account_name,s.name AS source_name,"
                "s.size AS source_size,s.sha256 AS source_sha256 FROM jobs j "
                "JOIN accounts a ON a.id=j.account_id JOIN sources s ON s.id=j.source_id")

    def job_page(self, *, cursor: str | None = None, limit: int = 50) -> dict:
        key = self._page_key(cursor, limit)
        query = ("SELECT * FROM (SELECT j.*,a.platform,a.name AS account_name,"
                 "s.name AS source_name,s.size AS source_size,s.sha256 AS source_sha256,"
                 "j.rowid AS _page_rowid," + _JOB_PRIORITY
                 + " AS _page_bucket FROM jobs j JOIN accounts a ON a.id=j.account_id "
                   "JOIN sources s ON s.id=j.source_id) page")
        parameters: list[int] = []
        if key is not None:
            query += " WHERE (_page_bucket>? OR (_page_bucket=? AND _page_rowid<?))"
            parameters.extend((key[0], key[0], key[1]))
        query += " ORDER BY _page_bucket,_page_rowid DESC LIMIT ?"
        parameters.append(limit + 1)
        with self._db() as db:
            rows = list(db.execute(query, parameters))
        selected = rows[:limit]
        next_cursor = None
        if len(rows) > limit and selected:
            next_cursor = f"{selected[-1]['_page_bucket']}:{selected[-1]['_page_rowid']}"
        return {"items": [self._job_public(row) for row in selected],
                "next_cursor": next_cursor}

    def jobs(self) -> list[dict]:
        return self.job_page(limit=200)["items"]

    def job(self, job_id: str) -> dict:
        with self._db() as db:
            return self._get_job(db, job_id)

    def jobs_by_ids(self, job_ids: list[str]) -> list[dict]:
        if (not isinstance(job_ids, list) or not 1 <= len(job_ids) <= 64
                or len(set(job_ids)) != len(job_ids)):
            raise UploadError("invalid_job_ids")
        for job_id in job_ids:
            _identifier(job_id)
        placeholders = ",".join("?" for _ in job_ids)
        with self._db() as db:
            rows = db.execute(self._job_query() + f" WHERE j.id IN ({placeholders})", job_ids)
            records = {row["id"]: self._job_public(row) for row in rows}
        return [records[job_id] for job_id in job_ids if job_id in records]

    def _get_job(self, db, job_id: str) -> dict:
        row = db.execute(self._job_query() + " WHERE j.id=?", (_identifier(job_id),)).fetchone()
        if row is None:
            raise UploadError("job_not_found")
        return self._job_public(row)

    def create_jobs(self, *, source_id: str, account_ids: list[str], title: str,
                    description: str, tags: list[str], idempotency_key: str,
                    category_id: int | None = None, mode: str = "publish",
                    copyright: int | None = None, source_credit: str = "") -> list[dict]:
        _identifier(source_id)
        if not isinstance(account_ids, list) or not 1 <= len(account_ids) <= 20 or len(set(account_ids)) != len(account_ids):
            raise UploadError("invalid_accounts")
        for account_id in account_ids:
            _identifier(account_id)
        title = _text(title, 100, required=True)
        description = _text(description, 2000)
        source_credit = _text(source_credit, 200)
        if not isinstance(tags, list) or len(tags) > 10:
            raise UploadError("invalid_tags")
        tags = [_text(tag, 20, required=True) for tag in tags]
        if len(set(tags)) != len(tags) or any(any(c in tag for c in ",\n\r\t") for tag in tags):
            raise UploadError("invalid_tags")
        if mode not in ("publish", "draft") or (copyright is not None and (type(copyright) is not int or copyright not in (1, 2))):
            raise UploadError("invalid_metadata")
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", idempotency_key):
            raise UploadError("invalid_idempotency_key")
        payload = [source_id, sorted(account_ids), title, description, tags, category_id, mode, copyright, source_credit]
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM requests WHERE id=?", (idempotency_key,)).fetchone()
            if prior:
                if prior["digest"] != digest:
                    raise UploadError("idempotency_conflict")
                return [self._get_job(db, job_id) for job_id in json.loads(prior["job_ids"])]
            if db.execute("SELECT id FROM sources WHERE id=?", (source_id,)).fetchone() is None:
                raise UploadError("source_not_found")
            accounts = []
            for account_id in account_ids:
                account = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
                if account is None:
                    raise UploadError("account_not_found")
                platform = account["platform"]
                if platform not in PLATFORMS or len(title) > TITLE_LIMITS[platform]:
                    raise UploadError("title_too_long")
                if mode == "draft" and platform != "tencent":
                    raise UploadError("draft_mode_unsupported")
                if platform == "bilibili":
                    if type(category_id) is not int or not 1 <= category_id <= 10000:
                        raise UploadError("bilibili_category_required")
                    if copyright is None:
                        raise UploadError("bilibili_copyright_required")
                    if not tags:
                        raise UploadError("bilibili_tags_required")
                    if copyright == 2 and not source_credit:
                        raise UploadError("source_credit_required")
                accounts.append(account)
            job_ids, now = [], _now()
            for account in accounts:
                job_id = uuid4().hex
                job_ids.append(job_id)
                db.execute("INSERT INTO jobs(id,account_id,source_id,title,description,tags,category_id,mode,copyright,source_credit,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                           (job_id, account["id"], source_id, title, description, json.dumps(tags, ensure_ascii=False), category_id, mode, copyright if copyright is not None else 1, source_credit, now, now))
            db.execute("INSERT INTO requests VALUES(?,?,?)", (idempotency_key, digest, json.dumps(job_ids)))
            return [self._get_job(db, job_id) for job_id in job_ids]

    def confirm(self, job_id: str) -> dict:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self._get_job(db, job_id)
            if job["state"] in ("queued", "running", "submitted", "draft_saved"):
                return job
            if job["state"] != "draft":
                raise UploadError("job_requires_new_draft")
            if not self.backend.inspect().get("ready"):
                raise UploadError("runtime_missing")
            account = db.execute("SELECT auth_state FROM accounts WHERE id=?", (job["account_id"],)).fetchone()
            if account["auth_state"] != "ready":
                raise UploadError("account_not_ready")
            db.execute("UPDATE jobs SET state='queued',code='',updated_at=? WHERE id=?", (_now(), job_id))
            result = self._get_job(db, job_id)
        self._wake.set()
        return result

    def cancel(self, job_id: str) -> dict:
        with self._active_guard, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self._get_job(db, job_id)
            if job["state"] in ("draft", "queued"):
                db.execute("UPDATE jobs SET state='canceled',code='canceled',updated_at=? WHERE id=?", (_now(), job_id))
            elif job["state"] == "running":
                db.execute("UPDATE jobs SET code='cancellation_requested',updated_at=? WHERE id=?", (_now(), job_id))
                if self._active_id == job_id:
                    self._operation_stop.set()
            return self._get_job(db, job_id)

    def retry(self, job_id: str, acknowledge_unknown: bool = False) -> dict:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self._get_job(db, job_id)
            if job["state"] not in ("failed", "canceled", "unknown"):
                raise UploadError("retry_not_allowed")
            if job["state"] == "unknown" and acknowledge_unknown is not True:
                raise UploadError("verify_remote_result_first")
            # Repeated requests for the same retry return the existing successor.
            successor = db.execute("SELECT id FROM jobs WHERE retry_of=? ORDER BY rowid LIMIT 1", (job_id,)).fetchone()
            if successor:
                return self._get_job(db, successor["id"])
            new_id, now = uuid4().hex, _now()
            db.execute("INSERT INTO jobs(id,account_id,source_id,title,description,tags,category_id,mode,copyright,source_credit,created_at,updated_at,retry_of) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (new_id, job["account_id"], job["source_id"], job["title"], job["description"], json.dumps(job["tags"], ensure_ascii=False), job["category_id"], job["mode"], job["copyright"], job["source_credit"], now, now, job_id))
            return self._get_job(db, new_id)

    def _claim(self) -> tuple[str, dict] | None:
        with self._active_guard, self._db() as db:
            if self._shutdown.is_set():
                return None
            db.execute("BEGIN IMMEDIATE")
            operation = db.execute("SELECT o.*,a.platform FROM operations o JOIN accounts a ON a.id=o.account_id WHERE o.state='queued' ORDER BY o.rowid LIMIT 1").fetchone()
            if operation:
                table, kind, row = "operations", "account", dict(operation)
            else:
                job = db.execute(self._job_query() + " WHERE j.state='queued' ORDER BY j.rowid LIMIT 1").fetchone()
                if job is None:
                    return None
                table, kind, row = "jobs", "upload", self._job_public(job)
            db.execute(f"UPDATE {table} SET state='running',code='',updated_at=? WHERE id=?", (_now(), row["id"]))
            self._active_id = row["id"]
            self._operation_stop = threading.Event()
            return kind, row

    def _run(self) -> None:
        try:
            while not self._shutdown.is_set():
                try:
                    claimed = self._claim()
                    if claimed is None:
                        self._wake.wait(0.5)
                        self._wake.clear()
                        continue
                    kind, row = claimed
                    self._execute(kind, row)
                    with self._active_guard:
                        self._active_id = None
                except Exception as exc:
                    with self._active_guard:
                        self._active_id = None
                        self._operation_stop.set()
                    if self._shutdown.is_set() or not self._recover_scheduler_failure(exc):
                        return
        finally:
            self._lock.release()

    def _recover_interrupted_records(self, *, timeout: float = 10) -> None:
        with self._db(timeout=timeout) as db:
            now = _now()
            db.execute("UPDATE jobs SET state='unknown',code='interrupted_result_unknown',updated_at=? WHERE state='running'", (now,))
            # Approval from a previous application run is not silently replayed.
            db.execute("UPDATE jobs SET state='draft',code='restart_confirmation_required',updated_at=? WHERE state='queued'", (now,))
            db.execute("UPDATE operations SET state='failed',code='operation_interrupted',updated_at=? WHERE state IN ('running','queued')", (now,))
            db.execute("UPDATE accounts SET auth_state='unchecked',code='operation_interrupted' WHERE auth_state='checking'")

    def _recover_scheduler_failure(self, exc: Exception) -> bool:
        with self._active_guard:
            self._scheduler_state = "recovering"
            self._scheduler_code = ("scheduler_database_unavailable"
                                    if isinstance(exc, sqlite3.Error) else "scheduler_failed")
        for delay in (0.0, 0.05, 0.15):
            if delay and self._shutdown.wait(delay):
                return False
            try:
                self._recover_interrupted_records(timeout=0.25)
            except Exception:
                continue
            with self._active_guard:
                self._scheduler_state = "running"
                self._scheduler_code = "scheduler_recovered"
            return True
        with self._active_guard:
            self._scheduler_state = "faulted"
        return False

    def _execute(self, kind: str, row: dict) -> None:
        monitor_done = threading.Event()
        monitor = threading.Thread(
            target=self._watch_cancellation, args=(kind, row["id"], monitor_done, self._operation_stop),
            name="open-flame-upload-cancel", daemon=True,
        )
        monitor.start()
        try:
            self._execute_operation(kind, row)
        finally:
            with self._active_guard:
                self._login_presentations.pop(row["id"], None)
            monitor_done.set()
            monitor.join(timeout=12)

    def _watch_cancellation(self, kind: str, operation_id: str, done: threading.Event, stop: threading.Event) -> None:
        table = "operations" if kind == "account" else "jobs"
        while not done.wait(0.2):
            if self._shutdown.is_set():
                stop.set()
                return
            try:
                with self._db() as db:
                    row = db.execute(f"SELECT code FROM {table} WHERE id=?", (operation_id,)).fetchone()
                if row and row["code"] == "cancellation_requested":
                    stop.set()
                    return
            except sqlite3.Error:
                # Unobservable cancellation must stop the owned operation too.
                stop.set()
                return

    def _execute_operation(self, kind: str, row: dict) -> None:
        invoked = False
        try:
            if kind == "account":
                interactive = getattr(self.backend, "login_interactive", None)
                stop = self._operation_stop
                if row["action"] == "login" and callable(interactive):
                    self._record_login(row, stop, "preparing")
                    result = interactive(row["platform"], row["account_id"], stop,
                        lambda phase, png=None, expires_at=None: self._record_login(row, stop, phase, png, expires_at))
                else:
                    result = getattr(self.backend, row["action"])(row["platform"], row["account_id"], stop)
            else:
                with self._db() as db:
                    account = db.execute("SELECT auth_state FROM accounts WHERE id=?", (row["account_id"],)).fetchone()
                if account is None or account["auth_state"] != "ready":
                    raise UploadError("account_not_ready")
                path = self._source_path(row["source_id"])
                if self._operation_stop.is_set():
                    result = BackendResult("canceled", "canceled_before_upload")
                else:
                    request = UploadRequest(job_id=row["id"], account_id=row["account_id"], platform=row["platform"], file_path=path,
                                            title=row["title"], description=row["description"], tags=tuple(row["tags"]),
                                            category_id=row["category_id"], mode=row["mode"], copyright=row["copyright"], source_credit=row["source_credit"])
                    invoked = True
                    result = self.backend.upload(request, self._operation_stop)
        except UploadError as exc:
            result = BackendResult("unknown" if invoked else "failed", exc.code)
        except Exception:
            result = BackendResult("unknown" if invoked else "failed", "backend_result_unknown" if invoked else "backend_failed")
        if (not isinstance(result, BackendResult) or not isinstance(result.code, str)
            or not isinstance(result.status, str) or not _SAFE_CODE.fullmatch(result.code)):
            result = BackendResult("unknown" if invoked else "failed", "backend_result_invalid")
        now = _now()
        with self._db() as db:
            if kind == "account":
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT * FROM operations WHERE id=?", (row["id"],)).fetchone()
                if current is None or current["state"] != "running" or current["account_id"] != row["account_id"]:
                    return
                if current["code"] == "cancellation_requested" or self._operation_stop.is_set():
                    result = BackendResult("canceled", "canceled")
                state = "ready" if result.status == "ready" else "canceled" if result.status in ("canceled", "cancelled") else "failed"
                auth_state = "ready" if state == "ready" else "unchecked" if state == "canceled" else "invalid"
                db.execute("UPDATE operations SET state=?,code=?,updated_at=? WHERE id=? AND state='running'", (state, result.code, now, row["id"]))
                db.execute("UPDATE accounts SET auth_state=?,code=? WHERE id=?", (auth_state, result.code, row["account_id"]))
            else:
                state = result.status
                if state not in ("submitted", "draft_saved", "failed", "unknown", "canceled"):
                    state = "unknown" if invoked else "failed"
                if invoked and state == "canceled":
                    state = "unknown"
                if state == "draft_saved" and row["mode"] != "draft":
                    state = "unknown"
                if state == "submitted" and row["mode"] != "publish":
                    state = "unknown"
                db.execute("UPDATE jobs SET state=?,code=?,updated_at=? WHERE id=? AND state='running'", (state, result.code, now, row["id"]))
