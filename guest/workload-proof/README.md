# PID 1 workload qualification helper

`proof.c` is a deterministic freestanding Linux x86_64 workload used only by
the native-KVM PID 1 supervisor qualification. It is not a production guest
component and its stdout or stderr is never qualification authority.

The exact successful process contract is:

```text
argv = [
  "/.__palimpsest_workload_proof_v1",
  "palimpsest-argv-one",
  "",
  "line\nbreak",
]
environment = [
  "PALIMPSEST_PROOF_ENV=value with spaces",
  "PALIMPSEST_PROOF_EMPTY=",
]
cwd = "/proof/workdir"
uid:gid = 65534:65534
supplementary groups = []
```

It also requires a non-PID1 main process whose parent is PID 1 and whose
process-group ID equals its PID. From the OCI root it reads both
`/.__palimpsest_oci_root_workload_proof_v1` and the same path below
`/proc/self/root`; each must contain exactly:

```text
palimpsest-oci-root-workload-proof-v1
```

The workload deliberately uses its own procfs root link. The proof runs as
UID/GID 65534 and must not depend on ptrace permission to dereference PID 1's
`/proc/1/root`. Root PID 1 independently verifies `/proc/self/root` against the
moved OCI root before launching this helper.
It also parses `/proc/1/status` within a fixed 8 KiB bound and requires exactly
one `Uid`, `Gid`, and `Groups` line. All real, effective, saved, and filesystem
UID/GID values must be zero and the supplementary-group list must be empty.
The helper independently requires its own `/proc/self/cgroup` to contain the
exact unified-v2 membership `0::/palimpsest.agent/exec-00000001`. It also
requires write-only opens of the cgroup root, agent parent, and its own session
leaf `cgroup.procs` to fail, proving the admitted UID 65534 cannot move itself
through any visible level of the hierarchy.

After validating the contract, the main process blocks SIGTERM and forks two
same-process-group descendants. The cooperative descendant consumes PID 1's
forwarded SIGTERM through `signalfd` and exits 43. The stubborn descendant
keeps SIGTERM blocked and remains until the broker uses the pinned
`cgroup.kill` descriptor, producing status 137. Once both descendants report
ready, the main arms its own signalfd, emits the proof-only scheduling marker,
and waits. The host sends STOP only after broker READY and that marker; root
PID 1 forwards the configured OCI stop signal, after which the main exits 42.
PID 1 observes the cooperative grace result and kills only the session leaf,
reaps to `ECHILD`, proves the leaf empty and removes it, then proves the empty
agent parent has no direct processes and removes it before terminal success.

This cgroup is a workload containment and cleanup boundary, not a security
sandbox against a hostile workload admitted as root or with powerful
capabilities.

Build the packaged test ELF with the same digest-pinned GCC image and ELF
sealer used by stage 1:

```sh
scripts/build_oci_guest_workload_proof.sh
scripts/build_oci_guest_kvm_filesystem_fixtures.py
```

The default output is `tests/kvm/assets/workload-proof.x86_64`. Rebuilding to
two independent temporary paths must produce bytes identical to that packaged
artifact before its digest is added to a KVM fixture manifest or receipt.
