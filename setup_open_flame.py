"""Stdlib-first source setup bootstrap, also safe for Windows spawn imports."""

from __future__ import annotations

import json
from pathlib import Path
import sys


def main() -> int:
    repository = Path(__file__).resolve().parent
    sys.path.insert(0, str(repository / "src"))
    try:
        from video_download_control.source_setup import main as setup
    except KeyboardInterrupt:
        return 130
    except Exception:
        # Without the setup module its diagnostic writer cannot be assumed to
        # exist. This fallback never reflects import errors or private paths.
        print(json.dumps({
            "status": "error", "error_code": "setup_launcher_unavailable",
            "failure_site": "setup_launcher_import", "log_status": "not_started",
            "diagnostic_status": "unavailable",
            "next_step": "Check the source files and follow README.md. No diagnostic was saved.",
        }, ensure_ascii=True, separators=(",", ":")), file=sys.stderr)
        return 2
    try:
        return setup(repository_root=repository)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
