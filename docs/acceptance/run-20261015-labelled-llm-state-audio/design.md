# Labelled online `llm_thinking` / `llm_unavailable` audio

Status. Architect Phase B synthesis for the product mandate that both fixed lines play online from AIGC-tagged peach-voice clips, not streaming TTS.

## Product requirement

- `llm_thinking` 「嗯，我想一想。」 and `llm_unavailable` (卡住了 line) play online from pre-recorded clips.
- Clips carry Appendix-E AIGC OpusTags (`CONTENT_ID`, `SERVICE_PROVIDER`, `AI_GENERATED`), voice `zh_female_tianmeitaozi_uranus_bigtts`.
- Keep thinking timing: 3 s after child speech end, at most once per turn, never after reply audio started.
- Tests prove no streaming TTS call for these two lines and that played audio has the AIGC label.

## Candidates

### A. `LabelledLineCatalogue` registry (chosen)

One load-once catalogue owns line id, text, AIGC tags, and 24 kHz PCM. Thinking filler frames PCM in-band. The provider short-circuits TTS when a phrase's spoken text is a labelled line. Shared OpusTags validation with the offline pack. Online assets live under `services/common/assets/labelled_lines/`; a render script can refresh offline 16 kHz pack copies.

Red flags. Deep module, single owner of AIGC validation, no new proto, timing gate unchanged.

### B. Device clip cue over the wire (rejected)

Server sends play-line id; firmware plays flash ogg. Splits "audio started", needs proto/edge/firmware, and still leaves `llm_unavailable` on the server TTS path.

### C. TTS provider cache only (rejected)

Caching synthesized PCM inside Doubao still calls streaming TTS on first miss and does not attach file-level AIGC labels to what is played.

## Synthesis

Candidate A. Data shape is a registry of frozen `LabelledLine` values. Call sites are `labelled_line_chunks(...)` from the thinking splice and `catalogue.by_text(phrase.spoken)` before any `push_text` / `synthesize`.
