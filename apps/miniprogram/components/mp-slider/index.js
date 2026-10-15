// v4 slider: the native <slider> cannot draw the design's 8px track and 32px thumb.
function percentOf(value, min, max) {
  if (!(max > min)) return 0;
  const ratio = (Number(value) - min) / (max - min);
  return Math.round(Math.min(1, Math.max(0, ratio)) * 1000) / 10;
}

function valueAt(fraction, min, max, step) {
  const clamped = Math.min(1, Math.max(0, fraction));
  const stride = step > 0 ? step : 1;
  return Math.min(max, min + Math.round((clamped * (max - min)) / stride) * stride);
}

Component({
  properties: {
    min: { type: Number, value: 0 },
    max: { type: Number, value: 100 },
    step: { type: Number, value: 1 },
    value: { type: Number, value: 0 },
    disabled: { type: Boolean, value: false },
    label: { type: String, value: "" },
  },
  data: { percent: 0 },
  observers: {
    "value, min, max, disabled"(value, min, max, disabled) {
      const held = this._held;
      if (held && (!held.released || disabled)) return;
      this._held = null;
      this.setData({ percent: percentOf(value, min, max) });
    },
  },
  lifetimes: {
    ready() {
      this._measure();
    },
  },
  methods: {
    _measure(then) {
      this.createSelectorQuery()
        .select(".mps-track")
        .boundingClientRect((rect) => {
          if (rect && rect.width) this._rect = rect;
          then?.();
        })
        .exec();
    },
    _follow(clientX) {
      const rect = this._rect;
      if (!rect) return;
      const { min, max, step } = this.data;
      const value = valueAt((clientX - rect.left) / rect.width, min, max, step);
      this._held = { value, released: false };
      this.setData({ percent: percentOf(value, min, max) });
    },
    _settle() {
      this._held = null;
      const { value, min, max } = this.data;
      this.setData({ percent: percentOf(value, min, max) });
    },
    onTouchStart(event) {
      if (this.data.disabled) return;
      const clientX = event.touches[0].clientX;
      if (this._rect) this._follow(clientX);
      else this._measure(() => this._follow(clientX));
    },
    onTouchMove(event) {
      if (this.data.disabled || !this._held) return;
      this._follow(event.touches[0].clientX);
    },
    onTouchEnd() {
      const held = this._held;
      if (!held || held.released) return;
      if (held.value === this.data.value) {
        this._settle();
        return;
      }
      held.released = true;
      this.triggerEvent("change", { value: held.value });
    },
    onTouchCancel() {
      if (this._held && !this._held.released) this._settle();
    },
  },
});
