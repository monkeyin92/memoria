// 整页氛围背景：固定铺满视口，位于内容之下。
const themes = Object.freeze(["sky", "night", "mist", "warm"]);

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
