# Shared contracts

Executable contracts shared by the Agent, H5, and Mini Program media gateway.

- `events.schema.json` defines Agent-authoritative UI events. Clients **must**
  drop events with a smaller `generation_id` than the latest accepted one.
- `miniprogram-media.json` defines the Mini Program WebSocket handshake, PCM
  headers, fixed 20 ms downlink audio shape, control-event fields, and
  cross-language golden frames consumed by both Python and JavaScript tests.
- `realtime-facade.json` defines the deliberately small OpenAI Realtime-style
  mapping surface. It delegates to the existing `DuplexRuntime` and does not
  expose a second audio transport or pipeline.
- `media-events.schema.json` defines the versioned media-v1 DataChannel envelope;
  it carries stream epoch/sequence/generation metadata and remains separate from
  the Agent-authoritative UI event schema.
- `../proto/memoria/media/v1/*.proto` is the language-neutral Media Edge ↔ Voice
  Core contract. It is authored in this repository and deliberately contains no
  provider, memory, persona or tool payloads.

These files are compatibility fixtures, not runtime configuration. Change a
contract and both implementations in the same commit.
