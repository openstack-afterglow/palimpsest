# Anonymous TLS OCI registry intake

`palimpsest oci pull` turns one fully qualified anonymously accessible registry reference into
a verified local OCI archive. The separate [automatic `run` resolver](registries.md#automatic-run-acquisition)
can use configured Docker authentication and custom CAs, but still passes only
verified local archive bytes into the runtime. Neither a registry nor Docker's
mutable image store becomes runtime authority.

```sh
palimpsest oci pull ghcr.io/nginx/nginx-unprivileged:stable-alpine \
  --output ./nginx.oci.tar
palimpsest oci pull quay.io/prometheus/busybox:latest \
  --output ./busybox.oci.tar
```

The command requires Skopeo 1.13 or newer on `PATH`. It selects
`linux/amd64` and requests digest-preserving OCI archive output. Tag-based copies
contain the selected platform; digest-pinned references preserve the complete
index (`--multi-arch=all`) so the original root pin remains verifiable before
selecting the amd64 image. It snapshots the graph through the private source CAS
and publishes the archive at mode `0600` only after verification. The JSON
result distinguishes the copied archive's root descriptor from the selected
platform manifest digest.

## Permanent shared layer cache

`oci cache --root PATH` is a separate, explicit persistent acquisition and
conversion path. It does not change `oci pull --output`, Docker authentication,
the automatic `run` resolver, or the native Palimpsest Hub API. In the paired
Torch qualification, upstream bytes came from **Docker Hub**, not Palimpsest
Hub: the original OCI manifest/config/compressed layers were verified and the
selected layers were converted into SquashFS for external-volume consumption.
The original compressed digest, uncompressed DiffID, conversion recipe key and
resulting SquashFS digest are distinct identities, not interchangeable hashes.

Before first initialization, an operator must mount the intended CephFS share
or single-writer RBD/Cinder filesystem, verify that mount independently, and
prepare an empty, physical, service-owned `0700` cache directory. Its ancestors
must be root/operator-owned and protected against replacement by other users;
trusted sticky temporary parents are allowed. The command never mounts,
formats, allocates, attaches, detaches or deletes provider resources. Ordinary
opens refuse missing cache state instead of creating a local fallback after
mount loss. Do not run `init` to repair a missing mount or a damaged catalog.

Run all writers as the same service UID. For example, after the operator has
prepared `/srv/palimpsest-cache/oci` on the intended mounted filesystem:

```sh
palimpsest oci cache --root /srv/palimpsest-cache/oci init
palimpsest oci cache --root /srv/palimpsest-cache/oci pull \
  docker.io/pytorch/pytorch:2.8.0-cuda12.6-cudnn9-runtime
# Use root_digest from the JSON response for subsequent operations.
ROOT_DIGEST='sha256:<reported-root-digest>'
palimpsest oci cache --root /srv/palimpsest-cache/oci materialize "$ROOT_DIGEST"
palimpsest oci cache --root /srv/palimpsest-cache/oci retain "$ROOT_DIGEST" --consumer vm-a
palimpsest oci cache --root /srv/palimpsest-cache/oci retain "$ROOT_DIGEST" --consumer vm-b
palimpsest oci cache --root /srv/palimpsest-cache/oci release --consumer vm-a
palimpsest oci cache --root /srv/palimpsest-cache/oci release --consumer vm-b
palimpsest oci cache --root /srv/palimpsest-cache/oci inspect --manifest "$ROOT_DIGEST"
```

`import LOCAL_ARCHIVE_OR_LAYOUT [--manifest DIGEST]` seeds the same cache from
already acquired bytes. `materialize` requires Linux and the qualified
`mksquashfs` toolchain; it returns the current canonical receipt and actual
ordered `blob_paths`. It does not boot an image or install arbitrary container
packages into a cloud image. The external runtime exporter still consumes an
operator-prepared, immutable runtime store at `/srv/palimpsest`; acquiring this
cache is not automatic Nova/Cinder provisioning or Manila guest support.

Online mutable-tag pulls first refresh bounded anonymous TLS manifest/index
metadata. A verified cached selected graph needs no layer copy. Misses copy the
resolved immutable selected manifest into the existing Skopeo OCI layout, so
shared compressed blobs are reused without another archive download or source
payload copy. Original pinned index metadata is retained separately; only the
selected `linux/amd64` graph is acquired, not every foreign-platform payload.
Known immutable roots and explicit `pull REF --offline` use verified cached
content without registry/tool access. A failed online refresh never silently
falls back to a stale tag. Valid recipe hits skip decompression and packing;
integrity hashing remains required, so reuse is not a zero-I/O operation.

The atomic `state/layer-cache.json` catalog records a stable cache identity,
neutral image/platform/descriptor/DiffID records, reference bindings, derived
recipe/output identities and exact consumer IDs. Original bytes live in
`state/runtime-packs/oci-source-v1/blobs/sha256`; derived SquashFS bytes live in
`state/store/blobs/sha256`. Shared layers have one digest-addressed blob, even
when multiple images or consumers refer to them. Host-bound SourceCAS/store IDs
and occurrence receipts are reconstructed locally, not replayed from another
host. `inspect` can publish current occurrence metadata; it is not a strictly
read-only filesystem operation.

Retention is **permanent**: no TTL, LRU, automatic pruning, last-consumer byte
deletion or storage detach. `release` removes only the exact consumer record;
it is idempotent and cannot remove bytes. Retaining a consumer for another root
requires explicit release first. Generic artifact deletion, including `--force`,
refuses this cache's permanent retention marker. Capacity exhaustion fails
rather than evicting existing content. Corrupt/missing retained content is an
error, not an automatic repair of a possibly consumed inode.
The physical publisher checks retention under its output-digest lock before
recreating missing bytes or replacing corrupt bytes. This also protects a
catalogued output when a different compressed-layer recipe produces the same
SquashFS digest; a valid existing output is reused without replacing its inode.

Whole-cache flock, atomic replacement and file/directory fsync serialize
cooperating writers. CephFS deployments require verified cross-host flock and
storage durability semantics. A writable ext4 filesystem on RBD/Cinder remains
**single-writer**; never attach it writable on multiple clients. Finalized
read-only consumer volumes are separate from the writable acquisition catalog.
The soft per-pull limit counts new destination bytes plus isolated tool scratch,
not the accumulated retained cache. Local CLI and isolated Linux conversion
proofs do not qualify CephFS locking, RBD fencing, power-loss recovery or cloud
deployment; those need separately authorized storage qualification.

## Security and compatibility limits

- The reference must include an explicit registry authority and repository.
  Scheme prefixes, embedded credentials, implicit Docker Hub names, and
  all-tags acquisition are rejected.
- Registry access is anonymous HTTPS with certificate verification forced on
  against system trust. The caller chooses the endpoint: it may resolve to the
  public Internet, a private network, or the local host. Palimpsest applies no
  endpoint allowlist and the command therefore carries the caller's normal
  network reachability.
  Ambient Docker, Podman, Skopeo, and registry credential files are not used.
  Private registry authentication and custom registry CAs are not supported by
  this initial command.
- Only `linux/amd64` is supported. Palimpsest's existing OCI media-type,
  descriptor, platform, process, layer, archive, and source-CAS checks still
  apply, so successful registry transfer alone is insufficient.
- The default deadline is 300 seconds. A polled 8 GiB soft limit covers the
  archive plus Skopeo temporary, cache, configuration, and runtime files in the
  command-owned staging tree; writes can briefly overshoot between polls.
  Descriptor-verified source-CAS copies require additional local storage.
  The deadline covers the external copy, not subsequent archive/CAS
  verification and publication.
  Standard output is discarded; stderr is file-backed and only a bounded diagnostic
  prefix is read on failure. The client runs in a dedicated process group,
  which is terminated on success or failure; as with any local process,
  kernel uninterruptible sleep can delay actual task and resource reclamation.
- The output parent must already exist, be owned by the caller, be free of
  group/world write permission, and contain no symbolic-link traversal. The
  command never replaces an existing output path and cleans up only its own
  private staging directory on failure.

[Skopeo's official copy documentation](https://github.com/containers/skopeo/blob/main/docs/skopeo-copy.1.md)
defines `--preserve-digests` as a fail-if-preservation-is-impossible copy mode.
Its `--src-no-creds` and `--src-tls-verify=true` flags make this command's
anonymous authentication and TLS boundary explicit.

## Verified checkpoint

At `de304ea8da1b6f8323b893e0c9fbd6e2442e668a`, the exact Linux checkout passed
76 focused tests and both commands above using Skopeo 1.13.3. Both archives
passed descriptor/platform/process/source-CAS checks, were published at mode
`0600`, and produced four valid command-journal records. Pre-existing domain
identities/states/XML remained unchanged, with no active VM or QEMU before or
after acquisition. The test used a private Skopeo installation with its
distribution-provided signature policy; TLS verification and anonymous access
remained enabled. This is not publisher-signature proof.

| Registry image | Selected Linux amd64 manifest | Archive bytes |
| --- | --- | ---: |
| `ghcr.io/nginx/nginx-unprivileged:stable-alpine` | `sha256:b8c179cd3c2ae222a873dd59fbae240fadc03836cae5198afc9e9c19919c3880` | 25,555,968 |
| `quay.io/prometheus/busybox:latest` | `sha256:d86ce8f332fdb3b84f73d2fb0953f61bc77374668bac32a15a8a79ec2ed8f0a9` | 1,134,080 |

These are successful real acquisitions, not fresh VM boots of those newly
downloaded archives. Tags may change; preserve the selected digest for
reproducible follow-up runs.

### Separate current-image VM checks

Immediately before the registry-only change, the unchanged guest/runtime at
`cfb80157e66bff3933eba9196b5c0925c2e7c1c8` was checked with existing pinned
Docker Hub archives. A 160-test focused gate passed. Results were:

| Case | Native result | Observed scope |
| --- | --- | --- |
| PostgreSQL 17, default process | Failed | Permission refusal during launch/readiness; no service probe |
| Redis 7 Alpine, default process | Failed, 17.46 s | `setpriv` / permission-refusal markers |
| NGINX stable Alpine, default process | Failed, 20.22 s | `chown` / permission-refusal markers |
| Redis 7 Alpine, explicit `--user redis` | Passed, 27.76 s | `PONG`, root/PID 1 checks and normal stop/remove |
| NGINX unprivileged, original process | Passed, 31.41 s | Detached/exec/root/PID 1 lifecycle proof, not HTTP qualification |

No workload or PID 1 permissions were relaxed. The three new failed domains
were preserved inactive; successful test resources were removed normally.
All original twelve archives and twelve historical domains were preserved,
leaving fifteen inactive domains and no active QEMU. Two private-wrapper
precondition/accounting errors were corrected and audited before continuation;
they do not convert any default-image failure into a pass. PostgreSQL was not
rerun after its failure. These results are separate from the acquisitions above
and do not establish universal container-image compatibility.
