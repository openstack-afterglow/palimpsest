# Docker Hub image-to-VM compatibility

## Scope and current public boundary

The public OCI-root runtime accepts a local uncompressed OCI image-layout
archive or directory, not a Docker Hub reference. `palimpsest pull` is currently
a Docker registry wrapper: it does not populate the OCI-root source store or
launch a VM. A legacy Docker-save archive is not the accepted OCI-layout
transport. Do not rename one and assume the format changed.

The compatibility workflow under test is:

```text
Docker Hub tag → unique linux/amd64 manifest digest
→ digest-preserving OCI archive copy → local public run / run -d
→ actual guest execution → public stop/rm
```

Use a preserving copier such as Skopeo, with the selected image manifest pinned:

```sh
skopeo --override-os linux --override-arch amd64 copy --preserve-digests \
  docker://docker.io/library/hello-world@sha256:SELECTED_AMD64_MANIFEST \
  oci-archive:hello-world.oci.tar
palimpsest run hello-world.oci.tar --name hello --memory 512 --vcpus 1
palimpsest rm hello
```

Replace the illustrative digest with the actual platform-specific digest and
configure the qualified host BOOT inputs and a dedicated runtime first; see
the [public runtime setup](oci-public-runtime-roadmap.md). Skopeo is an external
acquisition tool, not a newly implemented Palimpsest registry client. An
official digest-pinned Skopeo container may perform the copy on a Docker host;
the copied image's workload must still execute in the KVM VM, not in Docker.

The runtime verifies selected descriptors and layer DiffIDs. Preserving the
source manifest also binds the original image config and ordered compressed
layer descriptors. Archive SHA256 is a separate transport identity. None of
this is a publisher signature or a guarantee that arbitrary images are safe.

## Compatibility matrix and separately selected tests

Three independent opt-in checks target an unchanged foreground `hello-world`,
the default Redis Alpine service, and the non-root NGINX Alpine service.
The first proves output and terminal exit. Service proofs require a running
domain, default workload readiness, additional public exec, app root identity
matching authenticated PID 1 root evidence, direct PID 1 access refusal and
normal cleanup. They must preserve source archives and failure evidence.

### 2026-09-08 initial native results (`f606e32`)

All three independently selected checks ran on pushed commit
[`f606e32`](https://github.com/openstack-afterglow/palimpsest/commit/f606e32f497d835ffa04cf03655e920a22fa7917)
on the Linux x86_64 KVM host, using 512 MiB and one vCPU per requested VM.

| Original Docker Hub image | Result | Observed boundary |
| --- | --- | --- |
| `library/hello-world:latest` | **PASS**, 13.56 s | Default `/hello` executed in the VM, emitted `Hello from Docker!`, exited zero, and public `rm` removed the domain/run state. |
| `library/redis:7-alpine` | **FAIL**, 3.93 s | `oci-worker-pack` during layer conversion, before VM creation. Four layer occurrence receipts completed; the next layer contains only the `data/` directory. Exact packer cause requires a separate bounded diagnostic. |
| `nginxinc/nginx-unprivileged:stable-alpine` | **FAIL**, 1.03 s | The Linux image's `ArgsEscaped: true` is rejected by the existing process validator, before layer conversion/VM creation. |

The failures remain failures, not skips or expected-pass exclusions. No service
readiness, service exec/root-proof or service cleanup success is claimed from
them. Redis did not reach its entrypoint, so its observed failure is **not**
evidence of a guest capability or worker resource-limit problem. NGINX's
configured user is `101`, but its workload was never launched.

A proposed standalone Redis diagnostic was **not executed**: independent
review still rejected its interruption/descendant-cleanup guarantees and an
observation path that could replace the production error. Its rejected review
evidence is preserved separately from the native results. The precise packer
error remained unresolved at this checkpoint; zero-fragment superblock handling was only a
code-inspection hypothesis, not an observed diagnostic result or a fix.

These are new real-image compatibility checks, not a new image build or full
Gate 2. Existing local-build Gate 2 evidence remains separate. Initial portable
checks passed 117 cases locally and on the server; a fresh-runtime test setup
defect then failed the first hello attempt before any VM was created. The
proof-only correction isolated its source cache from lazily created product
state. Final focused checks passed 118 cases locally (2.67 s) and at the exact
server SHA (4.14 s); the three native checks skip unless individually opted in.
These overlapping runs are not an aggregate full-suite result.

### Immutable source identities

The tags were resolved once to the following unique `linux/amd64` manifests.
Copies used `--preserve-digests`; original archive hashes matched after each
native attempt. Both failed runtimes and their evidence remain preserved.

| Image | Selected manifest SHA256 | OCI archive SHA256 |
| --- | --- | --- |
| hello-world | `d1a8d0a4eeb63aff09f5f34d4d80505e0ba81905f36158cc3970d8e07179e59e` | `7e9f82b7d5203a3e0da8889915caeaed7b5e70cc3996de9c4cb471bdf3fa5aac` |
| Redis | `1db42ccef14898aa29bae778452d567534b59c107129cbc1163fb552de184d3c` | `90159972e37b9f02aed6b699361b7ab633d77323a056d811dc6684b8d06857d2` |
| NGINX | `b8c179cd3c2ae222a873dd59fbae240fadc03836cae5198afc9e9c19919c3880` | `64e3f9852293174971bbd81e54bf0093896fc0ee525c9b0494763d5208ffa029` |

All image Entrypoint/Cmd, configured user and environment remain unchanged.
There are no process overrides, baked probes or injected image files. VM
networking remains `none`; service readiness is not external port reachability.
The capabilityless workload policy may reject image entrypoints that perform
privileged operations such as switching user or changing ownership. Such a
failure must be reported, not hidden by weakening PID 1 protection or granting
privileges.

### 2026-09-08 zero-fragment follow-up

The in-repository, individually selected real-packer regressions at pushed
`d255fd2` reproduced the fragment-accounting rejection for directory-only and
empty layers (0.57 s and 0.37 s). The previously rejected standalone diagnostic
was not executed. These small production-packer component tests are not
worker-isolation or VM qualification; their limits are in [testing](testing.md).

At pushed `c95d948`, the host structural verifier v3 accepts an already-bounded
finite fragment-table offset for zero fragments, without changing the other
structural checks. Local focused checks passed 560 with seven Linux skips;
the same server selection passed 567 (169.94 s). Three real-packer cases,
including deterministic output, passed (0.71 s). These overlapping results
are not a full-suite aggregate.

The unchanged original Redis proof then passed layer conversion but failed
after VM boot (75.98 s). Its console reported stage-1 filesystem contract
rejection before mounting, not workload readiness. Read-only examination of
the seven generated lowers found the portable guest verifier rejected exactly
the two zero-fragment images, with finite offsets 188 and 136; the other five
passed. Both the portable guest verifier and C stage-1 verifier still contained
the old predicate. No Redis entrypoint/capability failure is established by
this attempt. The failed runtime and source archive remain preserved; no
domain remained in the read-only post-failure inventory.

Separately, the existing immutable Palimpsest-built image passed the cold
public run/exec/stop/rm proof at the same SHA (one test, 21.92 s), including
literal argv, separate streams, exit status and VM lifecycle. This is neither
a new image build nor full Gate 2. Guest verifier parity and a fresh original
Redis rerun remain separate follow-up work at this checkpoint.

## Next public intake contract

First align host, portable guest and C stage-1 structural validation for valid
zero-fragment SquashFS, preserving all other checks and PID 1/workload policy,
then rerun the unchanged original Redis proof. Do not execute the previously
rejected standalone diagnostic. Separately review Linux handling of the
legacy `ArgsEscaped` field against the OCI/Moby contract before changing the
existing fail-closed parser. Neither change is justified by silently editing
the downloaded image or bypassing its validator.

Before enabling a registry reference directly in `run`, define an explicit
OCI-reference grammar that cannot fall into the existing cloud-image path,
tag-to-platform-digest resolution, acquisition/tool/authentication limits,
private crash-safe storage and retry, and provenance binding into the existing
local source validator. Registry acquisition must remain separate from guest
networking and must never fall back to running the application in Docker.
Do not infer broad image compatibility from one successful example. Keep
root-disk deletion and multi-VM data-volume sharing as separate follow-ups.

## Upstream references

- [Skopeo copy and preserve-digests](https://github.com/containers/skopeo/blob/main/docs/skopeo-copy.1.md)
- [Official Skopeo container distribution](https://github.com/containers/skopeo/blob/main/install.md#container-images)
- [Docker image-save transport](https://docs.docker.com/reference/cli/docker/image/save/)
- [OCI image config and legacy ArgsEscaped](https://github.com/opencontainers/image-spec/blob/main/config.md)
- [SquashFS 4.6.1 table writer](https://github.com/plougher/squashfs-tools/blob/4.6.1/squashfs-tools/mksquashfs.c)
- [Linux 6.6 SquashFS superblock reader](https://github.com/torvalds/linux/blob/v6.6/fs/squashfs/super.c)
