# Retained-root inventory and deletion boundary

## Current slice: metadata observations

This continuation exposes discovery of saved OCI writable-root identifiers:

```sh
palimpsest oci root-volumes
palimpsest oci root-volume UUID
```

The commands use the configured existing state root. They do not create a
runtime, acquire/create volume locks, launch tools, open raw disks, contact a
VM, or require host BOOT configuration. An empty existing namespace can return
an empty list; a missing or inconsistent namespace is an error. Exact lookup
uses the same bounded namespace validation, so unrelated corruption may also
refuse it. Invalid identifiers are rejected before inventory I/O.

Both responses explicitly classify their contents as local metadata
observations. Public fields are the volume UUID, recorded lifecycle state,
retention policy, logical size, lower-graph digest, generation and recorded
attachment (run ID/name or null). No host paths, filesystem UUID, ACL receipts,
monitor authority, lock or quarantine names are exposed. In particular,
`retained` is a stored state, not a reusable/deletable verdict or proof of a
currently inactive VM. A report is not a cross-record atomic snapshot.

Enumeration and record reads are bounded and reject observed inconsistency
instead of emitting a partial success. Existing canonical record/raw namespace
association rules are preserved, but raw contents, ext4 identity and access
revocation are not verified by these commands. Symlinks and nonregular metadata
files must not cause following or blocking reads. The owner-bound configured
state remains the trust boundary, not protection against a malicious same-UID
writer. Reading files may update filesystem access times.

The existing public `run --root-retention retain --root-volume UUID` still
performs its independent same-graph/size, retained attachment, access and
single-writer checks. Inventory output never replaces those checks.

## Why standalone deletion is not enabled here

`release_oci_root_volume` is an attachment-release operation: it requires the
exact prior `ArtifactLeaseOwner` and a creating/attached/deleting record. A
retained record has no attachment, and the current deleting state requires an
attachment owner. Fabricating a VM owner, switching the saved policy, claiming
the disk through a disposable VM, or directly unlinking it is not an acceptable
public deletion implementation.

No deletion command, force flag, retention-policy rewrite, automatic cleanup,
or mutation of an existing retained disk is introduced by this slice. The
previously qualified reusable disk and failed-VM recovery archive remain intact.

## Required contract for the next deletion implementation

Before enabling exact-ID deletion, define and independently review these
conditions. This is an implementation gate, not a claim that deletion exists:

1. **Exact current identity.** Pin the existing volume lock and namespace;
   require canonical UUID, retain policy, retained state and no attachment.
   Bind the current record/generation, raw inode/device, ext4 identity, size and
   lower graph. A stale inventory response cannot authorize deletion by itself.
2. **Outstanding access and references.** Require revoked access evidence and
   the actual baseline ACL. Reject managed run references, incomplete recovery,
   unreadable/malformed metadata or uncertain ownership. Define a read-only
   active/inactive libvirt association check for the supported backend before
   deletion, including the original and quarantine targets. Do not infer
   absence from an unavailable backend. Out-of-band same-UID/admin modifications
   are outside the cooperative state trust boundary, not something a metadata
   listing can prove absent.
3. **Durable destructive intent.** Select a versioned nil-owner deleting state
   or a separate exact deletion-intent record. Do not overload the current
   attached-owner deletion contract. All claim/grant/recovery mutation paths
   must honor that fence under the existing volume lock, including after a
   process crash; do not introduce a run-lock/volume-lock ordering inversion.
4. **Guarded quarantine and completion.** Persist intent before renaming, verify
   exact identity and references again after quarantine, then remove only the
   proven target. Preserve unresolved evidence on failure. Define how restart
   resumes each durable phase, how identifier reuse is forbidden, and how
   already-completed deletion differs from an unknown or missing disk.
5. **Verification before delivery.** Test busy locks, concurrent claim, stale
   generations, references, unreleased ACLs, replaced paths, links and malformed
   state. Inject interruption before/after intent, rename, unlink and completion
   publication. Use a separately created disposable retained disk for an
   exact-SHA host test; do not use the only reusable proof disk or old failure
   recovery archive as an implicit deletion fixture.

Shared multi-VM data volumes remain a separate design. They must not weaken
the exclusive root disk contract.
