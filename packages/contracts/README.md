# Shared contracts

Executable contracts shared by the Agent, H5, and Mini Program media gateway.

- `events.schema.json` defines Agent-authoritative UI events. Clients **must**
  drop events with a smaller `generation_id` than the latest accepted one.
- `miniprogram-media.json` defines the Mini Program WebSocket handshake, PCM
  headers, fixed 20 ms downlink audio shape, control-event fields, and
  cross-language golden frames consumed by both Python and JavaScript tests.

These files are compatibility fixtures, not runtime configuration. Change a
contract and both implementations in the same commit.
