const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.resolve(__dirname, "..");
const api = require("../utils/api");
const binding = require("../utils/device-binding");
const { canonicalManifest } = require("./manifest-fixtures");

test("guardian page covers child confirmation, granular consent, summary, and alerts", () => {
  const appConfig = JSON.parse(fs.readFileSync(path.join(root, "app.json"), "utf8"));
  const script = fs.readFileSync(path.join(root, "pages/guardian/index.js"), "utf8");
  const template = fs.readFileSync(path.join(root, "pages/guardian/index.wxml"), "utf8");

  assert.ok(appConfig.pages.includes("pages/guardian/index"));
  assert.match(script, /createGuardianLink/);
  assert.match(script, /confirmGuardianLink/);
  assert.match(script, /grantGuardianConsent/);
  assert.match(script, /revokeGuardianConsent/);
  assert.match(script, /getGuardianNotifications/);
  assert.match(template, /输入 8 位绑定码/);
  assert.match(script, /语音陪伴/);
  assert.match(script, /学习与成长记录/);
  assert.match(script, /每周成长小结/);
  assert.match(template, /不含对话原文/);
  assert.doesNotMatch(template, /心理监测|心理诊断评分|严重度分级/);
});

test("student notice and bind consent remain explicit client choices", () => {
  const profile = fs.readFileSync(path.join(root, "pages/profile/index.wxml"), "utf8");
  const profileScript = fs.readFileSync(path.join(root, "pages/profile/index.js"), "utf8");
  const bind = fs.readFileSync(path.join(root, "pages/bind/index.wxml"), "utf8");
  const api = fs.readFileSync(path.join(root, "utils/api.js"), "utf8");

  // 敏感入口只能由 Runtime Profile capabilities 驱动，WXML 不得按本地年龄显示。
  assert.match(profile, /guardianEntryAllowed/);
  // 声纹已下线：「我的」页不再有主人声纹登记与旁人声音过滤。
  assert.doesNotMatch(profile, /speakerEnrollment|主人声纹|reject_non_owner_voice|过滤明显旁人/);
  assert.match(profile, /digitalSelfEntryAllowed/);
  assert.match(profile, /rawVoiceEntryAllowed/);
  assert.doesNotMatch(profile, /canUseAdultCapabilities|_allowAdultExperience/);
  assert.match(profile, /成长小结与监护授权/);
  assert.match(profileScript, /敏感能力入口已关闭/);
  assert.match(bind, /英语口语陪练/);
  assert.match(api, /updateDeviceSettings/);
  assert.match(api, /learning_mode/);
  assert.doesNotMatch(api, /session_focus/);
});

const storage = {};

function instantiate(definition) {
  const instance = { ...definition };
  instance.data = JSON.parse(JSON.stringify(definition.data));
  instance.setData = (updates) => {
    for (const [key, value] of Object.entries(updates)) {
      instance.data[key] = value;
    }
  };
  return instance;
}

test("guardian page grants and revokes person consent for an accountless child", async () => {
  const requests = [];
  global.wx = {
    getStorageSync: (key) => storage[key],
    setStorageSync: (key, value) => {
      storage[key] = value;
    },
    removeStorageSync: (key) => {
      delete storage[key];
    },
    request(options) {
      const pathname = options.url.replace("https://aigcnice.com:8443/memoria-api", "");
      requests.push({
        pathname,
        method: options.method,
        data: options.data,
        idempotencyKey: options.header?.["Idempotency-Key"] || "",
      });
      if (
        pathname === "/v1/guardian/minors/person_child/consents" &&
        (options.method || "GET") === "POST"
      ) {
        options.success({
          statusCode: 201,
          data: {
            consent_id: "consent_1",
            subject_person_id: "person_child",
            consent_kind: options.data.consent_kind,
            active: true,
            revoked_at: null,
            expires_at: null,
          },
        });
        return;
      }
      if (pathname === "/v1/guardian/minors/person_child/consents/consent_1") {
        options.success({
          statusCode: 200,
          data: {
            consent_id: "consent_1",
            consent_kind: "memory_retention",
            active: false,
            revoked_at: "2026-09-22T00:00:00Z",
            expires_at: null,
          },
        });
        return;
      }
      if (
        pathname === "/v1/guardian/minors/person_child/consents" &&
        (options.method || "GET") === "GET"
      ) {
        const granted = requests.some(
          (item) => item.pathname.endsWith("/consents") && item.method === "POST",
        );
        const revoked = requests.some((item) => item.pathname.endsWith("/consent_1"));
        options.success({
          statusCode: 200,
          data: {
            items: granted
              ? [
                  {
                    consent_id: "consent_1",
                    consent_kind: "memory_retention",
                    active: !revoked,
                    revoked_at: revoked ? "2026-09-22T00:00:00Z" : null,
                    expires_at: null,
                    granted_at: "2026-09-21T00:00:00Z",
                  },
                ]
              : [
                  {
                    consent_id: "consent_old",
                    consent_kind: "weekly_report",
                    active: false,
                    revoked_at: null,
                    expires_at: "2026-01-01T00:00:00Z",
                    granted_at: "2025-12-01T00:00:00Z",
                  },
                ],
          },
        });
        return;
      }
      options.success({ statusCode: 404, data: { detail: { code: "not_found" } } });
    },
  };
  global.getApp = () => ({
    globalData: {
      identity: { user_id: "person_owner", display_name: "主人" },
      accessToken: "test-token",
      accessTokenExpiresAt: Date.now() + 3600_000,
      authEpoch: 0,
    },
  });
  binding.saveBindingManifest(
    canonicalManifest({
      declared_mode: "parent_for_child",
      status: "active",
      account_owner_id: "person_owner",
      primary_subject_ids: ["person_child"],
      guardian_ids: ["person_owner"],
      roles: [
        { person_id: "person_owner", role: "account_owner", permissions: [] },
        { person_id: "person_child", role: "primary_subject", permissions: [] },
      ],
    }),
  );
  let definition;
  global.Page = (value) => {
    definition = value;
  };
  const pagePath = require.resolve("../pages/guardian/index");
  delete require.cache[pagePath];
  require(pagePath);
  const page = instantiate(definition);
  await page._loadBoundSubjectConsents();
  const before = page.data.boundSubjects[0].consentRows.find((row) => row.kind === "weekly_report");
  assert.equal(before.statusLabel, "已过期");
  assert.equal(before.active, false);
  const missing = page.data.boundSubjects[0].consentRows.find(
    (row) => row.kind === "minor_voice_session",
  );
  assert.equal(missing.statusLabel, "未授权");

  await page.togglePersonConsent({
    currentTarget: {
      dataset: {
        personId: "person_child",
        kind: "memory_retention",
        policyVersion: "minor-memory-v1",
        consentId: "",
        active: false,
      },
    },
  });
  const granted = page.data.boundSubjects[0].consentRows.find(
    (row) => row.kind === "memory_retention",
  );
  assert.equal(granted.active, true);
  assert.equal(granted.statusLabel, "已授权");
  assert.match(requests.find((item) => item.method === "POST").idempotencyKey, /^person-grant-/);

  await page.togglePersonConsent({
    currentTarget: {
      dataset: {
        personId: "person_child",
        kind: "memory_retention",
        policyVersion: "minor-memory-v1",
        consentId: "consent_1",
        active: true,
      },
    },
  });
  const revoked = page.data.boundSubjects[0].consentRows.find(
    (row) => row.kind === "memory_retention",
  );
  assert.equal(revoked.active, false);
  assert.equal(revoked.statusLabel, "未授权");
  const template = fs.readFileSync(path.join(root, "pages/guardian/index.wxml"), "utf8");
  assert.match(template, /consent.statusLabel/);
  assert.doesNotMatch(template, /已送达/);
});

test("person consent failure stays owner-scoped and does not blank the row", async () => {
  global.wx = {
    getStorageSync: (key) => storage[key],
    setStorageSync: (key, value) => {
      storage[key] = value;
    },
    removeStorageSync: (key) => {
      delete storage[key];
    },
    request(options) {
      const pathname = options.url.replace("https://aigcnice.com:8443/memoria-api", "");
      if (
        pathname === "/v1/guardian/minors/person_child/consents" &&
        (options.method || "GET") === "GET"
      ) {
        options.success({ statusCode: 200, data: { items: [] } });
        return;
      }
      options.success({
        statusCode: 403,
        data: { detail: { code: "guardian_binding_owner_required" } },
      });
    },
  };
  global.getApp = () => ({
    globalData: {
      identity: { user_id: "person_owner", display_name: "主人" },
      accessToken: "test-token",
      accessTokenExpiresAt: Date.now() + 3600_000,
      authEpoch: 0,
    },
  });
  let definition;
  global.Page = (value) => {
    definition = value;
  };
  const pagePath = require.resolve("../pages/guardian/index");
  delete require.cache[pagePath];
  require(pagePath);
  const page = instantiate(definition);
  await page._loadBoundSubjectConsents();
  assert.equal(page.data.boundSubjects[0].consentRows[0].statusLabel, "未授权");
  await page.togglePersonConsent({
    currentTarget: {
      dataset: {
        personId: "person_child",
        kind: "memory_retention",
        policyVersion: "minor-memory-v1",
        consentId: "",
        active: false,
      },
    },
  });
  assert.match(page.data.error, /只有监护绑定发起人/);
  assert.equal(page.data.boundSubjects[0].consentRows.length, 3);
});
