import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../../src/palimpsest_hub/static/hub-playbooks.js", import.meta.url), "utf8");

class Element {
  constructor(text = "") {
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.textContent = text;
    this.children = [];
    this.elements = {};
    this.listeners = new Map();
  }

  replaceChildren(...children) { this.children = children; }
  append(...children) { this.children.push(...children); }
  get options() { return this.children; }
  scrollIntoView() {}
  addEventListener(type, listener) { this.listeners.set(type, listener); }
  async dispatch(type) {
    const listener = this.listeners.get(type);
    assert.ok(listener, `Missing ${type} listener`);
    await listener({ currentTarget: this, preventDefault() {} });
  }
}

function dashboard({ rows = [], mutate = async () => ({}), clock = () => 0 } = {}) {
  const ids = [
    "playbook-list", "playbook-facts", "playbook-layers", "playbook-title", "playbook-detail",
    "playbook-message", "launch-form", "launch-submit", "launch-reason", "launch-message",
    "launch-name", "launch-lifecycle", "launch-image", "launch-flavor", "launch-network",
    "launch-key", "launch-boot", "launch-writable", "launch-command", "launch-readiness",
    "refresh-playbooks", "refresh-instances", "instance-list", "instance-message",
  ];
  const elements = new Map(ids.map((id) => [id, new Element()]));
  const get = (id) => {
    assert.ok(elements.has(id), `Unexpected element: ${id}`);
    return elements.get(id);
  };
  const job = {
    id: "job-1", name: "Own playbook", status: "complete", visibility: "private",
    output_digests: [`sha256:${"a".repeat(64)}`],
  };
  const requests = [];
  const timers = new Map();
  let timerId = 0;
  let resolveCreate, rejectCreate;
  const create = new Promise((resolve, reject) => { resolveCreate = resolve; rejectCreate = reject; });
  let version = 0, caller = null;
  const Hub = {
    connection: () => version,
    identity: () => caller,
    guard: (expected) => () => {
      if (expected !== version) throw new Error("Connection changed; operation cancelled.");
    },
    node: (_tag, text) => new Element(text),
    message: (id, text) => { get(id).textContent = text; },
    empty: (container, text) => container.replaceChildren(new Element(text)),
    action: (text, listener) => {
      const button = new Element(text);
      button.addEventListener("click", listener);
      return button;
    },
    bodyOptions: (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
    report: (expected, id, error) => {
      if (expected === version) get(id).textContent = error.message;
    },
    json: async (path, options) => {
      if (path === "/image-jobs?limit=100") return [job];
      if (path === "/published-images?limit=200&offset=0") return { items: [] };
      if (path === "/instances?limit=50") return rows;
      if (path.startsWith("/instances/")) {
        requests.push({ path, ...options });
        return mutate(path, options);
      }
      if (path === "/instance-options") return {
        images: [{ id: "boot-1", min_disk_gib: 2, size_bytes: 1024, disk_format: "qcow2" }],
        flavors: [{ id: "flavor-1", vcpus: 2, ram_mib: 4096 }],
        networks: [{ id: "network-1" }], key_names: [],
        limits: { max_boot_gib: 100, max_writable_gib: 50 }, truncated: {},
      };
      assert.equal(path, "/instances", "Unexpected request");
      assert.equal(options.method, "POST");
      requests.push({ path, ...options });
      return create;
    },
    onConnect: [],
    onDisconnect: [],
  };
  let issued = 0;
  const window = {
    Hub, crypto: { randomUUID: () => `request-${++issued}` },
    confirm: () => true, prompt: () => rows[0]?.name,
    setTimeout: (fn) => { timers.set(++timerId, fn); return timerId; },
    clearTimeout: (id) => timers.delete(id),
  };
  vm.runInContext(source, vm.createContext({ document: { getElementById: get }, window, Date: { now: clock } }), { filename: "hub-playbooks.js" });

  return {
    get, requests, resolveCreate, rejectCreate, timers, Hub,
    async connect() {
      version++;
      caller = { project_id: "project-1", capabilities: { upload: true, inventory: true } };
      await Promise.all(Hub.onConnect.map((hook) => hook()));
    },
    disconnect() {
      version++;
      caller = null;
      for (const hook of Hub.onDisconnect) hook();
    },
    async select() {
      const select = get("playbook-list").children[0].children[1].children[0];
      assert.equal(select.textContent, "Select");
      await select.dispatch("click");
      await new Promise(setImmediate);
      assert.equal(get("launch-form").hidden, false, "Own completed playbook permits creation");
      assert.equal(get("launch-image").options.length, 2, "Launch choices loaded");
      for (const [id, value] of Object.entries({
        "launch-name": "test-vm", "launch-lifecycle": "dev", "launch-image": "boot-1",
        "launch-flavor": "flavor-1", "launch-network": "network-1", "launch-boot": "10",
        "launch-writable": "2", "launch-command": "/bin/true",
      })) get(id).value = value;
    },
    submit: () => get("launch-form").dispatch("submit"),
  };
}

test("normal create sends one request and restores the submit button", async () => {
  const page = dashboard();
  await page.connect();
  await page.select();
  const submission = page.submit();
  assert.equal(page.get("launch-submit").disabled, true);
  assert.equal(page.requests.length, 1);
  const body = JSON.parse(page.requests[0].body);
  assert.equal(body.name, "test-vm");
  assert.equal(body.image_job_id, "job-1");
  assert.equal(body.request_id, "request-1");
  page.resolveCreate({ id: "vm-1", name: "test-vm" });
  await submission;
  assert.equal(page.get("launch-submit").disabled, false);
  assert.match(page.get("launch-message").textContent, /VM test-vm was accepted \(vm-1\)/);
  assert.equal(page.requests.length, 1);
});

for (const outcome of ["resolve", "reject"]) {
  test(`disconnect restores submit while a create request will ${outcome}`, async () => {
    const page = dashboard();
    await page.connect();
    await page.select();
    const submission = page.submit();
    assert.equal(page.get("launch-submit").disabled, true);
    assert.equal(page.requests.length, 1);
    page.disconnect();
    const disabledAfterDisconnect = page.get("launch-submit").disabled;
    if (outcome === "resolve") page.resolveCreate({ id: "vm-1", name: "test-vm" });
    else page.rejectCreate(new Error("Request aborted"));
    await submission;
    assert.equal(disabledAfterDisconnect, false, "Disconnect must restore the persistent submit button");
    assert.equal(page.get("launch-submit").disabled, false);
    assert.equal(page.get("launch-message").textContent, "", "Stale completion must not update the disconnected UI");
    await page.connect();
    await page.select();
    assert.equal(page.get("launch-submit").disabled, false, "Create remains available after reconnect");
    assert.equal(page.requests.length, 1, "Reconnect must not repeat the create request");
    // The control works again, and what it sends is a new request, not the old one under the new connection.
    await page.submit().catch(() => {});
    assert.equal(page.requests.length, 2);
    assert.notEqual(JSON.parse(page.requests[1].body).request_id, JSON.parse(page.requests[0].body).request_id);
  });
}

function instance(overrides = {}) {
  return {
    id: "vm-1", name: "demo", status: "ready", lifecycle: "dev", server_id: "server-1",
    provider_phase: "server_attempted", boot_volume_id: "volume-1", server_create_attempted: true,
    image_job_id: "job-1", ...overrides,
  };
}
function buttons(page) { return page.get("instance-list").children[0].children[1].children; }
function button(page, label) { return buttons(page).find((item) => item.textContent === label); }
const settled = () => new Promise(setImmediate);

for (const lifecycle of ["dev", "service"]) {
  test(`console stops ${lifecycle} once and only dev can resume`, async () => {
    const row = instance({ lifecycle }), page = dashboard({
      rows: [row],
      mutate: async (path, options) => {
        if (options?.method === "POST") {
          assert.equal(path, "/instances/vm-1/stop");
          row.status = "stopped";
        }
        return row;
      },
    });
    await page.connect();
    await button(page, "Stop").dispatch("click"); await settled();
    assert.equal(page.requests.filter((request) => request.method === "POST").length, 1);
    assert.equal(!!button(page, "Resume"), lifecycle === "dev");
    assert.equal(!!button(page, "Stop"), false);
    page.disconnect();
  });
}

test("resume polls a new restore result without replacing the sealed result", async () => {
  const row = instance({ status: "shelved", result: { stdout: "first" } });
  let reads = 0;
  const page = dashboard({
    rows: [row],
    mutate: async (path, options) => {
      if (options?.method === "POST") {
        assert.equal(path, "/instances/vm-1/resume");
        row.status = "resumed";
      } else {
        reads++;
        row.status = "ready"; row.resume_result = { stdout: "restored marker" };
      }
      return row;
    },
  });
  await page.connect();
  await button(page, "Resume").dispatch("click"); await settled();
  assert.equal(reads, 1);
  const details = page.get("instance-list").children[0].children[0].children.filter((item) => item.children[0]?.textContent.includes("result"));
  assert.deepEqual(details.map((item) => item.children[0].textContent), ["First result (sealed)", "Latest resume result"]);
  assert.match(details[0].children[1].textContent, /first/);
  assert.match(details[1].children[1].textContent, /restored marker/);
  assert.equal(page.requests.filter((request) => request.method === "POST").length, 1);
  page.disconnect();
});

for (const [status, retryAfter] of [[409, "10"], [503, "1"]]) {
  test(`${status} honours Retry-After through refresh without repeating resume`, async () => {
    let now = 0;
    const row = instance({ status: "shelved" });
    const page = dashboard({
      rows: [row], clock: () => now,
      mutate: async () => { throw Object.assign(new Error(`${status} conflict`), { status, retryAfter }); },
    });
    await page.connect();
    await button(page, "Resume").dispatch("click"); await settled();
    await page.get("refresh-instances").dispatch("click");
    assert.equal(button(page, "Resume").disabled, true);
    assert.equal(page.requests.length, 1);
    assert.equal(page.timers.size, 0);
    now = Number(retryAfter) * 1000;
    await page.get("refresh-instances").dispatch("click");
    assert.equal(button(page, "Resume").disabled, false);
    assert.equal(page.requests.length, 1, "Reading after the interval never reissues the action");
    page.disconnect();
  });
}

test("in-flight mutation survives row refresh and disconnect cannot affect a new connection", async () => {
  let finish;
  const row = instance();
  const page = dashboard({ rows: [row], mutate: () => new Promise((resolve) => { finish = resolve; }) });
  await page.connect();
  await button(page, "Stop").dispatch("click");
  await page.get("refresh-instances").dispatch("click");
  assert.equal(button(page, "Stop").disabled, true);
  assert.equal(button(page, "Delete").disabled, true);
  page.disconnect(); await page.connect();
  finish(row); await settled();
  assert.equal(page.get("instance-message").textContent, "");
  assert.equal(button(page, "Stop").disabled, false);
  assert.equal(page.requests.length, 1);
  page.disconnect();
});

test("completed job output selects its playbook and loads launch choices", async () => {
  const page = dashboard();
  await page.connect();
  await page.Hub.selectPlaybook("job-1"); await settled();
  assert.equal(page.get("playbook-title").textContent, "Own playbook");
  assert.equal(page.get("launch-form").hidden, false);
  assert.equal(page.get("launch-image").options.length, 2);
  page.disconnect();
});
