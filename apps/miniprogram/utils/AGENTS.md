# apps/miniprogram/utils/ — Domain Logic + API Client

## OVERVIEW
Client domain layer: HTTP API, device binding contract, BLE onboarding, consent, child memory, crisis alerts. Earned file: score 14 (44 js files, >10 exports, reference centrality: api.js 36, device-binding.js 18, nav-inset.js 19).

## WHERE TO LOOK
| Area | File | Notes |
|------|------|-------|
| API client | api.js (1558 L) | ~100 exports; auth-epoch + context-keyed guards |
| Binding contract | device-binding.js (1683 L) | fail-closed validators, capability matrix, runtime-profile cache |
| Onboarding | device-onboarding/ (13 js + vendor nacl) | BLE + Protocomm + crypto; index.js is the barrel |
| Child memory | child-memory.js | view-model builders; never normalizes transcripts |
| Crisis alerts | crisis-alerts.js | BIND_SUBSCRIBE_* constants, parseSafetyAlert |
| Consent | child-consent.js, legal-release.js | consentStamp; consent doc versions |
| Nav/auth | nav-inset.js, auth-gate.js, nav-back.js, tab-routes.js | required by nearly every page |
| Contracts seam | multi-subject-contracts.js | re-exports ../generated/ |
| Small helpers | storage.js, guest-access.js, subject-label.js, companions.js, quiet-hours.js, session-limits.js | page-agnostic |

## CONVENTIONS
- `module.exports = { ... }` named-object style; `"use strict"` in device-onboarding/ only.
- Copy strings and enum labels are frozen lookup tables (Object.freeze) defined here, consumed by pages.
- Storage keys namespaced `memoria:miniprogram:<domain>[:<sub>]` (auth, device:binding-manifest, onboarding_session_id).
- Async loads guarded by authEpoch / flowSeq; response normalization fail-closed (unknown enums → safe values; extra fields rejected).
- Time: accept ISO 8601 with Z or explicit offset only; display fixed to Beijing (formatBeijingTime).
- device-onboarding: contracts.js state enums; sensitive-buffer.js values never touch storage or logs.

## ANTI-PATTERNS
- buildBindingRequest must throw before send on fake-authorization fields (policy_version, *_accepted, authorized); consent never submitted on the parent's behalf.
- Never normalize raw transcripts/audio into view models (child-memory.js, api.js memory-day kinds).
- Onboarding: never store or log Wi-Fi/provisioning passwords; never rely on the platform BLE service filter; empty/stale value must not terminate a Protocomm read.
- Never surface a foreign device's QR query (qr-code.js).
- Pages must not call utils/api directly in gate-relevant paths — route through policy seams (configActionGate).
- Deprecated contracts: requireNewProducerContract throws for new producers (multi-subject-contracts.js).
