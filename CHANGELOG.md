# Changelog

All notable changes to Palimpsest Local are documented here.

## [0.3.1] - 2026-10-05 (candidate, unpublished)

- Prepare root `palimpsest-client`, independent `palimpsest-hub`, their module versions/lockfile entries and packaged Kolla image-tag default at 0.3.1. Preserve dependency resolution, native stage-1 binary/protocol, runtime and Afterglow tracking baseline. This patch preparation supersedes the 0.3.0 candidate metadata below; it does not claim a formal 0.3.0 release.
- Fix the oversized partial-native-evidence test fixture to supply its declared regular-file payload to `TarFile.addfile`. Final Python 3.13 portable verification exposed the missing file object before the production archive validator was reached. The fixture now exercises the intended oversized rejection without changing acquisition, extraction policy or native receipts.
- Diagnose actual main native run `37185453150`, job `111386304833`, step 7: both pinned acquisition URL inputs were empty and the HTTPS guard failed before either download. Read-only repository/environment inspection confirms the two URL variables are absent. No source defect or network/checksum failure is established. Exact pinned kernel/config hosting and owner-authorized variable configuration are blocked prerequisites; retain both hashes, HTTPS enforcement and required native gate without replacement/fallback.
- Document effective strict dev required checks, separate release-native owner approval, root-only PyPI/GitHub publication, and separate Hub API/worker image publication. Stable semver tags also update `latest` through metadata-action defaults; GHCR's existing own-Hub-test-only gate is not native success. No push, tag, environment approval/configuration, publication or rollout is performed by this preparation.
- Keep Kolla source mode pinned to separately reviewed immutable `c4887f7806608e98f215abbd377d2eafe159ff76` (Hub 0.3.0), not the uncommitted candidate. Source-mode 0.3.1 deployment requires the separately reviewed committed candidate SHA; image-mode deployment requires both published 0.3.1 image digests. See [release gates and failure evidence](docs/testing.md#release-preparation-and-publication-gates).
- Fail closed when deriving the public HAProxy hostname: accept only an exact HTTPS hostname origin with an optional trailing slash and disable empty-derived public maps even when a HAProxy-only play bypasses role prechecks. Preserve explicit overrides and opt-in exposure; add real Ansible rendering/precheck behavior regressions. Ansible Core is a development-only dependency; root runtime dependencies remain empty.
- Include the pending host-directory share lifecycle and SYSTEM direct-Nova operator tooling described below in the 0.3.1 dev candidate; this is not a stable-qualified release or a new native/cloud proof.
- Bind execution fingerprints to absolute host-share sources so relocated projects recreate instead of retaining old exports. Preflight recorded virtiofsd before every ordinary stopped-service start and before mixed-plan mutation. Reject writable shares exposing attached immutable base/layer artifacts in project preparation, direct dispatch/run and applied restart.
- Harden SYSTEM ownership and cleanup: require SDK-normalized enabled port security, durable keypair public-key identity, fail-closed RGW presence and pre-tag bucket receipts, timestamped exact-share Cephx access reconciliation, and recorded first-boot installation identity across phase resumes. Preserve unresolved objects/bindings while still cleaning independently ownership-verified resources. These portable regressions are not live cloud qualification.

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
