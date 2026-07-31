# Repository protection guide

> 简体中文说明：[docs/zh-CN/REPOSITORY_PROTECTION.md](zh-CN/REPOSITORY_PROTECTION.md)

This guide turns the repository's security expectations into GitHub settings
that a repository administrator can verify. Committing this file, a workflow,
or `.github/CODEOWNERS` **does not** enable those GitHub settings by itself.

## 1. Keep reports private

For a public fork or upstream repository, enable GitHub's private vulnerability
reporting in **Settings → Security and analysis**. A reporter should be able to
send a security report without opening a public issue or exposing a proof of
concept. Link the report to the response process in
[SECURITY.md](../SECURITY.md).

If private reporting is unavailable for the account or plan, ask reporters to
use the contact method in `SECURITY.md`; do not ask them to publish exploit
details in an issue.

## 2. Protect the default branch

Create a GitHub ruleset (or branch protection rule) for the default branch
before accepting outside changes. The baseline should:

1. Require a pull request before merge.
2. Require at least one approving review for changes from other contributors.
3. Require review from a code owner when the changed path matches
   [`.github/CODEOWNERS`](../.github/CODEOWNERS).
4. Require the `Verify public source / public-source-gate` check from
   [`.github/workflows/verify.yml`](../.github/workflows/verify.yml). Select
   the exact check name GitHub displays for the repository.
5. Block force pushes and branch deletion.
6. Apply the rule to administrators as well, unless a documented emergency
   procedure requires a time-limited exception.

For a single-maintainer repository, a self-review is not meaningful. Keep the
required CI check, protect force-pushes, and use a second trusted maintainer or
a reviewed release checklist before changing sensitive paths.

## 3. Review automation and credentials

- Keep workflow permissions minimal; the included verification workflow needs
  repository read access only.
- Never put provider keys, QQ credentials, Codex login state, cookies, SQLite
  data, or production manifests in repository secrets, workflow logs, example
  files, or issue attachments.
- Review every GitHub App, Action, deploy key, and personal access token with
  repository access. Remove access that is no longer required.
- Pin or review third-party Actions before adding them. Prefer the official
  `actions/checkout` and `actions/setup-python` actions already used here.
- Turn on Dependabot alerts and secret scanning when they are available for the
  repository. Treat an alert as an investigation, not automatic permission to
  change production configuration.

## 4. Protect releases

Before publishing a tag, release, or container image:

1. Review the exported file manifest and secret scan result.
2. Run `python tools/run_public_tests.py` in the exact exported directory.
3. Run `python tools/install_starter_pack.py --pack-dir <public-pack-dir>
   --dry-run`; it must only inspect the selected optional pack and must not
   import runtime state.
4. Confirm the release notes distinguish verified behavior from optional or
   unverified integrations.
5. Publish from a reviewed commit on the protected default branch. Do not
   force-update a public release tag.

## 5. Periodic audit

At least quarterly, or after a maintainer changes:

- review branch rules and code owners;
- review security-reporting settings and contact details;
- remove stale collaborators, Apps, deploy keys, and workflow secrets;
- inspect recent Actions runs for unexpected write permissions or artifacts;
- run the public export gate again before distributing a new snapshot.

These controls protect the public source repository. They do not replace the
runtime capability, approval, network, delivery-acknowledgement, and audit
boundaries described in the product documentation.
