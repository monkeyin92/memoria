const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const crisisAlerts = require("../utils/crisis-alerts");

const root = path.resolve(__dirname, "..");

test("pinnedAlerts keeps only server-marked undelivered crisis rows", () => {
  const items = crisisAlerts.pinnedAlerts([
    {
      notification_id: "n1",
      minor_user_id: "child-a",
      minor_display_name: "小明",
      occurred_at: "2026-10-14T00:00:00+00:00",
      delivery_status: "no_subscription",
      pinned: true,
      message: "请尽快联系",
    },
    {
      notification_id: "n2",
      minor_user_id: "child-a",
      minor_display_name: "小明",
      occurred_at: "2026-10-13T00:00:00+00:00",
      delivery_status: "delivered",
      pinned: false,
      message: "已送达",
    },
    {
      notification_id: "",
      pinned: true,
    },
    null,
  ]);

  assert.deepEqual(items, [
    {
      notificationId: "n1",
      minorUserId: "child-a",
      minorDisplayName: "小明",
      occurredAt: "2026-10-14T00:00:00+00:00",
      deliveryStatus: "no_subscription",
      pinned: true,
      message: "请尽快联系",
    },
  ]);
});

test("loadPinnedCrisisAlerts swallows API failures", async () => {
  const ok = await crisisAlerts.loadPinnedCrisisAlerts({
    async getGuardianNotifications() {
      return {
        items: [
          {
            notification_id: "n1",
            minor_display_name: "小明",
            pinned: true,
            message: "请尽快联系",
            delivery_status: "pending",
            occurred_at: "t",
            minor_user_id: "c",
          },
        ],
      };
    },
  });
  assert.equal(ok.length, 1);
  assert.equal(ok[0].notificationId, "n1");

  const failed = await crisisAlerts.loadPinnedCrisisAlerts({
    async getGuardianNotifications() {
      throw new Error("offline");
    },
  });
  assert.deepEqual(failed, []);
});

test("bind and memory pages expose subscribe entry; home and memory pin alerts", () => {
  const bindJs = fs.readFileSync(path.join(root, "pages/bind/index.js"), "utf8");
  const bindWxml = fs.readFileSync(path.join(root, "pages/bind/index.wxml"), "utf8");
  const memoryJs = fs.readFileSync(path.join(root, "pages/memory/index.js"), "utf8");
  const memoryWxml = fs.readFileSync(path.join(root, "pages/memory/index.wxml"), "utf8");
  const homeJs = fs.readFileSync(path.join(root, "pages/home/index.js"), "utf8");
  const homeWxml = fs.readFileSync(path.join(root, "pages/home/index.wxml"), "utf8");

  assert.match(bindJs, /offerCrisisSubscription/);
  assert.match(bindJs, /enableCrisisPush/);
  assert.match(bindWxml, /bindtap="enableCrisisPush"/);
  assert.match(memoryJs, /loadPinnedCrisisAlerts/);
  assert.match(memoryJs, /subscribeCrisisAlerts/);
  assert.match(memoryWxml, /pinnedCrisisAlerts/);
  assert.match(memoryWxml, /安全风险提醒/);
  assert.match(memoryWxml, /bindtap="enableCrisisPush"/);
  assert.match(homeJs, /loadPinnedCrisisAlerts/);
  assert.match(homeWxml, /pinnedCrisisAlerts/);
  assert.match(homeWxml, /安全风险提醒/);
});

test("shouldOfferCrisisSubscription only for active parent/elder bind modes", () => {
  assert.equal(
    crisisAlerts.shouldOfferCrisisSubscription({
      declared_mode: "parent_for_child",
      status: "active",
    }),
    true,
  );
  assert.equal(
    crisisAlerts.shouldOfferCrisisSubscription({
      declared_mode: "child_for_parent",
      status: "active",
    }),
    true,
  );
  assert.equal(
    crisisAlerts.shouldOfferCrisisSubscription({
      declared_mode: "self_use",
      status: "active",
    }),
    false,
  );
  assert.equal(
    crisisAlerts.shouldOfferCrisisSubscription({
      declared_mode: "parent_for_child",
      status: "revoked",
    }),
    false,
  );
});
