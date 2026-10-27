(function () {
  'use strict';

  const $ = id => document.getElementById(id);
  const token = window.palimpsestSessionToken;
  delete window.palimpsestSessionToken;
  const views = {
    overview: ['개요', '이 호스트의 실제 리소스와 최근 관찰을 확인합니다.'],
    vms: ['가상 머신', '이 호스트의 VM·로그·자원을 관찰합니다. --allow-control에서 외부 VM도 이름·백엔드 식별자 확인 후 제거할 수 있습니다. 시작·중지는 선택한 상태의 관리 VM만 허용됩니다.'],
    volumes: ['볼륨', '저장된 볼륨 기록과 호스트 백엔드 디스크 관찰입니다. 가상 용량, 파일 크기, 호스트 할당량을 구분하며 파일시스템 검증을 의미하지 않습니다.'],
    artifacts: ['레이어 · 이미지', '로컬 출력과 연결된 Palimpsest Hub·Docker Hub의 이미지/레이어를 조회하고 다운로드·업로드합니다. 런타임 호환성은 별도로 확인합니다.'],
    networks: ['네트워크', '저장된 네트워크 구성과 호스트 백엔드 관찰입니다. 연결 구성은 게스트 연결이나 트래픽을 보장하지 않습니다.'],
    builds: ['빌드', '기록된 최근 빌드와 실제 단계별 소요 시간, 콘솔 로그를 확인합니다.'],
    storage: ['스토리지', 'Palimpsest 상태 디렉터리의 실제 사용량과 호스트 디스크 여유 공간입니다.']
  };
  const endpoints = {
    summary: '/api/v1/summary', vms: '/api/v1/vms', volumes: '/api/v1/volumes',
    artifacts: '/api/v1/store/artifacts', networks: '/api/v1/networks', builds: '/api/v1/builds', storage: '/api/v1/storage',
    operations: '/api/v1/operations'
  };
  const state = Object.fromEntries(Object.keys(endpoints).map(key => [key, { data: null, error: null, updated: null }]));
  const completedOpIds = new Set();
  const pendingConsoleOpens = new Set();
  let activeConsoleSession = null;
  let activeConsoleTerm = null;
  let activeConsoleTarget = null;
  let consoleGeneration = 0;
  let consoleSizeObserver = null;
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
  let afterInspectorClose = null;
  let removalTarget = null;
  let removalReturnFocus = null;
  let diskDeletion = null;
  let diskDeletionReturnFocus = null;
  let volumeAttachmentSequence = 0;
  const remoteState = { connections: [], loaded: false, loading: false, searching: false, generation: 0, abort: null, result: null };
  // Remote credentials are deliberately tab-local: never serialize them or attach them to VM APIs.
  const hubState = { connection: null, connecting: false, generation: 0, abort: null };
  const remoteDetail = { generation: 0, abort: null, loading: false, item: null, connection: null, reference: null, returnFocus: null };
  let remoteRepositoryQuery = '';
  const pendingRemoteRuns = new Set();

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
    deleting: '삭제 중', available: '사용 가능', 'observed-file': '파일 확인됨', unobservable: '관찰 불가', active: '활성', inactive: '비활성'
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
  function resourceSource(item) { return item.source === 'host-backend' ? '호스트 백엔드 관찰' : text(item.source); }
  function isManagedVm(item) { return item.managed === true; }
  function vmScope(item) { return isManagedVm(item) ? '선택한 상태 · 수명 주기 제어' : item.lifecycle?.remove === true ? '호스트 VM · 이름 확인 후 제거 가능' : '호스트 관찰 · 제거 불가'; }
  function consoleMode(item) { return item.access?.shell === true ? 'shell' : item.access?.exec === true ? 'exec' : null; }
  function consoleEligible(item) { return canControl() && !state.vms.error && item.status === 'running' && item.stale !== true && consoleMode(item) !== null; }
  function lifecycleEligible(item, action = 'remove') {
    if (!canControl() || state.vms.error || state.vms.data?.stale === true || item.stale === true || item.status === 'removed') return false;
    const latest = collection('vms')?.find(vm => itemId('vms', vm) === itemId('vms', item));
    if (!latest || latest.name !== item.name || latest.backend !== item.backend || latest.managed !== item.managed || latest.run_id !== item.run_id || latest.stale === true || latest.status === 'removed' || latest.lifecycle?.[action] !== true) return false;
    return isManagedVm(item) ? !!item.run_id : action === 'remove' && !!item.removal_identity && latest.removal_identity === item.removal_identity;
  }
  function removalReason(item) {
    if (!canControl()) return '--allow-control 권한과 최신 호스트 정보가 필요합니다.';
    if (actionBusy) return '다른 변경 작업이 진행 중입니다.';
    if (state.vms.error || state.vms.data?.stale === true || item.stale === true) return 'VM 목록이 오래되었습니다. 지금 갱신 후 다시 선택하세요.';
    if (item.lifecycle?.remove !== true) return item.remove_reason || '이 백엔드는 VM 제거 기능을 제공하지 않습니다.';
    return lifecycleEligible(item) ? '' : '실행 UUID / 백엔드 식별자가 변경되었거나 확인되지 않습니다.';
  }
  function referenceEntries(item) {
    const refs = object(item.referenced_by); const blockers = object(item.deletion_blockers);
    return ['runs', 'projects', 'volumes'].flatMap(kind => [...new Set([...array(refs[kind]), ...array(blockers[kind])])].map(value => kind + ' · ' + text(value)));
  }
  function diskDeleteReason(key, item) {
    if (!canControl()) return '--allow-control 권한이 필요합니다.';
    if (state[key].error || state[key].data?.stale === true || item.stale === true) return '목록이 오래되었습니다. 지금 갱신 후 다시 선택하세요.';
    const latest = collection(key)?.find(row => itemId(key, row) === itemId(key, item));
    if (!latest || latest.name !== item.name || latest.backend !== item.backend || latest.stale === true) return '리소스 식별자가 변경되었습니다. 다시 선택하세요.';
    if (key === 'volumes' && !['named', 'retained-instance'].includes(latest.kind)) return '소유 이름 있는 볼륨 또는 보존 인스턴스 루트만 삭제할 수 있습니다.';
    if (key === 'volumes' && (!Array.isArray(latest.attachments) || latest.attachments.length)) return 'VM 연결 / 예약이 남아 있거나 연결 상태를 확인할 수 없습니다.';
    const refs = referenceEntries(latest);
    if (refs.length) return '참조 해제 필요: ' + refs.join(', ');
    return latest.delete_supported === true ? '' : latest.delete_reason || '서버가 안전한 삭제 기능을 제공하지 않습니다.';
  }
  function freshForMutation(...keys) {
    return canControl() && keys.every(key => state[key].data !== null && !state[key].error && state[key].data.stale !== true);
  }
  function volumeSize(item) {
    const sizes = ['용량 ' + bytes(item.size_bytes)];
    if ('file_size_bytes' in item) sizes.push('파일 ' + bytes(item.file_size_bytes));
    if ('allocated_bytes' in item) sizes.push('할당 ' + bytes(item.allocated_bytes));
    return sizes.join(' / ');
  }
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
  function canControlConsole() {
    if (!canControl() || !activeConsoleTarget || activeConsoleTarget.invalid || state.vms.error) return false;
    const latest = collection('vms')?.find(item => itemId('vms', item) === activeConsoleTarget.id);
    return !!latest && consoleEligible(latest) && latest.name === activeConsoleTarget.name && latest.run_id === activeConsoleTarget.run_id && latest.access?.[activeConsoleTarget.mode] === true;
  }
  function feedback(message, danger = false) {
    notice($('action-feedback'), message, danger);
    if (inspector) notice($('detail-feedback'), message, danger);
  }

  function hubHeaders(connection = hubState.connection) {
    return connection ? {
      'X-Palimpsest-Hub-Url': connection.url,
      'X-Palimpsest-Hub-Token': connection.token,
      ...(connection.project ? { 'X-Palimpsest-Hub-Project': connection.project } : {})
    } : {};
  }
  async function api(path, options = {}) {
    const controller = new AbortController();
    const remoteRequest = path === '/api/v1/remotes' || path.startsWith('/api/v1/remotes/');
    const remoteURL = remoteRequest ? new URL(path, window.location.origin) : null;
    const hubRequest = path === '/api/v1/remotes' || path === '/api/v1/remotes/hub/identity' || remoteURL?.searchParams.get('connection') === 'legacy';
    const timeout = !options.method || options.method === 'GET' ? setTimeout(() => controller.abort(), path.startsWith('/api/v1/remotes/artifact') ? 60000 : 15000) : null;
    const abort = () => controller.abort();
    if (options.signal?.aborted) abort();
    else options.signal?.addEventListener('abort', abort, { once: true });
    try {
      const response = await fetch(path, {
        ...options, signal: controller.signal, cache: 'no-store', credentials: 'omit', referrerPolicy: 'no-referrer',
        headers: { Authorization: 'Bearer ' + token, ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...(hubRequest ? hubHeaders() : {}), ...options.headers },
        ...(options.body ? { body: JSON.stringify(options.body) } : {})
      });
      if (response.status === 401 || (response.status === 403 && !remoteRequest && (!options.method || options.method === 'GET'))) {
        if (!authFailed) {
          authFailed = true;
          clearTimeout(timer);
          closeConsoleSession();
          updateConsoleBadge('인증 필요', 'bad');
          notice($('console-capability-info'), '인증이 만료되었습니다. CLI 링크를 다시 여세요.', true);
          updateHeader();
        }
        throw new Error('인증이 만료되었습니다. CLI에서 제공한 링크를 다시 여세요.');
      }
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.error === 'string' ? result.error : 'HTTP ' + response.status);
      return result;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error(remoteRequest ? '원격 조회가 취소되었거나 응답 시간이 초과되었습니다. 다시 조회하세요.' : '응답 시간이 초과되었습니다. palimpsest web 명령이 중지되었는지 확인하고, 다시 시작한 뒤 새 CLI 링크를 여세요.');
      if (error instanceof TypeError) throw new Error('로컬 서버에 연결할 수 없습니다. palimpsest web 명령이 중지되었을 수 있습니다. 다시 시작한 뒤 새 CLI 링크를 여세요.');
      if (error instanceof SyntaxError) throw new Error('서버 응답을 읽을 수 없습니다.');
      throw error;
    } finally {
      clearTimeout(timeout);
      options.signal?.removeEventListener('abort', abort);
    }
  }
  function validate(key, data) {
    if (key === 'operations') { if (!data || !Array.isArray(data.operations)) throw new Error('작업 목록 응답 형식이 올바르지 않습니다.'); return; }
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
      const images = new Set(items(data.images).map(item => itemId(key, item)));
      const layers = new Set(items(data.layers).map(item => itemId(key, item)));
      return items(data.artifacts).map(item => ({ ...item, display_kind: item.kind === 'oci-image' || item.kind === 'cloud-image' || images.has(itemId(key, item)) ? 'image' : layers.has(itemId(key, item)) ? 'layer' : 'unknown' }));
    }
    return items(data[key]);
  }
  function itemId(key, item) { return String(key === 'vms' ? item.id || item.name : key === 'builds' ? item.build_id : key === 'artifacts' ? (item.digest_type || 'cas') + '/' + item.digest : item.id); }
  function itemName(key, item) {
    if (key === 'builds') return text(item.build_id);
    if (key === 'artifacts') {
      const localTags = tags(item.tags); const buildTags = tags(item.output_tags);
      return localTags !== '없음' ? localTags : item.name || (buildTags !== '없음' ? buildTags : digest(item.digest));
    }
    return text(item.name);
  }
  const artifactKind = kind => ({ image: '이미지', layer: '레이어', unknown: '기타' })[kind] || '기타';
  const digestType = kind => ({ 'oci-manifest': 'OCI manifest', 'oci-archive': 'OCI 아카이브', cas: '로컬 CAS blob' })[kind] || text(kind);
  function artifactAvailability(item) {
    const subject = ['build-record', 'retained-source', 'remote-transfer'].includes(item.source) ? '아카이브 파일' : 'CAS blob';
    return ({ available: subject + ' 관찰됨', missing: subject + ' 없음', unavailable: subject + ' 관찰 불가' })[item.availability] || '가용성 정보 없음';
  }
  function buildFields(data) {
    return [['빌드 ID', data.build_id], ['엔진', data.engine], ['상태', badge(data.status)], ['플랫폼', data.platform], ['빌드 출력 태그 (CAS 태그 아님)', tags(data.output_tags)], ['출력 다이제스트', data.output_digest], ['OCI manifest 다이제스트', data.output_oci_manifest_digest], ['OCI 아카이브 다이제스트', data.output_oci_archive_digest], ['VM 런타임 블록 다이제스트', data.runtime_block_digest], ['기본 다이제스트', data.base_digest], ['부모 다이제스트', joined(data.parent_digests)], ['캐시 출처', data.cache_source], ['시작 시각', date(data.started_at)], ['완료 시각', date(data.finished_at)], ['소요 시간', duration(data.duration_ms)], ['로그 기록', data.log_available]];
  }
  function itemStatus(key, item) {
    if (key === 'artifacts') return item.display_kind;
    if (key === 'vms' && item.stale) return 'stale';
    return typeof item.status === 'string' && item.status ? item.status : 'unknown';
  }
  function searchText(key, item) {
    const fields = key === 'artifacts' ? [itemName(key, item), tags(item.output_tags), joined(item.build_ids), item.digest, item.digest_type, item.kind, item.arch, item.platform, item.media_type, item.parent_digest, item.runtime_block_digest, item.output_oci_manifest_digest, item.output_oci_archive_digest, item.source, item.availability, artifactAvailability(item), artifactKind(item.display_kind)] : [itemName(key, item), item.id, item.backend, item.project, item.kind, item.status, item.engine, item.subnet, item.mode, item.platform, item.output_digest, item.output_oci_manifest_digest, item.output_oci_archive_digest, item.runtime_block_digest, tags(item.output_tags)];
    if (key === 'artifacts') for (const build of items(item.builds)) fields.push(build.build_id, build.engine, build.platform, tags(build.output_tags), build.output_digest, build.output_oci_manifest_digest, build.output_oci_archive_digest, build.runtime_block_digest);
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
  function references(item) { return referenceEntries(item).join(', ') || '없음'; }
  function listColumns(key) {
    return {
      vms: ['이름 / 범위', '백엔드', '상태', '할당 메모리 / CPU', '접속 / 포트', '생성 시각', 'Actions'],
      volumes: ['이름 / 보존 정책', '유형', '관찰 / 기록 상태', '용량 / 파일 / 할당', '연결 / 참조', '백엔드', 'Actions'],
      artifacts: ['이름 / 다이제스트', '분류', '형식 / 아키텍처', '크기', '참조', '생성 시각'],
      networks: ['이름 / 출처', '유형 / 백엔드', '관찰 / 구성 상태', '모드', '서브넷', '연결 구성'],
      builds: ['빌드 ID / 출력 태그', '엔진', '상태', '소요 시간', '시작 시각', '로그']
    }[key];
  }
  function listHost(key) { return ['artifacts', 'vms', 'volumes', 'builds'].includes(key) ? $(key + '-list') : $('panel-' + key); }
  function createList(key) {
    filters[key] = { search: '', status: '', source: '', arch: '', compatibility: '', signature: null };
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
    if (key === 'artifacts') {
      search.placeholder = '태그, 빌드 ID, 다이제스트 또는 플랫폼';
      const sourceLabel = node('label', '', '출처'); const source = node('select'); source.id = 'artifacts-source-filter';
      source.append(new Option('전체', ''), new Option('로컬 빌드 출력', 'built'), new Option('빌드 연결 없는 아티팩트', 'other'));
      source.addEventListener('change', () => { filters[key].source = source.value; renderList(key, true); });
      sourceLabel.appendChild(source); controls.appendChild(sourceLabel);
      const archLabel = node('label', '', '아키텍처'); const arch = node('select'); arch.id = 'artifacts-arch-filter';
      arch.append(new Option('전체', ''), new Option('aarch64 · ARM64', 'aarch64'), new Option('x86_64 · AMD64', 'x86_64'));
      arch.addEventListener('change', () => { filters[key].arch = arch.value; renderList(key, true); });
      archLabel.appendChild(arch); controls.appendChild(archLabel);
      const compatibilityLabel = node('label', '', '이 클라이언트 플랫폼'); const compatibility = node('select'); compatibility.id = 'artifacts-compatibility-filter';
      compatibility.append(new Option('전체', ''), new Option('호환', 'compatible'), new Option('비호환', 'incompatible'), new Option('확인 불가', 'unknown'));
      compatibility.addEventListener('change', () => { filters[key].compatibility = compatibility.value; renderList(key, true); });
      compatibilityLabel.appendChild(compatibility); controls.appendChild(compatibilityLabel);
    }
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
    const signature = JSON.stringify([rows, canControl(), actionBusy, entry.error, settings.search, settings.status, settings.source, settings.arch, settings.compatibility]);
    if (!force && settings.signature === signature) return;
    settings.signature = signature;
    const select = $(key + '-status-filter');
    const options = [...new Set(rows.map(item => itemStatus(key, item)))].sort();
    if (settings.status && !options.includes(settings.status)) options.push(settings.status);
    select.replaceChildren(new Option('전체', ''));
    for (const value of options) select.appendChild(new Option(key === 'artifacts' ? artifactKind(value) : value === 'stale' ? '오래된 관찰' : statusText(value), value));
    select.value = settings.status;
    const query = settings.search.trim().toLocaleLowerCase();
    const selected = rows.filter(item => (!settings.status || itemStatus(key, item) === settings.status) && (!settings.source || (item.source === 'build-record' || items(item.builds).length > 0 ? 'built' : 'other') === settings.source) && (!settings.arch || item.compatibility?.arch === settings.arch) && (!settings.compatibility || item.compatibility?.status === settings.compatibility) && (!query || searchText(key, item).includes(query)));
    $(key + '-result-count').textContent = selected.length + ' / ' + rows.length + '개';
    if (key === 'artifacts' && $('tab-artifacts-local')) {
      $('tab-artifacts-local').textContent = `로컬 아티팩트 (${rows.length})`;
    }
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
      if (key === 'artifacts' && canControl() && item.display_kind === 'image') {
        const runBtn = node('button', 'btn-row-action btn-run-action', 'VM 실행');
        runBtn.type = 'button';
        runBtn.title = '이 이미지로 새 VM 실행 설정';
        runBtn.disabled = item.compatibility?.compatible !== true;
        if (runBtn.disabled) runBtn.title = array(item.compatibility?.reasons).join(' · ') || '플랫폼 호환성 확인 필요';
        runBtn.addEventListener('click', e => {
          e.stopPropagation();
          setupRunFromArtifact(item);
        });
        name.appendChild(runBtn);
      }
      if (key === 'volumes' || (key === 'vms' && item.project)) name.appendChild(node('small', 'cell-note', item.project ? '프로젝트 · ' + text(item.project) : '프로젝트 연결 없음'));
      if (key === 'vms') name.appendChild(node('small', 'cell-note', vmScope(item)));
      if (key === 'volumes' || key === 'networks') name.appendChild(node('small', 'cell-note', resourceSource(item)));
      if (key === 'artifacts') {
        name.appendChild(node('small', 'cell-note mono', digest(item.digest)));
        name.appendChild(node('small', 'cell-note', (items(item.builds).length || item.source === 'build-record' ? '로컬 빌드 · ' : '') + artifactAvailability(item)));
        const compatibility = object(item.compatibility);
        name.appendChild(node('small', 'cell-note', '플랫폼 ' + ({ compatible: '호환', incompatible: '비호환', unknown: '확인 불가' }[compatibility.status] || '확인 불가')));
      }
      if (key === 'builds') name.appendChild(node('small', 'cell-note', tags(item.output_tags)));
      if (key === 'vms') {
        cell(row, item.backend, 'nowrap'); cell(row, badge(item.status, item.stale === true));
        cell(row, (item.memory_mib == null ? '정보 없음' : text(item.memory_mib) + ' MiB') + ' / ' + (item.vcpus == null ? '정보 없음' : text(item.vcpus) + ' vCPU'));
        cell(row, endpoint(item), 'mono'); cell(row, date(item.created_at), 'nowrap');
        const ports = items(item.ports).map(port => `${port.host_ip || '*'}:${port.host_port} → ${port.guest_port}/${port.protocol || 'tcp'}`).join(' · ');
        if (ports) row.children[4].appendChild(node('small', 'cell-note mono', ports));
        const actions = node('div', 'row-actions');
        const detailsBtn = node('button', 'btn-row-action', '상세'); detailsBtn.type = 'button';
        detailsBtn.dataset.inspect = 'vms'; detailsBtn.dataset.resourceId = itemId('vms', item);
        detailsBtn.addEventListener('click', () => openInspector('vms', item, detailsBtn));
        const consoleBtn = node('button', 'btn-row-action btn-console', '콘솔'); consoleBtn.type = 'button';
        consoleBtn.disabled = !consoleEligible(item);
        consoleBtn.title = consoleEligible(item) ? item.name + ' 실제 게스트 콘솔 열기' : state.vms.error ? '목록 갱신 실패 · 최신 실행 확인 필요' : !canControl() ? '--allow-control 필요' : item.status !== 'running' ? '실행 중인 VM에서만 콘솔을 열 수 있습니다' : text(item.access?.reason || '명시적 셸 / 실행 기능 없음');
        consoleBtn.setAttribute('aria-label', item.name + ' 콘솔 · ' + consoleBtn.title);
        consoleBtn.addEventListener('click', () => openConsole(item));
        const removeBtn = node('button', 'btn-row-action danger-button', '제거'); removeBtn.type = 'button';
        removeBtn.dataset.vmId = itemId('vms', item);
        removeBtn.disabled = actionBusy || !lifecycleEligible(item);
        removeBtn.title = removalReason(item) || '이름과 백엔드 식별자 확인 후 제거 · 기본: 루트+레이어 토폴로지 보존';
        removeBtn.setAttribute('aria-label', item.name + ' 제거 · ' + removeBtn.title);
        removeBtn.addEventListener('click', () => openRemoval(item, removeBtn));
        actions.append(detailsBtn, consoleBtn, removeBtn); cell(row, actions);
        actions.appendChild(node('small', 'cell-note removal-reason', removalReason(item) || '기본: 루트+레이어 토폴로지 보존'));
      } else if (key === 'volumes') {
        cell(row, item.kind, 'nowrap'); cell(row, badge(item.status)); cell(row, volumeSize(item), 'nowrap');
        name.appendChild(node('small', 'cell-note', '보존 정책 · ' + text(item.retention_policy)));
        cell(row, [...items(item.attachments).map(attachment => text(attachment.name) + (attachment.mount_path ? ' → ' + attachment.mount_path : '') + (attachment.read_only ? ' (ro)' : '')), ...referenceEntries(item)].join(', ') || '미연결 · 참조 없음');
        cell(row, item.backend, 'nowrap');
        const remove = node('button', 'btn-row-action danger-button', '영구 삭제'); remove.type = 'button';
        remove.dataset.diskKey = key; remove.dataset.resourceId = itemId(key, item);
        remove.disabled = actionBusy || !!diskDeleteReason(key, item); remove.title = diskDeleteReason(key, item) || '정확한 이름 확인 후 데이터 영구 삭제';
        remove.addEventListener('click', () => openDiskDeletion(key, item, remove)); cell(row, remove);
      } else if (key === 'artifacts') {
        cell(row, artifactKind(item.display_kind), 'nowrap'); cell(row, text(item.disk_format || item.media_type || (item.kind === 'oci-image' ? 'OCI 아카이브' : null)) + ' / ' + text(item.arch || item.platform));
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
    return [...counts].sort((a, b) => b[1] - a[1]).map(([status, count]) => (status === 'stale' ? '오래된 관찰' : statusText(status)) + ' ' + count).join(' · ') || '관찰된 VM 없음';
  }
  function renderOverview() {
    const content = $('overview-content');
    const focus = captureFocus(content);
    content.replaceChildren();
    const metrics = node('div', 'metrics');
    const notes = { vms: '호스트 런타임 관찰', volumes: '호스트 관찰 · 저장된 기록', artifacts: '로컬 저장소', networks: '호스트 관찰 · 저장된 구성', builds: '최근 기록' };
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
    host.appendChild(infoList([['운영체제', machine.system], ['아키텍처', machine.machine], ['관찰 범위', '호스트 백엔드 발견 · 선택한 상태의 관리 기록'], ['제어 모드', canControl() ? '명시적 콘솔 · 확인된 호스트 VM 제거 · 선택한 관리 VM 시작·중지' : '관찰 전용']]));
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
    observations.appendChild(node('p', 'muted', '관리 기록과 호스트 발견을 함께 표시합니다. 로그·자원은 명시적 관찰 기능으로 확인하며 콘솔은 --allow-control이 필요합니다. 외부 VM도 이름·백엔드 식별자 확인 후 제거할 수 있고 시작·중지는 관리 VM만 가능합니다. 디스크의 가상 용량·파일 크기·할당량과 게스트 파일시스템 사용량은 다른 값입니다.'));
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
    $('access-mode').title = enabled ? '--allow-control · 기능별 VM 콘솔 허용 · 외부 VM도 이름·백엔드 식별자 확인 후 제거 · 시작/중지는 관리 VM만 허용' : '관찰 전용 · 콘솔 및 변경 작업 비활성';
    $('access-mode').className = 'badge' + (enabled ? ' warn' : '');
    $('control-guidance').hidden = enabled;
    for (const id of ['import-controls', 'storage-controls', 'vm-run-controls', 'volume-create-controls', 'build-controls', 'remote-upload-controls']) {
      const el = $(id);
      if (el) el.hidden = !enabled;
    }
    for (const control of document.querySelectorAll('.management input, .management select, .management textarea, .management button')) {
      if (control.closest('#detail-actions')) continue;
      control.disabled = !enabled || actionBusy;
    }
    updateVolumeForms();
    for (const control of document.querySelectorAll('#form-run-vm input, #form-run-vm select, #form-run-vm textarea, #form-run-vm button')) control.disabled ||= !freshForMutation('vms', 'volumes', 'artifacts');
    for (const button of document.querySelectorAll('#vms-table-content .danger-button')) {
      const item = collection('vms')?.find(vm => itemId('vms', vm) === button.dataset.vmId);
      button.disabled = actionBusy || !item || !lifecycleEligible(item);
      if (item) button.title = removalReason(item) || '루트+레이어 토폴로지 보존 (기본)';
    }
    for (const button of document.querySelectorAll('[data-disk-key]')) {
      const item = collection(button.dataset.diskKey)?.find(row => itemId(button.dataset.diskKey, row) === button.dataset.resourceId);
      const reason = item ? diskDeleteReason(button.dataset.diskKey, item) : '리소스 식별자를 확인할 수 없습니다.';
      button.disabled = actionBusy || !!reason; button.title = reason || '정확한 이름 확인 후 영구 삭제';
    }
    const session = activeConsoleSession;
    const consoleEnabled = canControlConsole();
    if (activeConsoleTarget && !consoleEnabled && !activeConsoleTarget.invalid) {
      activeConsoleTarget.invalid = true;
      closeConsoleSession();
      updateConsoleBadge('접속 중단', 'bad');
      notice($('console-capability-info'), '실행 교체, 상태 변경 또는 콘솔 기능 상실로 입력을 차단했습니다. 최신 목록에서 다시 여세요.', true);
    }
    for (const id of ['btn-console-exec-run', 'console-exec-argv']) $(id).disabled = !consoleEnabled;
    for (const id of ['btn-console-sigint', 'btn-console-sigterm', 'btn-console-sighup']) $(id).disabled = !consoleEnabled || !session?.active || session.exited || !session.capabilities.signal;
    if (activeConsoleTerm) activeConsoleTerm.options.disableStdin = !consoleEnabled || !session?.active || session.exited || !session.capabilities.stdin;
    const host = object(state.summary.data?.host);
    const isLinuxAmd64 = (host.system || '').toLowerCase() === 'linux' && ['x86_64', 'amd64'].includes((host.machine || '').toLowerCase());
    const ociWarn = $('host-oci-warning');
    if (ociWarn) {
      if (host.system && !isLinuxAmd64) {
        notice(ociWarn, `호스트 제약 알림: 현재 호스트(${text(host.system)} ${text(host.machine)})는 Linux amd64 KVM이 아닙니다. OCI 루트(oci-root) VM 실행은 지원되지 않으며 Docker 컨테이너로 대체 실행하지 않습니다. 일반 클라우드 이미지(cloud-image) VM을 사용하세요.`, false);
      } else {
        notice(ociWarn, '');
      }
    }
    if (inspector) { renderActions(); renderDetailTabs(); if (authFailed) renderResources(); }
    updateRemovalControls();
    updateDiskDeletionControls();
    updateRemoteControls();
  }
  function updateHeader() {
    document.documentElement.dataset.activityPaused = String(paused || authFailed || document.hidden || !!state.operations.error);
    $('btn-refresh').disabled = !!refreshPromise || actionBusy;
    $('btn-pause').disabled = false;
    $('btn-pause').textContent = paused ? '자동 갱신 재개' : '자동 갱신 일시 정지';
    $('btn-pause').setAttribute('aria-pressed', String(paused));
    $('last-success').textContent = lastSuccess ? date(lastSuccess) : '아직 없음';
    if (lastSuccess) $('last-success').dateTime = lastSuccess.toISOString();
    const failed = Object.keys(state).filter(key => state[key].error);
    $('refresh-state').textContent = authFailed ? '인증 필요 · 자동 갱신 중단' : refreshPromise ? '갱신 중…' : document.hidden ? '화면 숨김 · 자동 갱신 중단' : paused ? '자동 갱신 일시 정지' : failed.length ? '갱신 실패 · 5초 후 재시도' : '5초마다 자동 갱신';
    notice($('global-error'), authFailed ? '인증이 만료되었습니다. CLI 링크를 다시 여세요. 기존 데이터는 오래된 관찰로 유지됩니다.' : failed.length ? '갱신 실패: ' + failed.map(key => key === 'summary' ? '호스트 요약' : key === 'operations' ? '비동기 작업' : views[key][0]).join(', ') + '. 마지막 성공 데이터를 유지합니다 · 오래된 데이터. 서버가 실행 중이면 지금 갱신으로 다시 시도하세요.' : '', true);
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
    if (activeView === 'vms' || activeView === 'builds') renderOperations(activeView);
    if (activeView === 'artifacts') { renderOperations('artifacts'); updateUploadSources(); }
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
    if (key === 'artifacts' && !remoteState.loaded && !remoteState.loading) loadRemoteConnections();
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
      if (!authFailed && Object.values(state).every(entry => entry.data !== null && !entry.error)) lastSuccess = new Date();
      if (state.operations.data?.operations) {
        const ops = array(state.operations.data.operations);
        const retained = new Set(ops.map(op => op.id));
        for (const id of completedOpIds) if (!retained.has(id)) completedOpIds.delete(id);
        let newlyCompleted = false;
        for (const op of ops) {
          if (op && op.id && op.status === 'succeeded' && !completedOpIds.has(op.id)) {
            completedOpIds.add(op.id);
            newlyCompleted = true;
          }
        }
        for (const op of ops) {
          if (!pendingRemoteRuns.has(op.id) || op.status === 'running') continue;
          pendingRemoteRuns.delete(op.id);
          const receipt = transferReceipt(op);
          if (op.status === 'succeeded' && receipt?.run_supported === true) setupRunFromReceipt(receipt);
          else feedback('다운로드 후 실행 설정 불가 · ' + text(receipt?.run_limitation || op.error || '검증된 실행 영수증이 없습니다'), true);
        }
        if (newlyCompleted && !authFailed) {
          await Promise.all(['vms', 'volumes', 'builds', 'artifacts'].map(async k => {
            try {
              const d = await api(endpoints[k]);
              validate(k, d);
              state[k] = { data: d, error: null, updated: new Date() };
            } catch (_) {}
          }));
        }
      }
      render();
      if (inspector && !authFailed) { loadDetail(); await Promise.all([inspector?.logsOpened ? loadLogs() : Promise.resolve(), loadMetrics()]); }
    })().finally(() => { refreshPromise = null; updateHeader(); schedule(); });
    updateHeader();
    return refreshPromise;
  }

  const descriptions = {
    vms: '호스트 VM의 실제 로그와 자원을 명시적 관찰 기능으로 확인합니다. 콘솔은 --allow-control과 셸/실행 기능이 필요합니다. 외부 VM도 이름·백엔드 식별자 확인 후 제거할 수 있고 시작·중지는 관리 VM만 가능합니다.',
    volumes: '저장된 볼륨 기록과 호스트 디스크 관찰입니다. 가상 용량, 논리 파일 크기와 호스트 할당량은 서로 다른 값입니다. 연결 구성은 게스트 파일시스템 검증이 아닙니다.',
    networks: '저장된 네트워크 구성과 호스트 백엔드 관찰입니다. 백엔드 활성 상태는 게스트 연결, 리스너 또는 트래픽 검증이 아닙니다.',
    artifacts: '로컬 CAS 메타데이터, 로컬 빌드 기록과 파일 관찰입니다. Hub 연결 없이 표시하며 파일 관찰은 다이제스트 검증이나 실행 가능성을 보장하지 않습니다.',
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
      target.appendChild(infoList([['관리 범위', vmScope(data)], ['출처', resourceSource(data)], ['리소스 ID', itemId(key, data)]]));
      target.appendChild(node('p', 'notice', removalReason(data) || '제거 기본값: 루트와 순서 있는 레이어 토폴로지 보존. 독립 데이터 볼륨은 항상 보존합니다.'));
      const layers = node('section', 'detail-subsection'); layers.appendChild(node('h3', '', '레이어'));
      if (!array(data.layers).length) layers.appendChild(node('p', 'muted', Array.isArray(data.layers) ? '연결된 레이어 없음' : '레이어 정보 없음'));
      for (const layer of items(data.layers)) layers.appendChild(infoList([['다이제스트', layer.digest], ['대상 장치', layer.target_dev]]));
      target.appendChild(layers);
      const volumes = node('section', 'detail-subsection'); volumes.appendChild(node('h3', '', '볼륨 연결 선언'));
      if (!array(data.volumes).length) volumes.appendChild(node('p', 'muted', Array.isArray(data.volumes) ? '기록된 볼륨 연결 없음' : '볼륨 연결 정보 없음'));
      for (const volume of items(data.volumes)) volumes.appendChild(infoList([['이름', volume.name], ['마운트 경로', volume.mount_path], ['읽기 전용', volume.read_only], ['파일시스템 선언', volume.filesystem], ['대상 장치', volume.target_dev]]));
      target.appendChild(volumes); renderPorts(data.ports, target);
    } else if (key === 'volumes') {
      target.appendChild(infoList([['ID', data.id], ['이름', data.name], ['유형', data.kind], ['프로젝트', data.project], ['백엔드', data.backend], ['관찰 / 기록 상태', statusText(data.status)], ['가상 용량 (기록 / 백엔드 보고)', bytes(data.size_bytes)], ...('file_size_bytes' in data ? [['논리 파일 크기 (가상 용량 아님)', bytes(data.file_size_bytes)]] : []), ['보존 정책', data.retention_policy], ['출처', resourceSource(data)]]));
      if ('allocated_bytes' in data) target.appendChild(infoList([['호스트 할당량 (가상 용량 / 파일 크기와 별도)', bytes(data.allocated_bytes)]]));
      if (!array(data.attachments).length) target.appendChild(node('p', 'muted', Array.isArray(data.attachments) ? '연결 구성 없음 · 보존/미연결 기록' : '연결 정보 없음'));
      for (const attachment of items(data.attachments)) target.appendChild(detailSection('연결 구성 · ' + text(attachment.name), [['VM 이름', attachment.name], ['실행 ID', attachment.run_id], ['마운트 경로', attachment.mount_path], ['읽기 전용', attachment.read_only]]));
      target.appendChild(detailSection('보존 루트 · 의존성', [['인스턴스', data.instance_name || data.name], ['실행 ID', data.run_id], ['기본 이미지', data.base_digest], ['루트 볼륨', data.root_volume], ['토폴로지 보존 방식', data.storage_policy], ['참조', references(data)], ['삭제 제한', diskDeleteReason(key, data) || '미연결 · 이름 확인 후 삭제 가능']]));
      if (data.kind === 'retained-instance') {
        target.appendChild(infoList([['네이티브 인스턴스 식별자', data.instance_identity], ['보존 형태', data.reference_only === true ? '원래 디스크 참조 + 연결 기록 (이식 가능한 복사본 아님)' : '인스턴스 루트 + 연결 기록 보존']]));
        if (data.reference_only === true) target.appendChild(node('p', 'notice', '원래 디스크의 소유권을 인수하지 않았습니다. 보존 기록은 이동 가능한 루트 복사본을 뜻하지 않으며, 원래 디스크를 웹에서 영구 삭제하지 않습니다.'));
      }
      const topology = object(data.topology);
      if (Object.keys(topology).length) target.appendChild(detailSection('연결 토폴로지', Object.entries(topology).map(([name, value]) => [name, typeof value === 'object' ? JSON.stringify(value, null, 2) : value])));
      const layers = node('section', 'detail-subsection'); layers.appendChild(node('h3', '', '레이어 순서 · 루트부터'));
      if (!Array.isArray(data.layers)) layers.appendChild(node('p', 'muted', '레이어 연결 정보 없음'));
      for (const [index, layer] of array(data.layers).entries()) layers.appendChild(infoList([['순서', index + 1], ['다이제스트', typeof layer === 'string' ? layer : layer.digest], ['대상 장치', typeof layer === 'object' ? layer.target_dev : null]]));
      target.appendChild(layers);
    } else if (key === 'networks') {
      target.appendChild(infoList([['ID', data.id], ['이름', data.name], ['유형', data.kind], ['백엔드', data.backend], ['관찰 / 구성 상태', statusText(data.status)], ['모드', data.mode], ['서브넷', data.subnet], ['게이트웨이', data.gateway], ['외부 네트워크 선언', data.external], ['출처', resourceSource(data)]]));
      if (data.source === 'host-backend') target.appendChild(node('p', 'notice', '호스트 백엔드에서 관찰한 네트워크입니다. 선택한 Palimpsest 상태에만 한정된 목록이 아닙니다.'));
      if (data.mode === 'none') target.appendChild(node('p', 'notice', 'none 모드 · NIC 없는 격리 구성'));
      if (!array(data.attachments).length) target.appendChild(node('p', 'muted', Array.isArray(data.attachments) ? '연결 구성 없음' : '연결 정보 없음'));
      for (const attachment of items(data.attachments)) { const section = detailSection('연결 구성 · ' + text(attachment.name), [['VM 이름', attachment.name], ['실행 ID', attachment.run_id], ['기록된 게스트 주소', attachment.guest_ip]]); renderPorts(attachment.ports, section); target.appendChild(section); }
    } else if (key === 'artifacts') {
      target.appendChild(infoList([['다이제스트', data.digest], ['다이제스트 대상', digestType(data.digest_type || 'cas')], ['분류', artifactKind(data.display_kind)], ['부팅 이미지 여부', data.display_kind === 'image' ? '부팅 가능 이미지 (VM 실행 지원)' : data.display_kind === 'layer' ? 'SquashFS 레이어 (단독 부팅 불가)' : '기타 아티팩트'], ['저장소 유형', data.kind], ['이름', data.name], ['로컬 CAS 태그', tags(data.tags)], ['출처', data.source === 'build-record' ? '로컬 빌드 기록' : data.source === 'content-store' ? '로컬 콘텐츠 저장소' : data.source], ['파일 관찰', artifactAvailability(data)], ['로컬 CAS 변경 지원', data.editable === true ? '지원 · --allow-control 필요' : '지원하지 않음'], ['빌드 출력 태그 (CAS 태그 아님)', tags(data.output_tags)], ['연결된 빌드 ID', joined(data.build_ids)], ['크기 (OCI 이미지는 아카이브 크기)', bytes(data.size_bytes)], ['아키텍처', data.arch], ['플랫폼', data.platform], ['디스크 형식', data.disk_format], ['미디어 유형', data.media_type], ['OCI manifest 다이제스트', data.output_oci_manifest_digest], ['OCI 아카이브 다이제스트', data.output_oci_archive_digest], ['OCI 아카이브 크기', bytes(data.output_oci_archive_size_bytes)], ['VM 런타임 블록 다이제스트', data.runtime_block_digest], ['부모 다이제스트', data.parent_digest], ['생성 시각', date(data.created_at)], ['VM 참조', joined(object(data.referenced_by).runs)], ['프로젝트 참조', joined(object(data.referenced_by).projects)]]));
      const compatibility = object(data.compatibility);
      target.appendChild(detailSection('클라이언트 플랫폼 호환성', [['상태', ({ compatible: '호환', incompatible: '비호환', unknown: '확인 불가' })[compatibility.status] || '확인 불가'], ['클라이언트', [compatibility.client?.system, compatibility.client?.arch, compatibility.client?.backend].filter(Boolean).join(' / ')], ['이유', array(compatibility.reasons).join(' · ') || '선언된 대상 아키텍처 및 의존 체인 일치']]));
      target.appendChild(node('p', 'notice', '플랫폼 메타데이터 검사입니다. GPU·Vulkan·CUDA, 게스트 ABI 및 실제 부팅 성공을 보장하지 않습니다. 실행 시 런타임 사전 검증은 별도로 적용됩니다.'));
      if (data.kind === 'oci-image' || items(data.builds).length) target.appendChild(node('p', 'notice', 'OCI manifest는 이미지 내용의 식별자, OCI 아카이브는 내보낸 파일 바이트의 식별자입니다. VM 런타임 블록은 별도의 변환 결과입니다. 파일 관찰은 다이제스트 검증이나 실행 가능성을 보장하지 않습니다. 빌드 출력 태그는 로컬 CAS 태그나 Hub 게시 여부를 보장하지 않습니다.'));
      target.appendChild(detailSection('볼륨 / 루트 의존성', [['참조', references(data)], ['삭제 제한', diskDeleteReason(key, data) || '참조 없음 · 서버가 삭제 시 다시 검증합니다.']]));
      for (const build of items(data.builds)) target.appendChild(detailSection('로컬 빌드 출처 · ' + text(build.build_id), buildFields(build)));
    } else {
      target.appendChild(infoList(buildFields(data)));
      target.appendChild(node('p', 'muted', '출력 다이제스트, OCI manifest, 내보낸 아카이브와 VM 런타임 블록은 대상이 다릅니다. 기록된 다이제스트만 표시하며 파일 가용성이나 Hub 게시를 추정하지 않습니다.'));
      const timings = Object.entries(object(data.timings_ms));
      const section = node('section', 'detail-subsection'); section.appendChild(node('h3', '', '기록된 단계별 소요 시간'));
      section.appendChild(timings.length ? infoList(timings.map(([name, value]) => [name, duration(value)])) : node('p', 'muted', '단계별 시간 정보 없음'));
      target.appendChild(section);
    }
    renderDetailTabs();
    renderResources();
    const source = state[key];
    const warnings = [];
    if (source.error) warnings.push('목록 갱신 실패 · 마지막 성공 데이터 표시');
    if (key === 'vms' && data.stale === true) warnings.push('오래된 VM 런타임 관찰');
    if (inspector.error) warnings.push(inspector.error + ' · 마지막 상세 정보 유지');
    notice($('detail-state'), warnings.join(' / '), !!inspector.error);
    renderActions();
  }
  function renderDetailTabs() {
    if (!inspector) return;
    const vm = inspector.key === 'vms';
    $('vm-detail-tabs').hidden = !vm; $('vm-resource-summary').hidden = !vm;
    for (const [id, label] of [['log-section', 'tab-logs'], ['detail-content', 'tab-details']]) {
      if (vm) { $(id).setAttribute('role', 'tabpanel'); $(id).setAttribute('aria-labelledby', label); }
      else { $(id).removeAttribute('role'); if (id === 'log-section') $(id).setAttribute('aria-labelledby', 'log-title'); else $(id).removeAttribute('aria-labelledby'); }
    }
    $('detail-content').hidden = vm && inspector.tab !== 'details';
    $('log-section').hidden = !inspector.logsOpened || (vm && inspector.tab !== 'logs');
    $('resource-section').hidden = !vm || inspector.tab !== 'resources';
    $('terminal-section').hidden = !vm || inspector.tab !== 'terminal';
    for (const tab of document.querySelectorAll('[data-detail-tab]')) {
      const selected = tab.dataset.detailTab === inspector.tab;
      tab.setAttribute('aria-selected', String(selected)); tab.tabIndex = selected ? 0 : -1;
    }
    if (vm) {
      const enabled = !inspector.error && consoleEligible(inspector.data);
      $('btn-detail-console').disabled = !enabled;
      $('terminal-state').textContent = enabled ? '실제 게스트 ' + (consoleMode(inspector.data) === 'shell' ? 'PTY 셸' : '명령 실행') + ' 지원 · 접속은 명시적으로 요청합니다.' : state.vms.error ? '목록 갱신 실패 · 최신 실행 확인 전에는 콘솔 입력이 비활성화됩니다.' : !canControl() ? '콘솔은 --allow-control에서만 허용됩니다.' : inspector.data.status !== 'running' ? 'VM이 실행 중이 아닙니다. 콘솔 입력이 비활성화됩니다.' : '콘솔 미지원 · ' + text(inspector.error || inspector.data.access?.reason || '명시적 셸 / 실행 기능 없음');
    }
  }
  function selectDetailTab(tabName) {
    if (inspector?.key !== 'vms') return;
    inspector.tab = tabName; renderDetailTabs();
    if (tabName === 'logs') loadLogs();
  }
  const detailTabs = [...document.querySelectorAll('[data-detail-tab]')];
  for (const [index, tab] of detailTabs.entries()) {
    tab.addEventListener('click', () => selectDetailTab(tab.dataset.detailTab));
    tab.addEventListener('keydown', event => {
      let next;
      if (event.key === 'ArrowRight') next = (index + 1) % detailTabs.length;
      else if (event.key === 'ArrowLeft') next = (index + detailTabs.length - 1) % detailTabs.length;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = detailTabs.length - 1;
      else return;
      event.preventDefault(); detailTabs[next].focus(); selectDetailTab(detailTabs[next].dataset.detailTab);
    });
  }
  $('btn-detail-console').addEventListener('click', () => { if (inspector?.key === 'vms' && !inspector.error) openConsole(inspector.data); });
  $('btn-refresh-metrics').addEventListener('click', loadMetrics);
  $('resource-disk-select').addEventListener('change', () => { if (inspector?.key === 'vms') { inspector.diskName = $('resource-disk-select').value; inspector.diskSelected = true; renderResources(); } });
  async function openInspector(key, item, opener) {
    returnFocus = opener;
    returnSelector = { key, id: itemId(key, item) };
    inspector?.abort.abort();
    inspector = { key, id: itemId(key, item), runId: item.run_id, data: item, error: null, tab: key === 'vms' ? 'logs' : 'details', logsOpened: key === 'vms' || key === 'builds', log: null, logTime: null, abort: new AbortController(), metrics: null, metricsError: null, metricsLoading: false, metricHistory: [], previousMetrics: null };
    $('log-section').hidden = !inspector.logsOpened;
    notice($('detail-feedback'), '');
    $('log-content').textContent = '로그를 불러오는 중입니다.'; $('log-state').textContent = '';
    renderDetail();
    $('detail-drawer').showModal(); $('btn-close-drawer').focus();
    loadDetail();
    await Promise.all([inspector?.logsOpened ? loadLogs() : Promise.resolve(), loadMetrics()]);
  }
  // Details are the list projections themselves; re-reading one item would repeat a live VM reconcile.
  function loadDetail() {
    if (!inspector) return;
    const current = inspector;
    const latest = collection(current.key)?.find(item => itemId(current.key, item) === current.id);
    if (latest) {
      if (current.key === 'vms' && latest.run_id !== current.runId) {
        current.abort.abort(); current.abort = new AbortController(); current.metricsRequest = null; current.metricsLoading = false;
        current.runId = latest.run_id; current.log = null; current.logTime = null;
        current.metrics = null; current.metricsError = null; resetMetricRates(current); current.metricHistory = []; current.diskName = null; current.diskSelected = false;
        $('log-content').textContent = '실행이 교체되었습니다. 새 실행의 로그를 불러오는 중입니다.';
      }
      if (current.key === 'vms' && ['metrics', 'logs'].some(capability => current.data.access?.[capability] === true && latest.access?.[capability] !== true)) {
        current.abort.abort(); current.abort = new AbortController(); current.metricsRequest = null; current.metricsLoading = false; resetMetricRates(current);
      }
      if (current.abort.signal.aborted) current.abort = new AbortController();
      current.data = latest; current.error = state[current.key].error;
      if (current.key === 'vms' && !metricsEligible(current)) resetMetricRates(current);
    } else { current.error = '현재 목록에서 이 리소스를 찾을 수 없습니다'; current.abort.abort(); resetMetricRates(current); }
    renderDetail();
  }
  function metricsEligible(current) {
    return !authFailed && !state.vms.error && !current.error && current.data.status === 'running' && current.data.stale !== true && current.data.access?.metrics === true;
  }
  function resetMetricRates(current) { current.previousMetrics = null; current.rates = null; current.deviceRates = null; }
  function metricNumber(value) { return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null; }
  function metricPercent(value) { const n = metricNumber(value); return n === null ? '미지원' : n.toFixed(1) + '%'; }
  function metricBytes(value) { return metricNumber(value) === null ? '미지원' : bytes(value); }
  function cpuUsage(current, previous, seconds) {
    if (!seconds || !current || !previous) return null;
    const total = metricNumber(current.total_ticks); const oldTotal = metricNumber(previous.total_ticks);
    const idle = metricNumber(current.idle_ticks); const oldIdle = metricNumber(previous.idle_ticks);
    if ([total, oldTotal, idle, oldIdle].includes(null) || total <= oldTotal || idle < oldIdle || idle - oldIdle > total - oldTotal) return null;
    return (1 - (idle - oldIdle) / (total - oldTotal)) * 100;
  }
  function counterTotal(rows, field) {
    if (!Array.isArray(rows) || !rows.length || rows.some(row => metricNumber(row[field]) === null)) return null;
    return rows.reduce((sum, row) => sum + row[field], 0);
  }
  function counterRate(rows, oldRows, identity, field, seconds) {
    if (!seconds || !Array.isArray(rows) || !rows.length || !Array.isArray(oldRows) || rows.length !== oldRows.length) return null;
    let total = 0;
    for (const row of rows) {
      const previous = oldRows.find(old => old[identity] === row[identity]);
      const next = metricNumber(row[field]); const old = metricNumber(previous?.[field]);
      if (!previous || next === null || old === null || next < old) return null;
      total += (next - old) / seconds;
    }
    return total;
  }
  async function loadMetrics() {
    const current = inspector;
    if (current?.key !== 'vms' || current.closing || current.metricsRequest) return;
    if (!metricsEligible(current)) {
      resetMetricRates(current); renderResources(); return;
    }
    const signal = current.abort.signal; const runId = current.runId;
    const request = {}; current.metricsRequest = request; current.metricsLoading = true; renderResources();
    try {
      const query = runId == null ? '' : '?expected_run_id=' + encodeURIComponent(runId);
      const data = await api(endpoints.vms + '/' + encodeURIComponent(current.id) + '/metrics' + query, { signal });
      if (signal.aborted || inspector !== current || runId !== current.runId || !metricsEligible(current)) return;
      if (data.status === 'unavailable') throw new Error(text(data.message || data.reason || '자원 관찰 미지원'));
      if (data.id !== current.id || (data.run_id ?? null) !== (runId ?? null)) throw new Error('자원 표본의 VM 실행 식별자가 변경되었습니다');
      const timestamp = Date.parse(data.measured_at);
      if (!Number.isFinite(timestamp)) throw new Error('자원 표본 시각이 올바르지 않습니다');
      const previous = current.previousMetrics;
      const wallGap = previous ? (timestamp - Date.parse(previous.measured_at)) / 1000 : 0;
      const uptime = metricNumber(data.uptime_seconds); const oldUptime = metricNumber(previous?.uptime_seconds);
      const elapsed = uptime !== null && oldUptime !== null ? uptime - oldUptime : 0;
      const seconds = !paused && !document.hidden && previous?.source === data.source && previous?.id === data.id && previous?.run_id === data.run_id && data.boot_id && previous?.boot_id === data.boot_id && elapsed > 0 && elapsed <= 15 && wallGap > 0 && wallGap <= 15 ? elapsed : null;
      const rates = {
        cpu: cpuUsage(data.cpu, previous?.cpu, seconds),
        rx: counterRate(data.interfaces, previous?.interfaces, 'name', 'rx_bytes', seconds),
        tx: counterRate(data.interfaces, previous?.interfaces, 'name', 'tx_bytes', seconds),
      };
      if (previous && previous.boot_id !== data.boot_id) current.metricHistory = [];
      current.deviceRates = {
        interfaces: items(data.interfaces).map(row => ({ name: row.name, rx: counterRate([row], previous?.interfaces?.filter(old => old.name === row.name), 'name', 'rx_bytes', seconds), tx: counterRate([row], previous?.interfaces?.filter(old => old.name === row.name), 'name', 'tx_bytes', seconds) })),
        blocks: items(data.block_devices).map(row => ({ name: row.name, read: counterRate([row], previous?.block_devices?.filter(old => old.name === row.name), 'name', 'read_bytes', seconds), write: counterRate([row], previous?.block_devices?.filter(old => old.name === row.name), 'name', 'write_bytes', seconds) }))
      };
      current.metrics = data; current.rates = rates; current.metricsError = null;
      current.previousMetrics = paused || document.hidden ? null : data;
      const used = metricNumber(data.memory?.used_bytes); const total = metricNumber(data.memory?.total_bytes);
      current.metricHistory.push({ timestamp, memory: used !== null && total > 0 ? used / total * 100 : null, diskRates: current.deviceRates.blocks, ...rates });
      if (current.metricHistory.length > 36) current.metricHistory.shift();
    } catch (error) {
      if (signal.aborted || inspector !== current || runId !== current.runId) return;
      current.metricsError = error.message; resetMetricRates(current);
    } finally {
      if (current.metricsRequest === request) { current.metricsRequest = null; current.metricsLoading = false; }
      if (inspector === current) renderResources();
    }
  }
  function metricChart(history, field, label, percentage = false) {
    const chart = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    chart.setAttribute('viewBox', '0 0 300 72'); chart.setAttribute('role', 'img'); chart.setAttribute('aria-label', label + ' · 실제 표본 ' + history.filter(sample => sample[field] !== null).length + '개');
    chart.classList.add('resource-chart');
    const values = history.map(sample => sample[field]).filter(value => value !== null);
    if (!values.length) return node('p', 'chart-empty', '아직 차트 표본 없음');
    const max = percentage ? Math.max(100, ...values) : Math.max(1, ...values);
    const start = history[0].timestamp; const end = history[history.length - 1].timestamp;
    let points = [];
    const flush = () => {
      if (!points.length) return;
      const line = document.createElementNS(chart.namespaceURI, 'polyline');
      line.setAttribute('points', points.join(' ')); chart.appendChild(line); points = [];
    };
    history.forEach((sample, index) => {
      if (sample[field] === null || (index && sample.timestamp - history[index - 1].timestamp > 15000)) flush();
      if (sample[field] === null) return;
      const x = end > start ? 4 + (sample.timestamp - start) / (end - start) * 292 : 4;
      const y = 68 - sample[field] / max * 60;
      points.push(x.toFixed(2) + ',' + y.toFixed(2));
      const dot = document.createElementNS(chart.namespaceURI, 'circle'); dot.setAttribute('cx', x); dot.setAttribute('cy', y); dot.setAttribute('r', '2'); chart.appendChild(dot);
    });
    flush();
    const wrap = node('div', 'chart-frame'); wrap.append(chart, node('span', 'chart-scale', '0 — ' + (percentage ? max.toFixed(0) + '%' : bytes(max) + '/s') + ' · 실제 표본 ' + values.length + '개'));
    return wrap;
  }
  function renderResources() {
    const current = inspector;
    if (current?.key !== 'vms' || !$('resource-content')) return;
    const target = $('resource-content'); target.replaceChildren();
    const supported = metricsEligible(current);
    if (!supported) resetMetricRates(current);
    const data = current.metrics;
    const diskNames = items(data?.block_devices).map(row => row.name);
    const diskSelect = $('resource-disk-select'); const diskSignature = JSON.stringify(diskNames);
    if (diskSelect.dataset.signature !== diskSignature) {
      diskSelect.replaceChildren(...(diskNames.length ? diskNames.map(name => new Option(name, name)) : [new Option('관찰 표본 없음', '')]));
      diskSelect.dataset.signature = diskSignature;
    }
    if (!current.diskSelected || !diskNames.includes(current.diskName)) {
      current.diskSelected = false;
      const rootDevice = items(data?.filesystems).find(fs => fs.mountpoint === '/')?.device;
      const exactRoot = diskNames.find(name => rootDevice === name || rootDevice === '/dev/' + name);
      current.diskName = exactRoot || diskNames[0] || '';
    }
    diskSelect.value = current.diskName; diskSelect.disabled = !diskNames.length;
    const observationError = current.error || state.vms.error || (authFailed ? '인증 필요' : null);
    const retained = !supported || !!current.metricsError || paused || document.hidden;
    const sampleLabel = data ? (retained ? '마지막 표본 · ' : '측정 시각 · ') + date(data.measured_at) + ' · ' + text(data.source) : '';
    const unavailableLabel = observationError ? '관찰 갱신 실패' : current.data.stale === true ? '오래된 관찰' : current.data.status !== 'running' ? '실행 중 아님' : '미지원';
    const stateText = (!supported ? '자원 ' + unavailableLabel + ' · ' + text(observationError || current.data.access?.message || current.data.access?.reason || '명시적 기능 없음') : current.metricsError ? '자원 갱신 실패 · ' + current.metricsError : current.metricsLoading ? '실제 자원 표본을 불러오는 중…' : !data ? '첫 표본 대기 중' : paused || document.hidden ? '자동 갱신 정지 · 재개 후 속도 기준 재설정' : '실제 표본 ' + current.metricHistory.length + '개') + (sampleLabel ? ' · ' + sampleLabel : '');
    $('resource-state').textContent = stateText;
    $('btn-refresh-metrics').disabled = !supported || current.metricsLoading;
    const cpuLabel = !supported ? unavailableLabel + (data ? ' · 마지막 표본만 유지' : '') : current.metricsError ? '갱신 실패 · 계산 안 함' : !data ? '표본 대기' : !data.cpu ? '미지원' : paused || document.hidden ? '갱신 정지' : current.rates?.cpu == null ? '연속 표본 대기' : metricPercent(current.rates.cpu);
    const memoryLabel = !data ? (!supported ? unavailableLabel : current.metricsError ? '갱신 실패' : '표본 대기') : (retained ? '마지막 표본 · ' : '') + metricBytes(data.memory?.used_bytes) + ' / ' + metricBytes(data.memory?.total_bytes);
    $('vm-resource-summary').replaceChildren(badge(current.data.status, current.data.stale === true || !!observationError), node('span', '', 'CPU ' + cpuLabel), node('span', '', '메모리 ' + memoryLabel), node('span', 'muted', sampleLabel), node('span', 'muted', vmScope(current.data)));
    if (!data) { target.appendChild(node('p', 'empty-state', stateText)); return; }
    for (const section of array(data.unavailable)) target.appendChild(node('p', 'notice', '관찰 미지원 · ' + text(section)));
    const grid = node('div', 'resource-grid');
    const addPanel = (title, fields, chartFields, history = current.metricHistory) => {
      const panel = node('section', 'resource-panel'); panel.append(node('h3', '', title), infoList(fields));
      for (const [field, label, percentage] of chartFields) { panel.append(node('p', 'chart-label', label), metricChart(history, field, label, percentage)); }
      grid.appendChild(panel);
    };
    addPanel('CPU · 부하' + (retained ? ' · 마지막 표본' : ''), [['실제 CPU 사용률', cpuLabel], ['1분 평균 부하', metricNumber(data.load?.one) === null ? '미지원' : data.load.one.toFixed(2)], ['5분 / 15분 부하', data.load ? data.load.five.toFixed(2) + ' / ' + data.load.fifteen.toFixed(2) : '미지원']], [['cpu', 'CPU 사용률', true]]);
    const memory = object(data.memory);
    addPanel('메모리' + (retained ? ' · 마지막 표본' : ''), [['사용', metricBytes(memory.used_bytes)], ['사용 가능', metricBytes(memory.available_bytes)], ['전체', metricBytes(memory.total_bytes)]], [['memory', '메모리 사용률', true]]);
    const rate = value => !supported ? unavailableLabel + ' · 계산 안 함' : current.metricsError ? '갱신 실패 · 계산 안 함' : paused || document.hidden ? '갱신 정지 · 계산 안 함' : value == null ? '연속 표본 대기 / 기준 재설정' : bytes(value) + '/s';
    addPanel('네트워크 I/O · 인터페이스 합계', [['누적 수신 RX', metricBytes(counterTotal(data.interfaces, 'rx_bytes'))], ['누적 송신 TX', metricBytes(counterTotal(data.interfaces, 'tx_bytes'))], ['수신 속도', data.interfaces ? rate(current.rates?.rx) : '미지원'], ['송신 속도', data.interfaces ? rate(current.rates?.tx) : '미지원']], [['rx', '수신 RX · bytes/s'], ['tx', '송신 TX · bytes/s']]);
    const disk = items(data.block_devices).find(row => row.name === current.diskName);
    const diskRates = current.deviceRates?.blocks.find(row => row.name === current.diskName);
    const diskHistory = current.metricHistory.map(sample => ({ timestamp: sample.timestamp, read: sample.diskRates.find(row => row.name === current.diskName)?.read ?? null, write: sample.diskRates.find(row => row.name === current.diskName)?.write ?? null }));
    addPanel('디스크 I/O · ' + (current.diskName || '미지원'), [['누적 읽기', metricBytes(disk?.read_bytes)], ['누적 쓰기', metricBytes(disk?.write_bytes)], ['읽기 속도', disk ? rate(diskRates?.read) : '미지원'], ['쓰기 속도', disk ? rate(diskRates?.write) : '미지원']], [['read', current.diskName + ' 읽기 · bytes/s'], ['write', current.diskName + ' 쓰기 · bytes/s']], diskHistory);
    target.appendChild(grid);
    const filesystems = node('section', 'resource-panel filesystem-panel'); filesystems.appendChild(node('h3', '', '파일시스템 · 실제 사용 / 여유'));
    if (!items(data.filesystems).length) filesystems.appendChild(node('p', 'muted', '파일시스템 관찰 미지원 / 표본 없음'));
    for (const fs of items(data.filesystems)) filesystems.appendChild(infoList([['마운트', fs.mountpoint], ['사용', metricBytes(fs.used_bytes)], ['여유', metricBytes(fs.free_bytes)], ['전체', metricBytes(fs.total_bytes)]]));
    target.appendChild(filesystems);
    for (const [rows, rates, label, fields] of [[data.interfaces, current.deviceRates?.interfaces, '인터페이스', [['rx_bytes', 'rx', 'RX'], ['tx_bytes', 'tx', 'TX']]], [data.block_devices, current.deviceRates?.blocks, '장치', [['read_bytes', 'read', '읽기'], ['write_bytes', 'write', '쓰기']]]]) {
      const section = node('section', 'resource-panel filesystem-panel'); section.appendChild(node('h3', '', label + '별 누적 / 속도'));
      if (!items(rows).length) section.appendChild(node('p', 'muted', '관찰 미지원 / 표본 없음'));
      for (const row of items(rows)) {
        const rowRates = rates?.find(item => item.name === row.name);
        section.appendChild(infoList([[label, row.name], ...fields.map(([counter, rateKey, title]) => [title + ' 누적 / 속도', metricBytes(row[counter]) + ' / ' + rate(rowRates?.[rateKey])])]));
      }
      target.appendChild(section);
    }
    target.appendChild(node('p', 'muted', 'CPU·속도는 같은 실행·부팅·장치의 연속 표본 차이입니다. 첫 표본, 카운터 감소, 장치 변경, 15초 이상 공백에서는 기준을 재설정합니다. 디스크는 선택한 장치만 표시하며 파티션과 상위 장치를 합산하지 않습니다. 네트워크 합계는 루프백을 포함한 보고된 모든 인터페이스입니다.'));
  }
  function loadLogs() {
    if (!inspector?.logsOpened || inspector.closing) return Promise.resolve();
    if (inspector.key === 'vms' && (inspector.error || inspector.data.access?.logs !== true)) {
      $('log-state').textContent = '로그 관찰 미지원 · ' + text(inspector.error || inspector.data.access?.message || inspector.data.access?.reason || '명시적 로그 기능 없음');
      $('log-content').textContent = inspector.log || '이 VM의 로그를 관찰할 수 없습니다.';
      return Promise.resolve();
    }
    const current = inspector; const tail = $('log-tail-select').value;
    const signal = current.abort.signal; const runId = current.runId;
    if (logPromise) return logOwner === current ? logPromise : logPromise.then(() => inspector === current ? loadLogs() : undefined);
    logOwner = current;
    $('btn-refresh-log').disabled = true; $('log-tail-select').disabled = true;
    $('log-state').textContent = '최근 ' + tail + '줄을 불러오는 중…';
    logPromise = (async () => {
      try {
        const query = current.key === 'vms' && runId != null ? '&expected_run_id=' + encodeURIComponent(runId) : '';
        const path = endpoints[current.key] + '/' + encodeURIComponent(current.id) + (current.key === 'vms' ? '/logs' : '/log') + '?tail=' + encodeURIComponent(tail) + query;
        const result = await api(path, { signal });
        if (signal.aborted || runId !== current.runId) return;
        if (typeof result.log !== 'string') throw new Error('로그 응답 형식이 올바르지 않습니다');
        if (inspector !== current) return;
        if (result.status === 'unavailable') throw new Error('로그 관찰 미지원 · ' + text(result.message || result.reason));
        current.log = result.log; current.logTime = new Date();
        const log = $('log-content'); const nearBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 32;
        const next = result.log || '기록된 로그가 없습니다.';
        // Keep an unchanged log's text node so a selection survives auto-refresh.
        if (log.textContent !== next) log.textContent = next;
        if (nearBottom) log.scrollTop = log.scrollHeight;
        $('log-state').textContent = '최근 ' + tail + '줄 · ' + date(current.logTime) + ' 갱신' + (result.source ? ' · ' + result.source : '') + (result.truncated ? ' · 출력 한도에서 잘림' : '');
      } catch (error) {
        if (inspector !== current || signal.aborted || current.runId !== runId) return;
        $('log-state').textContent = error.message + (current.log !== null ? ' · 마지막 로그 유지 / 오래된 데이터 (' + date(current.logTime) + ')' : '');
        if (current.log === null) $('log-content').textContent = '로그를 불러오지 못했습니다. 로그 갱신으로 다시 시도하세요.';
        updateHeader();
      }
    })().finally(() => { logPromise = null; logOwner = null; $('btn-refresh-log').disabled = false; $('log-tail-select').disabled = false; });
    return logPromise;
  }
  function closeInspector() {
    const drawer = $('detail-drawer');
    if (!drawer.open || drawer.classList.contains('drawer-closing')) return;
    inspector?.abort.abort();
    if (inspector) inspector.closing = true;
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) { drawer.close(); return; }
    drawer.classList.add('drawer-closing');
    setTimeout(() => { drawer.close(); drawer.classList.remove('drawer-closing'); }, 160);
  }
  $('detail-drawer').addEventListener('cancel', event => { event.preventDefault(); closeInspector(); });
  $('detail-drawer').addEventListener('close', () => {
    inspector?.abort.abort();
    inspector = null;
    $('detail-actions').replaceChildren();
    if (afterInspectorClose) {
      const handoff = afterInspectorClose; afterInspectorClose = null;
      returnFocus = null; returnSelector = null;
      handoff(); return;
    }
    let focus = returnFocus?.isConnected && !returnFocus.closest('[hidden], [inert]') && returnFocus.getClientRects().length ? returnFocus : null;
    if (!focus && returnSelector) focus = [...document.querySelectorAll('[data-inspect]')].find(button => button.dataset.inspect === returnSelector.key && button.dataset.resourceId === returnSelector.id && !button.closest('[hidden]'));
    (focus || $('nav-' + activeView)).focus();
    returnFocus = null; returnSelector = null;
  });
  $('btn-close-drawer').addEventListener('click', closeInspector);
  $('detail-drawer').addEventListener('click', event => { if (event.target === $('detail-drawer')) { const rect = $('detail-drawer').getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right) closeInspector(); } });
  $('btn-refresh-log').addEventListener('click', loadLogs);
  $('log-tail-select').addEventListener('change', loadLogs);

  function canAct(current, action = 'remove') {
    if (!canControl() || inspector !== current || state[current.key]?.error || current.error) return false;
    if (current.data.stale === true) return false;
    if (current.key === 'artifacts') {
      if (current.data.display_kind === 'image' || current.data.display_kind === 'layer') return true;
      return current.data.editable === true && /^sha256:[0-9a-f]{64}$/.test(current.data.digest);
    }
    return true;
  }
  function renderActions() {
    const target = $('detail-actions'); const current = inspector;
    const isImageArtifact = current?.key === 'artifacts' && current?.data?.display_kind === 'image';
    const isLayerArtifact = current?.key === 'artifacts' && current?.data?.display_kind === 'layer';
    const isEditableArtifact = current?.key === 'artifacts' && current?.data?.editable === true;
    target.hidden = !current || !canControl() || !['vms', 'volumes', 'artifacts'].includes(current.key);
    if (target.hidden) { target.replaceChildren(); if (current) current.actionsSignature = null; return; }
    const verb = current.key === 'vms' && current.data.status === 'running' ? 'stop' : 'start';
    const signature = JSON.stringify([current.key, current.id, verb, isManagedVm(current.data), isImageArtifact, isLayerArtifact, isEditableArtifact]);
    // Polls update capability/busy state in place: never replace an active tag input.
    if (current.actionsSignature !== signature) {
      const focusAction = target.contains(document.activeElement) ? document.activeElement.dataset.action : null;
      target.replaceChildren();
      const group = node('div', 'action-group');
      if (current.key === 'vms') {
        if (isManagedVm(current.data)) {
          const lifecycle = node('button', '', verb === 'stop' ? 'VM 중지' : 'VM 시작'); lifecycle.dataset.action = verb;
          lifecycle.addEventListener('click', () => mutate('VM ' + (verb === 'stop' ? '중지' : '시작') + ': ' + current.data.name, endpoints.vms + '/' + encodeURIComponent(current.data.name) + '/' + verb, { method: 'POST', body: { expected_run_id: current.runId } }, false, () => canAct(current) && lifecycleEligible(current.data, verb)));
          group.appendChild(lifecycle);
        }
        const remove = node('button', 'danger-button', 'VM 제거'); remove.dataset.action = 'remove';
        remove.addEventListener('click', () => openRemoval(current.data, remove));
        group.appendChild(remove);
      } else if (current.key === 'volumes') {
        const remove = node('button', 'danger-button', '볼륨 / 보존 루트 영구 삭제'); remove.dataset.action = 'remove';
        remove.addEventListener('click', () => openDiskDeletion(current.key, current.data, remove)); group.appendChild(remove);
      } else {
        if (isImageArtifact) {
          const runBtn = node('button', 'primary', '이 이미지로 VM 실행');
          runBtn.type = 'button';
          runBtn.dataset.action = 'run-setup';
          runBtn.addEventListener('click', () => {
            afterInspectorClose = () => setupRunFromArtifact(current.data);
            closeInspector();
          });
          group.appendChild(runBtn);
        } else if (isLayerArtifact) {
          const layerNotice = node('p', 'notice', 'SquashFS 레이어는 단독 부팅 이미지가 아닙니다. 상위 OCI 이미지 또는 런타임 베이스 이미지에서만 부팅할 수 있습니다.');
          group.appendChild(layerNotice);
        }
        if (isEditableArtifact) {
          const form = node('form', 'tag-form'); form.id = 'form-artifact-tag';
          const label = node('label', '', '로컬 CAS 태그'); const input = node('input'); input.id = 'artifact-tag'; input.required = true; input.autocomplete = 'off'; input.dataset.action = 'tag-input';
          label.appendChild(input);
          const submit = node('button', 'primary', '태그 저장'); submit.type = 'submit'; submit.dataset.action = 'tag';
          form.append(label, submit);
          form.appendChild(node('p', 'muted', '이 다이제스트에 로컬 태그를 연결합니다. 기존 태그가 다른 다이제스트를 가리키면 변경하지 않고 오류를 표시합니다. Hub 태그는 변경하지 않습니다.'));
          form.addEventListener('submit', event => {
            event.preventDefault();
            const tag = input.value.trim();
            if (!tag) { input.setCustomValidity('태그를 입력하세요.'); input.reportValidity(); return; }
            mutate('로컬 CAS 태그 저장: ' + tag + ' → ' + current.data.digest, endpoints.artifacts + '/' + current.data.digest + '/tag', { method: 'POST', body: { tag } }, false, () => canAct(current));
          });
          input.addEventListener('input', () => input.setCustomValidity(''));
          target.appendChild(form);
        }
        if (isEditableArtifact || isImageArtifact || isLayerArtifact) {
          const remove = node('button', 'danger-button', '아티팩트 영구 삭제'); remove.dataset.action = 'remove';
          remove.addEventListener('click', () => openDiskDeletion(current.key, current.data, remove)); group.appendChild(remove);
        }
      }
      target.appendChild(group); current.actionsSignature = signature;
      if (focusAction) target.querySelector('[data-action="' + focusAction + '"]')?.focus({ preventScroll: true });
    }
    for (const control of target.querySelectorAll('input, button')) control.disabled = actionBusy || !canAct(current);
    if (current.key === 'vms') for (const button of target.querySelectorAll('button[data-action]')) {
      button.disabled ||= !lifecycleEligible(current.data, button.dataset.action);
      button.title = button.dataset.action === 'remove' ? removalReason(current.data) || '기본: 루트+레이어 토폴로지 보존' : current.data.lifecycle?.[button.dataset.action] === true ? '' : '이 상태에서는 ' + button.dataset.action + ' 기능이 없습니다.';
    }
    if (current.key !== 'vms') {
      const remove = target.querySelector('[data-action="remove"]');
      if (remove) { const reason = diskDeleteReason(current.key, current.data); remove.disabled ||= !!reason; remove.title = reason || '정확한 이름 확인 후 영구 삭제 · 서버가 참조를 다시 검사합니다.'; }
    }
    if (current.key === 'artifacts' && current.data.run_supported === false) target.querySelector('[data-action="run-setup"]')?.setAttribute('disabled', '');
  }
  async function mutate(label, path, options, closeOnSuccess = false, allowed = canControl) {
    if (!allowed() || actionBusy) return;
    if (!window.confirm(label + '\n이 작업은 로컬 리소스를 변경합니다. 계속하시겠습니까?')) return;
    actionBusy = true; clearTimeout(timer); updateControls(); updateHeader(); feedback('작업 중 · ' + label);
    try {
      if (refreshPromise) await refreshPromise;
      if (!allowed()) throw new Error('변경 권한이나 최신 리소스 정보를 확인할 수 없습니다. 지금 갱신 후 다시 시도하세요.');
      await api(path, options);
      if (closeOnSuccess && $('detail-drawer').open) closeInspector();
      feedback('완료 · ' + label);
      await refresh(true);
    } catch (error) {
      feedback('작업 실패 · ' + error.message, true);
    } finally { actionBusy = false; updateControls(); updateHeader(); schedule(); }
  }
  function diskConfirmationName(key, item) { return key === 'artifacts' ? item.digest : item.name; }
  function openDiskDeletion(key, item, trigger) {
    if (actionBusy || diskDeleteReason(key, item)) return;
    diskDeletion = { key, item: { ...item }, name: diskConfirmationName(key, item) }; diskDeletionReturnFocus = trigger;
    $('delete-disk-name').textContent = diskDeletion.name;
    $('delete-disk-description').textContent = key === 'artifacts' ? '아티팩트 바이트를 영구 삭제합니다. 연결된 VM·프로젝트·볼륨 / 보존 루트의 참조가 있으면 삭제할 수 없습니다. 서버 검사가 최종 판단입니다.' : item.kind === 'retained-instance' ? '보존 루트 디스크와 기본 이미지·순서 있는 레이어의 연결 기록을 함께 영구 삭제합니다. 이 데이터는 복구할 수 없습니다. 공유 이미지 / 레이어와 독립 데이터 볼륨은 삭제하지 않습니다.' : '이 독립 데이터 볼륨의 모든 데이터를 영구 삭제합니다. 복구할 수 없습니다. 연결 / 예약을 먼저 해제해야 합니다.';
    $('delete-disk-confirmation').value = ''; notice($('delete-disk-feedback'), '');
    $('delete-disk-dialog').showModal(); updateDiskDeletionControls(); $('delete-disk-confirmation').focus();
  }
  function updateDiskDeletionControls() {
    if (!$('delete-disk-dialog').open || !diskDeletion) return;
    const reason = diskDeleteReason(diskDeletion.key, diskDeletion.item);
    $('btn-delete-disk-submit').disabled = actionBusy || !!reason || $('delete-disk-confirmation').value !== diskDeletion.name;
    $('delete-disk-confirmation').disabled = actionBusy || !!reason;
    $('btn-delete-disk-cancel').disabled = actionBusy;
    if (reason && !actionBusy) notice($('delete-disk-feedback'), reason, true);
  }
  $('delete-disk-confirmation').addEventListener('input', updateDiskDeletionControls);
  $('btn-delete-disk-cancel').addEventListener('click', () => $('delete-disk-dialog').close());
  $('delete-disk-dialog').addEventListener('cancel', event => { if (actionBusy) event.preventDefault(); });
  $('delete-disk-dialog').addEventListener('close', () => {
    diskDeletion = null; if (diskDeletionReturnFocus?.isConnected) diskDeletionReturnFocus.focus(); diskDeletionReturnFocus = null;
  });
  $('form-delete-disk').addEventListener('submit', async event => {
    event.preventDefault(); const target = diskDeletion;
    if (actionBusy || !target || diskDeleteReason(target.key, target.item) || $('delete-disk-confirmation').value !== target.name) return;
    actionBusy = true; clearTimeout(timer); updateControls(); updateHeader();
    try {
      if (refreshPromise) await refreshPromise;
      const reason = diskDeleteReason(target.key, target.item); if (reason) throw new Error(reason);
      const id = target.key === 'artifacts' ? target.item.digest : itemId(target.key, target.item);
      const result = await api(endpoints[target.key] + '/' + encodeURIComponent(id), { method: 'DELETE', body: { confirmation: target.name } });
      $('delete-disk-dialog').close();
      if (inspector?.key === target.key && inspector.id === itemId(target.key, target.item)) closeInspector();
      feedback('영구 삭제 완료 · ' + target.name + (result.storage_policy ? ' · ' + result.storage_policy : ''));
      await refresh(true);
    } catch (error) { notice($('delete-disk-feedback'), '삭제 실패 · ' + error.message, true); }
    finally { actionBusy = false; updateControls(); updateHeader(); renderList(target.key, true); schedule(); }
  });
  function openRemoval(item, trigger) {
    if (actionBusy || !lifecycleEligible(item)) return;
    removalTarget = { ...item };
    removalReturnFocus = trigger;
    $('remove-vm-description').textContent = `${item.name} · ${item.backend} · ${isManagedVm(item) ? '실행 UUID ' + item.run_id : '호스트 식별자 ' + item.removal_identity}`;
    $('remove-vm-confirmation').value = '';
    $('remove-vm-files').checked = false;
    $('remove-vm-files').closest('label').hidden = false;
    $('remove-vm-warning').textContent = '기본: 루트+레이어 토폴로지 보존. 실행 중이면 VM을 종료하고 백엔드 정의를 제거합니다. 독립 데이터 볼륨은 어떤 선택에서도 보존합니다.';
    $('remove-vm-file-policy').textContent = '선택하지 않으면 루트와 기본 이미지·순서 있는 레이어 연결 기록을 함께 보존합니다. 선택하면 이 인스턴스가 소유한 루트·런타임 파일만 삭제합니다. 소유권을 증명할 수 없는 디스크는 삭제하지 않으며 서버가 안전하지 않은 요청을 거부합니다.';
    notice($('remove-vm-feedback'), '');
    $('remove-vm-dialog').showModal();
    updateRemovalControls();
    $('remove-vm-confirmation').focus();
  }
  function updateRemovalControls() {
    const dialog = $('remove-vm-dialog');
    if (!dialog.open || !removalTarget) return;
    const eligible = lifecycleEligible(removalTarget);
    $('btn-remove-vm-submit').disabled = actionBusy || !eligible || $('remove-vm-confirmation').value !== removalTarget.name;
    $('remove-vm-confirmation').disabled = actionBusy;
    const latest = collection('vms')?.find(item => itemId('vms', item) === itemId('vms', removalTarget));
    const canPurge = eligible && latest?.lifecycle?.remove_volumes === true;
    $('remove-vm-files').disabled = actionBusy || !canPurge;
    $('remove-vm-files').title = canPurge ? '독립 데이터 볼륨은 보존됩니다.' : latest?.lifecycle?.remove_volumes_reason || '이 VM의 루트 / 런타임 파일 소유권이 확인되지 않아 영구 삭제를 지원하지 않습니다.';
    if (!canPurge) $('remove-vm-files').checked = false;
    $('btn-remove-vm-cancel').disabled = actionBusy;
    if (!eligible && !actionBusy) notice($('remove-vm-feedback'), removalReason(removalTarget), true);
  }
  $('remove-vm-confirmation').addEventListener('input', updateRemovalControls);
  $('btn-remove-vm-cancel').addEventListener('click', () => $('remove-vm-dialog').close());
  $('remove-vm-dialog').addEventListener('cancel', event => { if (actionBusy) event.preventDefault(); });
  $('remove-vm-dialog').addEventListener('close', () => {
    removalTarget = null;
    if (removalReturnFocus?.isConnected) removalReturnFocus.focus();
    removalReturnFocus = null;
  });
  $('form-remove-vm').addEventListener('submit', async event => {
    event.preventDefault();
    const target = removalTarget;
    if (actionBusy || !target || !lifecycleEligible(target) || $('remove-vm-confirmation').value !== target.name) return;
    const latest = collection('vms')?.find(item => itemId('vms', item) === itemId('vms', target));
    if ($('remove-vm-files').checked && latest?.lifecycle?.remove_volumes !== true) return;
    const body = { expected_run_id: target.run_id, confirmation: $('remove-vm-confirmation').value, volumes: $('remove-vm-files').checked };
    if (!isManagedVm(target)) body.expected_removal_identity = target.removal_identity;
    actionBusy = true; clearTimeout(timer); updateControls(); updateHeader();
    notice($('remove-vm-feedback'), '선택한 VM의 백엔드 식별자 확인 후 제거 중…');
    try {
      if (refreshPromise) await refreshPromise;
      if (!lifecycleEligible(target)) throw new Error(removalReason(target));
      const latest = collection('vms')?.find(item => itemId('vms', item) === itemId('vms', target));
      if (body.volumes && latest?.lifecycle?.remove_volumes !== true) throw new Error('루트 / 런타임 파일 삭제 기능이 변경되었습니다. 다시 선택하세요.');
      const result = await api(endpoints.vms + '/' + encodeURIComponent(itemId('vms', target)), { method: 'DELETE', body });
      $('remove-vm-dialog').close();
      if (inspector?.key === 'vms' && inspector.id === itemId('vms', target)) closeInspector();
      const retained = object(result.retained_instance);
      const retainedName = typeof result.retained_instance === 'string' ? result.retained_instance : retained.name || retained.id;
      feedback(`VM 제거 완료 · ${target.name} · storage_policy: ${text(result.storage_policy)}${retainedName ? ' · 보존 루트: ' + retainedName : ''} · 독립 데이터 볼륨 보존`);
      await refresh(true);
    } catch (error) { notice($('remove-vm-feedback'), '제거 실패 · ' + error.message, true); }
    finally { actionBusy = false; updateControls(); updateHeader(); renderList('vms', true); schedule(); }
  });
  function decodeBase64ToBytes(b64) {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes;
  }

  function transferReceipt(op) {
    if (!['download', 'upload'].includes(op.kind) || op.status !== 'succeeded' || op.output_truncated) return null;
    try { const receipt = JSON.parse(String(op.output || '').trim().split('\n').pop()); return receipt && typeof receipt === 'object' && !Array.isArray(receipt) ? receipt : null; }
    catch (_) { return null; }
  }
  function updateRemoteBreadcrumb() {
    const repository = $('remote-repository').value.trim();
    $('remote-repository-breadcrumb').hidden = !repository;
    $('remote-repository-name').textContent = repository ? repository + ' · 태그 / 버전' : '';
  }
  function invalidateRemoteCatalog() {
    remoteState.generation++; remoteState.abort?.abort(); remoteState.searching = false; remoteState.result = null;
    closeRemoteDetail();
    $('remote-results').replaceChildren();
    $('remote-results-state').textContent = '연결을 선택하고 조회하세요. 원격 목록은 자동 갱신하지 않습니다.';
    $('btn-remote-next').hidden = true;
  }
  $('form-hub-connect').addEventListener('submit', async event => {
    event.preventDefault();
    if (hubState.connection || hubState.connecting || actionBusy || remoteState.loading || authFailed) return;
    const connection = { url: $('hub-url').value.trim(), token: $('hub-token').value.trim(), project: $('hub-project').value.trim() };
    $('hub-token').value = '';
    if (!connection.url || !connection.token) return;
    invalidateRemoteCatalog();
    const generation = ++hubState.generation;
    hubState.abort = new AbortController(); hubState.connecting = true;
    $('hub-connection-status').className = 'badge'; $('hub-connection-status').textContent = 'Hub 인증 확인 중…';
    updateRemoteControls();
    try {
      const identity = await api('/api/v1/remotes/hub/identity', { headers: hubHeaders(connection), signal: hubState.abort.signal });
      if (generation !== hubState.generation) return;
      if (!identity.user_id || !identity.project_id || !identity.capabilities) throw new Error('Hub 사용자·프로젝트 응답 형식이 올바르지 않습니다');
      hubState.connection = { ...connection, project: identity.project_id };
      const capabilities = Object.entries(object(identity.capabilities)).map(([key, allowed]) => key + ': ' + (allowed === true ? '허용' : '불가')).join(' · ');
      $('hub-connection-status').className = 'badge good';
      $('hub-connection-status').textContent = `연결됨 · 사용자 ${identity.user_id} · 프로젝트 ${identity.project_id} · ${capabilities}`;
      await loadRemoteConnections('legacy');
    } catch (error) {
      if (generation !== hubState.generation) return;
      $('hub-connection-status').className = 'badge bad';
      $('hub-connection-status').textContent = '연결 실패 · ' + error.message;
    } finally {
      if (generation === hubState.generation) { hubState.connecting = false; hubState.abort = null; updateRemoteControls(); }
    }
  });
  $('btn-hub-disconnect').addEventListener('click', () => {
    if (actionBusy || remoteState.loading) return;
    hubState.generation++; hubState.abort?.abort(); hubState.abort = null;
    hubState.connection = null; hubState.connecting = false;
    $('form-hub-connect').reset();
    $('hub-connection-status').className = 'badge';
    $('hub-connection-status').textContent = '탭 연결 해제됨 · 서버 설정 사용';
    selectRemoteConnection();
    loadRemoteConnections();
  });
  function updateRemoteControls() {
    const busy = actionBusy || remoteState.loading || remoteState.searching || hubState.connecting;
    for (const id of ['btn-remote-connections', 'remote-connection', 'remote-kind', 'remote-query', 'remote-repository', 'remote-arch', 'remote-disk-format', 'remote-parent', 'remote-compatible', 'btn-remote-search', 'btn-remote-back', 'btn-remote-catalog']) $(id).disabled = busy || authFailed;
    for (const button of $('remote-results').querySelectorAll('button')) button.disabled = busy || authFailed || (button.dataset.read !== 'true' && !canControl()) || button.dataset.supported !== 'true';
    $('btn-remote-next').disabled = busy || authFailed;
    for (const id of ['hub-url', 'hub-token', 'hub-project', 'btn-hub-connect']) $(id).disabled = busy || authFailed || !!hubState.connection;
    $('btn-hub-connect').hidden = !!hubState.connection;
    $('btn-hub-disconnect').hidden = !hubState.connection && !hubState.connecting;
    $('btn-hub-disconnect').disabled = actionBusy || remoteState.loading;
    const detail = remoteDetail.item;
    $('btn-remote-detail-download').disabled = busy || remoteDetail.loading || !canControl() || detail?.download_supported !== true;
    $('btn-remote-detail-run').disabled = $('btn-remote-detail-download').disabled || detail?.run_supported !== true;
    for (const button of $('remote-detail-content').querySelectorAll('.remote-platform')) button.disabled = busy || remoteDetail.loading || authFailed;
  }
  function selectRemoteConnection() {
    const selected = remoteState.connections.find(item => item.id === $('remote-connection').value);
    invalidateRemoteCatalog();
    $('remote-query').value = '';
    $('remote-repository').value = ''; remoteRepositoryQuery = ''; updateRemoteBreadcrumb();
    for (const id of ['remote-arch', 'remote-disk-format', 'remote-parent']) $(id).value = '';
    for (const id of ['remote-disk-filter', 'remote-parent-filter']) $(id).hidden = selected?.protocol !== 'legacy';
    if (!selected) { updateRemoteControls(); return; }
    const tabHub = selected.protocol === 'legacy' && hubState.connection;
    const instructions = selected.protocol === 'legacy' ? '원래 Keystone 토큰을 이 탭에 연결하거나 웹 프로세스의 Hub 환경 설정을 사용합니다. 프로젝트 ID는 원래 범위 확인용이며 재지정하지 않습니다.' : selected.protocol === 'palimpsest' ? '정확한 API base/namespace의 credential helper에 저장한 프로젝트 패키지 키를 사용합니다.' : 'Docker 인증 설정·helper를 사용합니다. Docker Hub 저장소 검색은 공개 검색이며 비공개 이미지는 정확한 참조로 조회하세요.';
    const credential = tabHub ? '이 탭의 인증 사용' : selected.credential_available === null ? '인증은 요청 시 helper로 확인' : selected.credential_available ? '인증 정보 설정됨' : '인증 정보 없음';
    notice($('remote-connection-feedback'), `${selected.label} · ${tabHub ? tabHub.url : selected.endpoint}${selected.namespace ? ' / ' + selected.namespace : ''} · ${selected.protocol} · ${credential}\n${instructions}\n표시된 기능은 프로토콜 지원이며 현재 사용자의 허가는 매 요청에서 확인합니다.`);
    const kinds = new Set(selected.kinds || []);
    for (const option of $('remote-kind').options) option.disabled = option.value !== 'all' && !kinds.has(option.value);
    $('remote-kind').value = 'all';
    updateRemoteControls();
    $('form-remote-search').requestSubmit();
  }
  async function loadRemoteConnections(preferred) {
    if (remoteState.loading || authFailed) return;
    remoteState.loading = true; updateRemoteControls();
    try {
      const data = await api('/api/v1/remotes');
      if (!Array.isArray(data.connections)) throw new Error('원격 연결 응답 형식이 올바르지 않습니다');
      remoteState.connections = data.connections; remoteState.loaded = true;
      const hub = data.connections.find(item => item.protocol === 'legacy');
      if (!hubState.connection && !$('hub-url').value.trim() && hub?.endpoint) {
        $('hub-url').value = hub.endpoint;
        $('hub-url').defaultValue = hub.endpoint;
      }
      const previous = typeof preferred === 'string' ? preferred : $('remote-connection').value;
      $('remote-connection').replaceChildren(...data.connections.map(item => new Option(item.label + ' · ' + item.protocol, item.id)));
      if (data.connections.some(item => item.id === previous)) $('remote-connection').value = previous;
      else {
        const initial = data.connections.find(item => item.protocol === 'legacy' && item.configured) ||
          data.connections.find(item => item.id === 'registry:docker') || data.connections[0];
        $('remote-connection').value = initial?.id || '';
      }
      selectRemoteConnection(); updateUploadSources();
    } catch (error) { notice($('remote-connection-feedback'), '연결 조회 실패 · ' + error.message, true); }
    finally { remoteState.loading = false; updateRemoteControls(); }
  }
  function updateUploadSources() {
    const connection = $('upload-connection'); const previousConnection = connection.value;
    const hubs = remoteState.connections.filter(item => ['legacy', 'palimpsest'].includes(item.protocol) && item.capabilities?.upload === true);
    const signature = JSON.stringify(hubs.map(item => [item.id, item.label, item.protocol]));
    if (connection.dataset.signature !== signature) {
      connection.replaceChildren(...hubs.map(item => new Option(item.label + ' · ' + item.protocol, item.id)));
      if (hubs.some(item => item.id === previousConnection)) connection.value = previousConnection;
      connection.dataset.signature = signature;
    }
    const choices = [];
    for (const build of collection('builds') || []) if (['success', 'succeeded'].includes(build.status)) choices.push({ label: '빌드 · ' + build.build_id + ' · ' + tags(build.output_tags), source: { kind: 'build', build_id: build.build_id } });
    for (const artifact of collection('artifacts') || []) if (artifact.upload_source || (artifact.digest && artifact.editable === true)) choices.push({ label: artifactKind(artifact.display_kind) + ' · ' + itemName('artifacts', artifact) + ' · ' + digest(artifact.digest), source: artifact.upload_source || { kind: 'local', reference: artifact.digest } });
    const source = $('upload-source'); const previousSource = source.value;
    const sourceSignature = JSON.stringify(choices);
    if (source.dataset.signature !== sourceSignature) {
      source.replaceChildren(...choices.map(item => new Option(item.label, JSON.stringify(item.source))));
      if ([...source.options].some(option => option.value === previousSource)) source.value = previousSource;
      source.dataset.signature = sourceSignature;
    }
    $('remote-upload-feedback').textContent = hubs.length ? choices.length ? 'OCI 빌드 → native Hub · 클라우드 이미지/SquashFS 다이제스트 → legacy Hub. 선택한 인증의 현재 권한을 서버에서 확인합니다.' : '선택한 상태에 업로드할 로컬 이미지·레이어 또는 성공한 빌드가 없습니다.' : 'Palimpsest Hub 연결이 없습니다. legacy Hub 환경 변수 또는 protocol=palimpsest 프로필을 설정하고 연결 다시 읽기를 요청하세요.';
  }
  function closeRemoteDetail() {
    remoteDetail.generation++; remoteDetail.abort?.abort(); remoteDetail.abort = null;
    remoteDetail.loading = false; remoteDetail.item = null; remoteDetail.connection = null; remoteDetail.reference = null;
    if ($('remote-detail-dialog').open) $('remote-detail-dialog').close();
    updateRemoteControls();
  }
  $('btn-remote-detail-close').addEventListener('click', closeRemoteDetail);
  $('remote-detail-dialog').addEventListener('close', () => {
    if ($('remote-detail-dialog').open) return;
    closeRemoteDetail();
    const target = remoteDetail.returnFocus;
    remoteDetail.returnFocus = null;
    if (activeView === 'artifacts') (target?.isConnected ? target : $('btn-remote-search')).focus({ preventScroll: true });
  });
  function remoteDigest(value) {
    const wrap = node('span', 'remote-digest'); wrap.appendChild(node('code', 'mono', text(value)));
    if (typeof value === 'string' && value) {
      const copy = node('button', 'btn-row-action', '복사'); copy.type = 'button'; copy.setAttribute('aria-label', '다이제스트 복사: ' + value);
      copy.addEventListener('click', async () => {
        const generation = remoteDetail.generation;
        try {
          await navigator.clipboard.writeText(value);
          if (generation === remoteDetail.generation) notice($('remote-detail-feedback'), '다이제스트를 복사했습니다.');
        } catch (_) {
          if (generation === remoteDetail.generation) notice($('remote-detail-feedback'), '클립보드에 접근할 수 없습니다. 표시된 다이제스트를 선택하여 복사하세요.', true);
        }
      });
      wrap.appendChild(copy);
    }
    return wrap;
  }
  function renderRemoteDetail(data) {
    const target = $('remote-detail-content'); target.replaceChildren();
    target.appendChild(infoList([
      ['참조', data.reference], ['고정 참조', data.immutable_reference], ['다이제스트', remoteDigest(data.digest)],
      ['분류', data.kind], ['미디어 형식', data.media_type], ['크기', bytes(data.size_bytes)],
      ['아키텍처', data.arch || joined(data.architectures)], ['디스크 형식', data.disk_format]
    ]));
    if ('chain_complete' in data || 'base_digest' in data || 'parent_digest' in data) {
      const section = detailSection('Hub 의존성 체인', [
        ['기본 이미지', remoteDigest(data.base_digest)], ['부모 레이어', remoteDigest(data.parent_digest)],
        ['chain_complete', badge(data.chain_complete === true ? 'success' : 'unknown')]
      ]);
      section.appendChild(node('p', 'muted', data.chain_complete === true ? '전체 의존성 체인 확인됨' : data.chain_complete === false ? '의존성 체인이 완전하지 않습니다.' : '체인 완전성 정보 없음'));
      const chain = node('ol', 'remote-chain');
      for (const dependency of items(data.chain)) {
        const entry = node('li');
        entry.append(node('strong', '', text(dependency.name || dependency.kind)), remoteDigest(dependency.digest), node('small', 'cell-note', [dependency.kind, dependency.media_type, dependency.arch, dependency.disk_format, bytes(dependency.size_bytes)].filter(Boolean).join(' · ')));
        chain.appendChild(entry);
      }
      section.appendChild(chain.childElementCount ? chain : node('p', 'muted', '의존성 항목이 없습니다.'));
      target.appendChild(section);
    }
    if (items(data.platforms).length) {
      const section = node('section', 'detail-subsection'); section.appendChild(node('h3', '', '플랫폼 / 아키텍처 선택'));
      const platforms = node('div', 'remote-platforms'); platforms.setAttribute('role', 'group'); platforms.setAttribute('aria-label', '인덱스 플랫폼');
      for (const platform of items(data.platforms)) {
        const button = node('button', 'remote-platform', [platform.os, platform.architecture, platform.variant].filter(Boolean).join('/') || '플랫폼 정보 없음'); button.type = 'button';
        button.setAttribute('aria-pressed', String(platform.digest === data.manifest_digest));
        button.appendChild(node('small', 'cell-note mono', text(platform.digest) + ' · ' + bytes(platform.size_bytes)));
        button.addEventListener('click', () => fetchRemoteDetail(platform.digest)); platforms.appendChild(button);
      }
      section.appendChild(platforms); target.appendChild(section);
    }
    if (data.config?.digest) target.appendChild(detailSection('이미지 구성', [
      ['구성 다이제스트', remoteDigest(data.config.digest)], ['미디어 형식', data.config.media_type], ['크기', bytes(data.config.size_bytes)]
    ]));
    const layers = node('section', 'detail-subsection'); layers.appendChild(node('h3', '', '순서대로 적용되는 레이어'));
    if (data.manifest_digest) layers.appendChild(infoList([['선택한 manifest', remoteDigest(data.manifest_digest)]]));
    if (items(data.layers).length) {
      const wrap = node('div', 'table-wrap remote-layer-table'); const table = node('table');
      table.appendChild(node('caption', '', data.kind === 'oci-image' ? '원격 manifest의 레이어 순서와 압축 크기 · 로컬 검증 전' : '원격 레이어 메타데이터 · 로컬 검증 전'));
      const head = node('thead'); const row = node('tr');
      for (const title of ['순번', '다이제스트', '크기', '미디어 형식']) { const th = node('th', '', title); th.scope = 'col'; row.appendChild(th); }
      head.appendChild(row); table.appendChild(head); const body = node('tbody');
      items(data.layers).forEach((layer, index) => {
        const tr = node('tr'); cell(tr, layer.ordinal ?? index + 1); cell(tr, remoteDigest(layer.digest)); cell(tr, bytes(layer.size_bytes), 'nowrap'); cell(tr, layer.media_type); body.appendChild(tr);
      });
      table.appendChild(body); wrap.appendChild(table); layers.appendChild(wrap);
    } else layers.appendChild(node('p', 'muted', data.is_index && !data.manifest_digest ? '플랫폼을 선택하면 해당 manifest의 레이어를 조회합니다.' : '레이어 항목이 없습니다.'));
    if (data.total_size_bytes !== undefined) layers.appendChild(node('p', 'muted', '레이어 총 크기: ' + bytes(data.total_size_bytes)));
    target.appendChild(layers);
    $('remote-detail-limitation').textContent = [data.download_limitation, data.run_limitation, !canControl() ? '읽기 전용 · 다운로드는 --allow-control이 필요합니다.' : ''].filter(Boolean).join(' · ');
  }
  async function fetchRemoteDetail(manifest) {
    if (!remoteDetail.connection || !remoteDetail.reference || remoteDetail.loading || authFailed) return;
    const restorePlatformFocus = !!manifest && document.activeElement?.classList.contains('remote-platform');
    const generation = ++remoteDetail.generation; const catalogGeneration = remoteState.generation;
    remoteDetail.abort?.abort(); remoteDetail.abort = new AbortController(); remoteDetail.loading = true; remoteDetail.item = null;
    notice($('remote-detail-feedback'), '원격 상세 메타데이터 조회 중…'); updateRemoteControls();
    const parameters = new URLSearchParams({ connection: remoteDetail.connection, reference: remoteDetail.reference });
    if (manifest) parameters.set('manifest', manifest);
    try {
      const data = await api('/api/v1/remotes/artifact?' + parameters, { signal: remoteDetail.abort.signal });
      if (generation !== remoteDetail.generation || catalogGeneration !== remoteState.generation) return;
      if (!data || typeof data !== 'object' || Array.isArray(data) || data.connection_id !== remoteDetail.connection || !data.reference) throw new Error('원격 상세 응답 형식이 올바르지 않습니다');
      remoteDetail.reference = data.immutable_reference || data.reference;
      remoteDetail.item = { ...data, reference: remoteDetail.reference };
      renderRemoteDetail(data); notice($('remote-detail-feedback'), '');
      if (restorePlatformFocus && document.activeElement === document.body) {
        $('remote-detail-content').querySelector('.remote-platform[aria-pressed="true"]')?.focus({ preventScroll: true });
      }
    } catch (error) {
      if (generation !== remoteDetail.generation || catalogGeneration !== remoteState.generation) return;
      $('remote-detail-content').replaceChildren(); $('remote-detail-limitation').textContent = '';
      notice($('remote-detail-feedback'), '상세 조회 실패 · ' + error.message, true);
    } finally {
      if (generation === remoteDetail.generation) { remoteDetail.loading = false; remoteDetail.abort = null; updateRemoteControls(); }
    }
  }
  function inspectRemoteArtifact(item, trigger) {
    if (item.details_supported !== true || !item.reference || !remoteState.result || remoteState.searching || hubState.connecting || authFailed) return;
    closeRemoteDetail();
    remoteDetail.connection = remoteState.result.connection_id; remoteDetail.reference = item.immutable_reference || item.reference; remoteDetail.returnFocus = trigger;
    $('remote-detail-content').replaceChildren(); $('remote-detail-limitation').textContent = '';
    $('remote-detail-dialog').showModal(); fetchRemoteDetail();
  }
  $('btn-remote-detail-download').addEventListener('click', () => { if (remoteDetail.item) startRemoteDownload(remoteDetail.item, false); });
  $('btn-remote-detail-run').addEventListener('click', () => { if (remoteDetail.item) startRemoteDownload(remoteDetail.item, true); });
  function renderRemoteResults(data) {
    const target = $('remote-results'); target.replaceChildren();
    const rows = items(data.items);
    $('remote-results-state').textContent = `${rows.length}개 · ${text(data.search_mode)}${data.message ? ' · ' + data.message : ''}`;
    $('btn-remote-next').hidden = !data.next_cursor;
    if (!rows.length) { target.appendChild(node('p', 'empty-state', data.next_cursor ? '이 페이지에 일치하는 결과가 없습니다. 다음 결과를 조회하세요.' : '일치하는 원격 아티팩트가 없습니다. 비공개 OCI 저장소는 정확한 이름:태그를 지정하세요.')); return; }
    const wrap = node('div', 'table-wrap'); const table = node('table');
    table.appendChild(node('caption', '', '연결된 원격 아티팩트 · 명시적 요청만 전송'));
    const head = node('thead'); const header = node('tr');
    for (const title of ['참조 / 이름', '분류 · 아키텍처', '실행 호환성', 'Actions']) { const th = node('th', '', title); th.scope = 'col'; header.appendChild(th); }
    head.appendChild(header); table.appendChild(head); const body = node('tbody');
    for (const item of rows) {
      const row = node('tr');
      const isCatalogRepo = !item.reference && item.repository;
      const name = cell(row);
      if (isCatalogRepo) {
        const repoBtn = node('button', 'row-open', item.name || item.repository);
        repoBtn.type = 'button';
        repoBtn.dataset.supported = 'true';
        repoBtn.dataset.read = 'true';
        repoBtn.title = `${item.repository} 태그 조회`;
        repoBtn.addEventListener('click', () => {
          $('remote-repository').value = item.repository;
          $('remote-query').value = '';
          $('form-remote-search').requestSubmit();
        });
        name.appendChild(repoBtn);
      } else {
        name.appendChild(document.createTextNode(item.name || item.repository || item.reference));
      }
      if (item.description) name.appendChild(node('small', 'cell-note', item.description));
      if (item.star_count != null || item.pull_count != null) {
        const stats = [];
        if (item.star_count != null) stats.push(`★ ${item.star_count.toLocaleString()}`);
        if (item.pull_count != null) stats.push(`다운로드 ${item.pull_count >= 1e9 ? (item.pull_count / 1e9).toFixed(1) + 'B' : item.pull_count >= 1e6 ? (item.pull_count / 1e6).toFixed(1) + 'M' : item.pull_count.toLocaleString()}`);
        if (item.last_updated) stats.push(`갱신 ${item.last_updated.slice(0, 10)}`);
        name.appendChild(node('small', 'cell-note', stats.join(' · ')));
      }
      if (item.reference) name.appendChild(node('small', 'cell-note mono', item.reference));
      if (item.digest) name.appendChild(node('small', 'cell-note mono', digest(item.digest)));
      cell(row, text(item.kind) + ' / ' + (array(item.architectures).join(', ') || '정보 없음'));
      cell(row, item.run_supported === true ? '다운로드 후 VM 실행 설정 가능' : item.run_limitation || item.download_limitation || '다운로드만 가능 · 실행 조건 확인 필요');
      const actions = node('div', 'row-actions');
      if (!item.reference && item.repository) {
        const browse = node('button', 'btn-row-action', data.search_mode === 'public-repository-search' ? '태그 조회' : '버전 조회'); browse.type = 'button'; browse.dataset.supported = 'true'; browse.dataset.read = 'true';
        browse.addEventListener('click', () => { remoteRepositoryQuery = $('remote-query').value; $('remote-repository').value = item.repository; $('remote-query').value = ''; updateRemoteBreadcrumb(); $('form-remote-search').requestSubmit(); }); actions.appendChild(browse);
      }
      if (item.details_supported === true) {
        const inspect = node('button', 'btn-row-action', '상세 정보'); inspect.type = 'button'; inspect.dataset.supported = 'true'; inspect.dataset.read = 'true';
        inspect.addEventListener('click', () => inspectRemoteArtifact(item, inspect)); actions.appendChild(inspect);
      }
      const download = node('button', 'btn-row-action', '다운로드'); download.type = 'button'; download.dataset.supported = String(item.download_supported === true);
      download.addEventListener('click', () => startRemoteDownload(item, false)); actions.appendChild(download);
      const run = node('button', 'btn-row-action btn-run-action', '다운로드 후 VM 설정'); run.type = 'button'; run.dataset.supported = String(item.download_supported === true && item.run_supported === true);
      run.addEventListener('click', () => startRemoteDownload(item, true)); actions.appendChild(run);
      cell(row, actions); body.appendChild(row);
    }
    table.appendChild(body); wrap.appendChild(table); target.appendChild(wrap); updateRemoteControls();
  }
  function remoteDownloadPlatform(item) {
    // Parent platforms are alternatives, not authority for a selected child.
    const selected = item.config || (item.manifest_digest && array(item.platforms).find(platform => platform.digest === item.manifest_digest));
    const architecture = selected?.architecture || selected?.arch || state.summary.data?.host?.machine;
    const arch = ({ amd64: 'amd64', x86_64: 'amd64', arm64: 'arm64', aarch64: 'arm64' })[(architecture || '').toLowerCase()];
    if (!arch) throw new Error('선택한 이미지 또는 호스트의 다운로드 플랫폼을 확인할 수 없습니다.');
    return `${selected?.os || 'linux'}/${arch}`;
  }
  async function startRemoteDownload(item, prepareRun) {
    if (!canControl() || actionBusy || hubState.connecting || remoteState.loading || remoteState.searching || remoteDetail.loading || item.download_supported !== true || (prepareRun && item.run_supported !== true)) return;
    const connection = remoteState.result?.connection_id;
    if (!connection || connection !== $('remote-connection').value || (item.connection_id && item.connection_id !== connection)) return;
    actionBusy = true; updateControls();
    try {
      const reference = item.download_reference || item.immutable_reference || item.reference;
      const preferredPlatform = remoteDownloadPlatform(item);
      const operation = await api('/api/v1/transfers', { method: 'POST', headers: connection === 'legacy' ? hubHeaders() : {}, body: { action: 'download', connection_id: connection, reference, platform: preferredPlatform } });
      if (prepareRun) pendingRemoteRuns.add(operation.id);
      const message = '다운로드 요청됨 · ' + reference + ' · 작업 ID ' + operation.id;
      feedback(message);
      if ($('remote-detail-dialog').open) closeRemoteDetail();
    } catch (error) {
      feedback('다운로드 요청 실패 · ' + error.message, true);
      if ($('remote-detail-dialog').open) notice($('remote-detail-feedback'), '다운로드 요청 실패 · ' + error.message, true);
    } finally { actionBusy = false; updateControls(); await refresh(true); }
  }
  function setupRunFromReceipt(receipt) {
    if (!canControl() || receipt.run_supported !== true) return;
    closeRemoteDetail();
    setupRunFromArtifact({ digest: receipt.image || receipt.source, kind: receipt.runtime_kind === 'oci-root' ? 'oci-image' : 'cloud-image', oci_root_digest: receipt.manifest });
    $('run-layers').value = array(receipt.layers).join('\n');
    notice($('run-form-feedback'), '원격 다운로드·무결성 검증 완료. 이미지와 전체 레이어 체인을 입력했습니다. 자원을 확인하고 새 VM 실행을 명시적으로 요청하세요.');
  }
  $('btn-remote-connections').addEventListener('click', loadRemoteConnections);
  $('remote-connection').addEventListener('change', selectRemoteConnection);
  $('remote-repository').addEventListener('input', updateRemoteBreadcrumb);
  $('form-remote-search').addEventListener('input', () => {
    if (remoteState.result) remoteState.result.next_cursor = null;
    $('btn-remote-next').hidden = true;
  });
  $('btn-remote-back').addEventListener('click', () => {
    $('remote-repository').value = ''; $('remote-query').value = remoteRepositoryQuery; updateRemoteBreadcrumb(); $('form-remote-search').requestSubmit();
  });
  $('btn-remote-catalog')?.addEventListener('click', () => {
    $('remote-repository').value = '';
    $('remote-query').value = '';
    $('form-remote-search').requestSubmit();
  });
  function switchArtifactsTab(tab) {
    const isLocal = tab === 'local';
    $('tab-artifacts-local')?.setAttribute('aria-selected', String(isLocal));
    $('tab-artifacts-remote')?.setAttribute('aria-selected', String(!isLocal));
    const localView = $('artifacts-local-view');
    const remoteView = $('artifacts-remote-view');
    if (localView) localView.hidden = !isLocal;
    if (remoteView) remoteView.hidden = isLocal;
    if (!isLocal) {
      if (!remoteState.loaded && !remoteState.loading) {
        loadRemoteConnections();
      } else if (!remoteState.result && !remoteState.searching && $('remote-connection')?.value) {
        $('form-remote-search')?.requestSubmit();
      }
    }
  }
  $('tab-artifacts-local')?.addEventListener('click', () => switchArtifactsTab('local'));
  $('tab-artifacts-remote')?.addEventListener('click', () => switchArtifactsTab('remote'));
  $('form-remote-search').addEventListener('submit', async event => {
    event.preventDefault(); if (actionBusy || hubState.connecting || remoteState.loading || remoteState.searching || !remoteState.loaded || authFailed || !$('remote-connection').value) return;
    closeRemoteDetail(); updateRemoteBreadcrumb();
    const generation = ++remoteState.generation; remoteState.abort?.abort(); remoteState.abort = new AbortController();
    const parameters = new URLSearchParams({ connection: $('remote-connection').value, kind: $('remote-kind').value, query: $('remote-query').value.trim(), repository: $('remote-repository').value.trim(), limit: '30' });
    const selected = remoteState.connections.find(item => item.id === $('remote-connection').value);
    if ($('remote-arch').value) parameters.set('arch', $('remote-arch').value);
    if (selected?.protocol === 'legacy') {
      for (const [key, id] of [['disk_format', 'remote-disk-format'], ['parent', 'remote-parent']]) if ($(id).value.trim()) parameters.set(key, $(id).value.trim());
    }
    parameters.set('compatible', $('remote-compatible').value);
    if (event.submitter === $('btn-remote-next') && remoteState.result?.next_cursor) parameters.set('cursor', remoteState.result.next_cursor);
    remoteState.result = null; remoteState.searching = true; updateRemoteControls(); $('btn-remote-next').hidden = true; $('remote-results-state').textContent = '원격 API 조회 중…';
    try {
      const data = await api('/api/v1/remotes/artifacts?' + parameters, { signal: remoteState.abort.signal });
      if (generation !== remoteState.generation) return;
      if (!Array.isArray(data.items)) throw new Error('원격 아티팩트 응답 형식이 올바르지 않습니다');
      remoteState.result = data; renderRemoteResults(data);
    } catch (error) { if (generation === remoteState.generation) { remoteState.result = null; $('remote-results').replaceChildren(); $('remote-results-state').textContent = '조회 실패 · ' + error.message; } }
    finally { if (generation === remoteState.generation) { remoteState.searching = false; updateRemoteControls(); } }
  });
  $('form-remote-upload').addEventListener('submit', async event => {
    event.preventDefault(); if (!canControl() || actionBusy || hubState.connecting || remoteState.loading || !$('upload-source').value || !$('upload-connection').value) return;
    const target = $('upload-target').value.trim();
    if (!window.confirm(`Hub 업로드: ${target}\n선택한 Hub 프로젝트에 게시합니다. 로컬 소스를 검증한 뒤 전송하며 원격 권한·태그 충돌은 서버에서 확인합니다. 계속하시겠습니까?`)) return;
    actionBusy = true; updateControls();
    try {
      const operation = await api('/api/v1/transfers', { method: 'POST', headers: $('upload-connection').value === 'legacy' ? hubHeaders() : {}, body: { action: 'upload', connection_id: $('upload-connection').value, source: JSON.parse($('upload-source').value), target } });
      rememberOperation(operation, 'artifacts');
      feedback('Hub 업로드 요청됨 · ' + target + ' · 작업 ID ' + operation.id);
    } catch (error) { feedback('Hub 업로드 실패 · ' + error.message, true); }
    finally { actionBusy = false; updateControls(); await refresh(true); }
  });

  const operationUi = {
    build: { label: '빌드', work: '빌드 준비·실행', running: 'Dockerfile 빌드와 캐시·산출물 처리를 진행하고 있습니다.', complete: '빌드 요청 처리 완료. 아래 출력과 빌드 목록에서 실제 산출물을 확인하세요.' },
    download: { label: '다운로드', work: '전송·무결성 검증', running: '로컬 클라이언트가 원격 아티팩트 전송과 무결성 검증을 처리하고 있습니다.', complete: '다운로드 처리 완료. VM 실행은 검증된 이미지의 실행 설정에서 별도로 요청하세요.' },
    upload: { label: '업로드', work: '검증·Hub 전송', running: '로컬 소스를 검증하고 선택한 Hub로 전송하고 있습니다.', complete: '업로드 요청 처리 완료. 아래 출력에서 등록 결과를 확인하세요.' },
    run: { label: 'VM 실행', work: '이미지 준비·실행', running: '이미지 준비와 VM 실행 요청을 처리하고 있습니다. 현재 VM 상태는 VM 목록에서 확인하세요.', complete: '실행 요청 처리 완료. 실행 중 여부는 VM 목록의 실제 상태를 확인하세요.' }
  };

  function rememberOperation(operation, viewKind) {
    if (!operation?.id || !operationUi[operation.kind]) return;
    const previous = array(state.operations.data?.operations).filter(item => item.id !== operation.id);
    state.operations = { data: { operations: [operation, ...previous] }, error: null, updated: new Date() };
    renderOperations(viewKind);
    const item = [...document.querySelectorAll('.operation-item')].find(item => item.dataset.operationId === operation.id);
    item?.scrollIntoView({ block: 'nearest', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
  }

  function createOperationFlow(op) {
    const flow = node('ol', 'operation-flow');
    const labels = ['요청 접수', operationUi[op.kind]?.work || '작업 처리', '결과 대기'];
    labels.forEach((label, index) => {
      const step = node('li', 'flow-step');
      const symbol = node('span', 'flow-symbol'); symbol.setAttribute('aria-hidden', 'true');
      if (index === 1) for (let layer = 0; layer < 3; layer++) symbol.appendChild(node('i', 'flow-slab'));
      else symbol.textContent = index === 0 ? '✓' : '·';
      step.append(symbol, node('span', 'flow-label', label));
      if (index < 2) {
        const track = node('span', 'flow-track'); track.setAttribute('aria-hidden', 'true');
        track.appendChild(node('i', 'flow-packet')); step.appendChild(track);
      }
      flow.appendChild(step);
    });
    return flow;
  }

  function updateOperationFlow(item, op) {
    item.dataset.status = op.status;
    const steps = item.querySelectorAll('.flow-step');
    steps[0].dataset.state = 'done';
    steps[1].dataset.state = op.status === 'succeeded' ? 'done' : op.status === 'failed' ? 'failed' : 'active';
    steps[2].dataset.state = op.status === 'succeeded' ? 'done' : 'pending';
    const result = op.status === 'succeeded' ? '요청 완료' : op.status === 'failed' ? '미완료' : '결과 대기';
    steps[2].querySelector('.flow-label').textContent = result;
    steps[2].querySelector('.flow-symbol').textContent = op.status === 'succeeded' ? '✓' : op.status === 'failed' ? '—' : '·';
    const description = op.status === 'running' ? operationUi[op.kind]?.running : op.status === 'succeeded' ? operationUi[op.kind]?.complete : '작업이 실패했습니다. 아래 오류와 실제 출력을 확인하세요.';
    const activity = item.querySelector('.operation-activity');
    if (activity.textContent !== description) activity.textContent = description || '';
  }

  function renderOperations(viewKind) {
    const prefix = viewKind === 'vms' ? 'vm' : viewKind === 'artifacts' ? 'transfer' : 'build';
    const list = $(prefix + '-operations-list');
    const card = $(prefix + '-operations-card');
    if (!list || !card) return;
    const kinds = viewKind === 'vms' ? ['run'] : viewKind === 'artifacts' ? ['download', 'upload'] : ['build'];
    const operations = array(object(state.operations.data).operations).filter(op => kinds.includes(op.kind)).slice().reverse();
    card.hidden = operations.length === 0;
    const retained = new Set(operations.map(op => op.id));
    for (const item of Array.from(list.children)) if (!retained.has(item.dataset.operationId)) item.remove();
    const items = new Map(Array.from(list.children, item => [item.dataset.operationId, item]));
    let cursor = list.firstChild;
    for (const op of operations) {
      const item = items.get(op.id) || createOperationItem(op);
      updateOperationItem(item, op);
      if (item !== cursor) list.insertBefore(item, cursor);
      cursor = item.nextSibling;
    }
  }

  function createOperationItem(op) {
    const item = node('div', 'operation-item');
    item.dataset.operationId = op.id;
    const header = node('div', 'operation-header');
    header.append(node('span', 'operation-title'), node('span', 'badge'));
    const activity = node('p', 'operation-activity'); activity.setAttribute('role', 'status');
    item.append(header, createOperationFlow(op), activity,
      node('p', 'operation-observation-paused', '관찰 갱신이 중지되어 모션을 일시 정지했습니다. 실제 작업은 계속될 수 있습니다.'),
      node('div', 'operation-meta'), node('p', 'notice danger'),
      node('div', 'cell-note muted', '출력 잘림: 마지막 128 KiB만 표시됩니다.'), node('pre', 'operation-output'), node('div', 'operation-actions'));
    return item;
  }

  function updateOperationItem(item, op) {
    updateOperationFlow(item, op);
    const title = item.querySelector('.operation-title');
    const operationLabel = operationUi[op.kind]?.label || '작업';
    const titleText = operationLabel + ': ' + text(op.resource_name || op.id);
    if (title.textContent !== titleText) title.textContent = titleText;
    const badge = item.querySelector('.badge');
    badge.className = 'badge' + (op.status === 'succeeded' ? ' good' : op.status === 'failed' ? ' bad' : ' warn');
    const label = op.status === 'running' ? '진행 중 (HTTP 202)' : statusText(op.status);
    if (badge.textContent !== label) badge.textContent = label;
    let metaText = `작업 ID: ${text(op.id)} · 분류: ${operationLabel}`;
    if (op.returncode !== undefined && op.returncode !== null) metaText += ` · 종료 코드: ${op.returncode}`;
    const meta = item.querySelector('.operation-meta');
    if (meta.textContent !== metaText) meta.textContent = metaText;
    const error = item.querySelector('.notice');
    error.hidden = !op.error;
    const errorText = op.error ? '오류: ' + text(op.error) : '';
    if (error.textContent !== errorText) error.textContent = errorText;
    item.querySelector('.cell-note').hidden = !op.output_truncated;
    const output = item.querySelector('.operation-output');
    const content = op.output || '';
    output.hidden = !content;
    if (output.textContent !== content) {
      const scroll = output.scrollTop;
      const following = output.scrollHeight - output.clientHeight - scroll <= 2;
      if (output.firstChild?.nodeType === Node.TEXT_NODE && content.startsWith(output.textContent)) output.firstChild.appendData(content.slice(output.textContent.length));
      else output.textContent = content;
      output.scrollTop = following ? output.scrollHeight : scroll;
    }
    const receipt = transferReceipt(op); const actions = item.querySelector('.operation-actions');
    if (op.kind === 'download' && receipt?.run_supported === true && !actions.children.length) {
      const run = node('button', 'btn-run-action', '검증된 이미지로 VM 실행 설정'); run.type = 'button';
      run.addEventListener('click', () => setupRunFromReceipt(receipt)); actions.appendChild(run);
    }
    for (const button of actions.querySelectorAll('button')) button.disabled = !canControl() || actionBusy;
  }

  function setupRunFromArtifact(item) {
    if (item.compatibility && item.compatibility.compatible !== true) { feedback('플랫폼 호환성: ' + (array(item.compatibility.reasons).join(' · ') || '확인 불가'), true); return; }
    if (item.run_supported === false) { feedback(item.run_limitation || '이 아티팩트의 VM 실행은 현재 호스트에서 지원되지 않습니다.', true); return; }
    if (item.source === 'remote-transfer') { setupRunFromReceipt(item); return; }
    navigate('vms');
    const details = $('vm-run-controls');
    if (details) details.open = true;
    const chosenImage = item.digest || '';
    $('run-image').value = chosenImage;
    const isOci = item.kind === 'oci-image' || (item.display_kind === 'image' && String(item.media_type || '').includes('oci'));
    $('run-runtime-kind').value = isOci ? 'oci-root' : 'cloud-image';
    const manifestDigest = item.oci_root_digest || item.output_oci_manifest_digest || (item.digest_type === 'oci-manifest' ? item.digest : '');
    $('run-manifest').value = manifestDigest || '';
    $('run-layers').value = '';
    notice($('run-form-feedback'), '선택한 이미지(' + chosenImage + ') 정보가 새 VM 실행 폼에 입력되었습니다. 리소스 설정을 검토한 후 실행 요청하세요.', false);
    $('run-image').focus();
  }

  function updateConsoleBadge(labelText, type) {
    const b = $('console-status-badge');
    if (!b) return;
    b.textContent = labelText;
    b.className = 'badge' + (type === 'good' ? ' good' : type === 'bad' ? ' bad' : type === 'warn' ? ' warn' : '');
  }

  function openConsole(item, initialArgv) {
    if (!consoleEligible(item)) return;
    const vmName = item.name; const runtimeKind = item.runtime_kind;
    if (!canControl()) {
      feedback('콘솔 접속은 --allow-control 모드에서만 지원됩니다.', true);
      return;
    }
    const mode = consoleMode(item);
    const isOci = mode === 'exec';
    closeConsoleSession();
    activeConsoleTarget = { id: itemId('vms', item), name: item.name, run_id: item.run_id, mode };
    $('console-title').textContent = vmName + ' 콘솔';
    $('console-kind').textContent = '가상 머신 콘솔 / ' + text(runtimeKind || item.backend);
    $('console-mode-label').textContent = isOci ? '모드: Guest Exec (non-TTY)' : '모드: 대화형 PTY 셸';
    const capInfo = $('console-capability-info');
    if (isOci) {
      notice(capInfo, '이 VM은 대화형 PTY 셸 대신 명시적 guest command exec를 지원합니다. 실제 게스트에서 명령을 실행하며 TTY 에뮬레이션은 비활성화됩니다.', false);
      $('console-exec-bar').hidden = false;
      if (initialArgv) $('console-exec-argv').value = Array.isArray(initialArgv) ? initialArgv.join(' ') : initialArgv;
    } else {
      notice(capInfo, '');
      $('console-exec-bar').hidden = true;
    }
    $('console-drawer').showModal();
    $('btn-close-console').focus();
    setupTerminalUI(vmName, isOci ? 'exec' : 'shell');
    updateControls();
    if (!isOci) {
      startConsoleSession(vmName, 'shell');
    } else {
      updateConsoleBadge('명령 입력 대기', 'warn');
    }
  }

  function setupTerminalUI(vmName, mode) {
    const container = $('console-terminal-container');
    activeConsoleTerm?.dispose?.();
    consoleSizeObserver?.disconnect();
    container.replaceChildren();
    if (typeof Terminal !== 'undefined') {
      const term = new Terminal({
        cursorBlink: true,
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
        fontSize: 13,
        lineHeight: 1.4,
        theme: {
          background: '#101722',
          foreground: '#d3deeb',
          cursor: '#7cb7f7',
          selectionBackground: 'rgba(255, 255, 255, 0.2)'
        }
      });
      term.open(container);
      activeConsoleTerm = term;
      consoleSizeObserver?.disconnect();
      const fitTerminal = () => {
        if (term !== activeConsoleTerm) return;
        const screen = term.element.querySelector('.xterm-screen');
        if (!screen) return;
        const bounds = screen.getBoundingClientRect();
        const cellWidth = bounds.width / term.cols;
        const cellHeight = bounds.height / term.rows;
        if (!(cellWidth > 0 && cellHeight > 0)) return;
        const viewport = term.element.querySelector('.xterm-viewport');
        const scrollbar = viewport ? viewport.offsetWidth - viewport.clientWidth : 0;
        const cols = Math.max(2, Math.floor((container.clientWidth - scrollbar) / cellWidth));
        const rows = Math.max(1, Math.floor(container.clientHeight / cellHeight));
        if (term.cols !== cols || term.rows !== rows) term.resize(cols, rows);
      };
      consoleSizeObserver = new ResizeObserver(() => requestAnimationFrame(fitTerminal));
      consoleSizeObserver.observe(container);
      requestAnimationFrame(fitTerminal);
      activeConsoleTerm.focus();
    } else {
      throw new Error('로컬 xterm 자산을 불러오지 못했습니다. 화면을 다시 여세요.');
    }
  }

  async function startConsoleSession(vmName, mode, argv) {
    if (!canControlConsole() || activeConsoleTarget.name !== vmName) return;
    for (const id of pendingConsoleOpens) api(`/api/v1/consoles/${id}`, { method: 'DELETE', keepalive: true }).catch(() => {});
    const generation = ++consoleGeneration;
    const term = activeConsoleTerm;
    if (activeConsoleSession) await closeConsoleSession(activeConsoleSession, false);
    if (generation !== consoleGeneration || !$('console-drawer').open || !canControlConsole()) return;
    updateConsoleBadge('연결 중…', 'warn');
    const requestId = Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
    pendingConsoleOpens.add(requestId);
    try {
      const target = activeConsoleTarget;
      const body = { mode, id: requestId, expected_run_id: target.run_id };
      if (argv?.length) body.argv = argv;
      // The ID is addressable even if this document never receives the response.
      const res = await api(`/api/v1/vms/${encodeURIComponent(target.id)}/console`, { method: 'POST', body });
      if (generation !== consoleGeneration || term !== activeConsoleTerm || !$('console-drawer').open || !canControlConsole()) {
        await api(`/api/v1/consoles/${encodeURIComponent(res.id)}`, { method: 'DELETE', keepalive: true });
        return;
      }
      const session = {
        id: res.id, vmName, mode, term, generation, capabilities: object(res.capabilities),
        cursor: res.cursor || 0, active: true, pollAbort: new AbortController(),
        controls: Promise.resolve(), pendingControls: [], drainingControls: false, queuedControls: 0, queuedBytes: 0
      };
      activeConsoleSession = session;
      updateConsoleBadge(`연결됨 (${mode === 'shell' ? 'PTY 셸' : 'Exec'})`, 'good');
      const caps = session.capabilities;
      notice($('console-capability-info'), `콘솔 세션 확정 · Stdin=${caps.stdin ? '지원' : '미지원'} · TTY=${caps.tty ? '지원' : '미지원'} · 크기조절=${caps.resize ? '지원' : '미지원'} · 시그널=${caps.signal ? '지원' : '미지원'}`, false);
      updateControls();
      session.inputDisposable = term.onData(data => queueConsoleInput(session, new TextEncoder().encode(data)));
      if (caps.resize) {
        session.resizeDisposable = term.onResize(({ rows, cols }) => queueConsoleControl(session, 'resize', { rows, columns: cols }));
        queueConsoleControl(session, 'resize', { rows: term.rows, columns: term.cols });
      }
      pollConsoleEvents(session);
    } catch (error) {
      await api(`/api/v1/consoles/${requestId}`, { method: 'DELETE', keepalive: true }).catch(() => {});
      if (generation !== consoleGeneration) return;
      updateConsoleBadge('연결 실패', 'bad');
      term?.write(`\r\n[콘솔 연결 실패: ${error.message}]\r\n`);
    } finally { pendingConsoleOpens.delete(requestId); }
  }

  function consoleControlAllowed(session, action) {
    const capability = action === 'input' ? 'stdin' : action;
    return session.active && !session.exited && activeConsoleSession === session && session.generation === consoleGeneration && canControlConsole() && session.capabilities[capability] === true;
  }
  function clearConsoleQueue(session) {
    for (const entry of session.pendingControls) {
      session.queuedControls--;
      session.queuedBytes -= entry.bytes;
    }
    session.pendingControls = [];
  }
  function queueConsoleInput(session, bytes) {
    if (!consoleControlAllowed(session, 'input') || !bytes.length) return;
    const tail = session.pendingControls[session.pendingControls.length - 1];
    const room = tail?.action === 'input' ? 16384 - tail.bytes : 0;
    const chunks = Math.ceil(Math.max(0, bytes.length - room) / 16384);
    // Reject the whole event before appending any bytes, including an Enter key.
    if (session.queuedBytes + bytes.length > 256 * 1024 || session.queuedControls + chunks > 64) {
      notice($('console-capability-info'), '입력 대기열 한도에 도달했습니다. 이 입력은 전송하지 않았습니다.', true);
      return;
    }
    let offset = 0;
    while (offset < bytes.length) {
      let entry = session.pendingControls[session.pendingControls.length - 1];
      // Only the pending tail is mutable; resize/signals and in-flight input are barriers.
      if (entry?.action !== 'input' || entry.bytes === 16384) {
        entry = { action: 'input', data: new Uint8Array(16384), bytes: 0 };
        session.pendingControls.push(entry); session.queuedControls++;
      }
      const count = Math.min(16384 - entry.bytes, bytes.length - offset);
      entry.data.set(bytes.subarray(offset, offset + count), entry.bytes);
      entry.bytes += count; offset += count;
    }
    session.queuedBytes += bytes.length;
    drainConsoleControls(session);
  }
  function queueConsoleControl(session, action, body) {
    if (!consoleControlAllowed(session, action)) return;
    if (session.queuedControls >= 64) {
      notice($('console-capability-info'), '콘솔 제어 대기열 한도에 도달했습니다. 요청을 전송하지 않았습니다.', true);
      return;
    }
    session.pendingControls.push({ action, body, bytes: 0 }); session.queuedControls++;
    drainConsoleControls(session);
  }
  function drainConsoleControls(session) {
    if (session.drainingControls) return;
    session.drainingControls = true;
    session.controls = session.controls.then(async () => {
      while (session.pendingControls.length) {
        if (!consoleControlAllowed(session, session.pendingControls[0].action)) { clearConsoleQueue(session); break; }
        const entry = session.pendingControls.shift();
        try {
          let body = entry.body;
          if (entry.action === 'input') {
            let binary = '';
            for (const byte of entry.data.subarray(0, entry.bytes)) binary += String.fromCharCode(byte);
            body = { data: btoa(binary) };
          }
          await api(`/api/v1/consoles/${encodeURIComponent(session.id)}/${entry.action}`, { method: 'POST', body });
        } catch (error) {
          if (session.active && activeConsoleSession === session) notice($('console-capability-info'), `콘솔 ${entry.action} 실패 · ${error.message}`, true);
        } finally {
          session.queuedControls--; session.queuedBytes -= entry.bytes;
        }
      }
    }).finally(() => {
      session.drainingControls = false;
      if (session.pendingControls.length) drainConsoleControls(session);
    });
  }

  async function pollConsoleEvents(session) {
    while (session.active && !authFailed) {
      try {
        const url = `/api/v1/consoles/${encodeURIComponent(session.id)}?after=${encodeURIComponent(session.cursor)}&wait=1`;
        const res = await api(url, { signal: session.pollAbort.signal });
        if (!session.active || activeConsoleSession !== session) break;
        const events = array(res.events);
        for (const ev of events) {
          if (ev.kind === 'output' && ev.data) session.term.write(decodeBase64ToBytes(ev.data));
          else if (ev.kind === 'exit') {
            session.exited = true;
            clearConsoleQueue(session);
            const category = text(ev.category);
            const outcome = ev.signal_number != null ? `시그널 ${ev.signal_number}` : ev.exit_code != null ? `종료 코드 ${ev.exit_code}` : `반환값 ${ev.returncode}`;
            session.exitLabel = `종료됨 (${category} · ${outcome})`;
            session.exitType = category === 'exited' && ev.exit_code === 0 ? 'good' : 'bad';
            updateConsoleBadge(session.exitLabel, session.exitType);
            session.term.write(`\r\n[${session.exitLabel}]\r\n`);
            updateControls();
          } else if (ev.kind === 'error') session.term.write(`\r\n[오류: ${ev.message || '콘솔 오류'}]\r\n`);
        }
        session.cursor = res.cursor;
        if (res.closed) {
          session.active = false;
          clearConsoleQueue(session);
          session.inputDisposable?.dispose();
          session.resizeDisposable?.dispose();
          updateConsoleBadge(session.exitLabel || '세션 닫힘', session.exitType || '');
          updateControls();
          break;
        }
      } catch (error) {
        if (!session.active || activeConsoleSession !== session) break;
        notice($('console-capability-info'), '콘솔 연결 실패 · ' + error.message, true);
        await closeConsoleSession(session);
        break;
      }
    }
  }

  function sendConsoleSignal(signal) {
    const session = activeConsoleSession;
    if (session?.capabilities.signal) queueConsoleControl(session, 'signal', { signal });
  }

  async function closeConsoleSession(session = activeConsoleSession, invalidate = true) {
    if (invalidate) {
      consoleGeneration++;
      for (const id of pendingConsoleOpens) api(`/api/v1/consoles/${id}`, { method: 'DELETE', keepalive: true }).catch(() => {});
    }
    if (!session) { updateConsoleBadge('연결 끊김', ''); updateControls(); return; }
    if (activeConsoleSession === session) activeConsoleSession = null;
    session.active = false;
    clearConsoleQueue(session);
    session.inputDisposable?.dispose();
    session.resizeDisposable?.dispose();
    session.pollAbort.abort();
    if (!activeConsoleSession) updateConsoleBadge('연결 끊김', '');
    updateControls();
    try { await api(`/api/v1/consoles/${encodeURIComponent(session.id)}`, { method: 'DELETE', keepalive: true }); } catch (_) {}
  }

  function volumeAttachReason(item, backend) {
    if (!backend) return '백엔드를 먼저 선택하세요.';
    if (item.kind !== 'named' || item.backend !== backend) return '백엔드가 일치하지 않습니다.';
    if (item.stale === true || item.status !== 'available') return '현재 연결 가능한 볼륨이 아닙니다.';
    if (!Array.isArray(item.attachments) || item.attachments.length) return '이미 연결되었거나 예약되어 있습니다.';
    return '';
  }
  function updateSelectOptions(select, choices, placeholder) {
    const signature = JSON.stringify(choices);
    if (select.dataset.optionsSignature === signature) return;
    const previous = select.value;
    select.replaceChildren(new Option(placeholder, ''));
    for (const choice of choices) { const option = new Option(choice.label, choice.value); option.disabled = choice.disabled === true; select.appendChild(option); }
    if (previous && !choices.some(choice => choice.value === previous)) {
      const missing = new Option(previous + ' · 현재 목록에서 확인되지 않음', previous); missing.disabled = true; select.appendChild(missing);
    }
    select.value = previous; select.dataset.optionsSignature = signature;
  }
  function updateVolumeForms() {
    const backends = array(state.volumes.data?.create_backends).filter(backend => typeof backend === 'string');
    const choices = backends.map(backend => ({ value: backend, label: backend }));
    updateSelectOptions($('volume-backend'), choices, '백엔드 선택');
    updateSelectOptions($('run-backend'), choices, '자동 선택');
    const ready = freshForMutation('volumes') && !actionBusy;
    for (const control of $('form-create-volume').querySelectorAll('input, select, button')) control.disabled = !ready || !backends.length;
    if (!ready && state.volumes.error) notice($('volume-create-feedback'), '목록 갱신 실패 · 최신 볼륨 정보를 확인한 뒤 생성하세요.', true);
    const backend = $('run-backend').value;
    const volumeChoices = (collection('volumes') || []).filter(item => item.kind === 'named' && (!backend || item.backend === backend)).map(item => {
      const reason = volumeAttachReason(item, backend);
      return { value: itemId('volumes', item), label: `${item.name} · ${item.backend} · ${bytes(item.size_bytes)} · retain${reason ? ' · ' + reason : ''}`, disabled: !!reason };
    });
    for (const row of $('run-volumes').children) {
      const select = row.querySelector('select'); updateSelectOptions(select, volumeChoices, '이름 있는 볼륨 선택');
      select.disabled = !ready || !backend;
    }
    $('btn-add-run-volume').disabled = !ready || !freshForMutation('vms', 'artifacts') || !backend || !volumeChoices.some(choice => !choice.disabled);
  }
  $('run-backend').addEventListener('change', updateControls);
  $('btn-add-run-volume').addEventListener('click', () => {
    if (actionBusy || !freshForMutation('vms', 'volumes', 'artifacts') || !$('run-backend').value) return;
    const number = ++volumeAttachmentSequence; const row = node('div', 'run-volume-row'); row.id = 'run-volume-row-' + number;
    const volumeLabel = node('label', '', '이름 있는 볼륨'); const select = node('select'); select.id = 'run-volume-name-' + number; select.required = true; volumeLabel.appendChild(select);
    const mountLabel = node('label', '', '게스트 마운트 경로'); const mount = node('input'); mount.id = 'run-volume-mount-' + number; mount.required = true; mount.placeholder = '/data'; mount.autocomplete = 'off'; mountLabel.appendChild(mount);
    const roLabel = node('label', 'check-label', '읽기 전용'); const ro = node('input'); ro.type = 'checkbox'; ro.id = 'run-volume-read-only-' + number; roLabel.prepend(ro);
    const remove = node('button', '', '연결 제외'); remove.id = 'btn-remove-run-volume-' + number; remove.type = 'button'; remove.addEventListener('click', () => { if (!actionBusy && freshForMutation('vms', 'volumes', 'artifacts')) { row.remove(); updateControls(); } });
    row.append(volumeLabel, mountLabel, roLabel, remove); $('run-volumes').appendChild(row); updateControls(); select.focus();
  });
  function selectedRunVolumes() {
    const backend = $('run-backend').value; const names = new Set(); const targets = new Set(); const volumes = [];
    for (const row of $('run-volumes').children) {
      const id = row.querySelector('select').value; const item = collection('volumes')?.find(volume => itemId('volumes', volume) === id);
      if (!item || volumeAttachReason(item, backend)) throw new Error('선택한 볼륨의 연결 가능 상태와 백엔드를 확인하세요.');
      const mount_path = row.querySelector('input:not([type="checkbox"])').value.trim();
      if (!mount_path.startsWith('/') || mount_path === '/' || mount_path.split('/').some(part => part === '.' || part === '..') || /[\s\x00-\x1f]/.test(mount_path)) throw new Error('마운트 경로는 /data처럼 / 이외의 절대 게스트 경로여야 합니다.');
      const normalizedTarget = mount_path.replace(/\/+$/, '').replace(/\/+/g, '/');
      if (names.has(item.name) || targets.has(normalizedTarget)) throw new Error('볼륨 또는 게스트 마운트 경로를 중복 지정할 수 없습니다.');
      names.add(item.name); targets.add(normalizedTarget);
      volumes.push({ name: item.name, mount_path, read_only: row.querySelector('input[type="checkbox"]').checked });
    }
    return volumes;
  }
  $('form-create-volume').addEventListener('submit', async event => {
    event.preventDefault(); if (actionBusy || !freshForMutation('volumes')) return;
    const name = $('volume-name').value.trim(); const backend = $('volume-backend').value; const size_bytes = Number($('volume-size-gib').value) * 1024 ** 3;
    if (!name || !array(state.volumes.data?.create_backends).includes(backend) || !Number.isSafeInteger(size_bytes) || size_bytes <= 0) return;
    actionBusy = true; clearTimeout(timer); updateControls(); updateHeader(); notice($('volume-create-feedback'), '독립 볼륨 생성 중…');
    try {
      if (refreshPromise) await refreshPromise;
      if (!freshForMutation('volumes') || !array(state.volumes.data?.create_backends).includes(backend)) throw new Error('최신 볼륨 목록과 백엔드를 확인할 수 없습니다.');
      const result = await api(endpoints.volumes, { method: 'POST', body: { name, backend, size_bytes } });
      notice($('volume-create-feedback'), `볼륨 생성 완료 · ${result.name || name} · 보존 정책: ${text(result.retention_policy)} · VM 생성 시 선택할 수 있습니다.`);
      feedback('독립 볼륨 생성 완료 · ' + name); await refresh(true);
    } catch (error) { notice($('volume-create-feedback'), '볼륨 생성 실패 · ' + error.message, true); }
    finally { actionBusy = false; updateControls(); updateHeader(); renderList('volumes', true); schedule(); }
  });

  $('form-run-vm').addEventListener('submit', async event => {
    event.preventDefault();
    if (!freshForMutation('vms', 'volumes', 'artifacts') || actionBusy) return;
    const image = $('run-image').value.trim();
    const name = $('run-name').value.trim() || undefined;
    const runtimeKind = $('run-runtime-kind').value || undefined;
    const registry = $('run-registry').value.trim() || undefined;
    const memoryStr = $('run-memory').value.trim();
    const memory = memoryStr ? parseInt(memoryStr, 10) : undefined;
    const vcpusStr = $('run-vcpus').value.trim();
    const vcpus = vcpusStr ? parseInt(vcpusStr, 10) : undefined;
    const network = $('run-network').value || undefined;
    const publishStr = $('run-publish').value.trim();
    const publish = publishStr ? publishStr.split(',').map(s => s.trim()).filter(Boolean) : undefined;
    const manifest = $('run-manifest').value.trim() || undefined;
    const cmdStr = $('run-command').value.trim();
    const command = cmdStr ? ['sh', '-lc', cmdStr] : undefined;
    const layers = $('run-layers').value.trim().split(/\s+/).filter(Boolean);

    const host = object(state.summary.data?.host);
    const isLinuxAmd64 = (host.system || '').toLowerCase() === 'linux' && ['x86_64', 'amd64'].includes((host.machine || '').toLowerCase());
    const forceArchitecture = $('run-force-architecture')?.checked;
    if (runtimeKind === 'oci-root' && !isLinuxAmd64 && !forceArchitecture) {
      notice($('run-form-feedback'), `실행 제한: OCI 루트 런타임은 Linux amd64 KVM 전용입니다. Darwin arm64 또는 이기종 이미지는 '이기종 강제 실행 시도'와 캐리어 설정을 선택해야 합니다.`, true);
      return;
    }

    let volumes;
    try {
      if ($('run-backend').value && !array(state.volumes.data?.create_backends).includes($('run-backend').value)) throw new Error('현재 사용할 수 있는 백엔드를 다시 선택하세요.');
      volumes = selectedRunVolumes();
    } catch (error) { notice($('run-form-feedback'), error.message, true); return; }
    const payload = { image, volumes, root_retention: forceArchitecture ? 'delete' : 'retain' };
    if ($('run-backend').value) payload.backend = $('run-backend').value;
    if (name) payload.name = name;
    if (runtimeKind) payload.runtime_kind = runtimeKind;
    if (registry) payload.registry = registry;
    if (memory) payload.memory = memory;
    if (vcpus) payload.vcpus = vcpus;
    if (network) payload.network = network;
    if (publish && publish.length) payload.publish = publish;
    if (manifest) payload.manifest = manifest;
    if (command && command.length) payload.command = command;
    if (layers.length) payload.layers = layers;
    const platformVal = $('run-platform')?.value || undefined;
    if (platformVal) payload.platform = platformVal;
    if (forceArchitecture) payload.force_architecture = true;
    const vmImage = $('run-vm-image')?.value.trim() || undefined;
    if (vmImage) payload.vm_image = vmImage;
    const vmImageDigest = $('run-vm-image-digest')?.value.trim() || undefined;
    if (vmImageDigest) payload.vm_image_digest = vmImageDigest;

    notice($('run-form-feedback'), 'VM 실행 요청 전송 중…', false);
    try {
      actionBusy = true;
      updateControls();
      if (refreshPromise) await refreshPromise;
      if (!freshForMutation('vms', 'volumes', 'artifacts')) throw new Error('최신 VM / 볼륨 / 이미지 목록을 확인할 수 없습니다.');
      selectedRunVolumes();
      const res = await api('/api/v1/vms', { method: 'POST', body: payload });
      rememberOperation(res, 'vms');
      notice($('run-form-feedback'), `VM 실행 작업 제출됨 (HTTP 202) · 작업 ID: ${text(res.id)} · 실제 완료 상태와 출력은 아래 최근 작업에서 확인하세요.`, false);
      feedback(`새 VM 실행 작업 제출: ${name || image} (작업 ID: ${res.id})`);
      await refresh(true);
    } catch (err) {
      notice($('run-form-feedback'), `VM 실행 요청 실패 · ${err.message}`, true);
    } finally {
      actionBusy = false;
      updateControls();
      schedule();
    }
  });

  $('form-build').addEventListener('submit', async event => {
    event.preventDefault();
    if (!canControl() || actionBusy) return;
    const context = $('build-context').value.trim();
    const tag = $('build-tag').value.trim();
    const dockerfile = $('build-dockerfile').value.trim() || undefined;
    const platform = $('build-platform').value || undefined;
    const target = $('build-target').value.trim() || undefined;
    const argsStr = $('build-args').value.trim();
    const buildArgs = argsStr ? argsStr.split(/[\n,]/).map(s => s.trim()).filter(Boolean) : undefined;
    const dockerfileText = $('build-dockerfile-text').value.trim() || undefined;
    const offline = $('build-offline').checked;

    const payload = { context, tag };
    if (dockerfile) payload.dockerfile = dockerfile;
    if (dockerfileText) payload.dockerfile_text = dockerfileText;
    if (platform) payload.platform = platform;
    if (target) payload.target = target;
    if (buildArgs && buildArgs.length) payload.build_args = buildArgs;
    if (offline) payload.offline = true;

    notice($('build-form-feedback'), '이미지 빌드 요청 전송 중…', false);
    try {
      actionBusy = true;
      updateControls();
      const res = await api('/api/v1/builds', { method: 'POST', body: payload });
      rememberOperation(res, 'builds');
      notice($('build-form-feedback'), `이미지 빌드 작업 제출됨 (HTTP 202) · 작업 ID: ${text(res.id)} · 실제 완료 상태와 출력은 아래 최근 작업에서 확인하세요.`, false);
      feedback(`새 이미지 빌드 작업 제출: ${tag} (작업 ID: ${res.id})`);
      await refresh(true);
    } catch (err) {
      notice($('build-form-feedback'), `이미지 빌드 요청 실패 · ${err.message}`, true);
    } finally {
      actionBusy = false;
      updateControls();
      schedule();
    }
  });

  $('console-drawer').addEventListener('close', () => {
    if ($('console-drawer').open) return;
    closeConsoleSession();
    consoleSizeObserver?.disconnect();
    activeConsoleTerm?.dispose?.();
    activeConsoleTerm = null;
    activeConsoleTarget = null;
  });
  $('btn-close-console').addEventListener('click', () => $('console-drawer').close());
  $('btn-console-disconnect').addEventListener('click', () => closeConsoleSession());
  $('btn-console-sigint').addEventListener('click', () => sendConsoleSignal('interrupt'));
  $('btn-console-sigterm').addEventListener('click', () => sendConsoleSignal('terminate'));
  $('btn-console-sighup').addEventListener('click', () => sendConsoleSignal('hangup'));
  $('btn-console-exec-run').addEventListener('click', () => {
    const cmdStr = $('console-exec-argv').value.trim();
    if (!cmdStr) { notice($('console-capability-info'), '실행할 게스트 명령을 입력하세요.', true); return; }
    const argv = ['sh', '-lc', cmdStr];
    if (canControlConsole()) startConsoleSession(activeConsoleTarget.name, 'exec', argv);
  });
  window.addEventListener('beforeunload', () => {
    clearTimeout(timer);
    const ownedIds = new Set(pendingConsoleOpens);
    if (activeConsoleSession) ownedIds.add(activeConsoleSession.id);
    for (const id of ownedIds) {
      try {
        fetch(`/api/v1/consoles/${id}`, {
          method: 'DELETE', keepalive: true, credentials: 'omit', referrerPolicy: 'no-referrer',
          headers: { Authorization: 'Bearer ' + token }
        });
      } catch (_) {}
    }
  });
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
  $('btn-pause').addEventListener('click', () => { paused = !paused; if (inspector) resetMetricRates(inspector); updateHeader(); renderResources(); if (paused) clearTimeout(timer); else if (!document.hidden) refresh(); });
  $('btn-refresh').addEventListener('click', () => { authFailed = false; refresh(true); });
  document.addEventListener('visibilitychange', () => { clearTimeout(timer); if (inspector) resetMetricRates(inspector); updateHeader(); renderResources(); if (!document.hidden && !paused && !authFailed) refresh(true); });
  window.addEventListener('hashchange', () => { const key = location.hash.slice(1); if (views[key]) navigate(key); });
  navigate(location.hash.slice(1) || 'overview');
  refresh(true);
})();
