# Device wake catalog

Device wake catalog lets the device settings UI load the selectable wake words and validate a custom wake word before saving it. Neither route needs a bound ESP32 or a login; the miniprogram sends its Bearer token anyway.

## Sub-features

- `wake-catalog-read` returns the hard-coded device-ready wake-word list.
- `wake-validate-custom` accepts a custom display + pinyin pair and returns its syllable count and warnings.
- `wake-validate-reject` rejects malformed bodies or invalid custom words with 422.

## How to get to it (user POV)

- Open miniprogram tab 「设备」 (`pages/device`); it loads `GET /v1/devices/wake-word-catalog`.
- Save a custom wake word on the same page; it calls `POST /v1/devices/wake-word/validate` with `wake_word_id`=`custom`, then saves device settings (binding required for that last step).

## Driving it with bin/api

Preconditions:

- `bin/doctor` PASS. A token is optional for these routes.

- **Load catalogue.** Run `bin/api GET /v1/devices/wake-word-catalog --evidence device/wake-catalog.json`. HTTP 200; `items` is non-empty, each item has `id`, `display`, `pinyin`, `syllables`, `device_ready`=`true`, `note`, and the ids include `mo_li`.
- **Validate custom.** Run `bin/api POST /v1/devices/wake-word/validate --evidence device/wake-validate.json -H 'Content-Type: application/json' -d '{"wake_word_id":"custom","wake_word_display":"你好小忆","wake_word_pinyin":"ni hao xiao yi"}'`. HTTP 200; `source`=`custom`, `syllables`=`4`, `warnings` has one entry.
- **Reject unknown field.** Run `bin/api POST /v1/devices/wake-word/validate --evidence device/wake-validate-bad.json -H 'Content-Type: application/json' -d '{"wake_word":"你好小忆"}'`. Exit 1; HTTP 422 with `detail[0].type`=`extra_forbidden`.
- **Proof.** Retain the three JSON files and their `*.meta.json`. `bin/api` saves evidence whatever the status.

## Gotchas

- The body fields are `wake_word_id`, `wake_word_display`, `wake_word_pinyin`; any other field is rejected. Omitting `wake_word_id` defaults to `mo_li`.
- Custom pinyin must be lowercase, space-separated, 2–6 syllables; display ≤ 16 characters. Validation failures are 422 with a string `detail`.
- Binding, runtime-profile, unbind, and firmware-release need a real device identity; do not fake them with internal tokens.
