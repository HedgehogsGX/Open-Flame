"""Source-checkout launcher; keep the existing supervisor in the main process.

The dependency-heavy application is imported only after the independent
diagnostic boundary is available. No installation, network request or service
process is created by this module itself.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from importlib import import_module
from pathlib import Path

from .startup_diagnostics import DiagnosticCode, emit_failure


def main(argv: Sequence[str] | None = None, *, repository_root: Path) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        try:
            cli = import_module(".local_app_cli", package=__package__)
        except ImportError:
            emit_failure(DiagnosticCode.LOCAL_DEPENDENCIES_UNAVAILABLE)
            return 2
        defaults = ["--allow-direct-network"]
        checkout_tools = repository_root / "runtime-tools" / "windows-x64"
        # Keep a broken existing bundle visible to the real toolchain validator.
        # Absence alone permits the already-supported LOCALAPPDATA default.
        if os.path.lexists(checkout_tools):
            defaults.extend(("--tool-root", str(checkout_tools)))
        # Explicit operator options appear last and override launcher defaults.
        # Business storage remains under the supervisor's existing app root.
        # ASCII also works in an English Windows redirected pipe. A friendly
        # banner must not prevent startup because of the caller's encoding.
        print("Open-Flame: local direct-network mode. Press Ctrl+C to stop.", flush=True)
        return cli.main([*defaults, *arguments])
    except KeyboardInterrupt:
        return 130
    except Exception:
        emit_failure(DiagnosticCode.INTERNAL_ERROR)
        return 70


__all__ = ["main"]
