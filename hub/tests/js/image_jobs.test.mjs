import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const asset = (name) => readFileSync(new URL(`../../src/palimpsest_hub/static/${name}`, import.meta.url), "utf8");
class Element {
  constructor(text = "") { this.textContent = text; this.value = ""; this.children = []; this.elements = []; this.listeners = new Map(); this.disabled = false; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  addEventListener(type, fn) { this.listeners.set(type, fn); }
  async dispatch(type) { await this.listeners.get(type)({ preventDefault() {}, currentTarget: this }); }
}

function dashboard({ admit = async () => ({ id: "job-1", status: "queued" }), upload = true } = {}) {
  const elements = new Map([...asset("hub.html").matchAll(/\bid="([^"]+)"/g)].map((match) => [match[1], new Element()]));
  const get = (id) => { assert.ok(elements.has(id), `Missing markup: ${id}`); return elements.get(id); };
  let now = 0, issued = 0, timerId = 0;
  const requests = [], timers = new Map(), selected = [], jobs = [];
  const window = {
    crypto: { randomUUID: () => `request-${++issued}` }, addEventListener() {},
    setTimeout: (fn) => { timers.set(++timerId, fn); return timerId; }, clearTimeout: (id) => timers.delete(id),
  };
  const context = vm.createContext({
    window, document: { getElementById: get, createElement: () => new Element() }, Headers, AbortController,
    URLSearchParams, Date: { now: () => now },
    fetch: async (path, options) => {
      const method = options.method || "GET";
      requests.push({ path, ...options });
      assert.equal(options.headers.get("X-Auth-Token"), "synthetic-not-a-real-token");
      assert.equal(options.headers.get("X-Project-Id"), "project-1");
      if (path === "/v1/me") return Response.json({ user_id: "user-1", project_id: "project-1", roles: ["member"], capabilities: { upload, inventory: true } });
      if (path === "/v1/image-jobs?limit=100") return Response.json(jobs);
      if (method === "POST") {
        const answer = await admit(path, options);
        return answer instanceof Response ? answer : Response.json(answer, { status: 202 });
      }
      assert.ok(path.startsWith("/v1/layers?") || path.startsWith("/v1/builds?"), path);
      return Response.json([]);
    },
  });
  vm.runInContext(asset("hub.js"), context);
  window.Hub.selectPlaybook = async (id) => selected.push(id);
  vm.runInContext(asset("hub-image-jobs.js"), context);
  for (const [id, value] of Object.entries({
    "image-job-kind": "git", "image-job-name": "demo", "image-job-visibility": "internal",
    "image-job-os-family": "ubuntu", "image-job-os-version": "24.04",
    "image-job-repo": "https://github.com/example/project.git", "image-job-revision": "a".repeat(40),
    "image-job-context": "subdir", "image-job-path": "docker/Dockerfile",
  })) get(id).value = value;
  return {
    get, requests, timers, jobs, selected, window,
    advance: (milliseconds) => { now += milliseconds; },
    async connect() {
      get("token").value = "synthetic-not-a-real-token"; get("project").value = "project-1";
      await get("auth-form").dispatch("submit");
      assert.match(get("connection").textContent, /Connected/);
    },
    disconnect: () => get("disconnect").dispatch("click"),
    submit: () => get("image-job-form").dispatch("submit"),
    posts: () => requests.filter((request) => request.method === "POST"),
  };
}

test("Git submission sends pinned context, path, name, audience and only a credential reference", async () => {
  const page = dashboard(); await page.connect();
  page.get("image-job-credential").value = "repo-reader";
  await page.submit();
  const request = page.posts()[0];
  assert.equal(request.path, "/v1/git-builds");
  assert.deepEqual(JSON.parse(request.body), {
    name: "demo", visibility: "internal", os_family: "ubuntu", os_version: "24.04", request_id: "request-1",
    repo_url: "https://github.com/example/project.git", revision: "a".repeat(40),
    dockerfile_path: "docker/Dockerfile", context_subdir: "subdir", credential_ref: "repo-reader",
  });
  assert.match(page.get("image-job-message").textContent, /job-1.*queued/);
  assert.equal(page.posts().length, 1);
  await page.disconnect();
});

for (const [kind, path] of [["registry", "/v1/registry-imports"], ["dockerfile", "/v1/dockerfile-builds"]]) {
  test(`${kind} submission uses its own descriptor and hides inactive required fields`, async () => {
    const page = dashboard(); await page.connect();
    page.get("image-job-kind").value = kind; await page.get("image-job-kind").dispatch("change");
    assert.equal(page.get("image-job-git-fields").disabled, true);
    assert.equal(page.get(`image-job-${kind}-fields`).hidden, false);
    page.get("image-job-reference").value = "docker.io/library/ubuntu:24.04";
    page.get("image-job-dockerfile").value = "FROM ubuntu:24.04\nCMD [\"/bin/true\"]";
    page.get("image-job-context-digest").value = `sha256:${"b".repeat(64)}`;
    await page.submit();
    const request = page.posts()[0], body = JSON.parse(request.body);
    assert.equal(request.path, path);
    assert.equal(body.repo_url, undefined);
    if (kind === "registry") assert.equal(body.reference, "docker.io/library/ubuntu:24.04");
    else { assert.match(body.dockerfile, /^FROM ubuntu/); assert.equal(body.context_digest, `sha256:${"b".repeat(64)}`); }
    await page.disconnect();
  });
}

test("job status reads complete output, shows errors, and selects a playbook without resubmission", async () => {
  const page = dashboard(); await page.connect();
  page.jobs.push({ id: "job-1", request_id: "request-1", name: "demo", kind: "git", visibility: "internal", status: "building" });
  await page.get("refresh-image-jobs").dispatch("click");
  assert.equal(page.timers.size, 1);
  page.jobs[0].status = "complete"; page.jobs[0].published_image_id = "image-1";
  page.jobs[0].output_digests = [`sha256:${"c".repeat(64)}`];
  const poll = [...page.timers.values()][0]; page.timers.clear(); await poll();
  const select = page.get("image-job-list").children[0].children[1].children[0];
  assert.equal(select.textContent, "Select output"); await select.dispatch("click");
  assert.deepEqual(page.selected, ["image-1"]);
  assert.equal(page.timers.size, 0); assert.equal(page.posts().length, 0);
  page.jobs[0].status = "error"; page.jobs[0].error_code = "build_failed";
  await page.get("refresh-image-jobs").dispatch("click");
  assert.match(page.get("image-job-list").children[0].children[0].children[2].textContent, /build_failed/);
  await page.disconnect();
});

for (const [status, retryAfter, guidance] of [[409, "10", /Request not applied/], [503, "1", /Outcome unknown/]]) {
  test(`${status} exposes Retry-After and never auto-repeats admission`, async () => {
    const page = dashboard({ admit: async () => Response.json({ detail: "busy" }, { status, headers: { "Retry-After": retryAfter } }) });
    await page.connect(); await page.submit();
    assert.match(page.get("image-job-message").textContent, guidance);
    assert.match(page.get("image-job-message").textContent, new RegExp(`Retry-After: ${retryAfter}`));
    assert.equal(page.get("image-job-submit").disabled, true);
    await page.get("refresh-image-jobs").dispatch("click"); await page.submit();
    assert.equal(page.posts().length, 1);
    page.advance(Number(retryAfter) * 1000);
    const release = [...page.timers.values()][0]; page.timers.clear(); release();
    assert.equal(page.get("image-job-submit").disabled, false);
    assert.equal(page.posts().length, 1);
    // Only a new, explicit submit can replay; unchanged descriptor keeps the original UUID.
    await page.submit();
    assert.equal(JSON.parse(page.posts()[0].body).request_id, JSON.parse(page.posts()[1].body).request_id);
    await page.disconnect();
    assert.equal(page.timers.size, 0, "Disconnect cancels the new cooldown timer");
  });
}

test("reader cannot submit and disconnect cancels polling without repeating a job", async () => {
  const reader = dashboard({ upload: false }); await reader.connect(); await reader.submit();
  assert.equal(reader.get("image-job-submit").disabled, true); assert.equal(reader.posts().length, 0);
  await reader.disconnect();
  let finish;
  const page = dashboard({ admit: () => new Promise((resolve) => { finish = resolve; }) });
  await page.connect(); const pending = page.submit();
  assert.equal(page.get("image-job-submit").disabled, true);
  await page.submit(); assert.equal(page.posts().length, 1);
  await page.disconnect(); await page.connect();
  finish({ id: "stale-job", status: "queued" }); await pending;
  assert.equal(page.get("image-job-message").textContent, "");
  assert.equal(page.get("image-job-submit").disabled, false);
  assert.equal(page.posts().length, 1); assert.equal(page.timers.size, 0);
  await page.disconnect();
});

test("confirmed admission allows an explicit new job with the same descriptor after failure", async () => {
  let admitted = 0;
  const page = dashboard({ admit: async () => ({ id: `job-${++admitted}`, status: "queued" }) });
  await page.connect(); await page.submit();
  page.jobs.push({ id: "job-1", status: "error", name: "demo", kind: "git", visibility: "internal", error_code: "worker_interrupted" });
  await page.get("refresh-image-jobs").dispatch("click");
  await page.submit();
  assert.equal(page.posts().length, 2);
  assert.notEqual(JSON.parse(page.posts()[0].body).request_id, JSON.parse(page.posts()[1].body).request_id);
  assert.match(page.get("image-job-message").textContent, /job-2/);
  await page.disconnect();
});

test("new image fields enforce valid Unicode-sets patterns used by browser constraints", () => {
  const html = asset("hub.html");
  for (const id of ["image-job-name", "image-job-credential"]) {
    const pattern = html.match(new RegExp(`<input id="${id}"[^>]*pattern="([^"]+)"`))[1];
    const constraint = new RegExp(`^(?:${pattern})$`, "v");
    assert.equal(constraint.test("valid-name.1"), true);
    assert.equal(constraint.test("invalid/name"), false);
  }
});
