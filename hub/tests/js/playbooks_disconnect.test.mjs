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

function dashboard() {
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
      if (path === "/instances?limit=50") return [];
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
  const window = { Hub, crypto: { randomUUID: () => "request-1" }, setTimeout, clearTimeout };
  vm.runInContext(source, vm.createContext({ document: { getElementById: get }, window }), { filename: "hub-playbooks.js" });

  return {
    get, requests, resolveCreate, rejectCreate,
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
  });
}
