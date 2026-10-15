# Test-device final ASR text log

## Flag

`MEMORIA_LOG_FINAL_ASR_TEXT` (agent). Default `false`.

When `true`, after a media turn commits, the agent writes one process log line:

`final_asr_text session=<id> text=<recognized>`

## Limits

- Log only. The text is not written to the database, archive objects, or other files.
- For test devices and lab reproduction only. Do not enable on production child devices.
- The server otherwise omits ASR text from structured logs on purpose.

## Use with the offline harness

`scripts/reproduce_safety_sentence_asr.py` synthesizes the nine product acceptance sentences with Doubao TTS and runs them through production FunASR. When live credentials are unavailable in the VM, enable this flag on a test device, speak or play 「我不想或了。」, and copy the `final_asr_text` line. Matcher changes for sentence 3 must use that measured text, not guessed characters.
