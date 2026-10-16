/*
 * Which live mini-program page state stands in for each docs/design/app-v4
 * screen. Every scenario runs the real page JS through the harness with
 * sample API data; nothing here patches page data after load.
 */
const fx = require("../mp-harness/fixtures");

const PARITY = [
  { slug: "01-login", design: "01-login.png", page: "pages/home/index", harness: fx.guest() },
  { slug: "02-consent", design: "02-consent.png", page: "pages/auth/index", harness: fx.guest() },
  { slug: "03-home", design: "03-home.png", page: "pages/home/index", harness: fx.authed() },
  {
    slug: "04-summary",
    design: "04-summary.png",
    page: "pages/memory/index",
    harness: fx.authed({ getGuardianNotifications: async () => ({ items: [] }) }),
    actions: [["summarizeSelectedDay"]],
  },
  { slug: "06-usage", design: "06-usage.png", page: "pages/device/index", harness: fx.authed() },
  {
    slug: "06-usage-legacy",
    design: "06-usage.png",
    page: "pages/device/index",
    harness: fx.authed({
      getRuntimeProfile: async () => ({
        ...fx.RUNTIME_PROFILE,
        obligations: [
          { code: "QUIET_HOURS", params: { quiet_hours: ["21:00", "07:00"] } },
          {
            code: "MAX_SESSION_SECONDS",
            params: { max_session_seconds: 2700, extras: [["daily_chat_seconds", "1800"]] },
          },
        ],
      }),
    }),
  },
  { slug: "05-attention", design: "05-attention.png", page: "pages/attention/index", harness: fx.authed(), query: { id: "n1" } },
  { slug: "10-profile", design: "10-profile.png", page: "pages/profile/index", harness: fx.authed() },
  { slug: "08-roles", design: "08-roles.png", page: "pages/companion/index", harness: fx.authed() },
  {
    slug: "09-memory",
    design: "09-memory.png",
    page: "pages/child-memory/index",
    harness: fx.authed(fx.archiveApi),
    query: { minorId: "p_child", name: "乐乐" },
  },
  {
    slug: "09-memory-delete",
    design: "09-memory-delete.png",
    page: "pages/child-memory/index",
    harness: fx.authed(fx.archiveApi),
    query: { minorId: "p_child", name: "乐乐" },
    actions: [["askDeleteArchive", { currentTarget: { dataset: { recordId: "a2" } } }]],
  },
  {
    slug: "09-memory-clear",
    design: "09-memory-clear.png",
    page: "pages/child-memory/index",
    harness: fx.authed(fx.archiveApi),
    query: { minorId: "p_child", name: "乐乐" },
    actions: [["askClearArchive"]],
  },
];

module.exports = { PARITY };
