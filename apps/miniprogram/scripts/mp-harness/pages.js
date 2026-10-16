/*
 * One harness scenario per page state the screenshot matrix and layout tests
 * cover. Sample data comes from fixtures.js; unknown API calls resolve null.
 */
const fx = require("./fixtures");

const { authed, guest } = fx;
const READY_AGREEMENT = "# 用户协议\n\n## 正文\n\n甲。\n";
const FIXTURE_EFFECTIVE_ON = "2026年4月1日";

function withReadyAgreement(harness) {
  return {
    ...harness,
    files: { "legal/user-agreement.md": READY_AGREEMENT },
    effectiveOn: FIXTURE_EFFECTIVE_ON,
  };
}

const PAGES = [
  { slug: "home", page: "pages/home/index", harness: authed() },
  { slug: "home-guest", page: "pages/home/index", harness: guest() },
  { slug: "memory", page: "pages/memory/index", harness: authed(fx.safetyAlertApi([])), actions: [["summarizeSelectedDay"]] },
  { slug: "memory-attention", page: "pages/memory/index", harness: authed() },
  {
    slug: "memory-attention-more",
    page: "pages/memory/index",
    harness: authed(fx.safetyAlertApi(["n3", "n2", "n1"].map((id) => ({ ...fx.SAFETY_ALERT, notification_id: id })))),
  },
  { slug: "memory-empty", page: "pages/memory/index", harness: authed(fx.emptyMemoryApi) },
  { slug: "memory-empty-memories", page: "pages/memory/index", harness: authed(fx.emptyMemoryApi), actions: [["showMemories"]] },
  { slug: "companion", page: "pages/companion/index", harness: authed() },
  { slug: "device", page: "pages/device/index", harness: authed() },
  { slug: "profile", page: "pages/profile/index", harness: authed() },
  { slug: "guardian", page: "pages/guardian/index", harness: authed(fx.guardianApi) },
  { slug: "bind", page: "pages/bind/index", harness: authed() },
  { slug: "bind-mode", page: "pages/bind/index", harness: authed(fx.bindApi), query: fx.BIND_QUERY },
  {
    slug: "bind-form",
    page: "pages/bind/index",
    harness: authed(fx.bindApi),
    query: fx.BIND_QUERY,
    actions: [["chooseMode", { currentTarget: { dataset: { mode: "parent_for_child" } } }]],
  },
  {
    slug: "bind-review",
    page: "pages/bind/index",
    harness: authed(fx.bindApi),
    query: fx.BIND_QUERY,
    actions: [
      ["chooseMode", { currentTarget: { dataset: { mode: "parent_for_child" } } }],
      ["setData", { "form.childNickname": "乐乐" }],
      ["goToReview"],
    ],
  },
  { slug: "persona-custom", page: "pages/persona-custom/index", harness: authed(fx.personaApi) },
  { slug: "auth", page: "pages/auth/index", harness: guest(), query: { reason: "view_device" } },
  {
    slug: "auth-checked",
    page: "pages/auth/index",
    harness: guest(),
    query: { reason: "view_device" },
    actions: [["toggleAccountConsent"]],
  },
  { slug: "child-consent", page: "pages/child-consent/index", harness: withReadyAgreement(guest()) },
  { slug: "child-consent-locked", page: "pages/child-consent/index", harness: guest() },
  { slug: "privacy-policy", page: "pages/privacy-policy/index", harness: guest() },
  { slug: "attention", page: "pages/attention/index", harness: authed(), query: { id: "n1" } },
  { slug: "attention-empty", page: "pages/attention/index", harness: authed(fx.safetyAlertApi([])), query: { id: "n1" } },
  {
    slug: "attention-ack-failed",
    page: "pages/attention/index",
    harness: authed({
      markGuardianNotificationRead: async () => {
        throw Object.assign(new Error("server"), { status: 503 });
      },
    }),
    query: { id: "n1" },
    actions: [["acknowledge"]],
  },
  { slug: "privacy", page: "pages/privacy/index", harness: authed() },
  { slug: "privacy-guest", page: "pages/privacy/index", harness: guest() },
  { slug: "user-agreement", page: "pages/user-agreement/index", harness: guest() },
  {
    slug: "privacy-granted",
    page: "pages/privacy/index",
    harness: authed({ getRawVoiceConsent: async () => ({ consent: fx.RAW_VOICE_CONSENT }) }),
  },
  { slug: "digital-self", page: "pages/digital-self/index", harness: authed(fx.digitalSelfApi), actions: [["toggleEngineering"]] },
  { slug: "digital-self-empty", page: "pages/digital-self/index", harness: authed() },
  { slug: "child-memory", page: "pages/child-memory/index", harness: authed(), query: { minorId: "p_child", name: "乐乐" } },
  { slug: "child-memory-archive", page: "pages/child-memory/index", harness: authed(fx.archiveApi), query: { minorId: "p_child", name: "乐乐" } },
  { slug: "device-onboarding", page: "pages/device-onboarding/index", harness: authed() },
  {
    slug: "device-onboarding-wifi",
    page: "pages/device-onboarding/index",
    harness: authed(),
    actions: [
      ["_applySnapshot", fx.onboardingSnapshot("wifi", { wifiNetworks: fx.WIFI_SCAN })],
      ["onWifiNetworkTap", { currentTarget: { dataset: { ssid: "Home-2.4G" } } }],
    ],
  },
  {
    slug: "device-onboarding-progress",
    page: "pages/device-onboarding/index",
    harness: authed(),
    actions: [["_applySnapshot", fx.onboardingSnapshot("progress", { network: { phase: "joining", joined: true, ssid: "Home-2.4G", elapsedS: 12 } })]],
  },
  {
    slug: "device-onboarding-complete",
    page: "pages/device-onboarding/index",
    harness: authed(),
    actions: [["_applySnapshot", fx.onboardingSnapshot("complete", { activation: { status: "ready_for_conversation" } })]],
  },
];

const VIEWS = [
  { name: "390", width: 390, height: 844, safeTop: 47, safeBottom: 34, fullPage: true },
  { name: "320", width: 320, height: 568, safeTop: 20, safeBottom: 0 },
  { name: "375", width: 375, height: 667, safeTop: 20, safeBottom: 0 },
  { name: "430", width: 430, height: 932, safeTop: 59, safeBottom: 34 },
  { name: "390-large-font", width: 390, height: 844, safeTop: 47, safeBottom: 34, fontScale: 1.3 },
];

const LONG_LIST_SIZE = 200;

const LONG_LISTS = [
  { slug: "memory-200", page: "pages/memory/index", harness: authed(fx.longMemoryApi(LONG_LIST_SIZE)) },
  {
    slug: "child-memory-200",
    page: "pages/child-memory/index",
    harness: authed(fx.longArchiveApi(LONG_LIST_SIZE)),
    query: { minorId: "p_child", name: "乐乐" },
  },
];

module.exports = { LONG_LISTS, LONG_LIST_SIZE, PAGES, VIEWS };
