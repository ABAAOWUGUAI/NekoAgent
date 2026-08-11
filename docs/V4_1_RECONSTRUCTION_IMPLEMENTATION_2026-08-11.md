# NekoAgent V4.1 — Phase 1 correction and first Artifact slice

> **HISTORICAL IMPLEMENTATION RECORD**
>
> This preserves the Artifact-slice implementation evidence created before
> merge. PR #34 was later merged to `main` at
> `59a83a95d95af421b433897fcf03fc73a511397f`; the historical conclusions below
> are not rewritten. The current state source is the workspace `CURRENT_STATE.md`
> plus the current `main`.

Date: 2026-08-11
Status: local implementation only; **not deployed**

This record is the implementation companion to the existing V4.1 semantic-ownership package. The V4.1 experience architecture remains frozen: Overview, QQ, AI Chat, Work, Artifact, Memory, XiaoFei, Console and Settings. It does not introduce V4.2, restore 17 legacy views as primary navigation, or retire any legacy behavior.

## 1. Review Pass 1

### Baseline and patch boundary

- **BASE:** public `main` at `0b20f2c1d9a64b2f6c1d1bdd166019346051b94f`, cloned into a clean worktree.
- **LOCAL R6:** the local snapshot's prior shell experiment.
- **INTENDED PATCH:** only V4 shell assets, their existing static-asset registrations, a focused regression test, and this implementation record.

The snapshot had 13 admin-file differences from current `main`. Only the V4 shell concept was intentional. Snapshot-only changes to `core.js`, `runtime.js`, legacy partials/persona files and pet/favicon assets were excluded. No whole-directory copy was used.

### Confirmed and additional defects

| Finding | Severity | Resolution |
| --- | --- | --- |
| Command Palette could attach a second navigation action to a button already bound by `createRouteButton`. | Blocking | Command entries now use the factory's single handler and close the dialog through its pre-navigation hook. |
| Shell ownership did not robustly cover every legacy view. | Blocking | One exact 17-view map now resolves to nine owner surfaces; legacy views are not reinstated in the sidebar. |
| An animation-frame callback and the view-class MutationObserver could read cleared/mutable transition state and overwrite an explicit compatibility route such as AI Chat. | Blocking | The shell captures `preserveExplicit` synchronously and retains the explicit route through the complete microtask/animation-frame window. |
| Security, Operational Notification, AI Chat Workbench and Artifact Revise/Delete had no separately named legacy behavior-group disposition. | High-confidence contract gap | The canonical registry now has four explicit protected groups; the existing routes remain intact. |

No production blocker remains for a local, opt-in patch. Live authorization/error-state parity remains a deployment-stage gate, not a local implementation claim.

## 2. Feasibility Assessment

**Decision: GO_WITH_CONTROLS.**

The existing frontend already has `window.switchView` and the existing bridge helper; no backend route, schema or service change is required. The V4 shell is opt-in through `?experience=v4` or session state, and legacy routes stay mounted. The clean patch is separable from snapshot drift and can be reverted as one patch.

Controls: no default-on flag, no deletion of legacy UI, no change to model routing or persona source files, no write call in the new Artifact slice, and an explicit full-management fallback before behavior parity.

## 3. Phase 1 Corrections

- Added `admin-v4-shell.css` and `v4-shell.js`, scoped to `body[data-v4-experience="active"]`.
- Registered the two assets through both `admin/index.html` and the existing `admin_console.py` allowlist.
- Defined exactly nine owner surfaces and exactly one owner for all 17 legacy view keys.
- Made one click equal one legacy `switchView` transition; Ctrl/Cmd+K and Escape retain dialog keyboard behavior.
- Dispatched a route-change event only after selection is resolved, so an owner-local slice can react without changing legacy navigation semantics.

## 4. Review Pass 2

### Adversarial findings and checks

- **Navigation:** local Chromium rehearsal verified Sidebar → AI Chat invokes one `overview` transition while retaining AI Chat selection; Command Palette → Work invokes one `tasks` transition and closes; programmatic `switchView('proxy')` selects Console.
- **Legacy fallback:** the shell still delegates to existing `switchView`; no legacy partial, deep-link target or action was removed.
- **Semantic boundaries:** `views-models.js`, `views-persona.js`, runtime routing and relationship/voice/expression sources are outside this diff. Manual model routing and Assistant Instance customization are therefore not replaced by a XiaoFei default.
- **Behavior depth:** the canonical registry now separately protects security/session rotation, operational notifications, AI Chat dispatch/context/work-start, and Artifact revision/delete. Skill/Plugin, three-way Voice and Habit/Asset distinctions remain separate groups.
- **Snapshot contamination:** no pet asset, favicon, `core.js`, `runtime.js`, legacy partial or backend file is in the patch.
- **Test-strength limitation:** the repository test is self-contained but mostly contract/static. The browser interaction rehearsal is explicit local external evidence (Chromium plus Playwright), not claimed as a bundled CI test.

No new blocking defect was found after the explicit-route retention fix. The remaining browser and live-API checks are controlled rollout gates.

## 5. Final Phase 1 Decision

**MERGE_READY_WITH_CONTROLS.** This means the patch is clean against the stated `main` commit, not that it is deployed or approved to enable V4 by default.

## 6. Clean Patch Summary

### Added

- `admin/admin-v4-shell.css` — opt-in V4.1 shell visual layer.
- `admin/v4-shell.js` — nine-surface navigation, exact 17-view ownership and one-transition command behavior.
- `admin/admin-v4-artifact-surface.css` — Artifact daily-layer presentation and explicit full-management reveal.
- `admin/v4-artifact-surface.js` — read-only recent-Artifact layer using the existing list endpoint.
- `tests/test_v4_shell_phase1.py` — clean-main asset, routing and Artifact boundary regression test.
- `tools/build_v4_1_reconstruction_patch.py` — builds and validates a forward-slash-only review ZIP for this clean patch.
- This record.

### Modified

- `admin/index.html` — versioned registration of the four V4 assets after legacy assets.
- `admin_console.py` — allowlists those same assets for safe versioned serving.

### Deleted

None.

Rollback is removal of the four V4 asset registrations and four V4 files (plus this test/record); the old console continues to own all retained actions.

## 7. Phase 2 Slice Selection

| Candidate | API readiness | Risk | Fallback strength | Decision |
| --- | --- | --- | --- | --- |
| Overview | already a shell lens | low | strong | not a meaningful first real migration |
| QQ | mixed product/infrastructure and delivery truth | high | strong | defer |
| Work | P0 state machines and approval semantics | high | strong | defer |
| **Artifact** | existing list endpoint and an isolated object boundary | **low** | **strong** | **selected** |
| XiaoFei | identity, policy, voice and learning coupling | medium/high | strong | defer |
| Console | sensitive operational and security actions | high | strong | defer |

Selected capability: `CAP-ARTIFACT-LIFECYCLE`. Protected contract: `MCON-ARTIFACT-LIFECYCLE`. Protected invariants: `INV-ARTIFACT-GRANT-GENERATION` and `INV-ARTIFACT-DELETION-TERMINAL`. Legacy source: `admin/views-artifacts.js`.

## 8. Phase 2 Implementation Result

Artifact now has one real V4 daily layer:

- When the V4 Artifact owner route is selected, it makes the existing read request `GET /assistant/artifacts?limit=6&offset=0` through the existing bridge helper.
- It shows a compact, truthful list of current recent items and explicit loading/error/empty states.
- It does not add POST/PUT/DELETE behavior.
- **完整库管理** reveals the existing Artifact Center. That fallback retains immutable versions, checksum, retention, download, revision, delete, publication, grant, stop, restore, extend, revoke and event history.

Local browser rehearsal supplied two returned items, verified the exact existing endpoint, rendered both items, then verified the full-management fallback became visible. No production request was made.

## 9. Remaining Controlled Risks

- The Artifact daily layer has not been verified against live authorization, feature-disabled or service-error responses; its error state and legacy fallback make this a controlled rollout gate.
- No bundled browser runner is added because the repository does not declare Playwright/Chromium as a portable dependency. The executed local browser rehearsal is documented as external evidence.
- Accessibility foundations remain in the shell's scoped focus, reduced-motion, forced-colors and narrow-layout rules; full axe and screen-size sign-off belong to the controlled rollout gate. No speech-reader tooling is used.
- No feature flag has been enabled, no service restarted, no database migrated and no production system changed.

## 10. Artifact vertical-slice Gate record (supersedes the earlier Phase 2 status)

### A. Review Pass 1 — implementation/repository analysis

The clean worktree is still based on public `main` `0b20f2c1d9a64b2f6c1d1bdd166019346051b94f`. The only migrated object surface is **Artifact**. Its actual implementation path is the existing read endpoint `GET /assistant/artifacts?limit=6&offset=0`, called through the existing `window.bridge` helper. The legacy `view-artifacts` view remains the owner of revisions, deletion, publication grants, version/retention controls, downloads and history.

Pass 1 found four material issues in the first local daily layer:

1. Leaving V4 removed the shell attribute but left `#v4ArtifactDaily` visible and interactive.
2. Route-change events could reload the same list repeatedly.
3. An item button promised an item-specific destination although no low-risk item handoff to the legacy center had been implemented.
4. An unknown legacy View key was silently classified as Overview.

### B. Review Pass 2 — adversarial behavior review

The first local Chromium rehearsal failed its disable scenario with `hidden=false`, `inert=false` and computed `display=block`. This was a hard stop: a V4-only layer could coexist with the old console. After that correction, keyboard execution exposed a second issue: command selection did not have a stable, intentional focus result in the synthetic browser harness. The shell now distinguishes cancel/escape (return focus to the command trigger) from route selection (the existing `switchView(..., { focusHeading: true })` moves focus to the legacy view heading).

No behavior is inferred from button presence. The Artifact daily layer makes no write request, does not fabricate source-work provenance, and never claims that an item-level legacy handoff exists.

### C. Initial weighted feasibility — **74/100, NO-GO; hard blockers: 1**

| Dimension | Weight | Initial score | Reason |
| --- | ---: | ---: | --- |
| Correctness & Lifecycle | 20 | 6 | Disable leakage was a blocking coexistence failure. |
| Legacy Safety & Rollback | 15 | 15 | Legacy Center and its full management remained reachable. |
| Behavior Preservation | 15 | 15 | Artifact protected behavior stayed legacy-owned. |
| Clean Patch & Baseline Safety | 10 | 10 | Clean current-main worktree; no snapshot import. |
| Testability & Evidence | 15 | 8 | Only static/contract evidence initially covered the slice. |
| V4.1 UX Fidelity | 10 | 9 | Daily/management separation was intact, but coexistence leakage made the result unsafe. |
| Accessibility / Resilience | 5 | 2 | Hidden state and keyboard-focus outcome were not proven. |
| Implementation Scope Control | 5 | 5 | Opt-in assets with no backend/database change. |
| Continuity / Documentation Truth | 5 | 4 | Migration state was not explicit enough. |

### D. Remediation performed

- Added the small V4 surface lifecycle contract: `nekoagent:v4-experience-enable`, `nekoagent:v4-experience-disable`, and the existing resolved route-change event.
- Artifact deactivation now cancels in-flight render authority (`requestVersion += 1`), sets `hidden` and `inert`, and clears its legacy-management mode. Its CSS is default-hidden and can render only when V4 is active on the Artifact route.
- Artifact activation reloads only on true entry/re-entry; duplicate route observations no longer duplicate the list read.
- All full-management labels now say **打开完整成品库**. This is intentionally a generic, truthful handoff because an item-ID transfer to the legacy Center has not been separately proven safe.
- Unknown legacy View keys now receive `legacy-unknown` and a visible compatibility notice; they are not silently mapped to Overview.
- Owner-surface metadata now declares `partial` versus `legacy`. Artifact is `partial`: *新版日用层；完整成品库仍使用旧控制台*.
- The canonical readiness statement records the same migration state as `PARTIAL_LOCAL_CLEAN_PATCH`; it explicitly says that this is not deployed.

### E. Final pre-implementation feasibility — **98/100, GO_WITH_CONTROLS; hard blockers: 0**

| Dimension | Weight | Final score | Evidence |
| --- | ---: | ---: | --- |
| Correctness & Lifecycle | 20 | 20 | Browser-tested disable, inert boundary, route re-entry and one mounted daily root. |
| Legacy Safety & Rollback | 15 | 15 | Full legacy management remains reachable with no legacy deletion. |
| Behavior Preservation | 15 | 15 | Read-only daily layer; protected Artifact behavior remains legacy-owned. |
| Clean Patch & Baseline Safety | 10 | 10 | Clean current-main patch with no snapshot contamination. |
| Testability & Evidence | 15 | 14 | Focused static test plus executed stateful local-browser rehearsal; runner is intentionally external to portable CI. |
| V4.1 UX Fidelity | 10 | 9 | Recognizable recent work with real version status and source-work linkage; item deep-linking is deliberately deferred. |
| Accessibility / Resilience | 5 | 5 | Hidden/inert safety, keyboard command, Escape restore, heading focus after navigation, `role=status` and `aria-live`. |
| Implementation Scope Control | 5 | 5 | Scoped opt-in assets; no legacy deletion or server change. |
| Continuity / Documentation Truth | 5 | 5 | Explicit partial/local state and non-claims recorded. |

The two-point deduction is for the non-portable browser dependency and the intentionally deferred item-level deep-link. It is **not** a claim that production parity is complete. The score is a local Artifact-slice feasibility score, not a deployment approval.

### F–I. Implementation and post-implementation review

The implementation is deliberately narrow:

- `admin/v4-shell.js` supplies resolved ownership, explicit implementation-state metadata, the lifecycle events and no silent unknown-View fallback.
- `admin/v4-artifact-surface.js` supplies the read-only daily layer, truthful current-version state (`准备中` / `已就绪` / `生成失败` / `暂无当前版本`), source-goal linkage without raw IDs, cancellation/inert lifecycle and the legacy full-management handoff.
- `admin/admin-v4-artifact-surface.css` makes the daily layer default-hidden and permits it only inside the selected V4 Artifact route.
- `tools/run_v4_artifact_browser_rehearsal.cjs` is a reproducible external local-browser rehearsal, not a CI dependency.

Post Review 1 found the command-focus ambiguity described above and fixed it. Post Review 2 found no new blocking defect in the isolated Artifact lifecycle. The repeated browser run covered: V4 disable/inert; Artifact → Work → Artifact request counts; Ctrl+K/Escape and keyboard route selection; proxy → Console and projects → Work ownership; error fallback; and an empty result. The result reported five Artifact reads, one mounted daily root, and retained Artifact selection.

### I. Post-implementation feasibility — **98/100, GO_WITH_CONTROLS; hard blockers: 0**

The post-implementation score uses the same 20/15/15/10/15/10/5/5/5 weighted rubric above. It remains 98 because the post reviews added evidence and exposed no new capability, lifecycle, owner-mapping, fallback, write-path or documentation regression. This is still only a local merge-readiness conclusion.

### J–N. Decision, artifact result and remaining risks

**Decision: MERGE_READY_WITH_CONTROLS for the local clean patch; NOT DEPLOYMENT_READY.**

The portable patch contains the shell, Artifact slice, focused test, browser rehearsal and this record. It has no production configuration, credentials, database migration, service restart, feature-default change or legacy deletion.

Remaining controlled risks:

1. The browser rehearsal uses a local Chromium and Playwright installation, so it is execution evidence rather than a dependency-free public gate.
2. Live roles, authorization failures, feature-disabled responses and real Artifact payload variation remain rollout gates.
3. The generic management handoff is correct for now; item-specific legacy deep-linking requires a separate contract and test.
4. QQ, Work, Console and all other Owner Surfaces remain outside this slice.

Recommended next slice: do **not** begin it until an Owner approves this Artifact result. If approved, choose exactly one capability contract with an existing truthful read/write boundary; do not bundle QQ, Work or Console into the Artifact rollout.

## 11. External browser evidence ledger

This ledger is intentionally separate from the dependency-free repository tests.

| Field | Recorded evidence |
| --- | --- |
| Date | 2026-08-11 |
| Target commit | `0b20f2c1d9a64b2f6c1d1bdd166019346051b94f` (local `HEAD` and `origin/main` rechecked equal) |
| Worktree | `C:\\FUWUQI\\worktrees\\nekoagent-main-20260811` |
| Harness | `tools/run_v4_artifact_browser_rehearsal.cjs` |
| Environment | Local Node runtime, Playwright supplied through `NODE_PATH`, and `C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe`; no production browser session or production URL was used. |
| Command | `node tools/run_v4_artifact_browser_rehearsal.cjs` with the local runtime and `NODE_PATH` described above |
| Result | `A:disable-and-inert`, `B:owner-round-trip`, `C:keyboard-command-and-focus`, `D:proxy-owner`, `E:projects-owner`, `F:error-fallback`, `G:empty` all passed; `requestCount=5`, `selected=artifact`. |

The original rehearsal failure is retained as review evidence, not hidden: before remediation it reported `hidden=false`, `inert=false`, `display=block` after Return Legacy. The successful run demonstrates the correction; it does not substitute for a production rollout check.

## 12. Reusable Surface Migration Pattern

The proved pattern for a future independent Owner Surface is:

`Owner Contract → Daily Layer → Management Layer → Technical Layer → Legacy Fallback → Behavior Parity → Cutover → Legacy Retirement`

These are complexity layers, not a mandatory page-count template. A Daily layer must use only proven data/actions, describe its migration state truthfully, deactivate on V4 disable, remain hidden and inert outside its owner route, and preserve a reachable legacy fallback until its protected behavior contract has parity evidence.
