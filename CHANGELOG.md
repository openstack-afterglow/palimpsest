# Changelog

All notable changes to Palimpsest Local are documented here.

## [Unreleased]

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
