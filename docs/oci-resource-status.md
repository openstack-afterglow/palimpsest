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
