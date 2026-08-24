# Release 20260824-120459-dtln-stats-contract-fix

**Date**: 2026-08-24
**Type**: Agent Component (Agent + Voice Core Media Bridge)
**Base**: `20260823-210222-voice-fix`
**Source**: `9a71e446a7df1943044a81fe841427a5648633d5`

## Scope

- Restore the authoritative ESP32 Direct audio path by removing the parallel
  `ListeningStateManager` gate that dropped Media Edge-admitted PCM.
- Remove VAD/playback callbacks to the parallel manager, including the
  production `logger` NameError and missing `stop_speaking()` call.
- Install the official paired DTLN ONNX models and implement the upstream
  stateful 16 kHz streaming contract.
- Keep DTLN recurrent/overlap state per media session while sharing immutable
  ONNX sessions; send denoised PCM only to ASR and original PCM to speaker
  identity classification.
- Make Agent component rollback preserve distinct current Agent and Bridge
  sources even when a parent image record was pruned: rebuild the running
  source tree over the surviving same-release peer and verify a full-tree hash.
- Separate the component image version from the production stack runtime
  authority so Control heartbeat remains tied to the current full-stack tag.
- Remove stale tests and parallel documents that claimed the previous
  listening/denoising release was deployed successfully.
- Keep runtime availability fields in every denoising stats sample. The first
  real board session exposed a `KeyError` at the 100-frame log boundary; the
  hotfix adds the missing contract fields and a regression assertion.

## Model provenance

- Upstream: `https://github.com/breizhn/DTLN`
- Commit: `1de1f15a8b5b7e1c44905618ff2ef70ca8277fbc`
- `model_1.onnx`: `22b91cae3855e5a0620e66a917ca6c82c58db0e842c770f58d86751c5e8d4ae3`
- `model_2.onnx`: `e20c92f9233fccf29cddf86970d0d0161a03aebccc26d6f4d5639c4d5ec2e639`
- License: MIT, included beside the model files.

## Pre-release verification

- Ruff: pass.
- strict MyPy: pass for 139 Agent source files.
- Agent unit + production deployment contract tests: pass.
- Media Session: 102 tests pass.
- Module budgets and shell syntax: pass.
- DTLN local benchmark: 2 seconds of 16 kHz PCM processed in about 0.055 seconds
  on the development Mac CPU; output length preserved and reset deterministic.

## Production cutover

- GitHub CI run `32688703120`: pass.
- Agent and Voice Core Media Bridge image:
  `sha256:9e9a7b7e3adc93e2b53f12c43e43fdc228e88cf7ec37116e134c3a5b1c8f9ecf`.
- Both target containers are healthy with restart count 0; Bridge gRPC socket
  and Control readiness pass.
- Runtime stack authority remains `20260823-210222-voice-fix`; component OCI
  revision/version are the source commit and release tag above.
- Production model hashes match the pinned values. A 2-second 16 kHz PCM smoke
  completed in about 0.127 seconds (real-time factor 0.064), preserved byte
  length, and reproduced identical output after reset. Stage 1 is unavailable
  and disabled; Stage 2 DTLN is available and enabled.
- Automatic rollback points:
  `rollback-20260824-120459-dtln-stats-contract-fix-pre-agent` and `-pre-bridge`.
- A 100-frame production-container ingress probe processed all frames through
  DTLN, returned `stage2_available=true`, and crossed the former logging
  boundary without `KeyError`. The first pre-hotfix board session had already
  confirmed real session-policy admission and DTLN initialization.
- Failed same-day candidates and stale rollback tags were removed. Only the
  current release and runnable `20260824-115603-dtln-boot-chain-fix` rollback
  remain; evidence SHA-256 is
  `fa49d3cc7ac9f21710c9afdac058d6d1c1eb0b6f1e3226b9fd3fbf833813ec85`.

## Acceptance boundary

This release is `code=complete / wired=complete / enabled=true /
verified=production runtime + pre-hotfix real-session DTLN initialization`.
The Mac stopped enumerating the Espressif USB serial device before a post-hotfix
retry, so stable BOOT/WSS, ASR/TTS, playback terminal, Actual Heard, full
duplex, and T1-T14 remain separate pending gates.
