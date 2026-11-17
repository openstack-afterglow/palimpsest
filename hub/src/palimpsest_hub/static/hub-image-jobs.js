"use strict";

// Image admission only. The isolated worker builds and publishes; this page polls job receipts.
(() => {
  const { node, action, empty, message, json, bodyOptions, guard, report, connection, identity } = window.Hub;
  const $ = (id) => document.getElementById(id);
  let pending = null, sending = false, timer = null, requestVersion = 0, quietRounds = 0, lastSeen = "", retryAt = 0;

  function controls() {
    const kind = $("image-job-kind").value;
    $("image-job-form").action = `/v1/${{ git: "git-builds", registry: "registry-imports", dockerfile: "dockerfile-builds" }[kind]}`;
    for (const name of ["git", "registry", "dockerfile"]) {
      const group = $(`image-job-${name}-fields`);
      group.hidden = name !== kind;
      group.disabled = name !== kind || sending || !identity()?.capabilities.upload;
    }
    $("image-job-submit").disabled = sending || !identity()?.capabilities.upload || Date.now() < retryAt;
  }

  async function listJobs(automatic = false) {
    const version = connection(), request = ++requestVersion, check = guard(version), container = $("image-job-list");
    if (!automatic) quietRounds = 0;
    try {
      if (!identity()?.capabilities.inventory) return empty(container, "This identity has no read authority for image jobs.");
      const jobs = await json("/image-jobs?limit=100"); check();
      if (request !== requestVersion) return;
      container.replaceChildren();
      if (!jobs.length) empty(container, "No image jobs submitted by you in this project.");
      for (const job of jobs) {
        const row = node("div", "", "row"), left = node("div", "", "row-main"), buttons = node("div", "", "row-actions");
        left.append(node("strong", job.name), node("span", job.status, `status ${job.status}`));
        left.append(node("div", `${job.kind} · ${job.visibility}${job.error_code ? ` · ${job.error_code}` : ""}`, "meta"));
        left.append(node("code", job.id), node("div", `Request: ${job.request_id}`, "meta"));
        if (job.status === "complete" && job.output_digests?.length) {
          left.append(node("div", `${job.output_digests.length} output layers${job.published_image_id ? ` · Published image: ${job.published_image_id}` : ""}`, "meta"));
          buttons.append(action("Select output", async () => {
            try { await window.Hub.selectPlaybook(job.published_image_id || job.id); check(); }
            catch (error) { report(version, "image-job-message", error); }
          }));
        }
        row.append(left, buttons); container.append(row);
      }
      window.clearTimeout(timer);
      const seen = JSON.stringify(jobs.map((job) => [job.id, job.status, job.error_code, job.published_image_id]));
      quietRounds = seen === lastSeen ? quietRounds + 1 : 0;
      lastSeen = seen;
      controls();
      if (jobs.some((job) => ["queued", "building"].includes(job.status))) {
        if (quietRounds < 60) timer = window.setTimeout(() => listJobs(true), 10000);
        else message("image-job-message", "No job changed for ten minutes; automatic reads stopped. Use Refresh jobs.");
      }
    } catch (error) { if (version === connection() && request === requestVersion) empty(container, error.message); }
  }

  $("image-job-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (sending || !identity()?.capabilities.upload || Date.now() < retryAt) return;
    const version = connection(), check = guard(version), kind = $("image-job-kind").value;
    const body = {
      name: $("image-job-name").value.trim(), visibility: $("image-job-visibility").value,
      os_family: $("image-job-os-family").value.trim(), os_version: $("image-job-os-version").value.trim(),
    };
    let path;
    if (kind === "git") {
      path = "/git-builds";
      Object.assign(body, {
        repo_url: $("image-job-repo").value.trim(), revision: $("image-job-revision").value.trim(),
        dockerfile_path: $("image-job-path").value.trim(), context_subdir: $("image-job-context").value.trim(),
      });
      const credential = $("image-job-credential").value.trim();
      if (credential) body.credential_ref = credential;
    } else if (kind === "registry") {
      path = "/registry-imports";
      body.reference = $("image-job-reference").value.trim();
    } else {
      path = "/dockerfile-builds";
      body.dockerfile = $("image-job-dockerfile").value;
      const digest = $("image-job-context-digest").value.trim();
      if (digest) body.context_digest = digest;
    }
    const fingerprint = JSON.stringify([path, body]);
    // Only an unconfirmed admission keeps its UUID for an explicit unchanged-descriptor replay.
    if (!pending || pending.fingerprint !== fingerprint) pending = { fingerprint, id: window.crypto.randomUUID() };
    const requestId = pending.id;
    sending = true; controls();
    try {
      const job = await json(path, bodyOptions("POST", { ...body, request_id: requestId })); check();
      pending = null;
      message("image-job-message", `Job ${job.id} accepted (${job.status}). The worker publishes its output; select it below when complete. No automatic submission repeat is sent.`);
      await listJobs();
    } catch (error) {
      if (version === connection()) {
        const seconds = Number(error.retryAfter);
        if (Number.isFinite(seconds) && seconds > 0) retryAt = Date.now() + seconds * 1000;
        message("image-job-message", `${error.message} Request ID: ${requestId}. Refresh jobs to reconcile admission before any explicit resubmission; keep this descriptor unchanged.`);
      }
    } finally { if (version === connection()) { sending = false; controls(); } }
  });
  $("image-job-kind").addEventListener("change", controls);
  $("refresh-image-jobs").addEventListener("click", () => listJobs());
  window.Hub.onConnect.push(() => { controls(); return listJobs(); });
  window.Hub.onDisconnect.push(() => {
    window.clearTimeout(timer); requestVersion++;
    pending = null; sending = false; quietRounds = 0; lastSeen = ""; retryAt = 0;
    $("image-job-list").replaceChildren(); message("image-job-message", ""); controls();
  });
  controls();
})();
