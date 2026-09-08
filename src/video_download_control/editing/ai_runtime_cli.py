"""Read-only check and real provider health command for the AI runtime."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from .ai_bridge import AiBridgeError, AiRuntimeBridge
from .ai_runtime import AiRuntimeError, inspect_ai_runtime, load_ai_runtime


def _emit(value: dict[str, object]) -> None:
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        flush=True,
    )


def _absolute_root(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("runtime root must be absolute")
    return path


def _secret_resolver(runtime):
    environment_by_provider = {
        provider.id: provider.auth_env
        for provider in runtime.providers
        if provider.auth_env is not None
    }

    def resolve(provider_id: str) -> str | None:
        name = environment_by_provider.get(provider_id)
        return None if name is None else os.environ.get(name)

    return resolve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="open-flame-ai-runtime",
        description="Verify an already installed optional Open-Flame AI runtime.",
    )
    parser.add_argument("--root", required=True, type=_absolute_root)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--health", action="store_true")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    args = parser.parse_args(argv)

    if args.check:
        if args.provider is not None or args.model is not None:
            parser.error("--provider/--model are only valid with --health")
        status = inspect_ai_runtime(args.root)
        _emit(status)
        return 0 if status["ready"] is True else 2

    if args.provider is None:
        parser.error("--provider is required with --health")
    try:
        runtime = load_ai_runtime(args.root)
        progress: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory(prefix="open-flame-ai-health-") as scratch:
            bridge = AiRuntimeBridge(
                args.root,
                Path(scratch),
                secret_resolver=_secret_resolver(runtime),
            )
            result = bridge.health(
                args.provider,
                args.model,
                progress=lambda fraction, code: progress.append(
                    {"fraction": fraction, "code": code}
                ),
            )
        identity = result["runtime"]
        _emit(
            {
                "ready": True,
                "code": "ready",
                "provider_id": args.provider,
                "model_id": args.model,
                "operations": result["operations"],
                "voice_count": len(result["voices"]),
                "runtime": identity,
                "progress": progress,
            }
        )
        return 0
    except (AiRuntimeError, AiBridgeError) as exc:
        _emit(
            {
                "ready": False,
                "code": exc.code,
                "provider_id": args.provider,
                "model_id": args.model,
            }
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
