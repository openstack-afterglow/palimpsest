# Palimpsest Local Compatibility & Integration Contract

`palimpsest-local` provides an independently versioned Python library and CLI (`palimpsest`) that integrates with Afterglow Hub while enforcing strict artifact verification and local KVM runtime invariants.

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
| `GET` | `/v1/layers` | Query layers (`name`, `kind`, `parent_digest`, `limit`) |
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
- If interrupted, the client queries `GET /v1/uploads/{session_id}` to retrieve server-acknowledged `received_bytes`, seeks to that exact offset, and resumes streaming via `PATCH` with `Upload-Offset: <offset>`.
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
| OCI `/v2` Registry API | **Unsupported** | Integrates via Palimpsest Hub REST protocol (`/v1`). |

---

## KVM Release Gate Notice

> **Mandatory Release Gate:** Package release `v0.1.0` on PyPI and Afterglow dependency cutover are **blocked** until full integration testing succeeds on a physical Linux x86_64 KVM host (`pytest -m kvm`). Pure unit contracts pass on all development hosts, but hardware-assisted virtualization proof remains a non-negotiable release prerequisite.
