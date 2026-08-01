# Owner-private voice-message output

> Simplified Chinese: [docs/zh-CN/VOICE_OUTPUT.md](zh-CN/VOICE_OUTPUT.md)

NekoAgent can optionally render an existing Assistant reply with a local
VoicePack and deliver it as a QQ voice message. This is a response modality,
not a second Conversation, a new Assistant identity or a native QQ call.

```text
Interaction Plan affect and Owner request
  -> server-owned response-modality policy
  -> local VoicePack and offline TTS
  -> immutable Audio Artifact
  -> existing Outbox lease
  -> QQ record message
  -> Delivery confirmed ACK
```

## Response-modality modes

The active Assistant has one versioned policy:

- `text_only`: never render voice;
- `explicit_only`: render only when the Owner explicitly requests voice;
- `emotion_auto`: explicit requests plus bounded automatic voice for selected
  affect kinds above the configured confidence threshold;
- `always`: prefer voice for ordinary Owner-private chat.

Automatic voice has a cooldown and a daily budget. An explicit opt-out always
wins. Explicit voice requests do not consume the automatic budget. The model
may propose affect or intent, but it never grants permission: the server still
requires an authenticated Owner-private scope, a chat dispatch, enabled voice
features, an active VoicePack and successful TTS/Artifact Gates.

## Local runtime and licensing

This repository includes no TTS model, voice asset or private VoicePack. The
deployer must select a locally executable TTS adapter and a VoicePack whose
license permits the intended use, store runtime/model configuration with exact
SHA256 digests, and keep runtime inference offline. Do not configure first-use
model downloads or send reply text to an undeclared external speech service.

Create and bind the VoicePack through the existing Assistant resource APIs or
Admin console. Verify the executable, model/config assets, language, sample
rate, file format and hashes before enabling `voice_output_v1`, then enable
`voice_delivery_v1` only after a local render and lease-bound Artifact read
both pass. The public source intentionally provides no production paths or
preselected voice.

The Admin console exposes the active Assistant's response-modality mode,
allowed affect kinds, minimum confidence, cooldown and daily limit. Updates use
an expected policy version and reject stale writes.

## Safety boundary

- Only authenticated Owner-private QQ chat can select voice; group chat and
  other Actors fail closed to text.
- TTS renders the already approved reply. It must not rewrite facts, code,
  commands, logs, quotations, Artifact hashes or execution status.
- The Audio Artifact is immutable and readable by the Adapter only through the
  current Delivery lease. Local storage paths and lease material are not sent
  to QQ.
- Failed synthesis or delivery falls back to truthful text. It must not claim
  that voice was sent.
- Ambiguous sends use the existing reconciliation path and must not be retried
  as a second voice message without evidence.

## Acceptance Gate

After a SQLite backup and a precise deployment, send one new Owner-private
message that explicitly requests voice, or use a natural affective message
when `emotion_auto` is configured. Prove one unique chain:

1. the Interaction Plan records only bounded affect/trigger metadata;
2. one immutable Audio Artifact exists with verified media properties and hash;
3. one `assistant_voice_reply` Outbox item binds that Artifact and Turn;
4. the Adapter starts the send under the current lease;
5. a platform message identifier is present and Delivery reaches confirmed ACK;
6. the Owner confirms that the voice message is playable;
7. no duplicate Delivery, leaked temporary file or unintended Learning
   candidate is created.

A TTS file, healthy service, loaded Adapter, queued Outbox item or `sent`
status alone does not pass the Gate. Stop at the first missing boundary.

Voice input is documented separately in [Voice Input](VOICE_INPUT.md). Group
voice participation and native QQ real-time calls remain separate, disabled
features.
