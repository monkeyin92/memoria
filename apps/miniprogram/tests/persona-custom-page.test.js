const assert = require("node:assert/strict");
const test = require("node:test");

const api = require("../utils/api");

const ENUMS = [
  { key: "warmth", options: ["warm", "bright", "soft", "calm", "reserved"] },
  { key: "directness", options: ["gentle", "direct"] },
  { key: "response_length", options: ["brief", "balanced"] },
  { key: "question_frequency", options: ["rare", "occasional", "frequent"] },
  {
    key: "interview_depth",
    options: ["light", "structured", "on_explicit_invitation"],
  },
  { key: "default_voice_emotion", options: ["neutral", "happy"] },
];

async function withPage(callback) {
  const previousWx = global.wx;
  const previousGetApp = global.getApp;
  const previousPage = global.Page;
  const modals = [];
  let pageDefinition = null;
  global.wx = {
    getStorageSync: () => "",
    setStorageSync: () => {},
    removeStorageSync: () => {},
    showToast() {},
    navigateTo() {},
    showModal(options) {
      modals.push(options);
      options.success?.({ confirm: true });
    },
  };
  global.getApp = () => ({
    globalData: { identity: { user_id: "person_owner" }, authEpoch: 0 },
    subscribeAuthCleared: () => () => {},
  });
  global.Page = (definition) => {
    pageDefinition = definition;
  };
  const resolved = require.resolve("../pages/persona-custom/index");
  delete require.cache[resolved];
  require(resolved);
  const instance = { ...pageDefinition };
  instance.data = JSON.parse(JSON.stringify(pageDefinition.data));
  instance.setData = (updates) => {
    for (const [key, value] of Object.entries(updates)) {
      const parts = key.split(".").filter(Boolean);
      let cursor = instance.data;
      for (let index = 0; index < parts.length - 1; index += 1) {
        if (typeof cursor[parts[index]] !== "object" || cursor[parts[index]] === null) {
          cursor[parts[index]] = {};
        }
        cursor = cursor[parts[index]];
      }
      cursor[parts[parts.length - 1]] = value;
    }
  };
  try {
    return await callback(instance, modals);
  } finally {
    if (previousWx === undefined) delete global.wx;
    else global.wx = previousWx;
    if (previousGetApp === undefined) delete global.getApp;
    else global.getApp = previousGetApp;
    if (previousPage === undefined) delete global.Page;
    else global.Page = previousPage;
  }
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

function baseStubs(extra = {}) {
  return {
    hasAuthenticatedSession: () => true,
    currentAuthEpoch: () => 0,
    isAuthEpochCurrent: () => true,
    listPersonas: async () => ({ custom_personas: [], builtin: [] }),
    ...extra,
  };
}

test("自定义人格：创建即冻结并把服务端记录显示出来", async () => {
  await withPage(async (page) => {
    const created = [];
    let listings = 0;
    const restore = stubApi(
      baseStubs({
        listPersonas: async () => {
          listings += 1;
          return {
            custom_personas:
              listings === 1
                ? []
                : [
                    {
                      persona_id: "cu_0123456789abcdef0123456789abcd",
                      display_name: "小北",
                      persona_version: 1,
                      fallback_designed_voice: "taoxi",
                    },
                  ],
            builtin: [],
          };
        },
        createCustomPersona: async (request) => {
          created.push(request);
          return {
            persona_id: "cu_0123456789abcdef0123456789abcd",
            display_name: request.displayName,
          };
        },
      }),
    );
    try {
      await page.loadPersonas();
      assert.deepEqual(page.data.personas, []);

      page.onNameInput({ detail: { value: "小北" } });
      page.onFreeTextInput({ detail: { value: "说话短一点，像朋友。" } });
      page.onTextFieldInput({
        currentTarget: { dataset: { field: "conversation_instruction" } },
        detail: { value: "说话短一点，像朋友。" },
      });
      page.onEnumChange({
        currentTarget: { dataset: { field: "warmth" } },
        detail: { value: "2" },
      });
      page.onRateChange({ detail: { value: 105 } });

      await page.create();

      assert.equal(created.length, 1);
      assert.equal(created[0].displayName, "小北");
      assert.equal(created[0].fallbackDesignedVoice, "starlight");
      assert.equal(created[0].structured.conversation_instruction, "说话短一点，像朋友。");
      assert.equal(created[0].structured.warmth, "soft", "选择器下标必须映射回受控值");
      assert.equal(created[0].structured.default_voice_rate, 1.05);
      assert.match(page.data.notice, /已创建并冻结/);
      assert.equal(page.data.personas.length, 1, "创建后必须回读账号实际情况");
      assert.equal(page.data.name, "", "创建成功后清空表单，避免重复提交同一人格");
    } finally {
      restore();
    }
  });
});

test("自定义人格：结构化未接入时如实提示并可手填创建", async () => {
  await withPage(async (page) => {
    let created = 0;
    const restore = stubApi(
      baseStubs({
        structureCustomPersona: async () => {
          const error = new Error("服务端还没接入");
          error.code = "persona_structuring_unavailable";
          throw error;
        },
        createCustomPersona: async () => {
          created += 1;
          return { persona_id: "cu_ffffffffffffffffffffffffffff", display_name: "小北" };
        },
      }),
    );
    try {
      await page.loadPersonas();
      page.onNameInput({ detail: { value: "小北" } });
      page.onFreeTextInput({ detail: { value: "温柔一点" } });
      await page.structure();
      assert.match(page.data.error, /手填/);
      assert.equal(page.data.structuring, false);

      page.onTextFieldInput({
        currentTarget: { dataset: { field: "style_description" } },
        detail: { value: "温和的陪伴者" },
      });
      await page.create();
      assert.equal(created, 1, "结构化不可用不得阻塞手填创建");
    } finally {
      restore();
    }
  });
});

test("自定义人格：删除必须先确认，取消则不发请求", async () => {
  await withPage(async (page, modals) => {
    const removed = [];
    const restore = stubApi(
      baseStubs({
        listPersonas: async () => ({
          custom_personas: [
            {
              persona_id: "cu_0123456789abcdef0123456789abcd",
              display_name: "小北",
              persona_version: 1,
              fallback_designed_voice: "taoxi",
            },
          ],
          builtin: [],
        }),
        deleteCustomPersona: async (personaId) => {
          removed.push(personaId);
          return { deleted: true };
        },
      }),
    );
    try {
      await page.loadPersonas();
      assert.equal(page.data.personas.length, 1);
      await page.remove({
        currentTarget: { dataset: { personaId: "cu_0123456789abcdef0123456789abcd" } },
      });
      assert.equal(modals.length, 1, "删除必须先经确认弹窗");
      assert.deepEqual(removed, ["cu_0123456789abcdef0123456789abcd"]);
      assert.equal(page.data.notice, "已删除自定义人格。");
    } finally {
      restore();
    }
  });
});
