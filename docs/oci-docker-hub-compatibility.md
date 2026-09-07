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

Qualification results are pending the exact pushed server SHA. These are new
real-image compatibility checks, not a new image build or full Gate 2. Existing
local-build Gate 2 evidence remains separate.

All image Entrypoint/Cmd, configured user and environment remain unchanged.
There are no process overrides, baked probes or injected image files. VM
networking remains `none`; service readiness is not external port reachability.
The capabilityless workload policy may reject image entrypoints that perform
privileged operations such as switching user or changing ownership. Such a
failure must be reported, not hidden by weakening PID 1 protection or granting
privileges.

## Next public intake contract

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
