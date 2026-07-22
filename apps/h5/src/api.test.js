import { beforeEach, describe, expect, it, vi } from "vitest";

function jsonResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: vi.fn().mockResolvedValue(body),
    text: vi.fn().mockResolvedValue(JSON.stringify(body)),
  };
}

function blobResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    blob: vi.fn().mockResolvedValue(body),
    text: vi.fn().mockResolvedValue(""),
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

describe("authenticated Control API client", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.restoreAllMocks();
    window.localStorage.clear();
  });

  it("registers an account before adding Bearer auth", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ detail: "no refresh cookie" }, 401))
      .mockResolvedValueOnce(
        jsonResponse({
          user_id: "registered-user",
          username: "memorykeeper",
          account_type: "registered",
          access_token: "short-token",
        }, 201),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          user_id: "registered-user",
          display_name: "新朋友",
          bio: "",
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const { bootstrapIdentity, getProfile, registerAccount } = await import("./api.js");

    await expect(bootstrapIdentity()).resolves.toBeNull();
    await registerAccount("memorykeeper", "safe-passphrase");
    await getProfile("registered-user");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/memoria-api/v1/auth/refresh",
      expect.objectContaining({ method: "POST", credentials: "include" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/memoria-api/v1/auth/register",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          username: "memorykeeper",
          password: "safe-passphrase",
        }),
        headers: expect.not.objectContaining({ Authorization: expect.anything() }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "/memoria-api/v1/memory/profile/registered-user",
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: "Bearer short-token",
        }),
      }),
    );
    expect(JSON.parse(window.localStorage.getItem("memoria:identity")))
      .toEqual({
        user_id: "registered-user",
        username: "memorykeeper",
        account_type: "registered",
      });
    expect(fetchMock.mock.calls.every(([, options]) => options.credentials === "include"))
      .toBe(true);
  });

  it("refuses protected requests until identity bootstrap completes", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { getMemoryDays } = await import("./api.js");

    await expect(getMemoryDays("anonymous-user")).rejects.toThrow(
      "账号身份尚未就绪",
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("uses authenticated persona, speaker and voice-profile lifecycle endpoints", async () => {
    const preview = new Blob(["RIFF-preview"], { type: "audio/wav" });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          user_id: "registered-user",
          username: "memorykeeper",
          account_type: "registered",
          access_token: "lifecycle-token",
        }, 201),
      )
      .mockResolvedValueOnce(jsonResponse({ learning_allowed: false }))
      .mockResolvedValueOnce(jsonResponse({ policy_version: "persona-learning-v1" }, 201))
      .mockResolvedValueOnce(jsonResponse({ items: [] }))
      .mockResolvedValueOnce(jsonResponse({ consent: null, items: [] }))
      .mockResolvedValueOnce(
        jsonResponse({ trial_id: "trial-001", slots: ["A", "B"] }, 201),
      )
      .mockResolvedValueOnce(blobResponse(preview))
      .mockResolvedValueOnce(jsonResponse({ status: "passed" }));
    vi.stubGlobal("fetch", fetchMock);
    const {
      createVoiceBlindTrial,
      evaluateVoiceProfile,
      getPersonaStatus,
      getSpeakerProfiles,
      getVoiceProfiles,
      grantPersonaConsent,
      previewVoiceBlindTrial,
      registerAccount,
    } = await import("./api.js");

    await registerAccount("memorykeeper", "safe-passphrase");
    await getPersonaStatus();
    await grantPersonaConsent();
    await getSpeakerProfiles();
    await getVoiceProfiles();
    await expect(createVoiceBlindTrial("voice-001")).resolves.toEqual({
      trial_id: "trial-001",
      slots: ["A", "B"],
    });
    await expect(
      previewVoiceBlindTrial("trial-001", "A", "同一句试听文本"),
    ).resolves.toBe(preview);
    const evaluation = {
      trial_id: "trial-001",
      preferred_slot: "A",
      similarity: 4,
      naturalness: 4,
      accent_similarity: 4,
      emotion_adherence: 4,
      instruction_adherence: 4,
      uncanny: 2,
      notes: "",
    };
    await expect(evaluateVoiceProfile("voice-001", evaluation)).resolves.toEqual({
      status: "passed",
    });

    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/memoria-api/v1/persona/status",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer lifecycle-token" }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "/memoria-api/v1/persona/consent",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          accepted: true,
          policy_version: "persona-learning-v1",
        }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      6,
      "/memoria-api/v1/voices/profiles/voice-001/blind-trials",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      7,
      "/memoria-api/v1/voices/blind-trials/trial-001/preview",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ slot: "A", text: "同一句试听文本" }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      8,
      "/memoria-api/v1/voices/profiles/voice-001/evaluations",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(evaluation),
      }),
    );
  });

  it("loads and reviews the structured life archive with the account token", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          user_id: "registered-user",
          username: "memorykeeper",
          account_type: "registered",
          access_token: "archive-token",
        }, 201),
      )
      .mockResolvedValueOnce(jsonResponse({ items: [] }))
      .mockResolvedValueOnce(jsonResponse({ items: [] }))
      .mockResolvedValueOnce(jsonResponse({ items: [] }))
      .mockResolvedValueOnce(jsonResponse({ claim_id: "claim-1", status: "confirmed" }));
    vi.stubGlobal("fetch", fetchMock);
    const {
      getLifeTimeline,
      getMemoryReviewQueue,
      registerAccount,
      reviewMemoryClaim,
      searchLifeArchive,
    } = await import("./api.js");

    await registerAccount("memorykeeper", "safe-passphrase");
    await getLifeTimeline();
    await searchLifeArchive("家风 家训");
    await getMemoryReviewQueue();
    await reviewMemoryClaim("claim-1", "confirm");

    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/memoria-api/v1/archive/life-timeline?limit=30",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer archive-token" }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "/memoria-api/v1/archive/search?q=%E5%AE%B6%E9%A3%8E%20%E5%AE%B6%E8%AE%AD&include_candidates=true&limit=30",
      expect.anything(),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "/memoria-api/v1/archive/review-queue",
      expect.anything(),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      5,
      "/memoria-api/v1/archive/memories/claim-1/review",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ action: "confirm" }),
      }),
    );
  });

  it("manages raw voice archive consent independently", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          user_id: "registered-user",
          username: "memorykeeper",
          account_type: "registered",
          access_token: "archive-token",
        }, 201),
      )
      .mockResolvedValueOnce(jsonResponse({ consent: null }))
      .mockResolvedValueOnce(jsonResponse({
        consent_grant_id: "raw-consent-1",
        policy_version: "raw-voice-archive-v1",
        retention_policy: "account_lifetime",
      }, 201))
      .mockResolvedValueOnce(jsonResponse({
        consent_grant_id: "raw-consent-1",
        revoked_at: "2026-07-19T12:00:00Z",
        deleted_blob_count: 2,
      }));
    vi.stubGlobal("fetch", fetchMock);
    const {
      getRawVoiceConsent,
      grantRawVoiceConsent,
      registerAccount,
      revokeRawVoiceConsent,
    } = await import("./api.js");

    await registerAccount("memorykeeper", "safe-passphrase");
    await getRawVoiceConsent();
    await grantRawVoiceConsent();
    await revokeRawVoiceConsent();

    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/memoria-api/v1/archive/raw-voice-consent",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer archive-token" }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "/memoria-api/v1/archive/raw-voice-consent",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          policy_version: "raw-voice-archive-v1",
          retention_policy: "account_lifetime",
        }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "/memoria-api/v1/archive/raw-voice-consent",
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("includes an optional counterexample when reviewing a persona trait", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          user_id: "registered-user",
          username: "memorykeeper",
          account_type: "registered",
          access_token: "persona-token",
        }, 201),
      )
      .mockResolvedValueOnce(jsonResponse({ trait_id: "trait-1", status: "confirmed" }));
    vi.stubGlobal("fetch", fetchMock);
    const { registerAccount, reviewPersonaTrait } = await import("./api.js");

    await registerAccount("memorykeeper", "safe-passphrase");
    await reviewPersonaTrait("trait-1", "confirm", {
      counterexample: "紧急安全风险出现时会立即行动。",
    });

    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/memoria-api/v1/persona/traits/trait-1/review",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          action: "confirm",
          counterexample: "紧急安全风险出现时会立即行动。",
        }),
      }),
    );
  });

  it("logs in without sending a stale Bearer and persists the returned account", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({
      user_id: "returning-user",
      username: "memorykeeper",
      account_type: "registered",
      access_token: "returning-token",
    }));
    vi.stubGlobal("fetch", fetchMock);
    const { loginAccount } = await import("./api.js");

    await expect(
      loginAccount("memorykeeper", "safe-passphrase"),
    ).resolves.toEqual({
      user_id: "returning-user",
      username: "memorykeeper",
      account_type: "registered",
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/memoria-api/v1/auth/login",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          username: "memorykeeper",
          password: "safe-passphrase",
        }),
        headers: expect.not.objectContaining({ Authorization: expect.anything() }),
      }),
    );
    expect(JSON.parse(window.localStorage.getItem("memoria:identity")))
      .toEqual({
        user_id: "returning-user",
        username: "memorykeeper",
        account_type: "registered",
      });
  });

  it("creates an anonymous session without persisting its access token", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({
      user_id: "anonymous-user",
      account_type: "anonymous",
      access_token: "anonymous-token",
    }));
    vi.stubGlobal("fetch", fetchMock);
    const { createAnonymousIdentity, getAccessToken } = await import("./api.js");

    await expect(createAnonymousIdentity()).resolves.toEqual({
      user_id: "anonymous-user",
      username: null,
      account_type: "anonymous",
    });
    expect(getAccessToken()).toBe("anonymous-token");
    expect(JSON.parse(window.localStorage.getItem("memoria:identity")))
      .not.toHaveProperty("access_token");
  });

  it("upgrades a legacy stored token once and immediately replaces it with a safe snapshot", async () => {
    window.localStorage.setItem(
      "memoria:identity",
      JSON.stringify({
        user_id: "anonymous-user",
        account_type: "anonymous",
        access_token: "saved-token",
      }),
    );
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({
        user_id: "anonymous-user",
        account_type: "anonymous",
        access_token: "upgraded-token",
      }))
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
      reject_non_owner_voice: false,
      timezone: "Asia/Shanghai",
    });

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/memoria-api/v1/auth/upgrade",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          Authorization: "Bearer saved-token",
        }),
      }),
    );
    const [, options] = fetchMock.mock.calls[1];
    expect(options.headers.Authorization).toBe("Bearer upgraded-token");
    expect(JSON.parse(options.body)).toEqual({
      display_name: "小忆",
      bio: "慢慢说",
      auto_summary: false,
      voice_reply: false,
      gentle_reminders: false,
      reject_non_owner_voice: false,
      timezone: "Asia/Shanghai",
    });
    expect(JSON.parse(window.localStorage.getItem("memoria:identity"))).toEqual({
      user_id: "anonymous-user",
      username: null,
      account_type: "anonymous",
    });
  });

  it.each([401, 403])(
    "returns to the account gate when legacy upgrade returns %i without deleting account caches",
    async (status) => {
      window.localStorage.setItem(
        "memoria:identity",
        JSON.stringify({ user_id: "old-user", access_token: "old-token" }),
      );
      window.localStorage.setItem("memoria:profile:old-user", JSON.stringify({ bio: "keep" }));
      window.localStorage.setItem("memoria:pending-messages:other-user", "[]");
      const fetchMock = vi
        .fn()
        .mockResolvedValueOnce(jsonResponse({ detail: "invalid token" }, status));
      vi.stubGlobal("fetch", fetchMock);
      const { bootstrapIdentity } = await import("./api.js");

      await expect(bootstrapIdentity()).resolves.toBeNull();

      expect(fetchMock).toHaveBeenNthCalledWith(
        1,
        "/memoria-api/v1/auth/upgrade",
        expect.objectContaining({
          headers: expect.objectContaining({
            Authorization: "Bearer old-token",
          }),
        }),
      );
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(window.localStorage.getItem("memoria:identity")).toBeNull();
      expect(window.localStorage.getItem("memoria:anonymous-identity")).toBeNull();
      expect(window.localStorage.getItem("memoria:profile:old-user")).not.toBeNull();
      expect(window.localStorage.getItem("memoria:pending-messages:other-user"))
        .toBe("[]");
    },
  );

  it("keeps a legacy credential until the bounded upgrade succeeds", async () => {
    const stored = {
      user_id: "registered-user",
      username: "memorykeeper",
      account_type: "registered",
      access_token: "saved-token",
    };
    window.localStorage.setItem(
      "memoria:identity",
      JSON.stringify(stored),
    );
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("network unavailable"))
      .mockResolvedValueOnce(jsonResponse({
        user_id: "registered-user",
        username: "memorykeeper",
        account_type: "registered",
        access_token: "upgraded-token",
      }));
    vi.stubGlobal("fetch", fetchMock);
    const { bootstrapIdentity } = await import("./api.js");

    const bootstrapping = bootstrapIdentity();
    expect(JSON.parse(window.localStorage.getItem("memoria:identity"))).toEqual(stored);

    await expect(bootstrapping).resolves.toEqual({
      user_id: "registered-user",
      username: "memorykeeper",
      account_type: "registered",
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][0]).toBe("/memoria-api/v1/auth/upgrade");
    expect(fetchMock.mock.calls[1][1].headers).toEqual(
      expect.objectContaining({ Authorization: "Bearer saved-token" }),
    );
    expect(JSON.parse(window.localStorage.getItem("memoria:identity"))).toEqual({
      user_id: "registered-user",
      username: "memorykeeper",
      account_type: "registered",
    });
  });

  it("can resume a committed legacy upgrade after a reload loses the response", async () => {
    const stored = {
      user_id: "anonymous-user",
      account_type: "anonymous",
      access_token: "saved-token",
    };
    window.localStorage.setItem("memoria:identity", JSON.stringify(stored));
    const lostResponse = vi.fn()
      .mockRejectedValueOnce(new TypeError("connection closed"))
      .mockRejectedValueOnce(new TypeError("connection closed"));
    vi.stubGlobal("fetch", lostResponse);
    const firstModule = await import("./api.js");

    await expect(firstModule.bootstrapIdentity()).rejects.toThrow("connection closed");
    expect(JSON.parse(window.localStorage.getItem("memoria:identity"))).toEqual(stored);

    vi.resetModules();
    const recovered = vi.fn().mockResolvedValueOnce(jsonResponse({
      user_id: "anonymous-user",
      account_type: "anonymous",
      access_token: "recovered-short-token",
    }));
    vi.stubGlobal("fetch", recovered);
    const secondModule = await import("./api.js");

    await expect(secondModule.bootstrapIdentity()).resolves.toEqual({
      user_id: "anonymous-user",
      username: null,
      account_type: "anonymous",
    });
    expect(recovered).toHaveBeenCalledWith(
      "/memoria-api/v1/auth/upgrade",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer saved-token" }),
      }),
    );
    expect(JSON.parse(window.localStorage.getItem("memoria:identity"))).toEqual({
      user_id: "anonymous-user",
      username: null,
      account_type: "anonymous",
    });
  });

  it("bootstraps a safe stored identity through the refresh cookie", async () => {
    window.localStorage.setItem("memoria:identity", JSON.stringify({
      user_id: "registered-user",
      username: "memorykeeper",
      account_type: "registered",
    }));
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({
      user_id: "registered-user",
      username: "memorykeeper",
      account_type: "registered",
      access_token: "refreshed-token",
    }));
    vi.stubGlobal("fetch", fetchMock);
    const { bootstrapIdentity, getAccessToken } = await import("./api.js");

    await expect(bootstrapIdentity()).resolves.toEqual({
      user_id: "registered-user",
      username: "memorykeeper",
      account_type: "registered",
    });
    expect(getAccessToken()).toBe("refreshed-token");
    expect(fetchMock).toHaveBeenCalledWith(
      "/memoria-api/v1/auth/refresh",
      expect.objectContaining({ method: "POST", credentials: "include" }),
    );
    expect(JSON.parse(window.localStorage.getItem("memoria:identity")))
      .not.toHaveProperty("access_token");
  });

  it("prefers a safe current snapshot over a stale legacy identity token", async () => {
    window.localStorage.setItem("memoria:identity", JSON.stringify({
      user_id: "current-user",
      username: "current",
      account_type: "registered",
    }));
    window.localStorage.setItem("memoria:anonymous-identity", JSON.stringify({
      user_id: "stale-user",
      access_token: "stale-long-token",
    }));
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({
      user_id: "current-user",
      username: "current",
      account_type: "registered",
      access_token: "current-short-token",
    }));
    vi.stubGlobal("fetch", fetchMock);
    const { bootstrapIdentity, getAccessToken } = await import("./api.js");

    await expect(bootstrapIdentity()).resolves.toEqual({
      user_id: "current-user",
      username: "current",
      account_type: "registered",
    });
    expect(getAccessToken()).toBe("current-short-token");
    expect(fetchMock).toHaveBeenCalledWith(
      "/memoria-api/v1/auth/refresh",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBeUndefined();
    expect(window.localStorage.getItem("memoria:anonymous-identity")).toBeNull();
  });

  it("retries one refresh race without clearing the saved identity", async () => {
    vi.useFakeTimers();
    try {
      window.localStorage.setItem("memoria:identity", JSON.stringify({
        user_id: "registered-user",
        username: "memorykeeper",
        account_type: "registered",
      }));
      const concurrent = jsonResponse(
        { detail: "refresh already rotated; retry with the current cookie" },
        409,
      );
      concurrent.headers = {
        get: vi.fn((name) => name.toLowerCase() === "retry-after" ? "1" : null),
      };
      const fetchMock = vi
        .fn()
        .mockResolvedValueOnce(concurrent)
        .mockResolvedValueOnce(jsonResponse({
          user_id: "registered-user",
          username: "memorykeeper",
          account_type: "registered",
          access_token: "refreshed-after-race",
        }));
      vi.stubGlobal("fetch", fetchMock);
      const { bootstrapIdentity, getAccessToken } = await import("./api.js");

      const bootstrapped = bootstrapIdentity();
      await vi.advanceTimersByTimeAsync(1_000);

      await expect(bootstrapped).resolves.toEqual({
        user_id: "registered-user",
        username: "memorykeeper",
        account_type: "registered",
      });
      expect(getAccessToken()).toBe("refreshed-after-race");
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(JSON.parse(window.localStorage.getItem("memoria:identity"))).toEqual({
        user_id: "registered-user",
        username: "memorykeeper",
        account_type: "registered",
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("shares one refresh across concurrent 401 responses and retries each request once", async () => {
    let refreshCalls = 0;
    const fetchMock = vi.fn(async (url, options) => {
      if (url.endsWith("/v1/auth/login")) {
        return jsonResponse({
          user_id: "registered-user",
          username: "memorykeeper",
          account_type: "registered",
          access_token: "expired-token",
        });
      }
      if (url.endsWith("/v1/auth/refresh")) {
        refreshCalls += 1;
        await Promise.resolve();
        return jsonResponse({
          user_id: "registered-user",
          username: "memorykeeper",
          account_type: "registered",
          access_token: "fresh-token",
        });
      }
      if (url.includes("/v1/memory/profile/")) {
        return options.headers.Authorization === "Bearer fresh-token"
          ? jsonResponse({ display_name: "小忆" })
          : jsonResponse({ detail: "expired" }, 401);
      }
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const { getProfile, loginAccount } = await import("./api.js");
    await loginAccount("memorykeeper", "safe-passphrase");

    await expect(Promise.all([
      getProfile("registered-user"),
      getProfile("registered-user"),
    ])).resolves.toEqual([{ display_name: "小忆" }, { display_name: "小忆" }]);

    expect(refreshCalls).toBe(1);
    expect(fetchMock.mock.calls.filter(([url]) => url.endsWith("/v1/auth/refresh")))
      .toHaveLength(1);
    expect(fetchMock.mock.calls.filter(([, options]) =>
      options.headers.Authorization === "Bearer fresh-token"))
      .toHaveLength(2);
  });

  it("does not recurse when refresh fails and clears the in-memory access token", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({
        user_id: "registered-user",
        account_type: "registered",
        access_token: "expired-token",
      }))
      .mockResolvedValueOnce(jsonResponse({ detail: "expired" }, 401))
      .mockResolvedValueOnce(jsonResponse({ detail: "invalid refresh" }, 401));
    vi.stubGlobal("fetch", fetchMock);
    const { getAccessToken, getProfile, loginAccount } = await import("./api.js");
    await loginAccount("memorykeeper", "safe-passphrase");

    await expect(getProfile("registered-user")).rejects.toThrow("invalid refresh");
    expect(getAccessToken()).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("logs out the current or all devices and keeps account-scoped caches", async () => {
    window.localStorage.setItem("memoria:profile:registered-user", "{}");
    window.localStorage.setItem("memoria:pending-messages:other-user", "[]");
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({
        user_id: "registered-user",
        account_type: "registered",
        access_token: "access-token",
      }))
      .mockResolvedValueOnce({ ok: true, status: 204 })
      .mockResolvedValueOnce(jsonResponse({
        user_id: "registered-user",
        account_type: "registered",
        access_token: "access-token-2",
      }))
      .mockResolvedValueOnce({ ok: true, status: 204 });
    vi.stubGlobal("fetch", fetchMock);
    const {
      getAccessToken,
      loginAccount,
      logoutAllDevices,
      logoutCurrentDevice,
    } = await import("./api.js");

    await loginAccount("memorykeeper", "safe-passphrase");
    await logoutCurrentDevice();
    expect(getAccessToken()).toBeNull();
    expect(window.localStorage.getItem("memoria:profile:registered-user")).toBe("{}");
    expect(window.localStorage.getItem("memoria:pending-messages:other-user")).toBe("[]");
    await loginAccount("memorykeeper", "safe-passphrase");
    await logoutAllDevices();

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/memoria-api/v1/auth/login",
      "/memoria-api/v1/auth/logout",
      "/memoria-api/v1/auth/login",
      "/memoria-api/v1/auth/logout-all",
    ]);
    expect(window.localStorage.getItem("memoria:identity")).toBeNull();
  });

  it("never flushes another account's locally queued messages", async () => {
    window.localStorage.setItem(
      "memoria:identity",
      JSON.stringify({
        user_id: "account-b",
        username: "account-b",
        account_type: "registered",
        access_token: "saved-token",
      }),
    );
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({
      user_id: "account-b",
      username: "account-b",
      account_type: "registered",
      access_token: "upgraded-token",
    }));
    vi.stubGlobal("fetch", fetchMock);
    const {
      bootstrapIdentity,
      cachePendingMessage,
      flushPendingMessages,
    } = await import("./api.js");

    await bootstrapIdentity();
    cachePendingMessage({
      user_id: "account-a",
      role: "user",
      text: "只属于账号 A",
      history_eligible: true,
    });
    await flushPendingMessages();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(
      JSON.parse(
        window.localStorage.getItem("memoria:pending-messages:account-a"),
      ),
    ).toHaveLength(1);
  });

  it("uses one client message ID across a failed send, cache, and retry", async () => {
    window.localStorage.setItem(
      "memoria:identity",
      JSON.stringify({
        user_id: "account-a",
        username: "account-a",
        account_type: "registered",
        access_token: "saved-token",
      }),
    );
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({
        user_id: "account-a",
        username: "account-a",
        account_type: "registered",
        access_token: "upgraded-token",
      }))
      .mockResolvedValueOnce(jsonResponse({ detail: "network retry" }, 503))
      .mockResolvedValueOnce(jsonResponse({ id: 1 }, 201));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("crypto", { randomUUID: () => "6c83b852-8c91-4e5e-91df-6fd4cb4fe7a8" });
    const { bootstrapIdentity, cachePendingMessage, flushPendingMessages, saveMessage } =
      await import("./api.js");
    await bootstrapIdentity();
    const message = {
      user_id: "account-a",
      role: "user",
      text: "响应丢失后只保存一次",
      emotion: "calm",
      history_eligible: true,
    };

    await expect(saveMessage(message)).rejects.toThrow("network retry");
    cachePendingMessage(message);
    await flushPendingMessages();

    const first = JSON.parse(fetchMock.mock.calls[1][1].body);
    const retry = JSON.parse(fetchMock.mock.calls[2][1].body);
    expect(first.client_message_id).toBe("6c83b852-8c91-4e5e-91df-6fd4cb4fe7a8");
    expect(retry.client_message_id).toBe(first.client_message_id);
    expect(window.localStorage.getItem("memoria:pending-messages:account-a")).toBe("[]");
  });

  it("upgrades a legacy pending message with an ID before retrying it", async () => {
    window.localStorage.setItem(
      "memoria:identity",
      JSON.stringify({
        user_id: "account-a",
        username: "account-a",
        account_type: "registered",
        access_token: "saved-token",
      }),
    );
    window.localStorage.setItem(
      "memoria:pending-messages:account-a",
      JSON.stringify([{
        user_id: "account-a",
        role: "assistant",
        text: "旧缓存",
        history_eligible: true,
      }]),
    );
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({
        user_id: "account-a",
        username: "account-a",
        account_type: "registered",
        access_token: "upgraded-token",
      }))
      .mockResolvedValueOnce(jsonResponse({ detail: "retry later" }, 503));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("crypto", { randomUUID: () => "e5bf50f4-4a5a-4d57-9a37-bf9ee7063431" });
    const { bootstrapIdentity, flushPendingMessages } = await import("./api.js");
    await bootstrapIdentity();
    await flushPendingMessages();

    expect(JSON.parse(fetchMock.mock.calls[1][1].body).client_message_id)
      .toBe("e5bf50f4-4a5a-4d57-9a37-bf9ee7063431");
    expect(JSON.parse(window.localStorage.getItem("memoria:pending-messages:account-a")))
      .toEqual([expect.objectContaining({ client_message_id: "e5bf50f4-4a5a-4d57-9a37-bf9ee7063431" })]);
  });

  it("does not recreate a deleted account's pending cache after a late flush", async () => {
    const identity = {
      user_id: "account-a",
      username: "account-a",
      account_type: "registered",
      access_token: "account-token",
    };
    window.localStorage.setItem("memoria:identity", JSON.stringify(identity));
    window.localStorage.setItem(
      "memoria:pending-messages:account-a",
      JSON.stringify([
        {
          user_id: "account-a",
          role: "user",
          text: "旧消息",
          history_eligible: true,
        },
      ]),
    );
    const lateSave = deferred();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(identity))
      .mockReturnValueOnce(lateSave.promise)
      .mockResolvedValueOnce(jsonResponse({ status: "completed" }));
    vi.stubGlobal("fetch", fetchMock);
    const { bootstrapIdentity, deleteAccountData, flushPendingMessages } =
      await import("./api.js");
    await bootstrapIdentity();

    const flushing = flushPendingMessages();
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await deleteAccountData("safe-passphrase", "永久删除我的全部数据");
    lateSave.resolve(jsonResponse({ detail: "account deleted" }, 409));
    await flushing;

    expect(
      window.localStorage.getItem("memoria:pending-messages:account-a"),
    ).toBeNull();
  });

  it("never sends or queues history-ineligible messages", async () => {
    window.localStorage.setItem(
      "memoria:identity",
      JSON.stringify({
        user_id: "account-a",
        username: "account-a",
        account_type: "registered",
        access_token: "saved-token",
      }),
    );
    const fetchMock = vi.fn().mockResolvedValueOnce(
      jsonResponse({
        user_id: "account-a",
        username: "account-a",
        account_type: "registered",
        access_token: "upgraded-token",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const {
      bootstrapIdentity,
      cachePendingMessage,
      flushPendingMessages,
      saveMessage,
    } = await import("./api.js");
    await bootstrapIdentity();
    const guest = {
      user_id: "account-a",
      role: "user",
      text: "访客消息",
      history_eligible: false,
    };

    await saveMessage(guest);
    cachePendingMessage(guest);
    await flushPendingMessages();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(
      window.localStorage.getItem("memoria:pending-messages:account-a"),
    ).toBeNull();
  });

  it("exports the current account and clears only its local data after server deletion", async () => {
    window.localStorage.setItem(
      "memoria:identity",
      JSON.stringify({
        user_id: "account-a",
        username: "account-a",
        account_type: "registered",
        access_token: "account-token",
      }),
    );
    window.localStorage.setItem("memoria:profile:account-a", JSON.stringify({ bio: "private" }));
    window.localStorage.setItem(
      "memoria:pending-messages:account-a",
      JSON.stringify([{ user_id: "account-a", text: "pending" }]),
    );
    window.localStorage.setItem(
      "memoria:pending-messages",
      JSON.stringify([
        { user_id: "account-a", text: "legacy-a" },
        { user_id: "account-b", text: "legacy-b" },
      ]),
    );
    const archive = { format_version: 1, sections: { conversations: [] } };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({
        user_id: "account-a",
        username: "account-a",
        account_type: "registered",
        access_token: "account-token",
      }))
      .mockResolvedValueOnce(jsonResponse(archive))
      .mockResolvedValueOnce(jsonResponse({ status: "completed" }));
    vi.stubGlobal("fetch", fetchMock);
    const {
      bootstrapIdentity,
      deleteAccountData,
      exportAccountArchive,
      getAccessToken,
    } = await import("./api.js");
    await bootstrapIdentity();

    await expect(exportAccountArchive("safe-passphrase")).resolves.toEqual(archive);
    await expect(
      deleteAccountData("safe-passphrase", "永久删除我的全部数据"),
    ).resolves.toEqual({ status: "completed" });

    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/memoria-api/v1/archive/exports",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ password: "safe-passphrase" }),
        headers: expect.objectContaining({ Authorization: "Bearer account-token" }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "/memoria-api/v1/archive/deletion-requests",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          password: "safe-passphrase",
          confirmation: "永久删除我的全部数据",
        }),
      }),
    );
    expect(getAccessToken()).toBeNull();
    expect(window.localStorage.getItem("memoria:identity")).toBeNull();
    expect(window.localStorage.getItem("memoria:profile:account-a")).toBeNull();
    expect(window.localStorage.getItem("memoria:pending-messages:account-a")).toBeNull();
    expect(JSON.parse(window.localStorage.getItem("memoria:pending-messages")))
      .toEqual([{ user_id: "account-b", text: "legacy-b" }]);
  });

  it("creates the selected voice backend and exchanges Omni SDP through the first-party API", async () => {
    window.localStorage.setItem(
      "memoria:anonymous-identity",
      JSON.stringify({ user_id: "anonymous-user", access_token: "saved-token" }),
    );
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({
        user_id: "anonymous-user",
        account_type: "anonymous",
        access_token: "upgraded-token",
      }))
      .mockResolvedValueOnce(
        jsonResponse({
          session_id: "omni-session",
          voice_backend: "qwen_omni",
          interaction: {
            interaction_mode: "companion",
            mode_policy_version: "s2-v1",
            companion_style_id: "starlight",
            companion_style_version: "companion-v1",
            digital_self_version_id: null,
            relationship_profile_id: null,
            legacy_grant_id: null,
          },
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
        interaction_mode: "companion",
      }),
    );
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).not.toHaveProperty(
      "digital_self_version_id",
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "/memoria-api/v1/sessions/omni-session/omni/sdp",
      expect.objectContaining({
        method: "POST",
        body: "offer-sdp",
        headers: expect.objectContaining({
          Authorization: "Bearer upgraded-token",
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
          Authorization: "Bearer upgraded-token",
          "Content-Type": "application/json",
        }),
      }),
    );
  });

  it("loads server mode capabilities and rejects an unbound voice session", async () => {
    window.localStorage.setItem(
      "memoria:identity",
      JSON.stringify({ user_id: "owner", access_token: "legacy-token" }),
    );
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({
        user_id: "owner",
        account_type: "registered",
        access_token: "short-token",
      }))
      .mockResolvedValueOnce(jsonResponse({
        selected_companion_id: "starlight",
        modes: {
          companion: { status: "available", conversational: true },
          self_preview: {
            status: "blocked",
            conversational: true,
            missing: ["approved_digital_self_version"],
          },
        },
      }))
      .mockResolvedValueOnce(jsonResponse({
        session_id: "unbound-session",
        voice_backend: "cascade",
      }));
    vi.stubGlobal("fetch", fetchMock);
    const {
      bootstrapIdentity,
      createSession,
      getInteractionCapabilities,
    } = await import("./api.js");
    await bootstrapIdentity();

    await expect(getInteractionCapabilities()).resolves.toEqual(
      expect.objectContaining({ selected_companion_id: "starlight" }),
    );
    await expect(createSession("owner")).rejects.toThrow(
      "服务端没有返回可验证的陪伴模式",
    );
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/memoria-api/v1/interaction/capabilities",
    );
  });
});
