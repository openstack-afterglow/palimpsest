# Changelog

All notable changes to Palimpsest Local are documented here.

## [Unreleased]

## [0.3.14] - 2026-11-08 (UTC)

### Changed

- The caller's own image quota view moves from `GET /v1/image-quota/me` to
  `GET /v1/published-images/quota` (clean cutover, no alias). The admin routes under
  `/v1/admin/image-quota/` are unchanged.
- Staged-but-unregistered publication payloads are bounded per user and project: an aggregate
  byte cap (`PALIMPSEST_HUB_STAGED_MAX_BYTES`, default 200 GiB, Kolla `palimpsest_hub_staged_max_bytes`)
  rejects the overflowing upload with 413 and the structured code `staged_payload_limit`
  (`limit_bytes`, `staged_bytes`; the client prints an actionable message). Payloads older than
  `PALIMPSEST_HUB_STAGED_TTL_SECONDS` (default 24 h, Kolla `palimpsest_hub_staged_ttl_seconds`) that no image of the same project and user uses
  are removed together with their `image-stage` blob reference, on that user's next staging request
  (committed even when the upload is refused) and by export maintenance under the image mutex, so the
  bytes become collectable. Expiry is age-based, measured from the latest stage (re-staging refreshes it). A payload counts as
  registered only when an image published by the same project and user uses its digest; another
  project's or user's registration neither frees quota nor reveals that the bytes exist. Re-staging a
  held digest is free. CAS bytes are never deleted by this path. The cap is evaluated after the request
  body is received (documented residual).

## [0.3.13] - 2026-11-08 (UTC; published to PyPI/GHCR and deployed to the three DMS Lab controllers)

Supersedes the 0.3.12 release candidate below: this version keeps 0.3.12's SDK publication and
native lifecycle work and replaces its standard-role authority implementation with the
reviewed thirteen-action reader/member/manager cutover. The 0.3.12 security fixes are retained
in the new implementation: member layer DELETE returns 409 instead of cascading another
project's grant (system administrators still bypass it by design), upload finalize, bundle
import and SDK/image-key publication revalidate the original subject immediately before commit
(after locks and payload copies), and both policy modules reject undefined or cyclic rule
references, including inside negations, and a missing configured policy directory with 503.
Hub does not follow role-implication links through domain-scoped intermediate roles; Keystone
already expands implied roles before the Hub sees tokens. Formal publication and deployment
are recorded separately in `docs/development-handoff.md`.

### Changed

- Make `public` published images readable without credentials, but only for
  `GET /v1/published-images`, `/{id}` and `/{id}/blobs/{digest}`; the anonymous
  projection omits owner, project and storage fields, and a present but invalid
  credential is 401 rather than anonymous. Graph, quota and write routes still
  require credentials. Anonymous reads are rate-limited per client address
  (`PALIMPSEST_HUB_ANONYMOUS_RATE_LIMIT_ENABLED/_REQUESTS/_WINDOW_SECONDS/_MAX_CLIENTS`,
  default 120 per 60 s, per process and node, IPv6 per /64).
- Deployment caveat: the limiter keys on uvicorn's resolved client address, so
  behind a proxy missing from `FORWARDED_ALLOW_IPS` every anonymous caller shares
  one bucket per proxy. The Kolla role now renders that variable from
  `palimpsest_api_forwarded_allow_ips` with the unchanged default `127.0.0.1`;
  operators must set the actual HAProxy sources.
- Accept `linux/arm64` manifests for `oci-root` publication (application stays
  `linux/amd64`); instance, native and image-job execution still refuse arm64 by
  the sealed manifest platform.
- Stage opaque blobs through `PUT /v1/published-images/blobs/{digest}` without a
  SquashFS magic check; layer payloads are SquashFS-checked at registration.
- Raise the unreleased local catalog to schema version 2 (`publications`,
  `pulled_images`, `pulled_layers`). Version 1 catalogs upgrade only on the
  first publication/pull write; catalogs with any other version-2 DDL, including
  the discarded round-1 tables, are refused. `store rm` refuses digests of
  pulled images.
- Use native reader/member/manager roles for Hub reads, creation/editing and
  management/removal, with hard native ceilings before operator policy.
- Allow original-token members to stop/delete only their own consumers and
  exclusive boot volumes, and reader-or-above to revoke only their own API keys.
  Key issuance and package/layer/export deletion remain manager-tier.
- Recheck creator/project authority after final provider observations and before
  lifecycle effects; retain private-row, exact native-profile and prepared-root
  request/provenance fences without requiring publication authority for cleanup.
- Refuse unsafe layer deletion across project grants or parent/base consumers.
  Preserve current membership, credential attenuation and shared system catalogs.
- Replace role-shaped capability aliases with action-derived feature booleans
  and policy actions. Bearer keys retain only five read/write actions and receive
  neither self-service nor management scope. Benchmark/smoke preflights require
  both publication and appropriate cleanup authority before consumer intents.
- Recognize native human `openid` federation in both ordinary and catalog token
  validators without uplifting delegated/reduced credentials. Regression changes
  are source-only and unrun; no release/deployment/IAM mutation is implied.
- Keep enabled native owner-home/project-domain validation independent of personal
  metadata, rejecting disabled or unavailable domains after the standard-role
  cutover. Parent-observed regression fixtures now isolate real policy settings
  and withdraw the actual project action before final effects/commit.
- Remove unused personal-owner helper, raw project provenance query and policy
  credential/result plumbing. Irrelevant partial personal listings cannot deny
  standard members; complete effective core authority and native domains remain
  mandatory, with a regression defining both behaviors.

### Added

- Add DB-authoritative published-image quotas: tier (`free|paid`) × visibility
  policies with nullable `max_images`, `max_chain_bytes` and `max_source_bytes`,
  and per-user/per-project tier assignment. Seeds only fill missing rows. Counts
  are distinct names per scope (private/public: user, internal: project); bytes
  are stored compressed sizes of distinct digests (GiB = 2^30), excluding an
  application image's original cloud image. oci-root charges every
  manifest layer including original OCI base layers (owner decision). Refusals are 403
  `image_count_limit`, 413 `image_chain_size_limit` and 413
  `image_source_size_limit`; image-job completion records them as a terminal job
  error. Lowering limits never deletes images, so limits do not cap total storage
  per scope or versions under an existing name.
- Add `/v1/admin/image-quota/{policies,tiers,usage,physical}` for fresh human
  system administrators and `GET /v1/image-quota/me` for the caller. The physical
  report counts each digest once per storage namespace and separates logical
  from physical bytes.
- Add `ppi_v1_` image API keys (`/v1/image-keys`): `images:read` and
  `images:publish` (publish does not imply read), SHA-256-only storage, secret
  shown once, at most 90 days, `PALIMPSEST_HUB_IMAGE_KEY_MAX_ACTIVE_PER_USER`
  (default 50) active keys per user. Only original human tokens manage them;
  each use revalidates current project membership. Package keys (`ppk_v1`) have
  no image authority. `migrate.py` copies keys, quota tables and source-closure
  rows.
- Add an optional OCI `source_closure` to `oci-root` publications. The server
  requires caller-staged blobs, manifest commitment and coherent OCI
  manifest/config/index documents (duplicate JSON keys refused, matching
  `diff_ids`), and serves closure blobs through the image blob route by image
  membership. Publishing a closure exposes image environment values in the
  config blob to every reader of the image.
- Add `palimpsest published push|pull|status`: durable request ids recorded before
  network use, `published` only after the Hub response matches, credential order
  `--image-key-file` (0600), `PALIMPSEST_IMAGE_KEY`, `PALIMPSEST_TOKEN`,
  mandatory `--confirm-public`, `--no-source` and an environment-exposure notice.
  Pull verifies payloads and the full OCI chain, rebuilds an OCI layout under
  `<state-root>/published/<id>/layout`, registers it as a local OCI source
  without starting a VM and prints a `plm run` hint.
- `published push` generations are owned by the credential that began them (public
  `ppi_v1_` key id; none for tokens): a credential that cannot read images replays
  only the Hub request id of a row its own image key recorded, retries its own
  unresolved attempt first, never starts a generation after a refused or uncertain
  replay (the message no longer claims nothing changed), and a 404 replay re-sends
  the stored request id instead of registering another image. Pull staging
  directories are swept only when nothing inside them changed for a day. Duplicate
  Hub images remain possible by design after a credential switch during an
  uncertain attempt, with two image keys of one user and with token credentials
  (see `docs/cli/usage.md`).
- Known open items: staged but unregistered blobs still have no aggregate quota
  or expiry; the Hub serves stored blobs without re-hashing (clients verify);
  MySQL/MariaDB quota SQL is untested; in-place bootstrap does not add
  `max_source_bytes` to an existing development quota table. Verification is
  synthetic-Keystone/SQLite tests and a loopback end-to-end harness only, with
  no live Hub, Kolla, MariaDB or Manila run.
- Cooperate with canonical run-name ownership for live native reads and share
  one five-second contended-wait allowance across the pass, without charging
  provider work or resetting it per run. Project one validated observation.
- Share only ongoing process-local native reconciliation between concurrent
  readers, with fresh waiter identity/generation/custody checks and no completed
  cache or reusable grants. Preserve independent legacy observations and stale
  safe discovery on failed joins.
- Sample dashboard metrics independently every five seconds with one joinable
  request, preserve rate validity guards, and avoid hidden VM log polls. Await
  already-started observations before mutations without pausing console sampling.
- Distinguish pure run/namespace mutex busy from uncertain custody HOLDs.
  Preserve stale/no-grant reporting, removed native retained logs across other
  Lima homes, and sanitized busy detail, console and lifecycle refusals.
- Keep admitted native log followers on their original pinned FD through pure
  mutex contention; preserve fail-closed snapshot/admission, strict identity
  failures and legacy boot-console reads without nested locks.
- Keep guest exec timeout independent of host preparation and fixed status
  actors. Use try-once run-name locks during native create/source custody
  without moving preflight/source guards or changing ordinary cleanup defaults.
- Correct CLI help/reference and runtime support guidance to include the
  explicitly configured, qualified Ubuntu24.04 arm64 Lima VZ profile; matching
  an arbitrary ARM image still does not admit execution.
- Separate native physical VM `status` from canonical main `runtime_status`.
  Preserve fresh pinned reconciliation independently of ambient Lima discovery;
  merge only same-home/full-identity aliases without borrowing generic access.
- Retain native dashboard exec through non-daemon drain, foreground detach and
  shutdown accounting. Select process-only versus required-mailbox-ACK completion
  from canonical dispatch identity; never fabricate native ACKs or hide unknown
  KVM ACKs, and always release completed registry entries after observation errors.
- Keep native VZ ordinary-stop settlement observation-only and crash-recoverable;
  recheck full stopped custody after the final resource reload, reject unknown
  start journals, and distinguish occupied-port admission from uncertain starts.
  Reconcile clean retained dev idle exits before a new boot.
- Fence native/ambiguous Lima identities from generic host force-removal and
  guest/detail transport, including stale cloud-image selections. Keep managed
  native access on its canonical runtime and preserve genuine owned cloud-image
  removal admission. Observed aliases cannot replace canonical OCI networks.
- Bind native catalog image links to consistent canonical source descriptors,
  CAS membership, snapshot/qualification and committed plan metadata without
  rehashing or decompressing source blobs during discovery.
- Keep canonical failed, unprepared VZ reservations visible in durable discovery
  when no native plan has been committed. Missing/null metadata is not provider
  absence or lifecycle authority; malformed committed plans remain fail-visible.
- Reconcile an already returned normal VZ stop without another stop or signal.
  Recognize only pinned, inactive, per-instance Lima2.2.1 default-gvisor socket
  entries under full original cleanup authority; reject replacement/reappearance
  and retain ordinary deletion plus absence-before-release. No manual unlink.
- Align inetd flags with the qualified password/PAM/plugin/syslog-disabled
  Dropbear build, and pin that exact executable in both asset build and load.
  Self-consistent version-marker counterfeits no longer qualify.
- Add the requested Packer golden-image build-inclusive comparison to canonical
  Goal M1-27: cold/reuse/source-change timing, readiness, evidence and amortization.
  This is a measurement requirement, not an executed benchmark or larger grant.
- Fix native readonly boot-identity injection through pinned file descriptors:
  harden a detached mount before FD-bound attachment, preserving inherited
  noexec/nosuid/nodev and covered-target provenance. Keep writable binds and the
  deliberate private control-jail setuid exception separate.
- Preserve bounded private Linux-worker stderr and observed exit status on
  HOLD, including stdout-first EOF, without public-message disclosure, retries,
  signals, marker removal or weaker ownership/total-output limits. Isolated
  root-run staging import checks explicitly disable bytecode with `-B`.
- Correct the offline asset-builder CLI description to require platform ABI2,
  matching the actual native runtime and manifest validator. No ABI1 alias or
  changed mount behavior is introduced.
  Preserve standalone source-path bootstrap with function-local product imports,
  resolving the pre-existing E402 lint failure without diagnostic suppression.
- Require a separate exact SIGKILL scope only after an original failed normal
  stop and spent unsettled SIGTERM. Preserve both histories; bind original
  PID/socket identities before delivery, reconcile only inactive original stale
  metadata after complete absence, and retain ordinary conditional removal.
  Revalidate directory/process/leaf/deadline authority at each unlink and forbid
  signal or metadata replay. No automatic force fallback or broad cleanup.
- Add separately scoped one-shot SIGTERM recovery for an exact failed-start
  actor after a failed normal stop. Persist signal intent before delivery,
  preserve the ordinary-stop failure history, and require fresh full shutdown
  proof before conditional removal. Default stop grants no signal fallback;
  uncertain writes, expired scope, changed identity or unresolved endpoints
  forbid replay and release.
- Retain live same-UID processes with an unavailable executable vnode path in
  native cleanup inventory. Preserve exact actor executable requirements, full
  metadata/argv stability and all related-process absence gates; never treat
  ENOENT from executable lookup as a missing process.
- Add concrete read-only Darwin process-birth/executable/argv identity and exact
  failed-start recovery scopes for canonical stop/removal. Preserve inconclusive
  errors as HOLD, original input pins, durable one-attempt and no-signal timeout
  fences, actor/network/upper exclusivity and absence-before-release. Actual n03
  historical carrier continuity still blocks operation; no waiver is implied.
- Report failed native guest mount source/target/type/flags and preserved errno,
  without exposing mount options or altering mount behavior. Real unprivileged
  failure probe passed; the actual n03 EINVAL call/root cause remains unresolved.
- Add an explicit typed ARM64 VZ candidate with canonical ordered lower/root
  ownership, isolated Linux-worker preparation and ABI2 static platform assets.
  Use genuine nonroot Lima bootstrap with separate root control and original OCI
  User; preserve exact process/session/timeout/StopSignal semantics. Verify actual
  create-produced boot inputs without re-creating them, pin private provider
  configuration files and fail closed on actor/custody or resource mismatches.
  Component probes and affected portable checks passed; actual VZ boot/root/SSH
  qualification remains open. Separate exact-owned failed-prestart destruction
  from historical carrier ctime only: preserve original pins, other seven fields,
  full current-read stability, original failure-marker pairs and every actor/
  consumer/absence gate. This does not restore boot/adoption authority.
- Install `plm` and `palimpsest` through one CLI implementation. Extend the actual
  offline package-install smoke to both executable names without changing command
  dispatch or published version identity. Native VZ run/exec acceptance is separate.
- Add one selected-root standard-library SQLite discovery catalog through existing
  state/inventory APIs, preserving source/reference/ordered occurrences, typed
  derivation receipts, builds/provenance, distinct labels/aliases and UUID/revision
  ledger observations without conferring execution, retention or Hub authority.
  `store catalog` reads persisted observations; `--refresh` explicitly recollects.
  Completed build/materialize CLI flows synchronize after durable producer commits
  and preserve successful output on late metadata failure. Actual VZ run linkage
  remains an open Goal acceptance item.
- Define reviewed local VZ actual-root and exact cloud-root/FROM contracts without
  claiming their implementation. Application roots bind the exact projected base;
  OCI roots preserve every original base/layer occurrence. Persistent upper reuse
  binds the run, owned attachment, filesystem and immutable chain.
- Add exact operator-selected upstream registry/namespace/repository admission,
  with missing/empty policy denying images. Reuse canonical registry resolution,
  apply admission before cached pin/offline reuse and build preflight, and carry
  an explicit policy to actual BuildKit source resolution. Namespace permission
  is not a verified-publisher/signature claim or a general network firewall.
- Add actual effective OCI `os-release` inspection and `oci qualify`, binding
  verified original graph/architecture/OS bytes to the reviewed Ubuntu24.04
  arm64 VZ direct-linux source profile. Source qualification never launches a
  VM or admits execution; the native runtime binding remains an open Goal item.
- Add a pinned original Ubuntu24.04 arm64 C/source/package fixture with exact
  stdout/report expectations, retaining sources and compiler for later VM checks.
- Apply context-root `.plmignore` to local Dockerfile builds using BuildKit's
  native ignore matcher after the selected Docker ignore rules, with temporary
  exact-byte definitions, no source writeback and effective-ignore cache identity.
  Remote-worker and commit support remain separate roadmap items.
- Extend explicit Linux-hosted filesystem materialization to validated ARM
  acquisition snapshots, retaining original ordered OCI occurrences and exact
  store receipts rather than flattening. Add `oci materialize --platform
  linux/arm64`; keep native amd64 identities/defaults and worker wire unchanged.
  Legacy KVM intent and persisted-root preparation reject ARM before effects.
  This does not implement VZ launch or confer source-profile execution authority.

### Fixed

- Isolate malformed catalog-source entries with explicit incomplete observations
  while retaining valid unrelated facts; keep normal `builds/outputs` out of the
  build-ledger namespace. Metadata discovery no longer creates a retained-instance
  lock, initializes absent state/config or probes live VMs. Corrupt/unknown DBs
  fail visibly rather than silently rebuild or fall back to another parser.
- Preserve verified source registrations across source-only refresh and state-root
  relocation, with graph/reference/order checks, independent destination identity,
  invalidated copied derivations and verified CAS rebinding. Persist the existing
  pre-solve context/Dockerfile hashes rather than recomputing changed input files;
  identify canonical semantic build-record hashes separately from raw-byte hashes.
- Preserve source-policy arrays in the existing main-config state-root writer.
  A real isolated regression failed with TypeError before the repair and passed
  afterward. Keep source configuration separate from cache storage authority.
- Recheck current operator source policy at actual worker payload/receipt links,
  registry catalog/cache commits and recovered Hub publication. Preserve immutable
  job provenance and any committed prefix on revocation; retain durable HOLD
  without replay or force-killing uncertain children. Use the existing SSH pipes,
  not a new service or credential.
- Admit retained upstream and Docker-store runtime sources before inspection,
  and preserve local Dockerfile grammar at the mandatory engine policy boundary.
  Require every running BuildKit node to enforce metadata resolution policy.
- Bound complete acquisition carriers and selected layout inputs before staging/
  promotion, including warm-CAS accounting. Initialize only source-CAS state
  without unrelated runtime cleanup or creation of an absent config tree.
- Bound synthetic-ancestor normalization before effective OS accounting, with
  entry/metadata budgets and cooperative checkpoints in the shared reducer.
  Include the new normalized-entry limit in the intake policy fingerprint.
- Accept successful optional-local cacheless OCI solves only when BuildKit emits
  an existing empty export directory. Record no cache archive and preserve the
  previous cache pointer; partial/missing exports and mandatory native cache
  still fail closed.
- Apply lower-only order-independent whiteout/opaque effects in the canonical
  v3 normalizer with deletion-dominated opacity, suppressed descendants and
  recreated-directory opacity. Preserve omitted parent/root directory metadata
  from the verified effective prefix and remove only generated opacity controls.
  Fix repeated unresolved forward-hardlink registration and share the effective
  reducer with the OS reader. Bind new derived recipe keys/cache views to exact
  preceding source descriptors/DiffIDs; retain historical read-only records.
  Authenticate warm prefix graphs and current worker contracts before reuse.
- Reject unsupported root `.plmignore` in CloudEnv remote context archives
  before packing/upload instead of silently ignoring user exclusions.


## [0.3.12] - release candidate, 2026-11-04 (UTC)

### 한국어

- 버전: client/Hub/Kolla0.3.12. 준비일:2026-11-04 (UTC). 정식 게시·운영 배포는 아직 수행하지 않았으며 각각 실제 완료 시각과 timezone을 별도로 기록한다.
- 변경·수정: 전용 Palimpsest 역할 없이 현재 native manager/member/project-admin이 자기 프로젝트 리소스를 관리하고 reader는 허용된 비밀 아닌 조회/다운로드를 수행한다. 정확한 custom/group/domain implication, 원본 token과 현재 소유자 교집합, 발급 key 범위·private/global 경계를 보존한다. 유효한 제한 credential은 실행할 수 없는 human 관리 기능을 capability로 광고하지 않는다.
- 보안 수정: 동일 digest를 다른 프로젝트가 별도로 업로드한 경우 member의 layer 삭제가 그 프로젝트 접근권을 cascade로 지우지 않도록 409로 거부한다. 업로드/번들/SDK publication은 lock·복사 뒤 commit 직전에 원본 subject(관리자는 target 재작성 전 원본 token scope)를 다시 검증한다. `not rule:missing`·부정된 순환·없는 policy 디렉터리는 503으로 실패한다. GHCR image tag도 root release와 같은 tag/version 일치 검사를 통과해야 한다. key 없는 version 출처 표시는 `original human token`이다.
- 전환 절차(필수): 새 ingress를 막고 원래 API가 살아 있을 때 전용 build worker를 정지·정산한 뒤 모든 API/export/upload/GC writer를 멈춘다. 같은 무-writer 구간에서 SQL+CAS+설정을 함께 백업하고 격리 복원을 증명한다(DB 기본 charset/collation 포함). 새 API image의 `palimpsest-hub-bootstrap`을 writer가 멈춘 상태로 한 번 실행한다. MariaDB DDL은 암묵 commit이므로 rollback은 transaction 취소가 아니라 writer 정지 후 짝지은 이전 SQL/CAS·설정과 정확한 이전 image 복원이다. 이전 worker를 새 상태 위에서 다시 실행하지 않는다.
- 최신 dev 전체 전환: 아래 미게시0.3.11 source의 SDK 이미지 publication/visibility/논리 layer/Manila 선택 경로와 기존57ab180 이후 repair를 포함한다. 별도 goal-m1 미커밋 변경은 포함하지 않는다. 기존 context PK·job visibility 변경, stopped-writer SQL/CAS backup/restore, API/export/build-worker 동시 cohort 전환이 필요하다. 옵트인 native/Ceph/GPU 실기 성공을 뜻하지 않는다.
- 폐기·제거: service-role-only 기본 허용과 personal metadata만으로 얻는 권한 fallback을 제거한다. 기존 Keystone 역할/할당은 삭제하지 않으며 실제 core implication은 계속 권한이다. 새로운 데이터/API 제거는 없음.

### English

- Version: client/Hub/Kolla0.3.12. Prepared:2026-11-04 (UTC). Formal publication and operational deployment have not occurred; record their actual dates/times/timezones separately.
- Changes/fixes: native manager/member/project-admin manage owned project resources without Palimpsest roles; reader performs authorized nonsecret reads/downloads. Preserve exact custom/group/domain implications, original/current-owner intersection, issued-key scope and private/global boundaries. Valid reduced credentials no longer advertise human actions they cannot execute.
- Security fixes: a member layer DELETE returns 409 instead of cascade-removing another project's separately uploaded grant for the same digest. Upload/bundle/SDK publication revalidates the original subject immediately before commit, after locks and copies (administrators use the original token scope before the logical-target rewrite). `not rule:missing`, negated cycles and absent configured policy directories fail with 503. GHCR image tags pass the same tag/version identity gate as the root release. Keyless version provenance is labelled `original human token`.
- Required cutover runbook: close new ingress, stop and settle the dedicated build worker while the old API is still available, then stop every API/export/upload/GC writer. Capture SQL+CAS+configuration from one no-writers interval and prove an isolated restore, including the database default charset/collation. Run the new API image's `palimpsest-hub-bootstrap` once while writers remain stopped. MariaDB DDL commits implicitly, so rollback is not a transaction undo: stop all writers and restore the paired old SQL/CAS/configuration plus the exact old images. Never run old workers over new state.
- Full latest-dev transition includes the unpublished0.3.11 SDK publication/visibility/logical-layer/optional Manila source below and repairs after deployed57ab180, but excludes separate uncommitted goal-m1 work. Existing context primary-key/job-visibility changes require stopped writers, paired SQL/CAS backup/restore and coordinated API/export/build-worker cohorts. Optional native/Ceph/GPU execution is not qualified by this source release.
- Deprecated/removed: service-role-only default authorization and personal-metadata-only authority fallback are removed. Existing Keystone roles/assignments are not deleted; genuine core implications remain authoritative. Other data/API removals:None.



## [0.3.11] - 2026-11-03

### Added

- Add one canonical image publication engine for worker output and locally built
  SDK pushes/extensions, authoritative Hub manifests and metadata discovery,
  parent-aware single-parent logical layers, immutable payload references and
  private/internal/public image membership with transitive reuse restrictions.
- Add namespace-qualified Manila/CephFS publication and exact MDS-path plus OSD
  pool/namespace read-only consumer credentials. Scope changes copy payloads;
  same-scope reuse remains content-addressed. Path-only isolation and
  cross-namespace hardlinks are explicitly rejected.
- Add an opt-in native SDK lifecycle with fast durable HTTP202 admission,
  fresh-original-caller background connections, project-scoped cumulative
  CPU-image grants, exact cleanup and SQL/private-journal uncertainty fences.
  Pair client/Hub `0.3.11` with an exact companion dependency. Local amd64
  container and database qualification are distinct from production deployment,
  actual namespace isolation and a new Nova warm-start proof.

- Add an explicit experimental `cloud native` candidate: separate BIOS carrier/initramfs PID1, authenticated loopback mTLS bootstrap with per-run restricted SSH transport, CephFS-backed actual-root assembly, separately pinned NVIDIA driver injection, and grant-bound Cinder-before-Nova lifecycle/cleanup. The standalone probe uses original-image cudart/cuBLAS plus Driver API computation. Source preparation preserves predecessor bytes; this entry does not claim an executed native boot or GPU proof.
- Add digest-pinned `cloud image-pull` on the Nova worker, with sequential layer conversion/publication/receipt commits, original OCI provenance, no-copy verified CephFS blob reuse, and durable no-replay intake identities. This does not implement or qualify native CephFS-root Nova GPU boot.
- Add local-driven `palimpsest cloud env up|status|down`, `cloud dockerfile-build` and `cloud layer-run`: the CLI uses a `clouds.yaml` cloud through openstacksdk to provision a Manila CephFS layer share and a Nova build worker (read-write cephx identity), builds a Dockerfile there with pinned BuildKit, converts it into ordered SquashFS layers published write-once to CephFS, then boots fresh consumer VMs with a separate read-only cephx rule that loop-mount the layers read-only under an overlay and run a command. No Palimpsest code runs in consumers; created resources are recorded per environment and deleted only by recorded ID.
- Add disabled-by-default API GPU profiles bound to original projects, exact qualified guest content, flavor/PCI aliases, driver and GPU count; use preinstalled NVIDIA vendor injection with preserved image identity and owned namespace cleanup. Actual GPU workload qualification remains blocked, not passed.
- Add one-job/two-consumer API cold/warm measurements with continuous cold timing, existing ordered-layer warm reuse, fresh writable state and explicit HTTPS-transfer/cache limits. Keep a fixed offline hash-pinned derived TensorFlow recipe and bounded streaming context upload; no GPU timing or deployment qualification is claimed.
- Add separately admitted two-node API k3s profiles, an external fixed offline-airgap build context, private expiring join capabilities and exact node/numerical-pod proofs. Cold/prepared pairs hold two live reservations and reuse one immutable image job; default CPU/GPU privilege is unchanged.
- Add project-bound registry imports, bounded Dockerfile/context builds and public image-job APIs with durable request identities, fixed HTTPS worker leases and verified atomic layer publication.
- Add original-human/project-bound instance create/list/detail/stop/delete APIs with provider-intent reconciliation, exact boot-volume checks, immutable ordered layer/runtime snapshots and bounded sealed guest results.
- Add conditional layer metadata PATCH, retained physical-blob references, matching Hub client methods and dedicated image-worker container/Kolla policies. Current guests fetch payloads over HTTPS into read-only SquashFS lowers with private disk-backed upper/work; Manila-managed CephFS runtime access remains a separate target, not implemented by this change.
- Add fixed PyTorch CPU/CUDA, NVIDIA CUDA, TensorFlow CPU/CUDA and rancher/k3s scenario programs with durable original-caller budgets, structured status/logs/reports and retained Hub instance/request identities, including uncertain provider creates. These source additions do not qualify new GPU or privileged-cluster execution.
- Add a separately budgeted private iperf3 peer/bastion helper with console-pinned SSH, exact-owned SG/FIP and forward-only cleanup. Bounded private-stdin authentication keeps original-caller credentials out of argv, environment and evidence files.
- Preserve the Korean visual guide and four interactive diagram artifacts byte-for-byte from the 2026-10-29 working-tree snapshot. README illustrations explain the external-control/Hub-metadata/Manila-managed-CephFS target, explicitly separate from current HTTPS bootstrap and historical Cinder prototypes; illustrations are not deployment or runtime evidence.

### Fixed
- Exclude non-distributed Finder `.DS_Store` metadata from deterministic worker
  source archives after actual Linux container qualification found editable and
  installed package fingerprints differed despite identical distributed code.
  Preserve the code-byte/grant pin instead of weakening the integrity check.
- Upgrade existing image-job visibility as historical project-wide `internal`
  before creating dependent tables. Preserve proven context creators in the
  creator-aware primary key; reject unknown creators before DDL, and quarantine
  legacy jobs lacking creator/OS declarations before worker effects. Schema
  cutover requires all writers stopped and a restorable paired SQL/CAS backup.
- Make copied public Hub source readable in all three runtime images without
  relying on root DAC capabilities; private configuration permissions are unchanged.
- Preserve inherited reuse ceilings through MariaDB's reflected JSON/LONGTEXT
  representation, binding decoded policy lists for the destination type. Reject
  blank or oversized UTF-8 OS declarations before image-worker admission.

- Consolidate the remaining local worktree features and port final allocation-expiry/no-send recovery without reverting prepared-root or paired-budget ownership. Preserve original staged work and historical evidence outside retired checkouts; no remote merge or release is implied.
- Fix imported cloud result registration, deletion replay, SSH isolation, bounded retained output and native tunnel/key-cleanup prerequisites. Controlled SIGTERM uses owned cleanup; experimental actual-root/GPU acceptance remains unqualified.
- Preserve sanitized, private, no-clobber Nova console evidence before failed experimental native-run cleanup; retain positive ACTIVE/enrollment timestamps and failure phase across revocation. Bound SDK connect/read timeouts through the actual session and restore transport settings even on cancellation. Diagnostic failure does not prevent cleanup, refund attempts or satisfy the CPU/GPU gate. This evidence-retention repair does not identify or qualify the failed native boot.
- Preserve fixed-operation/type/errno serial context for experimental PID1 failures without exposing exception messages, file paths, arbitrary class names or credentials. Keep labels compatible with the existing console sanitizer and preserve HOLD/result/gate semantics. This diagnostic-only candidate does not establish the cause or repair of the observed native boot failure.
- Add exact-approved recovery for cleaned native failures without resetting their outcomes or spent ledgers: bind predecessor preparation/run/history digests, recheck provider absence and stopped bootstrap service, and preserve preparation assets in separate source generations with unchanged kernel bundle/DNS. Reserve cleanup time inside the allocation and reject recovered over-budget CPU results as GPU prerequisites. Local verification does not qualify the undiagnosed native boot.
- Preserve private redacted carrier-builder stdout/stderr tails before failed pre-Nova cleanup, without interpreting an SSH exit status as remote-actor completion. Keep interrupted transports TERM-only and fail-closed source-identity checks intact; concurrent package-update evidence is not a guest-boot result or a refunded allocation.
- Give one-pass Glance carrier PUTs deadline-bound scalar socket waits without changing metadata defaults or shared sessions. Prevent hidden SDK mutation replay on connection/status failures, reauthentication and redirects through the original-caller factory; reject mutation3xx and retain only safe upload cause/progress fields. Observe existing deletion intents rather than resending, bind Cinder origin to its durable image intent, and refuse recovery with unreaped upload clients. Strict no-replay retains paid-resource HOLDs for separately authorized manual disposition; it is not an automatic cleanup guarantee.
- Recover prepared roots through original-project scoped Cinder inventory without the provider-rejected `project_id` query. Treat create responses as partial: bind a returned UUID only after an exact detail read verifies original owner, intent, size, image and no attachment; retain unknown POST outcomes without retries or Nova allocation.
- Prepare each official Hub instance root as one durable exact-project image-backed Cinder volume before Nova. Initial create is Cinder-only; GET never mutates providers, and explicit request/volume-bound advance dispatches the first volume-source Nova attempt or one deferred exact-owned volume deletion. Preserve original bootstrap expiry, private no-call/uncertain history, strict tri-state server-attempt certificates, mutex fences across COMMIT, bounded lost-response recovery and actual404-only cleanup without changing cloud wait settings or persisting raw credentials. Existing server-backed rows remain lifecycle-only; source/client cutover is not deployment or GPU acceptance evidence.
- Bound Hub asyncmy handshake/response reads with `DATABASE_READ_TIMEOUT` (default 30 seconds, valid 1–120) in API, export worker and offline bootstrap. Keep TCP connect/pool limits and readiness single-flight/closed-transport pre-ping semantics unchanged; do not pass MySQL-only connection arguments to SQLite.
- Retain published CAS bytes and resumable staging after an unknown final COMMIT outcome in legacy uploads/bundles, image contexts/outputs and native package/cache publication; do not mistake a fresh nonlocking absence read for rollback proof or overwrite a late completion through exception compensation. Preserve known precommit cleanup and propagate errors without blind mutation retries.
- Select a fixed fingerprint-bound 900-second service-image pack profile after an observed large-image packer timeout, keeping canonical callers at 120 seconds and preserving the original outer job deadline. Require profile/key agreement through request wire v3 without arbitrary timeout payloads, lease extension or failed-job replay.
- Preserve allowlisted OCI intake/packer/store error codes and validated resource/I/O errno through the strict materializer subprocess response and private executor summary. Migrate both ends to response wire v4 without exposing exception messages, paths or credentials or changing successful artifact, timeout or quota semantics.
- Resume image output publication from lock-serialized private receipts and exact result intents without re-executing builds, extending leases or replaying unknown mutations blindly. Preserve per-digest journal progress and private failure categories/status; wait for bulk CAS publication responses until the original absolute lease instead of a ten-second post-upload idle timeout. Reuse existing JSON columns, keeping pending image runtime private and public completion semantics unchanged.
- Require overlayfs snapshots on the image job's quota-bound ext4 scratch after a real copy-up/whiteout probe, avoiding native parent-directory copies without increasing deadlines or enabling fallback. Preserve actual failed probe return codes and bounded stderr through normal job disposal; uncertain mount cleanup remains fail-closed.
- Keep the image executor's BuildKit socket in its private `/run` tmpfs, independent of persistent job-path length. Retain only the latest image-worker failure in an atomic owner-only, bounded local artifact after exact executor cleanup, preserving trailing executor/BuildKit stderr outside mounted scratch and job deletion with fixed stage/category/errno diagnostics; public errors, database schema and journal recovery authority are unchanged.
- Settle lost worker claim responses through a durable channel/sequence cursor under the job-selection SQL mutex. Fence delayed original requests, retain exact-lease no-reexecution semantics, preserve legacy unidentified journals, and require explicit additive-schema bootstrap before coordinated API/worker cutover.
- Recheck expiry immediately after durable submission writes and after context hashing, before external effects. Bound slow/trickling dependency downloads by an owned-process absolute deadline, and release only definite no-call live occupancy without refunding cumulative reservations.
- Bind CUDA compilation to the validated device capability and reject execution mismatches using the kernel-written `__CUDA_ARCH__`, not echoed host compiler defines. Enforce cluster source expiry/pinned qualification, identity-safe child cleanup, streaming output limits, atomic simultaneous capacity and per-pair deadlines.
- Validate private flavor eligibility through the original caller's default available-flavor listing instead of requiring administrative whole-tenant ACL enumeration. Keep exact GPU profile/PCI/content checks and a fresh visibility fence before allocation.
- Reject cyclic build-context symlinks on Python 3.13 as well as older supported interpreters while retaining valid dangling internal links.
- Enforce a fully preallocated per-image workspace and packed-output budget with independent journal headroom; keep registry/tool temporary files in owned scratch and require exact cgroup/loop cleanup before publication.
- Require separate current download and write authority before instance payload capabilities; permit only verified failed-create cleanup and retain capacity while a bound boot volume survives. Distinct definite no-create errors can be explicitly cleaned without treating an uncertain provider response as absence.
- Bound guest result delivery by actual serialized JSON bytes, including escaped control output, while preserving identical-result retries and no workload replay.
- Serialize Kolla's writable-disk GiB limit as a string; bound workload argv/results before allocation, keep CPU threading identical across comparison methods and require completed cluster probe computation before readiness. Preserve uncertain resource dispositions and cumulative auxiliary-resource budgets rather than replay effects.
- Fence retained-station hourly/disk/create/repair/cleanup operations through one canonical station-wide lock, independent of checkpoint output paths. Retain failed attempts and per-output receipts; busy resident sampling returns a future wake-up without retry bursts.
- Bind external Lima removal to safe owned native configuration and protected-state identity; prove descriptor-pinned directory deletion even across rename. Revalidate libvirt native backend/lifecycle metadata and inactive TPM retention before effects, through existing `host_vm_control.py`; default root/topology retention, independent named disks and console authority are unchanged.
- Reuse the hardened private-stdin HTTPS/ambient-configuration boundary in the live Hub acceptance script. Persist exact consumer deletion identities before API effects and observe uncertain/deleting outcomes without issuing DELETE again; release budget only after original server/boot absence.
- Recheck allocation expiry/deadline before image-job requests and the retained station's separate data-volume request. Explicitly settle only synchronously certified pre-request station admission failures; missing or late effect receipts remain uncertain.
- Recover a completed disk checkpoint's latest pointer over an older result without repeating its probe, synthesizing a new timestamp or regressing a newer valid success.

### Changed

- Follow the owner's 2026-10-29 same-location measurement policy: reuse one retained 4-vCPU/8-GiB OpenStack station with a dedicated 500-GiB disk, pairing provider download/upload samples every 30–60 minutes during active experiments (default 60). Preserve individual provider values and equal-weight means; missing directions remain N/A. Workloads reference timestamped context instead of creating calibration VMs or running Internet probes per experiment. Historical matrices, failures and spent grants remain historical.
- Keep ordinary notebook development/checks and specifically approved operator administration permitted; only long benchmarks require a resident host. Current API/SDK-reference and standalone-calibration modes remain explicit, with no silent CPU fallback or automatic GPU driver installation. Publication, actual Kolla deployment and quarantined-worker disposition remain separate recorded actions.


## [0.3.10] - 2026-10-28

### Added

- Add explicit Nova cloud-init layer builds, project-bound asset registration, original allocation receipts and optional boot-volume type selection. These operator SDK workflows remain distinct from deployed Hub API builds.
- Retain original OCI sources and verified SquashFS conversions in a permanent layer cache, with project-bound asset graphs, canonical registry-worker jobs and a BuildKit-to-OCI asset adapter. Consumer release never deletes retained payloads.
- Export verified external-layer cloud-init with immutable read-only lowers and per-consumer writable OverlayFS state bound to guest, filesystem and ordered layer identities.
- Add explicit experimental `lima-krunkit` execution and compatible conventional named-volume support while preserving VZ defaults, shared Lima disk-name ownership and native OCI/carrier attachment restrictions.
- Add bounded disk/network environment qualification, OpenStack/workflow reporting and a private single-executor benchmark queue with preserved uncertain-job blocking and separate success/issue output.
- Expose artifact architecture/compatibility checks and filters consistently across local inventory, CLI, Hub transfer and dashboard metadata without claiming boot, ABI or GPU readiness.
- Add registered service-local authorization actions, operator policy configuration, native personal-project owner checks and fresh human system-administrator management with explicitly bounded issued-key authority.

### Fixed

- Enforce permanent retained-byte protection by physical SquashFS digest across distinct derivation recipes, preserving missing/corrupt retained targets rather than silently repairing them through another recipe.
- Keep OCI converter temporary spools on their verified private scratch; isolate capture mounts from peer namespaces and preserve strict normalized timestamps except for independently verified native captures.
- Preserve upstream tar type-bit semantics and current platform/root descriptor guards when acquiring retained OCI inputs.
- Select workflow environment qualification from the authoritative local/OpenStack mode rather than relying on an optional CLI-injected runner.
- Preserve typed console identity refusals when Darwin denies SIGTERM during PTY EOF exit: accept that race only after bounded leader reap and proof that its entire process group is absent; keep live or uncertain permission failures visible.

### Changed

- Reconcile unpublished public work onto released0.3.9, preserving the HTTPS-only service-VM build executor, shared-CAS/Sentinel readiness, catalog/unpack behavior and trusted journal-frame commitments. Private credentials and research control helpers are excluded from package bytes.
- Synchronize client/Hub package identities and packaged Kolla image defaults. External API build/consumer acceptance and an actual Kolla service rollout remain distinct from package publication and deployment-server pinning.
- **BREAKING (Hub credentials):** ordinary artifact/package management and system administration now require original human-authenticated subject tokens; application credentials, Trust/delegated tokens and service identities are denied for those human control paths. Automation uses explicitly issued attenuated package keys where supported, not elevated OpenStack credentials. System administrators are freshly revalidated without fabricating target membership; project-local policy replaces leaf-only defaults.
- **Compatibility:** artifact listing/check filters use canonical `x86_64`/`aarch64` names, layer inventory is SquashFS-only by default, and old chains without declared layer architecture remain unknown/not runnable rather than inheriting a base target. Metadata compatibility does not replace runtime preflight.
- Revalidate administrative authority and required actions after blocking CAS publication and immediately before SQL mutations; preserve rollback, retained references and unregistered-blob compensation if identity or policy is revoked during the request.
- Preserve publication-only repeat uploads without metadata disclosure or a fabricated digest shortcut, and report terminal Nova guest failures/uncertain registration with typed retained-resource errors under the original fixed deadline.

## [0.3.9] - 2026-10-27

- Reject rewritten non-tail supervisor output even when size/mtime/ctime are unchanged and its payload checksum is recomputed. Keep compact trusted per-record chain seals from startup verification and successful own durable appends; compare each requested frame before returning it.
- Preserve one-frame incremental I/O, descriptor/path binding, permanent corruption latching and the existing journal format. Memory is O(record count), 32 bytes per record plus bytearray capacity overhead; cursor metadata checks are not whole-history audits of unrequested payloads.
- Make foreground web shutdown handle SIGINT even when inherited as ignored from its launcher; main-thread serving explicitly installs the existing stop callback for both SIGINT and SIGTERM and restores each actual original disposition on exit. Preserve deadlines, browser threading and socket/transport cleanup.
- Make timestamp-change regressions deterministic and replace incidental operation-count assertions with bounded-byte and ordered consumer-output contracts. Prepare root/Hub metadata, generated locks and Kolla image defaults for patch 0.3.9; publication and production qualification are separate.

## [0.3.8] - 2026-10-26

- Add SQL-only visible named-leaf asset discovery and separately authorized global administrator layer ancestry/provenance catalog. Preserve the current0.3.7 project/package/publication authority; only global catalog reads use fresh human/system graph validation and targetless native `system:manage` policy.
- Add bounded literal package/tag search with query-bound cursors while preserving existing no-search cursor identity, package scope and immutable provenance/schema.

- Add local `bundle unpack ARCHIVE --output DIRECTORY` through existing atomic verified extraction; accept only canonical empty native directory markers, preserve digest/size/path checks, and reject unknown or conflicting runtime architecture rather than assuming x86_64.
- Preserve accepted native packages whose independently bound config omits optional `blob_digest`, without accepting explicit mismatches or guessing unannotated ancestor architecture. Exclude descendant-only/conflicting base dependencies from complete catalog eligibility while retaining reusable no-base chains.
- Prepare source-only metadata with isolated portable, real synthetic-authority/SQL HTTP, CLI-unpack and local amd64/arm64 image qualification; repair extracted import/format gate failures. Publication, independent source review, native reviewer gates and production cutover remain Main-owned; no deployment or native-runtime success is claimed.

## [0.3.7] - 2026-10-18

- Support multi-controller API/export deployment with an explicitly qualified shared CAS. Reject multi-host local volumes and missing, aliased or mismatched mounts before bootstrap/each writer start; never provision or chown shared storage, and preserve stop/pull/destroy during mount outages.
- Discover the current writable Redis primary through Sentinel instead of pinning a replica-prone seed. Preserve URL data credentials/database/options, separate discovery authentication, bound HA socket waits, close owned clients and never replay an ambiguous write or fall back to a seed.
- Sanitize malformed credential-bearing Redis URL parser errors and raise the redis-py minimum to5.0.1 for `aclose()`; preserve the native lock's Redis8.1.0 resolution.
- Retain the dedicated HTTPS-only build executor, SQL job/maintenance ownership and native/GPU qualification boundaries. Scope the canonical Hub Docker context to required public build inputs, excluding private proof environments and credentials.


## [0.3.6] - 2026-10-17

- Publish the owner-approved full feature release as `v0.3.6` and deploy all three immutable Hub images through scoped Kolla lifecycles. Verify paired SQL/CAS restore, offline bootstrap, direct/public readiness, authenticated local CLI upload/download bytes and restart persistence; preserve shared loadbalancers and native qualification boundaries.
- Add unauthenticated dependency readiness at `/v1/ready`, separate from backward-compatible process liveness at `/v1/health`; return sanitized database/Redis/blob-store status with HTTP 503 on failure. Wire API container health and HAProxy probe templates to readiness; the existing shared loadbalancers were not reconfigured in this rollout.
- Default the local client and dashboard to `https://palimpsest.dmslab.re.kr`, preserving explicit configuration overrides. Show operation-stage motion without invented percentages and honor reduced motion.
- Acquire verified OCI archives for both Linux amd64 and arm64 independently of native runtime eligibility; expose explicit experimental cross-architecture VM-carrier execution without weakening native OCI-root platform guards.
- Bind Docker download run receipts to the retained OCI root index while recording the selected child separately. Propagate explicit forced acquisition and the requested platform through Docker-cache exports, validate exported amd64/arm64 configs, and preserve native runtime preflight for normal runs.
- Reject QCOW2 carriers with external data files before any VM launch, matching the existing self-contained backing-file guard; digesting the carrier alone cannot authorize mutable external data.
- Clear only an exact matching removed-run archive pointer when deleting its retained root from the dashboard. Preserve run/CAS references until explicit CLI purge, use already-run-locked deletion in adapters, and reject ambiguous native persistence/inventory and aliased reattachments.
- Accept a single authenticated removed→removed lifecycle revision when repeated default removal re-archives surviving owned root bytes; preserve run identity, original cursor and final durable-state validation instead of reporting failure after successful retention.
- Add an explicit connected catalog to the local web dashboard: tab-scoped original-token Palimpsest Hub identity, filtered image/layer inventory and complete dependency details, plus real Docker Hub repository/tag/platform/config/layer inspection through the local client.
- Keep metadata independent of Docker/Skopeo, credentials out of browser storage/global environment/operation receipts, and verified downloads behind `--allow-control`. Selected Docker platforms download the immutable child manifest without losing the pinned parent index used for inspection.
- Verify bounded Docker manifest/config metadata and permit only one anonymous allowlisted config-CDN hop; reject foreign child digests, insecure redirects and corrupt descriptors. Preserve canonical acquisition/runtime platform restrictions.
- Default unconfigured dashboards to Docker Hub, reset provider-specific queries and changed-filter pagination, preserve platform keyboard focus, and expose the verified VM-setup form without leaving it behind a detail modal. No VM is launched by a download.
- Choose Docker downloads from the verified selected config/manifest platform, or the actual host architecture for an unselected multiarch root, never an arbitrary sibling. Submit forced VM-carrier runs in foreground with disposable roots; reject explicit retained roots in force mode while keeping native and conventional runs detached.
- Enable exact-name/native-identity confirmed removal of observed Lima VZ/QEMU/krunkit and libvirt VMs without adopting foreign state. Stale same-name ledgers no longer disable a verified native selection; host-host ambiguity still fails closed.
- Retain conventional instance root bytes, firmware/native configuration and ordered layer/connection topology by default. Explicit instance purge removes only safely owned state, never independent data disks or shared layers; external libvirt disks remain reference-only.
- Add standalone named ext4 data-volume create/list/delete commands and dashboard controls, with exclusive conventional VM attachments via `--volume NAME:TARGET[:ro]`. VM removal releases disks for reuse without reformatting; stopped VMs keep reservations. Native OCI-root/carrier attachments remain explicitly unsupported.
- Protect referenced layers even after VM removal using retained topology; force cannot bypass dependencies, invalid metadata blocks deletion, and destructive HTTP actions require exact-name/digest confirmation.
- Bound process-output selector waits so timer-thread cancellation can finish guest probes on macOS kqueue after output descriptors close; preserve genuine read errors and VM lifetime.


## [0.3.5] - 2026-10-14

- Integrate all preserved public local/client and Hub WIP on the 0.3.4 base `ad1674e842c6cefaf4e2d38705b109a579f56040`, synchronizing root/Hub module and package versions, native-generated locks and packaged Kolla image defaults. Fresh qualification is recorded below; inherited results remain bound to their original snapshots, not this release.
- Retain the HTTPS-only private build worker and direct executor. Move distributed single-flight ownership to the API SQL transaction, keep fixed persisted deadlines and status-only settlement, and never resurrect shared-SQL KVM worker APIs or replay uncertain recipes. Worker-local journal/process/loop cleanup remains exact-owned and retains ambiguous scratch.
- Preserve renewable SQL export-maintenance and Trust-cleanup roles, cancellation drain, generation/expiry publication fences and ownership-aware scratch collection. Keep SQL-side timestamp synchronization and naive UTC normalization. Translate obsolete build consumers to HTTPS API claim/status/publication tests; keep opt-in real MariaDB/process ownership coverage distinct from synthetic guest/provider effects.
- **BREAKING (local CLI):** replace `palimpsest ui` with foreground `palimpsest web`, without alias or daemon. Keep ordinary CLI/install headless, independent browser launch, signal shutdown, `--no-browser` and read-only default with explicit `--allow-control`.
- Expose successful local BuildKit OCI exports and non-editable provenance offline; add guarded CAS tag editing and stable form focus during refresh. Support local cache mode without Hub credentials, inline Dockerfile builds, asynchronous web build/run operations and cancellation-owned prelaunch scratch/helpers without VM STOP.
- Resolve default run inputs through cloud CAS/tags, retained local OCI/package/BuildKit bindings and all eligible Docker names before registry acquisition. Reject conflicting Docker IDs and non-absence failures, preserve exact authority selectors, Docker helpers/custom CAs and digest-pinned complete indexes. Accept a top-level local OCI `index.json` pin independently from its selected manifest.
- Add identity-bound PTY/non-TTY consoles with bounded replay, typed exit/signal handling, capability-aware input and cancellation-safe disconnect. Preserve the original OCI result consumer through drain/ACK; verify marked external Lima identity without adopting lifecycle authority.
- Discover host Lima/libvirt VMs, disks and networks without state adoption; keep stale/replaced resources read-only, capacity/logical/allocated bytes separate and backend diagnostics private. Add aligned wide-character CLI tables and compact web lists, tabs, reduced-motion transitions and bounded open-detail journals/resource counters.
- Retain cloud-image-only web removal with typed-name confirmation and retained-files default; OCI-root web removal stays disabled. Preserve legacy/native/Docker Hub remote browsing, search, verified transfer and publication. Catch only peer response cancellations, exclude incompatible native backend probes, and keep genuine application failures visible.
- Preserve latest direct system-all administrator lookup, independent ordinary legacy publication roots, native elevated-owner/key attenuation, reader manager exclusion and requester-only deferred Trust execution. Keep all live credential, IAM, shared-CAS, writer-drain and paired restore prerequisites; the private `test-openrc.sh` input is excluded from release bytes.
- Complete parent-run local root qualification: full lane **7,064 passed, 264 skipped**; lane manifest, Ruff lint/format, OCI fixture and generated CLI-reference checks passed. Build and smoke-install the `palimpsest_client-0.3.5` wheel/sdist; Python 3.11 CLI version smoke reports `0.3.5` (not a full Python 3.11 suite). Hub suite **559 passed, 2 skipped**, with Ruff lint/format and `palimpsest_hub-0.3.5` wheel/sdist builds passing.
- Record parent fixes before qualification: nine import-order/unused-import lint fixes, formatting of 38 root and ten Hub files within integrated/version-metadata paths, and a test-only local-cache fixture repair supplying a context Dockerfile with output outside that context. Production validation is unchanged.
- Fix a `palimpsest web` startup hang found by the protected-dev PR's macOS 15 hosted checks: `http.server.HTTPServer.server_bind` resolved `socket.getfqdn("127.0.0.1")`, which blocked for more than 15 seconds before the URL was printed (captured with a faulthandler stack on the runner). The loopback dashboard server now binds without that reverse lookup; a regression test fails when bind performs reverse DNS.
- Exercise disposable loopback MariaDB 11.4 ownership: **2 passed** with two and three real SQL/process workers and independent flocks; remote execution/provider effects remain synthetic. Build and execute all three canonical Hub images for declared `linux/amd64` under local emulation: entry-module imports and Hub `0.3.5` on `x86_64`; build-worker also carries client `0.3.5`, `bwrap`, `mksquashfs` and `mkfs.ext4`.
- Release through [PR #24](https://github.com/openstack-afterglow/palimpsest/pull/24): all six strict required hosted checks passed on `1ff87efe`, protected `dev` merged as `04d51ad6e1bf00c4b6e5bf82050383d0bb0a10cf`, and dev Test, Hub images and Development package CI succeeded. Annotated `v0.3.5` completed release verification, PyPI publication, GitHub release and GHCR Hub image publication; all three published Hub images executed under amd64 with `--network none` and successful module imports. Production Hub API/export worker on `dms-controller1` and the dedicated build-worker VM were upgraded after the terminal gate and cold backup/restore receipt (15 SQL tables, 10 rows, exact; encrypted blob archive 884,766,720 bytes). The worker resumed polling after evidence-based `claim_pending` disposition with its journal preserved; future maintenance must stop/drain it before the Hub API. **Native KVM proof was NOT performed: skipped by repository policy exactly as for 0.3.4.** BuildKit/cloud proofs also remain unqualified; publication and idle-worker recovery are not live-build acceptance. Hub has no migration ledger; bootstrap `create_all` and empty disposable MariaDB are not existing-schema compatibility proof. See [CI, publication and production receipts](docs/testing.md#full-source-035-integration--local-verification).


## [0.3.4] - 2026-10-14

- Prepare an unpublished candidate on published 0.3.3, synchronizing root/Hub metadata, module versions, locks and the Kolla image tag. The owner-approved scoped iterative release loop covers Hub build/upload/consume fixes through protected dev PRs, required checks, immutable patch tags, publication, operator pinning and Kolla deployment; main promotion and CI protection changes are excluded. Native qualification remains pending until separately recorded.
- Replace the co-located KVM build plane with an HTTPS-only worker in a dedicated CPU-only Nova service VM: no nested/per-build KVM, libvirt, controller recipe execution, shared SQL/CAS/NFS or worker DB/Redis/Keystone credentials. Keep system-admin/project-scoped `/v1/builds`; require `PALIMPSEST_HUB_BUILD_WORKER_KEY` and remove the builder-interpreter setting. Add single-active lease/deadline claims, revalidated digest-bound inputs and private project output finalization without a schema migration.
- Run the direct executor with read-only verified base/parent mounts, bounded tmpfs, network-isolated bubblewrap and size-limited SquashFS output. Journal claim/execution/settlement phases, never replay uncertain claims or re-execute recorded jobs, and retain scratch when exact process/loop cleanup cannot be established. The privileged VM is the kernel boundary, not hostile-kernel isolation; the request watchdog cannot bound DNS/connect establishment.
- Seed every newly built layer's root inode from the base image `/` in both the local guest builder and the direct Hub executor. Earlier builders published their private `0700` capture directory as the layer root, which masks the merged root for non-root consumers; existing immutable layers keep that metadata, while children built by 0.3.4 no longer inherit it. Recovery cleanup identifies owned loop devices by the kernel's backing device/inode (`LOOP_GET_STATUS64`) instead of sysfs path text, which changes once the executor's private mount namespace dies.
- Add the `palimpsest-hub-build-worker` Docker target and opt-in disjoint Kolla worker group, root-only keyfile, host-local service selection, resource bounds, optional hostname pins and packaged worker lifecycle playbook. The Afterglow aggregate does not yet target that group. Run reader provisioning through `kolla_toolbox`; document durable `99-palimpsest.yml` operator overrides.
- Allow legacy image/layer uploads by system administrators only as ordinary current-project publishers: require current membership and `palimpsest-publish_editor` from ordinary assignment roots, excluding builtin admin/manager roots and implied copies. Native owner policy stays separate and excludes elevated owners, including fresh direct system grants; deferred exports retain that native owner policy. These entries describe source contracts, not completed candidate publication, deployment or live build acceptance.

## [0.3.3] - 2026-10-11

- Extend the authenticated Hub console with original-token capability context (`GET /v1/me`), bounded image/layer pagination and metadata filters, managed image/layer upload/download/delete and build submission, and separate memory-only native package-key browsing/publication. Legacy Keystone tokens and native package keys never share an authentication domain.
- Keep repeat uploads consumable by publish-only CLI/console callers: take the existing-digest descriptor shortcut only with inventory authority; otherwise verify the ordinary upload's bytes and metadata without descriptor disclosure. Reject native keys before a legacy identity request and release missing upload sessions without assuming unacknowledged finalization failed or automatically retrying publication. Preserve the fresh direct system-admin lookup, manager exclusion, Trust cleanup generation fencing and UTC lease handling from 0.3.2.
- Synchronize root/Hub metadata, module versions, locks and packaged Kolla image default at 0.3.3 without changing dependencies, schemas, local runtime code, native binaries or CI policy. Formal publication and Kolla deployment remain separate approval/verification gates; this candidate is not the earlier dirty 0.3.1 management image.

## [0.3.2] - 2026-10-09

- Recognize directly assigned Keystone system administrators through a fresh separate reader lookup without project-oriented effective expansion. Preserve exact subject/system-all/admin checks, original token scope, fail-closed failures and independent ordinary service authority. Regress recognition and immediate revocation through the installed SDK and real HTTP; synchronize root/Hub metadata, locks, modules and packaged Kolla image tag without dependency or schema changes.
- Registered production-app loopback smoke with synthetic Keystone and fresh SQLite verified original-project-only administrator reads (200), immediate revocation despite project-admin token (403), and missing-token denial (401). This is not real cloud/KVM or production-reader policy proof.

- Complete GitHub Release creation after successful permitted root publication when native qualification was explicitly disabled. Keep cancellation and failed/skipped/missing publication fail-closed; preserve upstream verification and native-policy gates. The immutable v0.3.1 tag remains unchanged, and the missing release was repaired using its original hash-matched distributions.

## [0.3.1] - 2026-10-08

- Publish immutable `v0.3.1` (`1458db42a1449a25b664584d144d0a97086f8f6f`) root wheel/sdist to PyPI and [GitHub Release](https://github.com/openstack-afterglow/palimpsest/releases/tag/v0.3.1), with separate revision-matched Linux amd64 Hub API/worker images. Exact-commit Test, Hub-image and root-publication workflows succeeded. Native qualification was explicitly disabled, not passed. Production rollout remains held for reviewed requester-project service grades, authentication, shared storage and recovery prerequisites; root installation does not deploy the Hub.
- Exercise the actual unmodified published Hub digests with canonical MariaDB bootstrap/API lifespan, real Redis and conversion worker, and synthetic HTTP Keystone/Glance: 184 package/persistence, 26 delegation and 27 independent cache checks (counts include setup/repeated checks). All four exact-owned fixture sets were removed. This amd64-under-arm64 smoke does not claim native amd64/KVM, real cloud policy, BuildKit or installed client TLS/helper transfer proof. See [current release evidence](docs/testing.md#published-031-verification).
- Release qualification on this tree (2026-10-08, macOS arm64, Python 3.12): OCI fixture, CLI reference and test-lane manifest checks; package/CLI-registry selectors 131 passed; root Ruff check and format; `scripts/test_lanes.py run full` 6,435 passed with 261 opt-in skips (skips are not native/KVM qualification); `scripts/build_package.py` built and install-checked the `palimpsest-client` 0.3.1 sdist/wheel; Hub locked sync, focused auth/package/export-delegation/migration 124 passed, Hub Ruff check/format, full Hub suite 298 passed and the `palimpsest-hub` 0.3.1 sdist/wheel built. Release integration only reordered three function-local imports in `scripts/smoke_package_capabilities.py` for the root Ruff isort rule. Native KVM, cloud builds and production rollout are not exercised.
- Freeze the full source candidate from latest origin/dev `63f6f665e7f7f7469f384edd49619fa5d6683284`, retaining PR15 native opt-out and PR16 descendant fixture ordering, all 32 original tracked deltas and four untracked change groups. Reuse prepared 0.3.1 only after observing no stable origin tag or GitHub release and PyPI 0.3.1 returning 404. The preparation itself ran no verification, publication or deployment; historical results below apply only to their recorded source snapshots.
- Normalize stored UTC export leases before soft-delete comparisons and fence Trust-cleanup completion by its claimed generation, preventing an expired worker from overwriting a newer cleanup retry. Reject elevated manager authority on the separate system-reader validator. Add test-defined actual-model migration, attempt-exhaustion, requester-replacement and lease-race coverage; these new regressions were not executed during preparation.
- Require only `cache:write` for standalone native client cache upload, preserving independent read/write leaves and exact scope/current-owner attenuation. Online BuildKit still requires both cache actions. Add unexecuted client/CLI/Hub regressions; the existing container HTTP smoke does not execute cache transfer or installed native CLI/TLS/helper flows.
- Disable automatic native KVM CI attempts under the owner's 2026-10-07 decision. Test and Release accept enabled/success or explicitly disabled/skipped, and label the latter as no native proof rather than qualification. Preserve ordinary validation, trusted contexts, strict hosted checks, environment protection, pinned inputs, receipts and cleanup. Release publication explicitly rejects cancellation and failed verification while handling the intentionally skipped native dependency. Existing main/tag sources require the workflow cutover; no formal release tag or native VM is requested by this change, and ordinary dev publishing retains its existing gates.
- Make the existing real subprocess descendant-cleanup regression reliable on macOS: record the spawned descendant PID before exposing an oversized archive to the bounded-copy monitor. Retain production behavior, timeout/size limits, both failure cases and the descendant-kill assertion; the observed PID-file race is fixed rather than hidden by retries or weaker CI checks.
- **BREAKING (Hub export worker):** run deferred Glance image exports only through a bounded Keystone Trust created by the requester at admission. The Trust has requester trustor, service trustee, the token's project, impersonation, least configured roles (default `member`, which Glance's default `download_image` policy requires) and a finite expiry. Remove the service-password connection scoped to the tenant project. The worker revalidates current enabled user/project, publish authority and delegated role, then verifies trust-token scope before Glance I/O. Revoked, expired, swapped or undelegated (pre-cutover) exports fail terminally without fallback. Track Trusts in the additive `palimpsest_image_export_delegations` table. Terminal, deleted, exhausted and abandoned exports trigger durable cleanup, and cleanup failure never reruns an export. Add `PALIMPSEST_HUB_EXPORT_DELEGATED_ROLES` / `PALIMPSEST_HUB_EXPORT_DELEGATION_TTL_SECONDS`, matching Kolla defaults and a precheck, and validator inputs for the worker container. Native package original-token/key planes and local KVM/build domains are unchanged. Synthetic-Keystone HTTP regressions cover these boundaries; see the handoff for executed checks. No deployment was run.
- Enforce exact Palimpsest service leaves resolved through current unique-global Keystone role IDs and the actual implication graph. Separate metadata from content download, require publisher/key-editor/key-admin authority for their respective actions, and attenuate existing keys after role/graph downgrades. Preserve owner/project/package isolation, key-only package publication and verified global builder/GC boundaries. Add isolated real HTTP Hub/Keystone smoke cases; checks were not run during integration.
- Exercise the registered production Hub app in isolated arm64/amd64 containers: 182 checks per architecture cover original/resumed bytes, persistent SQLite/CAS after restart and recreation, publish-without-download, scope/revocation/current-owner attenuation, real Redis export tickets and verified-system-only builder/GC gates. Retain the canonical SQLite bootstrap failure separately; production database code, dependencies and Dockerfile remain unchanged. Align native CLI pull preflight with its inventory-plus-download route requirements; frozen relevant root tests passed 309 cases. See the scoped registry acceptance record; no cloud/KVM build, production rollout or Afterglow integration edit is claimed.
- Prepare root `palimpsest-client`, independent `palimpsest-hub`, their module versions/lockfile entries and packaged Kolla image-tag default at 0.3.1. Preserve unrelated dependency resolution, native stage-1 binary/protocol, runtime and Afterglow tracking baseline. This patch preparation supersedes the 0.3.0 candidate metadata below; it does not claim a formal 0.3.0 release.
- Fix the oversized partial-native-evidence test fixture to supply its declared regular-file payload to `TarFile.addfile`. Final Python 3.13 portable verification exposed the missing file object before the production archive validator was reached. The fixture now exercises the intended oversized rejection without changing acquisition, extraction policy or native receipts.
- Diagnose actual main native run `37185453150`, job `111386304833`, step 7: both pinned acquisition URL inputs were empty and the HTTPS guard failed before either download. Read-only repository/environment inspection confirms the two URL variables are absent. No source defect or network/checksum failure is established. Exact pinned kernel/config hosting and owner-authorized variable configuration are blocked prerequisites; retain both hashes, HTTPS enforcement and required native gate without replacement/fallback.
- Document effective strict dev required checks, separate release-native owner approval, root-only PyPI/GitHub publication, and separate Hub API/worker image publication. Stable semver tags also update `latest` through metadata-action defaults; GHCR's existing own-Hub-test-only gate is not native success. No push, tag, environment approval/configuration, publication or rollout is performed by this preparation.
- Keep Kolla source mode pinned to separately reviewed immutable `c4887f7806608e98f215abbd377d2eafe159ff76` (Hub 0.3.0), not the uncommitted candidate. Source-mode 0.3.1 deployment requires the separately reviewed committed candidate SHA; image-mode deployment requires both published 0.3.1 image digests. See [release gates and failure evidence](docs/testing.md#release-preparation-and-publication-gates).
- Fail closed when deriving the public HAProxy hostname: accept only an exact HTTPS hostname origin with an optional trailing slash and disable empty-derived public maps even when a HAProxy-only play bypasses role prechecks. Preserve explicit overrides and opt-in exposure; add real Ansible rendering/precheck behavior regressions. Ansible Core is a development-only dependency; root runtime dependencies remain empty.
- Include the pending host-directory share lifecycle and SYSTEM direct-Nova operator tooling described below in the 0.3.1 dev candidate; this is not a stable-qualified release or a new native/cloud proof.
- Bind execution fingerprints to absolute host-share sources so relocated projects recreate instead of retaining old exports. Preflight recorded virtiofsd before every ordinary stopped-service start and before mixed-plan mutation. Reject writable shares exposing attached immutable base/layer artifacts in project preparation, direct dispatch/run and applied restart.
- Harden SYSTEM ownership and cleanup: require SDK-normalized enabled port security, durable keypair public-key identity, fail-closed RGW presence and pre-tag bucket receipts, timestamped exact-share Cephx access reconciliation, and recorded first-boot installation identity across phase resumes. Preserve unresolved objects/bindings while still cleaning independently ownership-verified resources. These portable regressions are not live cloud qualification.
- Patch the Hub's reported Starlette/urllib3 advisories with the first FastAPI release supporting Starlette 1.x (`0.133.0`), explicit Starlette `1.3.1` and urllib3 `2.8.0` pins, and a narrowly refreshed lockfile. Add an actual bundle-import HTTP regression rejecting excessive URL-encoded fields before identity/backend work. This is a Hub dependency cutover, not native or production qualification.

## [0.3.0] - 2026-10-02 (candidate, unpublished)

Root `palimpsest-client` and independently packaged `palimpsest-hub` both prepare 0.3.0 for the native project-package registry. This supersedes the unpublished root 0.2.4 preparation; the Kolla image-tag candidate also becomes 0.3.0 and its immutable source default points at the reviewed candidate commit. Verify both image publications before deployment. No tag, PyPI release, GHCR publication or deployment is claimed.

### Host directory share lifecycle

- Separate bind-source policy parsing from physical directory validation. `compose config` retains live-path validation; cleanup and observation can load projects after a bind source disappears without following or creating it. `up` validates replacement sources before mutation, while missing old applied sources still retain lexical cross-service reservations. Named-volume preservation and owned-only deletion are unchanged.
- Retained running services (`up`, `up --no-recreate`) now fail when their applied bind source is missing, without resolving or probing launch prerequisites. Restart refuses a libvirt domain whose filesystem exports differ from the recorded applied set, and malformed recorded share metadata is a state error. Guest project commands, the bootstrap marker and readiness now require successful layer/share activation.

### SYSTEM direct-Nova proof tooling

- Add `scripts/package_native_afterglow.py` (pinned Afterglow SHA plus explicit native overlay allowlist, private-file exclusions) and `scripts/run_openstack_system.py` (member-only, exact-capped, receipt-journalled build/run/cleanup proof requiring a separate source-approval receipt and a single active experiment binding). These are operator tools, not a Palimpsest OpenStack backend.

### Extracted Hub tracking-contract cutover

- Retain Afterglow `main` tracking and review baseline `2862565c1f59eac0ef0904b97c905b177f12b335`; track standalone Hub `/v1` routes, models and bundle services as authority and Afterglow's authenticated BFF as consumer. Local build/runtime and Union ownership do not change.
- Replace incidental facade/client/runtime file pins with reviewed structural boundaries while retaining separate upstream digest/lineage hash checks. Require upload status/offset and caller scope, reject reintroduced embedded Hub ownership, and preserve digest/parent-chain and stricter local importer contracts.
- Remove the obsolete source-copy protocol-gap test rather than repinning it. Retain independent hash, required/forbidden-marker and absence-rule checker regressions; do not generate test fixtures from the manifest's own expected markers. Actual checkout validation and existing client/Hub behavioral gates remain separate from authentication/native/deployment proof.

### Project-scoped native packages

- Add native `palimpsest` registry profiles with a same-authority HTTPS `api_base`, verified CA trust and redirect refusal. A configured authority must map to one protocol, and an explicit `--registry` must match a qualified reference's authority. Unconfigured fully qualified authorities stay ordinary Docker/OCI, as before, including login/logout. OCI-profile Docker/Buildx operations are unchanged.
- Add secret-once expiring keys bound to owner, project, package and actions. Add helper-backed native login/logout and validated OCI-image/runtime-bundle publication with compare-and-set tags. Uploads use offset-acknowledged chunked sessions, and the client starts a fresh session for each push. Add verified original-archive pull. Native `push --input/--manifest`, `pull --output` and typed owner-only local references keep native packages separate from Docker images and SquashFS runtime-layer tags.
- Cut mandatory online BuildKit cache transfer over to exact project/package keys with seven-field binding and API-base/namespace/project/package/scope/platform/builder-fingerprint local partitioning. OCI output requires `--cache-registry` and `--cache-package`; native output defaults to its own profile/package. Package-only keys and legacy `PALIMPSEST_TOKEN` cannot authorize cache writes. Native exports/receipts survive later cache or publication failure; the original-token runtime path remains separate.
- Validate original Keystone subject tokens with a dedicated system reader instead of default-project re-scope. Require protected project/principal sets, immutable operator namespace bindings, trusted public HTTPS origin and fresh effective key-owner membership (including federated identity prerequisites). Native keys do not grant privileged server-build authority.
- Document source-defined options and operator prerequisites without changing deployment, production policy or namespace bindings. Strict `--offline` builds never load registry profiles and reject `--registry`; publish their archives later with native `push --input`. The DMS Lab example depends on the separately deployed Afterglow key gateway. Fresh candidate verification and unavailable native/production gates are recorded in the development handoff, separately from earlier isolated package proof.

### Dashboard and Hub observability

- Extend the existing loopback `palimpsest ui` with VM, volume, network, artifact and build views, bounded BuildKit log tails, polling/pause and search. Controls are disabled by default; opt in with `--allow-control`. Inventory is a durable projection, not an OpenStack adapter or native execution proof.
- Keep long-running control requests busy until their response instead of aborting them on the polling timeout. Reset VM volume-deletion choices between drawers, and preserve the current view when activating the keyboard skip link.
- Add shared Hub API/export/build-worker logging with INFO default, explicit Hub-only DEBUG, safe route-template/status/timing records and bounded worker lifecycle metadata without credential-bearing exception text.
- Verify the bytes copied into CAS before publication; preserve existing targets on failed repairs, and hold publication locks through database commits. Bind legacy uploads to their creating user, enforce case-exact legacy project ownership, recheck key expiry/revocation after reader lookups, and correlate package errors with server-owned request IDs.

### Retained release and deployment fixes

- Derive the public HAProxy hostname from `palimpsest_public_endpoint_url` instead of requiring a second copied hostname. Keep explicit public exposure and existing matching HTTPS-origin checks; private endpoints, images, reader/protected-ID settings and Hub volumes are unchanged. Updated Afterglow roles use the same configured endpoint for their package transport. Local Ansible and amd64/arm64 HAProxy HTTPS routing were exercised with a synthetic upstream, not a production rollout.

- Keep metadata, module versions and lockfiles consistent at 0.3.0 in both distributions; synchronize the Kolla image candidate and replace its obsolete source default with reviewed commit `c4887f7806608e98f215abbd377d2eafe159ff76`. During deploy/upgrade, verify/pull the reviewed API image and initialize the local Hub volume root on each Palimpsest host before starting its API/worker containers; keep the database bootstrap single-host and do not recursively change existing blobs.
- Move native CI orchestration to GitHub-hosted Ubuntu with a protected environment and disposable, member-project-only OpenStack VMs. PRs require hosted checks; trusted dev/main pushes and release tags retain strict native success gates. The helper validates source/kernel evidence and refuses foreign ownership during bounded failure/signal cleanup. The existing persistent runner remains stopped.
- Restrict release permissions to read-only by default, granting OIDC only to PyPI publication and contents-write only to formal GitHub release publication. Existing development-package/GHCR publication paths still require separate approval; this unpushed cutover does not claim a GitHub native pass or production promotion.
- Document the persistent loopback/SSH-only candidate environment, actual authenticated artifact and two-layer build proofs, timeout/restart/reboot recovery, and explicit backup/restore and promotion boundaries. The isolated manual native proof passed all 43 boots/44 QEMU invocations and exact-owned cleanup after replacing transient KVM device ACLs with standard guest group membership. Exact closed-transport signature, GitHub CI execution, and production multi-host readiness remain separate evidence gates.

### Retained Hub connection-pool fix

- Recover stale pooled asyncmy connections whose closed uvloop transport raises a non-DBAPI `RuntimeError` during SQLAlchemy pre-ping. Only the closed-transport signature is treated as a disconnect; unrelated runtime errors and interrupted transactions still propagate. At the time of the Hub fix the published root `palimpsest-client` was 0.2.3.

## [0.2.3] - 2026-09-25

- Renamed the root PyPI distribution from `palimpsest-local` to `palimpsest-client` for 0.2.3. The Python module `palimpsest_local`, CLI `palimpsest`, optional `[kvm]` extra, and packaged Kolla role remain unchanged.
- The `palimpsest-client` trusted publisher published the root wheel and sdist to [PyPI](https://pypi.org/project/palimpsest-client/0.2.3/), and [GitHub Release `v0.2.3`](https://github.com/openstack-afterglow/palimpsest/releases/tag/v0.2.3) contains the verified artifacts. The independent `palimpsest-hub` Python distribution remains 0.2.0; publication is not deployment.

## [0.2.2] - 2026-09-25

`palimpsest-local` Kolla role hotfix; Hub image distribution remains 0.2.0.
Before bootstrap, a short-lived root container from the pinned Hub API image
sets the persistent Hub volume root owner to the image's `palimpsest` user.
Docker creates a new named volume root as `root:root` (0755), while the Hub
process runs as UID 1000; without this step even a healthy API cannot stage an
upload. The ownership step changes only the volume root, never recursive
application data, and runs before the regular unprivileged bootstrap.

## [0.2.1] - 2026-09-25

`palimpsest-local 0.2.1` only. `palimpsest-hub` stays at 0.2.0 and the packaged
Kolla Hub image default remains `0.2.0`; the `v0.2.1` tag re-labels the same Hub
images under the existing image workflow.

### Fixed

- Kolla role: `SSL_VERIFY` in the API/worker service definitions and the bootstrap
  task rendered as a boolean, which `community.docker.docker_container` rejects
  ("Non-string value found for env option") and which blocked the first production
  deploy. All three now render through `| string`; a role contract test requires
  explicit stringification for every non-string env expression.

The 0.2.0 entries below describe the 0.2.0 release; the `v0.2.0` tag's verify and
native KVM proof passed and the Hub images were published, while the PyPI trusted
publisher exchange was refused (no publisher registered) so no PyPI distribution
or GitHub release exists for 0.2.0.

## [0.2.0] - pending publication

The release targets are `palimpsest-local 0.2.0` and the independently versioned
`palimpsest-hub 0.2.0`. A future repository `v0.2.0` tag also labels the Hub API
and worker GHCR images `0.2.0` and `v0.2.0` under the existing image workflow;
the tag does not publish the Hub Python distribution to PyPI.

### Added

- Added a Palimpsest-only Afterglow source contract, drift checker, and scheduled CI verification.
- Added an experimental Dockerfile/Buildx frontend with canonical cache lookup keys, Hub-first verified BuildKit cache archives, strict-offline digest-pinned OCI layouts, and machine-readable build receipts.
- Added a metadata-preserving BuildKit tar export and deterministic SquashFS runtime pack, including numeric UID/GID, xattrs, tool-version policy, boot-base/platform binding, and a verified local/Hub conversion cache.
- Added owner-only Docker/OCI registry profiles with default/external registry selection, BuildKit mirror/CA configuration generation, and Docker credential-store reuse.
- Added Docker-compatible `login`, `logout`, `pull`, `push`, `tag`, `images`, `image inspect|history|rm|save|load`, top-level aliases, and a shell-free generic Docker passthrough.
- Added repeated build tags, OCI `--push`, Docker `--load`, `--pull`, progress selection, and additive external Buildx cache imports/exports while preserving mandatory Hub cache participation.
- Added a strict `palimpsest.yml` multi-VM workflow with `compose config|up|down|ps|logs|exec|stop|port`, dependency ordering, environment interpolation, typed cloud-init, and transactional owner-bound reconciliation.
- Added persistent single-writer block volumes: raw ext4 `virtio-blk` on KVM and receipt-bound Lima standalone disks, with preserve-by-default `down` and exact-owner `down --volumes` deletion.
- Added Lima static TCP project forwarding and guest-journal logs while Linux libvirt project ports fail closed until a verified `passt` path is available.
- Added dynamic Zsh, Bash, and Fish completion generated from the live `argparse` command tree.
- Added OCI-root publication reporting to the top-level `ps` `PORTS` column and typed `inspect` JSON, derived from the committed domain plan without backend calls or state writes.
- Added the narrower Linux x86_64 KVM OCI-root path: verified local OCI archive/layout materialization into SquashFS, an authenticated guest `/` transition, PID 1 supervision, bounded lifecycle/exec, explicit `--user`, per-VM NAT/host-only/none networking, and opt-in host port publication. This is not general Docker compatibility or remote VM scheduling.
- Added a standalone Hub `/app` console and project-scoped `/v1/builds` queue for system administrators, executed by a separately provisioned Linux KVM build worker rather than the API container.

### Changed

- Moved the Palimpsest Kolla-Ansible role into the `palimpsest-local` root wheel as dependency-free shared data, removed the standalone `palimpsest-kolla` package, and relocated the Hub Dockerfile under `docker/`.
- Raised the root and independently versioned Hub packages to `0.2.0`; the root wheel's Kolla role now defaults to Hub API/worker image tag `0.2.0`. Source-build image refs remain tied to the configured checkout SHA. Earlier `0.1.4` root and `0.1.3` Hub versions remain historical.

### Fixed

- Added `base_image_digest` to Hub layer responses so a pulled root runtime layer preserves its boot-image chain.
- Runtime base architecture is now checked before BuildKit starts, and VM run receipts distinguish KVM direct-block attachment from Lima copy/loop activation.
- Split OCI image publication (`build --push`) from Hub runtime-block publication (`build --runtime-push`) and kept legacy Hub boot-image commands distinct from Docker image commands.
- Hardened registry/cache inputs against inline credentials, kept cache specifications out of receipts, and made strict-offline builds reject every registry-facing option before solving.
- Moved KVM readiness after typed provisioning and added a fresh per-boot readiness service so project restarts cannot reuse an old console sentinel.
- Made architecture review stamping compatible with the documented system-Python entry point when `datetime.UTC` is unavailable.
- Bound conventional cloud-image readiness to the architecture-specific serial port and first-boot project-init completion; a cloud-init-disabled subsequent boot uses a guarded fallback instead of reusing an old ready sentinel. The revised conventional-cloud path still lacks a post-merge native boot/reboot proof.
- Hardened Hub bundle publication against contradictory shared descriptors and oversized PAX member metadata, and fenced interrupted-build cleanup behind process-group verification.

### Documentation

- Reworked the repository README into a public project guide with installation, Hub configuration, artifact workflows, macOS Apple Silicon usage, Linux KVM requirements, layer builds, and development checks.
- Added this changelog.
- Added a declarative project schema, lifecycle, backend-support, and security guide.

### Verification and release limits

- A branch PR's native stage-1 KVM gate passed with retained-root, root-transition, PID 1, and negative-input evidence. That proof is for the OCI-root guest stage-1, **not** a post-merge conventional cloud-image VM boot, a Hub build-worker deployment, or a general OCI workload qualification.
- PyPI publication is gated by the tag-triggered release workflow's successful `verify` and native `kvm-proof` jobs on an enabled self-hosted Linux x86_64 KVM runner. A portable pass or a development-package prerelease does not satisfy the PyPI gate. The 0.2.0 tag/release, release builds and publication, and production deployment were not performed as part of this documentation update.
- The Kolla source default now references `0.2.0` Hub API/worker images, which must be published and verified before deploying that default. This source change does not establish their availability or deploy a service.

## [0.1.0.dev0]

### Added

- Python 3.12+ `palimpsest-local` package and `palimpsest` CLI.
- Content-addressed local artifact store for cloud images and SquashFS layers.
- Hub image, layer, and OCI-layout bundle operations.
- Palimpsestfile parsing and disposable guest builds that produce tagged SquashFS layers.
- Linux x86_64 KVM/libvirt lifecycle support for runs, inspection, logs, shell access, command execution, stopping, removal, and layer commits.
- macOS Apple Silicon prototype using Lima's VZ backend for ARM64 Ubuntu images, managed networking, guest shell access, and local builds.
- Unit, integration, and KVM test suites plus wheel and source-distribution build workflows.

### Notes

- The macOS VZ path supports `build` and `run`; macOS Lima runs do not support `commit`.
- The planned `0.1.0` release and Afterglow package cutover remain dependent on a clean Linux x86_64 KVM integration proof.
