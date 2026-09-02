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
`/proc/1/root`; each must contain exactly:

```text
palimpsest-oci-root-workload-proof-v1
```

After validating the contract, the main process blocks SIGTERM, creates a
`signalfd`, forks one same-process-group descendant, and waits for the
descendant to become ready. The main then sends SIGTERM to PID 1. A conforming
supervisor forwards that signal to the workload process group. Both workload
processes require the observed SIGTERM sender PID to be 1. The descendant
writes a private completion byte and exits 43. The main uses `waitid` with
`WNOWAIT` to prove that the descendant is already a zombie without consuming
its status, then exits 42. This removes the race where PID 1 cleanup could
replace the descendant status with SIGKILL while leaving PID 1 responsible for
the final `wait4` reap. PID 1 must establish both statuses itself; workload
output cannot prove execution, forwarding, or reaping.

Build the packaged test ELF with the same digest-pinned GCC image and ELF
sealer used by stage 1:

```sh
scripts/build_oci_guest_workload_proof.sh
```

The default output is `tests/kvm/assets/workload-proof.x86_64`. Rebuilding to
two independent temporary paths must produce bytes identical to that packaged
artifact before its digest is added to a KVM fixture manifest or receipt.
