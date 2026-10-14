"use strict";

const $ = (id) => document.getElementById(id);
const digestPattern = /^sha256:[0-9a-f]{64}$/;
const chunkBytes = 8 * 1024 * 1024;
const memoryDownloadLimit = 64 * 1024 * 1024;
function message(id, text) { $(id).textContent = text; }
function node(tag, text, className) {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}
function empty(container, text) { container.replaceChildren(node("p", text, "empty")); }
function action(text, fn, className = "secondary") {
  const button = node("button", text, className);
  button.type = "button";
  button.addEventListener("click", fn);
  return button;
}

// Incremental SHA-256 keeps image hashing bounded to the transfer chunk, not the image size.
class Sha256 {
  constructor() {
    this.state = new Uint32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
    this.buffer = new Uint8Array(64);
    this.words = new Uint32Array(64);
    this.length = 0;
    this.used = 0;
  }
  static rotate(x, n) { return (x >>> n) | (x << (32 - n)); }
  block(bytes, offset) {
    const w = this.words;
    const r = Sha256.rotate;
    for (let i = 0; i < 16; i++) {
      const j = offset + i * 4;
      w[i] = (bytes[j] << 24) | (bytes[j + 1] << 16) | (bytes[j + 2] << 8) | bytes[j + 3];
    }
    for (let i = 16; i < 64; i++) {
      const x = w[i - 15], y = w[i - 2];
      w[i] = w[i - 16] + (r(x, 7) ^ r(x, 18) ^ (x >>> 3)) + w[i - 7] + (r(y, 17) ^ r(y, 19) ^ (y >>> 10));
    }
    const state = this.state;
    let a = state[0], b = state[1], c = state[2], d = state[3], e = state[4], f = state[5], g = state[6], h = state[7];
    for (let i = 0; i < 64; i++) {
      const t1 = (h + (r(e, 6) ^ r(e, 11) ^ r(e, 25)) + ((e & f) ^ (~e & g)) + Sha256.constants[i] + w[i]) >>> 0;
      const t2 = ((r(a, 2) ^ r(a, 13) ^ r(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) >>> 0;
      h = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b; b = a; a = (t1 + t2) >>> 0;
    }
    state[0] += a; state[1] += b; state[2] += c; state[3] += d;
    state[4] += e; state[5] += f; state[6] += g; state[7] += h;
  }
  update(bytes) {
    this.length += bytes.length;
    let offset = 0;
    if (this.used) {
      const count = Math.min(64 - this.used, bytes.length);
      this.buffer.set(bytes.subarray(0, count), this.used);
      this.used += count;
      offset = count;
      if (this.used === 64) { this.block(this.buffer, 0); this.used = 0; }
    }
    while (offset + 64 <= bytes.length) { this.block(bytes, offset); offset += 64; }
    if (offset < bytes.length) {
      this.buffer.set(bytes.subarray(offset));
      this.used = bytes.length - offset;
    }
  }
  digest() {
    const bits = this.length * 8;
    this.buffer[this.used++] = 0x80;
    this.buffer.fill(0, this.used);
    if (this.used > 56) { this.block(this.buffer, 0); this.buffer.fill(0); }
    const view = new DataView(this.buffer.buffer);
    view.setUint32(56, Math.floor(bits / 0x100000000));
    view.setUint32(60, bits >>> 0);
    this.block(this.buffer, 0);
    return "sha256:" + Array.from(this.state, (value) => value.toString(16).padStart(8, "0")).join("");
  }
}
Sha256.constants = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

async function hashFile(file, check, progress) {
  const hash = new Sha256();
  for (let offset = 0; offset < file.size; offset += chunkBytes) {
    check();
    const bytes = new Uint8Array(await file.slice(offset, offset + chunkBytes).arrayBuffer());
    check();
    hash.update(bytes);
    progress(`Hashing ${Math.min(offset + bytes.length, file.size)} / ${file.size} bytes`);
  }
  check();
  return hash.digest();
}
async function downloadVerified(request, path, digest, size, filename, check, progress) {
  if (!digestPattern.test(digest) || !Number.isSafeInteger(size) || size < 0) throw new Error("Missing verified download descriptor.");
  filename = filename.replace(/[^a-zA-Z0-9._-]/g, "_");
  let sink = null, reader = null;
  try {
    if (window.showSaveFilePicker) {
      const handle = await window.showSaveFilePicker({ suggestedName: filename });
      check();
      sink = await handle.createWritable();
      check();
    } else if (size > memoryDownloadLimit) {
      throw new Error("Downloads over 64 MiB require a browser supporting streamed file saves or the authenticated CLI.");
    }
    const response = await request(path);
    check();
    if (!response.body) throw new Error("Download has no readable body.");
    reader = response.body.getReader();
    const hash = new Sha256(), parts = [];
    let received = 0;
    while (true) {
      const { value, done } = await reader.read();
      check();
      if (done) break;
      received += value.length;
      if (received > size || (!sink && received > memoryDownloadLimit)) throw new Error("Download exceeds its declared size.");
      hash.update(value);
      if (sink) { await sink.write(value); check(); } else parts.push(value);
      progress(`Downloading ${received} / ${size} bytes; SHA-256 verification pending`);
    }
    if (received !== size || hash.digest() !== digest) throw new Error("Download SHA-256 or size mismatch; file was not committed.");
    check();
    if (sink) { await sink.close(); sink = null; check(); }
    else {
      const url = URL.createObjectURL(new Blob(parts));
      const link = node("a", "");
      link.href = url; link.download = filename; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    }
    progress(`Verified ${digest} · ${received} bytes saved`);
  } catch (error) {
    if (reader) await reader.cancel().catch(() => {});
    if (sink) await sink.abort().catch(() => {});
    throw error;
  }
}
window.Hub = { node, message, hashFile, downloadVerified };

let token = "", project = "", identity = null, activeUpload = null, uploadBusy = false, uploadAborting = false, uploadFinalizing = false;
let connectionVersion = 0, connectionController = new AbortController(), searchVersion = 0, buildVersion = 0;
let uploadAttemptVersion = 0;
function guard(version = connectionVersion) {
  return () => { if (version !== connectionVersion || connectionController.signal.aborted) throw new Error("Connection changed; operation cancelled."); };
}
function report(version, id, error) { if (version === connectionVersion) message(id, error.message); }
async function api(path, options = {}) {
  if (!token || !project) throw new Error("Connect the original token and explicit project ID first.");
  const check = guard(), headers = new Headers(options.headers);
  headers.set("X-Auth-Token", token);
  headers.set("X-Project-Id", project);
  const response = await fetch(`/v1${path}`, { ...options, headers, signal: connectionController.signal, cache: "no-store", credentials: "omit", redirect: "error" });
  check();
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      check();
      detail = typeof body.detail === "string" ? body.detail : body.detail?.message || body.error?.message || JSON.stringify(body.detail || body.error || body);
    } catch (error) { check(); }
    throw Object.assign(new Error(`${response.status}: ${detail}`), { status: response.status, path });
  }
  return response;
}
async function json(path, options = {}) {
  const check = guard(), response = await api(path, options), result = await response.json();
  check();
  return result;
}
function bodyOptions(method, body) { return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }; }
function reportUploadError(version, error, upload = activeUpload) {
  if (version !== connectionVersion) return;
  if (error.status === 404 && upload && activeUpload === upload && error.path === `/uploads/${upload.sessionId}`) {
    activeUpload = null;
    message("upload-message", `Session ${upload.sessionId} is no longer available; local controls released. Finalization may already have committed. No publication retry was sent.`);
  } else report(version, "upload-message", error);
}
function uploadControls() {
  const allowed = !!identity?.capabilities.upload;
  const cloud = $("upload-kind").value === "cloud-image";
  for (const control of $("upload-form").elements) {
    if (["upload-submit", "upload-status", "upload-abort"].includes(control.id)) continue;
    control.disabled = !allowed || !!activeUpload || uploadBusy || (control.id === "upload-format" && !cloud) || (cloud && ["upload-base", "upload-parent", "upload-chain"].includes(control.id));
  }
  $("upload-submit").disabled = !allowed || uploadBusy;
  $("upload-status").disabled = !allowed || !activeUpload || uploadBusy;
  $("upload-abort").disabled = !allowed || !activeUpload || uploadAborting || uploadFinalizing;
}
function disconnect() {
  connectionController.abort(); connectionController = new AbortController(); connectionVersion++;
  token = ""; project = ""; identity = null; activeUpload = null; uploadBusy = false; uploadAborting = false; uploadFinalizing = false;
  $("token").value = "";
  $("workspace").hidden = true;
  message("connection", "Disconnected; credentials cleared. Interrupted server sessions expire after their idle timeout.");
  for (const id of ["identity", "permissions", "artifact-message", "build-message", "upload-message"]) message(id, "");
  $("artifact-list").replaceChildren(); $("build-list").replaceChildren();
  uploadControls();
}
async function artifactDownload(row, target = "artifact-message") {
  const version = connectionVersion, check = guard(version);
  try {
    await downloadVerified(api, `/layers/${encodeURIComponent(row.blob_digest)}/blob`, row.blob_digest, row.size_bytes, `${row.name}.${row.kind === "cloud-image" ? row.disk_format : "sqsh"}`, check, (text) => { check(); message(target, text); });
  } catch (error) { report(version, target, error); }
}
async function deleteArtifact(row) {
  const version = connectionVersion, check = guard(version);
  const confirmation = window.prompt(`Administrator deletion: type the exact name and digest below. Children block deletion.\n${row.name} ${row.blob_digest}`);
  if (confirmation !== `${row.name} ${row.blob_digest}`) return;
  try {
    check();
    await api(`/layers/${encodeURIComponent(row.blob_digest)}`, { method: "DELETE" });
    check(); message("artifact-message", `Deleted registration ${row.name} ${row.blob_digest}. Blob cleanup is deferred.`);
    await listArtifacts();
  } catch (error) { report(version, "artifact-message", error); }
}
async function artifactDetails(row, container) {
  const version = connectionVersion, check = guard(version);
  try {
    const detail = await json(`/layers/${encodeURIComponent(row.blob_digest)}`);
    check();
    container.replaceChildren(node("pre", JSON.stringify(detail, null, 2), "descriptor"));
  } catch (error) { report(version, "artifact-message", error); }
}
async function listArtifacts(page = null) {
  const version = connectionVersion, requestVersion = ++searchVersion, check = guard(version), container = $("artifact-list");
  const offset = page?.offset || 0, pageSize = 100;
  const params = new URLSearchParams(page?.params || { limit: String(pageSize + 1) });
  if (!page) {
    for (const [key, id] of [["name", "search-name"], ["kind", "search-kind"], ["digest_prefix", "search-digest"], ["arch", "search-arch"], ["os_variant", "search-os"], ["ubuntu_base", "search-ubuntu"], ["disk_format", "search-format"]]) {
      const value = $(id).value.trim(); if (value) params.set(key, value);
    }
  }
  params.set("offset", String(offset));
  try {
    if (!identity?.capabilities.inventory) return empty(container, "This identity has no legacy inventory authority.");
    const rows = await json(`/layers?${params}`);
    check(); if (requestVersion !== searchVersion) return;
    container.replaceChildren();
    if (!rows.length) empty(container, "No visible artifacts on this page. Refresh Search if inventory changed.");
    for (const row of rows.slice(0, pageSize)) {
      const line = node("div", "", "row"), left = node("div", "", "row-main"), buttons = node("div", "", "row-actions"), details = node("div", "");
      left.append(node("strong", row.name), node("span", row.kind, "status"));
      left.append(node("div", `${row.arch || "unspecified architecture"} · ${row.disk_format || "SquashFS"} · ${row.size_bytes} bytes · ${row.is_published ? "published" : "private/shared"}`, "meta"));
      left.append(node("code", row.blob_digest));
      left.append(node("div", `Base: ${row.base_image_digest || "—"} · Parent: ${row.parent_digest || "—"} · OS: ${row.os_variant || row.ubuntu_base || "—"}`, "meta"));
      buttons.append(action("Metadata", () => artifactDetails(row, details)));
      if (identity.capabilities.download) buttons.append(action("Verified download", () => artifactDownload(row)));
      if (identity.capabilities.delete) buttons.append(action("Delete registration", () => deleteArtifact(row), "danger"));
      left.append(details); line.append(left, buttons); container.append(line);
    }
    const navigation = node("div", "", "actions");
    navigation.append(node("span", `${rows.length ? `Matches ${offset + 1}–${offset + Math.min(rows.length, pageSize)}.` : "No matches on this page."} Inventory can change; Search refreshes the first page.`, "hint"));
    if (offset) navigation.append(action("Previous page", () => listArtifacts({ params: params.toString(), offset: Math.max(0, offset - pageSize) })));
    if (rows.length > pageSize) navigation.append(action("Next page", () => listArtifacts({ params: params.toString(), offset: offset + pageSize })));
    container.append(navigation);
  } catch (error) { if (version === connectionVersion && requestVersion === searchVersion) empty(container, error.message); }
}
async function listBuilds() {
  const version = connectionVersion, requestVersion = ++buildVersion, check = guard(version), container = $("build-list");
  try {
    if (!identity?.capabilities.build) return empty(container, "Build access requires system-administrator authority.");
    const rows = await json("/builds?limit=50");
    check(); if (requestVersion !== buildVersion) return;
    container.replaceChildren();
    if (!rows.length) return empty(container, "No builds in this project yet.");
    for (const row of rows) {
      const line = node("div", "", "row"), left = node("div", "", "row-main");
      left.append(node("strong", row.name), node("span", row.status, `status ${row.status}`), node("div", `${row.output_digest || row.id}${row.error_code ? ` · ${row.error_code}` : ""}`, "meta"));
      line.append(left);
      if (row.output_digest) line.append(action("Verified download", () => artifactDownload({ blob_digest: row.output_digest, name: row.name, size_bytes: row.output_size_bytes, kind: "squashfs" }, "build-message")));
      container.append(line);
    }
  } catch (error) { if (version === connectionVersion && requestVersion === buildVersion) empty(container, error.message); }
}
$("auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const suppliedToken = $("token").value.trim(), suppliedProject = $("project").value.trim();
  disconnect();
  if (suppliedToken.startsWith("ppk_v1_")) return message("connection", "Native package keys belong in the separate native key connection, not the Keystone token field.");
  token = suppliedToken; project = suppliedProject;
  const version = connectionVersion, check = guard(version);
  try {
    const me = await json("/me"); check();
    if (me.project_id !== project) throw new Error("Server identity does not match the asserted original project.");
    identity = me; $("workspace").hidden = false;
    message("connection", "Connected. Credentials remain in this tab's memory only.");
    message("identity", `${me.project_name || "Project"} · ${me.project_id} · User ${me.user_id} · Original token roles: ${me.roles.join(", ") || "none"} · Current system administrator: ${me.is_system_admin ? "yes" : "no"}`);
    message("permissions", `Current authority · ${Object.entries(me.capabilities).map(([name, allowed]) => `${name}: ${allowed ? "allowed" : "not allowed"}`).join(" · ")}. Every operation is authorized again by the server.`);
    for (const control of $("build-form").elements) control.disabled = !me.capabilities.build;
    uploadControls(); await Promise.all([listArtifacts(), listBuilds()]);
  } catch (error) { if (version === connectionVersion) { disconnect(); message("connection", error.message); } }
});
$("disconnect").addEventListener("click", disconnect);
$("build-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const version = connectionVersion, check = guard(version), button = event.submitter;
  const layers = $("build-layers").value.split(/\s+/).filter(Boolean).map((value) => value.toLowerCase());
  if (layers.some((value) => !digestPattern.test(value))) return message("build-message", "Parent digests must be sha256:<64 hex digits>.");
  button.disabled = true;
  try {
    const build = await json("/builds", bodyOptions("POST", { name: $("build-name").value.trim(), base_digest: $("build-base").value.trim().toLowerCase(), layer_digests: layers, recipe: $("build-recipe").value }));
    check(); message("build-message", `Queued ${build.id}. Refresh for worker status.`); await listBuilds();
  } catch (error) { report(version, "build-message", error); }
  finally { if (version === connectionVersion) button.disabled = !identity?.capabilities.build; }
});
function uploadMetadata() {
  const kind = $("upload-kind").value;
  const meta = { name: $("upload-name").value.trim(), kind, arch: $("upload-arch").value, os_variant: $("upload-os").value.trim() || null, ubuntu_base: $("upload-ubuntu").value.trim() || null, python_version: $("upload-python").value.trim() || null, is_published: false };
  if (kind === "cloud-image") Object.assign(meta, { disk_format: $("upload-format").value, parent_digest: null, base_image_digest: null, chain_id: null, media_type: `application/vnd.afterglow.palimpsest.image.${$("upload-format").value}.v1` });
  else {
    for (const [key, id] of [["base_image_digest", "upload-base"], ["parent_digest", "upload-parent"], ["chain_id", "upload-chain"]]) {
      meta[key] = $(id).value.trim().toLowerCase() || null;
      if (meta[key] && !digestPattern.test(meta[key])) throw new Error(`${key} must be sha256:<64 hex digits>.`);
    }
    meta.disk_format = null; meta.media_type = "application/vnd.afterglow.palimpsest.layer.squashfs.v1";
    if (meta.parent_digest && !meta.base_image_digest) throw new Error("Specify a base image for a parent-linked layer.");
  }
  return meta;
}
function identicalRegistration(row, meta) {
  const fields = ["name", "kind", "media_type", "disk_format", "arch", "os_variant", "chain_id", "parent_digest", "base_image_digest", "ubuntu_base", "python_version"];
  const mismatches = fields.filter((key) => (row[key] ?? null) !== (meta[key] ?? null));
  if (mismatches.length) throw new Error(`Identical bytes already have incompatible metadata: ${mismatches.join(", ")}. No registration changed.`);
}
$("upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (uploadBusy) return;
  const version = connectionVersion, attempt = ++uploadAttemptVersion, connectionCheck = guard(version);
  const check = () => { connectionCheck(); if (attempt !== uploadAttemptVersion) throw new Error("Upload abort requested."); };
  const progress = (text) => { check(); message("upload-message", text); };
  const file = $("upload-file").files[0];
  if (!file) return;
  uploadBusy = true; uploadControls();
  try {
    if (!activeUpload) {
      const meta = uploadMetadata(), digest = await hashFile(file, check, progress); check();
      const started = await json("/uploads", bodyOptions("POST", { digest })); check();
      if (started.completed) {
        if (started.blob_digest !== digest || !started.registration) throw new Error("Existing registration has no matching verified descriptor.");
        identicalRegistration(started.registration, meta);
        progress(`Already registered with identical metadata: ${digest}. Existing publication visibility is unchanged.`); await listArtifacts(); return;
      }
      activeUpload = { file, digest, meta, sessionId: started.session_id }; uploadControls();
    } else if (activeUpload.file !== file) throw new Error("Abort the active session before selecting another file.");
    const upload = activeUpload, status = await json(`/uploads/${upload.sessionId}`); check();
    let offset = status.received_bytes;
    if (!Number.isSafeInteger(offset) || offset < 0 || offset > file.size) throw new Error("Server upload offset exceeds this file.");
    while (offset < file.size) {
      const part = file.slice(offset, offset + chunkBytes);
      const result = await json(`/uploads/${upload.sessionId}`, { method: "PATCH", headers: { "Upload-Offset": String(offset), "Content-Type": "application/octet-stream" }, body: part }); check();
      if (result.received_bytes !== offset + part.size) throw new Error("Upload offset changed; use Status before resuming.");
      offset = result.received_bytes; progress(`Session ${upload.sessionId}: ${offset} / ${file.size} bytes uploaded`);
    }
    uploadFinalizing = true; uploadControls();
    const registered = await json(`/uploads/${upload.sessionId}`, { ...bodyOptions("PUT", upload.meta), headers: { "Content-Type": "application/json", "Upload-Offset": String(offset) } }); check();
    if (registered.blob_digest !== upload.digest || registered.size_bytes !== file.size) throw new Error("Final registration does not match this file's whole SHA-256 and size.");
    activeUpload = null; progress(`Verified and registered ${registered.blob_digest}`); await listArtifacts();
  } catch (error) { if (attempt === uploadAttemptVersion) reportUploadError(version, error); }
  finally { if (version === connectionVersion && attempt === uploadAttemptVersion) { uploadBusy = false; uploadFinalizing = false; uploadControls(); } }
});
$("upload-status").addEventListener("click", async () => {
  if (!activeUpload || uploadBusy) return;
  const version = connectionVersion, attempt = uploadAttemptVersion, upload = activeUpload, connectionCheck = guard(version);
  const check = () => { connectionCheck(); if (attempt !== uploadAttemptVersion) throw new Error("Upload status cancelled."); };
  uploadBusy = true; uploadControls();
  try { const status = await json(`/uploads/${upload.sessionId}`); check(); message("upload-message", `Session ${status.session_id} · Project ${status.project_id} · ${status.received_bytes} / ${upload.file.size} bytes · ${upload.digest}`); }
  catch (error) { if (attempt === uploadAttemptVersion) reportUploadError(version, error, upload); }
  finally { if (version === connectionVersion && attempt === uploadAttemptVersion) { uploadBusy = false; uploadControls(); } }
});
$("upload-abort").addEventListener("click", async () => {
  if (!activeUpload || uploadAborting || uploadFinalizing) return;
  const version = connectionVersion, upload = activeUpload, check = guard(version);
  uploadAttemptVersion++;
  uploadAborting = true; uploadBusy = true; uploadControls();
  message("upload-message", "Aborting after the in-flight chunk releases its server lock…");
  try { await api(`/uploads/${upload.sessionId}`, { method: "DELETE" }); check(); activeUpload = null; message("upload-message", `Aborted ${upload.sessionId}; registration was not created.`); }
  catch (error) { reportUploadError(version, error, upload); }
  finally { if (version === connectionVersion) { uploadBusy = false; uploadAborting = false; uploadFinalizing = false; uploadControls(); } }
});
$("refresh-builds").addEventListener("click", listBuilds);
$("search").addEventListener("click", () => listArtifacts());
$("upload-kind").addEventListener("change", uploadControls);
uploadControls();
window.addEventListener("pagehide", disconnect);
