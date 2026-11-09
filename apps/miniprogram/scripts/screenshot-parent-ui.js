const fs = require("node:fs");
const path = require("node:path");
const { launch, problems, shoot } = require("./mp-harness/shoot");
const { PAGES, VIEWS } = require("./mp-harness/pages");
const fx = require("./mp-harness/fixtures");

const OUTPUT = process.env.MP_SCREENSHOT_OUTPUT
  ? path.resolve(process.env.MP_SCREENSHOT_OUTPUT)
  : path.resolve(__dirname, "../../../docs/design/parent-ui-20261103");
const PAGE_SLUGS = new Set([
  "home", "home-guest", "memory", "memory-empty", "device",
  "profile", "bind-form", "bind-review", "attention", "child-memory",
  "child-memory-archive", "device-onboarding", "device-onboarding-complete",
  "persona-custom",
]);
const SCENARIOS = [
  ...PAGES.filter((scenario) => PAGE_SLUGS.has(scenario.slug)),
  {
    slug: "device-expanded",
    page: "pages/device/index",
    harness: fx.authed(),
    actions: [["toggleManagement"], ["toggleUsage"], ["toggleMemberSettings"], ["toggleStories"], ["toggleDiag"]],
  },
  {
    slug: "device-guest",
    page: "pages/device/index",
    harness: fx.guest(),
  },
  {
    slug: "device-offline",
    page: "pages/device/index",
    harness: fx.authed({
      getActivationStatus: async () => ({ status: "failed", network: { internet: false } }),
    }),
  },
  {
    slug: "home-offline",
    page: "pages/home/index",
    harness: fx.authed({
      getActivationStatus: async () => ({ status: "failed", network: { internet: false } }),
    }),
  },
  {
    slug: "home-usage-unknown",
    page: "pages/home/index",
    harness: fx.authed({
      getSessionLimits: async () => {
        throw new Error("session_limits_unavailable");
      },
    }),
  },
];

async function main() {
  process.env.TZ = "Asia/Shanghai";
  const requested = process.argv.slice(2);
  for (const slug of requested) {
    if (!SCENARIOS.some((scenario) => scenario.slug === slug)) throw new Error(`Unknown scenario: ${slug}`);
  }
  const scenarios = requested.length ? SCENARIOS.filter((scenario) => requested.includes(scenario.slug)) : SCENARIOS;
  const browser = await launch();
  const results = [];
  try {
    for (const scenario of scenarios) {
      for (const view of VIEWS) {
        const output = path.join(OUTPUT, "after", `${scenario.slug}-${view.name}.png`);
        const { htmlPath, ...result } = await shoot(browser, scenario, view, output);
        const issues = problems(result, view);
        results.push({ slug: scenario.slug, view: view.name, issues, ...result });
        console.log(`${scenario.slug}@${view.name}: ${issues.length ? issues.join("; ") : "PASS"}`);
      }
    }
  } finally {
    await browser.close();
  }
  fs.writeFileSync(path.join(OUTPUT, "screenshots.json"), `${JSON.stringify(results, null, 2)}\n`);
  if (results.some((result) => result.issues.length)) process.exitCode = 1;
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
