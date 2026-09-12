"""Reject additions or modifications of automated test artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import PurePosixPath
import subprocess
import sys
from typing import Sequence


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
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# One frozen maintenance patch, activated only after the user approves its
# reviewed 2026-09-13 proposal. Any other test diff remains blocked.
APPROVED_TEST_MAINTENANCE_PATCH_SHA256 = "5ddff9cedcdfe0f5dd4a67beb9cab8566e1a743cebd3a37695ce4caeb3297a14"


def _is_test_artifact(value: str) -> bool:
    path = PurePosixPath(value.replace("\\", "/"))
    parts = {part.casefold() for part in path.parts}
    name = path.name.casefold()
    return bool(parts & TEST_DIRECTORY_NAMES) or (
        name.startswith("test_") and name.endswith(".py")
    ) or name.endswith(TEST_FILE_SUFFIXES)


def _git(*args: str) -> bytes:
    result = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
    )
    return result.stdout


def _changed_paths(diff_args: Sequence[str]) -> list[str]:
    fields = [
        item
        for item in _git(
            "diff",
            "--name-status",
            "--find-renames",
            "--diff-filter=ACMRTUXB",
            "-z",
            *diff_args,
        ).split(b"\0")
        if item
    ]
    paths: list[str] = []
    index = 0
    while index < len(fields):
        status = fields[index].decode("ascii", errors="strict")
        index += 1
        path_count = 2 if status[:1] in {"C", "R"} else 1
        if index + path_count > len(fields):
            raise ValueError("git returned an incomplete name-status record")
        for item in fields[index : index + path_count]:
            paths.append(item.decode("utf-8", errors="surrogateescape"))
        index += path_count
    return paths


def _approved_test_maintenance(diff_args: Sequence[str], paths: Sequence[str]) -> bool:
    if not paths:
        return False
    patch = _git(
        "diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv",
        "--no-renames", "--no-color", "--src-prefix=a/", "--dst-prefix=b/",
        "--unified=3", "--inter-hunk-context=0", "--diff-algorithm=myers",
        "--no-indent-heuristic", *diff_args, "--", *paths,
    )
    return hashlib.sha256(patch).hexdigest() == APPROVED_TEST_MAINTENANCE_PATCH_SHA256


def _commit(value: str) -> str:
    if not value:
        raise ValueError("commit reference is empty")
    resolved = _git(
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{value}^{{commit}}",
    ).decode("ascii").strip()
    if len(resolved) != 40:
        raise ValueError("git did not resolve a full commit identity")
    return resolved


def _first_parent(value: str) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--end-of-options", f"{value}^"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    resolved = result.stdout.decode("ascii").strip()
    return resolved if len(resolved) == 40 else None


def _default_branch_commit(name: str) -> str:
    if not name:
        raise ValueError("GitHub event does not name a default branch")
    for candidate in (f"refs/remotes/origin/{name}", f"refs/heads/{name}"):
        try:
            return _commit(candidate)
        except subprocess.CalledProcessError:
            continue
    raise ValueError("the fetched checkout does not contain the default branch")


def _github_range() -> tuple[str, str, bool]:
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    if not event_path or not event_name:
        raise ValueError("GITHUB_EVENT_PATH and GITHUB_EVENT_NAME are required")
    with open(event_path, "r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise ValueError("GitHub event payload must be a JSON object")

    if event_name == "pull_request":
        pull_request = payload.get("pull_request")
        if not isinstance(pull_request, dict):
            raise ValueError("pull_request event payload is incomplete")
        base_entry = pull_request.get("base")
        head_entry = pull_request.get("head")
        base = base_entry.get("sha") if isinstance(base_entry, dict) else None
        head = head_entry.get("sha") if isinstance(head_entry, dict) else None
    elif event_name == "push":
        base = payload.get("before")
        head = payload.get("after")
        if isinstance(base, str) and base and set(base) == {"0"}:
            repository = payload.get("repository")
            default_branch = (
                repository.get("default_branch")
                if isinstance(repository, dict)
                else None
            )
            base = _default_branch_commit(default_branch)
    elif event_name == "workflow_dispatch":
        head = os.environ.get("GITHUB_SHA", "HEAD")
        base = _first_parent(head)
        if base is None:
            return EMPTY_TREE_SHA, _commit(head), True
    else:
        raise ValueError(f"unsupported GitHub event: {event_name}")

    if not isinstance(base, str) or not isinstance(head, str):
        raise ValueError(f"{event_name} event payload does not contain base/head commits")
    return _commit(base), _commit(head), False


def _range_test_changes(
    base: str,
    head: str,
    *,
    base_is_tree: bool = False,
) -> tuple[str, list[str]]:
    resolved_head = _commit(head)
    if base_is_tree:
        merge_base = base
    else:
        resolved_base = _commit(base)
        merge_base = (
            _git("merge-base", resolved_base, resolved_head).decode("ascii").strip()
        )
        if len(merge_base) != 40:
            raise ValueError("git did not find a unique merge base")
    paths = _changed_paths((merge_base, resolved_head))
    return merge_base, sorted({path for path in paths if _is_test_artifact(path)})


def staged_test_changes() -> list[str]:
    paths = _changed_paths(("--cached",))
    return sorted({path for path in paths if _is_test_artifact(path)})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--staged",
        action="store_true",
        help="inspect the staged Git diff",
    )
    mode.add_argument(
        "--range",
        nargs=2,
        metavar=("BASE", "HEAD"),
        help="inspect the net diff from the BASE/HEAD merge base to HEAD",
    )
    mode.add_argument(
        "--github-event",
        action="store_true",
        help="derive a push, pull-request, or dispatch range from GitHub Actions",
    )
    args = parser.parse_args()
    try:
        if args.staged:
            blocked = staged_test_changes()
            if _approved_test_maintenance(("--cached",), blocked):
                print("commit_scope_approved_test_maintenance")
                blocked = []
        else:
            if args.github_event:
                base, head, base_is_tree = _github_range()
            else:
                base, head = args.range
                base_is_tree = False
            merge_base, blocked = _range_test_changes(
                base,
                head,
                base_is_tree=base_is_tree,
            )
            print(f"commit_scope_range merge_base={merge_base} head={_commit(head)}")
            if _approved_test_maintenance((merge_base, _commit(head)), blocked):
                print("commit_scope_approved_test_maintenance")
                blocked = []
    except (OSError, UnicodeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"commit_scope_failed: {exc}", file=sys.stderr)
        return 2
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
