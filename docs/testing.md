# Fast feedback without dropping coverage

The last complete baseline, commit `6cf198f`, passed 4,325 local tests and
4,360 server tests; the server run took 879.97 seconds. Repeating that entire
suite after every small change is no longer the default development loop.
Full regression remains a release/integration check, not a per-edit gate.

## Everyday workflow

```sh
uv run python scripts/test_lanes.py list --check
uv run python scripts/test_lanes.py plan --changed HEAD
uv run python scripts/test_lanes.py run --changed HEAD --dry-run
uv run python scripts/test_lanes.py run --changed HEAD
```

Before committing, `HEAD` compares the working tree, including untracked files,
with the last commit. After committing, use `HEAD~1` or the exact last tested
commit. Inspect the plan before running: its source dependency map is a
conservative recommendation, not a proof that every indirect dependency has
been discovered. Unknown production files and shared foundations select the
whole portable set. New unclassified test files fail manifest validation.

Use a named lane for a focused rerun, for example:

```sh
uv run python scripts/test_lanes.py run oci-monitor
uv run python scripts/test_lanes.py run oci-access
uv run python scripts/test_lanes.py run qualification
```

Tests remain in their existing modules; the runner changes selection, not
assertions, fixtures or production safety checks. A documentation-only edit
can produce an empty test recommendation; that is not a successful test run.
Hub tests use their own environment and explicit lane.

## Shards and full regression

```sh
uv run python scripts/test_lanes.py run portable --shard 1/6
uv run python scripts/test_lanes.py run portable --shard 2/6
# Repeat through 6/6 to cover the complete portable set.
uv run python scripts/test_lanes.py run full
```

`full` preserves the original opt-in skip behavior of `pytest tests`; a
successful result is not evidence that skipped native or product gates passed.

Sharding uses stable module/class/function identity and parameter indices,
not display node IDs that can contain freshly generated UUIDs. It can divide
a large module such as `test_oci_store.py` without moving its fixtures. A single shard
is only partial evidence; every shard for the same commit, platform and shard
count is needed to claim complete portable coverage. This is count-oriented
partitioning, not measured runtime balancing. Collection failures, duplicate
case identities and empty shards must not turn into successful evidence.

```sh
uv run python scripts/test_lanes.py run portable --collect-only
uv run python scripts/test_lanes.py run portable --collect-only --shard 1/6
```

Collection-only output is coverage inventory, not a test pass. Compare stable
case identities across fresh shard processes to prove disjoint complete
assignment; randomized display IDs are not suitable for that comparison.

CI runs all portable tests across six Linux shards and four macOS shards.
The existing aggregate check names remain, and require every shard to succeed;
a skipped or cancelled shard cannot satisfy them. Lint, manifest checks and
package construction run once. The release workflow still performs its broad
unit, build and native proof checks.

## Native and product gates

Special lanes are explicit: `native-live`, `guest-kvm`, `guest-binary`,
`filesystem`, `gate1`, `gate2`. They retain their original prerequisites and
must not be implicitly activated by changed-file selection. The plan reports
suggested special verification separately; a still-disabled product gate is
not made runnable by a recommendation. Portable selection excludes special
nodes even if an opt-in environment variable happens to be set.

For the qualified `pieroot-server` libvirt lane, after pushing and checking out
the exact tested SHA:

```sh
env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/usr/lib/python3/dist-packages:src \
  PALIMPSEST_REQUIRE_OCI_ROOT_LIBVIRT=1 \
  PALIMPSEST_KVM_KERNEL=/home/pieroot/palimpsest-kvm-evidence/kernel-6.6.71.bzImage \
  PALIMPSEST_KVM_KERNEL_CONFIG=/home/pieroot/palimpsest-kvm-evidence/kernel-6.6.71.config \
  .venv/bin/python scripts/test_lanes.py run native-live
```

Ordinary server lanes use `env -u PYTHONPATH`, `PYTHONDONTWRITEBYTECODE=1` and
`umask 022`, as the previous full baseline did. Never run fixture mutations
against an existing user VM or delete retained failure evidence to speed up a
test. A native change still requires its actual native proof; a focused unit
pass is not a replacement.

The coordinated native case additionally requires the invoking Python to import
both the installed project and libvirt normally: clean product children do not
inherit `PYTHONPATH`. Use a qualified system-Python venv with
`--system-site-packages` and the current project installed, and unset
`PYTHONPATH`. Its targeted selector is
`tests/kvm/test_oci_root_libvirt_live.py::test_live_oci_root[True-True-True-True]`.
This exercises fresh coordinator, live console, separate-client STOP, cleanup
and retained-root reuse in one case; it is not the public build-to-run Gate 2.

Gate 1 verifies the Palimpsest local OCI build. Gate 2 retains the full public
`run -d → exec → stop → rm` contract on a qualified KVM host; Docker may coexist. Test
partitioning neither enables Gate 2 nor changes its acceptance criteria.

## When to broaden verification

The read-only NPROC diagnostic has a small independent feedback loop:

```sh
uv run pytest -q tests/unit/test_oci_resource_status.py tests/unit/test_oci_public_cli.py tests/unit/test_test_lanes.py
```

See [the advisory contract](oci-resource-status.md). Its fixtures cover actual
descriptor-based reads, partial/invalid observations and no state creation or
worker launch. Changes to the shared worker limit constant additionally select
worker/store coverage and require the separate cold public exec proof below;
the diagnostic alone does not require rebuilding the unchanged guest or
rerunning the full guest boot matrix.

For worker/packer resource failures and additional-exec diagnostics, keep the
two feedback loops separate:

```sh
uv run pytest -q tests/unit/test_oci_worker_protocol.py tests/unit/test_oci_converter_first_pass.py
uv run pytest -q tests/unit/test_oci_exec_session.py tests/unit/test_oci_exec_control.py tests/unit/test_oci_exec_client.py tests/unit/test_oci_exec_public_routing.py
```

The worker file includes real bounded subprocess failure cases; the converter
file contains Linux-only pinned-packer cases. macOS skips do not verify those
Linux paths: run the selected files on the exact pushed server SHA too.
After changes to these paths, run the separate public exec CLI native file
below with the existing immutable Palimpsest-built image. This exercises real
cold materialization and public run/exec/stop/rm without running the entire
native suite. It still does not substitute for Gate 2.

The first public OCI lifecycle has a separately addressable native smoke:
`tests/kvm/test_oci_public_cli_live.py`. Set `PALIMPSEST_OCI_PUBLIC_CLI_LIVE=1`
and the five explicit host BOOT/packer variables documented in
[the runtime roadmap](oci-public-runtime-roadmap.md). Use a normal Python
environment with libvirt importable without `PYTHONPATH`. The old native-live
flag alone does not enable this new proof. Its successful run checks foreground,
detached, Ctrl-C and completed cleanup, not additional guest exec or Gate 2.
New host/request/adapter/CLI unit files are in host-runtime; normal removal is
in oci-monitor. Each file can also be run directly for a smaller edit loop.

Additional exec has two separately selectable files:
`tests/kvm/test_oci_exec_live.py` uses the production exec engine and requires
`PALIMPSEST_OCI_EXEC_LIVE=1`; `tests/kvm/test_oci_exec_cli_live.py` uses only public
commands and requires `PALIMPSEST_OCI_EXEC_CLI_LIVE=1`. Both require
`PALIMPSEST_OCI_EXEC_LIVE_IMAGE` pointing to the Palimpsest-built archive and
adjacent acceptance receipt, plus the host BOOT settings. Run either file on its
own; neither silently enables the other or substitutes for Gate 2. New exec
protocol/mailbox/IPC/session/routing unit files belong to oci-monitor, while the
actual guest C harness belongs to oci-guest with its own platform prerequisites.

- Per edit: relevant lane(s), regression tests for the change, lint and format.
- Before push: inspect the changed-file plan and run the affected portable
  lanes; include the required native/build proof for changed runtime surfaces.
- Integration/release boundary: all portable shards, applicable privileged
  and product gates, and the existing release regression policy.
- Uncertain impact, shared state/schema foundations or unexplained failures:
  broaden to all portable tests or explicit full regression.

Keep the SHA, selected lanes/shards, platform, pass/skip counts and elapsed time
with each result. Do not report a selected subset as a full regression.
