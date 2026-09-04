# OCI lifecycle control v2 guest/KVM activation

This protocol is active in the pre-production guest PID 1, domain plan, and
native-KVM qualification path. The production runtime still has no lifecycle
owner, so `create/start/run/-d` remains disabled.

## Bootstrap trust boundary

The first `HELLO` is the only unsigned envelope. Its trust anchor is the
private, owner-pinned QEMU Unix socket plus the proof driver's peer-credential
check. It binds the run ID, domain-core digest, stage-1 artifact digest, a fresh
host boot-attempt UUID, host nonce, epoch, and host wire sequence. Same-UID host
or QEMU compromise and host-daemon restart recovery are outside this slice.

After workload fork and isolation, but before workload release, PID 1
generates a 32-byte key and returns a self-MACed `BOOTSTRAP`. The host verifies it,
returns a signed `KEY_ACK`, and only then may PID 1 release the workload and
return signed `READY`. The raw key is permitted only in the private bootstrap
envelope; it is excluded from object representations and receipt projections.

## Authentication

Every signed envelope is exactly `{body,mac}`; initial `HELLO` has `mac:null`.
Canonical JSON rejects duplicate or unknown fields. `key_id` is the SHA-256
digest of a protocol-domain label and the boot key.

Direction/carrier subkeys use HKDF-SHA256. The extract salt is
`SHA256(protocol + NUL + "hkdf-salt" + NUL)`. Expand info binds the protocol,
direction, carrier, and canonical run/domain/stage-1/boot-attempt/boot-generation
identity. The MAC input independently binds protocol, direction, carrier, the
four-byte big-endian canonical-body length, and canonical body. Tags are exact
64-character lowercase hexadecimal values and are compared in constant time.

The channel carrier rejects `BOUNDARY_ACK`; the console carrier accepts only
`BOUNDARY_ACK`. This prevents channel/console reflection even before MAC
verification.

## Reconnect ordering

Only `read(2) == 0` will create a boundary in the guest implementation; HUP is
advisory. Before the host opens or sends on a replacement socket, it must admit
a signed canonical console `BOUNDARY_ACK`. The acknowledgement commits:

- a boot-wide fresh boundary UUID, old nonce, previous and next epoch;
- the previous connection-opener request ID;
- discarded header/payload parser counters;
- last accepted host and last attempted guest wire sequences; and
- the exact public lifecycle/STOP/terminal projection and its digest.

The discarded parser state has only three reachable forms: empty
`(header=0,payload=0,expected=0)`, a partial four-byte header
`(header=1..3,payload=0,expected=0)`, or a partial payload
`(header=0,expected=1..65532,payload<expected)`. Complete frames and mixed
header/payload states cannot be reported as discarded input.
An authenticated ACK that reuses any previously admitted boundary UUID from
the same boot is rejected before epoch, wire, lifecycle, or boundary state is
changed.

`RECONNECT` carries the boundary UUID and SHA-256 digest of the complete
canonical acknowledgement envelope. Logical STOP request identity is separate
from monotonically increasing per-direction wire sequence.

The ACK projection is authoritative for the next `SNAPSHOT`: the snapshot must
match it exactly. In particular, after a host attempted only a partial `STOP`
write, the host may be in local `stop-sent` state while PID 1 correctly commits
`ready`. That `ready` ACK and snapshot return the host to `ready`, after which it
retries the same logical STOP request ID with a new wire sequence and MAC. A
`stop-sent` host also accepts an ACK committing `stopping`, or a terminal state
whose STOP identity is either the same request or the bounded natural-exit
race. A terminal state carrying the STOP ID must commit the STOP wire exactly.
If natural termination wins with a null STOP ID, STOP parsing and terminal
causality are independent: the host preserves both the preceding accepted wire
and attempted STOP wire as bounded candidates until the next signed ACK commits
one exact value. These alternatives never derive state from an unauthenticated
frame.

The host tracks attempted and guest-confirmed host wire sequences separately.
A partial STOP therefore commits the preceding accepted wire, not the locally
attempted STOP wire. If EOF instead interrupts a RECONNECT before PID 1 accepts
it, the next ACK binds the previous accepted nonce, connection opener, wire,
and lifecycle state. The host then retries the same logical RECONNECT request
under a fresh epoch, nonce, wire sequence, boundary digest, and MAC. This makes
that ambiguity an explicit replay-safe transition rather than a dead end.
If PID 1 accepted the RECONNECT but its SNAPSHOT was only partially emitted,
the ACK instead binds the attempted RECONNECT nonce, opener, and host wire plus
a monotonic lifecycle progression from the preceding committed state. That
consumes the logical RECONNECT; the host opens recovery with a new logical
request and fresh epoch, nonce, wire, boundary digest, and MAC. Both variants
allow ready to remain ready or terminate naturally, stopping to remain tied to
the same STOP or become terminal, and terminal only to remain exact. The
eventual SNAPSHOT must equal the new ACK state exactly.

## Evidence handling

The receipt projection accepts only the exact canonical bytes for the declared
carrier. The unsigned `HELLO` path explicitly requires no key. Every signed
kind requires the boot key and is authenticated internally before a projection
can be returned; callers cannot assert a verification boolean. The projection
records body/envelope digests, key ID, bindings, counters, carrier, direction,
and the derived result, but never the raw boot key or MAC tag. A digest of the
complete receipt-safe projection binds those fields against accidental
rewrites. Console evidence replaces the authenticated envelope with a fixed
redaction marker after verification; the transcript retains its authenticated
digest projection. Final evidence is recursively checked against every
observed boot key and tag before it is returned.

## Activation gate

Slice 29B activates the complete guest/KVM gate: post-fork PID 1 key custody,
the C codec and HKDF-HMAC verifier, console boundary emission, authenticated
negative controls, deterministic guest assets, and the fail-closed protocol/
broker/isolation/supervisor/plan/guest/initramfs/proof/domain version cascade.
The six-connection proof loses the initial READY, loses the first reconnect
SNAPSHOT after the RECONNECT is accepted, discards a partial STOP, retries the
same logical STOP on a fresh wire, deduplicates it without a second signal,
and reconnects through terminal state. Native evidence remains mandatory;
local policy tests and reproducible compilation are not a substitute.
