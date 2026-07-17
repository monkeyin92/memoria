import { beforeEach, describe, expect, it, vi } from "vitest";

function jsonResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: vi.fn().mockResolvedValue(body),
    text: vi.fn().mockResolvedValue(JSON.stringify(body)),
  };
}

describe("authenticated Control API client", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.restoreAllMocks();
    window.localStorage.clear();
  });

  it("bootstraps an anonymous identity before adding Bearer auth", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({ user_id: "anonymous-user", access_token: "short-token" }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          user_id: "anonymous-user",
          display_name: "新朋友",
          bio: "",
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const { bootstrapIdentity, getProfile } = await import("./api.js");

    await bootstrapIdentity();
    await getProfile("anonymous-user");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/memoria-api/v1/auth/anonymous",
      expect.objectContaining({
        method: "POST",
        headers: expect.not.objectContaining({ Authorization: expect.anything() }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/memoria-api/v1/memory/profile/anonymous-user",
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: "Bearer short-token",
        }),
      }),
    );
    expect(JSON.parse(window.localStorage.getItem("memoria:anonymous-identity")))
      .toEqual({ user_id: "anonymous-user", access_token: "short-token" });
  });

  it("refuses protected requests until identity bootstrap completes", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { getMemoryDays } = await import("./api.js");

    await expect(getMemoryDays("anonymous-user")).rejects.toThrow(
      "匿名身份尚未就绪",
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("sends every profile preference to the authenticated server profile", async () => {
    window.localStorage.setItem(
      "memoria:anonymous-identity",
      JSON.stringify({ user_id: "anonymous-user", access_token: "saved-token" }),
    );
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ user_id: "anonymous-user" }))
      .mockResolvedValueOnce(jsonResponse({}));
    vi.stubGlobal("fetch", fetchMock);
    const { bootstrapIdentity, updateProfile } = await import("./api.js");
    await bootstrapIdentity();

    await updateProfile("anonymous-user", {
      display_name: "小忆",
      bio: "慢慢说",
      auto_summary: false,
      voice_reply: false,
      gentle_reminders: false,
      timezone: "Asia/Shanghai",
    });

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/memoria-api/v1/auth/me",
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: "Bearer saved-token",
        }),
      }),
    );
    const [, options] = fetchMock.mock.calls[1];
    expect(options.headers.Authorization).toBe("Bearer saved-token");
    expect(JSON.parse(options.body)).toEqual({
      display_name: "小忆",
      bio: "慢慢说",
      auto_summary: false,
      voice_reply: false,
      gentle_reminders: false,
      timezone: "Asia/Shanghai",
    });
  });

  it.each([401, 403])(
    "replaces a stored identity when /v1/auth/me returns %i",
    async (status) => {
      window.localStorage.setItem(
        "memoria:anonymous-identity",
        JSON.stringify({ user_id: "old-user", access_token: "old-token" }),
      );
      const fetchMock = vi
        .fn()
        .mockResolvedValueOnce(jsonResponse({ detail: "invalid token" }, status))
        .mockResolvedValueOnce(
          jsonResponse({ user_id: "new-user", access_token: "new-token" }),
        );
      vi.stubGlobal("fetch", fetchMock);
      const { bootstrapIdentity } = await import("./api.js");

      await expect(bootstrapIdentity()).resolves.toEqual({
        user_id: "new-user",
        access_token: "new-token",
      });

      expect(fetchMock).toHaveBeenNthCalledWith(
        1,
        "/memoria-api/v1/auth/me",
        expect.objectContaining({
          headers: expect.objectContaining({
            Authorization: "Bearer old-token",
          }),
        }),
      );
      expect(fetchMock).toHaveBeenNthCalledWith(
        2,
        "/memoria-api/v1/auth/anonymous",
        expect.objectContaining({
          method: "POST",
          headers: expect.not.objectContaining({
            Authorization: expect.anything(),
          }),
        }),
      );
      expect(
        JSON.parse(window.localStorage.getItem("memoria:anonymous-identity")),
      ).toEqual({ user_id: "new-user", access_token: "new-token" });
    },
  );

  it("keeps a stored identity when /v1/auth/me has a network failure", async () => {
    const stored = { user_id: "anonymous-user", access_token: "saved-token" };
    window.localStorage.setItem(
      "memoria:anonymous-identity",
      JSON.stringify(stored),
    );
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("network unavailable"))
      .mockResolvedValueOnce(jsonResponse({ user_id: "anonymous-user" }));
    vi.stubGlobal("fetch", fetchMock);
    const { bootstrapIdentity } = await import("./api.js");

    await expect(bootstrapIdentity()).rejects.toThrow("network unavailable");
    expect(
      JSON.parse(window.localStorage.getItem("memoria:anonymous-identity")),
    ).toEqual(stored);

    await expect(bootstrapIdentity()).resolves.toEqual(stored);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][1].headers.Authorization).toBe(
      "Bearer saved-token",
    );
  });

  it("creates the selected voice backend and exchanges Omni SDP through the first-party API", async () => {
    window.localStorage.setItem(
      "memoria:anonymous-identity",
      JSON.stringify({ user_id: "anonymous-user", access_token: "saved-token" }),
    );
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ user_id: "anonymous-user" }))
      .mockResolvedValueOnce(
        jsonResponse({
          session_id: "omni-session",
          voice_backend: "qwen_omni",
        }),
      )
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        text: vi.fn().mockResolvedValue("answer-sdp"),
      })
      .mockResolvedValueOnce({ ok: true, status: 204 });
    vi.stubGlobal("fetch", fetchMock);
    const {
      bootstrapIdentity,
      createSession,
      exchangeOmniSdp,
      publishOmniTelemetry,
    } = await import("./api.js");
    await bootstrapIdentity();

    await createSession("anonymous-user", "qwen_omni");
    await expect(
      exchangeOmniSdp("omni-session", "offer-sdp"),
    ).resolves.toBe("answer-sdp");
    await publishOmniTelemetry("omni-session", {
      name: "webrtc_inbound_audio",
      elapsed_ms: 5000,
      turn_id: 1,
      generation_id: 1,
      metrics: { jitter: 0.004 },
    });

    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual(
      expect.objectContaining({
        user_id: "anonymous-user",
        voice_backend: "qwen_omni",
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "/memoria-api/v1/sessions/omni-session/omni/sdp",
      expect.objectContaining({
        method: "POST",
        body: "offer-sdp",
        headers: expect.objectContaining({
          Authorization: "Bearer saved-token",
          "Content-Type": "application/sdp",
        }),
      }),
    );
    expect(JSON.stringify(fetchMock.mock.calls)).not.toContain("DASHSCOPE_API_KEY");
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "/memoria-api/v1/sessions/omni-session/telemetry",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          name: "webrtc_inbound_audio",
          elapsed_ms: 5000,
          turn_id: 1,
          generation_id: 1,
          metrics: { jitter: 0.004 },
        }),
        headers: expect.objectContaining({
          Authorization: "Bearer saved-token",
          "Content-Type": "application/json",
        }),
      }),
    );
  });
});
