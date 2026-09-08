# Iteration 0.28.0 workflow preset evidence

Date: 2026-09-09

This source milestone adds a local `WorkflowPresetStore` and API routes for reusable workflow parameters. Presets persist only sanitized edit, AI authorization digest, upload, and confirmation intent fields. They do not persist the source URL, API keys, Cookies, QR state, account session revisions, or full AI authorization objects.

At use time the current profile is required. The saved AI provider/model and authorization digests must match the current runtime; the current full dubbing authorization is then rebound into the materialized profile. A changed authorization fails closed with `workflow_preset_authorization_changed`.

Validated locally with the ignored `validation/local/validate_workflow_presets.py` validator, Python compileall, and diff checks. The API exposes `GET/POST /api/v1/workflows/presets`, `GET /api/v1/workflows/presets/{id}`, and `POST /api/v1/workflows/presets/{id}/workflows`. Real AI providers, downloads, platform accounts, and publication were not exercised.
