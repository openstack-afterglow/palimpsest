# Read-only OCI worker resource observations

## Scope

`palimpsest oci resource-status` is a Linux-only, advisory NPROC diagnostic.
It is separate from VM admission, recovery and image materialization. The first
version reports inherited process-count limits, the unchanged configured worker
ceiling and its prospective effective limit, plus bounded visible-procfs
process/thread observations. Exact-SHA verification is recorded separately
from this command contract.

Linux applies RLIMIT_NPROC to a real user's threads rather than giving each
worker a private process-tree quota. Real UID 0 and specified capabilities can
be exempt. A finite configured worker ceiling does not prove that it caused
an earlier EAGAIN/ENOMEM failure.
See [getrlimit(2)](https://man7.org/linux/man-pages/man2/getrlimit.2.html) and
[proc_pid_status(5)](https://man7.org/linux/man-pages/man5/proc_pid_status.5.html).

## Safety and interpretation

- The command does not initialize product state, create locks, launch a worker,
  contact libvirt or Docker, change resource limits, stop services or retry work.
- Procfs reads and enumeration are bounded. Unavailable, malformed, truncated,
  unreadable or disappearing entries must be distinguished from a genuine
  zero count. Symlinks and non-regular status files cannot supply observations.
- Counts are non-atomic observations of visible processes grouped by their
  leader's real UID. Hidden namespaces/processes, races and differing
  per-thread credentials prevent an exact global kernel-accounting claim.
- No process list, command line, environment or per-process identifier is
  returned. The result is an allowlisted, versioned JSON report.
- This version does not measure cgroup PID/memory limits, system-wide capacity,
  address-space pressure or capability exemptions. A low visible count does
  not guarantee a worker can start, and the report supplies no free-slot count
  or automatic admission decision.

Inspect this report alongside the original resource error. Preserve uncertain
run/worker evidence; do not raise limits, stop unrelated processes or replay a
command solely because the report shows pressure or a lower count later.

## Public interface

```sh
palimpsest oci resource-status
```

No initialized runtime or BOOT variables are required. On Linux the command
returns JSON with schema `palimpsest.oci-resource-status.v1`:

- `inherited`: current NPROC soft/hard limits; `null` means infinity here.
- `worker`: configured ceiling (256) and prospective soft/hard limits, computed
  with the existing worker's minimum-of-inherited-and-configured rule. The
  prospective effective ceiling is the projected soft limit, not measured
  remaining capacity or a claim that the limit is enforced for this caller.
- `procfs_observation`: bounded aggregate counts and explicit partial,
  unavailable and rejected indicators. A missing/unreadable procfs source is
  unknown, not an observed zero. Partial counts are observations, not totals.
- `admission_verdict`: always `null`; `limitations` explains interpretation.

The reader examines at most 65,536 directory entries and 16 KiB per status
file (plus one byte to detect overflow). Production always reads `/proc`;
there is no public arbitrary-path or identity override. Unsupported platforms
return the existing typed unsupported-platform error without product state
creation. Successful JSON output, including a partial report, is not a health
check exit status.

## Verification plan

Run the new diagnostic's focused tests plus CLI/lane contracts and the existing
worker/converter tests. Exercise real proc-shaped fixtures, malformed and
bounded-input cases, inherited-limit projection and no-write/no-process-spawn
behavior. On the exact pushed server SHA, execute the public command and the
separate cold public exec native proof. Guest code and Gate 2 criteria are not
changed by this slice, so the unchanged full guest boot matrix is not a per-edit
requirement.

## Implementation and verification outcome — 2026-09-07

Implementation `5cd3d36c72273bdbd81c21bd5809840d20126d96` was independently
reviewed, pushed and checked out at that exact SHA on `pieroot-server`.
Astra managed planning/review/verification; GPT 5.6 Sol authored code and tests.

- Local selected `core-cli`, `host-runtime` and `oci-store` lanes: 2,551 passed,
  nine platform skips, one existing multi-threaded-fork deprecation warning
  (75.86 s). Final AST-gate tightening was separately rechecked: 30 passed
  (1.13 s). These overlapping selections are not a full-suite result.
- The first sandboxed lane run could not bind sockets/create a PTY and exposed
  stale pre-public-OCI expectations. Normal-permission checks resolved the
  environment failures. Five project/UI/facade assertions were corrected to
  current fail-closed contracts without changing product lifecycle behavior;
  evidence-preservation and narrow AST rejection regressions remain enforced.
- Exact-SHA server selection: eight diagnostic/CLI/worker/converter/lane/
  project/facade/UI files, 354 passed (33.88 s). This covered the focused
  worker/converter cases skipped on macOS.
- Actual Linux public command: versioned allowlist JSON, no BOOT settings,
  no pre-initialized runtime and no product config/state directories created.
  The observation was 238 visible matching threads with projected worker soft
  limit 256; it was not used as an admission decision. The actual macOS CLI
  returned the typed unsupported error and likewise created no product state.

### Required cold native proof: failed, still pending

The separate public exec CLI test failed before VM creation with
`oci-worker-resource` (2.29 s). Do not treat selected unit tests or the advisory
report as native qualification of this commit. The prior successful Gate 2 at
`36cf897` is historical evidence, not a replacement for this failed proof.

The worker started and reported its common resource category. That category
can represent internal EAGAIN/ENOMEM, MemoryError or packer process creation
failures; it does not retain the exact stage/errno. Post-failure read-only
checks found no PID-limit/OOM events in the inspected user cgroup ancestors
and substantial available/commit memory, but cannot reconstruct the earlier
failure or rule out races, other accounting scopes or per-process limits.
The packer uses one processor; no CPU-count-based thread fan-out is inferred.

Preserved server evidence:

- Selected tests: `/tmp/palimpsest-g36-server-selected-5cd3d36.log`.
- Public observation: `/tmp/palimpsest-g36-status.ylVk4H/report.json`.
- Native failure: `/tmp/palimpsest-g36-public-exec-5cd3d36.log` and
  `/tmp/p-execcli-0b710ea6` (including completed cached layers and launch error).

No VM or run was created; the libvirt domain list remained empty. Source archive
SHA-256 remained `862d4b9365f30e35a12ca48263223e4dfa11d00abb3ca68a428848e99e348458`.
No unrelated service was stopped, no resource limit was raised, and no unchanged
cold retry was attempted. Native verification remains blocked on obtaining
useful failure attribution or a deliberately changed test condition. Proposed
next slice: bounded allowlisted worker stage/errno diagnostics, then a separately
reviewed fresh cold proof. It is not implemented here and does not authorize
raw stderr/environment disclosure, automatic replay or result takeover.

## Approved follow-up: bounded failure attribution

The user approved adding failure-stage/errno diagnostics and performing a new
cold proof. Astra owns the contract, review and verification; GPT 5.6 Sol owns
implementation. The previous failed proof and its evidence remain open until
new verification actually succeeds.

The writer advances to response v3 while a strictly shaped canonical legacy
v2 response remains readable as no-details evidence. Existing nonce/request
digest binding is unchanged. Only resource failures may carry attribution;
success and other failure categories cannot. Stage names and errno values are
fixed allowlists, never arbitrary exception text, paths, arguments or stderr.
Only an actual EAGAIN/ENOMEM maps to that errno. A Python MemoryError must not
be represented as a fabricated operating-system errno, and absent facts stay
unknown.

Facts must be captured at the actual operation boundary before wrapping can
discard them. Include worker setup/materialization and the distinct dependency
inspector, version-probe and final packer spawn boundaries. Context-manager
publication and cleanup must not be incorrectly attributed to the preceding
packing operation. These facts locate an observed failure; they still cannot
identify which kernel, process, cgroup or service limit caused it.

Coarse operation labels are intentional: a subprocess-run boundary includes
communication as well as creation, and the store transaction includes producer
teardown. Only a catch around process creation itself may claim a spawn phase.

Do not change resource limits, privilege boundaries, success/cache behavior or
process cleanup. Do not introduce retries, per-process telemetry, raw exception
cause traversal or result takeover. Verify protocol rejection, legacy behavior,
sanitization and actual boundary error paths in focused files, then independent
review, push and exact-SHA Linux tests plus a separate fresh cold public proof.
