"""Stdlib-first source bootstrap, safe to import again by Windows spawn."""

from __future__ import annotations

import json
from pathlib import Path
import sys


def main() -> int:
    repository = Path(__file__).resolve().parent
    sys.path.insert(0, str(repository / "src"))
    try:
        from video_download_control.desktop_launcher import main as launch
        from video_download_control.source_environment_lock import (
            SourceEnvironmentBusy, source_environment_lock,
        )
        from video_download_control.startup_diagnostics import DiagnosticCode, emit_failure
    except KeyboardInterrupt:
        return 130
    except Exception:
        # If the bootstrap's own modules are missing, their logging code cannot
        # run either. Be explicit instead of claiming that a file was saved.
        print(json.dumps({
            "status": "error", "error_code": "local_launcher_unavailable",
            "failure_site": "launcher_import", "log_status": "not_started",
            "diagnostic_status": "unavailable",
            "next_step": "请检查源码是否完整，并按 README 修复本地 Python 环境；本次诊断未保存。",
        }, ensure_ascii=True, separators=(",", ":")), file=sys.stderr)
        return 2
    try:
        with source_environment_lock(repository, exclusive=False):
            return launch(repository_root=repository)
    except SourceEnvironmentBusy:
        emit_failure(DiagnosticCode.SETUP_BUSY)
        return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
