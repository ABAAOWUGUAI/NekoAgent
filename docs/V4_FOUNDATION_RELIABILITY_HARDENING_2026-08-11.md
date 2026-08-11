# V4 Foundation Reliability Hardening

Date: 2026-08-11

Status: PR #36 correction candidate with verification hardening; **not deployed, not default-on, and no legacy path retired**.

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
3. **Web dispatch receipt.** Every server-authenticated Admin Session or Admin
   Token dispatch requires the durable inbound receipt schema, regardless of a
   client payload `source`, even when QQ `task_message_reliability_v2` is off.
   Its receipt key is derived from the server-authenticated principal/session
   scope and client request ID; no raw token or session is stored. Legacy
   Workbench creates one request ID per deliberate submit and sends it in the
   existing `X-QQ-Message-ID` header.
4. **Failure semantics.** Same key plus another canonical payload is a 409
   conflict. A live receipt is a processing 409. An expired/failed Web receipt
   becomes terminal `web_dispatch_outcome_unknown`, never a new execution.
   Missing receipt schema returns a fail-closed 503 before the operation runs.
5. **UI truth.** The Chat UI offers same-ID result query only for an uncertain
   transport outcome. Processing has an explicit same-ID state confirmation;
   processing and unknown outcomes retain the unresolved request identity and
   disable ordinary submit. Starting a new request is a separately labelled,
   warned action. Conflict, deterministic validation and authentication each
   receive distinct recovery actions.
6. **Narrow error boundary.** Only explicit receipt/input validation errors
   become HTTP 400. Domain `ValueError` and other internal failures remain on
   the sanitized internal-error path and never echo their message to the client.

## Compatibility and rollback

- QQ paths continue to call `execute_once(..., require_receipt=False)`, retain
  their existing feature-flag semantics, and do not acquire the Web-only
  `BEGIN IMMEDIATE` lock.
- Existing admin session and admin-token authentication are reused. A client
  `X-QQ-Actor-ID` is ignored for Web receipt scoping.
- Admin Token represents the server-side control-plane principal. Its durable
  scope is intentionally stable across token rotation so a legitimate same-ID
  retry cannot become a second execution merely because the credential changed.
- The receipt schema is pre-existing assistant-core migration 12; this change
  adds no schema migration. A missing/drifted schema blocks Web dispatch safely.
- Rollback is source-level: revert the explicit shell wrapper, Web-only
  `require_receipt` invocation and UI classifier together. No stored data need
  be deleted or transformed.

## Required verification

- `tests/test_v4_foundation_hardening.py` starts a local Bridge HTTP server,
  invokes `/assistant/dispatch`, and proves Legacy Workbench compatibility,
  principal-based receipt enforcement, same ID/same payload one-task replay,
  differing-payload conflict, processing/expired no-reexecution, and sanitized
  internal errors. It separately protects optional QQ receipt semantics.
- `tools/run_v4_artifact_browser_rehearsal.cjs` mutates a real-ish legacy
  Artifact subtree after opening full management and proves Daily remains inert
  while focus moves from its hidden control to a visible Legacy heading.
- `tools/run_v4_ai_chat_browser_rehearsal.cjs` mutates hidden Overview while
  focus, Chinese/ASCII draft and caret are in Chat; it proves no remount,
  exercises lifecycle, error classifications and visible focus.
- `tools/run_v4_workbench_dispatch_browser_rehearsal.cjs` executes the actual
  Legacy Workbench source in a local browser fixture and proves one rapid
  double-submit sends one stable header ID, while a later deliberate submit
  receives a different ID.
- `tools/run_public_tests.py` remains dependency-free and runs the public-safe
  receipt scope regression.

## Verification isolation

`WebDispatchHttpIntegrationTests` imports the real `BridgeHandler` only after
replacing the process environment with an explicit test-local runtime. Its
temporary directory owns the assistant DB, task DB, workspace, dummy
admin/channel secret files, cache/history paths, session and an ephemeral
localhost port. It does not inherit `/opt/agent-stack`, production secrets,
runtime DB paths or environment configuration. The fixture stubs outbound
effects at the dispatch boundary, but continues to exercise actual HTTP
authentication, request parsing, principal selection and SQLite receipt code.

The full-source verification must additionally run after extraction into a
directory without Git metadata and without a production runtime path. Its
receipt tests are functional checks, not skips for missing host infrastructure.

## Package provenance

`tools/build_v4_1_reconstruction_patch.py` now names and packages the current
V4 Foundation files, including the Web receipt and Legacy Workbench correction.
Its manifest records Git provenance only when `git rev-parse --show-toplevel`
resolves to the package source root, and always records a deterministic content
manifest hash. A source ZIP without `.git`, including one placed below an
unrelated parent Git repository, remains reviewable and cannot inherit the
parent commit as its provenance. Its Git-only assertion is skipped rather than
misreported as a functional failure; an uncommitted package cannot impersonate
a commit. When the verified checkout is clean, each package entry is read with
`git show <HEAD>:<path>` rather than from platform-normalized working-tree
bytes. Therefore a Windows CRLF checkout and a `git archive` full-source ZIP
carry identical tracked bytes and cannot be misreported as different source.
The full-source command is `git -c core.autocrlf=false archive`; an unqualified
`git archive` may apply the caller's working-tree EOL conversion and is not a
canonical source-equality artifact.
Historical Artifact and AI Chat records retain their original conclusions and
now carry an explicit historical-status header.
