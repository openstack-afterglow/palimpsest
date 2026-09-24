# Install Palimpsest

Palimpsest requires Python 3.11 or newer. The root `palimpsest-local` release
target is `0.2.0`, not a claim that a tag or PyPI package has been published.
Git and outbound HTTPS access to GitHub are required for direct VCS installation.
Hub's independently versioned Python package (`palimpsest-hub`) is also `0.2.0`
in this source tree; a repository release tag does not publish that wheel.

## 1. Install the Local CLI

Install the default branch into an active Python 3.11+ environment with pip:

```sh
python3.12 -m pip install \
  "git+https://github.com/openstack-afterglow/palimpsest"
palimpsest --version
```

For a standalone CLI on a host whose system Python is older, let uv own the
compatible interpreter and tool environment:

```sh
uv python install 3.11
uv tool install --python 3.11 \
  "palimpsest-local @ git+https://github.com/openstack-afterglow/palimpsest"
palimpsest --version
```

To add Palimpsest to a new uv project, declare a compatible project range at
initialization time:

```sh
uv python install 3.11
uv init --bare --python 3.11
uv python pin 3.11
uv add "git+https://github.com/openstack-afterglow/palimpsest"
uv run python -c "import importlib.metadata as m; print(m.version('palimpsest-local'))"
uv run palimpsest --version
```

If an existing `pyproject.toml` says `requires-python = ">=3.10"`, raise it to
`">=3.11"` before `uv add`, or keep the project range and use `uv tool install`
instead. uv resolves dependencies for the entire declared range, not only the
pinned interpreter. `--frozen` is not a compatibility fix. A successful
metadata probe for an installed 0.2.0 root package must report `0.2.0`;
before promotion, the unpinned default branch may still resolve an older
version. Output from a same-named local application does not verify that
`palimpsest-local` was installed.

For a reproducible install, append a reviewed full commit SHA to the Git URL:

```sh
PALIMPSEST_REF="FULL_40_CHARACTER_COMMIT_SHA"
python3.12 -m pip install \
  "git+https://github.com/openstack-afterglow/palimpsest@${PALIMPSEST_REF}"
uv add \
  "git+https://github.com/openstack-afterglow/palimpsest@${PALIMPSEST_REF}"
```

The unpinned form follows the repository default branch. uv records the Git
source in `pyproject.toml` and locks the resolved commit in `uv.lock`.

## 2. Select the package variant

The base `palimpsest-local` distribution provides the `palimpsest` CLI and has
no required Python runtime dependencies. Install the `[kvm]` extra only when the
selected backend needs libvirt:

```sh
"$HOME/.venvs/palimpsest/bin/python" -m pip install --upgrade \
  "palimpsest-local[kvm] @ git+https://github.com/openstack-afterglow/palimpsest.git@${PALIMPSEST_REF}"
```

`[kvm]` adds `libvirt-python>=10.0.0`. It does not install or configure QEMU,
libvirt, `/dev/kvm`, firmware, networks, host permissions, or the experimental
macOS HVF stack.

Install the standalone Hub in a separate environment from the same reviewed ref:

```sh
python3.12 -m venv "$HOME/.venvs/palimpsest-hub"
"$HOME/.venvs/palimpsest-hub/bin/python" -m pip install --upgrade pip
"$HOME/.venvs/palimpsest-hub/bin/python" -m pip install \
  "palimpsest-hub @ git+https://github.com/openstack-afterglow/palimpsest.git@${PALIMPSEST_REF}#subdirectory=hub"
"$HOME/.venvs/palimpsest-hub/bin/python" -m pip show palimpsest-hub
```

Hub is a server distribution with FastAPI, OpenStack, Redis, SQLAlchemy, and
Uvicorn dependencies. Its service entrypoints require database, Redis, local
storage, and OpenStack settings before startup.

The repository `v0.2.0` tag, if created, also drives GHCR tags `0.2.0` and
`v0.2.0` for both `ghcr.io/openstack-afterglow/palimpsest-hub-api` and
`ghcr.io/openstack-afterglow/palimpsest-hub-worker`; these are image labels,
not the Hub wheel version. The Kolla role in this source tree defaults to Hub
image tag `0.2.0`, which requires both images to be published and verified
before deployment. Root PyPI publishing requires the tag-triggered
artifact checks and a successful native stage-1 proof on an enabled self-hosted
Linux x86_64 KVM runner; development packages and portable tests alone are
not sufficient. No 0.2.0 image/package release or Kolla deployment is claimed
by these instructions.

The Hub serves `/app` for browser upload/search/download and system-admin-only
server builds. The API package alone cannot run a build: install
`palimpsest-local[kvm]` at the same reviewed ref on a **separate Linux x86_64
KVM host**, configure the absolute `PALIMPSEST_HUB_BUILDER_PYTHON` interpreter
path on API and worker, and run `palimpsest-hub-build-worker` with the same SQL
database and Hub blob filesystem path. See [Hub service settings](docs/install.md#hub-service-settings)
for prerequisites and failure boundaries.

## 3. Match the supported platform

| Host/runtime | Support | Python package | External prerequisites |
| --- | --- | --- | --- |
| macOS Apple Silicon, Lima/VZ | Supported default | `palimpsest-local` | Lima 2.1+ |
| macOS Apple Silicon, libvirt/HVF | Experimental | `palimpsest-local[kvm]` | QEMU, libvirt, working `qemu:///session` |
| Linux `x86_64`, conventional cloud image | Supported | `palimpsest-local[kvm]` | KVM, QEMU, libvirt, EFI, `qemu-img`, `cloud-localds`, SquashFS, OpenSSH |
| Linux `aarch64`, conventional cloud image | Supported | `palimpsest-local[kvm]` | KVM, QEMU, libvirt, EFI, `qemu-img`, `cloud-localds`, SquashFS, OpenSSH |
| Linux `x86_64`/`amd64`, OCI-root | Supported narrow runtime | `palimpsest-local[kvm]` | Qualified KVM host, runtime kernel/initramfs/packer, Skopeo 1.13+ for registry acquisition |
| Standalone Hub | Service role, not a VM backend | `palimpsest-hub` | MySQL-compatible database, Redis, OpenStack credentials, local blob path |
| Hub server build worker | Separately provisioned service role | `palimpsest-hub` + `palimpsest-local[kvm]` at the same ref | Linux x86_64 KVM/libvirt and conventional cloud-image build tools; shared Hub DB/blob path |

The native KVM release gate covers the OCI-root guest stage-1, not the revised
conventional cloud-image readiness path. That path's serial-bound first boot
and cloud-init-disabled reboot still lack a post-merge native boot check;
installing `[kvm]` or passing the stage-1 gate does not close the gap.

Package installation never installs hypervisors, creates system accounts,
changes privileged groups, writes sudoers policy, or initializes mutable state.

## 4. Configure the first state root

An unconfigured Linux process selects `/var/lib/palimpsest`, which is normally
administrator-owned. For a user-owned artifact workflow, select a private state
parent before running stateful commands:

```sh
export XDG_STATE_HOME="$HOME/.local/state"
"$HOME/.venvs/palimpsest/bin/palimpsest" store show
```

`PALIMPSEST_STATE_HOME` is the highest-priority state override. Hub credentials,
registry credentials, and host permissions are separate settings; do not place
secrets in repository files.

## 5. Upgrade or remove code

Upgrade by reinstalling a newly reviewed SHA into the same isolated environment:

```sh
PALIMPSEST_REF="NEW_FULL_40_CHARACTER_COMMIT_SHA"
"$HOME/.venvs/palimpsest/bin/python" -m pip install --upgrade \
  "palimpsest-local @ git+https://github.com/openstack-afterglow/palimpsest.git@${PALIMPSEST_REF}"
"$HOME/.venvs/palimpsest/bin/palimpsest" --version
```

Remove only the Python environment when uninstalling:

```sh
"$HOME/.venvs/palimpsest/bin/python" -m pip uninstall palimpsest-local
```

Installing, upgrading, or uninstalling Python code does not remove or migrate
Palimpsest state, VM definitions, volumes, Docker data, Lima disks, or logs.

See the [detailed installation and configuration guide](docs/install.md),
[platform compatibility matrix](docs/compatibility.md), and
[command workflows](docs/cli/workflows.md). Repository contributors should use
the checkout-specific development procedure in [README.md](README.md#development),
not this end-user installation path.
