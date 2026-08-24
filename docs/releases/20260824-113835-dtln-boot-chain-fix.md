# Release 20260824-113835-dtln-boot-chain-fix

**Date**: 2026-08-24
**Type**: Agent Component (Agent + Voice Core Media Bridge)
**Base**: `20260823-210222-voice-fix`

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
  images, including a running container whose original image record was
  already pruned.
- Remove stale tests and parallel documents that claimed the previous
  listening/denoising release was deployed successfully.

## Model provenance

- Upstream: `https://github.com/breizhn/DTLN`
- Commit: `1de1f15a8b5b7e1c44905618ff2ef70ca8277fbc`
- `model_1.onnx`: `22b91cae3855e5a0620e66a917ca6c82c58db0e842c770f58d86751c5e8d4ae3`
- `model_2.onnx`: `e20c92f9233fccf29cddf86970d0d0161a03aebccc26d6f4d5639c4d5ec2e639`
- License: MIT, included beside the model files.

## Pre-release verification

- Ruff: pass.
- strict MyPy: pass for 140 Agent source files.
- Agent unit + production deployment contract tests: pass.
- Media Session: 102 tests pass.
- Module budgets and shell syntax: pass.
- DTLN local benchmark: 2 seconds of 16 kHz PCM processed in about 0.055 seconds
  on the development Mac CPU; output length preserved and reset deterministic.

## Acceptance boundary

Before cutover this release is `code=complete / wired=complete /
enabled=false / verified=local`. Production health, model load, BOOT/WSS,
ASR/TTS, playback terminal and Actual Heard require separate evidence.
