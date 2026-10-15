const assert = require("node:assert/strict");
const test = require("node:test");

const TRACK = { left: 48, width: 294 };

function mountSlider(props) {
  let definition;
  const previous = global.Component;
  global.Component = (config) => {
    definition = config;
  };
  try {
    delete require.cache[require.resolve("../components/mp-slider/index")];
    require("../components/mp-slider/index");
  } finally {
    global.Component = previous;
  }
  const events = [];
  const data = { ...definition.data };
  for (const [name, spec] of Object.entries(definition.properties)) data[name] = spec.value;
  const slider = {
    data,
    setData(patch) {
      Object.assign(data, patch);
    },
    triggerEvent(name, detail) {
      events.push({ name, detail });
    },
    createSelectorQuery() {
      const query = {
        select: () => query,
        boundingClientRect(callback) {
          query.callback = callback;
          return query;
        },
        exec: () => query.callback({ ...TRACK, top: 0, height: 8 }),
      };
      return query;
    },
  };
  for (const [name, fn] of Object.entries(definition.methods)) slider[name] = fn.bind(slider);
  const setProps = (patch) => {
    Object.assign(data, patch);
    for (const [key, observer] of Object.entries(definition.observers)) {
      const fields = key.split(",").map((field) => field.trim());
      if (fields.some((field) => field in patch)) observer.apply(slider, fields.map((field) => data[field]));
    }
  };
  setProps(props);
  definition.lifetimes.ready.call(slider);
  const at = (fraction) => ({ touches: [{ clientX: TRACK.left + TRACK.width * fraction }] });
  return { slider, events, setProps, at };
}

const CAP = { min: 15, max: 60, step: 15, value: 30 };

test("the thumb starts at the stored 30 of a 15–60 range", () => {
  const { slider } = mountSlider(CAP);
  assert.equal(slider.data.percent, 33.3);
});

test("dragging to the right end follows the finger and reports 60 only on release", () => {
  const { slider, events, at } = mountSlider(CAP);
  slider.onTouchStart(at(0.34));
  slider.onTouchMove(at(0.8));
  slider.onTouchMove(at(1.2));
  assert.equal(slider.data.percent, 100);
  assert.deepEqual(events, []);
  slider.onTouchEnd();
  assert.deepEqual(events, [{ name: "change", detail: { value: 60 } }]);
});

test("a tap snaps to the nearest step", () => {
  const { slider, events, at } = mountSlider(CAP);
  slider.onTouchStart(at(0.1));
  slider.onTouchEnd();
  assert.deepEqual(events, [{ name: "change", detail: { value: 15 } }]);
  assert.equal(slider.data.percent, 0);
});

test("releasing on the stored value reports nothing", () => {
  const { slider, events, at } = mountSlider(CAP);
  slider.onTouchStart(at(0.3));
  slider.onTouchEnd();
  assert.deepEqual(events, []);
});

test("a disabled slider ignores the finger while a save is in flight", () => {
  const { slider, events, at } = mountSlider({ ...CAP, disabled: true });
  slider.onTouchStart(at(1));
  slider.onTouchMove(at(1));
  slider.onTouchEnd();
  assert.deepEqual(events, []);
  assert.equal(slider.data.percent, 33.3);
});

test("a cancelled touch drops the drag and puts the thumb back", () => {
  const { slider, events, at } = mountSlider(CAP);
  slider.onTouchStart(at(1));
  slider.onTouchCancel();
  slider.onTouchEnd();
  assert.deepEqual(events, []);
  assert.equal(slider.data.percent, 33.3);
});

test("the released value holds through the save and a failed save snaps back to the stored value", () => {
  const { slider, setProps, at } = mountSlider(CAP);
  slider.onTouchStart(at(1));
  slider.onTouchEnd();
  setProps({ disabled: true });
  assert.equal(slider.data.percent, 100);
  setProps({ disabled: false });
  assert.equal(slider.data.percent, 33.3);
});

test("a successful save keeps the thumb on the new stored value", () => {
  const { slider, setProps, at } = mountSlider(CAP);
  slider.onTouchStart(at(0.7));
  slider.onTouchEnd();
  setProps({ disabled: true });
  setProps({ value: 45 });
  assert.equal(slider.data.percent, 66.7);
  setProps({ disabled: false });
  assert.equal(slider.data.percent, 66.7);
});

test("a stored value from the parent moves an idle thumb", () => {
  const { slider, setProps } = mountSlider(CAP);
  setProps({ value: 45 });
  assert.equal(slider.data.percent, 66.7);
});

test("the parent's value does not yank the thumb out from under the finger", () => {
  const { slider, events, setProps, at } = mountSlider(CAP);
  slider.onTouchStart(at(1));
  setProps({ value: 15 });
  assert.equal(slider.data.percent, 100);
  slider.onTouchEnd();
  assert.deepEqual(events, [{ name: "change", detail: { value: 60 } }]);
});
