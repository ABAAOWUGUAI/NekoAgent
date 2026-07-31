# Operations and recovery guide

NekoAgent is stateful. Treat Assistant identity, relationship, conversation,
memory, goals, runs, Delivery and Learning as deployment-owned data, not as
replaceable model or channel cache.

## Daily operating checks

Use the local health and admin-version endpoints through the private listener,
then review only redacted service logs and the console's diagnostics. A service
being active, a plugin being loaded or a task being marked done is not proof of
an end-to-end outcome.

For a real channel request, retain the minimum linked evidence:

```text
Inbound -> Identity/Access -> Turn -> policy decision -> Goal/Run/Task
-> Delivery -> confirmed ACK -> Outcome -> Learning (if admitted)
```

If any link is missing, record the observed stop point rather than inferring
success from a later-looking status.

## Safe startup and shutdown

For local Windows use, run:

```powershell
.\deploy\start-local.ps1
```

The process must remain bound to the configured private address. Stop it with
the supervisor that owns it, or with `Ctrl+C` during an interactive local run.
Do not restart unrelated channel runtimes merely because the Bridge changed.

For a managed deployment, use the host's normal service supervisor with a
dedicated service account, restart policy and log rotation. The supervisor is
an operational wrapper; it must not inject model credentials into command
lines or publish runtime directories.

## Incident triage

| Symptom | First checks | Do not do |
|---|---|---|
| Bridge will not start | Validate local config paths and that the two token files exist; inspect the redacted startup error | Replace the runtime database or generate a new Assistant |
| Console cannot connect | Verify the private listener, reverse-proxy route and authenticated session | Open the Admin API to the Internet to test it |
| Model validation fails | Check the selected connection, model ID, provider response class and account-side permission | Mark a catalog model as usable without non-empty validation output |
| Channel shows a sent response | Inspect Delivery attempt, confirmed ACK and Outcome linkage | Claim the user's request completed from `sent` or a plugin log alone |
| Group behaviour is wrong | Check exact group policy, authorization, cooldown and decision evidence | Raise global participation or bypass a group policy to force a reply |
| Image or media processing fails | Check the configured media Gate, accepted type and provider capability | Forward raw media URLs or files to an arbitrary Provider |

Use synthetic or redacted reproduction data in issue reports. Never attach
private chats, media, SQLite files, keys, cookies or channel credentials.

## Backup and restore

Back up the Assistant database, Task database and artifact storage as one
logical unit. Use the application's SQLite backup path or an equivalent
consistent snapshot; do not copy a live SQLite database together with only some
of its WAL files. Store backups outside the checkout, protect them with the
same access controls as runtime data and test a restore into an isolated host.

A restore test is successful only when it can:

1. start with the restored databases and artifacts;
2. preserve Assistant identity and expected state boundaries; and
3. serve a low-risk verification request without replaying old Delivery items.

Do not restore a backup merely to retry a failed Delivery. Retry only through
the Delivery policy after determining whether the original result was sent,
acknowledged, uncertain or failed.

## Upgrade procedure

1. Read the changelog and deployment notes for the target version.
2. Create a file-level diff or hash list. Never overwrite an entire production
   directory from a working checkout.
3. Make a consistent SQLite/artifact backup before schema or runtime changes.
4. Run `python tools/run_public_tests.py` in an isolated copy of the target.
5. Deploy only the affected files, restart only the affected service, then
   recheck private health, configuration version and redacted errors.
6. Run a proportionate real acceptance request. For channel work, require a
   new `Delivery confirmed ACK`; for Learning, require the scoped candidate,
   approval/application and feedback/undo evidence.
7. Record the version, changed files, verification result and rollback path.

## Logs, metrics and privacy

Logs should contain identifiers, decision classes and redacted error categories
needed to troubleshoot a request. They must not become a second store of
private conversation bodies, media URLs, tokens or Provider credentials.

Limit access to the console, logs, backups and artifact storage to operators
with a reason to use them. Rotate or remove stale local files through the
runtime retention policy, not with broad deletion commands against a checkout
or home directory.

## Escalation rule

Stop at the failed Gate. Record the evidence, root-cause scope and smallest
safe repair path. Do not compensate for a missing ACK, model validation result
or approval by widening network access, disabling a policy or inventing a
successful outcome.
