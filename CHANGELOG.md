# Changelog

All notable changes to Palimpsest Local are documented here.

## [Unreleased]

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

### Fixed

- Added `base_image_digest` to Hub layer responses so a pulled root runtime layer preserves its boot-image chain.
- Runtime base architecture is now checked before BuildKit starts, and VM run receipts distinguish KVM direct-block attachment from Lima copy/loop activation.
- Split OCI image publication (`build --push`) from Hub runtime-block publication (`build --runtime-push`) and kept legacy Hub boot-image commands distinct from Docker image commands.
- Hardened registry/cache inputs against inline credentials, kept cache specifications out of receipts, and made strict-offline builds reject every registry-facing option before solving.
- Moved KVM readiness after typed provisioning and added a fresh per-boot readiness service so project restarts cannot reuse an old console sentinel.

### Documentation

- Reworked the repository README into a public project guide with installation, Hub configuration, artifact workflows, macOS Apple Silicon usage, Linux KVM requirements, layer builds, and development checks.
- Added this changelog.
- Added a declarative project schema, lifecycle, backend-support, and security guide.

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
