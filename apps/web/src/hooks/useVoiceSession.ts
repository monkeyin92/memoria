import { useCallback, useState } from "react";
import type { TranscriptionSegment } from "livekit-client";

import { createSession, notifyRtcRecovered, stopResponse } from "../api/controlApi";
import { useSessionStore } from "../state/sessionStore";
import { parseLiveKitDataEvent, type UiState } from "../types/events";

export function useVoiceSession() {
  const session = useSessionStore((state) => state.session);
  const uiState = useSessionStore((state) => state.uiState);
  const micEnabled = useSessionStore((state) => state.micEnabled);
  const transcripts = useSessionStore((state) => state.transcripts);
  const error = useSessionStore((state) => state.error);
  const setUiState = useSessionStore((state) => state.setUiState);
  const setSession = useSessionStore((state) => state.setSession);
  const setError = useSessionStore((state) => state.setError);
  const setMicEnabled = useSessionStore((state) => state.setMicEnabled);
  const advanceGeneration = useSessionStore((state) => state.advanceGeneration);
  const endSession = useSessionStore((state) => state.endSession);
  const [connecting, setConnecting] = useState(false);

  const start = useCallback(async () => {
    setConnecting(true);
    setUiState("connecting");
    setError(null);
    try {
      const created = await createSession({
        user_id: "web-user",
        locale: "zh-CN",
        client: {
          platform: "web",
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai",
        },
      });
      setSession(created);
      return created;
    } catch (e) {
      setError(e instanceof Error ? e.message : "连接失败");
      setUiState("closed");
      return null;
    } finally {
      setConnecting(false);
    }
  }, [setError, setSession, setUiState]);

  const stopAssistant = useCallback(async () => {
    const sessionId = session?.session_id;
    if (!sessionId) return;
    try {
      await stopResponse(sessionId);
      // The server publishes the authoritative heard transcript after cancel.
      // Never invent a local truncation point here.
    } catch (e) {
      setError(e instanceof Error ? e.message : "停止回答失败");
    }
  }, [session?.session_id, setError]);

  const handleData = useCallback((payload: Uint8Array) => {
    const event = parseLiveKitDataEvent(payload);
    if (!event) return;
    const state = useSessionStore.getState();
    if (event.type === "assistant_state") {
      if (event.session_id !== state.session?.session_id) return;
      state.applyAssistantState(event.generation_id, event.state, event.turn_id);
      return;
    }
    state.applyTranscript({
      speaker: event.speaker,
      text: event.text,
      final: event.final,
      heard: event.heard,
      turn_id: event.turn_id,
      generation_id: event.generation_id,
    });
  }, []);

  const handleSynchronizedTranscript = useCallback((segments: TranscriptionSegment[]) => {
    useSessionStore.getState().applySynchronizedAssistantTranscript(segments);
  }, []);

  const handleConnectionState = useCallback(
    (state: UiState, message?: string) => {
      setUiState(state);
      setError(message ?? null);
    },
    [setError, setUiState],
  );

  const handleReconnected = useCallback(async (): Promise<boolean> => {
    const sessionId = useSessionStore.getState().session?.session_id;
    if (!sessionId) return false;
    try {
      await notifyRtcRecovered(sessionId);
      advanceGeneration();
      return true;
    } catch (error) {
      setError(error instanceof Error ? error.message : "重连同步失败");
      return false;
    }
  }, [advanceGeneration, setError]);

  return {
    connecting,
    start,
    stopAssistant,
    endSession,
    handleData,
    handleSynchronizedTranscript,
    handleConnectionState,
    handleReconnected,
    session,
    uiState,
    micEnabled,
    setMicEnabled,
    transcripts,
    error,
  };
}
