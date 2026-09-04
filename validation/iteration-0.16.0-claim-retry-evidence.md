# Iteration 0.16.0 claim fencing and explicit retry engineering evidence

Date: 2026-09-04

Product: `video-download-control` `0.16.0`

Database: Schema `11`

Status: final Windows engineering and project-only source-package record

## 1. Scope and evidence boundary

This iteration closes the Windows local-app stop/claim race with a
database-linearized, run-scoped Worker claim gate. It also adds an explicit
new-generation retry for terminal failed flat downloads while keeping platform
cooldown and manual circuit reset as separate controls.

The evidence recorded here is synthetic/offline engineering evidence, including
real spawned Windows process and browser checks. No real Cookie or platform URL
was used, and no new real-platform download was performed. It does not qualify
Stage 0 evidence, prove the target Linux/Docker candidate, validate a Windows
installer or frozen executable, or authorize redistribution of third-party
binaries, containers, dependency wheelhouses, or tool bundles. The
[Iteration 0.15.0 record](iteration-0.15.0-local-app-evidence.md) remains the
historical evidence for that version and must not be rewritten as 0.16.0 proof.

## 2. Schema 11 and the two-phase claim gate

Schema 11 adds one `worker_claim_gate` singleton. Its stored identity is the
supervisor `run_id` plus `worker_id`; runtime fields record whether claims are
accepted, when the gate was activated, when stop was requested, and when the row
was last updated. Migration creates the singleton closed with no run identity.
Readiness requires the normalized table DDL to match the canonical constrained
definition exactly, rejects every trigger attached to the gate, and accepts only
the exact `id=1` singleton in a valid pristine, prepared, active, or stopped state
with coherent non-empty timestamps. A constraintless look-alike table, malformed
state, extra row, or restored historical open value cannot authorize a new
supervisor run.

The local-app lifecycle is deliberately two phase:

1. After the control child is ready and its HTTP identity is verified, the
   supervisor prepares its new run identity with `accepting_claims=0`. This
   fences an older run before the Worker preflight command is sent.
2. The Worker validates its local configuration and reports preflight ready.
   The supervisor repeats the control identity check and any Cookie snapshot
   validation. Normal mode then activates that exact run before sending the
   claim command. Check mode sends the dedicated check-complete command and
   never activates the gate.

The supervised Worker passes its run ID into `claim_next()`. That method opens
`BEGIN IMMEDIATE` and checks the gate's run ID, Worker ID, and open state before
recovering expired leases or writing a new Job lease/Attempt. Claim and stop
therefore participate in SQLite's single writer order:

- if claim commits first, that one lease remains valid in-flight and stop
  prevents another claim from the same supervised run;
- if stop commits first, claim returns idle without changing the queued Job or
  inserting an Attempt;
- a stale run ID cannot claim after a newer run has prepared the singleton.

The command Pipe remains a wake/stop notification channel. An additional Pipe
poll is not used as the correctness boundary.

## 3. Shutdown fail-safe and lease recovery

Normal local-app cleanup commits `stop_claim_gate()` before closing the Worker
command Pipe, waits for the Worker, and only then closes the control command
Pipe. The stop operation re-reads the row within the same transaction and rejects
a write that was not durably reflected; readiness independently rejects all gate
triggers. If the gate stop cannot be established, cleanup marks the result forced
and terminates the Windows Job containing only the application's children before
touching the advisory Pipe. If the Job API fails, the fallback targets only those
still-live child processes. This path must not be reported as a graceful
database-linearized stop.

Gate stop does not revoke a lease that committed earlier. A cooperative Worker
may continue heartbeating and finish that Attempt during the bounded shutdown.
If it is forcibly terminated, the existing lease-expiry path later marks the
running Attempt `abandoned` with `worker_lost`, clears the stale lease, and
requeues the Job while the current generation remains below its four-attempt
limit; an exhausted generation becomes terminal failed. Pending asset commit
intents retain their existing recovery ordering and must not be deleted or
edited manually to accelerate recovery.

## 4. Explicit flat retry, cooldown, and manual reset

`POST /api/v1/jobs/{job_id}/retry` and the corresponding frontend action accept
only a terminal `failed` flat `download` Job with no graph parent, attachment
target, or active discovery. The repository uses `BEGIN IMMEDIATE` and a
generation compare-and-swap to:

- increment `run_generation`;
- return the Job and Input to `queued`;
- reset only the new generation's attempt counter and terminal fields; and
- preserve cumulative attempt count and all historical Attempt rows.

A missing Job returns 404. A non-terminal/graph Job, a lost concurrent retry,
or another queued, active, or ready Job for the same source returns 409. This is
an explicit operator action, not an automatic infinite retry path.

Retry does not edit `platform_circuits`. A rate-limited platform remains closed
to claims until its cooldown expires, at which point only one half-open probe
may be claimed. A circuit with `requires_manual_reset=true` remains blocked even
after a Job is moved to a new generation. Operators must diagnose the version,
credential, platform status, error code, and logs before calling
`POST /api/v1/platform-circuits/{platform}/reset`; reset is rejected when the
circuit does not actually require manual intervention.

## 5. Lifecycle logging contract

The allowlisted JSONL lifecycle events are
`local_app.claim_gate_prepared`, `local_app.claim_gate_activated`, and
`local_app.claim_gate_stopped`. They distinguish successful state transitions
from `prepare_failed`, `activate_failed`, `stop_failed`, and `fenced` outcomes
without recording paths, exception text, the input source, or a duplicate run
identifier. Forced shutdown pairings are constrained to
`gate_stop_failed + claim_gate_stop` and `child_timeout + child_shutdown`; the
reader rejects missing fields and invalid or tampered state/cause combinations.
Logging remains best-effort and is not the claim-fencing authority.

## 6. Confirmed final results

All rows below were run against the final documented 0.16.0 source tree unless a
row explicitly describes an earlier slice:

| Validation slice | Confirmed result |
|---|---|
| Claim/migration/backup focused slice | `101 passed in 27.92s` |
| Local-app/log/API focused slice | `135 passed in 9.01s` |
| Final full repository regression | `1238 passed, 8 skipped in 104.14s` |
| Static release checks | `compileall -q src tests`, `uv lock --check --offline`, and `git diff --check` passed |
| Real Windows check/full lifecycle | Isolated `--check` prepared then stopped without activation; a full TTY run prepared, activated, and stopped normally; both released their ports and left the pre-existing port 8000 process untouched |
| Browser race QA | Delayed stale submit, queue-status, and circuit-status responses could not overwrite a newer selected batch, queue resume, or manual reset; console contained no warning/error |
| Active-lease stop QA | With the queue initially paused, an external claim committed Attempt 1 under the exact active run, then supervisor stop closed the gate while the Job/Attempt remained `probing`/`running`; the Worker had not started media work and no platform request occurred. Deterministic tests separately prove expiry to `abandoned/worker_lost` and creation of a new Attempt |
| Structured logs | Lifecycle pairing validated with zero write failures, rejected events, or configured leak-marker hits in the isolated runs |
| Project source package | 212 checkout publishable files plus generated `PKG-INFO` produced 213 regular sdist files with no links; the wheel has 96 entries and 96 RECORD rows, with all 60 package files byte-identical to both checkout and extracted sdist |
| Package metadata and runtime | Metadata/import report `0.16.0` and `Apache-2.0`; 13 console scripts, 32 legal files, and the exact `Copyright 2026 HedgehogsGX & Cyaegha_Xu` NOTICE are present; the 13 locked runtime dependencies plus project form exactly 14 installed distributions and pass the dependency check |
| Identity and privacy | Checkout and installed-wheel build identities match; known real user/path/URL identifiers and high-confidence secret patterns have zero hits. Synthetic `C:\\Users\\PRIVATE-*-CANARY` test strings remain intentionally labeled test fixtures |

The eight skipped cases are not counted as passes: one POSIX Cookie
directory-FD case, four root POSIX/getfacl metadata cases, one AF_UNIX case, one
POSIX open-file replacement case, and one POSIX permission case. They require
the authorized target Linux environment.

## 7. Remaining acceptance work

- Execute the explicit Linux/Docker acceptance contract on an authorized target
  Linux host; Windows unit/integration results do not satisfy it.
- Validate real Cookie ownership and private Windows DACL behavior before using
  real credentials.
- Run separately authorized Stage 0 evidence for each platform/route identity;
  claim fencing and retry tests do not change any platform from `candidate`.
- Keep frozen executable/installer and third-party binary, container,
  dependency-wheelhouse, or tool-bundle redistribution blocked until the
  target-specific source, SBOM, notices, obligations, provenance, signatures,
  and human license review are complete.
