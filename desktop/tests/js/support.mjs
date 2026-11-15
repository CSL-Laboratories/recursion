// Shared by the desktop's JavaScript checks: the fixtures, and a stand-in document that refuses markup.

import { readFileSync } from "node:fs";

export function fixture(name) {
  return JSON.parse(readFileSync(new URL(`../fixtures/${name}`, import.meta.url), "utf8"));
}

class FakeStyle {
  constructor() {
    this.props = new Map();
  }

  setProperty(name, value) {
    this.props.set(name, value);
  }
}

class FakeText {
  constructor(text) {
    this.nodeType = 3;
    this.data = String(text);
  }

  get textContent() {
    return this.data;
  }
}

export class FakeElement {
  constructor(tag) {
    this.nodeType = 1;
    this.tagName = tag.toUpperCase();
    this.attributes = new Map();
    this.childNodes = [];
    this.listeners = new Map();
    this.style = new FakeStyle();
  }

  get children() {
    return this.childNodes.filter((n) => n.nodeType === 1);
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  hasAttribute(name) {
    return this.attributes.has(name);
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  get id() {
    return this.getAttribute("id");
  }

  // data-* attributes, as the DOM's dataset names them (data-theme-scope is themeScope).
  get dataset() {
    const out = {};
    for (const [name, value] of this.attributes) {
      if (name.startsWith("data-")) out[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = value;
    }
    return Object.freeze(out);
  }

  get hidden() {
    return this.attributes.has("hidden");
  }

  set hidden(on) {
    if (on) this.attributes.set("hidden", "");
    else this.attributes.delete("hidden");
  }

  get value() {
    return this._value ?? "";
  }

  set value(text) {
    this._value = String(text);
  }

  appendChild(node) {
    this.childNodes.push(node);
    return node;
  }

  replaceChildren(...nodes) {
    this.childNodes = [...nodes];
  }

  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }

  // Runs every click handler and gives back what they return together, so a check can wait for them.
  click() {
    return Promise.all((this.listeners.get("click") ?? []).map((fn) => fn()));
  }

  set textContent(value) {
    this.childNodes = [new FakeText(value)];
  }

  get textContent() {
    return this.childNodes.map((n) => n.textContent).join("");
  }

  set innerHTML(_value) {
    throw new Error("markup was written into the document");
  }

  set outerHTML(_value) {
    throw new Error("markup was written into the document");
  }

  insertAdjacentHTML() {
    throw new Error("markup was written into the document");
  }

  walk() {
    return [this, ...this.children.flatMap((c) => c.walk())];
  }

  find(predicate) {
    return this.walk().filter(predicate);
  }
}

export const fakeDocument = {
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => new FakeText(text),
};

const VOID = new Set(["meta", "link", "input", "br", "img", "hr"]);
const TAG = /<(\/?)([a-zA-Z][a-zA-Z0-9]*)((?:\s+[a-zA-Z-]+(?:="[^"]*")?)*)\s*>/g;
const ATTR = /([a-zA-Z-]+)(?:="([^"]*)")?/g;

// A stand-in of the window: the elements of ui/index.html, nested as written, with the two selectors the app
// uses. Any other selector is refused, so a new one in app.js has to be added here on purpose.
export function windowDocument() {
  const html = readFileSync(new URL("../../ui/index.html", import.meta.url), "utf8");
  const root = new FakeElement("document");
  const open = [root];
  for (const [, closing, tag, attrs] of html.matchAll(TAG)) {
    const name = tag.toLowerCase();
    if (closing) {
      while (open.length > 1 && open.pop().tagName !== name.toUpperCase());
      continue;
    }
    const node = new FakeElement(name);
    for (const [, attr, value] of attrs.matchAll(ATTR)) node.setAttribute(attr, value ?? "");
    open[open.length - 1].appendChild(node);
    if (!VOID.has(name)) open.push(node);
  }
  const all = () => root.walk().slice(1);
  const within = (tag, parentTag, direct) => all().filter((n) => n.tagName === tag && (direct
    ? parentOf(n)?.tagName === parentTag
    : ancestors(n).some((a) => a.tagName === parentTag)));
  const parentOf = (node) => all().concat(root).find((p) => p.childNodes.includes(node));
  const ancestors = (node) => {
    const out = [];
    for (let p = parentOf(node); p; p = parentOf(p)) out.push(p);
    return out;
  };
  return {
    ...fakeDocument,
    root,
    // Shown: neither the element nor any element around it is hidden, so an operator can see and press it.
    isShown: (node) => !node.hidden && ancestors(node).every((a) => !a.hidden),
    getElementById: (id) => all().find((n) => n.getAttribute("id") === id) ?? null,
    querySelectorAll(selector) {
      if (selector === "main > section") return within("SECTION", "MAIN", true);
      if (selector === "nav button") return within("BUTTON", "NAV", false);
      throw new Error(`the window stand-in has no selector ${selector}`);
    },
  };
}
