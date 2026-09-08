"""Reject staged additions or modifications of automated test artifacts."""

from __future__ import annotations

import argparse
from pathlib import PurePosixPath
import subprocess
import sys


TEST_DIRECTORY_NAMES = {"tests", "test", "__tests__"}
TEST_FILE_SUFFIXES = (
    ".test.js",
    ".test.jsx",
    ".test.ts",
    ".test.tsx",
    ".spec.js",
    ".spec.jsx",
    ".spec.ts",
    ".spec.tsx",
    "_test.py",
)


def _is_test_artifact(value: str) -> bool:
    path = PurePosixPath(value.replace("\\", "/"))
    parts = {part.casefold() for part in path.parts}
    name = path.name.casefold()
    return bool(parts & TEST_DIRECTORY_NAMES) or (
        name.startswith("test_") and name.endswith(".py")
    ) or name.endswith(TEST_FILE_SUFFIXES)


def staged_test_changes() -> list[str]:
    result = subprocess.run(
        [
            "git",
            "diff",
            "--cached",
            "--name-only",
            "--diff-filter=ACMRTUXB",
            "-z",
        ],
        check=True,
        capture_output=True,
    )
    paths = [
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    ]
    return sorted(path for path in paths if _is_test_artifact(path))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reject staged additions or modifications of test files."
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="inspect the staged Git diff",
    )
    args = parser.parse_args()
    if not args.staged:
        parser.error("--staged is required")

    blocked = staged_test_changes()
    if not blocked:
        return 0

    print("Commit blocked: test files must remain local and uncommitted.", file=sys.stderr)
    for path in blocked:
        print(f"  {path}", file=sys.stderr)
    print(
        "Move temporary verification into ignored validation/local/. "
        "Deletion-only cleanup remains allowed.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
