# media_edge

Media edge service: device audio over Direct Device WSS, bridged to Voice Core over
gRPC (memoria.media.v1). Separate Go module (`memoria/services/media_edge`,
go 1.26.10); single `mediaedge` package, cgo + libopus. Earned this file: score 13
(file count, code ratio, symbol/export/ref density) + distinct domain.

## WHERE TO LOOK

| Task | Location | Notes |
|------|----------|-------|
| Device frame codec + control envelope | `device_ws_protocol.go` | 30-byte network-order header, v2 required |
| Device JWT, tickets, leases | `device_ws_auth.go` | Replay capacity 100k, 5m TTL |
| Connection state machine | `device_ws_session.go` | hello CAS, token bucket, teardown |
| Uplink handlers + decode | `device_ws_uplink.go` | 16 kHz Opus in |
| Downlink resample + writeLoop | `device_ws_downlink.go` | 24k→16k, wire clock projection |
| Voice Core bridge | `bridge.go` / `bridge_supervisor.go` | Fenced streams, no retry/replay |
| Session + fence authority | `session.go` / `session_generation.go` | Advance/Cancel/Reconnect |
| Core runtime + event validation | `bridge_runtime.go` / `bridge_events.go` / `bridge_session_send.go` | gRPC seam, oneof events |
| HTTP edge | `server_http.go` / `server_http_router.go` | `/v1/device/media`, health/readyz/metrics |
| Opus codec | `opus_encoder.go` / `opus_decoder.go` | Mutex must cover destroy (P2-04) |
| VAD | `media_vad.go` | Threshold learned from non-speech only |
| Redis device state | `device_ws_shared_state.go` | Lua leases, fail closed |
| Entrypoint / env wiring | `cmd/mediaruntime/main.go` | Env-only config, TLS builders |
| Generated protobuf | `gen/` | Never hand-edit, CI diffs |
| Formal model | `formal/` | See `formal/AGENTS.md` |

## CONVENTIONS

- Strict JSON everywhere: `jsonUnmarshalStrict` + `requireJSONEOF`; control messages
  carry `"version": 2`, v1 refused, never downgraded.
- Fence (session/turn/generation/tool/session epochs) is the only downlink authority;
  cancel keeps fence with `generation_id+1`, `generationActive=false`.
- Audio: uplink 16 kHz / 320 samples Opus; downlink 24 kHz mono (16 kHz negotiable);
  20 ms frames. Bridge demands exactly 16k up / 24k down.
- Fail closed: mTLS unless `AllowInsecureDevelopment`; HS256 only via
  `AllowHS256DevOnly` + ≥32-byte secret; unknown audio mode → `half_duplex_safe`;
  unknown authority → error; `ENVIRONMENT=production` blocks insecure verifier.
- Error strings plain lowercase; sentinel errors only where callers compare
  (`ErrStaleDownlinkGeneration`, `ErrStaleStopFence`, `ErrDeviceSharedStateUnavailable`).
- Metrics hand-written Prometheus text (`media_edge_voice_core_*`, server.metrics).
- Tests: `Test<Area><Behavior>`, same package, injected runtime/dialer/clock hooks;
  `interaction_contract_test.go` pins protobuf bytes against a Python wire vector.

## ANTI-PATTERNS

- Fence is sole authority: a sequence number orders frames within an epoch, never
  replaces the generation gate; stale event is an error, not a queue clear.
- Never derive a device-side N+1 generation from cancel; never replay SessionHello/
  audio/generation/tool events on bridge reconnect; reconnect needs strictly larger
  stream_epoch; playback window not cleared on cancel (successor fence).
- Never hand-edit `gen/**`; regenerate via `scripts/generate_media_go_proto.sh`.
- Never hold `stateMu` across a gRPC Send; `sendMu` then `stateMu`; free libopus state
  never under its mutex; never silently fall back to process-local device state.
- `session.error` must flush before close — firmware reads terminal status only from it,
  never from the WebSocket close code.
- Forbidden barge dropped silently; device may never upgrade watermark quality to exact;
  write error must close the connection (gorilla sticky error).
- `go_shadow`/`go_authoritative` authority refused; Python authority only.
- Never use HTTP reference downlink queue in production: keep
  `RequireExternalDownlinkSender` / `AllowInsecureDevelopment` false.

## COMMANDS

```bash
cd services/media_edge
go test ./...                 # CI
go test -race ./...           # review gate
go vet ./... && test -z "$(gofmt -l .)"
scripts/generate_media_go_proto.sh   # repo root; then diff gen/ must be clean
```
