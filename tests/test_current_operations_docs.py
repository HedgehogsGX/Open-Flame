"""Guard current operational inputs while leaving historical evidence untouched."""
import json
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_current_runbook_inputs_match_package_and_tool_lock():
    version = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    runbook = (ROOT / 'docs/RUNBOOK.md').read_text(encoding='utf-8')
    lock = json.loads((ROOT / 'src/video_download_control/toolchains/windows-x64.json').read_text(encoding='utf-8'))
    assert runbook.startswith(f'# Open-Flame / Video Download Control v{version} Runbook')
    assert f'video_download_control-{version}-py3-none-any.whl' in runbook
    assert f'video-download-control=={version}' in runbook
    current_tools = next(line for line in runbook.splitlines() if line.startswith('- 当前固定工具为'))
    assert f'`{lock["yt_dlp"]["version"]}`' in current_tools
    assert f'`{lock["ffmpeg"]["version"]}`' in current_tools
    assert '下载数据库 Schema 11、独立上传数据库 Schema 1' in runbook
    assert '本节命令不包含这些上传数据' in runbook


def test_external_test_report_identifies_exact_source_and_running_payload():
    plan = (ROOT / 'docs/UPLOADER_TEST_PLAN.md').read_text(encoding='utf-8')
    report = plan.split('## 7. 回报模板与交付边界', 1)[1]
    for field in ('source_commit', 'working_tree_dirty', 'source_archive_sha256', 'product_identity'):
        assert field in report
    assert 'git checkout --detach $uploadTestCommit' in plan
    assert 'Get-FileHash -Algorithm SHA256' in plan
    assert 'SHA256SUMS`' in plan
    assert '/archive/refs/heads/' not in plan
