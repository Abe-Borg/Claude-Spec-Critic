// Behavioral harness for the HTML report's Ask AI chat (plan WP-12, chunk S04).
//
// Runs the report's exact executable script (the bytes write_html_report put
// on disk, which the report's CSP hash covers) under Node, against a stand-in
// DOM, a scripted `fetch`, and response bodies that stream real SSE bytes.
// It then drives the chat the way a reader would (save a key, send
// questions, press Stop / New chat / Forget key, change the model, leave the
// page) and prints one JSON object describing what the page did.
//
// Usage: node chat_harness.js <script.js> <page.json> <scenario.json>
//
// <page.json> describes the real report: { "elements": [{id, tag, hidden,
// type}], "text": {id: textContent} } for every element with an id, and the
// text of the embedded data blocks. getElementById returns null for any
// other id, as a browser would, so a typo in the script fails here too.
//
// <scenario.json>:
//   storage   "normal" | "throwing" (every Web Storage call throws) |
//             "unavailable" (reading window.sessionStorage itself throws)
//   preload   { "sessionStorage": {...}, "localStorage": {...} }
//   responses one entry per fetch the page makes, in order:
//             { "status": 200, "chunks_b64": [...] }   bytes, one chunk per read
//             options: "id" (for step-driven streams), "manual" (chunks arrive
//             only through steps), "hang" (never reaches end-of-stream),
//             "read_error" (a read rejects after the chunks), "ignore_abort"
//             (abort does not stop the stream, like a late network), and for
//             an error status, "body_text".
//             { "network_error": "..." }   fetch itself rejects
//   steps     actions run in order; see `actions` below.
//
// Output: { ok, requests, snapshots, final, storage_log, storage,
// effects, streams, page_errors, problems, unused_responses }. `problems`
// holds harness-level failures (a timeout, an unscripted request);
// `page_errors` holds exceptions the page's own code let escape. A passing
// scenario has neither.
"use strict";

const fs = require("fs");
const vm = require("vm");

const [scriptPath, pagePath, scenarioPath] = process.argv.slice(2);
const scriptSource = fs.readFileSync(scriptPath, "utf8");
const page = JSON.parse(fs.readFileSync(pagePath, "utf8"));
const scenario = JSON.parse(fs.readFileSync(scenarioPath, "utf8"));

const problems = [];
const pageErrors = [];
const effects = [];
const storageLog = [];
const snapshots = [];

function describeError(err) {
  return String((err && err.stack) || err);
}

// ---------------------------------------------------------------------------
// DOM stand-in
// ---------------------------------------------------------------------------

class ClassList {
  constructor() {
    this.names = [];
  }
  add(...names) {
    names.forEach((n) => {
      if (!this.names.includes(n)) this.names.push(n);
    });
  }
  remove(...names) {
    this.names = this.names.filter((n) => !names.includes(n));
  }
  contains(name) {
    return this.names.includes(name);
  }
  toggle(name, force) {
    const on = force === undefined ? !this.contains(name) : Boolean(force);
    if (on) this.add(name);
    else this.remove(name);
    return on;
  }
}

class HarnessEvent {
  constructor(type, init) {
    this.type = type;
    this.target = null;
    this.defaultPrevented = false;
    Object.assign(this, init || {});
  }
  preventDefault() {
    this.defaultPrevented = true;
  }
  stopPropagation() {}
}

class TextNode {
  constructor(text) {
    this.nodeType = 3;
    this.nodeName = "#text";
    this.data = String(text);
    this.parentNode = null;
  }
  get textContent() {
    return this.data;
  }
  set textContent(value) {
    this.data = String(value);
  }
  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }
}

// Selectors the page uses: "tag", ".cls", "#id", "tag.cls", an optional
// ":not(.cls)", and comma lists. Anything else matches nothing.
const COMPOUND = /^([a-zA-Z][\w-]*)?((?:[.#][\w-]+)*)(?::not\(\.([\w-]+)\))?$/;

function matches(element, selectorList) {
  return String(selectorList)
    .split(",")
    .some((selector) => {
      const m = COMPOUND.exec(selector.trim());
      if (!m || !(m[1] || m[2])) return false;
      if (m[1] && element.tagName !== m[1].toUpperCase()) return false;
      for (const part of m[2].match(/[.#][\w-]+/g) || []) {
        if (part[0] === "." && !element.classList.contains(part.slice(1))) return false;
        if (part[0] === "#" && element.id !== part.slice(1)) return false;
      }
      return !(m[3] && element.classList.contains(m[3]));
    });
}

function walk(root, visit) {
  for (const node of root.childNodes) {
    if (node.nodeType !== 1) continue;
    visit(node);
    walk(node, visit);
  }
}

class Element {
  constructor(tagName, id) {
    this.nodeType = 1;
    this.tagName = String(tagName).toUpperCase();
    this.nodeName = this.tagName;
    this.id = id || "";
    this.childNodes = [];
    this.parentNode = null;
    this.listeners = Object.create(null);
    this.classList = new ClassList();
    this.style = {};
    this.attributes = Object.create(null);
    this.hidden = false;
    this.disabled = false;
    this.open = false;
    this.type = "";
    this.href = "";
    this.target = "";
    this.rel = "";
    this.title = "";
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this._value = "";
    this._selectedIndex = 0;
  }
  get className() {
    return this.classList.names.join(" ");
  }
  set className(value) {
    this.classList.names = String(value).split(/\s+/).filter(Boolean);
  }
  get children() {
    return this.childNodes.filter((node) => node.nodeType === 1);
  }
  get firstChild() {
    return this.childNodes[0] || null;
  }
  get lastChild() {
    return this.childNodes[this.childNodes.length - 1] || null;
  }
  get textContent() {
    return this.childNodes.map((node) => node.textContent).join("");
  }
  set textContent(value) {
    this.childNodes.forEach((node) => {
      node.parentNode = null;
    });
    this.childNodes = [];
    const text = value === null || value === undefined ? "" : String(value);
    if (text) this.appendChild(new TextNode(text));
  }
  get options() {
    return this.tagName === "SELECT" ? this.children.filter((c) => c.tagName === "OPTION") : undefined;
  }
  // A select reports its selected option's value; setting a value no option
  // carries leaves nothing selected, as in a browser.
  get value() {
    if (this.tagName === "SELECT") {
      const options = this.options;
      const i = this._selectedIndex;
      return i >= 0 && i < options.length ? options[i].value : "";
    }
    return this._value;
  }
  set value(value) {
    const text = value === null || value === undefined ? "" : String(value);
    if (this.tagName === "SELECT") {
      this._selectedIndex = this.options.findIndex((o) => o.value === text);
      return;
    }
    this._value = text;
  }
  hasChildNodes() {
    return this.childNodes.length > 0;
  }
  appendChild(node) {
    if (node.tagName === "#FRAGMENT") {
      node.childNodes.slice().forEach((child) => this.appendChild(child));
      return node;
    }
    if (node.parentNode) node.parentNode.removeChild(node);
    this.childNodes.push(node);
    node.parentNode = this;
    return node;
  }
  append(...nodes) {
    nodes.forEach((node) => this.appendChild(typeof node === "string" ? new TextNode(node) : node));
  }
  insertBefore(node, reference) {
    if (!reference) return this.appendChild(node);
    if (node.parentNode) node.parentNode.removeChild(node);
    const at = this.childNodes.indexOf(reference);
    this.childNodes.splice(at < 0 ? this.childNodes.length : at, 0, node);
    node.parentNode = this;
    return node;
  }
  removeChild(node) {
    const at = this.childNodes.indexOf(node);
    if (at >= 0) this.childNodes.splice(at, 1);
    node.parentNode = null;
    return node;
  }
  replaceChild(newNode, oldNode) {
    const at = this.childNodes.indexOf(oldNode);
    if (at < 0) return oldNode;
    const incoming = newNode.tagName === "#FRAGMENT" ? newNode.childNodes.slice() : [newNode];
    incoming.forEach((node) => {
      if (node.parentNode) node.parentNode.removeChild(node);
      node.parentNode = this;
    });
    this.childNodes.splice(this.childNodes.indexOf(oldNode), 1, ...incoming);
    oldNode.parentNode = null;
    return oldNode;
  }
  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }
  normalize() {}
  contains(node) {
    for (let n = node; n; n = n.parentNode) if (n === this) return true;
    return false;
  }
  closest(selector) {
    for (let n = this; n && n.nodeType === 1; n = n.parentNode) if (matches(n, selector)) return n;
    return null;
  }
  matches(selector) {
    return matches(this, selector);
  }
  querySelectorAll(selector) {
    const found = [];
    walk(this, (node) => {
      if (matches(node, selector)) found.push(node);
    });
    return found;
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
  getElementsByTagName(tag) {
    return this.querySelectorAll(tag);
  }
  addEventListener(type, handler) {
    (this.listeners[type] = this.listeners[type] || []).push(handler);
  }
  removeEventListener(type, handler) {
    this.listeners[type] = (this.listeners[type] || []).filter((h) => h !== handler);
  }
  // Like a browser, an exception in one listener is reported, not thrown to
  // whoever dispatched the event. The harness reports it as a page error.
  dispatchEvent(event) {
    if (!event.target) event.target = this;
    (this.listeners[event.type] || []).slice().forEach((handler) => {
      try {
        handler.call(this, event);
      } catch (err) {
        pageErrors.push(`listener for ${event.type} on #${this.id || this.tagName}: ${describeError(err)}`);
      }
    });
    return !event.defaultPrevented;
  }
  click() {
    this.dispatchEvent(new HarnessEvent("click"));
  }
  focus() {}
  blur() {}
  select() {}
  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }
  getAttribute(name) {
    return name in this.attributes ? this.attributes[name] : null;
  }
  removeAttribute(name) {
    delete this.attributes[name];
  }
  hasAttribute(name) {
    return name in this.attributes;
  }
  scrollIntoView() {
    effects.push({ effect: "scrollIntoView", id: this.id });
  }
  getBoundingClientRect() {
    return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };
  }
}

const body = new Element("body");
const mainElement = new Element("main");
body.appendChild(mainElement);
const elementsById = new Map();
for (const spec of page.elements) {
  const element = new Element(spec.tag, spec.id);
  element.hidden = Boolean(spec.hidden);
  if (spec.type) element.type = spec.type;
  if (Object.prototype.hasOwnProperty.call(page.text, spec.id)) element.textContent = page.text[spec.id];
  elementsById.set(spec.id, element);
  (spec.tag === "section" ? mainElement : body).appendChild(element);
}

const documentListeners = Object.create(null);
const document = {
  body,
  documentElement: new Element("html"),
  readyState: "complete",
  title: "Spec Critic report",
  getElementById(id) {
    return elementsById.get(String(id)) || null;
  },
  querySelector(selector) {
    return body.querySelector(selector);
  },
  querySelectorAll(selector) {
    return body.querySelectorAll(selector);
  },
  createElement(tag) {
    return new Element(tag);
  },
  createTextNode(text) {
    return new TextNode(text);
  },
  createDocumentFragment() {
    return new Element("#fragment");
  },
  createTreeWalker(root) {
    const nodes = [];
    (function collect(node) {
      node.childNodes.forEach((child) => {
        if (child.nodeType === 3) nodes.push(child);
        else collect(child);
      });
    })(root);
    let at = -1;
    return {
      get currentNode() {
        return nodes[at];
      },
      nextNode() {
        at += 1;
        return nodes[at] || null;
      },
    };
  },
  addEventListener(type, handler) {
    (documentListeners[type] = documentListeners[type] || []).push(handler);
  },
  removeEventListener() {},
  execCommand() {
    return false;
  },
};

// ---------------------------------------------------------------------------
// Web Storage
// ---------------------------------------------------------------------------

const storageMode = scenario.storage || "normal";
const preload = scenario.preload || {};

function makeStorage(name) {
  const map = new Map(Object.entries(preload[name] || {}));
  function guard(op, entry) {
    if (entry) storageLog.push(Object.assign({ storage: name, op }, entry, storageMode === "throwing" ? { failed: true } : {}));
    if (storageMode === "throwing") throw new Error(`SecurityError: ${name}.${op} is blocked`);
  }
  return {
    getItem(key) {
      guard("getItem");
      key = String(key);
      return map.has(key) ? map.get(key) : null;
    },
    setItem(key, value) {
      guard("set", { key: String(key), value: String(value) });
      map.set(String(key), String(value));
    },
    removeItem(key) {
      guard("remove", { key: String(key) });
      map.delete(String(key));
    },
    clear() {
      guard("clear", {});
      map.clear();
    },
    key(index) {
      guard("key");
      return Array.from(map.keys())[index] ?? null;
    },
    get length() {
      guard("length");
      return map.size;
    },
    dump() {
      return Object.fromEntries(map);
    },
  };
}

const stores = { sessionStorage: makeStorage("sessionStorage"), localStorage: makeStorage("localStorage") };

// ---------------------------------------------------------------------------
// fetch and streamed response bodies
// ---------------------------------------------------------------------------

const requests = [];
const responseQueue = (scenario.responses || []).slice();
const streamsById = new Map();
const allStreams = [];

function abortError() {
  return new DOMException("The operation was aborted.", "AbortError");
}

class ScriptedStream {
  constructor(spec, signal) {
    this.spec = spec;
    this.queue = (spec.chunks_b64 || []).map((b64) => ({ bytes: Uint8Array.from(Buffer.from(b64, "base64")) }));
    if (!spec.manual) {
      if (spec.read_error) this.queue.push({ error: spec.read_error });
      else if (!spec.hang) this.queue.push({ eof: true });
    }
    this.waiter = null;
    this.aborted = false;
    this.cancelled = false;
    this.reads = 0;
    if (signal) {
      signal.addEventListener("abort", () => {
        if (spec.ignore_abort) return;
        this.aborted = true;
        this.settle();
      });
    }
  }
  push(entry) {
    this.queue.push(entry);
    this.settle();
  }
  settle() {
    if (!this.waiter) return;
    const { resolve, reject } = this.waiter;
    if (this.aborted) {
      this.waiter = null;
      reject(abortError());
      return;
    }
    if (this.cancelled) {
      this.waiter = null;
      resolve({ done: true, value: undefined });
      return;
    }
    const next = this.queue[0];
    if (!next) return;
    this.waiter = null;
    if (next.eof) {
      resolve({ done: true, value: undefined });
      return;
    }
    this.queue.shift();
    if (next.error) reject(new TypeError(next.error));
    else resolve({ done: false, value: next.bytes });
  }
  body() {
    const stream = this;
    const cancel = () => {
      stream.cancelled = true;
      stream.settle();
      return Promise.resolve();
    };
    return {
      getReader() {
        return {
          read() {
            stream.reads += 1;
            // Each read settles on a later macrotask, as a network read would.
            // setImmediate rather than setTimeout keeps one-byte streams fast.
            return new Promise((resolve, reject) => {
              stream.waiter = { resolve, reject };
              setImmediate(() => stream.settle());
            });
          },
          cancel,
          releaseLock() {},
        };
      },
      cancel,
    };
  }
}

function fetchMock(url, init) {
  init = init || {};
  let requestBody;
  try {
    requestBody = JSON.parse(init.body);
  } catch (err) {
    requestBody = { unparsed: String(init.body) };
  }
  requests.push({
    url: String(url),
    method: init.method || "GET",
    headers: Object.assign({}, init.headers || {}),
    body: requestBody,
  });
  const spec = responseQueue.shift();
  if (!spec) {
    problems.push(`the page sent request ${requests.length}, but the scenario scripted no response for it`);
    return Promise.reject(new TypeError("no scripted response"));
  }
  const signal = init.signal;
  if (signal && signal.aborted) return Promise.reject(abortError());
  if (spec.network_error) return Promise.reject(new TypeError(spec.network_error));
  const status = spec.status || 200;
  const stream = new ScriptedStream(spec, signal);
  allStreams.push({ id: spec.id || `#${requests.length}`, stream });
  if (spec.id) streamsById.set(spec.id, stream);
  const response = {
    ok: status >= 200 && status < 300,
    status,
    headers: {
      get(name) {
        return (spec.headers || {})[String(name).toLowerCase()] ?? null;
      },
    },
    body: stream.body(),
    text() {
      return Promise.resolve(spec.body_text || "");
    },
  };
  return new Promise((resolve, reject) => {
    let settled = false;
    if (signal) {
      signal.addEventListener("abort", () => {
        if (settled || spec.ignore_abort) return;
        settled = true;
        reject(abortError());
      });
    }
    setImmediate(() => {
      if (settled) return;
      settled = true;
      resolve(response);
    });
  });
}

// ---------------------------------------------------------------------------
// The page's global scope
// ---------------------------------------------------------------------------

const windowListeners = Object.create(null);
const sandbox = {
  document,
  console,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  queueMicrotask,
  TextDecoder,
  TextEncoder,
  AbortController,
  DOMException,
  URL,
  Event: HarnessEvent,
  NodeFilter: { SHOW_TEXT: 4, FILTER_ACCEPT: 1, FILTER_REJECT: 2, FILTER_SKIP: 3 },
  navigator: { clipboard: { writeText: () => Promise.resolve() }, userAgent: "spec-critic-chat-harness" },
  location: { href: "file:///report.html", hash: "", protocol: "file:" },
  requestAnimationFrame: (callback) => setTimeout(() => callback(Date.now()), 0),
  cancelAnimationFrame: (handle) => clearTimeout(handle),
  getSelection: () => ({ toString: () => "", rangeCount: 0, anchorNode: null }),
  scrollX: 0,
  scrollY: 0,
  print() {},
  alert() {},
  confirm: () => false,
  fetch: fetchMock,
  addEventListener(type, handler) {
    (windowListeners[type] = windowListeners[type] || []).push(handler);
  },
  removeEventListener() {},
};
// window.sessionStorage / window.localStorage. In "unavailable" mode reading
// the property itself throws, as some browsers do when storage is blocked.
// The accessors are installed from inside the context (see main): Node's
// sandbox interceptor turns an accessor defined on the sandbox object that
// throws into a plain `undefined`, which would simulate missing storage, not
// storage whose access throws.
function storageAccess(name) {
  if (storageMode === "unavailable") {
    storageLog.push({ storage: name, op: "access", denied: true });
    throw new Error(`SecurityError: access to ${name} is denied`);
  }
  return stores[name];
}
const INSTALL_STORAGE = `(function (access) {
  ["sessionStorage", "localStorage"].forEach(function (name) {
    Object.defineProperty(globalThis, name, {
      configurable: true, enumerable: true, get: function () { return access(name); }
    });
  });
})`;
sandbox.window = sandbox;
sandbox.self = sandbox;

process.on("unhandledRejection", (reason) => pageErrors.push(`unhandled rejection: ${describeError(reason)}`));
process.on("uncaughtException", (err) => pageErrors.push(`uncaught exception: ${describeError(err)}`));

// ---------------------------------------------------------------------------
// Steps
// ---------------------------------------------------------------------------

function el(id) {
  const element = elementsById.get(id);
  if (!element) throw new Error(`the report has no element #${id}`);
  return element;
}

function stream(id) {
  const found = streamsById.get(id);
  if (!found) throw new Error(`no response stream "${id}" has been requested yet`);
  return found;
}

function bytesOf(b64List) {
  return (b64List || []).map((b64) => ({ bytes: Uint8Array.from(Buffer.from(b64, "base64")) }));
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

async function ticks(count) {
  for (let i = 0; i < count; i += 1) await tick();
}

async function waitFor(predicate, what, limitMs = 5000) {
  const started = Date.now();
  while (!predicate()) {
    if (Date.now() - started > limitMs) throw new Error(`timed out waiting for ${what}`);
    await tick();
  }
}

function isIdle() {
  return !el("sc-chat-send").disabled && el("sc-chat-stop").hidden;
}

function snapshot(label) {
  return {
    label,
    messages: el("sc-chat-messages").children.map((node) => ({
      classes: node.classList.names.slice(),
      text: node.textContent,
      links: node.querySelectorAll("a").map((a) => ({ href: a.href, text: a.textContent })),
    })),
    send_disabled: el("sc-chat-send").disabled,
    stop_hidden: el("sc-chat-stop").hidden,
    input_disabled: el("sc-chat-input").disabled,
    input_value: el("sc-chat-input").value,
    status: el("sc-chat-status").textContent,
    ready: el("sc-chat").classList.contains("sc-ready"),
    key_message: el("sc-chat-keymsg").textContent,
    key_field: el("sc-chat-key").value,
    starters_hidden: el("sc-chat-starters").hidden,
    model: el("sc-chat-model").value,
    effort: el("sc-chat-effort").value,
    request_count: requests.length,
  };
}

const actions = {
  async open() {
    el("sc-chat-toggle").click();
  },
  async save_key(step) {
    el("sc-chat-key").value = step.key;
    el("sc-chat-keysave").click();
  },
  async send(step) {
    el("sc-chat-input").value = step.text;
    if (step.via === "enter") {
      el("sc-chat-input").dispatchEvent(new HarnessEvent("keydown", { key: "Enter", shiftKey: false }));
    } else {
      el("sc-chat-send").click();
    }
  },
  async click(step) {
    el(step.id).click();
  },
  async select(step) {
    const select = el(step.id);
    select.value = step.value;
    select.dispatchEvent(new HarnessEvent("change"));
  },
  async push(step) {
    const target = stream(step.response);
    bytesOf(step.chunks_b64).forEach((entry) => target.push(entry));
  },
  async end(step) {
    stream(step.response).push({ eof: true });
  },
  async fail(step) {
    stream(step.response).push({ error: step.message || "network error" });
  },
  async wait_idle() {
    await waitFor(isIdle, "the chat to finish its turn");
    await ticks(5);
  },
  async wait_requests(step) {
    await waitFor(() => requests.length >= step.count, `${step.count} request(s)`);
    await ticks(3);
  },
  async wait_text(step) {
    await waitFor(() => el("sc-chat-messages").textContent.includes(step.text), `the text ${JSON.stringify(step.text)}`);
  },
  async settle(step) {
    await ticks(step.ticks || 20);
  },
  async snapshot(step) {
    snapshots.push(snapshot(step.label));
  },
  async pagehide() {
    (windowListeners.pagehide || []).forEach((handler) => handler.call(sandbox, new HarnessEvent("pagehide")));
  },
};

function finish(result) {
  const text = JSON.stringify(result);
  process.stdout.write(text, () => process.exit(0));
}

async function main() {
  try {
    vm.createContext(sandbox);
    vm.runInContext(INSTALL_STORAGE, sandbox)(storageAccess);
    vm.runInContext(scriptSource, sandbox, { filename: "report-script.js" });
    (documentListeners.DOMContentLoaded || []).forEach((handler) => handler(new HarnessEvent("DOMContentLoaded")));
  } catch (err) {
    finish({ ok: false, stage: "load", error: describeError(err) });
    return;
  }
  const steps = scenario.steps || [];
  for (let i = 0; i < steps.length; i += 1) {
    const step = steps[i];
    const action = actions[step.do];
    if (!action) {
      problems.push(`step ${i}: unknown action "${step.do}"`);
      break;
    }
    try {
      await action(step);
    } catch (err) {
      problems.push(`step ${i} (${step.do}): ${err && err.message ? err.message : err}`);
      break;
    }
  }
  await ticks(10);
  finish({
    ok: true,
    requests,
    snapshots,
    final: snapshot("final"),
    storage_log: storageLog,
    storage: { sessionStorage: stores.sessionStorage.dump(), localStorage: stores.localStorage.dump() },
    effects,
    streams: allStreams.map(({ id, stream: s }) => ({ id, reads: s.reads, cancelled: s.cancelled, aborted: s.aborted })),
    page_errors: pageErrors,
    problems,
    unused_responses: responseQueue.length,
  });
}

main();
