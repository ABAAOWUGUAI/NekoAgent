# Changelog

All notable public-source changes are documented here. This project follows
the spirit of [Keep a Changelog](https://keepachangelog.com/) and uses
semantic version tags after the first public release.

## Unreleased

### Added

- Owner-first platform core with replaceable Assistant, Channel, Model and
  Executor bindings.
- Optional AstrBot adapter and optional locally logged-in Codex executor.
- Public, explicitly opt-in Xiaofei Starter Pack with asset hashes and no
  imported private state.
- Export audit, public-source tests, local bootstrap instructions and GitHub
  verification workflow.

### Security

- No runtime databases, credentials, conversations, QQ account state or Codex
  login state are included in the public source candidate.
