# Install Palimpsest Local

Palimpsest Local requires Python 3.12 or newer and
[uv](https://docs.astral.sh/uv/) for the commands below. From a trusted checkout,
the repository currently builds
development packages (`0.1.0.dev0`); public `v0.1.0` publication is blocked by
the KVM release gate, so install a locally built wheel or an exact-SHA GitHub
development package rather than assuming it is available from PyPI.

After a successful development-package workflow run, download the exact
commit's development wheel, sdist, and checksum file:

```sh
SHA="FULL_40_CHARACTER_COMMIT_SHA"
BASE="https://github.com/openstack-afterglow/palimpsest/releases/download/package-${SHA}"
DOWNLOAD_DIR="$(mktemp -d)"
cd "$DOWNLOAD_DIR"
curl -fLO "${BASE}/palimpsest_local-0.1.0.dev0-py3-none-any.whl"
curl -fLO "${BASE}/palimpsest_local-0.1.0.dev0.tar.gz"
curl -fLO "${BASE}/SHA256SUMS"
sha256sum -c SHA256SUMS && \
  uv tool install --no-index ./palimpsest_local-0.1.0.dev0-py3-none-any.whl
```

Use a full reviewed SHA and require the checksum check to pass. A
`package-<SHA>` prerelease is not Latest, stable, PyPI-published, or native
KVM/Gate 2-qualified. The workflow never replaces an existing package tag.
If publication fails after creating the tag, automated reruns leave it in
place and fail; a maintainer must inspect and recover it explicitly.
On macOS, substitute `shasum -a 256 -c SHA256SUMS` for `sha256sum`.

Alternatively, build and install from a trusted checkout:

```sh
uv run python scripts/build_package.py --out-dir dist/package-0.1.0.dev0
uv tool install --no-index \
  dist/package-0.1.0.dev0/palimpsest_local-0.1.0.dev0-py3-none-any.whl
palimpsest --help
```

If the tool executable is not on `PATH`, use uv's documented shell setup or
invoke the environment's executable directly; changing shell startup files is
an explicit user action.

Choose a new output directory for each build. For development, use
`uv sync --extra dev`. Linux KVM users also need the platform tools and Python
binding described in [the detailed installation guide](docs/install.md); the
core wheel has no required Python dependencies.

On a shared Linux host, an administrator can keep the Python environment under
an administrator-owned path such as `/opt/palimpsest` and then explicitly run
the packaged `palimpsest_local.linux_install` module. That separate step creates
the no-login account and owner-only (`0700`) state/log directories. Package
installation itself never invokes sudo, changes groups, or installs sudoers
policy.

Upgrading or uninstalling the Python package does not remove Palimpsest state,
VM data, or logs. Review and preserve those assets separately; never pipe a
download into a privileged shell.
