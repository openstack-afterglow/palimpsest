# Palimpsest Local Compatibility & Integration Contract

`palimpsest-local` provides an independently versioned Python library and CLI (`palimpsest`) that integrates with Afterglow Hub while enforcing strict artifact verification and local KVM runtime invariants.

---

## 1. Afterglow Hub API Contract

`palimpsest-local` communicates with Afterglow Hub via a Bearer-authenticated REST API.

- **API Base Prefix:** `/api/v1/palimpsest/hub`
- **Authentication Header:** `Authorization: Bearer <token>`
- **Content Media Types:**
  - `application/vnd.afterglow.palimpsest.layer.squashfs.v1` (SquashFS layer)
  - `application/vnd.afterglow.palimpsest.layer.config.v1+json` (Layer configuration metadata)
  - `application/vnd.afterglow.palimpsest.image.qcow2.v1` (Bootable qcow2 cloud image)
  - `application/vnd.afterglow.palimpsest.image.raw.v1` (Bootable raw cloud image)

### Supported Hub Endpoints

| Method | Route | Description |
|---|---|---|
| `GET` | `/api/v1/palimpsest/hub/images` | Query boot images (`ubuntu_base`, `arch`, `os_variant`, `disk_format`, `limit`) |
| `GET` | `/api/v1/palimpsest/hub/layers` | Query layers (`name`, `kind`, `parent_digest`, `limit`) |
| `GET` | `/api/v1/palimpsest/hub/layers/{digest}` | Retrieve layer metadata record |
| `GET` | `/api/v1/palimpsest/hub/layers/{digest}/ancestors` | Retrieve ordered parent chain digests |
| `GET` | `/api/v1/palimpsest/hub/layers/{digest}/blob` | Download image or layer blob (supports HTTP `Range`) |
| `POST` | `/api/v1/palimpsest/hub/uploads` | Initiate resumable upload session |
| `GET` | `/api/v1/palimpsest/hub/uploads/{session_id}` | Query current upload offset & session state |
| `PATCH` | `/api/v1/palimpsest/hub/uploads/{session_id}` | Append payload chunk (accepts optional `Upload-Offset`) |
| `PUT` | `/api/v1/palimpsest/hub/uploads/{session_id}` | Complete upload session with layer metadata |
| `DELETE` | `/api/v1/palimpsest/hub/uploads/{session_id}` | Cancel active upload session |
| `POST` | `/api/v1/palimpsest/hub/bundles` | Generate bundle tarball for stack (`refs`, `include_base_image`) |

---

## 2. Resumable Transfer Protocols

### Resumable Range Downloads (`pull_blob`)
- Downloads write to `<destination>.part` accompanied by an owner-only JSON sidecar tracking target digest, URL, and expected size.
- If interrupted, subsequent pull attempts issue `Range: bytes=<part_size>-`.
- Upon receiving `HTTP 206 Partial Content`, bytes are appended to `<destination>.part`.
- If the server answers `HTTP 200 OK`, the client truncates the partial file and restarts the transfer from byte zero.
- Complete downloads are verified against the declared SHA-256 digest before atomic promotion into `<state>/store/blobs/sha256/<hex>`.

### Crash-Safe Resumable Uploads (`push_blob`)
- When pushing a blob, `HubClient` checks Hub upload short-circuit APIs (`already_present` or `registered`). Existing verified blobs bypass payload transfer.
- For active uploads, transfer progress is recorded in `<state>/transfers/<digest_hex>.json` storing `{session_id, declared_digest, acknowledged_offset, path_fingerprint}`.
- If interrupted, the client queries `GET /api/v1/palimpsest/hub/uploads/{session_id}` to retrieve server-acknowledged `received_bytes`, seeks to that exact offset, and resumes streaming via `PATCH` with `Upload-Offset: <offset>`.
- **Hub Fallback:** If the remote Hub returns `HTTP 404` or `HTTP 405` for the upload offset query (indicating an older Hub without offset resumption), `palimpsest-local` deletes the local checkpoint and creates a fresh upload session. It never blindly replays chunks against an unverified offset.

---

## 3. Storage & Runtime Invariants

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
- **Maximum Layer Limit:** Up to 25 SquashFS layers per VM (`MAX_LAYER_DISKS = 25`).
- Serial numbers are derived from the first 20 hex characters of the layer's SHA-256 digest (`virtio-<serial>`).

---

## 4. Unsupported Base-SquashFS Policy

> **Policy Directive:** Official Ubuntu `base.squashfs` rootfs artifacts MUST NOT be used as a boot or lower runtime layer.

- Official Ubuntu cloud-image `base.squashfs` is a rootfs archive, not a boot disk. It lacks `linux-image-*` and `linux-modules-*` kernel packages.
- Placing `base.squashfs` under `/usr` conceals kernel module paths at `/usr/lib/modules/$(uname -r)` and introduces performance penalties from SquashFS compression over root filesystems.
- **Enforced Rule:** VM stacks must boot from a verified bootable `qcow2` or `raw` cloud image base disk. SquashFS layers are reserved exclusively for application deltas, toolchains, and package layers mounted under `/opt/layers/merged`.

---

## 5. Scope & Runtime Limitations

| Feature / Scenario | Supported in v1 | Details / Workaround |
|---|---|---|
| Host Platform | Linux x86_64 | Requires `/dev/kvm` and `qemu:///system`. Darwin/ARM hosts support pure artifact CLI only. |
| Guest OS / User | Ubuntu NoCloud (`ubuntu`) | Cloud-init configures `ubuntu` user with generated Ed25519 SSH keys. |
| Layer Mount Path | `/opt/layers/merged` | Layers mount at `/opt/layers/lowerN` and combine into `/opt/layers/merged`. |
| Root Overlay / Pivot | **Unsupported** | No rootfs pivot or `overlayroot` modification of `/` or `/usr`. |
| Remote KVM (`qemu+ssh://`) | **Unsupported** | Local `qemu:///system` daemon connection only. |
| Multi-Host Scheduling | **Unsupported** | Single-host local KVM execution only. |
| OCI `/v2` Registry API | **Unsupported** | Integrates via Afterglow Hub REST protocol (`/api/v1/palimpsest/hub`). |

---

## KVM Release Gate Notice

> **Mandatory Release Gate:** Package release `v0.1.0` on PyPI and Afterglow dependency cutover are **blocked** until full integration testing succeeds on a physical Linux x86_64 KVM host (`pytest -m kvm`). Pure unit contracts pass on all development hosts, but hardware-assisted virtualization proof remains a non-negotiable release prerequisite.
