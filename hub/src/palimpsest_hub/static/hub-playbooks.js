"use strict";

// Playbooks: images as the Hub stores them (an ordered layer stack with source and audience) and
// the VMs created from them. Reads and writes go through the connection hub.js owns.
(() => {
  const { node, message, empty, action, json, bodyOptions, guard, report, connection, identity } = window.Hub;
  const $ = (id) => document.getElementById(id);
  const architectures = { "linux/amd64": "x86_64", "linux/arm64": "aarch64" };
  const audiences = {
    private: "private: only the user who published it",
    internal: "internal: members of the owning project",
    public: "public: anyone, including anonymous readers",
  };
  const moving = new Set(["preparing", "booting", "stopping", "deleting", "resumed"]);
  const gib = 1024 ** 3;
  let playbooks = [], selected = null, choices = null, instances = [], pending = null, timer = null;
  let playbookVersion = 0, instanceVersion = 0, quietRounds = 0, lastSeen = "";
  const busy = new Set(), retryAt = new Map();

  function same(left, right) { return left.length === right.length && left.every((item, index) => item === right[index]); }
  function size(bytes) {
    if (typeof bytes !== "number") return "size not recorded";
    return bytes >= gib ? `${(bytes / gib).toFixed(2)} GiB` : `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  }
  function lines(id) { return $(id).value.split("\n").map((line) => line.replace(/\r$/, "")).filter((line) => line.length); }

  function merge(images, jobs) {
    const mine = new Map(jobs.map((job) => [job.id, job]));
    const result = images.map((image) => {
      const manifest = image.manifest, job = mine.get(image.id) || null;
      mine.delete(image.id);
      return {
        id: image.id, name: image.name, visibility: image.visibility, project: image.project_id, created: image.created_at,
        platform: manifest.platform || null, os: [manifest.os_family, manifest.os_version].filter(Boolean).join(" "),
        base: manifest.base_image_digest || null, layers: manifest.layers.map((layer) => ({ digest: layer.digest, size: layer.size_bytes })),
        source: manifest.source || null, runtime: manifest.runtime || null, closure: image.source_closure || [], job,
      };
    });
    // A completed job from before images were published has no image row; its ordered output is the stack.
    for (const job of mine.values()) {
      if (job.status !== "complete" || job.published_image_id || !job.output_digests?.length) continue;
      result.push({
        id: job.id, name: job.name, visibility: job.visibility, project: identity().project_id, created: job.completed_at || job.created_at,
        platform: null, os: "", base: null, layers: job.output_digests.map((digest) => ({ digest, size: null })),
        source: job.result?.source || null, runtime: job.result?.runtime || null, closure: [], job,
      });
    }
    return result.sort((left, right) => left.name.localeCompare(right.name) || String(left.created).localeCompare(String(right.created)));
  }

  // Why the Hub would refuse to start this playbook for the caller; empty when it can be requested.
  function refusal(playbook) {
    if (!identity()?.capabilities.upload) return "Creating a VM needs write authority in this project.";
    if (!playbook.job || playbook.job.status !== "complete") return "The Hub starts only images built by your own image job in this project. Pull this one with the client to run it elsewhere.";
    if (!same(playbook.job.output_digests || [], playbook.layers.map((layer) => layer.digest))) return "The image job's recorded layers differ from this playbook's layers.";
    if (playbook.platform && playbook.platform !== "linux/amd64") return `Built for ${playbook.platform}; Hub VMs are x86_64.`;
    return "";
  }

  function provenance(playbook) {
    const found = [], source = playbook.source || {}, git = playbook.job?.git;
    if (git) found.push(`Git ${git.repo_url} at commit ${git.revision}; Dockerfile ${git.dockerfile_path || "Dockerfile"}; context ${git.context_subdir || "repository root"}`);
    if (typeof source.kind === "string") found.push(`Source kind: ${source.kind}`);
    if (typeof source.reference === "string") found.push(`Reference: ${source.reference}`);
    for (const [key, label] of [["root_digest", "Root"], ["manifest_digest", "Manifest"], ["config_digest", "Config"]]) {
      if (typeof source[key] === "string") found.push(`${label}: ${source[key]}`);
    }
    if (playbook.job) found.push(`Built by your image job ${playbook.job.id} (${playbook.job.kind})`);
    if (playbook.closure.length) found.push(`${playbook.closure.length} source documents are kept with the image`);
    return found.length ? found : ["Not recorded"];
  }

  function fact(list, name, values) {
    list.append(node("dt", name));
    for (const value of [].concat(values)) list.append(node("dd", value));
  }

  function showPlaybook(playbook) {
    // The same playbook shown again (a list refresh) keeps the form and its request ID: a retry after a lost reply must not become a second VM.
    const changed = !selected || selected.id !== playbook.id;
    selected = playbook;
    if (changed) pending = null;
    const facts = $("playbook-facts"), layers = $("playbook-layers"), reason = refusal(playbook);
    const total = playbook.layers.every((layer) => typeof layer.size === "number") ? playbook.layers.reduce((sum, layer) => sum + layer.size, 0) : null;
    const command = [...(playbook.runtime?.entrypoint || []), ...(playbook.runtime?.cmd || [])];
    $("playbook-title").textContent = playbook.name;
    facts.replaceChildren(); layers.replaceChildren();
    fact(facts, "Identifier", playbook.id);
    fact(facts, "Audience", audiences[playbook.visibility] || playbook.visibility);
    fact(facts, "Owning project", playbook.project || "Not recorded");
    fact(facts, "Architecture", playbook.platform ? `${playbook.platform} (${architectures[playbook.platform] || "unknown"})` : "Not recorded for this image job; the Hub starts its output as x86_64");
    fact(facts, "Operating system", playbook.os || "Not recorded");
    fact(facts, "Base", playbook.base ? `Cloud image ${playbook.base}` : `Lowest layer ${playbook.layers[0].digest}. The stack is a complete root filesystem; the boot image chosen below only starts the VM.`);
    fact(facts, "Stack", `${playbook.layers.length} layers, ${total === null ? "total size not recorded" : size(total)}`);
    fact(facts, "Default command", command.length ? JSON.stringify(command) : "None; a command is required");
    fact(facts, "Source", provenance(playbook));
    fact(facts, "Created", playbook.created || "Not recorded");
    for (const layer of playbook.layers) {
      const item = node("li", "");
      item.append(node("code", layer.digest), node("span", ` ${size(layer.size)}`, "meta"));
      layers.append(item);
    }
    $("playbook-detail").hidden = false;
    $("launch-form").hidden = !!reason;
    message("launch-reason", reason || "You built this playbook in this project, so you can create a VM from it.");
    if (changed) message("launch-message", "");
    if (!reason) loadChoices();
  }

  function option(value, text) { const item = node("option", text); item.value = value; return item; }
  function fill(id, items, placeholder) {
    const kept = $(id).value;
    $(id).replaceChildren(...(placeholder === null ? [] : [option("", placeholder)]), ...items);
    if ([...$(id).options].some((item) => item.value === kept)) $(id).value = kept;
  }
  function bootMinimum() {
    const image = choices?.images.find((item) => item.id === $("launch-image").value);
    const minimum = image ? Math.max(2, image.min_disk_gib, Math.ceil(image.size_bytes / gib)) : 2;
    $("launch-boot").min = String(minimum);
    if (Number($("launch-boot").value) < minimum) $("launch-boot").value = String(minimum);
    if (choices) $("launch-boot").max = String(choices.limits.max_boot_gib);
  }
  async function loadChoices() {
    const version = connection(), check = guard(version);
    try {
      const first = !choices;
      if (first) {
        message("launch-message", "Reading the images, flavors, networks and keys you can use…");
        const loaded = await json("/instance-options"); check();
        choices = loaded;
      }
      fill("launch-image", choices.images.map((item) => option(item.id, `${item.name || item.id} · ${item.disk_format} · needs ${Math.max(item.min_disk_gib, Math.ceil(item.size_bytes / gib))} GiB`)), "Choose a boot image");
      fill("launch-flavor", choices.flavors.map((item) => option(item.id, `${item.name || item.id} · ${item.vcpus} vCPU · ${item.ram_mib} MiB`)), "Choose a flavor");
      fill("launch-network", choices.networks.map((item) => option(item.id, `${item.name || "unnamed"} · ${item.id}`)), "Choose a network");
      fill("launch-key", choices.key_names.map((name) => option(name, name)), "No key");
      $("launch-writable").max = String(choices.limits.max_writable_gib);
      bootMinimum();
      const cut = Object.entries(choices.truncated).filter(([, more]) => more).map(([name]) => name);
      if (first) message("launch-message", cut.length ? `Only the first 200 ${cut.join(", ")} are listed.` : "");
    } catch (error) { report(version, "launch-message", error); }
  }

  // Published images are listed oldest first, 200 a page; an own image beyond the pages read is fetched by its identifier.
  async function visibleImages(jobs, check) {
    const items = [];
    let more = true;
    for (let offset = 0; more && offset < 1000; offset += 200) {
      const page = (await json(`/published-images?limit=200&offset=${offset}`)).items; check();
      items.push(...page);
      more = page.length === 200;
    }
    const seen = new Set(items.map((image) => image.id));
    const missing = jobs.filter((job) => job.status === "complete" && job.published_image_id && !seen.has(job.published_image_id)).slice(0, 20);
    const own = await Promise.all(missing.map((job) => json(`/published-images/${job.published_image_id}`).catch(() => null))); check();
    return { items: [...items, ...own.filter(Boolean)], more };
  }

  async function listPlaybooks() {
    const version = connection(), request = ++playbookVersion, check = guard(version), container = $("playbook-list");
    try {
      if (!identity()?.capabilities.inventory) return empty(container, "This identity has no read authority for images.");
      const jobs = await json("/image-jobs?limit=100"); check();
      const images = await visibleImages(jobs, check);
      if (request !== playbookVersion) return;
      playbooks = merge(images.items, jobs);
      container.replaceChildren();
      if (!playbooks.length) return empty(container, "No playbooks are visible to this identity yet.");
      for (const playbook of playbooks) {
        const line = node("div", "", "row"), left = node("div", "", "row-main"), buttons = node("div", "", "row-actions");
        left.append(node("strong", playbook.name), node("span", playbook.visibility, "status"));
        left.append(node("div", `${playbook.platform || "architecture not recorded"} · ${playbook.layers.length} layers · ${refusal(playbook) ? "view only" : "can create a VM"}`, "meta"));
        left.append(node("code", playbook.id));
        buttons.append(action("Select", () => { showPlaybook(playbook); $("playbook-detail").scrollIntoView({ block: "nearest" }); }));
        line.append(left, buttons); container.append(line);
      }
      message("playbook-message", images.more ? "Only the 1000 oldest published images are listed, plus your own." : "");
      if (selected) {
        const current = playbooks.find((item) => item.id === selected.id);
        if (current) showPlaybook(current); else { selected = null; $("playbook-detail").hidden = true; }
      }
    } catch (error) { if (version === connection() && request === playbookVersion) empty(container, error.message); }
  }

  function describe(row) {
    const playbook = playbooks.find((item) => item.id === row.image_job_id);
    const steps = {
      volume_preparing: "boot volume is being prepared",
      volume_ready: row.server_create_attempted ? "server requested" : "boot volume is ready; the server has not been requested",
      server_attempted: "server requested",
      delete_volume_pending: "waiting for the server to disappear",
      delete_volume_ready: "server is gone; the boot volume can be removed",
      uncertain: "the provider state needs a manual check",
    };
    return [
      `${row.lifecycle} · ${steps[row.provider_phase] || row.provider_phase}${row.error_code ? ` · ${row.error_code}` : ""}`,
      `Playbook: ${playbook ? playbook.name : row.image_job_id || "selected by layer digests"} · Server: ${row.server_id || "none"} · Volume: ${row.boot_volume_id || "none"}`,
    ];
  }

  // One request per click: the button stays disabled until the answer is shown and the list is read again.
  async function instanceCall(button, text, call, row = null) {
    const version = connection(), check = guard(version);
    if (row && (busy.has(row.id) || Date.now() < (retryAt.get(row.id) || 0))) return;
    button.disabled = true;
    if (row) { busy.add(row.id); renderInstances(); }
    try {
      const result = await call(); check();
      message("instance-message", text(result));
      await listInstances();
    } catch (error) {
      report(version, "instance-message", error);
      if (row && version === connection() && error.retryAfter) {
        const seconds = Number(error.retryAfter);
        if (Number.isFinite(seconds) && seconds > 0) retryAt.set(row.id, Date.now() + seconds * 1000);
      }
    } finally {
      if (version === connection()) {
        if (row) { busy.delete(row.id); renderInstances(); }
        else button.disabled = false;
      }
    }
  }
  function advance(row, operation) {
    return json(`/instances/${row.id}/advance`, bodyOptions("POST", { operation, request_id: row.request_id, boot_volume_id: row.boot_volume_id }));
  }
  function removeInstance(row, button) {
    const typed = window.prompt(`Deleting a VM removes its server and its private writable data. Type the VM name to confirm.\n${row.name}`);
    if (typed !== row.name) return;
    instanceCall(button, () => `Delete requested for ${row.name}. No request is repeated automatically.`, () => json(`/instances/${row.id}`, { method: "DELETE" }), row);
  }
  function started(row, result) {
    if (result.server_create_attempted) return `Server requested for ${row.name}.`;
    return `No server was requested for ${row.name}${result.error_code ? `: ${result.error_code}` : ""}.`;
  }
  function stopInstance(row, button) {
    if (!window.confirm(`Stop ${row.name}? Its writable data is retained.${row.lifecycle === "service" ? " A stopped service cannot resume; delete it when finished." : " Dev resume requires retained successful execution state. Without it, this stop is one-way; delete the VM when finished."}`)) return;
    instanceCall(button, () => `Stop requested for ${row.name}. Writable data is retained.`, () => json(`/instances/${row.id}/stop`, { method: "POST" }), row);
  }
  function showResults(row, container) {
    for (const [label, result] of [["First result (sealed)", row.result], ["Latest resume result", row.resume_result]]) {
      if (!result) continue;
      const detail = node("details", "");
      detail.append(node("summary", label), node("pre", JSON.stringify(result, null, 2), "descriptor"));
      container.append(detail);
    }
  }
  async function showConnection(row, container) {
    const version = connection(), check = guard(version);
    try {
      const detail = await json(`/instances/${row.id}/connection`); check();
      container.replaceChildren(node("pre", JSON.stringify(detail, null, 2), "descriptor"));
    } catch (error) { report(version, "instance-message", error); }
  }

  function renderInstances() {
    const container = $("instance-list"), live = instances.filter((row) => row.status !== "deleted");
    container.replaceChildren();
    if (!live.length) empty(container, "No VMs in this project.");
    for (const row of live) {
      const line = node("div", "", "row"), left = node("div", "", "row-main"), buttons = node("div", "", "row-actions"), details = node("div", "");
      left.append(node("strong", row.name), node("span", row.status, `status ${["error", "uncertain"].includes(row.status) ? "error" : moving.has(row.status) ? "building" : ""}`));
      for (const text of describe(row)) left.append(node("div", text, "meta"));
      left.append(node("code", row.id), details);
      showResults(row, left);
      const mutate = (label, text, call, style = "") => {
        const button = action(label, (event) => instanceCall(event.currentTarget, text, call, row), style);
        button.disabled = busy.has(row.id) || Date.now() < (retryAt.get(row.id) || 0);
        buttons.append(button);
      };
      if (row.provider_phase === "volume_ready" && row.server_create_attempted === false && row.boot_volume_id) {
        mutate("Start server", (result) => started(row, result), () => advance(row, "create-server"));
      }
      if (row.provider_phase === "delete_volume_ready" && row.boot_volume_id) {
        mutate("Remove boot volume", () => `Boot volume removal requested for ${row.name}.`, () => advance(row, "delete-volume"), "danger");
      }
      if (row.server_id && ["ready", "error", "booting", "resumed"].includes(row.status)) {
        const button = action("Stop", (event) => stopInstance(row, event.currentTarget), "danger");
        button.disabled = busy.has(row.id) || Date.now() < (retryAt.get(row.id) || 0);
        buttons.append(button);
      }
      if (row.server_id && row.lifecycle === "dev" && ["shelved", "stopped"].includes(row.status)) {
        mutate("Resume", () => `Resume requested for ${row.name}. Watch Latest resume result; the first result stays sealed.`, () => json(`/instances/${row.id}/resume`, { method: "POST" }));
      }
      // Reading one VM records what the provider reports now; it requests nothing.
      buttons.append(action("Check provider", (event) => instanceCall(event.currentTarget, (result) => `${row.name}: ${result.status}; ${describe(result)[0]}.`, () => json(`/instances/${row.id}`))));
      if (row.server_id && row.status !== "deleting") buttons.append(action("Connection", () => showConnection(row, details)));
      if (row.status !== "deleting") {
        const button = action("Delete", (event) => removeInstance(row, event.currentTarget), "danger");
        button.disabled = busy.has(row.id) || Date.now() < (retryAt.get(row.id) || 0);
        buttons.append(button);
      }
      line.append(left, buttons); container.append(line);
    }
    const gone = instances.length - live.length;
    if (gone) container.append(node("p", `${gone} deleted VMs are not shown.`, "hint"));
  }

  // A VM is read again on its own only while the provider can change it without a step from the user.
  function settling(row) {
    if (!moving.has(row.status) || row.provider_phase === "uncertain") return false;
    if (row.provider_phase === "volume_ready" && row.server_create_attempted === false) return false;
    return !(row.status === "deleting" && (!row.server_id || row.provider_phase === "delete_volume_ready"));
  }

  // The list is SQL only; reading one VM also records what the provider reports now.
  async function listInstances(automatic = false) {
    const version = connection(), request = ++instanceVersion, check = guard(version);
    if (!automatic) quietRounds = 0;
    try {
      if (!identity()?.capabilities.inventory) return empty($("instance-list"), "This identity has no read authority for VMs.");
      const rows = await json("/instances?limit=50"); check(); if (request !== instanceVersion) return;
      const watched = rows.filter(settling).slice(0, 8);
      const fresh = await Promise.all(watched.map((row) => json(`/instances/${row.id}`).catch(() => row)));
      check(); if (request !== instanceVersion) return;
      const updated = new Map(fresh.map((row) => [row.id, row]));
      instances = rows.map((row) => updated.get(row.id) || row);
      renderInstances();
      window.clearTimeout(timer);
      const seen = JSON.stringify(instances.map((row) => [row.id, row.status, row.provider_phase, row.server_id, row.boot_volume_id, row.error_code]));
      quietRounds = seen === lastSeen ? quietRounds + 1 : 0;
      lastSeen = seen;
      if (!instances.some(settling)) return;
      // Ten minutes without any change ends the automatic reads; Refresh starts them again.
      if (quietRounds < 60) timer = window.setTimeout(() => listInstances(true), 10000);
      else message("instance-message", "Nothing changed for ten minutes; automatic reads stopped. Use Refresh or Check provider.");
    } catch (error) { if (version === connection() && request === instanceVersion) empty($("instance-list"), error.message); }
  }

  $("launch-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const version = connection(), check = guard(version), playbook = selected, button = $("launch-submit");
    if (!playbook || refusal(playbook)) return;
    const boot = Number($("launch-boot").value), writable = Number($("launch-writable").value), key = $("launch-key").value;
    if (!(writable < boot)) return message("launch-message", "Writable space must be smaller than the boot volume.");
    const body = {
      name: $("launch-name").value.trim(), lifecycle: $("launch-lifecycle").value, image_id: $("launch-image").value,
      flavor_id: $("launch-flavor").value, network_id: $("launch-network").value, boot_gib: boot, writable_gib: writable,
      image_job_id: playbook.job.id, layer_digests: playbook.job.output_digests, command: lines("launch-command"),
      readiness_argv: lines("launch-readiness"), ...(key ? { key_name: key } : {}),
    };
    // The same form sent again after a lost reply reuses its request ID, so the Hub answers with the same VM.
    const fingerprint = JSON.stringify(body);
    if (!pending || pending.fingerprint !== fingerprint) pending = { fingerprint, id: window.crypto.randomUUID() };
    button.disabled = true;
    try {
      const created = await json("/instances", bodyOptions("POST", { request_id: pending.id, ...body })); check();
      pending = null;
      message("launch-message", `VM ${created.name} was accepted (${created.id}). Its boot volume is being prepared; start the server from the list below when the volume is ready.`);
      await listInstances();
    } catch (error) { report(version, "launch-message", error); }
    finally { if (version === connection()) button.disabled = false; }
  });
  $("launch-image").addEventListener("change", bootMinimum);
  $("refresh-playbooks").addEventListener("click", listPlaybooks);
  $("refresh-instances").addEventListener("click", () => listInstances());

  window.Hub.selectPlaybook = async (id) => {
    const check = guard(connection());
    await listPlaybooks(); check();
    const playbook = playbooks.find((item) => item.id === id);
    if (!playbook) throw new Error("Job output is not in the visible playbook list. Refresh playbooks or check its publication receipt.");
    showPlaybook(playbook);
    $("playbook-detail").scrollIntoView({ block: "nearest" });
  };
  window.Hub.onConnect.push(() => Promise.all([listPlaybooks(), listInstances()]));
  window.Hub.onDisconnect.push(() => {
    window.clearTimeout(timer);
    playbooks = []; selected = null; choices = null; instances = []; pending = null; playbookVersion++; instanceVersion++;
    busy.clear(); retryAt.clear();
    quietRounds = 0; lastSeen = "";
    $("playbook-list").replaceChildren(); $("instance-list").replaceChildren(); $("playbook-detail").hidden = true;
    $("launch-submit").disabled = false;
    for (const id of ["playbook-message", "launch-message", "launch-reason", "instance-message"]) message(id, "");
  });
})();
