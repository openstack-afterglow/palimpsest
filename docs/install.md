# Detailed installation and configuration guide

This guide covers end-user installation from PyPI or a GitHub VCS URL,
package selection, supported host/runtime combinations, external dependencies,
initial configuration, upgrades, and administrator-owned Linux deployment.
It does not use a repository checkout as the user installation mechanism.

Palimpsest Local requires Python 3.11 or newer. The root distribution
[`palimpsest-client 0.3.1`](https://pypi.org/project/palimpsest-client/0.3.1/)
is published on PyPI. The independently versioned Hub distribution is
`palimpsest-hub 0.3.1` in this tree; this source version is not a PyPI release.
Git and outbound HTTPS access to GitHub
are required only for the direct VCS installation examples below.

Install the published CLI with `python3.12 -m pip install "palimpsest-client==0.3.1"`,
then run `palimpsest --version`. The isolated invocation
`uvx --from palimpsest-client==0.3.1 palimpsest --version` was exercised against
PyPI and reports `0.3.1`. The formal [GitHub Release](https://github.com/openstack-afterglow/palimpsest/releases/tag/v0.3.1)
contains the same hash-matched wheel and sdist as the exact tag run and PyPI.

The Hub 0.3.1 package registry and retained closed-transport pool recovery are
separate service code; update both reviewed Kolla API and worker image digests
to deploy it. A root wheel upgrade does not update running services.

The immutable release source is `1458db42a1449a25b664584d144d0a97086f8f6f`.
Root/Hub/locks/modules/Kolla image tag are synchronized at **0.3.1**. Published
API/worker revisions match this source, and `0.3.1`, `v0.3.1`, `sha-1458db4`
and `latest` resolve to the same respective indexes:

- API: `ghcr.io/openstack-afterglow/palimpsest-hub-api@sha256:4ee1a42d14f4b0193877d1cd7d4f55187a987628097ec424eda2b0b7812d929a`
- Worker: `ghcr.io/openstack-afterglow/palimpsest-hub-worker@sha256:4564a89553132366f38fd23be2ee101262a4f99a9962afdae37a0975511814a6`

Both published images are **Linux amd64 only**. No arm64 publication or literal
`stable` alias exists in the observed release; `latest` is not a deployment pin.
The Dockerfile supports prior amd64/arm64 source builds, which do not establish
an arm64 release manifest. Runtime smoke executed the published amd64 digests
under cross-architecture Docker Desktop on arm64, not on a native amd64/KVM host.
See [qualification boundaries](testing.md#published-031-verification).

**Production rollout is held**, not completed. Before Kolla cutover, the operator must:

1. Preserve the previous **actual** API/worker digest pins, existing private
   Kolla secret/config inputs, mounts and service identities. Both images must
   come from the exact reviewed release ref. Source mode still defaults to
   `c4887f7806608e98f215abbd377d2eafe159ff76`, not this release; override it
   only with the separately reviewed `1458db42a1449a25b664584d144d0a97086f8f6f`. The source-build task rejects
   a dirty existing checkout or a different existing HEAD; prepare a separate
   clean checkout path rather than resetting an operator's checkout.
2. Stop new ingress and quiesce **all** API/export/build/upload/GC writers, then
   capture SQL and CAS from the same no-writers interval. Keep backup/restore
   evidence and verify an isolated restore before production cutover. Do not
   delete volumes, retained jobs, unknown guest state or Keystone Trusts.
3. Use the reviewed release API image's `palimpsest-hub-bootstrap` to create missing
   tables. It is additive `create_all`, not an ALTER migration. For a separate
   database, bootstrap an empty destination, run
   `palimpsest-hub-migrate-data --source-url "$SOURCE_DATABASE_URL" \
   --destination-url "$DESTINATION_DATABASE_URL" --dry-run`, then the same
   command without `--dry-run`, with credentials supplied privately and output
   restricted. Migration rejects a nonempty destination and incomplete native
   package schema; it copies all package tables, exports/delegations, builds,
   layers/grants/uploads, skipping optional legacy tables only when absent.
   Review private URI handling because this legacy CLI accepts URLs on argv;
   do not put a real password in shell history or diagnostic logs.
4. API, export worker and native build worker must use the same SQL and exact
   absolute store path with a coherent shared filesystem/locking view. Default
   Docker named volumes are host-local: multi-host API/export placements do
   **not** become shared CAS merely because their SQL/volume names match.
   Supply reviewed shared storage or select one storage-owning placement before
   routing traffic. Preserve numeric owners/modes; bootstrap changes only each
   volume root, not recursive blobs. Plan capacity for temporary double-sized
   finalization, bundle expansion and private worker scratch.
5. Supply trustee service-project credentials, the separate system-reader-only
   validator, nonempty exact protected user/project ID sets, current role graph,
   immutable namespace bindings and the trusted package HTTPS origin. Confirm
   actual Keystone Trust create/authenticate/impersonating-delete policy and
   finite least-role delegation (default member, 21600 seconds) with a real
   requester. Never add tenant grants or fall back to service tenant scoping.
   Pre-cutover undelegated queued exports fail `delegation_required`; requesters
   resubmit them. Existing manual tenant grants require a separate owner decision.
6. Align the separately owned Afterglow Kolla overlay and package-key gateway,
   then use its normal precheck/reconfigure path. Keep API/export worker versions
   together; the Kolla role does not deploy the native build worker. Verify real
   SQL/Redis/auth/CLI bytes, worker cleanup, restart persistence and restored
   reads/writes before reopening ingress. Health 200 proves only liveness.

### Service-role provisioning prerequisite before reopening ingress

The 0.3.1 release gates every legacy artifact write and
`POST /v1/image-exports` on current `palimpsest-publish_editor`; the export
worker rechecks it before Glance I/O. Plain existing `member`/`reader` grants
are not service authority. Without an approved role-graph/assignment cutover,
existing export consumers (including Afterglow requesters) return 403. The
Palimpsest Kolla role registers the trustee and system reader only: it does
**not** create these service roles, inference rules or requester grants.

During this release's approved preset-only preparation, global roles and their
implication edges were created and verified, but no user/project assignments
were made. Read-only inventory found 106 ordinary user/project memberships
without service-grade assignments across 43 users/41 projects (104 enabled).
These rows describe cutover risk, not 106 exercised denied requests. Review
the intended service grade for each requester before reopening ingress; never
promote every `member` or grant tenant admin automatically.

Before reopening ingress, the IAM/operator owner must explicitly approve and
provision unique **global** roles with these exact names, then verify their
immutable IDs and actual acyclic Keystone inference graph:

- Leaves: `palimpsest-inventory_reader`, `palimpsest-download_user`,
  `palimpsest-publish_editor`, `palimpsest-keys_editor`,
  `palimpsest-keys_admin`.
- Documented parent grades: `palimpsest_reader`, `palimpsest_user`,
  `palimpsest_editor`, `palimpsest_admin`.
- Preset grade edges: `palimpsest_admin` → `palimpsest_editor` →
  `palimpsest_user` → `palimpsest_reader`.
- Preset grade-to-leaf edges: reader → inventory_reader; user → download_user;
  editor → publish_editor and keys_editor; admin → keys_admin, using the full
  exact names above.
- Each non-reader leaf also implies `palimpsest_reader` through an actual
  Keystone inference rule, supplying inventory without inventing download or
  key-management authority. Role inference must not grant base membership.

Assign only the owner-approved grade or independent leaves to the intended
requesting users/groups in each exact project, retaining separate baseline
`member` (or inventory-only `reader`) authority. Existing export requesters need
current publish capability and the delegated member role; artifact downloads
separately need download capability. Do not grant every leaf to every tenant,
assign the trustee to tenant projects, weaken the Hub gate or silently remove
existing grants. If operators choose granular leaves or a different graph, it
must still provide exactly the approved effective capabilities; the Hub never
hardcodes parent expansion. Obtain a renewed original subject token after any
assignment upgrade not represented in that token.

The system reader must be allowed to read the complete current roles,
role-ID inference rules and effective assignments, including domain-role
records referenced by the inference listing. Confirm current original-token
and key-owner intersections, a real authorized export and denied plain-member
write before reopening ingress. Record exact role/edge/assignment receipts and
live Trust delete proof privately. This is an approval-requiring operator step,
not an IAM mutation performed by source preparation.


## Package catalog

| Distribution | Install selector | Entrypoints | Purpose |
| --- | --- | --- | --- |
| `palimpsest-client` | repository root | `palimpsest` | Local artifact, build, registry, VM, Compose, store, and UI CLI |
| `palimpsest-client[kvm]` | repository root plus `kvm` extra | `palimpsest` | Local CLI plus `libvirt-python>=10.0.0` for libvirt backends |
| `palimpsest-hub` | `#subdirectory=hub` | `palimpsest-hub`, `palimpsest-hub-worker`, `palimpsest-hub-build-worker`, `palimpsest-hub-bootstrap`, `palimpsest-hub-migrate-data` | Standalone Hub API and web console, separate export/build workers, schema bootstrap, migration |

The distribution rename does not change the Python module `palimpsest_local`
or the `palimpsest` CLI.

The base Local distribution has no required Python runtime dependencies. The
Hub distribution is separate and installs FastAPI, Uvicorn, SQLAlchemy/asyncmy,
Redis, OpenStack SDK/Keystone, Pydantic, multipart, and rate-limit dependencies.
Use the same reviewed Git ref for Local and Hub to prevent source skew.

The root wheel also installs the `palimpsest` Kolla-Ansible role as shared data
under `share/kolla-ansible/ansible/roles/palimpsest`. It does not install
Kolla-Ansible, Ansible, Hub, or their runtime dependencies; deployments pin
Kolla-Ansible independently. The released role defaults to Hub image tag `0.3.1`,
while source builds bind to the configured reviewed checkout commit SHA. Both
published image digests above must be inspected and pinned for the deployment
host; source metadata alone does not establish runtime readiness.

The role's release matches the Hub 0.3.1 source. Operators deploying
the package registry or closed-connection fix must verify reviewed API/worker
image digests in Kolla globals; a Python update does not change running containers.

On deploy and upgrade, each Palimpsest host pulls the API image and sets the
root of its own named Hub volume to the image's runtime user before containers
start. Database/bootstrap initialization remains a single operation on the
first host; a local Docker volume is not shared across hosts. Existing blob
subdirectories are not recursively changed.

For an operator-owned public Hub URL, configure the HTTPS origin and explicitly
enable public HAProxy exposure:

```yaml
palimpsest_public_endpoint_url: "https://palimpsest.dmslab.re.kr"
palimpsest_public_haproxy_enabled: true
```

The role derives `palimpsest_public_haproxy_fqdn` only from an exact HTTPS
hostname origin, with an optional single trailing slash. Ports (even `:443`),
paths, queries, fragments, credentials, whitespace and non-HTTPS URLs derive
an empty hostname and disable public routing, including in HAProxy-only plays.
The default `https://<kolla_external_fqdn>:8020` cannot claim the main host.
Explicit hostname overrides remain supported and must match the origin at
precheck; they do not implicitly enable exposure. DNS and Kolla external
TLS/certificate setup remain operator prerequisites. The internal VIP endpoint
and private listen port are unchanged.

With the corresponding updated Afterglow role, this explicit endpoint defaults
`afterglow_service_palimpsest_internal_url` and is emitted as
`[services] palimpsest_internal_url` in its generated configuration. A separate
trusted HTTPS transport URL can override the Afterglow variable. Unset/empty
values preserve detailed Afterglow operator TOML, not a catalog fallback.
`palimpsest_package_public_origin` is still a separate package-key gateway origin
(the Afterglow origin in the DMS Lab example), not the Hub routing URL. Apply the
reviewed roles through normal `kolla-ansible reconfigure --tags palimpsest,afterglow`
after preserving image pins and data. A correct HAProxy route does not install
new package APIs; verify Hub version and authenticated `/v1/projects/current`.

Repository tags label both
`ghcr.io/openstack-afterglow/palimpsest-hub-api` and
`ghcr.io/openstack-afterglow/palimpsest-hub-worker` with tags derived from
the repository tag, not from the Hub wheel version. The root
[`v0.2.3` release workflow](https://github.com/openstack-afterglow/palimpsest/actions/runs/36081630018)
completed artifact verification and native stage-1 KVM proof, published through
the `palimpsest-client` trusted publisher, and created the GitHub Release. The
separate [Hub image workflow](https://github.com/openstack-afterglow/palimpsest/actions/runs/36081630008)
published tagged API and worker images. A PR check, development prerelease,
or skipped KVM job alone cannot substitute for the root release gate; neither
published artifact proves an operator deployment.

## Supported hosts and runtime scope

| Host/runtime | Status | Package | Runtime boundary |
| --- | --- | --- | --- |
| macOS Apple Silicon with Lima/VZ | Supported default | `palimpsest-client` | Lima 2.1+; conventional cloud-image VMs |
| macOS Apple Silicon with QEMU/libvirt HVF | Experimental | `palimpsest-client[kvm]` | `qemu:///session`; conventional cloud-image VMs only |
| Linux `x86_64` with libvirt/KVM | Supported | `palimpsest-client[kvm]` | `qemu:///system`; conventional cloud-image VMs and the narrower OCI-root runtime |
| Linux `aarch64` with libvirt/KVM | Supported | `palimpsest-client[kvm]` | `virt` + EFI; conventional cloud-image VMs |
| Linux `x86_64`/`amd64` OCI-root | Supported narrow runtime | `palimpsest-client[kvm]` | OCI content becomes guest `/`; Linux KVM and qualified runtime assets required |
| Remote KVM or multi-host scheduling | Unsupported | — | Local-host runtime only |

The conventional cloud-image path and OCI-root path have different root
semantics. A conventional VM boots its cloud image and mounts Palimpsest layers
under `/opt/layers/merged`. OCI-root materializes a Linux `amd64` OCI artifact,
switches the guest root, and runs the workload as PID 1. Portable package
installation is not native runtime qualification.

The branch PR's native stage-1 proof checks OCI-root guest `/` switching and
PID 1; it is not a conventional cloud-image VM boot. The serial-bound first
boot and cloud-init-disabled reboot readiness changes still lack a post-merge
native conventional-cloud boot/reboot check, even if the stage-1 gate passed.

## Install directly from GitHub

Install the default branch into an active Python 3.11+ environment with pip:

```sh
python3.12 -m pip install \
  "palimpsest-client @ git+https://github.com/openstack-afterglow/palimpsest"
palimpsest --version
```

For a standalone CLI, prefer a uv tool environment. This path does not inherit
an unrelated project's Python support range and can provision Python 3.11 on a
host whose system interpreter is older:

```sh
uv python install 3.11
uv tool install --python 3.11 \
  "palimpsest-client @ git+https://github.com/openstack-afterglow/palimpsest"
palimpsest --version
```

For a uv-managed application dependency, declare a compatible project range
when creating the project:

```sh
uv python install 3.11
uv init --bare --python 3.11
uv python pin 3.11
uv add "palimpsest-client @ git+https://github.com/openstack-afterglow/palimpsest"
uv run python -c "import importlib.metadata as m; print(m.version('palimpsest-client'))"
uv run palimpsest --version
```

uv resolves every version admitted by the application's `requires-python`
value. Merely pinning Python 3.11 does not make a project declaring
`requires-python = ">=3.10"` compatible: raise the project floor to 3.11 or use
the isolated tool path. Do not use the suggested `--frozen` escape hatch; it
skips locking/syncing and does not make the package runnable on Python 3.10. If
`uv init` created a local application that also owns the `palimpsest` command,
the `importlib.metadata` probe above is the authoritative installation check.

The root distribution's installed version is reported under `palimpsest-client`;
for a 0.2.3 install this probe prints `0.2.3`.

uv records the dependency as a Git source and locks its resolved commit. For a
reproducible pip or uv installation, pin a reviewed full 40-character SHA:

```sh
PALIMPSEST_REF="FULL_40_CHARACTER_COMMIT_SHA"
python3.12 -m pip install \
  "palimpsest-client @ git+https://github.com/openstack-afterglow/palimpsest@${PALIMPSEST_REF}"
uv add \
  "palimpsest-client @ git+https://github.com/openstack-afterglow/palimpsest@${PALIMPSEST_REF}"
```

A branch or tag can replace the SHA after `@`, but moving refs are not
reproducible. Review the commit before use. A VCS installation validates the
fetched Python project through the normal build backend; it is not a
signed-release or native-KVM claim.

### Add libvirt support

When host libraries and the approved Python dependency source are ready,
replace or upgrade the base installation with the `kvm` extra:

```sh
"$HOME/.venvs/palimpsest/bin/python" -m pip install --upgrade \
  "palimpsest-client[kvm] @ git+https://github.com/openstack-afterglow/palimpsest.git@${PALIMPSEST_REF}"
```

Building `libvirt-python` may require platform libvirt development headers. The
extra does not configure libvirt, QEMU, firmware, networks, permissions, or
`/dev/kvm`.

### Install the standalone Hub

Keep Hub in its own environment and pin it to the same ref:

```sh
python3.12 -m venv "$HOME/.venvs/palimpsest-hub"
"$HOME/.venvs/palimpsest-hub/bin/python" -m pip install --upgrade pip
"$HOME/.venvs/palimpsest-hub/bin/python" -m pip install \
  "palimpsest-hub @ git+https://github.com/openstack-afterglow/palimpsest.git@${PALIMPSEST_REF}#subdirectory=hub"

"$HOME/.venvs/palimpsest-hub/bin/python" -m pip show palimpsest-hub
```

Do not start `palimpsest-hub` merely as an installation check: it starts the
service and requires its database, Redis, blob path, and OpenStack settings.

For server-side builds only, provision a **separate Linux KVM host**. Install
`palimpsest-client[kvm]` at the same reviewed ref in its own administrator-owned
interpreter; install `palimpsest-hub` on the worker host as well. Point
`PALIMPSEST_HUB_BUILDER_PYTHON` at that absolute Local interpreter, not at the
Hub API container. The worker also needs readable/writable `/dev/kvm`,
`qemu:///system` access, QEMU, firmware, `cloud-localds`, SquashFS tools, and
the normal conventional cloud-image build dependencies. Do not expose a Docker
socket or run the build worker in the unprivileged Hub API container. Host
permissions, external services, and an actual guest boot remain separate
deployment prerequisites; package installation does not establish them.

The API/export worker still need the Hub Redis and Keystone settings. The export
worker also needs the `OS_READER_*` validator and both protected-ID arrays: it
revalidates the requester's current authority before every delegated export.
The build worker only reads `DATABASE_URL`, the database pool settings,
`PALIMPSEST_HUB_LOCAL_PATH`, `PALIMPSEST_HUB_MAX_BLOB_BYTES`,
`PALIMPSEST_HUB_BUILD_TIMEOUT_SECONDS`, and
`PALIMPSEST_HUB_BUILDER_PYTHON`; it does not require Redis or `OS_*` credentials.

### Hub API and worker logs

The API, Glance export worker, and separate build worker emit Hub-owned INFO
events by default. API events contain the HTTP method, **matched route
template** (or `<unmatched>`), status and elapsed milliseconds, not the URL.
Claimed export/build jobs emit start and terminal status; idle polling does not
emit a per-poll event. Set `PALIMPSEST_HUB_LOG_LEVEL=DEBUG` on the individual
process to opt in to bounded query presence/length and numeric job state/result
summaries. Other values retain INFO. DEBUG never includes query values, headers,
bearer/download tokens, SQL binds, recipe, disk bytes, paths, OpenStack payloads
or exception text. Hub disables Uvicorn's raw-target access logger at startup,
including when launched through the direct `uvicorn palimpsest_hub.main:app`
command. Do not enable third-party HTTP/SQL trace logging or an independent
server access logger that emits raw targets in production.

On a reused asyncmy connection whose uvloop TCP transport was already closed,
Hub marks the failed pool pre-ping as a disconnect and obtains a fresh connection
before the request's SQL operation. It does not retry transactions that lose
their connection after checkout; reconcile those requests before retrying.

The build worker executes `python -I -m palimpsest_local.hub_builder preflight`
using that exact Local interpreter **before** opening SQL or claiming a job.
Preflight requires an x86_64 Linux KVM host, an accessible system libvirt URI,
and root-owned non-writable QEMU, cloud-localds, SSH and SCP binaries. A
successful preflight does not prove that a guest can boot or be cleaned up.
For offline Linux deployment, prepare and hash a complete target-ABI wheelhouse
including Hub transitive dependencies and the `libvirt-python` dependency of
`palimpsest-client[kvm]`; two application wheels alone are not runnable. Do not
reuse an active CI runner's KVM/libvirt host as an isolated worker without an
explicit operator-owned runner drain/fence and resource assessment.

2026-09-24 live proof on a dedicated Linux KVM host found two deployment
prerequisites that are not enforced by preflight and cause a `build_failed`
or `cleanup_failed` job with no further detail (the worker never logs
recipe/path content). First, the QEMU execution user must be able to access
the build worker's private job tree and overlay disk. The default QEMU user
could not access the worker-owned tree and guest creation failed with
`Permission denied`. In the live proof, QEMU and the worker ran as the same
dedicated unprivileged user; the QEMU group can differ from the worker's
primary group, and must retain `/dev/kvm` access. Second, keep
`PALIMPSEST_HUB_LOCAL_PATH` short: the guest control socket path is
`<PALIMPSEST_HUB_LOCAL_PATH>/builds/<build-uuid>/state/runs/builder-<id>/builder.sock`.
Linux `AF_UNIX` rejects socket paths that do not fit its 108-byte `sun_path`
(including the terminator); choose a short path such as `/srv/h` and account
for the actual builder ID length. The longer path used in the first live
attempt failed with `UNIX socket path ... too long`.

That probe ran API, worker, and QEMU under one UID and kept an administrator
credential copy in private staging; it did not verify production account or
credential separation. Sharing the UID was a probe-specific access choice,
not a production requirement.

Third, size the dedicated host above the builder guest's fixed 4096 MiB
(`src/palimpsest_local/build.py`) plus QEMU, Hub, database, Redis, and host
overhead. A separate Nova host with only 4,106,240,000 bytes of RAM passed
preflight and accepted a two-layer/two-RUN job, but QEMU could not allocate its
4,294,967,296-byte guest RAM; the job ended `build_failed` before boot. Do not
interpret a successful preflight or HTTP 202 as proof of guest execution.

A separately approved `cpu.2c_8g` Nova host (8,327,811,072 bytes visible in
the guest) subsequently completed one network-none two-layer/two-RUN build.
This qualifies that minimal host/configuration only, not a universal memory
minimum or production isolation. The exact job and cleanup evidence are in
[`development-handoff.md`](development-handoff.md).

The separately approved timeout/restart probe used a fresh 8GiB KVM host,
one private SQL/Redis/store, a 900-second build timeout, and two distinct
network-disabled guest recipes whose `RUN` commands printed a marker before
sleeping 1800 seconds. After observing each live guest and its marker, the
timeout case ended `error/build_failed` with no guest or scratch left; killing
only the recorded build worker main and starting a replacement ended the other
case `error/worker_interrupted` with the same cleanup boundary. The replacement
worker logged one interrupted build reconciled. These outcomes do not qualify
hard power loss, arbitrary recipes, production credential separation, or an
otherwise underprovisioned KVM host. Exact job IDs and cleanup evidence are in
[`development-handoff.md`](development-handoff.md).

## External prerequisites by feature

Python package installation deliberately does not install or mutate host tools.

### macOS Apple Silicon default runtime

- Lima 2.1 or newer with the VZ backend.
- Host virtualization permission and sufficient disk/memory.
- TCP publication only for Compose-shaped project ports.

The experimental libvirt/HVF backend additionally needs QEMU, libvirt,
`qemu:///session`, `hdiutil`, and the Local `[kvm]` extra. With Homebrew,
`brew install qemu pkgconf` supplies the separate QEMU tools and the
`pkg-config` executable needed to build `libvirt-python` against the already
installed host libvirt. In a source checkout, install the optional binding with
`uv sync --frozen --extra dev --extra kvm`; verify session access with
`virsh -c qemu:///session list --all` before explicitly selecting
`--backend libvirt-hvf`. Package installation or successful preflight alone
does not prove a guest can boot; Lima/VZ remains the macOS default.

### Linux conventional cloud-image runtime

- `/dev/kvm`, QEMU, libvirt, and access to `qemu:///system`.
- Architecture-appropriate EFI firmware and the libvirt `default` network.
- `qemu-img`, `cloud-localds`, SquashFS tools, OpenSSH, and filesystem tools.
- The Local `[kvm]` extra.

The installer does not grant group membership or create libvirt networks.
Linux Compose project port publication is currently rejected for the
conventional cloud-image path.

### Linux OCI-root runtime and registry acquisition

- Linux `x86_64`/`amd64`, KVM, system libvirt, and qualified kernel,
  initramfs/stage-1, packer, and domain-profile inputs.
- Skopeo 1.13 or newer on `PATH` for `palimpsest oci pull`; local OCI archive
  materialization does not require Skopeo.
- Only anonymous HTTPS `linux/amd64` registry acquisition is supported by the
  current `oci pull` contract.

OCI-root networking supports `nat`, `host-only`, and `none` under its explicit
per-VM network contract. This is separate from Compose project publication.

### Docker/OCI and Dockerfile builds

- An installed Docker CLI for OCI-profile `login`, `pull`, `push`, `tag` and Docker inventory/history/save/load/remove/passthrough. Native package push/pull do not use Docker's image store.
- An installed Docker credential helper and exact API-base/namespace `credHelpers` entry (or `credsStore`) for native login. Host-only Docker credentials, `auths` and plaintext fallback are not accepted. The native client also supports an ephemeral `PALIMPSEST_PACKAGE_KEY`, not a legacy Keystone token.
- A separately managed Buildx builder with an OCI exporter for Dockerfile builds. The default `docker` driver does not provide the required exporter.
- Every online build requires a project-scoped key with both `cache:read` and `cache:write`; OCI output must pass `--cache-registry NATIVE_ALIAS --cache-package NAMESPACE/PACKAGE`, while native output defaults to its own profile/package. CLI package publication additionally requires `packages:inventory` and `packages:write`, not content-download authority. Pull requires effective inventory plus `packages:read`.
- Strict offline Dockerfile builds require preloaded pinned inputs and a
  separately bootstrapped single-node `docker-container` builder using
  `--driver-opt network=none`.

Palimpsest validates the selected builder but does not create, reconfigure, or
grant access to Docker or BuildKit.

## Local CLI configuration

### State and configuration roots

The Local state root is resolved in this order:

1. `PALIMPSEST_STATE_HOME`.
2. `[storage].state_root` in
   `${XDG_CONFIG_HOME:-~/.config}/palimpsest/config.toml`.
3. An explicitly set `XDG_STATE_HOME`, with `/palimpsest` appended.
4. Linux default `/var/lib/palimpsest`; other platforms default to
   `~/.local/state/palimpsest`.

For a user-owned first run on Linux:

```sh
export XDG_STATE_HOME="$HOME/.local/state"
"$HOME/.venvs/palimpsest/bin/palimpsest" store show
```

`store show` is the authoritative first check because it prints the selected
state, configuration, and journal boundaries before other stateful work.

Linux command journaling defaults to `/var/log/palimpsest/commands.jsonl`.
`PALIMPSEST_LOG_HOME` can select another pre-created absolute owner-private
directory. Outside Linux, journaling is disabled unless that override is set.

```sh
install -d -m 0700 "$HOME/.local/state/palimpsest-log"
export PALIMPSEST_LOG_HOME="$HOME/.local/state/palimpsest-log"
```

### Hub client settings

Hub URL precedence is:

1. Global `--url HUB_URL` before the command.
2. `PALIMPSEST_URL`.
3. `url` or `[hub].url` in `config.toml`.

Legacy Hub artifact commands require the original project-scoped `PALIMPSEST_TOKEN`; keep it in approved secret handling, never `config.toml`, documentation, history or local state. Native package/cache profiles use their own HTTPS API base and package key, not `PALIMPSEST_URL`/`PALIMPSEST_TOKEN`. Package keys cannot queue privileged `/v1/builds`; those remain system-administrator operations. Legacy tokens cannot authorize BuildKit-cache writes.

```sh
export PALIMPSEST_URL="https://hub.example.invalid"
# Inject PALIMPSEST_TOKEN through the approved secret mechanism.
palimpsest image ls --limit 10
```

### Registry profiles and credentials

Profiles live at `${XDG_CONFIG_HOME:-~/.config}/palimpsest/registries.toml`, schema version 1, and contain no credentials. `registry add --protocol oci|palimpsest` defaults to OCI, as do old profiles with no protocol. For unqualified references, selection is `--registry`, `PALIMPSEST_REGISTRY`, then default. A fully qualified authority with no configured profile stays ordinary Docker/OCI, as before; this includes login/logout. A configured authority must map to one protocol, and an explicit `--registry` must match a qualified reference's authority. Strict offline builds never read this file.

Native profiles require `--api-base HTTPS_URL` on exactly the `endpoint` authority and optionally a one-component namespace. Native HTTPS verifies system trust plus absolute `--ca` files and refuses redirects; mirrors, plain HTTP, TLS-skip and Docker cache exporters are rejected. The helper entry is exactly `api_base.rstrip('/') + '/projects/' + namespace`; lookup checks that `credHelpers` entry then `credsStore`, never host-only entries or `auths`. Configure/install `docker-credential-HELPER` first. Native login verifies `/auth/me` before storing anything, uses the key's public UUID as `--username`, and accepts the secret only through stdin/prompt. Native `--namespace` selects the credential namespace. OCI profiles retain Docker's `DOCKER_CONFIG`/`~/.docker` credentials.

An ordinary member first registers the operator-bound namespace and issues a secret-once exact-package/action-bound key through the native control API. Follow the complete [registry flow](registries.md#native-authentication-build-and-transfer), including conditional `cloud.dmslab.re.kr/openstack-afterglow/test:v1` usage only after binding the alias to the actual immutable project UUID. Native `push --input`/`--manifest` and `pull --output` transfer verified OCI-layout archives without loading Docker; local typed receipts and frozen exports live under `state/package-references/` and `state/package-artifacts/`, separate from runtime-layer tags. These new source paths and written tests are not deployment or executed acceptance evidence.

### Compose project identity

Compose project names resolve from `-p`, then `PALIMPSEST_PROJECT_NAME`, then
`COMPOSE_PROJECT_NAME`, then the model/default naming rule. Global Compose
options must precede the Compose subcommand.

## Hub service settings

Hub uses case-insensitive Pydantic environment settings with no custom prefix.
The required settings are:

| Environment setting | Meaning |
| --- | --- |
| `DATABASE_URL` | Async MySQL-compatible SQLAlchemy URL |
| `REDIS_URL` | Redis connection URL |
| `PALIMPSEST_HUB_LOCAL_PATH` | Hub-owned local blob/cache directory |
| `OS_AUTH_URL` | Keystone authentication endpoint |
| `OS_USERNAME` / `OS_PASSWORD` | Export Trust trustee identity. It authenticates only to its own service project (to resolve its user ID) and through requester-created Trusts; it needs no tenant role assignment and is never scoped to a tenant project |
| `OS_PROJECT_NAME` | The trustee's own service project |
| `OS_READER_USERNAME` / `OS_READER_PASSWORD` | Separate read-only Keystone validation identity. It must authenticate system-scoped (`all`) with `reader` and no `admin`, `manager` or `service` role. Missing/unavailable or elevated validator authority fails token validation with 503. |
| `PALIMPSEST_HUB_PACKAGE_FORBIDDEN_PROJECT_IDS` | JSON array of exact protected project IDs, for example `'["<project-id>"]'`. A comma-separated list does not parse. If empty, every package endpoint returns 503. |
| `PALIMPSEST_HUB_PACKAGE_FORBIDDEN_USER_IDS` | JSON array of exact protected administrator/service principal IDs, for example `'["<user-id>"]'`. If empty, every package endpoint returns 503. |

Optional settings and defaults:

| Environment setting | Default / constraint |
| --- | --- |
| `DATABASE_POOL_SIZE` | `10`, valid 1–100 |
| `DATABASE_MAX_OVERFLOW` | `20`, valid 0–100 |
| `DATABASE_CONNECT_TIMEOUT` | `10` seconds, valid 1–120 |
| `DATABASE_POOL_TIMEOUT` | `30` seconds, valid 1–120 |
| `DATABASE_UNHEALTHY_SECONDS` | `30` seconds, valid 1–3600 |
| `PALIMPSEST_HUB_MAX_BLOB_BYTES` | `107374182400` bytes (100 GiB), minimum 1 |
| `PALIMPSEST_HUB_MAX_BUNDLE_EXPANDED_BYTES` | `107374182400` bytes (100 GiB), minimum 1; total expansion allowed for one imported bundle |
| `PALIMPSEST_HUB_MAX_BLOCKING_OPERATIONS` | `2`, valid 1–16; process-wide concurrent hash/copy/parse workers |
| `PALIMPSEST_HUB_BUILDER_PYTHON` | Empty: build requests return 503. For build service, absolute path to the **separate** `palimpsest-client[kvm]` interpreter on the KVM worker; set the same path string on API and worker. |
| `PALIMPSEST_HUB_BUILD_TIMEOUT_SECONDS` | `3600`, valid 60–3600; guest teardown is attempted on timeout. |
| `PALIMPSEST_HUB_PACKAGE_NAMESPACE_BINDINGS` | `{}`; JSON object mapping canonical namespace aliases to exact immutable Keystone project IDs, for example `'{"openstack-afterglow":"<project-uuid>"}'`. One configured alias per project; an existing SQL namespace is never rebound. |
| `PALIMPSEST_HUB_PACKAGE_PUBLIC_ORIGIN` | Empty. Trusted HTTPS origin in canonical lower-case host/port form, with no credentials, path, query or fragment; other forms are rejected at startup. It is never inferred from the request Host. If empty, no package authority is advertised, and every native push fails closed with `503 HUB_UNAVAILABLE` before its tag is published. |
| `PALIMPSEST_HUB_EXPORT_DELEGATED_ROLES` | `'["member"]'`; JSON array of the least global roles a deferred Glance export Trust delegates. Glance's default `download_image` policy requires `member`, even for images the project owns. The requester must currently hold each listed role in the target project. `admin`, `manager` and `service` are rejected at startup. |
| `PALIMPSEST_HUB_EXPORT_DELEGATION_TTL_SECONDS` | `21600`, valid 900–86400; finite Trust lifetime. A queued export not started within it fails `delegation_expired`. |
| `OS_READER_USER_DOMAIN_NAME` | `Default`; domain of the separate validation identity |
| `OS_USER_DOMAIN_NAME` / `OS_PROJECT_DOMAIN_NAME` | `Default` |
| `OS_REGION_NAME` | `RegionOne` |
| `OS_INTERFACE` | `internal` |
| `SSL_VERIFY` | `true` |

### Original token and package-owner prerequisites

The `OS_USERNAME`/`OS_PASSWORD` service identity remains separate from the read-only validator. The validator authenticates itself with `system_scope="all"` and validates the **presented subject token** through Keystone's token-read API; it never exchanges that token for a new default-project token and never falls back to service/admin credentials. `X-Project-Id`, if supplied, is only an exact assertion against the original scope, not a re-scope request. Downstream caller-scoped OpenStack requests reuse the validated original token and catalog. A missing reader identity/policy response fails closed (503), invalid/expired tokens fail 401, and scope assertions fail 403.

Operator-owned Keystone policy must let this reader call token validation (`tokens.validate`), `users.get`, `projects.get` and `role_assignments.list`, including the system-role check that keeps privileged server builds administrator-only. On every package-key use the implementation checks that the user and project are currently enabled, and lists `role_assignments.list(user=OWNER, effective=True, include_names=True)`. It does not rely on the token or a cached membership. The owner needs a `member` or `reader` assignment on the target project, and an `admin` or `service` assignment on any scope makes the owner forbidden. Protected user/project IDs and the validator identity itself are also refused. Read-only members cannot delegate write actions. Configure both protected sets with every relevant administrative/service project and principal; empty sets are not an allow-all default. Do not grant package upload authority to an administrator to work around a missing prerequisite.

Keystone user/project IDs are exact bounded ASCII identifiers (up to 64 characters), including federated 64-hex user IDs. Do not parse them as UUIDs, case-fold or strip them. UUID formatting applies only to Hub-generated key/upload IDs. Protected sets and namespace binding values must preserve exact identity bytes. For a conventional project UUID, the conditional operator configuration is:

```text
PALIMPSEST_HUB_PACKAGE_FORBIDDEN_PROJECT_IDS='["<protected-project-id>"]'
PALIMPSEST_HUB_PACKAGE_FORBIDDEN_USER_IDS='["<protected-user-id>"]'
PALIMPSEST_HUB_PACKAGE_NAMESPACE_BINDINGS='{"openstack-afterglow":"<project-uuid>"}'
PALIMPSEST_HUB_PACKAGE_PUBLIC_ORIGIN=https://cloud.dmslab.re.kr
```

Replace each `<...>` marker with the operator-observed exact ID. A marker is not a display name, credential or authorization to change production. An ordinary member then registers the namespace for that original project. Without an alias, canonical lower-case 32-hex projects use `p-<project-id>`; other valid opaque IDs use `p-h-<56 hex characters of SHA-256(project-id)>`. Reserved deterministic names cannot bind another project. The SQL binding stays fixed even if configuration later changes. The native CLI reaches this Hub through the separate Afterglow key gateway at `https://cloud.dmslab.re.kr/api/v1/palimpsest/hub`; that gateway has not been deployed.

For federated owners, the reader must be able to see the owner's membership through a fresh effective role-assignment listing. That means a persistent direct user assignment or a currently persisted expiring group membership. [Keystone federation mapping combinations](https://docs.openstack.org/keystone/latest/admin/federation/mapping_combinations.html) distinguishes project mappings, which persist direct assignments, from group mappings, whose membership may be ephemeral. A user whose project role comes only from an ephemeral mapping group may not appear in that listing and then needs a persistent or direct assignment. A token-only ephemeral grant cannot stand in for fresh key-owner authority, because key use carries no caller Keystone token. If the cloud cannot expose that membership, key use fails closed. There is no automatic policy change or admin fallback, and no production mapping was changed here. This is an operator prerequisite, not a verified result.

These are operator prerequisites for a separately approved rollout, not a claim that Keystone policy, HTTPS origin, alias binding or package acceptance has been verified on the deployed service. The [2026-10-02 candidate checkpoint](development-handoff.md#candidate-integration--2026-10-02) records passed portable/Hub source gates and isolated HTTPS/BuildKit acceptance, with its identity/database limits.

The canonical Kolla role maps these inputs explicitly: `palimpsest_reader_user`, secret `palimpsest_reader_password` and `palimpsest_reader_user_domain_name` become `OS_READER_*`; `palimpsest_package_forbidden_project_ids` / `palimpsest_package_forbidden_user_ids` become the required protected JSON arrays; `palimpsest_package_namespace_bindings` becomes the alias map; and `palimpsest_package_public_origin` becomes the trusted HTTPS origin. The role precheck requires a separate reader username/password and both nonempty exact-ID protected sets. Secrets belong in approved private Kolla secret inputs, never globals/examples/logs. Rendering these settings is not evidence of Keystone policy or membership readiness.

Reader provisioning uses `openstack.cloud.role_assignment` with `system: all`. The [reviewed 2.6.0 module schema](https://docs.ansible.com/projects/ansible/latest/collections/openstack/cloud/role_assignment_module.html) supports that argument; verify the operator-installed collection supports it before rollout. Provision a dedicated reader identity and inspect its actual effective grants: `state: present` does not remove pre-existing elevated assignments, and the Hub rejects validator tokens holding `admin`, `manager` or `service`. No collection installation or cloud role mutation is implied.

### Deferred Glance export delegation

`POST /v1/image-exports` keeps the original-token admission: Glance visibility is checked with the presented token, and current `palimpsest-publish_editor` authority comes from the validator. When the request needs new Glance work (not byte reuse), the Hub creates a Keystone Trust **with the requester's validated token**: trustor = requester, trustee = this service identity, project = the token's project, `impersonation=true`, only `PALIMPSEST_HUB_EXPORT_DELEGATED_ROLES`, and a finite expiry. Keystone's response must match exactly or the Trust is deleted and the request fails. The Hub stores the Trust ID, scope, roles and expiry in `palimpsest_image_export_delegations`. It never stores the requester's token or password. Keystone refuses trust creation from application-credential, OAuth1 and EC2 tokens. Application credentials are exempt only when the insecure `allow_insecure_application_credential_trust_escalation` option is set. Exports requested with such tokens fail `delegation_denied` (403); requesters need a password, OIDC or federated token.

Before Glance I/O, the export worker re-reads the requester's current enabled user/project, publish authority and delegated role through the validator. It then authenticates `v3.Password(user_id=<trustee>, password, trust_id=...)` without project selectors. It verifies the trust token's ID, trustor, trustee, impersonated user, project, roles (no wider than current implications of the delegated roles) and expiry. Revoked, expired, swapped or legacy (pre-cutover, undelegated) exports fail terminally with `authorization_revoked`, `authorization_unavailable`, `delegation_revoked`, `delegation_expired`, `delegation_scope_mismatch` or `delegation_required`. There is no fallback to the service password scoped to the tenant. A new export always uses the current requester's own Trust. An earlier creator's delegation is retired, never reused.

Each Trust has a durable cleanup state. A terminal job, soft deletion, exhausted attempts or an abandoned admission queues its Trust for deletion. The worker deletes it through its own impersonating trust token, retries with backoff and records `deleted`. Admission deletes an unbound Trust immediately with the requester token when it can. Cleanup failure never reruns an export. A Trust that currently cannot issue tokens (trustor disabled or delegated role removed) cannot be deleted by the Hub. Keystone keeps that Trust, and restoring the role or user before `expires_at` makes it usable again. The finite expiry is therefore the real bound. After expiry the Hub records the Trust as `expired`; it stays in Keystone until the operator runs `keystone-manage trust_flush`. Keystone policy must allow trustors to create and delete their own trusts (`identity:create_trust`, `identity:delete_trust` defaults) and must allow trust-scoped authentication. These are operator prerequisites. No deployed Trust flow has been verified.

Cleanup completion is fenced by the claim's `cleanup_attempts` generation as
well as Trust ID/state: a stale worker returning after its five-minute lease
cannot overwrite the newer claimant's cleanup/backoff state. Only an update
that still owns the claim counts as finished. Export soft-delete compares
stored database DATETIME leases after UTC normalization; an unexpired claim
still returns busy rather than being deleted. New tests define these boundaries
and actual Base-model migration of all five delegation states, exact opaque
trustor IDs, roles JSON and expiry/retry metadata. No final regression or live
policy verification was run during source preparation.


The canonical Kolla role renders `palimpsest_export_delegated_roles` (default `["member"]`) and `palimpsest_export_delegation_ttl_seconds` (default `21600`) into the API environment. Its precheck rejects empty or administrative role lists and lifetimes outside 900–86400 seconds. The export worker container receives the same `OS_READER_*` and protected-ID inputs as the API. The service user's Kolla registration remains limited to its own `palimpsest_service_project_name`, where it still holds the existing `admin` grant. Do not add it to tenant projects. Existing manual tenant grants are no longer used by the Hub; removing them, or reducing the service-project grant, is a separate operator-approved change.

After provisioning dependencies and injecting secrets, initialize the schema
with `palimpsest-hub-bootstrap`, then run `palimpsest-hub` and
`palimpsest-hub-worker` as separately supervised processes. To enable
server-side builds, run `palimpsest-hub-build-worker` on the KVM host with the
same `DATABASE_URL` and the same **absolute** `PALIMPSEST_HUB_LOCAL_PATH`
filesystem view as the API. The worker takes one filesystem singleton lock and
polls queued jobs; a second worker for the same store is rejected. Bootstrap
creates `palimpsest_hub_builds` and `palimpsest_image_export_delegations`
(additive tables only); data migration additionally copies build rows, export
delegation references and project layer grants when present in the source.
Migration remains separate from schema bootstrap. Exports queued before the
delegation cutover have no delegation and fail `delegation_required` instead of
running with service credentials; requesters resubmit them.

The `/app` web console uses the existing project-scoped `/v1/layers` and
resumable `/v1/uploads` endpoints, plus `POST /v1/builds`, `GET /v1/builds`,
and `GET /v1/builds/{id}`. A build request supplies `name`, full
`Palimpsestfile` text as `recipe`, `base_digest`, and optional ordered
`layer_digests`. The `FROM`/`LAYER` instructions must exactly match those
digests. Only a Keystone **system administrator** with a project-scoped token
can queue or inspect builds. Input visibility is checked on enqueue and again
when claimed; successful output appears as a private SquashFS layer under
`GET /v1/layers/{digest}/blob`. Raw recipes, user identities, and worker paths
are not returned by the build API. Builds run with VM networking disabled;
queue capacity is four active jobs per project and creation is rate-limited to
six requests per hour. A failed/interrupted build is not automatically retried.
Guest cleanup failures stop the worker and retain private state for operator
inspection, rather than claiming the VM was removed.

Before running guest code, the Linux child records its boot ID/PID/start ticks
in the private job tree and sets a parent-death signal. On worker restart,
recovery verifies and stops that exact process group before VM cleanup. An
unverifiable process or failed cleanup leaves the job tree and halts the build
worker for operator inspection; do not delete the tree blindly.

Uploads cap at four active sessions per project. Before creating a session,
24-hour-idle sessions for that project are removed under per-session locks;
interrupted PATCH bytes beyond the last acknowledged offset are discarded on
resume. The browser keeps the token in tab memory only; use HTTPS except on
localhost. Chromium streams large downloads directly to a chosen file; other
browsers support downloads up to 64 MiB in this console and can use the Hub
API/client for larger artifacts. Server-side KVM execution has **not** been
established by merely installing either package or by passing the portable
HTTP contract tests.

Lock waits never occupy a worker thread: each attempt is one non-blocking
`flock`, and a failed attempt sleeps on the event loop before retrying. Waiters
therefore cannot starve the current owner's unlock, its next lock, or its
filesystem worker, but acquisition order is not FIFO — per-project session and
build caps are what bound contention. Blob garbage collection skips a locked
blob instead of waiting for it. Cancellation while waiting for an upload or
project lock cannot retain its eventual file descriptor. A digest-verified
upload keeps an independent staging file until its layer registration commits:
retry the same PUT after a crash before commit using the acknowledged offset.
Concurrent registration of identical bytes and build queue capacity use
store-backed digest/project locks, so API replicas must share the same
filesystem view.

`PALIMPSEST_HUB_MAX_BLOB_BYTES` is a **per-blob** ceiling, not a per-project
or whole-store disk quota. `PALIMPSEST_HUB_MAX_BUNDLE_EXPANDED_BYTES` bounds
one bundle import's total expanded bytes; a bundle also refuses more than 4096
members and any member larger than the per-blob ceiling, and it stages and
verifies every blob before publishing any of them.
`PALIMPSEST_HUB_MAX_BLOCKING_OPERATIONS` bounds how many requests may occupy
hashing, copying, parsing, or fsync threads at once; raising it trades event
loop responsiveness and disk throughput for upload concurrency. A disconnected
client does not abort an in-flight worker, so its file lock is held until that
worker finishes. The canonical Kolla role exposes the three settings as
`palimpsest_hub_max_blob_bytes`, `palimpsest_hub_max_bundle_expanded_bytes`,
and `palimpsest_hub_max_blocking_operations`. Completed build rows and result
blobs have no
automatic retention/garbage-collection policy here; provision filesystem
capacity and operator monitoring separately before exposing uploads/builds.

During finalization, an unregistered upload and the promoted blob may briefly
consume twice that artifact's size. Include this peak in free-space planning.

## Administrator-owned Linux deployment

For a single-operator service host, keep installed code separate from mutable
state. Create an administrator-owned environment and install the same reviewed
VCS ref without invoking pip through `sudo` from an untrusted current directory:

```sh
cd /
sudo python3.12 -I -m venv /opt/palimpsest
sudo /opt/palimpsest/bin/python -I -m pip install \
  "palimpsest-client[kvm] @ git+https://github.com/openstack-afterglow/palimpsest.git@${PALIMPSEST_REF}"
```

Verify that the environment is administrator-owned and not writable by the
service identity. Package installation creates no account or state directory.
After reviewing the deployment identity, invoke the packaged provisioner as a
separate explicit operation:

```sh
cd /
sudo /opt/palimpsest/bin/python -I -m palimpsest_local.linux_install
```

It creates the no-login `palimpsest` account and owner-only (`0700`)
`/var/lib/palimpsest` and `/var/log/palimpsest` directories. Matching objects
are accepted; conflicts and symlinks are rejected. It does not recursively
change ownership, migrate/remove data, add privileged groups, configure KVM or
Docker, or write sudoers policy.

Run management commands with inherited root overrides removed:

```sh
sudo -H -u palimpsest env -u PALIMPSEST_STATE_HOME -u PALIMPSEST_LOG_HOME \
  -u XDG_STATE_HOME -u XDG_CONFIG_HOME \
  /opt/palimpsest/bin/python -I -m palimpsest_local.cli store show
```

## Isolated Hub candidate access and data safety

This is an operator handoff for the **candidate**, not a production installation
recipe or a claim that `0.2.4` is published. The private local workdir is
`/Users/pieroot/.local/share/palimpsest-candidate-024/`; consult its nonsecret
`candidate-resources.json`, `artifact-receipt.json`, `reboot-receipt.json` and
the [dated evidence](development-handoff.md#격리-hub-후보-운영-인계--2026-09-27-production-미승격).
Do not paste its `api.env`, `build-worker.env`, `compose.env`, Keystone input,
SSH private key, or token into logs, tickets or commands. Root-owned candidate
configuration on the VM is `/etc/palimpsest-candidate/` (directory 0700,
files 0600). Its service state is `/srv/h` on the **separate mounted ext4 data
volume**, not the boot disk. API, export worker and bootstrap containers run
as UID/GID 2001 and mount `/srv/h:/srv/h`; the host KVM build worker is also
UID 2001. `/opt/palimpsest/local/bin/python` and
`/opt/palimpsest/hub/bin/python` are root-controlled interpreters. The candidate
uses `palimpsest-client 0.2.4` and Hub `0.2.1`, not the Kolla default Hub tag
`0.2.0` and not the already-published root `0.2.3`.

Candidate SSH config and alias are held in the private workdir. From an
authorized operator workstation, **after verifying the host key and access**:

```sh
WORKDIR="$HOME/.local/share/palimpsest-candidate-024"
ssh -F "$WORKDIR/ssh_config" -N -L 127.0.0.1:18020:127.0.0.1:8020 palimpsest-candidate-024
```

Leave that foreground process running for local `http://127.0.0.1:18020/app`
and `/v1` access; Ctrl-C closes only the tunnel. API port 8020 and SQL port
3306 are bound to **VM loopback**, Redis is not published, and local HTTP here
is inside the SSH forwarding boundary. Do not turn these into public listeners
or place tokens in URLs, command arguments, saved browser profiles or shell
history. Supply the project-scoped user token via the approved private input
path (browser tab memory or `PALIMPSEST_TOKEN` process environment), and the
CLI endpoint via `PALIMPSEST_URL=http://127.0.0.1:18020`; source Keystone
keys stay in protected input/env, never argv. Browser download links that
contain short-lived bearer tokens must not be shared or logged.

On the VM, service names are `palimpsest-candidate.service` (systemd oneshot
running `docker compose --env-file compose.env -f compose.yml up -d --wait
palimpsest-api palimpsest-worker` in `/etc/palimpsest-candidate`) and
`palimpsest-build-worker.service` (separate native KVM worker). Check active
units, mounted `/srv/h`, Compose bootstrap exit 0, API/export worker state,
MariaDB health, and the worker singleton before acting; use `docker compose
--env-file compose.env -f compose.yml ps` from that config directory without
printing resolved environment. `/v1/health` returning `{"status":"ok"}` is
**liveness only**, not SQL, Redis, auth, CAS, CLI or guest-build readiness.
The private `verify_readiness.py` documents stronger SQL `SELECT 1`, Redis
`PING`, UID/mount, loopback, disk and environment checks; inspect its scope,
do not output `/proc/*/environ`. The installed oneshot unit has **no
`ExecStop`**: stopping it alone leaves Compose containers running. Do not run
`docker compose down -v`; named/bind-mounted data must survive restart and
rollback.

### Consistent backup and isolated restore (procedure only; not executed)

These steps require an approved maintenance window and storage/SQL credentials;
there is **no** backup/restore proof in the candidate receipts. Restrict the
backup destination and transfer, encrypt it, record hashes and timestamps,
and test restoration on a separately isolated host before production use.

1. Verify `/srv/h` is the expected mounted data volume, not an empty mountpoint
   on the boot disk. Record source image IDs, package versions, current unit and
   container state, schema version, volume UUID and free space. Confirm **zero
   queued/building/exporting jobs and idle workers** (including any other
   writer, upload or garbage collector); do not merely trust `/v1/health`.
   Reject/defer new requests at the loopback/tunnel ingress, stop the native
   `palimpsest-build-worker.service` and stop API/export containers via Compose
   `stop` (not `down -v`). `systemctl stop palimpsest-candidate.service` is not
   a container stop. Recheck no writers remain and fail closed if a job or
   cleanup is ambiguous; never delete its private job tree.

   On the candidate host, once idleworker/no-new-ingress is established, the
   stop sequence is `sudo systemctl stop palimpsest-build-worker.service`,
   then, from `/etc/palimpsest-candidate`, `sudo docker compose --env-file
   compose.env -f compose.yml stop palimpsest-api palimpsest-worker`.
   Check the actual process/container states after each command; the still
   enabled units can restart services on reboot, so protect the maintenance
   window against an unexpected host restart. Do not stop MariaDB until after
   the logical dump (or before a deliberate cold full-volume snapshot).
2. With SQL still running and **no application writers**, take a
   transaction-consistent logical dump of the candidate `palimpsest` schema
   using credentials supplied by the protected Compose/container environment,
   **not** a password argument or saved shell history. Keep SQL credentials
   off stdout/stderr. Stop Redis cleanly after the workers are idle so its AOF
   is durable; copy mounted `/srv/h` (CAS blobs, metadata, private builds/
   uploads, Redis AOF/state) preserving numeric owners, permissions, symlinks
   and sparse files as appropriate, but **exclude `/srv/h/mysql` from this
   logical-dump restore set**. The SQL dump and CAS copy must span the **same
   no-writers interval**: a standalone SQL dump or live filesystem copy cannot
   prove referential consistency. `/srv/h/mysql` is MariaDB's live datadir and
   must not be copied as though it were a consistent cold snapshot. For a
   separate full-volume block snapshot, stop MariaDB and Redis cleanly first;
   restore that snapshot as a unit, not mixed with an unrelated SQL dump.
   Record dump/archive hashes, source volume identity and time, then verify no
   writer raced the snapshot.
3. On an isolated, empty replacement volume, restore the copied tree (without
   the old MariaDB datadir), preserving recorded numeric owners and exact modes.
   Hub CAS/build/upload files retain UID/GID 2001; Redis and database storage
   retain their own service identities, not a blanket recursive chown to 2001.
   Initialize a **new** MariaDB datadir/schema and import the matching SQL dump with credentials
   kept off argv/logs, and leave API/export/build workers stopped until SQL
   import and mounted CAS checks complete. Bring Redis up with its recovered
   AOF, check SQL and Redis, referenced blob digests and upload/job states,
   then start Compose API/export and the build-worker unit. If restoring a
   separately captured, consistently stopped whole-volume snapshot instead,
   do **not** import the logical dump on top of its MariaDB datadir; choose one
   database restore method. Inspect per-unit state, authenticated reads, blob
   digests and a disposable write/cleanup before reopening ingress. A health
   response alone does not certify restore. Retain the untouched backup and
   original volume until approval to retire.

For restart on the existing host after a verified backup, start the database
first if stopped; use `systemctl restart palimpsest-candidate.service` to
reissue its Compose `up` (a plain `start` of an already-active oneshot may do
nothing), then start the native build-worker unit. On reboot, the systemd
units require `/srv/h`; verify the mount and actual application readiness
again. Before any image rollback, record the **current candidate local** API
and worker image IDs in `artifact-receipt.json` and separately fetch the
**actual previous production immutable image digests** from production
deployment records. Those production previous digests are not in candidate
receipts and must never be guessed from candidate IDs, mutable tags or the
Kolla default. Retain volumes and SQL/CAS backup; switch both API and export
worker together only with schema compatibility reviewed, then verify the
restored path. No `down -v`, broad prune, destructive `install_host.py` rerun,
or production-ready claim follows from this candidate procedure.

## Upgrade and uninstall

Upgrade Local by reinstalling a newly reviewed SHA in the same environment:

```sh
PALIMPSEST_REF="NEW_FULL_40_CHARACTER_COMMIT_SHA"
"$HOME/.venvs/palimpsest/bin/python" -m pip install --upgrade \
  "palimpsest-client @ git+https://github.com/openstack-afterglow/palimpsest.git@${PALIMPSEST_REF}"
"$HOME/.venvs/palimpsest/bin/palimpsest" --version
```

Use the `[kvm]` selector again if that environment requires libvirt. Upgrade Hub
with the same ref and `#subdirectory=hub` selector only after its database and
worker compatibility have been reviewed.

```sh
"$HOME/.venvs/palimpsest/bin/python" -m pip uninstall palimpsest-client
"$HOME/.venvs/palimpsest-hub/bin/python" -m pip uninstall palimpsest-hub
```

Upgrading or removing Python code does not remove or migrate state, logs, VM
definitions, libvirt volumes, Docker data, BuildKit cache, or Lima disks. The
Linux provisioner has no uninstall mode; account and directory removal is an
explicit administrator operation.

## Contributor-only package build

Repository contributors can build the sdist/wheel and run the isolated package
smoke from a trusted checkout:

```sh
uv run python scripts/build_package.py --out-dir dist/package-0.3.1
```

This maintainer workflow is not the user installation path. Its local package
checks do not claim publication, signing, native KVM success, or arbitrary OCI
image compatibility.

See [platform compatibility](compatibility.md), [Linux storage and
logging](linux-storage-logging.md), [registry intake](registry-intake.md),
[Docker/OCI registry profiles](registries.md), [BuildKit workflow](buildkit-block-workflow.md),
and the [command workflows](cli/workflows.md) for operational details.
