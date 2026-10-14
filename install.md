# Install Palimpsest

Palimpsest requires Python 3.11 or newer. The current release is
[`palimpsest-client 0.3.5`](https://pypi.org/project/palimpsest-client/0.3.5/),
released on 2026-10-14. Install released bytes in isolation:

```sh
uv tool install --python 3.11 "palimpsest-client==0.3.5"
palimpsest --version

# Or use pip, without changing system Python:
python3.11 -m venv .venv
.venv/bin/python -m pip install "palimpsest-client==0.3.5"
.venv/bin/palimpsest --version
```

Released 0.3.5 includes the integrated local web, runtime, cache and
user-default/help/completion work. To use a trusted checkout's exact bytes,
run `uv tool install --force --python 3.11 .`, or `python -m pip install .`
inside a virtual environment. Remote branch/PyPI installation does not include
uncommitted edits. Do not infer source identity from the version alone.
If uv's bin directory is not on `PATH`, explicitly run `uv tool update-shell`
and open a new terminal. Hub is a separate distribution/deployment; a client
upgrade does not publish or roll out Hub images.

Release `v0.3.5` follows protected-dev PR #24 and merge
`04d51ad6e1bf00c4b6e5bf82050383d0bb0a10cf`. PyPI/GHCR publication and
production Hub on `dms-controller1` plus the dedicated build-worker VM rollout
are recorded in [release receipts](docs/testing.md#full-source-035-integration--local-verification).
Native KVM proof was NOT performed: repository-policy skip, exactly as for 0.3.4.

## 1. Install the Local CLI

Install the default branch into an active Python 3.11+ environment with pip:

```sh
python3.12 -m pip install \
  "palimpsest-client @ git+https://github.com/openstack-afterglow/palimpsest"
palimpsest --version
```

For a standalone CLI on a host whose system Python is older, let uv own the
compatible interpreter and tool environment:

```sh
uv python install 3.11
uv tool install --python 3.11 \
  "palimpsest-client @ git+https://github.com/openstack-afterglow/palimpsest"
palimpsest --version
```

To add Palimpsest to a new uv project, declare a compatible project range at
initialization time:

```sh
uv python install 3.11
uv init --bare --python 3.11
uv python pin 3.11
uv add "palimpsest-client @ git+https://github.com/openstack-afterglow/palimpsest"
uv run python -c "import importlib.metadata as m; print(m.version('palimpsest-client'))"
uv run palimpsest --version
```

If an existing `pyproject.toml` says `requires-python = ">=3.10"`, raise it to
`">=3.11"` before `uv add`, or keep the project range and use `uv tool install`
instead. uv resolves dependencies for the entire declared range, not only the
pinned interpreter. `--frozen` is not a compatibility fix. A successful
metadata probe must match the selected distribution version.
The unpinned Git URL follows the default branch and can resolve a different
version. Output from a same-named local application does not verify that
`palimpsest-client` was installed.

For a reproducible install, append a reviewed full commit SHA to the Git URL:

```sh
PALIMPSEST_REF="FULL_40_CHARACTER_COMMIT_SHA"
python3.12 -m pip install \
  "palimpsest-client @ git+https://github.com/openstack-afterglow/palimpsest@${PALIMPSEST_REF}"
uv add \
  "palimpsest-client @ git+https://github.com/openstack-afterglow/palimpsest@${PALIMPSEST_REF}"
```

The unpinned form follows the repository default branch. uv records the Git
source in `pyproject.toml` and locks the resolved commit in `uv.lock`.

## 2. Select the package variant

The base `palimpsest-client` distribution provides the `palimpsest` CLI and has
no required Python runtime dependencies. Install the `[kvm]` extra only when the
selected backend needs libvirt:

```sh
"$HOME/.venvs/palimpsest/bin/python" -m pip install --upgrade \
  "palimpsest-client[kvm] @ git+https://github.com/openstack-afterglow/palimpsest.git@${PALIMPSEST_REF}"
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

Repository tags also drive GHCR image tags for
`ghcr.io/openstack-afterglow/palimpsest-hub-api`,
`ghcr.io/openstack-afterglow/palimpsest-hub-worker` and
`ghcr.io/openstack-afterglow/palimpsest-hub-build-worker`; image tags follow the
repository tag, not the independently versioned Hub wheel. The released Kolla
role defaults to Hub image tag `0.3.5`; verify exact reviewed API/export/build-worker
digests and platform manifests before any future deployment.
Root publication requires trusted tag artifact checks and protected PyPI
publisher gates. Native policy permits only enabled/success or explicitly
disabled/skipped; disabled publication is **not native qualification**.
Native environment approval is separate from prior branch approval; the old
persistent runner stays offline. See [release prerequisites](docs/testing.md#release-preparation-and-publication-gates).

The Hub serves `/app` for browser upload/search/download and system-admin-only
server builds. API packages never execute recipes. Configure its private worker
key and run the same-source `palimpsest-hub-build-worker` in a dedicated CPU-only
Linux x86_64 service VM with verified Hub HTTPS, owner-only keyfile and private
scratch. Do not supply SQL/CAS/Redis/Keystone credentials or resurrect the old
KVM/shared-store interpreter setting. See [worker deployment and recovery](docs/install.md#remote-build-worker-deployment-034-source-candidate)
for tools, resource/disk bounds, fixed deadlines, journal/no-replay and exact-owned cleanup.

## 3. Match the supported platform

| Host/runtime | Support | Python package | External prerequisites |
| --- | --- | --- | --- |
| macOS Apple Silicon, Lima/VZ | Supported default | `palimpsest-client` | Lima 2.1+ |
| macOS Apple Silicon, libvirt/HVF | Experimental | `palimpsest-client[kvm]` | QEMU, libvirt, working `qemu:///session` |
| Linux `x86_64`, conventional cloud image | Supported | `palimpsest-client[kvm]` | KVM, QEMU, libvirt, EFI, `qemu-img`, `cloud-localds`, SquashFS, OpenSSH |
| Linux `aarch64`, conventional cloud image | Supported | `palimpsest-client[kvm]` | KVM, QEMU, libvirt, EFI, `qemu-img`, `cloud-localds`, SquashFS, OpenSSH |
| Linux `x86_64`/`amd64`, OCI-root | Supported narrow runtime | `palimpsest-client[kvm]` | Qualified KVM host, runtime kernel/initramfs/packer, Skopeo 1.13+ for registry acquisition |
| Standalone Hub | Service role, not a VM backend | `palimpsest-hub` | MySQL-compatible database, Redis, OpenStack credentials, local blob path |
| Hub server build worker | Separately provisioned service role | `palimpsest-hub` + `palimpsest-client[kvm]` at the same ref | Linux x86_64 KVM/libvirt and conventional cloud-image build tools; shared Hub DB/blob path |

The native KVM release gate covers the OCI-root guest stage-1, not the revised
conventional cloud-image readiness path. That path's serial-bound first boot
and cloud-init-disabled reboot still lack a post-merge native boot check;
installing `[kvm]` or passing the stage-1 gate does not close the gap.

Package installation never installs hypervisors, creates system accounts,
changes privileged groups, writes sudoers policy, or initializes mutable state.

## 4. Start using the installed CLI

The working source uses `~/.local/state/palimpsest` for ordinary Linux users
and macOS; no state variables are needed. Existing user artifacts and VM ledgers
stay there. Linux normal-user commands automatically create a private journal
under the selected state's `logs/`. Root and the dedicated `palimpsest` account
retain administrator-provisioned `/var/lib/palimpsest` and `/var/log/palimpsest`.
See [storage/logging](docs/linux-storage-logging.md) for override/security policy.

```sh
palimpsest                           # help and examples, no required-operation error
palimpsest store show
palimpsest store ls --kind image     # local inventory; no Hub credentials
palimpsest completion zsh --install # choose bash/fish if appropriate; reopen shell
palimpsest docker run --help         # actual installed Docker help
```

`image ls` queries Hub and still needs a Hub URL/token. `images` queries the
Docker daemon. These are not aliases for the local Palimpsest store.

`PALIMPSEST_STATE_HOME` is the highest-priority state override. Hub credentials,
registry credentials, and host permissions are separate settings; do not place
secrets in repository files.

## 5. Build and run locally or on Linux

For ordinary Docker containers, the installed Docker CLI and running daemon are
required. The passthrough preserves Docker's help, plugin commands and flags:

```sh
palimpsest docker build -t demo:local .
palimpsest docker run --rm demo:local
```

This runs a Docker container, not a Palimpsest VM. `palimpsest build` is the
separate artifact workflow: online Dockerfile builds require a native cache
profile/key; strict offline builds require a preprovisioned network-none Buildx
builder and pinned local OCI inputs. Follow the
[BuildKit workflow](docs/buildkit-block-workflow.md), not an implicit Hub fallback.

For a **Palimpsest VM**, first install Lima on macOS Apple Silicon, or the
documented KVM/libvirt tools and `[kvm]` extra on Linux. Download and verify an
architecture-matching Ubuntu cloud image using the
[Hello VM guide](examples/hello-vm/README.md#step-1-download-an-ubuntu-cloud-image).
Then use the installed CLI directly (no source `uv run` environment required):

```sh
ARCH=aarch64 # macOS Apple Silicon/Linux ARM64; Linux x86_64: ARCH=x86_64
BASE=$(palimpsest image import ./ubuntu.qcow2 --disk-format qcow2 --arch "$ARCH")
cat > Palimpsestfile <<EOF
FROM $BASE
RUN mkdir -p /srv/demo && printf 'hello from the built layer\n' > /srv/demo/message
EOF
LAYER=$(palimpsest build --frontend palimpsestfile --base "$BASE" \
  --tag demo-layer --file ./Palimpsestfile --network none)
palimpsest run "$BASE" --layer "$LAYER" --name demo-vm --memory 2048 --vcpus 2
palimpsest exec demo-vm -- cat /opt/layers/merged/srv/demo/message
palimpsest stop demo-vm
palimpsest rm demo-vm --volumes
```

Use a new run name, not an existing VM's name. Layers mount under
`/opt/layers/merged`; this conventional VM flow does not replace guest `/`.
OCI-root application launch is a distinct Linux amd64 KVM lane, not enabled by
Docker installation or a successful macOS build.

## 6. Upgrade or remove code

Upgrade by reinstalling a newly reviewed SHA into the same isolated environment:

```sh
PALIMPSEST_REF="NEW_FULL_40_CHARACTER_COMMIT_SHA"
"$HOME/.venvs/palimpsest/bin/python" -m pip install --upgrade \
  "palimpsest-client @ git+https://github.com/openstack-afterglow/palimpsest.git@${PALIMPSEST_REF}"
"$HOME/.venvs/palimpsest/bin/palimpsest" --version
```

Remove only the Python environment when uninstalling:

```sh
"$HOME/.venvs/palimpsest/bin/python" -m pip uninstall palimpsest-client
```

Installing, upgrading, or uninstalling Python code does not remove or migrate
Palimpsest state, VM definitions, volumes, Docker data, Lima disks, or logs.

See the [detailed installation and configuration guide](docs/install.md),
[platform compatibility matrix](docs/compatibility.md), and
[command workflows](docs/cli/workflows.md). Repository contributors should use
the checkout-specific development procedure in [README.md](README.md#development),
not this end-user installation path.
