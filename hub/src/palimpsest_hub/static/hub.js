"use strict";

let token = "";
let activeUpload = null;
let connectionVersion = 0;
let connectionController = new AbortController();
const $ = (id) => document.getElementById(id);
const digestPattern = /^sha256:[0-9a-f]{64}$/i;

async function api(path, options = {}) {
  if (!token) throw new Error("Connect a project-scoped token first.");
  const version = connectionVersion;
  const headers = new Headers(options.headers);
  headers.set("X-Auth-Token", token);
  const response = await fetch(`/v1${path}`, { ...options, headers, signal: connectionController.signal, cache: "no-store" });
  if (version !== connectionVersion) throw new Error("Connection changed; retry with the current project.");
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") detail = body.detail;
    } catch { /* A proxy can return a non-JSON error page. */ }
    throw new Error(`${response.status}: ${detail}`);
  }
  return response;
}

async function json(path, options = {}) {
  const version = connectionVersion;
  const response = await api(path, options);
  const result = await response.json();
  if (version !== connectionVersion) throw new Error("Connection changed; retry with the current project.");
  return result;
}

function message(id, text) { $(id).textContent = text; }
function empty(container, text) {
  container.replaceChildren();
  const node = document.createElement("p");
  node.className = "empty";
  node.textContent = text;
  container.append(node);
}
function node(tag, text, className) {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}
function shortDigest(value) { return value ? `${value.slice(0, 20)}…` : "—"; }

async function download(digest, name, size) {
  const response = await api(`/layers/${encodeURIComponent(digest)}/blob`);
  const filename = name.replace(/[^a-zA-Z0-9._-]/g, "_");
  if (window.showSaveFilePicker) {
    const file = await window.showSaveFilePicker({ suggestedName: filename });
    const sink = await file.createWritable();
    try { await response.body.pipeTo(sink); }
    catch (error) { await sink.abort().catch(() => {}); throw error; }
  } else {
    if (size > 64 * 1024 * 1024) {
      await response.body?.cancel();
      throw new Error("Large downloads require a Chromium browser or the authenticated API client.");
    }
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }
}

async function listBuilds() {
  const container = $("build-list");
  try {
    const rows = await json("/builds?limit=50");
    if (!rows.length) return empty(container, "No builds in this project yet.");
    container.replaceChildren();
    for (const row of rows) {
      const line = node("div", "", "row");
      const left = node("div", "", "row-main");
      left.append(node("strong", row.name));
      left.append(node("span", row.status, `status ${row.status}`));
      const info = node("div", "", "meta");
      info.append(node("code", row.output_digest || row.id));
      if (row.error_code) info.append(node("span", ` · ${row.error_code}`));
      left.append(info);
      line.append(left);
      if (row.output_digest) {
        const button = node("button", "Download layer", "secondary");
        button.type = "button";
        button.addEventListener("click", () => download(row.output_digest, `${row.name}.sqsh`, row.output_size_bytes).catch((error) => message("build-message", error.message)));
        line.append(button);
      }
      container.append(line);
    }
  } catch (error) { empty(container, error.message); }
}

async function listArtifacts() {
  const container = $("artifact-list");
  const params = new URLSearchParams({ limit: "100" });
  const name = $("search-name").value.trim();
  const kind = $("search-kind").value;
  if (name) params.set("name", name);
  if (kind) params.set("kind", kind);
  try {
    const rows = await json(`/layers?${params}`);
    if (!rows.length) return empty(container, "No visible artifacts match this search.");
    container.replaceChildren();
    for (const row of rows) {
      const line = node("div", "", "row");
      const left = node("div", "", "row-main");
      left.append(node("strong", row.name));
      left.append(node("span", row.kind, "status"));
      const info = node("div", "", "meta");
      info.append(node("code", row.blob_digest));
      info.append(node("span", ` · ${(row.size_bytes / (1024 * 1024)).toFixed(1)} MiB`));
      left.append(info);
      line.append(left);
      const button = node("button", "Download", "secondary");
      button.type = "button";
      const ext = row.kind === "cloud-image" ? row.disk_format : "sqsh";
      button.addEventListener("click", () => download(row.blob_digest, `${row.name}.${ext}`, row.size_bytes).catch((error) => empty(container, error.message)));
      line.append(button);
      container.append(line);
    }
  } catch (error) { empty(container, error.message); }
}

$("auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  connectionController.abort();
  connectionController = new AbortController();
  connectionVersion += 1;
  activeUpload = null;
  $("workspace").hidden = true;
  token = $("token").value.trim();
  $("token").value = "";
  const version = connectionVersion;
  try {
    await json("/layers?limit=1");
    if (version !== connectionVersion) return;
    $("workspace").hidden = false;
    message("connection", "Connected. Token held in memory only; re-enter after reload.");
    await Promise.all([listBuilds(), listArtifacts()]);
  } catch (error) {
    if (version !== connectionVersion) return;
    token = "";
    $("workspace").hidden = true;
    message("connection", error.message);
  }
});

$("build-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  const layers = $("build-layers").value.split(/\s+/).filter(Boolean);
  if (layers.some((value) => !digestPattern.test(value))) return message("build-message", "Each parent digest must be sha256:<64 hex digits>.");
  button.disabled = true;
  try {
    const payload = {
      name: $("build-name").value.trim(),
      base_digest: $("build-base").value.trim(),
      layer_digests: layers,
      recipe: $("build-recipe").value,
    };
    const build = await json("/builds", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    message("build-message", `Queued ${build.id}. Refresh to see its result.`);
    await listBuilds();
  } catch (error) { message("build-message", error.message); }
  finally { button.disabled = false; }
});

$("upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  const file = $("upload-file").files[0];
  if (!file) return;
  const kind = $("upload-kind").value;
  const base = $("upload-base").value.trim();
  const parent = $("upload-parent").value.trim();
  if (kind === "squashfs" && ((base && !digestPattern.test(base)) || (parent && !digestPattern.test(parent)))) {
    return message("upload-message", "Base and parent digests, when supplied, must be sha256:<64 hex digits>.");
  }
  if (parent && !base) {
    return message("upload-message", "Specify the base image when uploading a parent-linked layer.");
  }
  button.disabled = true;
  try {
    let offset;
    if (activeUpload?.file === file) {
      const status = await json(`/uploads/${activeUpload.sessionId}`);
      offset = status.received_bytes;
    } else {
      const session = await json("/uploads", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      activeUpload = { file, sessionId: session.session_id };
      offset = 0;
    }
    while (offset < file.size) {
      const part = file.slice(offset, offset + 8 * 1024 * 1024);
      const result = await json(`/uploads/${activeUpload.sessionId}`, { method: "PATCH", headers: { "Upload-Offset": String(offset), "Content-Type": "application/octet-stream" }, body: part });
      if (result.received_bytes !== offset + part.size) throw new Error("Upload offset changed; retry with the same file.");
      offset = result.received_bytes;
      message("upload-message", `${((offset / file.size) * 100).toFixed(1)}% uploaded`);
    }
    const meta = { name: $("upload-name").value.trim(), kind };
    if (kind === "cloud-image") Object.assign(meta, { disk_format: $("upload-format").value, arch: $("upload-arch").value });
    else Object.assign(meta, { base_image_digest: base || null, parent_digest: parent || null, arch: $("upload-arch").value });
    const registered = await json(`/uploads/${activeUpload.sessionId}`, { method: "PUT", headers: { "Content-Type": "application/json", "Upload-Offset": String(offset) }, body: JSON.stringify(meta) });
    activeUpload = null;
    message("upload-message", `Registered ${shortDigest(registered.blob_digest)}`);
    await listArtifacts();
  } catch (error) { message("upload-message", `${error.message} Retry with the same file to resume.`); }
  finally { button.disabled = false; }
});

$("refresh-builds").addEventListener("click", listBuilds);
$("search").addEventListener("click", listArtifacts);
