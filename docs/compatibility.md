# Palimpsest Local Compatibility & Integration Contract

`palimpsest-local` provides an independently versioned Python library and CLI (`palimpsest`) that integrates with Afterglow Hub while enforcing strict artifact verification and local KVM runtime invariants.

Palimpsest Hub's native `/v1` artifact API and external Docker/OCI `/v2` registries are distinct protocols and storage domains. The client does not reinterpret one as the other.

---

## 1. Standalone Palimpsest Hub API Contract

`palimpsest-local` communicates with dedicated Palimpsest Hub instances over a native `/v1` REST API using project-scoped Keystone token authentication.

- **API Base Prefix:** `/v1`
- **Authentication Headers:**
  - `X-Auth-Token: <token>` (Required Keystone auth token)
  - `X-Project-Id: <project_id>` (Optional project ID scope header)
- **OpenAPI Security Scheme:** `KeystoneToken` (`apiKey` in header `X-Auth-Token`)
- **Content Media Types:**
  - `application/vnd.afterglow.palimpsest.layer.squashfs.v1` (SquashFS layer)
  - `application/vnd.afterglow.palimpsest.layer.config.v1+json` (Layer configuration metadata)
  - `application/vnd.afterglow.palimpsest.buildkit.cache.v1.tar` (portable BuildKit local-cache archive)
  - `application/vnd.afterglow.palimpsest.image.qcow2.v1` (Bootable qcow2 cloud image)
  - `application/vnd.afterglow.palimpsest.image.raw.v1` (Bootable raw cloud image)

### Local Client Endpoint Contract

The table below describes the native `/v1` endpoint surface supported by the standalone Palimpsest Hub API and `HubClient`.

| Method | Route | Description |
|---|---|---|
| `GET` | `/` | Root version discovery (OpenStack style) |
| `GET` | `/v1/` | Version discovery document |
| `GET` | `/v1/health` | Health check endpoint |
| `GET` | `/v1/images` | Query boot images (`ubuntu_base`, `arch`, `os_variant`, `disk_format`, `limit`) |
| `GET` | `/v1/layers` | Query layers (`name`, `kind`, `chain_id`, `parent_digest`, `limit`) |
| `GET` | `/v1/layers/{digest}` | Retrieve layer metadata record |
| `GET` | `/v1/layers/{digest}/ancestors` | Retrieve ordered parent chain digests |
| `GET` | `/v1/layers/{digest}/blob` | Download image or layer blob (supports HTTP `Range`) |
| `POST` | `/v1/uploads` | Initiate resumable upload session |
| `GET` | `/v1/uploads/{session_id}` | Query authoritative upload offset & session status |
| `PATCH` | `/v1/uploads/{session_id}` | Append payload chunk (requires matching `Upload-Offset`; 409 on mismatch) |
| `PUT` | `/v1/uploads/{session_id}` | Complete upload session with layer metadata (requires matching `Upload-Offset`) |
| `DELETE` | `/v1/uploads/{session_id}` | Cancel active upload session |
| `POST` | `/v1/bundles` | Generate bundle tarball for stack (`refs`, `include_base_image`) |
| `POST` | `/v1/bundles/import` | Import OCI image-layout bundle with content-addressed digest re-verification |

---

## 2. Docker/OCI Registry Compatibility

Palimpsest delegates the following commands to the installed Docker CLI while adding registry-profile reference resolution:

| Palimpsest command | Docker operation | Scope |
|---|---|---|
| `login`, `logout` | `docker login`, `docker logout` | Selected profile endpoint or explicit server |
| `pull`, `push` | `docker pull`, `docker push` | Ordinary Docker/OCI images |
| `tag` | `docker tag` | Local source image to resolved target reference |
| `images` | `docker images` | Local Docker image store |
| `image inspect` | `docker image inspect` | Local Docker/OCI image metadata |
| `image history`, `history` | `docker image history` | Image layer history; top-level alias retained |
| `image rm`, `rmi` | `docker image rm` | Local image removal; top-level alias retained |
| `image save`, `save` | `docker image save` | Docker image archive export |
| `image load`, `load` | `docker image load` | Docker image archive import |
| `docker ...` | Arbitrary Docker CLI arguments | Generic passthrough without profile reference rewriting |

These top-level image commands are intentionally separate from Hub boot-image commands such as `palimpsest image ls`, `image pull`, and `image push`. Registry operations use Docker's existing `DOCKER_CONFIG` or `~/.docker` credential helpers; the Palimpsest profile file contains no credentials and accepts login passwords only through Docker's interactive flow or `--password-stdin`. The generic passthrough rejects a Docker-global `--config` override before the subcommand and all `login -p|--password` forms, runs without a shell, and returns Docker's exit status.

Registry profiles live at `${XDG_CONFIG_HOME:-~/.config}/palimpsest/registries.toml`. For an unqualified reference, an explicit registry in the reference wins, followed by `--registry`, `PALIMPSEST_REGISTRY`, and the configured default. The immutable built-in `docker` profile resolves through `docker.io` and adds `library` to one-component repository names.

Profile mirrors, CA files, plain-HTTP, and TLS-skip settings are inputs to `palimpsest registry buildkit-config`; they are not applied automatically to Docker Engine or an existing BuildKit daemon. The generated `buildkitd.toml` must be supplied to an explicitly configured builder. It does not alter Docker Engine/Desktop's pull/push trust store, insecure-registry list, or daemon mirrors. See [Docker/OCI Registry Profiles](registries.md).

---

## 3. Resumable Transfer Protocols

### Resumable Range Downloads (`pull_blob`)
- Downloads write to `<destination>.part` accompanied by an owner-only JSON sidecar tracking target digest, URL, and expected size.
- If interrupted, subsequent pull attempts issue `Range: bytes=<part_size>-`.
- Upon receiving `HTTP 206 Partial Content`, bytes are appended to `<destination>.part`.
- If the server answers `HTTP 200 OK`, the client truncates the partial file and restarts the transfer from byte zero.
- Complete downloads are verified against the declared SHA-256 digest before atomic promotion into `<state>/store/blobs/sha256/<hex>`.

### Crash-Safe Resumable Uploads (`push_blob`)
- When pushing a blob, `HubClient` checks Hub upload short-circuit APIs (`already_present` or `registered`). Existing verified blobs bypass payload transfer only when the returned canonical descriptor is compatible; a conflicting name/kind/media/chain/base/architecture fails explicitly.
- For active uploads, transfer progress is recorded in `<state>/transfers/<digest_hex>.json` storing `{session_id, declared_digest, acknowledged_offset, path_fingerprint}`.
- If interrupted, the client queries `GET /v1/uploads/{session_id}` to retrieve server-acknowledged `received_bytes`, seeks to that exact offset, and resumes streaming via `PATCH` with `Upload-Offset: <offset>`.
- **Hub Fallback:** If the remote Hub returns `HTTP 404` or `HTTP 405` for the upload offset query (indicating an older Hub without offset resumption), `palimpsest-local` deletes the local checkpoint and creates a fresh upload session. It never blindly replays chunks against an unverified offset.

---

## 4. Storage & Runtime Invariants

### Immutable Base Disks & Qcow2 Overlays
```text
[ Immutable Base Image: blobs/sha256/<hex> ]  <──  qemu-img backing link
                       ▲
                       │
[ Per-Run Writable Overlay: overlay.qcow2 ]  ──> Attached as RW vda
```
- Content-addressed base images (`qcow2`/`raw`) are stored read-only (`0444`).
- Base images are **never attached read-write** to any domain.
- All `run` and `build` invocations create a new qcow2 overlay (`overlay.qcow2`) as the sole writable disk (`vda`). This preserves base image digests and prevents state corruption.

### Virtio Disk & Layer Limits
- SquashFS layers are attached as read-only virtio-blk disks (`vdb`..`vdz`).
- Project named storage is attached as a writable raw ext4 block disk; NFS and host binds are not used.
- **Maximum Additional-Disk Limit:** SquashFS layers plus writable project volumes may total at most 25 disks (`MAX_LAYER_DISKS = 25`).
- Serial numbers are derived from the first 20 hex characters of the layer's SHA-256 digest (`virtio-<serial>`).

### Storage Relocation & Lima Disk Invariants
- `palimpsest store move` relocates the Palimpsest state root (`store`, `runs`, `projects`, `builds`, `tags`, etc.) after enforcing that no runs or projects are active.
- **Lima-Managed Disk Notice:** `limactl disk` persistent volumes live under Lima's own storage directory (`~/.lima`) and are **not** moved by `palimpsest store move`.

---

## 5. Unsupported Base-SquashFS Policy

> **Policy Directive:** Official Ubuntu `base.squashfs` rootfs artifacts MUST NOT be used as a boot or lower runtime layer.

- Official Ubuntu cloud-image `base.squashfs` is a rootfs archive, not a boot disk. It lacks `linux-image-*` and `linux-modules-*` kernel packages.
- Placing `base.squashfs` under `/usr` conceals kernel module paths at `/usr/lib/modules/$(uname -r)` and introduces performance penalties from SquashFS compression over root filesystems.
- **Enforced Rule:** VM stacks must boot from a verified bootable `qcow2` or `raw` cloud image base disk. SquashFS layers are reserved exclusively for application deltas, toolchains, and package layers mounted under `/opt/layers/merged`.

---

## 6. Scope & Runtime Limitations

| Feature / Scenario | Supported in v1 | Details / Workaround |
|---|---|---|
| Host Platform: Linux x86_64 | Supported (libvirt/KVM) | KVM acceleration, `q35` machine, `/dev/kvm`, `qemu:///system`, libvirt default network. |
| Host Platform: Linux aarch64 | Supported (libvirt/KVM) | KVM acceleration, `virt` machine + EFI firmware, `/dev/kvm`, `qemu:///system`, libvirt default network. |
| Host Platform: macOS arm64 (Default) | Supported (Lima/VZ) | Apple Silicon default runtime. Uses Lima/VZ backend with persistent `limactl disk` volumes and static TCP port forwarding. |
| Host Platform: macOS arm64 (Experimental) | Supported (`libvirt-hvf`) | Experimental `libvirt-hvf` backend (`--backend libvirt-hvf`). Hypervisor.framework acceleration (`qemu:///session`, `virt` machine + `hdiutil` seed ISO, SLIRP user-mode `hostfwd` networking, no libvirt network driver). |
| Root Overlay / Pivot | **Unsupported** | No rootfs pivot or `overlayroot` modification of `/` or `/usr`. |
| Remote KVM (`qemu+ssh://`) | **Unsupported** | Local `qemu:///system` daemon connection only. |
| Multi-Host Scheduling | **Unsupported** | Single-host local KVM execution only. |
| `palimpsest.yml` projects | **Supported strict subset** | Multi-VM services, dependency-started ordering, environment, typed cloud-init, one network, persistent named block volumes, and lifecycle commands. Unknown Compose fields fail closed. |
| Project storage | **Block only** | KVM raw ext4 `virtio-blk` or Lima standalone disks; one writable attachment per volume. No NFS, bind mount, or shared-writer filesystem. |
| Project port publishing | **Lima TCP only** | Lima uses static `portForwards`. Current KVM/libvirt network interfaces reject `ports`; hidden iptables/nftables DNAT is never installed. |
| Project networking | **One default/external network** | Managed custom networks, multi-NIC, aliases, and service DNS are not implemented. |
| Project cloud-init | **Typed subset** | `packages`, `write_files`, and argv-form `runcmd`; raw MIME/user-data and runtime-owned paths are rejected. |
| OCI `/v2` Registry API | **Supported through external registries** | Docker-compatible commands and Buildx outputs use the installed Docker CLI. Palimpsest Hub itself remains `/v1` only and is not an OCI registry server. |
| Native registry implementation | **Unsupported** | Palimpsest does not yet implement its own `/v2` server or independent OCI registry client/CAS; registry commands depend on Docker. |
| Mutable remote Dockerfile inputs | **Unsupported** | Remote `FROM`, frontend, and external-stage identities must be fully qualified and digest-pinned; profiles do not rewrite Dockerfiles. |

The project schema, lifecycle state, interpolation rules, and backend-specific limitations are specified in [Declarative multi-VM projects](projects.md).

---

## 7. BuildKit Cache and Single-Block Runtime Contract

The Dockerfile/BuildKit interface is experimental and does not replace the v1 `Palimpsestfile` contract until its Linux KVM acceptance gates pass.

- BuildKit cache keys describe reusable solve results. They are not SquashFS blob digests.
- Online builds consult the Hub cache index before executing a miss. Returned content is SHA-256 verified before BuildKit import; Hub errors fail closed rather than falling back to an implicit rebuild. Repeated command/profile `cache-from` and `cache-to` backends are additive and cannot replace mandatory Hub participation.
- `build --push` publishes the OCI output through Buildx. `build --runtime-push` uploads the verified SquashFS runtime block through Hub `/v1`; the flags are not aliases.
- Strict `--offline` builds accept only local contexts, digest-pinned local OCI images, a local runtime base, and local cache. They require an already-bootstrapped local `docker-container` builder whose container network mode is `none`, enforce `--network none` for build steps, do not load Palimpsest registry profiles or invoke registry authentication, and create no Hub or remote-registry client. Docker may still read its selected `DOCKER_CONFIG` to locate that local context and builder. `--registry`, `--pull`, both push flags, and external cache backends are rejected.
- Dockerfile/OCI layers are compacted into one deterministic SquashFS runtime block so VM disk and mount counts do not grow with Dockerfile instruction count.
- Linux KVM already attaches SquashFS artifacts as read-only raw `virtio-blk` disks. There is no active NFS layer-attachment implementation in this repository.
- Lima/VZ copies layer files into the guest and mounts them with `loop,ro`. It is a functional Apple Silicon path, not evidence for Linux KVM block transport or production startup performance.
- Runtime artifact metadata records the BuildKit platform and normalized cloud-image architecture, not a host-specific bus. The per-run state records the actual KVM `virtio-blk` or Lima SCP/loop attachment path.
- The initial runtime block remains mounted below `/opt/layers/merged`; root pivot and complete OCI rootfs semantics remain unsupported.

The complete interface, performance matrix, receipt fields, and acceptance gates are specified in [BuildKit Cache and Block Runtime Workflow](buildkit-block-workflow.md).

---

## KVM Release Gate Notice

> **Mandatory Release Gate:** Package release `v0.1.0` on PyPI and Afterglow dependency cutover are **blocked** until full integration testing succeeds on a physical Linux x86_64 KVM host (`pytest -m kvm`). Pure unit contracts pass on all development hosts, but hardware-assisted virtualization proof remains a non-negotiable release prerequisite.
