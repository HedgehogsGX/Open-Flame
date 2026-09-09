"""Fail closed when the no-credential CI workflow loses its review boundary."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
WORKFLOW_SHA256 = "0b6338b43e2f3f707252fe2f3467a9c4d95003f11e6da25894d40dfe2e327f77"
MATRIX_ENVIRONMENT_CHECK = (
    "uv run python -c \"import os, pytest, sys; "
    "actual='.'.join(map(str, sys.version_info[:3])); "
    "print(f'python={actual} pytest={pytest.__version__}'); "
    "assert actual == os.environ['EXPECTED_PYTHON']\""
)

PINNED_ACTIONS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",
    "astral-sh/setup-uv": "c771a70e6277c0a99b617c7a806ffedaca235ff9",
}
REQUIRED_SNIPPETS = (
    "push:",
    "pull_request:",
    "workflow_dispatch:",
    "permissions:\n  contents: read",
    "persist-credentials: false",
    "fetch-depth: 0",
    "timeout-minutes: 30",
    "fail-fast: false",
    "- windows-latest",
    "- ubuntu-latest",
    '- "3.12.10"',
    '- "3.13.14"',
    'UV_NO_SYNC: "1"',
    "UV_PYTHON: ${{ matrix.python-version }}",
    'version: "0.11.25"',
    "enable-cache: false",
    "python scripts/verify_commit_scope.py --github-event",
    "python scripts/verify_ci_contract.py",
    "node --version",
    "uv sync --extra dev --locked",
    "uv pip check",
    "uv run python -m pytest -q",
    "git diff --check",
    "git diff-tree --check --root -r -m --no-commit-id HEAD",
)
REQUIRED_EXACT_RUNS = (
    "python scripts/verify_ci_contract.py",
    "python scripts/verify_commit_scope.py --github-event",
    'uv sync --extra dev --locked --python "${{ matrix.python-version }}"',
    "uv pip check",
    MATRIX_ENVIRONMENT_CHECK,
    "uv run python -m pytest -q",
    "git diff --check",
    "git diff-tree --check --root -r -m --no-commit-id HEAD",
)
FORBIDDEN_SNIPPETS = (
    "pull_request_target:",
    "workflow_run:",
    "${{ secrets.",
    "continue-on-error: true",
    "VDC_COOKIE_",
    "VDC_ENABLE_CANDIDATE_REAL_WORKER",
    "VDC_ENABLE_LOCAL_REAL_WORKER",
)
ACTION_RE = re.compile(r"(?m)^\s*uses:\s*([^@\s]+)@([^\s#]+)")
SECRET_EXPRESSION_RE = re.compile(r"\$\{\{[^}]*\bsecrets\b", re.IGNORECASE)
YAML_HEX_ESCAPE_RE = re.compile(r"\\(?:x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8})")
PERMISSIONS_RE = re.compile(r"^(\s*)(?:permissions|'permissions'|\"permissions\")\s*:(.*)$")


class CiContractError(ValueError):
    """The checked workflow no longer matches the reviewed CI contract."""


def _validate_permissions(lines: list[str]) -> None:
    declarations = []
    for index, line in enumerate(lines):
        match = PERMISSIONS_RE.match(line)
        if match:
            declarations.append((index, len(match.group(1)), match.group(2).strip()))
    if len(declarations) != 1 or declarations[0][1:] != (0, ""):
        raise CiContractError("CI permissions must have one exact top-level block")

    index = declarations[0][0]
    entries: list[tuple[int, str]] = []
    for line in lines[index + 1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            break
        entries.append((indent, stripped))
    if entries != [(2, "contents: read")]:
        raise CiContractError("CI permissions must be exactly contents: read")


def validate_workflow(text: str) -> None:
    lines = text.splitlines()
    _validate_permissions(lines)
    for snippet in REQUIRED_SNIPPETS:
        if snippet not in text:
            raise CiContractError(f"missing required CI contract marker: {snippet}")
    for snippet in FORBIDDEN_SNIPPETS:
        if snippet in text:
            raise CiContractError(f"forbidden CI contract marker: {snippet}")
    if SECRET_EXPRESSION_RE.search(text):
        raise CiContractError("CI must not reference repository or environment secrets")
    if YAML_HEX_ESCAPE_RE.search(text):
        raise CiContractError("CI must not use YAML hex or Unicode escapes")
    for command in REQUIRED_EXACT_RUNS:
        pattern = re.compile(rf"(?m)^\s*run:\s*{re.escape(command)}\s*$")
        if not pattern.search(text):
            raise CiContractError(f"CI command must match the reviewed form: {command}")

    actions = ACTION_RE.findall(text)
    if len(actions) != len(PINNED_ACTIONS):
        raise CiContractError("CI actions must match the exact reviewed set")
    seen: set[str] = set()
    for name, revision in actions:
        expected = PINNED_ACTIONS.get(name)
        if expected is None:
            raise CiContractError(f"unreviewed CI action: {name}")
        if revision != expected or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise CiContractError(f"CI action is not pinned to its reviewed commit: {name}")
        if name in seen:
            raise CiContractError(f"duplicate CI action: {name}")
        seen.add(name)
    if seen != set(PINNED_ACTIONS):
        raise CiContractError("CI action set is incomplete")
    canonical = text.replace("\r\n", "\n").replace("\r", "\n")
    if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != WORKFLOW_SHA256:
        raise CiContractError("CI workflow does not match the reviewed bytes")


def main() -> int:
    try:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        validate_workflow(workflow)
    except (OSError, UnicodeError, CiContractError) as exc:
        print(f"ci_contract_failed: {exc}")
        return 2
    print("ci_contract_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
