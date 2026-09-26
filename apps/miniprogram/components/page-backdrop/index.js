// 整页氛围背景：固定铺满视口，位于内容之下。
// 全部页面统一浅色主题，只保留 mist；传入其他值也回落到 mist。
const themes = Object.freeze(["mist"]);

function srcFor(value) {
  return `/assets/ui/bg/${themes.includes(value) ? value : "mist"}.jpg`;
}

Component({
  properties: {
    theme: { type: String, value: "mist" },
  },
  data: { src: "/assets/ui/bg/mist.jpg" },
  lifetimes: {
    attached() {
      this.setData({ src: srcFor(this.data.theme) });
    },
  },
  observers: {
    theme(value) {
      this.setData({ src: srcFor(value) });
    },
  },
});
