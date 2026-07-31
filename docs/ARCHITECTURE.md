# Architecture

The platform is a modular Python and SQLite application, not an AstrBot fork
and not a Codex wrapper.

```text
Web / AstrBot / other Channel Adapter
  → Bridge application and domain services
  → Capability, approval and network policy
  → model connection or optional Codex executor
  → SQLite repositories, artifact storage and Delivery Outbox
```

- **AstrBot** owns its event runtime and plugin lifecycle. The optional plugin
  only translates structured events and Delivery acknowledgements.
- **Codex** owns its login and local runtime. The platform stores only routing
  metadata and checks an executor profile before approved work.
- **Model Providers** are connection instances. A provider catalog is not a
  permission grant and not a promise that a model works for every account.
- **Assistant Instance** owns identity and relationship continuity. Persona,
  PetPack, model and channel bindings are replaceable resources.

The public edition contains no default QQ account, character, memory, group
policy, learned preference, PetPack or media bundle.
