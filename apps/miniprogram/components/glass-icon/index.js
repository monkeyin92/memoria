// 3D 玻璃质感图标。图片随包发布，页面只传名字，不直接写本地图片路径。
const names = Object.freeze([
  "bell", "bluetooth", "chart", "check", "device", "export", "family", "memory", "mic", "moon",
  "pen", "person", "qr", "receipt", "shield", "sprout", "star", "volume", "wave", "wifi",
]);

function srcFor(value) {
  return `/assets/ui/icons/${names.includes(value) ? value : "star"}.png`;
}

Component({
  properties: {
    name: { type: String, value: "star" },
    size: { type: Number, value: 64 },
  },
  data: { src: "/assets/ui/icons/star.png" },
  lifetimes: {
    attached() {
      this.setData({ src: srcFor(this.data.name) });
    },
  },
  observers: {
    name(value) {
      this.setData({ src: srcFor(value) });
    },
  },
});
