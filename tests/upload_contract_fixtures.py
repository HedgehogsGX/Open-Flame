"""Synthetic Upload contract fixtures; no real runtime or platform interaction.

Evidence below models a controlled backend response, not remote acceptance.
Malformed-result tests must return their deliberately invalid object directly.
"""
import time
from unittest.mock import patch
from video_download_control.uploads.contracts import BackendResult, CURRENT_UPLOAD_ADAPTER_IDENTITIES


def synthetic_receipt_identity(platform):
    name, revision = CURRENT_UPLOAD_ADAPTER_IDENTITIES[platform]
    return {"adapter_name": name, "adapter_revision": revision}


def synthetic_upload_result(request, status=None, code="synthetic_result"):
    status = status or ("draft_saved" if request.mode == "draft" else "submitted")
    evidence = {
        ("bilibili", "publish", "submitted"): "process_exit_zero",
        ("douyin", "publish", "submitted"): "uploader_returned_after_final_action",
        ("tencent", "publish", "submitted"): "https_errcode_zero",
        ("tencent", "draft", "draft_saved"): "post_list_navigation",
    }.get((request.platform, request.mode, status))
    return BackendResult(status, code, evidence)


def fail_confirmed_upload(service, job_id):
    """Create a real failed receipt via explicit confirm and a controlled response."""
    already_running = service.status()["worker_running"]
    service.start()
    try:
        with patch.object(service.backend, "upload", return_value=BackendResult("failed", "synthetic_fixture_failure")):
            service.confirm(job_id)
            deadline = time.monotonic() + 5
            while service.job(job_id)["state"] != "failed" and time.monotonic() < deadline:
                time.sleep(.01)
            assert service.job(job_id)["state"] == "failed"
            attempt = service.attempt(job_id)
            assert (attempt["state"], attempt["result_status"]) == ("responded", "failed")
    finally:
        if not already_running:
            service.stop()


def reconcile_synthetic_not_accepted(service, job_id):
    """Simulate an operator's explicit check of this exact synthetic attempt."""
    attempt = service.attempt(job_id)
    result = service.reconcile_unknown(job_id, attempt_id=attempt["id"], expected_revision=attempt["revision"], conclusion="not_accepted", acknowledge_platform_check=True)
    assert result["attempt"]["state"] == "reconciled"
    assert result["attempt"]["reconciliation"] == "not_accepted"
    assert result["job"]["state"] == "failed"
    return result
