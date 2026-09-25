// 伙伴吉祥物形象。图片来自 assets/mascots/<id>/<mood>.png（由 assets/companions/<id>/ 的表情帧压缩而来），
// 未知角色（如自定义人格 cu_*）回落到默认伙伴。plain 去掉底板，用于大尺寸 hero；dim 用于设备离线。
// animated 打开空闲表情循环：平时是默认表情，隔几秒随机换一个表情再回来；点一下会笑。
const { companionById } = require("../../utils/companions");

const FRAMES = ["default", "happy", "surprised", "thinking", "dizzy", "sleepy"];
const IDLE_MOODS = ["happy", "surprised", "thinking", "dizzy"];
// 每种表情停留多久（毫秒）。
const HOLD_MS = { happy: 2400, surprised: 1800, thinking: 3200, dizzy: 2800 };
const IDLE_MIN_MS = 4500;
const IDLE_MAX_MS = 8500;
const POKE_MS = 1800;

function randomBetween(min, max) {
  return min + Math.floor(Math.random() * (max - min));
}

Component({
  properties: {
    role: {
      type: String,
      value: "starlight",
      observer(role) {
        this.setData({ roleId: companionById(role).id });
      },
    },
    size: { type: String, value: "md" },
    breathe: { type: Boolean, value: false },
    onLight: { type: Boolean, value: false },
    plain: { type: Boolean, value: false },
    dim: {
      type: Boolean,
      value: false,
      observer() {
        this._restart();
      },
    },
    animated: {
      type: Boolean,
      value: false,
      observer() {
        this._restart();
      },
    },
  },
  data: {
    roleId: "starlight",
    frames: FRAMES,
    mood: "default",
  },
  lifetimes: {
    attached() {
      this.setData({ roleId: companionById(this.data.role).id });
      this._restart();
    },
    detached() {
      this._stop();
    },
  },
  pageLifetimes: {
    show() {
      this._restart();
    },
    hide() {
      this._stop();
    },
  },
  methods: {
    poke() {
      if (!this.data.animated || this.data.dim) return;
      this._stop();
      this.setData({ mood: "happy" });
      this._timer = setTimeout(() => this._rest(), POKE_MS);
    },

    // 离线时睡着；在线时从默认表情开始排下一次换表情。
    _restart() {
      this._stop();
      if (!this.data.animated) {
        if (this.data.mood !== "default") this.setData({ mood: "default" });
        return;
      }
      if (this.data.dim) {
        this.setData({ mood: "sleepy" });
        return;
      }
      this._rest();
    },

    _rest() {
      this.setData({ mood: "default" });
      this._timer = setTimeout(() => this._play(), randomBetween(IDLE_MIN_MS, IDLE_MAX_MS));
    },

    _play() {
      const choices = IDLE_MOODS.filter((mood) => mood !== this._lastMood);
      const mood = choices[Math.floor(Math.random() * choices.length)];
      this._lastMood = mood;
      this.setData({ mood });
      this._timer = setTimeout(() => this._rest(), HOLD_MS[mood]);
    },

    _stop() {
      if (this._timer) clearTimeout(this._timer);
      this._timer = null;
    },
  },
});
