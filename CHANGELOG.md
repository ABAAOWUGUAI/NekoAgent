# Changelog

All notable public-source changes are documented here. This project follows
the spirit of [Keep a Changelog](https://keepachangelog.com/) and uses
semantic version tags after the first public release.

## Unreleased

## [0.2.0] - 2026-08-02

### Added

- Optional Owner-private QQ voice-message input through the AstrBot adapter,
  bounded Bridge fetch, local `ffmpeg` decoding and local `whisper.cpp`
  transcription. The signed media URL and transcript are not copied into
  receipt metadata, and source audio is deleted after processing.
- A fail-closed voice-input cutover command that requires an explicit
  text-reply-only confirmation and validates the local ASR executable, model
  digest and decoder before enabling the capability.

### Fixed

- Terminal Continuity outcomes now settle their bound Interaction Plan instead
  of leaving a confirmed turn in `dispatched`.
- Turns that selected no Skill now finish as `not_applied`; reconciliation can
  repair historical empty Skill Plans and terminal Interaction Plans only when
  final evidence exists.

### Security

- Voice input is disabled by default, accepts only authenticated Owner-private
  record events, enforces HTTPS host allowlists, DNS/peer-IP checks, size and
  duration limits, one ASR concurrency slot and ephemeral media retention.
- Voice output and native QQ voice calls remain outside this release.

## [0.1.1] - 2026-08-01

### Fixed

- Standalone Automation follow-ups such as “run it now” resolve through a
  quoted operational receipt or recent Automation context before execution.
- Model-proposed Automation actions that have not passed server-side object,
  capability and policy validation now fail closed instead of degrading to a
  conversational execution promise.
- Automation control replies use the operational Delivery lane and retain the
  bounded Automation job reference required for later quoted follow-ups.
- QQ quote identifiers and digests now remain attached to ordinary chat turns,
  preserving continuity without duplicating quoted private text.

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
