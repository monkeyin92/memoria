#pragma once

// One uplink Opus frame is 20 ms. The encode queue used to hold two of them.
// A drawn listening frame on this panel took 87 ms
// (docs/strategy/mmp2-patch-rect-design.md, 2026-10-07). opus_codec is already
// priority 5, so dropping mascot_anim from priority 2 to 1 does not return
// that time to the encoder. Hold the frames that arrive during those 87 ms,
// then leave a gap that can encode a full interval at 10 ms a frame. 10 ms is
// slower than the 0.34 core measured for opus_codec in that same note.

#define MEMORIA_ENCODE_FRAME_MS 20
#define MEMORIA_LISTENING_FRAME_MS 87
#define MEMORIA_ENCODE_SERVICE_MS 10
#define MEMORIA_ENCODE_QUEUE_DEPTH 8
#define MEMORIA_UPLINK_MASCOT_INTERVAL_MS 200

// 8 frames of 16 kHz mono PCM is 5120 bytes, and it stays under 8 KB.
#define MEMORIA_ENCODE_QUEUE_PCM_BYTES \
    (MEMORIA_ENCODE_QUEUE_DEPTH * (16000 * MEMORIA_ENCODE_FRAME_MS / 1000) * 2)

#ifdef __cplusplus
static_assert(MEMORIA_ENCODE_QUEUE_DEPTH * MEMORIA_ENCODE_FRAME_MS >= MEMORIA_LISTENING_FRAME_MS,
              "the encode queue must hold one listening frame");
static_assert(MEMORIA_UPLINK_MASCOT_INTERVAL_MS > MEMORIA_LISTENING_FRAME_MS,
              "the uplink cadence must leave a gap after the listening frame");
static_assert((MEMORIA_UPLINK_MASCOT_INTERVAL_MS - MEMORIA_LISTENING_FRAME_MS) >=
                  (MEMORIA_UPLINK_MASCOT_INTERVAL_MS / MEMORIA_ENCODE_FRAME_MS) *
                      MEMORIA_ENCODE_SERVICE_MS,
              "the gap must be long enough to encode the frames in one interval");
static_assert(MEMORIA_ENCODE_QUEUE_PCM_BYTES <= 8 * 1024, "encode queue PCM budget");
#endif
