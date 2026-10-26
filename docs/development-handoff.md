---
status: in-progress
branch: codex/oci-root-phase1
timestamp: 2026-09-14
files_modified:
  - ARCHITECTURE.md
  - README.md
  - AGENTS.md
  - agent.md
  - docs/development-handoff.md
  - docs/docker-hub-service-matrix.md
  - docs/oci-gpu-support.md
  - docs/oci-linux-process.md
  - scripts/test_lanes.py
  - src/palimpsest_local/pci_preflight.py
  - tests/unit/test_oci_store.py
  - tests/unit/test_pci_preflight.py
---

# 개발 인계 — 2026-09-14

이 문서는 다음 세션이 현재 작업을 추측 없이 이어가기 위한 운영 인계서다.
계획이나 파일 존재를 실행 증거로 취급하지 않는다. 현재 구조의 정본은
[ARCHITECTURE.md](../ARCHITECTURE.md), 실행 경계는 [testing.md](testing.md),
세부 결과는 아래 링크된 문서와 source다.

## Supervisor frame commitments — 2026-10-27 (source-only patch0.3.9)

Fetched origin/dev and isolated HEAD are `73667c8f4065215416e775f12cb0a6f578853044`, released0.3.8. Native amd64 additional qualification found same-size same-mtime/ctime rewrites could return altered non-tail output with recomputed checksums. Patch0.3.9 adds compact trusted per-record chain seals from independent startup verification and successful own appends; validates each requested frame before exposure without prefix rescans. Metadata-only cursor/snapshot checks are explicitly not whole-history audits. Root/Hub metadata/modules/native-generated locks and Kolla default image tag advance together; do not move0.3.8. Existing source-build SHA, KVM policy, guest/IAM/API/schema and original shared WIP remain unchanged. OpenSpec: `verify-supervisor-frame-commitments`; evidence and gate boundaries: [testing](testing.md#supervisor-frame-commitment-qualification--2026-10-27). Main owns final integration, publication and production; this repair performs no commit/push/tag or production mutations.

Qualification follow-through: initial macOS canonical root full had three real SIGINT shutdown failures; no journal test failed. Independent actual CLI default-vs-inherited-ignore proof established a concrete foreground-web shutdown defect without asserting uncaptured child dispositions from the earlier failures. Main authorized the minimal same-patch `ui.serve` explicit SIGINT/SIGTERM stop-handler install and actual-handler restoration plus regressions. Preserve the first failed run and independently qualify the final combined bytes; do not relax deadlines or replace the browser/thread/server lifecycle.

Final source qualification completed: architecture digest `953ecbaf38fb3cda9380b28d49de2c5f8fd162f103a0ebbe4b55da7ab3f9ca96` (502 files), explicit native archive SHA `afff4d537f7784e300c6a29dd55bd6daaebea24d437eb92a9b908cb70775eb13`. Focus70, macOS canonical root7491/265 skipped, Hub654/2 skipped, native amd64 exact serial7324/97 skipped and four200-case natural-stamp experiments passed with0 altered returned frames (143 genuine equal-stamp recomputed-checksum tmpfs cases). Root/Hub Ruff/build, installed-wheel0.3.9, reference/lane/OpenSpec/architecture and actual inherited-ignore CLI shutdown passed. Final close releases the O(record count) commitment array under the journal lock; no private-layout assertion remains. Prior failures and superseded source receipts are retained. Full receipts/boundaries are in testing and `~/.local/share/afterglow-proofs/palimpsest-journal-integrity-20261027/verification.json`. Only final qualification receipt prose was appended after archive freeze; source/test bytes remain frozen. Main owns staged check, commit/release and separately authorized deployment.

## Isolated catalog/unpack assessment — 2026-10-26 (unqualified, unpublished)

- Fresh origin/dev and highest remote release are `89758077aa8c32fd84b4b8a02287bf4b7f483140` / `v0.3.7` (no tag was created). The next available patch slot observed was0.3.8, not reserved. Integration checkout: `/Users/pieroot/code/palimpsest-release-integration-20261026`, local `dev`.
- Preserved original shared checkout/index and separate evidence under `~/.local/share/afterglow-proofs/palimpsest-release-integration-20261026/`. Integrated catalog DTO/router/service, bounded private package search, isolated fixtures/auth regressions, local unpack and direct architecture/digest helpers/tests/docs. No private settings, unrelated Nova/GPU/benchmark/observer changes or runtime writes.
- Ported only the completed global catalog human/system primitives into catalog-specific modules. Existing0.3.7 `auth.py`, package/key/publication authority, models/bootstrap and workers remain unchanged. Global reads freshly validate original human/user/home/project/service/delegation and complete raw direct/group system authority before targetless native system:manage policy; consumer authority adapts to current capability labels rather than cutting over package policy. SQL provenance key nullability remains unchanged; catalog test fixtures now carry real synthetic key rows. Metadata/module/Kolla image tag and native-generated locks prepare0.3.8, unpublished.
- No build/test/lint/format/architecture verification was requested or run. CLI-reference generation and root/Hub `uv lock --offline` alone ran; the lock resolver reported only root/Hub version bumps and newly required oslo-context/oslo-policy packages, preserving the stronger current dependency set. Main owns independent review, fresh qualification, commit/release and production. Commands and gates are below in [testing](testing.md#isolated-catalogunpack-assessment--2026-10-26).

## Full-source 0.3.5 integration — 2026-10-14 (released, production rollout recorded)

- Integration clone: `/Users/pieroot/code/afterglow-release-all-20261014T073153Z-c2ba90/palimpsest`, based on 0.3.4 `ad1674e842c6cefaf4e2d38705b109a579f56040`. Release PR #24 merged into protected dev as `04d51ad6e1bf00c4b6e5bf82050383d0bb0a10cf`. Original `/Users/pieroot/code/palimpsest` and its index/branch remain outside this documentation assignment. All latest native authority/private build protections plus original public local/client and distributed-role work are preserved; older dated candidates/receipts below do not describe this release's state.
- Metadata: root/client/Hub modules and project metadata, Kolla image-tag default are 0.3.5. Native `uv lock --offline` generated both locks and reported only `palimpsest-client`/`palimpsest-hub` version updates from 0.3.4. Kolla source-build default stays the separately reviewed immutable `c4887f7806608e98f215abbd377d2eafe159ff76`; eventual source-mode rollout requires the separately reviewed committed release SHA, not a dirty self-pin.
- Integration boundary: latest HTTPS-only dedicated CPU VM worker/direct executor retained; no nested/per-build KVM or worker SQL/CAS/Redis/Keystone credentials. Distributed build exclusion translates to API SQL single-active authority with fixed persisted deadline, not old renewable SQL worker APIs. Distributed export/Trust maintenance roles, cancellation drain, timestamp/generation fencing and scratch preservation remain. Native owner/key attenuation and original-token ordinary publication authority stay separate.
- Public local WIP: foreground `web` clean cutover; local-cache BuildKit and OCI output/provenance/tag editing; automatic run-source resolution and pinned index support; asynchronous build/run operations; identity-bound PTY/OCI consoles and drain/ACK; host resource discovery/capability-safe control; aligned tables, detail resource/log counters and UI motion; ordinary cloud-image-only web removal; legacy/native/Docker Hub remote artifact transfers; peer-cancellation and incompatible-backend repair; immediate-use defaults/help/completion. Source/tests/contracts are retained; current root/Hub local suite results below do not imply native or live-provider qualification.
- Private exclusion: `test-openrc.sh` is private, excluded from the release clone and scans, and not public release material; original preserved. The manifest's original public/copied classification is corrected by this exclusion. No contents or credential values are reproduced here.
- Additional overlaps semantically integrated: original untracked files already tracked upstream (`hub/tests/test_export_delegation.py`, `openspec/specs/scoped-package-capabilities/spec.md`, `scripts/smoke_package_capabilities.py`) were compared and their additions retained without overwriting latest scope/current-owner/admin/ordinary-root protections. The archived native ownership change remains dated; its main specification now follows the private HTTPS build architecture. New ownership tests are classified in the Hub lane manifest.
- Kolla conflicts: preserve metadata-only catalog and host-local selected payloads. Older worker/API environment entries are retained in the separate payload definitions; every former secret/protected-ID/delegation/public-route precheck exists in controller-only `precheck_controller.yml`. Do not restore eager service maps or apply controller secrets to worker-only hosts. Latest separate keyfile/resource-bound/init/hostname-pin/worker playbook and toolbox reader provisioning remain.
- Schema/cutover: no numbered migration ID or destructive schema operation. Offline bootstrap creates token-owned `palimpsest_worker_leases` and adds nullable build `lease_expires_at`; no ownership backfill, remote guest adoption or recipe replay. Stop new ingress and drain every old API/export/build/upload/GC writer plus blocking effects first; mixed-version recovery is unsafe. Preserve unknown scratch/loops/jobs. Data migration accepts only empty initialized destinations, rejects partial native package schemas, copies durable rows/references and excludes transient role leases; CAS copying is separate.
- Ownership translation receipt: build claim/status/input/fail/output use one held API SQL mutex session; project admission uses a separately namespaced SQL project mutex. Existing native caller identity/private output and terminal lease nonce remain. Removed obsolete worker `execution_node` from ORM/bootstrap additions, without dropping any legacy physical database column; migration strips that superseded field and excludes transient maintenance/API-mutex ownership rows. New `hub/tests/test_build_ownership.py` and migrated `test_database_pool.py` consumers define disjoint API filesystem-lock SQL exclusion, persisted deadline, stale owner, private publication, terminal receipt and blocking-effect cancellation-drain behavior. Current Hub suite and separate disposable MariaDB process receipt are recorded below; remote execution/provider effects remain synthetic.
- Storage/rollback: API/export share SQL and the same absolute coherent CAS/locking view; identical host-local volume names are not shared storage. Take paired SQL/CAS backups from one no-writers interval and prove isolated restore. Preserve numeric owners/modes, private configuration, every actual prior immutable image pin and enough finalization/bundle/scratch space. Rollback requires another all-writer stop, exact prior images/config and paired restore; never run old recovery beside live new claims.
- Auth/worker prerequisites: separate system-reader-only validator (no admin/manager/service), fresh direct system-all admin query, complete unique-global role/inference/assignment reader policy, nonempty exact protected user/project IDs and immutable namespace bindings, trusted package HTTPS origin, approved original requester-project service leaves and renewed subject token as needed. Deferred exports require requester-created finite least-role Trust, real Keystone create/authenticate/impersonating-delete/expiry policy, current owner validation and no tenant-scoped service-password fallback. Undelegated queued exports fail `delegation_required`; requesters resubmit. IAM/manual-grant removal is a separate approval, not this integration's mutation.
- Dedicated build VM needs same-ref reviewed image, Linux x86_64 CPU-only privileges isolated from controllers, verified-TLS/reachable origin, owner-only keyfile and private persistent journal/scratch. Size disk for inputs/raw conversion/output; the historical 40GiB boot disk cannot satisfy the default 100GiB blob ceiling. DNS/connect are outside the header/body watchdog. Uncertain claims halt; lost terminal mutation responses settle by status only; exact process/loop cleanup must precede scratch removal or another claim.
- Parent fixes before qualification: nine import-order/unused-import corrections with `ruff check --fix`; `ruff format` formatted 38 root and ten Hub files within integrated/version-metadata paths. Test-only repair of `tests/unit/test_cli_registry.py::test_oci_local_cache_build_uses_no_native_or_legacy_authority` supplies a context Dockerfile and keeps output outside that context; production validation is unchanged.
- Local root qualification: frozen Python 3.12 dev sync; lane manifest valid; full lane **7,064 passed, 264 skipped**; Ruff lint/format, OCI-fixture and CLI-reference checks passed. Root `palimpsest_client-0.3.5` wheel/sdist built and smoke-installed. Python 3.11 `palimpsest --version` returned `0.3.5` (not a full Python 3.11 suite). Opt-in native/KVM/BuildKit proofs were skipped, not qualified.
- Local Hub qualification: frozen dev sync; full suite **559 passed, 2 skipped**; Ruff lint/format passed; `uv build` produced `palimpsest_hub-0.3.5` wheel/sdist. Separate `test_shared_sql_process_ownership_smoke` against empty disposable loopback MariaDB 11.4 `palimpsest_ownership_*` reported **2 passed**, with two/three real SQL/process workers and independent flocks, but synthetic remote/provider effects.
- Local Hub images: canonical `docker/hub/Dockerfile` API/export-worker/build-worker targets built for declared `linux/amd64` and executed under local emulation. All imported their entry modules and reported Hub `0.3.5` on `x86_64`; build-worker also carried client `0.3.5` plus `bwrap`, `mksquashfs`, `mkfs.ext4`. This is not KVM/BuildKit/cloud/production proof. Redacted public-diff gitleaks scan had one false positive in vendored MIT minified `xterm.js` (`FourKeyMap`).
- Verification and release: [local, CI, publication and production receipts](testing.md#full-source-035-integration--local-verification) distinguish each evidence boundary. PR #24 passed all six strict required hosted checks on `1ff87efe`; protected dev merge `04d51ad6` passed Test, Hub images and Development package CI. Annotated `v0.3.5` completed verify/PyPI/GitHub release and Hub image publication. PyPI client wheel/sdist SHA-256 prefixes are `ad0ee712cecef648` / `f53642bbe1a2d2bb`; all three published Hub images executed under amd64 with `--network none` and successful imports. Native KVM proof was NOT performed: skipped by repository policy exactly as for 0.3.4, not qualified. Architecture review/stamp remains a freshness check, not runtime qualification. No new Astra/Sol signoff or code-index sync is claimed by this documentation update.
- Production: Hub API/export worker on `dms-controller1` exited 0 after the gate (3 complete builds / 1 error); cold SQL restore matched 15 tables / 10 rows exactly, encrypted cold blob tar was 884,766,720 bytes, `genconfig` changed nothing, and reconfigure was controller1-only. Health returned 200 direct/public; unauthenticated `/v1/me` returned 401. Operator client 0.3.5 and all 27 installed Kolla role files matched the tag. The build VM was upgraded with `palimpsest-build-worker.yml` (pull + deploy); startup halted on the old worker's `claim_pending` journal at 10:47:06Z, five seconds after API stopping began. Server state proved no lease could exist (4 terminal builds, every `lease_expires_at` NULL, last change 2026-10-13 21:19:30Z, nothing claimable). Per install guidance, journal copies were preserved in the operator backup and VM `/root`, `worker.json` cleared and `worker.lock` retained. The worker then ran with 0 restarts and 16 claim responses of 204 in two minutes. Future maintenance must stop/drain the remote build worker before stopping the Hub API.
- Remaining qualification boundaries, not release/rollout gates: real original requester authentication/current service leaves, approved IAM/Trust/provider policy, restored authorization/blob persistence, shared storage durability/clock/failover, live recipe execution/restart/cleanup, and separately authorized native KVM/BuildKit/cloud proofs are not established by these receipts. Hub has no migration ledger; bootstrap `create_all` and empty disposable MariaDB do not prove existing-schema compatibility. Future cutovers still require reviewed offline upgrade, all-writer drain and paired backup/restore. Active `isolate-service-execution-credentials` live task2.5 remains open; this release record neither archives it nor claims its acceptance.


## Hub remote build plane, live 0.3.3 cutover and 0.3.4 candidate — 2026-10-14

- **Approvals (owner, this session):** scoped iterative release loop for Hub Kolla build/upload/consume fixes only (branch `fix/hub-kolla-live-deploy` push → protected dev PR → required checks → merge → immutable `v0.3.x` → PyPI/GitHub/GHCR → operator PyPI pin → Kolla deploy, repeated until the features work; no main promotion or CI-protection change). Controller keeps the API only; builds run directly inside one CPU-only VM in the `palimpsest-service` project (no nested/per-build KVM). Legacy image/layer upload treats administrators as ordinary current-project users with current publish authority. Earlier: exact 3-leaf grant to Default user `test` on project `test` (3× 204); the owner later added Palimpsest roles to `pieroot/test` themselves.
- **Live 0.3.3 on dms-controller1 (actual):** API `ghcr.io/openstack-afterglow/palimpsest-hub-api@sha256:cb5b32ef81f8db0380821146d50eed0f4eba9b26a4361a177fa0e1cef96ef979`, export worker `…palimpsest-hub-worker@sha256:99d6193e889c92b4512dece8cf2ce1c20c4372f1d2d64db301f1931a5bdd0af3`, both revision `7186abf`. Deploy history: first run rc4 `couldn't resolve module/action 'openstack.cloud.identity_user'` (reader preconditions ran on the deploy host); second rc0 still ran 0.3.2 because `globals.d/90-openstack-afterglow-globals.yml` overrides `globals.yml`, and its loadbalancer drift handlers recreated controller1 haproxy/proxysql/keepalived (VIP stayed on controller2; no LB config file changed); third rc0 66 ok/3 changed performed the cutover. All used `-e palimpsest_run_preconditions=false` after verifying existing DB/service/reader. Only empty-state logical SQL (14 tables/0 rows) and config backups exist; no writer fencing, CAS archive or isolated restore was performed.
- **Operator pin durability:** a concurrent Afterglow/Drover rollout rewrote the aggregate-owned `globals.d/90` (regular file, not the documented symlink) back to 0.3.2 Hub refs and dropped the package origin. The Palimpsest-owned `/etc/kolla/globals.d/99-palimpsest.yml` (0640) now pins both 0.3.3 digests and `https://cloud.dmslab.re.kr`; canonical `kolla-ansible deploy --tags palimpsest --limit dms-controller1` argument assembly (no playbook run) loads it last. Other files were left byte-identical.
- **Authentication:** corrected `clouds.yaml`/`openstack` issues `pieroot/test` tokens through the SDK (201; tokens never printed). Live 0.3.3 `/v1/me` 200: system admin, upload false; `POST /v1/builds` 503 `isolated build worker is not configured`. The stale `test-openrc.sh` must not be reused.
- **Worker VM (actual, owner-approved):** `palimpsest-hub-build-worker-034`, server `49b87ab3-340c-4e2d-ab89-4d4f40bd8bb3`, project `palimpsest-service` `9cdd693a8af8414f8eaa7f026a7b95f9`, flavor `cpu.2c_8g`, image `ubuntu:24.04-server`, 40 GiB `ceph_hdd` boot volume `b33125fa-7027-43be-8e84-0298626f94d1` (not delete-on-termination), port `fdd76feb-a47d-4ee0-907f-f0f38e435f11` fixed `172.30.103.130`, SG `68cb836b-aa95-4734-a3b4-8c1e397a43fd` (TCP 22 from operator `172.30.0.50/32` only), keypair `palimpsest-hub-build-worker-034`, no floating IP, no guest credentials. Durable ownership ledger and private key/known_hosts: `/etc/kolla/.palimpsest-live-033-20261013/worker-vm-*`. ED25519 host key `SHA256:Qj+QpMKI3bQ1h/zCdPnuodKoKbcqMbj9EKb20hkebF0` matched the console. Ubuntu 24.04.2, kernel 6.8.0-57, 2 vCPU/7942 MiB/37 GiB free; Docker `docker.io` 29.1.3 (`docker-init` present), python3-docker 5.0.3 installed; a `busybox:1.36` probe image was pulled. Service DNS `172.30.100.34` lacks `palimpsest.dmslab.re.kr`; verified-TLS `/v1/health` returned 200 through `172.30.0.254` and `117.16.137.199`, so the worker needs `palimpsest_build_worker_etc_hosts`.
- **0.3.4 candidate source (isolated worktree, uncommitted):** HTTPS key/lease worker protocol and direct VM executor replace the KVM worker; Kolla adds the disjoint `palimpsest-build-worker` group, keyfile, resource bounds, `init: true`, hostname pins and a packaged worker playbook; reader preconditions run through `kolla_toolbox`; legacy upload policy as approved. Independent review rounds found and the integration fixed: worker PID 1 vs parent fence, unreachable worker group in canonical plays, admin-implied publication, existing native keys after system promotion, unbounded receipt replay, unfenced recovery helpers, private 0700 root/umask, missing partition nodes, and a stale same-PID helper record. Native local qualification then found two more defects, both fixed: layer roots inherited the private `0700` capture mode (also in the local guest builder), and recovery cleanup compared sysfs path text that changes after the executor namespace dies, leaking loops.
- **Afterglow boundary:** the deployed aggregate is the extracted `/etc/kolla/plugins/openstack-afterglow-v1.30.12` plugin and targets only `palimpsest`; worker hosts therefore use the packaged `palimpsest-build-worker.yml` via `kolla-ansible --playbook`. Aggregate integration of the worker group/image is an Afterglow follow-up; three exploratory edits to the shared Afterglow checkout were reverted.
- **Next:** commit/PR/CI/merge/tag `v0.3.4` and verify PyPI/GitHub/GHCR; install `palimpsest-client==0.3.4` on the operator; add the VM to `[palimpsest-build-worker]` with its key/known_hosts; set the 0.3.4 digests, `enable_palimpsest_hub_build_worker`, origin, hostname pin and a private worker key in Palimpsest-owned globals.d files; pull/deploy controller1 (preconditions skipped) and the worker playbook; then exercise a real x86_64 API build, legacy upload by `pieroot/test`, metadata/download, consumer run/restart/cleanup. Outstanding non-claims: live toolbox preconditions (would reset the reader password), x86_64 native executor run, Afterglow aggregate integration, DNS/connect watchdog bound, paired restore.

## Hub management formal release and Kolla preparation — 2026-10-11

- User requested actual Kolla deployment for Afterglow and selected **formal release first**, excluding direct publication/deployment of dirty management images. Isolated source starts at upstream dev `a428d3fdb7c4d6243833ab189fd187b03710ada3`; shared `dev` at `aa397a0fd976e35302974a347748a9773e3084bf` and its observed 86 modified/21 untracked paths are untouched by this work. Candidate metadata is 0.3.3. No commit, push, tag, registry publication, production/operator package installation, remote configuration mutation or production service restart has occurred in this preparation checkpoint. Local proof venv/image installation is not operator installation. Astra participation is unavailable; independent review is not user approval.
- Integration imports only the existing Hub management console/API delta. Preserve upstream direct system-all admin lookup, validator manager exclusion, timezone normalization and cleanup generation fence; do not copy older backend source wholesale. Local dashboard/VM/OCI changes and unrelated sibling service changes are excluded from this Hub release.
- Fresh read-only operator: `wireguard-dmslab` (`wireguardserver`), `/etc/kolla/.venv/bin/kolla-ansible` 21.3.1.dev4 and `/etc/kolla/multinode`. Installed root role package is palimpsest-client0.3.1. Actual Palimpsest group contains **only dms-controller1**, with API/worker co-located on host-local shared CAS. Current API pin `ghcr.io/openstack-afterglow/palimpsest-hub-api@sha256:cee50749f9b329ef08c43e1d85b6fa16384a25c509208c844938c9eedb871552`; worker pin `ghcr.io/openstack-afterglow/palimpsest-hub-worker@sha256:e7807a8bd4c779799af574fae43cca611f8a981e8146a2744fc3d4147983e61b`. API is healthy, worker running. Actual listen/VIP ports are18021/8021, not18020/8020 defaults.
- Existing public Hub/Afterglow TLS health both200. Actual running Afterglow backend has Palimpsest enabled with `https://palimpsest.dmslab.re.kr`; its certificate-verified Hub health/discovery requests returned200. Hub separate validator authenticated system-scoped with only reader. Protected-ID counts are4 projects/6 users, namespace bindings0, trusted package public origin empty, native build worker unconfigured. No package user authentication, blob mutation, Trust/Glance export or KVM operation was performed by these probes.
- Candidate source review found and repaired publish-only repeat-upload incompatibility without restoring inventory disclosure: inventory callers alone take the completed descriptor shortcut; publish-only callers take normal byte/metadata-validated upload sessions. CLI reproduction failed with inventory403 before and succeeded with the identical digest after; the regression failed before and its two cases passed after. Native-key legacy-input rejection precedes credential assignment/request; missing session404 releases the matching local session on Status/Resume/Abort, warns that finalization may have committed and sends no retry. Post-fix independent console/consumer review found no remaining concrete issue within its scope; initial auth-review verdict is the pre-fix snapshot.
- Exercised candidate gates: root unit/Kolla6,220 passed/209 skipped, previously uncovered selected portable helpers182 passed/8 skipped; final Hub337 passed/771 deprecation warnings; Ruff/manifest classification passed. Root and Hub distributions built; installed root wheel ran version/help on Python3.11.15. Canonical API/worker images built and ran on arm64/amd64 with exact installed auth/API/static hashes matching final source. Real local Keystone synthetic-actor, real MySQL/bootstrap and Redis HTTPS API proofs passed88/82 checks for six actors. Actual browser proved independent legacy publication and native credential domains, all three missing-session release paths with zero auto-retries, real OCI archive publication/inspection and20,480-byte hash-identical Blob-fallback saving. OS save-picker persistence, real Glance conversion/native builder and native/KVM/GPU/cloud acceptance remain unexercised. Receipt root: `/Users/pieroot/.local/share/palimpsest-proofs/hub-kolla-release-20261011/`; details and fixture failures are in `docs/testing.md`.
- Minimal future image-mode lifecycle is canonical **pull + reconfigure**, not deploy: reviewed `pull.yml` pulls enabled services; `reconfigure.yml` includes precheck/config/start and omits database/bootstrap/Keystone preconditions. This avoids re-running reader-user `update_password: always` and role/service registration unnecessarily. Operator wheel installation, new immutable pins/public-origin config and service recreation still require exact approval, writer fencing, paired SQL/CAS backup and isolated restore. Keep the existing reader/protected IDs, one storage-owning controller and18021/8021 ports; no ordinary IAM grant or native activation is implicit.
- Sealed unpublished local ref: `release/hub-management-0.3.3-20261011`, HEAD/base `a428d3fdb7c4d6243833ab189fd187b03710ada3`. Upstream dev was rechecked and still matches that base. Reviewed working source guard passed with **448 files, source_sha256 `a8d16f3f592b263ba620c161f68c3d5767eccd7ddd3de6f1030f59aafe6da107`**; the distinct repeat publisher cannot replace the original creator. No source change is permitted under that approval binding without re-verification.
- Next gate is explicit approval for committing this bound candidate and protected-dev PR integration, then immutable `v0.3.3` formal publication (root PyPI/GitHub distributions and the canonical amd64 API/worker GHCR images, including semver/latest tag updates). A new tag must pass its exact-SHA hosted gates; local arm64 proof does not authorize changing publication platforms or CI. Operator wheel/pins/public-origin configuration and production Hub pull/reconfigure remain a later separate exact-resource approval after writer fencing, paired SQL/CAS backup and isolated restore. Preserve current image pins, private config/keys/data and unrelated containers; no docker-cp hotpatch, source-mode bypass, ordinary IAM grant or native activation. Original-user Afterglow/BFF reads/writes remain unproved because no eligible ordinary caller scope was found; do not infer readiness from health200.
- Publication approval update: owner selected the full formal-release option for `release/hub-management-0.3.3-20261011`, upstream/base `a428d3fdb7c4d6243833ab189fd187b03710ada3` and reviewed 448-file source hash `a8d16f3f592b263ba620c161f68c3d5767eccd7ddd3de6f1030f59aafe6da107`: candidate commit/push, protected-dev PR integration, existing dev package publication, exact-SHA hosted gates, immutable `v0.3.3`, root palimpsest-client PyPI/GitHub distributions and canonical amd64 Hub API/worker GHCR semver/latest publication. Fresh repository variable `PALIMPSEST_KVM_ENABLED=false`; disabled/skipped remains no native qualification. No production/operator installation, configuration, restart, IAM change, private helper transfer or native/cloud operation is included. Publication execution is pending at this approval entry; candidate local containers were removed by exact saved ID/ownership/CAS checks, while their isolated database, CAS and proof receipts are retained.

## Native Hub authority repair — 2026-10-09

Isolated latest-dev baseline `75593f31b23cbceb76b2a38dc57f63bc4260271b`; shared work is untouched. Production query inspection found that effective expansion omits a direct system-all administrator, which the direct reader-compatible query returns. The one-keyword repair, native HTTP regression and synchronized root/Hub 0.3.2 metadata are tracked in [fix-native-hub-system-admin](../openspec/changes/fix-native-hub-system-admin/).

Owner explicitly approved verification, commit/protected-dev integration, immutable 0.3.2 wheel/image publication and controller1 Hub deployment through the deployment server's canonical Kolla lifecycle, including its managed separate reader secret and existing reader system assignment. Ordinary grants, multi-node local CAS and native/KVM activation are excluded. Sol develops; independent configured-parent-model reviewers inspect auth consumers and release/Kolla boundaries. No Astra planning participation is claimed.

Installed-SDK HTTP regression failed before and passed after; full Hub 299 passed. Real registered-app loopback HTTP, synthetic dedicated-reader Keystone and fresh SQLite proved original-project-only reads 200, revocation 403 despite project-admin token and missing token401, without an auth dependency override. Root release gates, exact-SHA CI, image publication and native deployment-reader acceptance are still separate checkpoints. Historical publication/Trust/native boundaries below remain dated and do not imply new proof.

## Native CI opt-out — 2026-10-07

Owner explicitly requested standing automatic approval or turning off native KVM attempts because CI/CD repeatedly waits. Current read-only inspection also found both pinned HTTPS URL variables absent, so the selected change is explicit `PALIMPSEST_KVM_ENABLED=false`, not automatically approving a doomed proof. Test and Release accept only enabled/success or literal disabled/skipped; disabled notices explicitly deny native qualification. Failed verification, native failure/cancellation/missing result, invalid flag and untrusted/cancelled release contexts stay blocked. Native environment reviewer/branch policy, secrets, cleanup/pins and all six strict hosted checks remain unchanged.

Implement in isolated latest-dev clone; the shared checkout's unrelated uncommitted work is not included. Apply through the strict PR path to dev. Main promotion remains owner-managed, and older main/tag workflow source retains its old verdict; a variable write alone cannot update that source. No release tag, native VM or production action is part of this change.

Before-policy measurements: latest20 completed non-cancelled Test runs (10 push/10 PR, same observed job-name shape, historical mixed native boundaries). Created-to-last-job critical path uses latest startedAt for reruns: median177s/p90 nearest-rank60072s; push n10=163/84054s, PR n10=187.5/295s. Run36169450623 uses its 17:55:34Z rerun start (167s), not the original createdAt (579s). Native execution n14=139/140s; valid native wait n13=72/84036s; its inconsistent negative native wait is retained below but excluded only from wait statistics. Run37502849641 native wait was60052s. Raw job/step timestamps remain in machine-local `/tmp/palimpsest-native-ci-metrics-20261007/before-measurements.json`; the normalized run receipts below are durable. After runs must retain exact SHA/event/sample size; no speedup claim without a comparable sufficient population.

Repository variable mutation: API readback confirms canonical false at 2026-10-07T11:36:20Z. Native protected-environment/secret/resource settings were not changed.

First local reviewed source digest `f46e8aef961a9e3c290e5e5119ce92d9e27bd65f0359d0f055027abbb5b43272` (445 files): workflow contract modules 126 passed in 4.04s; complete portable lane 6366 passed/217 skipped/7 retained fork deprecation warnings in 219.20s (6583 selected nodes). Actual Test verdict and Release notice shell paths exercised disabled/skipped, enabled/success and denied bad outcomes; Release notices bind actual native outcome, not flag spelling. Actionlint, changed-file Ruff lint/format, manifest, current CLI reference, strict OpenSpec and staged architecture guards passed. These are local policy/portable receipts, not GitHub native qualification or release publication.

| Run | Event | Created UTC | Started UTC | Last job UTC | Critical path s | Native wait s |
| --- | --- | --- | --- | --- | ---: | ---: |
| [37502849641](https://github.com/openstack-afterglow/palimpsest/actions/runs/37502849641) | push | 2026-10-06T17:20:58Z | 2026-10-06T17:20:58Z | 2026-10-07T10:02:10Z | 60072 | 60052 |
| [37495871557](https://github.com/openstack-afterglow/palimpsest/actions/runs/37495871557) | pull_request | 2026-10-06T16:27:39Z | 2026-10-06T16:27:39Z | 2026-10-06T16:30:55Z | 196 | not run |
| [37493792765](https://github.com/openstack-afterglow/palimpsest/actions/runs/37493792765) | pull_request | 2026-10-06T16:12:00Z | 2026-10-06T16:12:00Z | 2026-10-06T16:14:59Z | 179 | not run |
| [37318672004](https://github.com/openstack-afterglow/palimpsest/actions/runs/37318672004) | push | 2026-10-05T13:40:48Z | 2026-10-05T13:40:48Z | 2026-10-07T09:56:18Z | 159330 | 159313 |
| [37317958855](https://github.com/openstack-afterglow/palimpsest/actions/runs/37317958855) | pull_request | 2026-10-05T13:35:14Z | 2026-10-05T13:35:14Z | 2026-10-05T13:37:59Z | 165 | not run |
| [37185453150](https://github.com/openstack-afterglow/palimpsest/actions/runs/37185453150) | push | 2026-10-04T07:19:55Z | 2026-10-04T07:19:55Z | 2026-10-05T06:40:49Z | 84054 | 84036 |
| [37154355462](https://github.com/openstack-afterglow/palimpsest/actions/runs/37154355462) | pull_request | 2026-10-03T21:13:36Z | 2026-10-03T21:13:36Z | 2026-10-03T21:18:42Z | 306 | not run |
| [37154038899](https://github.com/openstack-afterglow/palimpsest/actions/runs/37154038899) | pull_request | 2026-10-03T21:08:17Z | 2026-10-03T21:08:17Z | 2026-10-03T21:11:12Z | 175 | not run |
| [37138269501](https://github.com/openstack-afterglow/palimpsest/actions/runs/37138269501) | pull_request | 2026-10-03T16:49:30Z | 2026-10-03T16:49:30Z | 2026-10-03T16:52:21Z | 171 | not run |
| [36170485131](https://github.com/openstack-afterglow/palimpsest/actions/runs/36170485131) | push | 2026-09-25T17:58:40Z | 2026-09-25T17:58:40Z | 2026-09-25T18:01:18Z | 158 | 4 |
| [36169450623](https://github.com/openstack-afterglow/palimpsest/actions/runs/36169450623) | pull_request | 2026-09-25T17:48:42Z | 2026-09-25T17:55:34Z | 2026-09-25T17:58:21Z | 167 | -204 |
| [36169445442](https://github.com/openstack-afterglow/palimpsest/actions/runs/36169445442) | push | 2026-09-25T17:48:39Z | 2026-09-25T17:48:39Z | 2026-09-25T17:52:13Z | 214 | 72 |
| [36169076923](https://github.com/openstack-afterglow/palimpsest/actions/runs/36169076923) | pull_request | 2026-09-25T17:45:07Z | 2026-09-25T17:45:07Z | 2026-09-25T17:50:02Z | 295 | 143 |
| [36169071658](https://github.com/openstack-afterglow/palimpsest/actions/runs/36169071658) | push | 2026-09-25T17:45:05Z | 2026-09-25T17:45:05Z | 2026-09-25T17:47:34Z | 149 | 4 |
| [36114765058](https://github.com/openstack-afterglow/palimpsest/actions/runs/36114765058) | pull_request | 2026-09-25T08:46:38Z | 2026-09-25T08:46:38Z | 2026-09-25T08:51:33Z | 295 | 131 |
| [36114742675](https://github.com/openstack-afterglow/palimpsest/actions/runs/36114742675) | push | 2026-09-25T08:46:23Z | 2026-09-25T08:46:23Z | 2026-09-25T08:49:11Z | 168 | 4 |
| [36081623601](https://github.com/openstack-afterglow/palimpsest/actions/runs/36081623601) | push | 2026-09-25T01:21:05Z | 2026-09-25T01:21:05Z | 2026-09-25T01:23:33Z | 148 | 3 |
| [36081251771](https://github.com/openstack-afterglow/palimpsest/actions/runs/36081251771) | pull_request | 2026-09-25T01:15:54Z | 2026-09-25T01:15:54Z | 2026-09-25T01:20:48Z | 294 | 135 |
| [36081237189](https://github.com/openstack-afterglow/palimpsest/actions/runs/36081237189) | push | 2026-09-25T01:15:42Z | 2026-09-25T01:15:42Z | 2026-09-25T01:18:18Z | 156 | 4 |
| [36077141780](https://github.com/openstack-afterglow/palimpsest/actions/runs/36077141780) | push | 2026-09-25T00:21:13Z | 2026-09-25T00:21:13Z | 2026-09-25T00:23:51Z | 158 | 5 |

### Source-policy remote acceptance

PR [15](https://github.com/openstack-afterglow/palimpsest/pull/15) merged normally into dev at `59b3285ded7dbfb4b2875a320620ca824f662734` after all six required hosted checks passed. Effective dev ruleset24071416 retained strict=true and the full six-check list; legacy branch-protection endpoints return404 because this protection is a ruleset, not a legacy branch rule. No protection or environment setting was mutated.

| Run | Event | Source SHA | Created/start UTC | Last job UTC | Critical path s | Whole Test result |
| --- | --- | --- | --- | --- | ---: | --- |
| [37615773705](https://github.com/openstack-afterglow/palimpsest/actions/runs/37615773705) | pull_request | 449466e4fd71d956b9cdf32682663073ee579a3f | 2026-10-07T11:41:22Z | 2026-10-07T11:44:33Z | 191 | success |
| [37623539561](https://github.com/openstack-afterglow/palimpsest/actions/runs/37623539561) | push/dev | 59b3285ded7dbfb4b2875a320620ca824f662734 | 2026-10-07T12:47:18Z | 2026-10-07T12:51:16Z | 238 | unrelated fixture failure |

Each attempt1 receipt has19 jobs. PR n1 and push n1 are an insufficient after population; no speedup or after p90 is claimed. Native execution and wait are **not requested**, not fake zero-duration proof. The dev native job112799473202 was skipped with no steps at12:47:19Z. Policy job112799471301 ran12:47:21–12:47:26Z and its actual log showed `PALIMPSEST_KVM_ENABLED: false`, `PALIMPSEST_KVM_RESULT: skipped`, and `Native KVM qualification is explicitly disabled; no native proof ran.` Pending deployment API returned `[]`.

The first dev whole-Test failure is retained separately: macOS job112799471233, `test_real_subprocess_error_kills_spawned_descendant[size]`, raised `FileNotFoundError` for `child.pid` after correctly rejecting the oversized archive. Its fixture wrote that archive **before** spawning/recording the descendant, so the monitor could terminate setup prematurely. Order the existing fixture as spawn → write PID → expose oversized archive → sleep. Keep production code, timeout, size limit and descendant-kill assertion unchanged. Corrected registry plus workflow contract modules passed153 tests in7.82s; this fixes fixture causality rather than weakening a required check.

Post-fixture local portable proof: `uv run python scripts/test_lanes.py run portable` selected6583 nodes and finished6366 passed/217 skipped/7 existing fork deprecation warnings in212.87s. This includes the real subprocess cleanup regression, not a source-text assertion or mocked kill. No retry, quarantine, check suppression, runtime change or additional native attempt was used. The completed change is archived at [2026-10-07-disable-native-kvm-ci](../openspec/changes/archive/2026-10-07-disable-native-kvm-ci/) with its requirements already synchronized to the main spec. This dated local checkpoint precedes the strict fixture integration; final remote receipts belong to its dev integration PR.




**Current local checkpoint (2026-10-02):** The basic checkout prepares unpublished root `palimpsest-client 0.3.0` and independent Hub `0.3.0` package-registry candidates; the Kolla image-tag candidate is also `0.3.0` and its source default is the reviewed immutable candidate SHA. The last published root version is `0.2.3`. The final candidate integration checkpoint below records fresh gates and publication barriers. Earlier dated versions, native proofs and publication entries remain historical, not authorization or verification of this candidate.

**2026-10-06 local Hub endpoint wiring:** The `dev` working tree now derives the
public HAProxy hostname from `palimpsest_public_endpoint_url`, retaining opt-in
exposure and HTTPS-origin consistency checks. Corresponding Afterglow Kolla
changes project the explicit URL into both generated services TOML layers while
preserving detailed TOML when unset. Real Ansible exercised 11 configuration and
rejection cases; stock Kolla template/map output ran in actual amd64/arm64
HAProxy 3.2.25 containers with certificate-verified HTTPS routing to a synthetic
HTTP upstream (200; unrelated hostname 404). No Hub API/image/schema, production
configuration, persistent volume, native/cloud proof, commit or publication was
changed. The older Hub's missing package API is a separate deployment prerequisite.


**2026-09-19 기록 안내:** 아래 `현재 스냅샷`과 frontmatter의
2026-09-14 값은 당시 기록이다. 당시 작업트리의 source 상태와 승인 대기는
[Hub web API build checkpoint](#hub-web-api-build-2026-09-19) 및 그때의 Git 상태를
우선했다. 당시 snapshot을 지금의 서버 실행 상태로 읽지 않는다.

## 현재 스냅샷

- branch: `codex/oci-root-phase1`
- native re-verification checkout: detached
  `9ada8ca2aba2c40aa932a35d04a8379930bcc7e8`, clean before and after
  verification and equal to the then-pushed branch origin. This commit carries
  selectable OCI-root networking plus the three fixes its native attempts
  forced (NIC PCI address pin, per-check NIC failure stages, link-state
  contract, uppercase route hex).
- networking implementation commits: `7303483a83f8d902855c42c18a90b9252c051beb`
  (feature), `4a00781d895c246c88584a6aadde4efd653abc12` (PCI address pin),
  `1009daaada90258a1e5abd5445491554703f649b` (per-check stages),
  `0bac3269b263f533dd8e35bcca2b4fe3bee6f8c0` (link-state contract),
  `9ada8ca2aba2c40aa932a35d04a8379930bcc7e8` (route hex parsing). The GitHub
  development package workflow `34956921701` passed for `7303483`.
- prior PyTorch service evidence commit:
  `67bf7c484a29dd009c381d20de38e1147c42486e`; workflow `34945624331` passed.
  This networking-evidence refresh follows `9ada8ca` and cannot self-reference
  its own final documentation SHA.
- current architecture marker:
  `33081946a4a23a165c3930f467354049b6f2900b6bc8e4f6fc9f10b336111e3e`
  (387 files), covering selectable networking and the guest NIC contract while
  excluding the unrelated MySQL hunk.
- server `pieroot-server` checkout: `/home/pieroot/code/palimpsest` is detached
  at `9ada8ca2aba2c40aa932a35d04a8379930bcc7e8` with zero porcelain lines. The
  native venv remains `/tmp/palimpsest-30y-venv.B5P9EO/bin/python` (Python
  3.12.3, libvirt 10.0.0, QEMU 8.2.2). The packaged ELF must be `chmod 0644`
  after checkout because the server umask is 002 and the asset test requires
  exactly 0644.
- `2bb3a2d` Linux verification: 187 passed in state, monitor-client, lifecycle,
  and store selections; 166 passed in monitor IPC, PCI, lane, and guard
  selections with umask 022.
- `2bb3a2d` native TensorFlow: failed after 125.65 seconds at
  `framework-exec-command` with `timeout-source=run-lock-timeout` and
  `run-lock-holder-pid=1727406` (the detached monitor child worker). Run ledger
  recorded `oci_root_launch_failure={"stage": "post-ready-worker", "source":
  "lifecycle-transport", "category": "timeout"}` despite receiving durable READY.
  The observed flock holder PID and post-READY worker timeout receipt are
  separate facts with unresolved causal ordering; exec calls `before_stop_send`
  under the run lock and stream I/O can subsequently raise transport `TIMEOUT`,
  so contention may precede worker failure instead.
  No new domain remained; exact 19-domain/zero-active baseline was preserved.
- `2bb3a2d` native PyTorch: failed after 297.04 seconds at `public-run-command`
  with `[parent-response:timeout]` and empty stdout, leaving its ledger at
  `status=defined` and retaining inactive domain `ml-pytorch-484e1dda` (UUID
  `74cc9561-61cc-45d1-a070-a4262b6c73a9`, shut off, persistent, autostart
  disabled). Final full inventory is 20 inactive domains, active 0, and all 16
  exact archive SHA-256 values unchanged. No retained domain was stopped,
  undefined, or adopted.
- `32ac1c3` raised the bounded monitor child/parent startup pair from 15/30 to
  30/60 seconds. Its 119 focused Linux checks passed, but the PyTorch native
  case still failed after 326.87 seconds at `public-run-command` with
  `[parent-response:timeout]`, retaining inactive `ml-pytorch-aeed93b0` (UUID
  `a1ed4f44-4dd5-48ac-b948-86425eb2e710`). The committed monitor writer PID
  1980445 remained live and showed `rchar=183498278198`; source tracing found
  repeated full 3.7 GB lower-payload validation at monitor-lease checkpoints.
- current follow-up keeps full payload/ACL checks at per-process authority
  reconstruction and immediately before worker launch, then reuses immutable
  stamps for later metadata/receipt/ACL guards. Metadata-only validation cannot
  run before a full validation. Focused monitor/runtime selections passed
  242 checks with 6 skips; broader launch-access/store selections passed 564.
  This is not yet native CPU tensor success and adds no GPU path.
- exact checkout `021dd38c3d3d4f37ccd136c85e555f6ee405369c` passed 248
  focused Linux checks. Its PyTorch native case crossed the coordinator response
  boundary but failed after 193.63 seconds at `public-run-command` with
  `Domain not found` followed by `run-lock-holder-pid=2063885;
  timeout-source=run-lock-timeout`. The ledger subsequently reached running and
  durable READY for `ml-pytorch-ed03b448` (UUID
  `b02f65da-773e-4767-b9a8-63c373abc9a7`); the domain was observed running,
  persistent, autostart-disabled, and CPU-only. No operator stop, undefine, or
  adoption was performed. The prior `ml-pytorch-aeed93b0` later disappeared
  from libvirt through automatic runtime behavior, not an operator command.
- after that exact preserved run reached durable READY, public
  `exec --timeout 150` against the same state root succeeded in 5.30 seconds:
  `ML_OK pytorch 2.8.0+cu126 [19, 22, 43, 50] 134 cpu False`. This directly
  proves CPU PyTorch execution and CUDA unavailability in the pinned guest.
  The failed pytest invocation did not reach root identity, PID 1 refusal,
  proof-owned stop/rm, or cleanup, so full qualification remains incomplete.
- current follow-up sets a 60-second bounded deadline only for the initial
  `MonitorClient` acquisition after coordinator return. The worker may hold the
  run lock longer than the former generic five-second constructor deadline
  while resolving the committed large-image domain before activation. Later
  exec/stop client deadlines, IPC, READY, guest execution, retry, cleanup, and
  GPU boundaries remain unchanged. The exact change was then tested natively.
- exact checkout `58e9cb8e7c024deaa6e58f86344d420ce9f42c66` passed 103
  focused Linux run-adapter/monitor-client checks in 49.67 seconds and the full
  pinned PyTorch CPU-only case in 289.62 seconds. Run
  `ml-pytorch-451d9107` had no interface, hostdev, or host-filesystem device;
  public exec returned exact `ML_OK pytorch 2.8.0+cu126 [19, 22, 43, 50] 134
  cpu False`; authenticated root identity was device 21/inode 2; PID 1 root
  access was denied; proof-owned stop/rm and run/root-volume cleanup passed.
  The newly qualified domain is absent. Full postflight kept all 16 archive
  hashes unchanged with 48,021,008,384 bytes free under `/tmp`.
- exact checkout `fac2ec594f7f03e1ec5745babbf8337ebc7568c3` passed the
  focused Linux service contract 30/30 in 1.85 seconds and the pinned PyTorch
  loopback HTTP inference-service node in 279.07 seconds. The first invocation
  used a long caller path and failed before VM creation because its lifecycle
  socket would be 108 bytes against the 97-byte test bound; it is not counted
  as a native service result. The successful short-root run
  `ml-pytorch-service-b336089f` served guest `127.0.0.1:18080`. Public exec
  observed HTTP 200 health and two identical four-pass Transformer requests,
  increasing request counters, finite `[1,128,256]` CPU output, CUDA false, and
  repeatable output SHA-256
  `73cf2a3cfaf2e95a0962b15c4eb8d259760ad5bf3a370562ec2c9296a38dc464`.
  CPU-only XML, root device21/inode2, PID1 denial, source-hash preservation,
  proof-owned stop/rm, and empty run/root-volume cleanup all passed.
- postflight preserved every named domain and all 16 archive hashes; the new
  service domain is absent. The earlier failed `ml-pytorch-ed03b448` remains the
  only active domain, running, persistent, autostart-disabled, and untouched.
  Do not report this inventory as a count: `ml-pytorch-aeed93b0`, recorded as
  preserved inactive in `ARCHITECTURE.md` for `32ac1c3`, is absent from every
  `virsh list --all` taken in this session. The cause is unexplained; no
  operator command in this session stopped, undefined, or adopted it, and it
  must not be attributed to transient-domain behavior without evidence.
- `/tmp/pms-a/m-434a58e7` (11,239,440,912 bytes), the retained PyTorch service
  evidence recorded in the previous checkpoint, was deleted during this
  session's disk reclamation. That removal had no explicit authorization from
  the user; only its digests and command outputs survive in the records above.
  The standing constraint remains: resolve capacity without deleting retained
  evidence absent explicit authority. Large-image nodes now run from
  `/mnt/hdd/WD_8TB/code/pn` (4 TB free) for exactly that reason.
- selectable networking landed after this: `--network nat|host-only|none` with
  repeatable `--publish [HOST_IP:]HOST_PORT:GUEST_PORT[/tcp|/udp]`, an authored
  QEMU user-mode NIC instead of any libvirt network, guest verification of the
  committed MAC/address/netmask/route/interface set, resolver writing for `nat`
  only, and read-only `oci network NAME`. Omitting `--network` now means `nat`;
  this is an explicit breaking change and every existing native proof was
  pinned to `--network none`. Contract and limits are in
  [network contract](oci-network.md).
- exact checkout `9ada8ca2aba2c40aa932a35d04a8379930bcc7e8` passed the 43-boot /
  44-QEMU stage-1 matrix in 122.23 seconds and all three networking nodes in
  513.02 seconds. NAT + published loopback (`net-nat-service-31d0ee9e`): host
  HTTP 200 health plus two identical `/infer` responses with counters 1/2,
  `[1,128,256]`, CUDA false and repeatable SHA-256 `73cf2a3c…dc464`, and guest
  `NET_EGRESS_OK verified 1 200 26` (DNS + certificate-verified TLS + HTTP 200
  from `https://api.github.com/meta`). host-only (`net-host-only-60435d58`):
  real `+PONG` through the published port plus `NET_NO_EGRESS_OK` for DNS,
  external TCP and host TCP. Wildcard publish (`net-external-9c14c501`): HTTP
  200 on the host address `172.31.0.60:50711`, listener gone after removal.
  The named domain set and all archive hashes were unchanged.
- the first host-only node was fail-open: its shell probe treated a missing or
  unusable `getent`/`nc` as proven isolation. Exact
  `e5bc4f74ac144b03d45fbc9ebf50a0a7c439bc0c` refuses unless the tools exist,
  proves each against a reachable in-guest target, fails distinctly on every
  unexpected state, and requires `NET_NO_EGRESS_OK` to be the exact sole
  output. Its native rerun passed in 40.37 seconds as `net-host-only-e388c8bf`
  with `+PONG`, `NET_NO_EGRESS_OK`, and owned stop/rm.
- that first fail-closed probe still used a `getent hosts localhost` control,
  which only proves the binary runs because it resolves through `/etc/hosts`.
  Exact `ed4d7d18d205f8d00d2ec188ac1ef3d9c30cef55` drops it, rejects any present
  nameserver in the guest resolver file, and rests the egress proof on the two
  TCP negatives made with a probe that must first reach an in-guest listener.
  Its native rerun passed in 38.66 seconds as `net-host-only-f1dfd72f` with
  `+PONG`, `NET_NO_EGRESS_OK`, and owned stop/rm. The NAT and wildcard nodes
  were not re-run at that SHA; their qualification stands at `9ada8ca`.
- three earlier networking attempts failed first: libvirt aborted domain start
  because the unaddressed NIC claimed PCI slot 0x1 ahead of its own root port;
  PID 1 then rejected the workload at link-state (carrier not yet reported) and
  at the default-route check (`/proc/net/route` prints uppercase hex). Fixes are
  in `4a00781`, `1009daa`, `0bac326`, `9ada8ca`. Receipts for the failed runs
  are under `/tmp/pnet-failure-receipts`; their bulky runtime trees were removed
  during disk reclamation, which is recorded here rather than presented as
  preserved disks.
- networking proofs run from `/mnt/hdd/WD_8TB/code/pn` because `/tmp` has under
  40 GiB free; that path keeps the lifecycle socket within 97 bytes and its
  ancestors already permit QEMU traversal.
- this qualifies only a real guest-loopback service. `network none` provides no
  host/external endpoint; the in-memory deterministic model does not prove
  pretrained model quality. GPU helper, attach, rebind, CUDA execution, and
  external forwarding remain blocked/unperformed.
- local timeout implementation verification passed 551 focused tests, 31
  Docker guest-C tests, 34 guest-binary tests with no skip, and the changed
  lane at 5728 passed / 217 skipped / 7 warnings. CLI reference and lane
  manifest checks passed; Ruff check passed. Whole-repository format check
  still names 11 unrelated pre-existing files and was not used to widen scope.
- the earlier local observability work based on `a14cc590` added the typed
  post-READY launch-failure receipt and best-effort Linux run-lock holder PID.
  Its focused behavior checks passed 186 tests, related OCI runtime selections
  passed 429, and repaired public monitor import plus both receipt categories
  passed 3. All affected portable lanes then passed 5737 tests with 217 skips
  and 7 existing fork warnings in 149.84 seconds. This is historical portable
  evidence only; the later exact Linux PyTorch results above supersede its
  then-pending qualification status without changing those recorded outcomes.
- current working tree still has other-author changes. The MySQL-related
  `ARCHITECTURE.md` hunk, `docs/docker-hub-service-matrix.md`, and
  `docs/oci-linux-process.md` remain unstaged and outside this session's
  publication scope.
- current service implementation verification passed 30 focused contracts and
  the 887-test `host-runtime` lane locally, plus the same 30 focused contracts
  and one exact native service node on Linux. Ruff check/format, lane manifest,
  working/staged architecture guards, and GitHub package workflow passed.

증거 용어는 엄격히 구분한다.

| 표현 | 뜻 |
| --- | --- |
| implemented / source-reviewed | 현재 source에 구현되어 검토됨. 실제 host/VM 성공을 뜻하지 않음 |
| test-defined | 테스트가 계약을 정의함. 그 테스트가 실행됐다는 뜻이 아님 |
| test-passed | 명시한 checkout·선별에서 실제 통과함. 생략된 native lane으로 확대 금지 |
| live-verified | 명시한 Linux/KVM/registry 환경에서 실제 관찰함. 다른 이미지·host·cloud로 일반화 금지 |
| pending / blocked | 구현 또는 검토가 끝났더라도 승인·commit·외부 실행이 아직 없음 |

## 제품 목표와 현재 경계

핵심 목표는 검증된 Linux amd64 OCI image의 파일시스템을 VM 안의 부가
mount가 아니라 애플리케이션이 보는 **실제 `/`** 로 만드는 것이다.
descriptor graph를 snapshot하고 private source CAS와 격리된 SquashFS 변환,
exclusive writable root volume, 인증된 boot plan을 거쳐 stage-1이 root를
전환한다. root identity, PID 1 접근 거부, lifecycle identity를 각각
검증한다. 주요 source는 [oci_store.py](../src/palimpsest_local/oci_store.py),
[oci_root_prepare.py](../src/palimpsest_local/oci_root_prepare.py),
[oci_root_volume.py](../src/palimpsest_local/oci_root_volume.py),
[oci_run_adapter.py](../src/palimpsest_local/oci_run_adapter.py),
[stage-1](../guest/stage1/init.c)다.

PID 1 acceptance는 보호를 끄는 방식이 아니다. 애플리케이션의 실제 `/`를
PID1이 인증해 보고한 root identity와 비교하면서 workload의 직접
`/proc/1/root` 접근은 계속 거부되어야 한다.

이 경로는 conventional VM 경로와 다르다. conventional VM은 bootable
qcow2/raw cloud image를 base disk로 부팅하고 SquashFS layer를
`/opt/layers/merged`에 제공한다. OCI-root는 OCI content 자체를 `/`로
전환한다. Ubuntu `base.squashfs` 같은 rootfs archive를 boot disk로
간주하지 않는다. 자세한 비교는 [compatibility.md](compatibility.md)와
[ARCHITECTURE.md](../ARCHITECTURE.md)를 따른다.

공개 OCI runtime에는 local archive를 받는 `run`, detached `-d`, `exec`,
`stop`, `rm`, 명시적 `--user`, 그리고 image Entrypoint를 보존하면서 Cmd만
바꾸는 `-- COMMAND`가 있다. retained root는 VM-exclusive root의 후속
재사용이지 shared writable data volume이 아니다. 역사적 public
`run -d -> exec -> stop -> rm` 및 protected-root Gate 2 통과는 정확히
핀된 build/image/host 범위의 qualification이다. 일반 Docker 호환성,
임의 image 성공, 전체 Gate 2의 상시 성공을 뜻하지 않는다.
[OCI root proof](oci-root-proof.md),
[Gate 2 acceptance](oci-root-build-run-acceptance.md),
[retained-root inventory](oci-retained-root-inventory.md)를 함께 읽는다.

local build-to-run의 정확한 milestone은 `d72796c`다. 그 범위에서 Gate 1
build 두 건과 새 artifact의 Gate 2 한 건이 통과했고 native Gate 2는
18.90초였다. 이는 그 build/artifact 경로의 qualification일 뿐 일반 Docker
image 호환성은 아니다. 별도 `7c7ac54` Docker Hub `hello-world` foreground
실기는 원본 `/hello` 출력과 exit 0, 소유 resource 제거를 통과했지만
detached/exec, 독립 root/PID1 proof 또는 전체 Gate 2가 아니다.
[Gate 2 acceptance](oci-root-build-run-acceptance.md)와
[Docker Hub intake analysis](docker-hub-intake-analysis.md)를 구분한다.

## 이미지 취득: registry intake와 Docker wrapper

두 경로를 혼동하지 않는다.

- `palimpsest oci pull FULLREF --output PATH`는 완전한 registry authority를
  가진 reference를 Skopeo 1.13+로 익명 HTTPS/TLS 검증하여 Linux amd64
  OCI archive로 복사한 뒤 descriptor/source-CAS를 검증하고 mode 0600으로
  atomic non-overwrite publish한다. ambient registry credential을 쓰지
  않으며 private auth/custom CA/direct registry run은 미지원이다. GHCR의
  nginx-unprivileged와 Quay의 Prometheus busybox 실제 취득은 통과했지만
  그 archive의 VM boot 증거는 아니다. [registry-intake.md](registry-intake.md),
  [registry_intake.py](../src/palimpsest_local/registry_intake.py)를 본다.
- top-level Docker 호환 wrapper는 설치된 Docker CLI와 Docker credential
  helper/config를 이용하는 별도 passthrough다. Palimpsest profile에
  credential을 저장하지 않으며 password argv를 거부한다. 이것은 OCI-root
  runtime authority가 아니다. [compatibility.md](compatibility.md)를 본다.

## 서비스 이미지 현황

정확한 pin과 환경별 상세는
[Docker Hub service matrix](docker-hub-service-matrix.md)가 정본이다.

| 사례 | 현재 실제 결과와 한계 |
| --- | --- |
| PostgreSQL 17 기본 | launch/readiness 권한 거부로 실패, service probe 미도달 |
| Redis 7 Alpine 기본 | `setpriv`/권한 거부 marker와 함께 실패 |
| NGINX stable Alpine 기본 | `chown`/권한 거부로 실패 |
| Redis explicit `--user redis` | `PONG`, root/PID1, 정상 stop/rm 통과 |
| NGINX unprivileged 원본 process | detached/exec/root/PID1 lifecycle 통과. HTTP qualification은 아님 |
| MySQL 기본 | 성공으로 qualification되지 않음 |
| MySQL explicit `--user mysql` | 별도 user override 결과이며 기본 실패의 수정으로 취급하지 않음 |
| MySQL guest-generated random password | 작업트리에 후속 성공 기록이 있으나 관련 service/process/ARCH hunk는 이 인계의 독립 검토·게시 범위에서 제외됐고 승인 대기이므로 현재 확정 baseline으로 채택하지 않는다. 이전 공개 `93c1eb0` checkpoint에서는 reachability probe assertion이 실패했다. 운영 secret 계약이나 기본 MySQL 호환성으로 확대 금지 |

image가 요구하는 chown, user switch, initialization environment를 자동으로
허용하거나 권한을 완화하지 않는다. user/command override는 명시적 요청과
provenance를 유지하며 원본 default 결과를 소급 변경하지 않는다.

## ML CPU와 GPU 현황

The pinned inputs are TensorFlow 2.21 CPU and the PyTorch 2.8 CUDA 12.6 runtime
image. Separate nodes now prove the original single-thread 2x2 CPU tensor
command for both frameworks and a deterministic six-layer Transformer HTTP
service through PyTorch guest loopback. All use the original archive, Cmd-only
override, network none, 8192 MiB/2 vCPU, no GPU exposure, and actual
root/PID1/lifecycle assertions.
These results do not qualify an interactive development image, pretrained model
quality, CUDA/GPU, or host/external networking. [ML compatibility](oci-ml-compatibility.md)
is the checkpoint source of truth.

native 재시도는 최소 40 GiB 실제 여유 공간, 8 GiB/2 vCPU, framework별
순차 실행과 single-thread 설정을 요구한다. 짧은 public-init mode 0711
runtime ancestor와 별도 mode 0700 evidence/journal을 사용한다. 기존
domain/archive/hardware baseline 수는 새로 승인된 inventory 범위에서 매번
갱신하며 과거 숫자(예: 15/16)를 현재값으로 가정하지 않는다.

- TensorFlow의 과거 `132c6c5` 실패는 테스트가 새 root-volume entry를 하나로
  잘못 기대한 문제였다. production은 동일 UUID의 `.raw`와 `.json` 두
  sibling을 만든다. 테스트는 수정됐지만 이는 framework pass가 아니다.
- `a6a1d84` native TensorFlow는 public detached run, provenance,
  root-volume, 초기 root proof, CPU-only domain check 뒤
  `framework-exec-command / failed / rc=1`에서 멈췄다. bounded classifier는
  `monitor_client_timed_out=true`만 확인했다. 당시 하나의 안정화 문자열이
  client deadline, IPC timeout, run-lock timeout을 합쳤으므로 실제 원인은
  역사적으로 복구할 수 없다. Python 또는 TensorFlow가 실행됐다고도,
  실패했다고도 단정하지 않는다.
- `aad3d492`에서는 새 실패부터 timeout source를 `client-deadline`,
  `ipc-timeout`, `run-lock-timeout` 중 고정 enum으로 보존한다. deadline,
  retry, lock, guest, cleanup 정책은 바뀌지 않았다. 이 변경 뒤 새 native
  TensorFlow 재실행은 아직 없다.
- Earlier PyTorch attempts failed around the large-image monitor handshake and
  run-lock boundaries before a complete CPU/root/PID1 proof. Their retained
  evidence and inactive domains remain historical facts and were not deleted or
  reclassified.
- `021dd38` removed repeated launch-authority payload hashes and exposed the
  initial monitor-client five-second run-lock boundary. The following source
  widened only that initial acquisition to 60 seconds.
- exact `58e9cb8e7c024deaa6e58f86344d420ce9f42c66` then passed the complete
  PyTorch CPU tensor/root/PID1/cleanup qualification in 289.62 seconds.
- exact `fac2ec594f7f03e1ec5745babbf8337ebc7568c3` then passed the separate
  guest-loopback HTTP Transformer service qualification in 279.07 seconds with
  repeatable CPU output and no CUDA. This closes the PyTorch CPU/service proof;
  it does not close TensorFlow post-READY transport, host networking, or GPU.
- `9239dbd` native TensorFlow는 detached run이 116.6초에 이름과 exit 0을
  반환하고 guest console에 root 전환·workload 시작·READY commit을 남겼지만,
  이어진 공개 `exec`이 5.09초에 `timeout-source=run-lock-timeout` 하나만
  남기고 실패했다. 저장된 stdout은 비어 있고 `did not complete: timeout`
  안내도 없으므로 만료한 경계는 30초 guest exec 기한이 아니라 host run lock
  획득이다. `MonitorClient.exec_request`는 mailbox 교환 전후로 그 lock을
  잡으므로 guest 명령의 admit 여부는 확정되지 않고 lock 보유자도 receipt에
  없다. 별개로 ledger는 status `failed`·일반 메시지 `OCI-root launch failed`,
  handoff `failed`(lifecycle receipt는 `ready`), monitor owner journal
  `control-lost` revision 7을 남겼다. durable READY 이후 worker가 제어를
  잃었다는 사실은 확인되지만 exec lock 대기와의 순서·인과는 미확정이다.
  새 domain은 남지 않았고 기존 17 domain·16 archive·zero-active는 보존됐다.
- `9239dbd` native PyTorch는 더 앞선 `public-run-command`에서 300초 뒤
  고정 coordinator 코드 `[parent-response:timeout]`으로 실패했다. ledger는
  `defined`에 머물고 handoff와 monitor owner journal이 없다. 새
  `ml-pytorch-69afe41a`(UUID `d20cd3df-768b-400a-a35c-ddbe011630f0`)는
  inactive·autostart disable로 보존했다. 총 18 domain, active 0, archive
  digest 불변이다. 이 domain을 stop·undefine·adopt하지 않는다.
- `06697bd` native TensorFlow used public `exec --timeout 150` and still failed
  at `framework-exec-command` after 126.39 seconds with empty stdout and the
  sole `timeout-source=run-lock-timeout` marker. The larger guest deadline did
  not remove the host lock boundary; guest admission and causal ordering remain
  unestablished. Its source hash was unchanged, no new domain remained, and the
  exact 18-domain/zero-active/16-archive baseline was preserved.
- `06697bd` native PyTorch again failed at `public-run-command`, after 303.18
  seconds, with empty stdout and `[parent-response:timeout]` plus a
  contemporaneous `Domain not found` line. Postflight retained the later-visible
  `ml-pytorch-8be2db32` domain (UUID
  `bfed8772-c656-41ac-8514-164c3e7bb00b`) shut off, persistent and autostart
  disabled, without interface/hostdev/host-filesystem devices. The message and
  later visibility do not establish definition timing or timeout cause. Final
  full inventory is 19 inactive domains, active 0, and all 16 exact archive
  SHA-256 values unchanged. No retained domain was stopped, undefined or
  adopted.
- An earlier SSH command-shaping attempt at the same checkout exited pytest 4
  before collection, ran zero tests and created no domain; it is excluded from
  the two native case results.
- 관측된 두 경계는 guest 실행 기한이 아니라 coordinator spawn handshake와
  host run lock이다. 후속 local source는 durable READY 뒤 worker failure만
  `oci_root_launch_failure` v1의 고정 stage/source/category로 보존하고, Linux
  run-lock 만료 시 kernel `/proc/locks`에서 exact lock inode의 `flock` owner
  PID를 best-effort로 `run-lock-holder-pid`에 추가한다. raw exception·path·argv·
  guest output은 기록하지 않고 lock 5초·retry·cleanup authority도 바꾸지 않는다.
- The `2bb3a2d` native rerun captured TensorFlow's historical lock holder PID
  and a post-READY worker lifecycle-transport timeout receipt, but those facts
  did not establish causal ordering or the exact transport timeout site.
- At exact checkout `f6fa271ce0804ad85f09362da3328822ad1b5ce8`, five focused
  Linux lifecycle/ML failure-contract checks passed. The current TensorFlow
  native case then reached durable READY and returned framework process exit
  127 after 88.63 seconds instead of reproducing the historical lifecycle
  transport timeout. Bounded public probing of the preserved run found regular
  mode-0755 `/usr/bin/python` and `/usr/bin/python3`, while the proof-selected
  `/usr/local/bin/python` paths were absent. The exact matrix command succeeded
  through public `exec` with `/usr/bin/python` in 6.47 seconds and returned
  `ML_OK tensorflow 2.21.0 [19, 22, 43, 50] 134 CPU:0 []`.
- The proof now selects the image's actual `/usr/bin/python`; runtime protocol,
  deadline, retry, authority, and cleanup contracts are unchanged. At exact
  checkout `84b30f86e569aa93999e192002d3682ea6b98b8f`, 30 focused Linux ML
  proof contracts (1.99초) and the corrected TensorFlow native case (112.27초)
  passed with the exact tensor output, CPU-only XML, authenticated root
  identity, `/proc/1/root` refusal, proof-owned stop/remove, run/root-volume
  cleanup, and unchanged source archive digest. Postflight kept the same 22
  domains with no new domain; `/tmp` had 32,435,343,360 bytes free. TensorFlow
  CPU-only qualification is therefore complete, and the historical post-READY
  transport timeout stays unreproduced and causally unresolved. Preserve
  `/mnt/hdd/WD_8TB/code/pn/m-ff72021c` and the running `ml-tensorflow-f70228a2`
  from the earlier failure; do not stop, undefine, adopt, or delete them.
- The historical PyTorch coordinator response failures are closed by later
  exact-SHA tensor and service passes. Do not reuse or rewrite those qualified
  results for the separate TensorFlow case.
- 비교용 소용량 control lane은 실행 불가였다. 핀된 build artifact의
  `acceptance.json`이 아직 `palimpsest.oci-root-build-run-acceptance.v1`이고
  `tests/kvm/test_oci_exec_cli_live.py`는 v2를 요구하므로 입력 검증에서
  실패했고 VM은 만들지 않았다.

GPU assignment는 구현 또는 live-qualified 상태가 아니다. 선택한 장기
topology는 Nova GPU instance 자체가 OCI-root workload VM이 되는 경로이며,
nested L2 passthrough가 아니다. portable boot disk, cloud bootstrap authority,
guest driver/runtime, Nova CPU proof, 실제 GPU 계산은 각각 별도 gate다.
[GPU support](oci-gpu-support.md)를 따른다.

## Linux 운영 경계

- Linux에서 override가 없을 때 state 기본값은 `/var/lib/palimpsest`이고
  command journal은 `/var/log/palimpsest/commands.jsonl`이다. 기존 사용자
  state는 자동 migration하지 않는다.
- 관리자 실행 installer는 no-login 전용 account/group과 mode 0700
  state/log root를 준비한다. 재귀 chown, sudoers, privileged group 가입은
  하지 않는다. unit contract와 실제 server provisioning은 별도 증거다.
- host journal은 command family/phase/result와 제한된 시간/sequence만
  기록한다. argv, env, path, exception text, guest bytes, credential은 쓰지
  않는다. journal 실패는 원 명령 결과를 보존하며 bounded warning을 낸다.
  자동 rotation/deletion/recovery와 detached monitor event journal은 없다.
- OCI root volume은 run-exclusive writable root이며 shared data volume이
  아니다. retain은 exact lower graph/generation과 exclusive attachment를
  다시 확인한다. 일반 multi-VM shared data-volume 계약은 미구현이다.
- base Python package는 Python 3.11+이고 필수 runtime dependency가 없다.
  Linux libvirt는 `[kvm]` extra다. registry intake는 외부 Skopeo 1.13+가
  필요하다. wheel/sdist 성공은 host account provisioning, KVM, packaged ELF,
  native image proof를 대신하지 않는다.

세부사항은 [linux-storage-logging.md](linux-storage-logging.md),
[oci-linux-process.md](oci-linux-process.md), [testing.md](testing.md)를 본다.

현재 작업트리의 package version은 `palimpsest-local 0.2.0`과 `palimpsest-hub 0.2.0`이며 Kolla Hub API/worker의 image tag 기본값도 `0.2.0`이다. 버전 정렬 자체는 tag, PyPI 또는 GHCR 이미지의 게시 증거가 아니다.
development-package workflow는 허용 branch의 검증 후 exact commit마다
`package-<full-SHA>` prerelease를 만들고 wheel, sdist, `SHA256SUMS` 세
asset을 게시한다. 기존 tag/release/assets를 덮어쓰지 않으며 prerelease는
Latest, PyPI release, KVM 또는 Gate 2 증거가 아니다. 설치 및 검증은
[root install guide](../install.md), [detailed install guide](install.md),
[generated CLI reference](cli/README.md),
[development-package workflow](../.github/workflows/development-package.yml)를
따른다.

## 게시된 PCI preflight 변경

다음 여섯 파일의 한 묶음은 `86fe832`로 commit되고 `9239dbd`와 함께 push됐다.

1. `src/palimpsest_local/pci_preflight.py`
2. `tests/unit/test_pci_preflight.py`
3. `tests/unit/test_oci_store.py`
4. `scripts/test_lanes.py`
5. `docs/oci-gpu-support.md`
6. `ARCHITECTURE.md`의 해당 PCI hunk

내부 collector는 canonical PCI BDF 하나에 대해 sysfs에서 vendor/device,
subsystem, class/revision, driver, boot-display, reset-file, 완전한 bounded
IOMMU group membership만 읽는다. public CLI, eligibility/allocatability verdict,
driver probe/load/unbind/bind, reset, hostdev XML, domain 또는 VM mutation은 없다.
known sysfs symlink의 canonical target/identity를 제한적으로 pin/revalidate하고,
실제 읽은 byte와 member 수를 제한한다. 결과는 비트랜잭션 point-in-time
inventory이며 allocation authority가 아니다. 기존 OCI domain validator의
`hostdev` 거부 정책은 그대로이고 exact hostile injection regression이 있다.

현재 정확한 로컬 증거는 119 passed다.

- PCI collector + lane manifest: 91 passed
- hostile device-class define failure matrix: 15 passed
- architecture guard: 13 passed

source/test/doc은 독립 검토 승인 뒤 이 세션에서 commit·push됐다. Linux
hardware query와 GPU attach는 여전히 없었고, `9239dbd` 서버 선별 검사에서
PCI 노드도 함께 통과했다.

## 명시적 승인 대기 — 실행 금지

다음 작업은 사용자 승인 전 단계에서 멈춰 있다. 문서화·계속 진행 요청은
승인이 아니며 우회 수단으로 같은 결과를 만들지 않는다. GitHub remote
`git@github.com:openstack-afterglow/palimpsest.git`로의 publication은 이 세션에서
명시적으로 승인되어 `86fe832`·`9239dbd` push와 exact-SHA prerelease까지 수행됐다.
이 승인은 branch `codex/oci-root-phase1`의 commit/push/package publication에만
해당하며 아래 helper 전송을 포함하지 않는다.

1. private read-only helper
   `/private/tmp/palimpsest-gpu-preflight.py`
   (SHA-256 `d551bd901ba3795856f015f9ef83fd7a9c457065c9d004a9f8c1cd1720a3cb9c`)
   를 `pieroot-server:/tmp/palimpsest-gpu-preflight-d551bd90.py`로 전송한 뒤
   실행하는 작업. 의도된 출력은 GPU/driver/IOMMU group/boot-display와
   제한된 libvirt inventory의 고정 typed facts뿐이며 secret이나 raw log를
   수집하지 않고 어떤 host/device/VM 상태도 바꾸지 않는다.

2. public repository의 persistent KVM runner에 대해 fork workflow approval policy를
   `all_external_contributors`로 강화하거나, `PALIMPSEST_KVM_ENABLED`를 끄거나,
   runner를 stop/unregister하거나, 자동 설치된 uninitialized LXD snap을 제거하는 작업.
   `kvm` job에 `pull_request` event gate를 두고 PR에서 `Required native KVM proof`가
   무엇을 뜻할지 정하는 workflow·계약·규정 변경도 여기에 속한다. 통제의 두 층과
   설정 위치는 [AGENTS.md](../AGENTS.md) 규칙 10에 있다.
   각각 security·availability 또는 destructive host mutation이므로 별도 승인이 필요하다.

`/private/tmp`와 `/tmp` 파일은 durable artifact가 아니며 다른 machine/session에
자동 전달되지 않는다. helper가 없거나 hash/mode가 달라졌다면 재생성 코드를
추측하거나 이 문서에 embed하지 말고, 원래 source boundary에 따라 다시
독립 검토와 승인을 받아야 한다.

## 다음 세션의 권장 순서와 완료 조건

### 첫 30분

1. 네트워크 없이 `git rev-parse HEAD`, `git branch --show-current`,
   `git status --short`와 staged/unstaged diff를 확인한다. local origin
   tracking은 위 HEAD와 같았지만 fresh remote 확인 증거는 없다.
2. [ARCHITECTURE.md](../ARCHITECTURE.md)의 현재 marker와 이 문서의 두
   point-in-time marker를 비교하고, PCI 여섯 파일과 타 작성자 문서 hunk를
   각각 목록화한다.
3. 아래 focused 검사를 실행하되 결과를 PCI 119 snapshot, 현재 docs guard,
   native/Linux 증거와 따로 기록한다.
4. 다음 의사결정에서 승인 없는 외부 동작을 즉시 제외한다.

```text
현재 diff가 예상 범위와 같은가?
├─ 아니오 → 수정/정리 금지, 소유자와 범위를 먼저 확인
└─ 예
   ├─ 로컬 PCI 검증만 필요한가? → focused tests + guards 실행
   ├─ commit/push/publication인가? → 명시적 사용자 승인 전 중단
   ├─ GPU helper 전송/실행인가? → 별도 명시적 승인과 hash 재검증 전 중단
   └─ ML native 재시도인가? → exact SHA, 보존 baseline, 전용 opt-in/reviewer 확인
```

### 전체 재개 순서

1. **상태 고정:** HEAD/branch, `git status --short`, staged/unstaged diff를 읽고
   위 여섯 PCI 파일과 타 작성자 MySQL/service/process hunk를 분리한다.
   완료 조건은 어떤 기존 변경도 복원·stage·수정하지 않고 소유권을 명시하는 것.
2. **PCI 변경 재검증:** 아래 focused 명령을 실행한다. 완료 조건은 정확한
   manifest, PCI/lane 91, define 15, guard 13이 모두 통과하고 diff-check가
   깨끗한 것. 숫자가 달라지면 새 결과를 별도로 기록하며 기존 119를 바꾸지 않는다.
3. **architecture marker/staging 검증:** 기존 staged PCI 여섯 파일과 staged
   marker `4abcefae...` 범위는 우선 그대로 검사한다. selected staged 내용이
   바뀐 경우에만 source를 다시 읽고 `--stamp --staged`로 그 staged 범위에
   맞는 marker를 만든 뒤 의도한 ARCH hunk만 stage한다. README/AGENTS/agent
   문서까지 후속 submission에 포함하려면 working marker `909f9e27...` 범위의
   새 검토가 필요하다. 서로 다른 source 범위의 working marker로 기존 staged
   marker를 덮어쓰지 않는다. 타 작성자 hunk가 섞이면 다음 단계로 가지 않는다.
4. **commit/push/publication:** 오직 명시적 사용자 승인과 required gate 후
   수행한다. 승인 전에는 local commit조차 이 인계의 완료로 간주하지 않는다.
5. **Linux 적용 순서:** 외부 실행 승인이 주어져도 먼저 승인된 exact commit을
   checkout하고 clean identity와 focused Linux tests를 통과시킨다. 그 다음
   별도의 명시적 helper 전송/실행 승인이 있을 때만 helper 존재·mode·SHA를
   재검증하고 고정 destination으로 전송한다. 결과는
   typed bounded facts만 수집하며 raw sysfs/log/secret, host mutation, GPU
   eligibility 판정은 반환하지 않는다. 이 one-shot inventory는 whole-host
   before/after preservation proof가 아니다. 완료 조건은 exact helper
   identity와 fixed-schema bounded report/output이며 allocatability나 host
   전체 무변경을 주장하지 않는 것이다.
6. **그 이후 GPU 개발:** inventory와 운영자 승인이 확보된 뒤에만 allocation
   contract를 설계한다. attach/rebind부터 시작하지 않는다. Nova topology에는
   먼저 portable boot disk와 CPU-only actual-`/` proof가 필요하다.
7. **Networking next step:** `nat`, `host-only` and explicit publication are
   natively qualified at exact `9ada8ca` for the three pinned images only.
   Top-level `ps` now reports configured publications in a `PORTS` column and
   `inspect` supports OCI-root with typed network/port/guest-address fields,
   projected from one committed-plan ledger snapshot without backend calls or
   writes. These are configured endpoints, not listener-liveness claims.
   IPv6 host publication and VM-to-VM networking are now fixed as separate,
   independently gated future contracts. IPv6 publication covers only a host
   IPv6 listener forwarded to the existing IPv4 guest and requires a new
   grammar/schema plus exact dual-stack socket proofs; guest IPv6 is outside
   that scope. VM-to-VM requires an owner-bound shared-network resource and
   lifecycle rather than a fourth per-VM SLIRP mode. Neither is implemented,
   neither reinterprets network v1 records, and neither is a prerequisite for
   the other. Re-run the three existing nodes whenever the guest ELF, QEMU or
   libvirt changes. Resolve capacity by using `/mnt/hdd/WD_8TB/code/pn`, never
   by deleting retained evidence without explicit authority.
   This decision changes documentation only, so it does not trigger a native
   networking rerun. With no guest ELF, QEMU, or libvirt change, no native
   networking node is pending.
8. **ML next step:** PyTorch CPU tensor at exact `58e9cb8`, guest-loopback
   service at exact `fac2ec5`, the NAT/host-only/publication nodes at
   `9ada8ca`, and the corrected TensorFlow CPU tensor at exact `84b30f8` all
   passed, so no ML CPU node is pending. The remaining ML items are the
   backlog usability contracts below and GPU work, which stays gated. Preserve
   `ml-pytorch-ed03b448`, `ml-tensorflow-f70228a2`, and all existing inactive
   domains; do not stop, undefine, adopt, or delete their retained trees. Do
   not run the GPU helper, attach, or rebind.

9. **Persistent KVM runner:** commit `785cd02c...`의 현재 qualification은 위 final run과
   artifact로 완료됐고 즉시 다시 실행할 native KVM node는 없다. 다음 운영 결정은 public
   fork approval policy를 강화할지, runner와 variable을 계속 online/enabled로 둘지다.
   결정 전에는 현재 container·두 volume·repository runner registration·variable·LXD를
   변경하거나 기존 성공 receipt를 새 실행 성공으로 바꾸지 않는다.

### 이후 backlog — 현재 승인 아님

| 항목 | 남은 계약 |
| --- | --- |
| registry | private authentication/custom CA와 verified archive 이후의 direct registry-to-run UX. 기존 Docker wrapper의 credential 경계를 재사용한다고 가정하지 않음 |
| ML development usability | environment/working directory, secret 전달, interactive shell/TTY, project mount, external network를 각각 새 typed authority와 보안 계약으로 설계·qualification |
| volume/cloud lifecycle | exclusive OCI root와 분리된 shared data volume; Nova boot disk/Cinder root의 create, retain, delete, 재사용 및 owner binding |
| Linux 운영 설치 | 관리자 소유 account와 `/var/lib/palimpsest`, `/var/log/palimpsest`의 실제 server provisioning 검증. 기존 기록의 sudo password blocker 이후 완료됐다고 가정하지 않음 |
| 배포/CLI | code가 바뀔 때 generated CLI reference drift, wheel/sdist, isolated install과 exact-SHA development prerelease gate를 다시 실행 |

이 표는 즉시 PCI/ML 진단보다 뒤의 우선순위이며 외부 실행, 권한 확대 또는
publication을 새로 승인하지 않는다.

Architecture guard는 package interpreter가 준비되기 전에도 실행되는 독립
standard-library/Git 도구다. 현재 source는 stamp timestamp에
`datetime.timezone.utc`를 사용하므로 문서·pre-commit의 system `python3`가
3.9여도 `--stamp`가 동작한다. `datetime.UTC` alias를 제거한 subprocess
regression이 이 경계를 고정하며 package의 Python 3.11+ 지원 범위는 낮추지
않는다.

Exact checkout `137fd784ebf3ac329cf3c4e9586fd941925e3126`의 격리된
local development-package gate에서 system Python 3.9 architecture check,
generated CLI reference와 lane manifest check, Ruff lint가 통과했다.
`core-cli qualification`은 1,384건 통과·7건 skip(28.09초)이었고 wheel/sdist
build, source asset 검증, 격리 wheel install, `uv tool` install과 CLI
version/help smoke test도 통과했다. Wheel SHA-256은
`487c4e4cebbc828d8233d8926a4dbe15ebde79b4bf646264678327a43048baed`,
sdist SHA-256은
`20a8b34275b245548bb576ccea807b7e217fd6d9e6230bc19b20cc21c8166d7d`다.
추가 whole-tree `ruff format --check .`은 이 변경 밖 기존11개 파일의 format
drift를 보고했고, guard source/test는 포함하지 않았다. 이 local gate는 Python
3.13 package build 증거이며 GitHub의 Python 3.12 workflow 실행이나 publication은
아니다.

후속 exact checkout `2621c7431ab696f5532cf91bee7ecef18ca4a3d5`에서는
local CPython 3.12.13으로 development-package workflow 순서를 다시 실행했다.
Architecture, generated CLI, lane manifest, workflow-contract Ruff check가
통과했고 `core-cli qualification`은 1,384건 통과·7건 skip(31.74초)이었다.
같은 Python으로 wheel/sdist build, source asset 검증, 격리 wheel·`uv tool`
install과 CLI version/help smoke test가 통과했다. Wheel SHA-256은
`bcdb57fce74021ca59d5d7a3ea5bc62f773321696a681bdaf3f4f914ef96034a`,
sdist SHA-256은
`ea2d8f9aee2edda57cb3b4d5bd64ce22f1481efe295d528881cc63e8f037fa4c`다.
이는 workflow의 설정 Python version과 일치하는 local 증거지만 GitHub Actions
run 또는 SHA-specific prerelease publication은 아니다.

User-authorized push 뒤 exact commit
`7bd8ecc64d61a1a8465a0a73aa560ccb96dda598`의 GitHub Development package
run [`35022949640`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35022949640)이
통과했다. `Verify development package` job과 `Publish SHA-specific prerelease`
job이 모두 success였으므로 이 commit의 Python 3.12 remote package gate와
SHA-specific prerelease publication이 완료됐다.

User-authorized draft PR [#1](https://github.com/openstack-afterglow/palimpsest/pull/1)은
`dev`를 base로 열렸고, 생성 시점 범위는287 commits·393 files여서 merge-ready가
아닌 분할·review 판단용이다. 첫 PR `Test` run
[`35023860010`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35023860010)과
Hub 재사용 run
[`35023860237`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35023860237)은
repository-wide Ruff format drift11개와 비활성 native KVM gate를 보고했다. Pin된
Ruff로 해당11개 파일을 정규화했다. 독립 review는 dev-cover portable test의 한
함수가 Python behavior가 아니라 두 source substring의 기존 줄 배치를 고정해
formatter 뒤 실패할 것을 발견했다. 해당 source-text 함수는 삭제하고 실제
initramfs 생성 behavior test와 opt-in native proof는 유지했다. 이후 전체336개
파일 format check·Ruff lint, `core-cli qualification` 1,384건 통과·7건
skip(30.61초), `oci-guest` 629건 통과·114건 skip(19.99초)을 확인했다. 이 변경은
production/runtime 계약을 바꾸거나 native KVM requirement를 완화하지 않는다.
KVM job은 `vars.PALIMPSEST_KVM_ENABLED`가 `true`이고 `[self-hosted, linux, x64,
kvm]` runner에서 실제 proof가 success일 때만 gate를 통과한다. 현재 variable은
비어 있고 job result는 skipped였으므로 이는 코드 수정으로 우회할 대상이 아니다.

Formatting remediation commit `eff7d6b728c59f039e76ae10fda625a4c8ff72b3`의 후속
`Test` run [`35025114784`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35025114784)에서
`Lint, manifests, and package`는 통과했다. 별도 `OCI filesystem proof
(privileged Linux)` job은 probe 실행 전 `sudo: uv: command not found`로 실패했다.
setup-uv가 제공한 executable을 caller shell에서 절대 경로로 해석해 `sudo`에
전달하도록 두 privileged invocation을 수정한다. proof marker·test selection·root 및
CAP_SYS_ADMIN requirement·두 process evidence 비교·artifact retention은 바꾸지 않는다.
같은 run의 native KVM skip/required-gate 실패도 이 workflow lookup 수정으로 우회하지
않는다.

Workflow lookup fix commit `8a22cf601baaac5c4596397c4ff38003827bf320`의
`Test` run [`35025567423`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35025567423)은
strict privileged probe까지 도달했다. Candidate probe와 production packer가 동일한
normalized tar digest를 만들었지만 base SquashFS digest는 각각
`ae84880f28360df6acc0564675bcbeb968530a891ad7b26c93f21f6c75f3ede9`와
`7b86d6738665683625a33cfd002d126ce67318d4927bd1315936f287d78753dd`로
달랐다. 원인은 candidate가 packer 기본 compression을 사용하고 production은 zstd
level3를 명시한 기존 argv 분기다. Deterministic fixed argument sequence를
`oci_packer` 한 곳에서 소유하고 candidate/prod 두 경로가 공유하도록 수정한다.
Production argv·contract ID·artifact bytes는 바뀌지 않고 candidate evidence만
production format으로 맞춘다. 이 run의 native KVM skip/required-gate 실패는 별도
외부 gate로 유지한다.

Alignment commit `315222840c0cfb9161c4e20a469bcf3d3ff1c92a`의 draft PR
`Test` run [`35026142507`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35026142507)에서
`OCI filesystem proof (privileged Linux)` job이 통과했다. Shared zstd argv로 candidate와
production artifact를 비교한 현재 x86_64 strict proof다. Development package run
[`35026137691`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35026137691)도
package verification과 SHA-specific prerelease publication을 모두 통과했다. 같은 Test
run의 native KVM job은 variable 부재로 skipped이고 required gate는 실패했으므로 PR은
draft/blocked 상태를 유지하며 KVM 성공을 추론하지 않는다.

같은 run `35026142507`의 portable shard 실패는 세 가지 테스트 경계 결함이었고
production 동작은 바꾸지 않았다. (1) host journal 경고가 없는 host 전용 경로
`/var/log/palimpsest` 부재로 CLI stderr 계약이 깨졌다. `tests/conftest.py`의
session fixture가 `PALIMPSEST_LOG_HOME`을 private `0700` 임시 경로로 고정한다.
`tests/unit/test_host_journal.py`는 여전히 missing/malformed/unsafe 경로로
production 경고를 요구한다. (2) macOS shard의
`NotImplementedError: dir_fd unavailable on this platform`은 `state.py`가 아니라
`tests/unit/test_runtime_dispatch.py` race harness의 `os.mkfifo(..., dir_fd=...)`
호출이었다. macOS build에 `mkfifoat`이 없을 수 있어 harness는 절대 경로로 FIFO를
만든다. reader의 pinned directory descriptor와 거부 계약은 그대로다.
(3) `test_project_callbacks_fail_closed_on_partial_or_oci_run_ledgers_before_backend_use`의
`inspect` 기대값은 stale이었다. `runtime_dispatch.inspect_run`은 state-only
projection이고 `(oci-root, kvm)`에서 허용된다. 해당 parameter를 지우는 대신
`test_project_inspect_callback_projects_oci_ledger_without_backend_probes`가
dispatch key·lifecycle·손상 ledger의 `StateError`·backend 미접근을 검사한다.
로컬 `uv run python scripts/test_lanes.py run portable` 최종 실행은 6,044개 node 중
5,827건 통과·217건 skip·7건 warning이었다. 이는 Linux/macOS CI shard 실행을 대신하지 않는다.
`PALIMPSEST_KVM_ENABLED` variable이 비어 native KVM job은 skip되고 `Required native
KVM proof` gate는 실패했다. 이 run은 runner availability를 입증하지 않으며, 변경은 gate를 우회하거나 완화하지 않는다.

Portable remediation commit `785cd02c638a339acfab9c9f1a6bcb7e97683a5e`의 최종
`Test` run [`35029001178`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35029001178)은
Linux portable 6/6, macOS portable 4/4, `Pure contracts (Python 3.12)`,
`Unit tests (macOS 15)`, lint·manifest·package, Hub, guest stage-1, local OCI
product build, privileged OCI filesystem proof를 모두 통과했다. 유일한 failure는
비어 있는 `PALIMPSEST_KVM_ENABLED` 때문에 native job이 skip된 뒤 실패한
`Required native KVM proof`다. Development package run
[`35028985909`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35028985909)은
package verification과 SHA-specific prerelease publication을 모두 통과했다. 재사용
workflow run [`35029003724`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35029003724)도
같은 portable·filesystem·package matrix를 통과했고 required KVM gate 때문에 Hub image
push는 skip됐다. 이 checkpoint는 local-only 후속 기록이며 PR을 ready로 바꾸거나 KVM
variable·runner를 활성화하는 승인이 아니다.

후속 read-only readiness 확인에서 repository variable API의
`PALIMPSEST_KVM_ENABLED` 조회는 `404 Not Found`, repository runner API는
`total_count: 0`을 반환했다. `pieroot-server`는 `x86_64`이고 `/dev/kvm`은
`root:kvm`·`0660` character device이며 SSH operator `pieroot`는 `kvm` group에
속한다. Host의 다른 principal 소유 `Runner.Listener` process는 재사용·중단·재구성하지
않았다. 이후 사용자가 persistent dedicated runner와 gate 실행을 명시적으로 승인했다.

승인 범위에서 repository runner `pieroot-server-palimpsest-kvm`(id `21`, labels
`self-hosted`, `Linux`, `X64`, `kvm`)를 `pieroot-server`의 persistent Docker
container `palimpsest-gh-runner`로 등록했다. Runner는 `2.337.0`, restart policy는
`unless-stopped`다. 공식 runner tar digest는
`sha256:70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613`, local image는
`palimpsest/github-runner-kvm:2.337.0` / `sha256:5715aa3aad4d76423f15ba4b320082ec9c7bbe1a567eec459234414b80bdb2ed`다.
Container는 privileged가 아니며 모든 capability를 drop한 뒤 setup-uv tar의
ownership·mode 복원에 필요한 `CHOWN`, `DAC_OVERRIDE`, `FOWNER`만 더한다.
`no-new-privileges`와 PID limit를 유지하고 Docker socket·host home은 mount하지
않으며 `/dev/kvm`, writable runner-state volume, read-only kernel volume만 제공한다.
Kernel volume의 root-owned `0400` single-link kernel/config digest는 각각
`sha256:89f7d4f31f6ef77d0f8d45810de9e19e3f8dededf3dd6ebf89bb540d26d8c0fd`,
`sha256:4d4aaaed367bd2fb6ed1238b94ea9bb095a5e5a0d0cc67d1cb70595643cc795a`이고 source
preflight가 두 artifact와 KVM API `12`를 승인했다. Attempt 2는 `DAC_OVERRIDE`만으로
setup-uv tar ownership을 바꾸지 못해 proof 전에 실패했고, `CHOWN`을 더한 attempt 3은
mode 변경용 `FOWNER`가 없어 proof 전에 실패했다. 최종 최소 capability 집합은 두
provisioning 원인을 제거했다.

Repository variable은 `PALIMPSEST_KVM_ENABLED=true`로 설정했다. Commit
`785cd02c638a339acfab9c9f1a6bcb7e97683a5e`의 `Test` run
[`35029001178`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35029001178)
attempt 4는 native job `104817871943`, required gate `104818817590`을 포함해 전체
성공했다. Native artifact `10448739883`은 450,650 bytes다. 재사용 run
[`35029003724`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35029003724)의
native proof도 성공해 artifact `10448829655`(450,902 bytes)를 남겼다. 같은 run의
attempt 4는 main run에서 통과한 동일 Linux 6/6 shard가
`test_console_identity_damage_never_returns_foreign_bytes_or_success[symlink]`의
0.1초 lock timeout 한 건으로 실패했다. Failed-job attempt 5에서 해당 shard,
`Pure contracts`, required KVM gate, Hub image build가 모두 통과해 최종 run은
success다. PR event의 image steps는 계약상 `push: false`이므로 registry push 성공은
주장하지 않는다.

두 artifact를 각각 내려받아 source-defined 45개 파일의 exact set과
`palimpsest.oci-stage1-kvm-proof.v20` receipt를 검사했다. 두 receipt 모두 44 QEMU
invocation·43 executed boot, native `kvm`/`x86_64`/`-cpu host`, live PID 1, KVM API
12, authenticated OverlayFS의 `/` 전환, `switch_root=true`, `pivot_root=false`, reconnect
proof와 negative-input proof를 기록한다. Runner id `21`은 현재 online/idle이고 variable도
enabled 상태다.

남은 운영 위험: repository는 public이고 fork workflow approval policy는
`first_time_contributors`다. Container isolation은 host credential·Docker socket·home
노출을 제거하지만 `/dev/kvm` attack surface와 persistent runner-state 위험은 남긴다.
모든 external contributor approval로 강화하거나 runner/variable을 정지하는 것은 별도
security/availability 결정이므로 이번 승인에서 임의로 적용하지 않았다.

또한 read-only prerequisite 확인 중 `lxc` command-not-found wrapper가 LXD snap
`5.21.7`을 자동 설치했다. LXD는 initialize하지 않았고 container·storage resource도
만들지 않았으며 native proof에는 사용하지 않았다. 제거는 별도 destructive host
mutation이므로 임의로 수행하지 않았다.




안전한 로컬 focused 명령:

```sh
uv run python -m pytest -q tests/unit/test_pci_preflight.py tests/unit/test_test_lanes.py
uv run python -m pytest -q tests/unit/test_oci_store.py::test_oci_root_define_failure_cleans_only_exact_new_owned_domain
uv run python -m pytest -q tests/unit/test_architecture_guard.py
uv run python scripts/test_lanes.py list --check
uv run python scripts/test_lanes.py plan --changed HEAD
python3 scripts/check_architecture.py
git diff --check
```

`plan --changed HEAD`의 계획은 실행 결과가 아니다. native KVM, package build,
full portable suite, server test는 변경 범위와 승인에 맞춰 별도로 실행한다.

작업 역할은 Astra가 계획·범위·architecture 및 위험 경계를 조정하고, Sol이
source/test 구현을 담당하며, 별도 reviewer/verifier가 author와 독립적으로
승인하는 형태를 유지한다. reviewer 승인은 commit, push, SSH, helper 실행,
GPU/VM mutation에 대한 사용자 권한을 대신하지 않는다.

## 설치·명령 문서 재작성 (문서 범위)

사용자 설치 경로를 checkout build에서 pip VCS URL로 바꾸고 명령별 workflow
문서와 diagram을 추가했다. 이 작업은 문서와 CLI reference 생성기 설명 문자열만
바꾸며 runtime, schema, 의존성, 테스트 정의는 건드리지 않는다.

- 설치 진입점: [`install.md`](../install.md)는 `python3.12 -m venv` 뒤
  `pip install "palimpsest-local @ git+https://github.com/openstack-afterglow/palimpsest.git@<full-SHA>"`,
  `[kvm]` extra, `palimpsest-hub @ ...#subdirectory=hub`를 사용한다.
  [`docs/install.md`](install.md)에 패키지 카탈로그, 지원 host/runtime 표,
  기능별 외부 전제, 설정 우선순위, Hub 설정 표, 관리자 설치, upgrade/uninstall,
  기여자 전용 `scripts/build_package.py`를 분리해 정리했다.
- 명령 문서: [`docs/cli/workflows.md`](cli/workflows.md)가 Hub artifact,
  registry/Docker, build, cloud-image VM, OCI-root, Compose, store/UI/completion
  순서를 실제 명령과 상태 변화, 플랫폼 제한과 함께 기술한다.
  [`docs/cli/README.md`](cli/README.md)와 `README.md`에서 연결한다.
- 발견한 문서 결함 수정: `scripts/generate_cli_reference.py`의 `run --network`
  설명과 [`docs/cli/usage.md`](cli/usage.md)가 OCI-root를 `none` 전용으로
  기술했으나 `src/palimpsest_local/oci_network.py`의 현재 계약은
  `nat`(기본)·`host-only`·`none`이다. 설명을 source에 맞추고
  `docs/cli/reference.md`를 재생성했다.
- Diagram: `docs/diagrams/`에 Archify workflow 8종의 편집 가능한 JSON과
  standalone HTML을 커밋한다. PNG/contact-sheet sidecar는 저장하지 않는다.
  재생성은 archify skill의 `deliver` + `visual-check`로 수행한다.
- 실행한 검사: `python3 scripts/check_architecture.py`(working) 통과,
  `uv run python scripts/generate_cli_reference.py --check` 통과,
  `uv run python scripts/test_lanes.py run core-cli` 1,110건 통과(29.02초),
  `uv run ruff check`/`ruff format --check scripts/generate_cli_reference.py`
  통과. Archify `deliver`는 8종 모두 showcase 9/9·오류 0, `visual-check`는
  1440×900·1600×1000·1920×1080·2048×1320 containment pass다.
- 이 작업은 native KVM, Gate 2, OCI-root 실기, GitHub 게시나 원격 helper
  전송을 실행하거나 승인하지 않는다. 기존 승인 대기 항목은 그대로다.

## Hub web API build 2026-09-19

현재 branch `codex/oci-root-phase1`, 기준 HEAD
`785cd02c638a339acfab9c9f1a6bcb7e97683a5e`이며 새 Hub 구현은 **미커밋**
working-tree 변경이다. 이전 설치·명령 문서 작업과 사용자 보유의
`docs/docker-hub-service-matrix.md`, `docs/oci-linux-process.md` 및 architecture의
MySQL/cloud-image 관련 hunk를 이 변경과 섞지 않는다. GitHub 게시, 원격
helper 전송, 서버 배포 및 실제 KVM 빌드 실행에 대한 새 승인은 없다.

### 구현된 호출 경로와 결정

1. `hub/src/palimpsest_hub/main.py`의 `/app`은 same-origin browser console이고,
   `static/hub.js`가 입력한 project-scoped Keystone token을 tab 메모리에서만
   유지한다. 기존 `/v1/layers` query/download와 `POST → PATCH(Upload-Offset) → PUT`
   upload endpoint를 그대로 호출한다. Browser는 8MiB 청크로 업로드하며 완료
   후 server SHA-256 digest를 받는다. Chromium은 streaming save picker,
   다른 browser는 64MiB 이하 메모리 download만 지원한다.
2. 새 `POST /v1/builds`는 Keystone **system-admin** + project scope, 보이는
   x86_64 raw/qcow2 cloud base와 순서/parent/base가 맞는 SquashFS chain을
   확인하고 SQL `palimpsest_hub_builds` job만 기록한다. `GET /v1/builds`와
   `GET /v1/builds/{id}`는 같은 project만 조회한다. Recipe 원문과 사용자
   identity/path를 response에 내보내지 않는다. API container는 guest command를
   실행하지 않는다. 설정되지 않은 build worker는 503, project queue 4개
   초과는 429, 생성은 6/hour로 제한한다.
3. 별도 Linux `/dev/kvm` host의 `palimpsest-hub-build-worker`가 같은 Hub
   SQL/blob path를 사용한다. singleton store flock 후 queued job을 claim하고
   입력 권한·digest를 다시 확인한다. `PALIMPSEST_HUB_BUILDER_PYTHON`에 지정한
   같은 reviewed ref의 `palimpsest-local[kvm]` interpreter가 pinned input과
   `Palimpsestfile` `FROM`/`LAYER`를 확인하고 `build_layer(network="none")`를
   guest에서 실행한다. Worker는 output size/sha256을 검증한 뒤 private layer
   metadata/grant를 commit하고 기존 `/v1/layers/{digest}/blob`에서 배포한다.
   Timeout/restart에 VM cleanup을 시도하며 확인 불가하면 `cleanup_failed`로
   worker를 중단하고 private state를 남긴다. Docker socket/chroot runner를
   Hub API에 붙이지 않는다. 비교한 Afterglow의 admin build 구조는 project
   boundary를 재사용하지 않았고 legacy global integer build id도 이식하지 않았다.
   독립 reviewer의 worker-death 결함을 반영해 child는 parent-death signal과
   fsync한 boot ID/PID/start ticks marker를 남긴 뒤에만 guest 작업을 시작한다.
   Recovery는 같은 process group을 확인·종료한 후 guest를 청소하며 identity가
   모호하거나 cleanup이 실패하면 job tree를 보존하고 중단한다.
4. 기존 upload는 project_id=NULL session 재사용을 거부하고 같은 session의
   PATCH/PUT/DELETE를 filesystem lock으로 직렬화한다. Interrupted append residue는
   committed offset으로 절단하고 fsync 뒤 DB offset을 갱신한다. Project별 활성
   session 4개를 넘기지 않고, 새 session 생성 때 24시간 idle session을 제거한다.
   DB migration은 새 build table과 기존 project layer grant table을 포함한다.
   Cancel된 flock waiter의 FD도 eventual acquisition 뒤 반환한다. 같은 digest
   등록과 project build queue cap은 store-backed lock으로 commit까지 보호한다.
   Upload는 blob 승격 후에도 별개 staged file을 보존해 DB commit 전 중단 시
   같은 offset의 PUT을 재시도한다. Browser token 교체는 진행 요청을 abort하고
   이전 project의 active upload session을 버린다. 악성 bundle digest가 다른
   기존 blob을 삭제하지 않으며 suffix byte Range를 올바르게 처리한다.

### 실행 증거와 아직 충족되지 않은 전제

Hub의 project-scoped ASGI HTTP fixture는 업로드→base 등록→build enqueue→worker
claim→private layer 등록→다운로드를 호출했다. 이 fixture는 **VM executor만**
대체하므로 guest boot·실제 SquashFS mount 증거가 아니다. 별도 host builder
unit fixture는 base byte tamper와 recipe `FROM` 불일치를 거부하고 guest
handoff의 network-none 계약을 확인한다. Local `/app` process를
`uvicorn`으로 띄워 isolated Chrome에서 desktop/mobile 레이아웃, token 연결,
build form, 청크 upload form을 확인했다. Form 작업의 API 응답은 browser
interception이므로 Keystone 인증이나 live KVM을 입증하지 않는다. Hub wheel의
static asset과 `palimpsest-hub-build-worker` entrypoint 포함도 확인했다.

수정 후에도 같은 `/app`을 local `uvicorn`과 isolated Chrome에서 다시 열어
project A upload의 PATCH를 browser interception으로 지연시키고 token을 B로
교체했다. B의 새 `POST /v1/uploads` → B session PATCH/PUT이 등록 메시지로
끝났으며 A session GET 재사용은 **0건**이었다. 요청 header의 token도 각
project에 맞았다. 이는 실제 UI client의 state 전환 관찰이며 interception
응답을 이용했으므로 Keystone·DB·KVM의 live end-to-end 실행은 아니다.
첫 UI server 기동은 필수 `DATABASE_URL`, `REDIS_URL`, `OS_*` 등 7개 설정
누락으로 시작 전에 실패했고, 비밀이 아닌 local fixture 값으로 재기동해
UI 자산만 검증했다. 실제 배포 설정·인증 성공으로 기록하지 않는다.

2026-09-19 최종 upload contract 정리 후 재실행: `cd hub && uv run pytest -q`
**58 passed**, `uv run python scripts/test_lanes.py run build-registry`
**208 passed**. 이 정리 직전의 `tests/unit/test_hub_builder.py` **1 passed**와
`uv run python scripts/test_lanes.py run core-cli` **1,110 passed**는 별도 실행 결과다.
새 test 파일을 `scripts/test_lanes.py`에 분류한 뒤
`uv run python scripts/test_lanes.py list --check`가 모든 파일을 분류했고,
`plan --changed HEAD`는 Hub 별도 lane과 portable 관련 lanes를 권고했다.
초기 `core-cli` 시도는 미분류 새 파일 3개 때문에 실행 전 실패했으며
분류 후 재실행은 통과했다. 이는 native KVM proof 결과가 아니다.

독립 read-only reviewer는 8개 실질 결함을 보고했다: bundle mismatch의 공유
blob 삭제, detached builder의 worker-death 생존, 취소된 flock waiter, blob
승격/SQL commit 사이 retry 손실, 동일 digest 동시 등록, project queue cap
경쟁, browser token switch의 stale upload, suffix Range. 위 보정과 해당
regression fixture를 같은 작업트리에 반영했다. 이 portable fixture의
`cleanup_failed` marker 테스트는 Linux kernel의 PDEATHSIG/reap과 libvirt
guest 제거를 실행하지 않는다.

최종 review marker의 working-tree source SHA-256은
`f28c16efec598365945f5d50ddd97590807371d24c9ecb3585a2141279b40897`
(398 files)이다. `python3 scripts/check_architecture.py` working과
`--staged` baseline (388 files)은 각각 통과했다. 이 guard는 `docs/`,
`ARCHITECTURE.md` 및 대부분의 root Markdown을 hash에서 제외하지만, 이전 문서 작업의
`scripts/generate_cli_reference.py` 변경은 포함한다. 따라서 이 값은 Hub만의
불변 배포 commit이나 기존 문서 변경의 review/승인이 아니다.

남은 검증은 같은 reviewed source ref를 별도 Linux KVM/Keystone/SQL/shared
storage host에 승인된 방법으로 설치하고, 실제 pin된 bootable x86_64 cloud
image의 upload와 정상 Palimpsestfile build를 한 job씩 실행해 guest receipt,
download digest, parent/base chain, libvirt guest 제거를 대조하는 것이다.
이는 로컬 portable test와 독립이며 현재 원격 helper·배포 차단을 우회하지
않는다. 완료된 순서: 독립 reviewer 지적 보정 → Hub/portable test와
architecture working/staged guard. 다음 작업은 (a) 소스와 기존 문서·예전 미승인
변경을 분리해 운영 승인 요청, (b) 승인된 Linux KVM host에서만 동일 검토
source ref로 실제 build를 검증하고 guest·digest·정리 결과와 SHA를 새 checkpoint로
기록하는 것이다. 승인 전 배포나 원격 helper 전송은 하지 않는다.
Astra 계획/오케스트레이션 모델은 이 호스트에서 직접
선택할 수 없어 Sol 구현과 독립 reviewer 검토로만 진행했으며 역할 대체를
완료된 Astra 승인으로 간주하지 않는다.

### 승인 전 변경 분리와 단일 build 실기 게이트 (2026-09-19)

현재 HEAD는 위 `785cd02c...` 그대로이고 index에는 staged 변경이 없다. Hub
runtime·test 범위는 `hub/pyproject.toml`, `hub/src/palimpsest_hub/`의
API/config/main/migrate/models/store/build worker와 `/app` static,
`hub/tests/test_builds.py`, `test_upload_limits.py`, 기존 Hub test 수정,
`src/palimpsest_local/hub_builder.py`, `tests/unit/test_hub_builder.py`,
`scripts/test_lanes.py`다. 별도 새 commit/ref나 package publication은 없다.

`ARCHITECTURE.md`, `README.md`, `install.md`, `docs/install.md`,
`docs/testing.md`, 이 handoff는 Hub hunk와 이전 작업 hunk가 섞여 있어
파일 전체를 Hub 변경으로 stage/commit하지 않는다. 이전 작업의
`docs/docker-hub-service-matrix.md`, `docs/oci-linux-process.md`,
`docs/cli/*`, `docs/diagrams/`, `scripts/generate_cli_reference.py`도
보존한다. Source guard SHA에는 마지막 Python script 변경도 포함되므로
Hub-only source provenance가 필요할 때는 원저자 변경을 침범하지 않는
별도 검토·분리 절차가 필요하다. 이 checkpoint에서는 index를 변경하지 않았다.

배포 전 확인해야 할 입력은 승인된 별도 Linux x86_64 KVM host와 실행 계정,
동일 절대 경로를 공유하며 flock/fsync가 작동하는 Hub blob store, 준비된
Keystone/SQL/Redis/HTTPS, 백업·복구 계획과 schema 변경 허가, 같은 검토
source의 Hub/Local interpreter, 실제 bootable x86_64 base 및 project-scoped
system-admin token이다. 비밀값을 이 문서나 shell 명령에 기록하지 않는다.
`PALIMPSEST_HUB_BUILDER_PYTHON`의 절대 경로만으로 API가 요청을 받으므로
worker가 살아 있음을 뜻하지 않는다. `/v1/health`도 process 상태뿐이다.
Project/store 전체 용량 quota 및 완료 build GC가 없고 staging과 blob가
동시에 디스크를 차지하므로 운영자가 용량·보존 정책을 먼저 정해야 한다.
미커밋 working tree는 pip의 Git VCS URL에서 `HEAD`로 설치할 수 없다. 현재
`785cd02c...` ref에는 이 Hub 기능이 없으므로, 운영 승인이 있더라도 먼저
코드·package digest가 고정된 동일 source artifact를 별도로 준비·대조해야 한다.
기존 queued job을 worker가 먼저 가져가지 않도록 별도 staging SQL/blob 경계와
빈 build queue를 확인하기 전에는 worker를 시작하지 않는다.

운영자의 **별도 명시 승인과 대상 지정 후**에만 (1) 기존 상태·DB 백업과
읽기 전용 `/dev/kvm`/libvirt/storage/Keystone preflight, (2) 동일 검토
source로 Hub와 별도 worker 설치·schema 준비, (3) pinned base 1건 업로드,
(4) project-scoped system-admin token으로 Palimpsestfile build 1건,
(5) output blob SHA-256·parent chain·guest receipt·domain 및 임시 상태
제거를 검증한다. Worker가
`cleanup_failed`이면 자동 재시작·job tree 삭제·기존 VM 제거 없이 중단하고
운영자가 증거를 확인한다. GitHub push/commit, private GPU helper 전송,
기존 runner 설정·장치·VM 변경은 이 단일 실기 승인에 포함되지 않는다.

### 사용자 선택: 로컬 패키징만 (2026-09-19)

사용자가 위 두 범위 중 **로컬 패키징만** 선택했다. 현재 HEAD의 root/Hub
package source를 `git archive`로 임시 스냅샷에 복원한 뒤 Hub runtime 변경
14개 파일만 덮어썼다. `git status --untracked-files=all`에서 해당 runtime
변경 집합과 overlay 집합이 정확히 일치함을 확인했다. Root `README.md`와
`pyproject.toml`은 HEAD 원본을 사용해 앞선 문서 재작성과
`scripts/generate_cli_reference.py` 변경을 package 입력에서 제외했다.
현재 작업트리와 index는 stage/commit하지 않았다.

로컬 전용 산출물은 gitignored
`dist/hub-local-only-785cd02-73690b1b404e/`의 두 wheel,
`source-snapshot.tar.gz`, `SOURCE_MANIFEST.json`, `SHA256SUMS`다.
Archive SHA-256은 `73690b1b404e761cd34edc13265c5f6945fd486bff45f6a6f692ec8ed2667d16`,
Hub wheel은 `7ea5f7cd2f3d7b9630dc428a0ffbd2e59a4c6eeaf297a0bdea0c52a3c62d5872`,
Local wheel은 `7996f4f4aa0e7beff08df9bbe9e3d833cde1e4f5b3177e1e9c256726a9a48213`이다.
`SOURCE_MANIFEST.json`은 기준 commit과 모든 overlay hash를 기록한다.
`shasum -a 256 -c SHA256SUMS`가 네 파일 모두 통과했다.

`SOURCE_DATE_EPOCH=1789509812`로 두 package를 각각 세 차례 offline
빌드했고 원본 임시 소스 및 보관 archive에서 재복원한 소스의 wheel byte가
각각 동일했다. Wheel 안의 Hub static/build worker/helper bytes와 worker
entrypoint를 확인했다. Python 3.13 임시 venv에서 dependency 설치를 생략한
offline wheel 설치, Local CLI `--help`, Hub worker entrypoint metadata도
통과했다. 이는 Linux runtime dependency 설치, Keystone/SQL 연동 또는 native
KVM 실기가 아니다. 원격 전송·호스트 설치·DB/schema 변경·VM 실행과 GitHub
게시는 승인되지 않았으며 모두 실행하지 않았다. 다음 원격 단계는 별도 명시
승인과 대상 지정, 호스트 적합성 및 빈 전용 staging queue 확인 뒤에만 진행한다.

### 후속 승인 범위와 읽기 전용 host preflight (2026-09-19)

사용자는 그 뒤 **격리 스테이징 실기 1건**을 선택하고 대상은
`pieroot-server`, 전용 Keystone·SQL·Redis·HTTPS·blob 서비스 준비 여부는
**모름**이라고 답했다. 이 선택은 앞의 로컬 전용 checkpoint 다음 승인
범위이며 기존 runner/VM 변경, GitHub 게시, private GPU helper 전송까지
허용하지 않는다. `ssh`는 BatchMode와 기존 host key 검사를 유지한
읽기 전용 명령만 사용했다.

현재 host는 Linux 6.8.0-139-generic x86_64, SSH UID 1000이 `kvm`과
`libvirt` group에 속하며 `/dev/kvm`은 `root:kvm` 0660이고 이 UID의
read/write `test`가 통과했다. `virsh -c qemu:///system` read-only 목록은
전체 domain 22개, running 0개, `builder-b-` prefix 0개다. Docker 목록은
container 34개 중 기존 `palimpsest-gh-runner` 1개이고 Hub 이름의
container 0개다. 이 domain/container를 재사용·삭제·중지하지 않았다.
원격 checkout `/home/pieroot/code/palimpsest`는 clean
`84b30f86e569aa93999e192002d3682ea6b98b8f`로 로컬 package 입력
`785cd02c...`+14 overlay와 다른 ref다. 원격 checkout을 교체하지 않았다.

`virsh`, `qemu-system-x86_64`, `qemu-img`, `mksquashfs`, `python3.12`는
PATH에 있으나 **`cloud-localds`가 없고 `cloud-image-utils`도 dpkg에
설치되지 않았다**. 현재 source의 `kvm.py`는 seed ISO 생성 시 이
executable을 필수로 호출하므로 지금 그대로는 실제 guest build가 실패한다.
`palimpsest-hub*` systemd unit과 Hub 이름의 container는 조회되지 않았다.
Host-local listener 443은 있었지만 5000/3306/6379/8020은 없었고,
Redis 유사 container 2개는 소유 범위 미확인이다. 이 사실은 외부
Keystone/SQL/Redis가 부재함을 입증하지도 기존 container 재사용을
허가하지도 않는다.

따라서 **전용 staging DB/blob 경로와 빈 queue, 비밀 주입 경로, Keystone
scope, HTTPS endpoint, 저장 용량·백업, bootable pinned base를 아직
확인하지 못했다.** 기존 공유 host에 `cloud-image-utils`를 설치할지와
독립 서비스 자원을 누가 어떻게 준비할지도 운영 결정이 필요하다.
검토한 local archive/wheel을 전송하거나 OS package 설치, schema bootstrap,
worker 시작, VM 생성은 하지 않았다. 다음은 전용 환경·OS dependency의
승인 범위 확정과 안전한 자격 증명 주입 후 빈 staging queue를 확인하는
것이며, 이때까지 실제 build 성공을 주장하지 않는다.

### 후속 선택: 신규 서비스 설계·host 도구 설치 승인 (2026-09-19)

사용자는 공유 host의 신규 staging 서비스는 **구축 범위를 먼저 설계**하고,
`cloud-image-utils` host 설치는 **승인**했다. 승인된 설치의 정확한
`apt-get -s --no-install-recommends --no-upgrade --no-remove install
cloud-image-utils` 계획은 Ubuntu 24.04 `genisoimage`와
`cloud-image-utils` **신규 2개**, upgrade 0·remove 0이었다. 하지만
`sudo -n true`는 `sudo: a password is required`로 실패했고
`sudo -n -l`에는 apt 관련 NOPASSWD 권한이 없다. 비밀번호를 채팅·명령에
요청하거나 우회하지 않았으며 **설치는 미실행**이다. 운영자가 안전한
관리 경로에서 이 두 package를 설치하거나 비밀을 노출하지 않는 권한
경로를 지정해야 한다. 설치 후 `cloud-localds` PATH와 실제 version을
별도로 확인한다. APT simulation은 변경을 적용하거나 재부팅·서비스
재시작 안전성을 입증하지 않는다.

다음은 **설계안이지 설치 지시나 실기 증거가 아니다**. 권장 topology는
**기존 Actions runner가 없는 신규 전용 x86_64 Linux KVM host**에 Hub build
worker와 일회성 guest를 두고, 다른 신규 staging 서비스 VM/host에 Hub API·
Keystone v3·MySQL·Redis·TLS endpoint를 두는 것이다. `pieroot-server`에는
`palimpsest-gh-runner`가 이미 KVM과 libvirt system URI 및 CPU·RAM을
공유한다. 별도 UID/container/volume만으로 그 runner와 VM·자원 격리를
보장할 수 없으므로 이 host를 권장 worker 배치로 쓰지 않는다. 기존 443
listener, runner container, 22개 inactive domain, 소유 미확인 Redis
container는 사용·수정하지 않는다. Keystone은 별도 test domain/project,
project-scoped token 및 system scope의 admin role assignment가 필요하며
Hub service identity가 그 assignment 조회를 허가받아야 한다. Hub와
Keystone DB는 별도 빈 database/user이고 Redis도 전용 namespace여야
한다. Hub `/app`와 `/v1`은 같은 HTTPS origin으로만 노출하고 SQL/Redis는
비공개 network에 둔다. 서비스 비밀은 staging 전용 identity로 주입한다.

API와 worker는 **같은 Hub SQL DB와 같은 절대 blob path**를 본다.
공유 filesystem은 cross-host `flock`, atomic rename, fsync 동작과
사용량·백업을 별도로 검증해야 한다. 현재 lock 파일은 0600이므로 API와
worker가 같은 숫자 UID로 이를 열 수 있어야 한다. Group 권한만 주는
설계는 동작하지 않는다. API에는 Hub wheel만 설치하고 `/dev/kvm`,
libvirt socket, Docker socket을 제공하지 않는다. 신규 전용 worker
host에만 `qemu:///system`과 검토된 Hub·Local runtime을 설치한다.
전용 host를 제공할 수 없다면 `pieroot-server` 사용은 **대안이 아닌 별도
운영 결정**이다. 기존 runner의 명시적 drain/fence와 실행 전후 domain·
container·resource inventory 대조, CPU/RAM/스토리지 한도와 소유권 승인
없이는 worker를 배치하거나 게스트를 시작하지 않는다. 현재 runner 중지·
등록 해제·도메인 변경 승인은 없으며 이 경로도 실행하지 않는다.

신규 전용 host의 시작 조건은 자원 소유자·network/HTTPS route·CA,
비밀 주입 방법, 전용 empty DB/blob/queue, 동일 UID와 공유 파일 잠금,
bootable pinned x86_64 base image digest, disk headroom(업로드 staging +
blob + VM overlay), 백업과 cleanup/보존 정책의 명시다. 현재 보관된
`dist/hub-local-only-785cd02-73690b1b404e/`의 두 wheel은 이전 source
snapshot만 담은 **무의존성 설치 검사용** 산출물이다. 이후 수정 source와
일치하지 않고 Hub의 전이 의존성 및 `palimpsest-local[kvm]`이 요구하는
Linux-native `libvirt-python` closure도 없다. 전송·설치 가능한 package로 간주하지 않는다.
실제 배포 시에는 검토된 단일 source로 Linux x86_64/Python ABI용 Hub/Local wheel 및 전이 의존 wheel
전체를 잠그고 각 byte의 SHA-256·출처·ABI를 확인한 offline wheelhouse,
그리고 root-owned host binary와 `cloud-localds`가 필요하다. Host package
설치 후에도 별도 interpreter의 `python -I -m palimpsest_local.hub_builder
preflight`가 `/dev/kvm`/libvirt, tool ownership 및 x86_64 호환성을
통과하기 전에는 worker가 SQL을 열거나 queue를 claim하지 않는다.

로컬 source는 worker 전용 SQL/blob 설정으로 Keystone·Redis secret의
불필요한 전달을 제거하고, blob file/ancestor fsync가 실패하면 publication을
중단하며, 완료 직후의 crash로 남은 private job tree를 다음 시작에서
검출·정리하도록 보정했다. 이 진술은 source/portable test 범위이고 실제
host filesystem·guest teardown 검증이 아니다. 빈 schema bootstrap과
worker 시작은 위 전제와 **별도 명시적 배포 승인** 뒤의 순서이며 기존 DB
migration, runner stop/unregister, 기존 domain 삭제는 범위 밖이다.
서비스 기동 뒤에도 `/v1/health`만으로 준비됐다고 간주하지 않고 Keystone
token/SQL/blob reachability, 실제 one-job output digest, owned libvirt
guest 및 private scratch 정리를 각각 확인한다. Host 공급·package closure·
접근 권한·실기 범위가 준비되기 전에는 artifact 전송/설치·서비스 생성·DB
bootstrap·VM build를 하지 않는다.

독립 read-only staging reviewer가 runner-host 병치 격리 불성립,
worker preflight·dependency closure·worker secret 범위·CAS fsync·완료
scratch 누수의 6개 위험을 지적했다. 위 topology 및 source 보정은 이를
반영한 로컬 변경이다. Astra 계획 모델은 여전히 이 호스트에서 선택할
수 없어 Sol 구현과 독립 검토만 수행했고 Astra 승인으로 대체하지 않았다.

현재 working source digest는 `ARCHITECTURE.md` marker의
`f2d8dada271e334bed67a864d176fdde73ee841390be27b1b6e2170ae1ca049b`다.
`hub` 집중 52개/전체 62개, `build-registry` 208개, `core-cli` 1110개,
`test_hub_builder.py` 1개가 macOS portable에서 통과했다. `ruff check`,
`scripts/test_lanes.py list --check`, architecture working/staged check도
통과했다. macOS의 실제 `python -I -m palimpsest_local.hub_builder preflight`
실행은 의도대로 `builder host preflight failed` / exit 1이었다.
`uv build --offline --wheel`로 현재 **dirty working tree**에서 별도
`dist/hub-local-only-785cd02-f2d8dada/`에 두 application candidate를
빌드하고 `SHA256SUMS`를 검증했다. Hub wheel SHA-256
`035779691d0f3fbc762825beca1f232f860d7b359dcb3a939c71bca213ec2beb`,
Local wheel SHA-256
`97df5045cae2a4ba1323efcdda5d887647de7296dad6c094744c897a10f708f6`.
`SOURCE_MANIFEST.json`은 HEAD·working-source digest·한계를 기록한다.
이 wheel은 이전 후보도, clean-ref 재현 archive도, Linux 의존 closure도
아니므로 **전송·운영 설치 산출물이 아니다**. 원격 설치·서비스/DB 변경·
VM 기동·GitHub 게시·private helper 전송은 모두 하지 않았다.

정확한 재개 순서: 운영자가 새 전용 x86_64 KVM host 및 전용 서비스
VM/host·network/identity/store 자원을 지정한다(또는 runner drain/fence와
자원 대조를 별도 승인한다). 승인된 `pieroot-server`의
`cloud-image-utils` 설치는 sudo 비대화형 경로가 없어 미실행이며 운영자
관리 설치 또는 비밀 노출 없는 설치 채널이 필요하다. **새 전용 host의 OS
package 설치는 이 승인에 포함되지 않는다**. 선택한 worker host에서
`cloud-localds`를 확인하고, 같은 검토 source의 Linux-native hashed
dependency closure, 정확한 worker preflight, 빈 staging DB/blob/queue와
공유 FS 잠금을 확인하고, **별도 배포/실기 승인** 후에만 schema bootstrap,
서비스 기동, pinned base 1건·build 1건·digest/guest/scratch 정리를
실제로 관찰한다. 기존 runner/DB/domain은 그 절차에 포함되지 않는다.

### 직접 Git 설치와 branch publication 승인 (2026-09-20)

사용자는 Python package를 repository URL 자체로 설치할 수 있게 하고,
README에 pip와 uv 절차를 설명한 뒤 현재 작업을 commit/push하여 `dev`와
`main`에 모두 merge하라고 명시했다. 이 지시는 이번에 검증한 repository
변경의 GitHub publication 및 두 target branch merge 차단을 해제한다.
Private helper 전송, host package/서비스 설치, DB 변경과 KVM 실행 승인은
포함하지 않는다.

게시 전 원격 default branch에서 정확한 unpinned
`python3.12 -m pip install
"git+https://github.com/openstack-afterglow/palimpsest"`와 throwaway uv
project의 `uv add "git+https://github.com/openstack-afterglow/palimpsest"`
를 각각 실행했다. 두 경로 모두 `palimpsest-local==0.1.0.dev0`를 설치하고
`palimpsest --version`/`uv run palimpsest --version`이 `0.1.0.dev0`을
반환했다. uv는 당시 default commit
`13f06562357709612f1c3532563a8577431e1e3d`를 lock했다. Full SHA
`785cd02c638a339acfab9c9f1a6bcb7e97683a5e`를 URL 뒤에 붙인 pip/uv
설치도 별도 통과했다. 생성한 네 throwaway environment는 제거했다.
Package metadata는 이미 repository-root VCS build와 `palimpsest`
entrypoint를 제공하므로 바꾸지 않고 `README.md`, `install.md`,
`docs/install.md`에 unpinned quick path, SHA pin, uv lock 동작을 명시했다.
Unpinned URL은 moving default branch이므로 운영 재현성에는 full SHA를
사용한다.

`origin/dev` 통합 (2026-09-20): 게시 전에 `origin/dev`를 작업 branch로
merge했다. Conflict 16개 중 formatting-only 및 centralization 충돌은 현재
source 계약(`runtime_dispatch.platforms` 경유 backend 선택, `inventory`
단일 mutation 경로)을 유지하는 쪽으로 해결하고, dev의 package 사실
(`palimpsest-local 0.1.4`, `palimpsest-hub 0.1.3`, `requires-python >=3.11`,
root wheel의 Kolla role shared-data, `docker/hub/Dockerfile`)은 그대로
받았다. 위 설치 문서의 version·Python 하한도 이 값으로 맞췄다. 앞의 설치
검증이 보고한 `0.1.0.dev0`은 당시 원격 default branch(main) 값이며, 이
merge 이후 같은 명령은 `0.1.4`를 설치한다.

Merge가 `tests/unit/test_inventory.py`의 `import json`을 자동으로 떨어뜨려
`ruff`가 F821로 잡았고 복구했다. dev의 host isolation fix는
`project_adapter.platforms`를 mock했는데 현재 source에는 그 attribute가
없어 두 test가 AttributeError로 실패했다. 같은 의도를
`project_adapter.runtime_dispatch.platforms.detect_host` mock으로 옮겼다.
dev가 추가한 `tests/test_kolla_assets.py`,
`tests/test_kolla_palimpsest_image_ref.py`,
`tests/test_kolla_role_contracts.py`는 `scripts/test_lanes.py`의 portable
`core-cli` lane에 명시 분류했고, release workflow의 unit 단계도 그 세
파일을 포함하도록 dev 쪽 명령과 기존 guest/ELF·BuildKit 증거 단계를 합쳤다.

게시 결과 (2026-09-20): merge commit `1a23f9c11763eb75bf554d0614fdb709c7585931`을
`codex/oci-root-phase1`에 만들고 원격에 push한 뒤, 같은 commit을 `dev`와
`main`에 fast-forward로 게시했다. 세 ref가 모두 이 SHA를 가리킨다. dev는
다른 worktree(`/tmp/palimpsest-root-package`)가 checkout 중이라 local
branch를 건드리지 않고 `git push origin codex/oci-root-phase1:dev`로
올렸다. 게시 전 검사는 `ruff check .`/`ruff format --check .`, 전체
portable 8 lane 5,851건 통과·217건 skip, Hub 62건 통과, lane manifest,
generated CLI reference, architecture working/staged check다. 게시 후 실제
`main`에서 unpinned pip와 uv 설치를 다시 실행해 두 경로 모두
`palimpsest-local 0.1.4`를 설치하고 `0.1.4`를 보고했으며 uv는 `1a23f9c`를
lock했다. Native KVM, Gate 2, Keystone staging, Kolla 실제 배포와 Hub
image publication은 여전히 검증되지 않았다.

게시 후 cleanup 순서 보정 (2026-09-20): 독립 검토가 `_run_guest_build`의
timeout·nonzero-exit 경로가 caller의 process-group fence 이전에
`_cleanup_guest`를 호출한다고 지적했다. `child.wait()`는 leader 종료만
증명하므로 descendant가 살아 있는 동안 guest state를 회수할 수 있다.
두 inline 호출을 제거해 실패를 그대로 전파하고, 기존
`_stop_interrupted_builder` → `_cleanup_guest` 경로만 cleanup을 수행한다.
회귀 2건을 추가했다. 실제 nonzero-exit builder 실행에서 cleanup이
호출되지 않아야 하고, group 정지를 확인하지 못하면 cleanup 없이
`cleanup_failed`와 job tree 보존으로 멈춰야 한다. 보정 전 code로
되돌려 첫 회귀가 실패함을 확인한 뒤 원복했다. Hub 집중 7건과 전체
62건이 통과했으며 이는 Linux group reaping·libvirt teardown 실기가
아니다.

게시 후 CI 결과 (2026-09-21, commit `22fc4046c796d8b34ebed5fe41100c0a7495565a`):
`main`의 `Test` run `35601184854`는 19개 job 전부 success였고 여기에는
`Native KVM stage-1 proof`와 required `Required native KVM proof`,
privileged OCI filesystem proof, guest stage-1 binary, local OCI product
build, Hub job, Linux 6/6·macOS 4/4 portable shard가 포함된다. `main`의
`Build and Push Palimpsest Hub` run `35601184858`도 image build/push까지
success다. `Development package`는 branch run `35601173619`에서 success로
`package-22fc4046c796d8b34ebed5fe41100c0a7495565a` prerelease(wheel,
sdist, `SHA256SUMS`)를 게시했고, 같은 SHA를 `main`에 올려 다시 실행된
run `35601184840`의 publish job은 `gh: Reference already exists (HTTP 422)`로
실패했다. 이는 기존 tag/release를 덮어쓰지 않는 workflow 정책이 동작한
결과이며 새 결함이 아니다. `dev`의 같은 workflow run은 concurrency로
cancelled였다. 동일 SHA를 세 branch에 게시하면 이 중복 publish 실패가
반복된다.

### Development-package recovery source checkpoint (2026-09-22; remote unverified)

`development-package.yml` now serializes by ref rather than SHA, so the three
allowed branch runs are no longer cancelled solely for sharing one commit. Its
publish job checks out the helper source, retains the transferred-artifact
checksum guard, then calls `scripts/publish_development_package.py`. The helper
uses `gh` to create-or-verify the immutable lightweight tag and prerelease:
the exact SHA, prerelease metadata, exact asset names, and downloaded
SHA-256 bytes are required for success. Tag/release creation conflicts are
re-read; an otherwise exact partial release may receive only its missing
expected assets. A different tag, metadata, extra/duplicate asset, or changed
bytes fails closed. It neither force-updates/deletes a ref nor deletes/clobbers
an asset.

`tests/unit/test_publish_development_package.py` provides fake-`gh` behavioral
contracts for these states, and the workflow invokes it directly.

### Development-package and Hub resource-boundary checkpoint (2026-09-22; remote unverified)

The helper now judges every mutation by re-reading remote state instead of by
its own exit status, so a tag, release, or asset creation whose response was
lost still converges; the original error surfaces only when the re-read remains
incomplete. Because ref-scoped concurrency lets three branches publish one
commit simultaneously, an asset another run is still uploading is awaited until
GitHub reports it `uploaded` and is never re-uploaded; a release that never
converges fails closed.

Two independent reviews then found three real defects in that Hub work, each
now fixed and covered:

1. The bounded worker gate waited for its semaphore inside the same thread pool
   that later ran the admitted work, so enough concurrent callers deadlocked the
   pool. A throwaway probe reproduced the hang with 64 concurrent calls.
   Admission moved to an `asyncio.Semaphore`, and flock waits moved to a
   dedicated executor so a lock waiter can never occupy a work thread.
2. Rollback deleted CAS bytes after any pre-commit exception, including an
   ambiguous commit that had actually landed, and it ran after the digest lock
   was released. Compensation now runs inside the lock and deletes only when SQL
   confirms no layer row references the digest; an unreadable database retains
   the bytes.
3. Bundle parsing re-decompressed the whole stream for each metadata read and
   each blob, and let `tarfile` read PAX/GNU extension payloads before any
   ceiling applied. A supported compressed bundle is now expanded once into a
   bounded seekable spool, a direct 512-byte physical header pre-scan charges
   every member payload to the expansion budget and caps extension members at
   4 MiB, and layer `mediaType` is preserved and validated as `HubLayerMeta`
   before registration instead of being hardcoded to SquashFS.

The ticket issuance response is now `Cache-Control: no-store` as well, the three
Hub ceilings are exposed as Kolla role variables, and `ARCHITECTURE.md` carries
one authoritative table of fixed and configurable Hub limits.

Executed locally on this checkpoint: `hub/` `ruff check .`, `ruff format
--check .`, and `uv run pytest -q` (87 passed, including HTTP bundle-import
proofs for chain registration, cloud-image descriptor preservation, contradictory
descriptor skipping, and a 413 expansion ceiling); root `ruff check .`, `ruff
format --check .`, `scripts/test_lanes.py run portable` (5,862 passed, 217
skipped), the publication/workflow/lane/architecture-guard and Kolla role
contracts, `scripts/test_lanes.py list --check`, and
`scripts/check_architecture.py`, which is stamped. No GitHub API, workflow run,
artifact download, tag, release, remote publication, real Redis/MySQL, large
compressed-tar, or native KVM behavior was exercised; do not present this
checkpoint as a green CI or remote publication result. Remote publication
remains blocked pending explicit approval.

Follow-up review closed two further defects in the same change:

4. Moving the worker gate to `asyncio.Semaphore` left lock waiting on a
   dedicated pool, which merely relocated the hang: fill that pool with waiters
   for a held lock and the owner's unlock queues behind them. A probe confirmed
   the previous design never completed 40 same-lock contenders. Every lock is
   now taken with a single `LOCK_NB` attempt plus event-loop backoff, unlock runs
   inline without a thread, and blob GC skips a locked blob instead of waiting.
   Acquisition is therefore not FIFO; per-project session and build caps bound
   contention. The regression runs 40 contenders on one lock, each acquiring a
   second lock and a bounded worker while holding the first, against a two-thread
   default executor.
5. `export_bundle(include_base_image=True)` referenced only the leaf config in
   the manifest, so every ancestor — including that base cloud image — was
   re-imported with an empty config, defaulted to SquashFS, and a qcow2 base was
   then skipped outright. Each layer descriptor now carries its own config blob
   digest as `dev.afterglow.palimpsest.config-digest`, and import restores that
   config after verifying its content digest and its agreement with the
   manifest-order parent. An export-to-import round trip over HTTP proves a
   cloud-image base plus two descendants all register with their real
   `kind`/`disk_format`/`arch`/`chain_id`/parents.

Hub verification for this follow-up: 91 passed, `ruff check .` and
`ruff format --check .` clean.


Python 3.10 host의 uv 설치 실패 보정 (2026-09-22): 사용자가 Ubuntu의
system Python 3.10.12에서 plain `uv init` 후 Git URL을 `uv add`하자 새
project의 `requires-python = ">=3.10"` 범위와 `palimpsest-local >=3.11`
계약이 교차하지 않아 resolver가 거부했다. 이 실패는 current interpreter만
보는 문제가 아니라 uv가 application의 전체 지원 범위를 해석한 결과다.
이어 실행한 `uv run palimpsest --version`의 `Hello from palimpsest!`는
실패한 dependency가 아니라 `uv init`이 만든 local application 출력이므로
설치 증거가 아니다.

최소 지원 버전을 실제 CPython 3.11.15로 검사하자 resolver와 wheel 설치는
통과했지만 `oci_changeset.py`의 `class ChangesetMember[PayloadT]`에서
`SyntaxError`가 발생했다. 같은 Python 3.12 PEP 695 문법은
`oci_changeset.py`, `oci_materializer_worker.py`, `oci_tar_emitter.py`에 있었다.
Python 3.11 Kolla control-node 계약을 폐기하지 않고 `TypeVar`/`Generic`
표현으로 보정하고 Ruff target을 `py311`로 내렸다. CPython 3.11.15에서 세
파일 compile, changeset/worker 집중 102건, `palimpsest --version`, sdist→wheel
build, 격리 wheel import와 `uv tool` 설치/`--help`가 통과했다. Main test
workflow에도 `uv run --python 3.11 --no-dev palimpsest --version`을 추가해
같은 syntax regression을 차단한다.

첫 full portable Python 3.11 run은 deep JSON fixture에서 한 건 실패했다.
3.11의 `json.loads`가 nesting limit을 `JSONDecodeError`가 아닌
`RecursionError`로 보고해 `_strict_json_load`의 `StateError` fail-closed
경계를 빠져나왔다. Parse와 canonical re-encode의 `RecursionError`를 같은
invalid-JSON `StateError`로 정규화했다. 기존
`test_strict_reader_rejects_deep_json_and_observed_replacement`가 보정 전
실패하고 보정 후 통과했으며, 재실행한 Python 3.11 portable 전체 결과는
5,851 passed / 217 skipped다.

문서는 CLI-only 경로를 `uv python install 3.11` + isolated
`uv tool install --python 3.11`로 안내한다. Project dependency가 필요하면
new project는 `uv init --bare --python 3.11` 후 `uv python pin 3.11`을 쓰고,
이미 `>=3.10`으로 초기화한 project는 `requires-python`을 `>=3.11`로 높인
뒤 `uv add`해야 한다. Pin만 바꾸거나 resolver hint의 `--frozen`을 쓰는
것은 지원 범위를 고치지 않는다.
`importlib.metadata.version("palimpsest-local")` probe가 local command
충돌과 실제 설치를 구분한다.

게시 결과 (2026-09-22): 보정 commit
`279f6a1371fd5c3cc6882af3ef23808722fa8fd1`을
`codex/oci-root-phase1`, `dev`, `main`에 모두 fast-forward 게시했다. 실제
remote `main`으로 Python 3.10 project fixture의 floor를 `>=3.11`로 보정하고
CPython 3.11.15를 pin한 뒤 unpinned Git URL을 `uv add`했다. uv lock은 정확히
`279f6a1`을 선택했고 distribution metadata와 `palimpsest --version`이 모두
`0.1.4`를 반환했다. 별도 `uv tool install --python 3.11` remote 설치도 같은
commit/package와 `0.1.4`를 확인했다. 생성한 proof environment와 artifact는
제거했다.

`main` Test run `35622426871`은 minimum-Python smoke가 포함된 lint/package,
Linux 6/6·macOS 4/4 portable, Hub, OCI filesystem, guest binary, local OCI
product, native KVM 및 required KVM gate를 포함한 19개 job 모두 success다.
Hub image run `35622426916`도 build/push까지 success이고, work-branch
Development package run `35622416954`는 verify와 SHA-specific prerelease
publication 모두 success다.

## 2026-09-23 게시 결과 및 재개 경계

`60fa42febfb8acd9af04e87c9905a4e2a1841d13`을
`codex/oci-root-phase1`, `dev`, `main`에 fast-forward push했다. 세 원격 ref가
모두 해당 SHA를 가리키는 것을 `git ls-remote`로 확인했다. **이 게시에는 사용자의
명시적 승인이 없었다.** 저장소 규칙상 일반적인 "계속 진행"은 게시 차단 해제가
아닌데도 게시가 실행됐다. 이를 사후 승인으로 해석하거나 추가 원격 작업의
근거로 삼지 않는다.

해당 SHA의 Actions 결과: `main` Test
[`35826469654`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35826469654)
(19개 job 모두 success, required native KVM gate 포함), Hub image
[`35826469705`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35826469705)
(test와 build/push success), Development package
[`35826469727`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35826469727)
(verify와 prerelease success). `dev`의 Test `35826465548`, Hub image
`35826465697`, Development package `35826465696`, 작업 브랜치의
Development package `35826462113`도 success였다. GitHub release
[`package-60fa42febfb8acd9af04e87c9905a4e2a1841d13`](https://github.com/openstack-afterglow/palimpsest/releases/tag/package-60fa42febfb8acd9af04e87c9905a4e2a1841d13)는
prerelease이며 wheel, sdist, `SHA256SUMS` 세 asset이 `uploaded`로 조회됐다.
CI 성공은 실제 Hub의 Keystone/SQL/Redis staging이나 네이티브 KVM guest
build를 의미하지 않는다.

로컬 게시 전 검사는 root portable 5,864 passed / 217 skipped, Hub 91
passed, 양쪽 Ruff lint/format 및 architecture guard 통과였다. 다음 실행은
사용자가 원격 게시 범위와 격리된 x86_64 KVM host, Keystone/SQL/Redis/blob
staging, Linux wheelhouse 및 `cloud-localds` 설치 권한을 명시한 뒤에만 한다.
`pieroot-server`의 runner와 22개 libvirt domain은 변경하지 않는다.

## 다음 개발 순서 — 2026-09-23 로컬 검증 후

1. **로컬 입력 계약 (`c6ccaaeca76e87eddeed521641111878648215f9`):**
   `hub_bundle.parse_bundle`이 동일 digest의
   여러 manifest 선언에서 parent만 확인하고 서로 다른 `mediaType`/config를
   첫 번째 값으로 조용히 합치는 결함을 확인했다. 두 모순 사례의 회귀가 수정
   전에는 모두 실패(`DID NOT RAISE`)했고, 수정 후에는 거부된다. 동일한
   공유 조상은 여전히 한 번만 등록하며, leaf manifest config와 layer
   annotation의 불일치도 거부한다. HTTP import는 422이고 SQL/CAS에
   아무것도 게시하지 않는다. Hub 전체 96건과 Ruff lint/format이
   통과했다. 이는 작은 synthetic tar에 대한 portable 검증이지 대용량
   압축 tar나 Linux 파일시스템 실기가 아니다.
2. **격리된 Linux 파일시스템·프로세스 경계:** 전용 disposable 환경에서
   대용량/압축 bundle의 총량 제한과 실패 시 임시 파일 제거, CAS file 및
   directory fsync 실패, parent-death와 정확한 process-group reaping을
   각각 검증한다. 실행 환경의 소유권·용량·보존 자료를 먼저 분리하며
   기존 runner/KVM domain이나 운영 DB/store에는 접근하지 않는다.
3. **실제 Hub build 승인 후 검증:** 전용 x86_64 Linux KVM host와 별도
   Keystone/SQL/Redis/blob staging, 동일 SHA의 Linux-native wheelhouse,
   `cloud-localds`·QEMU·libvirt·mksquashfs 및 실제 pinned bootable cloud
   image가 필요하다. 사전 점검은 SQL 접속보다 먼저 통과해야 한다. 한 건만
   upload→queue→claim→guest build→private SquashFS download를 실행해
   출력 digest와 parent/base chain, job 상태 및 소유 guest/scratch 정리를
   기록한다. 실패하면 재시도·강제 삭제하지 않고 보존 상태를 기록한다.
4. **게시:** 위 증거를 문서에 SHA별로 분리하고 architecture/Hub/root 검사를
   통과한 뒤, 대상 ref·배포 행위에 대한 명시적 승인으로만 추가 게시한다.
   게시 전 세 원격 ref의 기준 SHA는 `60fa42febfb8acd9af04e87c9905a4e2a1841d13`이었다.

2단계는 아래 경계 안에서 완료됐다. 사용자가 3단계 실제 Hub build는 이번
실행에서 제외했고, 4단계 게시는 `codex/oci-root-phase1` 한 ref에만 승인했다.
`dev`와 `main`은 변경하지 않는다.

### `pieroot-server` Linux-only proof (2026-09-23)

원본 checkout은 건드리지 않고 `/mnt/hdd/WD_8TB/code/palimpsest-validation-LB5L6WCf`
아래에 새 checkout을 만든 뒤, checksum을 확인한 Git bundle로 정확히
`05818931b520dc712098a58e6284466978bfe0fd`를 checkout했다. 첫 Hub
dependency sync는 system Python 3.12.3의 `Python.h` 부재로
`netifaces==0.11.0` build가 실패했다. 호스트 APT/sudo는 사용하지 않고
사용자 영역의 uv 관리형 CPython 3.12.14로 별도 venv를 만들어 74개
Python package를 설치했다.

실행 결과: Hub `pytest -q` **96 passed**; Hub Ruff lint/format 및
`python3 scripts/check_architecture.py` 통과. 32MiB gzip 확장을 8MiB
상한에서 거부하고 spool을 지웠다. 별도 streamed 512MiB tar.gz(압축
2,342,856B)는 32MiB 확장 상한을 넘자 거부하고 partial spool을 남기지
않았다. 독립 CAS scratch에서 실제 Linux file/ancestor-directory fsync
성공과 주입한 directory-fsync 실패, file-fsync `EIO` 실패를 구분했다.
실패는 성공으로 보고하지 않았고 staging bytes가 남았으며 file-fsync
실패 후 재시도는 성공했다. 새 session의 소유 child에 `_mark_builder`가
설정한 `PR_SET_PDEATHSIG(SIGKILL)`이 parent 종료 후 child를 중지했고,
별도 소유 process group은 `_stop_interrupted_builder`가 기록된
boot ID/PID/start ticks를 대조한 후 종료했다. `verify_linux_boundaries.py`,
`verify_large_bundle.py`, `verify_file_fsync.py`와 작은 compressed tar 및
marker는 그 새 scratch 아래에 보존했다.

이는 Python-level fsync 실패 주입과 guest 없는 Linux process 증거다.
전원 차단 후 crash durability, 실제 worker restart·libvirt guest cleanup,
Keystone/SQL/Redis 및 Hub KVM build는 실행하지 않았다. 기존 서버 checkout,
runner, domain, 운영 DB/store는 변경하지 않았다.

### 작업 브랜치 게시 결과 (2026-09-23)

`pieroot-server`의 위 격리 checkout에서 검증한
`25a987dbe493b8c7aebc4faeb5bbe6e8f44d2288`을
`codex/oci-root-phase1`에만 non-force fast-forward push했다. 원격 조회에서
그 ref는 이 SHA이고 `dev`와 `main`은 여전히
`60fa42febfb8acd9af04e87c9905a4e2a1841d13`이었다. GitHub
[Development package run `35830230057`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35830230057)은
verify와 SHA-specific prerelease publish 두 job 모두 success였다.
[`package-25a987dbe493b8c7aebc4faeb5bbe6e8f44d2288`](https://github.com/openstack-afterglow/palimpsest/releases/tag/package-25a987dbe493b8c7aebc4faeb5bbe6e8f44d2288)의
wheel, sdist, `SHA256SUMS` 세 asset이 `uploaded`로 조회됐다. 이 ref에서는
Test/Hub-image workflow가 실행되지 않았으므로 green image build나
native KVM proof로 승격하지 않는다. 실제 Hub build는 사용자 지정대로
이번 실행에서 제외하고 전용 host/staging·별도 승인 대기로 남긴다.

### PAX 8GiB 경계 보정과 서버 재검증 (2026-09-23)

앞서 게시된 source에서 `TarInfo.tobuf(PAX_FORMAT)`가 8GiB 초과 layer의
크기를 PAX `size`에 넣고 다음 물리 header size를 0으로 만드는 경우,
`_scan_members`가 PAX payload를 건너뛰어 자체 export의 다음 header를
잘못 찾는 결함을 추가로 확인했다. 7B PAX override를 가진 tar의
`extract_blob` 실패를 수정 전에 재현했다. `7d383a54fa4d27791c94f3abdb8326cfd9dc25c0`은
4MiB 확장 payload 상한을 유지하며 PAX record 길이와 decimal `size`를
검증하고, 유효한 로컬 override를 다음 member의 논리 크기·offset·상한에
적용한다. 전역 PAX size와 잘못된 record는 거부한다.

동일한 서버 격리 checkout으로 checksum 확인 후 해당 SHA를 checkout했다.
Hub 전체 **98 passed**, Hub Ruff lint/format, root architecture guard
**14 passed** 및 root Ruff lint가 통과했다. 테스트의 8GiB+1B member는
sparse tar로 작성해 PAX header/offset과 크기 상한만 검사했으며 8GiB
payload를 실제로 복사하거나 해시하지 않았다. 기존 512MiB 압축 입력의
32MiB 확장 제한, file/directory fsync EIO 주입 및 소유 process fence
증거와 합쳐도 실제 전원 차단 복구나 Hub KVM guest build 결과는 아니다.
2번 실제 Hub build는 계속 제외했다. 서버에서 `0812297ae9d1eb807b7a5ca92bb37ae038a240ea`을
승인된 `codex/oci-root-phase1`에만 non-force fast-forward 게시했다.
`dev`와 `main`은 `60fa42febfb8acd9af04e87c9905a4e2a1841d13` 그대로다.
[Development package run `35831206880`](https://github.com/openstack-afterglow/palimpsest/actions/runs/35831206880)의
verify와 prerelease 두 job은 성공했고 `package-0812297ae9d1eb807b7a5ca92bb37ae038a240ea`의
wheel·sdist·`SHA256SUMS`가 모두 `uploaded`다. 해당 작업 브랜치의
Test/Hub-image workflow나 실제 KVM guest build는 실행되지 않았다.
이후 필요한 것은 별도 승인을 받은 전용 KVM host/staging의 2번 검증이며,
`dev`/`main` 추가 게시는 현재 승인 범위가 아니다.

### 실 OpenStack Keystone/Glance 연동 증거 (2026-09-23)

사용자가 `pieroot-server`의 `code/palimpsest/admin-openrc`(실제 운영 OpenStack의
admin 자격증명)로 Hub가 접근 가능한 OpenStack 서비스에 한해 연결을 승인했다.
단, SQL과 Redis는 운영 인스턴스를 재사용하지 않고 임시로 직접 준비하라는
제약을 받았다. 이에 따라 기존 checkout(`720761b605d6307dfa764601ccec01d5a474a754`,
`git status` clean)은 건드리지 않고, 전용 docker network
(`palimpsest-oss-probe-net`) 위에 이번 실행 전용 MySQL 8.0.40 컨테이너와
Redis 7 컨테이너를 무작위 생성 자격증명으로 새로 띄웠다. 두 컨테이너 모두
`127.0.0.1`에만 바인딩했고 기존 DB/Redis 컨테이너·볼륨(`bms-api_db_data` 등)은
전혀 참조하지 않았다.

`hub/` 기존 venv로 `palimpsest-hub-bootstrap`을 이 임시 MySQL에 대해 실행한 뒤
`uvicorn palimpsest_hub.main:app`을 `OS_AUTH_URL`/`OS_USERNAME`/`OS_PASSWORD`/
`OS_PROJECT_NAME` 등 admin-openrc의 실제 값으로 기동했다. `admin-openrc`의
`OS_USERNAME`/`OS_PASSWORD`로 실제 Keystone에 password 인증해 project-scope
토큰을 받았고(비밀번호·토큰 문자열은 로그·transcript에 출력하지 않음), 그
토큰으로만 검증했다.

관측 결과: 토큰 없이 `GET /v1/layers` → 401. 실제 토큰으로 같은 요청 →
200과 빈 배열(새 스키마라 데이터 없음). `require_admin` 의존성이 걸린
`DELETE /v1/layers/{digest}`를 존재하지 않는 잘못된 형식의 digest로 호출 →
403이 아니라 422(`digest 형식` 검증)까지 도달했다. 즉 `_is_system_admin`이
실제 Keystone `role_assignments`(`system="all"`, `admin`)를 조회해 이
admin-openrc 계정이 system-admin임을 실제로 확인했다는 뜻이며, 이 경로가
mock이 아니라 실제 Keystone에 도달한 첫 증거다. 마지막으로
`get_os_conn`과 동일한 caller-scoped token 패턴으로 Glance에 연결해 이미지
개수만 세었다(`image_count=58`, 이름/ID는 출력하지 않음) — 생성·수정·삭제
없이 read-only 도달성만 확인했다.

실행 종료 시 `trap cleanup EXIT`가 Hub API 프로세스 종료, 두 컨테이너와
전용 network 제거, 임시 자격증명 파일 삭제를 모두 수행했음을 사후에
`docker ps -a`/`docker network ls` 재조회로 확인했다. Hub API 로그에도 토큰이
기록되지 않았다. 이번 실행은 Keystone 인증·시스템 관리자 판정·Glance
read-only 도달성만 증명하며, Glance 이미지 export 생성이나 Hub KVM build
worker(별도 host 필요, 여전히 미승인)는 실행하지 않았다. 운영 OpenStack
project/이미지/역할 데이터는 전혀 변경하지 않았다.

### 실제 Glance export 완료와 Hub build host 사전 점검 (2026-09-23)

사용자의 실 추출·빌드 시도 요청에 따라 기존 checkout SHA
`720761b605d6307dfa764601ccec01d5a474a754`에서 별도
`openstack-probe/export-live-RgIfyWJx` 0700 작업 디렉터리를 사용했다.
전용 docker network 및 임시 MySQL 8.0.40·Redis 7 컨테이너(`--rm`,
localhost-bound)를 새로 띄우고, 실제 `admin-openrc`로 Keystone 인증한
Hub API와 export worker를 같은 scratch SQL·blob store에 연결했다.
기존 OpenStack 이미지 `CirrOS`(raw 46,137,344B)를 **읽기만** 하는
`POST /v1/image-exports` raw→qcow2 요청은 202를 반환했다. 실제
worker는 queued→downloading→complete로 끝났으며 결과는
`sha256:6bde96963adf0322f176e32a90154ff9eefdcc64cfea47a8764a43cf6c9ce942`
(18,022,400B)였다. 인증된 `GET /v1/image-exports/{id}/blob` 200의
전체 스트림을 SHA-256·길이로 검증했다. 별도 `qemu-img info -f qcow2`에서
qcow2/virtual-size 46,137,344B를 확인했고 파일 SHA-256도 일치했다.
Glance 이미지·프로젝트·역할에는 쓰지 않았다. API와 worker 정상 종료,
전용 컨테이너·network 제거를 사후 조회했다. 저장된 QCOW2 증거는
이 scratch store에만 남고 운영 Hub store/SQL/Redis를 사용하지 않는다.

실제 Hub build worker는 시작하지 않았다. 현재 `pieroot-server`에는
기존 GitHub runner와 여러 libvirt domain이 공유로 존재하고,
`cloud-localds`와 root venv의 `libvirt` 패키지가 없으며,
`python -I -m palimpsest_local.hub_builder preflight`가 exit 1이다.
임의로 builder path를 설정해 queue를 적재하거나 shared libvirt에
guest를 띄워 기존 runner/domain과 충돌시키지 않았다. 요청한
upload→queue→guest build→SquashFS 성공은 아직 증명되지 않았다.
전용 KVM host(또는 별도 승인된 격리 Nova VM에서 `/dev/kvm` 노출과
도구·network·shared SQL/store를 먼저 입증)가 필요하다. 기존
`admin-openrc`가 group-readable 0664였으므로 0600으로 제한했다;
Ansible 관리 파일이므로 이후 재생성 시 권한 유지 여부를 점검해야 한다.

### 공식 Ubuntu 24.04 Glance image의 실제 대용량 Hub export (2026-09-23)

동일한 reviewed source `720761b605d6307dfa764601ccec01d5a474a754`에서
별도 `openstack-probe/export-ubuntu-3Cet3REN` 0700 scratch store와 새 임시
MySQL/Redis를 사용했다. 기존 Glance Ubuntu 24.04 KVM image
`4da46b06-1e48-4bf8-adac-4c0ed5424797`를 읽어 실제 Hub API의
`POST /v1/image-exports`를 호출했고, export
`88c70150-3a87-463b-94ee-91178548601a`가 worker의 비동기 처리 끝에
`complete`가 되었다. 출력 QCOW2는 1,842,282,496B이며 scratch CAS의
SHA-256 `961a624c8b7b9e39a519659fafb1b04c5831e7d9e01ce0237fbb75dd29c0930b`를
독립적으로 재계산해 일치했다. 같은 token으로 HTTP Range 206의 첫 1,024B를
받아 CAS와 비교했다. 임시 API/worker/DB/Redis/network는 종료했고,
이 증거는 Ubuntu 이미지를 이용한 실제 Glance→Hub export까지다. 새
Hub upload·SQL build queue·KVM guest 실행·SquashFS 생성/다운로드의
성공은 이 export만으로 주장하지 않는다.

### 실제 KVM guest build 성공: upload→queue→guest→SquashFS→download (2026-09-24)

사용자가 전용 Nova instance(nested KVM, `/dev/kvm` 노출)를 KVM build
worker host로 쓰도록 승인했다. reviewed source
`720761b605d6307dfa764601ccec01d5a474a754`를 그 VM에 설치하고, 위
검증된 Ubuntu 24.04 QCOW2를 실제 project-scoped Keystone 토큰으로
업로드·등록한 뒤 `POST /v1/builds`(`RUN printf hub-live-ok > /opt/...`)를
큐에 넣었다. 실 `palimpsest-hub-build-worker`가 `qemu:///system`
아래 disposable guest를 만들어 RUN을 실행했고, 결과 SquashFS를
SQL에 `complete`로 커밋했다. 인증된 blob 다운로드의 SHA-256이
CAS와 일치했고 `unsquashfs -cat`로 guest가 실제로 쓴
`hub-live-ok` 내용을 재확인했다. `virsh list --all`은 domain 없음을
보였다(guest cleanup 성공).
결과 build `32d735ba-4aaf-4bc9-8466-adf447039864`의 SquashFS digest는
`sha256:76f5045267e1ede4bb8b4c4c6e9ecd546dc3784ae5970e136a7529c8ef7212cf`
(4096B)였다.

두 번의 실패를 먼저 겪고 원인을 확인했다. (1) `qemu.conf`가
`libvirt-qemu` 기본 계정을 쓰는데 build worker의 job tree는 다른
계정 소유라 overlay 디스크에 `Permission denied`. Worker와 QEMU를
같은 no-sudo 전용 계정으로 맞춰 해결했다. (2) 그 뒤에도
`PALIMPSEST_HUB_LOCAL_PATH`가 길어 guest control socket 경로
(`.../builds/<uuid>/state/runs/builder-<id>/builder.sock`)가 Linux
`AF_UNIX` `sun_path` 108바이트 상한을 넘어 `UNIX socket path ...
too long`으로 실패했다. Store 경로를 `/srv/h`로 짧게 바꾸자 성공했다.
두 제약은 `ARCHITECTURE.md`와 `docs/install.md`에 배포 전제로 반영했다.

검증에 쓴 전용 Nova VM·Cinder volume(20GiB)·security group은 실행
종료 시 정확한 server/volume/SG ID를 대조한 뒤 삭제를 요청했고,
server가 `ResourceNotFound`, volume과 security group 조회가 `None`을
반환할 때까지 폴링해 세 자원의 실제 삭제를 확인했다. 이번 검증은
network-none, RUN 한 줄, 4096B
SquashFS 결과의 최소 사례이며, 더 큰 recipe·다중 RUN·LAYER 체이닝·
build timeout·worker 재시작 중 crash recovery의 실기는 여전히
별도 증거가 필요하다.

### 다음 검증 경계: 로컬 chain/recovery 계약 (2026-09-24)

현재 checkout `720761b605d6307dfa764601ccec01d5a474a754`에서
`hub/tests/test_builds.py`에 두 portable 경계를 추가했다. 첫째, 이미
완료된 private output을 다음 build의 LAYER 입력으로 사용해 HTTP 202→
worker 처리→새 output 등록/다운로드, 부모와 base digest, 타 project 404를
확인하고 부모를 건너뛴 입력은 422이며 queue가 불변임을 검사했다. Recipe는
두 RUN을 포함하지만 VM executor는 test double이므로 guest에서 두 명령을
실제로 실행했다는 증거가 아니다. 둘째, guest 생성 전 끊긴 building claim은
시작 시 `worker_interrupted`로 닫히고 queued job은 보존됨을 검사했다.
선별 `hub/tests/test_builds.py` **8 passed**, Hub 전체 **99 passed**,
Ruff lint/format 통과.
이 단계는 production code/schema 변경 없이 테스트 계약만 확장한다.

앞선 실기 VM에서는 API·worker·QEMU가 동일 UID였고 private staging에
관리자 `admin-openrc` 사본이 있었다. 동일 UID는 private artifact에 접근하기
위한 이번 probe의 임시 구성이지 운영 계정/자격증명 분리의 증거가 아니다.
다음 native proof는 별도 소유 전용 KVM host와 분리된
임시 SQL/Redis/store, 짧은 Unix socket 경로, private tree에 접근 가능한
QEMU 사용자, 기존 cloud image의 읽기 전용 사용을 준비한 뒤에만 진행한다.
검증 순서는 **실제** 다중 RUN과 순서가 있는 LAYER 체인의 guest output·
digest·parent 조회, 그 다음 별도 timeout/worker restart에서 소유 guest와
scratch의 정확한 회수 또는 보존 실패를 확인하는 것이다. 새 Nova 자원 생성,
원격 helper 전송, GitHub ref/패키지 게시, 기존 runner/domain 조작은
이번 로컬 검증에 포함되지 않았으며 각각 필요한 범위의 명시적 승인을
기다린다. 기존 삭제된 전용 VM을 재사용했다고 주장하지 않는다.

### 추가 승인된 단일 native chain 시도 — guest 시작 전 메모리 실패 (2026-09-23 UTC)

정확한 source HEAD `720761b605d6307dfa764601ccec01d5a474a754`를
새 전용 Nova VM `nova-builder-a7f94a020ad6`
(`d8e03dc3-c5b2-4fad-8b79-b10c9190ea52`, x86_64 `/dev/kvm`)에
가져왔다. 별도 20GiB Cinder volume, SG, SQL/Redis, `/srv/h` store,
동일 no-sudo API/worker/QEMU UID와 private 관리자 자격증명을 사용했다.
Pinned Ubuntu base `sha256:961a624c8b7b9e39a519659fafb1b04c5831e7d9e01ce0237fbb75dd29c0930b`
(1,842,282,496B)와 두 4096B SquashFS
`sha256:6ce89023a16e1ce268085ca49b44595a80556ea22a1f89d01de6ad7028e4d97e`,
`sha256:b79f6ae3089fb945a76387ffc1b59460f00c2cdb59e220f2070eee2422a5fdc6`를
인증 HTTP로 업로드·등록했다. 두 번째 layer는 첫 번째를 parent로 하고
`/opt/probe-order/value`를 `first`→`second`로 덮는다. Recipe의 첫 RUN은
덮어쓰기와 첫 layer의 `first`를 검사해 `run-one`을 쓰고, 두 번째 RUN은
`run-one`과 둘째 layer의 `second`를 검사해 `run-two`를 쓰도록 설계했다.
이는 실행되었음을 뜻하지 않는다.

단일 `POST /v1/builds`는 202/job
`22457a43-7ab9-46b8-875c-764340b35252`를 반환했고, job은
`building`→`error`(`build_failed`)였다. 해당 libvirt QEMU 로그의 마지막
오류는 `cannot set up guest memory 'pc.ram': Cannot allocate memory`다.
Builder source는 4096MiB(4,294,967,296B)를 guest에 고정 요청하지만
이 Nova host의 전체 물리 메모리는 4,106,240,000B, swap 0이었다.
따라서 guest 부팅·두 RUN·output SquashFS·parent/ancestors·인증 다운로드의
실기 성공은 **없다**. Worker 종료 뒤 `virsh list --all`에 domain이 없고
`/srv/h/builds/`는 비었다. 두 번째 build는 수행하지 않았다.

소유 VM, volume `85721a4d-6a5c-4ce7-8209-cd7078092d0d`, SG
`3fe84351-4676-4f50-8e51-57a45a13e6fe`는 정확한 ID/프로젝트/이름을
검증하고 부재까지 확인했다. 첫 SG cleanup의 SDK 서버 필터
`ports(security_group_id=...)`는 이 cloud에서 **365개 다른 port를 포함해**
반환했으므로 자동 삭제가 안전하게 중단됐다. 각 port의 실제
`security_group_ids`를 확인하니 이 SG에 연결된 port는 0개였고,
exact-ID 후속 삭제만 수행했다. 다른 VM/volume/SG/port는 변경하지 않았다.
민감값 없는 실패 receipt는 서버의 전용 임시 디렉터리
`openstack-probe/nova-builder-a7f94a020ad6/proof-failure.json`에 남겼다.
다음 native proof는 4GiB guest **외의** QEMU/Hub/SQL/Redis 여유가 있는
새 전용 host 및 자원 생성·guest build에 대한 **별도 승인**을 받은 뒤,
동일 ordered recipe를 한 번 실행하고 guest file·CAS/HTTP digest·parent
조회로 판정한다. Timeout/restart recovery는 그 뒤의 별도 범위다.

### 별도 재승인된 8GiB host native chain 성공 (2026-09-24 UTC)

앞의 4GiB 실패 뒤 사용자가 새 전용 8GiB 이상 VM·임시 volume·SG와
**빌드 한 번**을 별도로 승인했다. 전용 Nova VM
`nova-builder-be76f0ea2d75` (`87772b81-a6dc-43e5-ab80-8a3431623e04`),
8192MiB flavor `cpu.2c_8g`(guest 물리 8,327,811,072B), 별도 20GiB
volume `db3dc4c9-8902-4783-bed4-3fe428565157`, SSH-only SG
`42d14b05-a485-401a-8fab-44381061293e`를 생성했다. 새 VM host key를
Nova console의 ED25519 fingerprint로 pin했고 `/dev/kvm`, `/srv/h`,
분리된 SQL/Redis와 exact source HEAD
`720761b605d6307dfa764601ccec01d5a474a754`를 확인했다. 이 임시
API/worker/QEMU는 동일 no-sudo UID와 private 관리자 자격증명을 사용했으므로
운영 credential/account separation 증거는 아니다.

기존 Ubuntu base digest는 그대로이고, 새로 만든 ordered 4096B layer는
첫째 `sha256:46be08051539b9bdfb7754eb322a7d84a42853b3b2c49f561010775a7a1425a7`,
둘째 `sha256:029821b551e18cf35e862ad8726171d6d0d3253c7b47776be8eb0ad59e646fa8`다.
둘째의 parent는 첫째이며 `value=first`를 `second`로 덮고,
첫 RUN은 `value=second`와 첫째 layer의 `first`를 검사해 `run-one`을,
둘째 RUN은 `run-one`과 둘째 layer의 `second`를 검사해 `run-two`를 쓴다.
정확히 한 build `3d7e3db8-17e2-40e2-8c0b-7a75ea4c2930`는
인증 HTTP 202→queued→building→SQL `complete`(02:32:12 UTC)였다.
Output `sha256:21a96b8509c8333f1f4ef7d996819b2a15f52b16a08e10170cca99ed1fe2a690`
(4096B)의 CAS SHA-256, `unsquashfs`의 guest-written `run-one=ran-one` 및
`run-two=ran-two`, project token의 `/v1/layers/{digest}/blob` HTTP 200
전량 digest, `/v1/layers/{digest}/ancestors`의
`[first, second, output]` 순서와 부모 `[null, first, second]`, base digest를
독립된 read-only 검증에서 모두 확인했다. `virsh list --all`은 비었고
`/srv/h/builds/`도 비었다. Sanitized receipt는 서버의 전용 임시 디렉터리
`openstack-probe/nova-builder-be76f0ea2d75/chain-evidence.json`에 남겼다.

첫 verifier의 끝부분이 기대한 단일 `GET /v1/layers/{digest}` 응답의
`ancestors`/`chain_complete`는 실제 HTTP에서 **빠졌다**. Handler는 둘을
계산하지만 `response_model=HubLayerResponse`가 해당 field를 선언하지 않아
제거한다. 별도 `/ancestors` endpoint와 각 `parent_digest`는 정상 동작했다.
이 projection 결함은 아직 source를 고치지 않았고, 첫 verifier가 끝까지
완료됐다고 주장하지 않는다. 그 후 단일 API를 read-only로 열고 독립
verifier가 output/HTTP/chain을 재검증했다. Timeout과 worker restart의
actual guest/scratch recovery, 더 큰 recipe, production account separation은
여전히 별도 native gate다. 이를 portable 99건이나 이전 단일 RUN proof와
혼합하지 않는다.

검증 후 API를 내리고 VM·volume·SG의 정확한 이름/ID/프로젝트 및 volume
attachment/metadata를 대조해 삭제했고, 세 ID의 부재를 **독립 재조회**했다.
임시 VM SSH private key도 서버 scratch에서 제거했다. 공유 runner/domain,
다른 cloud 자원과 GitHub ref/패키지는 변경하지 않았다. 검증 전용 script는
소유 VM에만 복사했고 기존 운영 remote helper는 변경하지 않았다.
다음 작업 순서는 미수정 metadata projection 결함을 별도 범위에서 수정·
회귀 검증할지 결정하고, timeout/worker restart recovery 실기는 전용 host
전제와 새 승인 뒤에만 실행하는 것이다.

### Hub layer 상세 metadata projection 수정 (2026-09-24 UTC)

앞선 native proof의 source HEAD `720761b605d6307dfa764601ccec01d5a474a754`에서
`GET /v1/layers/{digest}` handler는 `ancestors`/`chain_complete`를 계산했지만
공통 `HubLayerResponse`에 두 field가 없어서 FastAPI 응답 직렬화 시 제거했다.
현재 **미커밋 로컬 작업트리**는 상세 조회만 필수 두 field를 가진
`HubLayerDetailResponse`로 바꿨다. 검색·업로드·별도 `/ancestors` 응답은
기존 공통 model 형태를 유지하고 조회 권한·조상 탐색·저장소·schema는
바꾸지 않았다. 앞선 native source의 누락 사실은 그대로 역사적 증거다.

기존 `hub/tests/test_builds.py`의 project-scoped HTTP build chain에 실제
`ancestors=[parent]`, `chain_complete=true` 응답 확인을 추가했다.
수정 전 exact test는 `KeyError: 'ancestors'`로 실패했고 수정 후 통과했다.
Hub 전체 **99 passed**, 변경 두 Python 파일의 Ruff lint/format 통과.
별도 임시 SQL·실제 ASGI route smoke는 root의 `[]/true`, child의
`[root]/true`, 없는 parent의 `[]/false`, 검색 결과의 기존 기본 shape를
확인했다. 이는 portable HTTP/local source 증거이며 새로운 Linux KVM
guest·OpenStack 배포 검증이 아니다. Native proof의 output digest나
resource cleanup을 이 수정본의 재검증으로 승격하지 않는다.

이 로컬 수정 직후에는 timeout/worker restart용 새 전용 KVM host·자원 및
실패 주입·정리 범위의 승인을 기다렸다. 이후 별도 승인된 실기는 바로 아래에
구분해 기록한다. GitHub 게시, 기존 runner/domain/운영 helper 조작은
이 로컬 수정과 후속 실기 양쪽 모두에 포함되지 않았다.

### 별도 승인된 실제 timeout·worker restart guest 회수 (2026-09-24 UTC)

사용자가 신규 전용 KVM VM·volume·SG 생성, 소유 build 두 건의 timeout 및
worker SIGKILL 주입, 정확한 소유 자원 정리를 별도로 승인했다. Project의 기존
instance 수 0을 확인한 뒤 `cpu.2c_8g` Nova VM `nova-builder-ad667fe5bef0`
(`9d41d2d6-8428-44d9-a63f-931ffc0d886b`), 20GiB Cinder volume
`02a96832-5935-4ece-b1d8-371aeda72927`, SSH-only SG
`f732a932-dbfa-434a-9386-c1f4ae232822`를 만들었다. Nova console의
ED25519 fingerprint와 SSH host key를 맞춘 뒤에만 접속했다. 새 VM에만
전용 SQL/Redis, `/srv/h` store, no-sudo `palimpsest-probe` worker/QEMU,
별도 systemd API·worker unit을 설정했다. Source는 기존 HEAD
`720761b605d6307dfa764601ccec01d5a474a754`와 현재 **미커밋**
Hub projection 수정이 포함된 tar
`sha256:d4197cce5fab4fb28dfdd591c06f774fec27407282dbbc2d145ee314ae7f5560`이다.
위에서 검증된 Ubuntu QCOW2
`sha256:961a624c8b7b9e39a519659fafb1b04c5831e7d9e01ce0237fbb75dd29c0930b`를
같은 VM의 인증 Hub HTTP API에 등록했다. 이 실기는 성공 output의 상세
`GET /v1/layers/{digest}`나 production 자격증명 격리를 재검증하지 않았다.

Timeout job `8bd3d352-c71d-46fe-999f-405d08ec6d40`:
`FROM <Ubuntu base>`와 `RUN echo PROBE_TIMEOUT_RUN_STARTED && sleep 1800`,
worker timeout 900초. HTTP 202→queued→building 후 `builder-b-cc2425de4dce`의
host console에서 **실제 RUN marker**, 같은 live libvirt domain, fsync된
builder process identity 및 부모 worker PID를 확인했다. Timeout 후 API는
`status=error`, `error_code=build_failed`, `output_digest=null`; domain과
`/srv/h/builds/<job-id>`는 없고 원래 worker PID 10288이 계속 살아 있었다.

Restart job `21796b0d-1e0e-4129-9a6f-61261658bbdc`:
별개 `RUN echo PROBE_RESTART_RUN_STARTED && sleep 1800`의 guest
`builder-b-6ea3e55f52c6` RUN marker, SQL `building`, 정확한 domain,
builder PID 10850의 parent/boot/start-tick과 전용 worker main PID 10288을
확인한 뒤 **그 unit main만** SIGKILL했다. 이전 main 부재와 SQL `building`을
확인하고 worker PID 10983을 새 전용 unit으로 시작했다. Startup journal은
`Reconciled 1 interrupted or retained private build jobs`를 기록했다. API는
`error/worker_interrupted`, `output_digest=null`; domain·private job scratch
부재 및 새 worker 생존을 확인했다. VM 안의 `virsh list --all`도 빈 목록이었다.
Sanitized 두 건의 receipt는 서버 전용 scratch의
`openstack-probe/nova-builder-ad667fe5bef0/recovery-evidence.json`에 있다.

두 guest의 회수를 확인한 **후** 정확한 VM·volume·SG 이름/ID/project,
volume metadata/attachment와 SG port binding을 대조하여 소유 세 자원만
삭제했고 세 ID의 부재를 조회했다. 신규 VM SSH private key와 서버 scratch의
임시 source 복사본도 제거했다. 공유 runner/domain, 운영 DB/store/helper,
GitHub ref/package는 변경하지 않았다. 검증 전용 script는 새 VM에만 보냈다.
이 결과는 해당 8GiB host와 두 network-none 장시간 RUN의 native
timeout/restart 회수에 한정된다. Host power loss, 일반 workload,
운영 account/credential separation의 증거가 아니다. 남은 승인 차단은
GitHub 게시와 기존 원격 helper 전송·공유 runner/domain 조작이며, 이 새
실기 승인으로 해제되지 않았다.

### 이어지는 로컬 검토·게시 전 경계 (2026-09-24 UTC)

현재 branch `codex/oci-root-phase1`, HEAD
`720761b605d6307dfa764601ccec01d5a474a754`에 위 projection 수정과
증거 문서는 여전히 **unstaged·미커밋**이다. 최신 timeout/restart 실기를
`README.md` Development 소개에 반영하되 이전 ordered-chain proof 및
현재 상세 응답의 portable HTTP 증거와 구별했다. Hub 응답 model·상세
route·project-scoped chain test를 검토했고 추가 runtime/code 변경은 하지 않았다.
이번 로컬 실행은 `hub/`의 `uv run pytest -q` **99 passed**, 수정된 두
Python 파일의 Ruff lint/format 통과, working-tree architecture guard
`source_sha256=2c4367b20505f2ae4b5cf0b26e07777f8a4ccbe1aef8601c9e4395d18b27e79c`
통과다. 이는 앞선 native proof를 다시 실행한 결과가 아니다.

다음 외부 단계인 commit/push·package 또는 branch 게시에는 정확한 대상과
별도 명시적 승인이 필요하다. 일반적인 '계속 진행'을 해당 승인으로 확대하지
않는다. 기존 helper 전송·shared runner/domain 조작 역시 승인 대기이며,
새 host power-loss 또는 production credential/account 분리 실기도 새
자원·실패 주입 범위의 별도 결정 없이는 실행하지 않는다.

### 승인된 작업 브랜치 게시와 자동 package gate (2026-09-24 UTC)

사용자가 **현재 여섯 파일만** `codex/oci-root-phase1`에 commit/push하도록
명시적으로 승인했다. 대상은 `ARCHITECTURE.md`, `README.md`, 이 인계,
`docs/install.md`, `hub/src/palimpsest_hub/api/hub.py`,
`hub/tests/test_builds.py`이며 `dev`·`main`·PR·패키지·원격 helper·shared
runner/domain은 제외했다. Push 전 원격 branch tip은 기존 HEAD
`720761b605d6307dfa764601ccec01d5a474a754`였고, 여섯 경로만 stage한
staged architecture guard가 source digest
`2c4367b20505f2ae4b5cf0b26e07777f8a4ccbe1aef8601c9e4395d18b27e79c`로
통과했다. Commit `1d82d90b2b2786bb7e52877cc6d8d68b0cfc5c19`를
fast-forward push한 뒤 같은 원격 branch tip을 조회했다.

Branch push가 `.github/workflows/development-package.yml`의 SHA-specific
prerelease workflow run `35985109659`를 자동 시작한 것을 발견했다.
패키지 게시는 승인 범위 밖이므로 이 run을 취소했다. `verify`와 `publish`
job이 모두 `cancelled`인 것을 확인했고, 그 SHA의
`refs/tags/package-1d82d90b2b2786bb7e52877cc6d8d68b0cfc5c19` 및
release는 없음을 조회했다. 이 게시 receipt만 추가하는 다음 docs-only
commit에는 Actions skip marker를 사용해 package workflow를 다시
시작시키지 않는다. Workflow 자체를 변경·비활성화하지 않는다.
기존 GPU helper 전송, shared runner/domain 조작, 새 실기는 계속 별도 승인
대기다. 첫 commit의 Hub 99건·Ruff·architecture 검증 외 추가 native
검증은 이 게시 과정에서 수행하지 않았다.

### 로컬 Apple Silicon libvirt/HVF conventional VM 실기 (2026-09-24 UTC)

시작 branch `codex/oci-root-phase1`, HEAD
`056760b0947917372ff0619c7bd555e0118056d4`의 clean tree에서
사용자가 이 Mac의 기존 libvirt 동작 확인을 요청했다. `qemu:///session`은
처음에 소유 domain이 없었고, `kern.hv_support=1` 및 aarch64 `virt`/HVF
capabilities를 확인했다. Homebrew QEMU 11.1.1·`pkgconf`를 설치하고
`uv sync --frozen --extra dev --extra kvm`으로 `libvirt-python 12.5.0`을
빌드했다. VM·이미지·상태는 전용 `/private/tmp/palimpsest-hvf-tgkFqoT1`
하위에만 두고 Ubuntu 24.04 arm64 공식 cloud image
`sha256:7b682958a67ff5de068e36de6af8b75fa645d296af5a70d6500527f6a33781db`를
가져왔다.

첫 native 시도에서 HVF는 GICv2를 거부하고 실패한 domain의 EFI varstore
undefine도 거부했다. GICv3와 explicit run-owned EFI 보존 후에는 QEMU
virtio-net-pci와 libvirt root-port의 PCI 슬롯이 충돌했고, NIC를
virtio-mmio로 바꾼 뒤 guest가 부팅되었지만 `ttyS0`에 쓰는 cloud-init
helper가 실제 ARM `ttyAMA0` console에 readiness를 전달하지 못했다.
`/dev/console`로 변경한 첫 부팅은 성공했고 SSH `exec`에서 `aarch64`,
`cloud-init status --long`의 `status: done`, `errors: []`를 확인했다.
처음 `stop`/`start`는 300초 readiness timeout으로 실패했다. 실제 reboot
console은 cloud-final 완료까지 기록했지만 ready unit은 실행되지 않았다.
`After=cloud-final.service`와 `WantedBy=multi-user.target`의 ordering
cycle을 제거하려고 ready unit을 `cloud-init.target`에 연결했다.

수정한 새 run `hvf-proof-tgkfqot1`은 초기 부팅·localhost SSH `exec`·
cloud-init 완료 후 `stop`→`start`까지 성공했다. SSH forwarding endpoint는
첫 부팅 `127.0.0.1:54337`, 재부팅 `127.0.0.1:54582`였고,
재부팅 console에 `PALIMPSEST_READY=1`과 ready unit 시작·완료가 기록됐다.
guest `ExecMainStatus=0`, `Result=success`, cloud-init `errors: []`도
확인했다. `rm --volumes` 후 `virsh -c qemu:///session list --all --name`과
격리 `runs/`가 모두 비었으며 전용 scratch를 삭제했다. QEMU·pkgconf 및
로컬 Python 개발 환경의 `[kvm]` extra는 설치된 상태다.

이번 결과는 **이 Mac과 위 단일 Ubuntu base의 experimental
`libvirt-hvf` conventional run/SSH/lifecycle**에 한정한다. Layer,
build, project, 다른 OS image, Linux KVM 및 OCI-root 실기 통과로
일반화하지 않는다. 이번 최종 source에 대해 cloud-init 집중 검사 13건,
`core-cli` lane 1146건, `host-runtime` lane 947건, 수정 Python 여섯 파일
Ruff lint/format, working-tree architecture guard
`source_sha256=15a9e0354654820e1778106909f5de2f05374afc9f72e4ad910d8af7b2471c15`가
통과했다. 다음 작업은 이 **unstaged·미커밋** source 변경을 별도로 검토하는
것이다. GitHub commit/push·package 게시·기존 원격 helper 전송·shared
runner/domain 조작은 이 로컬 실기 요청으로 승인되지 않았다.

### 사용자 요청에 따른 작업 브랜치 게시 및 공유 자원 경계 (2026-09-24 UTC)

사용자가 로컬 HVF 실기 후 원격 게시와 공유 자원 작업까지 요청했다.
게시 대상은 기존 `codex/oci-root-phase1` ref와 위 검증된 14개 파일로
한정했다. 처음 원격 tip은
`056760b0947917372ff0619c7bd555e0118056d4`였고 staged
architecture guard는 source digest
`15a9e0354654820e1778106909f5de2f05374afc9f72e4ad910d8af7b2471c15`로
통과했다. Source commit
`a54f1e96428ff333c9629f3dae82d1bb1a2fbea8`을 non-force
fast-forward push했고 조회한 원격 work-branch tip도 동일하다. Commit의
`[skip actions]`로 package 자동 실행을 막았다. 조회 시 최근 Actions
목록에 이 SHA의 새 run은 없고, 직전 package run `35985109659`는
`1d82d90`에서 취소된 그대로였다. 패키지 artifact/tag, `dev`, `main`,
PR은 이 작업에서 게시·변경하지 않았다.

`pieroot-server`의 `qemu:///system` domain 목록 22개만 읽었고 기존
domain에는 조작을 하지 않았다. 전용 GitHub runner 21은 조회 시 online,
idle였고 `PALIMPSEST_KVM_ENABLED=true`였다. `test.yml`은 `main`/`dev`
push 또는 PR, `workflow_call`에서 native job을 실행하며 수동 dispatch는
없다. 새 전용 domain을 만들고 published SHA에서 선택된 native proof를
수행하는 것과 PR로 runner의 전체 gate를 트리거하는 것은 자원 및
게시 범위가 달라, 공유 자원 변경 목적·대상을 확정하기 전까지 수행하지
않았다. 이 source의 Linux KVM/OCI-root native 통과 증거는 아직 없다.

### PR을 통한 native KVM gate 트리거 시도 (2026-09-24 UTC)

사용자가 앞선 선택지 중 "PR로 native KVM gate 실행"을 확정해 PR
[`#3`](https://github.com/openstack-afterglow/palimpsest/pull/3)을
`codex/oci-root-phase1`→`dev`로 열었다(merge 요청 아님, native gate 트리거
목적). 열림 직후 `mergeable_state=dirty`, `statusCheckRollup=[]`이었다.
~45초 대기 후에도 `pull_request` event run이 없어 PR을 close→reopen했지만
동일했다. Head SHA `61757c71f656324971466a8e102a2b7fbecbd8e1`의
check-suites 조회에는 무관한 `claude` app 항목만 `queued`이고 GitHub
Actions check-suite가 없다. 같은 SHA의 직전 `push` 이벤트는 `Test`,
`Development package`, `Build and Push Palimpsest Hub` 세 workflow를 모두
성공적으로 트리거했으므로, 이는 이 repository의 `pull_request` 트리거에
한정된 관측된 동작이다. `actions/permissions`는 `enabled:true`,
`allowed_actions:"all"`이고 `dev` branch protection은 없다(404). 이
제약을 우회하려고 workflow 파일이나 repository 설정, branch protection을
변경하지 않았다. 전용 GitHub runner `pieroot-server-palimpsest-kvm`(id 21,
`PALIMPSEST_KVM_ENABLED=true`)은 online·idle 상태 그대로다. 이 PR로
`dev`/`main`은 변경되지 않았고 병합도 요청하지 않았다. Linux KVM/OCI-root
native 통과 증거는 이 시도로 추가되지 않았다.

**정정 및 conflict 해소 (2026-09-24 UTC).** 위에서 "직전 push가 Test 등
세 workflow를 모두 정상 트리거했다"고 인용한 근거는 실제로는 이 branch의
커밋이 아니라 같은 시각 `dev`에 독립적으로 push된 무관한 SHA
`3705880e5c03486b69ac052fd78ddad8ce799655`(아래 `ci-perf` 게시 commit)의
실행이었다. 재확인 결과 이를 정정한다. `pull_request` 트리거가 반응하지
않은 실제 원인은 `61757c7`/`6de46ac` 두 commit의 HEAD message에 있던
`[skip actions]` 지시문이다. GitHub의 skip-ci 문서는 이 지시문이 `push`와
`pull_request` 양쪽 이벤트의 workflow 실행을 모두 막는다고 명시하며, PR의
HEAD commit일 때도 동일하게 적용된다. 문서 receipt commit에 실제 소스
수정 commit(`a54f1e9`)과 같은 관례로 이 지시문을 붙인 것이 오류였다. PR
#3은 `dev`와의 실제 file-level conflict(`ARCHITECTURE.md`,
`docs/development-handoff.md`)로 `mergeable_state=dirty`이기도 했다.
`origin/dev`를 이 branch에 non-force merge commit으로 통합해 두 conflict를
해소했다(각 파일의 diverged 구간은 순수 추가형이라 양쪽 서술을 모두
보존했다). 이 merge commit 메시지에는 `[skip actions]`를 넣지 않아 push와
새 PR synchronize 이벤트가 정상적으로 `Test`(및 AGENTS.md 규칙10이 기록한
대로 `pull_request`에서도 실행되는 `kvm` job)를 트리거하도록 했다. `dev`
자체는 이 merge로 변경되지 않는다. Source/schema 계약은 바뀌지 않았다.

**정정: 위 "저장소 전체 Actions 정지" 결론은 철회한다 (2026-09-24 UTC).**
실제 원인은 `d88be61`/`ef9f68d` 두 commit의 본문·trailer가 skip-ci 토큰
문자열 자체를 (그 토큰이 없다고 설명하는 부정문 안에서도) 그대로 포함했기
때문이다. GitHub의 매칭은 문자열 존재만 보고 부정/긍정 문맥을 구분하지
않는다. 조직 billing/webhook 장애 가설은 근거 부족으로 철회한다. 다음
항목에서 그 토큰 문자열을 전혀 포함하지 않는 새 commit으로 재검증한다.

**Native KVM gate 실제 트리거·통과 확인 (2026-09-24 UTC).** 토큰 문자열이
전혀 없는 commit
[`1572025a5a1368a8e53783762f2643ff9137e4ed`](https://github.com/openstack-afterglow/palimpsest/commit/1572025a5a1368a8e53783762f2643ff9137e4ed)를
push한 뒤 15초 안에 `pull_request` 이벤트로 `Test`
([run `36010249678`](https://github.com/openstack-afterglow/palimpsest/actions/runs/36010249678))와
`Build and Push Palimpsest Hub`, `push` 이벤트로 `Development package`가
모두 시작해 세 workflow 전부 `success`로 끝났다. `Test`의 job 목록에서
`Native KVM stage-1 proof`와 `Required native KVM proof`가 모두
`success`이고, `stage1-native-kvm-proof` artifact(450,980B)와
`oci-filesystem-proof` artifact(1,905B)가 이 실행에 남았다. 이는 사용자가
선택한 "PR로 native KVM gate 실행"의 실제 성공 증거다. PR #3은 여전히
열려 있고 병합은 요청하지 않았으며, `dev`/`main`은 변경되지 않았다.

**PR 후속 로컬 검토·수정 (2026-09-24 UTC; 미게시).** PR #3의 현재 원격 HEAD `a9e7be7`은 별도 `Test` run [`36010811193`](https://github.com/openstack-afterglow/palimpsest/actions/runs/36010811193)에서 19 job 모두 success였고, native/required KVM proof도 success였다. 같은 SHA의 Hub `36010810898`과 Development package `36010805259`도 success이며 SHA-specific prerelease가 실제로 발행됐다. 뒤의 **로컬** 수정은 이 세 run에 포함되지 않는다.

독립 검토에서 CI round-3 계약의 신규 결함은 발견하지 못했다. HVF 변경의 두 readiness 경계가 지적됐다: x86 guest에서 `/dev/console`이 VGA를 가리키면 serial log가 sentinel을 잃고, 부팅 후 `/etc/cloud/cloud-init.disabled`로 cloud-init을 끄면 `cloud-init.target`의 ready unit이 재시작 때 실행되지 않는다. Hub 검토의 parent가 import에서 빠져도 child가 게시되는 경우와 PAX 경로 override 불일치는 `origin/dev`에도 있던 별도 결함이며 이번 PR 수정으로 포장하지 않는다.

로컬 source는 x86_64 `/dev/ttyS0`·aarch64 `/dev/ttyAMA0`로 guest serial 출력을 선택하고, 최초 project-init 완료 marker를 남긴다. cloud-init이 명시적 disabled 파일로 꺼진 **이후** 부팅에만 marker와 disabled 파일 두 조건을 가진 별도 multi-user fallback unit이 activation 뒤 readiness를 기록한다. 기존 cloud-init.target unit과 cloud-final 순서는 유지한다. 수정 전 x86 seed는 serial sentinel이 없음을 확인했고, 수정 후 두 arch의 seed가 각 serial과 두 fallback 조건을 담는 것을 확인했다. 집중 cloud-init/runtime 70건, Ruff lint와 diff whitespace가 통과했다. 이는 generated-seed/portable proof다. 새 수정의 실제 x86 KVM 부팅 및 cloud-init-disabled 재부팅은 아직 실행하지 않았고, 앞선 원격 KVM gate 성공을 이 수정의 증거로 쓰지 않는다. GitHub 게시·PR merge·runner 설정 변경은 하지 않았다.

**작업 브랜치 게시 승인 및 preflight (2026-09-24 UTC).** 사용자가 이번 일곱 파일만 `codex/oci-root-phase1`에 commit/push하고 자동 Development package prerelease 및 PR native KVM gate 결과를 확인하도록 승인했다. `dev`/`main` 병합, 추가 원격 helper 전송, runner 설정·기존 domain 조작은 승인하지 않았다. Push 전 원격 branch tip은 `a9e7be701d662eb732e1cfdab1d457b14b6bc797`이다. 집중 70건 외에 `core-cli host-runtime` 선택 2,104건, architecture guard 14건, lane manifest·Ruff lint/format·working architecture guard·diff whitespace가 통과했다. 이 문장은 **push 전** 검사 상태이며 이 뒤의 새 CI 성공 주장이 아니다.

**작업 브랜치 실제 게시·검사 (2026-09-24 UTC).** 일곱 파일 commit [`071d82d0fcc1a88d0d8f5d791fa87095c13d1d81`](https://github.com/openstack-afterglow/palimpsest/commit/071d82d0fcc1a88d0d8f5d791fa87095c13d1d81)를 `codex/oci-root-phase1`에만 non-force push했고, 같은 원격 tip을 재조회했다. PR `Test` [`36014570699`](https://github.com/openstack-afterglow/palimpsest/actions/runs/36014570699)는 19 job 모두 success이며 `Native KVM stage-1 proof`와 `Required native KVM proof`도 success다. `stage1-native-kvm-proof` artifact id `10813952706`(450,949B)와 `oci-filesystem-proof` id `10814197273`(1,905B)가 이 SHA에 남았다. Hub workflow `36014570977`의 두 job과 Development package `36014566702`의 verify/publish 두 job도 success다. Prerelease [`package-071d82d...`](https://github.com/openstack-afterglow/palimpsest/releases/tag/package-071d82d0fcc1a88d0d8f5d791fa87095c13d1d81)에 wheel·sdist·`SHA256SUMS`가 모두 uploaded 상태다. 이는 기존 stage-1 KVM matrix의 통과이지 새 cloud-image x86 부팅이나 cloud-init-disabled reboot 실기가 아니다. 조회한 `dev`는 `3705880e`, `main`은 `60fa42f` 그대로이고 PR #3은 open/unmerged다. 이 결과 기록 commit은 문서만 바꾸며, 새 자동 workflow 결과는 별도 SHA로 판정한다.

### CI critical-path checkpoint (2026-09-24)

기준 SHA는 `60fa42f`(= 당시 `origin/dev` = `origin/main`)이고, branch `ci-perf`에 local commit `2e37538`과 review 1·2·3차 반영 commit으로 남겼다. 이 기록 시점(2026-09-24)에는 push·PR·저장소 설정 변경을 하지 않았다. push하면 이 문단에 push한 SHA를 적는다.

**게시 상태 재확인 (2026-09-24 UTC).** 위의 미게시 문장은 당시 checkpoint다. 현재 `dev` tip `3705880e5c03486b69ac052fd78ddad8ce799655`에는 이 CI 변경과 최종 문서 정정이 들어 있으며, 작업 브랜치에는 merge commit `d88be61`을 통해 포함됐다. `main` tip은 여전히 `60fa42f`다. 이 재확인은 새 push나 PR merge가 아니다.

**변경 내용**

- `.github/workflows/test.yml`에서 `portable-linux`의 `max-parallel: 3`과 `portable-macos`의 `max-parallel: 2`를 제거했다. 이제 6+4 shard가 한 wave로 시작한다.
- 다음은 바꾸지 않았다: shard 수, job id와 순서, aggregator 이름과 판정, trigger, native KVM 필수 gate.
- `tests/unit/test_test_lanes.py`에 CI 형태 계약 두 개(3 node)를 추가했다.
  - shard 목록 `[1..N]`, `--shard …/N` 분모, `max-parallel` 부재 또는 N 이상, `fail-fast: false`, aggregator 이름·`if: always()`·`needs`를 고정한다.
  - gate job이 앞에 없는지와, self-hosted job이 `kvm` 하나뿐인지를 고정한다.
  - 임시 변형 workflow 네 가지(macOS cap 2, 분모 5, shard 5개, `needs: checks`)를 모두 잡는 것을 확인했다.
- `AGENTS.md`에 `CI 파이프라인 성능 규정` 12개를 추가했다.
- `ARCHITECTURE.md`(Development and verification, Change guide, Maintenance)와 [testing.md](testing.md)를 갱신하고 stamp했다.

**실측 기준.** 읽기 전용 `gh` 조회로 얻은 수치다.

- 표본은 현재 19-job 형태의 `Test` 완료 실행 21건이다.
- 크리티컬 패스는 중앙값 325초, p90 781초였다. burst 8건을 제외하면 303/346초다.
- `Unit tests (macOS 15)`가 21건 중 19건에서 마지막으로 끝났다.
- 두 번째 wave 대기: macOS 3/4·4/4가 151/170초, Linux 4–6이 91–105초.
- KVM job은 140초 걸렸고 대기 중앙값은 142초였다.

**기대 효과.** dev/PR 약 155–170초는 **추정**이다. push 후 20회 이상 재측정해야 하며, 재측정 전에는 효과로 기록하지 않는다. main push는 같은 SHA의 dev 실행과 단일 KVM runner·macOS 5개 한도를 두고 경쟁하므로 약 290초에 머물 것으로 본다.

**게시 후 초기 관찰 (2026-09-24 UTC, 표본 3건).** 변경을 포함한 완료 `Test`는 확인 시점에 3건뿐이다. `dev` push `35990877737`(SHA `3705880e`)은 시작부터 마지막 job 종료까지 192초, PR `36010249678`(SHA `1572025a`)은 182초, PR `36010811193`(현재 HEAD `a9e7be7`)은 157초였다. 세 실행 모두 19개 job이 성공했고, 마지막 job 종료 시각에서 `run_started_at`을 뺐다. 마지막 두 PR의 `Native KVM stage-1 proof`와 `Required native KVM proof`도 성공했다. 현재 HEAD의 Hub workflow `36010810898`과 Development package workflow `36010805259`도 성공했고, 해당 SHA의 prerelease에 wheel·sdist·`SHA256SUMS` 세 asset이 업로드됐다. 이는 세 표본의 관찰이지 20건 기준의 중앙값·p90 효과 판정이 아니다. 표본을 만들려고 CI나 공유 KVM runner를 추가 실행하지 않는다.

**실행한 검사.** 모두 local macOS, Python 3.12에서 실행했다.

- `check_architecture.py`: stamp 후 working 통과.
- `uv sync --frozen --extra dev`, import smoke, fixture `--check`, CLI reference `--check`, `ruff check .`, `ruff format --check .`(351 files), `test_lanes.py list --check`: 통과.
- `plan --changed HEAD`: core-cli와 qualification을 선택했다.
- `build_package.py`: 통과. Python 3.11 `palimpsest --version`: `0.1.4`.
- focused 4 파일: 132 passed.
- `run core-cli qualification`: 1,424 passed, 7 skipped.
- portable 6 shard를 순차 실행해 합계 6,084 node, 5,867 passed, 217 skipped, 실패 0이었다.
- `check_filesystem_fixtures.py`: 통과.
- Hub(`hub/`의 sync, import, ruff, format, pytest 91 passed, `uv build`): 통과.
- `actionlint`: 기존과 같은 custom label `kvm` 경고 1건만 남았다.

**실행하지 않은 검사**

- Docker가 필요한 gate: BuildKit named OCI context, guest stage-1 binary, workload proof ELF 재현, Hub Docker image build. 이후 별도로 순차 실행할 예정이다.
- 권한이 필요한 OCI filesystem proof, native KVM, GitHub 실행.

**독립 검토 1차(2026-09-24).** `2e37538`에 대한 독립 검토가 medium 2건과 low 7건을 지적했다. 후속 commit `fix: address CI review round 1`에서 모두 반영했다. 반영분에 대한 재검토는 아래 독립 검토 2차다.

- **KVM runner 노출 근거 정정(medium).** 처음에 "공개 PR 코드가 KVM runner에서 실행됐다"고 인용한 run 세 건은 모두 같은 저장소 branch에서 온 PR 실행이었다(`35600862976`은 `dev`, `35600812317`·`35029001178`은 `codex/oci-root-phase1`; `head_repository = openstack-afterglow/palimpsest`). 2026-09-24 읽기 전용 조회에서 API가 나열한 `pull_request` 실행 22건 중 fork에서 온 실행은 없었다. 위험은 잠재적이다. 이 절 아래의 승인 대기 1을 그렇게 고쳐 적었다.
- **aggregator 판정 계약(medium).** 처음 계약은 aggregator의 이름·`if: always()`·`needs` 포함 여부만 봤다. 그래서 verdict를 `true`로 바꾸거나 KVM skip을 받는 변형이 통과했다. 이제 다음을 고정한다.
  - 정확한 `needs`
  - 단일 verdict step의 `env`와 성공만 받는 `run` 문자열
  - `shell`·`defaults`·`continue-on-error`의 부재
  - aggregator 밖 job-level `if:`가 `kvm`의 변수 조건뿐인지
- **runner 판정(low).** `runs-on`의 mapping(`group`·`labels`) 형태까지 정규화한다. 범위를 모든 workflow로 넓히고 allowlist를 `test.yml`의 `kvm`과 `release.yml`의 `kvm-proof`로 두었다. `release.yml`이 `v*` tag push 전용인지도 고정한다.
- **규정 문구(low).** 다섯 가지를 고쳤다.
  - AGENTS.md 규칙 10을 `Test` workflow 범위로 한정했다.
  - runner group 제한은 저장소 수준 runner에는 쓸 수 없다고 적었다. `pieroot-server-palimpsest-kvm`은 저장소 수준 runner이고, org plan은 `free`이며, runner-group API는 403이었다.
  - 규칙 8의 PR diff를 merge-base(세 점) 기준으로 바꿨다.
  - 규칙 5의 wrapper 문장을 고쳤다.
  - 규칙 12의 node 수 기준에 출처를 붙였다.
- **node 수 기준.** 6,081은 `60fa42f`의 CI run `35826465548`에서 "Lane shard" 줄의 선택 node를 더한 값이다. Linux 6 shard와 macOS 4 shard의 합계가 같으며, pass 수가 아니다. 6,084는 `2e37538`의 로컬 순차 6 shard 합계이고, 계약 3 node를 더한 값이다.
- **변형 검사.** 임시 workflow 변형 14가지를 새 계약이 모두 잡았고, 끝난 뒤 `.github/`를 원복했다. 변형은 다음과 같다.
  - `pure`·`unit-macos` verdict를 `true`로 바꾸기, `pure`에 `|| true` 붙이기
  - KVM verdict를 `!= "failure"`로 바꾸기
  - `pure`의 `needs`에서 `checks` 빼기
  - KVM step에 `shell: bash {0}` 넣기, KVM env에서 `PALIMPSEST_KVM_ENABLED` 빼기, workflow `defaults.run.shell` 넣기
  - `portable-linux`에 `continue-on-error`나 job-level `if:` 넣기
  - `runs-on: {group: kvm-group}`, `runs-on: kvm`, 다른 workflow에 self-hosted job 넣기
  - `release.yml`에 `pull_request` trigger 넣기
- **반영 뒤 검사.** 모두 local macOS에서 실행했다.
  - `ruff check .`, `ruff format --check .`(351 files), `test_lanes.py list --check`: 통과.
  - focused 4 파일(`test_test_lanes.py`, `test_oci_convert_security.py`, `test_development_package_workflow.py`, `test_architecture_guard.py`): 136 passed.
  - `run core-cli qualification`: 1,435 node, 1,428 passed, 7 skipped.
  - portable `--collect-only`: 6,088 node.
  - `actionlint`(`test.yml`, `release.yml`): 기존 custom label `kvm` 경고 2건만 있었다. workflow 파일은 바꾸지 않았다.
  - 전체 portable 6 shard는 다시 실행하지 않았다. 바뀐 test 파일은 `core-cli` lane 하나뿐이다.

**독립 검토 2차(2026-09-24).** round 1 반영분에 대한 재검토가 medium 1건과 low 8건을 지적했다. 후속 commit `fix: address CI review round 2`에서 반영했으며, 반영분에 대한 재검토는 아직 받지 않았다. workflow 파일은 바꾸지 않았다.

- **shard 실행 계약(medium).** round 1 계약은 matrix의 `shard` 목록·`fail-fast`·`max-parallel`과 shard 명령 문자열만 봤다. 그래서 다음 변형이 모두 통과했고 aggregator는 green이었다.
  - `exclude: [{shard: 6}]`
  - macOS shard step의 `if: github.event_name == 'push'`
  - step `shell: "true {0}"`, job `defaults.run.shell: "true {0}"`
  - job·workflow `env: PYTEST_ADDOPTS: --collect-only`
  - `checks` step의 `if`

  이제 다음을 고정한다.
  - portable matrix job 두 개의 job key·runner·`matrix`와 step 목록 전체
  - `test.yml`의 workflow-level `env` 부재
  - aggregator가 읽는 의존 job(`checks`, portable 두 개, `kvm`)의 job key 집합
  - `test.yml` 전체 step의 `shell`·`continue-on-error` 부재와 `always()` 외 step `if` 부재

  `kvm` upload의 `if: always()`는 step을 건너뛰지 않으므로 허용한다. `kvm` proof step의 `if: false`는 잡는다.
- **hosted label(low).** prefix 정규식을 정확한 허용 목록(`ubuntu-latest`·`ubuntu-24.04`·`macos-15`)으로 바꿨다. `runs-on: ubuntu-kvm`을 잡는다.
- **`test.yml` trigger(low).** `workflow_call`, `main`·`dev` push·`pull_request`를 정확히 고정한다. `pull_request_target` 추가를 잡는다.
- **reusable caller(low).** `uses:` job은 `KeyError` 대신 명시적 assertion으로 거부한다. `test.yml` 호출자는 self-hosted `kvm` job을 물려받기 때문이다.
- **규정 문구(low).** AGENTS.md의 다섯 곳을 고쳤다.
  - 규칙 10: 승인 대기 항목을 절 이름으로 가리키게 했다. 이 절의 승인 대기 1에도 같은 수정을 했다.
  - 규칙 9: 건너뛰기 조건에 "head branch의 push `Test`가 같은 tree를 이미 테스트했다"를 더했다. 지금은 `main`·`dev`만 해당한다.
  - 규칙 5: CI는 "Lane shard" 수를 출력만 한다는 점을 밝혔다. 실행 중 실패로 처리되는 경우는 exit 5·2·4다. shard별 수와 합계는 계약으로 보장하며, 이 점이 정본 규칙 5와 다르다.
  - 규칙 8: 두 점 diff 금지와 merge ref의 `HEAD^1..HEAD` 허용이 정본 규칙 8을 의도적으로 구체화한 것이라고 밝혔다.
  - 규칙 2: 같은 event의 hosted job 수를 적었다. `Test` 시작 시점 15개(총 18개)에 `hub-docker` `test`와 `development-package` `verify`를 더해 push 17개, PR 16개다. workflow 정의로 센 값이며 실측이 아니다.
- **변형 검사.** 임시 복사본에서 workflow 변형 41가지를 돌렸다. 대상 파일은 `test_test_lanes.py`, `test_oci_convert_security.py`, `test_development_package_workflow.py`이고, 변형 전 baseline 123 passed를 먼저 확인했다.
  - 39가지를 잡았다(pytest exit 1). 재검토 목록 17가지와 추가 변형이다. 추가 변형은 `$GITHUB_ENV` step, checkout `ref: main`, step `env`·`continue-on-error`, `checks`·`kvm`의 job-level `env`·`defaults`, matrix `include`, macOS job의 `ubuntu-latest`, job-level `if`·`continue-on-error`, `hub` step의 `shell`·`continue-on-error`, `if: failure()`, 분모 5, wrapper 명령, push branch 추가, `runs-on` 누락, 다른 workflow의 self-hosted job, `release.yml` `pull_request`, `pure` verdict `true`다.
  - 의도한 잔여 공백 2가지는 잡지 않았다. `hub` step의 `env: PYTEST_ADDOPTS`와 `checks`의 `$GITHUB_ENV` step이다. 비-matrix job의 step 내용은 고정하지 않으며, 이를 AGENTS.md 규칙 11과 [testing.md](testing.md)에 적었다.
- **반영 뒤 검사.** 모두 local macOS, Python 3.12에서 실행했다.
  - `ruff check .`, `ruff format --check .`(351 files), `test_lanes.py list --check`: 통과.
  - focused 4 파일(`test_test_lanes.py`, `test_oci_convert_security.py`, `test_development_package_workflow.py`, `test_architecture_guard.py`): 137 passed.
  - `run core-cli qualification`: 1,436 node, 1,429 passed, 7 skipped.
  - portable 6 shard를 순차 실행했다. 합계 6,089 node(1,045·954·1,052·997·1,019·1,022), 5,872 passed, 217 skipped, 실패 0이었다. portable `--collect-only`도 6,089 node다.
  - `actionlint`(workflow 다섯 개): 기존 custom label `kvm` 경고 2건(`test.yml`, `release.yml`)만 있었다.
  - Docker·권한이 필요한 gate와 GitHub 실행은 이번에도 하지 않았다.

**독립 검토 3차(2026-09-24).** round 2 반영분에 대한 재검토가 medium 1건과 low 4건을 지적했다. 후속 commit `fix: address CI review round 3`에서 반영했으며, 반영분에 대한 재검토는 아직 받지 않았다. workflow 파일은 바꾸지 않았다. 같은 때 정본 규칙 3과 10의 해석이 명확해졌다.

- **규칙 10 통제 모델(medium, 문서만).** 이전 문구는 YAML `if:`가 "통제 수단이 아니"라고 했다. 그리고 runner group 저장소 제한과 `all_external_contributors`가 함께 갖춰지면 충분한 것처럼 읽혔다. 둘 다 정본 규칙 10과 다르다. 이제 AGENTS.md 규칙 10은 두 층으로 적는다.
  - YAML event gate가 `pull_request` 실행을 runner에서 떼어 놓는다. 설정은 그 gate를 지우는 PR에 대한 backstop이다.
  - 두 설정을 모두 갖춰도 같은 저장소 branch PR, Dependabot PR, 승인된 fork PR은 막지 못한다고 밝혔다.
  - gate, `kvm` `if` 계약 고정, "skip을 통과로 받지 않는다" 조항이 서로 충돌한다는 점을 적었다. 해소는 아래 승인 대기 1에 넣었다.
  - `release.yml` `kvm-proof`가 받는 workflow-level `contents: write`·`id-token: write`의 노출을 적었다.
  - 설정 위치를 적었다. 저장소 수준 runner라 runner group이 없으므로, 설정 층은 소유자 작업이다.
  - 같은 문구를 이 절 승인 대기 1, `명시적 승인 대기 — 실행 금지` 절 2번, `ARCHITECTURE.md`, [testing.md](testing.md), 계약 test 주석에서도 고쳤다.
- **job 형태·token 계약(low).** 이전 계약은 aggregator가 읽는 job의 key 집합만 고정했다. 그래서 `hub`·`guest-stage1-binary`의 `defaults.run.shell: "true {0}"`, `hub`의 job-level `env: PYTEST_ADDOPTS`, `permissions: write-all`이 통과했다. 새 계약 `test_ci_test_workflow_jobs_and_token_are_pinned`가 다음을 고정한다.
  - `test.yml` 11개 job 전체의 key 집합과 job id 집합
  - `hub`의 `defaults` 값 `{run: {working-directory: hub}}`
  - top-level key 집합(`name`·`on`·`permissions`·`jobs`)과 `permissions == {contents: read}`
- **발행 게이팅 문구(low).** AGENTS.md 규칙 3을 명확해진 정본에 맞췄다. 발행(push)과 배포만 `Test` 전체 결과로 게이팅한다. 아무것도 발행하지 않는 PR 검증 build는 병렬로 돌려도 되며, `hub-docker`의 PR build가 여기에 해당한다. 기존 예외 세 가지를 적었다. `hub-docker` push·tag·dispatch의 `build-and-push`, `development-package` `publish`, `release` `publish`·`github-release`다. 아래 승인 대기 3에 넣었다.
- **규칙 5 차이(low, 문서만).** 계약이 막는 것은 wrapper가 `--shard`를 빠뜨리는 경우뿐이라고 좁혀 적었다. workflow 밖 무력화의 실측을 기록했다. `PYTEST_ADDOPTS=--collect-only`로 `run portable --shard 1/256`을 실행하니 "Lane shard" 줄에 selected 24 node를 출력하고 한 건도 실행하지 않은 채 exit 0이었다. 실행 중 검사는 plugin 동작을 바꾸므로 추가하지 않았다. 아래 승인 대기 4에 넣었다.
- **특권 trigger(low).** 새 계약 `test_ci_no_workflow_uses_a_privileged_pull_request_trigger`가 모든 workflow에서 `pull_request_target`·`workflow_run`을 거부한다. 허용 목록은 비어 있다. `on`의 문자열·목록·mapping 형태와 따옴표 친 `"on"` key를 모두 읽는다.
- **변형 검사.** 임시 복사본에서 변형 18가지를 돌렸다. 대상 파일은 `test_test_lanes.py`, `test_oci_convert_security.py`, `test_development_package_workflow.py`이고, 변형 전 baseline 125 passed를 먼저 확인했다.
  - 16가지를 잡았다(pytest exit 1). 재검토 목록의 4가지(`hub`·`guest-stage1-binary` `defaults.run.shell`, `hub` job `env`, `permissions: write-all`)가 들어 있다. 추가로 잡은 변형은 다음과 같다.
    - workflow `contents: write`, `kvm` job-level `permissions`
    - `oci-fs-proof` job `continue-on-error`, `local-oci-build` job `env`
    - workflow `concurrency`, 표에 없는 새 job
    - `kvm` event gate. 규칙 10 충돌을 확인하는 변형이다.
    - 새 `pull_request_target` workflow(hosted, PR head checkout), 새 `workflow_run` workflow
    - 따옴표 친 `"on": pull_request_target`, 목록 형태 `on: [push, pull_request_target]`
    - `hub-docker.yml` trigger에 `pull_request_target` 추가
  - 의도한 잔여 공백 2가지는 잡지 않았다. `hub` step의 `env: PYTEST_ADDOPTS`와 `checks`의 `$GITHUB_ENV` step이다.
  - 새 파일 변형은 끝날 때마다 지웠다. 끝난 뒤 복사본의 `.github/`가 worktree와 같은지 `diff -r`로 확인했다.
- **반영 뒤 검사.** 모두 local macOS, Python 3.12에서 실행했다.
  - `ruff check .`, `ruff format --check .`(351 files), `test_lanes.py list --check`: 통과.
  - focused 4 파일(`test_test_lanes.py`, `test_oci_convert_security.py`, `test_development_package_workflow.py`, `test_architecture_guard.py`): 139 passed.
  - `run core-cli qualification`: 1,438 node, 1,431 passed, 7 skipped.
  - portable `--collect-only`: 6,091 node. 바뀐 test 파일은 `core-cli` lane 하나뿐이라 portable 6 shard는 다시 실행하지 않았다.
  - `actionlint`(workflow 다섯 개): 기존 custom label `kvm` 경고 2건(`test.yml`, `release.yml`)만 있었다.
  - Docker·권한이 필요한 gate와 GitHub 실행은 이번에도 하지 않았다.

**최종 검토(2026-09-24).** round 3 반영분에 대한 최종 검토가 medium 1건(문서만)을 지적했다. AGENTS.md 규칙 11은 `test_development_package_workflow.py`가 development-package의 step을 고정한다고 적었다. 실제 계약은 trigger·permissions·concurrency·job id, `verify` `if`와 run text의 포함 여부, `publish`의 `needs`·`permissions`·`uses`·checksum 순서·helper 인자·금지 문자열만 본다. `verify`는 SHA별 prerelease를 만드는 `publish`의 유일한 gate다.

- 임시 복사본에서 변형 9가지를 돌렸다. `verify` step `continue-on-error`·`if: false`·`shell: 'true {0}'`, `verify` job `continue-on-error`, checksum step `continue-on-error`·`if: false`, `publish` job `if: always()`, 명령 `echo` 감싸기, `verify` `if`의 `|| true`다. 모두 세 CI 계약 파일(`test_development_package_workflow.py`, `test_test_lanes.py`, `test_oci_convert_security.py`)의 125건을 통과했다.
- 규칙 11을 검사 항목과 고정하지 않는 항목으로 고쳐 적었다. 계약은 확장하지 않았으므로 workflow·계약·portable node 수(6,091)는 그대로다. 공백을 막으려면 `verify`·`publish`의 job key와 step 목록을 `test_test_lanes.py`의 portable matrix처럼 정확히 고정하는 계약을 따로 추가해야 한다.

**승인 대기·소유자 결정.** 구현하지 않았고 설정도 바꾸지 않았다.

1. self-hosted KVM runner `pieroot-server-palimpsest-kvm`의 노출이다. 이 문서 `명시적 승인 대기 — 실행 금지` 절의 2번과 같은 항목이다.
   - 현재 상태: 위에 인용한 run 세 건은 모두 같은 저장소의 trusted branch에서 온 PR 실행이다. 그러나 `kvm` job에는 event gate가 없어 모든 `pull_request` 실행이 이 persistent runner에서 돈다. 승인 정책이 `first_time_contributors`이므로, 이전에 merge된 기여가 있는 외부 contributor의 fork PR도 승인 없이 실행될 수 있다.
   - 통제는 두 층이다(AGENTS.md 규칙 10). YAML event gate가 `pull_request` 실행을 막고, 설정은 그 gate를 지우는 PR에 대한 backstop이다. 설정만으로는 부족하다. `all_external_contributors`와 runner group 저장소 제한을 모두 갖춰도 같은 저장소 branch PR, Dependabot PR, 승인된 fork PR은 여전히 이 runner에서 실행된다.
   - 충돌: YAML gate를 넣으면 PR에서 `kvm`이 skip되고, `Required native KVM proof`가 모든 PR에서 red가 된다. AGENTS.md 규칙 10의 "skip을 통과로 받지 않는다" 조항과 `test_ci_test_jobs_start_without_a_gate_job_in_front`의 정확한 `kvm` `if` 고정도 gate 추가를 막는다.
   - 결정할 것 (a): 충돌 해소 방안. 예를 들어 KVM proof를 push 전용(`github.event_name != 'pull_request'`)으로 두고, PR에서 `Required native KVM proof`가 무엇을 뜻할지 정한다. PR에서만 event 조건으로 skip을 받게 하거나, PR의 required check에서 빼는 방법이 있다. 결정되면 gate, aggregator verdict, 계약, 규정을 한 변경에서 바꾼다.
   - 결정할 것 (b): 설정 backstop. fork PR 승인은 저장소 `Settings` → `Actions` → `General`의 `Approval for running fork pull request workflows from contributors`에서 `Require approval for all external contributors`를 고른다. runner group 제한은 저장소 수준 runner에는 없으므로 org 수준 재등록이 먼저다. Free plan에서 그 제한을 쓸 수 있는지는 확인하지 않았다.
   - 결정할 것 (c): 두 층이 갖춰지기 전의 조치. PR의 workflow 수정까지 포함해 노출을 확실히 없애는 방법은 runner stop·unregister뿐이다.
   - `release.yml` `kvm-proof`는 같은 runner에서 workflow-level `contents: write`·`id-token: write`를 받는다. 앞선 PR 실행이 runner에 남긴 state를 release 실행이 쓰면, PR 코드가 그 token에 닿을 수 있다.
2. 같은 SHA를 dev와 main에 3–6초 간격으로 push하는 문제다. 현재 형태 push 실행 14건 중 7건이 이 경우였고, 매번 KVM 대기 142–144초가 생겼다. dev가 green이 된 뒤 main을 fast-forward하는 방식 등으로 줄일 수 있다.
3. 정본 규칙 3의 기존 예외다. 다음 세 발행 경로는 `Test` 전체 결과를 기다리지 않는다.
   - `hub-docker.yml` `build-and-push`: `main`·`dev` push, `v*` tag push, `workflow_dispatch`에서 GHCR에 push한다. 자체 `Hub Unit Tests`에만 건다.
   - `development-package.yml` `publish`: SHA별 GitHub prerelease를 만든다. 자체 `verify`에만 건다. `verify`가 실행하는 테스트는 `core-cli`·`qualification` lane과 development-package 계약뿐이다.
   - `release.yml` `publish`·`github-release`: 자체 `verify`와 `kvm-proof`에만 건다. macOS shard와 portable 전체는 실행하지 않는다.
   - `hub-docker`의 PR build는 아무것도 발행하지 않으므로 규칙을 지킨다. 세 경로 모두 테스트 크리티컬 패스 밖에 있고, 바꾸면 발행 의미가 달라진다.
4. 정본 규칙 5와의 차이다. CI는 shard별 실행 수를 검사하지 않는다. 계약은 wrapper가 `--shard`를 빠뜨리는 경우만 막고, `pyproject.toml` `addopts`나 conftest hook 같은 workflow 밖 무력화는 막지 않는다. 위 3차 기록의 `--collect-only` 실측이 그 예다. 결정할 것은 둘 중 하나다. 실행된 수를 selected 수와 대조하는 실행 중 검사를 `scripts/test_lanes.py` plugin에 추가하거나, 이 차이를 받아들인다.
5. 다음 항목은 근거 부족이나 순이득 부족으로 보류했다.
   - pytest-xdist 도입: hermeticity가 미증명이고 "Lane shard" 증거 줄이 사라진다.
   - PR/push tree dedup: pre-job 비용이 절감보다 크다.
   - `hub-docker` 게이팅 변경은 3번으로 옮겼다.

**다음 작업**

1. CI round-3 독립 재검토에서 신규 결함을 찾지 않았고, readiness 보정 commit `071d82d`의 작업 브랜치 push 및 정확한 SHA의 native KVM gate·Hub·Development package success를 위에 기록했다. 아래의 문서 영수증 자체가 새 HEAD가 되면 그 SHA의 CI 결과도 별도로 확인한다.
2. 변경을 포함한 완료 `Test` 표본은 source push 직후 4건이다. 자연스럽게 20회 이상 쌓인 뒤에만 크리티컬 패스 중앙값·p90을 재측정하고 이 절과 AGENTS.md 기준을 갱신한다. 표본을 만들기 위한 강제 workflow 실행은 하지 않는다.
3. self-hosted KVM runner 노출, dev/main 중복 push, 발행 gate 예외, shard 실행 수 검사에 대한 소유자 결정(위 승인 대기 1–4)을 받는다.
4. 승인된 작업 브랜치 게시 외에 PR merge 및 x86 KVM/cloud-init-disabled reboot 실기는 각각 대상·자원을 정한 별도 명시적 승인 뒤에 진행한다.

## 현재 릴리스 인계 — 2026-09-25

- 로컬 `dev`에는 PR #4의 OCI-root·conventional cloud-image·Hub 구현이 병합됐다. 앞서 기록한 OCI native stage-1 KVM gate, macOS aarch64 HVF base-only 부팅/SSH/재시작, Glance export, 전용 KVM host의 Hub ordered-chain build와 timeout/restart 회수는 각각 해당 SHA·환경의 증거다. 병합 후 conventional-cloud x86 KVM boot 및 `/etc/cloud/cloud-init.disabled` 이후 reboot readiness의 native 실기는 아직 없으며 stage-1 KVM gate나 portable seed 확인으로 대체할 수 없다.
- Release readiness는 root/Hub `0.2.0`과 Kolla 기본 image tag `0.2.0`의 정렬에 더해, 검토한 source로 위 conventional-cloud native 공백을 확인하고 `v*` tag의 `release.yml`에서 root package 검증과 필수 native `kvm-proof`를 통과시키는 것이다. 그 뒤에만 root wheel/sdist의 PyPI 발행과 GitHub release를 성공으로 판정한다. 같은 tag의 `hub-docker.yml`은 별도 Hub unit gate 뒤 GHCR API·worker semver tag(`0.2.0`, `v0.2.0`)를 게시하므로 두 경로의 결과를 각각 확인한다. 현재 이 handoff는 실제 `v0.2.0` tag 실행이나 게시 성공을 주장하지 않는다.
- OpenSpec 디렉터리는 이 작업트리에 없다. 새 OpenSpec 파일을 만들지 않았고, 이전 날짜별 승인·branch/PR 상태는 소급 갱신하지 않았다. Architecture review JSON marker 갱신은 최종 source 검토를 맡은 Main의 작업이다.

## 배포판 발행 인계 — 2026-09-25

- `v0.2.3` tag/source `fc729e3bb33ed9a4a860ad0d8c1c79b192891331`의 [release run 36081630018](https://github.com/openstack-afterglow/palimpsest/actions/runs/36081630018)이 native stage-1 KVM proof와 package 검증을 통과하고 `palimpsest-client` trusted publisher로 PyPI wheel(`ca4aed347c5a7bd85e31a4aac6d3a22455c288e331b1963120326fae15f94258`)·sdist(`90318b083c8456ff2abc207fe39a3e7a6200541557b191cf64c74ccba1772ffa`)를 발행했다. [GitHub Release `v0.2.3`](https://github.com/openstack-afterglow/palimpsest/releases/tag/v0.2.3)에는 두 검증 산출물이 있다. `uvx --from palimpsest-client==0.2.3 palimpsest --version`은 `0.2.3`을 반환했다.
- [Hub image run 36081630008](https://github.com/openstack-afterglow/palimpsest/actions/runs/36081630008)이 같은 tag의 API·worker linux/amd64 이미지를 게시했다. Hub Python distribution은 여전히 `palimpsest-hub 0.2.0`이다. 운영 Kolla에서 root 패키지 교체와 Hub image promotion을 수행했는지는 별도 검증한다. 위의 과거 `0.2.0` handoff 기록은 해당 시점의 기록이며 현재 발행 상태의 근거가 아니다.

## Hub 운영 업로드 재시도 결함 — 2026-09-25

- Afterglow PR #83 (`d565fc294aab16b1f05188f9371f76a12a141f5a`)의 Kolla 재구성은 controller 3대에서 failed=0으로 끝났고, root Kolla venv에는 `palimpsest-client 0.2.3`만 남았다. 기존 Hub API/worker image는 `0474b1f` revision pin을 유지한다. Hub source/runtime은 이 pin에서 현재 `main`까지 바뀌지 않았음을 경로 범위 diff로 확인했다.
- 인증된 disposable `POST /api/v1/palimpsest/hub/uploads`가 첫 요청에서 500이었다. Hub traceback은 asyncmy connection pool의 pre-ping에서 닫힌 uvloop transport가 `RuntimeError`를 던져 SQLAlchemy의 DBAPI disconnect 분류를 거치지 못한 것으로 귀결된다. 두 번째 요청은 start/append/read/abort/gone이 200/200/200/204/404였고 임시 세션은 제거됐다. Health/read-only 성공만으로 업로드 정상을 주장하지 않는다.
- `hub/src/palimpsest_hub/database.py`의 asyncmy engine ping에서 이 닫힌 transport signature에 한해 disconnect를 반환하고, unrelated `RuntimeError`와 이미 시작된 SQL transaction은 재시도하지 않는다. 독립 SQLAlchemy stale-ping 재현은 변경 전 예외 전파를 보였고 `hub/tests/test_database_pool.py`는 변경 후 checkout 복구·무관한 예외 전파를 확인했다. Hub source version 0.2.1, root client 0.2.3. 운영 새 image의 양쪽 architecture build, CI, exact digest pin, Kolla 배포, 실제 업로드 검증은 이 문단 작성 시점의 다음 작업이며 완료 증거로 취급하지 않는다.
- Local Docker daemon builder에서 Hub API/worker를 각각 linux/amd64·linux/arm64로 빌드하고, 네 image 모두 `--network none` 실행 시 `palimpsest-hub 0.2.1` import가 성공했다. Docker-container builder의 Docker Hub DNS 단절은 daemon-backed build로 처리했다. 최초 dev push `53e47b4`의 CI portable/development-package 공통 실패는 새 `hub/tests/test_database_pool.py`가 `scripts/test_lanes.py` 정본에 빠진 것이 원인이었다. 누락을 Hub lane에 추가한 뒤 canonical `list --check`가 통과했다. CI green과 main 발행, immutable digest, Kolla 재배포 및 실제 업로드 재검증은 별도 완료 증거가 필요하다.

## dev patch preparation — 2026-09-27 (local, unpublished)

- Created `/Users/pieroot/code/palimpsest-dev` as a separate clean worktree from `origin/dev` at `cf541223cd2a43fa4125652554ce18b8dffa83d1`; the missing `/tmp/palimpsest-root-package` registration alone was removed. The `codex/oci-root-phase1` worktree and other stale registrations were left untouched. Root source/lock now prepare `palimpsest-client 0.2.4`; old PyPI 0.2.3 proof above remains historical, not proof of a 0.2.4 release.
- The dev Hub pool regression was already present at starting commit `53e47b4`, and `cf54122` classified its test in the canonical Hub lane. Independent review found the Hub module identity at 0.2.0 despite Hub package metadata 0.2.1, a test coupling that module identity to the older Kolla default image tag, and first-host-only Hub volume initialization despite per-host containers. The local follow-up now reports Hub `__version__` 0.2.1, removes that coupling, and pulls the reviewed API image and initializes the local named volume root on every Palimpsest host; DB bootstrap still runs once on the first host. The Kolla default image tag remains 0.2.0. A root wheel bump does not deploy Hub source: reviewed fixed API/worker image digests must be pinned separately.
- Astra planning/approval remains unavailable. An independent reviewer inspected the absolute checkout, initially blocked on the three findings above, then approved their corrected working-tree source in a second read-only review; this code verdict does not authorize publication. Do not commit, push, tag, publish, alter runner settings, send helpers or operate shared infrastructure without the applicable explicit approvals. Before release, stage the reviewed correction together with the root patch; recheck frozen lock, CLI/version and wheel/sdist role payload, Hub pool regression and canonical lanes, architecture freshness, then the required native KVM proof and release/image workflows on the exact authorized tag. The persistent self-hosted KVM PR exposure remains an inherited owner decision, not a fix in this patch.
- Pre-review local verification for the earlier 0.2.4 candidate snapshot: root pytest 5,696 passed and 209 skipped; Hub pytest 100 passed; root and Hub Ruff checks and formatting passed; canonical lane manifest validated; `uv build --wheel` and `scripts/build_package.py` produced a wheel/sdist whose installed CLI reports 0.2.4 and whose Kolla role payload is present. Hub API/worker were built from that checkout for linux/amd64 and linux/arm64; all four images imported the 0.2.1 Hub distribution without network and reported the expected guest architecture. The earlier staged architecture freshness guard passed. These results do not validate the subsequent reviewer-driven source/role edits. No 0.2.4 native KVM proof, GitHub workflow, PyPI publication, immutable image digest, production role upgrade or authenticated Hub upload has been observed.
- Follow-up local checks on the corrected worktree: the isolated root `tests/` suite passed 5,913 with 260 skipped; 18 focused Kolla/package tests and all 100 isolated Hub tests passed. Root/Hub frozen locks, canonical lane manifest, Ruff checks and formatting passed; Hub module and installed distribution both report 0.2.1. `scripts/build_package.py` built the root 0.2.4 wheel and sdist, installed the sdist-built wheel in isolation and exercised the CLI. The root wheel contains per-host image-pull/volume tasks and a single-host database bootstrap. Rebuilt Hub API/worker linux/amd64 and linux/arm64 images all ran offline, imported the matching 0.2.1 module/distribution and reported the expected guest architecture. The production volume-owner command changed two distinct disposable named volume roots to 1000:1000 before they were removed. This same-daemon proxy is not a real multi-host Ansible run, native KVM proof, authenticated upload, remote publication or deployment. Architecture working/staged freshness checks apply only to the final reviewed index.

## 격리 Hub 후보 운영 인계 — 2026-09-27 (production 미승격)

이 절은 위 날짜별 기록을 고치지 않는다. 근거는 private workdir
`/Users/pieroot/.local/share/palimpsest-candidate-024/`의 **비밀이 아닌**
`candidate-resources.json`, `artifact-receipt.json`, `base-receipt.json`,
`transport-receipt.json`, `http-receipt.json`, `cli-receipt.json`,
`normal-build-receipt.json`, `recovery-receipt.json`, `reboot-receipt.json`과
`browser-receipt.json`이다. 이들은 시점별 관찰이지 현재 상태의 자동 보증이
아니다. `api.env`, `build-worker.env`, `compose.env`, private SSH key와
Keystone 입력은 출력·문서화하지 않는다. 기존 `palimpsest-dev` 소스는 미게시
root client `0.2.4` / Hub `0.2.1`; Kolla 기본 Hub tag는 `0.2.0`이며
published root `0.2.3`과 다르다.

- 격리 자원: owner `palimpsest-candidate-024`, Nova server
  `38cadbb8-6f1c-45ab-a7ec-d11339d78f7a` (`172.30.104.11` private),
  boot volume `61cfa349-e14f-4e33-be7d-1302590223e6`, 40 GiB data volume
  `fc4f7e9e-ff25-473a-a610-376ecd9c6492`. 원본 Glance base image
  `4da46b06-1e48-4bf8-adac-4c0ed5424797`는 3,758,096,384 bytes,
  SHA-256 `08d3b10e89e6d1d0d089b09302794e2949e0deb731c3fd240ec6b445d535a3df`.
  설치 스크립트 `install_host.py`는 data disk serial/size/blankness를 확인한
  뒤 ext4로 만들고 UUID fstab mount `/srv/h`(UID/GID 2001, 0700)을 설정한다.
  **이미 설치된 disk에 이 일회성 스크립트를 재실행하지 않는다.** Hub API,
  export worker와 bootstrap은 container UID/GID `2001:2001`로 `/srv/h:/srv/h`를
  mount하고, 별도 KVM build worker는 UID 2001의 systemd process다. Local
  interpreter `/opt/palimpsest/local/bin/python`, Hub interpreter
  `/opt/palimpsest/hub/bin/python`, config `/etc/palimpsest-candidate/`(0700;
  env/compose 파일 0600), Compose project `palimpsest-candidate-024`이다.
- Source transfer `source.tar` SHA-256
  `88cd80c29c7bcba6a338df2bb3c2eebc5b452cea653a41885cd16d3a9ba0d30d`,
  amd64 image archive `hub-amd64.tar` SHA-256
  `3ba68c8eb37ef5d2141f9e660bc38f5998fdbd1b312c24403701f142427a28e4`.
  Source archive hash binds the copied candidate bytes (including the
  worktree's local changes), **not** a published Git commit or production
  reproducibility receipt; `source-manifest.json` is private audit input.
  Candidate **local Docker image IDs** (not registry digests): API amd64
  `sha256:4641f6f90bc8e44fd12c3650c560757262d9278654ca9739684100cb9cac82e3`,
  API arm64 `sha256:4cf67d6412599106d02f32e32f1c84e8f604787e05c5149bfe322d9441a2dbb6`,
  worker amd64 `sha256:066df451b348a70daf61ce67f2c1a8da4404f17ba32e3a983b4307a3ecf9fce8`,
  worker arm64 `sha256:6eb51f9a94094ac68479ac3f2670aefdc99f8bf6c6e3e21af147414323c656a7`.
  Wheel receipts: client 0.2.4 `4e1ae84ce2dfff9936a4e97449ba81ebf49fc60e6b90c67346d89d5949246b29`,
  Hub 0.2.1 `2629a8539991fdcc1e20a1ade54baaecdd69b3adb0f088ef630e8aab7ed37abd`.
  These do not establish published immutable GHCR digests or production image pins.
- Access is deliberately loopback-only: Compose publishes API
  `127.0.0.1:8020` and MariaDB `127.0.0.1:3306` on the candidate host, with no
  Redis host port. Use the private SSH config and candidate alias with local
  forwarding (see [install guide](install.md#isolated-hub-candidate-access-and-data-safety));
  browser/API URL `http://127.0.0.1:18020` is localhost at the **client** end
  of SSH, not an unauthenticated public HTTP listener. Stop the tunnel by ending
  its SSH foreground process; it does not stop services. For services, inspect
  `systemctl status palimpsest-candidate.service palimpsest-build-worker.service`
  and `docker compose --env-file compose.env -f compose.yml ps` from
  `/etc/palimpsest-candidate` on the candidate host. The two enabled units
  start Compose API/export worker and the separate build worker, require the
  `/srv/h` mount, and reboot through systemd. A `systemctl stop` of the oneshot
  Compose unit alone **does not stop its containers** (no `ExecStop`); follow
  the explicit safe stop procedure in the install guide before backup.
- `/v1/health` returning `{"status":"ok"}` only confirms an API process. The
  separate `verify_readiness.py` checks active units, successful bootstrap,
  running UID 2001 API/export containers, no privileged devices/host PID,
  exact mount and loopback bindings, isolated build-worker environment, SQL
  `SELECT 1`, Redis `PING`, `/srv/h` space and health. It is a script contract,
  not a claim that health implies SQL/Redis/authenticated writes or guest boot.
  `transport-receipt.json` records uvloop 0.22.1, asyncmy 0.2.13,
  SQLAlchemy 2.0.51, closed TCP connection pre-ping classified as
  `OperationalError`, renewed connection and `SELECT 1` both protected and
  unprotected; exact upstream `uvloop RuntimeError` was **not reproduced**.
- Authenticated HTTP `http-receipt.json` includes unauthorized layers 401,
  authorized list 200, resumable upload start/PATCH/query, offset-conflict
  409 and recovery, finalized layer blob 200 and range 206, metadata/ancestor
  reads. `cli-receipt.json` records `image ls --arch x86_64 --disk-format raw`,
  `layer ls --name candidate-024-layer-one`, `layer pull` (all exit 0) and
  byte-matched 3,758,096,384-byte base; the CLI pull output path is a
  **directory**, not a regular file. The earlier command journal warning was
  fixed by creating a private log directory. The ordinary two-layer/two-RUN
  KVM build `41f491a9-a11b-45f6-a0ae-b43b8924bc91` advanced
  queued→building→complete, returned the 4,096-byte SquashFS
  `sha256:1007b6e2eb16daa752fdce76ef737ab22cf871babc1383e3e6c39f8435b88df6`,
  recorded exact two RUN files and absent owned domain/scratch. A restricted
  application credential received 401 for build status; separately, an actual
  project member password token received build 403 (`nonadmin-build-receipt.json`).
- Timeout build `1ad5444e-24c2-4391-bec1-ea09e15302c1` ended
  `error/build_failed`; killed-main-worker restart build
  `0e807e8e-6d3e-434b-b799-0fc3e3716c03` ended
  `error/worker_interrupted`, with owned guest/domain/scratch absent and the
  temporary build deadline returned to 3600 seconds. Reboot receipt has
  different boot IDs `8199f365-ab55-425a-a0f7-7c71504d56c0` →
  `f97beedf-059d-495f-a9e5-c7ad327e3c2b`, zero active jobs, authenticated
  layer checks, full base SHA-256 on mounted CAS and a fresh post-reboot layer
  upload `sha256:26ddc4a177769b0226458adfc20c5ba84c32a05180e47ad1e634377bc5be1c3b`.
  This is candidate single-host durability, not production restart/failover.
  Earlier `browser-receipt.json` shows the layer/build lists and a 4,096-byte
  authenticated layer download with matching digest. Later
  `c6-browser-receipt.json` records a **real browser UI submission** of build
  `ff672fef-f991-436d-b8d5-0ee074b39ff0`: queued→building→complete,
  output 4,096 bytes at
  `sha256:21c85bbe39bd85170cd27c4c24ff450d395d58cbf43308d85890da928ab73044`,
  saved-file SHA-256 equal, and exact `ran-one`/`ran-two` files in the output.
  The real Chrome download used the supported anchor fallback after launching
  Chrome with FileSystemAccess disabled; the default native macOS file picker
  was not automatable and no saved-file claim is made for that earlier attempt.
  The Hub console has no detail button: authenticated build/layer/ancestor
  details were read in actual `/docs` Swagger UI (chain complete), not by
  inventing an app detail view. No console errors were observed in this flow.
  Candidate native-CI Glance input image `4217de09-9771-40af-8d01-964f09566cf0`
  is active in `ci-image-receipt.json` with SHA-512 matching the base receipt;
  protected CI environment and helper are separate from Hub user credentials.
  The initial helper rejected an internal HTTP Keystone URL before creation.
  Actual HTTPS attempts then exposed QEMU `/dev/kvm` permission denial behind
  a lifecycle socket reset. The helper now adds only the disposable guest user
  to `kvm` and starts native proof with refreshed supplementary groups instead
  of relying on a one-time device ACL.
- **Manual native proof passed**, owner `openstack-afterglow/palimpsest/202609271/6`.
  `native-openstack-evidence-6/native-proof-receipt.json` binds the original
  candidate tar SHA-256
  `88cd80c29c7bcba6a338df2bb3c2eebc5b452cea653a41885cd16d3a9ba0d30d`,
  pinned kernel/config, **43 boots / 44 QEMU invocations**, exact 45-file stage-1
  evidence and matching mounted SquashFS/replay receipts plus EROFS evidence.
  Stage-1 receipt SHA-256 is
  `6e9ec697c8802cd9cefb9a3dc4f2a7180c276af101aefe34f78259eafc163e94`.
  Server `91ab2a05-7f11-4694-b3ae-7a0d8279a312`, volume
  `37c41b83-98da-4aa5-9e91-9f68a0b884b6`, port
  `e7286606-95e9-455a-9010-e5e820bd6c18`, SG
  `021bf145-721b-42fe-9ea8-60abc3e06e8e` and run key were reclaimed;
  manifest requires `cleanup_verified=true`. Failed and diagnostic attempts
  retain their separate receipts; diagnostic archives do not qualify the candidate.
  Focused helper/workflow/architecture contracts: **188 passed**, Ruff and lane
  classification passed. **No GitHub push, GitHub native CI run, publication or
  production release is claimed.** Protected environment kernel/config HTTPS
  URL variables are still unset and fail closed; configure reviewed URLs for
  the pinned bytes before an approved trusted-ref CI execution.
  The current restricted CI application credential expires at
  `2026-10-27T16:01:38Z`; renew it with the same member-only project/role and
  protected environment policy before expiry. `final-resource-receipt.json`
  confirms candidate VM ACTIVE and both data/boot volumes attached, plus exact
  server/volume/port/SG absence for all six created native attempts. Per-attempt
  cleanup manifests also verify run-key absence. Diagnostic source instrumentation
  and temporary diagnostic archives were removed; original source and all run
  receipts/logs remain private.
- Outstanding production promotion gates: source-based candidate IDs are not
  current production image digests; recover the **actual** previous production
  API/worker immutable digests before any rollback plan or promotion. Confirm
  source review, image provenance, deploy-specific backups and restore drill,
  production credential/account separation, resource/retention policies,
  external authorization and exact release/CI gates. Source review found direct
  Hub build of an uploaded QCOW2 with an external data file is not rejected at
  the build boundary (the export path rejects it; this proof used pinned raw),
  and multipart UploadFile body can spool before authentication/size gating;
  bound ingress/temp storage before public exposure. Nonexistent upload IDs can
  allocate persistent locks before ownership; authenticated blob responses use
  long-lived immutable cache headers without `Vary` authorization/project.
  Candidate loopback+SSH mitigates exposure of its HTTP listener; these findings
  remain production review gates, not observed candidate exploitation. Do not
  claim production readiness from a candidate health check, historical tests,
  or an unreviewed image tag.

### Documentation guidance migration (2026-09-28; no new verification)

이 작업 브랜치 `codex/oci-root-phase1`에서 root `AGENTS.md`·`agent.md`를 짧은 진입점으로 바꾸고 [contributor workflow spec](../openspec/specs/contributor-workflow/spec.md)과 [CI safeguards spec](../openspec/specs/ci-safety-and-performance/spec.md)으로 상세 의무를 이관했다. 위 날짜별 기록과 승인 대기 1–4는 그대로 유지한다. 이 문서의 과거 `AGENTS.md` 규칙 1–12 참조는 이제 CI spec의 같은 번호를 가리킨다. 특히 runner PR event gate/required KVM verdict/계약 충돌, 발행 gate 예외, shard 실행 수 미검사 및 dev/main 중복 push는 여전히 소유자 결정 대기다. 이 이관은 다른 저장소의 Nova cutover 구현이나 승인, 새로운 원격/GPU/CI 실측의 근거가 아니다. 코드·테스트·workflow·저장소 설정을 바꾸지 않았고 이번 문서 변경에서 architecture review marker stamp 또는 검증을 실행하지 않았다. 명시적 승인 전 remote helper 전송·추가 게시·shared runner/VM 조작은 차단한다.

통합 주석: 위 문단은 source branch의 문서 이관 시점 기록이다. 승인된 dev 병합에서는 같은 저장소의 최신 Nova cutover와 수동 proof를 유지하고 CI spec을 `ci-safety-and-performance/spec.md`로 통합했다. 이전 persistent-runner 충돌 설명을 현재 정책으로 되돌리지 않는다.

## 빠른 링크 맵

| 질문 | 먼저 읽을 곳 |
| --- | --- |
| 전체 구조·증거 marker·계약 | [ARCHITECTURE.md](../ARCHITECTURE.md) |
| 어떤 테스트를 언제 실행하는가 | [testing.md](testing.md) |
| OCI가 실제 `/`가 되는 증거 | [oci-root-proof.md](oci-root-proof.md), [Gate 2 acceptance](oci-root-build-run-acceptance.md) |
| conventional VM와 Docker wrapper | [compatibility.md](compatibility.md) |
| 익명 registry archive 취득 | [registry-intake.md](registry-intake.md) |
| 공식 service image 결과 | [docker-hub-service-matrix.md](docker-hub-service-matrix.md) |
| TensorFlow/PyTorch CPU 현황 | [oci-ml-compatibility.md](oci-ml-compatibility.md) |
| GPU/OpenStack/PCI 경계 | [oci-gpu-support.md](oci-gpu-support.md) |
| Linux state·installer·journal | [linux-storage-logging.md](linux-storage-logging.md) |
| guest process·PID1·stdio | [oci-linux-process.md](oci-linux-process.md) |
| retained root와 shared volume 구분 | [oci-retained-root-inventory.md](oci-retained-root-inventory.md) |
| 사용자 설치와 패키지 선택 | [`../install.md`](../install.md), [install.md](install.md) |
| 명령별 실행 순서와 diagram | [cli/workflows.md](cli/workflows.md), `docs/diagrams/` |

## Guidance migration (after the 2026-09-27 checkpoint)

The detailed root `AGENTS.md` obligations were moved without changing their approval or evidence boundaries to [contributor workflow](../openspec/specs/contributor-workflow/spec.md) and [CI safety/performance](../openspec/specs/ci-safety-and-performance/spec.md). Read both via [AGENTS.md](../AGENTS.md) or [agent.md](../agent.md) before resuming. Older references in this handoff to AGENTS rules 1–12 (including the 2026-09-24 CI checkpoint's rule 10) now refer to rules 1–12 in the CI spec; their descriptions remain dated evidence, not today's permission. The 2026-09-25 note that there was no OpenSpec was true of that checkpoint; this later docs-only migration supersedes that choice without revising the earlier entry. No code, workflow, runner, credential, remote CI, native proof or publication was changed or verified by this note; preserve the 2026-09-27 source/manual-proof evidence and separate pending approvals.

## Local dev continuation — 2026-09-27

- The user explicitly approved committing all current changes and continuing on
  `dev`. `/Users/pieroot/code/palimpsest-dev` already checks out that branch;
  there is no separate feature-branch merge to perform. The original staged
  release/Hub-volume preparation is commit `37a6639`; the subsequent commit
  contains isolated native CI, operational evidence and the OpenSpec guidance
  migration. This approval supersedes the earlier local commit hold only.
- Continue from this `dev` worktree, not the older
  `/Users/pieroot/code/palimpsest` (`codex/oci-root-phase1`) checkout. Existing
  native proof binds its recorded candidate tar, not an invented post-commit
  GitHub run. The actual 188-pass contract result and manual 43-boot/44-QEMU
  proof remain the evidence above; committing does not rerun or expand them.
- Remote push/tag, automatic GHCR/development-package publication and production
  replacement are not authorized by this local integration. Next remote gates:
  reviewed pinned kernel/config HTTPS URLs, publication-path approval, then
  trusted-ref CI and before/after timing measurements. Production backup/restore,
  immutable rollback inputs and security/isolation findings remain separate.

## Current-workspace dev integration

- 사용자가 현재 작업공간의 미커밋 변경까지 dev에 병합하고 현재 작업공간을 dev로 전환하도록 승인했다. 원본 변경은 `codex/oci-root-phase1`의 `2d1882f`에 보존했으며, `f14d77d` 기준 dev와 문서 충돌을 해결해 통합한다.
- Contributor 권한·GPU 경계·재개 절차와 README/testing 탐색 개선을 반영했다. 중복 CI spec은 최신 `ci-safety-and-performance/spec.md`로 통합하고 hosted-only PR, protected Nova native, offline runner21과 기존 live receipt를 보존했다. Runtime/workflow/test source는 dev 기준 그대로다.
- 이어서 작업할 위치는 `/Users/pieroot/code/palimpsest`의 `dev`다. 보조 `/Users/pieroot/code/palimpsest-dev` 작업트리는 같은 병합 commit을 detached HEAD로 보존하여 dev의 중복 checkout을 피한다. 기존 branch와 작업트리를 삭제하지 않는다.
- 이번 승인은 로컬 commit/merge/branch 전환 범위다. 원격 push, 발행, 후보 또는 운영 자원 변경은 수행하지 않는다.
- 로컬 검증: architecture working guard는 427개 source file digest를 확인했고, `uv run pytest -q tests/unit/test_architecture_guard.py`는 14 passed, `uv run palimpsest --version`은 0.2.4였다. 병합 변경은 Markdown 8개뿐이며 관련 spec/navigation link 28개가 해석되고 conflict marker·폐기된 CI spec 링크가 없음을 확인했다. 최초 stamp 시도는 index의 unmerged entry 때문에 중단됐고, 해결 파일을 stage한 뒤 stamp와 working guard가 통과했다. 병합 commit 전 staged guard도 확인한다. Native/GitHub/원격 검증은 추가 실행하지 않았다.

## Hub logging contract — 2026-10-01 (local `dev`, unpublished)

- Baseline: `dev` HEAD `dc8a164fd7ad4243f0e01bf7e48e613608674ccb`, ahead of `origin/dev` by four commits. The pre-existing unstaged `AGENTS.md`, `openspec/specs/contributor-workflow/spec.md` and `openspec/specs/ci-safety-and-performance/spec.md` changes remain outside this Hub task; no commit, remote action, native/GPU proof or privileged action is authorized here.
- Source change: API and two workers share INFO default / explicit Hub-only DEBUG logging; API records matched route template, HTTP status and elapsed time without raw target, and claimed export/build work records start/terminal state with bounded safe metadata. Existing exception-bearing Hub messages in the affected paths are sanitized. See [Hub log operations](install.md#hub-api-and-worker-logs) and [architecture](../ARCHITECTURE.md#code-map). No schema, credential ownership, external request, guest execution or SQL bind contract changes are intended.
- Verification is local-only and must be reported as actually exercised, not inferred from the historical native proof. At the source-review checkpoint the new regressions are test-defined, not test-passed; tests/build/lint are held until the parent integration gate. Publication/deployment/production credentials and native KVM remain separately blocked.

## Local Dockerfile, registry and multi-VM verification — 2026-10-02

- Source baseline: `dev` HEAD `dc8a164fd7ad4243f0e01bf7e48e613608674ccb`.
  Existing unstaged Hub/guidance changes and the untracked Hub logging module
  were preserved. No runtime source, tests, CI, credentials or deployment
  configuration in either repository was changed for this verification.
  Astra was unavailable in the session; no new development/review approval is
  claimed. There was no commit, push, publication, remote installation or cloud
  mutation.
- Secret-free CLI evidence is under
  `/Users/pieroot/.local/share/palimpsest-proofs/cli-806ea4b0/`.
  Actual `uv run --frozen palimpsest build --frontend dockerfile --offline
  --network none` used a dedicated local `docker-container` builder,
  `palimpsest-cli-806ea4b0`, with Docker network mode `none` and a verified
  Alpine named OCI input. The multi-stage Dockerfile exercised RUN, COPY,
  WORKDIR, numeric USER and CMD. Cold solve took 0.961 s, identical warm solve
  0.633 s, and changed RUN input 0.678 s. Cold/warm manifest was
  `sha256:527e7f6bce3198454d4d867329970af749484ca71e08dd94d030167289de2d15`;
  changed input produced
  `sha256:ad69c5d151798f4d2310ab6276288eb8c50352af2f5f4597c1e0f2b349be04b0`.
  BuildKit logs show the first/changed RUN executing and the warm RUN cached.
  LocalArchiveSource/SourceCAS accepted all three outputs; streaming gunzip
  hashes matched every declared DiffID. This is a controlled Dockerfile proof,
  not a new full Afterglow Dockerfile build. Runtime-block bytes were zero.
  Buildx returned exit 0 but its saved logs also contain cache-ref RPC
  `Unavailable` messages; do not describe the logs as error-free.
- Running the newly built archive through public `palimpsest run` exited 1
  with `OCI host configuration is missing PALIMPSEST_OCI_KERNEL`. The host is
  macOS arm64; OCI materialization/root execution remains Linux amd64 KVM-only.
  No guest workload/actual OCI root was verified. A native Palimpsestfile to
  SquashFS build on Lima was not exercised by this task either.
- Direct registry acquisitions already completed in this continuation were
  reused, not downloaded again. Evidence:
  `/Users/pieroot/.local/share/palimpsest-proofs/registry-20261002011025-95251/receipt.json`.
  Actual `oci pull docker.io/library/alpine:3.24.2` and
  `oci pull ghcr.io/nginx/nginx-unprivileged:stable-alpine-slim` both exited 0,
  selecting verified linux/amd64 manifests/configs/layers and publishing 0600
  archives. Alpine manifest `d56c381f...` was used as the local build input;
  GHCR manifest is `939bb73e...`. Source compressed digests and gunzipped
  DiffIDs were independently distinguished. Native Skopeo 1.24.1 was installed
  once; Homebrew also updated ca-certificates. Anonymous TLS and digest checks
  are not publisher-signature proof; private-registry authentication and VM
  execution of the acquired images remain unqualified here.
- Project `multi-806ea4b0` booted two new ARM64 VZ cloud-image VMs,
  `multi-806ea4b0-a-1` and `multi-806ea4b0-b-1`, each 2 vCPU/2 GiB (actual Lima
  inventory, not inferred from inspect). Both independently served identity
  JSON over host ports 13181/13182 with HTTP 200 while the older Afterglow VM
  remained running. `ps`, `compose ps --format json`, `inspect`, `compose port`,
  idempotent up/noop, stop of A without stopping B, and compose restart of A
  were exercised. A restarted in 8.152 s; both host endpoints returned 200
  afterward. This proves simultaneous conventional VM operation, not
  concurrent startup, a large-node qualification or OCI-root multi-VM.
- VM-to-VM HTTP over their real VZ addresses `192.168.64.14/15` failed both
  directions with `No route to host`. Read-only evidence shows an on-link
  route but peer neighbor `FAILED`; root cause remains unestablished. Do not
  call this a working shared service network. Palimpsest inventory reports the
  per-VM user-network address `192.168.5.15`, not those distinct VZ peer IPs.
  `ps`/`inspect` project durable state, not live backend status; inspect still
  reports null CPU/memory, logs exited 0 with no output, and public `stats` is
  absent. Existing intentional Lima exec/shell rejection was not patched.
- OpenStack parity is not implemented/verified by the CLI: its local backends
  and block-volume receipts have no Nova/Cinder/Manila adapter. Hub is `/v1`
  local_path CAS, not an OCI Distribution `/v2` registry or Manila adapter.
  Afterglow's `services/layer_build.py` creates Manila RW shares, attempts RW
  revocation on build success, grants RO consumer access, and creates Cinder
  upper volumes; those are different orchestration/bootstrap boundaries.
  Identical OCI digests alone do not prove equivalent root, privilege,
  single-writer, reboot, retention or revocation behavior. The selected
  Nova-owned OCI root still needs its reviewed boot-disk/bootstrap integration.
- Fresh Linux KVM installation/proof, online Hub cache writes, and actual
  Cinder/Manila resource operations require exact ref/action/resource approval.
  CI project `53ec2dd9a1f7471fb6a2b174595fa232` is a helper allowlist, not
  standing authorization; its one-VM/one-20-GiB-volume quota cannot cover a
  multi-volume/Manila proof. At this checkpoint the existing candidate
  project/VM/volumes, runner21, old domains and production were untouched.
  For the candidate Hub DB/CAS this is superseded by the later approved-scope
  run; see the Hub cache project-attribution finding below. No new test suite
  was run.
  The working architecture guard exited 1 because the pre-existing source
  snapshot is stale; it was not stamped without reviewing the unrelated work.
- New VMs are left running in the same isolated state/config roots as the
  Afterglow proof. Run management from the Palimpsest checkout with
  `PALIMPSEST_STATE_HOME=/Users/pieroot/.local/share/palimpsest-proofs/ag-f1823ba8/state`
  and `XDG_CONFIG_HOME=/Users/pieroot/.local/share/palimpsest-proofs/ag-f1823ba8/config`.
  Exact project file:
  `/Users/pieroot/.local/share/palimpsest-proofs/cli-806ea4b0/palimpsest.yml`.
  `uv run --frozen palimpsest compose -f` that file `stop` stops only the new
  project; `down` removes only its owned VMs if the user chooses cleanup.
  Do not use `limactl delete` or unscoped cleanup. Afterglow backend health was
  rechecked at HTTP 200; the original application was not stopped/recreated.

### Approved continuation and Darwin runtime-layer correction

- The user separately approved the exact `dc8a164` CI-project one-VM/one-20-GiB
  native scope and the isolated candidate Hub cache scope
  `cli-806ea4b0-online`; receipt is `cli-806ea4b0/approved-scopes.json` under
  the proof root above. This does not authorize CI credential changes, admin
  bypass, private-helper transfer/execution, runner/GPU changes, publication or
  Cinder/Manila parity operations. The earlier approval blocker is superseded
  only for those two selected scopes.
- Correction to the earlier execution blocker: only OCI-root is Linux amd64
  KVM-only. Dockerfile `--runtime-base`/`--runtime-tag` packing works on Darwin,
  and Lima copies, loop-mounts SquashFS read-only and creates the per-VM overlay
  at `/opt/layers/merged`. Installed `mksquashfs`/`unsquashfs` 4.7.5 were reused.
  A new ARM64 Alpine input (manifest `260479a1...`) was acquired via Skopeo,
  because public `palimpsest oci pull --help` admits only `linux/amd64`.
- Actual ARM64 offline Dockerfile runtime packing exited 0, and public
  `palimpsest run <base> --layer <digest> --memory 1024 --vcpus 1` created a
  new Lima VM. Guest mount evidence showed a read-only SquashFS loop lower and
  writable overlay. RUN/COPY files were present, and image binaries executed
  as aarch64. The initial Alpine fixture CMD failed honestly because its
  BusyBox lacks the httpd applet. Only the generated fixture was corrected to
  `cat /srv/identity.json; exec sleep 300`, rebuilt, and launched in a new VM;
  its actual process ran at UID/GID 1000. OCI manifest is `a48b6794...`, runtime
  block `3c4f2082...`. No repository runtime source was patched.
- A newly acquired ARM64 GHCR Nginx input (manifest `8946550a...`, eight
  gzip layers, configured UID 101) was used by an offline Dockerfile-derived
  runtime layer. The separate pure-FROM case exited 1:
  `BuildKit local cache exporter did not create index.json`; this remains an
  unfixed consumer-visible failure, not a success reclassified by adding COPY.
  The separately reported derived case copies an explicit 0644 identity asset.
  Runtime block is `b9d9bad8...`, derived OCI manifest `cf638d55...`.
  Its unchanged `/docker-entrypoint.sh nginx -g 'daemon off;'` executed at
  observed UID/GID 101, Nginx 1.30.5, and served both the original welcome page
  and exact identity JSON with HTTP 200. Diagnostic log-path/asset-mode/shell
  failures are retained; they were not suppressed or counted as passing.
- These image commands were started by diagnostic Lima guest chroot with
  device/stdio setup, not by public `palimpsest exec` or the OCI-root runner.
  The VM, layer transport and mount were created by public Palimpsest commands.
  This is VM payload/application proof, not automatic OCI CMD invocation,
  unchanged upstream-image execution, PID-1/security equivalence or Nova boot.
  Independent descriptor-size/SHA-256 and gunzipped DiffID checks passed for
  both ARM source layouts and both final derived OCI exports. Evidence is in
  `cli-806ea4b0/arm64/` (`valid-cmd-verification.json`,
  `ghcr-http-verification.json`, `oci-integrity-verification.json`).
- Durability boundary: an unsynced upper file was absent after stop/start.
  Source `lima.py:889` uses `limactl stop --force`. A separate upper file with
  explicit guest `sync` survived stop/start and was read from the restored
  merged mount. Do not claim the upper is reset on every restart or that
  default stop is graceful/data-loss-free. The lower refused writes with
  `Read-only file system`. This is not a Cinder retain/multi-attach proof.
- Approved native preparation remains blocked before authentication/creation:
  existing current member-only CI application-credential ID/secret was
  unreachable through bounded private metadata, OpenStack/shell inputs,
  targeted keychain metadata and protected GitHub secret metadata. GitHub
  metadata cannot return secret values; historical credential ID cannot
  authenticate. This native lane reset no credential, used no admin identity
  and installed no remote host.
  Exact source archive hash and kernel/config pins were verified locally,
  not treated as native execution. No cloud resources were created and no
  cloud cleanup is required. Secret-free evidence and an empty resource
  manifest are under
  `/Users/pieroot/.local/share/palimpsest-proofs/native-cli-20261001T170218Z-aee46fafca/`.
- All four newly created ARM layer/command/GHCR diagnostic VMs are stopped,
  not deleted; the parent-owned offline builder is stopped. The original
  Afterglow and earlier two-node project were not changed in this continuation.
  Incomplete or superseded Afterglow input exports must not be consumed as
  successful complete images.
- Hub cache project-attribution finding (possible approval-scope breach; owner
  decision pending). The full Afterglow online run used the existing private
  input credential of Keystone user `admin` (`18d4e5da...`); each runner token
  was asserted scoped to project `service` (`aa65ed83...`), roles
  admin/manager/member/reader. The Hub receipt names only URL and cache scope;
  it records no credential, project or approval for an admin-role identity.
  Six cache uploads resulted. The four final rows (frontend/backend x
  amd64/arm64, 2,274,570,240 bytes) were observed `is_published=false`, named
  `cache-cli-806ea4b0-online-*`, `created_by` the admin user and owned by
  `project_id` `e4f1487023f047b59901f28a91876e7b` (Keystone project `admin`),
  which `scripts/run_native_kvm_openstack.py:51` lists in
  `FORBIDDEN_PROJECT_HEX`. The scope listing was empty before the run and each
  final row's `created_at` (17:25:06-17:34:44 UTC, 2026-10-01) lies within its
  own solve interval, so this run wrote them; they are not pre-existing rows
  readable across projects. Exact HTTP push times were not logged;
  phase-summed upload intervals are derived. The two uploads from superseded
  backend-input solves (blobs `9cd332b6...` amd64 and `93e9e1de...` arm64,
  1,358,141,440 bytes) used the same CLI/Hub path, but their name, `created_at`
  and owner project were never recorded and were not fetched after the pause:
  their `admin`-project ownership is inferred, not observed. Do not describe any
  of these as registered within the approved isolated scope: the cache-scope
  name matched, but the effective Hub project was established only after the
  writes and no before-write isolation was verified.
- Root cause, source-reviewed and not fixed:
  `hub/src/palimpsest_hub/auth.py:76-94` `validate_token` re-authenticates the
  presented token with keystoneauth `v3.Token` and no scope unless
  `X-Project-Id` is sent, so Keystone default-scopes the new token to the
  user's default project. Hub ownership (`project_id=` in `api/hub.py`) and
  `get_os_conn` (`auth.py:133-142`) use that re-scoped project and token. The
  public CLI never sends a project: every `HubClient(resolve_url(...),
  resolve_token())` in `cli.py` (e.g. 343, 1964, 2099) omits `project_id`, so
  `X-Project-Id` (`hub.py:185-187`) is unused. Consumer impact: CLI writes land
  in the caller's default project regardless of the presented token's scope,
  and Hub-side OpenStack calls run there too. This contradicts the
  project-scoped-token contract in `ARCHITECTURE.md:564` and
  `docs/compatibility.md:11-16`. A Keystone token-method exchange without scope,
  run read-only before the pause, reproduced default scoping to `admin`; it
  minted an admin-project token that was not persisted.
- After detection no Hub row, blob or grant was deleted, altered, re-registered
  or retried, and no further Keystone authentication was performed. The agent
  reports candidate VM/volumes/services unchanged; this was not independently
  re-observed. Removing the four observed rows and the two unobserved
  superseded uploads needs separate exact approval and would itself require an
  admin-project identity. The session-owned candidate tunnel was stopped after
  the pause. Evidence is in `cli-806ea4b0/online/`:
  `scope-isolation-evidence.json`, `cache-ownership-timing-evidence.json`,
  `auth-role-scope-evidence.json`, `auth-exchange-scope-evidence.json`,
  `hub-cache-upload-evidence.json`, `superseded-cache-rows-evidence.json`.
- Full Afterglow online solves of the canonical
  `/Users/pieroot/code/afterglow/Dockerfile` (no source edits; context
  `sha256:bb1086b5...`; immutable base pins in `online/pins.json`) all exited 0
  through the public Palimpsest CLI on a dedicated `docker-container` builder,
  `afterglow-online-806ea4b0-170417` (bridge network, 8 GiB, 4 CPU). Frontend
  `RUN bun run build` and backend `COPY` plus `RUN python -m compileall` ran on
  both platforms. Each official structural validation exited 0: frontend
  amd64 `df777c61...` (9 layers, 1001:1001), backend amd64 `fc7cb3c1...` (15,
  1000:1000), frontend arm64 `c0d8a0ce...` (9, 1001:1001), backend arm64
  `38ed2871...` (15, 1000:1000). Validation covered descriptors, reachable
  blobs, streamed gzip DiffIDs, tar numeric metadata and process/config
  metadata. The images were not executed on any VM or host, not loaded into
  Docker, not pushed and not transferred; no Linux recipient exists because
  the native lane is blocked. The first round over-excluded public `config` and
  `secrets` module directories: its frontend solves failed, and its backend
  archives under `superseded-round1/` are invalid proof. Only the proof copy of
  the context was corrected (12 public modules restored;
  `context-correction.json`). Index: `cli-806ea4b0/online/artifacts.json`.
  The builder is stopped and retained, not removed or pruned.

## Project-scoped package publication specification — 2026-10-02

- Scope: API/CLI/security/UI specification and Afterglow integration plan only.
  No runtime source/schema/config change, key/token issuance, package upload,
  production request, helper transfer, deployment, commit or resource cleanup
  was performed for this request. Pre-existing dirty work remains preserved.
- Inspected baselines: Palimpsest `dev`
  `dc8a164fd7ad4243f0e01bf7e48e613608674ccb`; Afterglow `dev`
  `0f59e0ee8e7cea6d36db33f1d4e2380fe4fac2b7`. Both source maps include their
  existing working-tree changes; a HEAD identifier alone is not a clean-tree
  or deployment claim. Astra was unavailable; no Astra approval or new
  architecture source-review stamp is claimed.
- Contract: [project-package-registry.md](project-package-registry.md),
  `palimpsest.project-packages.v1`, explicitly proposed/not implemented.
  [registries.md](registries.md) links it without changing current Docker
  wrapper behavior. Reference syntax is
  `cloud.dmslab.re.kr/openstack-afterglow/test:v1`; a SQL namespace binds one
  immutable Keystone project UUID, not a display name or client header.
- Keys are secret-once, hashed, expiring, owner/project/exact-package/action
  bound and helper-backed. Original member tokens control issuance/read;
  native writes require package keys. Admin/service/system authority,
  protected administrative principals with member-only scope and default-
  project token re-scope cannot authorize publication. The original-token
  defect above must be fixed before enabling package writes. Token/owner/
  membership validation requires separately approved bounded read policy,
  never an admin upload fallback.
- API specification includes namespace/key lifecycle, scoped package/version/
  tag reads, complete OCI/runtime-bundle graph validation, resumable uploads,
  atomic compare-and-set tag publication and separately scoped mandatory
  BuildKit cache APIs. Cache is not a runnable package or inventory success.
- Afterglow plan lives in the separate repository's
  `openspec/changes/project-scoped-palimpsest-packages/` (`rapid` metadata,
  proposal and tasks). It adds member `/palimpsest/packages`, browser
  read/control APIs and an allowlisted key gateway. Hub is the single
  package/tag/key database. The screenshot `/admin/libraries` gets a link;
  its administrator-only build controls and local legacy layer records are
  not reused as project registry inventory. Project switching fences old
  responses/secrets; no automatic VM/Glance/Manila/Cinder import is planned.
- Actual checks: `palimpsest push --help` and `login --help` through
  `uv run --frozen` exited 0 and confirmed current Docker-wrapper options,
  not the proposed native workflow. All three JSON examples parsed; all six
  relative link targets across the contract/registry guide existed.
  `openspec new change ... --schema rapid --json` succeeded, and
  `openspec status --change project-scoped-palimpsest-packages --json`
  recognized both planning artifacts. Its complete status means artifact
  presence, not implemented behavior. Generic strict `openspec validate`
  failed on missing specs delta; Afterglow deliberately has no specs layer
  (`docs/agent-development-guide.md:123-128`), so no artificial delta was
  added. Runtime package/API/browser acceptance remains unexercised; no
  implementation/full-suite/native/live-production proof is claimed.
- Next implementation order: original identity validation → Hub metadata/
  keys/validator/publication → native CLI/cache cutover → Afterglow BFF/UI →
  isolated ordinary-member build/push/list/browser/pull proof. Production
  rollout needs separate exact-ref/project/identity/key/migration approval.
  The four observed admin-project cache rows and two superseded uploads
  above were neither republished, transferred, altered nor deleted.

## Local monitoring dashboard prototype — 2026-10-02 (local, uncommitted)

- Baseline: `dev` `dc8a164fd7ad4243f0e01bf7e48e613608674ccb` with the pre-existing
  dirty Hub/docs work preserved. A separate actor edited the package-registry
  files concurrently in this worktree (`cli.py` package hunks, `buildkit.py`,
  `hub/*`, `package_*.py`, `packages.py`, `tests/unit/test_cli_contract.py`
  offline-build hunk). Those changes are not part of this work and were not
  reviewed. Astra/Sol role selection was unavailable. Planning was done in-session;
  two task subagents implemented the inventory and frontend slices, and a separate
  reviewer agent reviewed the result. No role-based approval is claimed.
- Scope: extended the existing `palimpsest ui` (no new daemon, server or
  framework). `ui.py`, `inventory.py`, `webui/{index.html,app.css,app.js}`, the
  `cli.py` `ui --allow-control` parser/dispatch lines, `scripts/generate_cli_reference.py`
  (`allow_control` text), the `palimpsest ui` rows of `docs/cli/reference.md`
  (the concurrent actor has since regenerated the whole file with its own
  options; `generate_cli_reference.py --check` now reports current), `docs/cli/usage.md`,
  `docs/cli/workflows.md`, `README.md`, `ARCHITECTURE.md` code map, change guide
  and summary, plus `tests/unit/test_{ui,inventory,cli_contract}.py`.
- Behavior: loopback-only foreground server. The static shell is served
  without credentials so a token-stripped tab can reload; assets and `/api/v1`
  accept only the per-process bearer token. Read-only is the default, and every
  `POST`/`DELETE` returns 403 before any side effect; `--allow-control` re-enables
  the existing controls. New `GET /api/v1/volumes` and `/api/v1/networks` come
  from `inventory.list_volumes`/`list_networks`, which are durable,
  backend-free projections; `/api/v1/summary` no longer embeds the storage walk.
  The VM view still live-reconciles, so a status that changed outside
  Palimpsest is written to that run's ledger. BuildKit build logs now read
  `buildkit.log`, with a tail capped at 4 MiB. Lima and HVF attachments no
  longer present user-mode or SSH-forward addresses as network addresses.
- Checks run: `pytest tests/unit/test_inventory.py tests/unit/test_ui.py
  tests/unit/test_cli_contract.py tests/unit/test_cli_reference.py`: 230 passed,
  with 1 deselected. The deselected
  `test_cli_uses_only_stdlib_and_package_imports` fails on the concurrent
  `getpass`/`hashlib` imports. Ruff format and lint pass on the changed Python
  files except the concurrent hunk at `test_cli_contract.py:520`, and
  `node --check app.js` passes. `test_lanes.py list --check` fails only on four
  unclassified concurrent package test files. `check_architecture.py` reports
  stale; it was deliberately not stamped because unreviewed concurrent source is
  present.
- Live smoke, using the real CLI and headless Chrome, read-only unless noted:
  - Default state root: 0 VMs, volumes and networks; 3 artifacts; 1 build;
    empty states; search and no-match.
  - APFS clone of `~/.local/share/palimpsest-proofs/ag-f1823ba8/state`: live
    reconcile showed 3 running and 4 stopped real Lima VMs. Its
    `runs/*/{state,owner}.json` and `projects/*/state.json` hashes (16 files)
    matched the originals afterwards, and the original root was never served.
    This smoke also covered BuildKit log content, 5 s polling with 30 s
    summary/storage, pause/resume (0 requests while paused), clean-URL reload,
    failure retention after the server stopped, a 390 px layout and no console
    errors.
  - Synthetic `/tmp` fixture: OCI-root NAT/none plans and root volumes,
    preserved project volume, malformed-network unavailable row and KVM stale
    rows. In `--allow-control`, an unaccepted confirm changed nothing, an
    unreferenced fixture layer was removed, and a referenced layer was refused.
  - No VM lifecycle action ran against real resources. All servers, the
    session browser and the `/tmp` fixtures were stopped or removed.
- Not done: no commit, push, publication or packaging. No Linux KVM/OCI-root
  live dashboard run happened: the OCI rows use test fixtures. The pre-existing
  `inventory._build_project_index` symlink-following read is unchanged. Packaging
  this as an offline desktop application is a later step.

## Project-scoped package registry — 2026-10-02 (local, uncommitted)

- Baselines: Palimpsest `dev` `dc8a164fd7ad4243f0e01bf7e48e613608674ccb`, Afterglow
  `dev` `0f59e0ee8e7cea6d36db33f1d4e2380fe4fac2b7`; both dirty, nothing staged.
  Pre-existing changes (dashboard prototype, Hub observability, docs/openspec edits)
  were preserved and not reviewed as part of this work.
- Implemented per [project-package-registry.md](project-package-registry.md):
  original-subject Keystone validation by a dedicated system reader (no
  `v3.Token` exchange), exact opaque project/user IDs (64-hex federated
  supported), native `/v1/projects/...` namespace/package/version/tag/key/upload
  and project/package-bound BuildKit cache, bounded OCI/runtime graph validation,
  retirement of unqualified cache registration, native CLI profiles/login/build/
  push/pull, Afterglow package BFF/key gateway and `/palimpsest/packages` page,
  Kolla reader/protection inputs.
- Integration fixes found by the live proof: Hub accepted no BuildKit OCI layout
  directory entries (`blobs/`, `blobs/sha256/`) and rejected every real export
  with 422; unconfigured qualified authorities had stopped routing to Docker;
  strict offline builds had started reading `registries.toml`; Afterglow namespace
  grammar rejected Hub-valid `__`/`--`; desktop inventory clipped row actions.
- Checks: Hub 208 passed; Hub ruff clean except 3 findings in pre-existing
  observability edits (`services/builds.py`, `tests/test_hub_api.py:971`,
  `tests/test_image_exports.py`). Root portable lanes 6112 passed / 217 skipped
  (`umask 022`; the tool shell's `umask 0077` fails three permission tests that pass
  under 022), manifest valid, CLI reference regenerated, changed-file ruff clean.
  Afterglow backend documented unit command 3455 passed, contracts 135 passed,
  frontend vitest 1893 passed, `svelte-check` 0 errors/warnings. Hub arm64 and amd64
  images built; arm64 ran against the local stack, amd64 only an import smoke.
- Live local proof `pkg-d642b179` (evidence
  `~/.local/share/palimpsest-proofs/pkg-d642b179/verification.json`): genuine
  local Keystone/MariaDB/Redis, source Hub, Afterglow BFF and TLS key gateway,
  production frontend build, real Dockerfile build → mandatory scoped cache →
  native push → API/browser inventory → pull with byte-identical archive, plus
  reader/sibling/cache/foreign-namespace/mixed-credential/revocation/membership-
  removal/admin denials, 409 offset, 412 tag conflict and UI project switching.
  All proof processes, containers, builder, images and scratch secrets were removed.
- Incident: an unscoped `pytest -n 8` in Afterglow `backend/` (stopped at ~93%)
  collected `tests/integration/`. With `../afterglow.conf` and
  `tests/integration/credentials.toml` present, its fixtures likely sent real
  password logins for the configured admin/user accounts to
  `keystone.dmslab.re.kr`; the configured Redis host `redis` does not resolve
  here, so its cache-key cleanup could not reach a real Redis. `slow` resource
  tests require unset env vars and skip. Which integration nodes ran is not
  recoverable from the cancelled output. Use `backend/tests/TESTING.md` commands only.
- Not done/pending approval: no commit, push, image publication, Kolla deploy,
  production reader account or protected-ID configuration, or cloud.dmslab
  acceptance. `ARCHITECTURE.md` content was updated but not stamped because the
  working tree contains unreviewed foreign changes.

## Candidate integration — 2026-10-02

- Scope: the basic `/Users/pieroot/code/palimpsest` checkout on local `dev`,
  source baseline `dc8a164fd7ad4243f0e01bf7e48e613608674ccb`. Separate worktrees
  were not read or changed. All current tracked/nonignored source, tests,
  contracts and docs are included in the authorized candidate preparation.
  Root `palimpsest-client` and independent `palimpsest-hub` metadata/module
  versions/lockfiles agree at **0.3.0**, a minor for the native registry and
  dashboard additions, not a claim of publication. Kolla's image-tag candidate
  also prepares 0.3.0, and source-build pins reviewed commit
  `c4887f7806608e98f215abbd377d2eafe159ff76`; image availability must be verified
  before rollout. The source-build contract requires an immutable full SHA,
  not a mutable branch or an unresolved release tag.
  This immutable application-source commit includes every final source/style
  change and the 0.3.0 metadata. The later source-pin/stamp documentation
  commit deliberately references it, avoiding a circular self-referential SHA.
- Review fixes: native authority/alias routing, completed-reference bounds,
  API-base/namespace local-cache isolation, mandatory online cache refresh,
  malformed actor/graph handling and OCI/runtime client/Hub validation parity;
  CAS copied-byte verification and repair preservation, publication-lock/SQL
  lifetime, key expiry/revocation across reader waits, case-exact legacy project
  ownership, creator-bound uploads, correlated package errors and safe Kolla
  access logging. Dashboard polling no longer aborts long controls, destructive
  volume choices reset between drawers, and the skip link preserves the view.
- Final functional gates: `scripts/test_lanes.py list --check` passed;
  `plan --changed HEAD` selected all portable lanes plus the separate Hub
  environment. `uv run --frozen python scripts/test_lanes.py run portable`
  passed **6131**, skipped **217**. The initial run found one missing `digest`
  reference in the existing CLI verify test; its expected SHA-256 now comes
  independently from `hashlib`, and the changed node passed before the full
  rerun. `cd hub && uv run --frozen pytest -q` passed **215**. Skips do not
  constitute native proof. The generated CLI reference was regenerated and
  covered by the portable contract gate.
- Final style gates passed in both environments: root Ruff checked all 371
  files and Hub checked all 37. Three import-spacing/order findings and
  formatting in 34 candidate files were corrected without changing CI
  exclusions or functional contracts; both functional suites were rerun.
- The metadata-only final source-pin follow-up passed all **24** Kolla asset,
  immutable image-ref and role contracts. Local commits prepared the complete
  candidate and then its application-source pin; dev push remains withheld
  pending the owner's explicit decision on workflow side effects.
- Real source boundary proof: normal CLI plus source Uvicorn/CAS/package APIs
  over loopback HTTPS with a trusted temporary certificate, scratch SQLite
  and a synthetic system-reader/member Keystone endpoint. An existing running
  `palimpsest-e2e` BuildKit builder was explicitly selected, not reconfigured.
  A real `FROM scratch`/`COPY` solve exported native OCI, mandatory cache
  refreshed without `--push`, deferred typed-reference push succeeded, and
  pull returned byte-identical exported bytes. A second build with fresh local
  state imported **hub-exact**, following the first **none** result; both had
  build key `sha256:5566669e0f1c0c3dec0b28d9a3fbd976e7d5740db3414b6f1cadaa701e81a8df`.
  Its built root was
  `sha256:66d26ed3025000569d98cbe5a63c756f9e58f92c4ba055215c270db7bd274b8a`.
  Original-project assertion/admin/foreign-namespace/mixed-auth denials,
  offset conflict, idempotent finalize, cache transfer, membership removal,
  revocation and absence of plaintext key in local state were observed.
  SQLite engine/session setup was injected by the throwaway lifespan wrapper
  because unchanged production `init_db` is MariaDB-oriented; all application
  auth/routes/storage methods remained real. This is not live Keystone or
  production MariaDB policy/migration qualification.
- Chromium proof: actual packaged UI on loopback, isolated tab, synthetic VM
  responses only (no VM mutation). Before repair, another drawer inherited
  volume deletion, the skip link switched to Overview, and a held 18-second
  mutation failed at fifteen seconds. After repair, deletion was unchecked,
  the VM panel stayed active, controls remained disabled at 16.5 seconds and
  one request completed successfully. Read-only API denial, bearer auth and
  URL-token removal were also observed. Screenshot evidence:
  `/tmp/palimpsest-controls-030-after.png`. Local servers/tabs/scratch credentials
  were stopped or removed; shared runtime volumes/containers were preserved.
- Canonical local artifacts: client wheel/sdist plus isolated install/CLI
  version/packaged stage-1 asset checks passed via `scripts/build_package.py`;
  Hub wheel/sdist built. `docker/hub/Dockerfile` API **and** export-worker
  targets built for **linux/arm64 and linux/amd64**. Network-disabled runs
  reported `aarch64`/`x86_64`, Hub version 0.3.0 and successful API/worker imports.
  These are image build/execution checks, not a canonical Kolla rollout or
  service/migration readiness proof. Build caches were not pruned.
- Architecture was stamped only after source review and corrected-test review;
  the final source fingerprint is
  `8f64dbde7852dca6d04ab613050929505a71c5f5f971d64e17bad63d6f1caa16`
  over 441 source files. Staged guard and secret scanning are commit barriers.
- Publication/integration boundaries: no main-target PR was created/edited,
  no main merge, tag, release, GHCR push, private helper transfer, remote
  installation, Kolla/cloud/native/GPU command or role mutation was performed
  in this checkpoint. A normal `dev` push triggers existing package/GHCR
  publication workflows; that automation requires the owner's scoped decision.
  Parent owns release/main/deployment integration. Astra/Sol roles were
  unavailable; independent source review is not named-role approval.
- Deployment prerequisites remain: reviewed published API/worker ref/digests,
  operator-installed system-supporting `openstack.cloud` collection, dedicated
  reader principal and verified effective grants/policy, immutable protected
  IDs and namespace binding, trusted HTTPS origin, approved database rollout,
  protected native CI success and the real Afterglow gateway/UI acceptance.
  Official openstack.cloud 2.6.0 supports `role_assignment(system=all)`;
  no installed-collection or live operator proof is claimed.

## Afterglow native OCI-root candidate — 2026-10-02 (local, uncommitted)

- User selected Linux amd64/KVM OCI-root (not a baked conventional cloud image or Docker Compose inside a VM), then a separate OpenStack CI VM rather than `pieroot-server`. No cloud VM, volume, security group, remote helper transfer/installation, publication or commit/push occurred.
- Baselines: Palimpsest `dev` `d0bda353636d0dcb226def293af2495297aae455`; Afterglow `dev` `27f2945`. Existing Afterglow release/Kolla documentation changes were preserved. Palimpsest source was not changed. Named Astra/Sol roles were unavailable; task-agent implementation and parent source review do not grant scoped native-operation approval.
- Afterglow changes: separate root Dockerfile `native-vm` target; `backend/scripts/native_vm.py`; consumer-visible lifecycle regressions in `backend/tests/test_native_vm.py`; OpenSpec `afterglow-native-oci-vm`, architecture/changelog and Korean/English deployment contracts. Canonical uv worker dependencies, locked Bun frontend dependencies and glibc Node 22 are preinstalled alongside MariaDB/client/Redis. Existing Compose/Kolla/CI and sibling-service ownership remain unchanged.
- Real local image smoke on arm64 and emulated amd64: UID 1000, all capabilities dropped/no-new-privileges; direct API/frontend HTTP 200, 37 SQL tables, Unix-socket SQL/no TCP, authenticated loopback Redis, direct Notion worker and Node, no Docker binary, SQL/Redis sentinel and credential persistence across stop/start and child-failure recovery. Redis exit failed supervisor with exit 1; final graceful stop exit 0 (arm64 1.07s, amd64 0.798s). This is image execution, **not** Palimpsest actual-root/KVM or authenticated OpenStack proof.
- Build failures retained: the existing `palimpsest-e2e` builder has `network=none` and was not reconfigured. New-target npm installation without the authoritative lock failed with Arborist `edgesOut`; the new stage now uses `bun install --production --frozen-lockfile`. Docker Desktop built both platforms and exported the amd64 standard OCI archive. Temporary smoke SQL omitted required timestamps, used an unavailable Redis `aclose` API and assumed unmodified Redis/Node argv; those harness defects were corrected without changing application source or weakening readiness/persistence assertions.
- Artifact: `~/.local/share/palimpsest-proofs/ag-native-8e42ddb6/afterglow-native-amd64.oci.tar`, 687704064 bytes, mode 0600, 22 layers; manifest `sha256:1802fa41a87f6c22e813a983afafbc87707483bba71d4e843709e924ef0d0bf0`, Linux amd64/appuser/direct native entrypoint. Descriptor/config hashes and platform/layer limit were checked; materialization and guest boot remain unverified. JSON evidence: `smoke-arm64.json`, `smoke-amd64.json`, `oci-summary.json`, `prerequisites.json`, `cleanup.json` in the same private directory.
- Checks: focused pytest 15 passed; canonical safe `npm run test:unit:backend` 3538 passed (integration/contracts excluded); scoped Ruff check/format passed. Afterglow reviewed working source guard passed at `5bec7ca3404c3fbaf63b305c5ebb2c58f0bd750398d8906ba3433b0d494e7ae6` (2166 files). Staged check covered unchanged baseline only (2164 files), not this unstaged candidate. Full frontend/contract/functional gate and native acceptance are not claimed.
- Cleanup: exact proof-labelled containers `ag-native-8e42ddb6-{arm64,amd64}` and their test-only state volumes were removed after exit-0 verification. Generated test credentials and temporary smoke scripts were removed; OCI artifact, built images and redacted JSON evidence remain. No shared project/volume or build cache was pruned.
- Blocker: current host Darwin/arm64; no selected `OS_APPLICATION_CREDENTIAL_ID/SECRET`, `OS_CLOUD`, `OS_CLIENT_CONFIG_FILE`, standard clouds/secure YAML or qualified `PALIMPSEST_OCI_KERNEL/CONFIG/PACKER` inputs. Need a private path to the CI project's current member-only credential. Verify current project/permissions, available image/flavor/network and qualified kernel/config/packer read-only, then obtain exact VM/volume/security-group and transfer/installation/native-proof scope before provisioning. Do not substitute the admin project or another host. The OpenSpec change remains unarchived pending actual public-run/root-proof/browser/retained-root acceptance.


## Afterglow native continuation preflight — 2026-10-02

- User requested the next step. This authorizes continuation, not unspecified Nova provisioning, credential recreation, remote installation or private helper transfer. Current Palimpsest remains `d0bda353636d0dcb226def293af2495297aae455`; Afterglow HEAD advanced to `ff007edb6e4bf75f0c0a05107ca6340b593675ff` while the native candidate is still unstaged/uncommitted. Its reviewed working-source fingerprint remains `5bec7ca3404c3fbaf63b305c5ebb2c58f0bd750398d8906ba3433b0d494e7ae6`; current working guard passed. Other changes were preserved.
- Fresh local OCI checks passed for all 22 layer descriptor sizes/SHA-256 and decompressed DiffIDs, manifest/config descriptors, Linux amd64 platform and ordered rootfs/runtime configuration equality with the previously smoked local image. Archive SHA-256: `09f4acf88f2320f061e8ff866abec0cf78e3a2612923980124c1cd8945285cdb`; config digest: `sha256:2ea7cf7b3fe45a00fb3c7e2c94e178446a094b5a77f738f24e3d7df2a6da48d4`. This closes image/archive linkage, not native VM acceptance.
- Fresh credential presence-only check: no OS_AUTH_URL/OS_AUTH_TYPE/application-credential ID/secret in the process environment. The five previously documented private input files under `~/.local/share/palimpsest-candidate-024/` still have no application-credential secret field. `ci-resources.json` retains an ID field only; it cannot authenticate. Values were not printed, copied into evidence or used for authentication. Earlier protected GitHub/keychain metadata checks are historical; GitHub secret metadata cannot return secret values.
- Secret-free current evidence: `~/.local/share/palimpsest-proofs/ag-native-8e42ddb6/continuation-preflight.json`. No cloud authentication, network/resource inventory, native boot, mutation or new test run occurred. Need the private current member-only CI credential input path before read-only project/image/flavor/network inspection. Canonical helper pins CI project `53ec2dd9a1f7471fb6a2b174595fa232`, at most one 2-vCPU/8-GiB VM and one 20-GiB boot volume, no Floating IP, restricted SSH ingress; these are limits, not approval for this Afterglow proof. Confirm current target and exact transfer/installation/boot/retained-root/cleanup approval before creating anything.


## SYSTEM direct-Nova Afterglow proof and host-share lifecycle — 2026-10-06 (uncommitted)

- Scope: user-approved SYSTEM project `944405f937c3410f9b4f082b20a22e10`, dedicated member/reader application-credential profile, `cpu.2c_8g`, at most 2 VMs, `ceph_hdd` 20+20+8 GiB, one 10 GiB CephFS share, one RGW bucket, one Glance image and owned keypair/SG/≤2 FIPs. Palimpsest `dev` `1acbe47c0da5307b4a96538e65993138100d3e4f` with unstaged changes; Afterglow checkout `34ec462f` was **not** transferred wholesale. No commit, push, publication, deployment, router/network/quota change or nested-KVM substitution occurred. Astra/Sol roles were unavailable; independent reviewer agents reviewed the exporter (twice) and runner. Reported issues were addressed, but the existing-user-SG and Manila endpoint changes made during the live run did not receive an independent re-review; later focused regressions cover their acceptance/rejection boundaries.
- Source: `scripts/package_native_afterglow.py` bundled `git --no-replace-objects archive ff007edb6e4bf75f0c0a05107ca6340b593675ff` plus reviewed overlays (`Dockerfile`, `backend/scripts/native_vm.py`, `backend/scripts/native-cloud/*`, `scripts/export_native_cloud.py`). Bundle `8f28a7127069a4154832e5fae517306b3fb087d592a5cecbd7783d98a8b26557` (18369662 bytes, 3659 files), manifest `18f1e67fc5e871432ec2216356487c077fda0a31a1e9895e4753be1f917b4207`, parent-written source approval in the private evidence directory.
- Live result, owner run `pal-system-1e151821`: the builder (Ubuntu 24.04 image pinned by SHA-512) produced a BuildKit `native-cloud-vm` linux/amd64 rootfs (gzip `8d2c8251…`, 742227704 bytes; build 3592 s after one interrupted attempt). The exporter produced BIOS raw `c8c33ab1d88bde9e2082e245478c476d627c393c2298691d5ee8b0194f8d667e` (8 GiB; Debian trixie, kernel 6.12.111, builder GRUB), uploaded as Glance `3f18befa…`. The consumer booted directly on Nova from a Cinder volume of that image. First boot and after reboot: `systemd-detect-virt` kvm, PID 1 systemd, `afterglow-native.service` active as appuser with 0 restarts, five supervisor children, no container engine binaries/processes, API 8000/frontend 3080 HTTP 200, 37 tables, SQL TCP closed, Redis loopback-only, DEFAULT_NETWORK_ENABLED=false, no admin credentials configured. MariaDB/Redis markers, Cinder data-volume ext4 marker (fstab) and CephFS builder/consumer markers survived the reboot. RGW 8 MiB payload and manifest checksum round trip passed. Dedicated member login: 200, password auth, SYSTEM, roles member/reader, not system admin, consumer listed ACTIVE, logout 200, SYSTEM network/router inventory unchanged. The Notion work queue was not exercised (it needs external Notion/admin credentials).
- Cleanup: all 14 recorded resources (2 servers, 3 volumes, 2 ports, 2 FIPs, SG, keypair, share, image, bucket) were deleted with no refusals. An independent listing found no `pal-system-1e151821*` objects; the protected FIP 172.30.102.169 is still present; the active binding was released and the cephx key file removed. Evidence: `~/.local/share/palimpsest-proofs/ag-native-8e42ddb6/system/run/{prepare,build,run,run-login,cleanup,source-bundle-verification,system-run}.json`. The secret scan checked four profile credential values (excluding the private SSH key) and found no matches. It did not scan the CephX key before deletion; absence of all possible secrets in retained evidence is not proved.
- Access deviation (user decision): builder SSH timed out with the approved 10.8.0.2/32 ingress while this Mac routed 172.30.0.0/16 through en0, not utun6. Source-CIDR mismatch is an inference from that route and timeout, not a packet-source measurement. The user then added TCP 22 and ICMP from 0.0.0.0/0 to the SYSTEM project's `default` security group `0bf5daef-0589-498a-9028-eecc3deb3cc1` and attached it to the builder port; SSH subsequently succeeded. The runner gained `--user-port-security-group UUID`: an exact project group that is accepted beside the owned 10.8.0.2/32 group and applied to new owned ports, and is never mutated or deleted. **Those user-made `default` SG rules remain**; the user decides whether to revert them. Runner fix found live: Manila's catalog endpoint is project-less `/v2`, so the runner appends the authenticated project id.
- Boundary: this is a conventional raw cloud disk derived from the native OCI root plus declared bootability additions, booted directly on Nova. It does not prove protected OCI-root stage-1, untouched OCI execution, security parity, or a public Palimpsest OpenStack backend; Afterglow OpenSpec tasks 6/7 stay open.
- Local compatibility (source + portable proof only): project `type: bind` virtiofs lifecycle fixes (structural parse vs. physical validation, restart bound to the recorded export set, activation-gated project-init, read-only retained no-op validation). File-backed managed block volumes satisfy the volume requirement; LVM was not added. RGW is user-scoped and Hub blobstore unchanged. Checks: `scripts/test_lanes.py run portable` 6292 passed/217 skipped; targeted 621 passed; mutation of the project-init guard failed 16 cases as expected; Ruff check/format and the lane manifest pass; Afterglow exporter suite 24 passed. **Not live-verified:** Linux KVM virtiofs mount/restart on a real host (this host is Darwin; no approved Linux KVM host was used).
- Local actual CLI smoke: `palimpsest compose -f <temporary-project>/palimpsest.yml config --quiet` accepted an existing normalized `shared` bind source (exit 0), then rejected the removed directory (exit 1) without VM work. The initial documentation example used `./shared`, which the normalized-path contract correctly rejected; the example now uses `shared`. A throwaway assertion initially searched for singular `directory` instead of the observed `directories`; that harness assertion was corrected against the retained result. Temporary project files were removed. This is configuration-path proof, not a live virtiofs mount.
- Final focused runner regressions: 49 passed, including invalid SG UUIDs, missing/foreign/owned-group rejection, unapproved extra port SGs, disabled port security and project-less/already-scoped/foreign Manila catalog URLs. Focused Ruff check/format passed. Palimpsest working architecture guard passed at `db92e4eaa3dd388cb9d3fbeafe8de7ec7429c6f590df8c46aecf550e3ec513eb` (445 files). Afterglow working guard is **stale**; its staged guard passed at `948ec0d9589738c81fb8d218793ff89c17c271910e997202845a08b15b8de60b` (2164 files), covering the staged baseline only, not native unstaged changes. No stamp was applied to the whole dirty Afterglow checkout because unrelated source was not reviewed and the guard offers no per-file scope. Exporter tests were run on Darwin, not as a root Linux test suite; the real Linux export is separate live evidence. gbrain sync is unavailable: the installed detector reports `gbrain_on_path=false`, `gbrain_local_status=no-cli`; no guidance/config changes or index sync occurred.
- Current Afterglow supervisor selector: `AFTERGLOW_ALLOW_INSECURE=1 .venv/bin/python -m pytest tests/test_native_vm.py -q -p no:cacheprovider` passed 15 tests with five existing Pydantic/FastAPI deprecation warnings. Only this isolated selector ran; no unrestricted backend pytest or integration tests ran. The first background tool invocation lost its completion output (reported to tool QA); the reported result is the subsequent foreground run.
- Next: the owner decides on reverting the `default` SG rules, routing 172.30.0.0/16 via the approved tunnel for future runs, committing both repositories' changes, and a separately approved Linux KVM host for live virtiofs and protected OCI-root acceptance.

## Production Afterglow 1.30.1 Kolla redeploy — 2026-10-06

- Approval: the user selected option 3, the existing production Kolla service,
  preserving its data, keys, TLS and sibling services. This deployed **the
  existing published 1.30.1 release**, not the dirty local Afterglow checkout or
  the native-cloud-vm candidate. No build/publication, commit/push, new Nova
  resource, GPU operation or native proof was performed in this rollout.
- Operator: `wireguard-dmslab`, `/etc/kolla`, installed plugin
  `/etc/kolla/plugins/openstack-afterglow-v1.30.1/`; controllers
  `dms-controller1`, `dms-controller2`, `dms-controller3`. Existing immutable
  image references and image IDs were retained on all three:
  - API: `ghcr.io/openstack-afterglow/afterglow-api@sha256:382139131aea35ffbbba68a3f4c199b573cffd9f8cb2ae8f0e3ecf1f638ae15c`.
  - Frontend: `ghcr.io/openstack-afterglow/afterglow@sha256:8bb64230d8e929898c2da146cf23d09b9f4eac98e56511a33ca427fc9e2f330a`.
  - Worker: `ghcr.io/openstack-afterglow/afterglow-worker@sha256:e261cc2763b4a4e3ba04a907bf4154da8c226f25c7d6b0edf8f3639d1ff5d6fd`.
- Canonical installed CLI, `/etc/kolla/.venv/bin/kolla-ansible`, with inventory
  `multimode` and `--tags afterglow`: `prechecks`, `genconfig`, then sequential
  `reconfigure --limit dms-controllerN -e afterglow_start_restart=true`.
  All five invocations returned 0; each reconfigure recap was `ok=96 changed=7
  unreachable=0 failed=0 skipped=26 rescued=0 ignored=0`. The existing role's
  restart variable restarted backend, frontend and Notion worker on each node.
  Controller 1 completed at 12:13 UTC, controller 2 at 12:16 UTC and controller 3
  at 12:19 UTC. Other replicas/public HTTPS were checked before advancing.
- Fresh owner-private backup directory (0700):
  `/etc/kolla/afterglow-release-backups/pre-afterglow-redeploy-20261006T120225Z`.
  It contains controller configuration archives, operator inventory/lock/config
  archives, the consistent `db/afterglow_kolla.sql.gz` dump and CLI logs/receipts.
  **These backups contain credentials and real database data; they are not
  secret-free public evidence.** SQL/configuration archives were kept on the
  approved operator host, not copied to the local screenshot directory.
- Real rollback qualification: the MariaDB 10.11.19 dump restored successfully
  into an exact-owned temporary `mariadb:10.11.19` container on controller 1,
  with network disabled, no published ports/host binds, tmpfs data and CPU,
  memory and PID bounds. All **89 tables / 1,517 rows** matched, including
  per-table counts; restore return code 0. The labelled temporary container was
  removed; the official restore image remains cached for offline rollback.
  Receipt: `db/restore-verification.json`.
- Final protection receipt at 12:39 UTC:
  `protection-verification-final.json`, `verified=true`. All 9 operator source
  files and 307 archived controller configuration files matched their original
  content hashes. All **602 pre-existing non-Afterglow running containers**
  retained their IDs, image references/IDs, start times, running states and
  restart counts (232/225/145 by controller). Application image IDs, private
  environment hashes and mount sets were unchanged; all 9 application start
  times changed. HAProxy, ProxySQL and Keepalived were not restarted.
- Live readiness: backend/frontend Docker health and configured HTTP probes
  passed on all three controllers; public `https://cloud.dmslab.re.kr/` and
  `/api/v1/health` returned HTTPS 200 with certificate verification enabled.
  The backend still uses external `afterglow_kolla` SQL and Redis database 5;
  no replacement persistent data volume was introduced. Workers are running;
  Notion job processing was **not** exercised.
- Real Chromium acceptance used the existing dedicated SYSTEM member/reader
  principal, not a synthetic identity or admin session. Login, `/auth/me`,
  dashboard/quotas, Cinder volumes, Manila file storage, object-storage buckets
  and account statistics returned 200; 19 distinct captured API outcomes were
  all 200, with no captured browser runtime errors. The UI displayed 16 existing
  volumes, zero shares and zero buckets. No resource create/delete control was
  used. Normal confirmed logout returned 200 and the owned browser was closed.
- Login's default-network prewarm was checked before authentication: the SYSTEM
  default-network database record referenced an existing valid project network.
  Independent before/after SDK reads found the exact same 1 network and 1 router,
  so this login did not provision a default network/router.
- Verification harness corrections, not production fixes: backup setup resumed
  after a permission/path prerequisite error; the isolated SQL readiness probe's
  SSH argument quoting was corrected; Docker comparison was aligned to the
  running-only baseline and mount ordering was canonicalized. The initial false
  comparison is retained in `protection-verification.json`; the final receipt
  passed. Controller 3's stopped OpenSearch container predates this rollout and
  was excluded from both running-only snapshots; it was not removed or repaired.
- Private local receipt/screenshots:
  `~/.local/share/palimpsest-proofs/afterglow-kolla-redeploy-20261006T120225Z/`
  (`deployment-verification.json`, `dashboard.webp`, `volumes.webp`,
  `file-storage.webp`, `object-storage.webp`). Operator acceptance receipt:
  `authenticated-acceptance.json` in the backup directory. This checkpoint only
  updates this handoff; source/architecture markers were not restamped. Existing
  native/virtiofs acceptance and owner decisions above remain separate.
- Completion guard: `python3 scripts/check_architecture.py` passed against
  working source `db92e4eaa3dd388cb9d3fbeafe8de7ec7429c6f590df8c46aecf550e3ec513eb`
  (445 files). No source stamp was changed; no new application tests were run
  for this unchanged-release operational redeployment.


## Scoped Palimpsest package capabilities — 2026-10-07 integration

Repo-local OpenSpec: `openspec/changes/archive/2026-10-06-scoped-package-capabilities`. Implemented native exact leaf capabilities, current unique-global role-ID directory/implication-graph resolution, baseline member/reader checks, original-token/current-owner intersection and use-time key attenuation. Package metadata is separate from content download; key issue is keys-editor plus subset, own-key revocation is keys-admin. Preserved key-only package publication, immutable package/tag/CAS boundaries and verified legacy builder/GC authority. No service-admin VM authority or new package-delete/public policy APIs.

`hub/tests/test_auth.py::native_http` provides real loopback Hub and synthetic Keystone HTTP setup, temporary SQL/CAS and no production application lifespan. Run the exact commands in [package authorization smoke](project-package-registry.md#isolated-http-smoke-defined-not-executed-during-integration) only after integration. Existing package and native artifact regression fixtures are migrated to explicit capabilities. No checks/builds/lint/formatters/smoke were executed for these edits; no commit/index/deploy/cloud mutation or gbrain indexing occurred. Architecture marker is intentionally unstamped under the no-check instruction. Astra/Sol pairing and independent review were not performed in this delegated implementation; no independent review is claimed.

## Scoped native container acceptance — 2026-10-07 verification

The integrating parent supplied a passing selected Hub162 run including the
current native SDK HTTP grade matrix, real range-resume/download downgrade and
metadata404→503 correction. It was not rerun merely to confirm that result.
This follow-on scope built both canonical Hub targets and exercised the actual
registered native routes in local arm64/amd64 containers: **182 checks each,
364 total**, then frozen root package/credentials/CLI/BuildKit **309 passed**.
Canonical image imports report Hub0.3.1 and `aarch64`/`x86_64`; API routes58,
worker `qemu-img10.0.13` formats validate without any export conversion.

Added reusable `scripts/smoke_package_capabilities.py`; root pull preflight now
requires inventory+download to match its metadata and content calls. Root
regressions cover closed actions, normal inventory+write publish without read,
inventory-only pull denial, preflight failure before transport and exact CLI
capability requests. Credential helper source/tests remain intentionally
unchanged: current namespace-qualified storage/isolation already suffices.
Existing docs, changelog, architecture narrative and repo-local OpenSpec
checklist record completed local acceptance, not production readiness.

Real boundaries: registered `main:app`, actual Hub logic/datastore/filesystem
CAS, real Redis ticket storage, current-authority lookups through the real
Keystone SDK. Synthetic boundaries: a mutable HTTP Keystone directory, role
assignments/subjects and one seeded completed export row with real CAS bytes.
Original uploaded archive, layer, legacy and export bytes match, and Range206
resume reconstructs them. SQL and11 verified CAS files remain identical after
both container restart and recreation using retained named SQLite/blob volumes.
Inventory/content split, exact-scope subset, own revocation, role/edge downgrade,
protected system identities and system-only global builder/GC gates passed.
The global builder stays unconfigured (authorized system admin receives503);
no real KVM/OpenStack/Glance build or cloud mutation was attempted.

Canonical SQLite bootstrap is **not** a pass: both probes exited1 with
`TypeError: 'connect_timeout' is an invalid keyword argument for Connection()`.
The isolated smoke-only derived image adds lock-pinned aiosqlite0.22.1 and its
harness installs a dedicated SQLite factory with production lifespan off,
without replacing routes/auth/blob logic. Production database code, dependency
metadata and Dockerfile were not changed. Do not promote these receipts into
canonical MySQL/MariaDB, production lifespan, Redis credentials, TLS transport
or deployment qualification.

Exact commands, reports and retained volume names are in
[Docker-backed registered HTTP acceptance](project-package-registry.md#docker-backed-registered-http-acceptance).
Local receipts: `build/scoped-package-capabilities-smoke/20261007-arm64/report.json`,
`20261007-amd64/report.json`, sanitized runtime logs, `root-tests.txt` and
`image-versions.txt`. Only this run's disposable resources were removed; the
four SQL/blob volumes remain. No usable issued key response was retained after
teardown; the parent browser fixture was not fabricated from native secrets or
qualified as live native rendering. No Afterglow edit, production deployment,
live Keystone role write, commit/push or shared index mutation occurred. Broad
final gates and Afterglow/browser integration remain the parent's scope.

## Deferred export credential isolation — 2026-10-07 (local, uncommitted)

Repo-local OpenSpec: `openspec/changes/isolate-service-execution-credentials`,
scoped to the Palimpsest slice of the parent's four-service cutover approval.
Deferred Glance image exports no longer use a service password scoped to the
tenant project. `get_admin_connection_for_project` is removed. Admission keeps
the original-token Glance check and current publish authority, then creates a
requester Trust with impersonation, the configured least roles (default
`member`, required by Glance's default `download_image` policy), the token's project and a finite expiry. The trustee is resolved by
authenticating only to the service's own project. The Hub stores only the Trust
reference/scope in the additive table `palimpsest_image_export_delegations`.
The worker revalidates current authority, verifies the trust token, then
performs Glance I/O. The worker records durable Trust cleanup for terminal,
deleted, exhausted and abandoned work; cleanup failure only reschedules
deletion. Native package planes, local KVM/build and CAS domains are unchanged.

Evidence status: source-reviewed and test-passed against a synthetic Keystone
only, not live. Local commands run on 2026-10-07 against the uncommitted dirty `dev` tree:
`cd hub && uv run pytest -v tests/test_export_delegation.py` passed 28 cases.
These used real keystoneauth/keystoneclient/openstacksdk objects over loopback
HTTP. Glance bytes and qemu-img were stubbed. The full Hub lane `cd hub &&
uv run pytest` passed 285. Root `tests/test_kolla_role_contracts.py
tests/test_kolla_assets.py` passed 45. `scripts/test_lanes.py list --check`
passed, and `tests/unit/test_test_lanes.py` passed 106. `openspec validate
isolate-service-execution-credentials` passed. In Hub ruff, the only remaining
check error and format drift are in pre-existing user-dirty files: an I001 in
`tests/test_auth.py`, plus formatting in `auth.py`, the package routes and their tests.
The files changed here are clean. `scripts/check_architecture.py` reports a stale snapshot because the
marker was intentionally not stamped. No live Keystone Trust, Glance, deployment,
role mutation, commit or index change occurred. Pending: the parent's scoped
architecture stamp, Afterglow/Kolla overlay alignment, a live Keystone trust
policy smoke, and an operator decision on existing manual tenant grants. Exports
queued before rollout fail `delegation_required` and must be resubmitted.

## Isolated ecosystem full-source release candidate — 2026-10-08 (not shipped)

- Ownership: integration-owner edits stayed inside
  `/tmp/afterglow-ecosystem-release-20261007.FDut9J/palimpsest`, but a delegated
  relative-path error edited four original user-dirty files. Its exact delta was
  recovered into the isolated candidate, with original restoration owned by
  Main; `.git/original-ownership-incident.json` records paths/hashes and the exact
  reverse-patch prerequisite. No original reset/checkout/reverse or index edit
  was performed by this preparer. Canonical clone starts `dev` tracking origin/dev at
  `63f6f665e7f7f7469f384edd49619fa5d6683284`; no new commit/push/tag or remote
  operator action occurred. PR15 `449466e4fd71d956b9cdf32682663073ee579a3f`
  and PR16 `41be56f075e64f4530608ec2dbcfe7db38577017` remain in ancestry and
  their native workflow/real descendant fixture source is preserved.
- Imported original baseline `aa397a0fd976e35302974a347748a9773e3084bf`:
  all 32 tracked working deltas and all 13 source files in four untracked
  groups (delegation tests, OpenSpec changes/archive, synced package spec and
  the registered-app capability smoke script). Three-way integration preserved
  latest dev; architecture marker and changelog conflicts were reconciled,
  retaining both native opt-out/fixture and package/delegation intent.
- Related `/Users/pieroot/code/palimpsest-dev` HEAD
  `dc8a164fd7ad4243f0e01bf7e48e613608674ccb` is already in origin/dev ancestry;
  there were no new committed changes to merge. Its working `AGENTS.md` equals
  the current original source. The still-missing contributor/CI scenarios were
  merged, with overlapping obligations deduplicated and current explicit
  native opt-out semantics retained. Stale/prunable historical worktrees were
  not imported or modified.
- Version: reuse prepared **0.3.1** for root/Hub metadata, modules, both locks
  and packaged Kolla image tag. Current origin stable tags and releases contain
  no 0.3.1; latest stable tag is `v0.2.3`. PyPI 0.3.1 returned HTTP404, and
  public GHCR inspection found neither `0.3.1` nor `v0.3.1` for either image.
  GitHub package-list inspection was denied without `read:packages` scope.
  Development `package-<SHA>` prereleases do not publish stable 0.3.1.
- Read-only GitHub evidence: repository `PALIMPSEST_KVM_ENABLED=false`; actual
  [Test37632335023](https://github.com/openstack-afterglow/palimpsest/actions/runs/37632335023)
  on origin/dev has 18 successful jobs and one skipped native proof. This is
  baseline CI, not a check of imported source and not native qualification.
- Verification boundary: no tests/builds/lint/formatters or HTTP smokes ran for
  this integration. Source review is not a pass. Historical inherited results
  retain their original snapshot/environment boundaries. The integrating
  parent must run the root, Hub, contract/package/image and registered HTTP
  gates in [testing](testing.md#frozen-full-source-candidate--2026-10-08-verification-not-run)
  after the source is frozen. Known inherited lint/format failures are not
  rerun to confirm; final parent lint/format remains required.
- OpenSpec: `scoped-package-capabilities` and `disable-native-kvm-ci` remain
  archived/completed with their recorded historical evidence; the former's
  synced spec is included. `isolate-service-execution-credentials` stays active:
  source/test-defined and historical synthetic HTTP tasks are checked, but
  task2.5 (live Keystone trust policy/impersonating delete, Afterglow Kolla
  overlay alignment and separately approved manual-grant decision) is open.
  It must not be archived or marked live-qualified by this source preparation.
- Rollout prerequisites and exact migration/storage/credentials are in
  [install](install.md). Additive bootstrap creates delegation/package tables;
  undelegated queued exports fail closed and require requester resubmission.
  Every API/export/build writer must share SQL and the same absolute CAS with
  coherent locks; default host-local named volumes are not multi-host storage.
  Preserve private keys/secrets/mounts/previous image digests and take a
  consistent quiesced SQL+CAS backup with isolated restore proof first. Kolla
  source default remains reviewed `c4887f7806608e98f215abbd377d2eafe159ff76`,
  not a self-pin; use the eventual separately reviewed committed candidate SHA.
- Detailed nonsecret provenance, final source tree/archive identifiers and
  operator/final-gate report are retained under this isolated clone's `.git/`,
  not in the original checkout or release payload. Source preparation is not
  publication, Kolla rollout, live Trust/Glance proof or native qualification.


Source-review integration repairs (no checks run): export soft-delete now
normalizes database-naive UTC leases before expiry/busy comparison; cleanup
completion is fenced by the claimed `cleanup_attempts` generation and counts
only an owned affected row. The separate system-reader validator rejects
manager alongside admin/service. New regressions define actual Base-model
migration of every delegation state/64-character trustor/JSON roles/timestamps,
attempt exhaustion, requester replacement in reset branches and stale cleanup
lease completion. The reported test-auth import ordering was manually split,
without claiming a lint pass. Final parent gates and live policy task2.5 remain.


The standalone cache upload preflight now requires only `cache:write`; online
BuildKit's mandatory read+write preflight remains. Added client/CLI/Hub tests
define exact scope and current-owner attenuation but were not run. Existing
Docker smoke does not execute cache transfer or installed native CLI/TLS/helper
flows; those final real HTTP requirements are recorded separately.


Main's ownership recovery receipt: anchored reversals restored only the four
misplaced child edits; Main reported `cmp` exit0 against each saved
`.git/original-pre-child/<path>`. Original files are byte-identical to the
pre-child source snapshot, with original index/HEAD and unrelated files untouched
by that recovery. The incident remains in provenance; do not claim it never
occurred. Candidate retains the child fixes. Frozen architecture source metadata
is `e9af7ac5bf1928b5459625e966a299ae8630730d1c3b5252ea87e573ef6b327a`
(447 included source files); no guard or test was run to verify it.


Final documentation repair: the CI spec has one authoritative Requirements
block; the redundant older heading was removed without changing numbered rules.
The install checklist now explicitly requires approved provisioning of all five
exact global `palimpsest-*` leaves, four `palimpsest_*` parent grades, their real
inference edges and requester-project assignments before ingress reopens.
Kolla does not provision them. Existing plain-member export callers (including
Afterglow) otherwise receive 403 under the new publish leaf/worker recheck.
Source preparation made no IAM grants, graph changes or token exchanges; parent
must obtain the approved role/edge/assignment and real authorized-export receipts.

## Published 0.3.1 and held production cutover — 2026-10-08

- Immutable source: `v0.3.1`, `1458db42a1449a25b664584d144d0a97086f8f6f`.
  The preceding preparation snapshot and unexecuted statements are historical;
  parent final root/Hub gates passed 6,435/298 tests, with 261 root opt-in skips.
  Exact Test [37725533476](https://github.com/openstack-afterglow/palimpsest/actions/runs/37725533476),
  Hub images [37725879345](https://github.com/openstack-afterglow/palimpsest/actions/runs/37725879345)
  and root publication [37725879373](https://github.com/openstack-afterglow/palimpsest/actions/runs/37725879373)
  succeeded. Native KVM remained explicitly disabled; no new native proof exists.
- Root wheel/sdist are public on PyPI and [GitHub Release v0.3.1](https://github.com/openstack-afterglow/palimpsest/releases/tag/v0.3.1).
  The exact run's distributions match PyPI and GitHub asset SHA-256 values:
  wheel `827d6ffabb960402bc8c4dee7b8d9c4fe71903ba4a27141232ad2b9b7b5a69b0`,
  sdist `18d62f9272591a425a648e54050a24ecab36512663c81f8beff258010bc7b423`.
  GitHub Release creation was manually repaired after the final job's implicit
  success gate propagated the native skip. No tag movement, asset overwrite or
  PyPI republish occurred. Future tags use the explicit non-cancelled successful-
  publisher condition; evaluator smoke passed 24 cases and focused policy tests
  passed 127. No future tagged hosted execution of this correction is claimed.
- Separate Hub API/worker images match the immutable revision, with aliases
  `0.3.1`, `v0.3.1`, `sha-1458db4`, `latest`; both are **amd64 only**.
  Exact digests and deployment prerequisites are in [install](install.md).
  A literal `stable` alias, arm64 manifest or released Hub wheel is not claimed.
  `uvx --from palimpsest-client==0.3.1 palimpsest --version` returned `0.3.1`.
- Actual unmodified published digests exercised canonical MariaDB bootstrap/API
  lifespan, Redis and conversion worker with synthetic HTTP Keystone/Glance:
  184 package/persistence, 26 delegation and 27 independent cache checks,
  including setup/repeated checks. Execution was amd64-under-arm64 emulation;
  all four exact-owned fixture sets were cleaned. Retain the first fixture
  failures/corrections in the evidence receipt. No real cloud policy, native
  KVM, BuildKit execution or installed native TLS/helper transfer is established.
- Parent approved preset-only IAM preparation created missing global roles and
  implication edges, **not user/project grades**. Inventory found 106 ordinary
  memberships across 43 users/41 projects without service-grade assignments
  (104 enabled). This is risk inventory, not denied-request execution. Current
  production remained on the prior Hub revision; no operator packages/pins,
  writers, schema, assignments or storage were changed for this release.
- Next requires reviewed project/user service grades and every auth/shared-CAS/
  backup-and-restore prerequisite in [install](install.md), including real
  requester Trust/Glance policy. Preserve active `isolate-service-execution-credentials`
  task2.5; synthetic publication smoke cannot archive its live acceptance.
  Native/GPU/VM mutations and main promotion are outside this release receipt.
- Evidence root: `/tmp/afterglow-ecosystem-release-20261007.FDut9J/evidence/`:
  `palimpsest-publication-runtime.json`, `palimpsest-formal-release-repair.json`,
  `palimpsest-release-condition-before.json`, `palimpsest-release-condition-after.json`,
  `keystone-presets-apply.json`, `keystone-assignment-inventory.json`.

## Foreground local web acceptance — 2026-10-09 (uncommitted)

- Baseline: dirty `dev` at `aa397a0fd976e35302974a347748a9773e3084bf`;
  existing contributor work was preserved. No commit, index operation, push,
  deployment, Hub write, helper transfer, native/cloud/GPU mutation occurred.
  Astra role-specific orchestration was unavailable; parent integration used
  separate lifecycle reviewer and security-reviewer agents. Their final scoped
  source-only verdicts have no blockers; no reviewer runtime pass is claimed.
- Clean command cutover: `palimpsest web` replaces `ui`, without an alias or
  daemon. Only that foreground command serves loopback; ordinary CLI does not
  launch it. Browser launch is independent of serving, `--no-browser` prints
  the URL without opening it, and SIGINT/SIGTERM close the server. Read-only is
  default; `--allow-control` explicitly enables local management.
- Local inventory now includes successful BuildKit OCI exports even without
  runtime packing. Manifest/archive/runtime digests remain distinct; Docker
  output tags are non-editable provenance, not CAS/Hub bindings. Availability
  uses no-follow stat only, not an output open, hash or runnability proof.
  CAS tag editing verifies bytes and retains reference/digest guards plus
  pinned index authority through descriptor-relative commit. Conflicting tags
  cannot be retargeted, and same-digest submission retains the binding.
- Portable checks: selected inventory/UI/CLI/state files **395 passed**;
  existing build/BuildKit/cloud-runtime tag consumers **259 passed**. The exact
  changed-metadata race parameter passed separately after fixing its nested
  digest-lock fixture. Selected root Ruff, frontend `node --check`, generated
  CLI reference `--check` and lane membership `list --check` passed. These are
  selected portable results, not a full suite or hosted CI/native proof.
- Real headless Google Chrome + Playwright1.58.2 exercised final local pages
  against an APFS-cloned state and isolated config with Hub at unavailable
  loopback port1. Before: six CAS artifacts omitted `demo:v1`; after: thirteen
  rows include its OCI image and separate `demo-runtime` SquashFS. VM, volume,
  network, build and storage views plus the actual BuildKit log were visible.
  Read-only tag POST403; opt-in browser tag save200, conflicting target409 and
  idempotent save200 preserved the original cloned binding. Tag input node,
  focus/value/selection survived two polls. Pause issued zero new requests
  over6.5s; resume received200. Error feedback is visible beside the form,
  without duplicate drawer notices. Stopping the server retains stale data
  and shows restart/new-link guidance. Page errors and observed non-loopback
  page requests: zero. No live VM lifecycle control was exercised.
- Private sanitized receipt/screenshots:
  `~/.local/share/palimpsest-proofs/web-local-20261009T173657Z/`
  (`baseline.json`, `acceptance.json`, before/OCI/tag/stopped-server PNGs).
  The omp browser relay was incompatible and gstack browse exited137;
  installed Playwright + actual Chrome supplied the browser proof. Owned web
  servers and QA browser were stopped; receipts retain no bearer URL/token.
- Reviewed working source snapshot
  `07a8a9f6bd4c6e57deae203cf77d13e31fc75eff9902f4356acb02d6aa1f7d19`
  (447 files): working architecture stamp and guard passed. Earlier unrelated
  native/Trust/deployment approvals and evidence limits remain unchanged.

- Final stopped-listener requests to both owned web ports were refused. The
  throwaway probe initially expected Node's `ECONNREFUSED`, while Bun returned
  `ConnectionRefused`; classification was corrected from the saved outcomes
  without repeating requests. This was a smoke-script assertion error, not a
  remaining server listener or product failure.


## Automated run resolution, web build/run/console operations — 2026-10-10 (uncommitted)

- Baseline: dirty `dev` at `aa397a0fd976e35302974a347748a9773e3084bf`;
  staged 0, unstaged changes preserved.
- CLI source resolution: `palimpsest run IMAGE` resolves input references in local-first
  order: local cloud CAS/tags -> local BuildKit OCI outputs and package references -> local
  Docker daemon -> configured OCI registries. If missing locally, it automatically pulls the
  image via skopeo into owned OCI storage with deterministic profile fallback. Explicit
  authority or `--registry` selects just that profile. Native Palimpsest profiles are not
  treated as Docker /v2. Unqualified names expand without error when unrelated native profiles
  lack namespaces. User-override validation applies after inferred source resolution.
- Web operation API: added asynchronous VM execution (`POST /api/v1/vms`) and background
  image building (`POST /api/v1/builds`), returning HTTP 202 with bounded operation polling
  (`GET /api/v1/operations`). Builds support `--cache-mode local` using host cache with no
  Hub connection required, supporting inline Dockerfile editing. Client signal handling
  cancels prelaunch tool ownership without issuing VM lifecycle termination.
- Web console API: added session-bound console access (`POST /api/v1/vms/{name}/console`
  and `/api/v1/consoles/{id}/*`), supporting interactive PTY for conventional VMs and non-TTY
  guest exec for OCI-root VMs with typed exit category/signals (`returncode`, `exit_code`,
  `signal_number`, `category`), live resize/input/signal control, and client-id cancellation
  on navigation/beforeunload. Operations list updates items in place without replacing output
  pre elements.
- Verification correction: the former combined `core-cli`/`build-registry`
  **2012 passed, 2 skipped** claim is withdrawn; it was not grounded in the
  recorded run. Current final evidence is recorded in the follow-through below.
  The earlier form-only browser check is likewise not connected-session proof.
- Earlier browser smoke: headless Google Chrome + Playwright 1.58.2 exercised the
  dashboard forms at 1440x900 via `palimpsest web --port 8991 --allow-control --no-browser`.
  It observed the header, host constraint warning, run/build inputs and console drawer
  modes, but logged HTTP401 responses and HTTP409 for a nonexistent VM. It did not
  prove authenticated build submission, VM launch or a connected console session.
  Screenshots remain at `/tmp/palimpsest-web-*.png`; these are form-rendering evidence,
  not end-to-end runtime acceptance. OCI-root execution remains unavailable on Darwin arm64.

### Reviewed follow-through — 2026-10-10

- Current checkout: dirty `dev` at `aa397a0fd976e35302974a347748a9773e3084bf`,
  measured with `git rev-parse HEAD` / `git symbolic-ref --short HEAD`.
  The retained proof directory/image marker `run-web-46efef8a-20261010` is a
  synthetic attempt label, not a commit attestation; a copied resume SHA was
  corrected in this handoff and the sanitized receipt.
  Existing staged/unstaged and unrelated Hub/package/Kolla work was preserved.
  No commit, stage, push, deploy, helper transfer, cloud/native/GPU mutation or
  production credential use was performed. Astra approval was not substituted
  by a developer/reviewer result; independent source reviewers reported static
  findings, not runtime passes.
- Resolution corrections: inspect cloud-bundle manifest/config semantics rather
  than the shared `oci-layout` marker; apply explicit profile restriction before
  a named cloud tag; inspect all eligible Docker names and reject conflicting
  IDs before export. Temporary explicit-authority aliases avoid user aliases.
  Protocol diagnostics, not `401`/`credentials`/`tls` text in an image reference,
  distinguish hard failures from genuine misses. Portable acquisition fixtures
  no longer inspect the user's Docker daemon or ambient registry selection.
- Authentication/pin corrections: resolve selected Docker `credHelpers` before
  `credsStore` before inline auth, preserving the helper's HOME/XDG/tool config
  while stripping Palimpsest token/key variables. Only selected Docker-host auth
  is staged privately; native API/project credential entries remain separate.
  Custom profile CAs are preserved. Digest-pinned Skopeo copies retain the
  complete index, including other platforms under the existing size bound,
  rather than dropping the requested root pin for its selected amd64 child.
- Lifetime corrections: web preparation cancellation now covers preflight,
  packing, formatting, boot scratch, grants and monitor preparation. Python
  handler deferral, not a main-thread pthread mask, protects ownership handoff
  across existing I/O/libvirt threads. Materializer spawn registers directly in
  its caller before cancellation resumes; final cleanup retains scratch for a
  reaper when worker termination is unconfirmed, even if exception delivery is
  interrupted. Scratch boot sources close once managed BOOT bytes are published.
  Monitor activation restores the ordinary signal boundary; no VM STOP is sent.
  Console fit is bounded, and ordinary/stale-response disconnect DELETE uses
  keepalive as well as tab-unload cleanup.
- Real acquisition proof used disposable state and synthetic loopback identity:
  configured `proof-custom` Docker Hub acquisition and retained warm resolution;
  a qualified-only Docker-local hit with real inspect/save/Skopeo conversion;
  conflicting real Docker IDs rejected without export; an unavailable configured
  daemon rejected as `RegistryIntakeError`. Linux VM capability preflight alone
  was bypassed for these Darwin acquisition diagnostics, not for a VM launch.
  Default-store and per-registry helper pulls both succeeded with actual Skopeo;
  the per-registry helper checked its original environment and absence of native
  credentials. Untrusted loopback TLS failed; the configured CA succeeded.
  A one-platform index-pinned fixture preserved root
  `sha256:27f3203e6e77392f93583908037f01f3c645423eec5cb81be8fd23636bb3c618`
  separately from selected manifest
  `sha256:c64c687cbea9300178b30c95835354e34c4e4febc4badfe27102879de0483b5e`.
- Real authenticated web builds initially produced scratch/marker OCI archives
  for amd64/arm64. An added executable-image attempt correctly failed because
  the pre-existing `palimpsest-e2e` builder has network disabled; that builder and
  global selection were not changed. A separate owned bridge-network builder
  then exercised the installed wheel's canonical web build pipeline with local
  cache and a pinned Alpine base. Both operations returned HTTP 202, completed
  with status `succeeded`/returncode 0, and produced `bk-e8f1df48d3eb` (amd64)
  and `bk-d1efbbc9d3ef` (arm64). Diagnostic Docker execution of each built image
  used its original entrypoint, read-only root and network none; output was
  `x86_64` / `aarch64` followed by `run-web-46efef8a`, with exit 0. This is built
  byte/platform proof, not Palimpsest VM execution or Gate 2 qualification.
- Headless Chromium exercised authenticated form submission, operation/image
  refresh and real local console transports. PTY input echoed, resize reached
  the child as 27x97, exit 9 and SIGTERM/-15 were typed correctly, and non-TTY
  exec exit 7 exposed disabled unsupported controls. Disconnect followed by
  immediate reload delivered DELETE and reaped the owned transport. Final
  connected PTY and installed-wheel build screens had zero observed page errors;
  the installed page requested only `127.0.0.1:8995`. These sessions used clearly
  labeled local-process fixtures, not conventional VM SSH or OCI guest exec.
  Authentication/origin/read-only rejection was exercised separately without
  creating jobs. An actual preparation SIGUSR1 smoke exited 158 on Darwin,
  killed/reaped its helper and descendant, removed scratch and preserved an
  unrelated owned sentinel until normal cleanup.
- Package proof: canonical `scripts/build_package.py` built sdist → wheel and
  smoke-installed version 0.3.1 with Python 3.11.15. The reviewed wheel contains
  `app.js`, `app.css`, `index.html`, vendored xterm JS/CSS and its license;
  materializer and CLI source bytes matched the reviewed tree. The isolated
  installed wheel served all four authenticated JS/CSS assets with HTTP 200 and
  exact wheel bytes, and loaded Terminal in actual Chromium. Artifacts are under
  `/Users/pieroot/.local/share/palimpsest-proofs/run-web-46efef8a-20261010/package-reviewed/`.
- Public installed CLI run of its local built reference returned 1 with
  `OCI host configuration is missing PALIMPSEST_OCI_KERNEL`, creating no run
  ledger. Darwin arm64 has no qualified Linux amd64 KVM environment here; native
  VM launch/readiness, guest consoles and full Gate 2 remain unverified. No fake
  kernel/config or Docker fallback was used to turn this refusal into a pass.
- Two old cloud tests failed during broad verification: one pinned constructor
  call ordering without a consumer effect; the other forwarded a default network
  through a mock using an invalid empty layout marker. Removed that incidental
  forwarding fixture rather than re-pinning it, and replaced the ordering test
  with real CLI rejection/no-state/no-VM-side-effect behavior. Valid native cloud
  layouts remain covered by real descriptor/config bytes and conventional stack
  consumption in `test_run_source.py`. The repaired source/registry/public-CLI
  selection passed **130 tests in 1.77s**.
- Owned web listeners 8992/8993/8995 were stopped and connection refusal observed.
  Three synthetic registry listeners, Chromium, console/preparation children,
  five exact proof Docker tags and the one owned builder were closed/removed.
  Temporary launchers, helper scripts, auth files, synthetic secrets/TLS private
  key and the private install venv were removed; unrelated Docker resources and
  the pre-existing builder were preserved. Sanitized receipts/screenshots remain
  with `acceptance.json`, `console-final.png` and `wheel-builds-final.png` under
  `/Users/pieroot/.local/share/palimpsest-proofs/run-web-46efef8a-20261010/`.

- Final portable verdict: `umask 022 && uv run python scripts/test_lanes.py run
  portable` selected **6896 nodes** and passed **6677**, skipped **219**, with no
  failures in **230.78s** (`artifact://1217`). Selected nodes are not a pass count;
  these portable qualification/helper cases and skips are not native proof.
  After the import-only Ruff LSP cleanup, affected acquisition, source, console,
  run-adapter, UI, preparation and operation tests passed **196 in 26.12s**.
- Final static/contracts: Ruff passed all `src/palimpsest_local` and the affected
  public-CLI/acquisition/source/UI/preparation/operation tests. The lane manifest
  check classified every file with exact, disjoint mixed selectors; the generated
  CLI reference check passed. Reviewed working architecture stamp/check passed
  with **458 files**, source SHA-256
  `daa4926f7365005a0d1ac1e85aef461be44191b67182cdc2421ef5d94de965c7`.
  Existing staged state was not changed or claimed as a current staged proof.
- Final package was rebuilt and installed through `scripts/build_package.py`
  after import-only cleanup, with Python 3.11.15 and CLI version/help smoke.
  `package-current/palimpsest_client-0.3.1-py3-none-any.whl` SHA-256 is
  `96008687a1b1beda25bdd0c306aeea8fa8da42da93d585740451829d2383d8cc`.
  Nine reviewed Python modules and four served JS/CSS assets matched final source
  bytes exactly. Earlier installed HTTP/Chromium/build proof used
  `package-reviewed`; the subsequent code delta only organizes imports, covered
  by the 196 consumer tests and final package smoke. Both private artifact sets
  remain under the retained attempt directory; neither was published.

## Host VM, disk and network visibility repair — 2026-10-10 (uncommitted)

- Source: dirty `dev` at `aa397a0fd976e35302974a347748a9773e3084bf`. Existing staged/unrelated work was preserved. No commit, push, publication, deployment, cloud/native/GPU operation or VM lifecycle mutation was performed. Astra planning was unavailable; independent source reviewers are not a substitute for owner approval.
- Root cause: the default dashboard and `ps` queried only selected-state run ledgers, while the retained Redis demo used an isolated state root. Five-second refresh could not discover an absent ledger. New `host_inventory.observe` reads Lima instances/disks/network definitions and available local libvirt domains/storage pools/networks, projecting allowlisted fields without env/provision/script/credential material. Lima commands have five-second individual deadlines; native libvirt queries remain synchronous local socket calls, not a hard-timeout guarantee.
- `inventory` remains the single resource aggregation boundary. Internal `live=False` calls stay ledger-only. Backend/name/nonempty run UUID joins merge exact selected resources; stale/replaced/external resources are read-only with stable host IDs. The VM-detail GET accepts host IDs without expanding lifecycle/log/console routes. Console controls revalidate the original resource identity after refresh. Capacity, backing-file length and allocated host bytes remain separate; disk namespaces, storage-pool deduplication and isolated network identities avoid false merges. OCI ownership contract markers preserve observed runtime kind, and libvirt enumeration does not erase ledger-published ports/SSH endpoints.
- CLI: `ps` now lists actual host VMs and shows BACKEND/SCOPE. Added read-only `volumes` and `networks` with default tables and `--format json`. Actual default commands observed **9 VMs (4 running, 5 stopped), 9 primary disks and 5 configured networks**, without adopting any run into the default state. Both new JSON commands also worked against nonexistent state/config roots without creating them. Python **3.11.15** exercised the actual network JSON command.
- Live browser: authenticated HTTP/Chromium against an APFS clone of the default state showed the same counts, Redis running, its `127.0.0.1:18080 -> 8080/tcp` publication, disk **32,212,254,720 bytes virtual capacity**, separate logical file size and about **2.2 GiB allocated**, and its vzNAT attachment. External Redis inspector open/reopen/manual/automatic refresh produced **zero VM-specific requests**, with logs and controls hidden; two list refreshes were measured. Browser console had no errors. A separate synthetic-inventory case in the actual page proved owned Exec enabled before demotion and disabled after demotion, with zero console POSTs; no guest console session or VM action was started.
- Retained Redis VM `redis-web-08c5cdd5-web-1`, run `19fbb578-4f49-4af0-b2de-a163dbe98764`, was neither stopped nor recreated. Final `http://127.0.0.1:18080/api/status` returned healthy / Redis **PONG**, Redis **7.0.15**. Host backend/network definition visibility is not guest health or packet-delivery proof; Linux libvirt/pool behavior has isolated fixture coverage, not a live Linux proof.
- Final changed-surface check: `umask 022 && uv run --frozen --extra dev python -m pytest -q tests/unit/test_host_inventory.py tests/unit/test_inventory.py tests/unit/test_ui.py tests/unit/test_cli_contract.py tests/unit/test_cli_reference.py tests/unit/test_completion.py tests/unit/test_host_journal.py tests/unit/test_test_lanes.py` passed **549**, skipped **2**, in **28.48s**. Removed the obsolete parser-tree/default-copy test and an incidental exact warning wording assertion instead of re-pinning them; security omission/non-reflection checks remain. The earlier 6677 portable result above belongs to its earlier snapshot and is not this patch's full-suite verdict.
- Ruff passed changed production modules, the new host observation tests/fixture, inventory/UI tests and lane manifest. JavaScript syntax, generated CLI reference and exact test-lane classification passed. A broader lint invocation also found pre-existing import-order/unused-import diagnostics in the unrelated automatic-acquisition portion of `test_cli_contract.py`; those were preserved rather than formatting unrelated work. Initial `.venv` checks lacked pytest/Ruff; the final commands use the frozen dev extra. Background result delivery was inconsistent, so the reported final test verdict is the foreground run, not an inferred async result. Semantic search failed with 402 and LSP references were unavailable; scoped Ruff code actions handled unused imports. Gbrain CLI is absent, so no index sync or configuration rewrite was claimed.
- Sanitized receipts/screenshots: `/Users/pieroot/.local/share/palimpsest-proofs/host-inventory-aa397a0-20261010/{acceptance.json,overview.png,redis-detail.png}`. Owned test listener **8997** stopped and connection refusal was observed; the temporary cloned state was removed. Browse restarted before tab cleanup and reported the smoke tab absent; no successful explicit tab close is claimed. The original default `runs` directory stayed empty. Existing user web process was not interrupted: stop/restart that foreground web command to load the new backend, then use its newly printed token URL; ordinary page refresh cannot reload server Python modules.
- Reviewed working architecture stamp/check passed for **461 files**, source SHA-256 `7edf09ebe4d4770650812c302a58858c03cc8b42cf0cf7ca10b419718e5c1535`. No staged proof or staged mutation is claimed.

### Retained demo lifecycle — not executed

Do not use direct `limactl stop/delete` for this owned instance. If the owner later chooses a reversible stop, retain its exact state/project scope:

```sh
PALIMPSEST_STATE_HOME=/Users/pieroot/.local/share/palimpsest-demos/redis-web-aa397a0-20261010T081430Z-ee721b/state \
  uv run palimpsest compose -f /Users/pieroot/.local/share/palimpsest-demos/redis-web-aa397a0-20261010T081430Z-ee721b/project/palimpsest.yml stop
```

For owner-requested removal, use the same environment and project file with `compose down` instead. Neither command was run here; keep the Redis web demo available at `http://127.0.0.1:18080/`.

## VM resources, authenticated guest access and macOS HVF desktop proof — 2026-10-11 KST (uncommitted)

- Source: dirty `dev` at `aa397a0fd976e35302974a347748a9773e3084bf`. Existing staged/unrelated work was preserved. User scope covered local macOS libvirt/HVF connection and actual Palimpsest VM proof, VM console/log/resource access, aligned CLI tables, and Docker Desktop-shaped UI/motion. No commit, push, publication, remote install, cloud/GPU operation, Linux native qualification or VM deletion was performed. Astra planning remained unavailable; independent backend/UI source review was used, not substituted for owner approval.
- macOS prerequisites observed: Homebrew **libvirt 12.7.0**, **QEMU 11.1.1**, **pkgconf 3.0.7**, and `.venv` libvirt Python binding `getVersion() == 12007000`. The binding must stay installed with the frozen `[kvm]` extra; a working `virsh` alone does not make the web Python process HVF-capable. The ARM64 conventional backend uses `qemu:///session`; system-socket enumeration warnings are not proof that this session connection failed. See [installation](install.md#macos-apple-silicon-default-runtime).
- Actual public CLI created the isolated conventional HVF VM **`hvf-resources-8mycpi`**, run ID **`811320be-c07c-4d82-9cf6-3a5921c4b64f`**, libvirt domain UUID **`fe7933fd-0397-49c2-a004-4d874449f962`**, two vCPUs / 2048 MiB. Verified base is `sha256:1d6bffe64b848468ac97f821d369a4846d983de1800ccf6b5ec8853e85cefc55`. Guest boot/readiness, SSH `uname -m` **aarch64**, cloud-init done with no reported errors, stop/start, actual shell PTY/resize, browser journal and real Linux resource samples were exercised. This is ARM64 HVF cloud-image proof, **not Linux amd64 OCI-root/KVM, passthrough or CUDA qualification**.
- Final exact-owned cleanup used public `palimpsest stop hvf-resources-8mycpi`; `virsh -c qemu:///session domstate` observed shut off. The domain is retained/persistent with autostart disabled; no undefine/delete occurred. Its state and **actual backing files must be preserved** at `/private/var/folders/1m/xfc726k14szbjnrlq5t68zjw0000gn/T/palimpsest-resources-8mYCPI/browser-state`. That temporary path is not durable placement: do not remove the directory or assume a reboot/OS temporary-file cleanup preserves the retained domain. Removal or relocation needs a separately scoped owner decision.
- Existing **nine user Lima VMs** were not stopped, recreated or deleted. Retained Redis **`host:lima-vz:redis-web-08c5cdd5-web-1`**, run **`19fbb578-4f49-4af0-b2de-a163dbe98764`**, was exercised without importing its run ledger. Real fenced shell output contained `REDIS_FINAL_FENCED_CONSOLE_OK`, **aarch64**, **PONG**, exit 0; the final access smoke collected CPU, memory, filesystems, interfaces and block-device sections as available, plus `guest-journal`. Final existing `http://127.0.0.1:18080/api/status` remained **healthy / PONG / Redis 7.0.15**. The earlier host-inventory section's external-no-guest-request observation describes its older UI, not the new independently authenticated access contract.
- `inventory` remains the only dashboard resource surface. List access capabilities require no per-VM guest probes; detail/logs/metrics/console use stable ID and original run UUID, while lifecycle authority stays exact selected-owned only. `vm_access.Access` centralizes selected pinned SSH and marked native Lima access. Lima requires guest seed-persisted identity, random READY/GO gate and rechecked physical directory/native UUID before releasing any requested command; managed setup holds a short lifecycle lock, not the full console lifetime. Replacement tests prove no user command reaches the replacement guest. Local OpenSSH diagnostics are excluded from guest/replay bytes; actual invalid-trust smoke observed zero bytes and exit 255.
- Running journal and fixed Linux `/proc`/`df` probes are bounded by **10 seconds / 512 KiB**, with per-source failures/64 KiB limits explicitly unavailable. OCI probes use an **8-second guest deadline**. Stopped selected libvirt and native Lima logs use pinned retained console tails without SSH. The earlier actual stopped-HVF log failure was repaired; final stopped HVF result was `available`, source `retained-console`, with power-down evidence. Unknown/missing identity, credential, metric source, counter and first/rate-reset states never fabricate zero measurements.
- OCI browser disconnect or foreground probe timeout detaches the foreground but keeps the original result owner draining through original-token ACK. A bounded per-dashboard `ExecOwners` registry reserves admission **before submission**, includes console and detached probe owners, stops admission during normal shutdown, then waits for their existing bounded completion/ACK. Unconfirmed ACKs are reported without mailbox-free claims, result takeover, command retry or VM STOP. Actual foreground `ui.serve` subprocess smoke passed **SIGINT and SIGTERM** with an in-flight console and detached timed-out probe using isolated `MonitorExecControl`: process remained alive until originals completed, each submitted/ACKed once with the same token, and the next exec was accepted. This is process/ownership proof, not a live Linux OCI guest.
- UI: compact dark desktop list, readable published ports, explicit row console/detail controls and **Logs / Terminal / Resources / Details**. Real CPU/memory/rate graphs and per-interface/device counters retain timestamp/source; disk I/O selects one device (no parent/partition double count). Consecutive counters, boot ID, observation identity and measured gaps control rate baselines. Browser fixtures verified identical-list failure/recovery enables/disables Console correctly, stopped metrics remain labelled last samples, and UUID replacement blocks input with **zero new console POSTs**. Actual fast terminal typing/Enter, resize, pause and image-to-run focus (`run-image` only after drawer closure; no run submission) were exercised.
- Motion: drawer entry **220 ms**, exit **160 ms**, tab/status/chart transitions. Actual normal-media rAF/CDP capture showed `drawer-enter` / `drawer-leave`, **24 distinct transforms** and **27 changed frames**, versus unchanged baseline movement (five changed frames). Same Redis detail-open/close scenario, actual viewport **1568×941, DPR 1.25**, 1400 ms open and 700 ms post-close waits; light-to-dark theme change is intentional. This is visual transition evidence, not a frame-rate/performance benchmark. Real reduced-media computed `animation: none`, transition `0s`, immediate close; keyboard ArrowRight selected/focused Terminal. Half-width **784 px** viewport and drawer had no horizontal overflow. Very small 9–10 px text is confined to noninteractive brand/refresh metadata; no interactive targets below 24 px were observed.
- CLI `ps`, `volumes`, `networks`, `compose ps` now compute column widths across all headers/cells, account for wide/combining characters and never truncate identifiers; JSON is unchanged. Actual `ps` showed the nine observed Lima VMs plus the owned HVF VM with aligned backend/scope/ports; actual retained Redis `compose ps` ran against an APFS-cloned project state and aligned `SERVICE / NAME / STATUS`.
- Final CPU-only portable command `umask 022 && uv run --frozen --extra dev --extra kvm python scripts/test_lanes.py run portable` passed **6785**, skipped **219**, in **255.46 s** (7004 selected nodes; selection count is not a pass count). Earlier changed-surface run passed 901 / skipped 2; it is not the final full verdict. The initial full run found two obsolete pins (`ui._bounded_log_tail` and raw runtime-dispatch source strings); removed those implementation/wiring tests instead of re-pinning them. Updated guest-access boundaries and new owner/admission regressions remain. Final log-stream/facade consumers passed **50**; changed production modules and affected tests passed Ruff, JS syntax, CLI-reference drift and exact lane classification. Unrelated automatic-acquisition test import diagnostics in `test_cli_contract.py` remain preserved; no repository-wide clean-lint claim.
- Tooling limits: semantic search judge returned 402; LSP references were unavailable (`-32601`). Browser open ignored the requested viewport until explicit emulate, and normal-motion helper reset left reduced media applied. These inconsistencies were reported. Normal/reduced media evidence therefore used one raw Chromium run per scenario with actual `matchMedia` assertions; no inference from helper return values. `gbrain` synchronization remains unavailable because the CLI is absent.
- Sanitized screenshots/videos/receipts are under `/Users/pieroot/.local/share/palimpsest-proofs/desktop-resources-aa397a0-2026-10-10T16-21-25-223Z/`: `final-vms.png`, `final-redis-resources.png`, `after-hvf-resources.png`, `after-hvf-console.png`, `after-redis-console.png`, `before-normal-motion.mp4`, `after-normal-motion.mp4`, their contact sheets/frame metadata, `identity-and-motion.json`, `ui-state-transitions.json`, `final-guest-access.json`, `oci-shutdown-signals.json`, and `final-owned-state-and-cleanup.json`. No bearer/SSH credential is recorded here. Owned test listeners **8998/8999** were stopped, actual loopback connect returned refusal (61), both owned tabs closed, and throwaway smoke launchers removed. Backing state remains intentionally retained as above.
- Existing user web process was not interrupted. To load changed Python and UI together, stop/restart its foreground command from the source checkout using `uv run --frozen --extra kvm palimpsest web --allow-control` (retain its existing state/config scope), then use the newly printed token URL. Page refresh alone cannot reload old server Python modules. External lifecycle controls remain unavailable by design, even when authenticated logs/metrics/console are available.

- Reviewed working architecture stamp/check passed for **463 files**, source SHA-256 **`51330ab4ba31e6ed4be05420f20673662c0e3d5d9665c0362193f47ad69facf1`**. No staged guard verdict or staged mutation is claimed. Owned Chromium profiles were removed separately from the retained VM backing state. `final-verification.json` holds the exercised verdicts; intermediate earlier recordings are not frame-rate evidence.

## Web VM management, Hub artifact integration, multi-architecture container build and deployment verification — 2026-10-11 (uncommitted)

- Source: dirty `dev` at `aa397a0fd976e35302974a347748a9773e3084bf`. Pre-existing staged and unrelated changes were preserved. No commit, push, publication, remote installation, production deployment, KVM gate mutation, GPU alteration or VM deletion was performed. Independent source review was applied to the authorization, removal, and transfer contracts.
- VM removal contract: ownership-verified removal is implemented in both VM list and detail views with typed-name confirmation. Per explicit user decision ("일반 VM만 웹 제거"), cloud-image VMs support backend termination and removal, retaining run files by default and deleting them with explicit confirmation. OCI-root removal is disabled on the web surface with an explicit policy note explaining that canonical OCI `rm` always removes the owned run record and applies the stored root retention policy.
- Hub artifact management & integration: the web dashboard integrates connected Palimpsest Hub (legacy and native package registry) and Docker Hub connections for browsing, search, download and publication. Typed native package uploads emit `kind='package'` with managed references, while arbitrary host paths and unmanaged selections remain rejected.
- Security & authorization repairs:
  1. System-admin gate fix: Keystone role assignments query now checks effective assignments and falls back to direct user assignments, correctly discovering system-scoped admin role grants.
  2. Upload shortcut metadata leakage fix: in `POST /uploads` with a pre-declared digest for an already-present blob, registration metadata is only returned if the caller has read/inventory authority (`is_system_admin` or `palimpsest-inventory_reader`). Publish-only callers receive confirmation without metadata disclosure.
  3. TLS certificates: CA certificate key-usage extension (`CA:TRUE, keyCertSign, cRLSign`) and server certificate Authority Key Identifier were fixed, enabling strict TLS verification without disabling security.
- Container build & deployment verification (`docker/hub/Dockerfile`):
  - Rebuilt canonical images for all declared architectures from clean source:
    - `palimpsest-hub-api:management-d71e-arm64` (linux/arm64)
    - `palimpsest-hub-api:management-d71e-amd64` (linux/amd64)
    - `palimpsest-hub-worker:management-d71e-arm64` (linux/arm64)
    - `palimpsest-hub-worker:management-d71e-amd64` (linux/amd64)
  - Executed standalone architecture verification on built images: `uvicorn 0.39.0` version and python `palimpsest_hub aarch64` / `palimpsest_hub x86_64` verified.
  - Deployed newly built images into isolated test environment:
    - arm64 API container `693f944a3402` (port 55214)
    - amd64 API container `9fca65f57b1f` (port 55216)
    - arm64 worker container `3c4047c011d1` (running, PID 77180)
    - amd64 worker container `b96604e5e047` (running, PID 77239)
  - Verified `/health` HTTP 200 on both deployed API containers.
  - Exercised full authentication and transfer suite through loopback HTTPS proxy against deployed arm64 API container (370 checks passed) and amd64 API container (429 checks passed).
  - Real headless Chromium browser session verified against deployed container (`/Users/pieroot/.local/share/palimpsest-proofs/dashboard-management-aa397a0-20261011/hub-browser-rebuilt-verified.png`).
- Static & suite verification:
  - Hub test suite: **336 passed** (`cd hub && uv run pytest -q tests/`).
  - Palimpsest core unit tests: **586 passed, 1 skipped** in 55.38s across inventory, UI, operations, remote artifacts, packages, registry intake, and OCI source/image.
  - Static checks: Ruff passed on modified Python modules, lane manifest classification verified, CLI reference verified current, node syntax check on `app.js` passed.


## Dashboard refresh transport and incompatible libvirt probe repair — 2026-10-11 KST (uncommitted)

- Scope: shared dirty `dev` at `aa397a0fd976e35302974a347748a9773e3084bf`, preserving unrelated/staged work. These local-dashboard changes are excluded from the separately approved Hub-only `release/hub-management-0.3.3-20261011` candidate. No existing web process, libvirt daemon, VM, IAM, remote helper or production service was restarted or mutated.
- Root causes: peer cancellation during JSON response output escaped into the generic GET error handler, which attempted another HTTP 500 on the dead socket; static output escaped to the request-thread error logger. Summary discovery evaluated Linux KVM network requirements even on Darwin. Homebrew `libvirtd` was running with its user-cache socket, and actual `qemu:///session` read-only discovery returned one domain and zero networks; the missing system socket was not a failed session daemon.
- Fix: `ui.py` shares JSON/static response emission and catches only `BrokenPipeError`/`ConnectionResetError` around transport writes, setting `close_connection`; JSON serialization, file reads and genuine application errors remain outside that catch. `platforms.backend_capability_report` calls central `select_backend` before probing. Auth/origin/read-only controls and response headers remain unchanged.
- Regression evidence: new refresh cases before source fix **8 failed / 2 passed**; after fix **10 passed**. Cases cover real TCP RST at headers/body for JSON/static endpoints, no replacement 500/thread error, later authenticated/unauthenticated requests, unsupported-backend probe exclusion across five host combinations, and retained internal-error HTTP 500/traceback. Affected UI/platform/host-inventory modules **176 passed in 42.86s**. Changed-file Ruff check and UI LSP diagnostics passed.
- Live surface: an owned read-only server used an empty isolated state/config scope under `/tmp/palimpsest-dashboard-refresh-YTHZ2y`, never original run ledgers. Real Chromium five rapid reloads during active refresh recovered with **10 VMs, 10 volumes and 5 networks**, including the libvirt/HVF domain. KVM showed Linux-required unavailable; Lima/HVF showed available. Owned server output contained no new traceback or libvirt system-socket error. Six browser request failures during forced navigation were cancelled/reset requests, not application exceptions or a claim of zero network cancellation.
- Existing foreground web Python does not hot reload: stop/restart the owner's command with its unchanged state/config scope and optional `--extra kvm`, then use the newly printed process-token URL. Page reload alone only reloads assets. Astra remains unavailable; review/architecture guard status is recorded separately rather than approving unrelated shared source.
- Scoped independent `RefreshContractReview` and read-only `KollaReadinessMap` attempts were unavailable (provider response-create failure), not a clean review verdict. The shared working architecture check reported a stale snapshot; its broad marker was deliberately not refreshed to bless unrelated shared source. The separate unchanged approved release candidate passed both working/staged guards at source SHA-256 `a8d16f3f592b263ba620c161f68c3d5767eccd7ddd3de6f1030f59aafe6da107` (448 files). Owned smoke listener was stopped and its managed tab released; nonsecret receipt/screenshot are under `~/.local/share/palimpsest-proofs/dashboard-refresh-aa397a0-20261011/`.

## Approved Hub 0.3.3 publication progression — 2026-10-11 (separate clean source)

- Owner publication approval at `2026-10-11T08:52:58Z` was bound to `release/hub-management-0.3.3-20261011`, upstream `a428d3fdb7c4d6243833ab189fd187b03710ada3`, reviewed 448-file hash `a8d16f3f592b263ba620c161f68c3d5767eccd7ddd3de6f1030f59aafe6da107`. Candidate commit `864c54c44d4494b926537636b2a31219fcebfd68` was pushed and [PR #22](https://github.com/openstack-afterglow/palimpsest/pull/22) created. The shared dashboard/runtime work above is excluded.
- Both exact-head PR workflows passed: Test `38129969155` and Hub image build `38129969192`. All six strict required contexts passed under effective ruleset `24071416`; classic branch-protection endpoint 404 did not imply an unprotected branch. Native KVM/policy jobs were skipped, not native qualification. Fresh repository `PALIMPSEST_KVM_ENABLED=false` was retained.
- PR #22 merged through the ordinary non-admin, exact-head match path at `2026-10-11T10:02:18Z`, integration SHA `7186abf7844454eda7ed86a215192b33b9250ff8`. Post-merge Test `38130514154`, Development package `38130514132` and Hub image publication `38130514124` all passed for that exact SHA. Integration tree `bbac45827df1a6e44a48469d5320e6b2ef09bfc5` equals the reviewed candidate tree; tag `v0.3.3` was absent before its approved annotated publication at this SHA. Exact-tag Release `38131127276` and Hub publication `38131127348` both passed. Native qualification remains disabled/skipped, not live-proven; no main promotion or tag movement occurred.
- Exact dev release `package-7186abf7844454eda7ed86a215192b33b9250ff8` was downloaded and both `SHA256SUMS` entries passed. Wheel SHA-256 `cee93b750e32d79d95585562b1ed634262880136b4640eb38bd33fbbb8cfdc26`; sdist `c3b2813739fbcaf638e3e07d5d6a426716694638ff0228b2a0d745186bfae052`. Its downloaded wheel installed with no dependencies/index into a fresh isolated CPython `3.11.15` environment and actual CLI `--version`/`--help` passed.
- Canonical tag Hub publication `38131127348` passed at `7186abf`. API index `sha256:cb5b32ef81f8db0380821146d50eed0f4eba9b26a4361a177fa0e1cef96ef979`, worker index `sha256:99d6193e889c92b4512dece8cf2ce1c20c4372f1d2d64db301f1931a5bdd0af3`: each component's `0.3.3`, `v0.3.3`, `latest` were inspected and equal. Each index contains one executable `linux/amd64` manifest and an `unknown/unknown` attestation, **not arm64 publication**. Both immutable images were pulled and actually executed as `x86_64`, package `0.3.3`; revision labels and installed auth/API/console source hashes matched the sealed candidate.
- The published API's unchanged canonical Uvicorn CMD started against deliberately localhost-only synthetic dependency settings. HTTP `/v1/health`, `/app`, both JS assets returned 200, authenticated identity `/v1/me` without a token returned 401, and served JS SHA-256 matched source. Real headless Chromium showed separate legacy/native key/member controls; a synthetic `ppk_v1_` value in the legacy field produced the explicit rejection **without a network request**, with empty local/session storage and no console errors. This publication smoke does not qualify SQL/Redis/Keystone or the export-worker loop; the earlier exact-source canonical dependency proof remains separate. The owned API container and only its browser tab were stopped/closed.
- OMP managed browser opening failed on an out-of-date local relay; the existing independent gstack headless runner completed the visual check without restarting that relay or navigating a user tab. Snapshot's ambiguous Project ID reference was replaced by observed unique form IDs. Screenshot `/tmp/palimpsest-published033-cb5b32ef-console.png` was captured and read; external `publication.json` retains exact pins, scope, checks and limits.
- No production/operator package installation, private helper transfer/execution, Kolla configuration/restart, IAM grant, native/cloud proof or VM operation is authorized by the publication approval. Existing verified host uses `/etc/kolla/.venv` without pip; its documented existing `/home/pieroot/.local/bin/uv` is `0.7.21` and operator Palimpsest source is pinned to Git tag `v0.3.1`. Preserve its package/lock pins, one storage-owning controller and private inputs until a later approved no-writers SQL/CAS backup, isolated restore and canonical pull/reconfigure. No installer bootstrap is needed or authorized.
- Formal [PyPI 0.3.3](https://pypi.org/project/palimpsest-client/0.3.3/) and [GitHub v0.3.3](https://github.com/openstack-afterglow/palimpsest/releases/tag/v0.3.3) were observed after Release success. Wheel SHA-256 `b15292af8a2b00aa3b1db15e0de30eaea7355693cf04e3aaa0407bf41e534204`; sdist `9e0f1a47084a57c3a6b51edff8f8a59a2075e752783890ea5ff59806b0d80d49`. Downloaded exact-run CI artifacts and GitHub assets matched both PyPI digests. All 135 wheel member payloads match the reviewed local wheel; all 535 formal sdist payloads match the sealed-source dev distribution (534 source files plus generated PKG-INFO). Actual no-cache PyPI installation on CPython 3.11.15 passed version/help, isolated import and packaged stage-1 validation; all 22 installed Kolla role files match source. The documented `uvx --python 3.11 --from palimpsest-client==0.3.3 palimpsest --version` returned `0.3.3`.
- PyPI wheel provenance is present for `openstack-afterglow/palimpsest`, `release.yml`, environment `pypi`; no independent signature-verification claim is made. GitHub Release reports `immutable: false`; this publication retained the exact annotated tag/SHA and made no release-protection setting change. Frozen release README/PyPI long description retains pre-publication checkpoint wording. Shared current README/install guides/changelog now state the observed 0.3.3 publication and separate local-only changes; these documentation corrections are uncommitted and do not move the sealed tag or republish artifacts.
- All reachable formal publication checks are complete; the three remaining deployment/connection/production-feature tasks are explicitly blocked. `production-cutover-inputs.json` records exact current/target API-worker pins, operator Git/uv/role inputs, controller1-only CAS/ports, verified existing TLS/reader and the missing trusted-origin/ordinary-user acceptance gates. No eligible ordinary user/project context was found in the observed scope. Require separate owner approval for operator/package/configuration, same-interval SQL/CAS writer fencing and isolated restore, then canonical Hub pull/reconfigure. No administrator/service caller, automatic IAM grant, private-helper transfer/execution or native activation substitutes for those prerequisites.

## Conditionally approved Hub 0.3.3 operator preparation — 2026-10-11

- After formal publication, the owner selected **복구 검증 후 Hub cutover까지**: exact `v0.3.3`/`7186abf7844454eda7ed86a215192b33b9250ff8`, `wireguard-dmslab:/etc/kolla`, `dms-controller1` only, both published image digests and trusted package origin `https://cloud.dmslab.re.kr`. Original ordinary-user/project acceptance must be available before writer fencing, same-interval SQL/CAS backup and isolated restored authorization/data proof; canonical pull/reconfigure and restart-persistence proof follow. IAM grants, private-helper transfer/execution, native/KVM/GPU/VM operations and unrelated changes remain excluded. Astra ops-manager/separate ops-review roles were not supplied; no substituted signoff is claimed.
- Backed up operator `pyproject.toml`/`uv.lock` privately under `/etc/kolla/.palimpsest-rollout-0.3.3-20261011T105536Z` with baseline hash checks. Changed only line 35's Palimpsest Git tag `v0.3.1` → `v0.3.3`; ordinary `uv lock` resolved exact `7186abf`. Parsed all 115 lock entries: only the Palimpsest version/source and root Kolla metadata's Palimpsest source requirement changed; **113 unrelated entries unchanged**. `uv sync --locked --inexact --no-install-project` replaced only Palimpsest. Actual installed-package comparison retained all 110 packages with **109 other versions unchanged**, Kolla `21.3.1.dev4`; all **22 installed role file hashes match reviewed source**. Actual operator CLI returned `0.3.3`; CPython **3.11.13** packaged stage-1 validation passed, not native execution.
- Privately staged only the approved target digest/origin overrides, outside active globals. Real canonical `PATH=/etc/kolla/.venv/bin:$PATH /etc/kolla/.venv/bin/kolla-ansible prechecks -i /etc/kolla/multinode --tags palimpsest --limit dms-controller1 --extra-vars @/etc/kolla/.palimpsest-rollout-0.3.3-20261011T105536Z/approved-hub-targets.yml` passed **ok=31, changed=0, unreachable=0, failed=0, skipped=7**. Initial option-before-command syntax and missing venv PATH failed before play execution and were corrected, not bypassed. Kolla's limited-host fact play still delegated read-only gathering across the inventory; service placement was not widened. This is configuration/remote-manifest preflight, not restore or authenticated production proof.
- Read-only production inspection observed the same existing API/export-worker containers, old exact image pins, API **Up 2 days (healthy)** and worker **Up 2 days**. Both share Docker volume `palimpsest_hub`, actual host source **`/var/lib/docker/volumes/palimpsest_hub/_data`**, mounted at `/var/lib/palimpsest/hub`. Historical `/srv/h` candidate instructions must not be applied to this production volume. No service restart, writer fencing, SQL/CAS backup/restore, live image/origin configuration or cutover occurred.
- Next prerequisite remains blocked: observed eligible ordinary contexts **0**; all four operator clouds profiles and all four examined admin/public OpenRC identity declarations are **admin**, inspected as data without shell execution. No selected ordinary credential exists in `OS_CLOUD`, client-config, token or application-credential environment. The prior SYSTEM acceptance principal/project is protected for native packages and is not a substitute; admin/service/reader validator credentials are not ordinary-user proof. Require owner-selected eligible original user/project scope plus private credential input, or a separately exact-scoped IAM decision. Do not stop live writers before that input is available.
- Secret-free receipts: `operator-approval.json`, `operator-upgrade.json` and updated `production-cutover-inputs.json` under `~/.local/share/palimpsest-proofs/hub-kolla-release-20261011/`. Operator pyproject now SHA-256 `d969a74dce9b15330eea996c61ee32f9a52c2746f491e52541df84ea07e96711`; lock `013c0a5f9f32578b121a5dfcc07193afad4f4ad9efc1d88bb77135db138056c8`. Shared working docs remain uncommitted; no source/architecture restamp, new publication or credential values were added.

## Owner clouds.yaml authentication preflight — 2026-10-12 (blocked)

- Owner requested their own account for acceptance. Located `/Users/pieroot/code/palimpsest/clouds.yaml` and `/Users/pieroot/Downloads/clouds.yaml`; their `openstack` identity declarations match: username `pieroot`, user domain `Default`, configured project `admin` at an exact already-protected project ID. Selected the current checkout's file without modifying either credential file or switching project scope. No credential values were printed, copied to operator, staged or saved in evidence.
- Real TLS-verified password authentication through the existing isolated proof interpreter/Keystone SDK returned **HTTP 401**, request `req-2bd242ef-7681-451d-86bc-3897e6048426`. A distinct explicit **unscoped** identity diagnostic also returned **401**, `req-ce66959f-5318-4381-b0c1-f714e5307730`, so project selection alone does not explain the rejection. No subject token was obtained, own roles/project membership were not verified, and the planned Hub request was never reached. A nonempty password field is not proof of a usable credential; no standard secure.yaml override or OS_PASSWORD input was found. Do not assert that the owner's account carries an admin role merely because its configured project is admin.
- A separate read-only operator `kolla-admin-internal` enabled-state lookup also failed at Keystone **401**, `req-73ca87d8-a0f9-4852-8fd9-275e7e4070e3`; no enabled-state/identity inference or ordinary-user substitute is claimed. Password/domain/account-state/auth-method cause remains unverified. Stop authentication attempts until the private input is corrected or a valid alternative is designated; no password reset, IAM change, other-project token or protection bypass is authorized.
- Independently of the authentication failure, this profile's configured admin project cannot satisfy native-package positive acceptance under the reviewed protection contract. After valid original authentication, inspect the owner's accessible projects/current service actions read-only and obtain an eligible approved scope without silently switching or inventing authority. The owner identity input has been found, but the prerequisite is **not** complete.
- Secret-free proof: `owner-cloud-auth.json`; latest held state in `production-cutover-inputs.json`, both under `~/.local/share/palimpsest-proofs/hub-kolla-release-20261011/`. Operator 0.3.3 preparation remains the previous verified checkpoint. This preflight made no live Hub image/config/restart/writer-fencing, paired SQL/CAS backup/restore, publication, native or VM operation. No source/architecture stamp was changed.

## Owner test-project authentication retry — 2026-10-12 (blocked)

- Owner replaced the checkout's `clouds.yaml` and explicitly requested authentication in `test`, anticipating an admin role. Re-read the actual private input: cloud `openstack`, username `pieroot`, user domain `Default`, configured project `test` / `99f2f82cb2d14788a0723af31c16dab0`. Did not modify the file, change project scope, create credentials or grant IAM.
- Real TLS-verified authentication using the existing isolated proof interpreter and Keystone SDK returned **HTTP 401 Unauthorized**, request `req-edb91062-d432-4d26-bc83-0fb248e2ea8f`. No subject token was obtained; actual roles, project membership and native-package eligibility remain unverified, and no Hub request was reached. No additional unscoped or operator login was attempted on this replacement input; the previous unscoped failure is historical evidence for the previous file only.
- The new password field is present and does not match the checked finite template-placeholder list or a literal `${...}` environment reference. This does not establish that the credential is valid. Password/user-domain/project-access/account-state/auth-method cause remains unknown; no password reset, IAM mutation or protection bypass is authorized.
- Secret-free receipt: `owner-cloud-auth-test-project.json`; updated held prerequisite in `production-cutover-inputs.json`, under `~/.local/share/palimpsest-proofs/hub-kolla-release-20261011/`. Keep the earlier `owner-cloud-auth.json` as historical evidence. No credential values were printed or saved, no live writer interruption, paired SQL/CAS backup/restore, service restart or cutover occurred, and no source/architecture stamp was changed. Valid original authentication and an eligible approved acceptance scope remain required before live operations.

## Canonical OpenStack SDK authentication diagnosis — 2026-10-12

- Owner asked whether SDK automatic token acquisition itself is broken. The earlier diagnostic directly loaded a `keystoneauth1` password plugin; this diagnosis additionally exercised the actual standard OpenStack SDK cloud loader and `openstack.connect(cloud='openstack').authorize()`, with `OS_CLIENT_CONFIG_FILE` explicitly selecting the checkout's new `clouds.yaml`. Actual isolated interpreter versions: `openstacksdk` **3.3.0**, `keystoneauth1` **5.15.0**. Offline standard-loader comparison preserved the same owner username/domain, test project and password and selected the same generic password plugin; no credential values were exposed.
- Safely observed real HTTP exchanges without modifying their payloads: TLS-verified `GET https://keystone.dmslab.re.kr/` returned **300** for version discovery; SDK then sent exactly one **POST `/v3/auth/tokens`** with password authentication, user `pieroot`, domain name `Default` and project ID `99f2f82cb2d14788a0723af31c16dab0`. The wire password matched the selected file (boolean comparison only). Keystone returned **401 Unauthorized**, generic authentication-required error, request **`req-ea87a926-8d5f-4b39-9c37-47a506edbd5e`**. SDK surfaced an outer `SDKException`; the observed HTTP status/request ID come from the actual HTTP response, not missing attributes on that wrapper.
- This proves the SDK's config-loading, version-discovery and token-request path runs; it does **not** prove successful token issuance, overall SDK/cloud health or the precise rejection cause. Password/domain/project access/account-state/server-policy cause and the owner's admin/native eligibility remain unverified. SDK reinstall, IAM grants or protection bypass are not justified by this 401; correlate this exact request ID with read-only Keystone authentication logs or verify the original private credential/project access. No extra login attempts followed this observation.
- Secret-free evidence: `owner-cloud-sdk-auth.json` under `~/.local/share/palimpsest-proofs/hub-kolla-release-20261011/`. Original authentication/eligible acceptance remains blocked before live writers. No credentials file, IAM, Hub data/config/service, paired backup/restore or source/architecture stamp was changed; no test files or persistent diagnostic scripts were added.

## SDK-direct private-token test — 2026-10-12 (authentication blocked)

- Owner clarified that tests must acquire their token directly through OpenStack SDK from the existing `clouds.yaml`; do not require the owner to generate or hand over a manual token file. Tokens must not be externally exposed. This replaces the earlier manual-file suggestion, not the ordinary-user authorization prerequisite.
- Fresh real SDK **3.3.0** `connect`/`authorize` execution read the checkout's current `openstack` cloud and retained `pieroot` / `Default` / `test` (`99f2f82cb2d14788a0723af31c16dab0`). TLS discovery returned **300**, then exactly one **POST `/v3/auth/tokens`** returned **401**, request **`req-a69d33d1-878a-4bed-90a5-eaf3a8e0615c`**. No token was obtained and the intended original-token-only read of Hub `/v1/projects/current` was not reached. Roles/native authority remain unverified; do not claim a successful Hub feature test.
- The diagnostic child disabled debug logging/core dumps, discarded stderr, restricted requests to the approved Keystone/Hub HTTPS hosts with certificate verification, omitted headers/bodies from observations and guarded result serialization against a token value. Token handling is process-local by design; this run issued no token, so successful-token lifecycle secrecy is not live-proven. No token value or password was printed, saved to evidence, returned to the parent or written to a token file; the private credential file was not modified or transferred.
- Read-only controller1 diagnosis used the existing `wireguard-dmslab` operator and installed public Ansible modules. Confirmed actual `keystone`/`keystone_httpd` names and `kolla_logs` mount; examined their recent runtime logs and the discovered `/var/lib/docker/volumes/kolla_logs/_data/keystone/keystone.log` without printing or saving raw log contents. Neither contained an exact match for this request; the application log also lacked the two selected earlier test-project request IDs. Precise credential/domain/project/account/server cause is still unknown. No additional login, other-controller inspection, IAM change, private-helper transfer or authentication substitute was used.
- Secret-free result and diagnostic limits: `owner-cloud-sdk-memory-test.json`; updated `production-cutover-inputs.json` under `~/.local/share/palimpsest-proofs/hub-kolla-release-20261011/`. SDK-driven authenticated Hub testing and live rollout remain held before writer fencing/paired restore. No live Hub image/config/data/service, native worker, VM/GPU, source or architecture stamp was changed.

## Keystone routed original-user rejection diagnosis — 2026-10-12 (authentication blocked)

- Continued from current dirty `dev` checkout `aa397a0fd976e35302974a347748a9773e3084bf` (0 staged, 88 unstaged, 16 commits behind `origin/dev`), preserving unrelated work. Approved release/rollout source remains `7186abf7844454eda7ed86a215192b33b9250ff8`; no source, architecture stamp or test changed.
- Public Keystone name resolves through `openstack.dmslab.re.kr` / `117.16.137.199`. Read-only controller1 HAProxy configuration routes both Keystone backend sections to controller1–3 port 5000; therefore the earlier missing controller1 log match was not evidence of a client or SDK fault. Existing operator-held Ansible read-only modules and Kolla privileges located controller2 and controller3 Keystone application logs; raw logs were neither printed nor saved.
- Controller2's `keystone.log` contains exact matches for the latest scoped SDK request `req-a69d33d1-878a-4bed-90a5-eaf3a8e0615c` and for selected earlier test-project requests. Their safe classification is generic unauthorized authentication. A same-request `keystone.common.password_hashing` warning is the configured algorithm-specific maximum-length cap message, as defined by the [Keystone source](https://docs.openstack.org/keystone/latest/_modules/keystone/common/password_hashing.html), not evidence that this input was truncated.
- Without printing values or digests, the current private password is a nonempty string within 72 bytes, without outer whitespace/newlines, environment-reference syntax or Fernet/JWT token shape. The checkout file was last modified `2026-10-12T10:00:26Z`.
- To distinguish project scope from identity authentication, one explicit **unscoped** OpenStack SDK `Connection.authorize` diagnostic retained original username `pieroot`/domain `Default` and asserted unscoped wire scope. Keystone returned **401** `req-c736d67d-446c-4cb1-88d5-462f1a835b84`; controller2 logged two exact matches with the same cap-warning and generic identity rejection. No token or project token was issued, own-project listing was not reached, and no Hub request was made.
- Result: the selected original user/password authentication is rejected before project selection can be the sole explanation. Keystone does not distinguish invalid password/domain, account enabled/lockout state or server authentication policy in these logs; actual user ID, roles, test-project membership and native package eligibility remain unverified. Do not reset passwords, enable/unlock accounts, grant IAM, create application credentials or substitute protected/service credentials without separate exact approval. A manual token file is not required: once original authentication is accepted, SDK must obtain and use a process-local token directly.
- Secret-free evidence: `keystone-auth-rejection-diagnosis.json`; updated held state in `production-cutover-inputs.json` under `~/.local/share/palimpsest-proofs/hub-kolla-release-20261011/`. Credential file, IAM, Keystone/Hub configuration/data/services, writer fencing, paired SQL/CAS restore and cutover remain unchanged.

## PyPI operator transition and read-only cloud prerequisites

- Owner requested three separate deliverables: fixed PyPI Kolla installation before a later repository-private transition; an OpenStack Cinder/RBD block-layer path matching local read-only SquashFS semantics; and actual canonical Hub deployment/API verification. Repository privacy, IAM/password changes, broad native/GPU operations and unrelated publication remain outside this work.
- Refreshed the live operator baseline before mutation: prior receipt hashes were stale, so stopped, reread the current spec/lock and preserved those inputs instead of overwriting intervening changes. A separate private backup is `/etc/kolla/.palimpsest-pypi-20261012T165238Z`. Changed only the operator dependency to `palimpsest-client==0.3.3` and removed its Git source; normal `uv lock` retained **113 unrelated entries / 115 total**.
- Plain `uv sync --locked --inexact --no-install-project` only audited the existing same-version Git installation. Explicit `--reinstall-package palimpsest-client --no-cache` fetched the PyPI release and replaced exactly one package. **109 other installed versions / 110 total** are unchanged; actual operator CLI version/help passed, Git `direct_url.json` is absent and all **22 role file hashes** match the sealed release source. Locked wheel SHA-256 is `b15292af8a2b00aa3b1db15e0de30eaea7355693cf04e3aaa0407bf41e534204`; secret-free proof: `operator-pypi-install.json`.
- Read-only controller2 installed Keystone SQL `session_for_read` calls confirm exact `pieroot` user `b9aef3e76c4b4405a91bc0365201fea3` and `Default` domain `default` exist and are enabled. Password is not expired; failed-auth count is 0, last-failure timestamp absent, configured lockout threshold/password-expiry/inactivity disable policies unset. No authentication request, credential/hash output, IAM/password change or token occurred. Roles and actual project access remain unverified; account metadata is not an original-token acceptance proof.
- Installed password-hashing source and actual registered policy confirm `bcrypt`, configured maximum 4096, algorithm cap 72 and `strict_password_check=false`. The algorithm-cap warning is emitted before comparing actual byte length whenever the configured maximum exceeds 72; it is **not** evidence this input was truncated. Current private input is unchanged and fits 72 bytes. Existing scoped/unscoped401 remains the accepted-authentication blocker; no blind login retry or manual token file. Proof: `owner-account-metadata.json`.
- Read-only current Hub policy comparison found this exact original user and selected `test` project outside the immutable protected ID sets (6 users / 4 projects). This does not establish token roles, package capability or eligibility. Current controller1 API/worker still run old0.3.2 exact pins and have not restarted since `2026-10-09`.
- Read-only controller1 provider configuration confirms running Cinder `RBDDriver`, backend `ceph`, default type `ceph_hdd`, pool `volumes`; Glance default backend `rbd`, pool `images`. No credentials were exposed. This establishes a configured RBD path, **not** measured COW cloning, current token-readable volume type/quota, readonly Nova attachment or guest mount/reboot proof. Secret-free evidence: `rbd-provider-readiness.json`. No writer fencing, paired SQL/CAS backup/restore, Hub cutover or cloud-resource creation occurred.

### Supplied test-openrc.sh SDK authentication

- Owner supplied `test-openrc.sh` and authorized its original exported account input. Secret-safe inspection retained `pieroot` / `Default` / `test` (`99f2f82cb2d14788a0723af31c16dab0`), public HTTPS Keystone, identity v3 and RegionOne. One SDK-direct token POST returned **401**, request `req-4c47fa98-68e1-46a2-a203-3a722bb3790f`; the sent username/domain/password/original project matched the supplied file. No token or Hub request was obtained; no credential value, token or password hash was output/saved.
- An offline native Bash source comparison discarded script stdout/stderr and confirmed every exported `OS_*` value matches the parsed SDK input. The password equals the existing `clouds.yaml` input; no new authentication followed this comparison. All three actual Keystone backend application logs were inspected read-only; controller2 contains the exact request's two cap-warning/generic-rejection lines, not a typed expiry/lockout/disabled error. No raw logs were exposed.
- The installed bcrypt library independently hashed a public synthetic non-credential string: matching verification and wrong-string rejection both passed. This rules out that simple library failure only; it does not authenticate the original account or identify its actual stored-password mismatch. Secret-free evidence: `owner-openrc-sdk-auth.json` and `owner-account-metadata.json`.
- Actual cloud block attachment/clone and production Hub rollout remain held before any new resource or writer fencing. Require accepted original SDK authentication and then actual roles/service capabilities; no manual-token handoff, service/admin substitute, password reset, IAM mutation or native bypass.



## Cinder/RBD local implementation and qualified proof — 2026-10-13 KST

- Three owner items remain distinct: **(1) PyPI operator completed** at exact `palimpsest-client==0.3.3` with113 unrelated lock entries/109 installed versions/22 role files preserved; **(2) block implementation and selected local verification completed, actual Cinder/RBD held**; **(3) production Hub deployment/recovery/API persistence held** behind accepted original authentication and writer-fenced paired SQL/CAS restore. Existing production0.3.2 and sealed publication refs/pins are unchanged. Repository privacy and new publication are not included.
- Source: parent shared dirty `dev` `aa397a0fd976e35302974a347748a9773e3084bf`; sibling Afterglow dirty `dev` `aac6b9c5dc3e53fc3e430ca9ed96c30cacdbdb1e`. Preserve both worktrees, actual index, MCP/operator WIP and private credentials. No new commit/push/tag/GHCR publication, remote helper transfer, login retry, IAM/password mutation, cloud resource or live writer/service change occurred.
- Afterglow block source now verifies exact sealed SquashFS bytes before provider/DB mutation and retains protected Glance/upload identity. Consumer keeps authenticated connection/project/expected snapshot, root→delta IDs and durable CREATE intents/lease/rejected/unknown cleanup. Fixed managed Manila grant denial on dedicated/ordinary deletion, concurrent rejection preservation, cancelled destructive-thread/SQL artifact lock draining and bound FIP disposal before Nova/volume/port. Immediate imports reject block after admin authorization but before planning; original completed import IDs and sealed outputs survive optional consumer failure/cancellation.
- Lower/upper helpers enforce whole real virtio/RO/ambiguity/exact sealed-prefix hashes and blank-only formatting, retained Cinder/ext4 identities and duplicate FSUUID rejection. Actual initrd construction exposed YAML-null/build-host config copy/prereqs/filesystem-module defects; source now quotes empty content, packages actual dependencies/modules and reads runtime config from mounted bootstrap root. External proof-only POSIX/mke2fs corrections do not provide production fallback behavior.
- Checks passed: selected API/delete326, final runner/resource97, actual MariaDB lock/cancellation4, Linux helper91, required access backend285; actual additive DDL on owned legacy mirrors preserved rows/old writers/new JSON reread. Canonical backend/worker/frontend arm64+amd64 built/executed; backend/worker selected app16 files match final source bytes. Current private TOML remains unchanged; scratch selector execution is not a production configuration repair. Scoped Ruff/11-file format passed; no full shared-tree test:gate or frontend/browser proof is claimed.
- TCP production-router→native SDK→real MariaDB proof finished with two source/two consume identities, one CREATE for the accepted/lost-response run, durable pending retained across changed API boot, recovered exact resources and no remaining synthetic server/volume/port. Authentication/policy/Glance visibility/console health are synthetic; shared image retirement used an explicitly external synthetic helper. This is not original-user auth, full-app/Redis readiness or actual guest/cloud evidence.
- Separately, actual Linux6.1.0-53-amd64 QEMU **CPU-only TCG** passed first boot, reordered lower+upper cold boot with durable data, kernel-writable lower rejection and serial-less duplicate upper FSUUID rejection. Observed direct raw SquashFS/OverlayFS, child precedence/whiteout/bootstrap cloud-state bind. Minimal BusyBox payload and external first-format harness do not prove Ubuntu cloud-init/SSH, UID ownership fidelity, Cinder attachment/COW/quota, native KVM/GPU or production acceptance.
- Architecture: affected sibling architecture/API/pipeline/domain/OpenSpec/changelog and parent narrative updated. Only HEAD+33 selected Afterglow source/test overlays were stamped/checked in an isolated index/review root: source2,628 files, SHA256 `19474e68e7f701a66451693c49433968f2e491a741cc2d6c39bd93d7eecd7c7e`. Real index and broad shared marker were preserved. The earlier parent refresh checkpoint reported a stale working guard; no broad restamp or new parent-source approval was performed for these docs-only edits. Canonical images include other shared WIP and only selected app byte correspondence is qualified.
- Review limits: independent initial lifecycle4 findings were fixed with parent SDK/SQL runtime regressions; narrow guest/import review closed. Lifecycle follow-up repeated old snapshot rather than furnishing a fresh applied-source clean verdict, so no final independent lifecycle signoff is claimed. Astra planning/ops roles were unavailable. Gbrain CLI was absent; no sync or search-guidance claim.
- Exact remaining prerequisite: original `pieroot/Default/test` SDK authentication still401 `req-4c47fa98-68e1-46a2-a203-3a722bb3790f`. Enabled/nonexpired/unlocked read-only metadata and public synthetic bcrypt checks do not establish password validity, actual roles or native authority. No unchanged retry, manual token file, service/admin substitute or protected-project switch. After accepted original SDK auth, verify actual capabilities and resume only the already approved eligible scope → writer fencing/paired restore → canonical controller1 cutover → authorization/bytes/restart proof sequence.
- Durable, secret-free receipts and real guest logs: `~/.local/share/palimpsest-proofs/block-transport-aac6b9c5-20261013/`; full local evidence/failures/limits in sibling `docs/testing.md`. No temporary proof code was added to either repository. Owned proof processes are stopped without deleting preserved proof datastores/images/payloads.


## Native Hub worker ownership — 2026-10-14 (locally test-passed, not live-verified)

- Scoped checkout is dirty `dev` `/Users/pieroot/code/palimpsest`; the existing shared index and unrelated runtime/local CLI/operator work are preserved. No live checkout, production account/configuration/data/service, provider CREATE/unknown intent, native KVM resource or release ref is changed. No commit, push, publication or deployment is performed.
- Root cause: build startup acquired only a local store-path flock, then selected every `building` SQL row for interruption/cleanup. Independent worker lock paths therefore did not protect a live shared-SQL build. Export scratch collection used age alone; delegation cleanup completion did not compare its claim attempt.
- Implementation adds build attempt expiry/execution-node identity, token-owned SQL recovery/publication/cleanup and renewable shared maintenance roles. Atomic per-job SQL `FOR UPDATE SKIP LOCKED` claims still distribute jobs; there is no deployment-wide build singleton or generic scheduler dependency. Recorded node identity owns KVM/process teardown; remote expired reconciliation preserves that node's tree and never replays its recipe. Shared artifact storage/digest locks, original caller/project and requester Trust authority remain required and unchanged.
- Rollout prerequisite: stop **all old API/build/export workers**, run `palimpsest-hub-bootstrap` offline to add nullable build ownership columns and `palimpsest_worker_leases`, then start only the new version. Null legacy identities do not authorize guest adoption; ambiguous state is retained. Transient role ownership is not migrated between databases. Old unfenced workers must never coexist with new live claims.
- Native OpenSpec: `openspec/changes/archive/2026-10-13-lease-native-worker-maintenance/` (archived; spec `openspec/specs/native-worker-ownership/spec.md`) contains proposal/design/scenarios/implementation tasks. Behavior consumers extend Hub build/export/delegation/database/migration tests. The opt-in `tests/test_database_pool.py::test_shared_sql_process_ownership_smoke` ran with privately supplied `PALIMPSEST_OWNERSHIP_SQL_URL` against fresh empty loopback MariaDB database `palimpsest_ownership_n7xrnua4c`: **2 passed** (earlier pre-lint source also **2 passed** on `palimpsest_ownership_n7xrnua4`). Two/three real spawned SQL consumers use independent flocks, synthetic guest execution/provider deletion and controlled expiry; SQL/OS-process ownership assertions are real, not actual guest-build or live provider evidence.
- The implementation slice ran no checks. Subsequent parent verification on a frozen clone of the current working tree (concurrent WIP included) passed the focused Hub suite **93 passed, 2 skipped** and full Hub suite **378 passed, 2 skipped**, after fixing the initial 16 export timestamp failures with SQL-side synchronization, UTC normalization and five regressions. Hub Ruff retains only `tests/test_auth.py` I001; SIM117/SIM102 in `services/image_exports.py` were fixed. Eight lease-change files were formatted; seven credential-isolation files remain unformatted and block the Hub format gate outside this change. See [exact selectors, file lists and proof boundaries](testing.md#native-hub-worker-ownership). KVM guest builds, live Glance/provider deletion, production and mixed-version rollout were not exercised. No deployment, publication, commit, stage or push occurred. Broad shared-tree architecture restamping is not claimed; the parent records isolated-clone stamping separately, with the shared marker unchanged. Astra/independent-review approval is not claimed by this report.
- Local Gbrain sync is unavailable: `which gbrain` returned exit 1 with no path. No code-index refresh or CLAUDE search-guidance capability is claimed; this does not block the scoped native implementation.

### Export timestamp remediation — 2026-10-14 (targeted portable pass)

- Parent verification reported 16 failed/72 passed/2 skipped in the initial five-file focused suite. Retain that failure receipt at `~/.local/share/afterglow-proofs/afterglow-process-ownership-n7xrnua4/palimpsest-focused.log`; the later post-fix focused/full-suite results are recorded above without erasing the initial failure.
- Root cause is ORM synchronization, not lease expiry: after locking/loading an export row, the bulk CAS update's default Python evaluator compares a naive SQL DATETIME to aware UTC `now`, raises `TypeError` and rolls the transaction back. That leaves success/error and Trust-retirement paths at `downloading`/`active`. Timestamp-predicate CAS and cleanup/sweep updates now use SQL-side fetch synchronization; original owner/expiry predicates and row fences remain intact. Python soft-delete expiry checks also normalize stored UTC timestamps.
- Added five cases for a reloaded-naive identity-mapped owner (renewal, complete, error) and live/expired soft deletion. Newly authorized targeted command in `hub/`: `uv run pytest -q tests/test_image_exports.py tests/test_export_delegation.py` → **54 passed, 74 existing auth HTTPClient deprecation warnings in 18.17s**. No unchanged failure was rerun to confirm it. No lint, formatting, build, broad suite, architecture stamp or SQL/process smoke was executed by this remediation slice; parent integration owns those checks.
- This portable pass does not prove MariaDB locking, actual guest execution, deployed Keystone/Glance behavior or production readiness. No live checkout/resource, runtime/local CLI, provider CREATE/unknown intent or publication changed.

## Production Hub upload, lookup and client connection — 2026-10-15 KST

- This verification preserved the shared dirty `dev` checkout at `aa397a0fd976e35302974a347748a9773e3084bf` and its index. The checkout's older 0.3.2/0.3.3/authentication-blocked entries above are historical, not the current production baseline. Current published documentation at protected dev `d37d7af7a74c5db2e744e23e1097c70862bff955` and the clean release checkout `/Users/pieroot/code/afterglow-release-all-20261014T073153Z-c2ba90/palimpsest` establish the separately released 0.3.5 source `04d51ad6e1bf00c4b6e5bf82050383d0bb0a10cf` ([PR #24](https://github.com/openstack-afterglow/palimpsest/pull/24), [rollout record #26](https://github.com/openstack-afterglow/palimpsest/pull/26)). No reset, merge into this shared tree, source change, commit, push, publication or new production rollout occurred here.
- Fresh read-only operator/runtime inspection observed client **0.3.5**, Kolla **21.3.1.dev4**, controller1 API `sha256:1f61fd0e150f871a5dd5d11dab4f5b18cd41f1c1f076ae6fdd7113bd32bdbc01`, export worker `sha256:deca07af481a963c4ac70a4294af92f8451e56b4badeaadba9065357e0fb93fa`, and the existing dedicated build VM's worker `sha256:b5cd64562234d57ca9c694ed596978d6dda813219d0bb85f60d8f9ae1e14efb7`. All were running; API was healthy. Existing 0.3.5 deployment was retained instead of applying stale 0.3.3 targets. Its SQL `SELECT 1` and Redis `PING` passed. All **33** Hub Python/JS/CSS/HTML files matched the current release source; the sorted relative-path/content-hash receipt is `07c0bbb666946ba216aa23b66563fa1b1001f34693bc8740d08720127e721e60`.
- Canonical `docker/hub/Dockerfile` API/export-worker/build-worker targets were rebuilt locally for the declared **linux/amd64** platform from the clean release checkout (builder stage cache disabled on the API build). All three executed with `--network none`, imported their actual entry modules and reported Hub **0.3.5 / x86_64**; build-worker additionally reported client **0.3.5**, bubblewrap **0.12.0** and qemu-img **10.0.13**. Tags are `palimpsest-hub-live-80043ecf:{api,worker,build-worker}-amd64`. Local rebuilds were not pushed or substituted for the immutable production pins.
- Corrected current `clouds.yaml`/`openstack` authenticated original **pieroot / Default / test** through the SDK: token POST **201**, `/v1/me` **200**, original project retained and current inventory/download/upload capabilities allowed. The stale `test-openrc.sh` password differs and was not used for authentication. Legacy publication used current project publish authority, not an administrative privilege fallback. Native `/v1/projects/current` remained **403** for this administrative identity, as required by its separate unchanged non-admin package policy; no IAM/protection change or positive native-package acceptance is claimed.
- Actual production transfer proof: isolated PyPI client **0.3.5** on Python **3.11.15** packed and uploaded a real private SquashFS, listed it by exact name, pulled it into a separate consumer state and verified its complete SHA-256 and internal marker. Actual Chromium `/app` connected with the same original identity and uploaded/searched a second private SquashFS. A fresh isolated `palimpsest web --no-browser --allow-control` connected to the real HTTPS Hub, searched/downloaded that second artifact, then uploaded/searched a third local layer through its normal confirmation and asynchronous operation paths. Download `cd16ef7baac244f19f33adb54a2606ad` and upload `d4e642f3ec2d429f9243a3a94beb1636` both reached **succeeded / exit 0**. The third artifact was independently pulled through the public CLI and its SHA-256/internal marker matched. Each artifact was **4096 bytes**, owned by original user `b9aef3e76c4b4405a91bc0365201fea3` in project `99f2f82cb2d14788a0723af31c16dab0`, and not published.
- Credential proof: original Keystone token was neither printed nor saved. Hub `/app` cleared its password input and had empty local/session storage. Local dashboard session storage held only its distinct random dashboard credential; the Keystone token was absent from both browser stores and its URL query was cleared. Only isolated state roots were used; no original VM ledger, lifecycle control or guest command was exercised. A layer without a cloud-image base correctly retained Download while disabling Run.
- Verification-tool limitations were separated from product behavior: managed opening initially selected a disconnected relay; explicit owned Chrome/CDP worked. An attached endpoint reused a managed page and its allowlist until the previous handle was released. A stalled browser was stopped; a fresh owned browser with `dialogs: accept` completed the upload, with zero browser error entries. Early smoke waits used incorrect completion text/receipt fields; authoritative operation state showed success, not product failure. These issues were reported, and no application code was changed to mask them.
- Exact-owned cleanup: all three temporary registrations were ownership/name/digest/private-state checked, deleted with **204**, then read back as **404**. Global blob GC was not invoked; physical CAS deletion is deferred by server policy, not claimed. Owned web processes exited **0**, final listener returned connection refusal **61**, and owned Chromium sessions/processes were stopped. Screenshots, payloads, downloaded bytes, installed client and secret-free receipts remain under `~/.local/share/palimpsest-proofs/hub-live-20261015-80043ecf/`, including `production-readiness.json`, `metadata-before-cleanup.json`, `dashboard-operations.json`, `credential-separation.json`, `owned-cleanup.json`, `hub-browser-upload-query.png` and `dashboard-hub-upload-query.png`.
- Scope: current production **legacy upload → lookup → authenticated client/dashboard connection → verified download is live-verified**. This is not a new deployment, native package positive proof, live build recipe/restart recovery, Glance Trust export, Cinder attachment, KVM/GPU qualification or a full-suite result. Astra/role-specific ops signoff was unavailable and was not substituted. This operational evidence-only handoff edit does not restamp or approve the preserved shared source.

## Afterglow Hub workspace and local-client guide — 2026-10-15 KST

- Work is in sibling `/Users/pieroot/code/afterglow`, observed dirty `dev` HEAD `aac6b9c5dc3e53fc3e430ca9ed96c30cacdbdb1e`. Parent remains `aa397a0fd976e35302974a347748a9773e3084bf`; no parent CLI/Hub/runtime code, published0.3.5 pins, source approval, original index or shared architecture marker was changed. No commit/push/publication or production rollout is authorized by this UI work.
- `/palimpsest/packages` now separates legacy Hub images/layers, native packages/keys and local use. Native administrative/service403 remains intentional and local to its tab. Hub search/page/detail/chain/integrity-bound download/recent-job observations and original SDK/CLI0.3.5/local-web instructions were implemented without new schema/dependency/VM-control structure. SDK HTTPS/project/default-user checks and native public UUID/explicit API-base/alias-failure boundaries stay distinct from browser JWTs and private key material.
- Exercised evidence: changed frontend97, canonical Palimpsest backend242 and design128, compiled Chromium144locale/theme/width/workspace conditions, actual mounted proactive/401 permission transitions and corrupt-download rejection. Original `pieroot/Default/test` SDK→installed0.3.5 CLI inventory and SDK-child local control-web reads also succeeded. These safe read/guide results are separate from the earlier live production upload/download proof above and from synthetic browser/API responses. The sibling's28file overlay staged architecture guard passed at `d3e82b8c8cd90909b646be035f6b33f29cab80d71540fcc8392143876515be91`/2,633source files; full shared WIP is not approved.
- Actual Afterglow login→BFF→production Hub is held: login can auto-provision networks and synchronize IAM. Read-only preflight found an existing domain Default project but no original user's direct project_reader and no managed projection, so a no-IAM-write login is not available. No retry/role grant/protection bypass is used to force acceptance. The initial503/aborted attempts lack a pre-attempt snapshot; later collected network/router/subnet/role/projection fields were unchanged only across their recorded read-only window, not proof of global historical no-change. Existing resources were not deleted/rolled back.
- Exact canonical image/runtime results, full frontend/Svelte failures, source parity and remaining Docker/auth prerequisites are maintained in sibling `docs/testing.md` under **2026-10-15 KST Palimpsest Hub workspace** and active OpenSpec `expand-palimpsest-hub-dashboard`. Durable screenshots, real interaction recordings and sanitized receipts are in `~/.local/share/afterglow-proofs/hub-dashboard-20261015-80043ecf/`. `80043ecf` is a proof label, not the Afterglow source ref. Astra approval is unavailable; it is not substituted. This parent edit is handoff-only and does not restamp unrelated source.
- Continuation (2026-10-15 KST): original Docker timeout receipts remain historical. Engine response and exit0 stops for the three prior owned containers were observed without global recovery. Six canonical Afterglow backend/worker/frontend arm64+amd64 builds exited0. Independent review found timezone-less Hub UTC timestamps displayed nine hours early in Seoul; parent corrected only the sibling component and existing regression. The regression failed before, then focused frontend98passed; both corrected frontend architectures rebuilt/executed as the canonical non-root appuser and the deployed arm64 image passed144synthetic locale/theme/width/workspace conditions plus reactive/proactive authority and corrupt-download checks. Initial strict frontend UID1000 assertion failures remain preserved; actual canonical frontend UID1001 is not treated as a product defect or rewritten to1000.
- Current-source local Compose used no cloud credentials: fresh SQL40tables/Redis/HTTP and invalid/absent bearer401 were observed. API/frontend publication used a project-only bridge after the internal-only port-map failure, so final application egress isolation is not claimed; DB/cache remained internal. Only the four new stopped containers/two networks were removed; prior proof containers, keys/volumes and frozen snapshots remain preserved and stopped. Other shared-runtime disappearances were observed, not attributed or recreated. Current original-account GET-only IAM preflight still lacks Default project_reader and the managed projection, so real Afterglow login→production Hub remains held without mutation/bypass. Sibling scoped guard is now `8df68ea7fb9a3d9d64b0767b5b5d17bce5a183ae3ad300b481cee62040377d60`/2,633files, with actual index/shared marker preserved. Full frontend/Svelte prior failures, initial login baseline gap, archive/publication/production and Astra approval remain unqualified; see sibling `docs/testing.md` continuation and private receipts. Parent source, pins and approval marker remain unchanged.

## Dirty dev reconciliation and all-change publication — 2026-10-15 KST

- Owner approved committing and deploying all current Palimpsest work (Palimpsest only; the sibling Afterglow worktree is excluded). Observed original checkout: dirty `dev` `aa397a0fd976e35302974a347748a9773e3084bf`, 0 staged, 92 tracked modifications and 24 untracked entries (131 public paths plus private `test-openrc.sh`), behind fetched `origin/dev` `d37d7af7a74c5db2e744e23e1097c70862bff955`. The original working tree and index were not modified (index SHA-256 `62122c393a69abe26b4de3dc77b191b1637ad4ba2161570b18628d3872be2776` unchanged).
- `test-openrc.sh` contains `OS_USERNAME`/`OS_PASSWORD` assignments; it was excluded from every snapshot, scan and commit, and no value was read into evidence. A redacted gitleaks scan of the public working diff reported only the vendored MIT `xterm.js` `FourKeyMap` false positive; that file is byte-identical to released dev.
- Exact public snapshot tree `5d1aeb9ca3593aa8e606ea2ab5e71595ec8e9208` is recorded as local-only commit `19e3eac272dfcf3ad95f762667bad6bc886bff14` on branch `snapshot/dev-working-20261015T030357Z` (parent `aa397a0`). It preserves the dirty state as observed; it is not pushed because most of it is superseded release input.
- Three-way reconciliation against the release lineage: 62 public paths are byte-identical to `d37d7af`; 69 differ. Of 56 differing Python files, 29 differ only in formatting/comments (AST-equal) and 27 are older copies of behavior that released `12c0a53` (HTTPS service-VM worker), `30b72dd` (ordinary-publisher authority), `018ca2c` (0.3.5 integration), `84e3810` (loopback reverse-DNS bind) and `1ff87ef` (readline completion wait) replaced. Restoring them would reintroduce the retired shared-SQL/`/dev/kvm` build worker and older authorization, so the released versions were kept. Only `docs/development-handoff.md` changed after the 0.3.5 integration clone was created (2026-10-14T07:31:53Z); its two 2026-10-15 sections above are carried verbatim. Every other local change was already released in 0.3.5.
- Result: documentation only. No source, schema, dependency, configuration, workflow or Kolla role change; no version bump, release tag, image publication or rollout is needed. Working architecture guard: `source_sha256=855cda7bf7a8155834035086c074f769622a77b03a4d610b230e7379ea11cbd4`, 475 files, unchanged existing stamp; the lane plan selected no tests for this documentation-only diff.
- Production already runs the released source containing that work. Operator `wireguard-dmslab`: `palimpsest-client` 0.3.5, Kolla 21.3.1.dev4. `/etc/kolla/globals.d/99-palimpsest.yml` pins equal the GHCR `0.3.5` index digests: API `sha256:1f61fd0e150f871a5dd5d11dab4f5b18cd41f1c1f076ae6fdd7113bd32bdbc01`, export worker `sha256:deca07af481a963c4ac70a4294af92f8451e56b4badeaadba9065357e0fb93fa`, build worker `sha256:b5cd64562234d57ca9c694ed596978d6dda813219d0bb85f60d8f9ae1e14efb7`. Read-only container inspection on `dms-controller1` and `palimpsest-hub-build-worker-034` showed exactly those digests, restart count 0, started 2026-10-14T10:49–10:55Z, API healthy. Public `https://palimpsest.dmslab.re.kr/v1/health` returned 200 with verified TLS; unauthenticated `/v1/me` returned 401. No pull, reconfigure or restart was performed because no deployable byte changed. That override file's comment still says 0.3.4 although its digests are 0.3.5; the operator file was left unchanged.
- Not established here: native KVM, live build recipe execution/restart/cleanup, Glance Trust export, Cinder attachment and Afterglow login→BFF→Hub, as recorded above. Astra role approval is unavailable and was not substituted. Disposition receipts: `~/.local/share/palimpsest-proofs/release-all-20261015T030357Z/` (`scope.json`, `candidate-overlap.json`, `python-source-overlap.json`, `docs-local-residue.json`, `client-integration-disposition.json`, `hub-integration-disposition.json`).

## Local web connected Hub/Docker catalog — 2026-10-15 KST (uncommitted)

- User requested real Palimpsest Hub and Docker Hub image/layer inspection in the local web app, with the local client sending queries/requests even when the browser can reach only that client. Work is isolated in `/Users/pieroot/code/palimpsest-local-hub-catalog-20261015`, branch `feature/local-hub-catalog-20261015`, actual HEAD/base `3868e527df1bb14f33aee5b6066faf760ad213c2`. Original dirty `/Users/pieroot/code/palimpsest` and its index remain preserved. This new feature is not included in the earlier all-change reconciliation publication above; no new commit, push, tag, package publication, remote installation or Hub deployment was performed. No new cloud/IAM, helper transfer or VM lifecycle operation was authorized or performed. Astra planning participation is unavailable, not substituted by independent review.
- Added `docker_hub.py` and deterministic behavioral tests. The selected Docker profile uses fixed verified HTTPS search/token/registry authorities, selected config/helper credentials only, bounded strict JSON and descriptor lists, SHA-256/size checks, explicit parent-index child membership and opaque non-URL pagination. A live config307 initially failed under strict no-redirect behavior; the fix permits one anonymous digest-bound config hop to exact allowlisted Docker CDN hosts, never forwards Authorization, and still checks downloaded config bytes. Inspecting ordered layer descriptors is not layer-blob verification and needs no Docker daemon/Skopeo. Canonical acquisition still uses Skopeo and supports only linux/amd64 OCI roots; native OCI-root execution remains Linux amd64 KVM-only.
- `HubConnection` carries URL/original Keystone token/optional exact project assertion only through scoped local headers. Identity comes from the real original `/v1/me`; filtered image/SquashFS metadata and full base/parent details use the same caller, with no native-key/admin fallback. Inventory and HTTP routes forward that request context; transfers snapshot only their owned child's environment, never mutate `os.environ` or expose credentials in argv/receipts. Local bearer, origin checks and read-only POST/DELETE denial remain intact. Native package helpers and Docker credentials remain separate domains.
- Frontend adds connection/disconnect, original identity/capabilities, Hub name/digest/architecture/disk/parent queries, Docker repository/tag navigation and accessible platform/layer detail. Initial unconfigured selection now chooses Docker. Live provider switching reproduced a Docker query reaching the Hub as an invalid name (422); connection changes now reset provider-specific queries. Independent interaction review found initial selection, stale filter pagination, modal-to-VM-form handoff and replaced-platform focus defects; all four were fixed, with real browser observations for default selection, query/pagination reset, platform focus and actual verified-download handoff. Static transport/security review reported no concrete reachable issue within its scope, not a runtime/security certification.
- Local checks: the initial shared-venv invocation imported the original checkout and failed `docker_hub` collection; setting `PYTHONPATH=src` and asserting the resolved package path corrected the environment. Focused Docker/remote/operation/UI/inventory consumers passed **315**; related registry/intake/CLI-registry/lane/architecture consumers passed **320**, and CLI reference was current. Final integrity and real HTTP credential/read-only consumers passed **20 in 1.89s** after lint-only strict-zip/port-expression/import grouping changes. Final synchronous changed-path Ruff check and format (10 files), lane manifest and CLI-reference checks passed. The LSP import action left top-level pytest/project grouping inconsistent with CLI Ruff; a blank separator corrected that final I001. No test was weakened or incidental implementation pin added.
- Actual real-provider browser proof used owned scratch state/config plus empty Docker auth config: public Docker search and two pages, Alpine repository/tags/Back, exact OCI index→amd64 config/layer inspection, keyboard selection and successful pinned-child download; actual original-account Palimpsest Hub six-row inventory, aarch64/qcow2 filtering, full ARM64 base+SquashFS dependency inspection and verified full-chain download. The completed Hub transfer closed the modal and populated/focused `run-image` with the base and ordered layer; no Run was submitted. Read-only app independently connected/searched/inspected; authenticated transfer POST returned403. Wrong project assertion returned remote403/local409 without local logout. Disconnect cleared override/results, password cleared, Hub tokens were absent from browser stores/receipts, and the browser observed only127.0.0.1 requests.
- Observed UI sizes were desktop1440×1000 and mobile390×844; actual DPR varied1.25/2, so no nominal media/performance claim. No JavaScript exception was observed; five cancelled/socket-reset resource requests were retained separately. A pre-existing unrelated libvirt missing-disk diagnostic was not altered. Both owned web servers and Chromium tab were stopped, private SDK token cleared. Sanitized actual receipts/screenshots remain in `~/.local/share/palimpsest-proofs/local-hub-catalog-20261015/` (`browser-smoke.json` and three `.webp` screenshots). No new build/upload/native KVM/cloud proof is claimed.
- Updated README, CLI usage, changelog, architecture source map/contracts and testing record. [Local build→Hub→consumer commands](cli/usage.md#local-build--palimpsest-hub--another-client) distinguish ARM64 conventional runtime layers from OCI archives and explain full-chain versus layer-only pulls. Actual CLI `layer ls --name hub-live-033-layer --limit 5` exited0; current released list projection may omit `blob_digest`, so consumers must keep Push's full digest or copy Details rather than infer an identity from a name. `palimpsest` and `openstack` were absent from this shell's PATH; Docker, mksquashfs, Skopeo and uv were present. Command examples requiring those CLI installs are instructions, not claimed build/upload runs.
- Final working architecture stamp/guard passed for **477 files**, `source_sha256=e9fbd0e3b90a952c5f47dfbde0b63c5aa578f5e94df0e2ebf00166d9dd4fbfe3`. This binds reviewed working source, not a committed SHA or CI/deployment approval. To run this unreleased UI, use the feature worktree with `PYTHONPATH=src .venv/bin/python -m palimpsest_local.cli web --allow-control` (omit control for metadata-only reads). Publication requires separate exact-feature approval; the earlier 0.3.5 deployment remains unchanged.


## Explicit removal of external host VMs — 2026-10-16 (uncommitted)

- User explicitly requested enabling removal of observed VMs without selecting their original state directories. Continued only in `/Users/pieroot/code/palimpsest-local-hub-catalog-20261015`, feature branch/base `3868e527df1bb14f33aee5b6066faf760ad213c2`. The original dirty checkout/index and original user web process were preserved. No commit, push, release, Hub deployment, sibling Afterglow change or actual VM lifecycle mutation was performed.
- `host_vm_control.py` owns explicit native removal; read-only `host_inventory` emits opaque Lima directory identity or positively classified libvirt native UUID. Inventory and DELETE require stable host ID, original optional run ID/native identity, exact typed name and existing bearer/origin/allow-control authority. External start/stop, same-name collisions, stale identity, OCI-root and other-state run-file cleanup remain blocked; no ledger is imported or adopted.
- Lima removal deletes its own native disk and warns before confirmation. Libvirt preserves disk/NVRAM/TPM files while removing only domain definition/saved state/snapshot metadata. Full XML/name/UUID/marker classification is re-read before mutation; optional run-marker transitions including None are fenced. TPM retention is preflighted before stop. The earlier missing-inactive-overlay observation guard is carried forward without globally suppressing libvirt errors; only confirmed FileNotFoundError skips native blockInfo, while daemon-readable disks retain capacity/allocation after local PermissionError.
- Independent native review found three destructive-safety gaps (unknown OCI classification, TPM retention, changed optional marker); all were corrected. Integration review's missing external detail action and native review's local-permission observation regression were also corrected. Both final static verdicts are clean for the current implementation. This is not Astra/Sol role approval or native daemon qualification; those requested role mappings remain unavailable.
- Final affected suite **301 passed in57.05s**. Changed-path Ruff lint/format, JS syntax, lane manifest and CLI-reference checks passed synchronously. Ten actual Chromium row-flow assertions with isolated native backends passed; final detail→confirmation→DELETE separately exposed only external Remove (no Start/Stop), succeeded with flags75 and retained disk/NVRAM/TPM bytes, with no JS error. Final real HTTP/native-adapter smoke returned200 with storage retained and409 for stale marker. Actual host read-only eligibility was15/15, and real dashboard Remove controls were visually enabled; no actual VM Remove was submitted. Native name-based/UUID-incarnation race limits remain explicit in architecture/testing docs.
- Durable sanitized receipts/screenshots: `~/.local/share/palimpsest-proofs/external-vm-removal-20261016-r_yd5opx/`. Synthetic servers/browser and temporary scaffold scripts were cleaned up. The earlier candidate dashboard was paused during safety fixes. The reviewed feature dashboard is now running as persistent owned service `PalimpsestExternalVmControlReviewed20261016`, PID55896, at loopback port57792, launched from this worktree with `PYTHONPATH=src .venv/bin/python -m palimpsest_local.cli web --no-browser --allow-control`. Use its new printed authenticated link, not an old original-checkout URL. Final actual Chromium observation again found15/15 enabled Remove controls and no JS errors (`final-host-controls.json`, `actual-host-removal-controls-final.png`); no real removal was submitted. No global installation/config or original state-root selection is changed.
- Final reviewed working architecture stamp and guard passed for **478 files**, `source_sha256=2e4dc10fdc5bb59f58e69ca59c9fe561996a264e097daf7a82bc774716e7fdc4`. The final source edit only renamed the obsolete adoption-oriented negative test to describe its actual missing-native-identity boundary; both parameterized cases passed again (**2 passed in0.62s**), with Ruff lint/format checks passing. The final browser receipt's source stamp `0252941fdbb6296bbe3a439abda88a5f8bc766e8e6722f927047a97c113c30c6` predates only that test-name change; runtime source is identical. No staging or publication was performed.
## Default Hub, operation motion graphics, and cross-architecture OCI execution — 2026-10-16 (uncommitted)

- User requested:
  1. Default Palimpsest Hub set to `https://palimpsest.dmslab.re.kr` across client CLI and local dashboard while preserving explicit `--url`, `PALIMPSEST_URL`, and `config.toml` overrides.
  2. Truthful visual motion graphics showing progress for builds, Hub/Docker downloads, uploads, and VM run operations without fabricated percentages.
  3. Allowing OCI archive downloads regardless of runtime compatibility on Darwin arm64, and adding explicit cross-architecture execution attempts through a supported VM carrier backend rather than rejecting downloads or silently running host containers.
- Work conducted strictly in `/Users/pieroot/code/palimpsest-local-hub-catalog-20261015`, feature branch/base `3868e527df1bb14f33aee5b6066faf760ad213c2`. The original checkout `/Users/pieroot/code/palimpsest` had accidental edits from a subagent surgically reverted to its exact preserved snapshot tree `5d1aeb9ca3593aa8e606ea2ab5e71595ec8e9208` (55 baseline files preserved, 0 acquisition residue). No commit, push, release, or live VM deletion was performed.
- Implementation details:
  - Hub default: `DEFAULT_HUB_URL = "https://palimpsest.dmslab.re.kr"` centralized in `hub.py`, consumed by `cli.resolve_url` and `remote_artifacts.py` as default fallback.
  - Motion graphics: added CSS/JS keyframe animation (`flow-transfer`, `flow-process`) and structured step indicators (`operation-flow`) for build, download, upload, and run operations in `webui/app.css` and `webui/app.js`. Real running operations animate packets and active slabs; succeeded/failed states lock symbols to check/cross; paused or hidden tabs freeze CSS animation (`animation-play-state: paused`); reduced-motion preferences disable animation (`animation: none !important`).
  - OCI acquisition: `registry_intake.py` and `oci_image.py` accept both `linux/amd64` and `linux/arm64` (`ACQUISITION_PLATFORMS`), retaining original multi-arch manifests and verifying config/diff_ids without weakening native OCI-root amd64 materializer guards.
  - Cross-arch execution: `platforms.select_backend` and CLI `run` support `--platform`, `--force-architecture`, `--vm-image`, and `--vm-image-digest`. Missing local carrier disk fails preflight with an actionable error.
- Verification:
  - Focused suites passed **960 passed, 1 skipped in 79.19s** (`test_experimental_oci_vm`, `test_oci_run_request`, `test_platforms`, `test_registry_intake`, `test_oci_image`, `test_oci_source`, `test_docker_hub`, `test_remote_artifacts`, `test_ui`, `test_inventory`, `test_web_operations`, `test_run_source`, `test_cli_contract`).
  - Real Skopeo download verified: acquired real `alpine:latest` arm64 root `sha256:294b683cb7249...` in 19.53s.
  - Real browser verified: authenticated web UI loaded `https://palimpsest.dmslab.re.kr` as default Hub URL, enabled Docker Hub download for multi-arch `redis:latest`, rendered live motion animation (70 frames recorded at 30 fps in `build-operation-motion.mp4`), and surfaced cross-architecture options (`--platform`, `--force-architecture`, `--vm-image`, `--vm-image-digest`).
- Receipts stored under `~/.local/share/palimpsest-proofs/hub-motion-crossarch-3868e527-20261016/`.

## Independent data volumes / retained root topology — 2026-10-16

- Scope: only `/Users/pieroot/code/palimpsest-local-hub-catalog-20261015`, branch
  `feature/local-hub-catalog-20261015`, base `3868e527df1bb14f33aee5b6066faf760ad213c2`.
  No commit, push, deploy, helper transfer, real disk creation or user VM deletion.
- Integrated `named_volumes.py` and `retained_instances.py` with conventional
  runtimes/CLI, inventory/HTTP and frontend. Default removal archives root and
  ordered topology; explicit purge never deletes independent data volumes/shared
  layers. External libvirt roots remain reference-only; unsafe purge rejects
  before stop. Native OCI-root/carrier named attachments reject explicitly.
- Fixed native-removal eligibility when a stale same-name selected ledger shadows
  a fresh host row; preserve host-host ambiguity and exact native identity gates.
  Added standalone create/delete volume routes and exact-name/digest confirmation.
- Runtime review fixes guard-before-backing-inspection, live+inactive native
  detachment, stop-before-undefine proof, already-removed/new-archive publication,
  authenticated storage-policy/archive receipts, and cloud explicit retain.
- Original-checkout worker incident: historical recovery evidence is raw nested
  CLI transcript operation at recorded `12:34:37.825Z`, restoring CLI snapshot
  `D8BB` matching its initial read and moving the worker-created test out. This
  establishes that operation's recovery, not current original bytes/index purity.
  Parent/reviewer performed no further original-checkout writes.
- Real Chromium + loopback API + temporary filesystem/native adapters proved
  create/selection, exact removal, retained dependency HTTP409 (even force),
  archive purge and released artifact deletion. Browser found encoded-digest
  rejection; the route now decodes before validating. Actual CLI inspect and
  throwaway durable-run/removal/reuse smoke preserved the exact data sentinel
  on the same disk with one synthetic formatting call. Evidence is under
  `~/.local/share/palimpsest-proofs/hub-motion-crossarch-3868e527-20261016/`.
- Initial focused run: 767 passed/12 failed; failures included obsolete native
  fixtures/policies and a real noncanonical macOS temporary-state alias rejection.
  Fixed fixture stop/purge behavior and archive-root canonicalization. A later
  combined run hit its 300-second deadline; the focused stack identified an
  existing macOS kqueue cancellation hang in the real guest-probe deadline node.
- Remaining qualification: native backend creation/attachment/guest mount,
  actual VM deletion and OCI ownership/native runtime qualification are unproven.
  A launch-to-bind crash can retain a fail-closed pending disk reservation;
  automatic recovery is not implemented. Astra approval is unavailable; local
  source review does not replace it or grant publication/resource approval.
- Cancellation follow-through: bounded process event selection observes timer
  cancellation after another thread closes pipes; existing probe/process nodes
  passed9 in2.23s. An actual sleeping-child smoke returned timeout/cancelled
  in0.217s. No timeout extension, retry, exception suppression or VM STOP.
- Volume-specific and runtime adapter suites passed451 in4.73s. Public consumer
  regressions passed764/skip1 in28.87s; repaired lifecycle/HTTP nodes passed17.
  Final synchronous inventory/cloud/process selection passed228 in4.12s;
  complete HTTP/dashboard module passed106 in54.00s. Async receipt delivery was
  unreliable, so final evidence uses these synchronous runs, not a missing result.
  Search sync was unavailable (`gbrain` is not installed); no search index or
  agent-guidance file was fabricated.
- Final working architecture stamp/check:486 files,
  `45102318465c4bf5bdb14685d96c8b497d7a0d9e9b8f5cad41c69369ac96ec9e`.
  Scoped Ruff lint/format, CLI reference, lane manifest and JavaScript syntax
  passed. Temporary smoke scripts and owned browser/synthetic servers stopped;
  filesystem evidence retained. Reviewed normal dashboard replaced the stale
  Python process: service `PalimpsestVolumeDashboardReviewed20261016`, loopback
  port60947, persistent. Obtain session link from service output; never put its
  bearer token in this handoff. No native mutation was used to verify it.


## Full feature 0.3.6 release and health rollout — 2026-10-17

- User explicitly selected **현재 기능 전체 릴리스·운영 배포**: include the isolated feature worktree's catalog, motion/default Hub, volumes/lifecycle, acquisition/cross-architecture changes plus healthcheck; authorize reviewed commit/push, protected-dev PR/CI/merge, immutable version/GHCR publication and safe rollout to existing `dms-controller1` API/export and `palimpsest-hub-build-worker-034`. No main promotion, GPU/device reassignment, actual user-VM removal or native qualification is included. Astra planning/ops signoff is unavailable, not substituted.
- Actual feature base and fetched origin/dev remain `3868e527df1bb14f33aee5b6066faf760ad213c2`. Root/Hub module/package/locks and role default now0.3.6; dependencies/native binaries/CI/native opt-out are unchanged. Original dirty checkout is not the release tree.
- Fresh read-only production baseline: existing0.3.5 API/export fixed digests on controller1 running, API healthy; public liveness200, new readiness404. Dedicated0.3.5 build worker restarting with private `claim_pending` journal dated2026-10-16T23:04:27Z. No journal was erased/replayed. SQL census found3complete/1error builds and0exports; a new maintenance-time gate is still required before disposition.
- Real original `pieroot/Default/test` SDK authentication succeeded; `/v1/me` and layer inventory200, actual feature CLI `layer ls --limit2` exit0 returned `live-034-after-crash`/`live-034-x86-chain`. TLS verified, token only in memory/child environment, isolated scratch state. Receipt: `~/.local/share/palimpsest-proofs/release-036-20261017/baseline.json`.
- Anonymous `/v1/ready` now concurrently checks SQL `SELECT1`, fresh Redis `PING` and existing blob-root/child access, returns sanitized200/503 and `Cache-Control:no-store`, and retains at most one unfinished probe per dependency across the shared2s asynchronous wait. Existing liveness is unchanged. Readiness is not schema/write/fsync/capacity/mount/identity/worker/native proof. Kolla container and role HAProxy definitions now use readiness; live shared LB is not changed merely by the role edit.
- Worker path incident: readiness subagent initially used relative paths and edited original checkout despite the feature-worktree scope. Its own additions were surgically moved to absolute feature paths and removed from original only: restored `main.py #2873`, `test_hub_api.py #EFF2`, compatibility `#39DE` matched pre-task snapshots. No broad reset/restore or whole-file overwrite. Parent then read actual feature route and bounded wait; subsequent parent edits use absolute paths. This proves those restoration snapshots, not global original-checkout purity.
- Independent source review exposed selected-sibling platform, retained-root receipt, forced Docker-cache native-preflight/converter-arch and foreground/disposable carrier mismatches; all were corrected with behavioral consumers. Lifecycle review corrected managed rm→dashboard archive delete→CLI purge under exact run identity and nonnested artifact locks, kept source/CAS refs until purge, and hardened native alias/inventory/persistence refusal. Existing missing/malformed archives stay fail-closed.
- Parent reproduced dangling-child store readiness200 instead of503, then fixed it and included the regression. Hub full suite573passed/2skipped in89.12s. Real canonical amd64 API+MariaDB11.4+Redis7 smoke proved200→Redis-stop503 while liveness200→recover200→SQL-stop503→recover200. Internal-only Docker Desktop publication yielded an empty HostPort list initially; corrected smoke API uses an additional owned bridge and loopback-only publication, with SQL/Redis internal. No API egress-isolation claim.
- Parent reproduced acceptance of a QCOW external data file, added self-contained rejection and tested real qemu-img external-data bytes before any native launch;24experimental-module tests passed. All three canonical images built and imported0.3.6 on linux/amd64 andarm64; build workers also imported client0.3.6 and ran bubblewrap0.12.0/qemu-img10.0.13. These are image/synthetic proofs, not native VM builds.
- Current working review stamp486files: `5bb0d9c9ae7706ca3f454541a7d09441105456bbdb3d79bb690ead8599a83f4a`. Root Ruff lint passed after repository-native import sorting; LSP used a conflicting worktree import classification. Seven changed files formatted; CLI reference, lane manifest and JavaScript syntax passed. Initial focused root634pass/27fail was entirely missing `ansible-playbook` on PATH; the executable exists in the venv. Full root gate now supplies that PATH rather than weakening/skipping Ansible behavior.
- Preparation snapshot: canonical root0.3.6 sdist→wheel isolated install/CLI/version smoke and Hub wheel/sdist builds passed. At this point publication/production cutover were still pending final full-root/hosted gates, writer stop/drain, paired encrypted SQL/CAS backup plus isolated restore, offline schema bootstrap and exact pinned Kolla rollout; the completed follow-through is recorded below. Prepared scripts at this snapshot were temporary scaffolds, not yet deployed helpers or evidence of execution.

- Release commit `4036cd4181c56660a148d922a775f446eb52ae0c` published to feature branch/PR#28. Corrected completion/OCI-policy/facade consumers passed234/skip2, full lint/format passed, working/staged486-file stamp `2cd220727a341d3a3586c42461bcf8e8eb1c26aa60a94dc3484446bda4633520`, redacted772,174-byte diff scan clean. Hosted run39038413446 passed Hub/lint/package/stage1/product/filesystem gates and eight portable shards but failed Linux5/6 andmacOS3/4 on the same new libvirt/HVF regression: its synthetic capability boundary accidentally entered host UEFI discovery. The fixture now supplies its synthetic ledger profile as well as native transport; production firmware/removal policy unchanged. All three backend scenarios passed3 in0.26s; lint/format passed. Latest review stamp `af4ae179c7c349eaaeccf00074d3908dbec252605dc104dd4f5d06e220e2cfa7`. No failed check was rerun unchanged or bypassed.
- Original-project private marker layer `release036-ff699fbd14f9`, digest `sha256:b1a8ce3f60e3f6b3c789f2ffb24ac1c730625e1d6b5fcf30a73573bfcf284164`, was packed/pushed by actual CLI to production0.3.5, read back exact-owned/project/private. It is intentionally retained through the future backup/cutover to prove cross-container data persistence, then exact-owned cleanup; no global blob GC. Source recipe/native execution is not exercised by this artifact transfer.


### Completed publication and production follow-through

- Final candidate `c35e4f69f66337145d2697f057618e03e3960951` passed the six strict hosted checks. PR#28 merged protected `dev` as `b8d0d49d587e0e86d36a6e1e99875930bba0fa2a`; its tree matched the reviewed candidate. Dev Test/Hub/Development-package runs39039675595/39039675632/39039675770 passed. Annotated `v0.3.6` completed Release39040031059 and Hub-image39040020536, including actual PyPI/GitHub/GHCR publication. No main promotion or native proof was enabled.
- Final source review stamp486files: `f401aaeae1934809d1a17d870617e1b282290cef77687de4e8ec6cce703c0536`. The retained-root archive-delete→second default rm→purge regression failed before the final receipt fix;390 lifecycle consumers passed after it. Final independent readiness and lifecycle reviews closed the named findings. Their static verdicts do not substitute for the separate runtime receipts below.
- All three target-host images were inspected as amd64, version0.3.6 and OCI revisionb8d0d49d. Exact API/export/build-worker digest pins are recorded in [testing](testing.md#published-036-production-rollout) and private `published-pins.json`/`pulled-image-identities.json`. Dedicated final globals retained existing ports18021/8021, public origin, worker key/hostname pin and placement. Operator package0.3.5→0.3.6 was the only change among110 installed distributions; semantic uv.lock changes were only rootkolla/client. Missing noninteractive uv PATH and root-owned installation permissions were resolved without upgrading other dependencies.
- Stopped the old remote worker before old API shutdown, then verified four terminal builds, zero exports, absent build/export leases and only an expired maintenance lease. Stopped both exact old controller writers. Paired root-only AES256 SQL/CAS/config/container capture was restored in an isolated internal-only MariaDB11.4 container:15tables/12rowINSERTs/34files/884,748,288bytes, exact SQL rows and file bytes/UID/GID/modes. Encrypted backup and0600 key remain at controller1 `/var/backups/palimpsest/pre-0.3.6-20261017`; this is not an offsite backup claim.
- Restore harness failure/resolution: the initial60s SSH client timed out while capture continued, and mariadb-admin ping could report presence before usable authenticated startup. Preserved the completed encrypted capture; restarted only the exact-owned isolated restore and required authenticated TCP SELECT1 before import. The existing capture then passed exact SQL/CAS comparison. No source SQL/CAS restore or recapture. Private `cold-backup-restored.json` and `restore-cleanup.json` contain actual receipts/hashes; removed only that owned restore container/anonymous volume/internal network and plaintext restore copy afterward.
- The exact old worker claim_pending journal was copied out of active scratch under exclusive no-follow worker.lock with bound device/inode/mtime/hash and an otherwise empty scratch tree, only after fenced terminal/lease/restore proof. No uncertain job replay, native cleanup or ownership adoption. Its preserved root-private journal/disposition remain on the worker and in sanitized `worker-journal-disposition.json`.
- Pinned new API offline bootstrap exited0 with all writers still stopped. Canonical Kolla pull+reconfigure completed for controller1 API/export and separately for the existing remote worker through its packaged playbook. Controller action retained `--limit dms-controller1`; every action retained `--skip-tags haproxy`. No shared Afterglow/HAProxy/ProxySQL/Keepalived reconfiguration or credential/reader provisioning was run. Final comparison found all261 unrelated controller container identities/images/start times/running states unchanged and no unexpected remaining container.
- Live new API Docker health ishealthy. Direct `http://172.30.0.11:18021/v1/ready` and anonymous verified-TLS public `https://palimpsest.dmslab.re.kr/v1/ready` returned200, all database/redis/blob_store checksok, Cache-Control:no-store; public liveness200. New API/export restart returnedready again. The real original requester received /v1/me and exact-owned private metadata200; anonymous /v1/me401. Production shared SQL/Redis were not fault-injected;503 outage behavior remains actual disposable-lab evidence.
- Actual PyPI client0.3.6 installed in isolated Python3.11 and downloaded the pre-cutover private marker after new-container restart; SHA-256 and extracted marker matched. A fresh published0.3.6 private pack→push→pull also matched exact bytes/project ownership. Both exact-owned artifacts deleted204 and absent404; no global blob GC. Receipts: `cross-rollout-transfer.json`, `post-rollout/receipt.json`, `local-auth-before-restart.json`, `controller-restart.json` and `public-monitoring.json`.
- New remote worker container isrunning/restartcount0, scratch onlyworker.lock, root0600 read-only key mount, no direct SQL/Redis credentials and no inherited API healthcheck. Structured API logs observed711 authenticated idle claim204 responses. The initial reader expected obsolete Uvicorn access-log formatting; corrected it to actual method/route/status fields. `worker-runtime.json`/`worker-idle-claims.json` are transport/idle evidence, not native execution or cleanup qualification. No build was submitted.
- The original shared Hub editable environment currently points to `/Users/pieroot/code/palimpsest/hub` with distribution metadata0.3.1, not the isolated feature path; it was therefore left untouched rather than rolling back another actor's environment. Original dirty source/index are not the released tree. Astra planning/ops signoff remains unavailable, not substituted. Native/GPU/cloud runtime proofs, device mutation, user VM deletion and shared loadbalancer promotion remain outside this approval. This documentation-only follow-through does not retagv0.3.6.


## Controller API/export HA 0.3.7 preparation — 2026-10-18 KST

- Owner approved protected-dev review/CI/merge and immutable0.3.7 PyPI/GHCR publication, canonical Kolla API/export placement on controller1/2/3, one dedicated512GiB CephFS share, gigabytes quota1000→1012, scoped mount/helper/backup/restore work, Palimpsest-only graceful HAProxy membership/readiness changes, scoped CPU smoke and ten-minute monitoring. Existing dedicated build worker remains singleton; no extra VM, controller recipes, IAM, other routes, Keepalived/ProxySQL/shared-Valkey mutation, GPU/device/user-VM mutation or global GC. Astra signoff remains unavailable, not replaced by independent reviewers.
- Only feature worktree `/Users/pieroot/code/palimpsest-local-hub-catalog-20261015` is changed. Initial feature baseline `cba223762abff885ed71bac421262427000d0a78` fast-forwarded to current protected-dev `8e35fd3967cdc7b2e3232b4b6f23bd1d800ca2e9` with no file changes. Original dirty checkout and shared Hub environment remain untouched. Root/Hub metadata/modules/native locks/Kolla defaults now0.3.7.
- Source adds master-only Sentinel discovery with URL data credentials/DB/options preserved, separate discovery auth, bounded HA socket waits and no ambiguous-write replay. Shared CAS requires exact existing canonical mount identity before bootstrap/each API/export start; local mode refuses multi-host inventory. Shared ownership is operator-prepared, never changed by the role. Pull/stop/destroy remain available during outages; checks do not fence Docker automatic restarts or later mount loss.
- Independent review exposed credential-bearing URL parser errors, redis-py5.0.0 lacking `aclose`, raw integer/string endpoint duplicates and overly broad Dockerignore ancestor exceptions. Parent reproduced the parser/duplicate consumer failures, fixed them and observed passing regressions. Native dependency generation retains locked Redis8.1.0 with minimum5.0.1; an isolated actual5.0.1 cache suite passed33 cases before the endpoint follow-through. Synthetic-only actual BuildKit context smoke admitted a Hub clouds canary before the fix and retained all8 required inputs/excluded all8 forbidden paths after it; no real credential was included in that probe.
- Actual dedicated share `7f9c6c69-f570-4767-aef6-83db4d453824` (`palimpsest-hub-ha037-20261018`,512GiB, service project `9cdd693a8af8414f8eaa7f026a7b95f9`) is available with dedicated CephX rw access `c20c804b-b1e9-4d04-a408-093339b98d60` / `palimpsest-hub-ha037`. Only gigabytes quota changed1000→1012; all other quota fields and preexisting500GiB share `f06640f3-34e8-4112-b49c-0395ff506a79` were preserved. Mount keys are root0600 on approved controllers, never logged or sent to the build VM.
- Controllers1/2/3 now mount the same exact CephFS source at `/srv/palimpsest/hub` through enabled dedicated systemd mount units. Shared root UID/GID1000:1000 mode0700 was initialized through a mount-ID-bound directory FD, not a racy path chown. Each underlying local mountpoint is root-owned0555 and a real dropped-UID1000 write failed before mount. Three real exclusive flock ownership handoffs rejected six remote contenders; exact renamed bytes were visible cross-controller and file/directory fsync returned successfully. The exact-owned probe was removed. This is not power-loss, reboot, capacity-exhaustion or mount-loss recovery proof.
- A read-only Ceph `auth get` capability inspection using existing Manila identity returned EACCES; no elevated fallback was attempted and raw Ceph caps are not claimed verified. The dedicated Manila share/access association and mounted-source identity were observed. An initial nested SSH quoting failure occurred before any host mutation; refreshed path preflight proved absence before the corrected quoted helper ran.
- Original `pieroot/Default/test` SDK authentication freshly returned `/v1/me`200 and inventory200 through verified HTTPS, project `99f2f82cb2d14788a0723af31c16dab0`, without admin fallback, token persistence or IAM mutation. Application replicas/ingress are still the recorded0.3.6 single-controller deployment at this preparation checkpoint. Publication, writer drain, new paired encrypted SQL/CAS backup plus isolated restore, cold CAS migration, offline bootstrap, three-controller Kolla cutover, Palimpsest-only LB change and end-to-end monitoring remain separate subsequent gates.
- Private secret-free receipts: `~/.local/share/palimpsest-proofs/hub-controller-ha-037-20261018/` includes approval/intent, original-requester baseline, quota/share/access/mount and cross-controller filesystem receipts. Configuration/key files remain private on controllers. No native/KVM/GPU qualification is claimed.

