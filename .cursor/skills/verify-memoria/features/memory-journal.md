# Memory journal

Memory journal lets a signed-in user update their profile, append journal messages, list days, and generate a daily summary. In the miniprogram this backs the day list and summary on 「回顾」(`pages/memory`), profile edits on 「我的」(`pages/profile`), and companion selection on 「伙伴」(`pages/companion`). 「回顾」 also loads archive routes covered by [archive-timeline](./archive-timeline.md).

## Sub-features

- `memory-profile-read` loads the account profile.
- `memory-profile-write` updates display name / bio / timezone / auto_summary.
- `memory-companion-select` stores the chosen built-in companion as `companion_id`.
- `memory-message-create` appends a user or assistant message for today (idempotent on `client_message_id`).
- `memory-days-list` lists recent days, including days that have messages but no summary yet.
- `memory-day-summary` generates a daily summary for a date with messages.

## How to get to it (user POV)

- Open miniprogram tab 「我的」 and edit profile fields (calls `PUT /v1/memory/profile/{userId}`).
- Open tab 「伙伴」 and pick a companion (calls `PUT /v1/memory/profile/{userId}` with `companion_id`).
- Open tab 「回顾」 to list days (`GET /v1/memory/days`) and generate a summary (`POST /v1/memory/days/{date}/summary`). Guardian mode uses `/v1/guardian/minors/...` instead; not covered here.
- No miniprogram page or device path posts messages; `POST /v1/memory/messages` is a legacy dev/smoke write (503 in production). Verify uses it only to seed a day for the summary.

## Driving it with bin/api

Preconditions:

- `bin/doctor` PASS.
- Registered session in `evidence/<run>/session.env` (`TOKEN`, `USER_ID`) from [account-auth](./account-auth.md).
- Shell: `source evidence/<run>/session.env` and set `TODAY` to Asia/Shanghai calendar date (`python3 -c 'from datetime import datetime; from zoneinfo import ZoneInfo; print(datetime.now(ZoneInfo("Asia/Shanghai")).date())'`).

- **Read profile.** Run `bin/api --token-file evidence/<run>/session.env GET /v1/memory/profile/$USER_ID --evidence memory/profile-before.json`. HTTP 200; `user_id`=`$USER_ID`, `display_name`=`朋友`, `companion_id`=`null`.
- **Write profile.** Run `bin/api --token-file evidence/<run>/session.env PUT /v1/memory/profile/$USER_ID --evidence memory/profile-after.json -H 'Content-Type: application/json' -d '{"display_name":"验收小忆","bio":"verify-memoria","timezone":"Asia/Shanghai","auto_summary":true}'`. HTTP 200; `display_name`=`验收小忆`.
- **Select companion.** Run `bin/api --token-file evidence/<run>/session.env PUT /v1/memory/profile/$USER_ID --evidence memory/companion-select.json -H 'Content-Type: application/json' -d '{"companion_id":"taoxi"}'`. HTTP 200; `companion_id`=`taoxi`, `display_name` unchanged.
- **Empty days.** Run `bin/api --token-file evidence/<run>/session.env GET "/v1/memory/days?user_id=$USER_ID&limit=7" --evidence memory/days-before.json`. HTTP 200; `items`=`[]`.
- **Create message.** Set `CM=$(uuidgen | tr '[:upper:]' '[:lower:]')`, then run `bin/api --token-file evidence/<run>/session.env POST /v1/memory/messages --evidence memory/message.json -H 'Content-Type: application/json' -d "{\"user_id\":\"$USER_ID\",\"client_message_id\":\"$CM\",\"role\":\"user\",\"text\":\"今天完成了验证技能试跑。\",\"emotion\":\"happy\"}"`. HTTP 201; `role`=`user`; `local_date`=`$TODAY`. The identical call again returns 200 with the same `id`; the same `$CM` with different `text` returns 409.
- **List days.** Run `bin/api --token-file evidence/<run>/session.env GET "/v1/memory/days?user_id=$USER_ID&limit=7" --evidence memory/days-after-message.json`. HTTP 200; `items[0]` has `day`=`$TODAY`, `message_count`=`1`, `summary`=`null`.
- **Generate summary.** Run `bin/api --token-file evidence/<run>/session.env POST /v1/memory/days/$TODAY/summary --evidence memory/summary.json -H 'Content-Type: application/json' -d "{\"user_id\":\"$USER_ID\"}"`. HTTP 200; `message_count`=`1`; `summary` has `title`, `overview`, `highlights`, `mood`, `suggestion`; `source`=`fallback` (`bin/launch` blanks provider keys).
- **Proof.** Artifacts show `验收小忆`, `companion_id`=`taoxi`, the message, the day row before and after summary, and the summary body.

## Gotchas

- `user_id` in path/body/query must match the Bearer subject: mismatch is 403 `user identity does not match access token`; a malformed id is 422.
- `client_message_id` must be a 36-character UUID.
- Summary for a date without messages returns 404 `no messages found for this date`.
- `source` depends on provider keys, not `OFFLINE_MOCK`. If you launch outside `bin/launch` with `DASHSCOPE_API_KEY`/`DEEPSEEK_API_KEY` set, summaries call a real LLM.
- Under `ENVIRONMENT=production` message and summary writes return 503; verify always runs `development`.
