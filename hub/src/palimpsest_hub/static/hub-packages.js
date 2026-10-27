"use strict";

// Same-origin native /v1 consumer. Hub helpers are supplied by hub.js before this script.
// Keys never enter the legacy console; member tokens are used only for member reads/control.
// All connection credentials, issued secrets, files and upload snapshots are tab-memory only.
(() => {
  const panel = document.getElementById("packages");
  if (!panel) return;
  const { node, message, hashFile, downloadVerified } = window.Hub;
  const digestPattern = /^sha256:[0-9a-f]{64}$/;
  const componentPattern = /^[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*$/;
  const projectPattern = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
  const chunkBytes = 8 * 1024 * 1024;
  let generation = 0, operation = 0, controller = new AbortController(), task = null;
  let keyConnection = null, memberConnection = null, activeUpload = null;
  let inventoryCursor = null, versionCursor = null, selectedPackage = "", inventoryType = "";
  let issuedSecret = "";
  const controls = [];

  function identified(tag, id, text = "", className = "") {
    const element = node(tag, text, className);
    element.id = id;
    return element;
  }
  function status(id) {
    const element = identified("p", id);
    element.setAttribute("role", "status");
    element.setAttribute("aria-live", "polite");
    return element;
  }
  function section(title) {
    const element = node("section", "", "native-section");
    element.append(node("h3", title));
    panel.append(element);
    return element;
  }
  function field(form, labelText, id, type = "text", required = false) {
    const label = node("label", labelText);
    const element = identified(type === "textarea" ? "textarea" : "input", id);
    if (type !== "textarea") element.type = type;
    else element.rows = 3;
    element.name = id;
    element.required = required;
    element.autocomplete = "off";
    element.spellcheck = false;
    label.htmlFor = id;
    label.append(element);
    form.append(label);
    return element;
  }
  function choice(form, labelText, id, choices) {
    const label = node("label", labelText), element = identified("select", id);
    element.name = id;
    label.htmlFor = id;
    for (const [value, text] of choices) {
      const option = node("option", text);
      option.value = value;
      element.append(option);
    }
    label.append(element);
    form.append(label);
    return element;
  }
  function button(text, handler, allowed = () => true, className = "secondary", allowBusy = false) {
    const element = node("button", text, className);
    element.type = "button";
    element.addEventListener("click", handler);
    controls.push({ element, allowed, allowBusy });
    return element;
  }
  function submit(form, text, handler, allowed) {
    const element = button(text, () => {}, allowed, "");
    element.type = "submit";
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (!task && allowed()) handler();
    });
    return element;
  }
  function descriptor(container, value) {
    container.replaceChildren(node("pre", JSON.stringify(value, null, 2), "descriptor"));
  }
  function keyAllows(action, packageName = null) {
    const identity = keyConnection?.identity;
    return !!identity?.actions.includes(action) && (!packageName || identity.scope.all_packages === true || identity.scope.packages?.includes(packageName));
  }
  function memberAllows(capability) { return memberConnection?.identity.capabilities?.[capability] === true; }
  function reader() { return readMode.value === "key" ? keyConnection : memberConnection; }
  function canRead() { return readMode.value === "key" ? keyAllows("packages:inventory") : memberAllows("packages_read"); }
  function canDownload(packageName) { return readMode.value === "key" ? keyAllows("packages:read", packageName) : memberAllows("packages_download"); }
  function namespacePath(connection, suffix, parameters = {}) {
    if (!connection?.identity.namespace) throw new Error("This connection has no registered namespace.");
    const query = new URLSearchParams(parameters).toString();
    return `/projects/${encodeURIComponent(connection.identity.namespace)}${suffix}${query ? `?${query}` : ""}`;
  }
  function body(method, value) { return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(value) }; }
  function ensureContext(value, connection, packageName = null) {
    if (!value || value.project_id !== connection.project || value.namespace !== connection.identity.namespace ||
        (packageName !== null && (value.package ?? value.name) !== packageName)) {
      throw new Error("Response ownership/reference does not match the captured project and namespace.");
    }
    return value;
  }
  function ensureProject(value, project) {
    if (!value || value.project_id !== project) throw new Error("Response project does not match the explicit immutable project ID.");
    return value;
  }
  function clearSecret() {
    issuedSecret = "";
    secretValue.textContent = "";
    secretValue.hidden = true;
    secretNote.textContent = "Issued keys are returned once. Reveal and retain the key securely before clearing; reload or any connection change discards it.";
    syncControls();
  }
  function clearViews() {
    inventoryCursor = null; versionCursor = null; selectedPackage = ""; inventoryType = "";
    inventoryList.replaceChildren(); versionList.replaceChildren(); packageDetail.replaceChildren(); versionDetail.replaceChildren();
    keyList.replaceChildren(); issuedMetadata.replaceChildren(); uploadDescriptor.replaceChildren();
    for (const element of [archiveFile, uploadPackage, uploadType, uploadTag, rootDigest, revision, buildId, lookupPackage, scopeNames, keyName]) element.value = "";
    for (const id of ["native-browse-message", "native-upload-message", "native-control-message"]) message(id, "");
  }
  function invalidate() {
    controller.abort(); task?.controller.abort();
    controller = new AbortController(); generation++; operation++; task = null;
    const interrupted = activeUpload;
    activeUpload = null;
    clearViews(); clearSecret();
    if (interrupted) message("native-upload-message", `Connection changed; session ${interrupted.session.upload_id} is no longer controlled by this tab. No automatic publication retry or deletion was sent. Server sessions expire after idle timeout; a finalization already received may have committed.`);
    renderConnections(); syncControls();
  }
  function syncControls() {
    panel.setAttribute("aria-busy", task ? "true" : "false");
    for (let i = controls.length - 1; i >= 0; i--) {
      const { element, allowed, allowBusy } = controls[i];
      if (!element.isConnected) { controls.splice(i, 1); continue; }
      element.disabled = (!allowBusy && !!task) || !allowed();
    }
    for (const element of keyForm.querySelectorAll("input")) element.disabled = !!task || !!keyConnection;
    for (const element of memberForm.querySelectorAll("input")) element.disabled = !!task || !!memberConnection;
    readMode.disabled = !!task;
    typeFilter.disabled = !!task;
    lookupPackage.disabled = !!task;
    for (const element of uploadForm.querySelectorAll("input, select")) element.disabled = !!task || !!activeUpload || !keyAllows("packages:write");
    for (const element of issueForm.querySelectorAll("input, select, textarea")) element.disabled = !!task || !memberAllows("keys_issue") || !memberConnection?.identity.namespace;
    scopeNames.disabled = scopeNames.disabled || scopeMode.value === "all";
  }
  function renderConnections() {
    keyIdentity.replaceChildren(); memberIdentity.replaceChildren();
    if (keyConnection) descriptor(keyIdentity, keyConnection.identity);
    if (memberConnection) descriptor(memberIdentity, memberConnection.identity);
  }
  function execute(target, handler, interrupt = false) {
    if (task && !interrupt) return;
    if (interrupt) task?.controller.abort();
    const version = generation, epoch = ++operation, abort = new AbortController();
    const connectionSignal = controller.signal;
    const relay = () => abort.abort();
    connectionSignal.addEventListener("abort", relay, { once: true });
    const context = {
      signal: abort.signal,
      check() {
        if (version !== generation || epoch !== operation || connectionSignal.aborted || abort.signal.aborted) {
          throw new Error("Connection or operation changed; cancelled.");
        }
      },
      progress(text) { this.check(); message(target, text); },
    };
    task = { controller: abort, epoch, target };
    syncControls();
    // Invoke synchronously so downloadVerified can open a save picker inside the click activation.
    let result;
    try { result = handler(context); }
    catch (error) { result = Promise.reject(error); }
    Promise.resolve(result).catch((error) => {
      if (version === generation && epoch === operation) message(target, error.message || "Native operation failed.");
    }).finally(() => {
      connectionSignal.removeEventListener("abort", relay);
      if (version === generation && epoch === operation) { task = null; syncControls(); }
    });
  }
  async function request(connection, path, context, options = {}) {
    context.check();
    if (!connection?.credential || !connection.project) throw new Error("Connect the explicit project and its credential first.");
    const headers = new Headers(options.headers);
    headers.delete("Authorization"); headers.delete("X-Auth-Token");
    headers.set("X-Project-Id", connection.project);
    headers.set(connection.mode === "key" ? "Authorization" : "X-Auth-Token", connection.mode === "key" ? `Bearer ${connection.credential}` : connection.credential);
    const response = await fetch(`/v1${path}`, { ...options, headers, signal: context.signal, cache: "no-store", credentials: "omit", redirect: "error" });
    context.check();
    if (!response.ok) {
      let text = response.statusText || "Request failed", code = "", requestId = response.headers.get("X-Request-Id") || "";
      try {
        const result = await response.json(); context.check();
        const detail = result.error || result.detail;
        if (typeof detail === "string") text = detail;
        else if (detail?.message) { text = detail.message; code = detail.code || ""; requestId = detail.request_id || requestId; }
      } catch (error) { context.check(); }
      const failure = new Error(`${response.status}${code ? ` ${code}` : ""}: ${text}${requestId ? ` (request ${requestId})` : ""}`);
      failure.status = response.status; failure.code = code;
      throw failure;
    }
    return response;
  }
  async function json(connection, path, context, options = {}) {
    const response = await request(connection, path, context, options);
    const result = await response.json(); context.check();
    return result;
  }
  function validateProject(project) {
    if (!projectPattern.test(project)) throw new Error("Enter the exact case-sensitive Keystone project ID (1–64 ASCII identifier characters).");
  }
  function validatePackage(value, namespace) {
    if (!value || value.length > 253 || namespace.length + 1 + value.length > 255 || !value.split("/").every((part) => componentPattern.test(part))) {
      throw new Error("Package names require canonical lowercase repository components, e.g. team/app (namespace + name at most 255 characters).");
    }
  }
  function validateDigest(value, label) {
    if (!digestPattern.test(value)) throw new Error(`${label} must be sha256: followed by 64 lowercase hexadecimal characters.`);
  }
  function validateNamespace(value) {
    if (typeof value !== "string" || value.length > 63 || !componentPattern.test(value)) throw new Error("The server returned an invalid namespace.");
  }
  function connect(mode) {
    const keyMode = mode === "key";
    const project = (keyMode ? keyProject : memberProject).value;
    const credentialInput = keyMode ? keyInput : memberToken;
    const credential = credentialInput.value;
    credentialInput.value = "";
    if (keyMode) keyConnection = null; else memberConnection = null;
    invalidate();
    const target = keyMode ? "native-key-message" : "native-member-message";
    execute(target, async (context) => {
      validateProject(project);
      if (!credential) throw new Error("Enter a credential; it is retained only in this tab's memory after submission.");
      if (keyMode && !/^ppk_v1_[0-9a-f]{32}\.[A-Za-z0-9_-]{43}$/.test(credential)) throw new Error("Enter a native ppk_v1 package key, not a Keystone token.");
      if (!keyMode && credential.startsWith("ppk_v1_")) throw new Error("A package key cannot substitute for an original Keystone member token.");
      const pending = { mode, project, credential };
      const identity = ensureProject(await json(pending, keyMode ? "/auth/me" : "/projects/current", context), project);
      context.check();
      if (keyMode) {
        if (identity.actor_type !== "package-key" || !Array.isArray(identity.actions) || !identity.scope || !identity.key_id) throw new Error("The native key identity response is incomplete.");
        validateNamespace(identity.namespace);
      } else if (identity.namespace !== null) validateNamespace(identity.namespace);
      const connection = Object.freeze({ ...pending, identity });
      if (keyMode) keyConnection = connection; else memberConnection = connection;
      renderConnections();
      context.progress(`Connected original ${keyMode ? "package-key" : "member"} project ${project}${identity.namespace ? ` / ${identity.namespace}` : "; namespace is not registered"}. Auth domains remain separate.`);
    });
  }
  function disconnect(mode) {
    if (mode === "key") { keyConnection = null; keyInput.value = ""; }
    else { memberConnection = null; memberToken.value = ""; }
    invalidate();
    message(mode === "key" ? "native-key-message" : "native-member-message", "Disconnected; credential and secret-once display cleared. Changing either native connection cancels pending operations.");
  }

  panel.append(node("p", "SEPARATE NATIVE AUTHORITY", "eyebrow"), node("h2", "Project packages"),
    node("p", "Native package keys are separate from Keystone. They cannot build, export, manage legacy artifacts, issue keys or switch projects. Exact project IDs and namespace ownership are verified by the server; credentials are never persisted.", "hint"));
  const keySection = section("Package-key connection");
  const keyForm = identified("form", "native-key-form", "", "form-grid");
  const keyProject = field(keyForm, "Immutable project ID (exact assertion)", "native-key-project", "text", true);
  const keyInput = field(keyForm, "Native package key", "native-key", "password", true);
  keyInput.placeholder = "ppk_v1_…";
  const keyActions = node("div", "", "wide actions");
  keyActions.append(submit(keyForm, "Connect key", () => connect("key"), () => !keyConnection), button("Disconnect key", () => disconnect("key"), () => true, "secondary", true));
  keyForm.append(keyActions);
  const keyIdentity = identified("div", "native-key-identity");
  keySection.append(keyForm, status("native-key-message"), keyIdentity);

  const memberSection = section("Member control — original Keystone token");
  memberSection.append(node("p", "Only this token's original project is available. Namespace registration is explicit and immutable; key lists/revocation are caller-owned, not administrator-wide. The server checks current capability and membership on every request.", "hint"));
  const memberForm = identified("form", "native-member-form", "", "form-grid");
  const memberProject = field(memberForm, "Immutable project ID (exact assertion)", "native-member-project", "text", true);
  const memberToken = field(memberForm, "Original member Keystone token", "native-member-token", "password", true);
  const memberActions = node("div", "", "wide actions");
  memberActions.append(submit(memberForm, "Connect member", () => connect("member"), () => !memberConnection), button("Disconnect member", () => disconnect("member"), () => true, "secondary", true));
  memberForm.append(memberActions);
  const memberIdentity = identified("div", "native-member-identity");
  memberSection.append(memberForm, status("native-member-message"), memberIdentity);

  const browseSection = section("Inventory & immutable versions");
  const browseToolbar = node("div", "", "toolbar");
  const readMode = choice(browseToolbar, "Read authentication", "native-read-mode", [["key", "Package key"], ["member", "Original member token"]]);
  const typeFilter = choice(browseToolbar, "Package type", "native-type-filter", [["", "All types"], ["oci-image", "OCI image"], ["runtime-bundle", "Runtime bundle"]]);
  browseToolbar.append(button("Refresh inventory", () => execute("native-browse-message", (context) => loadInventory(context, false)), () => canRead() && !!reader()?.identity.namespace));
  const inventoryList = identified("div", "native-inventory", "", "rows");
  const inventoryNext = button("Next inventory page", () => execute("native-browse-message", (context) => loadInventory(context, true)), () => canRead() && !!inventoryCursor);
  const lookupForm = identified("form", "native-lookup-form", "", "toolbar");
  const lookupPackage = field(lookupForm, "Exact package name (without namespace)", "native-lookup-package", "text", true);
  lookupPackage.placeholder = "team/app";
  lookupForm.append(submit(lookupForm, "Inspect package", () => inspectPackage(lookupPackage.value), () => canRead() && !!reader()?.identity.namespace));
  const packageDetail = identified("div", "native-package-detail"), versionDetail = identified("div", "native-version-detail");
  const versionList = identified("div", "native-versions", "", "rows");
  const versionNext = button("Next version page", () => execute("native-browse-message", (context) => loadVersions(context, true)), () => canRead() && !!selectedPackage && !!versionCursor);
  browseSection.append(browseToolbar, status("native-browse-message"), inventoryList, inventoryNext, lookupForm, packageDetail, versionList, versionNext, versionDetail);
  readMode.addEventListener("change", () => {
    controller.abort(); task?.controller.abort();
    controller = new AbortController(); generation++; operation++; task = null;
    inventoryCursor = null; versionCursor = null; selectedPackage = ""; inventoryType = "";
    inventoryList.replaceChildren(); versionList.replaceChildren(); packageDetail.replaceChildren(); versionDetail.replaceChildren();
    lookupPackage.value = ""; clearSecret();
    message("native-browse-message", "Read authentication changed; previous project results and secret-once state cleared. A captured package-key upload remains bound to its original key and metadata.");
    syncControls();
  });
  typeFilter.addEventListener("change", () => { inventoryCursor = null; inventoryList.replaceChildren(); syncControls(); });

  async function loadInventory(context, next) {
    const connection = reader();
    if (!canRead()) throw new Error("This read connection has no effective packages:inventory authority.");
    const selectedType = next ? inventoryType : typeFilter.value;
    const params = { limit: "50" };
    if (selectedType) params.package_type = selectedType;
    if (next && inventoryCursor) params.cursor = inventoryCursor;
    const value = ensureContext(await json(connection, namespacePath(connection, "/packages", params), context), connection);
    context.check();
    if (!Array.isArray(value.items)) throw new Error("Invalid inventory page response.");
    for (const item of value.items) ensureContext(item, connection);
    inventoryType = selectedType; inventoryCursor = value.next_cursor || null;
    inventoryList.replaceChildren();
    for (const item of value.items) {
      const row = node("article", "", "row"), main = node("div", "", "row-main");
      main.append(node("strong", item.name), node("p", `${item.package_type} · ${item.version_count} immutable versions · ${item.visibility}`, "meta"),
        node("p", `Project ${item.project_id} / ${item.namespace} · latest ${item.latest_pushed_at || "—"} by ${item.latest_pushed_by || "—"}`, "meta"),
        node("code", (item.tags || []).map((tag) => `${tag.tag} → ${tag.digest}`).join("\n") || "No tags"));
      row.append(main, button("Details & versions", () => inspectPackage(item.name), () => canRead()));
      inventoryList.append(row);
    }
    if (!value.items.length) inventoryList.append(node("p", "No packages on this authorized page.", "empty"));
    context.progress(`Inventory page loaded for ${connection.project} / ${connection.identity.namespace}${inventoryCursor ? "; another page is available" : "; last page"}.`);
  }
  function inspectPackage(packageName) {
    execute("native-browse-message", async (context) => {
      const connection = reader();
      validatePackage(packageName, connection.identity.namespace);
      const value = ensureContext(await json(connection, namespacePath(connection, "/package", { package: packageName }), context), connection, packageName);
      context.check();
      selectedPackage = packageName; versionCursor = null; lookupPackage.value = packageName;
      descriptor(packageDetail, value); versionDetail.replaceChildren();
      const tags = node("div", "", "row-actions");
      for (const tag of value.tags || []) tags.append(button(`Inspect tag ${tag.tag}`, () => inspectTag(packageName, tag.tag), () => canRead()));
      packageDetail.append(tags);
      await loadVersions(context, false); context.check();
    });
  }
  async function loadVersions(context, next) {
    const connection = reader(), packageName = selectedPackage;
    const params = { package: packageName, limit: "50" };
    if (next && versionCursor) params.cursor = versionCursor;
    const value = ensureContext(await json(connection, namespacePath(connection, "/versions", params), context), connection, packageName);
    context.check();
    if (!Array.isArray(value.items)) throw new Error("Invalid version page response.");
    for (const item of value.items) { ensureContext(item, connection, packageName); validateDigest(item.root_digest, "Version root"); }
    versionCursor = value.next_cursor || null;
    versionList.replaceChildren();
    for (const item of value.items) {
      const row = node("article", "", "row"), main = node("div", "", "row-main"), actions = node("div", "", "row-actions");
      main.append(node("code", item.root_digest), node("p", `Pushed ${item.pushed_at} by ${item.pushed_by} · ${item.pushed_key_id === null ? "human system administrator" : `key ${item.pushed_key_id}`}`, "meta"),
        node("p", `Original archive ${item.archive_size_bytes} bytes · ${item.archive_digest}`, "meta"),
        node("p", (item.platforms || []).map((platform) => `${platform.os}/${platform.architecture}${platform.variant ? `/${platform.variant}` : ""}`).join(", ") || "No platform descriptors", "meta"));
      actions.append(button("Version details", () => inspectVersion(packageName, item.root_digest), () => canRead()),
        button("Download verified archive", () => downloadArchive(connection, packageName, item), () => canDownload(packageName)));
      row.append(main, actions); versionList.append(row);
    }
    if (!value.items.length) versionList.append(node("p", "No immutable versions on this page.", "empty"));
    context.progress(`Versions loaded for ${packageName}${versionCursor ? "; another page is available" : "; last page"}. No version or permanent-delete controls are exposed.`);
  }
  function inspectTag(packageName, tag) {
    execute("native-browse-message", async (context) => {
      const connection = reader();
      const value = ensureContext(await json(connection, namespacePath(connection, "/resolve", { package: packageName, tag }), context), connection, packageName);
      context.check();
      if (value.tag !== tag || value.digest !== value.root_digest) throw new Error("Tag resolution does not match the requested reference.");
      showVersion(connection, packageName, value);
      context.progress(`Tag ${tag} resolves to ${value.root_digest}; this console does not move or delete tags.`);
    });
  }
  function inspectVersion(packageName, digest) {
    execute("native-browse-message", async (context) => {
      const connection = reader();
      const value = ensureContext(await json(connection, namespacePath(connection, `/versions/${encodeURIComponent(digest)}`, { package: packageName }), context), connection, packageName);
      context.check();
      if (value.root_digest !== digest) throw new Error("Version response does not match the immutable requested digest.");
      showVersion(connection, packageName, value);
      context.progress(`Immutable version ${digest} loaded.`);
    });
  }
  function showVersion(connection, packageName, value) {
    descriptor(versionDetail, value);
    versionDetail.append(button("Download verified original archive", () => downloadArchive(connection, packageName, value), () => canDownload(packageName)));
  }
  function downloadArchive(connection, packageName, version) {
    execute("native-browse-message", (context) => {
      ensureContext(version, connection, packageName);
      validateDigest(version.root_digest, "Version root");
      return downloadVerified((path) => request(connection, path, context),
        namespacePath(connection, `/versions/${encodeURIComponent(version.root_digest)}/download`, { package: packageName }),
        version.archive_digest, version.archive_size_bytes, `${packageName}-${version.root_digest.slice(7, 19)}.tar`,
        context.check, (text) => context.progress(text));
    });
  }

  const uploadSection = section("Verified archive publication — package key only");
  uploadSection.append(node("p", "Select a real OCI-layout or runtime-bundle archive and explicitly enter its package type, tag and selected graph root. The archive bytes are hashed before transfer; the server independently validates the whole archive and complete selected graph. No snapshot or graph is fabricated. Existing versions/type are immutable. This console creates new tags (expected absence) or idempotently re-pushes the same tag/root; it does not move tags.", "hint"));
  const uploadForm = identified("form", "native-upload-form", "", "form-grid");
  const archiveFile = field(uploadForm, "Complete archive file", "native-upload-file", "file", true);
  const uploadPackage = field(uploadForm, "Package name (without namespace)", "native-upload-package", "text", true);
  const uploadType = choice(uploadForm, "Package type (explicit selection)", "native-upload-type", [["", "Select type"], ["oci-image", "OCI image"], ["runtime-bundle", "Runtime bundle"]]);
  uploadType.required = true;
  const uploadTag = field(uploadForm, "Tag (new or same root)", "native-upload-tag", "text", true);
  uploadTag.maxLength = 128;
  const rootDigest = field(uploadForm, "Selected graph root digest (not archive digest)", "native-upload-root", "text", true);
  rootDigest.placeholder = "sha256:…";
  const revision = field(uploadForm, "Source revision (optional, client-reported)", "native-upload-revision");
  const buildId = field(uploadForm, "Build ID (optional, client-reported)", "native-upload-build-id");
  revision.maxLength = 128; buildId.maxLength = 128;
  const uploadActions = node("div", "", "wide actions");
  uploadActions.append(submit(uploadForm, "Hash, upload & publish", startArchive, () => keyAllows("packages:write") && !activeUpload),
    button("Resume captured upload", resumeArchive, () => !!activeUpload && ["uploading", "validating"].includes(activeUpload.session.status) && keyAllows("packages:write", activeUpload.packageName)),
    button("Upload status", checkUploadStatus, () => !!activeUpload),
    button("Abort active upload", abortArchive, canAbortUpload, "danger", true),
    button("Clear completed/failed session", () => { activeUpload = null; uploadDescriptor.replaceChildren(); archiveFile.value = ""; syncControls(); },
      () => !!activeUpload && ["complete", "failed", "aborted"].includes(activeUpload.session.status)));
  uploadForm.append(uploadActions);
  const uploadDescriptor = identified("div", "native-upload-descriptor");
  uploadSection.append(uploadForm, status("native-upload-message"), uploadDescriptor);

  function renderUpload() {
    if (!activeUpload) { uploadDescriptor.replaceChildren(); return; }
    descriptor(uploadDescriptor, { session: activeUpload.session, captured_request: activeUpload.parameters, package: activeUpload.packageName,
      selected_file: activeUpload.file.name, phase: activeUpload.phase });
    syncControls();
  }
  function ensureSession(value, upload) {
    ensureContext(value, upload.connection, upload.packageName);
    if (value.key_id !== upload.connection.identity.key_id || value.owner_user_id !== upload.connection.identity.owner_user_id ||
        (upload.session && value.upload_id !== upload.session.upload_id) || !/^(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$/.test(value.upload_id) ||
        !Number.isSafeInteger(value.received_bytes) || value.received_bytes < 0 || value.received_bytes > upload.parameters.archive_size_bytes) {
      throw new Error("Upload status does not match the captured key, owner, session or archive size.");
    }
    return value;
  }
  function ensurePublication(value, upload) {
    ensureContext(value, upload.connection, upload.packageName);
    if (value.digest !== upload.parameters.root_digest || value.tag !== upload.parameters.tag || value.package_type !== upload.parameters.package_type || value.visibility !== "project") {
      throw new Error("Publication receipt does not match the captured root, tag, type or project visibility.");
    }
    return value;
  }
  function uploadPath(upload) {
    return namespacePath(upload.connection, `/uploads/${encodeURIComponent(upload.session.upload_id)}`, { package: upload.packageName });
  }
  function startArchive() {
    execute("native-upload-message", async (context) => {
      const connection = keyConnection, file = archiveFile.files[0], packageName = uploadPackage.value;
      const parameters = { package_type: uploadType.value, tag: uploadTag.value, root_digest: rootDigest.value,
        archive_size_bytes: file?.size, expected_tag_digest: null, provenance: {} };
      if (revision.value) parameters.provenance.source_revision = revision.value;
      if (buildId.value) parameters.provenance.build_id = buildId.value;
      validatePackage(packageName, connection.identity.namespace);
      validateDigest(parameters.root_digest, "Selected root digest");
      if (!file || !Number.isSafeInteger(file.size) || file.size < 1) throw new Error("Select a nonempty real archive with a safe integer byte length.");
      if (!["oci-image", "runtime-bundle"].includes(parameters.package_type)) throw new Error("Select the actual archive's package type explicitly.");
      if (!/^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$/.test(parameters.tag)) throw new Error("Tag must be 1–128 ASCII letters, digits, underscores, dots or hyphens; it cannot start with dot/hyphen.");
      if (!keyAllows("packages:write", packageName)) throw new Error("The connected key cannot publish this exact package.");
      parameters.archive_digest = await hashFile(file, context.check, (text) => context.progress(text));
      context.check(); validateDigest(parameters.archive_digest, "Archive digest");
      Object.freeze(parameters.provenance); Object.freeze(parameters);
      const upload = { connection, file, packageName, parameters, session: null, phase: "starting" };
      let value;
      try {
        value = await json(connection, namespacePath(connection, "/uploads", { package: packageName }), context, body("POST", parameters));
        context.check();
      } catch (error) {
        context.check();
        throw new Error(`${error.message} Upload start was not acknowledged. No blind retry was sent; if the server received it, an unacknowledged staged session may remain until its idle timeout.`);
      }
      upload.session = ensureSession(value, upload);
      activeUpload = upload; renderUpload();
      await transferArchive(upload, context); context.check();
    });
  }
  async function refreshUpload(upload, context) {
    const value = await json(upload.connection, uploadPath(upload), context);
    context.check(); upload.session = ensureSession(value, upload); renderUpload();
    return value;
  }
  function resumeArchive() {
    execute("native-upload-message", async (context) => {
      const upload = activeUpload;
      if (!upload) throw new Error("There is no captured upload in this tab. Reload deliberately discards files and credentials.");
      await refreshUpload(upload, context); context.check();
      await transferArchive(upload, context); context.check();
    });
  }
  async function transferArchive(upload, context) {
    try {
      context.check();
      if (upload.session.status === "complete") {
        ensurePublication(upload.session.result, upload); upload.phase = "complete"; renderUpload();
        context.progress(`Publication already complete: ${upload.session.result.digest}. Original immutable bytes are retained by the server.`); return;
      }
      if (!["uploading", "validating"].includes(upload.session.status)) throw new Error(`Upload is ${upload.session.status}; it cannot be resumed. Clear this terminal session before starting again.`);
      upload.phase = "transferring"; renderUpload();
      let offset = upload.session.received_bytes;
      if (upload.session.status === "validating" && offset !== upload.parameters.archive_size_bytes) throw new Error("Server validation state has an incomplete archive; check status before retrying.");
      while (offset < upload.parameters.archive_size_bytes) {
        context.check();
        const end = Math.min(offset + chunkBytes, upload.parameters.archive_size_bytes);
        const response = await request(upload.connection, uploadPath(upload), context, {
          method: "PATCH", headers: { "Content-Type": "application/octet-stream", "Upload-Offset": String(offset) }, body: upload.file.slice(offset, end),
        });
        context.check();
        const acknowledgement = response.headers.get("Upload-Offset");
        if (!/^[0-9]+$/.test(acknowledgement || "") || Number(acknowledgement) !== end) throw new Error("Server did not acknowledge the exact transferred offset. Use status/resume to reconcile; no blind retry was sent.");
        offset = end; upload.session = { ...upload.session, received_bytes: offset };
        context.progress(`Uploading ${offset} / ${upload.parameters.archive_size_bytes} bytes · ${upload.session.upload_id}`);
      }
      context.check(); upload.phase = "finalizing"; renderUpload();
      context.progress("Archive received; server is verifying digest, size and the real selected graph before atomic publication. Abort is unavailable during finalization.");
      const receipt = await json(upload.connection, uploadPath(upload), context, body("PUT", {}));
      context.check(); ensurePublication(receipt, upload);
      upload.session = { ...upload.session, status: "complete", result: receipt }; upload.phase = "complete";
      renderUpload(); context.progress(`Verified publication ${receipt.namespace}/${receipt.package}:${receipt.tag} @ ${receipt.digest}${receipt.already_published ? " (idempotent; existing immutable archive retained)" : ""}. Refresh inventory to inspect the stored version.`);
    } catch (error) {
      context.check();
      upload.phase = "paused"; renderUpload();
      throw new Error(`${error.message} Captured metadata/file are retained. Check status or resume with this same key; a lost finalization response may already have committed. A tag conflict requires aborting this session and selecting a new tag, not silently moving the existing tag.`);
    }
  }
  function checkUploadStatus() {
    execute("native-upload-message", async (context) => {
      const upload = activeUpload;
      const value = await refreshUpload(upload, context); context.check();
      if (value.status === "complete") ensurePublication(value.result, upload);
      upload.phase = value.status; renderUpload();
      context.progress(`Session ${value.upload_id}: ${value.status} · ${value.received_bytes} acknowledged bytes · expires ${value.expires_at}.`);
    });
  }
  function canAbortUpload() {
    return !!activeUpload && ["uploading", "validating"].includes(activeUpload.session.status) &&
      !["finalizing", "aborting"].includes(activeUpload.phase) && (!task || task.target === "native-upload-message");
  }
  function abortArchive() {
    const upload = activeUpload;
    if (!canAbortUpload()) return;
    execute("native-upload-message", async (context) => {
      upload.phase = "aborting"; renderUpload();
      let accepted = false;
      try {
        await request(upload.connection, uploadPath(upload), context, { method: "DELETE" }); context.check();
        accepted = true;
        const value = await refreshUpload(upload, context); context.check();
        if (value.status === "complete") ensurePublication(value.result, upload);
        upload.phase = value.status; renderUpload();
        context.progress(value.status === "complete"
          ? `Session ${value.upload_id} had already committed ${value.result.digest}; abort did not delete its immutable version or shared blobs.`
          : `Session ${value.upload_id}: ${value.status}. No immutable version or shared blob was deleted.`);
      } catch (error) {
        context.check(); upload.phase = "paused"; renderUpload();
        throw new Error(`${error.message} Transfer continuation was cancelled. ${accepted ? "Abort was accepted, but its final status could not be read; check status before clearing." : "Abort was not confirmed; retry abort/status after any server-side chunk lock is released."}`);
      }
    }, true);
  }

  const controlSection = section("Current project namespace & caller-owned keys");
  const controlActions = node("div", "", "actions");
  controlActions.append(button("Refresh current project", refreshMember, () => !!memberConnection),
    button("Register namespace", registerNamespace, () => !!memberConnection && !memberConnection.identity.namespace && memberAllows("packages_write")),
    button("List my keys", listKeys, () => !!memberConnection?.identity.namespace && (memberAllows("keys_issue") || memberAllows("keys_revoke"))));
  controlSection.append(controlActions, status("native-control-message"));
  const issueForm = identified("form", "native-issue-form", "", "form-grid");
  const keyName = field(issueForm, "Key name", "native-issue-name", "text", true);
  keyName.maxLength = 128;
  const keyDays = field(issueForm, "Expires in days (1–90)", "native-issue-days", "number", true);
  keyDays.min = "1"; keyDays.max = "90"; keyDays.step = "1"; keyDays.value = "30";
  const scopeMode = choice(issueForm, "Explicit package scope", "native-issue-scope", [["exact", "Exact package names"], ["all", "All packages in this project"]]);
  const scopeNames = field(issueForm, "Exact package names (one per line; 1–32)", "native-issue-packages", "textarea");
  scopeNames.placeholder = "team/app";
  const actionGroup = node("fieldset", "", "wide");
  actionGroup.append(node("legend", "Requested actions — only current owner authority can be delegated"));
  const actionInputs = [];
  for (const action of ["packages:inventory", "packages:read", "packages:write", "cache:read", "cache:write"]) {
    const label = node("label", action, "check"), input = node("input", "");
    input.type = "checkbox"; input.value = action; input.name = "native-key-action";
    input.checked = action === "packages:inventory";
    label.prepend(input); actionGroup.append(label); actionInputs.push(input);
  }
  issueForm.append(actionGroup);
  const issueActions = node("div", "", "wide actions");
  issueActions.append(submit(issueForm, "Issue secret-once key", issueKey, () => memberAllows("keys_issue") && !!memberConnection?.identity.namespace && !issuedSecret && !secretValue.textContent));
  issueForm.append(issueActions);
  const issuedMetadata = identified("div", "native-issued-metadata");
  const secretNote = node("p", "", "hint"), secretValue = identified("pre", "native-secret-once", "", "secret-once");
  secretValue.hidden = true;
  const secretActions = node("div", "", "actions");
  secretActions.append(button("Reveal issued key once", () => {
    if (!issuedSecret) return;
    secretValue.textContent = issuedSecret; issuedSecret = ""; secretValue.hidden = false;
    secretNote.textContent = "This is the only reveal. Retain the key securely now. Clear removes it from the DOM; it cannot be recovered from key listings. Reload or connection changes also clear it.";
    syncControls();
  }, () => !!issuedSecret), button("Clear issued secret", clearSecret, () => !!issuedSecret || !!secretValue.textContent, "secondary", true));
  const keyList = identified("div", "native-key-list", "", "rows");
  controlSection.append(issueForm, issuedMetadata, secretNote, secretValue, secretActions, keyList);
  scopeMode.addEventListener("change", syncControls);

  function refreshMember() {
    execute("native-control-message", async (context) => {
      const connection = memberConnection;
      const value = ensureProject(await json(connection, "/projects/current", context), connection.project);
      context.check();
      if (value.namespace !== null) validateNamespace(value.namespace);
      if (value.namespace !== connection.identity.namespace) {
        memberConnection = Object.freeze({ ...connection, identity: value });
        invalidate(); message("native-control-message", "Current namespace changed; stale inventory, upload and secret state cleared. Inspect the verified current context before continuing.");
      } else {
        memberConnection = Object.freeze({ ...connection, identity: value }); renderConnections();
        context.progress("Current project capabilities refreshed; no project switch or namespace registration was performed.");
      }
    });
  }
  function registerNamespace() {
    execute("native-control-message", async (context) => {
      const connection = memberConnection;
      const value = ensureProject(await json(connection, `/projects/${encodeURIComponent(connection.project)}/namespace`, context, body("PUT", {})), connection.project);
      context.check(); validateNamespace(value.namespace);
      memberConnection = Object.freeze({ ...connection, identity: value });
      invalidate();
      message("native-control-message", `Registered verified immutable namespace ${value.namespace} for ${value.project_id}. No arbitrary alias, rename or takeover was requested.`);
    });
  }
  async function loadKeys(context) {
    const connection = memberConnection;
    const value = ensureContext(await json(connection, namespacePath(connection, "/keys"), context), connection);
    context.check();
    if (!Array.isArray(value.items)) throw new Error("Invalid caller-owned key list response.");
    for (const item of value.items) ensureContext(item, connection);
    keyList.replaceChildren();
    for (const item of value.items) {
      const row = node("article", "", "row"), main = node("div", "", "row-main");
      main.append(node("strong", item.name), node("code", item.key_id),
        node("p", `Owner ${item.owner_user_id} · project ${item.project_id} / ${item.namespace}`, "meta"),
        node("p", `Actions: ${(item.actions || []).join(", ")} · scope: ${JSON.stringify(item.scope)}`, "meta"),
        node("p", `Created ${item.created_at} · expires ${item.expires_at} · ${item.revoked_at ? `revoked ${item.revoked_at}` : Date.parse(item.expires_at) <= Date.now() ? "expired" : "not revoked"}`, "meta"));
      row.append(main, button("Revoke my key", () => revokeKey(item), () => memberAllows("keys_revoke") && !item.revoked_at, "danger"));
      keyList.append(row);
    }
    if (!value.items.length) keyList.append(node("p", "No keys owned by this member in the current project.", "empty"));
    context.progress("Loaded caller-owned metadata only; no secret or secret hash is returned by listings.");
  }
  function listKeys() { execute("native-control-message", loadKeys); }
  function issueKey() {
    execute("native-control-message", async (context) => {
      const connection = memberConnection, name = keyName.value, days = Number(keyDays.value);
      if (!name || name.length > 128 || /[\x00-\x1f\x7f]/.test(name)) throw new Error("Key name must contain 1–128 non-control characters.");
      if (!Number.isInteger(days) || days < 1 || days > 90) throw new Error("Key expiration must be an integer from 1 to 90 days.");
      const names = scopeNames.value.split(/\r?\n/).filter((value) => value !== "");
      if (scopeMode.value === "exact") {
        if (names.length < 1 || names.length > 32 || new Set(names).size !== names.length) throw new Error("Choose 1–32 unique exact package names, one per line.");
        for (const packageName of names) validatePackage(packageName, connection.identity.namespace);
      }
      const actions = actionInputs.filter((input) => input.checked).map((input) => input.value);
      if (!actions.length) throw new Error("Select at least one action to delegate; the server rejects actions beyond current owner authority.");
      const parameters = { name, expires_in_days: days, actions, scope: scopeMode.value === "all" ? { all_packages: true } : { packages: names } };
      const result = await json(connection, namespacePath(connection, "/keys"), context, body("POST", parameters));
      context.check(); ensureContext(result.key, connection);
      if (!/^ppk_v1_[0-9a-f]{32}\.[A-Za-z0-9_-]{43}$/.test(result.secret || "")) throw new Error("Issue response did not contain a valid secret-once package key.");
      issuedSecret = result.secret; result.secret = "";
      descriptor(issuedMetadata, result.key);
      secretNote.textContent = "Key issued. Reveal once and retain it securely. No automatic native or Keystone connection was changed; listings cannot recover this secret.";
      context.progress(`Issued ${result.key.key_id}. Reveal the secret once, then explicitly clear it when secured.`);
    });
  }
  function revokeKey(item) {
    if (!window.confirm(`Revoke your key ${item.name} (${item.key_id}) in project ${item.project_id}? Existing packages remain immutable and are not deleted.`)) return;
    execute("native-control-message", async (context) => {
      const connection = memberConnection;
      ensureContext(item, connection);
      await request(connection, namespacePath(connection, `/keys/${encodeURIComponent(item.key_id)}`), context, { method: "DELETE" }); context.check();
      clearSecret(); issuedMetadata.replaceChildren();
      if (keyConnection?.identity.key_id === item.key_id) {
        keyConnection = null; invalidate();
        message("native-key-message", "Connected package key was revoked; credential cleared and pending native state cancelled.");
        message("native-control-message", `Revoked caller-owned key ${item.key_id}. Refresh your key list to inspect current metadata.`);
      } else { await loadKeys(context); context.check(); context.progress(`Revoked caller-owned key ${item.key_id}; immutable packages were not deleted.`); }
    });
  }
  window.addEventListener("pagehide", () => {
    keyConnection = null; memberConnection = null; keyInput.value = ""; memberToken.value = "";
    invalidate(); message("native-key-message", "Credentials discarded on page exit; reconnect explicitly."); message("native-member-message", "Credentials discarded on page exit; reconnect explicitly.");
  });
  clearSecret();
  message("native-key-message", "Native package key not connected.");
  message("native-member-message", "Member control not connected. The legacy console connection is not reused.");
  syncControls();
})();
