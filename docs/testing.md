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

### Linux legacy ArgsEscaped

For the Linux-only [process metadata contract](oci-linux-process.md), start
with `tests/unit/test_oci_process.py tests/unit/test_oci_source.py`. Before push,
also select `test_oci_image.py`, `test_oci_layout.py`, `test_oci_run_request.py`,
`test_oci_run_adapter.py`, `test_oci_store.py`,
`test_oci_docker_hub_cli_live_contract.py` and `test_architecture_guard.py`
from `tests/unit/`. This is focused consumer coverage, not the full suite.

Select the unchanged native node
`tests/kvm/test_oci_docker_hub_cli_live.py::test_docker_hub_nginx_unprivileged_detached_default_process_and_public_exec`
independently with `PALIMPSEST_OCI_DOCKER_HUB_NGINX_LIVE=1` and its `IMAGE`,
`ARCHIVE_SHA256`, `MANIFEST_SHA256` settings under the same prefix, plus the
five host BOOT/packer settings. Use the pinned original archive, no user or
command override, 512 MiB / one vCPU and network none. The proof checks process
readiness/root/lifecycle, not HTTP serving. Preserve failed runtimes and prior
inactive domains with reviewed exact inventory/hash checks before and after.
Then run the separate unchanged built-image cold exec proof below at the same
pushed SHA, sequentially. A failed or skipped native test is not qualification;
neither these checks nor the unchanged guest binary imply a new full Gate 2.

### Explicit OCI run user

The optional `run --user USER[:GROUP]` has a focused host-contract loop:

```sh
uv run pytest -q tests/unit/test_oci_process.py tests/unit/test_oci_run_request.py \
  tests/unit/test_oci_public_cli.py tests/unit/test_oci_run_adapter.py \
  tests/unit/test_oci_docker_hub_cli_live_contract.py
uv run pytest -q tests/unit/test_oci_store.py \
  -k 'boot_plan or oci_root_prepare or user_override or override_preparation or oci_root_domain'
```

The separately selected real-image node is
`tests/kvm/test_oci_docker_hub_cli_live.py::test_docker_hub_redis_detached_explicit_user_and_public_exec`.
It requires `PALIMPSEST_OCI_DOCKER_HUB_REDIS_USER_LIVE=1`, plus
`PALIMPSEST_OCI_DOCKER_HUB_REDIS_USER_IMAGE` (absolute local archive),
`PALIMPSEST_OCI_DOCKER_HUB_REDIS_USER_ARCHIVE_SHA256` and
`PALIMPSEST_OCI_DOCKER_HUB_REDIS_USER_MANIFEST_SHA256` (canonical `sha256:` pins),
and the existing five host BOOT/packer settings. It is independent of the
original `REDIS` opt-in and explicitly uses `--user redis` with the original
entrypoint/arguments. Neither test substitutes for the other.

Use one fresh 512 MiB / one-vCPU, network-none VM. Preserve all failed runtimes
and previously retained domains/disks. A separate unchanged-image cold public
exec proof checks the default launch contract at the same pushed SHA; its
existing defaults are 4 GiB / two vCPUs. Execute sequentially with reviewed
inventory checks. Guest C/ELF are unchanged by the host-only user override;
this does not itself require the whole guest boot matrix or qualify Gate 2.

### Other focused paths

Runtime-parent diagnostic-only edits use
`tests/unit/test_oci_host.py tests/unit/test_oci_run_adapter.py`
and `tests/unit/test_architecture_guard.py`. Inspect the lane planner as usual;
this explicit selection covers the verifier and direct launch consumer, not
the full host-runtime lane. Run the same selection on the pushed server SHA.
Injected metadata changes test rejection diagnostics, not the cause of a
previous intermittent native launch failure. Do not relax ACL/identity/ctime
checks or retry to manufacture a pass. No guest rebuild is needed when guest
source and packaged bytes are unchanged. Another cold VM attempt requires a
reviewed fresh-name strategy preserving the retained `exec-cli` definition;
unit passes do not qualify that unrun native attempt.

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

The main-console versus additional-exec standard-I/O diagnostic is separately
opted in with `PALIMPSEST_OCI_STDIO_CLI_LIVE=1`. Run the two explicit pytest
nodes in `tests/kvm/test_oci_stdio_cli_live.py` sequentially with `-x`: UID 0
and UID 101 each receive a fresh 512 MiB, one-vCPU, no-network runtime and a
tiny test-only scratch OCI image. The probe records bounded FD 1/2 metadata and
path-reopen errno without reading standard I/O, changing permissions, or
creating aliases. Its portable parser contract is
`tests/unit/test_oci_stdio_cli_live_contract.py`. This diagnostic does not
change or qualify the packaged guest, an original application image, Gate 2,
or general OCI compatibility; retain its receipt and exact failed runtime for
review rather than normalizing observed console metadata.

For worker/packer resource failures and additional-exec diagnostics, keep the
two feedback loops separate:

```sh
uv run pytest -q tests/unit/test_oci_worker_protocol.py tests/unit/test_oci_converter_first_pass.py
uv run pytest -q tests/unit/test_oci_exec_session.py tests/unit/test_oci_exec_control.py tests/unit/test_oci_exec_client.py tests/unit/test_oci_exec_public_routing.py
```

For the bounded worker-attribution change, also run the direct public consumers:

```sh
uv run pytest -q tests/unit/test_oci_run_request.py tests/unit/test_oci_run_adapter.py tests/unit/test_oci_public_cli.py tests/unit/test_oci_resource_status.py
```

Attribution tests must exercise canonical request/response validation and real
file/descriptor boundaries, with faults injected at the relevant operation.
Synthetic exception wrappers alone do not verify parent acceptance, publication
or teardown. Whole-module dependency mappings remain conservative; a reviewed
attribution-only edit may use these explicit files without claiming all mapped
consumers or the full guest matrix were exercised.

The worker file includes real bounded subprocess failure cases; the converter
file contains portable intake cases. Real pinned-packer component cases are
separate `native-live` nodes in `tests/oci_fs/test_layer_filesystem.py`; they do
not run in portable lanes and do not require mounts or `CAP_SYS_ADMIN`. Select
the exact nodes with `PALIMPSEST_OCI_PACK_LIVE=1`,
`PALIMPSEST_OCI_PACK_LIVE_PACKER` set to an absolute path, and
`PALIMPSEST_OCI_PACK_LIVE_PACKER_SHA256` set to its lowercase SHA-256. These
tests use the production packer subprocess management with small explicit byte
and time limits so parser errors remain visible. They are standalone component
tests, not hard-worker or end-to-end materializer qualification. macOS skips do
not verify those Linux paths: run the selected nodes on the exact pushed server
SHA too.

The zero-fragment regression first failed on real `mksquashfs` 4.6.1 at
`d255fd2`, separately for directory-only and empty layers. Structural verifier
v3 permits an already-in-bounds fragment-table offset when no fragments exist;
nonzero fragments still require a table. Required tables, bounds, root location
and padding checks remain intact. The verifier version participates in the
existing derived recipe/receipt identity; v2 records are not migrated or deleted.
This matches the [upstream writer](https://github.com/plougher/squashfs-tools/blob/4.6.1/squashfs-tools/mksquashfs.c)
and [Linux reader](https://github.com/torvalds/linux/blob/v6.6/fs/squashfs/super.c).
Run the unchanged real-packer nodes again after the fix; a portable structural
fixture is not proof of native format compatibility.

At `c95d948`, all three real-packer cases passed on the server (0.71 s), but
the original Redis VM then exposed the same obsolete predicate in portable
guest and C stage-1 validation. Host parser success alone therefore does not
qualify guest filesystem compatibility. Keep portable/C differential cases
separate from native VM proofs, and retain nonzero missing-table, bounds,
required-table, padding and whole-device digest negative controls.

For guest parity edits, the focused portable selection is
`tests/unit/test_oci_guest_filesystems.py tests/unit/test_oci_initramfs.py`.
The actual C source harness is `tests/unit/test_oci_guest_exec.py`; Linux uses
host `cc`, while macOS requires explicit `PALIMPSEST_GUEST_EXEC_DOCKER_TESTS=1`
with the existing pinned offline GCC image. The packaged ELF is separately
checked by `tests/integration/test_oci_guest_stage1_binary.py`, including two
pinned rebuilds matching the packaged bytes. C source checks alone do not
prove that the shipped binary changed. These tests do not mount an OCI root
or replace the separately selected original-image native proof.

Transition-target diagnostics have a separate real-C module,
`tests/unit/test_oci_guest_transition.py`, selected by
`PALIMPSEST_GUEST_TRANSITION_DOCKER_TESTS=1`. It uses the pinned offline GCC
image; compilation runs as the invoking UID, and ownership-sensitive checks
run as container UID 0 with all capabilities dropped, no-new-privileges,
network disabled, a read-only root/binary and small fresh tmpfs fixtures.
No host-root fixture mutation is needed. Without the explicit opt-in these
tests skip; skips are not evidence that C compiled or executed. Fixture
checks and fixed marker formatting do not qualify OverlayFS mount/move or
root identity; retain separate native positives/negatives and original-image
verification. In particular, a diagnostic native Redis failure remains a
compatibility failure, even when it successfully identifies the rejecting check.

For additional-exec output ownership changes, run both
`tests/unit/test_oci_guest_exec.py` and
`tests/unit/test_oci_exec_pipe_ownership.py`. The first uses real anonymous
pipes to check distinct UID/GID ownership and UID 101 self-FD reopening; the
second injects failures through the production start callsite and requires
pre-fork refusal, complete endpoint cleanup and unchanged root-owned control
pipes. These tests do not qualify the packaged ELF or native lifecycle.

The approved proc-only `0555` exception uses the same production policy in
the real C fixture module. It separately checks acceptance, pre-move readiness,
unchanged metadata and initial-identity retention across `0755`/`0555` changes.
Generic directory checks and `dev`/`sys` still reject `0555`; wrong owner,
nonempty/nofollow/type, special bits, replaced inode and filesystem identity
remain negative controls. The internal readiness checker uses tmpfs magic in
the restricted fixture only; the runtime wrapper fixes OverlayFS magic.
Retain the existing native guest matrix and an unchanged original Redis run
as separate real mount/workload evidence. A later Redis entrypoint failure
must not be hidden by changing its user, command, image or privileges.

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

The cold public-CLI proof uses one fresh eight-hex UUID suffix for both its
`/tmp/p-execcli-<suffix>` runtime and `exec-cli-<suffix>` run/domain name.
It never reuses the fixed `exec-cli` name of retained failed evidence. A name
collision is still a failure, not permission to adopt or delete a domain.
All public commands and domain checks use the selected name; root identity,
PID 1 refusal, split streams, exit status and stop/rm assertions are unchanged.
Only a fully successful proof removes its own identity-checked temporary
runtime. Failed runtimes remain available for investigation.

For this test-name-only change, select portable cold-proof contract tests plus
`test_oci_host.py`, `test_oci_run_adapter.py`, `test_test_lanes.py` and
`test_architecture_guard.py` in `tests/unit/`. Run the same selection on the
exact pushed server SHA, followed by the separately opted-in cold native file.
Check the exact preserved domain inventory/UUIDs/state/autostart and original
archive hashes before and after, including after a failed native test. This
does not require a guest rebuild or qualify the full Gate 2.

At `6c230b5`, the focused five-file selection passed 157 tests locally (9.40 s)
and the same 157 tests on `pieroot-server` (21.04 s). Portable collection
found 5,471 tests; collection is not execution. The separately selected cold
native proof passed with fresh runtime suffix `d569fc7c`, and all 21 wrapper
postflight observations passed. The proof removed only its successful fresh
run/domain/runtime; the prior Redis, NGINX and fixed `exec-cli` definitions
remained inactive with their original UUIDs and autostart disabled. All four
original archive hashes were unchanged, with no active VM afterward.
This run did not reproduce the earlier ancestor-change failure, but did not
identify or fix its cause. It is not a new application build, NGINX test,
standard-I/O pathname qualification, or full Gate 2 run.

- Per edit: relevant lane(s), regression tests for the change, lint and format.
- Before push: inspect the changed-file plan and run the affected portable
  lanes; include the required native/build proof for changed runtime surfaces.
- Integration/release boundary: all portable shards, applicable privileged
  and product gates, and the existing release regression policy.
- Uncertain impact, shared state/schema foundations or unexplained failures:
  broaden to all portable tests or explicit full regression.

Keep the SHA, selected lanes/shards, platform, pass/skip counts and elapsed time
with each result. Do not report a selected subset as a full regression.
