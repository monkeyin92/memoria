const assert = require("node:assert/strict");
const test = require("node:test");

process.env.TZ = "Asia/Shanghai";

const { renderPage } = require("../scripts/mp-harness/render");
const { scenarioData } = require("../scripts/mp-harness/shoot");
const { LONG_LISTS, LONG_LIST_SIZE, PAGES } = require("../scripts/mp-harness/pages");
const { packageSize } = require("../scripts/package-size");

const KiB = 1024;
const stripStyle = (html) => html.replace(/<style>[\s\S]*?<\/style>/g, "");

async function measure(scenario) {
  const { data, setDataLog, loadMs } = await scenarioData(scenario);
  const started = process.hrtime.bigint();
  const html = renderPage(scenario.page, data);
  const renderMs = Number(process.hrtime.bigint() - started) / 1e6;
  const bytes = setDataLog.map((entry) => entry.bytes);
  return { html, loadMs, renderMs, calls: bytes.length, largest: Math.max(0, ...bytes), total: bytes.reduce((a, b) => a + b, 0) };
}

for (const scenario of PAGES) {
  test(`${scenario.slug} loads in at most 16 setData calls, none over 16 KiB`, async (t) => {
    const m = await measure(scenario);
    t.diagnostic(`calls=${m.calls} largest=${m.largest}B total=${m.total}B load=${m.loadMs.toFixed(1)}ms render=${m.renderMs.toFixed(1)}ms`);
    assert.ok(m.calls <= 16, `setData calls ${m.calls}`);
    assert.ok(m.largest <= 16 * KiB, `largest setData ${m.largest} bytes`);
    assert.ok(m.total <= 32 * KiB, `total setData ${m.total} bytes`);
  });
}

test(`memory renders all ${LONG_LIST_SIZE} days and keeps each setData under WeChat's 256 KiB guidance`, async (t) => {
  const m = await measure(LONG_LISTS.find((item) => item.slug === "memory-200"));
  t.diagnostic(`calls=${m.calls} largest=${m.largest}B render=${m.renderMs.toFixed(1)}ms`);
  assert.equal((stripStyle(m.html).match(/class="card memory-day"/g) || []).length, LONG_LIST_SIZE);
  assert.ok(m.largest < 256 * KiB, `largest setData ${m.largest} bytes`);
  assert.ok(m.renderMs < 500, `render ${m.renderMs}ms`);
});

test(`child-memory renders all ${LONG_LIST_SIZE} archive facts and keeps each setData under 256 KiB`, async (t) => {
  const m = await measure(LONG_LISTS.find((item) => item.slug === "child-memory-200"));
  t.diagnostic(`calls=${m.calls} largest=${m.largest}B render=${m.renderMs.toFixed(1)}ms`);
  const facts = stripStyle(m.html).replace(/<[^>]+>/g, " ").match(/第 \d+ 条/g) || [];
  assert.equal(new Set(facts).size, LONG_LIST_SIZE);
  assert.ok(m.largest < 256 * KiB, `largest setData ${m.largest} bytes`);
  assert.ok(m.renderMs < 500, `render ${m.renderMs}ms`);
});

test("the shipped files fit WeChat's 2 MiB main package before minification", (t) => {
  const { total, limit } = packageSize();
  t.diagnostic(`${(total / KiB).toFixed(1)} KiB of ${limit / KiB} KiB`);
  assert.ok(total < limit, `${total} bytes`);
});
