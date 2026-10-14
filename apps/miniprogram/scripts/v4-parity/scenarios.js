/*
 * Which live mini-program page state stands in for each docs/design/app-v4
 * screen. Every scenario runs the real page JS through the harness with
 * sample API data; nothing here patches page data after load.
 */
const fx = require("../mp-harness/fixtures");

const PARITY = [
  { slug: "01-login", design: "01-login.png", page: "pages/home/index", harness: { api: fx.baseApi(fx.GUEST_API), now: fx.NOW } },
  { slug: "03-home", design: "03-home.png", page: "pages/home/index", harness: { api: fx.baseApi(), now: fx.NOW } },
  {
    slug: "04-summary",
    design: "04-summary.png",
    page: "pages/memory/index",
    harness: { api: fx.baseApi({ getGuardianNotifications: async () => ({ items: [] }) }), now: fx.NOW },
    actions: [["summarizeSelectedDay"]],
  },
  { slug: "05-attention", design: "05-attention.png", page: "pages/memory/index", harness: { api: fx.baseApi(), now: fx.NOW } },
];

module.exports = { PARITY };
