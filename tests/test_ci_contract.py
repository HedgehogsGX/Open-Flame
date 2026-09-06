from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_ci_contract.py"
SPEC = importlib.util.spec_from_file_location("verify_ci_contract", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)


def workflow_text() -> str:
    return (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def test_ci_workflow_matches_the_reviewed_offline_matrix() -> None:
    ci.validate_workflow(workflow_text())


@pytest.mark.parametrize(
    ("old", "new", "error"),
    [
        (
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            "actions/checkout@v7",
            "not pinned",
        ),
        (
            "permissions:\n  contents: read",
            "permissions:\n  contents: write",
            "permissions",
        ),
        (
            "workflow_dispatch:",
            "pull_request_target:",
            "missing required",
        ),
        (
            "uv run python -m pytest -q",
            "uv run python -m pytest -q || true",
            "forbidden",
        ),
        (
            "git diff-tree --check --root -r -m --no-commit-id HEAD",
            "git diff --check HEAD",
            "missing required",
        ),
    ],
)
def test_ci_contract_rejects_gate_weakening(old: str, new: str, error: str) -> None:
    text = workflow_text().replace(old, new, 1)
    if "|| true" in new:
        text += "\ncontinue-on-error: true\n"
    with pytest.raises(ci.CiContractError, match=error):
        ci.validate_workflow(text)


@pytest.mark.parametrize(
    ("injection", "error"),
    [
        ("    permissions: write-all\n", "permissions"),
        ("    'permissions': write-all\n", "permissions"),
        ('    "permissions": write-all\n', "permissions"),
        ("    permissions:\n      issues: write\n", "permissions"),
        ("    env:\n      TOKEN: ${{secrets.SOME_TOKEN}}\n", "must not reference"),
        ("    env:\n      TOKEN: ${{  SeCrEtS.SOME_TOKEN }}\n", "must not reference"),
        ('    env:\n      TOKEN: "${{ \\u0073ecrets.SOME_TOKEN }}"\n', "Unicode escapes"),
    ],
)
def test_ci_contract_rejects_added_permissions_and_secret_expressions(
    injection: str, error: str
) -> None:
    text = workflow_text().replace("jobs:\n", f"jobs:\n{injection}", 1)
    with pytest.raises(ci.CiContractError, match=error):
        ci.validate_workflow(text)


def test_ci_contract_rejects_an_extra_top_level_permission() -> None:
    text = workflow_text().replace(
        "  contents: read\n",
        "  contents: read\n  actions: write\n",
        1,
    )
    with pytest.raises(ci.CiContractError, match="permissions"):
        ci.validate_workflow(text)


def test_ci_contract_rejects_shell_success_bypass_without_continue_on_error() -> None:
    text = workflow_text().replace(
        "run: uv run python -m pytest -q",
        "run: uv run python -m pytest -q || true",
        1,
    )
    with pytest.raises(ci.CiContractError, match="reviewed form"):
        ci.validate_workflow(text)


def test_ci_contract_rejects_inline_flow_permissions() -> None:
    injection = (
        "  malicious: {permissions: write-all, runs-on: ubuntu-latest, "
        "steps: [{run: echo hi}]}\n"
    )
    text = workflow_text().replace("jobs:\n", "jobs:\n" + injection, 1)
    with pytest.raises(ci.CiContractError, match="reviewed bytes"):
        ci.validate_workflow(text)


def test_ci_contract_rejects_yaml_line_continuation_obfuscation() -> None:
    continued_key = '    "permis' + "\\" + '\n      sions": write-all\n'
    key_text = workflow_text().replace("    runs-on:", continued_key + "    runs-on:", 1)
    with pytest.raises(ci.CiContractError, match="reviewed bytes"):
        ci.validate_workflow(key_text)

    continued_secret = (
        '      LEAK: "${{ se' + "\\" + '\n        crets.TEST_TOKEN }}"\n'
    )
    secret_text = workflow_text().replace(
        '      UV_NO_PROGRESS: "1"\n',
        '      UV_NO_PROGRESS: "1"\n' + continued_secret,
        1,
    )
    with pytest.raises(ci.CiContractError, match="reviewed bytes"):
        ci.validate_workflow(secret_text)
