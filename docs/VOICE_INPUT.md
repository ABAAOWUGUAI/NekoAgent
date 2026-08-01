# Owner-private voice-message input

> Simplified Chinese: [docs/zh-CN/VOICE_INPUT.md](zh-CN/VOICE_INPUT.md)

NekoAgent can optionally accept a voice message from the Owner in a private QQ
conversation. This is an input adapter, not a VoicePack, voice synthesis system
or native QQ call feature. The transcript enters the same platform chain as a
text message and the reply is delivered as text.

```text
Owner-private QQ record
  -> authenticated AstrBot adapter
  -> bounded HTTPS media fetch
  -> local ffmpeg decode
  -> local whisper.cpp transcription
  -> Continuity Turn and Interaction Plan
  -> normal Delivery and confirmed ACK
```

## Safety boundary

Voice input is disabled by default and fails closed. The implementation:

- accepts only authenticated QQ Channel requests for Owner-private record
  events; group voice and other media types are rejected;
- allows one record component, at most 10 MiB and at most 60 seconds;
- requires HTTPS, an explicit media-host suffix allowlist, DNS validation and
  peer-IP pinning, with no credentials, fragments or arbitrary ports;
- uses a single local transcription slot, a bounded timeout and at most two
  CPU threads;
- stores only receipt/provenance metadata and hashes, never the signed media
  URL or transcript in the voice receipt;
- deletes downloaded audio and temporary decoded files after processing;
- keeps transport-probe and controlled-fetch diagnostic flags disabled when
  the complete voice-input flag is enabled.

The normal Conversation record contains the Owner's transcript because that is
the message supplied to the Assistant. Protect the Assistant database under the
same private-message retention and backup policy as text conversations.

## Local runtime prerequisites

Install `ffmpeg`, a `whisper.cpp` CLI and a multilingual model on the Bridge
host. Model binaries are not included in this repository. Keep the executable
and model in an immutable, service-readable location and independently verify
the model's SHA256 before configuration.

After the database migrations have run, configure the following
`qq_channel_settings` fields for the deployment:

- `voice_transcription_executable`
- `voice_transcription_model_path`
- `voice_transcription_model_sha256`
- `voice_transcription_ffmpeg`
- `voice_transcription_threads` (`1` or `2`)
- `voice_transcription_timeout_seconds` (`10` through `120`)
- `voice_transcription_max_duration_seconds` (`1` through `60`)

Do not enable the feature while any path or digest is a placeholder. The public
edition intentionally supplies no private runtime path or preinstalled model.

## Explicit cutover

Back up the Assistant SQLite database with SQLite's backup API. Keep both
diagnostic flags off, then run the preflight and enable command against the
deployment's real Assistant database:

```text
python tools/set_voice_input.py --assistant-db <assistant-db-path> --enable --confirm-owner-private-text-reply-only
```

The command validates the schema, executable permissions, decoder, model file
and exact SHA256 before changing the feature flag. Disable without the
confirmation switch:

```text
python tools/set_voice_input.py --assistant-db <assistant-db-path> --disable
```

Restart only the Bridge and AstrBot adapter when their deployed source changed.
Do not restart the QQ transport merely to apply this platform feature.

## Acceptance Gate

Send one new, low-risk Owner-private voice message and prove one unique chain:

1. one Voice Receipt reaches fetched, transcribed and dispatched completion;
2. source deletion is recorded and no temporary audio remains;
3. one Conversation message has `source_type=qq_voice_transcript` and the same
   external event identity;
4. its Continuity Turn and Interaction Plan reach a truthful terminal state;
5. an empty Skill Plan is `not_applied`, or selected Skills have real outcomes;
6. one Delivery is linked to that turn and reaches confirmed ACK;
7. no duplicate reply or unintended Learning candidate is created.

Service health, adapter load, transcription success, a queued Delivery or
`sent` alone does not pass this Gate. Stop at the first missing evidence and
repair that boundary before enabling more channels or voice capabilities.

Voice replies are a separate, explicitly configured capability documented in
[Voice Output](VOICE_OUTPUT.md). Group voice participation and native QQ voice
calls remain excluded.
