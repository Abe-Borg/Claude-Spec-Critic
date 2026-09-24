// Runs the HTML report's exact executable script under Node and reports where
// an API key typed into the Ask AI panel ends up.
//
// Used by tests/test_plan_open_defects.py (plan WP-12, chunk S01). It is a
// narrow probe, not the behavioral chat harness S04 builds: it loads the
// script against a permissive stand-in DOM, types a key into the key field,
// clicks "Save key", and prints one JSON object describing every Web Storage
// write, the final storage contents, and whether the chat became ready.
//
// Usage: node chat_key_probe.js <script.js> <elements.json> <fake-key>
//
// <elements.json> maps element ids to the text content the report embeds
// (the JSON data blocks and the plaintext copy), read from the real report.
// Every other element the script asks for is a generic stand-in, so the
// script's own wiring runs unmodified. The probe never fails silently: a
// load error or a missing handler is reported as {"ok": false, ...} so the
// Python side can fail loudly instead of reading an empty result as a pass.
"use strict";

const fs = require("fs");
const vm = require("vm");

const [scriptPath, elementsPath, fakeKey] = process.argv.slice(2);
const scriptSource = fs.readFileSync(scriptPath, "utf8");
const elementText = JSON.parse(fs.readFileSync(elementsPath, "utf8"));

const storageWrites = [];

function makeStorage(name) {
  const map = new Map();
  return {
    getItem(key) {
      key = String(key);
      return map.has(key) ? map.get(key) : null;
    },
    setItem(key, value) {
      storageWrites.push({ storage: name, key: String(key), value: String(value) });
      map.set(String(key), String(value));
    },
    removeItem(key) {
      map.delete(String(key));
    },
    clear() {
      map.clear();
    },
    key(index) {
      return Array.from(map.keys())[index] ?? null;
    },
    get length() {
      return map.size;
    },
    dump() {
      return Object.fromEntries(map);
    },
  };
}

class ClassList {
  constructor() {
    this.names = new Set();
  }
  add(...names) {
    names.forEach((n) => this.names.add(n));
  }
  remove(...names) {
    names.forEach((n) => this.names.delete(n));
  }
  contains(name) {
    return this.names.has(name);
  }
  toggle(name, force) {
    const on = force === undefined ? !this.names.has(name) : Boolean(force);
    if (on) this.names.add(name);
    else this.names.delete(name);
    return on;
  }
}

class FakeElement {
  constructor(tagName, id) {
    this.tagName = String(tagName || "div").toUpperCase();
    this.id = id || "";
    this.children = [];
    this.listeners = {};
    this.classList = new ClassList();
    this.style = {};
    this.dataset = {};
    this.attributes = {};
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.textContent = "";
    this.innerHTML = "";
    this.className = "";
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.open = false;
    this.parentNode = null;
  }
  addEventListener(type, handler) {
    (this.listeners[type] = this.listeners[type] || []).push(handler);
  }
  removeEventListener(type, handler) {
    this.listeners[type] = (this.listeners[type] || []).filter((h) => h !== handler);
  }
  dispatchEvent(event) {
    const handlers = this.listeners[event.type] || [];
    handlers.forEach((handler) => handler.call(this, event));
    return true;
  }
  click() {
    this.dispatchEvent({
      type: "click",
      target: this,
      preventDefault() {},
      stopPropagation() {},
    });
  }
  focus() {}
  blur() {}
  select() {}
  appendChild(child) {
    this.children.push(child);
    if (child && typeof child === "object") child.parentNode = this;
    return child;
  }
  append(...nodes) {
    nodes.forEach((node) => this.appendChild(node));
  }
  prepend(...nodes) {
    nodes.forEach((node) => this.children.unshift(node));
  }
  insertBefore(child) {
    return this.appendChild(child);
  }
  removeChild(child) {
    this.children = this.children.filter((c) => c !== child);
    return child;
  }
  replaceChildren(...nodes) {
    this.children = nodes;
  }
  remove() {}
  querySelector() {
    return new FakeElement("div");
  }
  querySelectorAll() {
    return [];
  }
  getElementsByTagName() {
    return [];
  }
  closest() {
    return null;
  }
  contains() {
    return false;
  }
  matches() {
    return false;
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
  }
  removeAttribute(name) {
    delete this.attributes[name];
  }
  hasAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name);
  }
  scrollIntoView() {}
  getBoundingClientRect() {
    return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };
  }
  cloneNode() {
    return new FakeElement(this.tagName);
  }
}

const elementsById = new Map();
function getElementById(id) {
  if (!elementsById.has(id)) {
    const element = new FakeElement("div", id);
    if (Object.prototype.hasOwnProperty.call(elementText, id)) {
      element.textContent = elementText[id];
    }
    elementsById.set(id, element);
  }
  return elementsById.get(id);
}

const fetchCalls = [];
const documentListeners = {};
const document = {
  getElementById,
  querySelector() {
    return new FakeElement("div");
  },
  querySelectorAll() {
    return [];
  },
  getElementsByTagName() {
    return [];
  },
  createElement(tagName) {
    return new FakeElement(tagName);
  },
  createTextNode(text) {
    return { nodeType: 3, textContent: String(text) };
  },
  createDocumentFragment() {
    return new FakeElement("fragment");
  },
  createTreeWalker() {
    return { nextNode: () => null };
  },
  createRange() {
    return { selectNodeContents() {}, setStart() {}, setEnd() {} };
  },
  addEventListener(type, handler) {
    (documentListeners[type] = documentListeners[type] || []).push(handler);
  },
  removeEventListener() {},
  execCommand() {
    return false;
  },
  body: new FakeElement("body"),
  documentElement: new FakeElement("html"),
  readyState: "complete",
  title: "Spec Critic report",
};

const sandbox = {
  document,
  console,
  JSON,
  Math,
  Date,
  Promise,
  Array,
  Object,
  String,
  Number,
  Boolean,
  RegExp,
  Error,
  TypeError,
  SyntaxError,
  Map,
  Set,
  WeakMap,
  Symbol,
  Uint8Array,
  TextDecoder,
  TextEncoder,
  AbortController,
  URL,
  isNaN,
  isFinite,
  parseInt,
  parseFloat,
  encodeURIComponent,
  decodeURIComponent,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  queueMicrotask,
  sessionStorage: makeStorage("sessionStorage"),
  localStorage: makeStorage("localStorage"),
  NodeFilter: { SHOW_TEXT: 4, FILTER_ACCEPT: 1, FILTER_REJECT: 2, FILTER_SKIP: 3 },
  navigator: { clipboard: { writeText: () => Promise.resolve() }, userAgent: "node-probe" },
  location: { href: "file:///report.html", hash: "", protocol: "file:" },
  CSS: { escape: (value) => String(value) },
  requestAnimationFrame: (callback) => setTimeout(callback, 0),
  getSelection: () => ({ toString: () => "", rangeCount: 0, removeAllRanges() {} }),
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  print() {},
  alert() {},
  confirm: () => false,
  fetch: (...args) => {
    fetchCalls.push(String(args[0]));
    return new Promise(() => {});
  },
  addEventListener() {},
  removeEventListener() {},
};
sandbox.window = sandbox;
sandbox.self = sandbox;
sandbox.globalThis = sandbox;

function report(payload) {
  process.stdout.write(JSON.stringify(payload));
}

try {
  vm.createContext(sandbox);
  vm.runInContext(scriptSource, sandbox, { filename: "report-script.js" });
  (documentListeners.DOMContentLoaded || []).forEach((handler) => handler({ type: "DOMContentLoaded" }));
} catch (error) {
  report({ ok: false, stage: "load", error: String((error && error.stack) || error) });
  process.exit(0);
}

const keyInput = getElementById("sc-chat-key");
const saveButton = getElementById("sc-chat-keysave");
if (!(saveButton.listeners.click || []).length) {
  report({ ok: false, stage: "wiring", error: "no click handler on #sc-chat-keysave" });
  process.exit(0);
}

try {
  keyInput.value = fakeKey;
  saveButton.click();
} catch (error) {
  report({ ok: false, stage: "save", error: String((error && error.stack) || error) });
  process.exit(0);
}

setTimeout(() => {
  report({
    ok: true,
    storageWrites,
    sessionStorage: sandbox.sessionStorage.dump(),
    localStorage: sandbox.localStorage.dump(),
    chatReady: getElementById("sc-chat").classList.contains("sc-ready"),
    keyFieldCleared: keyInput.value === "",
    fetchCalls,
  });
}, 0);
