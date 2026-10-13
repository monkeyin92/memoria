# Archive timeline

Archive reads let an owner see retained memory evidence and reviewed conversation turns. Every archive read route is gated on the `conversation_review` subject capability, which only `adult` and `minor` (with guardian `memory_retention` consent) accounts have. A freshly registered username/password account has `subject_category=unknown`, so on a verify instance the observable user path is the gate itself: 401 without a token and 403 `subject_capability_forbidden` with one.

## Sub-features

- `archive-unauth` rejects a request with no Bearer token.
- `archive-gate` rejects an `unknown`-category account on both `timeline` and `conversation-review`.
- `archive-timeline-read` returns `{"items": [...]}` for an adult or consented minor (verified-unreachable on a verify instance; see Gotchas).

## How to get to it (user POV)

- Miniprogram 「回顾」 (`pages/memory`) loads `GET /v1/archive/conversation-review`, `GET /v1/archive/conversation-sessions`, and `GET /v1/archive/conversation-history`; 「首页」 (`pages/home`) loads `conversation-review`.
- `GET /v1/archive/timeline` is a control-plane read with the same gate; no miniprogram page calls it.

## Driving it with bin/api

Preconditions:

- `bin/doctor` PASS.
- Registered session in `evidence/<run>/session.env` from [account-auth](./account-auth.md).

- **Unauthenticated.** Run `bin/api GET "/v1/archive/timeline?limit=20" --evidence archive/timeline-unauth.json`. Exit 1; HTTP 401 with `detail`=`Bearer authentication required`.
- **Timeline gate.** Run `bin/api --token-file evidence/<run>/session.env GET "/v1/archive/timeline?limit=20" --evidence archive/timeline.json`. Exit 1; HTTP 403 with `detail`=`{"code":"subject_capability_forbidden","capability":"conversation_review"}`.
- **Review gate (miniprogram route).** Run `bin/api --token-file evidence/<run>/session.env GET /v1/archive/conversation-review --evidence archive/conversation-review.json`. Same 403 and code.
- **Proof.** `bin/api` saves evidence for non-2xx too; the three `*.meta.json` files carry 401/403/403.

## Gotchas

- The 200 path needs an adult subject profile. The only HTTP route to `adult` is WeChat phone binding during `POST /v1/auth/wechat-login`, which needs real WeChat credentials. Report `archive-timeline-read` as verified-unreachable with that prerequisite; do not promote the account by writing the SQLite store directly.
- `conversation-review` has the same capability gate as `timeline`, not a stricter one.
- Writing archive events needs internal/session tokens; out of scope here.
