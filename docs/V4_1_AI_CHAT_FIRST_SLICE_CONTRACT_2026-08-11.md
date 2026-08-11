# NekoAgent V4.1 AI Chat First Slice Contract

Date: 2026-08-11
Status: implementation contract; local opt-in slice only

## Product outcome

AI Chat is the Deep Work Frontstage: one owner-controlled conversation entry
that can return an answer, start or supplement a tracked work item, or state
that an existing approval is required. It is not an Overview alias, QQ Web,
an unbounded task console, or a model/runtime configuration page.

## Scope and exclusions

Included: current-page conversation display, a single dispatch action,
existing conversation continuity, truthful task handoff and approval fallback.

Excluded: attachments, model selection or automatic model switching, model
backend details, raw reasoning, a new approval action, project selection,
conversation-history replay, backend/schema changes, and any default-on or
production change.

The current-page transcript is intentionally ephemeral. The existing service
may use its scoped web-console continuity path, but this slice does not claim
that it can read, reconstruct, or display persisted message history after a
refresh.

## API reality review

| Need | Existing implementation path | Classification | Slice decision |
| --- | --- | --- | --- |
| Dispatch and intent routing | `POST /assistant/dispatch`, `source=web-console`, `user_id=web-console`, `force=auto` | DIRECTLY_REUSABLE | Use unchanged. |
| Conversation continuity | Dispatch resolves `admin:web-console` follow-up history and records exchanges through the existing interaction store | DIRECTLY_REUSABLE | Keep backend-owned; do not fabricate a local thread. |
| Task creation / supplement | Dispatch returns `task` / `task_append` and task identity; existing `window.loadTask(id)` opens the legacy work view | REUSABLE_WITH_ADAPTER | Render status, then use the existing task opener. |
| Approval | Dispatch returns `approval_required`; existing work flow owns confirmation | REUSABLE_WITH_ADAPTER | Show status and open existing workbench; no approval mutation. |
| Retry idempotency | Dispatch already passes `X-QQ-Message-ID` to the existing receipt service; the browser helper supports request headers | REUSABLE_WITH_ADAPTER | Retain one generated request ID for retries; never auto-retry. |
| Current project/task context selection | No independently proven Chat-context read/write route | NOT_AVAILABLE | Do not show a project/task selector. |
| Attachment upload / analysis | No safe AI Chat attachment contract | NOT_AVAILABLE | No attachment UI. |
| Readable conversation history | Home exposes only protected conversation metadata, not message bodies | NOT_AVAILABLE | No history replay UI. |
| Model chooser / automatic switch | Manual routing and role bindings are Console-owned | NOT_AVAILABLE | No model control in Chat. |
| Rich inline approval / artifact workflow | Existing protected Work/Artifact behavior only | TARGET_GAP | Leave to later slices after contracts. |

## Non-negotiable behavior controls

1. One submitted request owns one generated receipt ID. A user-triggered retry
   reuses it; automatic retry is prohibited.
2. While a request is pending, the compositor submit action is disabled.
3. Leaving AI Chat, disabling V4, or opening legacy fallback invalidates the
   render authority of in-flight requests and makes the Chat root hidden and
   inert.
4. A task/approval result never pretends the action is complete: it reports
   the dispatch status and opens the existing owner surface for protected work.
5. Existing manual model routing, approval rules and contextual routing are
   preserved by using only the established dispatch path with `force=auto`.

## Acceptance bar

No implementation proceeds if the first two reviews find a duplicate-dispatch
path, stale response visibility after lifecycle exit, inaccessible legacy
fallback, manual-routing bypass, approval mutation, invented project/task
context, attachment simulation, or an unclean baseline.
