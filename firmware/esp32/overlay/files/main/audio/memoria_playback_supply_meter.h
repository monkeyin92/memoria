#ifndef MEMORIA_PLAYBACK_SUPPLY_METER_H
#define MEMORIA_PLAYBACK_SUPPLY_METER_H

/*
 * Memoria playback supply metering -- observation only, no playback authority.
 *
 * WHAT IS MEASURED
 *   The consumer side of the decoded playout queue (audio_playback_queue_).
 *   A "software supply wait" is one block of time in which the single consumer
 *   wanted the next decoded frame and the queue was empty.  The measurement
 *   stops at that queue boundary: it records that the consumer was waiting, not
 *   why it was waiting.
 *
 * WHAT IS NOT MEASURED
 *   The codec DMA ring fill is not observable here, so a counted wait is never
 *   reported as a proven I2S/DMA underrun.  Nothing here measures what the
 *   listener heard, and nothing here establishes the sender's or the bridge's
 *   pacing as the cause.  The only hardware-backed playback evidence on this
 *   board stays the exact TX-EOF completion watermark.  Its completion events
 *   are counted separately as exact_confirmed; that field is not a count of
 *   frames or audible output.
 *
 * WHY THE PREVIOUS COUNTER WAS WRONG (2026-09-14 session 4)
 *   The old counter sampled its streaming flag *after* the wait returned, so
 *   it billed the wait before the first frame of a reply (1377 ms) and the
 *   idle gap between two replies (5857 ms, 3423 ms) to the new generation.
 *   Therefore:
 *     - a wait is classified from the state observed when it *began*;
 *     - it is billed as supply only when that same generation really delivered
 *       the frame it was waiting for, so an inter-generation gap can never be
 *       billed as that generation's supply wait;
 *     - a wait that crosses a generation switch, a flush, a cancel, a reset or
 *       a stop is never billed as supply: the part that elapsed before the
 *       boundary is booked to the generation that owned it, and the rest
 *       is re-bound without a generation so a later resolution can only land
 *       in prestart/outside if an episode is open when the wait resolves;
 *       with no open episode, the unowned remainder is not recorded;
 *     - a decoder reset retaining the accepted generation rebinds the open
 *       episode to the new playback fence; it cannot create an episode, and
 *       waits become countable again only after a fresh output submission;
 *     - a generation's episode is summarised exactly once, so the last
 *       generation and a generation with no waits at all stay observable;
 *     - generation 0 (a local cue) never opens an episode.
 *
 * THREADING
 *   Every method is called with the AudioService audio queue mutex held: the
 *   meter shares the mutex of the queue it observes and takes no lock of its
 *   own.  No clock lives here; callers pass millisecond timestamps so the same
 *   state machine runs on the host under firmware/esp32/tests.
 *   The one exception in time, not in locking, is a codec submission: the
 *   consumer releases the lock to call the codec, so it freezes the episode
 *   with CaptureOutputToken while the lock is still held and passes that token
 *   to NoteOutputSubmitted afterwards.  A token names one episode, never a
 *   generation number, because a flush can reuse a generation id.
 */

#include <cstddef>
#include <cstdint>
#include <cstdio>

namespace memoria {

/* Why an episode ended.  The audio layer cannot separate a normal end of
 * stream from a cancel: both arrive as a flush that clears the accepted
 * generation, so it reports one honest channel_flush instead of guessing. */
enum class SupplyCloseReason : uint8_t {
    kNone = 0,
    kGenerationSwitch,
    kChannelFlush,
    kDecoderReset,
    kServiceStop,
};

/* A token frozen while an output task is still under audio_queue_mutex_.  The
 * episode id is intentionally independent of generation: a flush may reuse
 * the same generation number, but it always opens a new episode id. */
struct PlaybackSupplyEpisodeToken {
    bool valid = false;
    uint64_t episode_id = 0;
    uint32_t generation = 0;
    uint32_t playback_fence = 0;
};

inline const char* SupplyCloseReasonName(SupplyCloseReason reason) {
    switch (reason) {
        case SupplyCloseReason::kGenerationSwitch:
            return "generation_switch";
        case SupplyCloseReason::kChannelFlush:
            return "channel_flush";
        case SupplyCloseReason::kDecoderReset:
            return "decoder_reset";
        case SupplyCloseReason::kServiceStop:
            return "service_stop";
        default:
            return "none";
    }
}

struct SupplyWaitStats {
    uint32_t count = 0;
    int64_t total_ms = 0;
    int64_t max_ms = 0;

    void Add(int64_t wait_ms) {
        if (wait_ms < 0) {
            wait_ms = 0;
        }
        ++count;
        total_ms += wait_ms;
        if (wait_ms > max_ms) {
            max_ms = wait_ms;
        }
    }

    void Clear() {
        count = 0;
        total_ms = 0;
        max_ms = 0;
    }
};

/* One generation's episode.  `supply` is the only bucket that counts software
 * supply waits; every other bucket exists so the excluded waits stay
 * auditable. */
struct SupplyEpisodeSummary {
    bool valid = false;
    uint32_t generation = 0;
    SupplyCloseReason reason = SupplyCloseReason::kNone;
    /* Frames of this generation committed to the codec output. */
    uint32_t output_frames = 0;
    bool first_output_submitted = false;
    /* Codec-submission latency: generation announcement -> first frame
     * committed to the codec.  -1 when no announcement opened the episode.
     * This is a submission-layer observation, not an audible or DAC-level
     * "first sound". */
    int64_t first_output_latency_ms = -1;
    /* Counted software supply waits: the measurement. */
    SupplyWaitStats supply;
    /* Wait that ended before this generation submitted its first codec frame.
     * Holds the reply-open wait and the idle gap between generations, i.e.
     * exactly the waits the old counter mis-billed. */
    SupplyWaitStats prestart;
    /* Wait that began while this generation was playing but came back with a
     * frame of another generation.  Guard only: every generation switch in
     * this firmware flushes first, so a close normally claims these. */
    SupplyWaitStats boundary;
    /* Portion of a wait that elapsed while this generation was the active one
     * before a close or same-generation decoder reset cut the wait. */
    SupplyWaitStats close_dropped;
    /* Wait this generation cannot account for: begun before it was announced,
     * after it was closed, or released by a service stop. */
    SupplyWaitStats outside;
    /* Exact TX-EOF completion observations; none of them is a supply wait.
     * exact_polls counts poll-loop iterations, exact_confirmed counts armed
     * outputs whose TX-EOF watermark completed, exact_timeouts counts armed
     * outputs that gave up waiting.  exact_confirmed is a completion-watermark
     * event count, not a count of frames and not audible output. */
    uint32_t exact_polls = 0;
    uint32_t exact_confirmed = 0;
    uint32_t exact_timeouts = 0;
};

/* Emitted for a counted supply wait that reached the reporting threshold. */
struct SupplyWaitRecord {
    bool valid = false;
    uint32_t generation = 0;
    int64_t wait_ms = 0;
    uint32_t supply_waits = 0;  // Cumulative count in this episode, not a log sequence.
};

/* Log-line capacities.  The two formats below are fixed, and the summary has a
 * worst case of 636 characters (every counter at its maximum, every duration at
 * its minimum), so a 512-byte buffer silently dropped the summary of the busiest
 * and of the final generation.  These constants are the single source of truth
 * for the firmware buffer and for the host test that fills every field to its
 * maximum. */
inline constexpr size_t kSupplyWaitLogCapacity = 192;
inline constexpr size_t kSupplySummaryLogCapacity = 768;

class PlaybackSupplyMeter {
  public:
    /* Forget everything, including a wait in flight. */
    void Reset() {
        episode_open_ = false;
        generation_ = 0;
        playing_ = false;
        announced_ = false;
        announced_ms_ = 0;
        first_output_submitted_ = false;
        first_output_ms_ = 0;
        output_frames_ = 0;
        supply_.Clear();
        prestart_.Clear();
        boundary_.Clear();
        close_dropped_.Clear();
        outside_.Clear();
        exact_polls_ = 0;
        exact_confirmed_ = 0;
        exact_timeouts_ = 0;
        playback_fence_ = 0;
        episode_id_ = 0;
        exact_output_token_ = PlaybackSupplyEpisodeToken();
        waiting_ = false;
        wait_countable_ = false;
        wait_exact_poll_ = false;
        wait_generation_ = 0;
        wait_begin_ms_ = 0;
        wait_token_ = PlaybackSupplyEpisodeToken();
    }

    /* A generation became the accepted downlink generation
     * (generation.started, playback.flush, pause/resume, profile re-apply).
     * Closes the previous episode, if any, as a generation switch and opens a
     * fresh episode for this announcement.
     *
     * One generation number can own more than one episode: a pause/resume or a
     * profile re-apply re-announces the same generation id, and an id can be
     * reused after a flush.  Episodes are therefore identified by an internal
     * id, never by the generation number, and the log is chronological.
     * playback_fence is the caller's current playback_generation_ revision,
     * read under the same queue lock. */
    SupplyEpisodeSummary NoteGenerationAnnounced(uint32_t generation,
                                                 uint32_t playback_fence,
                                                 int64_t now_ms) {
        if (generation == 0) {
            return Close(SupplyCloseReason::kChannelFlush, now_ms);
        }
        const SupplyEpisodeSummary closed =
            Close(SupplyCloseReason::kGenerationSwitch, now_ms);
        OpenEpisode(generation, playback_fence, now_ms, true);
        return closed;
    }

    /* ResetDecoderLocked keeps the accepted generation but advances its local
     * fence, including at speaking entry BEFORE the first output.  Keep the
     * already announced episode and its original first-submission latency;
     * closing here would silently discard the entire reply's observations.
     * Cut any pending wait and invalidate pre-reset output/exact tokens.  No
     * fresh announcement is inferred: zero, a mismatch or a closed episode
     * cannot revive playback accounting after cancel/flush/stop.  The caller
     * must pass the newly advanced fence under the same audio queue lock. */
    SupplyEpisodeSummary NoteDecoderReset(uint32_t generation, uint32_t playback_fence,
                                          int64_t now_ms) {
        if (!episode_open_ || generation == 0 || generation != generation_ ||
            playback_fence != static_cast<uint32_t>(playback_fence_ + 1u)) {
            return Close(SupplyCloseReason::kDecoderReset, now_ms);
        }
        CutWait(now_ms);
        playback_fence_ = playback_fence;
        playing_ = false;
        exact_output_token_ = PlaybackSupplyEpisodeToken();
        return SupplyEpisodeSummary();
    }

    /* Decode-dequeue observation.  The codec task has just taken a packet out
     * of the decode queue and has not decoded it yet.  This observation runs
     * under the queue lock, but the decode runs after releasing it and may be
     * overtaken by a flush.  This only reports whether the current episode
     * still matches, and never opens, closes or switches an episode.  It says
     * nothing about the playout queue, which the codec task has not reached. */
    PlaybackSupplyEpisodeToken NoteDecodeDequeued(uint32_t generation,
                                                   uint32_t playback_fence) const {
        return CurrentToken(generation, playback_fence);
    }

    /* Freeze the episode that owns a frame the consumer just dequeued.  The
     * token stays valid only for that exact episode: a flush ends it and opens
     * a new one, possibly with the same generation id, and the frozen token
     * stops matching. */
    PlaybackSupplyEpisodeToken CaptureOutputToken(uint32_t generation,
                                                   uint32_t playback_fence) const {
        return CurrentToken(generation, playback_fence);
    }

    /* The consumer committed a frame to the codec output.  The token was frozen
     * under the queue lock when that frame was dequeued; the codec submission
     * itself happens outside the lock, so a late task whose episode has since
     * closed is observation only.  It can neither revive the closed episode nor
     * close or reopen the episode that replaced it, so a late submission never
     * produces a duplicate summary or pollutes a newer generation. */
    SupplyEpisodeSummary NoteOutputSubmitted(const PlaybackSupplyEpisodeToken& token,
                                              int64_t now_ms) {
        if (!IsCurrentToken(token)) {
            return SupplyEpisodeSummary();
        }
        ++output_frames_;
        if (!first_output_submitted_) {
            first_output_submitted_ = true;
            first_output_ms_ = now_ms;
        }
        /* From here on the generation has really reached the codec, so a queue
         * wait that begins now is eligible to be billed as supply. */
        playing_ = true;
        return SupplyEpisodeSummary();
    }

    /* Remember which output armed the exact TX-EOF watermark, so its
     * completion observation is attributed to that output's episode only. */
    void NoteExactOutputArmed(const PlaybackSupplyEpisodeToken& token) {
        exact_output_token_ = token;
    }

    /* The consumer is about to block on the empty playout queue. */
    void NoteWaitBegin(int64_t now_ms) {
        waiting_ = true;
        wait_exact_poll_ = false;
        wait_begin_ms_ = now_ms;
        wait_token_ = CurrentToken();
        wait_countable_ = episode_open_ && playing_;
        wait_generation_ = episode_open_ ? generation_ : 0;
    }

    /* One iteration of the 5 ms DMA-completion poll loop.  It is never a
     * software supply wait, and a poll whose armed output belongs to an episode
     * that has since closed is not counted against the replacement episode. */
    void NoteExactWaitBegin(int64_t now_ms) {
        waiting_ = true;
        wait_exact_poll_ = true;
        wait_begin_ms_ = now_ms;
        wait_countable_ = false;
        wait_generation_ = 0;
        wait_token_ = exact_output_token_;
        if (IsCurrentToken(exact_output_token_)) {
            ++exact_polls_;
        }
    }

    /* The blocking call returned.  `frame_available` is false when only
     * service_stopped_ released it; `frame_generation` is the generation of
     * the frame now at the head of the queue. */
    SupplyWaitRecord NoteWaitEnd(bool frame_available, uint32_t frame_generation,
                                 int64_t now_ms) {
        SupplyWaitRecord record;
        if (!waiting_) {
            return record;
        }
        const bool poll = wait_exact_poll_;
        const bool countable = wait_countable_;
        const uint32_t bound_generation = wait_generation_;
        const PlaybackSupplyEpisodeToken bound_token = wait_token_;
        const int64_t wait_ms = now_ms - wait_begin_ms_;
        waiting_ = false;
        wait_countable_ = false;
        wait_exact_poll_ = false;
        wait_generation_ = 0;
        wait_token_ = PlaybackSupplyEpisodeToken();
        if (poll) {
            /* Counted as a poll, never as supply. */
            return record;
        }
        if (countable && IsCurrentToken(bound_token) && frame_available &&
            frame_generation == bound_generation) {
            supply_.Add(wait_ms);
            record.valid = true;
            record.generation = bound_generation;
            record.wait_ms = wait_ms;
            record.supply_waits = supply_.count;
            return record;
        }
        if (!episode_open_) {
            /* Nothing to bill: a stop or a close already reported the part of
             * this wait that belonged to a generation. */
            return record;
        }
        if (countable) {
            if (frame_available) {
                boundary_.Add(wait_ms);
            } else {
                outside_.Add(wait_ms);
            }
        } else if (announced_ && !playing_ && frame_available &&
                   frame_generation == generation_) {
            prestart_.Add(wait_ms);
        } else {
            outside_.Add(wait_ms);
        }
        return record;
    }

    /* The armed TX-EOF watermark completed.  A stale armed output cannot credit
     * the episode that replaced it. */
    void NoteExactCompletionConfirmed() {
        if (IsCurrentToken(exact_output_token_)) {
            ++exact_confirmed_;
        }
    }

    void NoteExactCompletionTimeout() {
        if (IsCurrentToken(exact_output_token_)) {
            ++exact_timeouts_;
        }
    }

    /* Close the open episode.  An invalid summary is returned when none is
     * open, so a repeated close never emits a second summary. */
    SupplyEpisodeSummary Close(SupplyCloseReason reason, int64_t now_ms) {
        SupplyEpisodeSummary summary;
        /* An armed watermark must never be claimed by whatever episode comes
         * next. */
        exact_output_token_ = PlaybackSupplyEpisodeToken();
        if (!episode_open_) {
            /* Still cut a wait in flight: it belongs to no episode and must not
             * leak into whatever generation comes next. */
            CutWait(now_ms);
            return summary;
        }
        CutWait(now_ms);
        summary.valid = true;
        summary.generation = generation_;
        summary.reason = reason;
        summary.output_frames = output_frames_;
        summary.first_output_submitted = first_output_submitted_;
        summary.first_output_latency_ms =
            (announced_ && first_output_submitted_) ? (first_output_ms_ - announced_ms_) : -1;
        summary.supply = supply_;
        summary.prestart = prestart_;
        summary.boundary = boundary_;
        summary.close_dropped = close_dropped_;
        summary.outside = outside_;
        summary.exact_polls = exact_polls_;
        summary.exact_confirmed = exact_confirmed_;
        summary.exact_timeouts = exact_timeouts_;
        episode_open_ = false;
        generation_ = 0;
        playback_fence_ = 0;
        episode_id_ = 0;
        playing_ = false;
        announced_ = false;
        announced_ms_ = 0;
        first_output_submitted_ = false;
        first_output_ms_ = 0;
        output_frames_ = 0;
        return summary;
    }

    bool has_episode() const { return episode_open_; }
    uint32_t episode_generation() const { return generation_; }
    uint64_t episode_id() const { return episode_id_; }
    uint32_t playback_fence() const { return playback_fence_; }
    bool episode_playing() const { return playing_; }
    bool wait_in_flight() const { return waiting_; }
    /* Observability helper: does this frozen token still name the open
     * episode? */
    bool token_is_current(const PlaybackSupplyEpisodeToken& token) const {
        return IsCurrentToken(token);
    }

  private:
    void OpenEpisode(uint32_t generation, uint32_t playback_fence, int64_t now_ms,
                     bool announced) {
        episode_open_ = true;
        generation_ = generation;
        playback_fence_ = playback_fence;
        /* A fresh id on every open: generation ids repeat across a flush, a
         * pause/resume and a profile re-apply, so only this id identifies the
         * episode a token belongs to. */
        episode_id_ = ++next_episode_id_;
        playing_ = false;
        announced_ = announced;
        announced_ms_ = now_ms;
        first_output_submitted_ = false;
        first_output_ms_ = 0;
        output_frames_ = 0;
        supply_.Clear();
        prestart_.Clear();
        boundary_.Clear();
        close_dropped_.Clear();
        outside_.Clear();
        exact_polls_ = 0;
        exact_confirmed_ = 0;
        exact_timeouts_ = 0;
        exact_output_token_ = PlaybackSupplyEpisodeToken();
    }

    PlaybackSupplyEpisodeToken CurrentToken() const {
        if (!episode_open_) {
            return PlaybackSupplyEpisodeToken();
        }
        return PlaybackSupplyEpisodeToken{true, episode_id_, generation_, playback_fence_};
    }

    /* The token of the current episode, or an invalid token when the caller's
     * generation and fence no longer describe it. */
    PlaybackSupplyEpisodeToken CurrentToken(uint32_t generation,
                                            uint32_t playback_fence) const {
        if (!episode_open_ || generation_ != generation || playback_fence_ != playback_fence) {
            return PlaybackSupplyEpisodeToken();
        }
        return CurrentToken();
    }

    bool IsCurrentToken(const PlaybackSupplyEpisodeToken& token) const {
        return token.valid && episode_open_ && token.episode_id == episode_id_ &&
            token.generation == generation_ && token.playback_fence == playback_fence_;
    }

    /* A wait in flight at a close or same-generation decoder reset is cut: the
     * part before the boundary is booked as close_dropped and the rest re-bound
     * with no generation, so the next generation can never inherit the previous
     * wait as its own supply wait.  Only a wait that began inside the closing
     * episode is booked to it; a wait that began before the episode existed
     * keeps its original start, so a reply-to-reply gap stays visible as the
     * next generation's pre-start idle.  If no episode is open when the wait
     * resolves, its unowned remainder is not recorded. */
    void CutWait(int64_t now_ms) {
        if (!waiting_) {
            return;
        }
        const bool poll = wait_exact_poll_;
        const bool bound = !poll && wait_countable_ && IsCurrentToken(wait_token_);
        if (bound) {
            close_dropped_.Add(now_ms - wait_begin_ms_);
            wait_begin_ms_ = now_ms;
        }
        wait_countable_ = false;
        wait_exact_poll_ = poll;
        wait_generation_ = 0;
        wait_token_ = PlaybackSupplyEpisodeToken();
    }

    bool episode_open_ = false;
    uint32_t generation_ = 0;
    uint32_t playback_fence_ = 0;
    uint64_t episode_id_ = 0;
    /* Monotonic across resets, so an episode id is never reused and a token
     * from an earlier audio session can never match a later episode. */
    uint64_t next_episode_id_ = 0;
    bool playing_ = false;
    bool announced_ = false;
    int64_t announced_ms_ = 0;
    bool first_output_submitted_ = false;
    int64_t first_output_ms_ = 0;
    uint32_t output_frames_ = 0;
    SupplyWaitStats supply_;
    SupplyWaitStats prestart_;
    SupplyWaitStats boundary_;
    SupplyWaitStats close_dropped_;
    SupplyWaitStats outside_;
    uint32_t exact_polls_ = 0;
    uint32_t exact_confirmed_ = 0;
    uint32_t exact_timeouts_ = 0;

    bool waiting_ = false;
    bool wait_countable_ = false;
    bool wait_exact_poll_ = false;
    uint32_t wait_generation_ = 0;
    PlaybackSupplyEpisodeToken wait_token_;
    int64_t wait_begin_ms_ = 0;
    PlaybackSupplyEpisodeToken exact_output_token_;
};

/* Renders the per-wait line body without the log level and tag.  Returns the
 * number of characters written, or 0 when there is nothing to render. */
inline int FormatSupplyWaitLog(char* out, size_t capacity, const SupplyWaitRecord& record) {
    if (out == nullptr || capacity == 0 || !record.valid) {
        return 0;
    }
    const int written = std::snprintf(
        out, capacity,
        "media playback supply wait layer=playout_queue scope=software_queue_wait "
        "generation=%u wait_ms=%lld supply_waits=%u",
        static_cast<unsigned int>(record.generation),
        static_cast<long long>(record.wait_ms),
        static_cast<unsigned int>(record.supply_waits));
    if (written < 0 || static_cast<size_t>(written) >= capacity) {
        return 0;
    }
    return written;
}

/* Renders the per-generation summary body without the log level and tag. */
inline int FormatSupplySummaryLog(char* out, size_t capacity,
                                  const SupplyEpisodeSummary& summary) {
    if (out == nullptr || capacity == 0 || !summary.valid) {
        return 0;
    }
    const int written = std::snprintf(
        out, capacity,
        "media playback supply summary layer=playout_queue scope=software_queue_wait "
        "generation=%u close=%s output_frames=%u first_output=%s "
        "first_output_latency_ms=%lld supply_waits=%u supply_max_ms=%lld "
        "supply_total_ms=%lld prestart_waits=%u prestart_max_ms=%lld "
        "boundary_waits=%u boundary_max_ms=%lld close_dropped_waits=%u "
        "close_dropped_ms=%lld outside_waits=%u outside_max_ms=%lld "
        "exact_confirmed=%u exact_timeouts=%u exact_polls=%u",
        static_cast<unsigned int>(summary.generation),
        SupplyCloseReasonName(summary.reason),
        static_cast<unsigned int>(summary.output_frames),
        summary.first_output_submitted ? "yes" : "no",
        static_cast<long long>(summary.first_output_latency_ms),
        static_cast<unsigned int>(summary.supply.count),
        static_cast<long long>(summary.supply.max_ms),
        static_cast<long long>(summary.supply.total_ms),
        static_cast<unsigned int>(summary.prestart.count),
        static_cast<long long>(summary.prestart.max_ms),
        static_cast<unsigned int>(summary.boundary.count),
        static_cast<long long>(summary.boundary.max_ms),
        static_cast<unsigned int>(summary.close_dropped.count),
        static_cast<long long>(summary.close_dropped.max_ms),
        static_cast<unsigned int>(summary.outside.count),
        static_cast<long long>(summary.outside.max_ms),
        static_cast<unsigned int>(summary.exact_confirmed),
        static_cast<unsigned int>(summary.exact_timeouts),
        static_cast<unsigned int>(summary.exact_polls));
    if (written < 0 || static_cast<size_t>(written) >= capacity) {
        return 0;
    }
    return written;
}

}  // namespace memoria

#endif
