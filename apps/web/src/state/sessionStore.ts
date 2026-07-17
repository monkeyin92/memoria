import { create } from "zustand";
import type { TranscriptionSegment } from "livekit-client";

import type { CreateSessionResponse } from "../api/controlApi";
import type { UiState } from "../types/events";
import { shouldAcceptGeneration } from "../types/events";

export type TranscriptLine = {
  speaker: "user" | "assistant";
  text: string;
  final: boolean;
  turn_id: number;
  generation_id: number;
  heard?: boolean;
};

type SessionState = {
  uiState: UiState;
  session: CreateSessionResponse | null;
  generationId: number;
  currentTurnId: number;
  awaitingServerGeneration: boolean;
  micEnabled: boolean;
  transcripts: TranscriptLine[];
  error: string | null;
  setUiState: (s: UiState) => void;
  setSession: (s: CreateSessionResponse) => void;
  setError: (e: string | null) => void;
  setMicEnabled: (v: boolean) => void;
  applyAssistantState: (generationId: number, state: string, turnId: number) => void;
  applyTranscript: (line: TranscriptLine) => void;
  applySynchronizedAssistantTranscript: (segments: TranscriptionSegment[]) => void;
  advanceGeneration: () => void;
  endSession: (error?: string) => void;
  reset: () => void;
};

const mapServerState = (state: string): UiState => {
  switch (state) {
    case "speaking":
      return "speaking";
    case "interrupted":
    case "interruption_pending":
      return "interrupted";
    case "thinking":
      return "thinking";
    case "listening":
    case "user_speaking":
    case "eot_pending":
      return "listening";
    case "tool_waiting":
      return "tool_waiting";
    case "recovering":
      return "reconnecting";
    case "closed":
      return "closed";
    default:
      return "ready";
  }
};

export const useSessionStore = create<SessionState>((set, get) => ({
  uiState: "connecting",
  session: null,
  generationId: 0,
  currentTurnId: 0,
  awaitingServerGeneration: false,
  micEnabled: true,
  transcripts: [],
  error: null,
  setUiState: (s) => set({ uiState: s }),
  setSession: (session) =>
    set({
      session,
      uiState: "ready",
      generationId: 0,
      currentTurnId: 0,
      awaitingServerGeneration: false,
      transcripts: [],
      error: null,
    }),
  setError: (error) => set({ error }),
  setMicEnabled: (micEnabled) => set({ micEnabled }),
  applyAssistantState: (generationId, state, turnId) => {
    if (!shouldAcceptGeneration(get().generationId, generationId)) return;
    set({
      generationId,
      currentTurnId: turnId,
      awaitingServerGeneration: false,
      uiState: mapServerState(state),
    });
  },
  applyTranscript: (line) => {
    if (!shouldAcceptGeneration(get().generationId, line.generation_id)) {
      return;
    }
    set((st) => {
      const transcripts = [...st.transcripts];
      const last = transcripts[transcripts.length - 1];
      if (
        last &&
        last.speaker === line.speaker &&
        last.turn_id === line.turn_id &&
        last.generation_id === line.generation_id &&
        (!last.final || (line.speaker === "assistant" && line.heard === true))
      ) {
        transcripts[transcripts.length - 1] = line;
      } else {
        transcripts.push(line);
      }
      return {
        transcripts,
        generationId: Math.max(st.generationId, line.generation_id),
        currentTurnId: line.turn_id,
        awaitingServerGeneration: false,
      };
    });
  },
  applySynchronizedAssistantTranscript: (segments) => {
    const state = get();
    if (state.awaitingServerGeneration || segments.length === 0) return;
    const text = segments.map((segment) => segment.text).join("");
    if (!text) return;
    state.applyTranscript({
      speaker: "assistant",
      text,
      final: segments.every((segment) => segment.final),
      heard: true,
      turn_id: state.currentTurnId,
      generation_id: state.generationId,
    });
  },
  advanceGeneration: () =>
    set((st) => ({
      generationId: st.generationId + 1,
      awaitingServerGeneration: true,
    })),
  endSession: (error) =>
    set({
      session: null,
      uiState: "closed",
      micEnabled: true,
      currentTurnId: 0,
      awaitingServerGeneration: false,
      error: error ?? null,
    }),
  reset: () =>
    set({
      uiState: "connecting",
      session: null,
      generationId: 0,
      currentTurnId: 0,
      awaitingServerGeneration: false,
      micEnabled: true,
      transcripts: [],
      error: null,
    }),
}));
