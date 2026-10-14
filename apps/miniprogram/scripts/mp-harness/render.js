/*
 * Renders a page's real WXML + WXSS with a data object into standalone HTML so
 * Chrome can lay it out. Supports the subset the pages use: mustache bindings,
 * wx:if/elif/else, wx:for, block, custom components with slots, the custom tab
 * bar, rpx, env(safe-area-inset-*) and a font scale for large-text checks.
 * WeChat built-ins are approximated with their documented default sizes.
 */
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.join(__dirname, "..", "..");
const TAB_PAGES = ["pages/home/index", "pages/memory/index", "pages/companion/index", "pages/device/index", "pages/profile/index"];

function parseWxml(source) {
  const root = { type: "root", name: "#root", children: [] };
  const stack = [root];
  let index = 0;
  const top = () => stack[stack.length - 1];
  while (index < source.length) {
    if (source.startsWith("<!--", index)) {
      const end = source.indexOf("-->", index);
      index = end < 0 ? source.length : end + 3;
      continue;
    }
    if (source[index] === "<" && source[index + 1] === "/") {
      const end = source.indexOf(">", index);
      const name = source.slice(index + 2, end).trim();
      while (stack.length > 1) {
        if (stack.pop().name === name) break;
      }
      index = end + 1;
      continue;
    }
    if (source[index] === "<" && /[a-zA-Z]/.test(source[index + 1] || "")) {
      let cursor = index + 1;
      while (/[\w-]/.test(source[cursor])) cursor += 1;
      const node = { type: "el", name: source.slice(index + 1, cursor), attrs: {}, children: [] };
      let selfClosing = false;
      while (cursor < source.length) {
        while (/\s/.test(source[cursor])) cursor += 1;
        if (source[cursor] === ">") {
          cursor += 1;
          break;
        }
        if (source[cursor] === "/" && source[cursor + 1] === ">") {
          selfClosing = true;
          cursor += 2;
          break;
        }
        const nameStart = cursor;
        while (cursor < source.length && !/[\s=/>]/.test(source[cursor])) cursor += 1;
        const attrName = source.slice(nameStart, cursor);
        while (/\s/.test(source[cursor])) cursor += 1;
        if (source[cursor] === "=") {
          cursor += 1;
          while (/\s/.test(source[cursor])) cursor += 1;
          const quote = source[cursor];
          if (quote === '"' || quote === "'") {
            const end = source.indexOf(quote, cursor + 1);
            node.attrs[attrName] = source.slice(cursor + 1, end);
            cursor = end + 1;
          } else {
            const valueStart = cursor;
            while (cursor < source.length && !/[\s>]/.test(source[cursor])) cursor += 1;
            node.attrs[attrName] = source.slice(valueStart, cursor);
          }
        } else if (attrName) {
          node.attrs[attrName] = true;
        } else {
          cursor += 1;
        }
      }
      top().children.push(node);
      if (!selfClosing) stack.push(node);
      index = cursor;
      continue;
    }
    let cursor = index;
    while (cursor < source.length && source[cursor] !== "<") {
      if (source.startsWith("{{", cursor)) {
        const end = source.indexOf("}}", cursor);
        cursor = end < 0 ? source.length : end + 2;
      } else {
        cursor += 1;
      }
    }
    top().children.push({ type: "text", value: source.slice(index, cursor) });
    index = cursor;
  }
  return root;
}

const exprCache = new Map();
function evaluate(expression, scope) {
  let fn = exprCache.get(expression);
  if (!fn) {
    try {
      fn = new Function("__scope", `with (__scope) { return (${expression}); }`);
    } catch (error) {
      if (process.env.MP_HARNESS_DEBUG) console.warn(`bad expression: ${expression}`);
      fn = () => undefined;
    }
    exprCache.set(expression, fn);
  }
  const proxy = new Proxy(scope, {
    has: () => true,
    get: (target, key) => (key === Symbol.unscopables ? undefined : target[key]),
  });
  try {
    return fn(proxy);
  } catch {
    return undefined;
  }
}

function interpolate(value, scope) {
  if (value === true) return true;
  if (typeof value !== "string" || !value.includes("{{")) return value;
  const single = value.split("{{").length === 2 && value.match(/^\s*\{\{([\s\S]*)\}\}\s*$/);
  if (single) return evaluate(single[1], scope);
  return value.replace(/\{\{([\s\S]*?)\}\}/g, (_, expression) => {
    const result = evaluate(expression, scope);
    return result == null ? "" : String(result);
  });
}

function escapeHtml(value) {
  return String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function camel(name) {
  return name.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
}

function rpxToPx(text, ctx) {
  return String(text).replace(/(-?\d*\.?\d+)rpx/g, (_, number) => `${+(Number(number) * ctx.width / 750).toFixed(3)}px`);
}

function assetUrl(src) {
  if (typeof src !== "string" || !src) return "";
  if (/^(https?:|data:|file:)/.test(src)) return src;
  return `file://${path.join(ROOT, src.replace(/^\//, ""))}`;
}

function readJson(file) {
  return fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : {};
}

function loadComponent(componentPath, ctx) {
  if (ctx.components.has(componentPath)) return ctx.components.get(componentPath);
  const base = path.join(ROOT, componentPath.replace(/^\//, ""));
  let config = {};
  const previous = global.Component;
  global.Component = (definition) => {
    config = definition;
  };
  try {
    delete require.cache[require.resolve(`${base}.js`)];
    require(`${base}.js`);
  } finally {
    global.Component = previous;
  }
  const entry = {
    config,
    tree: parseWxml(fs.readFileSync(`${base}.wxml`, "utf8")),
    using: readJson(`${base}.json`).usingComponents || {},
  };
  ctx.css.push(cssFromFile(`${base}.wxss`, ctx));
  ctx.components.set(componentPath, entry);
  return entry;
}

function componentData(entry, attrs, scope) {
  const { config } = entry;
  const data = { ...(config.data || {}) };
  const properties = config.properties || {};
  for (const [name, spec] of Object.entries(properties)) {
    data[name] = spec && typeof spec === "object" && "value" in spec ? spec.value : undefined;
  }
  const passed = {};
  for (const [rawName, rawValue] of Object.entries(attrs)) {
    if (/^(class|style|id|wx:|bind|catch|data-)/.test(rawName)) continue;
    const name = camel(rawName);
    if (!(name in properties)) continue;
    let value = interpolate(rawValue, scope);
    const type = properties[name]?.type || properties[name];
    if (type === Boolean) value = value === "" ? true : Boolean(value);
    if (type === Number) value = Number(value);
    passed[name] = value;
  }
  Object.assign(data, passed);
  const instance = { data, properties: data };
  instance.setData = (patch) => Object.assign(data, patch);
  for (const [name, fn] of Object.entries(config.methods || {})) instance[name] = fn.bind(instance);
  const realTimeout = global.setTimeout;
  global.setTimeout = () => 0;
  try {
    config.lifetimes?.attached?.call(instance);
    for (const [name, value] of Object.entries(passed)) properties[name]?.observer?.call(instance, value);
    for (const [key, observer] of Object.entries(config.observers || {})) {
      const fields = key.split(",").map((field) => field.trim());
      if (fields.some((field) => field in passed)) observer.apply(instance, fields.map((field) => data[field]));
    }
  } catch {
    // Component init errors leave defaults, matching a component that failed to attach.
  } finally {
    global.setTimeout = realTimeout;
  }
  return data;
}

function renderChildren(children, scope, ctx, slot) {
  let out = "";
  let chainSatisfied = false;
  for (const child of children) {
    if (child.type === "text") {
      out += escapeHtml(interpolate(child.value, scope));
      continue;
    }
    const attrs = child.attrs;
    if ("wx:for" in attrs) {
      chainSatisfied = false;
      out += renderFor(child, scope, ctx, slot);
      continue;
    }
    if ("wx:if" in attrs) {
      chainSatisfied = Boolean(interpolate(attrs["wx:if"], scope));
      if (chainSatisfied) out += renderElement(child, scope, ctx, slot);
      continue;
    }
    if ("wx:elif" in attrs) {
      if (!chainSatisfied && interpolate(attrs["wx:elif"], scope)) {
        chainSatisfied = true;
        out += renderElement(child, scope, ctx, slot);
      }
      continue;
    }
    if ("wx:else" in attrs) {
      if (!chainSatisfied) out += renderElement(child, scope, ctx, slot);
      chainSatisfied = false;
      continue;
    }
    chainSatisfied = false;
    out += renderElement(child, scope, ctx, slot);
  }
  return out;
}

function renderFor(node, scope, ctx, slot) {
  let list = interpolate(node.attrs["wx:for"], scope);
  if (typeof list === "number") list = Array.from({ length: list }, (_, index) => index);
  else if (typeof list === "string") list = list.split("");
  else if (list && !Array.isArray(list) && typeof list === "object") list = Object.values(list);
  if (!Array.isArray(list)) return "";
  const itemName = node.attrs["wx:for-item"] || "item";
  const indexName = node.attrs["wx:for-index"] || "index";
  const { "wx:for": _for, ...rest } = node.attrs;
  const clone = { ...node, attrs: rest };
  return list
    .map((item, index) => {
      const itemScope = { ...scope, [itemName]: item, [indexName]: index };
      if ("wx:if" in rest && !interpolate(rest["wx:if"], itemScope)) return "";
      return renderElement(clone, itemScope, ctx, slot);
    })
    .join("");
}

function htmlAttrs(attrs, scope, ctx, extraStyle = "") {
  const out = [];
  const cls = interpolate(attrs.class, scope);
  if (cls) out.push(`class="${escapeHtml(cls)}"`);
  let style = attrs.style ? rpxToPx(interpolate(attrs.style, scope) || "", ctx) : "";
  if ("hidden" in attrs && interpolate(attrs.hidden, scope)) style += ";display:none";
  style += extraStyle;
  if (style) out.push(`style="${escapeHtml(style)}"`);
  if (attrs.id) out.push(`id="${escapeHtml(interpolate(attrs.id, scope))}"`);
  return out.length ? ` ${out.join(" ")}` : "";
}

function renderElement(node, scope, ctx, slot) {
  const { name, attrs } = node;
  if (name === "block") return renderChildren(node.children, scope, ctx, slot);
  if (name === "slot") return slot ? renderChildren(slot.children, slot.scope, ctx) : "";
  const using = ctx.usingStack[ctx.usingStack.length - 1];
  if (using[name]) {
    const entry = loadComponent(using[name], ctx);
    const data = componentData(entry, attrs, scope);
    ctx.usingStack.push(entry.using);
    const inner = renderChildren(entry.tree.children, data, ctx, { children: node.children, scope });
    ctx.usingStack.pop();
    return `<${name}${htmlAttrs(attrs, scope, ctx)}>${inner}</${name}>`;
  }
  const attr = (key) => interpolate(attrs[key], scope);
  if (name === "image") {
    const mode = attr("mode") || "scaleToFill";
    const fit = { aspectFit: "contain", aspectFill: "cover", widthFix: "cover", heightFix: "cover", scaleToFill: "fill" }[mode] || "fill";
    const extra = mode === "widthFix" ? ";height:auto" : "";
    const imgStyle = mode === "widthFix" ? "height:auto" : `object-fit:${fit}`;
    return `<wx-image${htmlAttrs(attrs, scope, ctx, extra)}><img src="${escapeHtml(assetUrl(attr("src")))}" style="${imgStyle}" alt=""></wx-image>`;
  }
  if (name === "button") {
    const classes = [attr("size") === "mini" ? "wx-btn-mini" : "", attr("plain") ? "wx-btn-plain" : "", attr("type") === "primary" ? "wx-btn-primary" : ""].filter(Boolean);
    const merged = { ...attrs, class: `${classes.join(" ")} ${attrs.class || ""}`.trim() };
    const disabled = attr("disabled") ? " disabled" : "";
    return `<button${htmlAttrs(merged, scope, ctx)}${disabled}>${renderChildren(node.children, scope, ctx, slot)}</button>`;
  }
  if (name === "input") {
    const value = attr("value");
    const placeholder = attr("placeholder");
    return `<input${htmlAttrs(attrs, scope, ctx)} value="${escapeHtml(value ?? "")}" placeholder="${escapeHtml(placeholder ?? "")}">`;
  }
  if (name === "textarea") {
    return `<textarea${htmlAttrs(attrs, scope, ctx)} placeholder="${escapeHtml(attr("placeholder") ?? "")}">${escapeHtml(attr("value") ?? "")}</textarea>`;
  }
  if (name === "switch") {
    const on = Boolean(attr("checked"));
    const color = attr("color") || "#04BE02";
    return `<switch${htmlAttrs(attrs, scope, ctx)}><span class="wx-switch${on ? " on" : ""}" style="${on ? `background:${escapeHtml(color)};border-color:${escapeHtml(color)}` : ""}"></span></switch>`;
  }
  if (name === "checkbox") {
    const on = Boolean(attr("checked"));
    return `<checkbox${htmlAttrs(attrs, scope, ctx)}><span class="wx-checkbox${on ? " on" : ""}" style="${on ? `color:${escapeHtml(attr("color") || "#09BB07")}` : ""}"></span>${renderChildren(node.children, scope, ctx, slot)}</checkbox>`;
  }
  if (name === "slider") {
    const min = Number(attr("min") || 0);
    const max = Number(attr("max") || 100);
    const value = Number(attr("value") || 0);
    const percent = max > min ? ((value - min) / (max - min)) * 100 : 0;
    const active = attr("activeColor") || attr("active-color") || "#1AAD19";
    const back = attr("backgroundColor") || attr("background-color") || "#e9e9e9";
    return `<slider${htmlAttrs(attrs, scope, ctx)}><span class="wx-slider-track" style="background:${escapeHtml(back)}"><span class="wx-slider-fill" style="width:${percent}%;background:${escapeHtml(active)}"></span><span class="wx-slider-thumb" style="left:${percent}%"></span></span></slider>`;
  }
  if (name === "swiper") {
    const current = Number(attr("current") || 0);
    let itemIndex = -1;
    const items = node.children.map((child) => {
      if (child.type !== "el" || child.name !== "swiper-item") return child;
      itemIndex += 1;
      return itemIndex === current ? child : { ...child, attrs: { ...child.attrs, hidden: "{{true}}" } };
    });
    return `<swiper${htmlAttrs(attrs, scope, ctx)}>${renderChildren(items, scope, ctx, slot)}</swiper>`;
  }
  if (name === "rich-text") {
    const nodes = attr("nodes");
    return `<rich-text${htmlAttrs(attrs, scope, ctx)}>${typeof nodes === "string" ? nodes : ""}</rich-text>`;
  }
  const tag = /^[a-z][\w-]*$/.test(name) ? name : "view";
  return `<${tag}${htmlAttrs(attrs, scope, ctx)}>${renderChildren(node.children, scope, ctx, slot)}</${tag}>`;
}

function cssFromFile(file, ctx, seen = new Set()) {
  if (!fs.existsSync(file) || seen.has(file)) return "";
  seen.add(file);
  let css = fs.readFileSync(file, "utf8");
  css = css.replace(/@import\s+["']([^"']+)["']\s*;?/g, (_, target) => cssFromFile(path.resolve(path.dirname(file), target), ctx, seen));
  return convertCss(css, ctx);
}

function convertCss(css, ctx) {
  let out = css.replace(/\/\*[\s\S]*?\*\//g, "");
  // The HTML parser rewrites <image> to <img>, so WXML images render as <wx-image>.
  out = out.replace(/(^|})([^{}@]+)\{/g, (_, close, selector) =>
    `${close}${selector.replace(/(^|[\s,>+~])image(?=$|[\s,.:\[>+~])/g, "$1wx-image")}{`,
  );
  out = out.replace(/(^|[}\s,])page(?=\s*[{,.:\[])/g, "$1.mp-page");
  out = out.replace(/(?:env|constant)\(safe-area-inset-bottom\)/g, `${ctx.safeBottom}px`);
  out = out.replace(/(?:env|constant)\(safe-area-inset-top\)/g, `${ctx.safeTop}px`);
  out = out.replace(/(?:env|constant)\(safe-area-inset-(left|right)\)/g, "0px");
  out = rpxToPx(out, ctx);
  if (ctx.fontScale !== 1) {
    out = out.replace(/((?:^|[;{\s])(?:font-size|--mp-type-[\w-]+)\s*:\s*)(-?\d*\.?\d+)px/g, (_, prefix, number) => `${prefix}${+(Number(number) * ctx.fontScale).toFixed(3)}px`);
  }
  return out;
}

function baseCss(ctx) {
  return `
html, body { margin: 0; padding: 0; }
body { width: ${ctx.width}px; overflow-x: hidden; -webkit-font-smoothing: antialiased; }
.mp-page { display: block; min-height: 100vh; font-size: ${16 * ctx.fontScale}px; }
view, scroll-view, swiper, swiper-item, picker, navigator, page-container, label, slider, rich-text { display: block; }
scroll-view { overflow: auto; }
text { display: inline; white-space: pre-line; }
wx-image { display: inline-block; position: relative; width: 320px; height: 240px; overflow: hidden; }
wx-image > img { display: block; width: 100%; height: 100%; }
swiper { position: relative; height: 150px; overflow: hidden; }
swiper-item { position: absolute; left: 0; top: 0; width: 100%; height: 100%; box-sizing: border-box; overflow: hidden; }
button { position: relative; display: block; box-sizing: border-box; width: 100%; margin: 0 auto; padding: 0 14px; border: 0; border-radius: 5px; overflow: hidden; color: #000; background: #f8f8f8; font: inherit; font-size: ${18 * ctx.fontScale}px; line-height: 2.55555556; text-align: center; }
button.wx-btn-mini { display: inline-block; width: auto; padding: 0 1.34em; font-size: ${13 * ctx.fontScale}px; line-height: 2.3; }
button.wx-btn-primary { color: #fff; background: #07c160; }
input { display: block; box-sizing: border-box; width: 100%; height: 1.4rem; min-height: 1.4rem; border: 0; outline: 0; background: transparent; font: inherit; color: inherit; padding: 0; }
textarea { display: block; box-sizing: border-box; width: 300px; height: 150px; border: 0; outline: 0; background: transparent; font: inherit; color: inherit; padding: 0; resize: none; }
switch { display: inline-block; vertical-align: middle; }
.wx-switch { position: relative; display: inline-block; box-sizing: border-box; width: 52px; height: 32px; border: 1px solid #dfdfdf; border-radius: 16px; background: #dfdfdf; }
.wx-switch::after { content: ""; position: absolute; top: 0; left: 0; width: 30px; height: 30px; border-radius: 15px; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,.4); }
.wx-switch.on::after { left: 20px; }
checkbox { display: inline-flex; align-items: center; gap: 6px; }
.wx-checkbox { display: inline-block; box-sizing: border-box; width: 22px; height: 22px; border: 1px solid #d1d1d1; border-radius: 3px; background: #fff; text-align: center; line-height: 20px; }
.wx-checkbox.on::before { content: "✓"; font-weight: 700; }
slider { margin: 10px 18px; padding: 12px 0; }
.wx-slider-track { position: relative; display: block; height: 2px; border-radius: 2px; }
.wx-slider-fill { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 2px; }
.wx-slider-thumb { position: absolute; top: -13px; width: 28px; height: 28px; margin-left: -14px; border-radius: 50%; background: #fff; box-shadow: 0 0 4px rgba(0,0,0,.2); }
`;
}

/*
 * renderPage("pages/home/index", data, { width: 390, safeBottom: 34, fontScale: 1 })
 * → full HTML document string.
 */
function renderPage(pagePath, data, options = {}) {
  const ctx = {
    width: options.width || 390,
    safeBottom: options.safeBottom ?? 34,
    safeTop: options.safeTop ?? 0,
    fontScale: options.fontScale || 1,
    components: new Map(),
    css: [],
    usingStack: [],
  };
  const base = path.join(ROOT, pagePath);
  const pageJson = readJson(`${base}.json`);
  const appJson = readJson(path.join(ROOT, "app.json"));
  ctx.usingStack.push({ ...(appJson.usingComponents || {}), ...(pageJson.usingComponents || {}) });
  const appCss = cssFromFile(path.join(ROOT, "app.wxss"), ctx);
  const pageCss = cssFromFile(`${base}.wxss`, ctx);
  const tree = parseWxml(fs.readFileSync(`${base}.wxml`, "utf8"));
  let body = renderChildren(tree.children, { ...data }, ctx);
  const tabIndex = TAB_PAGES.indexOf(pagePath);
  if (tabIndex >= 0 && options.tabBar !== false) {
    ctx.usingStack.push({ "custom-tab-bar": "/custom-tab-bar/index" });
    const tabNode = { type: "el", name: "custom-tab-bar", attrs: { selected: String(tabIndex) }, children: [] };
    body += `<div class="wx-tabbar-host">${renderElement(tabNode, {}, ctx)}</div>`;
    ctx.css.push(".wx-tabbar-host { position: fixed; left: 0; right: 0; bottom: 0; z-index: 50; }");
  }
  const title = escapeHtml(pageJson.navigationBarTitleText || appJson.window?.navigationBarTitleText || "");
  return `<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=${ctx.width}"><title>${title}</title><style>${baseCss(ctx)}\n${appCss}\n${ctx.css.join("\n")}\n${pageCss}</style></head><body><div class="mp-page">${body}</div></body></html>`;
}

module.exports = { ROOT, TAB_PAGES, convertCss, evaluate, interpolate, parseWxml, renderPage };
