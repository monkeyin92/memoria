const assert = require("node:assert/strict");
const test = require("node:test");

const api = require("../utils/api");
const binding = require("../utils/device-binding");
const { canonicalManifest } = require("./manifest-fixtures");

const storage = {};
const toasts = [];
const profileWrites = [];

global.wx = {
  getStorageSync: (key) => storage[key],
  setStorageSync: (key, value) => {
    storage[key] = value;
  },
  removeStorageSync: (key) => {
    delete storage[key];
  },
  showToast: (options) => toasts.push(options),
  navigateTo() {},
  switchTab() {},
};

api.hasAuthenticatedSession = () => true;
api.currentIdentity = () => ({ user_id: "person_owner", display_name: "主人" });
api.updateProfile = async (userId, profile) => {
  profileWrites.push({ userId, companion_id: profile.companion_id });
  return { user_id: userId, companion_id: profile.companion_id };
};

let pageDefinition;
global.Page = (definition) => {
  pageDefinition = definition;
};
require("../pages/companion/index");

function instantiate(definition) {
  const instance = { ...definition };
  instance.data = JSON.parse(JSON.stringify(definition.data));
  instance.setData = (updates) => {
    for (const [key, value] of Object.entries(updates)) {
      if (key.includes(".")) {
        const [head, tail] = key.split(".");
        instance.data[head] = { ...instance.data[head], [tail]: value };
      } else {
        instance.data[key] = value;
      }
    }
  };
  return instance;
}

test("a pick with a bound device says the device will switch", async () => {
  toasts.length = 0;
  profileWrites.length = 0;
  binding.saveBindingManifest(canonicalManifest());
  try {
    const page = instantiate(pageDefinition);
    await page.chooseCompanion({ currentTarget: { dataset: { id: "axu" } } });
    assert.deepEqual(profileWrites, [{ userId: "person_owner", companion_id: "axu" }]);
    assert.equal(toasts.at(-1).title, "已保存，设备将同步切换");
    assert.equal(toasts.at(-1).icon, "none", "带图标的 toast 放不下这句提示");
  } finally {
    binding.clearBindingManifest();
  }
});

test("a pick without a device just says saved", async () => {
  toasts.length = 0;
  const page = instantiate(pageDefinition);
  await page.chooseCompanion({ currentTarget: { dataset: { id: "taoxi" } } });
  assert.deepEqual(toasts.at(-1), { title: "已保存", icon: "success" });
});

test("a failed save puts the card back on the account's companion", async () => {
  toasts.length = 0;
  const originalUpdate = api.updateProfile;
  api.updateProfile = async () => {
    throw new Error("网络不可用");
  };
  try {
    const page = instantiate(pageDefinition);
    page.setData({ "profile.companion_id": "taoxi", currentName: "桃喜", currentTone: "tone-a" });
    await page.chooseCompanion({ currentTarget: { dataset: { id: "axu" } } });
    assert.equal(page.data.profile.companion_id, "taoxi");
    assert.equal(page.data.currentName, "桃喜");
    assert.equal(page.data.currentTone, "tone-a");
    assert.equal(toasts.at(-1).title, "网络不可用");
  } finally {
    api.updateProfile = originalUpdate;
  }
});
