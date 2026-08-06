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

  it("does not abort a voice enrollment at the former shared 10 second timeout", async () => {
    vi.useFakeTimers();
    const enrollmentResponse = deferred();
    let enrollmentSignal = null;
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          user_id: "registered-user",
          username: "memorykeeper",
          account_type: "registered",
          access_token: "short-token",
        }),
      )
      .mockImplementationOnce((_url, options) => {
        enrollmentSignal = options.signal;
        return enrollmentResponse.promise;
      });
    vi.stubGlobal("fetch", fetchMock);
    const { bootstrapIdentity, enrollVoiceProfile } = await import("./api.js");

    await bootstrapIdentity();
    let settled = false;
    const pending = enrollVoiceProfile({ sample: "audio" }).finally(() => {
      settled = true;
    });
    await vi.advanceTimersByTimeAsync(120_000);

    expect(settled).toBe(false);
    expect(enrollmentSignal).toBeInstanceOf(AbortSignal);
    expect(enrollmentSignal.aborted).toBe(false);
    enrollmentResponse.resolve(jsonResponse({ profile_id: "voice-001" }, 201));
    await expect(pending).resolves.toEqual({ profile_id: "voice-001" });
    vi.useRealTimers();
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
    await registerAccount("memorykeeper", "safe-passphrase", "朋友");
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
          display_name: "朋友",
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

  it("validates and sends the immutable digital-self lifecycle contract", async () => {
    const digest = "a".repeat(64);
    const version = (status, versionNumber = 1) => ({
      version_id: versionNumber === 1 ? "digital-self-1" : "digital-self-2",
      version_number: versionNumber,
      status,
      manifest_sha256: digest,
      manifest: {
        schema_version: "digital-self-manifest-v1",
        compiler_version: "digital-self-compiler-v1",
        policy_version: "digital-self-policy-v1",
        parent_version_id: null,
        rollback_target_version_id: null,
        entries: [],
        source_summary: {
          memory_claim_count: 2,
          persona_trait_count: 1,
          persona_version_id: "persona-1",
          source_summary_sha256: "b".repeat(64),
        },
      },
      source_summary: {
        memory_claim_count: 2,
        persona_trait_count: 1,
        persona_version_id: "persona-1",
        source_summary_sha256: "b".repeat(64),
      },
      parent_version_id: null,
      rollback_target_version_id: null,
      created_at: "2026-07-22T00:00:00+00:00",
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          user_id: "registered-user",
          username: "memorykeeper",
          account_type: "registered",
          access_token: "digital-self-token",
        }, 201),
      )
      .mockResolvedValueOnce(jsonResponse({ items: [version("draft")] }))
      .mockResolvedValueOnce(jsonResponse(version("draft"), 201))
      .mockResolvedValueOnce(jsonResponse(version("draft")))
      .mockResolvedValueOnce(jsonResponse(version("testing")))
      .mockResolvedValueOnce(jsonResponse(version("approved")))
      .mockResolvedValueOnce(jsonResponse(version("frozen")))
      .mockResolvedValueOnce(jsonResponse(version("revoked")))
      .mockResolvedValueOnce(jsonResponse(version("draft", 2), 201));
    vi.stubGlobal("fetch", fetchMock);
    const {
      approveDigitalSelfVersion,
      beginDigitalSelfTesting,
      buildDigitalSelfVersion,
      freezeDigitalSelfVersion,
      getDigitalSelfVersion,
      getDigitalSelfVersions,
      registerAccount,
      revokeDigitalSelfVersion,
      rollbackDigitalSelfVersion,
    } = await import("./api.js");

    await registerAccount("memorykeeper", "safe-passphrase");
    await expect(getDigitalSelfVersions()).resolves.toEqual({
      items: [version("draft")],
    });
    await expect(buildDigitalSelfVersion()).resolves.toEqual(version("draft"));
    await expect(getDigitalSelfVersion("digital-self-1")).resolves.toEqual(
      version("draft"),
    );
    await beginDigitalSelfTesting("digital-self-1", digest);
    await approveDigitalSelfVersion("digital-self-1", "safe-passphrase", digest);
    await freezeDigitalSelfVersion("digital-self-1", "safe-passphrase", digest);
    await revokeDigitalSelfVersion("digital-self-1", "safe-passphrase", digest);
    await rollbackDigitalSelfVersion("digital-self-1", "safe-passphrase", digest);

    expect(fetchMock).toHaveBeenNthCalledWith(
      5,
      "/memoria-api/v1/digital-self/versions/digital-self-1/testing",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ expected_manifest_sha256: digest }),
      }),
    );
    for (const [call, action] of [
      [6, "approve"],
      [7, "freeze"],
      [8, "revoke"],
      [9, "rollback"],
    ]) {
      expect(fetchMock).toHaveBeenNthCalledWith(
        call,
        `/memoria-api/v1/digital-self/versions/digital-self-1/${action}`,
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({
            expected_manifest_sha256: digest,
            password: "safe-passphrase",
          }),
        }),
      );
    }
  });

  it("rejects malformed digital-self responses before the UI can trust them", async () => {
    const sourceSummary = {
      memory_claim_count: 2,
      persona_trait_count: 1,
      persona_version_id: "persona-1",
      source_summary_sha256: "b".repeat(64),
    };
    const valid = {
      version_id: "digital-self-1",
      version_number: 1,
      status: "approved",
      manifest_sha256: "a".repeat(64),
      manifest: {
        schema_version: "digital-self-manifest-v1",
        compiler_version: "digital-self-compiler-v1",
        policy_version: "digital-self-policy-v1",
        parent_version_id: null,
        rollback_target_version_id: null,
        entries: [],
        source_summary: sourceSummary,
      },
      source_summary: sourceSummary,
      parent_version_id: null,
      rollback_target_version_id: null,
      created_at: "2026-07-22T00:00:00+00:00",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          user_id: "registered-user",
          username: "memorykeeper",
          account_type: "registered",
          access_token: "digital-self-token",
        }, 201),
      )
      .mockResolvedValueOnce(jsonResponse({
        items: [{ ...valid, parent_version_id: 1 }],
      }))
      .mockResolvedValueOnce(jsonResponse({
        items: [{
          ...valid,
          manifest: { ...valid.manifest, source_summary: [] },
        }],
      }))
      .mockResolvedValueOnce(jsonResponse({
        items: [{
          ...valid,
          source_summary: { ...sourceSummary, memory_claim_count: 3 },
        }],
      }));
    vi.stubGlobal("fetch", fetchMock);
    const { getDigitalSelfVersions, registerAccount } = await import("./api.js");

    await registerAccount("memorykeeper", "safe-passphrase");
    await expect(getDigitalSelfVersions()).rejects.toThrow(
      "数字分身版本响应无效",
    );
    await expect(getDigitalSelfVersions()).rejects.toThrow(
      "数字分身版本响应无效",
    );
    await expect(getDigitalSelfVersions()).rejects.toThrow(
      "数字分身版本响应无效",
    );
  });

  it("validates all manifest-v2 source counts against the embedded summary", async () => {
    const sourceSummary = {
      memory_claim_count: 2,
      persona_trait_count: 1,
      cognitive_claim_count: 3,
      decision_case_count: 2,
      relationship_profile_count: 1,
      persona_version_id: "persona-1",
      source_summary_sha256: "b".repeat(64),
    };
    const version = {
      version_id: "digital-self-v2",
      version_number: 2,
      status: "draft",
      manifest_sha256: "a".repeat(64),
      manifest: {
        schema_version: "digital-self-manifest-v2",
        compiler_version: "digital-self-compiler-v2",
        policy_version: "digital-self-policy-v2",
        parent_version_id: "digital-self-v1",
        rollback_target_version_id: null,
        entries: [],
        source_summary: sourceSummary,
      },
      source_summary: sourceSummary,
      parent_version_id: "digital-self-v1",
      rollback_target_version_id: null,
      created_at: "2026-07-22T00:00:00+00:00",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({
        user_id: "registered-user",
        username: "memorykeeper",
        account_type: "registered",
        access_token: "digital-self-token",
      }, 201))
      .mockResolvedValueOnce(jsonResponse({ items: [version] }))
      .mockResolvedValueOnce(jsonResponse({
        items: [{
          ...version,
          source_summary: {
            ...sourceSummary,
            decision_case_count: 3,
          },
        }],
      }));
    vi.stubGlobal("fetch", fetchMock);
    const { getDigitalSelfVersions, registerAccount } = await import("./api.js");

    await registerAccount("memorykeeper", "safe-passphrase");
    await expect(getDigitalSelfVersions()).resolves.toEqual({
      items: [version],
    });
    await expect(getDigitalSelfVersions()).rejects.toThrow(
      "数字分身版本响应无效",
    );
  });

  it("accepts a v3 manifest with an exact approved personal voice reference", async () => {
    const sourceSummary = {
      memory_claim_count: 2,
      persona_trait_count: 1,
      cognitive_claim_count: 3,
      decision_case_count: 2,
      relationship_profile_count: 1,
      persona_version_id: "persona-1",
      source_summary_sha256: "b".repeat(64),
      voice_profile: {
        profile_id: "voice-profile-1",
        version_number: 2,
        provider: "volcengine_doubao",
        target_model: "seed-icl-2.0",
        resource_id: "seed-icl-2.0",
        provider_expires_at: "2026-08-01T00:00:00+00:00",
        speaker_sha256: "c".repeat(64),
      },
    };
    const version = {
      version_id: "digital-self-v3",
      version_number: 3,
      status: "approved",
      manifest_sha256: "a".repeat(64),
      manifest: {
        schema_version: "digital-self-manifest-v3",
        compiler_version: "digital-self-compiler-v3",
        policy_version: "digital-self-policy-v3",
        parent_version_id: "digital-self-v2",
        rollback_target_version_id: null,
        entries: [],
        source_summary: sourceSummary,
      },
      source_summary: sourceSummary,
      parent_version_id: "digital-self-v2",
      rollback_target_version_id: null,
      created_at: "2026-07-22T00:00:00+00:00",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          user_id: "registered-user",
          username: "memorykeeper",
          account_type: "registered",
          access_token: "digital-self-token",
        }, 201),
      )
      .mockResolvedValueOnce(jsonResponse({ items: [version] }));
    vi.stubGlobal("fetch", fetchMock);
    const { getDigitalSelfVersions, registerAccount } = await import("./api.js");

    await registerAccount("memorykeeper", "safe-passphrase");
    await expect(getDigitalSelfVersions()).resolves.toEqual({
      items: [version],
    });
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

    await createSession("anonymous-user", "qwen_omni", "natural-chat-task");
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
        learning_task_id: "natural-chat-task",
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
            missing: ["self_preview_runtime"],
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

  it("shows a safe message when LiveKit credentials are not configured", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({
        user_id: "owner",
        username: "memorykeeper",
        account_type: "registered",
        access_token: "short-token",
      }, 201))
      .mockResolvedValueOnce(jsonResponse({
        detail: { code: "livekit_credentials_missing" },
      }, 503));
    vi.stubGlobal("fetch", fetchMock);
    const { createSession, registerAccount } = await import("./api.js");

    await registerAccount("memorykeeper", "safe-passphrase");
    await expect(createSession("owner")).rejects.toMatchObject({
      message: "语音服务尚未配置，请联系管理员。",
      status: 503,
      code: "livekit_credentials_missing",
    });
  });

  it("uses strict growth-map endpoints without percentage fields", async () => {
    const task = {
      task_id: "task-1",
      kind: "life_interview",
      status: "active",
      revision: 1,
      prompt_id: null,
      prompt: "哪段经历最影响你？",
      created_at: "2026-07-22T00:00:00Z",
      updated_at: "2026-07-22T00:00:00Z",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({
        user_id: "owner",
        account_type: "registered",
        access_token: "growth-token",
      }))
      .mockResolvedValueOnce(jsonResponse({
        dimensions: [{
          key: "expression",
          status: "supported",
          adopted_sources: [{
            event_id: "source-1",
            kind: "owner_statement",
            event_type: "owner.action_recorded",
            target_kind: "memory_claim",
            target_id: "claim-1",
            label: "我会先确认事实",
            weight: "strong",
            occurred_at: "2026-07-21T00:00:00Z",
          }],
          rejected_reason_counts: {},
          conflicts: [],
          recent_changes: [],
          dependency_blockers: [],
          version_readiness: { status: "not_built" },
        }],
      }))
      .mockResolvedValueOnce(jsonResponse({ items: [task] }))
      .mockResolvedValueOnce(jsonResponse(task, 201))
      .mockResolvedValueOnce(jsonResponse({ ...task, revision: 2, status: "active" }))
      .mockResolvedValueOnce(jsonResponse({ ...task, revision: 3, status: "completed" }))
      .mockResolvedValueOnce(jsonResponse({ status: "recorded" }));
    vi.stubGlobal("fetch", fetchMock);
    const {
      createGrowthTask,
      getGrowthOverview,
      getGrowthTasks,
      registerAccount,
      respondGrowthTask,
      reviewGrowthOwnerAction,
      transitionGrowthTask,
    } = await import("./api.js");

    await registerAccount("growth-owner", "safe-passphrase");
    await expect(getGrowthOverview()).resolves.toEqual(expect.objectContaining({
      dimensions: [expect.objectContaining({
        key: "expression",
        status: "supported",
        adopted_sources: [expect.objectContaining({
          event_type: "owner.action_recorded",
          target_kind: "memory_claim",
        })],
      })],
    }));
    await expect(getGrowthTasks()).resolves.toEqual({ items: [task] });
    await createGrowthTask("event-1", "life_interview");
    await respondGrowthTask("task-1", "event-1", 1, "我的回答", {
      options: ["直接上线", "先验证"],
      constraints: ["预算有限"],
      chosen_option: "先验证",
      rejected_options: ["直接上线"],
      outcome: "避免返工",
      reflection: "先验证更稳妥。",
      still_endorsed: true,
    });
    await transitionGrowthTask("task-1", "event-1", "completed", 2);
    await reviewGrowthOwnerAction("event-1", "not_me", "memory_claim", "trait-1");

    expect(fetchMock.mock.calls[2][0]).toBe("/memoria-api/v1/growth/tasks");
    expect(JSON.parse(fetchMock.mock.calls[3][1].body)).toEqual({
      event_id: "event-1",
      kind: "life_interview",
    });
    expect(JSON.parse(fetchMock.mock.calls[4][1].body)).toEqual({
      event_id: "event-1",
      expected_revision: 1,
      answer: "我的回答",
      options: ["直接上线", "先验证"],
      constraints: ["预算有限"],
      chosen_option: "先验证",
      rejected_options: ["直接上线"],
      outcome: "避免返工",
      reflection: "先验证更稳妥。",
      still_endorsed: true,
    });
    expect(JSON.parse(fetchMock.mock.calls[5][1].body)).toEqual({
      event_id: "event-1",
      to_status: "completed",
      expected_revision: 2,
    });
    expect(JSON.parse(fetchMock.mock.calls[6][1].body)).toEqual({
      event_id: "event-1",
      action: "not_me",
      target_kind: "memory_claim",
      target_id: "trait-1",
    });
  });

  it("rejects malformed structured growth responses before sending them", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { respondGrowthTask } = await import("./api.js");

    expect(() => respondGrowthTask("task-1", "event-1", 1, "", {})).toThrow(
      "请先写下你的回答",
    );
    expect(() => respondGrowthTask("task-1", "event-1", 1, "", {
      options: ["可行方案", ""],
    })).toThrow("成长任务选项无效");
    expect(() => respondGrowthTask("task-1", "event-1", 1, "", {
      still_endorsed: "yes",
    })).toThrow("成长任务结构化回答无效");
    expect(() => respondGrowthTask("task-1", "event-1", 1, "", {
      unknown: true,
    })).toThrow("成长任务结构化回答无效");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects malformed growth-map status values", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({
        user_id: "owner",
        account_type: "registered",
        access_token: "growth-token",
      }))
      .mockResolvedValueOnce(jsonResponse({
        dimensions: [{
          key: "expression",
          status: "unsupported",
          adopted_sources: [],
          rejected_reason_counts: {},
          conflicts: [],
          recent_changes: [],
          dependency_blockers: [],
          version_readiness: { status: "not_built" },
        }],
      }));
    vi.stubGlobal("fetch", fetchMock);
    const { getGrowthOverview, registerAccount } = await import("./api.js");
    await registerAccount("growth-owner", "safe-passphrase");
    await expect(getGrowthOverview()).rejects.toThrow("成长地图响应无效");
  });

  it("validates and reviews cognitive, decision and relationship material", async () => {
    const source = {
      source_event_id: "source-1",
      relation: "support",
      adopted: true,
      negative: false,
      speaker_class: "owner",
      occurred_at: "2026-07-22T00:00:00Z",
      excerpt: "我会先确认事实。",
    };
    const common = {
      account_id: "owner",
      sharing_scope: "private",
      unresolved_conflict: false,
      sources: [source],
      owner_reviewed_at: null,
      step_up_verified: false,
      effective: false,
      effective_reasons: ["not_approved"],
      created_at: "2026-07-22T00:00:00Z",
    };
    const claim = {
      ...common,
      kind: "cognitive_claim",
      claim_id: "claim-1",
      claim_type: "belief",
      statement: "我会先确认事实。",
      context: "",
      confidence: 0.9,
      status: "candidate",
      version: 2,
      updated_at: "2026-07-22T00:00:00Z",
    };
    const decision = {
      ...common,
      kind: "decision_case",
      case_id: "case-1",
      decision_kind: "hypothetical",
      context: "如果重新选择",
      options: ["先确认"],
      constraints: [],
      chosen_option: "先确认",
      rejected_options: [],
      outcome: "",
      reflection: "",
      still_endorsed: true,
      status: "candidate",
      version: 2,
      updated_at: "2026-07-22T00:00:00Z",
    };
    const relationship = {
      ...common,
      kind: "relationship_profile",
      profile_id: "profile-1",
      version_number: 1,
      person_id: "person-1",
      relationship_id: "relationship-1",
      salutation: "梅姐",
      tone: "坦诚",
      advice_style: "先听再建议",
      boundaries: ["不谈财务"],
      status: "candidate",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({
        user_id: "owner",
        account_type: "registered",
        access_token: "self-model-token",
      }))
      .mockResolvedValueOnce(jsonResponse({
        claims: [claim],
        decision_cases: [decision],
        relationship_profiles: [relationship],
      }))
      .mockResolvedValueOnce(jsonResponse({
        ...claim,
        status: "confirmed",
        effective: true,
        effective_reasons: [],
        owner_reviewed_at: "2026-07-22T01:00:00Z",
        version: 3,
        updated_at: "2026-07-22T01:00:00Z",
      }))
      .mockResolvedValueOnce(jsonResponse({
        ...decision,
        status: "confirmed",
        effective_reasons: ["hypothetical_decision"],
        owner_reviewed_at: "2026-07-22T01:00:00Z",
        version: 3,
        updated_at: "2026-07-22T01:00:00Z",
      }))
      .mockResolvedValueOnce(jsonResponse({
        ...relationship,
        status: "approved",
        effective: true,
        effective_reasons: [],
        owner_reviewed_at: "2026-07-22T01:00:00Z",
        step_up_verified: true,
      }));
    vi.stubGlobal("fetch", fetchMock);
    const {
      getSelfModel,
      registerAccount,
      reviewSelfModelClaim,
      reviewSelfModelDecisionCase,
      reviewSelfModelRelationshipProfile,
    } = await import("./api.js");

    await registerAccount("self-model-owner", "safe-passphrase");
    await expect(getSelfModel()).resolves.toEqual({
      claims: [claim],
      decision_cases: [decision],
      relationship_profiles: [relationship],
    });
    await reviewSelfModelClaim("claim-1", "confirmed", 2, "confirm-claim");
    await reviewSelfModelDecisionCase("case-1", "confirmed", 2, "confirm-case");
    await reviewSelfModelRelationshipProfile(
      "profile-1",
      1,
      "approved",
      "candidate",
      "approve-profile",
      "safe-passphrase",
    );

    expect(fetchMock.mock.calls[2][0]).toBe(
      "/memoria-api/v1/self-model/claims/claim-1/review",
    );
    expect(JSON.parse(fetchMock.mock.calls[4][1].body)).toEqual({
      status: "approved",
      expected_status: "candidate",
      idempotency_key: "approve-profile",
      password: "safe-passphrase",
    });
  });

  it("records an owner counterexample for a cognitive claim", async () => {
    const claim = {
      account_id: "owner",
      kind: "cognitive_claim",
      claim_id: "claim-1",
      claim_type: "value",
      statement: "家庭安全高于短期收益。",
      context: "",
      confidence: 0.9,
      sharing_scope: "private",
      status: "candidate",
      unresolved_conflict: false,
      sources: [{
        source_event_id: "counterexample-1",
        relation: "counterexample",
        adopted: false,
        negative: false,
        speaker_class: "owner",
        occurred_at: "2026-07-22T00:00:00Z",
        excerpt: "当家人已经安全时，我愿意尝试。",
      }],
      owner_reviewed_at: null,
      step_up_verified: false,
      effective: false,
      effective_reasons: ["not_approved"],
      version: 3,
      created_at: "2026-07-22T00:00:00Z",
      updated_at: "2026-07-22T00:00:00Z",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({
        user_id: "owner",
        account_type: "registered",
        access_token: "self-model-token",
      }))
      .mockResolvedValueOnce(jsonResponse(claim));
    vi.stubGlobal("fetch", fetchMock);
    const {
      addSelfModelClaimCounterexample,
      registerAccount,
    } = await import("./api.js");

    await registerAccount("self-model-owner", "safe-passphrase");
    await expect(
      addSelfModelClaimCounterexample(
        "claim-1",
        "家人安全且风险可控时，我也愿意尝试。",
        2,
        "counterexample-1",
      ),
    ).resolves.toEqual(claim);

    expect(fetchMock.mock.calls[1][0]).toBe(
      "/memoria-api/v1/self-model/claims/claim-1/counterexamples",
    );
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({
      event_id: "counterexample-1",
      expected_version: 2,
      text: "家人安全且风险可控时，我也愿意尝试。",
    });
  });

  it("freezes and validates a self-preview grant, session, sources, feedback, and fidelity", async () => {
    const digest = "c".repeat(64);
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          user_id: "owner",
          username: "owner",
          account_type: "registered",
          access_token: "preview-token",
        }, 201),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          status: "available",
          conversational: true,
          registered_owner: true,
          active_owner_voice: true,
          missing: [],
          versions: [{
            version_id: "self-v1",
            version_number: 1,
            status: "approved",
            manifest_sha256: digest,
            version_stale: false,
            fidelity_verdict: "approve",
            fidelity_eligible: true,
            preview_eligible: true,
          }],
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          grant_id: "grant-1",
          account_id: "owner",
          version_id: "self-v1",
          manifest_sha256: digest,
          perspective: "child",
          status: "active",
          expires_at: "2026-07-23T10:00:00Z",
          created_at: "2026-07-23T09:00:00Z",
          used_at: null,
          revoked_at: null,
          simulation_only: true,
          legacy_authority: false,
        }, 201),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          session_id: "preview-session",
          voice_backend: "cascade",
          interaction: {
            interaction_mode: "self_preview",
            mode_policy_version: "s7-v1",
            digital_self_version_id: "self-v1",
            manifest_sha256: digest,
            preview_grant_id: "grant-1",
            perspective: "child",
            simulated_output: true,
            history_eligible: false,
            owner_projection_eligible: false,
            companion_style_id: null,
            companion_style_version: null,
            voice_profile_id: null,
            voice_profile_version: null,
            voice_provider: null,
            voice_model: null,
            voice_resource_id: null,
            voice_provider_expires_at: null,
            voice_speaker_sha256: null,
            fallback_voice_profile_id: "warm_companion",
            fallback_voice_provider: "volcengine_doubao",
            fallback_voice_model: "seed-tts-2.0",
            fallback_voice_resource_id: "seed-tts-2.0",
            relationship_profile_id: null,
            legacy_grant_id: null,
            capabilities: {
              conversation: true,
              private_memory: false,
              persona: false,
              persona_low_sensitivity: false,
              tools: false,
              history: false,
              learning: false,
              voice_profile: true,
            },
          },
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          session_id: "preview-session",
          turn_id: 2,
          generation_id: 4,
          tool_epoch: 1,
          items: [{
            kind: "memory_claim",
            item_id: "memory-1",
            source_event_id: "event-1",
            excerpt: "我在雨天喜欢散步。",
          }],
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          feedback_id: "feedback-1",
          version_stale: true,
          rebuild_required: true,
        }, 201),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          evaluation_id: "eval-1",
          version_id: "self-v1",
          manifest_sha256: digest,
          status: "active",
          verdict: null,
          created_at: "2026-07-23T09:00:00Z",
          completed_at: null,
          mapping_hidden: true,
          trials: [{
            trial_id: "trial-1",
            category: "unknown",
            prompt: "未知问题",
            slot_a: "回答 A",
            slot_b: "回答 B",
            available: true,
            coverage_gap: null,
            preferred_slot: null,
          }],
          summary: {},
        }, 201),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          evaluation_id: "eval-1",
          version_id: "self-v1",
          manifest_sha256: digest,
          status: "active",
          verdict: null,
          created_at: "2026-07-23T09:00:00Z",
          completed_at: null,
          mapping_hidden: true,
          trials: [{
            trial_id: "trial-1",
            category: "unknown",
            prompt: "未知问题",
            slot_a: "回答 A",
            slot_b: "回答 B",
            available: true,
            coverage_gap: null,
            preferred_slot: "a",
          }],
          summary: {},
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const {
      chooseFidelityTrial,
      createSession,
      getSelfPreviewCapability,
      getSelfPreviewSources,
      issueSelfPreviewGrant,
      registerAccount,
      startFidelityEvaluation,
      submitSelfPreviewFeedback,
    } = await import("./api.js");

    await registerAccount("owner", "safe-passphrase");
    await expect(getSelfPreviewCapability()).resolves.toMatchObject({
      status: "available",
      versions: [{ version_id: "self-v1" }],
    });
    const grant = await issueSelfPreviewGrant({
      versionId: "self-v1",
      manifestSha256: digest,
      perspective: "child",
      password: "safe-passphrase",
      idempotencyKey: "grant-idempotency",
    });
    expect(grant.grant_id).toBe("grant-1");
    await expect(
      createSession("owner", "cascade", null, {
        interactionMode: "self_preview",
        previewGrantId: grant.grant_id,
      }),
    ).resolves.toMatchObject({
      session_id: "preview-session",
      interaction: {
        interaction_mode: "self_preview",
        perspective: "child",
      },
    });
    await expect(
      getSelfPreviewSources({
        sessionId: "preview-session",
        turnId: 2,
        generationId: 4,
        toolEpoch: 1,
      }),
    ).resolves.toMatchObject({
      items: [{ source_event_id: "event-1" }],
    });
    await expect(
      submitSelfPreviewFeedback({
        sessionId: "preview-session",
        turnId: 2,
        generationId: 4,
        toolEpoch: 1,
        versionId: "self-v1",
        manifestSha256: digest,
        action: "not_like_me",
        targetSourceEventIds: ["event-1"],
        eventId: "feedback-event-1",
        idempotencyKey: "feedback-idempotency-1",
      }),
    ).resolves.toMatchObject({ version_stale: true });
    const evaluation = await startFidelityEvaluation({
      versionId: "self-v1",
      manifestSha256: digest,
      password: "safe-passphrase",
      idempotencyKey: "fidelity-1",
    });
    expect(evaluation.trials[0].slot_a).toBe("回答 A");
    await expect(
      chooseFidelityTrial({
        evaluationId: "eval-1",
        trialId: "trial-1",
        preferredSlot: "a",
      }),
    ).resolves.toMatchObject({
      trials: [{ preferred_slot: "a" }],
    });

    expect(JSON.parse(fetchMock.mock.calls[3][1].body)).toMatchObject({
      interaction_mode: "self_preview",
      preview_grant_id: "grant-1",
      learning_task_id: null,
    });
    expect(fetchMock.mock.calls[4][0]).toContain(
      "/v1/digital-self/preview-sessions/preview-session/turns/2/generations/4/sources",
    );
  });
});

describe("legacy Control API client", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.restoreAllMocks();
    window.localStorage.clear();
  });

  it("sends the exact grant lifecycle and relationship-shell preference contract", async () => {
    const grant = {
      grant_id: "legacy-grant-1",
      owner_account_id: "owner-1",
      owner_username: "owner",
      grantee_account_id: "grantee-1",
      grantee_username: "family-member",
      version_id: "digital-self-7",
      version_number: 7,
      manifest_sha256: "a".repeat(64),
      relationship_profile_id: "relationship-1",
      relationship_profile_version: 3,
      allowed_items: [{ kind: "memory_claim", item_id: "memory-1" }],
      visibility: "family",
      scope_sha256: "b".repeat(64),
      grant_snapshot_sha256: "c".repeat(64),
      voice_allowed: false,
      expires_at: "2026-08-01T00:00:00Z",
      activated_at: null,
      revoked_at: null,
      revision: 1,
      created_at: "2026-07-23T00:00:00Z",
      status: "pending",
    };
    const preferences = {
      shell_id: "shell-1",
      grant_id: "legacy-grant-1",
      owner_account_id: "owner-1",
      grantee_account_id: "grantee-1",
      revision: 4,
      preferences: {
        preferred_response_length: "balanced",
        question_frequency: "occasional",
      },
      created_at: "2026-07-23T00:00:00Z",
      updated_at: "2026-07-23T00:00:00Z",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({
        user_id: "owner-1",
        username: "owner",
        account_type: "registered",
        access_token: "legacy-token",
      }, 201))
      .mockResolvedValueOnce(jsonResponse({ role: "owner", items: [grant] }))
      .mockResolvedValueOnce(jsonResponse(grant, 201))
      .mockResolvedValueOnce(jsonResponse({
        ...grant,
        status: "active",
        revision: 2,
        activated_at: "2026-07-23T01:00:00Z",
      }))
      .mockResolvedValueOnce(jsonResponse({
        ...grant,
        status: "revoked",
        revision: 3,
        revoked_at: "2026-07-23T02:00:00Z",
      }))
      .mockResolvedValueOnce(jsonResponse(preferences))
      .mockResolvedValueOnce(jsonResponse({
        ...preferences,
        revision: 5,
        preferences: {
          preferred_response_length: "brief",
          question_frequency: "rare",
        },
      }));
    vi.stubGlobal("fetch", fetchMock);
    const {
      activateLegacyGrant,
      createLegacyGrant,
      getLegacyGrants,
      getLegacyShellPreferences,
      registerAccount,
      revokeLegacyGrant,
      updateLegacyShellPreferences,
    } = await import("./api.js");

    await registerAccount("owner", "safe-passphrase");
    await expect(getLegacyGrants("owner")).resolves.toMatchObject({
      role: "owner",
      items: [{ grant_id: "legacy-grant-1", status: "pending" }],
    });
    const createBody = {
      grantee_username: "family-member",
      version_id: "digital-self-7",
      relationship_profile_id: "relationship-1",
      allowed_items: [{ kind: "memory_claim", item_id: "memory-1" }],
      voice_allowed: false,
      expires_at: "2026-08-01T00:00:00Z",
      password: "safe-passphrase",
      idempotency_key: "create-legacy-1",
    };
    await expect(createLegacyGrant(createBody)).resolves.toMatchObject({
      grant_snapshot_sha256: "c".repeat(64),
    });
    const transitionBody = {
      expected_grant_snapshot_sha256: "c".repeat(64),
      password: "safe-passphrase",
      idempotency_key: "transition-legacy-1",
    };
    await expect(
      activateLegacyGrant("legacy-grant-1", transitionBody),
    ).resolves.toMatchObject({ status: "active" });
    await expect(
      revokeLegacyGrant("legacy-grant-1", {
        ...transitionBody,
        idempotency_key: "revoke-legacy-1",
      }),
    ).resolves.toMatchObject({ status: "revoked" });
    await expect(getLegacyShellPreferences("shell-1")).resolves.toMatchObject({
      revision: 4,
    });
    const preferenceBody = {
      expected_revision: 4,
      preferred_response_length: "brief",
      question_frequency: "rare",
      idempotency_key: "preferences-legacy-1",
    };
    await expect(
      updateLegacyShellPreferences("shell-1", preferenceBody),
    ).resolves.toMatchObject({ revision: 5 });

    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/memoria-api/v1/legacy/grants?role=owner",
      expect.any(Object),
    );
    expect(JSON.parse(fetchMock.mock.calls[2][1].body)).toEqual(createBody);
    expect(JSON.parse(fetchMock.mock.calls[3][1].body)).toEqual(transitionBody);
    expect(JSON.parse(fetchMock.mock.calls[6][1].body)).toEqual(preferenceBody);
  });

  it("rejects unknown legacy roles/statuses and incomplete trust-boundary responses", async () => {
    const validGrant = {
      grant_id: "legacy-grant-1",
      owner_account_id: "owner-1",
      owner_username: "owner",
      grantee_account_id: "grantee-1",
      grantee_username: "family-member",
      version_id: "digital-self-7",
      version_number: 7,
      manifest_sha256: "a".repeat(64),
      relationship_profile_id: "relationship-1",
      relationship_profile_version: 3,
      allowed_items: [{ kind: "memory_claim", item_id: "memory-1" }],
      visibility: "family",
      scope_sha256: "b".repeat(64),
      grant_snapshot_sha256: "c".repeat(64),
      voice_allowed: false,
      expires_at: "2026-08-01T00:00:00Z",
      activated_at: null,
      revoked_at: null,
      revision: 1,
      created_at: "2026-07-23T00:00:00Z",
      status: "pending",
    };
    const { grant_snapshot_sha256: _digest, ...missingDigest } = validGrant;
    const { owner_username: _owner, ...missingOwner } = validGrant;
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({
        user_id: "owner-1",
        username: "owner",
        account_type: "registered",
        access_token: "legacy-token",
      }, 201))
      .mockResolvedValueOnce(jsonResponse({ role: "delegate", items: [] }))
      .mockResolvedValueOnce(jsonResponse({
        role: "owner",
        items: [{ ...validGrant, status: "disabled" }],
      }))
      .mockResolvedValueOnce(jsonResponse({ role: "owner", items: [missingDigest] }))
      .mockResolvedValueOnce(jsonResponse({ role: "owner", items: [missingOwner] }))
      .mockResolvedValueOnce(jsonResponse({
        shell_id: "shell-1",
        grant_id: "legacy-grant-1",
        owner_account_id: "owner-1",
        grantee_account_id: "grantee-1",
        revision: 4,
        preferences: {
          preferred_response_length: "unbounded",
          question_frequency: "occasional",
        },
        created_at: "2026-07-23T00:00:00Z",
        updated_at: "2026-07-23T00:00:00Z",
      }));
    vi.stubGlobal("fetch", fetchMock);
    const { getLegacyGrants, getLegacyShellPreferences, registerAccount } =
      await import("./api.js");

    await registerAccount("owner", "safe-passphrase");
    expect(() => getLegacyGrants("delegate")).toThrow("传承授权视角无效");
    await expect(getLegacyGrants("owner")).rejects.toThrow("传承授权列表响应无效");
    await expect(getLegacyGrants("owner")).rejects.toThrow("传承授权响应无效");
    await expect(getLegacyGrants("owner")).rejects.toThrow("传承授权摘要无效");
    await expect(getLegacyGrants("owner")).rejects.toThrow("传承授权字段无效");
    await expect(getLegacyShellPreferences("shell-1")).rejects.toThrow(
      "关系外壳偏好响应无效",
    );
  });

  it("starts a legacy session with only the grant id and verifies the frozen boundary", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({
        user_id: "grantee-1",
        username: "family-member",
        account_type: "registered",
        access_token: "legacy-token",
      }, 201))
      .mockResolvedValueOnce(jsonResponse({
        session_id: "legacy-session-1",
        voice_backend: "cascade",
        interaction: {
          interaction_mode: "legacy",
          mode_policy_version: "s9-v1",
          policy_scope: "session",
          actor_account_id: "grantee-1",
          resource_owner_account_id: "owner-1",
          digital_self_version_id: "digital-self-7",
          manifest_sha256: "a".repeat(64),
          preview_grant_id: null,
          perspective: null,
          relationship_profile_id: "relationship-1",
          relationship_profile_version: 3,
          legacy_actor_role: "grantee",
          legacy_grantee_account_id: "grantee-1",
          legacy_grant_id: "legacy-grant-1",
          legacy_shell_id: "shell-1",
          legacy_grant_snapshot_sha256: "b".repeat(64),
          legacy_scope_sha256: "c".repeat(64),
          legacy_voice_allowed: false,
          legacy_expires_at: "2026-08-01T00:00:00Z",
          companion_style_id: null,
          companion_style_version: null,
          voice_profile_id: null,
          voice_profile_version: null,
          voice_provider: null,
          voice_model: null,
          voice_resource_id: null,
          voice_provider_expires_at: null,
          voice_speaker_sha256: null,
          fallback_voice_profile_id: "warm_companion",
          fallback_voice_provider: "volcengine_doubao",
          fallback_voice_model: "seed-tts-2.0",
          fallback_voice_resource_id: "seed-tts-2.0",
          simulated_output: true,
          history_eligible: false,
          owner_projection_eligible: false,
          capabilities: {
            conversation: true,
            private_memory: false,
            persona: false,
            persona_low_sensitivity: false,
            tools: false,
            history: false,
            learning: false,
            voice_profile: false,
          },
        },
      }));
    vi.stubGlobal("fetch", fetchMock);
    const { createSession, registerAccount } = await import("./api.js");

    await registerAccount("family-member", "safe-passphrase");
    await expect(createSession("grantee-1", "qwen-omni", "task-1", {
      interactionMode: "legacy",
      legacyGrantId: "legacy-grant-1",
    })).resolves.toMatchObject({
      session_id: "legacy-session-1",
      interaction: {
        interaction_mode: "legacy",
        legacy_actor_role: "grantee",
        legacy_shell_id: "shell-1",
      },
    });
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toMatchObject({
      user_id: "grantee-1",
      voice_backend: "cascade",
      interaction_mode: "legacy",
      legacy_grant_id: "legacy-grant-1",
      learning_task_id: null,
    });
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).not.toHaveProperty(
      "preview_grant_id",
    );
  });
});
