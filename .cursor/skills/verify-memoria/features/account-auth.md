# Account auth

Account auth lets a user obtain a Memoria access token (anonymous or registered), inspect the current principal, and log in again with username/password. The miniprogram normally uses WeChat login; local verification uses anonymous + register/login, which exercise the same token and `/v1/auth/me` surface.

## Sub-features

- `auth-anonymous` issues an anonymous access token and user id.
- `auth-register` creates a registered account and returns a writable token.
- `auth-me` returns the current principal for a Bearer token.
- `auth-login` exchanges username/password for a new access token.

## How to get to it (user POV)

- Miniprogram opens `pages/auth/index` and completes WeChat login (production path; needs WeChat — not driven here).
- Local/API path: `POST /v1/auth/anonymous` for a guest session.
- Local/API path: `POST /v1/auth/register` then `POST /v1/auth/login` for a registered account.
- Any authenticated screen calls `GET /v1/auth/me` (or relies on the stored token).

## Driving it with bin/api

Preconditions:

- `bin/doctor` PASS on the active verify instance.
- Username `verify_auth_<suffix>` does not already exist (use a unique suffix).

- **Anonymous.** Create a guest session. Run `bin/api POST /v1/auth/anonymous --evidence auth/anonymous.json`. HTTP 200; body has `account_type`=`anonymous`, non-empty `user_id` and `access_token`.
- **Me (anonymous).** Persist `TOKEN`/`USER_ID` into `evidence/<run>/session-anon.env`, then run `bin/api --token-file evidence/<run>/session-anon.env GET /v1/auth/me --evidence auth/me-anonymous.json`. HTTP 200; `user_id` matches.
- **Register.** Run `bin/api POST /v1/auth/register --evidence auth/register.json -H 'Content-Type: application/json' -d '{"username":"verify_auth_<suffix>","password":"verify-pass-12345"}'`. HTTP 201; `account_type`=`registered` with `access_token`.
- **Me (registered).** Save token to `evidence/<run>/session.env`, then `bin/api --token-file evidence/<run>/session.env GET /v1/auth/me --evidence auth/me.json`. HTTP 200; `user_id` matches register.
- **Duplicate register.** Repeat the register call with `--evidence auth/register-dup.json`. Exit 1; HTTP 409.
- **Login.** Run `bin/api POST /v1/auth/login --evidence auth/login.json -H 'Content-Type: application/json' -d '{"username":"verify_auth_<suffix>","password":"verify-pass-12345"}'`. HTTP 200; same `user_id`, new `access_token` that works on `GET /v1/auth/me`.
- **Bad password.** Repeat login with a wrong password and `--evidence auth/login-bad.json`. Exit 1; HTTP 401.
- **Proof.** Keep `auth/*.json` and `*.meta.json`. Confirm `me.json` `user_id` equals `register.json` `user_id` (`account-…`) and `me-anonymous.json` reports `anon-…`.

## Gotchas

- Anonymous tokens can write (e.g. `PUT /v1/memory/profile/{anon-id}` returns 200); `require_writable_account` only blocks accounts being deleted. Use a registered account anyway so evidence matches a real user.
- Sending a guest's Bearer token to `/register` upgrades that guest in place and keeps its `anon-…` id. Register without a token when you want a fresh `account-…` id.
- Usernames are 2–32 chars of letters, digits, `._-`; passwords 8–128 chars. Duplicate username returns 409 — always pick a unique suffix.
- WeChat login (`POST /v1/auth/wechat-login`) needs real WeChat credentials and returns 428 `phone_authorization_required` until a phone code is sent; register/login is not proof of the WeChat entry point.
- Refresh cookies are set on responses; Bearer `access_token` is the stable agent handle.
