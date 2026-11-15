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

  click() {
    for (const fn of this.listeners.get("click") ?? []) fn();
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
