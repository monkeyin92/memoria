#pragma once

#define MEMORIA_ENCODE_FRAME_MS 20
#define MEMORIA_LISTENING_DRAW_MS 87
#define MEMORIA_ENCODE_SERVICE_MS 10
#define MEMORIA_ENCODE_QUEUE_DEPTH 8
#define MEMORIA_UPLINK_MASCOT_INTERVAL_MS 200
#define MEMORIA_UPLINK_SAMPLE_RATE 16000
#define MEMORIA_PCM_BYTES_PER_SAMPLE 2

#define MEMORIA_ENCODE_QUEUE_PCM_BYTES \
    (MEMORIA_ENCODE_QUEUE_DEPTH * (MEMORIA_UPLINK_SAMPLE_RATE * MEMORIA_ENCODE_FRAME_MS / 1000) * \
     MEMORIA_PCM_BYTES_PER_SAMPLE)

#ifdef __cplusplus
static_assert(MEMORIA_ENCODE_QUEUE_DEPTH * MEMORIA_ENCODE_FRAME_MS >= MEMORIA_LISTENING_DRAW_MS,
              "the encode queue must hold one listening draw");
static_assert(MEMORIA_UPLINK_MASCOT_INTERVAL_MS > MEMORIA_LISTENING_DRAW_MS,
              "the uplink cadence must leave a gap after the listening draw");
static_assert((MEMORIA_UPLINK_MASCOT_INTERVAL_MS - MEMORIA_LISTENING_DRAW_MS) >=
                  (MEMORIA_UPLINK_MASCOT_INTERVAL_MS / MEMORIA_ENCODE_FRAME_MS) *
                      MEMORIA_ENCODE_SERVICE_MS,
              "the gap must be long enough to encode the frames in one interval");
static_assert(MEMORIA_ENCODE_QUEUE_PCM_BYTES <= 8 * 1024, "encode queue PCM budget");
#endif
