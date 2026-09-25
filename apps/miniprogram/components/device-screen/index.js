// 设备形象：圆角方形外框 + 黑色屏幕上的一双眼睛。在线时会眨眼，离线时闭眼变暗。
Component({
  properties: {
    online: { type: Boolean, value: true },
    size: { type: Number, value: 220 },
  },
});
