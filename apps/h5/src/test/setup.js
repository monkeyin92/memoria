import "@testing-library/jest-dom/vitest";

const storedValues = new Map();

Object.defineProperty(window, "localStorage", {
  configurable: true,
  value: {
    getItem: (key) => storedValues.get(String(key)) ?? null,
    setItem: (key, value) => storedValues.set(String(key), String(value)),
    removeItem: (key) => storedValues.delete(String(key)),
    clear: () => storedValues.clear(),
    key: (index) => [...storedValues.keys()][index] ?? null,
    get length() {
      return storedValues.size;
    },
  },
});
