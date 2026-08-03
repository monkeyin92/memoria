import { LiveKitCascadeTransport } from "./LiveKitCascadeTransport.js";
import { StreamCoreTransport } from "./StreamCoreTransport.js";
import { assertVoiceTransport } from "./VoiceTransport.js";

/**
 * Select a media transport from a server-owned runtime flag.
 *
 * The default remains LiveKit.  StreamCore is opt-in and must carry a
 * server-issued WHIP token; callers can use ``fallback`` to return to the
 * existing production transport when negotiation fails.
 */
export function createVoiceTransport({
  mediaRuntime = "livekit",
  liveKitOptions = {},
  streamCoreOptions = {},
  fallback = true,
  liveKitTransport = LiveKitCascadeTransport,
  streamCoreTransport = StreamCoreTransport,
} = {}) {
  const LiveKit = liveKitTransport;
  const StreamCore = streamCoreTransport;
  if (mediaRuntime === "streamcore") {
    const primary = assertVoiceTransport(new StreamCore(streamCoreOptions));
    if (!fallback) return primary;
    // Keep fallback construction lazy so enabling the experiment does not
    // allocate a LiveKit Room or touch credentials until it is needed.
    primary.createFallback = () =>
      assertVoiceTransport(new LiveKit(liveKitOptions));
    return primary;
  }
  return assertVoiceTransport(new LiveKit(liveKitOptions));
}
