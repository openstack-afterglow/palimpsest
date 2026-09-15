// Palimpsest Local Manager Dashboard JavaScript
(function () {
  'use strict';

  // Auth token initialization
  const urlParams = new URLSearchParams(window.location.search);
  let token = urlParams.get('token');
  if (token) {
    sessionStorage.setItem('palimpsest_token', token);
    const cleanUrl = window.location.protocol + "//" + window.location.host + window.location.pathname;
    window.history.replaceState({}, document.title, cleanUrl);
  } else {
    token = sessionStorage.getItem('palimpsest_token');
  }

  // App state
  let activeTab = 'vms';
  let activeLogSource = null; // { type: 'vm'|'build', name: string }
  let pollInterval = null;

  // DOM elements
  const elGlobalError = document.getElementById('global-error');
  const elHostInfo = document.getElementById('host-info');
  const elBackendsInfo = document.getElementById('backends-info');
  const elStorageInfo = document.getElementById('storage-info');

  const elLogDrawer = document.getElementById('log-drawer');
  const elDrawerOverlay = document.getElementById('drawer-overlay');
  const elDrawerTitle = document.getElementById('drawer-title');
  const elLogContent = document.getElementById('log-content');
  const elLogTailSelect = document.getElementById('log-tail-select');

  // Utility helpers
  function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function showError(msg) {
    if (!msg) {
      elGlobalError.classList.add('hidden');
      return;
    }
    elGlobalError.textContent = msg;
    elGlobalError.classList.remove('hidden');
  }

  function formatBytes(bytes) {
    if (bytes === undefined || bytes === null || isNaN(bytes)) return '0 B';
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
  }

  function formatDuration(ms) {
    if (ms === undefined || ms === null || isNaN(ms)) return '-';
    if (ms < 1000) return `${ms}ms`;
    const sec = (ms / 1000).toFixed(1);
    if (sec < 60) return `${sec}s`;
    const min = Math.floor(sec / 60);
    const remSec = (sec % 60).toFixed(0);
    return `${min}m ${remSec}s`;
  }

  function formatDate(iso) {
    if (!iso) return '-';
    try {
      const d = new Date(iso);
      return d.toLocaleString();
    } catch (e) {
      return iso;
    }
  }

  function shortDigest(digest) {
    if (!digest) return '-';
    if (digest.length > 19) {
      return digest.substring(0, 12) + '...' + digest.substring(digest.length - 4);
    }
    return digest;
  }

  function copyToClipboard(text) {
    navigator.clipboard.writeText(text).then(() => {
      alert(`Copied digest to clipboard:\n${text}`);
    }).catch(err => {
      console.error('Failed to copy digest:', err);
    });
  }

  // API Fetch Wrapper
  async function apiFetch(endpoint, options = {}) {
    const currentToken = sessionStorage.getItem('palimpsest_token') || '';
    options.headers = options.headers || {};
    options.headers['Authorization'] = `Bearer ${currentToken}`;
    if (options.body && typeof options.body === 'object' && !(options.body instanceof FormData)) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(options.body);
    }

    try {
      const res = await fetch(endpoint, options);
      let data = {};
      const contentType = res.headers.get('Content-Type') || '';
      if (contentType.includes('application/json')) {
        data = await res.json();
      }

      if (!res.ok) {
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      return data;
    } catch (err) {
      throw err;
    }
  }

  // Header Summary Loader
  async function loadSummary() {
    try {
      const summary = await apiFetch('/api/v1/summary');
      if (summary.host) {
        elHostInfo.textContent = `${summary.host.system} / ${summary.host.machine}`;
      }
      if (summary.backends) {
        const parts = Object.entries(summary.backends).map(([b, status]) => {
          const avail = status.available ? 'avail' : 'unavail';
          return `${b} (${avail})`;
        });
        elBackendsInfo.textContent = parts.join(', ');
      }
      if (summary.storage) {
        elStorageInfo.textContent = `${formatBytes(summary.storage.total_state_bytes)} used`;
      }
    } catch (err) {
      console.error('Failed to load summary:', err);
      elHostInfo.textContent = 'Error';
    }
  }

  // VMs Tab
  async function loadVMs(isPoll = false) {
    const tbody = document.getElementById('vms-tbody');
    const warningsDiv = document.getElementById('vms-warnings');
    if (!isPoll) tbody.innerHTML = '<tr><td colspan="9" class="loading-cell">Loading virtual machines...</td></tr>';

    try {
      const data = await apiFetch('/api/v1/vms');
      warningsDiv.innerHTML = '';
      if (data.warnings && data.warnings.length > 0) {
        data.warnings.forEach(w => {
          const alert = document.createElement('div');
          alert.className = 'alert alert-danger';
          alert.textContent = `Warning: ${w}`;
          warningsDiv.appendChild(alert);
        });
      }

      const vms = data.vms || [];
      if (vms.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" class="empty-cell">No virtual machines found.</td></tr>';
        return;
      }

      tbody.innerHTML = '';
      vms.forEach(vm => {
        const tr = document.createElement('tr');

        // Status badge
        let statusBadgeClass = vm.status === 'running' ? 'badge-running' : 'badge-stopped';
        let statusLabel = vm.status || 'unknown';
        if (vm.stale) {
          statusBadgeClass = 'badge-stale';
          statusLabel += ' (stale)';
        }

        // SSH Endpoint
        let sshStr = '-';
        if (vm.ssh && vm.ssh.host) {
          sshStr = `${vm.ssh.host}:${vm.ssh.port || 22}`;
        } else if (vm.guest_ip) {
          sshStr = vm.guest_ip;
        }

        // Layer details
        const layerCount = vm.layer_count || (vm.layers ? vm.layers.length : 0);
        let layersCellHtml = `${layerCount}`;
        if (vm.layers && vm.layers.length > 0) {
          const layerList = vm.layers.map(l => escapeHtml(shortDigest(l.digest))).join(', ');
          layersCellHtml = `<details><summary>${layerCount} layers</summary><small style="font-family:var(--font-mono);">${layerList}</small></details>`;
        }

        tr.innerHTML = `
          <td><strong>${escapeHtml(vm.name)}</strong></td>
          <td><span class="badge badge-backend">${escapeHtml(vm.backend)}</span></td>
          <td><span class="badge ${statusBadgeClass}">${escapeHtml(statusLabel)}</span></td>
          <td><span class="digest-tag" title="${escapeHtml(vm.base_digest || '')}" data-digest="${escapeHtml(vm.base_digest || '')}">${escapeHtml(shortDigest(vm.base_digest))}</span></td>
          <td>${layersCellHtml}</td>
          <td>${escapeHtml(vm.memory_mib || '-')} MiB / ${escapeHtml(vm.vcpus || '-')} vCPU</td>
          <td><code>${escapeHtml(sshStr)}</code></td>
          <td>${escapeHtml(formatDate(vm.created_at))}</td>
          <td>
            <div class="btn-action-group">
              ${vm.status === 'running'
                ? `<button class="btn btn-secondary btn-sm action-vm-stop" data-name="${escapeHtml(vm.name)}">Stop</button>`
                : `<button class="btn btn-primary btn-sm action-vm-start" data-name="${escapeHtml(vm.name)}">Start</button>`
              }
              <button class="btn btn-secondary btn-sm action-vm-logs" data-name="${escapeHtml(vm.name)}">Logs</button>
              <button class="btn btn-danger btn-sm action-vm-rm" data-name="${escapeHtml(vm.name)}">Remove</button>
              <label class="vol-check"><input type="checkbox" class="rm-vol-check" data-name="${escapeHtml(vm.name)}"> +vol</label>
            </div>
          </td>
        `;
        tbody.appendChild(tr);
      });

      // Attach event listeners
      tbody.querySelectorAll('.digest-tag').forEach(tag => {
        tag.addEventListener('click', () => copyToClipboard(tag.getAttribute('data-digest')));
      });
      tbody.querySelectorAll('.action-vm-start').forEach(btn => {
        btn.addEventListener('click', () => startVM(btn.getAttribute('data-name')));
      });
      tbody.querySelectorAll('.action-vm-stop').forEach(btn => {
        btn.addEventListener('click', () => stopVM(btn.getAttribute('data-name')));
      });
      tbody.querySelectorAll('.action-vm-rm').forEach(btn => {
        btn.addEventListener('click', () => {
          const name = btn.getAttribute('data-name');
          const tr = btn.closest('tr');
          const volCheck = tr ? tr.querySelector('.rm-vol-check') : null;
          removeVM(name, volCheck ? volCheck.checked : false);
        });
      });
      tbody.querySelectorAll('.action-vm-logs').forEach(btn => {
        btn.addEventListener('click', () => openVMLogs(btn.getAttribute('data-name')));
      });

    } catch (err) {
      if (!isPoll) tbody.innerHTML = `<tr><td colspan="9" class="loading-cell" style="color:var(--status-danger);">Error loading VMs: ${escapeHtml(err.message)}</td></tr>`;
    }
  }

  async function startVM(name) {
    try {
      await apiFetch(`/api/v1/vms/${name}/start`, { method: 'POST' });
      loadVMs(true);
    } catch (err) {
      showError(`Failed to start VM ${name}: ${err.message}`);
    }
  }

  async function stopVM(name) {
    try {
      await apiFetch(`/api/v1/vms/${name}/stop`, { method: 'POST' });
      loadVMs(true);
    } catch (err) {
      showError(`Failed to stop VM ${name}: ${err.message}`);
    }
  }

  async function removeVM(name, volumes) {
    if (!confirm(`Are you sure you want to remove VM '${name}'?`)) return;
    try {
      await apiFetch(`/api/v1/vms/${name}?volumes=${volumes ? '1' : '0'}`, { method: 'DELETE' });
      loadVMs(true);
    } catch (err) {
      showError(`Failed to remove VM ${name}: ${err.message}`);
    }
  }

  // Artifacts Tab
  async function loadArtifacts() {
    const imagesTbody = document.getElementById('images-tbody');
    const layersTbody = document.getElementById('layers-tbody');
    const feedbackDiv = document.getElementById('artifacts-feedback');
    feedbackDiv.classList.add('hidden');

    imagesTbody.innerHTML = '<tr><td colspan="8" class="loading-cell">Loading images...</td></tr>';
    layersTbody.innerHTML = '<tr><td colspan="6" class="loading-cell">Loading layers...</td></tr>';

    try {
      const data = await apiFetch('/api/v1/store/artifacts');
      const images = data.images || [];
      const layers = data.layers || [];

      // Render Images
      if (images.length === 0) {
        imagesTbody.innerHTML = '<tr><td colspan="8" class="empty-cell">No cloud images found in store.</td></tr>';
      } else {
        imagesTbody.innerHTML = '';
        images.forEach(img => {
          const tr = document.createElement('tr');
          const refs = img.referenced_by ? [...(img.referenced_by.runs || []), ...(img.referenced_by.projects || [])].join(', ') : '-';
          const nameOrTags = img.tags && img.tags.length > 0
            ? img.tags.map(tag => typeof tag === 'string' ? tag : (tag && typeof tag === 'object' ? tag.tag : '')).filter(Boolean).join(', ')
            : (img.name || '-');

          tr.innerHTML = `
            <td><span class="digest-tag" title="${escapeHtml(img.digest)}" data-digest="${escapeHtml(img.digest)}">${escapeHtml(shortDigest(img.digest))}</span></td>
            <td><strong>${escapeHtml(nameOrTags)}</strong></td>
            <td>${escapeHtml(img.arch || '-')}</td>
            <td>${escapeHtml(img.disk_format || '-')}</td>
            <td>${escapeHtml(formatBytes(img.size_bytes))}</td>
            <td><small>${escapeHtml(refs || '-')}</small></td>
            <td>${escapeHtml(formatDate(img.created_at))}</td>
            <td>
              <button class="btn btn-danger btn-sm action-delete-artifact" data-digest="${escapeHtml(img.digest)}">Delete</button>
            </td>
          `;
          imagesTbody.appendChild(tr);
        });
      }

      // Render Layers
      if (layers.length === 0) {
        layersTbody.innerHTML = '<tr><td colspan="6" class="empty-cell">No layers found in store.</td></tr>';
      } else {
        layersTbody.innerHTML = '';
        layers.forEach(layer => {
          const tr = document.createElement('tr');
          const refs = layer.referenced_by ? [...(layer.referenced_by.runs || []), ...(layer.referenced_by.projects || [])].join(', ') : '-';
          const parentShort = layer.parent_digest ? shortDigest(layer.parent_digest) : '-';

          tr.innerHTML = `
            <td><span class="digest-tag" title="${escapeHtml(layer.digest)}" data-digest="${escapeHtml(layer.digest)}">${escapeHtml(shortDigest(layer.digest))}</span></td>
            <td><small>${escapeHtml(layer.media_type || '-')}<br>Parent: ${escapeHtml(parentShort)}</small></td>
            <td>${escapeHtml(formatBytes(layer.size_bytes))}</td>
            <td><small>${escapeHtml(refs || '-')}</small></td>
            <td>${escapeHtml(formatDate(layer.created_at))}</td>
            <td>
              <button class="btn btn-danger btn-sm action-delete-artifact" data-digest="${escapeHtml(layer.digest)}">Delete</button>
            </td>
          `;
          layersTbody.appendChild(tr);
        });
      }

      // Digest click & delete buttons
      document.querySelectorAll('.digest-tag').forEach(tag => {
        tag.addEventListener('click', () => copyToClipboard(tag.getAttribute('data-digest')));
      });
      document.querySelectorAll('.action-delete-artifact').forEach(btn => {
        btn.addEventListener('click', () => deleteArtifact(btn.getAttribute('data-digest')));
      });

    } catch (err) {
      imagesTbody.innerHTML = `<tr><td colspan="8" class="loading-cell" style="color:var(--status-danger);">Error: ${escapeHtml(err.message)}</td></tr>`;
      layersTbody.innerHTML = `<tr><td colspan="6" class="loading-cell" style="color:var(--status-danger);">Error: ${escapeHtml(err.message)}</td></tr>`;
    }
  }

  async function deleteArtifact(digest) {
    const feedbackDiv = document.getElementById('artifacts-feedback');
    feedbackDiv.classList.add('hidden');
    if (!confirm(`Delete artifact ${shortDigest(digest)}?`)) return;

    try {
      await apiFetch(`/api/v1/store/artifacts/${digest}`, { method: 'DELETE' });
      loadArtifacts();
    } catch (err) {
      feedbackDiv.className = 'feedback-msg alert alert-danger';
      feedbackDiv.textContent = `Deletion refused: ${err.message}`;
      feedbackDiv.classList.remove('hidden');
    }
  }

  // Import Form Handler
  document.getElementById('form-import').addEventListener('submit', async (e) => {
    e.preventDefault();
    const feedback = document.getElementById('import-feedback');
    feedback.classList.add('hidden');

    const path = document.getElementById('import-path').value.trim();
    const disk_format = document.getElementById('import-format').value;
    const arch = document.getElementById('import-arch').value;
    const os_variant = document.getElementById('import-variant').value.trim() || null;

    try {
      const res = await apiFetch('/api/v1/store/import', {
        method: 'POST',
        body: { path, disk_format, arch, os_variant }
      });
      feedback.className = 'feedback-msg alert alert-success';
      feedback.textContent = `Successfully imported image (digest: ${shortDigest(res.digest)})`;
      feedback.classList.remove('hidden');
      loadArtifacts();
    } catch (err) {
      feedback.className = 'feedback-msg alert alert-danger';
      feedback.textContent = `Import failed: ${err.message}`;
      feedback.classList.remove('hidden');
    }
  });

  // Builds Tab
  async function loadBuilds(isPoll = false) {
    const tbody = document.getElementById('builds-tbody');
    if (!isPoll) tbody.innerHTML = '<tr><td colspan="9" class="loading-cell">Loading build records...</td></tr>';

    try {
      const builds = await apiFetch('/api/v1/builds');
      if (!builds || builds.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" class="empty-cell">No builds recorded.</td></tr>';
        return;
      }

      tbody.innerHTML = '';
      builds.forEach(b => {
        const tr = document.createElement('tr');
        const tagsStr = b.output_tags && b.output_tags.length > 0 ? b.output_tags.map(escapeHtml).join(', ') : '-';
        const durStr = formatDuration(b.duration_ms);

        let statusClass = 'badge-stopped';
        if (b.status === 'success' || b.status === 'succeeded') statusClass = 'badge-running';
        if (b.status === 'failed' || b.status === 'error') statusClass = 'badge-stale';

        tr.innerHTML = `
          <td><code>${escapeHtml(b.build_id)}</code></td>
          <td><span class="badge badge-backend">${escapeHtml(b.engine)}</span></td>
          <td><span class="badge ${statusClass}">${escapeHtml(b.status)}</span></td>
          <td><strong>${tagsStr}</strong></td>
          <td>${escapeHtml(durStr)}</td>
          <td>${escapeHtml(formatDate(b.started_at))}</td>
          <td><span class="digest-tag" title="${escapeHtml(b.base_digest || '')}" data-digest="${escapeHtml(b.base_digest || '')}">${escapeHtml(shortDigest(b.base_digest))}</span></td>
          <td><small>${escapeHtml(b.cache_source || '-')}</small></td>
          <td>
            <button class="btn btn-secondary btn-sm action-build-details" data-id="${escapeHtml(b.build_id)}">Details</button>
          </td>
        `;
        tbody.appendChild(tr);

        // Expandable Detail Row
        const trDetail = document.createElement('tr');
        trDetail.id = `build-detail-${b.build_id}`;
        trDetail.className = 'hidden';
        let timingsHtml = '-';
        if (b.timings_ms && Object.keys(b.timings_ms).length > 0) {
          timingsHtml = Object.entries(b.timings_ms)
            .map(([k, v]) => `<strong>${escapeHtml(k)}:</strong> ${escapeHtml(formatDuration(v))}`)
            .join(' | ');
        }

        trDetail.innerHTML = `
          <td colspan="9" style="background:var(--bg-primary); padding:1rem;">
            <div style="margin-bottom:0.5rem;"><strong>Phase Timings:</strong> ${timingsHtml}</div>
            <div>
              <button class="btn btn-secondary btn-sm action-build-log" data-id="${escapeHtml(b.build_id)}">View Build Console Log</button>
            </div>
          </td>
        `;
        tbody.appendChild(trDetail);
      });

      tbody.querySelectorAll('.digest-tag').forEach(tag => {
        tag.addEventListener('click', () => copyToClipboard(tag.getAttribute('data-digest')));
      });
      tbody.querySelectorAll('.action-build-details').forEach(btn => {
        btn.addEventListener('click', () => {
          const id = btn.getAttribute('data-id');
          const detailRow = document.getElementById(`build-detail-${id}`);
          if (detailRow) detailRow.classList.toggle('hidden');
        });
      });
      tbody.querySelectorAll('.action-build-log').forEach(btn => {
        btn.addEventListener('click', () => openBuildLogs(btn.getAttribute('data-id')));
      });

    } catch (err) {
      if (!isPoll) tbody.innerHTML = `<tr><td colspan="9" class="loading-cell" style="color:var(--status-danger);">Error loading builds: ${escapeHtml(err.message)}</td></tr>`;
    }
  }

  // Storage Tab
  async function loadStorage() {
    try {
      const data = await apiFetch('/api/v1/storage');
      document.getElementById('storage-root-path').textContent = data.state_root || '-';
      document.getElementById('storage-root-source').textContent = data.source || '-';
      document.getElementById('storage-total-bytes').textContent = formatBytes(data.total_state_bytes);
      document.getElementById('storage-free-bytes').textContent = formatBytes(data.free_bytes);

      const breakdownUl = document.getElementById('storage-breakdown');
      breakdownUl.innerHTML = '';
      if (data.directories && Object.keys(data.directories).length > 0) {
        Object.entries(data.directories).forEach(([k, v]) => {
          const li = document.createElement('li');
          li.innerHTML = `<span>${escapeHtml(k)}:</span> <strong>${escapeHtml(formatBytes(v))}</strong>`;
          breakdownUl.appendChild(li);
        });
      } else {
        breakdownUl.innerHTML = '<li>No breakdown data available.</li>';
      }
    } catch (err) {
      console.error('Failed to load storage report:', err);
    }
  }

  // Move Storage Form Handler
  document.getElementById('form-move-storage').addEventListener('submit', async (e) => {
    e.preventDefault();
    const feedback = document.getElementById('move-feedback');
    feedback.classList.add('hidden');

    const destination = document.getElementById('move-dest').value.trim();
    const keep_source = document.getElementById('move-keep-source').checked;

    try {
      await apiFetch('/api/v1/storage/move', {
        method: 'POST',
        body: { destination, keep_source }
      });
      feedback.className = 'feedback-msg alert alert-success';
      feedback.textContent = 'Storage root successfully relocated!';
      feedback.classList.remove('hidden');
      loadStorage();
      loadSummary();
    } catch (err) {
      feedback.className = 'feedback-msg alert alert-danger';
      feedback.textContent = `Relocation failed: ${err.message}`;
      feedback.classList.remove('hidden');
    }
  });

  // Set Storage Form Handler
  document.getElementById('form-set-storage').addEventListener('submit', async (e) => {
    e.preventDefault();
    const feedback = document.getElementById('set-feedback');
    feedback.classList.add('hidden');

    const destination = document.getElementById('set-dest').value.trim();

    try {
      await apiFetch('/api/v1/storage/set', {
        method: 'POST',
        body: { destination }
      });
      feedback.className = 'feedback-msg alert alert-success';
      feedback.textContent = 'Storage root pointer updated!';
      feedback.classList.remove('hidden');
      loadStorage();
      loadSummary();
    } catch (err) {
      feedback.className = 'feedback-msg alert alert-danger';
      feedback.textContent = `Pointer update failed: ${err.message}`;
      feedback.classList.remove('hidden');
    }
  });

  // Log Drawer Functions
  async function openVMLogs(name) {
    activeLogSource = { type: 'vm', name };
    elDrawerTitle.textContent = `Logs for VM '${name}'`;
    elDrawerOverlay.classList.remove('hidden');
    elLogDrawer.classList.remove('hidden');
    await fetchLogs();
  }

  async function openBuildLogs(id) {
    activeLogSource = { type: 'build', name: id };
    elDrawerTitle.textContent = `Console Log for Build '${id}'`;
    elDrawerOverlay.classList.remove('hidden');
    elLogDrawer.classList.remove('hidden');
    await fetchLogs();
  }

  async function fetchLogs() {
    if (!activeLogSource) return;
    elLogContent.textContent = 'Loading logs...';
    const tail = elLogTailSelect.value;
    try {
      let data;
      if (activeLogSource.type === 'vm') {
        data = await apiFetch(`/api/v1/vms/${activeLogSource.name}/logs?tail=${tail}`);
      } else {
        data = await apiFetch(`/api/v1/builds/${activeLogSource.name}/log?tail=${tail}`);
      }
      elLogContent.textContent = data.log || '(no log output)';
      elLogContent.scrollTop = elLogContent.scrollHeight;
    } catch (err) {
      elLogContent.textContent = `Failed to fetch logs: ${err.message}`;
    }
  }

  function closeDrawer() {
    activeLogSource = null;
    elLogDrawer.classList.add('hidden');
    elDrawerOverlay.classList.add('hidden');
  }

  document.getElementById('btn-close-drawer').addEventListener('click', closeDrawer);
  elDrawerOverlay.addEventListener('click', closeDrawer);
  document.getElementById('btn-refresh-log').addEventListener('click', fetchLogs);
  elLogTailSelect.addEventListener('change', fetchLogs);

  // Tab Navigation
  function switchTab(tabId) {
    activeTab = tabId;
    document.querySelectorAll('.tab-button').forEach(btn => {
      const isSelected = btn.getAttribute('data-tab') === tabId;
      btn.classList.toggle('active', isSelected);
      btn.setAttribute('aria-selected', isSelected ? 'true' : 'false');
    });

    document.querySelectorAll('.tab-panel').forEach(panel => {
      if (panel.id === `panel-${tabId}`) {
        panel.classList.remove('hidden');
      } else {
        panel.classList.add('hidden');
      }
    });

    // Load tab content
    if (tabId === 'vms') loadVMs();
    else if (tabId === 'artifacts') loadArtifacts();
    else if (tabId === 'builds') loadBuilds();
    else if (tabId === 'storage') loadStorage();
  }

  document.querySelectorAll('.tab-button').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.getAttribute('data-tab')));
  });

  document.getElementById('btn-refresh-vms').addEventListener('click', () => loadVMs());
  document.getElementById('btn-refresh-artifacts').addEventListener('click', () => loadArtifacts());
  document.getElementById('btn-refresh-builds').addEventListener('click', () => loadBuilds());
  document.getElementById('btn-refresh-storage').addEventListener('click', () => loadStorage());

  // Polling setup: 3 seconds interval, only when visible and for active tab (vms / builds)
  function setupPolling() {
    if (pollInterval) clearInterval(pollInterval);
    pollInterval = setInterval(() => {
      if (document.visibilityState === 'visible') {
        if (activeTab === 'vms') {
          loadVMs(true);
        } else if (activeTab === 'builds') {
          loadBuilds(true);
        }
      }
    }, 3000);
  }

  // Initial Boot
  loadSummary();
  switchTab('vms');
  setupPolling();

})();
