# Docker Hub intake analysis at `3aafb7a`

## Result

At exact local and Linux host commit
`3aafb7a6881c2a58594554b51b714fb9fc4d7d73`, Palimpsest does not accept a
Docker Hub reference as an OCI-root run source. The verified current path is an
external registry acquisition followed by Palimpsest's local OCI boundary:

```text
Docker Hub tag/index
  -> externally select a linux/amd64 manifest
  -> externally copy a digest-pinned OCI archive
  -> Palimpsest descriptor/CAS snapshot
  -> Palimpsest hard-worker SquashFS materialization
  -> optional, separately qualified VM preparation and boot
```

A fresh `hello-world` archive passed external acquisition, local intake, and
cold materialization on the Linux host. No VM was prepared or booted in this check.
Consequently this result is not a workload-execution, lifecycle, KVM, or Gate 2
result.

| Stage | Current implementation | Evidence at this commit | Limit |
| --- | --- | --- | --- |
| Live registry fetch | External only. The CLI `pull` route delegates to Docker and leaves the result in Docker's store. | Existing Docker 29.5 and the existing `quay.io/skopeo/stable:latest` image resolved and copied the public image successfully. | Palimpsest did not authenticate to Docker Hub, resolve the tag, or populate its source CAS in this stage. |
| Local secure source import | Implemented by `LocalArchiveSource` / `LocalLayoutSource` and `SourceCAS`. | The new OCI archive was selected by its platform manifest digest, all selected descriptors were copied into a private CAS, and the image's bootable process contract was accepted. | The accepted input was a local OCI archive. A registry reference and a Docker-save archive are not accepted substitutes. |
| Hard-worker conversion | Implemented by `materialize_image_hard` and the OCI store. | A new dedicated runtime produced a cold-miss SquashFS result and a v2 materialization receipt with one verified layer. | This proves conversion, not root-volume preparation or guest filesystem behavior. |
| VM boot and workload | Implemented only through the separate local OCI-root KVM path. | Not run in this analysis. The before/after libvirt inventory was identical and contained no active domains. | No claim is made for `/hello` output, exit status, authenticated guest root, PID 1 protection, stop/rm, or cleanup. |

## Source boundary

The current CLI routes `pull`, `save`, `load`, and related commands through the
Docker CLI adapter in `src/palimpsest_local/cli.py`. That route is operationally
separate from OCI-root intake: it does not create a `SourceCAS` snapshot or an
OCI materialization receipt.

OCI-root request resolution in `src/palimpsest_local/oci_run_request.py`
requires an existing local file or directory. `src/palimpsest_local/oci_source.py`
then validates an OCI layout/archive, root selection, descriptor sizes and
digests, config, layer graph, and private source-CAS identity. The hard worker
in `src/palimpsest_local/oci_materializer.py` consumes that authenticated local
snapshot. There is no current registry-reference adapter joining these two
boundaries.

The separate Palimpsest Hub is also not that adapter. Its API is the native
artifact `/v1` service described in `ARCHITECTURE.md`, not an OCI Distribution
`/v2` registry endpoint.

## Fresh Linux-host evidence (2026-09-10)

The authorized host checkout was clean and its exact SHA matched the local
checkout. Before acquisition, `virsh -c qemu:///system list --all` showed four
preserved, inactive domains and no active VM:

```text
exec-cli
exec-cli-e16c31ba
hub-nginx-a6ccaf95
hub-redis-b17784be
```

They were not stopped, modified, or deleted. The same inventory was present
after materialization.

An owner-private scratch directory was created as
`/tmp/palimpsest-hub-intake.vGGU1uty`. Raw inspection of
`docker.io/library/hello-world:latest` distinguished the multi-platform index
digest from its unique `linux/amd64` manifest:

| Identity | SHA-256 |
| --- | --- |
| Current tag index | `5e23090353324d887c48ad5e5c56d294eab81588df9605b07d1afe895f9cc8f8` |
| Selected `linux/amd64` manifest | `d1a8d0a4eeb63aff09f5f34d4d80505e0ba81905f36158cc3970d8e07179e59e` |
| Selected config | `e2ac70e7319a02c5a477f5825259bd118b94e8b02c279c67afa63adab6d8685b` |
| Selected compressed layer | `4f55086f7dd096d48b0e49be066971a8ed996521c2e190aa21b2435a847198b4` |
| OCI archive transport | `7fa3e4b7e1b1b4b2f06da0c361e157b723f1849b269e7821827b51b759b80242` |

The preserving acquisition was equivalent to:

```sh
docker run --rm --user "$(id -u):$(id -g)" \
  -v /tmp/palimpsest-hub-intake.vGGU1uty:/work:rw \
  quay.io/skopeo/stable:latest copy \
  --override-os linux --override-arch amd64 --preserve-digests \
  docker://docker.io/library/hello-world@sha256:5e23090353324d887c48ad5e5c56d294eab81588df9605b07d1afe895f9cc8f8 \
  oci-archive:/work/hello-world-user.oci.tar
```

The selected archive was 10,240 bytes, mode `0600`, owned by UID/GID 1000. A
first copy without `--user` also completed but produced a root-owned mode-0644
archive that the host user could not chmod. That file and failure were
preserved; the owner-controlled copy above is the artifact used by Palimpsest.

Passing the tag-index digest as the local root pin was correctly rejected with
`OCI layout must declare the pinned root descriptor exactly once`: the copied
single-platform archive declares the selected manifest, not the remote index.
Using the actual `linux/amd64` manifest pin succeeded. The source snapshot
accepted process `argv=["/hello"]`, working directory `/`, user/group `0:0`,
and produced source-CAS binding
`sha256:9d60818537f08a3ffe26ab750f96e9389e2286872011f224899200f1e7db01c7`.

Hard materialization used the repository at the same SHA, Python
`/tmp/palimpsest-30y-venv.B5P9EO/bin/python`, a new runtime at
`/tmp/palimpsest-hub-materialize.b63a8ca8`, `/usr/bin/mksquashfs` 4.6.1, and a
120-second bound:

```sh
PALIMPSEST_STATE_HOME=/tmp/palimpsest-hub-materialize.b63a8ca8/state \
XDG_CONFIG_HOME=/tmp/palimpsest-hub-materialize.b63a8ca8/config \
PYTHONPATH=src /tmp/palimpsest-30y-venv.B5P9EO/bin/python \
  -m palimpsest_local.cli oci materialize \
  /tmp/palimpsest-hub-intake.vGGU1uty/hello-world-user.oci.tar \
  --manifest sha256:d1a8d0a4eeb63aff09f5f34d4d80505e0ba81905f36158cc3970d8e07179e59e \
  --packer /usr/bin/mksquashfs --timeout 120
```

The result was a cold miss, one 4,096-byte SquashFS image, DiffID
`sha256:897b3f2a7c1bc2f3d02432f7892fe31c6272c521ad4d70257df624504a3238b4`,
and materialization receipt
`sha256:67a3266372565096e645e9a980a6fc686d79aa8cccd512cfc4e305ac5f8843bb`.
The receipt file hash was
`c422a87fd4dd7850d70c52138245f22eeac3e9aa2ea467b9851c4aca7208524c`.
The source archive hash and ownership still matched after conversion. The CLI
also warned that the optional host journal was unavailable; conversion
continued and the receipt was produced, so this run does not prove host-journal
installation or recording.

## Compatibility implications and next bounded stage

This fresh result verifies that the external acquisition/local-intake seam is
usable for a small, unchanged Docker Hub image. It does not broaden the runtime
contract to arbitrary Docker images. The current runtime still intentionally
has no network and no facility to add capabilities or rewrite image defaults.

The shortest next proof is a fresh, uniquely named `hello-world` public KVM run
using this exact archive and manifest pin, followed by source-hash and domain
inventory checks. It should be a separate explicit opt-in native lifecycle
proof, not folded into acquisition or conversion.

For service compatibility, the next work should remain image-specific and
fail-closed:

1. NGINX: verify a narrowly specified guest `/dev/stdout` and `/dev/stderr`
   alias contract with dedicated negative controls before changing stage-1.
   Current evidence says the image's log symlinks target those absent aliases;
   it does not yet prove that adding them is sufficient for readiness.
2. Redis default user: retain the capabilityless policy and diagnose the
   entrypoint's `setpriv` capability-retention request as a compatibility
   decision. The already demonstrated explicit `--user redis` path is a
   separate user override, not proof that the unchanged default succeeds.
3. A registry bridge, if desired later, should terminate at the existing local
   snapshot boundary with an explicit platform manifest and transport receipt.
   It should not make Docker's mutable store authoritative or bypass
   descriptor, CAS, DiffID, worker, or VM admission checks.

Historical Redis, NGINX, and earlier `hello-world` native outcomes remain in
`docs/oci-docker-hub-compatibility.md`. They are qualification history, not
evidence that this newly fetched archive booted at `3aafb7a`.
