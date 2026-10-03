const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const api = require("../utils/api");
const { CONTROL_API_BASE_URL } = require("../config");

const storage = {};
let pageDefinition = null;

async function withWx(fn) {
  const previousWx = global.wx;
  const previousGetApp = global.getApp;
  const previousPage = global.Page;
  global.wx = {
    getStorageSync: (key) => storage[key],
    setStorageSync: (key, value) => {
      storage[key] = value;
    },
    removeStorageSync: (key) => {
      delete storage[key];
    },
    showToast() {},
    navigateTo() {},
    showModal(options) {
      options.success?.({ confirm: true });
    },
  };
  global.getApp = () => ({
    globalData: {
      identity: { user_id: "person_owner", display_name: "主人" },
      accessToken: "t",
      accessTokenExpiresAt: Date.now() + 3600_000,
      authEpoch: 0,
    },
    subscribeAuthCleared: () => () => {},
  });
  global.Page = (definition) => {
    pageDefinition = definition;
  };
  try {
    return await fn();
  } finally {
    if (previousWx === undefined) delete global.wx;
    else global.wx = previousWx;
    if (previousGetApp === undefined) delete global.getApp;
    else global.getApp = previousGetApp;
    if (previousPage === undefined) delete global.Page;
    else global.Page = previousPage;
  }
}

function loadPage(relativePath) {
  const resolved = require.resolve(relativePath);
  delete require.cache[resolved];
  require(resolved);
  return pageDefinition;
}

function instantiate(definition) {
  const instance = { ...definition };
  instance.data = JSON.parse(JSON.stringify(definition.data));
  instance.setData = (updates) => {
    for (const [key, value] of Object.entries(updates)) {
      const parts = key.replace(/\[(\d+)\]/g, ".$1").split(".").filter(Boolean);
      let cursor = instance.data;
      for (let index = 0; index < parts.length - 1; index += 1) {
        const part = parts[index];
        if (typeof cursor[part] !== "object" || cursor[part] === null) {
          cursor[part] = /^\d+$/.test(parts[index + 1]) ? [] : {};
        }
        cursor = cursor[part];
      }
      cursor[parts[parts.length - 1]] = value;
    }
  };
  return instance;
}

function stubApi(overrides) {
  const originals = {};
  for (const [key, value] of Object.entries(overrides)) {
    originals[key] = api[key];
    api[key] = value;
  }
  return () => {
    for (const [key, value] of Object.entries(originals)) {
      if (value === undefined) delete api[key];
      else api[key] = value;
    }
  };
}

function allowedGate() {
  return { allowed: true, reason: "allowed", profile: { valid: true } };
}

test("getConversationReview and reviewMemoryClaim use the canonical endpoints", async () => {
  await withWx(async () => {
    const requests = [];
    global.wx.request = (options) => {
      requests.push(options);
      options.success({ statusCode: 200, data: {} });
    };

    await api.getConversationReview();
    assert.equal(requests[0].url, `${CONTROL_API_BASE_URL}/v1/archive/conversation-review`);
    assert.equal(requests[0].method, "GET");

    await api.reviewMemoryClaim("claim_abc", "confirm");
    assert.equal(
      requests[1].url,
      `${CONTROL_API_BASE_URL}/v1/archive/memories/claim_abc/review`,
    );
    assert.equal(requests[1].method, "POST");
    assert.deepEqual(requests[1].data, { action: "confirm" });

    await assert.rejects(api.reviewMemoryClaim("claim_abc", "reject"), TypeError);
    await assert.rejects(api.reviewMemoryClaim(""), TypeError);
    await assert.rejects(api.reviewMemoryClaim(" claim_1 "), TypeError);
  });
});

test("conversation session APIs validate parameters and keep session ids encoded", async () => {
  await withWx(async () => {
    const requests = [];
    global.wx.request = (options) => {
      requests.push(options);
      options.success({ statusCode: 200, data: { items: [], turns: [] } });
    };

    await api.getConversationSessions(10);
    await api.getConversationHistory("session/a", 20);
    assert.equal(
      requests[0].url,
      `${CONTROL_API_BASE_URL}/v1/archive/conversation-sessions?limit=10`,
    );
    assert.equal(
      requests[1].url,
      `${CONTROL_API_BASE_URL}/v1/archive/conversation-history?session_id=session%2Fa&turn_limit=20`,
    );
    await assert.rejects(api.getConversationSessions(0), TypeError);
    await assert.rejects(api.getConversationHistory(" session"), TypeError);
    await assert.rejects(api.getConversationHistory("session", 51), TypeError);
  });
});

test("conversation sessions are displayed without hiding the daily review when unavailable", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/memory/index");
    const page = instantiate(definition);
    let historySession = "";
    const restore = stubApi({
      currentIdentity: () => ({ user_id: "person_owner" }),
      isAuthEpochCurrent: () => true,
      requireRuntimeCapability: async () => allowedGate(),
      getMemoryDays: async () => ({ items: [] }),
      getConversationReview: async () => ({
        actual_heard: [],
        memory_candidates: [],
        confirmed_memories: [],
      }),
      getConversationSessions: async () => ({
        items: [
          {
            session_id: "session/1",
            occurred_at: "2026-09-22T10:20:00Z",
            turn_count: 2,
            preview: "明天记得去公园",
          },
        ],
      }),
      getConversationHistory: async (sessionId) => {
        historySession = sessionId;
        return {
          turns: [{ turn_id: 1, owner_text: "你好", assistant_text: "你好呀", assistant_approximate: true }],
        };
      },
    });
    try {
      await page.loadDays();
      assert.equal(page.data.days.length, 0);
      assert.deepEqual(page.data.conversationSessions, [
        {
          session_id: "session/1",
          occurred_at: "2026-09-22T10:20:00Z",
          occurred_label: page._formatConversationTime("2026-09-22T10:20:00Z"),
          turn_count: 2,
          preview: "明天记得去公园",
        },
      ]);
      await page.openConversation({
        currentTarget: { dataset: { sessionId: "session/1" } },
      });
      assert.equal(historySession, "session/1");
      assert.equal(page.data.conversationTurns[0].assistant_approximate, true);
    } finally {
      restore();
    }
  });
});

test("late conversation detail after logout clears the transient selection state", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/memory/index");
    const page = instantiate(definition);
    let lastEpoch = 0;
    let releaseHistory;
    const historyReady = new Promise((resolve) => {
      releaseHistory = resolve;
    });
    const restore = stubApi({
      currentIdentity: () => ({ user_id: "person_owner" }),
      isAuthEpochCurrent: (epoch) => epoch === lastEpoch,
      getConversationHistory: async () => historyReady,
    });
    try {
      const pending = page.openConversation({
        currentTarget: { dataset: { sessionId: "session-late" } },
      });
      await new Promise((resolve) => setImmediate(resolve));
      assert.equal(page.data.conversationLoading, true);
      lastEpoch = 1;
      releaseHistory({ turns: [{ owner_text: "不应展示" }] });
      await pending;
      assert.equal(page.data.conversationLoading, false);
      assert.equal(page.data.selectedConversationSessionId, "");
      assert.deepEqual(page.data.conversationTurns, []);
    } finally {
      restore();
    }
  });
});

test("conversation review is normalized into three partitions and candidates never enter confirmed", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/memory/index");
    const page = instantiate(definition);
    const restore = stubApi({
      currentIdentity: () => ({ user_id: "person_owner" }),
      isAuthEpochCurrent: () => true,
      requireRuntimeCapability: async () => allowedGate(),
      getMemoryDays: async () => ({ items: [] }),
      getConversationReview: async () => ({
        actual_heard: [
          {
            event_id: "ev_1",
            occurred_at: "2026-08-17T10:00:00Z",
            session_id: "ses_1",
            turn_id: "turn_1",
            generation_id: "gen_1",
            text: "明天记得去公园",
            approximate: false,
          },
          {
            event_id: "ev_2",
            occurred_at: "2026-08-17T10:01:00Z",
            text: "好的，我会提醒你",
          },
          { event_id: "ev_3", occurred_at: "2026-08-17T10:02:00Z", text: "" },
          { event_id: "ev_4", occurred_at: "2026-08-17T10:03:00Z" },
        ],
        memory_candidates: [
          {
            claim_id: "claim_1",
            value: "喜欢清晨散步",
            status: "pending",
            reason: "新出现的偏好",
            domain_category: "habit",
            memory_kind: "preference",
            conflict_state: "",
          },
        ],
        confirmed_memories: [
          {
            memory_id: "mem_1",
            kind: "memory",
            title: "散步偏好",
            snippet: "主人常去公园散步",
            status: "confirmed",
            domain_category: "habit",
            memory_kind: "preference",
            occurred_at: "2026-08-16",
          },
        ],
      }),
    });
    try {
      await page.loadDays();
      assert.equal(page.data.days.length, 0);

      // 空文本与缺失文本的听到事件按展示过滤；缺失 approximate 默认近似。
      assert.equal(page.data.heardTurns.length, 2);
      assert.equal(page.data.heardTurns[0].approximate, false);
      assert.equal(page.data.heardTurns[1].approximate, true, "缺失近似标记默认按近似展示");
      assert.deepEqual(page.data.heardTurns[0], {
        event_id: "ev_1",
        occurred_at: "2026-08-17T10:00:00Z",
        session_id: "ses_1",
        turn_id: "turn_1",
        generation_id: "gen_1",
        text: "明天记得去公园",
        approximate: false,
      });

      assert.equal(page.data.memoryCandidates.length, 1);
      assert.deepEqual(Object.keys(page.data.memoryCandidates[0]).sort(), [
        "claim_id",
        "conflict_state",
        "domain_category",
        "memory_kind",
        "reason",
        "status",
        "value",
      ]);
      assert.equal(page.data.confirmedMemories.length, 1);
      assert.deepEqual(Object.keys(page.data.confirmedMemories[0]).sort(), [
        "domain_category",
        "kind",
        "memory_id",
        "memory_kind",
        "occurred_at",
        "snippet",
        "status",
        "title",
      ]);

      // 候选与已确认是互斥分区，候选 claim 不得混入已确认列表。
      const candidateIds = new Set(page.data.memoryCandidates.map((item) => item.claim_id));
      assert.ok(page.data.confirmedMemories.every((item) => !candidateIds.has(item.memory_id)));
      assert.ok(page.data.memoryCandidates.every((item) => item.value !== page.data.confirmedMemories[0].title));
    } finally {
      restore();
    }
  });
});

test("confirming a candidate calls the canonical review API then refetches the authoritative projection", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/memory/index");
    const page = instantiate(definition);
    const reviewed = [];
    let reviewFetches = 0;
    const restore = stubApi({
      currentIdentity: () => ({ user_id: "person_owner" }),
      isAuthEpochCurrent: () => true,
      requireRuntimeCapability: async () => allowedGate(),
      getMemoryDays: async () => ({ items: [] }),
      getConversationReview: async () => {
        reviewFetches += 1;
        if (reviewFetches === 1) {
          return {
            actual_heard: [],
            memory_candidates: [
              { claim_id: "claim_1", value: "喜欢晚饭后散步", reason: "new" },
            ],
            confirmed_memories: [],
          };
        }
        return {
          actual_heard: [],
          memory_candidates: [],
          confirmed_memories: [
            { memory_id: "mem_1", title: "散步偏好", snippet: "喜欢晚饭后散步" },
          ],
        };
      },
      reviewMemoryClaim: async (claimId) => {
        reviewed.push(claimId);
        return { ok: true };
      },
    });
    try {
      await page.loadDays();
      assert.equal(page.data.memoryCandidates.length, 1);
      assert.equal(page.data.confirmedMemories.length, 0);

      await page.confirmCandidate({
        currentTarget: { dataset: { claimId: "claim_1" } },
      });
      assert.deepEqual(reviewed, ["claim_1"]);
      assert.equal(reviewFetches, 2, "确认成功后必须重新拉取服务端权威投影");
      assert.equal(page.data.memoryCandidates.length, 0);
      assert.equal(page.data.confirmedMemories.length, 1);
      assert.equal(page.data.confirmedMemories[0].memory_id, "mem_1");
    } finally {
      restore();
    }
  });
});

test("a failed confirmation does not locally upgrade the candidate", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/memory/index");
    const page = instantiate(definition);
    let fetchCount = 0;
    const restore = stubApi({
      currentIdentity: () => ({ user_id: "person_owner" }),
      isAuthEpochCurrent: () => true,
      requireRuntimeCapability: async () => allowedGate(),
      getMemoryDays: async () => ({ items: [] }),
      getConversationReview: async () => {
        fetchCount += 1;
        return {
          actual_heard: [],
          memory_candidates: [{ claim_id: "claim_1", value: "喜欢晚饭后散步" }],
          confirmed_memories: [],
        };
      },
      reviewMemoryClaim: async () => {
        throw new Error("review unavailable");
      },
    });
    try {
      await page.loadDays();
      await page.confirmCandidate({
        currentTarget: { dataset: { claimId: "claim_1" } },
      });
      assert.equal(fetchCount, 1, "确认失败不得重新拉取或本地改写");
      assert.equal(page.data.memoryCandidates.length, 1);
      assert.equal(page.data.memoryCandidates[0].claim_id, "claim_1");
      assert.equal(page.data.confirmedMemories.length, 0);
      assert.ok(page.data.error.length > 0, "失败要给出可解释错误");
      assert.equal(page.data.reviewingClaimId, "");
    } finally {
      restore();
    }
  });
});

test("a profile without memory_recall_private no longer hides either review interface", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/memory/index");
    const page = instantiate(definition);
    let memoryCalls = 0;
    let reviewCalls = 0;
    let gateCalls = 0;
    const restore = stubApi({
      currentIdentity: () => ({ user_id: "person_owner" }),
      isAuthEpochCurrent: () => true,
      requireRuntimeCapability: async () => {
        gateCalls += 1;
        return { allowed: false, reason: "capability_missing" };
      },
      getMemoryDays: async () => {
        memoryCalls += 1;
        return { items: [] };
      },
      getConversationReview: async () => {
        reviewCalls += 1;
        return { actual_heard: [], memory_candidates: [], confirmed_memories: [] };
      },
      getConversationSessions: async () => ({ items: [] }),
    });
    try {
      await page.loadDays();
      assert.equal(gateCalls, 0);
      assert.equal(memoryCalls, 1);
      assert.equal(reviewCalls, 1);
      assert.equal(page.data.error, "");
    } finally {
      restore();
    }
  });
});

test("a late conversation-review response cannot repopulate partitions after auth is cleared", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/memory/index");
    const page = instantiate(definition);
    let lastEpoch = 0;
    let releaseReview;
    const reviewReady = new Promise((resolve) => {
      releaseReview = resolve;
    });
    const restore = stubApi({
      currentIdentity: () => ({ user_id: "person_owner" }),
      isAuthEpochCurrent: (epoch) => epoch === lastEpoch,
      requireRuntimeCapability: async () => allowedGate(),
      getMemoryDays: async () => ({ items: [] }),
      getConversationReview: async () => reviewReady,
    });
    try {
      const pending = page.loadDays();
      await new Promise((resolve) => setImmediate(resolve));
      // 登录态清理会使 authEpoch 前进；迟到的 review 响应必须被丢弃。
      lastEpoch += 1;
      releaseReview({
        actual_heard: [{ event_id: "ev_late", text: "不应回到游客页面" }],
        memory_candidates: [{ claim_id: "claim_late", value: "不应回到游客页面" }],
        confirmed_memories: [{ memory_id: "mem_late", title: "迟到投影" }],
      });
      await pending;
      assert.deepEqual(page.data.heardTurns, []);
      assert.deepEqual(page.data.memoryCandidates, []);
      assert.deepEqual(page.data.confirmedMemories, []);
    } finally {
      restore();
    }
  });
});

test("guest state clears every private review partition", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/memory/index");
    const page = instantiate(definition);
    page.setData({
      days: [{ date: "2026-08-17" }],
      heardTurns: [{ event_id: "ev_1", text: "x" }],
      memoryCandidates: [{ claim_id: "claim_1", value: "x" }],
      confirmedMemories: [{ memory_id: "mem_1", title: "x" }],
      reviewingClaimId: "claim_1",
    });
    page._enterGuestState();
    assert.equal(page.data.authenticated, false);
    assert.deepEqual(page.data.days, []);
    assert.deepEqual(page.data.heardTurns, []);
    assert.deepEqual(page.data.memoryCandidates, []);
    assert.deepEqual(page.data.confirmedMemories, []);
    assert.equal(page.data.reviewingClaimId, "");
  });
});

test("memory review WXML exposes daily recap, pending memory, and approximate heard labels", () => {
  const wxml = fs.readFileSync(
    path.join(__dirname, "../pages/memory/index.wxml"),
    "utf8",
  );
  assert.match(wxml, /日常回顾/);
  assert.match(wxml, /记住的事/);
  assert.match(wxml, /实际听到的回复/);
  assert.match(wxml, /待你确认的记忆/);
  assert.match(wxml, /已确认的记忆/);
  assert.match(wxml, /近似播放记录/);
});

test("guardian review endpoints are canonical and encode the child id and day", async () => {
  await withWx(async () => {
    const requests = [];
    global.wx.request = (options) => {
      requests.push(options);
      options.success({ statusCode: 200, data: { items: [] } });
    };
    await api.getGuardianChildDays("person/child", 30);
    await api.generateGuardianChildRecap("person/child", "2026-10-01");
    assert.equal(
      requests[0].url,
      `${CONTROL_API_BASE_URL}/v1/guardian/minors/person%2Fchild/days?limit=30`,
    );
    assert.equal(requests[0].method, "GET");
    assert.equal(
      requests[1].url,
      `${CONTROL_API_BASE_URL}/v1/guardian/minors/person%2Fchild/days/2026-10-01/recap`,
    );
    assert.equal(requests[1].method, "POST");
  });
});

const CHILD_BINDING = Object.freeze({
  declared_mode: "parent_for_child",
  status: "active",
  account_owner_id: "person_owner",
  primary_subject_ids: ["person_child"],
});

function guardianStubs(overrides = {}) {
  return stubApi({
    currentIdentity: () => ({ user_id: "person_owner" }),
    isAuthEpochCurrent: () => true,
    readBindingManifest: () => CHILD_BINDING,
    getMemoryDays: async () => {
      throw new Error("the legacy day list must not be used for a bound child");
    },
    getConversationReview: async () => ({
      actual_heard: [],
      memory_candidates: [],
      confirmed_memories: [],
    }),
    getConversationSessions: async () => ({ items: [] }),
    getGuardianChildDays: async () => ({
      items: [
        { day: "2026-10-01", message_count: 8 },
        { day: "2026-09-30", message_count: 3 },
      ],
    }),
    getGuardianChildMemories: async () => ({ candidates: [], confirmed: [] }),
    ...overrides,
  });
}

test("a parent who bound the device for a child sees the child's days and never their words", async () => {
  await withWx(async () => {
    const page = instantiate(loadPage("../pages/memory/index"));
    const restore = guardianStubs();
    try {
      await page.loadDays();
      assert.equal(page.data.guardianMode, true);
      assert.equal(page.data.guardianSubjectId, "person_child");
      assert.deepEqual(
        page.data.days.map((day) => [day.date, day.count, day.title]),
        [
          ["2026-10-01", 8, "孩子聊了 8 次"],
          ["2026-09-30", 3, "孩子聊了 3 次"],
        ],
      );
      assert.deepEqual(page.data.days[0].highlights, []);
    } finally {
      restore();
    }
  });
});

test("the generate button writes one day's recap into that day's card only", async () => {
  await withWx(async () => {
    const page = instantiate(loadPage("../pages/memory/index"));
    const recapCalls = [];
    const restore = guardianStubs({
      requireRuntimeCapability: async () => allowedGate(),
      generateGuardianChildRecap: async (minorId, day) => {
        recapCalls.push([minorId, day]);
        return {
          day,
          summary: {
            title: "今天聊了画画",
            overview: "孩子聊了学校里的画画，心情不错。",
            highlights: ["画画"],
            suggestion: "可以一起看看他的画。",
          },
        };
      },
      summarizeDay: async () => {
        throw new Error("the account's own summary must not be generated for a child");
      },
    });
    // requireLogin passes for an authenticated session.
    const originalHas = api.hasAuthenticatedSession;
    api.hasAuthenticatedSession = () => true;
    try {
      await page.loadDays();
      page.setData({ selectedDate: "2026-09-30" });
      await page.summarizeSelectedDay();
      assert.deepEqual(recapCalls, [["person_child", "2026-09-30"]]);
      const [first, second] = page.data.days;
      assert.equal(first.title, "孩子聊了 8 次", "other days are untouched");
      assert.equal(second.title, "今天聊了画画");
      assert.deepEqual(second.highlights, ["画画"]);
      assert.equal(second.suggestion, "可以一起看看他的画。");
      assert.equal(page.data.summarizing, false);
    } finally {
      api.hasAuthenticatedSession = originalHas;
      restore();
    }
  });
});

test("a missing long-term memory consent is explained in words, not as an error code", async () => {
  await withWx(async () => {
    const page = instantiate(loadPage("../pages/memory/index"));
    const restore = guardianStubs({
      getGuardianChildDays: async () => {
        const error = new Error("guardian_consent_required");
        error.detail = { code: "guardian_consent_required" };
        throw error;
      },
    });
    try {
      await page.loadDays();
      assert.equal(page.data.guardianMode, true);
      assert.deepEqual(page.data.days, []);
      assert.match(page.data.guardianError, /长期记忆还没有开启/);
      assert.doesNotMatch(page.data.guardianError, /guardian_consent_required/);
    } finally {
      restore();
    }
  });
});

test("an account that is not a bound child's guardian keeps its own daily review", async () => {
  await withWx(async () => {
    const page = instantiate(loadPage("../pages/memory/index"));
    const restore = guardianStubs({
      readBindingManifest: () => ({ ...CHILD_BINDING, declared_mode: "self_use" }),
      getMemoryDays: async () => ({ items: [{ day: "2026-10-01", message_count: 2, summary: null }] }),
      getGuardianChildDays: async () => {
        throw new Error("a self-use account must not read a child's days");
      },
    });
    try {
      await page.loadDays();
      assert.equal(page.data.guardianMode, false);
      assert.equal(page.data.days.length, 1);
      assert.equal(page.data.days[0].count, 2);
    } finally {
      restore();
    }
  });
});

test("guardian memory endpoints are canonical, encode their ids and only allow confirm or retract", async () => {
  await withWx(async () => {
    const requests = [];
    global.wx.request = (options) => {
      requests.push(options);
      options.success({ statusCode: 200, data: { candidates: [], confirmed: [] } });
    };
    await api.getGuardianChildMemories("person/child", 20);
    await api.reviewGuardianChildMemory("person/child", "claim/1", "confirm");
    await api.reviewGuardianChildMemory("person/child", "claim/1", "retract");
    assert.equal(
      requests[0].url,
      `${CONTROL_API_BASE_URL}/v1/guardian/minors/person%2Fchild/memories?limit=20`,
    );
    assert.equal(requests[0].method, "GET");
    assert.equal(
      requests[1].url,
      `${CONTROL_API_BASE_URL}/v1/guardian/minors/person%2Fchild/memories/claim%2F1/review`,
    );
    assert.equal(requests[1].method, "POST");
    assert.deepEqual(requests[1].data, { action: "confirm" });
    assert.deepEqual(requests[2].data, { action: "retract" });
    await assert.rejects(api.reviewGuardianChildMemory("person_child", "claim_1", "correct"), TypeError);
    await assert.rejects(api.reviewGuardianChildMemory("person_child", "", "confirm"), TypeError);
    await assert.rejects(api.reviewGuardianChildMemory("person_child", " claim_1", "confirm"), TypeError);
    await assert.rejects(api.reviewGuardianChildMemory("", "claim_1", "confirm"), TypeError);
    assert.equal(requests.length, 3, "an invalid call never reaches the network");
  });
});

const CHILD_MEMORIES = Object.freeze({
  candidates: [
    {
      claim_id: "claim_child_1",
      value: "今天练习了乘法口诀。",
      status: "candidate",
      reason: "pending_confirmation",
      domain_category: "study_progress",
    },
  ],
  confirmed: [
    {
      memory_id: "claim_child_2",
      title: "我最喜欢蓝色。",
      snippet: "我最喜欢蓝色。",
      status: "confirmed",
      domain_category: "daily_life",
      occurred_at: "2026-10-02T13:30:00+00:00",
    },
  ],
});

test("a parent who bound the device for a child reviews the child's memories, not their own", async () => {
  await withWx(async () => {
    const page = instantiate(loadPage("../pages/memory/index"));
    const restore = guardianStubs({
      getGuardianChildMemories: async (minorId) => {
        assert.equal(minorId, "person_child");
        return CHILD_MEMORIES;
      },
      // The signed-in account's own review is not what this tab shows for a child's robot.
      getConversationReview: async () => ({
        actual_heard: [],
        memory_candidates: [{ claim_id: "claim_parent", value: "家长自己的候选" }],
        confirmed_memories: [{ memory_id: "mem_parent", title: "家长自己的记忆" }],
      }),
    });
    try {
      await page.loadDays();
      assert.deepEqual(
        page.data.memoryCandidates.map((item) => item.claim_id),
        ["claim_child_1"],
      );
      assert.deepEqual(
        page.data.confirmedMemories.map((item) => item.memory_id),
        ["claim_child_2"],
      );
      assert.equal(page.data.pendingCount, 1);
      assert.deepEqual(
        page.data.visibleMemories.map((item) => [item.id, item.pending]),
        [
          ["claim_child_1", true],
          ["claim_child_2", false],
        ],
      );
      assert.equal(page.data.visibleMemories[0].body, "确认后才会加入记忆档案。");
      assert.doesNotMatch(JSON.stringify(page.data.visibleMemories), /pending_confirmation|家长自己/);
    } finally {
      restore();
    }
  });
});

test("an account without a bound child keeps its own memories and never asks for a child's", async () => {
  await withWx(async () => {
    const page = instantiate(loadPage("../pages/memory/index"));
    const restore = guardianStubs({
      readBindingManifest: () => ({ ...CHILD_BINDING, declared_mode: "self_use" }),
      getMemoryDays: async () => ({ items: [] }),
      getConversationReview: async () => ({
        actual_heard: [],
        memory_candidates: [{ claim_id: "claim_own", value: "喜欢清晨散步", reason: "conflicting_values" }],
        confirmed_memories: [],
      }),
      getGuardianChildMemories: async () => {
        throw new Error("a self-use account must not read a child's memories");
      },
    });
    try {
      await page.loadDays();
      assert.equal(page.data.guardianMode, false);
      assert.deepEqual(
        page.data.memoryCandidates.map((item) => item.claim_id),
        ["claim_own"],
      );
      assert.equal(page.data.visibleMemories[0].body, "和之前记下的内容不一致，请确认以哪个为准。");
    } finally {
      restore();
    }
  });
});

test("a missing consent leaves the child's memory list empty and says why in words", async () => {
  await withWx(async () => {
    const page = instantiate(loadPage("../pages/memory/index"));
    const restore = guardianStubs({
      getGuardianChildDays: async () => ({ items: [] }),
      getGuardianChildMemories: async () => {
        const error = new Error("guardian_consent_required");
        error.detail = { code: "guardian_consent_required" };
        throw error;
      },
    });
    try {
      await page.loadDays();
      assert.deepEqual(page.data.memoryCandidates, []);
      assert.deepEqual(page.data.confirmedMemories, []);
      assert.match(page.data.guardianError, /长期记忆还没有开启/);
    } finally {
      restore();
    }
  });
});

async function withGuardianMemoryPage(overrides, fn) {
  await withWx(async () => {
    const page = instantiate(loadPage("../pages/memory/index"));
    const calls = [];
    const restore = guardianStubs({
      getGuardianChildMemories: async () => CHILD_MEMORIES,
      reviewGuardianChildMemory: async (minorId, claimId, action) => {
        calls.push([minorId, claimId, action]);
        return { claim_id: claimId, status: action === "confirm" ? "confirmed" : "retracted" };
      },
      reviewMemoryClaim: async () => {
        throw new Error("the account's own review must not confirm a child's claim");
      },
      ...overrides,
    });
    const originalHas = api.hasAuthenticatedSession;
    api.hasAuthenticatedSession = () => true;
    try {
      await page.loadDays();
      await fn(page, calls);
    } finally {
      api.hasAuthenticatedSession = originalHas;
      restore();
    }
  });
}

test("the parent's confirmation goes to the child's review endpoint and refetches the list", async () => {
  let listed = 0;
  await withGuardianMemoryPage(
    {
      getGuardianChildMemories: async () => {
        listed += 1;
        return CHILD_MEMORIES;
      },
    },
    async (page, calls) => {
      await page.confirmCandidate({ currentTarget: { dataset: { claimId: "claim_child_1" } } });
      assert.deepEqual(calls, [["person_child", "claim_child_1", "confirm"]]);
      assert.equal(listed, 2, "the authoritative list is read again after a confirmation");
      assert.equal(page.data.reviewingClaimId, "");
    },
  );
});

test("a failed confirmation of a child's memory leaves the list as the server sent it", async () => {
  await withGuardianMemoryPage(
    {
      reviewGuardianChildMemory: async () => {
        throw new Error("网络不稳定");
      },
    },
    async (page) => {
      await page.confirmCandidate({ currentTarget: { dataset: { claimId: "claim_child_1" } } });
      assert.equal(page.data.error, "网络不稳定");
      assert.deepEqual(
        page.data.memoryCandidates.map((item) => item.claim_id),
        ["claim_child_1"],
      );
      assert.equal(page.data.reviewingClaimId, "");
    },
  );
});

test("not remembering a candidate and forgetting a kept memory are both a retract after a question", async () => {
  await withGuardianMemoryPage({}, async (page, calls) => {
    const asked = [];
    global.wx.showModal = (options) => {
      asked.push([options.title, options.confirmText]);
      options.success?.({ confirm: true });
    };
    await page.retractMemory({ currentTarget: { dataset: { claimId: "claim_child_1", pending: true } } });
    await page.retractMemory({ currentTarget: { dataset: { claimId: "claim_child_2", pending: false } } });
    assert.deepEqual(calls, [
      ["person_child", "claim_child_1", "retract"],
      ["person_child", "claim_child_2", "retract"],
    ]);
    assert.deepEqual(asked, [
      ["不记这一条？", "不记"],
      ["忘掉这一条？", "忘掉"],
    ]);
  });
});

test("declining the question changes nothing, and outside a child's robot nothing is retracted", async () => {
  await withGuardianMemoryPage({}, async (page, calls) => {
    global.wx.showModal = (options) => options.success?.({ confirm: false });
    await page.retractMemory({ currentTarget: { dataset: { claimId: "claim_child_1", pending: true } } });
    assert.deepEqual(calls, []);
    global.wx.showModal = (options) => options.success?.({ confirm: true });
    page.setData({ guardianMode: false, guardianSubjectId: "" });
    await page.retractMemory({ currentTarget: { dataset: { claimId: "claim_child_1", pending: true } } });
    await page.retractMemory({ currentTarget: { dataset: {} } });
    assert.deepEqual(calls, []);
  });
});

test("memory review WXML offers the guardian's confirm, not-this and forget actions", () => {
  const wxml = fs.readFileSync(path.join(__dirname, "../pages/memory/index.wxml"), "utf8");
  assert.match(wxml, /确认记住/);
  assert.match(wxml, /不记这个/);
  assert.match(wxml, /忘掉这条/);
  assert.match(wxml, /bindtap="retractMemory"/);
  assert.match(wxml, /只有你点了「确认记住」的，机器人才会在以后的聊天里提起/);
});
