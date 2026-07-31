# Changelog

All notable public-source changes are documented here. This project follows
the spirit of [Keep a Changelog](https://keepachangelog.com/) and uses
semantic version tags after the first public release.

## Unreleased

## [0.1.0] - 2026-08-01

### Added

- Owner-first platform core with replaceable Assistant, Channel, Model and
  Executor bindings.
- Optional AstrBot adapter and optional locally logged-in Codex executor.
- Public, explicitly opt-in Xiaofei Starter Pack with asset hashes and no
  imported private state.
- Export audit, public-source tests, local bootstrap instructions and GitHub
  verification workflow.
- Durable Automation Action Plans that preserve one bound Automation across
  clarification turns and enforce ordered `update → run_now` dependencies.
- Versioned Automation output contracts and evidence-backed GitHub result
  presentation with explicit unavailable states instead of guessed facts.
- Interaction Plan schema v2 with fail-closed dependencies and Automation Run
  snapshots for job revision, configuration and output-contract hashes.

### Security

- No runtime databases, credentials, conversations, QQ account state or Codex
  login state are included in the public source candidate.
- Quoted channel messages are represented by bounded identifiers and normalized
  content digests; quoted private text is not duplicated into control metadata.
