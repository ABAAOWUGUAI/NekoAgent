# NekoAgent V4.1 AI Chat First Slice — Review Record

Date: 2026-08-11
Baseline: Artifact Draft branch commit `12d113004e52d59bfd92b6dc1099ff46cc2b0ef0`
Status: local opt-in implementation; **not deployed and not default-on**

## Review Pass 1 — product boundary and implementation path

AI Chat is a Deep Work Frontstage, not a replacement for the legacy Overview,
QQ, Work or Console. The first slice may show only one current-page exchange
stream and the outcome of the existing dispatch decision.

Confirmed implementation evidence:

- `POST /assistant/dispatch` accepts `user_id`, `source`, `message`, `force`,
  `trace_id` and `timeout`; `source=web-console` maps to the existing admin
  dispatch path.
- The unchanged dispatcher returns `chat`, `light`, `task`, `task_append` or
  `approval_required`; task results include the existing legacy task identity.
- Existing `window.loadTask(id)` fetches that task and switches the old console
  to the Work view. It is reused rather than recreated.
- Existing manual model routing and approval evaluation happen behind the
  dispatch boundary. The new UI neither reads nor writes either setting.
- Existing Home data is metadata-only for recent conversations. It cannot
  safely reconstruct message bodies, so a historical transcript is excluded.

## Review Pass 2 — adversarial contract review

The review tested the proposed contract against failure and transition paths
before accepting implementation:

- The HTTP helper supports caller-supplied headers. Dispatch already passes
  `X-QQ-Message-ID` into the established receipt path, so a manual retry can
  reuse one generated ID. The UI must never auto-retry or generate a second ID
  for the same uncertain request.
- A task response is HTTP 202 rather than a failure. The UI must treat any
  successful JSON result as a dispatch result, then expose a truthful existing
  Work handoff.
- The receipt service is runtime-configured; local code proves the adapter but
  does not prove a live configuration. This is a rollout control, not a reason
  to invent a second dispatch endpoint.
- No direct API was found for attachments, project selection, readable history
  replay, model selection, automated routing changes or approval completion.
  Showing any of those controls would be fictitious.
- The V4 Artifact lifecycle establishes the required safety pattern: route
  entry activates one root; owner change / V4 exit makes it hidden and inert;
  a version counter blocks stale async rendering; legacy remains reachable.

### Initial feasibility: 96/100 — GO_WITH_CONTROLS; hard blockers: 0

| Dimension | Score | Evidence / control |
| --- | ---: | --- |
| Product ownership | 15/15 | Conversation frontstage with Work/approval kept at their true owners. |
| API reality | 18/20 | Existing dispatch, task and receipt paths; no invented API. |
| Safety and routing | 15/15 | `force=auto`, existing approval/manual routing remain authoritative. |
| Lifecycle / rollback | 15/15 | Reuses proven opt-in V4 lifecycle and visible legacy fallback. |
| Truthfulness | 10/10 | No attachment/history/model/approval simulation. |
| Testability | 13/15 | Unit/public checks and local-browser rehearsal planned; live role state remains later. |
| Scope discipline | 10/10 | Frontend-only, no server/schema/default change. |

The four-point deduction is solely for live receipt-service configuration and
production role/error parity, neither of which was touched in this local slice.

## Implementation result

- Added an opt-in `#v4AiChatSurface` that appears only for V4 route `chat` and
  hides the compatibility Overview while active.
- Uses the existing `/assistant/dispatch` contract with `force=auto`,
  `source=web-console`, `user_id=web-console`, a per-request trace and a
  stable request receipt header.
- Disables normal submission while processing. On an uncertain result it only
  offers manual retry of that same request ID.
- Shows a current-page exchange only. It clears on owner exit and never claims
  historical replay after a refresh.
- A task result uses `window.loadTask(id)`; an approval result links only to
  the existing workbench, without adding an approval mutation.
- On route exit, V4 disable or legacy fallback it increments render authority,
  hides the root, and sets `inert`; late responses cannot alter visible UI.

## Post Review Pass 1

One issue was found during implementation review: after an uncertain result,
the compositor could have accepted a changed draft while retry logic correctly
retained the original receipt. That would make the user's intent ambiguous.
The compositor is now disabled while the retry decision is outstanding; the
only available next action is an explicit retry with the original request ID.

No model controls, hidden model identifiers, attachment affordances, approval
write calls, project selectors, direct task creation or backend edits appeared
in the diff.

## Post Review Pass 2

Executed local browser rehearsal passes:

1. AI Chat is a visible V4 frontstage while legacy Overview is hidden.
2. Dispatch sends only the established auto-route payload.
3. Retry reuses the same receipt header and does not render a second user turn.
4. A task uses the existing task opener and selects Work.
5. A delayed response remains absent after leaving Chat.
6. Return Legacy leaves Chat hidden/inert and focuses the visible legacy heading.
7. At 390×844, the labelled composer has no document or surface horizontal overflow.

Regression evidence:

- `python tests/test_v4_ai_chat_slice.py` — PASS.
- `python tests/test_v4_shell_phase1.py` — 5 PASS.
- `python tools/run_public_tests.py` — 295 Python files compiled, 30 public tests PASS.
- `python -m pytest -q` in this clean worktree — 35 PASS.
- `node tools/run_v4_ai_chat_browser_rehearsal.cjs` with the bundled local
  Node package path and local Chrome — 7 scenarios PASS.
- `node --check` passes for the new JavaScript files; `git diff --check` is clean.

## Final score: 97/100 — MERGE_READY_WITH_CONTROLS

This is a local, reviewable code conclusion. It is not a deployment approval.
The remaining three points cover the external local-browser dependency and
live runtime verification of receipt-service configuration, role/error states
and real data. No production frontend, backend, database, service, model
routing, approval policy, QQ path or feature default has been changed.

## Merge-readiness decision

The AI Chat slice may be submitted as a **stacked Draft PR** whose base is the
unmerged Artifact branch. It must not be merged, deployed or made default-on
under this instruction. After Artifact merges, the AI Chat PR base can be
changed to `main` without changing the committed implementation.

## Controlled follow-ups, intentionally not started

- Live opt-in authorization/error-state verification.
- A supported web-native idempotency contract if receipt configuration cannot
  be guaranteed at rollout.
- Restorable conversation history, project/task context selection and rich
  approval handoff — each needs its own API/behavior contract.
- QQ, Work, Memory, XiaoFei and Console slices.
