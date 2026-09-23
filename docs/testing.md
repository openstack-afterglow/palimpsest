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

Linux state-root default and override changes can first be checked without
creating system directories:

```sh
uv run pytest -q tests/unit/test_state.py -k 'state_root or unconfigured or explicit_xdg'
```

These tests exercise resolution only for `/var/lib/palimpsest`; they do not
provision `/var/lib`, migrate existing data, or verify the separate implemented
`/var/log/palimpsest` host command journal.

The Linux service-account installer has a root-free unit contract:

```sh
uv run pytest -q tests/unit/test_linux_install.py
```

This test does not modify host accounts or system directories. Any root smoke
test must run in a disposable, network-disabled container and must never mount
host account databases or writable host system paths.

Tests remain in their existing modules; the runner changes selection, not
assertions, fixtures or production safety checks. A documentation-only edit
can produce an empty test recommendation; that is not a successful test run.
Hub tests use their own environment and explicit lane.

The Hub's server-build path has a focused project-scoped HTTP contract lane:

```sh
cd hub
uv run pytest -q tests/test_builds.py tests/test_upload_limits.py \
  tests/test_hub_api.py tests/test_migrate.py
```

This exercises upload offsets and private download over ASGI HTTP, build
authorization/visibility, durable queue claim, publication, project quota,
expiry, and data migration. The main HTTP build test replaces the **guest VM executor**;
the bytes labeled as a cloud image are a portable fixture, not a bootable
image. Therefore an HTTP pass proves no KVM guest boot, no native SquashFS
mount, no Keystone deployment, and no production worker availability. A native
server-build qualification must separately use a real pinned x86_64 cloud
image on a provisioned Linux KVM host, observe `palimpsest-hub-build-worker`
claim and actual `build_layer` guest execution, verify the downloaded SquashFS
digest and parent/base chain, then check owned VM cleanup. Do not perform that
host/remote operation or infer success from portable CI without its explicit
deployment prerequisites and approval.

The Hub lane also exercises a malicious bundle member that must not remove a
shared blob, suffix byte ranges, canceled lock waiters, retry after interrupted
blob promotion, refusal on blob directory fsync failure, competing
identical-digest registrations, competing project enqueue requests, worker
preflight rejection before SQL access, fail-closed recovery of an unverifiable
builder marker, retry of completed-build private scratch cleanup without
losing its published output, and refusal to reclaim guest state after a failed
or timed-out builder run until its recorded process group is verified. These are portable contract tests: a Linux-only
run must still confirm parent-death signals, exact process-group reaping,
filesystem crash durability, and libvirt teardown.

## CLI reference and distribution checks

For command documentation and packaging-only edits, use the focused contracts:

```sh
uv run pytest -q tests/unit/test_cli_reference.py tests/unit/test_packaging.py \
  tests/unit/test_test_lanes.py tests/unit/test_architecture_guard.py
uv run python scripts/generate_cli_reference.py --check
uv run python scripts/build_package.py --out-dir ./dist/verify-001
```

The unit checks and documentation drift check are distinct from the final
command's real wheel/sdist build and isolated wheel installation. The package
smoke runs outside the checkout, without installing optional KVM dependencies
or provisioning host accounts. Build tooling may need to download its build
backend; wheel installation itself uses only the newly built local artifact.
Choose a new output directory on each invocation; existing packages are never
overwritten. The wheel is rebuilt from the produced source distribution, and
the installed package verifies the bundled stage-1 ELF digest and format.
The helper also exercises the README's `uv tool install --no-index` path in
temporary tool directories and runs that installed executable. `--no-deps`
belongs to the separate `uv pip install` probe, not `uv tool install`.
The generated reference normalizes argparse usage whitespace and optional
positional requiredness: Python 3.12 and 3.13 otherwise render different
wrapping and internal `required` values for `*` and remainder arguments.
Check the same generated file under both supported versions; accepting a stale
file or changing production parsing is not the fix.
These checks do not boot a VM, qualify Gate 2, publish a release, or authorize
changes to `/var/lib/palimpsest` or `/var/log/palimpsest`.

The separate `development-package.yml` workflow runs the architecture guard,
CLI reference check, lane-manifest check, focused lint, `core-cli` plus
`qualification`, the focused publication contracts, and the real package smoke
before the publish job receives `contents: write`. It accepts pushes only from
`main`, `dev`, or `codex/oci-root-phase1`, plus manual dispatch of those same
refs; pull requests cannot publish. Ref-scoped concurrency lets those three
branch runs reach their own publish job even when they share a commit SHA.

After local transfer checksum verification, the standard-library
`scripts/publish_development_package.py` helper treats
`package-<full-commit-SHA>` as an immutable create-or-verify publication. It
creates an absent lightweight tag at exactly the event SHA and refuses a
different tag. It creates an absent prerelease with the wheel, sdist, and
`SHA256SUMS`, then requires exact tag/title/notes/draft/prerelease metadata,
exact asset names, and downloaded SHA-256 bytes. Every mutation is judged by
re-reading remote state rather than by its own exit status, so a conflict or an
interrupted response that already created the tag, release, or asset still
succeeds; the original error surfaces only when the re-read is still
incomplete. An otherwise exact partial release may upload only its missing
expected assets. An asset another run is still uploading is awaited until
GitHub reports it `uploaded`, never re-uploaded, and a release that never
converges fails closed. An extra, duplicate, or byte-mismatched asset fails
closed. The helper never force-updates or deletes a ref, deletes an asset, or
uses `--clobber`.

These are source and fake-`gh` unit contracts, not remote publication
verification. The prerelease is not Latest, a PyPI publication, or evidence
that native KVM, guest-binary, filesystem, Gate 1, or Gate 2 lanes passed.

The two tooling scripts select `core-cli` in the changed-file planner. CI also
runs the real package smoke explicitly; ordinary unit lanes do not need to
download and rebuild distributions for every test iteration. Run the same
focused checks and package smoke on the exact pushed Linux server SHA with
`umask 022` and `PYTHONPATH` unset. Preserve existing runtime state and evidence.

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
Neither matrix sets `max-parallel`, so every shard of a run starts in the same
wave.

- **Why the caps were removed.** The earlier caps allowed 3 Linux and 2 macOS
  shards at a time, which queued a second wave. Across the 21 completed `Test`
  runs of the current job shape (2026-09-15 to 2026-09-23):
  - macOS shards 3/4 and 4/4 waited a median 151s and 170s;
  - Linux shards 4–6 waited 91–105s;
  - the `Unit tests (macOS 15)` aggregate finished last in 19 of the 21 runs.
- **Critical-path baseline.** Median 325s and p90 781s. Excluding one burst
  that was serialized on the single self-hosted KVM runner, the figures are
  303s and 346s.
- **Expected effect (estimate).** A dev/PR critical path of roughly 155–170s.
  The ~140s native KVM job sets the floor. Re-measure after the change lands
  before treating this as achieved.
- **Shared capacity.** The matrices still draw on the organization's Free-plan
  pool of 20 hosted jobs and 5 macOS jobs, shared with sibling repositories.
  Same-SHA dev and main pushes therefore still queue behind each other,
  especially on the single KVM runner.
- **Contract test.** `tests/unit/test_test_lanes.py` pins:
  - the shard lists, the exact shard command and its `--shard N/M`
    denominator;
  - that `max-parallel` is absent or at least the shard count;
  - the aggregate check names, `if: always()` and their exact `needs`;
  - each aggregate's single verdict step: its `env` maps every dependency to
    `needs.<dep>.result` (and, for KVM, `vars.PALIMPSEST_KVM_ENABLED`), its
    `run` is the exact success-only script; neither the step nor its job
    sets `shell`, `defaults` or `continue-on-error`, the workflow has no
    `defaults`, and no dependency job or step sets `continue-on-error`;
  - that no gate job sits in front of the test jobs, and that the only
    job-level `if:` outside the aggregates is the `kvm` opt-in variable;
  - that across all workflows the only jobs whose `runs-on` (string, list or
    `group`/`labels` mapping) is not a GitHub-hosted `ubuntu-*`, `macos-*` or
    `windows-*` label are `kvm` in `test.yml` and `kvm-proof` in
    `release.yml`, and that `release.yml` runs only on `v*` tag pushes.
  These pin the workflow shape only. Keeping `pull_request` code off the
  self-hosted runner is a repository-settings control, not a YAML one.
- **Rules for future CI changes.** Measurement and change rules are in the
  `CI 파이프라인 성능 규정` section of [AGENTS.md](../AGENTS.md).

The existing aggregate check names remain, and require every shard to succeed;
a skipped or cancelled shard cannot satisfy them. Lint, manifest checks and
package construction run once. The release workflow still performs its broad
unit, build and native proof checks.

The pytest session sets `PALIMPSEST_LOG_HOME` to a private `0700` temporary directory through `tests/conftest.py`. Portable CLI tests therefore never write `/var/log/palimpsest` or acquire Linux-only warning output merely because the host-global journal directory is absent. Journal failure tests explicitly replace this override with missing, malformed, or unsafe paths and still require the production warning; the fixture does not suppress or weaken that behavior.

Portable test harnesses may only use directory-relative (`dir_fd`) syscalls that
every supported interpreter exposes. `os.open`, `os.stat`, `os.unlink`,
`os.rename` and `os.replace` are available on Linux and macOS; `os.mkfifo` with
`dir_fd` is not, because `mkfifoat` is absent from some macOS builds and raises
`NotImplementedError: dir_fd unavailable on this platform`. Race harnesses that
create a non-regular node mid-read use the absolute node path; the production
reader still holds its pinned directory descriptor, so the swap it must reject is
unchanged. This restriction applies to test scaffolding only: production code in
`state.py` keeps its `dir_fd`-pinned open/stat/rename sequences.

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

### Required stage-1 GitHub KVM gate

The reusable `Native KVM stage-1 proof` job is enabled only when repository
variable `PALIMPSEST_KVM_ENABLED` is exactly `true` and requires runner labels
`self-hosted`, `linux`, `x64`, and `kvm`. `Required native KVM proof` fails when
the native job is skipped or fails; do not replace it with a portable or TCG
result.

The current dedicated repository runner is `pieroot-server-palimpsest-kvm`
(runner id `21`). It runs Actions runner `2.337.0` in persistent container
`palimpsest-gh-runner` with restart policy `unless-stopped`. The container is
not privileged, drops all capabilities before adding only `CHOWN`,
`DAC_OVERRIDE`, and `FOWNER`, sets `no-new-privileges`, and has no Docker socket
or host-home mount. Its writable runner-state volume is separate from a
read-only kernel volume; `/dev/kvm` is the only host device. The root-owned
mode-`0400`, single-link kernel/config digests are respectively
`sha256:89f7d4f31f6ef77d0f8d45810de9e19e3f8dededf3dd6ebf89bb540d26d8c0fd`
and `sha256:4d4aaaed367bd2fb6ed1238b94ea9bb095a5e5a0d0cc67d1cb70595643cc795a`.
Changing the runner registration, capabilities, volumes, device, kernel pins,
or repository variable is infrastructure mutation, not test setup.

Commit `785cd02c638a339acfab9c9f1a6bcb7e97683a5e` is qualified by `Test` run
`35029001178` attempt 4 and reusable run `35029003724` attempts 4/5. Their
native artifacts `10448739883` and `10448829655` each contain the exact 45-file
evidence set and a `palimpsest.oci-stage1-kvm-proof.v20` receipt covering 44
QEMU invocations and 43 executed boots. The repository is public and its
current fork-workflow approval policy is `first_time_contributors`; persistent
runner isolation does not eliminate the `/dev/kvm` or cross-job state risk.
Changing that approval policy, stopping the runner, or disabling the variable
requires an explicit security/availability decision.

Ordinary server lanes use `env -u PYTHONPATH`, `PYTHONDONTWRITEBYTECODE=1` and
`umask 022`, as the previous full baseline did. Never run fixture mutations
against an existing user VM or delete retained failure evidence to speed up a
test. A native change still requires its actual native proof; a focused unit
pass is not a replacement.

Set the process `umask 022` **before Git checkout/merge**, not merely before
pytest. Git's non-executable file entry does not encode the full required
`0644` mode: a checkout under the server's `002` mask can create the packaged
guest ELF as `0664` while its content hash and Git status still match. Retain
the strict package mode/provenance check. Do not broadly chmod a checkout or
relax the assertion; any existing mismatch needs exact-file identity/hash
inspection and a separately reviewed, narrowly scoped metadata repair.

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

At exact `d72796c`, the focused local/server acceptance selection passed 100
tests on each host (7.74/6.48 seconds), followed by Gate 1's two tests
(2.47 seconds), a fresh v2 artifact build, and Gate 2's one test (18.90
seconds) on the qualified server. The run used a fresh network-none Buildx
builder, fresh runtime and private journal; the builder and successful Gate 2
run/domain were removed through their owned cleanup. Each of the 24 preflight,
post-build and final preservation observations passed for four existing
inactive domains and seven archives, with no active QEMU. See
[`oci-root-build-run-acceptance.md`](oci-root-build-run-acceptance.md) for exact
artifact digests, evidence paths, the preserved pre-Gate-1 wrapper failure and
the explicit non-claims. This is executed product-gate evidence, not permission
to implicitly enable either special lane in portable selection.

## When to broaden verification

### Selectable guest networking

Networking policy, CLI parsing, domain XML and the durable contract are
portable: `tests/unit/test_oci_network.py`,
`tests/unit/test_oci_network_live_contract.py`, `tests/unit/test_kvm_contract.py`
and the real-C guest harness `tests/unit/test_workload_network.py` (the harness
needs a working `cc`). Because the guest ELF changed, a production networking
change also requires the reproducible packaged-ELF/provenance checks and the
explicit stage-1 boot matrix before any networking proof counts.

The native proof `tests/kvm/test_oci_network_live.py` is a separate `native-live`
opt-in and never runs from changed-file selection. It needs
`PALIMPSEST_OCI_NETWORK_LIVE=1`, a `0711` runtime parent in
`PALIMPSEST_OCI_NETWORK_PROOF_ROOT`, the ordinary native boot variables, and
per-node `PALIMPSEST_OCI_NETWORK_{PYTORCH,REDIS,NGINX}_{IMAGE,ARCHIVE_SHA256,MANIFEST_SHA256}`
pins. Its three nodes are independent: NAT with a published loopback port plus
one real outbound HTTPS API request, host-only with a published port plus proven
absence of DNS/TCP egress, and NAT with an explicit `0.0.0.0` publication
reached through the host's own address. Record each node separately; a passing
portable contract is not a networking proof, and one node's success says nothing
about the others. See the [network contract](oci-network.md) for the exact
commands and limits.

### Official service-image matrix

The approved `/sys` 0555 compatibility change uses the focused real-C
`tests/unit/test_oci_guest_transition.py` with
`PALIMPSEST_GUEST_TRANSITION_DOCKER_TESTS=1`. Both proc/sys need unchanged-mode
and mode-drift cases; sys needs explicit owner, nonempty, symlink/file,
replacement, wrong-filesystem and disallowed-mode negatives. Keep dev/generic
0555 rejection. A production guest change also requires reproducible packaged
ELF/provenance checks, the explicit stage-1 boot matrix, then one pinned MySQL
case. Portable success cannot stand in for either native proof.

The independent [official service matrix](docker-hub-service-matrix.md) uses
`tests/kvm/test_oci_docker_hub_services_live.py` for PostgreSQL, Redis, MySQL,
and NGINX defaults, with a separate explicit-user Redis case. Run one exact
parameter or `-k` selection at a time; each needs its own
`PALIMPSEST_OCI_DOCKER_HUB_SERVICE_<CASE>_LIVE` and source pins. Portable
feedback is `tests/unit/test_oci_docker_hub_services_live_contract.py` plus
the reused `test_oci_docker_hub_cli_live_contract.py`, lane-manifest and
architecture checks. These tests do not implicitly enable VM execution.

Readiness is not a SQL/PING/HTTP response. An absent client or unavailable
guest loopback must not become a successful application proof. Preserve the
actual result, root/PID1 observations where reachable, failed runtime and
source hashes. Only an exactly owned active failed VM may be publicly stopped;
uncertain ownership or inactivity blocks subsequent native cases.

Redis-user network qualification cross-checks proc/sysfs interface sets and
unique positive indices: `lo` type772 with flags0x9/0x49, plus only optional
`tunl0` type768 and `ip6tnl0` type769 with exact flags0x80. A live domain XML
check independently rejects NIC devices. Reject malformed/duplicate/missing
records, unexpected devices and UP tunnels; retain all workload security,
PING, root/PID1 and cleanup checks. This test-only revision is distinct from
the earlier literal-only-lo failures and does not change production networking.

Executed checkpoint `1752ba9` (2026-09-11): all five native cases failed;
the default four never reached application probes, while explicit-user Redis
passed readiness/version/root/PID1 checks but its PING failed with network
unreachable. Focused portable contracts passed 109 tests on both local and
exact-SHA server; these are not service passes. All 41 final preservation
observations passed with no active VM/QEMU. See the matrix for exact source
pins, retained evidence, reached stages and separately scoped next steps.

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
requires exactly three pre-existing root-owned mode-0777 single-link aliases:
`/dev/stdout -> /proc/self/fd/1`, `/dev/stderr -> /proc/self/fd/2`, and
`/dev/fd -> /proc/self/fd`; `/dev/stdin` remains absent. Only after exact no-follow metadata
and bounded target validation does it open each alias with the NGINX-relevant
write/create/append flags plus diagnostic `O_NONBLOCK`, require full reopened
FD identity, and emit one stream-specific marker through the pathname. It
never truncates, reads standard I/O, changes permissions, or creates an alias.
Before opening its own files it requires only inherited FD 0/1/2 (excluding
the inventory directory FD itself). The fixed record schema is V3; V2 receipts
are historical and must not be parsed as the new proof. It also checks the exact nine `/dev`
entries, opens its own dynamic pipe via `/dev/fd/N`, compares identities and
known read/write data, and requires ENOENT after closing that descriptor.
Direct `/dev/fd/1` and `/dev/fd/2` writes must retain endpoint identity, and
the PID1 `fd`/`fdinfo` masks must remain empty and read-only.
Do not infer pathname read denial merely from an original write-only FD.
Its portable parser/source contract is
`tests/unit/test_oci_stdio_cli_live_contract.py`. Until independent review, a
new guest ELF and exact-SHA native UID 0/101 passes, this remains `test-defined`
and does not qualify the changed packaged guest, an original application
image, Gate 2, or general OCI compatibility. Retain its receipt and exact
failed runtime rather than normalizing observed metadata.

The pre-transition PID 1 console-open-description diagnostic is a distinct
native opt-in:

```sh
PALIMPSEST_OCI_CONSOLE_OFD_LIVE=1 uv run python -m pytest -q \
  tests/kvm/test_oci_console_ofd_live.py::test_pid1_console_procfd_reopen_has_independent_nonblocking_ofd
```

It boots a test-only static PID 1 with the same qualified kernel/config and KVM
selection, 128 MiB memory, one vCPU and no network. The bounded probe mounts its
own procfs before any root transition, requires `O_NOFOLLOW` to reject the exact
`/proc/self/fd/1` magic link, then narrowly reopens that fixed self descriptor.
It checks stable character-device identity and proves the reopened descriptor is
nonblocking without changing the inherited console flags. Both fixed writes and
the final marker must occur exactly once. QEMU output is capped at 1 MiB and the
20-second boot is terminated through only its newly owned process group; evidence
is retained on failure. This diagnostic does not use or modify production
`guest/stage1/init.c`, the packaged ELF, an OCI image, or the main-output path.
Do not run it before independent code review, and do not treat compilation,
collection, a skip, or a host userspace probe as native evidence.

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

The approved proc/sys `0555` exceptions use the same production policy in
the real C fixture module. It separately checks acceptance, pre-move readiness,
unchanged metadata and initial-identity retention across `0755`/`0555` changes.
Generic directory checks and `dev` still reject `0555`; wrong owner,
nonempty proc/sys, nofollow/type, special bits, replaced inode and filesystem identity
remain negative controls. The internal readiness checker uses tmpfs magic in
the restricted fixture only; the runtime wrapper fixes OverlayFS magic.
The separately approved DEV-only populated target is covered, not adopted:
test unchanged contents/readiness and continued DEV owner/mode/identity refusal,
then separately exercise real mount coverage and child-private devices in the
opt-in native diagnostic (a defined test, not yet an executed result).
Retain the existing native guest matrix and unchanged original-image runs
as separate real mount/workload evidence. A later Redis entrypoint failure
must not be hidden by changing its user, command, image or privileges.

The populated-device component diagnostic is independently selectable:

```sh
PALIMPSEST_OCI_DEV_COVER_LIVE=1 uv run pytest -q -s \
  tests/kvm/test_oci_dev_cover_live.py
```

It requires the qualified `PALIMPSEST_KVM_KERNEL` and
`PALIMPSEST_KVM_KERNEL_CONFIG`, Linux KVM/QEMU, and the already-local pinned
GCC container. Two sequential 128 MiB/one-vCPU/no-network boots use a separate
test PID1: positive mount coverage and a pre-move mode-drift rejection.
Each boot has a 20-second observation budget and 1 MiB console limit, followed
by bounded QEMU process-group cleanup. Compilation has a 60-second host budget.
The fixture explicitly uses tmpfs, not the production OverlayFS root. It
checks retained image-FD closure, trusted filesystem identity across the move,
and private child device entries with the parent mount unchanged. It does not
execute the packaged ELF, OCI application, credential drop, or PID1 protection
proof; run the existing stage-1 matrix and service case separately. Temporary
receipts and consoles remain on failure; opt-out skips are not qualification.

The official MySQL explicit-user diagnostic is a separate service case:

```sh
uv run pytest -q -s \
  'tests/kvm/test_oci_docker_hub_services_live.py::test_official_service_default_process_compatibility[mysql_user]'
```

Set `PALIMPSEST_OCI_DOCKER_HUB_SERVICE_MYSQL_USER_LIVE=1` and its own
`IMAGE`, `ARCHIVE_SHA256`, `MANIFEST_SHA256` variables with the same service
prefix, plus the existing qualified host settings. It changes only the existing
public `--user mysql` flag, retains image argv/environment, and leaves the
default `[mysql]` case untouched. It needs 2048 MiB/one vCPU sequentially and
does not add initialization credentials or capabilities. A missing opt-in is a
skip, not proof; retain failed runtime/source evidence.

The test-only random-password follow-up is separately selected:

```sh
uv run pytest -q -s \
  'tests/kvm/test_oci_docker_hub_services_live.py::test_official_service_default_process_compatibility[mysql_user_random_password]'
```

Set `PALIMPSEST_OCI_DOCKER_HUB_SERVICE_MYSQL_USER_RANDOM_PASSWORD_LIVE=1`
and its matching `IMAGE`, `ARCHIVE_SHA256`, and `MANIFEST_SHA256` values, which
pin the unchanged original archive. The test derives a no-secret config wrapper,
generates the password only inside the guest, uses public `--user mysql`, and
requires ordered initialization-complete then final-ready markers. Its socket
ping proves liveness only. The newly owned run/root must be removed on either
application success or failure; inability to prove exact cleanup fails the test.
No existing retained failure may be removed.

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

The main-output pump and unified console queue have a dependency-free local C harness:

```sh
uv run python -m pytest -q tests/unit/test_main_output_pump.py
```

It compiles `tests/c/main_output_pump_harness.c` with the local C compiler and
directly includes the same `guest/stage1/main_output_pump.h` as production.
It exercises callback I/O, queue backpressure versus sticky diagnostic overflow,
partial writes, combined pump/queue drain and real full nonblocking pipe
recovery without a VM. This is not packaged-ELF, PID 1 or native STOP proof.

Run pipe ownership/wiring and bounded teardown independently:

```sh
uv run python -m pytest -q tests/unit/test_main_output_pipes.py
uv run python -m pytest -q tests/unit/test_main_output_terminate.py
uv run python -m pytest -q tests/unit/test_main_console_lifecycle.py
uv run python -m pytest -q tests/unit/test_main_control_deadline.py
```

These source-based harnesses must run the actual production helpers/loop with
deterministic syscall doubles; supplementary structural assertions are not
runtime fault-injection evidence. The teardown contract is five seconds of
grace followed by at most one additional second after force cleanup, with
nonblocking reap and no normal TERMINAL for undrained or failed output.
Each new portable file is explicitly registered in `oci-guest`; select it
directly during edits rather than rerunning all portable lanes.
The terminal-console helper tests check actual partial boundary delivery,
permanent/closed-sink failure and deadline expiry. Their injected boundary
bytes do not replace cryptographic frame construction or native reconnect
proof. Ordinary diagnostics retire, but signed boundary frames must actually
be delivered through the retained nonblocking sink; discard is not success.
Control I/O and random acquisition use the current cleanup phase deadline;
an expired STOP deadline cannot erase the separate post-kill drain budget.
The parser yields on EINTR and lifecycle pumping yields after at most 64
complete frames so repeated control traffic cannot starve output or teardown.
The frame's own five-second timeout remains distinct from the caller's STOP
or cleanup slice: exhausted caller time yields without dropping parser state,
while an expired partial-frame deadline still rejects the connection.

`tests/unit/test_workload_loopback.py` extracts the production loopback helper
into an actual-C syscall-double harness. It covers fresh and already-up success,
exact ioctl order and payloads, interface index/name/flag drift, every ioctl
failure, final-state rejection, and socket closure. A Linux-only compile check
binds the freestanding constants and 40-byte `ifreq` layout to the Linux UAPI.
The KVM kernel-config proof requires built-in `CONFIG_NET` and `CONFIG_INET`;
module or missing values are rejected. Native service proof remains explicit.

After `f86e4da`, the Redis-user probe also records raw interface rows and sysfs
name/flags/type/index. Only its security assertion is deferred until after
service/root/PID1 observations; it still fails overall if that assertion fails.
This diagnostic-only edit does not require another unchanged-ELF boot matrix;
run the isolated Redis-user node after exact-SHA focused contract tests and
reviewed preservation of every prior failed runtime/domain.

The production console-sink helpers and their child/parent call sites have
separate source-extraction harnesses:

```sh
uv run python -m pytest -q tests/unit/test_main_console_sink.py tests/unit/test_main_console_sink_callsites.py
```

These compile the current functions and call-site bodies from
`guest/stage1/init.c` against deterministic syscall doubles. They cover console
identity and descriptor validation, cleanup faults, and explicit close ordering
before child isolation and parent TERMINAL handling. They do not replace the
packaged-binary, PID 1, or native VM proof.

After a guest source freeze, rebuild the sealed ELF with the pinned offline
compiler and update both the binary digest and the versioned, named source
bundle digest (`init.c` plus `main_output_pump.h`). Run `test_oci_initramfs.py`
and `tests/integration/test_oci_guest_stage1_binary.py` separately, then the
exact pushed SHA on the native host. Select the stage-1 matrix, UID0/101 stdio
probe and cold public exec as distinct finite runs with inventory/archive
preservation checks before and after every run, including failures. The
updated stdio probe expects main and additional-exec output to be separate
workload-owned FIFO0600 endpoints with successful self-FD reopen, exact fixed
stdout/stderr/self-FD aliases, identity-preserving pathname reopen and observed
write on each stream. PID1 access restrictions remain unchanged. Set a healthy private
`PALIMPSEST_LOG_HOME` for stdio and cold guest-only stderr comparisons. Keep the separate
host journal failure-warning tests; never strip a warning to pass the proof.
These selections do not qualify NGINX, a new application build or full Gate 2.

For the self-FD change, run the narrow alias and parser contracts first:

```sh
PALIMPSEST_WORKLOAD_DEV_ALIAS_DOCKER_TESTS=1 uv run pytest -q -x tests/unit/test_workload_dev_aliases.py
uv run pytest -q -x tests/unit/test_oci_stdio_cli_live_contract.py tests/unit/test_oci_initramfs.py
```

The first command requires the existing pinned local Docker compiler image;
an opt-out skip is not real-C evidence. After proof-fixture provenance is
synchronized, run the filesystem/proof contracts separately. Only after
independent review, package rebuild and commit/push should the exact-SHA
server execute stage1, UID0/101 stdio, cold exec and the independent
`mysql_user_random_password` service node sequentially. Preserve all existing
failed domains and source archives. MySQL's own new root is the explicit
disposable exception, even on failure; never transfer its raw logs to debug it.

When the test-only `guest/workload-proof/proof.c` changes, also rebuild its
ELF and SquashFS fixtures, synchronize their canonical manifest/source/ELF
pins, and run the portable retained-root injection consumers before push:

```sh
uv run python -m pytest -q tests/kvm/test_oci_root_libvirt_live.py -k reuse_fixture
```

These are mocked offline-injection contracts, not an opt-in VM run. The
separate reuse fixture keeps an explicit ELF digest in addition to the shared
fixture loader; it must match the newly reproduced test binary. Retain its
domain-absence, identity and journal-replay failure controls. The broader
`core-cli qualification` selection remains the development-package gate.

The stage-1 composite reconnect proof waits for both READY_COMMITTED and the
workload's signal-armed marker before its first intentional disconnect. Pipe
delivery makes readiness and child-output observation asynchronous; the test
must establish the ordering it later verifies. The six-connection portable
fixture covers both immediately available and delayed signal readiness.
Receipt marker counts/order and authenticated boundary checks are unchanged.

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

Pinned TensorFlow and PyTorch CPU compatibility uses separate per-image opt-ins
and exact archive/manifest digests. See [OCI machine-learning image
compatibility](oci-ml-compatibility.md) for the two explicit native nodes,
resource bounds, and non-GPU scope.

- Per edit: relevant lane(s), regression tests for the change, lint and format.
- Before push: inspect the changed-file plan and run the affected portable
  lanes; include the required native/build proof for changed runtime surfaces.
- Integration/release boundary: all portable shards, applicable privileged
  and product gates, and the existing release regression policy.
- Uncertain impact, shared state/schema foundations or unexplained failures:
  broaden to all portable tests or explicit full regression.

Keep the SHA, selected lanes/shards, platform, pass/skip counts and elapsed time
with each result. Do not report a selected subset as a full regression.
