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

test("memory page requests neither private review interface without the capability", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/memory/index");
    const page = instantiate(definition);
    let memoryCalls = 0;
    let reviewCalls = 0;
    const restore = stubApi({
      currentIdentity: () => ({ user_id: "person_owner" }),
      isAuthEpochCurrent: () => true,
      requireRuntimeCapability: async () => ({
        allowed: false,
        reason: "capability_missing",
      }),
      getMemoryDays: async () => {
        memoryCalls += 1;
        return { items: [] };
      },
      getConversationReview: async () => {
        reviewCalls += 1;
        return { actual_heard: [], memory_candidates: [], confirmed_memories: [] };
      },
    });
    try {
      await page.loadDays();
      assert.equal(memoryCalls, 0, "未授权时不得请求私人回顾日数据");
      assert.equal(reviewCalls, 0, "未授权时不得请求 conversation review");
      assert.deepEqual(page.data.heardTurns, []);
      assert.deepEqual(page.data.memoryCandidates, []);
      assert.deepEqual(page.data.confirmedMemories, []);
      assert.ok(page.data.error.includes("尚未开放"));
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

test("memory review WXML exposes the three sections with approximate labels", () => {
  const wxml = fs.readFileSync(
    path.join(__dirname, "../pages/memory/index.wxml"),
    "utf8",
  );
  assert.match(wxml, /实际听到的回复/);
  assert.match(wxml, /待你确认的记忆/);
  assert.match(wxml, /已确认的记忆/);
  assert.match(wxml, /近似播放记录/);
});
