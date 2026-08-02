# Changelog

All notable changes to Palimpsest Local are documented here.

## [Unreleased]

### Documentation

- Reworked the repository README into a public project guide with installation, Hub configuration, artifact workflows, macOS Apple Silicon usage, Linux KVM requirements, layer builds, and development checks.
- Added this changelog.

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
