(function () {
  'use strict';

  const $ = id => document.getElementById(id);
  const token = window.palimpsestSessionToken;
  delete window.palimpsestSessionToken;
  const views = {
    overview: ['개요', '이 호스트의 실제 리소스와 최근 관찰을 확인합니다.'],
    vms: ['가상 머신', '관리 중인 VM의 런타임 관찰입니다. 오래된 관찰은 실행 상태를 보장하지 않습니다.'],
    volumes: ['볼륨', '로컬 메타데이터 기준입니다. 연결 선언은 현재 블록 장치 연결이나 파일시스템 검증을 의미하지 않습니다.'],
    artifacts: ['레이어 · 이미지', '로컬 콘텐츠 저장소의 이미지, 레이어와 기타 아티팩트입니다.'],
    networks: ['네트워크', '저장된 구성 기준입니다. 브리지, 리스너 또는 실제 연결 활성 여부를 보장하지 않습니다.'],
    builds: ['빌드', '기록된 최근 빌드와 실제 단계별 소요 시간, 콘솔 로그를 확인합니다.'],
    storage: ['스토리지', 'Palimpsest 상태 디렉터리의 실제 사용량과 호스트 디스크 여유 공간입니다.']
  };
  const endpoints = {
    summary: '/api/v1/summary', vms: '/api/v1/vms', volumes: '/api/v1/volumes',
    artifacts: '/api/v1/store/artifacts', networks: '/api/v1/networks', builds: '/api/v1/builds', storage: '/api/v1/storage'
  };
  const state = Object.fromEntries(Object.keys(endpoints).map(key => [key, { data: null, error: null, updated: null }]));
  const filters = {};
  let activeView = 'overview';
  let paused = false;
  let authFailed = false;
  let timer = null;
  let refreshPromise = null;
  let lastSuccess = null;
  let actionBusy = false;
  let inspector = null;
  // Host capability probes and state-directory size walks change slowly; refresh them every sixth tick.
  const slowKeys = new Set(['summary', 'storage']);
  let tick = 0;
  let logPromise = null;
  let logOwner = null;
  let returnFocus = null;
  let returnSelector = null;

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }
  function captureFocus(container) {
    const focused = document.activeElement;
    return container.contains(focused) ? { ...focused.dataset } : null;
  }
  function restoreFocus(container, saved, fallback) {
    if (!saved) return;
    const match = [...container.querySelectorAll('button')].find(button =>
      saved.overviewView ? button.dataset.overviewView === saved.overviewView :
        saved.inspect && button.dataset.inspect === saved.inspect && button.dataset.resourceId === saved.resourceId);
    (match || fallback)?.focus({ preventScroll: true });
  }
  function text(value) {
    if (value === null || value === undefined || value === '') return '정보 없음';
    if (typeof value === 'string' || typeof value === 'number') return String(value);
    if (typeof value === 'boolean') return value ? '예' : '아니요';
    return '정보 없음';
  }
  const array = value => Array.isArray(value) ? value : [];
  const object = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  const items = value => array(value).filter(item => item && typeof item === 'object' && !Array.isArray(item));
  function bytes(value) {
    if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return '정보 없음';
    if (value === 0) return '0 B';
    const unit = Math.min(4, Math.max(0, Math.floor(Math.log(value) / Math.log(1024))));
    return new Intl.NumberFormat('ko-KR', { maximumFractionDigits: 1 }).format(value / 1024 ** unit) + ' ' + ['B', 'KiB', 'MiB', 'GiB', 'TiB'][unit];
  }
  function date(value) {
    if (!value) return '정보 없음';
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? '날짜 정보 없음' : parsed.toLocaleString('ko-KR', { hour12: false });
  }
  function duration(value) {
    if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return '정보 없음';
    return value < 1000 ? value + ' ms' : (value / 1000).toLocaleString('ko-KR', { maximumFractionDigits: 1 }) + ' 초';
  }
  function digest(value) { return typeof value === 'string' && value ? (value.length > 24 ? value.slice(0, 18) + '…' + value.slice(-6) : value) : '정보 없음'; }
  function joined(value) { return array(value).map(text).filter(part => part !== '정보 없음').join(', ') || '없음'; }
  function tags(value) { return array(value).map(tag => typeof tag === 'string' ? tag : object(tag).tag).filter(tag => typeof tag === 'string').join(', ') || '없음'; }
  const statusLabels = {
    creating: '생성 중', defined: '정의됨', fetching: '가져오는 중', converting: '변환 중', 'root-mounted': '루트 마운트됨',
    starting: '시작 중', running: '실행 중', stopping: '중지 중', stopped: '중지', exited: '종료됨', removing: '제거 중',
    removed: '제거됨', failed: '실패', error: '오류', success: '성공', succeeded: '성공', building: '빌드 중', pending: '대기',
    unknown: '알 수 없음', unavailable: '확인 불가', configured: '구성됨', isolated: '격리', missing: '없음',
    declared: '선언됨', 'declared-attached': '연결 선언', attached: '연결 기록', detached: '미연결 선언', retained: '보존됨',
    deleting: '삭제 중', 'observed-file': '파일 확인됨', unobservable: '관찰 불가'
  };
  // Fixed, path-free inventory warnings; per-run VM diagnostics keep the CLI's wording.
  const warningLabels = {
    'Some VM resource metadata is unavailable or inconsistent': '일부 VM 실행 기록을 읽을 수 없거나 일관되지 않습니다.',
    'Some managed VM disk observations are unavailable or inconsistent': '일부 관리 VM 디스크 파일을 확인할 수 없습니다.',
    'Some configured network metadata is unavailable or inconsistent': '일부 네트워크 구성 기록을 확인할 수 없습니다.',
    'Project volume metadata is unavailable or inconsistent': '프로젝트 볼륨 기록을 읽을 수 없거나 일관되지 않습니다.',
    'OCI root-volume metadata is unavailable or inconsistent': 'OCI 루트 볼륨 기록을 읽을 수 없거나 일관되지 않습니다.',
    'OCI network status is unavailable; the exact run has no committed domain plan or its plan is invalid': 'OCI 네트워크 상태 확인 불가 · 해당 실행에 확정된 도메인 계획이 없거나 유효하지 않습니다.'
  };
  function warningText(value) { return warningLabels[value] || text(value); }
  // A VM disk has no recorded capacity; show its observed overlay file length, labeled as such.
  function volumeSize(item) { return item.size_bytes == null && typeof item.file_size_bytes === 'number' ? '파일 ' + bytes(item.file_size_bytes) : bytes(item.size_bytes); }
  function statusText(value) { return statusLabels[value] || text(value); }
  function badge(value, stale) {
    const good = ['running', 'success', 'succeeded'].includes(value);
    const bad = ['failed', 'error'].includes(value);
    return node('span', 'badge' + (stale ? ' warn' : good ? ' good' : bad ? ' bad' : ''), statusText(value) + (stale ? ' · 오래된 관찰' : ''));
  }
  function notice(target, message, danger = false) {
    target.hidden = !message;
    target.textContent = message || '';
    target.className = 'notice' + (danger ? ' danger' : '');
  }
  function canControl() { return state.summary.data?.read_only === false && !state.summary.error && !authFailed; }
  function feedback(message, danger = false) { notice($('action-feedback'), message, danger); }

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timeout = !options.method || options.method === 'GET' ? setTimeout(() => controller.abort(), 15000) : null;
    try {
      const response = await fetch(path, {
        ...options, signal: controller.signal, cache: 'no-store', credentials: 'omit', referrerPolicy: 'no-referrer',
        headers: { Authorization: 'Bearer ' + token, ...(options.body ? { 'Content-Type': 'application/json' } : {}) },
        ...(options.body ? { body: JSON.stringify(options.body) } : {})
      });
      if (response.status === 401 || (response.status === 403 && (!options.method || options.method === 'GET'))) {
        authFailed = true;
        throw new Error('인증이 만료되었습니다. CLI에서 제공한 링크를 다시 여세요.');
      }
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.error === 'string' ? result.error : 'HTTP ' + response.status);
      return result;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('응답 시간이 초과되었습니다. 로컬 서버를 확인하세요.');
      if (error instanceof TypeError) throw new Error('로컬 서버에 연결할 수 없습니다. 서버 실행 상태를 확인하세요.');
      if (error instanceof SyntaxError) throw new Error('서버 응답을 읽을 수 없습니다.');
      throw error;
    } finally { clearTimeout(timeout); }
  }
  function validate(key, data) {
    if (key === 'builds') { if (!Array.isArray(data)) throw new Error('빌드 목록 응답 형식이 올바르지 않습니다.'); return; }
    if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('리소스 응답 형식이 올바르지 않습니다.');
    const list = { vms: 'vms', volumes: 'volumes', networks: 'networks', artifacts: 'artifacts' }[key];
    if (list && !Array.isArray(data[list])) throw new Error('리소스 목록 응답 형식이 올바르지 않습니다.');
  }
  function collection(key) {
    const data = state[key].data;
    if (data === null) return null;
    if (key === 'builds') return items(data);
    if (key === 'artifacts') {
      const images = new Set(items(data.images).map(item => item.digest));
      const layers = new Set(items(data.layers).map(item => item.digest));
      return items(data.artifacts).map(item => ({ ...item, display_kind: images.has(item.digest) ? 'image' : layers.has(item.digest) ? 'layer' : 'unknown' }));
    }
    return items(data[key]);
  }
  function itemId(key, item) { return String(key === 'vms' ? item.name : key === 'builds' ? item.build_id : key === 'artifacts' ? item.digest : item.id); }
  function itemName(key, item) {
    if (key === 'builds') return text(item.build_id);
    if (key === 'artifacts') return tags(item.tags) !== '없음' ? tags(item.tags) : item.name || digest(item.digest);
    return text(item.name);
  }
  const artifactKind = kind => ({ image: '이미지', layer: '레이어', unknown: '기타' })[kind] || '기타';
  function itemStatus(key, item) {
    if (key === 'artifacts') return item.display_kind;
    if (key === 'vms' && item.stale) return 'stale';
    return typeof item.status === 'string' && item.status ? item.status : 'unknown';
  }
  function searchText(key, item) {
    const fields = key === 'artifacts' ? [itemName(key, item), item.digest, item.arch, item.media_type, item.parent_digest] : [itemName(key, item), item.id, item.backend, item.project, item.kind, item.status, item.engine, item.subnet, item.mode, tags(item.output_tags)];
    fields.push(...items(item.attachments).map(attachment => attachment.name));
    return fields.filter(value => typeof value === 'string').join(' ').toLocaleLowerCase();
  }
  function openButton(key, item) {
    const button = node('button', 'row-open', itemName(key, item));
    button.dataset.inspect = key;
    button.dataset.resourceId = itemId(key, item);
    button.setAttribute('aria-label', itemName(key, item) + ' 상세 보기');
    button.addEventListener('click', () => openInspector(key, item, button));
    return button;
  }
  function cell(row, content, className) {
    const td = node('td', className);
    if (content instanceof Node) td.appendChild(content); else td.textContent = text(content);
    row.appendChild(td);
    return td;
  }
  function countText(value) { return Array.isArray(value) ? value.length + '개' : '정보 없음'; }
  function endpoint(item) {
    const ssh = object(item.ssh);
    if (ssh.host) return text(ssh.host) + (ssh.port != null ? ':' + text(ssh.port) : '');
    return text(item.guest_ip);
  }
  function references(item) { const refs = object(item.referenced_by); return [...array(refs.runs), ...array(refs.projects)].map(text).join(', ') || '없음'; }
  function listColumns(key) {
    return {
      vms: ['이름 / 프로젝트', '백엔드', '관찰 상태', '메모리 / CPU', 'SSH / 게스트 주소', '생성 시각'],
      volumes: ['이름 / 프로젝트', '유형', '메타데이터 상태', '기록된 크기', '연결 선언', '백엔드'],
      artifacts: ['이름 / 다이제스트', '분류', '형식 / 아키텍처', '크기', '참조', '생성 시각'],
      networks: ['이름', '유형 / 백엔드', '구성 상태', '모드', '서브넷', '연결 선언'],
      builds: ['빌드 ID / 출력 태그', '엔진', '상태', '소요 시간', '시작 시각', '로그']
    }[key];
  }
  function listHost(key) { return key === 'artifacts' ? $('artifacts-list') : $('panel-' + key); }
  function createList(key) {
    filters[key] = { search: '', status: '', signature: null };
    const host = listHost(key);
    const warning = node('div'); warning.id = key + '-warnings'; host.appendChild(warning);
    const controls = node('div', 'filters');
    const searchLabel = node('label', 'search-field', '검색');
    const search = node('input'); search.type = 'search'; search.id = key + '-search'; search.placeholder = '이름, ID 또는 관련 리소스';
    search.addEventListener('input', () => { filters[key].search = search.value; renderList(key, true); });
    searchLabel.appendChild(search); controls.appendChild(searchLabel);
    const filterLabel = node('label', '', key === 'artifacts' ? '분류' : '상태');
    const select = node('select'); select.id = key + '-status-filter';
    select.appendChild(new Option('전체', ''));
    select.addEventListener('change', () => { filters[key].status = select.value; renderList(key, true); });
    filterLabel.appendChild(select); controls.appendChild(filterLabel);
    const result = node('span', 'result-count'); result.id = key + '-result-count'; controls.appendChild(result);
    host.appendChild(controls);
    const body = node('div'); body.id = key + '-table-content'; host.appendChild(body);
  }
  function renderList(key, force = false) {
    const entry = state[key];
    const rows = collection(key);
    const settings = filters[key];
    const warning = $(key + '-warnings');
    warning.replaceChildren();
    if (entry.error) warning.appendChild(node('p', 'notice danger', entry.error + (entry.data ? ' 마지막 성공 데이터를 유지합니다 · 오래된 데이터' : ' 아직 표시할 데이터가 없습니다.')));
    for (const message of array(entry.data?.warnings)) warning.appendChild(node('p', 'notice', warningText(message)));
    if (rows === null) {
      $(key + '-result-count').textContent = '—';
      $(key + '-table-content').replaceChildren(node('p', 'empty-state', entry.error ? '불러오지 못했습니다. 지금 갱신으로 다시 시도하세요.' : '리소스를 불러오는 중입니다.'));
      return;
    }
    const signature = JSON.stringify([rows, canControl(), settings.search, settings.status]);
    if (!force && settings.signature === signature) return;
    settings.signature = signature;
    const select = $(key + '-status-filter');
    const options = [...new Set(rows.map(item => itemStatus(key, item)))].sort();
    if (settings.status && !options.includes(settings.status)) options.push(settings.status);
    select.replaceChildren(new Option('전체', ''));
    for (const value of options) select.appendChild(new Option(key === 'artifacts' ? artifactKind(value) : value === 'stale' ? '오래된 관찰' : statusText(value), value));
    select.value = settings.status;
    const query = settings.search.trim().toLocaleLowerCase();
    const selected = rows.filter(item => (!settings.status || itemStatus(key, item) === settings.status) && (!query || searchText(key, item).includes(query)));
    $(key + '-result-count').textContent = selected.length + ' / ' + rows.length + '개';
    const container = $(key + '-table-content');
    const focused = captureFocus(container);
    if (!selected.length) {
      container.replaceChildren(node('p', 'empty-state', rows.length ? '검색 또는 필터와 일치하는 리소스가 없습니다.' : views[key][0] + ' 리소스가 아직 없습니다.'));
      restoreFocus(container, focused, $(key + '-search'));
      return;
    }
    const wrap = node('div', 'table-wrap');
    const table = node('table'); table.id = 'table-' + key;
    table.appendChild(node('caption', '', views[key][0] + ' · 이름을 선택하면 상세 정보를 엽니다.'));
    const head = node('thead'); const hr = node('tr');
    for (const title of listColumns(key)) { const th = node('th', '', title); th.scope = 'col'; hr.appendChild(th); }
    head.appendChild(hr); table.appendChild(head);
    const tbody = node('tbody'); tbody.id = key + '-tbody';
    for (const item of selected) {
      const row = node('tr');
      const name = cell(row, openButton(key, item));
      if (key === 'vms' || key === 'volumes') name.appendChild(node('small', 'cell-note', item.project ? '프로젝트 · ' + text(item.project) : '프로젝트 연결 없음'));
      if (key === 'artifacts') name.appendChild(node('small', 'cell-note mono', digest(item.digest)));
      if (key === 'builds') name.appendChild(node('small', 'cell-note', tags(item.output_tags)));
      if (key === 'vms') {
        cell(row, item.backend, 'nowrap'); cell(row, badge(item.status, item.stale === true));
        cell(row, (item.memory_mib == null ? '정보 없음' : text(item.memory_mib) + ' MiB') + ' / ' + (item.vcpus == null ? '정보 없음' : text(item.vcpus) + ' vCPU'));
        cell(row, endpoint(item), 'mono'); cell(row, date(item.created_at), 'nowrap');
      } else if (key === 'volumes') {
        cell(row, item.kind, 'nowrap'); cell(row, badge(item.status)); cell(row, volumeSize(item), 'nowrap');
        cell(row, countText(item.attachments), 'nowrap'); cell(row, item.backend, 'nowrap');
      } else if (key === 'artifacts') {
        cell(row, artifactKind(item.display_kind), 'nowrap'); cell(row, text(item.disk_format || item.media_type) + ' / ' + text(item.arch));
        cell(row, bytes(item.size_bytes), 'nowrap'); cell(row, references(item)); cell(row, date(item.created_at), 'nowrap');
      } else if (key === 'networks') {
        cell(row, text(item.kind) + ' / ' + text(item.backend), 'nowrap'); cell(row, badge(item.status));
        cell(row, item.mode, 'nowrap'); cell(row, item.subnet, 'mono'); cell(row, countText(item.attachments), 'nowrap');
      } else {
        cell(row, item.engine, 'nowrap'); cell(row, badge(item.status)); cell(row, duration(item.duration_ms), 'nowrap');
        cell(row, date(item.started_at), 'nowrap'); cell(row, item.log_available === false ? '기록 없음' : item.log_available === true ? '상세에서 보기' : '정보 없음', 'nowrap');
      }
      tbody.appendChild(row);
    }
    table.appendChild(tbody); wrap.appendChild(table); container.replaceChildren(wrap);
    restoreFocus(container, focused, $(key + '-search'));
  }

  function infoList(fields) {
    const dl = node('dl', 'info-list');
    for (const [label, value] of fields) {
      dl.appendChild(node('dt', '', label));
      const dd = node('dd');
      if (value instanceof Node) dd.appendChild(value); else dd.textContent = text(value);
      dl.appendChild(dd);
    }
    return dl;
  }
  function vmBreakdown(rows) {
    const counts = new Map();
    for (const vm of rows) {
      const status = vm.stale === true ? 'stale' : typeof vm.status === 'string' && vm.status ? vm.status : 'unknown';
      counts.set(status, (counts.get(status) || 0) + 1);
    }
    return [...counts].sort((a, b) => b[1] - a[1]).map(([status, count]) => (status === 'stale' ? '오래된 관찰' : statusText(status)) + ' ' + count).join(' · ') || '관리 VM 없음';
  }
  function renderOverview() {
    const content = $('overview-content');
    const focus = captureFocus(content);
    content.replaceChildren();
    const metrics = node('div', 'metrics');
    const notes = { vms: '런타임 관찰', volumes: '메타데이터 기준', artifacts: '로컬 저장소', networks: '구성 기준', builds: '최근 기록' };
    for (const key of ['vms', 'volumes', 'artifacts', 'networks', 'builds']) {
      const rows = collection(key);
      const button = node('button', 'metric'); button.dataset.overviewView = key;
      const note = state[key].error ? '갱신 실패 · 오래된 데이터' : key === 'vms' && rows !== null ? vmBreakdown(rows) : notes[key];
      button.append(node('span', 'metric-label', views[key][0]), node('strong', 'metric-value', rows === null ? '—' : rows.length), node('span', 'metric-note', note));
      button.addEventListener('click', () => navigate(key)); metrics.appendChild(button);
    }
    content.appendChild(metrics);
    const grid = node('div', 'two-columns');
    const host = node('div', 'card'); host.appendChild(node('h2', '', '호스트 및 실행 환경'));
    const summary = object(state.summary.data); const machine = object(summary.host);
    host.appendChild(infoList([['운영체제', machine.system], ['아키텍처', machine.machine], ['관찰 범위', '현재 호스트의 관리 리소스'], ['제어 모드', canControl() ? '관리 허용 · 명시적 opt-in' : '읽기 전용']]));
    const backends = object(summary.backends);
    const backendSection = node('div', 'detail-subsection'); backendSection.appendChild(node('h3', '', '백엔드 가용성'));
    if (!Object.keys(backends).length) backendSection.appendChild(node('p', 'muted', '백엔드 정보 없음'));
    for (const [name, raw] of Object.entries(backends)) {
      const backend = object(raw); const line = node('div', 'backend-line'); const description = node('div');
      description.appendChild(node('strong', '', name));
      if (backend.reason || backend.profile) description.appendChild(node('div', 'backend-description', [backend.reason, backend.profile].filter(value => typeof value === 'string').join(' · ')));
      line.append(description, node('span', 'badge' + (backend.available === true ? ' good' : ' warn'), backend.available === true ? '사용 가능' : backend.available === false ? '사용 불가' : '정보 없음'));
      backendSection.appendChild(line);
    }
    host.appendChild(backendSection); grid.appendChild(host);
    const storage = node('div', 'card'); storage.appendChild(node('h2', '', '상태 저장소'));
    const report = object(state.storage.data);
    storage.appendChild(infoList([['상태 사용량', bytes(report.total_state_bytes)], ['디스크 여유', bytes(report.free_bytes)], ['상태 루트', report.state_root], ['구성 출처', report.source]]));
    const storageButton = node('button', '', '스토리지 보고서 보기'); storageButton.dataset.overviewView = 'storage'; storageButton.style.marginTop = '24px'; storageButton.addEventListener('click', () => navigate('storage')); storage.appendChild(storageButton);
    if (state.storage.error) storage.appendChild(node('p', 'muted', '보고서 갱신 실패 · 마지막 데이터 표시'));
    grid.appendChild(storage); content.appendChild(grid);
    const observations = node('div', 'card'); observations.appendChild(node('h2', '', '관찰 안내'));
    const vms = collection('vms');
    observations.appendChild(node('p', 'muted', vms === null ? 'VM 관찰 정보가 아직 없습니다.' : 'VM ' + vms.length + '개 중 오래된 런타임 관찰 ' + vms.filter(vm => vm.stale === true).length + '개. 오래된 상태는 현재 실행 여부를 보장하지 않습니다.'));
    observations.appendChild(node('p', 'muted', '볼륨 연결과 네트워크 상태는 저장된 메타데이터 및 구성입니다. 실시간 사용량, 연결 상태 또는 트래픽을 추정하지 않습니다.'));
    for (const key of ['vms', 'volumes', 'networks']) for (const warning of array(state[key].data?.warnings)) observations.appendChild(node('p', 'notice', views[key][0] + ' · ' + warningText(warning)));
    content.appendChild(observations);
    const builds = collection('builds');
    const activity = node('div', 'card'); activity.appendChild(node('h2', '', '최근 빌드'));
    if (!builds?.length) activity.appendChild(node('p', 'muted', builds === null ? '빌드 정보를 기다리는 중입니다.' : '기록된 빌드가 없습니다.'));
    for (const build of (builds || []).slice(0, 3)) { const line = node('div', 'activity-line'); line.append(openButton('builds', build), badge(build.status), node('span', 'muted', date(build.started_at))); activity.appendChild(line); }
    content.appendChild(activity);
    restoreFocus(content, focus, $('nav-overview'));
  }
  function renderStorage() {
    const entry = state.storage; const content = $('storage-content'); content.replaceChildren();
    if (entry.error) content.appendChild(node('p', 'notice danger', entry.error + (entry.data ? ' 마지막 성공 데이터 유지 · 오래된 데이터' : '')));
    if (!entry.data) { content.appendChild(node('p', 'empty-state', entry.error ? '스토리지 보고서를 불러오지 못했습니다.' : '스토리지 보고서를 불러오는 중입니다.')); return; }
    const data = entry.data; const grid = node('div', 'two-columns');
    const root = node('div', 'card'); root.appendChild(node('h2', '', '상태 루트 정보'));
    root.appendChild(infoList([['현재 경로', data.state_root], ['구성 출처', data.source], ['상태 사용량', bytes(data.total_state_bytes)], ['호스트 디스크', bytes(data.total_bytes)], ['디스크 여유', bytes(data.free_bytes)], ['보고 시각', date(entry.updated)]])); grid.appendChild(root);
    const breakdown = node('div', 'card'); breakdown.appendChild(node('h2', '', '디렉터리별 사용량'));
    const directories = Object.entries(object(data.directories));
    if (!directories.length) breakdown.appendChild(node('p', 'muted', '디렉터리 사용량 정보 없음'));
    for (const [name, size] of directories) { const line = node('div', 'breakdown-line'); line.append(node('span', 'mono', name), node('strong', 'mono', bytes(size))); breakdown.appendChild(line); }
    grid.appendChild(breakdown); content.appendChild(grid);
  }
  function updateControls() {
    const enabled = canControl();
    $('access-mode').textContent = enabled ? '관리 허용' : '읽기 전용';
    $('access-mode').title = enabled ? '--allow-control로 시작됨 · VM, 아티팩트, 스토리지 변경 가능' : '변경 작업 비활성 · VM 상태 확인은 실제 상태가 바뀐 실행 기록의 상태 값을 갱신할 수 있습니다';
    $('access-mode').className = 'badge' + (enabled ? ' warn' : '');
    for (const id of ['import-controls', 'storage-controls']) $(id).hidden = !enabled;
    for (const control of document.querySelectorAll('.management input, .management select, .management button')) control.disabled = !enabled || actionBusy;
    if (inspector) renderActions();
  }
  function updateHeader() {
    $('btn-refresh').disabled = !!refreshPromise || actionBusy;
    $('btn-pause').disabled = false;
    $('btn-pause').textContent = paused ? '자동 갱신 재개' : '자동 갱신 일시 정지';
    $('btn-pause').setAttribute('aria-pressed', String(paused));
    $('last-success').textContent = lastSuccess ? date(lastSuccess) : '아직 없음';
    if (lastSuccess) $('last-success').dateTime = lastSuccess.toISOString();
    const failed = Object.keys(state).filter(key => state[key].error);
    $('refresh-state').textContent = authFailed ? '인증 필요 · 자동 갱신 중단' : refreshPromise ? '갱신 중…' : document.hidden ? '화면 숨김 · 자동 갱신 중단' : paused ? '자동 갱신 일시 정지' : failed.length ? '갱신 실패 · 5초 후 재시도' : '5초마다 자동 갱신';
    notice($('global-error'), authFailed ? '인증이 만료되었습니다. CLI 링크를 다시 여세요. 기존 데이터는 오래된 관찰로 유지됩니다.' : failed.length ? '갱신 실패: ' + failed.map(key => key === 'summary' ? '호스트 요약' : views[key][0]).join(', ') + '. 마지막 성공 데이터를 유지합니다. 로컬 서버 확인 후 지금 갱신으로 다시 시도하세요.' : '', true);
    for (const key of ['vms', 'volumes', 'artifacts', 'networks', 'builds']) {
      const rows = collection(key); document.querySelector('[data-count="' + key + '"]').textContent = rows === null ? '—' : rows.length + (state[key].error ? ' !' : '');
    }
    updateControls();
  }
  function render() {
    updateHeader();
    if (activeView === 'overview') renderOverview();
    else if (activeView === 'storage') renderStorage();
    else renderList(activeView);
  }
  function navigate(key) {
    if (!views[key]) key = 'overview';
    activeView = key;
    for (const name of Object.keys(views)) {
      $('panel-' + name).hidden = name !== key;
      const button = $('nav-' + name);
      if (name === key) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
    }
    $('view-title').textContent = views[key][0]; $('view-description').textContent = views[key][1];
    history.replaceState({}, '', location.pathname + location.search + '#' + key);
    render();
  }
  function schedule() {
    clearTimeout(timer);
    if (!paused && !authFailed && !document.hidden && !actionBusy) timer = setTimeout(refresh, 5000);
  }
  function refresh(full = false) {
    if (refreshPromise) return refreshPromise;
    clearTimeout(timer);
    const includeSlow = full || tick % 6 === 0;
    tick += 1;
    refreshPromise = (async () => {
      const keys = Object.keys(endpoints).filter(key => includeSlow || !slowKeys.has(key) || state[key].data === null || state[key].error);
      await Promise.all(keys.map(async key => {
        try { const data = await api(endpoints[key]); validate(key, data); state[key] = { data, error: null, updated: new Date() }; }
        catch (error) { state[key].error = error.message; }
      }));
      if (Object.values(state).every(entry => entry.data !== null && !entry.error)) { lastSuccess = new Date(); authFailed = false; }
      render();
      if (inspector && !authFailed) { loadDetail(); if (inspector?.logsOpened) await loadLogs(); }
    })().finally(() => { refreshPromise = null; updateHeader(); schedule(); });
    updateHeader();
    return refreshPromise;
  }

  const descriptions = {
    vms: 'VM 런타임 관찰입니다. 오래된 관찰은 마지막 기록을 표시하며 현재 실행 여부를 보장하지 않습니다.',
    volumes: '메타데이터 관찰 · 크기는 기록된 값만 표시합니다. 연결은 선언 기준이며 실제 블록 연결이나 파일시스템 검증이 아닙니다.',
    networks: '구성 관찰 · 저장된 네트워크 계획입니다. 실제 브리지, 리스너, 트래픽 또는 연결 활성 여부는 확인하지 않습니다.',
    artifacts: '로컬 콘텐츠 저장소 메타데이터와 관리 리소스 참조입니다.',
    builds: '빌드 기록과 실제 기록된 소요 시간입니다. 로그는 요청한 최근 줄 수로 제한됩니다.'
  };
  function detailSection(title, fields) { const section = node('section', 'detail-subsection'); section.append(node('h3', '', title), infoList(fields)); return section; }
  function renderPorts(ports, parent) {
    const section = node('section', 'detail-subsection'); section.appendChild(node('h3', '', '포트 공개 구성'));
    if (!Array.isArray(ports)) section.appendChild(node('p', 'muted', '포트 구성 정보 없음'));
    else if (!ports.length) section.appendChild(node('p', 'muted', '기록된 공개 포트 없음'));
    for (const port of items(ports)) section.appendChild(infoList([['호스트 주소', port.host_ip], ['호스트 포트', port.host_port], ['게스트 포트', port.guest_port], ['프로토콜', port.protocol]]));
    parent.appendChild(section);
  }
  function renderDetail() {
    if (!inspector) return;
    const { key, data } = inspector;
    const target = $('detail-content'); target.replaceChildren();
    $('drawer-kind').textContent = views[key][0] + ' / 상세'; $('drawer-title').textContent = itemName(key, data);
    $('drawer-description').textContent = descriptions[key];
    if (key === 'vms') {
      target.appendChild(infoList([['이름', data.name], ['실행 ID', data.run_id], ['프로젝트', data.project], ['백엔드', data.backend], ['런타임', data.runtime_kind], ['관찰 상태', badge(data.status, data.stale === true)], ['메모리', data.memory_mib == null ? '정보 없음' : text(data.memory_mib) + ' MiB'], ['vCPU', data.vcpus], ['SSH 주소', endpoint(data)], ['게스트 주소', data.guest_ip], ['네트워크 모드', data.network], ['기본 다이제스트', data.base_digest], ['아키텍처', data.base_arch], ['생성 시각', date(data.created_at)], ['기록 갱신', date(data.updated_at)]]));
      const layers = node('section', 'detail-subsection'); layers.appendChild(node('h3', '', '레이어'));
      if (!array(data.layers).length) layers.appendChild(node('p', 'muted', Array.isArray(data.layers) ? '연결된 레이어 없음' : '레이어 정보 없음'));
      for (const layer of items(data.layers)) layers.appendChild(infoList([['다이제스트', layer.digest], ['대상 장치', layer.target_dev]]));
      target.appendChild(layers);
      const volumes = node('section', 'detail-subsection'); volumes.appendChild(node('h3', '', '볼륨 연결 선언'));
      if (!array(data.volumes).length) volumes.appendChild(node('p', 'muted', Array.isArray(data.volumes) ? '기록된 볼륨 연결 없음' : '볼륨 연결 정보 없음'));
      for (const volume of items(data.volumes)) volumes.appendChild(infoList([['이름', volume.name], ['마운트 경로', volume.mount_path], ['읽기 전용', volume.read_only], ['파일시스템 선언', volume.filesystem], ['대상 장치', volume.target_dev]]));
      target.appendChild(volumes); renderPorts(data.ports, target);
    } else if (key === 'volumes') {
      target.appendChild(infoList([['ID', data.id], ['이름', data.name], ['유형', data.kind], ['프로젝트', data.project], ['백엔드', data.backend], ['메타데이터 상태', statusText(data.status)], ['기록된 크기', bytes(data.size_bytes)], ...('file_size_bytes' in data ? [['관찰된 파일 크기 (가상 용량 아님)', bytes(data.file_size_bytes)]] : []), ['보존 정책', data.retention_policy], ['출처', data.source]]));
      if (!array(data.attachments).length) target.appendChild(node('p', 'muted', Array.isArray(data.attachments) ? '연결 선언 없음 · 보존/미연결 메타데이터' : '연결 정보 없음'));
      for (const attachment of items(data.attachments)) target.appendChild(detailSection('연결 선언 · ' + text(attachment.name), [['VM 이름', attachment.name], ['실행 ID', attachment.run_id], ['마운트 경로', attachment.mount_path], ['읽기 전용', attachment.read_only]]));
    } else if (key === 'networks') {
      target.appendChild(infoList([['ID', data.id], ['이름', data.name], ['유형', data.kind], ['백엔드', data.backend], ['구성 상태', statusText(data.status)], ['모드', data.mode], ['서브넷', data.subnet], ['게이트웨이', data.gateway], ['외부 네트워크 선언', data.external], ['출처', data.source]]));
      if (data.mode === 'none') target.appendChild(node('p', 'notice', 'none 모드 · NIC 없는 격리 구성'));
      if (!array(data.attachments).length) target.appendChild(node('p', 'muted', Array.isArray(data.attachments) ? '연결 선언 없음' : '연결 정보 없음'));
      for (const attachment of items(data.attachments)) { const section = detailSection('연결 선언 · ' + text(attachment.name), [['VM 이름', attachment.name], ['실행 ID', attachment.run_id], ['기록된 게스트 주소', attachment.guest_ip]]); renderPorts(attachment.ports, section); target.appendChild(section); }
    } else if (key === 'artifacts') {
      target.appendChild(infoList([['다이제스트', data.digest], ['분류', artifactKind(data.display_kind)], ['저장소 유형', data.kind], ['이름', data.name], ['태그', tags(data.tags)], ['크기', bytes(data.size_bytes)], ['아키텍처', data.arch], ['디스크 형식', data.disk_format], ['미디어 유형', data.media_type], ['부모 다이제스트', data.parent_digest], ['생성 시각', date(data.created_at)], ['VM 참조', joined(object(data.referenced_by).runs)], ['프로젝트 참조', joined(object(data.referenced_by).projects)]]));
    } else {
      target.appendChild(infoList([['빌드 ID', data.build_id], ['엔진', data.engine], ['상태', badge(data.status)], ['플랫폼', data.platform], ['출력 태그', tags(data.output_tags)], ['출력 다이제스트', data.output_digest], ['기본 다이제스트', data.base_digest], ['부모 다이제스트', joined(data.parent_digests)], ['캐시 출처', data.cache_source], ['시작 시각', date(data.started_at)], ['완료 시각', date(data.finished_at)], ['소요 시간', duration(data.duration_ms)], ['로그 기록', data.log_available]]));
      const timings = Object.entries(object(data.timings_ms));
      const section = node('section', 'detail-subsection'); section.appendChild(node('h3', '', '기록된 단계별 소요 시간'));
      section.appendChild(timings.length ? infoList(timings.map(([name, value]) => [name, duration(value)])) : node('p', 'muted', '단계별 시간 정보 없음'));
      target.appendChild(section);
    }
    const source = state[key];
    const warnings = [];
    if (source.error) warnings.push('목록 갱신 실패 · 마지막 성공 데이터 표시');
    if (key === 'vms' && data.stale === true) warnings.push('오래된 VM 런타임 관찰');
    if (inspector.error) warnings.push(inspector.error + ' · 마지막 상세 정보 유지');
    notice($('detail-state'), warnings.join(' / '), !!inspector.error);
    renderActions();
  }
  async function openInspector(key, item, opener) {
    returnFocus = opener;
    returnSelector = { key, id: itemId(key, item) };
    inspector = { key, id: itemId(key, item), data: item, error: null, logsOpened: key === 'vms' || key === 'builds', log: null, logTime: null };
    $('log-section').hidden = !inspector.logsOpened;
    $('log-content').textContent = '로그를 불러오는 중입니다.'; $('log-state').textContent = '';
    renderDetail();
    $('detail-drawer').showModal(); $('btn-close-drawer').focus();
    loadDetail();
    if (inspector?.logsOpened) await loadLogs();
  }
  // Details are the list projections themselves; re-reading one item would repeat a live VM reconcile.
  function loadDetail() {
    if (!inspector) return;
    const current = inspector;
    const latest = collection(current.key)?.find(item => itemId(current.key, item) === current.id);
    if (latest) { current.data = latest; current.error = null; } else current.error = '현재 목록에서 이 리소스를 찾을 수 없습니다';
    renderDetail();
  }
  function loadLogs() {
    if (!inspector?.logsOpened) return Promise.resolve();
    const current = inspector; const tail = $('log-tail-select').value;
    if (logPromise) return logOwner === current ? logPromise : logPromise.then(() => inspector === current ? loadLogs() : undefined);
    logOwner = current;
    $('btn-refresh-log').disabled = true; $('log-tail-select').disabled = true;
    $('log-state').textContent = '최근 ' + tail + '줄을 불러오는 중…';
    logPromise = (async () => {
      try {
        const path = endpoints[current.key] + '/' + encodeURIComponent(current.id) + (current.key === 'vms' ? '/logs' : '/log') + '?tail=' + encodeURIComponent(tail);
        const result = await api(path);
        if (typeof result.log !== 'string') throw new Error('로그 응답 형식이 올바르지 않습니다');
        if (inspector !== current) return;
        current.log = result.log; current.logTime = new Date();
        const log = $('log-content'); const nearBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 32;
        const next = result.log || '기록된 로그가 없습니다.';
        // Keep an unchanged log's text node so a selection survives auto-refresh.
        if (log.textContent !== next) log.textContent = next;
        if (nearBottom) log.scrollTop = log.scrollHeight;
        $('log-state').textContent = '최근 ' + tail + '줄 · ' + date(current.logTime) + ' 갱신';
      } catch (error) {
        if (inspector !== current) return;
        $('log-state').textContent = error.message + (current.log !== null ? ' · 마지막 로그 유지 / 오래된 데이터 (' + date(current.logTime) + ')' : '');
        if (current.log === null) $('log-content').textContent = '로그를 불러오지 못했습니다. 로그 갱신으로 다시 시도하세요.';
        updateHeader();
      }
    })().finally(() => { logPromise = null; logOwner = null; $('btn-refresh-log').disabled = false; $('log-tail-select').disabled = false; });
    return logPromise;
  }
  function closeInspector() { $('detail-drawer').close(); }
  $('detail-drawer').addEventListener('close', () => {
    inspector = null;
    $('detail-actions').replaceChildren();
    let focus = returnFocus?.isConnected ? returnFocus : null;
    if (!focus && returnSelector) focus = [...document.querySelectorAll('[data-inspect]')].find(button => button.dataset.inspect === returnSelector.key && button.dataset.resourceId === returnSelector.id && !button.closest('[hidden]'));
    (focus || $('nav-' + activeView)).focus();
    returnFocus = null; returnSelector = null;
  });
  $('btn-close-drawer').addEventListener('click', closeInspector);
  $('detail-drawer').addEventListener('click', event => { if (event.target === $('detail-drawer')) { const rect = $('detail-drawer').getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right) closeInspector(); } });
  $('btn-refresh-log').addEventListener('click', loadLogs);
  $('log-tail-select').addEventListener('change', loadLogs);

  function renderActions() {
    const target = $('detail-actions');
    const focusAction = target.contains(document.activeElement) ? document.activeElement.dataset.action : null;
    const checked = $('remove-volumes')?.checked || false;
    target.replaceChildren();
    target.hidden = !inspector || !canControl() || !['vms', 'artifacts'].includes(inspector.key);
    if (target.hidden) return;
    const current = inspector; const group = node('div', 'action-group');
    if (current.key === 'vms') {
      const verb = current.data.status === 'running' ? 'stop' : 'start';
      const lifecycle = node('button', '', verb === 'stop' ? 'VM 중지' : 'VM 시작'); lifecycle.dataset.action = verb;
      lifecycle.disabled = actionBusy;
      lifecycle.addEventListener('click', () => mutate('VM ' + (verb === 'stop' ? '중지' : '시작') + ': ' + current.id, endpoints.vms + '/' + encodeURIComponent(current.id) + '/' + verb, { method: 'POST' }));
      group.appendChild(lifecycle);
      const remove = node('button', 'danger-button', 'VM 삭제'); remove.dataset.action = 'remove'; remove.disabled = actionBusy;
      remove.addEventListener('click', () => {
        const volumes = $('remove-volumes').checked;
        mutate('VM 삭제: ' + current.id + (volumes ? ' · 연결 볼륨도 삭제합니다' : ' · 볼륨은 유지합니다'), endpoints.vms + '/' + encodeURIComponent(current.id) + '?volumes=' + (volumes ? '1' : '0'), { method: 'DELETE' }, true);
      }); group.appendChild(remove);
      const label = node('label', 'check-label'); const checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.id = 'remove-volumes'; checkbox.checked = checked; checkbox.disabled = actionBusy; checkbox.dataset.action = 'volumes'; label.append(checkbox, document.createTextNode('VM 삭제 시 연결 볼륨도 삭제')); group.appendChild(label);
    } else {
      const remove = node('button', 'danger-button', '아티팩트 삭제'); remove.dataset.action = 'remove'; remove.disabled = actionBusy || !/^sha256:[0-9a-f]{64}$/.test(current.id);
      remove.addEventListener('click', () => {
        if (!/^sha256:[0-9a-f]{64}$/.test(current.id)) return;
        mutate('아티팩트 삭제: ' + current.id, endpoints.artifacts + '/' + current.id, { method: 'DELETE' }, true);
      }); group.appendChild(remove);
    }
    target.appendChild(group);
    if (focusAction) target.querySelector('[data-action="' + focusAction + '"]')?.focus();
  }
  async function mutate(label, path, options, closeOnSuccess = false) {
    if (!canControl() || actionBusy) return;
    if (!window.confirm(label + '\n이 작업은 로컬 리소스를 변경합니다. 계속하시겠습니까?')) return;
    actionBusy = true; clearTimeout(timer); updateControls(); updateHeader(); feedback('작업 중 · ' + label);
    try {
      if (refreshPromise) await refreshPromise;
      if (!canControl()) throw new Error('읽기 전용 모드이거나 호스트 요약을 확인할 수 없습니다.');
      await api(path, options);
      if (closeOnSuccess && $('detail-drawer').open) closeInspector();
      feedback('완료 · ' + label);
      await refresh(true);
    } catch (error) {
      feedback('작업 실패 · ' + error.message, true);
      if (inspector) notice($('detail-state'), '작업 실패 · ' + error.message, true);
    } finally { actionBusy = false; updateControls(); updateHeader(); schedule(); }
  }
  $('form-import').addEventListener('submit', event => {
    event.preventDefault();
    mutate('클라우드 이미지 가져오기', '/api/v1/store/import', { method: 'POST', body: { path: $('import-path').value.trim(), disk_format: $('import-format').value, arch: $('import-arch').value, os_variant: $('import-variant').value.trim() || null } });
  });
  $('form-move-storage').addEventListener('submit', event => {
    event.preventDefault();
    mutate('상태 루트 이동: ' + $('move-dest').value.trim() + ($('move-keep-source').checked ? ' · 원본 유지' : ' · 원본 파일 삭제'), '/api/v1/storage/move', { method: 'POST', body: { destination: $('move-dest').value.trim(), keep_source: $('move-keep-source').checked } });
  });
  $('form-set-storage').addEventListener('submit', event => {
    event.preventDefault();
    mutate('상태 루트 변경: ' + $('set-dest').value.trim(), '/api/v1/storage/set', { method: 'POST', body: { destination: $('set-dest').value.trim() } });
  });
  for (const key of ['vms', 'volumes', 'artifacts', 'networks', 'builds']) createList(key);
  for (const button of document.querySelectorAll('[data-view]')) button.addEventListener('click', () => navigate(button.dataset.view));
  $('btn-pause').addEventListener('click', () => { paused = !paused; updateHeader(); if (paused) clearTimeout(timer); else if (!document.hidden) refresh(); });
  $('btn-refresh').addEventListener('click', () => { authFailed = false; refresh(true); });
  document.addEventListener('visibilitychange', () => { clearTimeout(timer); updateHeader(); if (!document.hidden && !paused && !authFailed) refresh(true); });
  window.addEventListener('hashchange', () => { const key = location.hash.slice(1); if (views[key]) navigate(key); });
  navigate(location.hash.slice(1) || 'overview');
  refresh(true);
})();
