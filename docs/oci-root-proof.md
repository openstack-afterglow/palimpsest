# Gate 2 root identity contract

## Approved scope

The user approved replacing the application's direct `/proc/1/root` access
criterion with two separate checks: the application verifies its actual OCI
root, and PID 1 reports minimal root identity over the authenticated lifecycle
channel. PID 1 non-dumpable state, workload capabilities, seccomp, private
mounts and lifecycle key/FD isolation remain enabled. Docker may coexist with
KVM on the host. This decision supersedes the previously pending root-proof
choice; it does not authorize a full-root VM mode.

The implementation uses guest consumer/bootstrap contract v18 and native proof
receipt v20. Only separately recorded tests establish Gate 2 completion; the
receipt version alone does not establish full native-matrix qualification.

## Evidence and trust boundary

1. PID 1's root transition checks the held merged-root directory, `/` and
   `/proc/self/root` identity, OverlayFS type and synchronization. Before
   emitting READY, it verifies the root identity again and emits its actual
   device/inode, PID and filesystem kind.
2. READY's existing run, domain-core, stage1 transport, boot attempt/generation,
   nonce, sequence and HMAC bindings cover the root identity payload too.
3. The host verifies the message before retaining an allowlisted, secret-free
   projection. A read-only report must match the current running/ready run,
   monitor binding, durable handoff and exact active libvirt domain. Missing,
   malformed, duplicate or mismatched evidence is an error.
4. The image-baked application probe verifies its random source marker at `/`
   and `/proc/self/root`, and emits their matching device/inode. Gate 2 compares
   this identity with the authenticated-at-receipt PID 1 report. A separate
   negative check requires direct `/proc/1/root` access to remain denied.

The report is evidence recorded by a trusted host after authenticating a
trusted guest supervisor. Its public hashes are not signatures. It does not
provide independent offline MAC verification, TPM attestation, protection from
a malicious host/guest kernel, or a proof that an arbitrary malicious image
program reports honestly. The acceptance probe is a controlled test program.

## Narrow protocol extension

READY may carry exactly this payload (numbers below describe types, not values
to hardcode):

```text
root_identity:
  schema: palimpsest.oci-root-identity.v1
  pid: 1
  filesystem: overlayfs
  device: unsigned 64-bit integer
  inode: positive unsigned 64-bit integer
```

Unknown fields, booleans in integer fields, invalid ranges and wrong fixed
values are rejected. Empty legacy READY payloads remain protocol-compatible
but do not constitute root identity evidence and cannot satisfy the new gate.
No general privileged file-reading API, image-supplied path or credential
override is added. The root proof identifies the root directory; the separate
image marker and OCI build receipt establish the controlled image provenance.

## Delivery and acceptance

The public command is `palimpsest oci root-proof NAME`. It reports only
allowlisted run/domain/boot identity, root facts and receipt/binding digests;
it does not expose boot keys, MAC tags, raw transcripts, commands or host paths.
It is a current-running-boot operation, not a recovery or result-takeover API.
It does not change the state-only contract of ordinary `inspect`.

Gate 2's new build receipt is explicitly versioned to distinguish its probe
from the old direct-PID-1-root probe. Existing archives and failed-gate evidence
are preserved. A new image must be produced by `palimpsest build`, then run
through public detached startup, root-proof readout, guest exec, protected-PID-1
negative check, stop and rm. The source archive must remain unchanged and the
exact domain/run state must be removed, including checks after a probe failure.

The build acceptance receipt is `palimpsest.oci-root-build-run-acceptance.v2`.
Gate 2 brackets execution with two reports and requires identical run, boot,
domain and root identity. The probe output uses canonical unsigned decimal
integers with explicit 64-bit bounds; the denial check requires both a failed
read and `Permission denied`, not just any failure.

Focused protocol, projection, stale-binding, guest and acceptance tests precede
push. The server fetches that exact commit before native and Gate 2 tests.
Earlier successful run/exec proofs and a weaker substituted probe are not
evidence that this contract passed.
