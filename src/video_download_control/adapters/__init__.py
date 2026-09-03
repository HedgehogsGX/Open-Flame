"""Download-adapter contracts.

Concrete adapters live behind the types exported here.  Importing this package
does not perform network, process, or filesystem I/O.
"""

from .base import (
    AdapterContext,
    AdapterFailure,
    AdapterNetworkMode,
    CancellationToken,
    DownloadAdapter,
    DownloadRequest,
    DownloadResult,
    NetworkExecutionGuard,
    ProbeItem,
    ProbeRequest,
    ProbeResult,
    ProducedFile,
    ProgressReporter,
    ProgressUpdate,
)
from .fake import ScriptedFakeAdapter, ScriptedGraphFakeAdapter
from .yt_dlp import CookieResolver, YtDlpAdapter
from .yt_dlp_contract import (
    ControlledEgressEndpoint,
    CookieMount,
    DirectEgress,
    YtDlpCommand,
    YtDlpCommandFactory,
    YtDlpContractError,
    YtDlpJsRuntime,
    YtDlpJsRuntimeName,
)

__all__ = [
    "AdapterContext",
    "AdapterFailure",
    "AdapterNetworkMode",
    "CancellationToken",
    "ControlledEgressEndpoint",
    "CookieMount",
    "DirectEgress",
    "CookieResolver",
    "DownloadAdapter",
    "DownloadRequest",
    "DownloadResult",
    "NetworkExecutionGuard",
    "ProbeItem",
    "ProbeRequest",
    "ProbeResult",
    "ProducedFile",
    "ProgressReporter",
    "ProgressUpdate",
    "ScriptedFakeAdapter",
    "ScriptedGraphFakeAdapter",
    "YtDlpAdapter",
    "YtDlpCommand",
    "YtDlpCommandFactory",
    "YtDlpContractError",
    "YtDlpJsRuntime",
    "YtDlpJsRuntimeName",
]
