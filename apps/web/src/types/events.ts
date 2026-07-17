import { z } from "zod";

export const UiStateSchema = z.enum([
  "connecting",
  "ready",
  "listening",
  "thinking",
  "speaking",
  "interrupted",
  "tool_waiting",
  "reconnecting",
  "closed",
]);

export type UiState = z.infer<typeof UiStateSchema>;

export const UI_STATE_LABELS: Record<UiState, string> = {
  connecting: "正在连接",
  ready: "可以说话",
  listening: "正在听",
  thinking: "正在思考",
  speaking: "正在回答",
  interrupted: "已被打断",
  tool_waiting: "正在处理任务",
  reconnecting: "连接恢复中",
  closed: "已结束",
};

export const AssistantStateEventSchema = z.object({
  type: z.literal("assistant_state"),
  session_id: z.string(),
  state: z.string(),
  turn_id: z.number().int().nonnegative(),
  generation_id: z.number().int().nonnegative(),
  at: z.string().datetime({ offset: true }),
}).strict();

export const TranscriptDeltaEventSchema = z.object({
  type: z.literal("transcript_delta"),
  speaker: z.enum(["user", "assistant"]),
  text: z.string(),
  final: z.boolean(),
  heard: z.boolean().optional(),
  turn_id: z.number().int().nonnegative(),
  generation_id: z.number().int().nonnegative(),
}).strict();

export const LiveKitDataEventSchema = z.discriminatedUnion("type", [
  AssistantStateEventSchema,
  TranscriptDeltaEventSchema,
]);

export type TranscriptDeltaEvent = z.infer<typeof TranscriptDeltaEventSchema>;
export type AssistantStateEvent = z.infer<typeof AssistantStateEventSchema>;
export type LiveKitDataEvent = z.infer<typeof LiveKitDataEventSchema>;

const decoder = new TextDecoder();

export function parseLiveKitDataEvent(
  payload: Uint8Array,
): LiveKitDataEvent | null {
  try {
    const parsed: unknown = JSON.parse(decoder.decode(payload));
    const result = LiveKitDataEventSchema.safeParse(parsed);
    return result.success ? result.data : null;
  } catch {
    return null;
  }
}

export function shouldAcceptGeneration(
  current: number,
  incoming: number,
): boolean {
  return incoming >= current;
}
