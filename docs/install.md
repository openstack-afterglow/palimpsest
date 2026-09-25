# Detailed installation and configuration guide

This guide covers end-user installation from PyPI or a GitHub VCS URL,
package selection, supported host/runtime combinations, external dependencies,
initial configuration, upgrades, and administrator-owned Linux deployment.
It does not use a repository checkout as the user installation mechanism.

Palimpsest Local requires Python 3.11 or newer. The root distribution
[`palimpsest-client 0.2.3`](https://pypi.org/project/palimpsest-client/0.2.3/)
is published on PyPI. The independently versioned Hub distribution remains
`palimpsest-hub 0.2.0` in this tree. Git and outbound HTTPS access to GitHub
are required only for the direct VCS installation examples below.

Install the published CLI with `python3.12 -m pip install "palimpsest-client==0.2.3"`,
then run `palimpsest --version`. The isolated invocation
`uvx --from palimpsest-client==0.2.3 palimpsest --version` was verified against
the published wheel and reports `0.2.3`.

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
Kolla-Ansible independently. This source role defaults to Hub image tag `0.2.0`,
while source builds bind to the configured checkout commit SHA. Verify both
`0.2.0` Hub images before deploying this default; source metadata
alone does not establish image availability.

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

The API/export worker still need the Hub Redis and Keystone settings. The build
worker only reads `DATABASE_URL`, the database pool settings,
`PALIMPSEST_HUB_LOCAL_PATH`, `PALIMPSEST_HUB_MAX_BLOB_BYTES`,
`PALIMPSEST_HUB_BUILD_TIMEOUT_SECONDS`, and
`PALIMPSEST_HUB_BUILDER_PYTHON`; it does not require Redis or `OS_*` credentials.
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

- An installed Docker CLI for `login`, `pull`, `push`, `tag`, `images`,
  `history`, `rmi`, `save`, `load`, image aliases, and `docker` pass-through.
- A separately managed Buildx builder with an OCI exporter for Dockerfile
  builds. The default `docker` driver does not provide the required exporter.
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

Hub commands require `PALIMPSEST_TOKEN`. Keep the token in a secret manager or
process environment; never persist it in `config.toml`, documentation, shell
history, or the local state store.

```sh
export PALIMPSEST_URL="https://hub.example.invalid"
# Inject PALIMPSEST_TOKEN through the approved secret mechanism.
palimpsest image ls --limit 10
```

### Registry profiles and credentials

Registry profiles live at
`${XDG_CONFIG_HOME:-~/.config}/palimpsest/registries.toml` and contain no
credentials. For unqualified references, selection order is:

1. Registry written in the image reference.
2. Command `--registry PROFILE`.
3. `PALIMPSEST_REGISTRY`.
4. Configured default profile.

Credentials remain in Docker's `DOCKER_CONFIG` directory or `$HOME/.docker`.
Use `login --password-stdin` for non-interactive authentication.

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
| `OS_USERNAME` / `OS_PASSWORD` | Service identity credentials |
| `OS_PROJECT_NAME` | Service project |

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
| `OS_USER_DOMAIN_NAME` / `OS_PROJECT_DOMAIN_NAME` | `Default` |
| `OS_REGION_NAME` | `RegionOne` |
| `OS_INTERFACE` | `internal` |
| `SSL_VERIFY` | `true` |

After provisioning dependencies and injecting secrets, initialize the schema
with `palimpsest-hub-bootstrap`, then run `palimpsest-hub` and
`palimpsest-hub-worker` as separately supervised processes. To enable
server-side builds, run `palimpsest-hub-build-worker` on the KVM host with the
same `DATABASE_URL` and the same **absolute** `PALIMPSEST_HUB_LOCAL_PATH`
filesystem view as the API. The worker takes one filesystem singleton lock and
polls queued jobs; a second worker for the same store is rejected. Bootstrap
creates `palimpsest_hub_builds`; data migration additionally copies build rows
and project layer grants when present in the source. Migration remains separate
from schema bootstrap.

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
uv run python scripts/build_package.py --out-dir dist/package-0.2.3
```

This maintainer workflow is not the user installation path. Its local package
checks do not claim publication, signing, native KVM success, or arbitrary OCI
image compatibility.

See [platform compatibility](compatibility.md), [Linux storage and
logging](linux-storage-logging.md), [registry intake](registry-intake.md),
[Docker/OCI registry profiles](registries.md), [BuildKit workflow](buildkit-block-workflow.md),
and the [command workflows](cli/workflows.md) for operational details.
