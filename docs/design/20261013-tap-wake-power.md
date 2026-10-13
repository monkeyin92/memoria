# Tap wake, mic-off standby, power states (2026-10-13)

Owner-approved product change. One PR. Do not merge from this agent.

## Domain model (principle: Model the domain)

Power and wake are one explicit state machine, not scattered ifs on the board file.

```
DeepSleep --tap(GPIO10 RTC)--> Waking
Waking    --quiet hours?--> NightSoothing --clip done--> DeepSleep
Waking    --else--> Connecting --channel ready--> Conversing
Conversing --15s no speech--> WifiHold
WifiHold  --tap--> Waking (mic still closed until conversation arms)
WifiHold  --hold window expires (default 3 min)--> DeepSleep
```

Illegal combinations made unrepresentable:

- Keyword wake cannot be armed while product policy is tap-only.
- Mic cannot be wanted open while phase is WifiHold or DeepSleep.
- Night tap cannot open a conversation channel.

## Designs compared (principle: Foundational thinking)

1. Keep `wake_mode` enum, default to `button`. Smallest server diff. Rejected: leaves keyword paths live (violates Subtract before you add).
2. Delete voice-wake packaging and gate; force tap-only; add `memoria_power_policy` + `memoria_mic_gate`. Chosen.
3. Split power policy into Application patches only. Rejected: board, display, and audio would re-encode the same transitions.

## Subtract before add (principle: Subtract before you add)

Remove first:

- `CONFIG_USE_CUSTOM_WAKE_WORD` and `CONFIG_SR_MN_CN_MULTINET6_QUANT` (packages `mn6_cn`, ~3.7 MB inferred from prior acceptance notes).
- Keyword branches in `memoria_wake_mode`, USB/tap wake_mode gates, mini-program wake switches, server acceptance of keyword modes.
- `check-overlay` rules that require MultiNet6 for this board.

Keep: AFE AEC/NS/VAD for conversation. Local stop keyword stays off (`kLocalStopKeywordEnabled=false`), so MultiNet is not required.

Then add: tap debounce, local 「我在呢」 clip, waiting animation, mic gate logs, WifiHold, deep sleep on GPIO10, offline story pack.

## Flash (principle: Explain the number)

Partition table already ends at 16 MiB (`assets` at `0x800000` size 8M). Claimed `FLASHSIZE_32MB` / `N32R16` is wrong for the EchoEar/VoCat N16R16 board. Rename overlay table to `16m.csv` and set `FLASHSIZE_16MB`. App slots stay `0x3f0000` each; assets stay 8 MiB.

## Mic and GPIO48 (principle: Prove it works / report findings)

Measured from board source: `CORDEC_POWER_CTRL` is GPIO48. `DetectPcbVersion` toggles it and probes I2C `0x18` (ES8311). ES7210 uses `AUDIO_CODEC_ES7210_ADDR`. Upstream `BoxAudioCodec::EnableInput(false)` closes the ES7210 codec device (`esp_codec_dev_close`) and does not toggle GPIO48. Inferred: GPIO48 is a shared codec rail used for ES8311 bring-up detection; it is not a dedicated ES7210 power switch in this tree. Mic-off policy therefore uses `EnableInput(false)` / voice-processing off, and logs every open/close at tag `MemoriaMic`.

## Quiet hours / night tap (principle: Boundary discipline)

Server already enforces quiet hours on the conversation path (`minor_quiet_hours`). Device deep sleep and WifiHold need a local copy so a night tap can play a soothing line without opening the channel. Deliver optional `quiet_hours` on the existing display-profile poll (additive), cache in NVS, evaluate on device in Asia/Shanghai wall clock when available, else last-cached window only.

## Offline stories (principle: Explain the number)

Freed MultiNet space goes to Opus 16 kbps story clips in the board assets extra-files dir. Budget: assets partition 8 MiB; keep >= 0.5 MiB free margin after mascots + prompts + stories. Packing script produces placeholders; real recordings replace later.

## Blast radius

| Area | Change |
|------|--------|
| `config.json` / `config.bench.json` | wake disabled, MN none, 16MB flash |
| `partitions/v2/32m.csv` -> `16m.csv` | rename + comment |
| `check-overlay.sh` | stop requiring MN6; require wake disabled + 16MB |
| patches `0032` wake mode | retarget to arm mic-off idle, not keyword |
| new patch for power/mic/sleep | Application + audio_service hooks |
| control_api settings | `wake_mode` only `button` |
| display-profile | optional `quiet_hours`; `wake_mode` stays `button` |
| mini-program device page | remove wake switches; copy says tap only |
| contracts `device-media-v2.json` | wake_mode enum -> `button` only |
| host tests | wake_mode, usb, overlay, protocol source, device page |

## Verification plan

Host: firmware unit tests, control_api tests, miniprogram device-page tests, `check-overlay` after bootstrap. Device build: `build.sh`, report `app.bin` and assets sizes vs 8 MiB with 0.5 MiB margin. On-device (owner): tap debounce, 1s prompt, 5s ready, mic logs, WifiHold, deep sleep wake, night soothing, offline stories.

## Measured build (2026-10-13, host)

Product `./scripts/build.sh --no-idf-install` after overlay hash `d8035b49…`. Limiter for flash is the partition map, not the binary packing script.

| Artifact | Bytes | Limit | Notes |
|---|---:|---:|---|
| `memoria-esp-vocat-app.bin` | 3,354,432 | 0x3f0000 (4,063,232) OTA slot | measured |
| `generated_assets.bin` | 2,186,328 | 8 MiB assets partition | measured; free 6,202,280 (≥0.5 MiB) |
| board `assets/` tree | 812,839 | feeds generated pack | measured; includes 3 placeholder stories + prompts |

sdkconfig (measured): `CONFIG_WAKE_WORD_DISABLED=y`, `CONFIG_SR_MN_CN_NONE=y`, `CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y`, partition `partitions/v2/16m.csv`. MultiNet6 not selected.

## Adversarial review (before PR)

- `SetOnScreenOff` was private; board could not wire WifiHold. Fixed (compile-fail → public next to `SetOnFirstFrame`).
- Quiet hours on the wire today map `night_mode` / `do_not_disturb` to `00:00`–`23:59`. Guardian consent windows are not yet copied onto the same poll. Documented gap.
- `MemoriaMic` logs policy edges from `SetPhase`. Actual `EnableInput(false)` still rides `AudioService` idle power-down after wake-word and voice-processing are off. On-device serial must confirm timing.
- Waiting animation is the existing connecting/listening mascot path after `WakeWordInvoke("screen_tap")`, not a new clip. On-device check required.
- Story Ogg files are ffmpeg sine placeholders at Opus 16 kbps. Product recordings replace later via `build_offline_stories.py`.

## Open questions

- Exact ES7210 rail wiring beyond GPIO48 probe behavior (needs schematic or scope).
- Whether CST816S INT polarity for `ext0` wake is active-low on all PCB revs (code assumes active-low, matches current ISR usage).
- Whether guardian consent quiet-hours should replace or intersect the night_mode/DND full-day window on the display-profile poll.
- Wall-clock source quality after deep sleep before SNTP (night window may use stale NVS until sync).
