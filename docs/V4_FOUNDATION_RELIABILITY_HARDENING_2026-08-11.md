# V4 Foundation Reliability Hardening

Date: 2026-08-11

Status: local hardening candidate; **not deployed, not default-on, and no legacy path retired**.

## Scope and baseline

The candidate starts from public `main` commit
`ca12f1be5e1f2a816a0e35218037cfbd79d06ce1` after the merged Artifact and AI
Chat first slices. It hardens only their shared navigation, lifecycle, retry
truth and current-package provenance. It does not add a V4 surface, alter
information architecture, change QQ product behavior, or execute a production
migration.

## Contracts closed

1. **Explicit navigation truth.** The V4 shell observes the existing
   `window.switchView()` transition and publishes one owner event after that
   transition. Arbitrary DOM class changes are no longer navigation input.
2. **Idempotent surface lifecycle.** Artifact and AI Chat treat an active,
   visible, same-mode activation as a no-op. Owner change, V4 disable and
   explicit Legacy management remain explicit state transitions.
3. **Web dispatch receipt.** `source=web-console` requires the existing durable
   inbound receipt schema even when QQ `task_message_reliability_v2` is off.
   Its receipt key is derived from the server-authenticated principal/session
   scope and client request ID; no raw token or session is stored.
4. **Failure semantics.** Same key plus another canonical payload is a 409
   conflict. A live receipt is a processing 409. An expired/failed Web receipt
   becomes terminal `web_dispatch_outcome_unknown`, never a new execution.
   Missing receipt schema returns a fail-closed 503 before the operation runs.
5. **UI truth.** The Chat UI offers same-ID result query only for an uncertain
   transport outcome. Conflict, processing, deterministic validation, unknown
   server outcome and authentication each receive a distinct recovery action.

## Compatibility and rollback

- QQ paths continue to call `execute_once(..., require_receipt=False)` and
  retain their existing feature-flag semantics.
- Existing admin session and admin-token authentication are reused. A client
  `X-QQ-Actor-ID` is ignored for Web receipt scoping.
- The receipt schema is pre-existing assistant-core migration 12; this change
  adds no schema migration. A missing/drifted schema blocks Web dispatch safely.
- Rollback is source-level: revert the explicit shell wrapper, Web-only
  `require_receipt` invocation and UI classifier together. No stored data need
  be deleted or transformed.

## Required verification

- `tests/test_v4_foundation_hardening.py` starts a local Bridge HTTP server,
  invokes `/assistant/dispatch`, and proves same ID/same payload writes one
  SQLite task, differing payload receives conflict, and processing/expired
  receipts do not execute again.
- `tools/run_v4_artifact_browser_rehearsal.cjs` mutates a real-ish legacy
  Artifact subtree after opening full management and proves Daily remains inert.
- `tools/run_v4_ai_chat_browser_rehearsal.cjs` mutates hidden Overview while
  focus, Chinese/ASCII draft and caret are in Chat; it proves no remount,
  exercises lifecycle, error classifications and visible focus.
- `tools/run_public_tests.py` remains dependency-free and runs the public-safe
  receipt scope regression.

## Package provenance

`tools/build_v4_1_reconstruction_patch.py` now names and packages the current
V4 Foundation shell, Artifact and AI Chat slices. Its manifest records the Git
base revision, whether the working tree is clean, and a deterministic content
manifest hash; an uncommitted package cannot impersonate a commit.
Historical Artifact and AI Chat records retain their original conclusions and
now carry an explicit historical-status header.
