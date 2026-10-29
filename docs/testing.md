# Fast feedback without dropping coverage

## Full-current integration — 2026-10-30

Canonical full-root entrypoint is `uv run python scripts/test_lanes.py run full`
(`python -m pytest`), not the console-script launcher with a different import path.
Pre-final-review baseline:8574passed/272skipped in610.63s. That result does not
qualify the later six review corrections. Their regression cases exercise original
API delete journals, accepted/lost responses, no early budget release, before/after
intent expiry, station Cinder allocation expiry, definite no-call cleanup and
interrupted disk-latest publication without reprobes. Parent demonstrated18scenario
failures and5station failures/1control pass against the frozen pre-fix source;
corrected scenario45 and station cloud/disk125 cases passed separately. The final
complete root run is recorded in the latest handoff when settled.

An actual owned loopback authentication smoke made1plaintext HTTP request from
the old acceptance-script path, then the corrected shared helper rejected the HTTP
endpoint before0requests. All credentials were synthetic; no real endpoint/token
was used. Initial reproduction hit ambient SDK configuration instead and is not
claimed as transport proof. The temporary config/directory guard smoke exercised
real filesystem identity and rename/removal distinctions, not a real VM deletion.

API/export source is byte-identical to pushed `5c9a30b`, so its canonical1127/2Hub
proof remains bound to those bytes. The integrated five-image matrix passed;
the worker alone was rebuilt after companion scenario/station fixes and its four
corrected installed modules were hash-matched to source. All image smokes used
networknone/read-only containers, and amd64 execution was emulated. No new
privileged kernel, native-cloud, GPU, provider isolation or production proof is
implied by these source-integration checks.


## Hub execution API candidate — 2026-10-29

This source-only feature branch adds layer PATCH, durable image-job and instance
APIs. It does not release a new package, deploy services or implement the target
Manila-managed CephFS runtime. Start with the changed-node checks and canonical
Hub environment; pytest includes `../src` for the dedicated worker's companion
tests without adding client dependencies to the Hub controller package:

```sh
uv sync --frozen --extra dev --extra cloud
uv sync --project hub --frozen --extra dev
uv run --project hub pytest -q hub/tests
uv run python scripts/test_lanes.py run build-registry
uv run pytest -q tests/test_kolla_remote_build.py tests/unit/test_architecture_guard.py tests/unit/test_test_lanes.py
uv run python scripts/test_lanes.py list --check
uv run python scripts/generate_cli_reference.py --check
```

Fresh remediated-source checkpoints: Hub1127passed/2skipped, build-registry575
passed/4skipped and Kolla/guard/lane164passed. Ruff lint/format, root/Hub
wheel/sdist builds and minimumPython3.11 imports passed. Canonical Dockerfile
builds and network-none real health/OpenAPI smokes covered API/export arm64 and
amd64 plus the amd64 build worker; x86 execution used local emulation, not a
native amd64 worker. The registered main app also served real host-loopback
requests against temporary SQLite/CAS and synthetic identity: context bytes,
registry request-id idempotency, Dockerfile admission, conflict409, cross-project
404 and wrong-subject403 passed. These are not live Keystone/provider effects.

The separately approved single privileged local container ran the exact
`test_native_scratch_bounds_temp_kill_journal_and_exact_loop_cleanup` body via a
standard-library launcher: real256MiB preallocation, many-file ENOSPC, writable
outer journal/output reserve, killed temporary-state containment and exact loop
detachment passed within264MiB peak new data. The container had1GiB RAM/128PIDs,
networknone and no host binds; its settled container/private volume were removed
and absence checked. Evidence remains private under
`~/.local/share/palimpsest-proofs/hub-api-push-20261029/`.

The permanent Linux scratch regression is opt-in via
`PALIMPSEST_IMAGE_SCRATCH_LINUX_SMOKE=1`; it requires a separately authorized
disposable root Linux environment with mount/loop permissions and non-CoW
ext4/xfs/tmpfs backing. Do not enable it on a shared host implicitly. Existing
detached-session cgroup cleanup tests need their separate delegated-cgroup
prerequisites and were not newly qualified by the scratch proof. No cloud VM,
GPU, production schema, Manila grant/revocation or deployment proof is claimed.


## Supervisor frame-commitment qualification — 2026-10-27

Patch0.3.9 is prepared from fetched origin/dev `73667c8f4065215416e775f12cb0a6f578853044` (published0.3.8). Native amd64 control exposed a real equal-metadata non-tail rewrite defect despite hosted release success; the previous receipt remains unchanged under `~/.local/share/afterglow-proofs/palimpsest-qualification-20261026/release-stall/native-amd64-server/`. Compact trusted frame commitments repair requested-frame integrity, not instant auditing of unrequested history. Deterministic coarse-stamp tests preserve actual file bytes, inode/path/ownership checks; stamp-change tests explicitly advance mtime rather than depending on clock resolution.

Qualification uses focused supervisor tests first, then canonical full root/Hub, Ruff, package, CLI-reference, lane and architecture checks. Authorized native amd64 execution uses only a unique throwaway Docker container on `pieroot-server`, Python3.12, UID1000, umask022, frozen locks, final explicit source archive, private disposable scratch and read-only proof inputs; no existing service, runner, private, socket or device mounts. The previous helper's200-case natural same-stamp experiments are preserved and repeated for invalid and recomputed checksums on tmpfs and overlayfs. Source hashes, runtime stamps, logs, commands and verdicts are stored separately under `~/.local/share/afterglow-proofs/palimpsest-journal-integrity-20261027/`. No current-source full qualification is claimed until these final-byte gates finish; native KVM, guest proofs, publication and production cutover remain separate Main-owned gates.

Canonical initial local root qualification returned7478 passed/265 skipped/3 failed: existing SIGINT web shutdown cases timed out. Their child signal dispositions were not captured, so inherited SIG_IGN is not asserted as the proven historical cause. A separate controlled actual CLI proof showed default SIGINT exits0, inherited SIG_IGN ignores SIGINT, then SIGTERM exits0 and closes the socket. Main authorized the minimal explicit SIGINT/SIGTERM handler installation/restoration fix and deterministic actual-process inherited-ignore plus original-handler restoration coverage; no timeout or browser-loop relaxation. Prior failed logs remain preserved. Journal-only final native qualification separately passed7314/97 skipped on7411 collected cases, with0 altered frames returned in each200-case natural-stamp attack; these receipts precede the additional UI repair and do not qualify its final bytes.

Final lifecycle-complete reviewed source (`source_sha256=953ecbaf38fb3cda9380b28d49de2c5f8fd162f103a0ebbe4b55da7ab3f9ca96`,502 architecture-covered files) passed focused70; macOS arm64 CPython3.13.12 canonical root7491/265 skipped; Hub654/2 skipped; root/Hub Ruff lint+format, both wheel/sdist builds, isolated Python3.12 installed-wheel CLI0.3.9, CLI-reference/lane/OpenSpec/architecture checks. Actual default and inherited-SIG_IGN CLI consumers both exited0 on SIGINT with sockets closed. No earlier failure is overwritten.

The exact final explicit archive `source-release-final.tar` SHA256 `afff4d537f7784e300c6a29dd55bd6daaebea24d437eb92a9b908cb70775eb13` ran on native amd64 Python3.12.15 UID1000/umask022:7421 collected,7324 passed/97 skipped in575.23s. The unchanged previous stamp helper ran200 cases per checksum/filesystem combination. Tmpfs observed178 identical-stamp invalid-checksum and143 identical-stamp recomputed-checksum rewrites; every pull rejected,0 altered frames returned. The historical recomputed-checksum control returned169/200 altered outputs. Overlayfs observed no identical-stamp cases, so it is not claimed as natural equal-stamp coverage. Actual file/path binding remains in the deterministic coarse-state regressions. Owned proof containers were removed; one post-proof SSH cleanup connection timed out, then exact-owned cleanup succeeded on reconnect. Runtime logs, commands, source/evidence hashes and `verification.json` remain under the evidence root above. These CPU/filesystem tests preserve opt-in skips and do not prove KVM/guest/BuildKit/cloud, hosted release or production cutover. No commit/push/tag, private config or production mutation occurred.


## Isolated catalog/unpack assessment — 2026-10-26

This checkout prepares a source-only0.3.8 catalog/unpack candidate from latest origin/dev0.3.7. Catalog-only fresh-human/system graph and targetless native policy primitives are extracted without replacing current project auth/package ownership/key/publication behavior or SQL schema. Existing native capability labels are reused for consumer download eligibility. Qualification receipts live under `~/.local/share/afterglow-proofs/palimpsest-qualification-20261026/`; historical2026-10-25 catalog proof covers older dirty working-source bytes, not this integration.

Qualification owner should start with these root commands (retain the root venv on PATH so genuine `ansible-playbook` is available):

```sh
uv sync --frozen --extra dev
PATH="$PWD/.venv/bin:$PATH" uv run pytest -q tests/unit/test_bundle_unpack.py tests/unit/test_oci_layout.py tests/unit/test_refs.py tests/unit/test_cli_contract.py tests/unit/test_cli_project.py tests/unit/test_run_source.py tests/unit/test_cli_reference.py tests/unit/test_test_lanes.py
uv run python scripts/generate_cli_reference.py --check
uv run python scripts/test_lanes.py list --check
PATH="$PWD/.venv/bin:$PATH" uv run python scripts/test_lanes.py run full
uv run ruff check .
uv run ruff format --check .
uv build
```

Hub must remain a separate environment: from `hub/`, `uv sync --frozen --extra dev`, then focused `uv run pytest -q tests/test_catalog.py tests/test_catalog_auth.py tests/test_auth.py tests/test_packages.py`; full `uv run pytest -v tests`, `uv run ruff check .`, `uv run ruff format --check .`, `uv build`. Architecture freshness must be stamped only after independent source review and checked with `python3 scripts/check_architecture.py --staged` once Main owns the staged release index. The new strict default system:manage policy is role:admin only after fresh independent human/system authority; an explicitly configured missing/malformed policy fails503, and project-target policy cannot inherit a fabricated scope.

Canonical `docker/hub/Dockerfile` targets are `palimpsest-hub-api`, `palimpsest-hub-worker` and `palimpsest-hub-build-worker`. Build/load each target with `docker buildx build --platform linux/amd64 --target TARGET -f docker/hub/Dockerfile --load -t UNIQUE_LOCAL_TAG .`; independently build/run arm64 if claiming arm64 qualification. Hosted publication currently declares amd64 only. Verify actual installed version, entry-module imports, platform and non-root API/export execution; build-worker intentionally executes as root in the dedicated worker VM. Do not treat import smoke or synthetic auth as live cloud/guest qualification.

Local qualification on2026-10-26 prepared separate root/Hub frozen-lock CPython3.13.12 environments. Initial focused suites passed443 root and206 Hub cases; initial canonical full suites passed7,465 root (265 skipped) and648 Hub (2 skipped). Ruff exposed three extracted import blocks and five formatting-only files. Independent source review then found two concrete compatibility/eligibility defects: optional native `blob_digest` absence incorrectly rejected independently bound annotated/leaf config architecture, and descendant base declarations were ignored by complete-DAG eligibility. Qualification repairs preserve explicit blob mismatch rejection, unknown unannotated ancestor architecture, valid no-base reusable chains and existing authorization semantics. Consumer regressions and final related/full/image-cycle receipts, exact commands and source hashes are retained in the proof directory above; cancelled pre-repair cycles are not final proof. Main owns independent architecture review/stamping and protected hosted gates.

Final reviewed source cycle: focused root449/Hub212 passed; canonical root full7,471 passed/265 skipped and Hub full654 passed/2 skipped; root/Hub Ruff lint+format, CLI-reference/lane checks and both wheel/sdist builds passed. Each six-case regression failed five cases under its prior function in an isolated pytest process (repository bytes unchanged), then passed in the full fixed-source gates. Real installed CLI unpack and run-source stack resolution passed for annotated three-entry and unannotated base-only native packages omitting optional blob identity; no VM was launched. Actual SDK/Uvicorn consumer HTTP omitted the contradictory DAG and administrator detail changed complete→incomplete as dependency declarations conflicted. Final image/source hash receipts cover all six rebuilt architecture/target combinations in `reviewed-final/`.

Actual installed CLI `bundle unpack` verified a three-entry synthetic native OCI archive. Actual loopback Uvicorn→installed Keystone SDK→synthetic identity HTTP plus SQLite rows exercised real catalog authorization/policy (no dependency authorization overrides): fresh human/system authority returned200/no-store; missing token/target-bound projectless reads401; revoked/delegated/disabled human and policy denial403; partial native graph and malformed policy503; policy recovery200, with `@` unable to bypass missing system authority. These synthetic identity/SQL/payload fixtures do not prove live Keystone, genuine qcow2/SquashFS boot or production schema compatibility.

All three canonical targets built and executed locally for both linux/amd64 andlinux/arm64 with unique proof tags, reporting Hub0.3.8 andx86_64/aarch64; API/export UID1000 and build-worker UID0. Both build-worker images also executed client0.3.8 unpack and native filesystem-tool version commands. Both API images served liveness200 and unauthenticated catalog401 under `--network none` with application lifespan disabled: this deliberately starts no SQL/Redis/bootstrap writers and is not readiness, export-loop, recipe, KVM, BuildKit or production-deployment proof. No protected environment/variable, original sibling checkout, shared runtime, publication or production state was changed.

Protected-dev PR requires all six strict checks: Pure contracts (Python3.12), Unit tests (macOS15), Hub tests/lint/build, OCI filesystem proof (privileged Linux), Local OCI image product build and Guest stage-1 binary (Linux x86_64). Exact tag verification additionally runs the pinned stage-1/workload fixture reproduction and isolated BuildKit scenario in `.github/workflows/release.yml`. Native KVM is literal `PALIMPSEST_KVM_ENABLED=true` plus successful exact-ref native proof, or explicit `false` plus skipped proof with no qualification claim. Do not change this variable, bypass policy or auto-approve protected environment `palimpsest-native-kvm`: enabled runs retain reviewer `jung-geun`, no admin bypass, bounded exact-owned one-VM cleanup and repo-wide serialization. Refresh actual server-side protections/variable before release; these source requirements are not live settings evidence.

The candidate changes no Hub schema or nullable provenance/key ownership. Catalog reads existing layer/access/build/export SQL rows; original linked outputs establish provenance and caller metadata never attests it. Hub0.3.7 bootstrap has no migration ledger and `create_all` alone is not existing-schema compatibility proof. For any authorized native rollout, drain the HTTPS build worker before API/export/upload/GC writers, preserve uncertain journals/scratch, take paired SQL/CAS backup from one no-writer interval and prove isolated restore, then use matching immutable API/export/build-worker image revisions and operator wheel. Preserve current Sentinel/shared-CAS mount qualification, ports, protected IDs, reader credential and private configuration. No schema/IAM repair, ledger rewrite, mixed-version recovery, shared-service restart or production acceptance occurred here. A running0.3.7 Hub lacks the new catalog endpoints and CLI unpack; Afterglow catalog BFF propagates native4xx/5xx instead of returning an empty success, but native0.3.7 silently ignores the new package q filter, so it is not matching search semantics.

## 0.3.6 qualification and production — 2026-10-17

Owner approved the isolated feature source's full release and existing Kolla
rollout, not native/GPU qualification. Durable receipts are under
`~/.local/share/palimpsest-proofs/release-036-20261017/`.

- Actual original `pieroot/Default/test` SDK→verified public HTTPS `/v1/me` and
  layer inventory returned200; real local CLI inventory exited0. Existing
  production0.3.5 liveness200, readiness404; API/export running, dedicated worker
  restarting with preserved `claim_pending` uncertainty. This is a baseline,
  not new-version rollout success.
- Hub full suite **573 passed,2 skipped in89.12s**. New readiness regressions
  include actual temporary SQLite/store, Redis failure/recovery, bounded shared
  probes/caller cancellation and secret sanitization. A dangling existing child
  link initially returned200, then failed closed after the regression/fix.
- Canonical amd64 API + real isolated MariaDB11.4/Redis7: normal readiness200,
  Redis outage503 with process liveness200, recovery200; SQL outage503 and
  recovery200, all readiness responses `Cache-Control:no-store`. Docker Desktop
  did not allocate an internal-only network's dynamic API publication; corrected
  harness added a separate API bridge and loopback-only host publication. DB and
  Redis stayed internal; API egress isolation is not claimed.
- All three canonical API/export/build-worker targets built and executed as
  **linux/amd64 andlinux/arm64**, importing Hub0.3.6 onx86_64/aarch64. Build-worker
  also imported client0.3.6 and executed bubblewrap0.12.0/qemu-img10.0.13.
  Production image automation still declaresamd64; these local source builds
  are not published arm64 manifests or native VM/runtime qualification.
- A real qemu-img QCOW disk with external data bytes was rejected before native
  launch; external bytes preserved and cleanup completed. Its consumer regression
  failed before the guard; complete experimental module **24passed** after.
- Canonical root0.3.6 sdist→wheel build and isolated minimum-Python3.11 install/
  CLI version/help smoke passed; independent Hub wheel/sdist built. Initial scoped
  root selection **634passed/27failed**: every failure was absent
  `ansible-playbook` on PATH, not an Ansible product failure. The installed venv
  executable was then verified and supplied to the full gate.
- Full root gate **7,390passed/264skipped/3failed in369.29s** exposed stale
  completion enumeration and cloud-retain rejection expectations, plus a
  source-text architecture false positive on named-volume inventory equality
  (not backend routing). Removed obsolete reject/source-text assertions; retain
  behavior remains covered by CLI/runtime consumers. Completion now exercises
  new-backend prefix matching and an invalid prefix rather than copying the
  complete incidental choices list. Corrected consumer checks and exact-ref
  hosted verdicts are recorded after they execute, not inferred here.
- Release diff secret scan: **no leaks** in765,952 scanned bytes. CLI reference,
  lane manifest and JS syntax passed; repository-native import sorting corrected
  LSP's differing worktree classification, and seven changed files formatted.
  No native checks were enabled or skipped-as-success; policy remains explicit
  `PALIMPSEST_KVM_ENABLED=false`.

### Published 0.3.6 production rollout

[PR #28](https://github.com/openstack-afterglow/palimpsest/pull/28) passed all six
required hosted checks on `c35e4f69f66337145d2697f057618e03e3960951` and merged
into protected `dev` as `b8d0d49d587e0e86d36a6e1e99875930bba0fa2a`.
The merge tree matched the reviewed candidate. Dev Test/Hub/Development-package
runs `39039675595`, `39039675632`, `39039675770` succeeded. Annotated `v0.3.6`
completed [Release](https://github.com/openstack-afterglow/palimpsest/actions/runs/39040031059)
(verification, PyPI and GitHub release) and the
[Hub image workflow](https://github.com/openstack-afterglow/palimpsest/actions/runs/39040020536).
Final repeated-removal consumers passed **390** locally; independent readiness
and lifecycle reviews closed their named findings. The reviewed source stamp
covers 486 files, `f401aaeae1934809d1a17d870617e1b282290cef77687de4e8ec6cce703c0536`.

All three published images were inspected on their target hosts: Linux amd64,
OCI revision `b8d0d49d587e0e86d36a6e1e99875930bba0fa2a`, version `0.3.6`.
The unknown-platform descriptor is attestation metadata, not arm64 publication.

| Image (`ghcr.io/openstack-afterglow/`) | Immutable digest |
| --- | --- |
| `palimpsest-hub-api` | `sha256:d278ce1617e208bf42b2ecd7be99abe444487e7f9a3e0b13b7e35cfe78547dc2` |
| `palimpsest-hub-worker` | `sha256:0afb13a900e218be383626dd581c2c98a03b004005cb464fdec7af83e45e2a56` |
| `palimpsest-hub-build-worker` | `sha256:ccdc7a1430fb2e38f6b0d4c83c6d82a7b3dd7d3960a51eafc3c5220856740057` |

- **Operator preservation:** only `palimpsest-client` changed among 110 installed
  packages, from 0.3.5 to 0.3.6. Semantic lock changes were limited to the root
  `kolla` requirement and that package. Existing private inputs and final
  `99-palimpsest.yml` precedence, ports, worker hostname pin and placement were
  preserved. The noninteractive shell lacked `uv` on PATH; its actual binary
  was used. Sync needed existing root-owned environment permissions; no other
  dependency was upgraded or removed.
- **Writer-fenced recovery:** the remote worker stopped while the old API was
  available; all four builds were terminal, exports were empty, build/export
  leases were absent and the sole maintenance lease was expired. API/export
  then stopped before paired SQL/CAS capture. Isolated restore matched **15
  tables, 12 row INSERTs, 34 files and 884,748,288 bytes**, including file UID,
  GID and mode. Root-only AES256 backups and a 0600 key remain on controller1
  at `/var/backups/palimpsest/pre-0.3.6-20261017`; hashes are in the private
  `restore-cleanup.json` receipt. This is an isolated-restore proof, not offsite
  disaster-recovery availability.
- **Resolved restore harness failures:** the initial SSH helper had a 60-second
  client timeout while the capture continued. The first isolated SQL attempt
  also used `mariadb-admin ping`, which reported server presence before valid
  authenticated startup. Recovery retained the existing encrypted capture and
  restarted only the exact-owned restore container, requiring authenticated
  TCP `SELECT 1` before import. SQL rows and restored CAS then matched exactly;
  production SQL/CAS were not recaptured or restored over. The isolated server,
  its anonymous volume/internal network and plaintext restore copy were removed
  after receipt/hash verification; encrypted recovery material was retained.
- **Offline bootstrap and Kolla:** the pinned new API bootstrap exited 0 while
  old writers remained stopped. Canonical `pull` + `reconfigure` targeted only
  controller1 API/export and, through the packaged worker playbook, the existing
  dedicated worker. Every action retained `--skip-tags haproxy`; no shared
  Afterglow, HAProxy, ProxySQL or Keepalived reconfiguration was performed.
- **Live API:** API Docker health is `healthy`; direct
  `http://172.30.0.11:18021/v1/ready` and anonymous certificate-verified public
  `https://palimpsest.dmslab.re.kr/v1/ready` returned 200 with SQL/Redis/blob
  checks all `ok` and `Cache-Control: no-store`. Public `/v1/health` remained
  200. The real original `pieroot/Default/test` requester obtained `/v1/me` and
  private metadata 200; anonymous `/v1/me` remained 401. No production database
  or Redis outage was injected; outage/503 proof above remains disposable-lab
  evidence only.
- **Actual published local client:** isolated Python 3.11 installed PyPI
  `palimpsest-client==0.3.6` and reported 0.3.6. It downloaded the private layer
  uploaded before cutover to 0.3.5 after the new API/export restart: SHA-256 and
  extracted marker matched. A second fresh private layer was packed/pushed/
  pulled using the published 0.3.6 client against the new API, again matching
  exact bytes and original project ownership. Both exact-owned SQL artifacts
  were deleted with 204 and verified absent with 404; no global blob GC ran.
- **Worker recovery and live transport:** the old exact `claim_pending` journal
  was preserved outside active scratch, with inode/hash checks under exclusive
  `worker.lock`, only after the fenced terminal/lease and restore gates.
  Nothing was replayed or adopted. The new worker is running with restart count
  zero and only `worker.lock` in scratch; the key mount is read-only/root/0600
  and SQL/Redis credentials and API healthcheck are absent. Actual structured
  API logs recorded 711 authenticated idle-claim HTTP 204 responses after its
  start. An initial proof parser expected Uvicorn access-log syntax; reading
  the actual structured `method=POST route=... status=204` format corrected
  that evidence harness, not service behavior. No native build was submitted.
- **Scope:** all 261 unrelated controller container IDs, images, start times and
  running states matched the pre-cutover baseline; no unexpected container
  remained. Astra planning/ops signoff remains unavailable, not substituted.
  No main promotion, GPU/device reassignment, user VM deletion or native/cloud
  runtime qualification was performed.

Private JSON receipts remain under
`~/.local/share/palimpsest-proofs/release-036-20261017/`; they contain sanitized
ownership/result evidence, not persisted requester tokens. The immutable
release tag is not changed by this documentation-only follow-through.


## Full-source 0.3.5 integration — local verification

Integration began on the 0.3.4 base `ad1674e842c6cefaf4e2d38705b109a579f56040`.
The isolated 0.3.5 clone received the following parent-run local qualification
on 2026-10-14 before release. These local/disposable receipts are distinct from
the hosted CI, publication and production receipts recorded after them below.
Historical counts still apply only to their recorded source/environment; the
earlier SQL/KVM ownership receipt does not qualify this HTTPS build cutover.

- **Parent fixes before the passing gates:** `ruff check --fix` corrected nine
  import-order/unused-import issues; `ruff format` formatted 38 root and ten Hub
  files, all within integrated or version-metadata paths. The test-only repair
  to `tests/unit/test_cli_registry.py::test_oci_local_cache_build_uses_no_native_or_legacy_authority`
  supplies a context Dockerfile and keeps output outside the build context;
  production validation is unchanged.
- **Root:** `uv sync --python 3.12 --frozen --extra dev`;
  `scripts/test_lanes.py list --check` passed; `scripts/test_lanes.py run full`
  reported **7,064 passed, 264 skipped**. Opt-in native/KVM/BuildKit proofs were
  skipped, not qualified. Root `ruff check .` and `ruff format --check .`, OCI
  fixture generation `--check` and CLI reference generation `--check` passed.
  `scripts/build_package.py --out-dir <owned output directory>` built and
  smoke-installed the `palimpsest_client-0.3.5` wheel and sdist.
  `uv run --python 3.11 --no-dev palimpsest --version` returned `0.3.5`;
  this is a minimum-interpreter CLI smoke, not a full Python 3.11 suite.
- **Hub:** frozen dev sync; `pytest tests -q` reported **559 passed, 2 skipped**;
  Ruff check/format passed; `uv build` built `palimpsest_hub-0.3.5` wheel and sdist.
- **Disposable MariaDB ownership:** the separate
  `test_shared_sql_process_ownership_smoke` run reported **2 passed**, using an
  empty loopback MariaDB 11.4 `palimpsest_ownership_*` database selected by private
  `PALIMPSEST_OWNERSHIP_SQL_URL`. Two and three real SQL/process workers used
  independent flocks. Remote execution and provider effects remained synthetic;
  this is not a live-build or production compatibility proof.
- **Hub images:** canonical `docker/hub/Dockerfile` targets
  `palimpsest-hub-api`, `palimpsest-hub-worker` and `palimpsest-hub-build-worker`
  built for the declared `linux/amd64` platform and executed under local
  emulation. Each imported its entry module and reported `palimpsest-hub 0.3.5`
  on `x86_64`; the build-worker also contained `palimpsest-client 0.3.5` and
  `bwrap`, `mksquashfs`, `mkfs.ext4`. This is image execution evidence, not
  native KVM, BuildKit, cloud or production proof.
- **Public diff scan:** redacted gitleaks found one false positive in vendored
  MIT minified `xterm.js` (`FourKeyMap`).
- **Hosted macOS 15 finding:** the first PR run passed Linux, Hub, OCI and
  guest jobs but failed six `test_web_process_browser_choice_and_shutdown`
  cases on the macOS 15 runners only (local macOS and the same portable shard
  passed). A throwaway diagnostic run's faulthandler stack showed the `web`
  child blocked in `socket.getfqdn()` inside `HTTPServer.server_bind`. The
  dashboard now uses a loopback server that skips that reverse lookup;
  `test_dashboard_bind_does_not_reverse_resolve_loopback` fails against the
  stdlib default bind and passes with the fix, and `tests/unit/test_ui.py`
  passed **87** locally.
- **Hosted Linux flake:** the rerun then failed WIP-added
  `test_interactive_bash_readline_tab_completion` once on Linux (`--backend=a`
  captured before readline inserted `auto`); it also failed 1/12 locally. The
  test now waits for the expected completion text, or two idle seconds for its
  negative case, within a 15-second bound instead of a fixed 0.4-second sleep:
  **20/20** local runs passed and `tests/unit/test_completion.py` reported
  85 passed, 2 skipped.

**Hosted CI and publication — 2026-10-14:** [PR #24](https://github.com/openstack-afterglow/palimpsest/pull/24)
passed all six strict required hosted checks on `1ff87efe` and merged into
protected `dev` as `04d51ad6e1bf00c4b6e5bf82050383d0bb0a10cf`. Dev CI on
`04d51ad6` succeeded for Test, Hub images and Development package. The two
pre-merge fixes were `84e38103` (loopback dashboard bind without reverse DNS)
and `1ff87efe` (completion-test deflake described above). Annotated `v0.3.5`
completed the Release workflow (verify, PyPI publish, GitHub release) and the
Hub image workflow. PyPI `palimpsest-client` 0.3.5 SHA-256 digest prefixes:
wheel `ad0ee712cecef648`, sdist `f53642bbe1a2d2bb`.

Published GHCR Hub images have amd64 platform, OCI revision `04d51ad6` and
version label `0.3.5`. Each executed under amd64 with `--network none` and
successful entry-module import:

| Image (`ghcr.io/openstack-afterglow/`) | Digest |
| --- | --- |
| `palimpsest-hub-api` | `sha256:1f61fd0e150f871a5dd5d11dab4f5b18cd41f1c1f076ae6fdd7113bd32bdbc01` |
| `palimpsest-hub-worker` | `sha256:deca07af481a963c4ac70a4294af92f8451e56b4badeaadba9065357e0fb93fa` |
| `palimpsest-hub-build-worker` | `sha256:b5cd64562234d57ca9c694ed596978d6dda813219d0bb85f60d8f9ae1e14efb7` |

**Production receipt — 2026-10-14:** canonical Kolla used `--skip-tags haproxy`
on every action. Hub API and export worker on `dms-controller1` exited 0 after
a terminal gate (3 builds complete, 1 error). Cold SQL restore matched all
15 tables and 10 rows exactly; the encrypted cold Hub blob-volume tar was
884,766,720 bytes. `genconfig` changed nothing; Hub reconfigure was limited
to `dms-controller1`. `/v1/health` returned 200 direct and public;
`/v1/me` returned 401 without authentication. Operator `palimpsest-client`
was promoted to 0.3.5; all 27 installed Kolla role files matched the tag.

The dedicated build-worker VM was upgraded using `palimpsest-build-worker.yml`
(pull + deploy). Startup halted because the old 0.3.4 worker journaled
`{"phase": "claim_pending"}` at 10:47:06Z, five seconds after Hub quiesce began
stopping the API; the claim response was lost. Disposition followed the
[operational recovery guidance](install.md#claim-execution-and-operational-recovery):
server state proved no lease could exist—4 builds, all terminal, every
`lease_expires_at` NULL, last change 2026-10-13 21:19:30Z, and nothing claimable.
The journal was preserved in the operator backup and VM `/root` before
`worker.json` was cleared; `worker.lock` was retained. The worker then ran with
0 restarts, and the Hub logged 16 `/v1/build-worker/claim` responses of 204 in
two minutes. **Maintenance lesson: stop/drain the remote build worker before
stopping the Hub API to avoid an uncertain claim.**

**Native KVM proof was NOT performed:** skipped by repository policy exactly
as for 0.3.4, not qualified. BuildKit/cloud opt-in proofs also remain
unqualified. Idle polling and this evidence-based uncertain-claim disposition
do not prove recipe execution, live-build restart/cleanup, requester authority
or cloud/GPU operation. Hub has no migration ledger; bootstrap `create_all`
and empty-database smoke are not existing-schema compatibility proof. Future
cutovers still require all-writer drain, reviewed offline schema upgrade,
paired SQL/CAS backups and isolated restore, and current requester authority.

Current authoritative root entrypoints (reproduction/selection commands below
are not additional receipts):

```sh
uv sync --frozen --extra dev
python3 scripts/check_architecture.py
uv run python tests/fixtures/oci-root/generate_fixtures.py --check
uv run python scripts/generate_cli_reference.py --check
uv run ruff check .
uv run ruff format --check .
uv run python scripts/test_lanes.py list --check
uv run python scripts/test_lanes.py plan --changed HEAD
uv run python scripts/test_lanes.py run core-cli build-registry
uv run python scripts/test_lanes.py run portable
uv run python scripts/test_lanes.py run full
uv run python scripts/build_package.py --out-dir /tmp/palimpsest-dist
uv run --python 3.11 --no-dev palimpsest --version
```

`portable` is all portable lanes; `full` is the root legacy suite with opt-in
skips and excludes Hub. CI uses Python 3.12, Linux portable shards `1/6`–`6/6`
and macOS15 shards `1/4`–`4/4`, each `run portable --shard N/M`.
Root supports Python3.11+; conventional Linux KVM supports x86_64/aarch64,
macOS Apple Silicon defaults to Lima/VZ and HVF is experimental. OCI-root KVM
remains Linux amd64 only; neither macOS portable nor emulated image success
is native proof.

Hub has its own Python3.12+ environment; root gates do not cover it:

```sh
cd hub
uv sync --frozen --extra dev
uv run ruff check .
uv run ruff format --check .
uv run pytest -q tests/test_auth.py tests/test_hub_api.py tests/test_packages.py \
  tests/test_build_worker.py tests/test_builds.py tests/test_build_ownership.py \
  tests/test_image_exports.py tests/test_export_delegation.py \
  tests/test_database_pool.py tests/test_migrate.py
uv run pytest -v
uv build
```

The optional process ownership node is
`uv run pytest -q -s tests/test_database_pool.py::test_shared_sql_process_ownership_smoke`
in Hub, with private `PALIMPSEST_OWNERSHIP_SQL_URL` selecting **empty disposable
loopback MariaDB** named `palimpsest_ownership_*`. It checks real SQL/process
ownership, not guest/provider execution or production readiness. Never point
it at production or substitute a SQLite pass for MariaDB exclusion evidence.

Canonical image builds use root context, `docker/hub/Dockerfile` and targets
`palimpsest-hub-api`, `palimpsest-hub-worker`, `palimpsest-hub-build-worker`:
`docker build --file docker/hub/Dockerfile --target TARGET .`.
For separately scoped local multi-arch evidence use Buildx with
`--platform linux/amd64,linux/arm64 --output type=oci,dest=OWNED_ARCHIVE` and the
same context/Dockerfile/target; no push. Current Hub CI builds host-default
platforms and does not define a multi-arch publication matrix.

Privileged filesystem, stage-1 binary, real BuildKit, native KVM, direct-executor,
SYSTEM/cloud and GPU paths require distinct tools/resources/authorization.
CI filesystem proof needs Linux root, squashfs/erofs tools and
`PALIMPSEST_REQUIRE_OCI_FS=1`; stage-1 uses the pinned amd64 GCC image;
BuildKit integration uses `PALIMPSEST_BUILDKIT_E2E=1` and an owned
`docker-container` builder bootstrapped with `network=none`. Native CI remains
trusted-ref/protected-environment only, using the reviewed source/kernel/config
pins and exact-owned Nova quota/cleanup receipts. Literal native `false/skipped`
means **no native qualification**. No native/cloud/GPU command was executed here.

Exact opt-in lane entrypoints (only after separate resource/authority approval):

| Lane command | Required platform/input boundary |
| --- | --- |
| `uv run python scripts/test_lanes.py run filesystem` | privileged Linux, `PALIMPSEST_REQUIRE_OCI_FS=1`, squashfs/erofs tools and owned evidence directories |
| `uv run python scripts/test_lanes.py run guest-binary` | pinned Docker compiler image and guest ELF reproducibility prerequisites; not a VM boot |
| `uv run python scripts/test_lanes.py run gate1` | real Docker/Buildx, owned network-none OCI exporter, `PALIMPSEST_BUILDKIT_E2E=1` |
| `uv run python scripts/test_lanes.py run guest-kvm` | Linux x86_64 `/dev/kvm`, exact pinned kernel/config/proof fixtures, `PALIMPSEST_REQUIRE_STAGE1_KVM=1` |
| `uv run python scripts/test_lanes.py run native-live` | qualified Linux amd64 libvirt runtime, `PALIMPSEST_REQUIRE_OCI_ROOT_LIBVIRT=1` plus each selected proof's own image/flag; no implied GPU authorization |
| `uv run python scripts/test_lanes.py run gate2` | qualified Linux amd64 runtime and transferred verified artifacts; `PALIMPSEST_OCI_ROOT_E2E=1` and `PALIMPSEST_OCI_ROOT_E2E_ARTIFACT_DIR` |

The direct Hub executor is a different CPU-only service-VM boundary, not a
native-live/KVM lane. Its current HTTPS/journal/direct-root integration requires
separately approved same-ref dedicated-VM build, restart and exact-owned process/
loop cleanup acceptance. No canonical new live receipt is fabricated here.


The last complete baseline, commit `6cf198f`, passed 4,325 local tests and
4,360 server tests; the server run took 879.97 seconds. Repeating that entire
suite after every small change is no longer the default development loop.
Full regression remains a release/integration check, not a per-edit gate.

## Native dashboard catalog and bundle unpack (2026-10-25; source qualified, not deployed)

Source: isolated public snapshot `/var/folders/1m/xfc726k14szbjnrlq5t68zjw0000gn/T/afterglow-palimpsest-dashboard-6m5hqfl9/palimpsest`, copied from dirty `dev` based on `aa397a0fd976e35302974a347748a9773e3084bf`. Evidence root: `~/.local/share/afterglow-proofs/palimpsest-asset-dashboard-20261025/`. HEAD/package version alone does not identify working-tree source.

| Check | Actual result and boundary |
| --- | --- |
| Complete separate Hub suite | **710 passed, 2 skipped**, 268.36s (`native-hub-final.log`), including actual-router SQL catalog/package tests and real loopback Keystone/Hub authentication tests. Opt-in skips are not native passes. |
| Focused CLI/layout/unpack regressions | **54 passed**, 1.43s (`native-cli-reviewed.log`). |
| Root core-cli original attempt | **2046 passed, 2 skipped, 27 failed**, 114.44s (`native-core-cli-root-env.log`), 2075 selected. Every failure was missing `ansible-playbook`; the original failure is retained. Earlier `native-core-cli.log` incorrectly used the Hub environment without Jinja2 and is not core evidence. |
| Only original27 missing-tool cases, genuine Ansible | **27 passed**, 18.30s (`native-kolla-missing-27-real-ansible.log`). Existing root `.venv/bin/ansible-playbook`: Core2.19.14, Python3.11.15, Jinja3.1.6. No fake binary, installation or operator changes. Together the runs qualify **2073 passed and 2 skipped**, not a new single green2075-node run. |
| Actual source CLI extraction + runtime resolver | `local-cli-positive.log`: one qcow2 x86_64 base and two ordered dependency layers, no manual layer selection. Synthetic payload bytes; **no hypervisor/guest executed**. |
| Final post-format canonical regressions | **22 catalog passed**,2.54s (`native-catalog-postformat.log`); **29 unpack passed**,0.42s (`native-unpack-postformat.log`). Earlier710/54/core counts are the pre-format baseline, not a full rerun of final bytes. |
| Final restored-source checks | CLI contract/reference/lane/unpack **342passed22.58s**, actual help/reference/lane and9owned-file Ruff checks pass (`native-final-restored-source-checks.log`). Unrelated CLI/Nova/lane formatting restored exactly; their preexisting style debt excluded. Final17file alternate-index guard digest `bc918dbd56e96a552458345d7f6c2c9a4852bb94213bcf3d17846f5d78c32431` (`native-scoped-architecture-restored-source.log`). Earlier formatted-byte76b30155digest and inherited507file stamp are not final acceptance. |

`hub/tests/test_catalog.py` and `test_auth.py::native_http` cover actual native response models, SQL and authentication/context fences. Temporary SQLite/CAS plus real oslo.policy fixtures replace external identity only and never load operational settings/main lifespan. Visible public/site/project/shared leaves preserve private-child/ancestor nondisclosure; global admin labels are absolute public/site/shared/private independent of original project. Recorded completed build/export outputs establish provenance; forged config and unlinked registrations remain unknown. Download policy is independent of visibility.

AssetPage pagination is over ordered SQL candidates: validate at most limit plus one SQL lookahead, never rescan/fill; empty items can have advancing next_offset. Regression invalid pages advance0→5→10 before a valid leaf. LayerPage retains row pagination. Ancestor over-depth fails413 at native MAX_DEPTH64. Local metadata eligibility requires visible raw/qcow2 base plus known matching x86_64/aarch64 base/layers; no-base complete reusable chains remain discoverable but not locally runnable. size_bytes includes base+layers, excludes archive metadata; fixture torch6bytes is not a runtime image.

The new `palimpsest bundle unpack ARCHIVE --output DIRECTORY` is **working-source-only**, not asserted present in published0.3.5. Verify the exact source command help. Canonical empty native blobs/blobs/sha256 directory markers are accepted without extracting permissions; noncanonical/duplicate/link/special entries, malformed input and digest errors fail without partial destination publication. `test_bundle_unpack.py` belongs to core-cli; `test_catalog.py` belongs to the separate Hub lane.

Reproduce only the missing-tool completion from the isolated snapshot (do not duplicate the recorded full suites):

```sh
ROOT=/Users/pieroot/code/palimpsest
export PATH="$ROOT/.venv/bin:$PATH" PYTHONPATH="$PWD/src"
"$ROOT/.venv/bin/python" -m pytest -q \
  tests/test_kolla_role_contracts.py::test_ansible_public_route_derivation_fails_closed_without_precheck \
  tests/test_kolla_role_contracts.py::test_ansible_public_route_preserves_opt_in_and_explicit_override \
  tests/test_kolla_role_contracts.py::test_ansible_haproxy_only_guard_rejects_invalid_explicit_origin
"$ROOT/.venv/bin/python" -m palimpsest_local.cli bundle unpack --help
```

The loopback-only `scripts/serve_catalog_fixture.py` serves actual catalog and safe layer/blob/bundle-export routes with synthetic identity and automatically removed SQLite/CAS/policy storage. `member`/`reader` differ in download authority; `human-admin` needs no fabricated target; service/member/missing identities cannot globally read. Synthetic bytes are not genuine SquashFS/cloud images. The new catalog routes require a **matching reviewed Hub rollout**, independently authorized and verified. No deployment, package publication, schema/IAM mutation or runtime VM proof occurred. Scoped guards/fingerprints and closure evidence are recorded in the native change verification artifact; shared native index/review marker remain untouched.



## Native Nova build/assets workflow — 2026-10-22

Evidence root: `~/.local/share/palimpsest-proofs/nova-gpu-comparison-xt8fep1d/`.
Source remains dirty `dev` at `aa397a0fd976e35302974a347748a9773e3084bf`;
the new executor is not a published release or a Hub deployment.

| Check | Observed scope/result |
| --- | --- |
| Actual installed-wheel canonical hard Linux worker | Two real OCI fixture layers converted and extracted; durable same-job reuse, contextual parents, zero-consumer byte/inode retention and actual kernel counter sampling passed |
| Actual disk-backed build capture | Canonical overlay/chroot install/check created a real SquashFS on a fresh loop-backed ext4 disk; extracted payload matched, original root was unchanged |
| Actual prepared immutable runtime | Asset registration, original blob-path manifest, clean RO/no-replay filesystem, blockRO=1, actual SquashFS/bind mounts, repeated activation and non-login UID65534 native command all passed |
| Fresh Python3.11.15 wheel installation | No-dependency isolated install and actual `palimpsest cloud --help` passed; this is not a remote GPU package qualification |
| Nova CPU worker cloud-init | Actual driver-free image on cpu.2c_8g with exclusive100GiB data volume produced checked4096-byte `sha256:c17257e73498b66f7b121a0c323bd8dbab585bcfd335022970487b6d5aa5793d`; build172.102→194.663 guest-monotonic seconds; cloud-init done/errors=[] |
| Replacement CPU worker ML jobs | Default300s/120s policies, cpu.8c_32g: PyTorch5 layers (asset `c9d1a30e…`) and, as a new job after a generic first failure, TensorFlow18 layers (asset `289965f9…`); warm repeats were5/5 and18/18; all8 retained job records were inspected, successes with full asset verification. The first TensorFlow failure coincided with unattended-upgrade replacing `libc6` (inferred, no stable code captured). CPU-worker facts, not GPU boots |
| Capture mount isolation | Privileged aarch64 Linux with shared-propagation peer: before8 stale mounts/`needs_recovery`/nonempty journal, after0/no/journal0. Permanent regression `test_capture_releases_store_and_journal_despite_peer_namespace` now runs in the existing `oci_fs` lane (1 passed in the privileged container); parent-layer helper isolation separately passed `ns-leak-proof/parent-after.json`. Full capture→asset→RO consumer smoke passed again |
| New GTX1080Ti GPU attempts | NoValidHost while stopped VMs still held GPUs, then MaxRetriesExceeded on `dms-compute8` for a fresh attached-store builder, a no-data-volume direct Torch case and two zero-instance probes (`gpu.1080ti_8c_16g`, `gpu.1080ti_4c_8g`, ~25s each). No guest GPU/nvidia-smi success; **0/40** normal measurements, not a passed benchmark |
| Source regressions | Affected Nova/assets/metrics/worker/cloud-init/build/CLI/cache/OCI selection: 1170 passed, 11 skipped after the capture isolation (`nova-final-tests-ns.xml`; earlier receipt `nova-final-tests.xml`); skipped Linux/fish cases are not native passes |
| Scoped source guard | Only a private inherited-reviewed-cache plus explicit Nova overlay snapshot is staged/stamped; no shared index/marker, commit or publication. Exact fingerprint is in `scoped-source-review.json` |
| RTX3060LHR substitute condition | Probe and native driver/Torch/TF builds passed; fresh native PyTorch consumer passed UID1001/boot/GPU identity, CUDA matrix+cuDNN convolution and blockRO1/ext4 no-replay. The separate initial40-boot matrix completed:30 full collector successes,31 GPU proofs,5 original registry-TF GPU failures and5 collector failures; all40 distinct VMs deleted404,40 boot volumes retained. This is not five successful repetitions of every case |
| Post-review regressions |1244 passed/11 skipped in `nova-final-tests-review.xml`, CLI reference/lane manifest passed; earlier Nova/cloud-init/build/Lima selection261 passed. Lima builder execution itself was not live-verified. Additional-batch collector correction preserves observed NVIDIA time on subsequent metadata failure; regression failed before/passed after, metrics25 passed. An initial fixture lacking the real controller's monotonic stamp caused7 failures and was corrected without weakening the timing contract |
| Separate registry-TF loader diagnostic | Same original relocated interpreter/loader with `TF_CPP_VMODULE=dso_loader=2,cuda_dnn=2` and `LD_DEBUG=libs`: actual `libcudnn.so.9` dlopen failed with ENOENT; libcuda/cudart/cublas/cufft/cusolver/cusparse loaded. NVML symbol lookup failure also observed. At least one blocker established for this runtime; adding cuDNN9 alone is not verified sufficient and no original bytes/matrix input were changed |
| Additional variability batch | Initial successful first-SMI max/min ratios exceeded1.3 for all7 functioning cases; add5 fresh boots per case under a separate marker and ordinal6–10. Original registry-TF0/5 stays separate. Check quota and zero active project instances before allocation; preserve every boot/data volume and publish per-attempt results to Notion |

The real immutable-runtime smoke exposed a contract mismatch: conventional
`mksquashfs` retains its mkfs timestamp, while normalized OCI derivatives require
zero. Native build assets now opt out only of that normalization-time condition;
all structural/hash/FD stability checks and every existing OCI default remain.
The raw artifact header/digest was not patched to pass. A native-header regression
also confirms the default normalized-OCI verifier still rejects nonzero time.
Other retained failures were the sysfs `bonding_masters` metadata file, an initially
overbroad fixture monkeypatch, a missing container udev UUID link and inherited
read-only loop flags. Fixture fixes are not cloud or performance claims.

The runner measures first ordinary-user nvidia-smi while cloud-init/Ansible is
still progressing, and records framework GPU readiness separately. It preserves
failed-attempt console/counter evidence before exact-owned VM deletion; all boot
and data volumes remain retained. Counters describe observed virtual devices and
interfaces, not Ceph physical I/O or link capacity. The actual GPU, native sharing
and five-repetition comparison remain blocked until Nova GPU build prerequisites
are fixed; the Notion report contains the attempt/resource ledger and N/A matrix.

Independent Astra/development-review agents were unavailable; no independent
signoff is claimed. LSP references returned-32601; bounded caller inspection was
the explicit fallback. gbrain CLI was unavailable and no search guidance changed.

## Permanent shared layer cache — 2026-10-21 UTC

Evidence root: `~/.local/share/palimpsest-proofs/layer-cache-rs9csyji/`.
Working-source proof is on dirty `dev` at
`aa397a0fd976e35302974a347748a9773e3084bf`; the installed wheel is an unpublished
candidate, not that commit's clean tree or an operational storage deployment.

| Check | Observed result |
| --- | --- |
| Final cache/intake/store/source/image/inventory/CLI/run-source/worker/handoff/lower-export selection | **1470 passed, 4 skipped**, 30.98s |
| Scoped Ruff | Ten changed source/test/manifest files passed; four cache/publication files passed format check |
| Generated CLI reference and lane manifest | Current; every test classified, mixed selectors exact/disjoint |
| Installed Python3.11.15 CLI with real Skopeo1.24.1 and isolated TLS registry | One common-layer payload GET across cold A, repeated A and distinct B; retained source inode, both consumers released without deletion, offline reuse after registry shutdown |
| Original PyTorch2.8.0 CUDA12.6 archive in final installed CLI | Five original compressed layers, seven source blobs; real Docker Hub raw-manifest refresh only, payload copy explicitly forbidden; inode/size/mtime preserved and offline reuse passed without Skopeo |
| Final installed Python3.12.15 Linux arm64 hard workers and qualified mksquashfs | Distinct gzip digests/same DiffID produced two recipes and one real SquashFS; valid inode reused, missing/corrupt retained output refused without catalog/byte replacement, actual payload extracted with unsquashfs |
| Restart/cross-host authority reconstruction | Copied cache has new SourceCAS/OCIStore identities; both recipes warm-hit using current receipts; zero-consumer output retained; relative symlink packer worked |

The final Darwin selection skips the Linux-only openat2 case, the actual Linux
cache converter and two unavailable fish cases. The separate network-disabled,
read-only-container, capability-dropped Linux smoke above exercises real hard
conversion; it does not turn those skipped nodes into test passes. The source
fixture graph is `linux/amd64`; arm64 conversion does not execute its workload.

Final test command:

```sh
umask 022
uv run pytest -q \
  tests/unit/test_layer_cache.py tests/unit/test_registry_intake.py \
  tests/unit/test_oci_store.py tests/unit/test_oci_source.py \
  tests/unit/test_oci_image.py tests/unit/test_inventory.py \
  tests/unit/test_cli_contract.py tests/unit/test_cli_reference.py \
  tests/unit/test_completion.py tests/unit/test_run_source.py \
  tests/unit/test_remote_artifacts.py tests/unit/test_test_lanes.py \
  tests/unit/test_oci_worker_protocol.py tests/unit/test_oci_run_request.py \
  tests/unit/test_oci_store_handoff.py tests/unit/test_oci_lower_exports.py
```

Preserved failures drove corrections, not relaxed checks. Real Skopeo initially
tried to truncate a retained `0400` manifest on a repeat tag pull; metadata-first
selection now avoids copy for a complete known graph and copies misses by the
resolved immutable manifest digest. Independent review identified unsafe writable
ancestors; they now refuse before use. A later cross-recipe retention regression
failed both missing and aged-corrupt cases before the physical publication guard,
then passed along with valid inode reuse. A wider intermediate run had one legacy
bundle-routing fixture without per-artifact architecture; the fixture now supplies
its real digest binding and `x86_64`, keeping the production unknown-arch refusal.
An early throwaway Linux harness incorrectly expected an internal error message
past the worker's sanitized boundary; corrected harness checks the public store
error category and unchanged inode/catalog instead. Initial failures remain in
their separate receipts. Existing unrelated whole-file format drift in
`scripts/test_lanes.py` was not restyled or claimed format-clean.

Receipts: `registry-cache-smoke.json`, `pytorch-cache-acceptance.json`,
`linux-retention-final-v2/result.json`,
`final-retention-cache-consumer-tests.xml`, the before/after cross-recipe JUnit
files, installed-wheel provenance and the scoped architecture receipt. The
[intake contract](registry-intake.md#permanent-shared-layer-cache) separates this
writable permanent acquisition catalog from frozen read-only guest stores.
CephFS cross-host flock, RBD fencing, power-loss recovery, provider deployment,
native OCI boot and GPU execution are not qualified by this local cache proof.
No existing GPU VM, shared Cinder volume, production service, global state or
credential domain was changed.

## Service-local Hub policy verification — 2026-10-21

Evidence root: `/Users/pieroot/.local/share/afterglow-proofs/service-policy-20261020-palimpsest-019qotth`. This is local working-source verification, not production Keystone/IAM, Kolla rollout, live Glance, KVM guest or release qualification. No existing local service, database, operator policy or production role was replaced.

| Check | Observed result |
| --- | --- |
| `cd hub && uv sync --locked --extra dev` | Successful; real oslo.policy 6.0.1 installed |
| `cd hub && uv run --frozen --extra dev pytest -q` | **650 passed, 2 skipped**, 249.85s; two opt-in MariaDB cases exercised separately below |
| `cd hub && uv run --frozen --extra dev ruff check .` | Passed |
| Root Kolla role contracts | **41 passed**, 21.65s |
| Root smoke-driver/Kolla-test import lint | Three import-order findings repaired; final check/fix exit 0 |
| Standard `oslopolicy-sample-generator --namespace palimpsest` | Exit 0, generated YAML retained in evidence root |
| Canonical Hub API and export-worker image targets | Both **arm64 and amd64** built successfully, serially, without publication |
| Actual registered-app Docker HTTP smoke | **185 checks per architecture; 370 total**, no failed checks or abort |
| Exact nonroot config/mount probes | All four canonical images ran as UID:GID **1000:1000**; root:root **0640** `/app/palimpsest.conf` denied read; root:root **0644** policy-only TOML/YAML and policy.d were read and enforced |
| Opt-in real MariaDB/multiprocess ownership smoke | **2 passed**, 27.03s, on a new labelled tmpfs-backed loopback-only database; owned container/temporary 0600 credential file removed afterward |
| Standalone fresh human system HTTP smoke | **49 expected statuses passed**, exit0 in a clean environment; direct/group-implied raw authority, no target membership, own audit, deletion and revocation-before-I/O verified |

The full Hub run includes original-subject/current-owner action intersection, zero service edges/grade-only access, project-local admin versus fresh human system administration, own-key issuance/revocation/current issuer ceilings, foreign namespace/package denial, actual loopback Uvicorn cache-only archive upload/finalize/resolve/download with **empty leaf-capability labels**, independent package-content access without inventory, YAML grant/deny/reload and policy.d behavior. Real native-parser malformed clauses under `not`/`or` return 503 in main YAML and policy.d, restore the original parser logger and recover after correction; quoted empty/`@`/`!` retain Oslo semantics. Casefold-colliding global role IDs deny before policy authority. Local SDK fixtures are synthetic Keystone HTTP, not production identity proof.

The first focused integration run had **27 failures / 528 passes**: two tests still asserted the retired blanket project-admin veto; the deferred-export identity fixture omitted fresh `/users/{id}/groups` collection reads. Those fixture/expectation defects were corrected without weakening native authority. The first native MariaDB ownership smoke also failed twice: its synthetic builder omitted the current output architecture and a visible cloud-base descriptor. The fixture now supplies content-addressed synthetic base bytes/metadata and `x86_64` result architecture; production build validation remains unchanged. Initial and final receipts are retained, not overwritten as successes.

Exact command/stdout/stderr receipts are `hub-full-final.log`, `hub-lint-settled.log`, `kolla-contracts.log`, `policy-generator.log`, `image-{api,worker}-{arm64,amd64}.log`, `docker-http-{arm64,amd64}.log`, `nonroot-permissions-{api,worker}-{arm64,amd64}.log`, `native-db-process-smoke-final.log` and `human-system-http.log`/`human-system-http-result.json`. Docker reports are `build/service-policy-019qotth-{arm64,amd64}/report.json`; synthetic credential material was not copied into documentation.

| Task-owned image tag | Docker image ID |
| --- | --- |
| `palimpsest-policy-019qotth-api:arm64` | `sha256:f00a97c1acae469c6cc12a9040fb5eaf7eac512055b9e99f1545cbb2cf631889` |
| `palimpsest-policy-019qotth-worker:arm64` | `sha256:728f2776f97d3127985ab4d9dfdddd58b1a3c7d2d038b562e4b7fdc44f5de976` |
| `palimpsest-policy-019qotth-api:amd64` | `sha256:baf637e2ccab6efe520a7493126940c9fedf478f8fd0e7f959a8e3b59d2b415d` |
| `palimpsest-policy-019qotth-worker:amd64` | `sha256:25889ece00f31dea27613e5057de1b74720502767b326f29ce5ed935968f150c` |

The API runtime uses the registered app/routes/middleware and real Keystone SDK against synthetic identity, real Redis and retained SQLite/CAS. Its SQLite adapter intentionally bypasses production lifespan: canonical SQLite bootstrap still fails on MySQL-only connection arguments and remains recorded, not hidden by the passing MariaDB ownership test. Worker image imports/qemu validation and nonroot policy reads are exercised, not a real delegated Glance export or VM build. Retained evidence volumes are `palimpsest-scope-smoke-20261020t184422z-5f6550-{sql,blobs}` and `palimpsest-scope-smoke-20261020t184659z-ed3560-{sql,blobs}`; only run-owned containers/networks/derived smoke images were cleaned. Separate root-owned permission-fixture volume and disposable MariaDB container were label-checked and removed. No prune, Git commit/push/tag or deployment occurred.

## Everyday workflow

```sh
uv run python scripts/test_lanes.py list --check
uv run python scripts/test_lanes.py plan --changed HEAD
uv run python scripts/test_lanes.py run --changed HEAD --dry-run
uv run python scripts/test_lanes.py run --changed HEAD
```

Before committing, `HEAD` compares the working tree, including untracked files,
with the last commit. After committing, use `HEAD~1` or the exact last tested
commit. Inspect the plan before running: its source dependency map is a
conservative recommendation, not a proof that every indirect dependency has
been discovered. Unknown production files and shared foundations select the
whole portable set. New unclassified test files fail manifest validation.

Use a named lane for a focused rerun, for example:

```sh
uv run python scripts/test_lanes.py run oci-monitor
uv run python scripts/test_lanes.py run oci-access
uv run python scripts/test_lanes.py run qualification
```

### Local host resource discovery

`tests/unit/test_host_inventory.py` exercises read-only Lima/libvirt snapshots
with isolated backend fixtures. `test_inventory.py` covers selected-state joins,
same-name replacements, independent access capabilities, bounded Linux source
reads and explicit partial measurements, running journal failures, pinned stopped
console tails, disk-capacity separation and network identities. `test_console.py`
uses real isolated PTY/process consumers to verify guest identity fencing and
that replacement guests receive no user command, disconnect reaping and safe
errors. `test_oci_exec_session.py` exercises original-token drain/ACK after
dashboard timeout/disconnect, shutdown admission closure (including a pending
submission factory), bounded owner admission and unresolved ACK reporting,
without changing core close semantics. Actual foreground `ui.serve` SIGINT and
SIGTERM smoke holds both a console owner and a detached timed-out probe until
their original completion/ACK using isolated `MonitorExecControl`; this proves
process lifetime, not a native OCI VM. `test_ui.py`
covers stable external identifiers and parameter bounds without lifecycle
authority. Inventory unit consumers stub host discovery; they never enumerate
the developer's real VMs.

For actual UI proof, run the foreground web command against an APFS clone of
selected state (`cp -cRp`), because selected-run reconciliation can update its
ledger even in read-only mode. List refresh must not probe every guest. Open
detail explicitly for running journal and real counter samples; exercise enabled
external Lima and selected-owned consoles, disconnect without VM stop, pause and
resume, first-sample/rate reset, stale/error labels, identical-list failure/recovery,
keyboard focus and reduced motion. Test replacement/capability-loss projections
with isolated browser response fixtures, never by replacing unrelated VMs. Do
not stop/delete unrelated VMs. Host discovery, portable tests and macOS HVF
cloud-image proof are not Linux KVM/OCI-root, cloud, passthrough or GPU qualification.
The latest handoff distinguishes test-defined, test-passed and live-verified evidence.

Linux state-root default and override changes can first be checked without
creating system directories:

```sh
uv run pytest -q tests/unit/test_state.py -k 'state_root or unconfigured or explicit_xdg'
```

These tests exercise resolution only for `/var/lib/palimpsest`; they do not
provision `/var/lib`, migrate existing data, or verify the separate implemented
`/var/log/palimpsest` host command journal.

The Linux service-account installer has a root-free unit contract:

```sh
uv run pytest -q tests/unit/test_linux_install.py
```

This test does not modify host accounts or system directories. Any root smoke
test must run in a disposable, network-disabled container and must never mount
host account databases or writable host system paths.

Tests remain in their existing modules; the runner changes selection, not
assertions, fixtures or production safety checks. A documentation-only edit
can produce an empty test recommendation; that is not a successful test run.
Hub tests use their own environment and explicit lane.

`hub/tests/test_database_pool.py` checks that a closed-transport pre-ping renews
the pooled connection and that an unrelated runtime error still propagates. The
`hub` lane includes this file; the fixture is isolated SQLite and does not
claim live MariaDB failover or in-flight transaction recovery.

`hub/tests/test_export_delegation.py` drives the export route and worker
against a loopback synthetic Keystone. The Hub's real keystoneauth/keystoneclient
and openstacksdk objects perform token validation, the current role graph,
OS-TRUST create/delete and trust-scoped authentication over HTTP. Glance byte
I/O and qemu-img are stubbed at the Hub's own boundary. The file defines
revocation, scope substitution, expiry, abandonment and cleanup-retry
regressions. It does not prove the deployed Keystone trust policy or a real
Glance download.

### Native Hub worker ownership

**Historical SQL/KVM worker snapshot, not 0.3.5 verification.** The following
receipt and former rollout description are preserved for their original dirty
`aa397a0fd976e35302974a347748a9773e3084bf` source, before integration onto
the latest HTTPS-only build plane. The current schema/drain/private-worker
contract is in [installation](install.md); current commands are at the top of
this document. Older renewable distributed build launches and execution-node
guest recovery APIs are superseded, not reintroduced by retained test evidence.

The implementation slice ran no checks. Subsequent parent verification ran on
a frozen public clone synced byte-for-byte from that earlier working tree,
including concurrent WIP (544 files; snapshot manifest SHA-256
`fb720648c50a6d2f4140988b4f10ced074c4647dd721ed86f828e86bdd9d35d5`).
Logs, JUnit XML and per-lane execution JSON are under
`/Users/pieroot/.local/share/afterglow-proofs/afterglow-process-ownership-n7xrnua4/`.
These results are **test-passed locally, not live-verified**:

- Focused Hub selection below: **93 passed, 2 skipped**.
- MariaDB multiprocess hook below: **2 passed** against fresh empty owned
  database `palimpsest_ownership_n7xrnua4c`, with `PALIMPSEST_OWNERSHIP_SQL_URL`.
  An earlier pre-lint source run also passed **2** against
  `palimpsest_ownership_n7xrnua4`. SQL and OS-process ownership assertions are
  real; guest execution/provider deletion are synthetic and expiry is controlled.
- Full Hub suite, `pytest -p no:cacheprovider tests`: **378 passed, 2 skipped**.

The initial parent focused run exposed 16 export timestamp failures; the
SQLAlchemy ORM's Python predicate evaluator compared naive SQL DATETIME values
against aware UTC lease times after loading the fenced row. Those failures were
fixed: timestamp-predicate updates now synchronize through SQL
(`synchronize_session="fetch"`), retaining owner/expiry predicates and locks;
Python soft-delete lease checks normalize UTC. Five added regressions exercise
reloaded-naive renewal/complete/error synchronization and live/expired soft
deletion. The earlier authorized remediation command `uv run pytest -q
tests/test_image_exports.py tests/test_export_delegation.py` passed **54 tests**,
with 74 existing auth-client deprecation warnings, in 18.17 seconds; the later
focused/full Hub passes above include the fix. Full focused selection:

```sh
cd hub
uv run pytest -q tests/test_builds.py tests/test_image_exports.py \
  tests/test_export_delegation.py tests/test_database_pool.py tests/test_migrate.py
```

Portable SQLite consumers cover live startup preservation, expired recovery,
late-owner fencing, local cleanup authority, role tokens and scratch/delegation
lifetime. SQLite does not implement MariaDB's `FOR UPDATE SKIP LOCKED`; a
portable pass alone is not distributed SQL exclusion evidence.

The opt-in local-process hook requires an **empty disposable MariaDB database**
whose name starts with `palimpsest_ownership_`, an asyncmy URL, and no production
credentials or services. Supply the URL through approved private environment
handling (do not put passwords into shell history):

```sh
cd hub
# PALIMPSEST_OWNERSHIP_SQL_URL is supplied privately, not DATABASE_URL.
uv run pytest -q -s tests/test_database_pool.py::test_shared_sql_process_ownership_smoke
```

Hub `ruff check .` retains only `tests/test_auth.py` I001, credential-isolation
WIP outside this change. SIM117/SIM102 in `services/image_exports.py` were fixed.
The eight lease-change files were formatted: `services/builds.py`,
`services/image_exports.py`, `services/worker_leases.py`, `tests/test_builds.py`,
`tests/test_database_pool.py`, `tests/test_export_delegation.py`,
`tests/test_image_exports.py`, and `tests/test_migrate.py`. Hub CI's
`ruff format --check .` gate still fails on seven unformatted credential-isolation
files outside this change: `api/hub.py`, `api/packages.py`, `auth.py`,
`services/package_registry.py`, `tests/test_auth.py`, `tests/test_hub_api.py`, and
`tests/test_packages.py`. These remaining findings are not lease-change failures
or a claim that the whole Hub lint/format gates passed.

The hook refuses a nonempty schema or a non-loopback SQL endpoint. It creates
and removes only the schema objects it owns in that disposable database. Real
spawned OS processes hold independent local flock paths while sharing SQL and
artifact storage. Assertions distinguish per-job SQL claims, live-build
startup preservation, successful publication, expired/crashed-owner recovery
without recipe replay, and maintenance role exclusion/takeover. Guest execution
and provider boundaries are synthetic: no `/dev/kvm`, libvirt, Keystone Trust
policy, Glance transfer, provider create, production readiness, shared-network
filesystem crash durability or guest-build proof is established. A SQL/process
failure is a failed ownership hook, not a reason to substitute SQLite success.

Verification used disposable owned Docker MariaDB 11.4 (tmpfs, loopback
`127.0.0.1:56498`) and Redis 7 (`127.0.0.1:56497`) fixtures; providers were
synthetic loopback HTTP or in-process fakes. No private configuration or live
credentials, live OpenStack/Keystone/K3s/Nova/Octavia/Glance/KVM, paid inference,
deployment, image publication, commit, stage or push was involved. KVM guest
builds, live Glance/provider deletion, production and mixed-version rollout
remain **not exercised**. These checks also do not qualify production
Redis/Sentinel failover, async-replication lease/journal loss or promoted-node
TIME skew. Architecture guard stamping is recorded separately by the parent in
isolated clones; the existing shared review marker/digest/UTC block is unchanged.

Rollout is offline: stop **all old API/build/export workers**, run
`palimpsest-hub-bootstrap`, then start only the new version. Nullable build
ownership columns do not authorize adoption of unknown legacy guests. A remote
expired-build reconciler retains another node's private tree; that node must
return for verified cleanup or require an explicitly authorized local operator.
The shared artifact filesystem must still provide coherent cross-node digest
locks; independent worker flocks are only local process guards.
The Hub's server-build path has a focused project-scoped HTTP contract lane:

```sh
cd hub
uv run pytest -q tests/test_builds.py tests/test_upload_limits.py \
  tests/test_hub_api.py tests/test_migrate.py
```

This exercises upload offsets and private download over ASGI HTTP, build
authorization/visibility, durable queue claim, publication, project quota,
expiry, and data migration. The main HTTP build test replaces the **guest VM executor**;
the bytes labeled as a cloud image are a portable fixture, not a bootable
image. Therefore an HTTP pass proves no KVM guest boot, no native SquashFS
mount, no Keystone deployment, and no production worker availability. A native
server-build qualification must separately use a real pinned x86_64 cloud
image on a provisioned Linux KVM host, observe `palimpsest-hub-build-worker`
claim and actual `build_layer` guest execution, verify the downloaded SquashFS
digest and parent/base chain, then check owned VM cleanup. Do not perform that
host/remote operation or infer success from portable CI without its explicit
deployment prerequisites and approval.

The Hub lane also exercises a malicious bundle member that must not remove a
shared blob, suffix byte ranges, canceled lock waiters, retry after interrupted
blob promotion, refusal on blob directory fsync failure, competing
identical-digest registrations, competing project enqueue requests, worker
preflight rejection before SQL access, fail-closed recovery of an unverifiable
builder marker, retry of completed-build private scratch cleanup without
losing its published output, and refusal to reclaim guest state after a failed
or timed-out builder run until its recorded process group is verified. These
are portable contract tests. A Linux stand-in process probe has exercised
`PR_SET_PDEATHSIG` and exact owned-group reaping separately; real builder
restart, filesystem crash durability, and libvirt teardown are not proven.

Multi-manifest bundle regression also checks that a shared ancestor with the
same digest and descriptor registers once, while a different media type or
config for that digest fails parsing before registration. A leaf manifest
config that disagrees with its layer annotation is rejected. These are
portable parser contracts, not a large-tar throughput or live-filesystem
qualification.

The 2026-09-23 isolated `pieroot-server` run at source `0581893` used only a
new scratch checkout. A streamed 512 MiB gzip tar (2,342,856 compressed bytes)
hit a 32 MiB expansion cap and left no spool. CAS promotion completed real
file/ancestor-directory fsync calls; separately injected file-fsync `EIO` and
directory-fsync failures refused success and preserved staging for retry.
The stand-in builder process group was reaped after exact marker verification,
and a marked new-session child died after its parent exited. These checks
did not start Hub services, open a database, boot a guest, or simulate a real
power loss.

PAX `size` is a logical size override: the physical tar header can report
zero for blobs larger than 8 GiB. The parser regression uses a seven-byte
override for extraction and a sparse 8 GiB+1 byte member for offset and
limit checks. Neither test reads or allocates an 8 GiB payload; an actual
large-blob end-to-end import and crash-durability proof remain separate.

## Extracted Hub tracking checklist

For changes to `tracking/afterglow-palimpsest.json`, validate the real upstream
checkout **and** existing executable consumer contracts. The reviewed baseline
is Afterglow `main` commit `2862565c1f59eac0ef0904b97c905b177f12b335`; the
scheduled workflow still follows moving `main`. `AFTERGLOW_MAIN_CHECKOUT` below
must be the intended upstream `main` checkout, not the shared Afterglow `dev`
worktree. Record its actual SHA in the validation receipt.

From the Palimpsest repository root:

```sh
uv sync --frozen --extra dev
uv run python scripts/check_afterglow_drift.py \
  --upstream-root "$AFTERGLOW_MAIN_CHECKOUT" --local-root .
uv run pytest -q tests/unit/test_afterglow_tracking.py \
  tests/unit/test_hub_contract.py tests/unit/test_oci_layout.py
```

The first suite retains independent hash, required/forbidden-marker and
absence-rule checker regressions. The preceding command checks the real
manifest against the actual upstream and local source; it is not replaced by
fixtures generated from the manifest's own expected markers. The obsolete
`test_manifest_records_the_current_hub_protocol_gap` source-copy assertion is
deleted, not repinned. Checker fixture inputs prove no HTTP or authentication
behavior.

In the separate Hub environment:

```sh
cd hub
uv sync --frozen --extra dev
uv run pytest -q tests/test_auth.py tests/test_upload_limits.py \
  tests/test_hub_api.py
```

Existing behavioral selectors to retain include:

- Client offset/range and transport: `tests/unit/test_hub_contract.py::test_push_reconciles_offset_conflict_and_finalizes_with_exact_offset`, `::test_pull_blob_resumes_only_from_exact_content_range`, `::test_hub_client_blocks_redirects`, and `::test_hub_client_redacts_tokens_on_error`.
- Local strict importer: `tests/unit/test_oci_layout.py::test_extract_and_verify_bundle` and `::test_extract_bundle_security_rejections`.
- Hub scope and creating user (paths below are relative to `hub/`): `tests/test_auth.py::test_require_token_missing_header_raises_401`, `::test_require_token_rejects_missing_project_scope`, `::test_project_header_is_assertion_not_rescope`; `tests/test_upload_limits.py::test_upload_sessions_are_project_bounded_and_released_on_abort`; `tests/test_hub_api.py::test_legacy_upload_remains_private_to_its_original_member` and `::test_private_artifact_visibility_is_exact_on_case_insensitive_legacy_schema`.
- Hub received-byte/parent chain: `tests/test_hub_api.py::test_finalize_rejects_declared_digest_mismatch_and_discards_bytes`, `::test_bundle_digest_mismatch_rejects_before_cas_publication`, `::test_parse_bundle_reconstructs_parent_chain_from_manifest_order`, `::test_parse_bundle_rejects_annotated_config_parent_that_contradicts_manifest_order`, and `::test_import_registers_the_whole_chain_with_its_declared_parents`.

These HTTP/store/client fixtures do not authenticate against deployed Keystone
or prove crash durability, native guest execution or production rollout.
Afterglow BFF authentication and header-forwarding tests belong to Afterglow's
own gate. Retain all existing full/package/image/native release requirements;
this focused checklist does not replace them. The initial ownership-cutover
edit did not dispatch CI or run native/deployment operations. Parent validation
against actual Afterglow `main` and the retained client/Hub suites is recorded
in `ARCHITECTURE.md`; hosted publication remains a separate gate.

## CLI reference and distribution checks

For command documentation and packaging-only edits, use the focused contracts:

```sh
uv run pytest -q tests/unit/test_cli_reference.py tests/unit/test_packaging.py \
  tests/unit/test_test_lanes.py tests/unit/test_architecture_guard.py
uv run python scripts/generate_cli_reference.py --check
uv run python scripts/build_package.py --out-dir ./dist/verify-001
```

The unit checks and documentation drift check are distinct from the final
command's real wheel/sdist build and isolated wheel installation. The package
smoke runs outside the checkout, without installing optional KVM dependencies
or provisioning host accounts. Build tooling may need to download its build
backend; wheel installation itself uses only the newly built local artifact.
Choose a new output directory on each invocation; existing packages are never
overwritten. The wheel is rebuilt from the produced source distribution, and
the installed package verifies the bundled stage-1 ELF digest and format.
The helper also exercises the README's `uv tool install --no-index` path in
temporary tool directories and runs that installed executable. `--no-deps`
belongs to the separate `uv pip install` probe, not `uv tool install`.
The generated reference normalizes argparse usage whitespace and optional
positional requiredness: Python 3.12 and 3.13 otherwise render different
wrapping and internal `required` values for `*` and remainder arguments.
Check the same generated file under both supported versions; accepting a stale
file or changing production parsing is not the fix.
These checks do not boot a VM, qualify Gate 2, publish a release, or authorize
changes to `/var/lib/palimpsest` or `/var/log/palimpsest`.

The separate `development-package.yml` workflow runs the architecture guard,
CLI reference check, lane-manifest check, focused lint, `core-cli` plus
`qualification`, the focused publication contracts, and the real package smoke
before the publish job receives `contents: write`. It accepts pushes only from
`main`, `dev`, or `codex/oci-root-phase1`, plus manual dispatch of those same
refs; pull requests cannot publish. Ref-scoped concurrency lets those three
branch runs reach their own publish job even when they share a commit SHA.

After local transfer checksum verification, the standard-library
`scripts/publish_development_package.py` helper treats
`package-<full-commit-SHA>` as an immutable create-or-verify publication. It
creates an absent lightweight tag at exactly the event SHA and refuses a
different tag. It creates an absent prerelease with the wheel, sdist, and
`SHA256SUMS`, then requires exact tag/title/notes/draft/prerelease metadata,
exact asset names, and downloaded SHA-256 bytes. Every mutation is judged by
re-reading remote state rather than by its own exit status, so a conflict or an
interrupted response that already created the tag, release, or asset still
succeeds; the original error surfaces only when the re-read is still
incomplete. An otherwise exact partial release may upload only its missing
expected assets. An asset another run is still uploading is awaited until
GitHub reports it `uploaded`, never re-uploaded, and a release that never
converges fails closed. An extra, duplicate, or byte-mismatched asset fails
closed. The helper never force-updates or deletes a ref, deletes an asset, or
uses `--clobber`.

These are source and fake-`gh` unit contracts, not remote publication
verification. The prerelease is not Latest, a PyPI publication, or evidence
that native KVM, guest-binary, filesystem, Gate 1, or Gate 2 lanes passed.

The two tooling scripts select `core-cli` in the changed-file planner. CI also
runs the real package smoke explicitly; ordinary unit lanes do not need to
download and rebuild distributions for every test iteration. Run the same
focused checks and package smoke on the exact pushed Linux server SHA with
`umask 022` and `PYTHONPATH` unset. Preserve existing runtime state and evidence.

## Shards and full regression

```sh
uv run python scripts/test_lanes.py run portable --shard 1/6
uv run python scripts/test_lanes.py run portable --shard 2/6
# Repeat through 6/6 to cover the complete portable set.
uv run python scripts/test_lanes.py run full
```

`full` preserves the original opt-in skip behavior of `pytest tests`; a
successful result is not evidence that skipped native or product gates passed.

Sharding uses stable module/class/function identity and parameter indices,
not display node IDs that can contain freshly generated UUIDs. It can divide
a large module such as `test_oci_store.py` without moving its fixtures. A single shard
is only partial evidence; every shard for the same commit, platform and shard
count is needed to claim complete portable coverage. This is count-oriented
partitioning, not measured runtime balancing. Collection failures, duplicate
case identities and empty shards must not turn into successful evidence.

```sh
uv run python scripts/test_lanes.py run portable --collect-only
uv run python scripts/test_lanes.py run portable --collect-only --shard 1/6
```

Collection-only output is coverage inventory, not a test pass. Compare stable
case identities across fresh shard processes to prove disjoint complete
assignment; randomized display IDs are not suitable for that comparison.

CI runs all portable tests across six Linux shards and four macOS shards.
Neither matrix sets `max-parallel`, so every shard of a run starts in the same
wave.

- **Why the caps were removed.** The earlier caps allowed 3 Linux and 2 macOS
  shards at a time, which queued a second wave. Across the 21 completed `Test`
  runs of the current job shape (2026-09-15 to 2026-09-23):
  - macOS shards 3/4 and 4/4 waited a median 151s and 170s;
  - Linux shards 4–6 waited 91–105s;
  - the `Unit tests (macOS 15)` aggregate finished last in 19 of the 21 runs.
- **Critical-path baseline.** Median 325s and p90 781s. Excluding one burst
  that was serialized on the single self-hosted KVM runner, the figures are
  303s and 346s.
- **Expected effect (estimate).** A dev/PR critical path of roughly 155–170s.
  The ~140s native KVM job sets the floor. Re-measure after the change lands
  before treating this as achieved.
- **Shared capacity.** The matrices still draw on the organization's Free-plan
  pool of 20 hosted jobs and 5 macOS jobs, shared with sibling repositories.
  The following counts come from the workflow definitions, not from
  measurement:
  - under the 2026-09-27 cutover, a PR starts 15 hosted `Test` jobs
    (4 macOS), followed by two portable aggregates; native is not a PR check;
  - a trusted `main`/`dev` push adds one hosted native orchestrator and its
    aggregate; Hub and development-package verification bring the initial
    hosted job count to 18;
  - a PR also starts `hub-docker.yml` `test`, for 16 initial hosted jobs.

  Same-SHA dev/main pushes still compete for hosted capacity. Native jobs
  additionally serialize through the repository-wide `palimpsest-native-kvm`
  concurrency group; environment review time is part of observed latency.
- **Contract test.** `tests/unit/test_test_lanes.py` pins:
  - each portable matrix job exactly:
    - its job keys are `name`, `runs-on`, `strategy` and `steps`, so there is
      no job-level `if`, `env`, `defaults` or `continue-on-error`;
    - its runner is `ubuntu-latest` or `macos-15`;
    - `strategy` has only `fail-fast`, `matrix` and `max-parallel`, and
      `matrix` is exactly `{shard: [1..N]}`, so no `exclude` or `include`;
    - `fail-fast` is false and `max-parallel` is absent or at least N;
    - the whole step list is pinned, including action versions, `with`, and
      the shard command with its `--shard N/M` denominator. No step `if`,
      `shell` or `env`, no checkout `ref`, and no extra step (a `$GITHUB_ENV`
      write, for example) can be added;
  - that `test.yml` has exactly the top-level keys `name`, `on`,
    `permissions` and `jobs`, and that `permissions` is exactly
    `{contents: read}`, so there is no workflow `env`, `defaults` or write
    token;
  - the aggregate check names and exact `needs`; portable aggregates use
    `always()`, while native also requires the trusted repository/event/ref;
  - verdict dependency environments and failure-bypass prevention. Native
    verdicts run as actual shell commands over enabled/result combinations;
    exactly `true/success` and explicitly opted-out `false/skipped` pass. The
    latter is policy success with no native proof or qualification;
  - the exact job keys of every `test.yml` job, and the exact set of job
    ids, so a new job needs its own table entry. No job sets job-level
    `env`, `permissions` or `continue-on-error`; only the aggregates and
    `kvm` set `if`; only `hub` sets `defaults`, pinned by value to
    `{run: {working-directory: hub}}`, so no `defaults.run.shell` can
    turn its steps green;
  - that no step in `test.yml` sets `shell` or `continue-on-error`, and that
    the only step-level `if` is `always()`, which never skips a step and is
    used by upload and cleanup steps. The `kvm` proof step is covered too;
  - that no gate job sits in front of the test jobs; native's positive
    repository/event/ref allowlist rejects PRs, forks, arbitrary branches,
    and reusable invocation;
  - that every workflow uses exact hosted labels `ubuntu-latest`,
    `ubuntu-24.04`, or `macos-15`. Non-hosted exceptions are empty; runner
    groups and missing labels are rejected too;
  - that no job calls a reusable workflow (`uses:`); any new caller requires
    a separate review of its trigger and credential boundary;
  - the exact `test.yml` triggers (`workflow_call`, and `push` and
    `pull_request` for `main`/`dev`, with no `pull_request_target`), and that
    `release.yml` runs only on `v*` tag pushes;
  - that no workflow uses a `pull_request_target` or `workflow_run` trigger
    (the allowlist is empty), reading the string, list and mapping forms of
    `on`. Both run with the base repository's token and secrets even when a
    fork PR triggers them.

  The approved cutover uses GitHub-hosted orchestration and a protected
  environment, not a YAML-only defense around a persistent self-hosted runner.
  Runner 21 remains stopped. Six hosted PR required checks are strict; native
  success is required only on trusted dev/main pushes and release tags.
  Worktree policy and manual proof are not evidence that an unpushed workflow
  has run on GitHub. Publication still requires separate approval.

  Not pinned:
  - step contents of the non-matrix jobs (`checks`, `hub` and the proof
    jobs), such as a step `env` or an extra `$GITHUB_ENV` step. The `kvm`
    proof fails at run time if no evidence file exists, because its upload
    step sets `if-no-files-found: error`;
  - neutering outside the workflow, such as a `pyproject.toml` `addopts` or
    a conftest hook (see Shard counts below);
  - the job shape of workflows other than `test.yml`, beyond triggers,
    runners and reusable calls. The development-package workflow has its
    own contract.
- **Shard counts.** Each shard prints a `Lane shard` count line, but CI only
  prints it and does not check it. Three cases fail at run time:
  - an empty shard exits 5;
  - a collection error exits 2;
  - `--test-lane-shard` passed without the plugin is a usage error (exit 4).

  CI does not check per-shard counts, or whether the shards add up to the
  portable total. This is a documented deviation from the shared rule 5
  (migrated to the [CI safety/performance spec](../openspec/specs/ci-safety-and-performance/spec.md)),
  which asks CI to verify per-shard counts. The pinned shard
  command, the `commands()` argv test, and the disjoint-and-complete
  assignment tests cover only the wrapper risk that rule names: a `--shard`
  dropped before `test_lanes.py` would run the full suite in every shard, and
  only the pinned command string guards against it. They do not catch
  neutering outside the workflow. On 2026-09-24, locally,
  `run portable --shard 1/256` with `PYTEST_ADDOPTS=--collect-only` printed
  its `Lane shard` line for 24 selected nodes, ran none, and exited 0. A
  run-time check of executed against selected counts would catch that;
  whether to add one is an owner decision.
- **Rules for future CI changes.** Measurement and change rules 1–12 are in
  the [CI safety/performance spec](../openspec/specs/ci-safety-and-performance/spec.md), formerly the `AGENTS.md` CI section.

The existing aggregate check names remain, and require every shard to succeed;
a skipped or cancelled shard cannot satisfy them. Lint, manifest checks and
package construction run once. The release workflow still performs its broad
unit, build and native proof checks.

The pytest session sets `PALIMPSEST_LOG_HOME` to a private `0700` temporary directory through `tests/conftest.py`. Portable CLI tests therefore never write `/var/log/palimpsest` or acquire Linux-only warning output merely because the host-global journal directory is absent. Journal failure tests explicitly replace this override with missing, malformed, or unsafe paths and still require the production warning; the fixture does not suppress or weaken that behavior.

Portable test harnesses may only use directory-relative (`dir_fd`) syscalls that
every supported interpreter exposes. `os.open`, `os.stat`, `os.unlink`,
`os.rename` and `os.replace` are available on Linux and macOS; `os.mkfifo` with
`dir_fd` is not, because `mkfifoat` is absent from some macOS builds and raises
`NotImplementedError: dir_fd unavailable on this platform`. Race harnesses that
create a non-regular node mid-read use the absolute node path; the production
reader still holds its pinned directory descriptor, so the swap it must reject is
unchanged. This restriction applies to test scaffolding only: production code in
`state.py` keeps its `dir_fd`-pinned open/stat/rename sequences.

## Native and product gates

Special lanes are explicit: `native-live`, `guest-kvm`, `guest-binary`,
`filesystem`, `gate1`, `gate2`. They retain their original prerequisites and
must not be implicitly activated by changed-file selection. The plan reports
suggested special verification separately; a still-disabled product gate is
not made runnable by a recommendation. Portable selection excludes special
nodes even if an opt-in environment variable happens to be set.

For the qualified `pieroot-server` libvirt lane, after pushing and checking out
the exact tested SHA:

```sh
env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/usr/lib/python3/dist-packages:src \
  PALIMPSEST_REQUIRE_OCI_ROOT_LIBVIRT=1 \
  PALIMPSEST_KVM_KERNEL=/home/pieroot/palimpsest-kvm-evidence/kernel-6.6.71.bzImage \
  PALIMPSEST_KVM_KERNEL_CONFIG=/home/pieroot/palimpsest-kvm-evidence/kernel-6.6.71.config \
  .venv/bin/python scripts/test_lanes.py run native-live
```

### Opt-in stage-1 GitHub KVM qualification

The 2026-10-07 owner decision disables automatic native attempts with explicit
repository variable `PALIMPSEST_KVM_ENABLED=false`. `Test.kvm` still runs on
`ubuntu-24.04` only for repository `openstack-afterglow/palimpsest`, event `push`,
ref `refs/heads/dev` or `refs/heads/main`, and literal flag `true`. Its
`Native KVM qualification policy` verdict accepts exactly `true/success` or
`false/skipped`. Disabled policy success reports that **no native proof ran**;
missing/malformed flags, failed/cancelled native attempts and enabled skip fail.
PRs run neither the native proof nor its verdict, retaining six strict hosted
required checks.

Release uses the same opt-in proof for `v*` tags. PyPI/formal GitHub publication
requires a non-cancelled trusted repository push/tag, `verify=success`, and the
same two-state native policy. Its explicit `!cancelled()` condition handles an
intentionally skipped dependency without bypassing verification. A disabled
release notice says it has **no native KVM proof**; portable/TCG results are not
native qualification. No release tag or publication is needed to test this policy.

Both pinned HTTPS URL inputs remain absent; disabling does not repair or replace
them. Re-enabling requires exact verified inputs and existing protected approval.
An older main/tag workflow retains its old gate until its source includes this
change; main promotion remains the owner's `dev → main` merge.

Configure lowercase `true`/`false`: the actual Test Bash verdict compares exact
strings, while GitHub's job expressions (including Release) compare strings
case-insensitively. Uppercase aliases are not a release-condition rejection
claim. Missing/non-boolean values still cannot satisfy the publication policy.

Both jobs use protected environment `palimpsest-native-kvm`, reviewer
`jung-geun`, no admin bypass, and deployment policies for dev/main branches
and v* tags. Their shared concurrency group has `cancel-in-progress: false`.
The old `pieroot-server-palimpsest-kvm` runner (id21) stays stopped; the new
VM is never registered as an Actions runner.

`scripts/run_native_kvm_openstack.py` authenticates with a restricted,
30-day member-only application credential in project
`53ec2dd9a1f7471fb6a2b174595fa232`. Standard `OS_AUTH_TYPE=v3applicationcredential`,
`OS_AUTH_URL`, `OS_APPLICATION_CREDENTIAL_ID`, and
`OS_APPLICATION_CREDENTIAL_SECRET` are protected environment secrets, not
command arguments. The script verifies the project-scoped Cinder catalog,
private image SHA-512, pinned flavor/network, console host key and KVM API12.
It creates at most one 2-vCPU/8-GiB VM and one 20-GiB boot volume, with TCP22
ingress restricted to the orchestrator's public IPv4 /32. Cloud and GitHub
credentials are not forwarded to the guest.

The guest consumes a SHA-256-pinned source tar and kernel/config pair:
kernel `89f7d4f31f6ef77d0f8d45810de9e19e3f8dededf3dd6ebf89bb540d26d8c0fd`,
config `4d4aaaed367bd2fb6ed1238b94ea9bb095a5e5a0d0cc67d1cb70595643cc795a`.
CI downloads them only from configured HTTPS URLs and verifies both hashes;
missing URLs fail closed. Manual proof may use already verified local files.
A manual staged-candidate tar must include the staged bytes: an archive of
HEAD alone does not qualify that candidate.


Before native execution, the helper runs the complete privileged mounted
filesystem gate, replays SquashFS in another process, compares the receipts,
and retains EROFS evidence. The canonical native command remains
`PALIMPSEST_REQUIRE_STAGE1_KVM=1 uv run python -m pytest -m stage1_kvm tests/kvm -vv`
with the pinned kernel/config and evidence-directory environment. The stage-1
receipt must cover 43 boots/44 QEMU invocations and its exact 45-file evidence
set. Historical CI run `35029001178` and artifacts `10448739883`/`10448829655`
qualified their historical commit, not this new helper or candidate.

The 2026-09-27 isolated manual run `202609271/6` passed against candidate tar
SHA-256 `88cd80c29c7bcba6a338df2bb3c2eebc5b452cea653a41885cd16d3a9ba0d30d`:
43 boots, 44 QEMU invocations, all 45 stage-1 evidence files, mounted SquashFS
and byte-identical independent replay, plus EROFS. Its stage-1 receipt SHA-256
is `6e9ec697c8802cd9cefb9a3dc4f2a7180c276af101aefe34f78259eafc163e94`.
The disposable guest uses standard `kvm` group membership with a fresh
supplementary-group session; a transient device ACL had allowed the ioctl
preflight but later QEMU failed with permission denied. Owned cleanup was
verified. This is not a GitHub run: reviewed HTTPS kernel/config URLs and an
approved push/tag remain prerequisites for the protected hosted job.

Every successful create records ownership in private, atomic
`resource-manifest.json` before continuing. A separate durable
`resource-manifest.json.creation-intent.json` precedes cloud creation: if the
primary manifest disappears afterward, cleanup fails closed rather than
claiming that no resources were created. Both are retained as workflow artifacts.
SIGINT/SIGTERM and the workflow's `always()` cleanup use that same manifest.
Signals during teardown allow reclamation to finish but still fail the gate.
Recovery discovers accepted-but-unreturned creates by exact owner identity,
refuses collisions/foreign IDs, and verifies server/volume/port/SG/keypair absence.
Cleanup failure fails the gate. After forced runner loss, use the surviving
manifest with `python scripts/run_native_kvm_openstack.py cleanup --manifest
<private-manifest>` and the same restricted credentials; never delete by a broad
name prefix. Loss of both ownership files requires manual exact-owner recovery,
not a claimed cleanup success. `cleanup_verified=true` is required in addition
to native/filesystem receipts. The helper's 2700-second proof deadline has
separate bounded evidence recovery and cleanup intervals. Failed proofs retain
safely allowlisted partial captures without a success receipt. Unit ownership/
signal regressions are classified in `core-cli`; they do not prove real cloud
reachability or KVM behavior.

Ordinary server lanes use `env -u PYTHONPATH`, `PYTHONDONTWRITEBYTECODE=1` and
`umask 022`, as the previous full baseline did. Never run fixture mutations
against an existing user VM or delete retained failure evidence to speed up a
test. A native change still requires its actual native proof; a focused unit
pass is not a replacement.

Set the process `umask 022` **before Git checkout/merge**, not merely before
pytest. Git's non-executable file entry does not encode the full required
`0644` mode: a checkout under the server's `002` mask can create the packaged
guest ELF as `0664` while its content hash and Git status still match. Retain
the strict package mode/provenance check. Do not broadly chmod a checkout or
relax the assertion; any existing mismatch needs exact-file identity/hash
inspection and a separately reviewed, narrowly scoped metadata repair.

The coordinated native case additionally requires the invoking Python to import
both the installed project and libvirt normally: clean product children do not
inherit `PYTHONPATH`. Use a qualified system-Python venv with
`--system-site-packages` and the current project installed, and unset
`PYTHONPATH`. Its targeted selector is
`tests/kvm/test_oci_root_libvirt_live.py::test_live_oci_root[True-True-True-True]`.
This exercises fresh coordinator, live console, separate-client STOP, cleanup
and retained-root reuse in one case; it is not the public build-to-run Gate 2.

Gate 1 verifies the Palimpsest local OCI build. Gate 2 retains the full public
`run -d → exec → stop → rm` contract on a qualified KVM host; Docker may coexist. Test
partitioning neither enables Gate 2 nor changes its acceptance criteria.

At exact `d72796c`, the focused local/server acceptance selection passed 100
tests on each host (7.74/6.48 seconds), followed by Gate 1's two tests
(2.47 seconds), a fresh v2 artifact build, and Gate 2's one test (18.90
seconds) on the qualified server. The run used a fresh network-none Buildx
builder, fresh runtime and private journal; the builder and successful Gate 2
run/domain were removed through their owned cleanup. Each of the 24 preflight,
post-build and final preservation observations passed for four existing
inactive domains and seven archives, with no active QEMU. See
[`oci-root-build-run-acceptance.md`](oci-root-build-run-acceptance.md) for exact
artifact digests, evidence paths, the preserved pre-Gate-1 wrapper failure and
the explicit non-claims. This is executed product-gate evidence, not permission
to implicitly enable either special lane in portable selection.

### Release preparation and publication gates

#### 0.3.4 remote build candidate — 2026-10-14

Isolated worktree `fix/hub-kolla-live-deploy` on published `7186abf`; uncommitted at verification time. Results below are local; publication, deployment and the live API build are recorded separately in the handoff.

- Full Hub suite **483 passed** (existing deprecation warnings). Full portable lane **6,473 passed, 218 skipped** (6,691 selected; skips are opt-in proofs, not qualification). An earlier focused executor/guest-builder/build/Kolla selection had **1 failed, 189 passed, 1 skipped**: the shared-data packaging contract still pinned only the role directory; after updating it for the packaged worker playbook, the full portable lane above (which includes that file) passed. Root and Hub Ruff check/format, `test_lanes.py list --check`, CLI reference and fixture checks passed; 13 integration files were formatted once after the slices landed.
- Canonical `docker/hub/Dockerfile` targets built for **linux/amd64** (the publication platform) and ran under cross-architecture execution on this arm64 host: API and export worker import 0.3.4 on `x86_64`; build worker runs as UID 0 with root/Hub 0.3.4, bubblewrap 0.12.0, mksquashfs 4.6.1, qemu-img 10.0.13 and util-linux 2.41.5. The arm64 build-worker target was built and used for the native qualification below. These are tool/import smokes, not an x86_64 executor run.
- `scripts/build_package.py` built and smoke-tested root `palimpsest-client` 0.3.4 wheel/sdist; the wheel contains `share/kolla-ansible/ansible/palimpsest-build-worker.yml` and the role. Hub 0.3.4 wheel/sdist built and the wheel passed `zipfile -t`. Python 3.11 `palimpsest --version` reported 0.3.4.
- **Native local executor qualification (real kernel, not the service VM):** Docker Desktop linuxkit `7.0.14` aarch64, privileged `--init` container of the canonical `palimpsest-hub-build-worker` arm64 image with the current executor, driven through the worker's own `run_executor`. Inputs were the real cached Ubuntu 24.04 aarch64 QCOW2 base `sha256:1d6bffe64b848468ac97f821d369a4846d983de1800ccf6b5ec8853e85cefc55` and real parent layer `sha256:6610c0116c5c83d499ccf07fa5959a6b0d4a33d32f21528326ed6f5829b4572f`. Preflight ok; build 2.5 s, output digest matched the file; SquashFS root `0755 0:0` from the base `/`; RUN-created `4321:4321` ownership, `0750`/`0644` modes, a `user.*` xattr and a `0/0` whiteout for a parent file were preserved; RUN had no network, UID 0, `CapEff=0xdb`, `NoNewPrivs=1` and a clean environment. A 45 s timeout raised `BuildTimeout`; worker `SIGKILL` during RUN left no RUN processes. Each left two attached loops that recovery cleanup detached to zero. Before the fixes the same run showed root `0700` and cleanup refusing `/worker/...` namespace-relative backing paths; recovery of those real leaked loops succeeded with the fixed code. Harness/results: `~/.local/share/palimpsest-proofs/hub-live-033-20261013/direct-executor-local/`. Not proven: x86_64 service-VM execution, Kolla-launched container, OOM/memory pressure, DNS stalls.

#### Isolated 0.3.3 management candidate — 2026-10-11

Uncommitted candidate source starts at current-dev `a428d3fdb7c4d6243833ab189fd187b03710ada3`; the shared dirty checkout is not its source. Root/Hub versions, locks and packaged Kolla default are synchronized without dependency/schema/native/CI changes. Independent console/consumer review cleared the corrected source; the original auth review's duplicate-upload finding applies to the pre-fix snapshot, not a second remaining issue. Astra participation is unavailable; review does not authorize publication.

- Root unit/Kolla gate: **6,220 passed, 209 skipped**. The planned portable set's previously uncovered pure helpers in `tests/kvm/test_oci_root_libvirt_live.py` and `tests/oci_fs/test_layer_filesystem.py` separately passed **182, with 8 skipped**. These selected helper nodes are not opt-in live KVM or privileged filesystem proofs. Ruff and manifest classification passed.
- Final full Hub gate: **337 passed**, 771 existing deprecation warnings; Ruff passed and 38 files were formatted. The meaningful publish-only duplicate regression first failed (1 failed/1 passed), then both inventory cases passed after the server fix. A same-descriptor repeat succeeds without metadata disclosure; incompatible metadata remains 409 with unchanged registration ownership.
- Root and Hub wheel/sdist build passed. The root wheel's installed Python **3.11.15** CLI reported **0.3.3** and executed help. This minimum-interpreter smoke is not the full suite on Python 3.11.
- Canonical `docker/hub/Dockerfile` API and export-worker targets built and ran as Linux arm64 and amd64 (aarch64/x86_64; amd64 is cross-architecture execution on this arm64 host). Installed auth/API/two-script SHA-256 values match final source bytes in all four owned containers. Canonical MySQL bootstrap/API lifespan, real Redis and real local Keystone with synthetic identities were used: **88 arm64 and 82 amd64 HTTP checks**, six actors, exact original-project assertions and independent capabilities, filtered pages, byte/digest/offset verification, legacy create/read/delete, native key issuance/real archive publication/verified read. No ASGI dependency override or SQLite factory replaced the registered app. Export workers were started and source-bound; no real Glance conversion or native build was exercised.
- Actual browser: native key pasted into legacy input produced **zero `/me` requests** and cleared the field; publish-only repeat upload succeeded with inventory/download still denied; real server deletion followed by Status, Resume and Abort each released controls with **zero automatic publication starts**, while warning that finalization may have committed. Separate native key/member connections published and inspected a real OCI archive. The built-in 64 MiB Blob fallback saved **20,480 bytes** identical to the original with SHA-256 `50b211cfc8d0ef4781cc8fa0d2db2681eb5d8e3af9e913d8f0b13dbb320b9e0e`. Native save-picker availability was disabled only in the test tab; its OS picker/persistence path was not exercised. A console screenshot was inspected and the tab closed.

Before-fix receipts retain the CLI inventory-403 failure and missing-session UI lock. An initial owned-fixture CAS path/mount mismatch returned upload 500; correcting the private fixture environment to the mounted CAS path resolved it without a source fallback or exception suppression. Receipts are under `/Users/pieroot/.local/share/palimpsest-proofs/hub-kolla-release-20261011/`: `local-container-ownership.json`, `arm64/actual-hub-api.json`, `amd64/actual-hub-api.json`, `browser-final.json`, `browser-console.webp` and `portable-helper-selectors.json`. Proof disks are empty or marker-only and OCI bytes are a real graph, not an executable/boot qualification. No production package write, IAM grant, Trust/Glance/native/KVM/GPU operation, operator installation, source publication or production restart is claimed. Exact-SHA hosted/tag publication and consistent SQL/CAS backup/restore precede a separately approved single-host Kolla cutover.

#### Published 0.3.1 verification

On 2026-10-08, immutable `v0.3.1` resolves to
`1458db42a1449a25b664584d144d0a97086f8f6f`. Final local root gates passed
6,435 tests with 261 opt-in skips, and Hub passed 298. Exact-commit
[Test 37725533476](https://github.com/openstack-afterglow/palimpsest/actions/runs/37725533476),
[Hub images 37725879345](https://github.com/openstack-afterglow/palimpsest/actions/runs/37725879345)
and [root Release 37725879373](https://github.com/openstack-afterglow/palimpsest/actions/runs/37725879373)
succeeded. Native KVM was deliberately skipped (`false/skipped`), not qualified.

The tag-run wheel SHA-256 is `827d6ffabb960402bc8c4dee7b8d9c4fe71903ba4a27141232ad2b9b7b5a69b0`;
sdist is `18d62f9272591a425a648e54050a24ecab36512663c81f8beff258010bc7b423`.
Both match PyPI and the uploaded [GitHub Release](https://github.com/openstack-afterglow/palimpsest/releases/tag/v0.3.1)
assets. The final GitHub job was originally skipped despite successful PyPI
publication; authorized manual repair reused these exact distributions without
moving the tag or republishing PyPI. The future-tag job now uses
`!cancelled() && needs.publish.result == 'success'`: existing evaluator smoke
reproduced old ineligibility and passed all 24 fixed acceptance/denial contexts;
focused workflow policy modules passed 127 tests. This evaluator is not hosted
GitHub execution of a new tag. No Test scheduling or performance claim is made.
The repaired source's full portable root gate subsequently passed 6,436 tests
with 261 opt-in skips and seven existing fork warnings (226.29 s). The extra
case covers final-job eligibility, not native qualification or a future-tag
hosted publication run.

Unmodified published API/worker digests in [install](install.md) were executed
as Linux amd64 under arm64 Docker Desktop cross-architecture execution. Canonical
MariaDB bootstrap and API lifespan, real Redis, API and conversion worker passed
184 package/persistence, 26 delegation and 27 independent cache checks, including
setup/repeated checks. Keystone/Glance were synthetic HTTP fixtures called by the
real client libraries; the bounded cache archive was a fixture, not BuildKit output.
All four exact-owned fixture sets were cleaned. Earlier fixture failures and their
corrections remain in the parent receipt, not hidden by a final success claim.

Receipts: `/tmp/afterglow-ecosystem-release-20261007.FDut9J/evidence/palimpsest-publication-runtime.json`
and `palimpsest-formal-release-repair.json`. `uvx --from palimpsest-client==0.3.1
palimpsest --version` reported `0.3.1`. No native amd64/KVM, live Keystone Trust/
Glance policy, installed native TLS/credential-helper transfer or production
cutover is claimed. Published Hub images are amd64 only; no separate Hub wheel
is a root-release artifact. Production IAM/auth/shared-store/recovery prerequisites
remain held. The following preparation snapshot is historical, not current status.

#### Frozen full-source candidate — 2026-10-08 (verification not run)

The isolated local `dev` candidate starts at GitHub origin/dev
`63f6f665e7f7f7469f384edd49619fa5d6683284`, preserving PR15 native opt-out and
PR16's real descendant PID-before-archive fixture fix. It integrates all 32
original tracked changes, all 13 files in four untracked groups, and the
remaining related worktree contributor/CI scenarios. Root, Hub, both locks,
module versions and Kolla's image-tag candidate remain **0.3.1**. Read-only
origin tags/releases and PyPI inspection found no stable 0.3.1 publication;
SHA development prereleases are not stable releases. No checks, builds, lint,
formatters or runtime smokes were run for this integrated source. Historical
results below must not be reused as its final receipts.

The integrating parent must run final root and separate Hub gates once after
the source is frozen, retaining their complete outputs:

```sh
uv sync --frozen --extra dev
python3 scripts/check_architecture.py
python3 scripts/check_architecture.py --staged
uv run python tests/fixtures/oci-root/generate_fixtures.py --check
uv run python scripts/generate_cli_reference.py --check
uv run python scripts/test_lanes.py list --check
uv run ruff check .
uv run ruff format --check .
uv run python scripts/test_lanes.py run full
uv run python scripts/build_package.py --out-dir "$RUNNER_TEMP/palimpsest-dist"
uv run --python 3.11 --no-dev palimpsest --version
(cd hub && uv sync --frozen --extra dev && uv run ruff check . && \
 uv run ruff format --check . && uv run pytest -v && uv build)
```

An empty changed-file plan, architecture fingerprint or source archive is not
a test pass. On the exact eventual submitted ref, retain all six strict hosted
required checks and the actual native-disabled receipt (`false/skipped`, no
proof). Do not flip KVM on to finish ordinary source preparation. Opt-in real
filesystem, guest-binary and network-none BuildKit proofs retain the canonical
commands/prerequisites in `.github/workflows/test.yml`; full portable/root/Hub
checks do not replace them. The registered-app Docker smoke below must also run
on both supported image architectures and retain its synthetic-Keystone/SQLite
boundary. Installed CLI native push/pull/login/logout and canonical
MySQL/MariaDB bootstrap/lifespan, real Keystone Trust/Glance and restoration
require their own separately recorded HTTP/runtime receipts.

The 2026-10-05 patch candidate is root/Hub/Kolla image-tag **0.3.1**;
`v0.3.1` was absent from origin at preparation. No push, tag, native approval,
publication or deployment is authorized by local release preparation.

Actual `Test` run [37185453150](https://github.com/openstack-afterglow/palimpsest/actions/runs/37185453150)
at main `6f21ef56da17416021c74230302eba2dc4c9c360` failed native job
`111386304833`, step 7, before either `curl`: **both `KERNEL_URL` and
`CONFIG_URL` were empty**, then the HTTPS guard emitted `Kernel and config
require HTTPS URLs` and exited 1. The required native aggregate also failed.
This is missing acquisition configuration, not evidence of a network failure,
checksum mismatch, or guest failure; the failed run was not rerun to confirm it.

Read-only configuration inspection found repository variable
`PALIMPSEST_KVM_ENABLED=true`, and only project/image/flavor/network variables
in `palimpsest-native-kvm`. Both native workflows bind
`vars.PALIMPSEST_NATIVE_KERNEL_URL` and
`vars.PALIMPSEST_NATIVE_KERNEL_CONFIG_URL`; neither variable was configured.
The historical verified pair is `kernel-6.6.71.bzImage` and
`kernel-6.6.71.config` under `/home/pieroot/palimpsest-kvm-evidence`, with
the hashes above. A paginated read-only release-asset search found no
published matching kernel/config filenames or pinned digests. The reachable
remediation is owner-authorized provision of HTTPS locations for those **exact
verified bytes**, followed by configuration of those two variables. Recovering
and publishing that pair and changing GitHub configuration remain prerequisites,
not source fixes. Do not replace the kernel, weaken HTTPS/hash verification,
add fallback/retries, or skip the native gate to conceal missing inputs.

Additional read-only recovery on 2026-10-05 reached the configured
`pieroot-server` SSH host (port 18209) and SHA-256-checked both current files
at that exact directory: both **match the workflow pins above**. `stat`
reports regular, single-link `pieroot:pieroot` files, mode `0400`, with
kernel size 11,535,360 bytes and config size 136,357 bytes. This establishes
the actual existing source pair, not an inferred filename-to-hash binding.
No bytes were transferred/published and no remote configuration changed.
The remaining prerequisite is explicit owner approval of an HTTPS serving
location and the two GitHub variable writes; existing public release assets
did not provide a matching serving URL.
Read-only route inspection of the existing TLS ingress configuration found no
Palimpsest/kernel/fixture route or backend; its mounted S3 certificate filenames
alone do not establish a serving path. No approved matching HTTPS pair was
located in the examined ingress or published release records. This is not
permission to alter shared ingress, start a new server, or publish bytes.



Before publication:

- Follow the normal PR path to dev and satisfy its effective strict ruleset
  (`24071416`): `Pure contracts (Python 3.12)`, `Unit tests (macOS 15)`,
  `Hub tests, lint, and build`, `OCI filesystem proof (privileged Linux)`,
  `Local OCI image product build`, and `Guest stage-1 binary (Linux x86_64)`.
  Branch-protection API returned 404, but that does not remove ruleset checks;
  do not bypass them. Keep portable/native skip receipts distinct.
- With the current owner-selected `false` policy, a future release tag skips
  the protected native job and does not request native environment approval.
  Re-enabling `true` restores that exact-ref protected proof and its pinned
  prerequisites; never approve on the owner's behalf. Keep runner21 stopped.
- `release.yml` publishes only the root wheel/sdist: rebuild/verify must succeed
  before PyPI (`pypi` trusted publisher), followed by formal GitHub release.
  Native policy must be exactly enabled/success or disabled/skipped; the latter
  has no current native qualification. The separately versioned Hub wheel is
  not published there.
- `hub-docker.yml` builds separate API and worker targets. A stable `v0.3.1`
  tag selects `0.3.1`, `v0.3.1`, `sha-…` **and `latest`** for both images:
  metadata-action's [default `latest=auto` semver flavor](https://github.com/docker/metadata-action/tree/v5#latest-tag)
  adds `latest`, independently of the explicit main-branch `latest` rule.
  Dev pushes select `dev`/`sha-…`; main selects `latest`/`sha-…`.
  This workflow waits only for its own Hub tests, not the native release gate;
  its existing publication exception is unchanged. Obtain explicit publication
  approval before any push/tag and verify both image digests/revision/platforms
  before deployment. Do not equate a tag-triggered GHCR upload with a passed
  native proof or root release.
- Kolla's source-build default remains the separately reviewed immutable
  `c4887f7806608e98f215abbd377d2eafe159ff76` (Hub 0.3.0 source), not this
  released 0.3.1 ref. To deploy this release from source, pin its
  separately reviewed `1458db42a1449a25b664584d144d0a97086f8f6f`; do not invent a circular self-pin.
  Root package installation alone does not replace running API/worker images.

#### Pending-change source review — 2026-10-06

The source review includes the pending host-share lifecycle and SYSTEM direct-Nova operator scripts, not only release metadata. Regression coverage binds relocated projects to their absolute applied source policy, preflights the recorded virtiofsd before ordinary stopped-service starts, rejects writable exports of immutable attached artifacts, and preserves ownership/freshness/partial-cleanup receipts. Public endpoint tests run actual Ansible Core 2.19 playbooks against the role defaults and both real guard tasks. Folded `{{ }}` templates use one physical backslash in regex anchors/backreferences because Ansible pre-escapes those literals; ordinary assert expressions retain their separate conditional escaping rules. Existing explicit-hostname policy and exposure opt-in remain unchanged.

The initial full run exposed five FQDN literal-escaping failures plus one applied-share fixture missing immutable artifact-path identities. Correcting those produced a local full regression of 6,385 passed/261 skipped; opt-in skips are not native qualification. Root and separate Hub lint/format, generated CLI/fixture and lane checks passed; the Hub suite passed 215 tests. Root isolated sdist-to-wheel and CLI installs passed. The final committed scope still requires the normal six strict PR contexts and separate protected exact-ref release-native approval; local receipts never replace them.

Separate actual loopback TCP Hub package-router smoke passed five scenarios using real isolated SQLite/CAS and synthetic original-token/member/owner identity: complete OCI graph/archive/ranged download, exact project/package/key/session scope, key revocation and membership removal during transfer, and offset recovery with new-key resume refusal. This is authenticated package API behavior, not a live Keystone or KVM proof. Installed openstacksdk 3.3.0 `Port.existing` resource smoke separately accepted enabled security and rejected disabled/omitted API fields.

Canonical API and worker Docker targets built for amd64/arm64; both workers reported Hub 0.3.1 and imported API/export/build-worker modules. Both actual API containers returned health/discovery 200 and missing-token/project-header-only 401 with synthetic required settings. No DB/Redis/Keystone connection was established, and exact-owned smoke containers were stopped. The isolated network-none BuildKit product proof passed two cases and its own builder was removed.

Strict privileged Linux filesystem proof retained the first environment failures: Debian's EROFS CLI was incompatible with the pinned comparison and the isolated `-I` worker could not use a PYTHONPATH-only installation. Re-running with the actual installed root package and canonical Ubuntu 24.04 prerequisites, without relaxing `PALIMPSEST_REQUIRE_OCI_FS=1`, passed all four filesystem cases; a second process passed the selected SquashFS case and byte-identical evidence comparison. These container filesystem semantics do not establish native x86 KVM, guest compatibility or production acceptance.

Subsequent dependency review mapped all eight reported default-branch advisories to actual Hub lockfile pins, not only stale publication metadata. The minimal cutover uses [FastAPI 0.133.0's Starlette 1.x support](https://fastapi.tiangolo.com/release-notes/#01330-2026-02-24), explicit Starlette `1.3.1` and urllib3 `2.8.0`, refreshing only those three dependency versions. Fresh frozen Hub lint/format, 216 tests (with the upstream TestClient deprecation warning retained) and wheel/sdist passed. Fresh canonical API/worker builds and worker imports passed for both architectures with exact installed version assertions. Each actual loopback API returned health/root/discovery 200, missing-token and project-header-only 401, and excessive URL-encoded form fields 400; owned smoke containers were stopped. This addresses the [reported form-limit advisory](https://github.com/openstack-afterglow/palimpsest/security/dependabot/5) and the current reported patched-version ranges, not a comprehensive security clearance, live identity/storage or native qualification.

The exact pinned kernel/config HTTPS URL bindings and protected native approval remain separately required. No stable 0.3.1 tag, PyPI release, production mutation or native-gate bypass is implied by this candidate review.

#### Local 0.3.1 verification evidence

Final reviewed working source digest:
`ea24a472d94ebbbad1e71262140a812a526ac91759b5c0260c6f0b109c19b3a0`
(441 files). These receipts are local candidate verification, not new GitHub
required-check results, publication, native proof or Kolla deployment.

| Final command or exercised path | Observed result |
| --- | --- |
| `uv run python scripts/test_lanes.py run full` (Python 3.13.12) | 6,165 passed, 260 skipped, 7 warnings; 178.84 s |
| `uv run python scripts/test_lanes.py run portable` (Python 3.13.12) | 6,130 passed, 217 skipped, 7 warnings; 162.02 s; 6,347 selected nodes |
| Oversized partial-evidence fixture regression, all five unsafe cases | 5 passed; 0.14 s |
| Hub separate Python 3.12 environment, `uv run python -m pytest -q` | 215 passed; 15.24 s |
| Root and Hub `ruff check .` / `ruff format --check .` | Passed; 371 root and 37 Hub formatted files |
| CLI version, generated reference, lane manifest, architecture guard | `0.3.1`; current reference; valid manifest; matching digest above |
| Canonical `scripts/build_package.py` | Built root sdist, rebuilt wheel from it, verified packaged stage-1 bytes, isolated no-deps/no-index wheel and uv-tool installs; CLI version/help passed |
| Root minimum Python 3.11.15 isolated wheel | Metadata/module/imports and CLI report `0.3.1` |
| Hub wheel and sdist, separately installed into isolated Python 3.12 environments | Metadata/module `0.3.1`; API, export-worker and build-worker imports passed |
| Canonical `docker/hub/Dockerfile` API and worker targets, `linux/arm64` and `linux/amd64` | Both built; changed frozen-sync stage executed; network-disabled runs report `aarch64` / `x86_64`, Hub `0.3.1`, successful imports |
| Actual isolated-wheel Uvicorn HTTP | Health 200; root and `/v1/` discovery 200 (`v1.0`, `CURRENT`); missing-token and project-header-only legacy calls 401; package project-context header-only call 401 `UNAUTHORIZED` |
| Actual built API HTTP, both image architectures | Startup complete; health/discovery 200; project-header-only 401 on both, missing-token 401 on amd64 |

The earlier Python 3.13 portable run failed before archive validation:
1 failed, 6,129 passed, 217 skipped, 7 warnings. Its oversized fixture declared
16 MiB + 1 regular-file bytes but supplied no file object to `TarFile.addfile`.
The one-line test-only correction supplies those bytes; extraction/acquisition
policy and all native pins/binaries/receipts remain unchanged. Final lanes above
were run after that correction, not merely rerun to confirm the known failure.

Skipped native/platform/opt-in proofs remain **unqualified**, not native passes.
The seven warnings in each root lane are Python fork-from-multithreaded-process
deprecations; they were not suppressed. Runtime smokes used no replacement
dependencies or auth mocks: DB/Redis/Keystone endpoints were deliberately
unreachable loopback addresses, and these health/discovery/missing-token routes
do not call them. Thus these observations do not establish backend health,
authenticated cloud behavior, migrations, export execution or Kolla readiness.
All exact-owned ephemeral API containers and the wheel smoke server were stopped;
no shared fixtures, persistent data, production services or runner21 were touched.

## When to broaden verification

### Selectable guest networking

Networking policy, CLI parsing, domain XML and the durable contract are
portable: `tests/unit/test_oci_network.py`,
`tests/unit/test_oci_network_live_contract.py`, `tests/unit/test_kvm_contract.py`
and the real-C guest harness `tests/unit/test_workload_network.py` (the harness
needs a working `cc`). Because the guest ELF changed, a production networking
change also requires the reproducible packaged-ELF/provenance checks and the
explicit stage-1 boot matrix before any networking proof counts.

The native proof `tests/kvm/test_oci_network_live.py` is a separate `native-live`
opt-in and never runs from changed-file selection. It needs
`PALIMPSEST_OCI_NETWORK_LIVE=1`, a `0711` runtime parent in
`PALIMPSEST_OCI_NETWORK_PROOF_ROOT`, the ordinary native boot variables, and
per-node `PALIMPSEST_OCI_NETWORK_{PYTORCH,REDIS,NGINX}_{IMAGE,ARCHIVE_SHA256,MANIFEST_SHA256}`
pins. Its three nodes are independent: NAT with a published loopback port plus
one real outbound HTTPS API request, host-only with a published port plus proven
absence of DNS/TCP egress, and NAT with an explicit `0.0.0.0` publication
reached through the host's own address. Record each node separately; a passing
portable contract is not a networking proof, and one node's success says nothing
about the others. See the [network contract](oci-network.md) for the exact
commands and limits.

### Official service-image matrix

The approved `/sys` 0555 compatibility change uses the focused real-C
`tests/unit/test_oci_guest_transition.py` with
`PALIMPSEST_GUEST_TRANSITION_DOCKER_TESTS=1`. Both proc/sys need unchanged-mode
and mode-drift cases; sys needs explicit owner, nonempty, symlink/file,
replacement, wrong-filesystem and disallowed-mode negatives. Keep dev/generic
0555 rejection. A production guest change also requires reproducible packaged
ELF/provenance checks, the explicit stage-1 boot matrix, then one pinned MySQL
case. Portable success cannot stand in for either native proof.

The independent [official service matrix](docker-hub-service-matrix.md) uses
`tests/kvm/test_oci_docker_hub_services_live.py` for PostgreSQL, Redis, MySQL,
and NGINX defaults, with a separate explicit-user Redis case. Run one exact
parameter or `-k` selection at a time; each needs its own
`PALIMPSEST_OCI_DOCKER_HUB_SERVICE_<CASE>_LIVE` and source pins. Portable
feedback is `tests/unit/test_oci_docker_hub_services_live_contract.py` plus
the reused `test_oci_docker_hub_cli_live_contract.py`, lane-manifest and
architecture checks. These tests do not implicitly enable VM execution.

Readiness is not a SQL/PING/HTTP response. An absent client or unavailable
guest loopback must not become a successful application proof. Preserve the
actual result, root/PID1 observations where reachable, failed runtime and
source hashes. Only an exactly owned active failed VM may be publicly stopped;
uncertain ownership or inactivity blocks subsequent native cases.

Redis-user network qualification cross-checks proc/sysfs interface sets and
unique positive indices: `lo` type772 with flags0x9/0x49, plus only optional
`tunl0` type768 and `ip6tnl0` type769 with exact flags0x80. A live domain XML
check independently rejects NIC devices. Reject malformed/duplicate/missing
records, unexpected devices and UP tunnels; retain all workload security,
PING, root/PID1 and cleanup checks. This test-only revision is distinct from
the earlier literal-only-lo failures and does not change production networking.

Executed checkpoint `1752ba9` (2026-09-11): all five native cases failed;
the default four never reached application probes, while explicit-user Redis
passed readiness/version/root/PID1 checks but its PING failed with network
unreachable. Focused portable contracts passed 109 tests on both local and
exact-SHA server; these are not service passes. All 41 final preservation
observations passed with no active VM/QEMU. See the matrix for exact source
pins, retained evidence, reached stages and separately scoped next steps.

### Linux legacy ArgsEscaped

For the Linux-only [process metadata contract](oci-linux-process.md), start
with `tests/unit/test_oci_process.py tests/unit/test_oci_source.py`. Before push,
also select `test_oci_image.py`, `test_oci_layout.py`, `test_oci_run_request.py`,
`test_oci_run_adapter.py`, `test_oci_store.py`,
`test_oci_docker_hub_cli_live_contract.py` and `test_architecture_guard.py`
from `tests/unit/`. This is focused consumer coverage, not the full suite.

Select the unchanged native node
`tests/kvm/test_oci_docker_hub_cli_live.py::test_docker_hub_nginx_unprivileged_detached_default_process_and_public_exec`
independently with `PALIMPSEST_OCI_DOCKER_HUB_NGINX_LIVE=1` and its `IMAGE`,
`ARCHIVE_SHA256`, `MANIFEST_SHA256` settings under the same prefix, plus the
five host BOOT/packer settings. Use the pinned original archive, no user or
command override, 512 MiB / one vCPU and network none. The proof checks process
readiness/root/lifecycle, not HTTP serving. Preserve failed runtimes and prior
inactive domains with reviewed exact inventory/hash checks before and after.
Then run the separate unchanged built-image cold exec proof below at the same
pushed SHA, sequentially. A failed or skipped native test is not qualification;
neither these checks nor the unchanged guest binary imply a new full Gate 2.

### Explicit OCI run user

The optional `run --user USER[:GROUP]` has a focused host-contract loop:

```sh
uv run pytest -q tests/unit/test_oci_process.py tests/unit/test_oci_run_request.py \
  tests/unit/test_oci_public_cli.py tests/unit/test_oci_run_adapter.py \
  tests/unit/test_oci_docker_hub_cli_live_contract.py
uv run pytest -q tests/unit/test_oci_store.py \
  -k 'boot_plan or oci_root_prepare or user_override or override_preparation or oci_root_domain'
```

The separately selected real-image node is
`tests/kvm/test_oci_docker_hub_cli_live.py::test_docker_hub_redis_detached_explicit_user_and_public_exec`.
It requires `PALIMPSEST_OCI_DOCKER_HUB_REDIS_USER_LIVE=1`, plus
`PALIMPSEST_OCI_DOCKER_HUB_REDIS_USER_IMAGE` (absolute local archive),
`PALIMPSEST_OCI_DOCKER_HUB_REDIS_USER_ARCHIVE_SHA256` and
`PALIMPSEST_OCI_DOCKER_HUB_REDIS_USER_MANIFEST_SHA256` (canonical `sha256:` pins),
and the existing five host BOOT/packer settings. It is independent of the
original `REDIS` opt-in and explicitly uses `--user redis` with the original
entrypoint/arguments. Neither test substitutes for the other.

Use one fresh 512 MiB / one-vCPU, network-none VM. Preserve all failed runtimes
and previously retained domains/disks. A separate unchanged-image cold public
exec proof checks the default launch contract at the same pushed SHA; its
existing defaults are 4 GiB / two vCPUs. Execute sequentially with reviewed
inventory checks. Guest C/ELF are unchanged by the host-only user override;
this does not itself require the whole guest boot matrix or qualify Gate 2.

### Other focused paths

Local web/inventory changes start with the portable, temporary-state selection:

```sh
umask 022
uv run --frozen pytest -q tests/unit/test_inventory.py tests/unit/test_ui.py tests/unit/test_cli_contract.py tests/unit/test_state.py tests/unit/test_run_source.py tests/unit/test_registry_intake.py tests/unit/test_web_operations.py tests/unit/test_web_job_client.py tests/unit/test_console.py
```

The web subprocess cases exercise browser/no-browser selection, a blocking
browser command, HTTP responsiveness, SIGINT/SIGTERM exit and listener closure.
Inventory cases cover OCI export provenance/availability, FIFO observation
without opening the output, local CAS tag conflicts and commit-time authority
changes. A metadata-restore fixture must replace its JSON directly rather than
re-entering the cooperating writer while its digest lock is held. Shared tag
writer changes also select `test_build.py`, `test_buildkit.py` and
`test_cloud_runtime.py`.

For real-browser smoke, use a temporary clone with `PALIMPSEST_STATE_HOME` and
isolated config, then run `palimpsest web --no-browser`, followed separately by
`--allow-control`. Read-only VM GETs can reconcile and write run ledgers, so do
not point QA at protected proof state. Verify local data with Hub unavailable,
read-only and authentication/origin refusal, tag save/conflict, polling input
stability, pause/resume, build logs and stopped-server guidance. Exercise an
actual local-cache BuildKit job via the authenticated build form, including
inline Dockerfile output, and observe its operation and image receipt. For
console changes, exercise PTY input/resize, typed exit/signal completion,
capability-disabled non-TTY controls, and disconnect immediately followed by
reload/unload; verify the owned transport is reaped. A local subprocess fixture
proves HTTP/browser/transport behavior only, not VM launch or guest exec.
Stop the owned server/browser afterward; this is not native VM/control or Hub
qualification.

#### Local connected catalog acceptance — 2026-10-15

Source is the uncommitted `feature/local-hub-catalog-20261015` worktree at base
`3868e527df1bb14f33aee5b6066faf760ad213c2`, not a new published package or Hub
deployment. The original dirty checkout/index was not used for these edits.
Use the feature worktree's `src` explicitly if reusing an existing virtualenv:

```sh
umask 022
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/unit/test_docker_hub.py tests/unit/test_remote_artifacts.py \
  tests/unit/test_web_operations.py tests/unit/test_ui.py tests/unit/test_inventory.py
```

The first attempt imported the original checkout's editable package and failed
collection (`docker_hub` missing). The corrected invocation prints/asserts its
import path before testing; that environmental failure is not a provider failure.
Ruff found strict-zip, an unused port-validation expression and fixture import
spacing; those were corrected without changing input or authorization policy.
Final focused/check receipts are recorded in the dated handoff.

Real Chromium exercised both control and read-only foreground apps using private
scratch state/config and an empty Docker auth directory. It observed public
Docker search pages (`redis`, then `alpine`), repository→tags→Back, changed-query
pagination reset, exact Alpine index, selected amd64 config and compressed-layer
descriptors, retained platform keyboard focus and a successful Skopeo download of
the pinned child. Docker's live config endpoint returned307: the original strict
no-redirect implementation failed; the final one-hop anonymous allowlisted CDN
path resolved real config bytes and verified their descriptor digest/size.

The original SDK-issued caller connected to the actual HTTPS Palimpsest Hub;
identity/project matched the original account, six artifact rows were visible,
and `aarch64`/`qcow2` filters selected one cloud image. Details resolved the ARM64
base and SquashFS chain. Download verified both actual base and layer bytes;
completion closed the modal, populated the VM form and focused `run-image`.
No VM execution was requested. Wrong project assertion was rejected (remote403,
local409) without losing dashboard authentication. Read-only connection/search/
inspection worked; a direct authenticated transfer POST returned403. Disconnect
cleared results; the Hub password field cleared and no Hub credentials appeared
in local/session storage or transfer receipts. Only the loopback dashboard token
remained in session storage. Browser network observations contained only127.0.0.1.

Desktop1440×1000 and mobile390×844 were observed; actual DPR varied1.25/2 despite
helper requests, so no nominal-DPR/performance claim is made. Screenshots show
wrapped full digests and scrollable details. No JavaScript exceptions were
observed; five cancelled/socket-reset resource requests during reload/transition
were retained, not reported as a clean network run. An unrelated pre-existing
libvirt missing-disk diagnostic was not repaired or used as feature evidence.
Both owned servers and the browser were stopped; the private SDK token was cleared.

Sanitized receipts and screenshots are under
`~/.local/share/palimpsest-proofs/local-hub-catalog-20261015/`
(`browser-smoke.json`, `hub-layer-chain-desktop.webp`,
`docker-platform-desktop.webp`, `hub-layer-chain-mobile.webp`). This is actual
metadata/download proof, not a new build, upload, native KVM, OCI-root VM,
production mutation, formal publication or Astra approval.


Acquisition changes additionally need real Docker inspect/save and Skopeo smoke
against disposable owned state. Cover qualified-only local Docker hits,
conflicting image IDs, hard daemon failures, Docker `credsStore` and per-host
`credHelpers` with their original sanitized environment, custom CA success and
untrusted TLS rejection, and a digest-pinned index whose root remains distinct
from its selected amd64 child. Loopback registry fixtures must not use production
credentials. On unsupported hosts, a bypassed capability preflight is diagnostic
acquisition evidence only; it cannot qualify public OCI-root `run` or Gate 2.
Preparation ownership changes select worker-protocol/run-adapter/project-volume
coverage as well. Run the lane planner and canonical isolated package build;
inspect the wheel/sdist's vendored terminal assets and license, then exercise
the installed wheel's authenticated asset surface.


Runtime-parent diagnostic-only edits use
`tests/unit/test_oci_host.py tests/unit/test_oci_run_adapter.py`
and `tests/unit/test_architecture_guard.py`. Inspect the lane planner as usual;
this explicit selection covers the verifier and direct launch consumer, not
the full host-runtime lane. Run the same selection on the pushed server SHA.
Injected metadata changes test rejection diagnostics, not the cause of a
previous intermittent native launch failure. Do not relax ACL/identity/ctime
checks or retry to manufacture a pass. No guest rebuild is needed when guest
source and packaged bytes are unchanged. Another cold VM attempt requires a
reviewed fresh-name strategy preserving the retained `exec-cli` definition;
unit passes do not qualify that unrun native attempt.

The read-only NPROC diagnostic has a small independent feedback loop:

```sh
uv run pytest -q tests/unit/test_oci_resource_status.py tests/unit/test_oci_public_cli.py tests/unit/test_test_lanes.py
```

See [the advisory contract](oci-resource-status.md). Its fixtures cover actual
descriptor-based reads, partial/invalid observations and no state creation or
worker launch. Changes to the shared worker limit constant additionally select
worker/store coverage and require the separate cold public exec proof below;
the diagnostic alone does not require rebuilding the unchanged guest or
rerunning the full guest boot matrix.

The main-console versus additional-exec standard-I/O diagnostic is separately
opted in with `PALIMPSEST_OCI_STDIO_CLI_LIVE=1`. Run the two explicit pytest
nodes in `tests/kvm/test_oci_stdio_cli_live.py` sequentially with `-x`: UID 0
and UID 101 each receive a fresh 512 MiB, one-vCPU, no-network runtime and a
tiny test-only scratch OCI image. The probe records bounded FD 1/2 metadata and
requires exactly three pre-existing root-owned mode-0777 single-link aliases:
`/dev/stdout -> /proc/self/fd/1`, `/dev/stderr -> /proc/self/fd/2`, and
`/dev/fd -> /proc/self/fd`; `/dev/stdin` remains absent. Only after exact no-follow metadata
and bounded target validation does it open each alias with the NGINX-relevant
write/create/append flags plus diagnostic `O_NONBLOCK`, require full reopened
FD identity, and emit one stream-specific marker through the pathname. It
never truncates, reads standard I/O, changes permissions, or creates an alias.
Before opening its own files it requires only inherited FD 0/1/2 (excluding
the inventory directory FD itself). The fixed record schema is V3; V2 receipts
are historical and must not be parsed as the new proof. It also checks the exact nine `/dev`
entries, opens its own dynamic pipe via `/dev/fd/N`, compares identities and
known read/write data, and requires ENOENT after closing that descriptor.
Direct `/dev/fd/1` and `/dev/fd/2` writes must retain endpoint identity, and
the PID1 `fd`/`fdinfo` masks must remain empty and read-only.
Do not infer pathname read denial merely from an original write-only FD.
Its portable parser/source contract is
`tests/unit/test_oci_stdio_cli_live_contract.py`. Until independent review, a
new guest ELF and exact-SHA native UID 0/101 passes, this remains `test-defined`
and does not qualify the changed packaged guest, an original application
image, Gate 2, or general OCI compatibility. Retain its receipt and exact
failed runtime rather than normalizing observed metadata.

The pre-transition PID 1 console-open-description diagnostic is a distinct
native opt-in:

```sh
PALIMPSEST_OCI_CONSOLE_OFD_LIVE=1 uv run python -m pytest -q \
  tests/kvm/test_oci_console_ofd_live.py::test_pid1_console_procfd_reopen_has_independent_nonblocking_ofd
```

It boots a test-only static PID 1 with the same qualified kernel/config and KVM
selection, 128 MiB memory, one vCPU and no network. The bounded probe mounts its
own procfs before any root transition, requires `O_NOFOLLOW` to reject the exact
`/proc/self/fd/1` magic link, then narrowly reopens that fixed self descriptor.
It checks stable character-device identity and proves the reopened descriptor is
nonblocking without changing the inherited console flags. Both fixed writes and
the final marker must occur exactly once. QEMU output is capped at 1 MiB and the
20-second boot is terminated through only its newly owned process group; evidence
is retained on failure. This diagnostic does not use or modify production
`guest/stage1/init.c`, the packaged ELF, an OCI image, or the main-output path.
Do not run it before independent code review, and do not treat compilation,
collection, a skip, or a host userspace probe as native evidence.

For worker/packer resource failures and additional-exec diagnostics, keep the
two feedback loops separate:

```sh
uv run pytest -q tests/unit/test_oci_worker_protocol.py tests/unit/test_oci_converter_first_pass.py
uv run pytest -q tests/unit/test_oci_exec_session.py tests/unit/test_oci_exec_control.py tests/unit/test_oci_exec_client.py tests/unit/test_oci_exec_public_routing.py
```

For the bounded worker-attribution change, also run the direct public consumers:

```sh
uv run pytest -q tests/unit/test_oci_run_request.py tests/unit/test_oci_run_adapter.py tests/unit/test_oci_public_cli.py tests/unit/test_oci_resource_status.py
```

Attribution tests must exercise canonical request/response validation and real
file/descriptor boundaries, with faults injected at the relevant operation.
Synthetic exception wrappers alone do not verify parent acceptance, publication
or teardown. Whole-module dependency mappings remain conservative; a reviewed
attribution-only edit may use these explicit files without claiming all mapped
consumers or the full guest matrix were exercised.

The worker file includes real bounded subprocess failure cases; the converter
file contains portable intake cases. Real pinned-packer component cases are
separate `native-live` nodes in `tests/oci_fs/test_layer_filesystem.py`; they do
not run in portable lanes and do not require mounts or `CAP_SYS_ADMIN`. Select
the exact nodes with `PALIMPSEST_OCI_PACK_LIVE=1`,
`PALIMPSEST_OCI_PACK_LIVE_PACKER` set to an absolute path, and
`PALIMPSEST_OCI_PACK_LIVE_PACKER_SHA256` set to its lowercase SHA-256. These
tests use the production packer subprocess management with small explicit byte
and time limits so parser errors remain visible. They are standalone component
tests, not hard-worker or end-to-end materializer qualification. macOS skips do
not verify those Linux paths: run the selected nodes on the exact pushed server
SHA too.

The zero-fragment regression first failed on real `mksquashfs` 4.6.1 at
`d255fd2`, separately for directory-only and empty layers. Structural verifier
v3 permits an already-in-bounds fragment-table offset when no fragments exist;
nonzero fragments still require a table. Required tables, bounds, root location
and padding checks remain intact. The verifier version participates in the
existing derived recipe/receipt identity; v2 records are not migrated or deleted.
This matches the [upstream writer](https://github.com/plougher/squashfs-tools/blob/4.6.1/squashfs-tools/mksquashfs.c)
and [Linux reader](https://github.com/torvalds/linux/blob/v6.6/fs/squashfs/super.c).
Run the unchanged real-packer nodes again after the fix; a portable structural
fixture is not proof of native format compatibility.

At `c95d948`, all three real-packer cases passed on the server (0.71 s), but
the original Redis VM then exposed the same obsolete predicate in portable
guest and C stage-1 validation. Host parser success alone therefore does not
qualify guest filesystem compatibility. Keep portable/C differential cases
separate from native VM proofs, and retain nonzero missing-table, bounds,
required-table, padding and whole-device digest negative controls.

For guest parity edits, the focused portable selection is
`tests/unit/test_oci_guest_filesystems.py tests/unit/test_oci_initramfs.py`.
The actual C source harness is `tests/unit/test_oci_guest_exec.py`; Linux uses
host `cc`, while macOS requires explicit `PALIMPSEST_GUEST_EXEC_DOCKER_TESTS=1`
with the existing pinned offline GCC image. The packaged ELF is separately
checked by `tests/integration/test_oci_guest_stage1_binary.py`, including two
pinned rebuilds matching the packaged bytes. C source checks alone do not
prove that the shipped binary changed. These tests do not mount an OCI root
or replace the separately selected original-image native proof.

Transition-target diagnostics have a separate real-C module,
`tests/unit/test_oci_guest_transition.py`, selected by
`PALIMPSEST_GUEST_TRANSITION_DOCKER_TESTS=1`. It uses the pinned offline GCC
image; compilation runs as the invoking UID, and ownership-sensitive checks
run as container UID 0 with all capabilities dropped, no-new-privileges,
network disabled, a read-only root/binary and small fresh tmpfs fixtures.
No host-root fixture mutation is needed. Without the explicit opt-in these
tests skip; skips are not evidence that C compiled or executed. Fixture
checks and fixed marker formatting do not qualify OverlayFS mount/move or
root identity; retain separate native positives/negatives and original-image
verification. In particular, a diagnostic native Redis failure remains a
compatibility failure, even when it successfully identifies the rejecting check.

For additional-exec output ownership changes, run both
`tests/unit/test_oci_guest_exec.py` and
`tests/unit/test_oci_exec_pipe_ownership.py`. The first uses real anonymous
pipes to check distinct UID/GID ownership and UID 101 self-FD reopening; the
second injects failures through the production start callsite and requires
pre-fork refusal, complete endpoint cleanup and unchanged root-owned control
pipes. These tests do not qualify the packaged ELF or native lifecycle.

The approved proc/sys `0555` exceptions use the same production policy in
the real C fixture module. It separately checks acceptance, pre-move readiness,
unchanged metadata and initial-identity retention across `0755`/`0555` changes.
Generic directory checks and `dev` still reject `0555`; wrong owner,
nonempty proc/sys, nofollow/type, special bits, replaced inode and filesystem identity
remain negative controls. The internal readiness checker uses tmpfs magic in
the restricted fixture only; the runtime wrapper fixes OverlayFS magic.
The separately approved DEV-only populated target is covered, not adopted:
test unchanged contents/readiness and continued DEV owner/mode/identity refusal,
then separately exercise real mount coverage and child-private devices in the
opt-in native diagnostic (a defined test, not yet an executed result).
Retain the existing native guest matrix and unchanged original-image runs
as separate real mount/workload evidence. A later Redis entrypoint failure
must not be hidden by changing its user, command, image or privileges.

The populated-device component diagnostic is independently selectable:

```sh
PALIMPSEST_OCI_DEV_COVER_LIVE=1 uv run pytest -q -s \
  tests/kvm/test_oci_dev_cover_live.py
```

It requires the qualified `PALIMPSEST_KVM_KERNEL` and
`PALIMPSEST_KVM_KERNEL_CONFIG`, Linux KVM/QEMU, and the already-local pinned
GCC container. Two sequential 128 MiB/one-vCPU/no-network boots use a separate
test PID1: positive mount coverage and a pre-move mode-drift rejection.
Each boot has a 20-second observation budget and 1 MiB console limit, followed
by bounded QEMU process-group cleanup. Compilation has a 60-second host budget.
The fixture explicitly uses tmpfs, not the production OverlayFS root. It
checks retained image-FD closure, trusted filesystem identity across the move,
and private child device entries with the parent mount unchanged. It does not
execute the packaged ELF, OCI application, credential drop, or PID1 protection
proof; run the existing stage-1 matrix and service case separately. Temporary
receipts and consoles remain on failure; opt-out skips are not qualification.

The official MySQL explicit-user diagnostic is a separate service case:

```sh
uv run pytest -q -s \
  'tests/kvm/test_oci_docker_hub_services_live.py::test_official_service_default_process_compatibility[mysql_user]'
```

Set `PALIMPSEST_OCI_DOCKER_HUB_SERVICE_MYSQL_USER_LIVE=1` and its own
`IMAGE`, `ARCHIVE_SHA256`, `MANIFEST_SHA256` variables with the same service
prefix, plus the existing qualified host settings. It changes only the existing
public `--user mysql` flag, retains image argv/environment, and leaves the
default `[mysql]` case untouched. It needs 2048 MiB/one vCPU sequentially and
does not add initialization credentials or capabilities. A missing opt-in is a
skip, not proof; retain failed runtime/source evidence.

The test-only random-password follow-up is separately selected:

```sh
uv run pytest -q -s \
  'tests/kvm/test_oci_docker_hub_services_live.py::test_official_service_default_process_compatibility[mysql_user_random_password]'
```

Set `PALIMPSEST_OCI_DOCKER_HUB_SERVICE_MYSQL_USER_RANDOM_PASSWORD_LIVE=1`
and its matching `IMAGE`, `ARCHIVE_SHA256`, and `MANIFEST_SHA256` values, which
pin the unchanged original archive. The test derives a no-secret config wrapper,
generates the password only inside the guest, uses public `--user mysql`, and
requires ordered initialization-complete then final-ready markers. Its socket
ping proves liveness only. The newly owned run/root must be removed on either
application success or failure; inability to prove exact cleanup fails the test.
No existing retained failure may be removed.

After changes to these paths, run the separate public exec CLI native file
below with the existing immutable Palimpsest-built image. This exercises real
cold materialization and public run/exec/stop/rm without running the entire
native suite. It still does not substitute for Gate 2.

The first public OCI lifecycle has a separately addressable native smoke:
`tests/kvm/test_oci_public_cli_live.py`. Set `PALIMPSEST_OCI_PUBLIC_CLI_LIVE=1`
and the five explicit host BOOT/packer variables documented in
[the runtime roadmap](oci-public-runtime-roadmap.md). Use a normal Python
environment with libvirt importable without `PYTHONPATH`. The old native-live
flag alone does not enable this new proof. Its successful run checks foreground,
detached, Ctrl-C and completed cleanup, not additional guest exec or Gate 2.
New host/request/adapter/CLI unit files are in host-runtime; normal removal is
in oci-monitor. Each file can also be run directly for a smaller edit loop.

Additional exec has two separately selectable files:
`tests/kvm/test_oci_exec_live.py` uses the production exec engine and requires
`PALIMPSEST_OCI_EXEC_LIVE=1`; `tests/kvm/test_oci_exec_cli_live.py` uses only public
commands and requires `PALIMPSEST_OCI_EXEC_CLI_LIVE=1`. Both require
`PALIMPSEST_OCI_EXEC_LIVE_IMAGE` pointing to the Palimpsest-built archive and
adjacent acceptance receipt, plus the host BOOT settings. Run either file on its
own; neither silently enables the other or substitutes for Gate 2. New exec
protocol/mailbox/IPC/session/routing unit files belong to oci-monitor, while the
actual guest C harness belongs to oci-guest with its own platform prerequisites.

The main-output pump and unified console queue have a dependency-free local C harness:

```sh
uv run python -m pytest -q tests/unit/test_main_output_pump.py
```

It compiles `tests/c/main_output_pump_harness.c` with the local C compiler and
directly includes the same `guest/stage1/main_output_pump.h` as production.
It exercises callback I/O, queue backpressure versus sticky diagnostic overflow,
partial writes, combined pump/queue drain and real full nonblocking pipe
recovery without a VM. This is not packaged-ELF, PID 1 or native STOP proof.

Run pipe ownership/wiring and bounded teardown independently:

```sh
uv run python -m pytest -q tests/unit/test_main_output_pipes.py
uv run python -m pytest -q tests/unit/test_main_output_terminate.py
uv run python -m pytest -q tests/unit/test_main_console_lifecycle.py
uv run python -m pytest -q tests/unit/test_main_control_deadline.py
```

These source-based harnesses must run the actual production helpers/loop with
deterministic syscall doubles; supplementary structural assertions are not
runtime fault-injection evidence. The teardown contract is five seconds of
grace followed by at most one additional second after force cleanup, with
nonblocking reap and no normal TERMINAL for undrained or failed output.
Each new portable file is explicitly registered in `oci-guest`; select it
directly during edits rather than rerunning all portable lanes.
The terminal-console helper tests check actual partial boundary delivery,
permanent/closed-sink failure and deadline expiry. Their injected boundary
bytes do not replace cryptographic frame construction or native reconnect
proof. Ordinary diagnostics retire, but signed boundary frames must actually
be delivered through the retained nonblocking sink; discard is not success.
Control I/O and random acquisition use the current cleanup phase deadline;
an expired STOP deadline cannot erase the separate post-kill drain budget.
The parser yields on EINTR and lifecycle pumping yields after at most 64
complete frames so repeated control traffic cannot starve output or teardown.
The frame's own five-second timeout remains distinct from the caller's STOP
or cleanup slice: exhausted caller time yields without dropping parser state,
while an expired partial-frame deadline still rejects the connection.

`tests/unit/test_workload_loopback.py` extracts the production loopback helper
into an actual-C syscall-double harness. It covers fresh and already-up success,
exact ioctl order and payloads, interface index/name/flag drift, every ioctl
failure, final-state rejection, and socket closure. A Linux-only compile check
binds the freestanding constants and 40-byte `ifreq` layout to the Linux UAPI.
The KVM kernel-config proof requires built-in `CONFIG_NET` and `CONFIG_INET`;
module or missing values are rejected. Native service proof remains explicit.

After `f86e4da`, the Redis-user probe also records raw interface rows and sysfs
name/flags/type/index. Only its security assertion is deferred until after
service/root/PID1 observations; it still fails overall if that assertion fails.
This diagnostic-only edit does not require another unchanged-ELF boot matrix;
run the isolated Redis-user node after exact-SHA focused contract tests and
reviewed preservation of every prior failed runtime/domain.

The production console-sink helpers and their child/parent call sites have
separate source-extraction harnesses:

```sh
uv run python -m pytest -q tests/unit/test_main_console_sink.py tests/unit/test_main_console_sink_callsites.py
```

These compile the current functions and call-site bodies from
`guest/stage1/init.c` against deterministic syscall doubles. They cover console
identity and descriptor validation, cleanup faults, and explicit close ordering
before child isolation and parent TERMINAL handling. They do not replace the
packaged-binary, PID 1, or native VM proof.

After a guest source freeze, rebuild the sealed ELF with the pinned offline
compiler and update both the binary digest and the versioned, named source
bundle digest (`init.c` plus `main_output_pump.h`). Run `test_oci_initramfs.py`
and `tests/integration/test_oci_guest_stage1_binary.py` separately, then the
exact pushed SHA on the native host. Select the stage-1 matrix, UID0/101 stdio
probe and cold public exec as distinct finite runs with inventory/archive
preservation checks before and after every run, including failures. The
updated stdio probe expects main and additional-exec output to be separate
workload-owned FIFO0600 endpoints with successful self-FD reopen, exact fixed
stdout/stderr/self-FD aliases, identity-preserving pathname reopen and observed
write on each stream. PID1 access restrictions remain unchanged. Set a healthy private
`PALIMPSEST_LOG_HOME` for stdio and cold guest-only stderr comparisons. Keep the separate
host journal failure-warning tests; never strip a warning to pass the proof.
These selections do not qualify NGINX, a new application build or full Gate 2.

For the self-FD change, run the narrow alias and parser contracts first:

```sh
PALIMPSEST_WORKLOAD_DEV_ALIAS_DOCKER_TESTS=1 uv run pytest -q -x tests/unit/test_workload_dev_aliases.py
uv run pytest -q -x tests/unit/test_oci_stdio_cli_live_contract.py tests/unit/test_oci_initramfs.py
```

The first command requires the existing pinned local Docker compiler image;
an opt-out skip is not real-C evidence. After proof-fixture provenance is
synchronized, run the filesystem/proof contracts separately. Only after
independent review, package rebuild and commit/push should the exact-SHA
server execute stage1, UID0/101 stdio, cold exec and the independent
`mysql_user_random_password` service node sequentially. Preserve all existing
failed domains and source archives. MySQL's own new root is the explicit
disposable exception, even on failure; never transfer its raw logs to debug it.

When the test-only `guest/workload-proof/proof.c` changes, also rebuild its
ELF and SquashFS fixtures, synchronize their canonical manifest/source/ELF
pins, and run the portable retained-root injection consumers before push:

```sh
uv run python -m pytest -q tests/kvm/test_oci_root_libvirt_live.py -k reuse_fixture
```

These are mocked offline-injection contracts, not an opt-in VM run. The
separate reuse fixture keeps an explicit ELF digest in addition to the shared
fixture loader; it must match the newly reproduced test binary. Retain its
domain-absence, identity and journal-replay failure controls. The broader
`core-cli qualification` selection remains the development-package gate.

The stage-1 composite reconnect proof waits for both READY_COMMITTED and the
workload's signal-armed marker before its first intentional disconnect. Pipe
delivery makes readiness and child-output observation asynchronous; the test
must establish the ordering it later verifies. The six-connection portable
fixture covers both immediately available and delayed signal readiness.
Receipt marker counts/order and authenticated boundary checks are unchanged.

The cold public-CLI proof uses one fresh eight-hex UUID suffix for both its
`/tmp/p-execcli-<suffix>` runtime and `exec-cli-<suffix>` run/domain name.
It never reuses the fixed `exec-cli` name of retained failed evidence. A name
collision is still a failure, not permission to adopt or delete a domain.
All public commands and domain checks use the selected name; root identity,
PID 1 refusal, split streams, exit status and stop/rm assertions are unchanged.
Only a fully successful proof removes its own identity-checked temporary
runtime. Failed runtimes remain available for investigation.

For this test-name-only change, select portable cold-proof contract tests plus
`test_oci_host.py`, `test_oci_run_adapter.py`, `test_test_lanes.py` and
`test_architecture_guard.py` in `tests/unit/`. Run the same selection on the
exact pushed server SHA, followed by the separately opted-in cold native file.
Check the exact preserved domain inventory/UUIDs/state/autostart and original
archive hashes before and after, including after a failed native test. This
does not require a guest rebuild or qualify the full Gate 2.

At `6c230b5`, the focused five-file selection passed 157 tests locally (9.40 s)
and the same 157 tests on `pieroot-server` (21.04 s). Portable collection
found 5,471 tests; collection is not execution. The separately selected cold
native proof passed with fresh runtime suffix `d569fc7c`, and all 21 wrapper
postflight observations passed. The proof removed only its successful fresh
run/domain/runtime; the prior Redis, NGINX and fixed `exec-cli` definitions
remained inactive with their original UUIDs and autostart disabled. All four
original archive hashes were unchanged, with no active VM afterward.
This run did not reproduce the earlier ancestor-change failure, but did not
identify or fix its cause. It is not a new application build, NGINX test,
standard-I/O pathname qualification, or full Gate 2 run.

Pinned TensorFlow and PyTorch CPU compatibility uses separate per-image opt-ins
and exact archive/manifest digests. See [OCI machine-learning image
compatibility](oci-ml-compatibility.md) for the two explicit native nodes,
resource bounds, and non-GPU scope.

- Per edit: relevant lane(s), regression tests for the change, lint and format.
- Before push: inspect the changed-file plan and run the affected portable
  lanes; include the required native/build proof for changed runtime surfaces.
- Integration/release boundary: all portable shards, applicable privileged
  and product gates, and the existing release regression policy.
- Uncertain impact, shared state/schema foundations or unexplained failures:
  broaden to all portable tests or explicit full regression.

Keep the SHA, selected lanes/shards, platform, pass/skip counts and elapsed time
with each result. Do not report a selected subset as a full regression.


## Integrated native-selection guards — 2026-10-30 source

The current integration retains `host_vm_control.py` and `retained_instances.py`
as the host-removal/retention implementation; it does not restore a parallel
`vm_removal.py`. It carries forward safe owned Lima configuration/protected-state
binding, descriptor-pinned deletion checks across rename, and libvirt native
backend/lifecycle-marker classification with inactive TPM retention preflight.
Existing default root/firmware/topology retention, separately confirmed
owned-instance purge, independent named data disks and console capabilities
remain the current contracts described in [CLI usage](cli/usage.md).

Parent qualification must cover the focused host-inventory/host-VM-control,
retained-instance, inventory and UI regressions, including changed config/state,
renamed selected native directories, backend/marker disagreement and unsupported TPM
retention before any stop/undefine effect. No checks or native lifecycle effects
were run in this documentation-integration slice. The dated receipt below
remains evidence only for its original source; its pass counts and observed
host state do not qualify these integrated guards or a deployed service.

## External host VM removal — 2026-10-16

Source: uncommitted `feature/local-hub-catalog-20261015` at base
`3868e527df1bb14f33aee5b6066faf760ad213c2`, isolated from the original dirty
checkout. No real VM was created, stopped, removed or adopted for this work.

- Final focused command: `PYTHONPATH=src .venv/bin/python -m pytest -q tests/unit/test_host_inventory.py tests/unit/test_ui.py tests/unit/test_inventory.py`
  → **301 passed in 57.05s**. This is the selected inventory/web/native-adapter
  suite, not full portable or native-daemon qualification.
- Changed Python Ruff lint/format, JavaScript syntax, portable lane manifest and
  generated CLI reference were checked. The earlier asynchronous check receipts
  were not delivered; only the subsequent explicit synchronous results are used.
- An owned real Chromium drove the actual loopback application against isolated
  native boundaries: unmarked Lima/libvirt selection, exact-name confirmation,
  hidden external run-file cleanup, changed-directory dialog invalidation,
  stale direct DELETE409, read-only403, confirmed Lima deletion and Unicode
  libvirt deletion. Ten browser assertions passed; independent disk and
  libvirt disk/NVRAM files remained, with no selected run ledger created.
- Native safety review exposed uncertain XML/OCI classification, TPM retention
  and optional marker-transition gaps. Final source requires matching full XML,
  rejects malformed/duplicate/OCI markers and compares optional run IDs on every
  native check. Persistent emulator TPMs require explicit KEEP_TPM; unsafe
  transient TPM retention is rejected before stop. Regression coverage includes
  absent→UUID, UUID→absent and UUID-A→UUID-B at both pre-stop checks.
- A final real loopback HTTP smoke after those fixes returned200 for a marked
  external libvirt selection, with native flags75 and disk/NVRAM/TPM bytes
  preserved; an earlier-marker request returned409 without stopping it. The
  libvirt adapter was synthetic, not a real daemon storage proof.
- Read-only observation of the actual host reported **15 observed / 15
  removable / 0 blocked** after the final guard. The actual dashboard had also
  rendered all15 Remove buttons enabled; none was clicked. The missing inactive
  HVF overlay now skips its native blockInfo query instead of emitting recurring
  ENOENT stderr; no global libvirt error handler suppression was added.
- Lima still offers native by-name deletion, not atomic identity compare/delete.
  A pinned directory and final checks do not exclude an independent concurrent
  Lima mutation. A deliberately reused libvirt UUID/name is not an incarnation
  counter. These native limits are not represented as stronger guarantees.

Sanitized screenshots and receipts:
`~/.local/share/palimpsest-proofs/external-vm-removal-20261016-r_yd5opx/`
(`browser-checks.json`, `post-review-http-smoke.json`,
`actual-host-controls.json`, `external-lima-confirmation.png`,
`external-removal-complete.png`, `actual-host-removal-controls.png`).
The attached-CDP browser reused a named page when opening the read-only view;
the surviving handle was explicitly returned to the control origin before
continuing. This tool behavior required no application workaround. Owned smoke
servers/browser and scaffolds were stopped/removed; original VMs, original web
process, original checkout and index were not modified.

Final detail-view browser follow-through also passed: an unmarked stopped
external libvirt VM exposed only the enabled Remove action (not Start/Stop),
then completed detail→name confirmation→DELETE with native flags75 and all
disk/NVRAM/TPM bytes retained. No JavaScript error was observed. See
`detail-removal-smoke.json` and `external-detail-remove.png` in the same receipt
directory. The last metadata regression additionally proves local lstat
PermissionError still permits daemon blockInfo capacity/allocation; only
positively missing inactive local disks skip that query.

The reviewed persistent user dashboard was then checked in actual Chromium:
all15 observed host VMs had enabled Remove controls and no JS errors; no real
Remove action was submitted. See `final-host-controls.json` and
`actual-host-removal-controls-final.png`. The browser receipt's source stamp
predates only the final negative-test rename (adoption→missing native identity),
whose two parameterized cases passed again in0.62s. Final working architecture
stamp/check passed for478 files with source SHA256
`2e4dc10fdc5bb59f58e69ca59c9fe561996a264e097daf7a82bc774716e7fdc4`.
## Default Hub, operation motion graphics, and cross-architecture OCI execution — 2026-10-16

- Default Hub fallback tested via `test_cli_contract.py::test_default_hub_keeps_authentication_required` and live browser inspection confirming `https://palimpsest.dmslab.re.kr`.
- Motion graphics tested via browser Puppeteer session recording 70 real animation frames (`build-operation-motion.mp4`) and verifying computed style transformation during running, succeeded, paused, and prefers-reduced-motion conditions.
- OCI multi-arch acquisition tested with real Skopeo acquiring `linux/arm64` `alpine:latest` root `sha256:294b683cb7249...`.
- Total focused unit test coverage: **960 passed, 1 skipped in 79.19s**.
- Static checks: Ruff lint and format clean across 278 files, CLI reference regenerated and current, test lanes manifest validated.


## Independent data volumes and retained topology — 2026-10-16

Working source is in `feature/local-hub-catalog-20261015` at base
`3868e527df1bb14f33aee5b6066faf760ad213c2`, not a published commit. New portable
behavioral modules are `test_cli_volumes.py`, `test_named_volumes.py`,
`test_retained_instances.py`, and `test_oci_volume_policy.py`. They cover
format-once/exclusive ownership, stopped reservations, exact-run removal/reuse,
retained ordered topology/backing closure/firmware, unsafe native purge refusals,
artifact-lock exclusion and authenticated removal receipts. Integration nodes in
`test_inventory.py`/`test_ui.py` cover stale same-name ledger independence,
retained-layer force refusal, malformed metadata and exact digest confirmation.

Real Chromium exercised authenticated loopback HTTP with temporary state/files
and synthetic native adapters: named-volume creation and run-form selection;
wrong/exact VM-name gates; native removal with retained archive; forced dependent
layer deletion HTTP409; exact-name archive purge; dependency release; and
URL-encoded digest deletion after fixing a browser-discovered colon-decoding
bug. An actual `volume inspect data --backend kvm` subprocess read the standalone
temporary ledger. A throwaway runtime smoke performed reserve → durable run →
bind → managed removal → second reservation; the same disk retained the exact
`persistent-data-smoke` sentinel with one synthetic formatting call.

Evidence: `~/.local/share/palimpsest-proofs/hub-motion-crossarch-3868e527-20261016/`
contains `volume-lifecycle-verification.json`, `retained-layer-blocker.png` and
`named-volume-attachment.png`. Native disk creation/attachment/mount and actual
user VM deletion were not authorized or exercised. Native OCI-root/carrier data
volume attachment remains explicitly unsupported. A crash between native launch
and reservation binding can leave a fail-closed pending reservation; automatic
crash-gap recovery is not implemented. Portable pass is not native qualification.

Selected public-consumer regression run passed **764, skipped 1** in 28.87s
(`test_cli_contract`, `test_cli_reference`, `test_test_lanes`, `test_remote_artifacts`,
`test_oci_image`, `test_oci_source`, `test_registry_intake`, `test_experimental_oci_vm`,
`test_web_operations`, `test_run_source`, `test_platforms`). The 17 repaired
removal/HTTP/overlay nodes passed in 9.68s. Scoped Ruff lint and format checks
passed (126 files already formatted); CLI reference and lane manifest checks
passed, and JavaScript syntax passed. These counts are separate selections,
not a full portable/native run.

Final synchronous selections on the completed cancellation fix passed
**228** inventory/cloud/process cases in4.12s and **106** HTTP/dashboard cases
in54.00s; the independent volume/native-adapter selection passed **451**
in4.73s. Architecture working stamp/check passed for486 source files with
SHA-256 `45102318465c4bf5bdb14685d96c8b497d7a0d9e9b8f5cad41c69369ac96ec9e`.
The earlier combined asynchronous result was not reliably delivered by the tool;
these smaller synchronous receipts are authoritative. The macOS selector stall
was fixed, not skipped: the existing real-probe deadline node and an actual
sleeping-child smoke now return bounded timeout/cancelled (0.217s observed).


## Controller HA 0.3.7 qualification — 2026-10-18

All source work is in the isolated feature worktree, based on protected dev
`8e35fd3967cdc7b2e3232b4b6f23bd1d800ca2e9`. Working review guard:
492 files, `ebc2598ad7d3266c25cf876be463d63b64a102ef9abd185ea7c0ee3ab98bb7be`.
The original dirty checkout/shared editable environment is not this candidate.

- Hub full suite: **603 passed,2 skipped**; focused Kolla controller/storage,
  placement/image consumers: **120 passed,1 skipped**. Root/Hub Ruff lint/format
  and lane manifest passed. An isolated actual redis-py5.0.1 environment also
  passed the33 cache cases before the endpoint-dedup follow-through; native lock
  remains8.1.0, not9. Neither result qualifies Redis9 or native execution.
- Parent observed failing-before malformed-URL startup traceback disclosure and
  accepted integer/string duplicate Sentinel endpoints in actual Settings and
  Ansible consumers. Fixed parser errors are credential-free with suppressed
  cause, endpoints are unique after coercion, and Kolla requires integer ports
  before counting distinct endpoints. Incidental exception-wording assertions
  and duplicate same-path timeout rows were removed, not repinned.
- Actual BuildKit synthetic-only context copy admitted a Hub clouds canary
  before the ignore fix. After precise input exceptions/private-path exclusions,
  all8 required files/subtrees were present and all8 forbidden canaries absent.
  This proves the BuildKit path, not the distinct community.docker source-mode
  context packager. No live credential was used in the context probe.
- Canonical API/export/build-worker targets built and executed on linux/arm64
  and linux/amd64. All6 reported Hub0.3.7/Redis8.1.0, expected architecture and
  config source SHA256 `b091a9462132ff320e84fecc82f0a7b720cc82749f2c0fc431081502844ddcee`.
  API/export UID1000; build-worker UID0/client0.3.7, bubblewrap0.12.0 and
  qemu-img10.0.13. These are local images/import/tool executions, not publication
  or a recipe/native build. Hub wheel/sdist build passed.
- Real disposable Redis7 primary/two replicas/three Sentinels: old0.3.6 fixed
  seed selected an ineligible replica and SETEX raised ReadOnlyError. Final0.3.7
  used primary discovery, DB9, exact bytes and two replica acknowledgements;
  after stopping only the owned fixture primary, the same client discovered the
  eligible successor in2.209seconds after one observed TimeoutError. Before/after
  values matched and both owned keys were deleted. This does not prove preservation
  of unacknowledged writes, TLS handshakes, cancellation fault injection or a
  production Valkey election; shared production Valkey was not mutated.
- Approved dedicated512GiB CephFS share/access and persistent controller1/2/3
  mounts were actually prepared. Real UID1000 cross-controller flock handoffs
  rejected all6 remote contenders; file fsync, atomic replacement, directory
  fsync and exact subsequent cross-client reads succeeded on each owner. Probe
  bytes were removed. Root-owned0555 underlying local paths rejected UID1000
  writes before mount. This is not power-loss/reboot/capacity/mount-loss fencing.
  Existing Manila identity denied raw Ceph auth-cap inspection; no elevated
  fallback or raw-cap verification is claimed.

Secret-free receipts and the exact storage approval boundary are in
`~/.local/share/palimpsest-proofs/hub-controller-ha-037-20261018/`.
Production publication, paired cold SQL/CAS restore, migration, all-controller
API/export cutover, narrow LB change, original-client bytes/CPU smoke and
stability monitoring are separately recorded when exercised; this preparation
record does not claim those gates completed.
## Artifact architecture and client compatibility — 2026-10-16 KST

The selected source is the dirty `dev` checkout at
`aa397a0fd976e35302974a347748a9773e3084bf`, not a published package or a
production rollout. Guest-target provenance, unknown legacy metadata,
immutable digest architecture, root-bound dependency chains, mixed-target
rejection, supported platform variants and client/backend restrictions are
covered by existing consumer suites. No new test file or lane was introduced.

The main portable selection passed **1,423 tests, 2 skipped** in 58.022 s:

```sh
umask 022
PYTHONPATH=src uv run pytest -q \
  tests/unit/test_inventory.py tests/unit/test_refs.py \
  tests/unit/test_oci_layout.py tests/unit/test_cli_contract.py \
  tests/unit/test_hub_contract.py tests/unit/test_remote_artifacts.py \
  tests/unit/test_build.py tests/unit/test_lima.py \
  tests/unit/test_cloud_runtime.py tests/unit/test_hub_builder.py \
  tests/unit/test_buildkit.py tests/unit/test_platforms.py \
  tests/unit/test_runtime_dispatch.py tests/unit/test_project_adapter.py \
  tests/unit/test_project_runtime.py tests/unit/test_ui.py \
  tests/unit/test_completion.py
```

The Hub API/build selection in `hub/` passed **118 tests** in 8.064 s
(`uv run pytest -q tests/test_hub_api.py tests/test_builds.py`), retaining one
existing TestClient deprecation warning. After the final compatible-list
pagination change, the root CLI and Hub-client contract files passed **202
tests** in 4.496 s. The separate image-publication/store-check selection
passed **4 tests** in 0.188 s. These selections overlap; they are not additive
full-suite counts. The broader portable result preceded the last pagination
edit; the 202-test selection exercises that final edit.

The final scoped Ruff lint check passed for 12 client modules, seven producer/
store test files, three Hub modules and two Hub test files. The generated CLI
reference, JavaScript syntax and test-lane manifest checks passed. The
pre-existing seven import diagnostics in `test_cli_contract.py` remain outside
this scoped lint result; formatting of its newly changed test ranges does
not establish a clean whole-file lint gate.

The separate 24-file format check passed for 22 files and reported remaining
format drift in `src/palimpsest_local/remote_artifacts.py` and `ui.py`.
Their unrelated existing layout was not rewritten; no clean full-format gate
is claimed.

### Exercised command, HTTP and browser paths

- Packed real ARM and x86 SquashFS directories with `mksquashfs`, published
  synthetic base/layer records through an isolated loopback Hub backed by
  SQLite, queried architecture and complete compatibility, and rejected a
  known mixed-target chain. No production identity or Hub was used.
- Verified `image push` retains a recorded ARM target without an override and
  rejects a conflicting x86 override. Unknown image targets require an
  explicit declaration; manually declared targets are not binary inspection.
- Exported/verified/extracted a bundle, downloaded the ARM image and layer
  into a fresh client state, and obtained a compatible two-artifact dependency
  result from the actual `store check` command.
- Inserted an ARM candidate without a base before a valid layer and exercised
  actual `layer ls --compatible --limit 1`: it scanned past the incomplete
  first candidate and returned the later complete compatible layer.
- Drove the actual local dashboard in owned Chrome/CDP against isolated
  synthetic store state: architecture/status filters, unknown details and
  backend/client explanations. Initially five records split into two
  compatible, two incompatible and one unknown. The later pagination fixture
  adds a sixth incomplete record; the compatible result remains two.
  The settled 390 px mobile view had no global error and no horizontal page
  overflow (`document.scrollWidth=375`). A stale operations-refresh warning
  cleared on manual refresh; the operations and artifact endpoints returned
  200. One favicon reset, but no JavaScript exception, was observed in the
  exercised artifact interactions.

Receipts and screenshots are retained under
`~/.local/share/palimpsest-proofs/artifact-platform-zm70h2be/`, including
`client-current-source-tests.xml`, `hub-final-tests.xml`,
`pagination-final-tests.xml`, `browser-proof.json`, `platform-compatible.png`,
`platform-unknown-details.png`, `platform-mobile.png` and
`validated/{cli-http-proof,publication-arch-proof,pagination-proof,pull-arch-proof}.json`.
The synthetic raw base payloads are not bootable images. This proof establishes
metadata and consumer behavior, not a new guest build/boot, Vulkan/CUDA/ABI
qualification, Linux KVM proof, full-suite result or deployment. Existing
macOS GPU hardware evidence is a separate dated proof.


## Human system administrator native management — 2026-10-17

Current verification uses the settled source/tests/scripts overlay at
`$PROOF/palimpsest-source`, where
`PROOF=/private/var/folders/1m/xfc726k14szbjnrlq5t68zjw0000gn/T/afterglow-system-admin-f6gnvztx`.
The base checkout is `aa397a0fd976e35302974a347748a9773e3084bf` plus preserved dirty
changes, not a committed/released snapshot. Exact selected source hashes are in
`$PROOF/palimpsest-final-source-manifest.json`.

The coordinator's installed-dependency runner uses an empty allowlisted
environment, loopback-only network policy, sandbox filesystem permissions and
CPU/process/file/wall-clock bounds. No dependencies are fetched. From `hub/`,
the focused selectors are:

```sh
python -m pytest -q tests/test_auth.py tests/test_packages.py \
  tests/test_hub_api.py tests/test_builds.py tests/test_database_pool.py
```

The first current run finished with **483 passed, 2 failed, 2 skipped** in
201.70 seconds (`palimpsest-final-focused.{json,stdout.log,stderr.log}`). All
new compatibility and raw-envelope regressions passed. One failure exposed the
remaining package fixture migration: its manager dependency still used ordinary
member admission for the human operator. The fixture now models the distinct
current manager context and disabled-operator revalidation without fabricating
service leaves. The other failure is the runner's 256 MiB `RLIMIT_FSIZE` rejecting
the existing real sparse PAX archive with logical size over 8 GiB; the sparse test
has not been skipped, narrowed or mocked. A complete post-fixture pass is not
claimed by this first receipt.

The final complete five-file run after fixture migration **passed: 485 passed,
2 skipped**, 207.58 seconds (runner elapsed208.41s), exit0. It used the unchanged
selectors above through the parent-provided sparse-aware runner:

```sh
"$HUB_PYTHON" "$PROOF/run_pal_sparse.py" palimpsest-final-focused-sparse \
  tests/test_auth.py tests/test_packages.py tests/test_hub_api.py \
  tests/test_builds.py tests/test_database_pool.py
```

Receipts: `$PROOF/palimpsest-final-focused-sparse.{json,stdout.log,stderr.log}`.
The two skips require the explicit disposable MariaDB ownership URL; no shared
SQL or schema was used. The original real sparse seek/footer test ran unchanged.
The parent-owned runner retains sandbox/network/CPU/process/wall-clock bounds,
uses a fresh dedicated TMPDIR/pytest basetemp, and permits a bounded 9GiB logical
file size solely to accommodate that sparse test. It monitors allocated
`st_blocks` every10ms with a256MiB threshold and owned-process-group kill;
observed peak was87,670,784bytes and the threshold was not exceeded. This is
allocated-space monitoring, **not a hard filesystem quota**. Deprecation warnings
remain (Keystone HTTPClient, Starlette test client and websockets); no warnings
were suppressed or assertions narrowed. The unavailable `python3.12` launcher
was replaced with the already installed Hub interpreter for the trusted wrapper,
not a dependency installation or runtime change. No executable source changed
between the passing smoke and this final focused run.

The actual standalone command also **passed, 49/49 expected HTTP statuses**, exit
0 in 2.12 seconds, through `run_native.py` from the overlay's `hub/` directory:

```sh
"$HUB_PYTHON" "$PROOF/run_native.py" palimpsest \
  palimpsest-final-native-http-sdk ../scripts/smoke_system_admin.py \
  --scratch "$PROOF/palimpsest-final-native-smoke"
```

Receipts are `$PROOF/palimpsest-final-native-http-sdk.{json,stdout.log,stderr.log}`;
stderr is empty. This serves actual registered `palimpsest_hub.main:app`, the
installed Keystone SDK and real loopback HTTP with fresh isolated SQLite/CAS.
Zero-target-role admin A manages B: namespace registration, bounded OCI image
package upload/publication/inventory/download/delete; native cloud-image/layer
upload/publication/detail/blob/bundle/delete; own-key issue/list/revoke and bounded
key downloads; target-bound build enqueue/list/detail with original requester
audit. Raw direct authority transitions to group-implied authority with an empty
effective-system view; group/edge revocation denies existing keys. Final
revocation denies pending PATCH/new upload/build access before I/O, confirmed in
SQL and staged bytes. Existing smoke upload-offset and package/build fixture
repairs are preserved.

Installed-SDK/loopback regressions now admit project/system/unscoped standard
human `token` and `password`+`token` subjects with zero target roles, alongside
password/TOTP/federation. Trust, application-credential, OAuth, bind and access-rule
markers on the original subject deny broad administration, including explicitly
empty markers. Original subject/project/auth reference and service/validator
exact-ID exclusions remain intact. Existing ordinary keys never inherit later
system elevation; admin-issued keys retain only declared actions/scope and fresh
native owner, domain and target checks.

Raw collection completeness tests exercise actual JSON envelopes, not truthiness
flags normalized by SDK lists. Both ordinary/admin tokens and existing keys
require `links.next` absent/null and `truncated` absent/null/literal `false` for
roles, inference and effective-all assignments. `next=false/0/""/[]/{}` and
`truncated=0/0.0/""/[]/{}` yield 503. Existing all-scope service-veto regressions
still require 503 when project/domain service grants are hidden behind partial
effective-all metadata and 403 with complete service records. Raw exact-system
grants and native domains stay independently complete in those cases.

No production endpoint, IAM or existing schema was accessed. Fresh SQLite
bootstrap does not qualify a MySQL/MariaDB upgrade. No Redis, Docker, VM/build
worker, KVM, provider export, deployment, full-suite or bootable-image qualification
is claimed. Synthetic image/layer payloads prove metadata/authority only.
Architecture stamp/guard remains parent-owned and was not run; no lint/formatter
or gbrain indexing/search-guidance refresh is claimed.


## Host VM removal across state roots — 2026-10-18 KST

Source is the preserved dirty `dev` checkout at
`aa397a0fd976e35302974a347748a9773e3084bf`. The user reported working detail/
console access but disabled removal for service-created VMs. Read-only native
inspection found16 VM rows (15 Lima, one libvirt/HVF), ten with a canonical run
marker and six unmarked. The selected default state did not contain their run
ledgers. No real VM was stopped, deleted, adopted or reconfigured.

The regression `test_host_removal_does_not_require_selected_state_ledger`
failed before implementation because `lifecycle.remove` was false. The first
integrated inventory/host/HTTP selection passed **395 tests** in58.829s.
After native-review corrections, the final selected consumer command passed
**895 tests, zero failures/skips** in64.699s:

```sh
umask 022
PYTHONPATH=src uv run pytest -q \
  tests/unit/test_host_inventory.py tests/unit/test_inventory.py \
  tests/unit/test_ui.py tests/unit/test_lima.py \
  tests/unit/test_cloud_runtime.py tests/unit/test_runtime_dispatch.py \
  tests/unit/test_console.py tests/unit/test_platforms.py
```

The intermediate run is retained: **888 passed, five failed**. Four failures
exposed the invalid assumption that an unlinked, held directory has zero link
count on Darwin/APFS; an actual throwaway-directory probe observed link count2
after unlink. F_GETPATH followed a rename and retained the last pathname after
unlink. The correction compares that descriptor path's current inode with the
held inode, rather than requiring a Linux link-count invariant on Darwin.
The fifth failure was an existing external-console ready-vm-type refusal
reported as `transport` rather than `identity`. The final corrected-source run
passed that selection without changing console source; this is not evidence
that the intermediate categorization discrepancy was fixed. Counts overlap
and are not full-suite claims.

Native storage regressions cover physical/config/backend/marker changes,
protected/OCI/refused identities, marked/unmarked and persistent/transient
domains, strict command/SDK/absence failures, Unicode libvirt names, retained
external disks/NVRAM, renamed-original false receipts and TPM retention. TPM
preflight inspects both live and inactive definitions before stopping; missing
retention support and transient emulator-TPM removal refuse before mutation.
Fixtures model actual temporary-file retention/deletion, not forwarding echoes.

### Actual HTTP and browser execution

An owned executable `limactl` simulator runs as a real child process, reading
only private synthetic instance directories. The packaged dashboard, inventory,
native removal implementation, HTTP authentication/CSRF and frontend remain
real. Real libvirt observation is disabled only in this proof harness; the
simulator never delegates to a real hypervisor.

- Marked running VM removal succeeded from the list; unmarked stopped VM
  removal succeeded from Details. Exact-name confirmation enabled submission;
  blank/wrong names did not. Host run-file deletion was hidden and disabled;
  unmarked details offered no selected-state start/stop controls.
- Real HTTP additionally removed a marked stopped krunkit fixture and an
  unmarked running VZ fixture. Six missing-identity, wrong-name and run-file
  requests returned409 without native deletion.
- A changed same-name instance disabled an already-open confirmation before
  DELETE. A later fresh explicit selection was distinct from that refused
  request. Independent disk bytes and other-state record bytes were retained;
  the selected run directory stayed empty throughout (no adoption).
- After the Darwin correction, a fresh native selection completed the actual
  subprocess/physical-directory absence path successfully. This smoke is
  retained separately as `final-native-smoke.json`. Browser error collection
  remained empty in the exercised list/detail/dialog/replacement paths.
- Fresh read-only eligibility reported all16 real host rows with native
  fingerprints and removal capability. A separate before/after comparison
  retained all16 names/backends/run markers/runtime kinds/statuses unchanged.

Final scoped Ruff lint passed for the four changed Python modules and three
test files. Six selected Python format checks passed; the pre-existing
`ui.py` whole-file format drift was not rewritten or claimed clean. JavaScript
syntax, generated CLI reference and test-lane manifest checks passed. No new
test file/lane, dependency, schema, workflow, commit, push or deployment was
introduced. This is source/portable/simulated-process verification, **not real
Lima/libvirt teardown or TPM hardware evidence**. Lima's name-addressed command
is not atomic against arbitrary concurrent same-UID instance replacement.

Secret-free JUnit, host comparison, private simulator receipts and screenshots:
`~/.local/share/palimpsest-proofs/vm-remove-09_cakkk/`. Key files are
`failing-before.xml`, `focused-after.xml`, `final-removal-tests.xml`,
`final-platform-removal-tests.xml`, `host-preservation.json`,
`host-eligibility-after.json`, `darwin-directory-absence.json`,
`browser-proof.json`, `http-proof.json`, `final-native-smoke.json` and
`verification.json`; screenshots are `removal-{list-after,dialog,after,replacement-blocked}.png`.
Owned browser/dashboard proof processes were stopped and proof data retained.
No original VM or state root was used as a destructive test target.


## API-uploaded build inputs and NVIDIA attempt — 2026-10-21 KST

`umask 022 && uv run pytest -q tests/unit/test_buildkit.py tests/unit/test_packages.py tests/unit/test_cli_contract.py` passed471 tests (zero failures/skips). Actual published Hub uploaded-base/parent CPU build and offline archive-backed PyTorch COPY-layer build passed, including full downloaded digest and derived marker consumption. FROM-only empty-cache-export rejection remains a failed attempt. No full-suite or GPU pass is claimed.

Approved maximum-one3060LHR Nova attempt remained BUILD after903.88s; its create event reports compute build Error, with no attached volume or detailed reason. No guest login, standalone driver build/load, mount, CUDA/framework GPU execution or final Torch interactive access was verified. Native archive publication separately failed403 under the original human admin; no auth bypass/deployment occurred. See the dated handoff and `~/.local/share/palimpsest-proofs/api-nvidia-kms6qrv4/` for exact identities, JUnit, source guard, failures and retained resources.

## Resumed NVIDIA layer and original-image diagnostics — 2026-10-21 KST

After operator role separation and explicit failed-VM replacement approval,
the bounded single3060LHR instance booted and remains ACTIVE. Actual console
password login, pieroot sudo, pinned SSH via the existing management jump,
cloud-init done and restored guest monitoring services were checked.

Rebuilt NVIDIA535.309.01 for guest kernel6.8.0-57-generic, canonically packed
an x86_64 SquashFS driver layer, loaded its compute modules and used its NVML.
Mount filesystem/ro/backing-path/inode checks passed. PyTorch2.8.0+cu126 and
CUDA12.6.3-devel performed actual assigned-GPU2x2 math (sum134, no CPU fallback)
over5 and11 original-image-derived mounted SquashFS layers respectively.
This is Nova-guest diagnostic chroot proof, **not original OCI-root boot**.

Original TensorFlow2.21 GPU exposed type-bearing tar mode0120777. New matching
regular/directory/symlink/hardlink acceptance cases failed before the narrow
normalization fix. `uv run pytest -q tests/unit/test_oci_converter_first_pass.py
tests/unit/test_oci_convert_security.py` then passed153 with7 skips; scoped
Ruff passed. Independent source/proof review found no remaining blocker.
Isolated two-file staged architecture guard passed; shared marker/index were
not stamped. Corrected real conversion completed18 TensorFlow layers, but
GPU registration failed because its build requires cuDNN9 while the unchanged
archive contains only cuDNN8.9.6. Full original layer inventory and dlopen
diagnostics confirmed the mismatch; no CPU fallback or library replacement.

Final PyTorch environment and driver mount are retained; launcher re-entry
with a real torch GPU command passed. No automatic reboot restoration was
qualified. Native publication is still403 and driver API readback404; no
native API upload/OCI-boot success, full-suite pass or production deployment
is claimed. Exact identities, hashes, runtime receipts, errors and access
limits are in the dated handoff and private proof root
`~/.local/share/palimpsest-proofs/api-nvidia-kms6qrv4/resume-20261021/`.

## Driver-free Ubuntu layer transfer proof — 2026-10-21 UTC

After explicitly approved sequential VM replacement, exact image
`ubuntu:24.04-server-pbr-hotplug-26-07-29` (`2a57a904-fc77-46e6-851a-148e8cb0ca98`)
booted with no NVIDIA packages, loaded modules, nvidia-smi or firmware directory.
The requested earlier nvidia-v3 image had already contained535.309.01; that
prerequisite failure is separately retained in the handoff, not called clean proof.

Transferred the existing canonically packed x86_64/kernel6.8.0-57 driver artifact
unchanged, verified SHA256 plus mount/backing-file identity, loaded its compute
modules and NVML, and directed guest firmware lookup into its read-only mount.
RTX3060/535.309.01/12288MiB succeeded without installing NVIDIA packages.
Actual firmware consumption, fresh compilation on this clean guest and other
kernels were not qualified.

Full original archive hashes matched before canonical materialization. PyTorch5
and CUDA11 mounted layers each passed assigned-GPU matrix math sum134, refusing
CPU fallback. TensorFlow18 layers materialized but GPU acceptance failed:
cuDNN9 required, libcudnn.so.9 absent, cuDNN8.9.6 available and physical GPUs[].
All framework runs are diagnostic guest chroots, not original OCI VM boot.
Retained PyTorch launcher passed real GPU re-entry; password-only SSH and sudo
passed. The final VM remains ACTIVE, with no reboot-persistence qualification.
No new source tests or API publication claims; receipts are under
`~/.local/share/palimpsest-proofs/api-nvidia-kms6qrv4/plain-ubuntu-hotplug-20261021/`.

## Native unprivileged GPU shell correction — 2026-10-21 UTC

The earlier enter-pytorch root chroot lacked the driver's bin in PATH; explicit
GPU proof success did not cover plain interactive nvidia-smi. Replaced that usage
with global profile/interactive-Bash PATH configuration, ldconfig registration
of the mounted driver libraries and a read-only bind of original-image Conda
at /opt/palimpsest/pytorch. The obsolete entry wrapper is archived, not aliased.

Actual new PTY SSH login as pieroot UID1001 ran nvidia-smi directly and gpu-check
without sudo/chroot. The single executable gpu-check.py prints real nvidia-smi
output and checks an exact CUDA matrix product/sum134 with CPU fallback refused.
Direct remote command, login Bash and NVML resolution without LD_LIBRARY_PATH
also passed. No new driver packages, shell binary patch, reboot or source tests;
runtime receipts are in the existing proof root's shell-usability directory.
Reboot-persistence and the earlier TensorFlow/API/OCI-boot limitations remain.

## Cinder-backed native runtime after reboot — 2026-10-21 UTC

Added reusable account-free `layer cloud-init --config` export for prepopulated
ext4 UUID storage under `/srv/palimpsest`, digest-bound readonly stacks and native
prefixes, global PATH/linker configuration and runtime checks before SSH service.
Current guest migration used a newly owned40GiB Cinder volume; no other disk was
formatted. Generated runtime/SSH graph passed actual guest systemd verification.
Source tests cover architecture/storage/blob/mount/collision/error boundaries,
an unrelated Ubuntu stacked mount, repeated bootcmd without socket activation,
and atomic output refusal with an intervening destination. Focused cloud-init/CLI
selection passed315 tests; scoped source/cloud-init Ruff, CLI reference and lane
manifest passed. Unrelated existing CLI test-tail lint findings remain separate.

Kernel147 modules were built from a checksum-verified535.309.01 source extract
without installing NVIDIA packages. Driver ownership was repacked rather than
relaxing root-owned prefix validation. A distinct hash-pinned TensorFlow dependency
layer over original PyTorch fixes the combined native runtime; the earlier original
TensorFlow image's cuDNN9 failure remains valid.

Approved same-VM reboot actually loaded6.8.0-147-generic and restored external
storage, driver and both frameworks before SSH daemon startup. New unprivileged
PTY login ran nvidia-smi and gpu-check; both Torch and TensorFlow GPU matrices
returned sum134 and cuDNN convolutions passed, with CPU fallback refused. Native
nvcc12.6 sm_86 compilation/kernel execution and pip check passed. Every active loop
backing file is on the Cinder filesystem; no home runtime mounts remain. Historical
home proof artifacts were archived, not deleted. Boot activation took about252s;
the earlier240s banner wait timed out while the gate remained closed.

Scope is migrated existing guest plus actual reboot/login, not fresh-instance
cloud-init provisioning, public Nova orchestration, Manila or arbitrary GPU/kernel
compatibility. Exact source guard, identities, failures and receipts are in the
handoff and private `srv-cinder-runtime/` proof root; guest completion/manifest
are `/srv/palimpsest/receipts/`.

The integrated wheel also built successfully and passed a fresh Python3.11.15
installation/actual CLI export smoke. Installed output is byte-identical to the
verified ready cloud-init; packaged module bytes match reviewed working source.
wheel-export-provenance.json seals the candidate hash; no publication occurred.





## Fresh paired GPU guests and immutable Cinder sharing — 2026-10-21 UTC

Source selection and local installed-package smoke:

```sh
umask 022
uv run ruff format src/palimpsest_local/external_layers.py tests/unit/test_cloudinit_guest.py
uv run ruff check src/palimpsest_local/external_layers.py tests/unit/test_cloudinit_guest.py
uv run pytest -q tests/unit/test_cloudinit_guest.py tests/unit/test_cli_contract.py
uv run python scripts/generate_cli_reference.py --check
uv run python scripts/test_lanes.py list --check
uv build --wheel --out-dir "$PROOF/wheels"
"$PROOF/client-env/bin/palimpsest" layer cloud-init \
  --config "$PROOF/external-runtime.json" --output "$PROOF/installed-runtime-cloud-init.yaml"
```

The source selection passed **325**, zero failures/skips; scoped Ruff/format,
reference and manifest checks passed. The built wheel was installed without
dependencies into fresh Python3.11.15; installed exporter bytes match source
output. No full-suite/full-tree lint result or release is claimed. Existing
unrelated CLI-test-tail lint findings were not rewritten.

New behavioral regressions cover invalid nonboolean `storage_read_only`, refusal
of writable storage, replay-enabled ext4 and writable block devices, accepted
noload/norecovery aliases, and existing empty ROFS mountpoints without mkdir.
Actual generated mount unit semantics were also exercised for true, false and
omitted policy. Independent source review found no blocker.

Fresh anonymous pull of pinned original Torch2.8.0 CUDA12.6 OCI content and five
canonical cold materializations preceded creation of a clean root-owned ext4
data image. On-image ownership and consistency, QCOW2 check, local/Glance MD5
and SHA512 agreement were observed. Two fresh Nova plain-Ubuntu GPU guests then
booted with composed account/exporter policy and the **same**20GiB Cinder
multiattach volume. Cinder3.44 reports both attach/connection modes `ro`; each
guest reports blockRO1 and ext4 `ro,nosuid,nodev,norecovery` at `/srv/palimpsest`.
No writable attachment or provider/guest writer was used after finalization.

Both boot-time ordinary-user checks and actual password-only pinned-host-key
interactive SSH ran nvidia-smi, default Torch Python and gpu-check with no manual
environment entry, sudo/chroot compute wrapper, driver package install or home
runtime tree. GTX1080Ti535.309.01, Torch2.8.0+cu126 and cuDNN91002 performed exact
CUDA matrices/sum134 and convolutions4 without CPU fallback. Root permission
was verified separately with sudo, not used to count framework success.
The first A harness encountered queued-input/sudo flushing, logout-errexit and
ANSI marker issues; raw failed evidence is retained, and no guest repair was
needed. B's first password/PTY session passed the corrected prompt-paced harness.

Overlapping15-second CUDA workloads passed on different GPU UUIDs/guest boot
IDs. All seven shared blob digest/size/inode/owner records and filesystem UUID
matched. Same-path private-root markers were independent and removed afterward.
Runtime finished before SSH startup at approximately234s/239s after boot; API
ACTIVE and early banner timeouts alone were not treated as readiness.

Both new VMs and40GiB boot disks, shared data volume, access resources and the
original VM remain retained. Both guests are on `dms-compute8`; cross-host,
Manila, arbitrary GPU/kernel, new-VM reboot, TensorFlow and nvcc qualification
are outside this proof. Base-image openipmi failed on both, fwupd/refresh also
failed on B; these were recorded rather than repaired or hidden. Full resource,
SDK upload/attachment and harness failure details are in the latest handoff.
Receipts: private local `multi-instance-torch-5aglh79o/` proof root and
user-readable guest `/var/lib/palimpsest/receipts/`. Do not publish the private
proof directory wholesale or copy credentials into documentation.

## Nova readiness probe timeout regression — 2026-10-23

- A30-second SSH connect budget collided with the30-second subprocess budget; `_wait_guest` let TimeoutExpired escape, so the first probe prematurely ended all20 fresh direct-stage attempts. First four observed request-to-failure elapsed55.65–58.16s. These records are not GPU failure or timing samples; all20 servers were deleted404 and boot volumes retained.
- `test_readiness_poll_survives_probe_timeout_until_deadline` covers both timeout→later readiness and repeated timeouts→exact overall deadline with retained partial stderr/timed_out status. Both fail before the fix; focused metrics29 passed afterward. SSH ConnectTimeout returns to10s, per-probe timeouts remain at most30s and both probe/sleep are clipped to remaining readiness time. No overall readiness deadline is extended.
- A throwaway actual Python child exceeded a .05s subprocess timeout and the next real child returned readiness; receipt `restart-readiness-runtime-smoke.json`. A fresh one-VM RTX3060 cloud-init collector qualification runs separately under sleep inhibition and is excluded from timing repetitions. Pending live result is not claimed by the local proof.

