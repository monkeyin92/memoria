/*
 * One harness scenario per page state the screenshot matrix and layout tests
 * cover. Sample data comes from fixtures.js; unknown API calls resolve null.
 */
const fx = require("./fixtures");

const { authed, guest } = fx;

const PAGES = [
  { slug: "home", page: "pages/home/index", harness: authed() },
  { slug: "home-guest", page: "pages/home/index", harness: guest() },
  { slug: "memory", page: "pages/memory/index", harness: authed({ getGuardianNotifications: async () => ({ items: [] }) }), actions: [["summarizeSelectedDay"]] },
  { slug: "memory-attention", page: "pages/memory/index", harness: authed() },
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
  { slug: "privacy", page: "pages/privacy/index", harness: authed() },
  {
    slug: "privacy-granted",
    page: "pages/privacy/index",
    harness: authed({ getRawVoiceConsent: async () => ({ consent: fx.RAW_VOICE_CONSENT }) }),
  },
  { slug: "digital-self", page: "pages/digital-self/index", harness: authed(fx.digitalSelfApi), actions: [["toggleEngineering"]] },
  { slug: "digital-self-empty", page: "pages/digital-self/index", harness: authed() },
  { slug: "child-memory", page: "pages/child-memory/index", harness: authed(), query: { minorId: "p_child", name: "乐乐" } },
  { slug: "child-memory-archive", page: "pages/child-memory/index", harness: authed(fx.archiveApi), query: { minorId: "p_child", name: "乐乐" }, actions: [["showArchive"]] },
  { slug: "device-onboarding", page: "pages/device-onboarding/index", harness: authed() },
];

const VIEWS = [
  { name: "390", width: 390, height: 844, safeTop: 47, safeBottom: 34, fullPage: true },
  { name: "375", width: 375, height: 667, safeTop: 20, safeBottom: 0 },
  { name: "430", width: 430, height: 932, safeTop: 59, safeBottom: 34 },
  { name: "390-large-font", width: 390, height: 844, safeTop: 47, safeBottom: 34, fontScale: 1.3 },
];

module.exports = { PAGES, VIEWS };
