# Iteration 0.28.0 full workflow chain smoke

Date: 2026-09-09

The ignored validator `validation/local/validate_workflow_full_chain.py` drives a synthetic URL through the durable workflow state machine with an offline adapter: download completion, exact AI authorization, translation and dubbing step, edit confirmation/render completion, upload preparation, and final upload acknowledgement. It reaches `completed` with `submission_acknowledged` without network access.

This proves orchestration and restart-safe state transitions only. It does not prove OpenAI quality, real download extraction, account login, platform acceptance, public visibility, scheduled publication, or pricing. Those require separate user-authorized live checks.
