export type CreateSessionRequest = {
  user_id: string;
  locale: string;
  client: { platform: string; timezone: string };
};

export type CreateSessionResponse = {
  session_id: string;
  livekit_url: string;
  room_name: string;
  participant_token: string;
  expires_in: number;
  agent_name: string;
  config: { locale: string; allow_text_fallback: boolean };
};

const defaultBase = () =>
  import.meta.env.VITE_CONTROL_API_URL ?? "http://localhost:8000";

export async function createSession(
  body: CreateSessionRequest,
  baseUrl: string = defaultBase(),
): Promise<CreateSessionResponse> {
  const res = await fetch(`${baseUrl}/v1/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    throw new Error(`createSession failed: ${res.status}`);
  }
  return (await res.json()) as CreateSessionResponse;
}

export async function stopResponse(
  sessionId: string,
  baseUrl: string = defaultBase(),
): Promise<void> {
  const res = await fetch(`${baseUrl}/v1/sessions/${sessionId}/stop-response`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason: "user_button" }),
  });
  if (!res.ok) {
    throw new Error(`stopResponse failed: ${res.status}`);
  }
}

export async function notifyRtcRecovered(
  sessionId: string,
  baseUrl: string = defaultBase(),
): Promise<void> {
  const res = await fetch(`${baseUrl}/v1/sessions/${sessionId}/rtc-recovered`, {
    method: "POST",
  });
  if (!res.ok) {
    throw new Error(`rtcRecovered failed: ${res.status}`);
  }
}

export async function healthLive(
  baseUrl: string = defaultBase(),
): Promise<{ status: string }> {
  const res = await fetch(`${baseUrl}/health/live`);
  if (!res.ok) throw new Error("health live failed");
  return (await res.json()) as { status: string };
}
