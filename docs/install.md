# Detailed installation guide

Palimpsest Local is a Python 3.12+ CLI. Its base distribution has no required
Python dependencies. The checkout version is `0.1.0.dev0`; public `v0.1.0`
publication remains blocked pending the documented physical Linux KVM release
gate. Build and install a local artifact rather than assuming PyPI availability.

## Build and verify distributions

From a trusted checkout with [uv](https://docs.astral.sh/uv/) available:

```sh
uv run python scripts/build_package.py --out-dir dist/package-0.1.0.dev0
```

The output directory must not already exist; choose a new path for a rebuild.
The command builds an sdist, builds the wheel from that sdist, validates every
archive and checks the sealed `oci-stage1-init.x86_64` ELF byte-for-byte. It then
creates a fresh environment, installs with `--no-deps --no-index`, invokes the
runtime ELF validator under isolated Python, runs installed CLI help away from
the checkout, and writes sorted `SHA256SUMS`. A commit-derived
`SOURCE_DATE_EPOCH` stabilizes archive timestamps; it does not imply that
different source trees produce identical packages.

Use another uv executable when PATH is intentionally restricted:

```sh
python3 scripts/build_package.py --out-dir /tmp/palimpsest-package \
  --uv /absolute/path/to/uv
```

This is a package smoke test, not publication, signing, or native KVM proof.

## Developer or user-private installation

Install the wheel into an isolated tool environment:

```sh
uv tool install --no-index \
  dist/package-0.1.0.dev0/palimpsest_local-0.1.0.dev0-py3-none-any.whl
palimpsest --help
```

On Linux, an unconfigured process selects `/var/lib/palimpsest`, which a normal
user generally cannot create. For a user-private artifact-only setup, select an
explicit private state parent before stateful commands:

```sh
export XDG_STATE_HOME="$HOME/.local/state"
palimpsest store show
```

That does not provision the dedicated system identity and is not a substitute
for administrator review of KVM/libvirt access. For checkout development use
`uv sync --extra dev`; installing the package does not alter shell startup files.

## Optional runtime prerequisites

The package does not install hypervisors or modify host permissions.

- Linux KVM/libvirt needs the `kvm` extra (`libvirt-python>=10.0.0`) and the
  host's QEMU, libvirt, `qemu-img`, `cloud-localds`, SquashFS, OpenSSH, and
  filesystem tools. `/dev/kvm`, firmware, network, and libvirt access must
  already be configured. OCI-root qualification is currently Linux x86_64.
- macOS Apple Silicon uses separately installed Lima/VZ by default. The
  experimental libvirt/HVF backend additionally needs QEMU, libvirt, and the
  `kvm` Python extra.
- Dockerfile workflows need a separately managed Docker Buildx builder.

Installing the optional Python dependency may require libvirt development
headers and an approved package index. The base-wheel offline installation
deliberately does not resolve it.

In an environment where the host libraries and approved dependency source are
already configured, install the wheel with its extra explicitly:

```sh
python3.12 -m venv .venv-kvm
.venv-kvm/bin/python -m pip install \
  'dist/package-0.1.0.dev0/palimpsest_local-0.1.0.dev0-py3-none-any.whl[kvm]'
.venv-kvm/bin/palimpsest --help
```

This can contact the configured index for `libvirt-python`; it is intentionally
separate from the offline base-wheel smoke test.

## 3. BuildKit Requirements for Dockerfile Builds

Dockerfile builds require an existing OCI-export-capable Buildx builder. The
default `docker` driver cannot provide that exporter; select a separately
managed `docker-container`, `kubernetes`, or `remote` builder. Palimpsest checks
the selected builder but does not create or reconfigure it. Strict offline
builds additionally require a previously bootstrapped, single-node local
`docker-container` builder using `--driver-opt network=none` and locally
available pinned inputs. See the [BuildKit cache and runtime-block
workflow](buildkit-block-workflow.md) for creation commands, network checks,
cache rules, and registry configuration.

## Administrator-owned Linux installation

For a single-operator service host, keep installed code separate from mutable
assets. An administrator-owned virtual environment is one possible pattern:

```sh
cd /
sudo python3.12 -I -m venv /opt/palimpsest
sudo /opt/palimpsest/bin/python -I -m pip install --no-index --no-deps \
  /trusted/path/palimpsest_local-0.1.0.dev0-py3-none-any.whl
```

Use an approved local environment installer if that Python lacks `venv` or
pip. Verify the environment and wheel are administrator-owned and not writable
by the service identity. Never run privileged commands from an editable user
checkout, an untrusted current directory, or a download-to-`sudo` pipeline.

Package installation creates no account or system directory. After reviewing
the deployment identity, explicitly invoke the packaged provisioner:

```sh
cd /
sudo /opt/palimpsest/bin/python -I -m palimpsest_local.linux_install
```

It creates the no-login `palimpsest` user and primary group, then
`/var/lib/palimpsest` and `/var/log/palimpsest`, owned by that identity with
mode `0700`. Matching existing objects are accepted; conflicts and symlinks are
rejected. It never recursively changes ownership, migrates or removes data,
adds privileged group memberships, or writes sudoers policy. A failed account
command can leave partial setup that requires administrator inspection. Group
ownership is descriptive: mode `0700` does not grant shared writes.

Run management commands under that identity with inherited root overrides
removed, for example:

```sh
sudo -H -u palimpsest env -u PALIMPSEST_STATE_HOME -u PALIMPSEST_LOG_HOME \
  -u XDG_STATE_HOME -u XDG_CONFIG_HOME \
  /opt/palimpsest/bin/python -I -m palimpsest_local.cli store show
```

Any KVM, libvirt, or Docker access is a separate host-policy decision; the
installer does not grant it.

The command journal defaults to `/var/log/palimpsest` for this system setup. A
user-private Linux process cannot write there and reports a fixed warning while
continuing the requested command. To retain a private journal, pre-create a
private directory with mode `0700` and select its absolute path explicitly:

```sh
install -d -m 0700 "$HOME/.local/state/palimpsest-log"
export PALIMPSEST_LOG_HOME="$HOME/.local/state/palimpsest-log"
```

## Upgrade and uninstall

Build into a new directory and smoke-test the new wheel first. Stop workloads
according to local operations policy, replace only the package in the same
isolated Python environment, then run CLI and relevant runtime checks.

For a uv tool installation, upgrade and uninstall the code explicitly:

```sh
uv tool install --force --no-index /path/to/new/palimpsest_local-*.whl
uv tool uninstall palimpsest-local
```

Upgrading or removing Python code does **not** remove or migrate
`/var/lib/palimpsest`, `/var/log/palimpsest`, explicit XDG state, VM definitions,
volumes, Docker data, or Lima-managed disks. Preserve and inspect those assets
before changing the environment. The provisioner has no uninstall mode;
account and directory removal is an explicit administrator operation outside
package management.

See [Linux storage and logging](linux-storage-logging.md) for state and journal
contracts and the [VM workflow guide](vm-workflow.md) for runtime prerequisites.
