# Opt-in durable exec observations: implementation contract

Status: internal storage foundation; not yet an available CLI/session feature.
The original process-local baseline was `c2def266774bf2eb7c9edd246a4fb8e99722aa55`, including
the [process-local observation](oci-exec-result-observation.md). This contract
adds local historical inspection, **not recovery of monitor/client authority**.

## Product boundary

Keep ordinary `exec` unchanged: no default files, retention database, new
permission requirements or additional ACK retries. The proposed opt-in is:

```text
palimpsest exec --completion-record /absolute/private-parent/new-record NAME -- COMMAND ARG...
palimpsest oci exec-record /absolute/private-parent/new-record
```

The option must precede NAME because the existing command tail is literal
`argparse.REMAINDER`. Arguments after NAME must retain their existing meaning.
The first command supports only the existing Linux OCI-root runtime. Reject
other runtimes and invalid literal argv before record creation or submission;
do not silently ignore the option. Compose, shell, stdin/TTY and cloud adapters
are not extended. Both commands above remain proposed until implemented.

The second command reads a local directory only. It must work after the
original CLI has exited and after normal VM removal, without BOOT settings,
runtime roots, init, libvirt, a monitor, an image or a live run. Successful
inspection returns zero even if the recorded child failed; inspection success
is not exec success. Invalid/unreadable records return a fixed nonzero error.
An inspection never submits, polls, acknowledges, cancels, stops, deletes,
reconnects, acquires monitor authority or recommends automatic replay.

## Contents, ownership and retention

Store only a versioned local record ID and the existing minimal observation:
terminal exit-code/signal or permitted pre-child cancellation null, allowlisted
reason, bounded stdout/stderr byte counts and ACK confirmation state. Generate
the record ID independently of command tokens and sequences. Its only purpose
is to bind files in this one directory; it grants no authority.

Do not store output content, argv, environment, credentials, command token,
sequence, monitor endpoint, run identity, VM name, runtime paths or raw errors.
Byte counts still reveal workload metadata; persistence is explicit opt-in.
There is no output-content recovery or guarantee that streamed bytes were
flushed, saved or consumed by a downstream application.

The chosen directory is the user's association with their invocation. There is
no machine-verifiable run/command association in this format. Files are local
claims, not signed guest attestations: the owning user or administrator can
forge, copy, roll back or delete them. An offline reader cannot re-establish
that a file came from the original client, that a VM is unchanged, or that a
mailbox still contains a result. Never promote a loaded record to the live
session's `observed_completion` or use it for protocol operations.

Require a new directory below an existing explicit private parent, separate
from Palimpsest's managed runtime state. Do not create missing ancestors,
adopt an existing directory, chmod existing data, or overwrite old records.
New record directory mode is 0700 and files 0600, owned by the effective user.
Normal stop/rm and source-image cleanup must not remove this directory.
Retention is manual and indefinite until the user removes it; no automatic GC,
record deletion command, encryption/key store or backup synchronization is
added. Do not describe this as tamper resistance or secure erasure.

## Publication and crash semantics

Use three bounded, immutable version-1 JSON artifacts in a pinned directory:

- `pending.json`: schema and independent local record ID, published before any
  submit attempt. It does not assert that a command was or was not submitted.
- `observed.json`: the same schema/ID and complete validated observation with
  ACK `unconfirmed`, published before the first ACK request.
- `confirmed.json`: the same schema/ID and identical terminal/reason/counts,
  differing only in ACK `confirmed`, published after a validated ACK response.

Each artifact has an exact schema and is at most 4 KiB. Reject duplicate keys,
unknown fields/versions, booleans as integers, unbounded integers, invalid UTF-8,
non-finite JSON values, invalid terminal combinations and identity mismatches.
Reuse the existing observation validation instead of relaxing its invariants.
Do not serialize generic dataclass internals or arbitrary exception objects.

For each publication, create a private temporary regular file exclusively in
the pinned directory; write all bytes, fsync the file, publish under its final
name atomically without replacing an existing entry, then fsync the directory.
The initial new directory's parent must also be synced before submit. A plain
overwriting rename or write-in-place is insufficient. Implementation must
select and test a no-clobber primitive and exact-inode temporary cleanup.
Retain partial final artifacts on failure. Never retry publication by adopting
an existing target or deleting another invocation's files.

Only a completed publication barrier permits the corresponding live transition.
File/parent/directory fsync errors are failures, not warnings. First qualify on
a local Linux filesystem with these operations; do not claim equivalent
durability on network filesystems or immunity to faulty hardware. A completed
fsync sequence is the OS durability contract, not a power-loss test result.

| Last trustworthy local evidence | What it says after restart | What it does not say |
|---|---|---|
| No valid pending artifact | Record unavailable/incomplete | Command did not execute |
| Pending only | A recording attempt was reserved; completion unknown | Submit failed, command is still running, or replay is safe |
| Valid observed artifact | Terminal and output events were observed; ACK unconfirmed | ACK was never sent, failed to apply, or result remains retained |
| Matching confirmed artifact | Original writer recorded a validated ACK response | Current mailbox state, current VM identity, durable output or trusted provenance |
| Malformed/conflicting artifact | Inspection fails closed | A lower, convenient state can safely replace the conflict |

Crashes after ACK application but before confirmed publication leave a valid
unconfirmed record. That ambiguity is intentional. A crash before submit and
a crash after submit but before terminal publication may both leave only
pending. A visible valid artifact can survive a reported fsync failure; reading
it later establishes local contents, not that the original CLI reported
successful persistence or that a prior power loss was survived.

## Session and error behavior

Reserve pending only after literal request validation and existing exact-record
OCI capability preflight, but before session construction can submit. Maintain
the original binding checks again when entering the session; do not replace
them with record ownership. Close every record/monitor descriptor on all exits.

After the existing output-event generator resumes past all validated terminal
output, keep the in-memory unconfirmed observation and persist observed. If
that publication fails, do not send ACK: raise a dedicated fixed recording
error, preserve in-memory facts and partial files, and release client pins.
Do not cancel the command or claim that the mailbox remains occupied. Poll,
consumer/output errors or incomplete terminal evidence must not fabricate an
observed artifact in a finally block.

Then perform the existing bounded ACK request/retry and response/next-sequence
validation, unchanged. Ordinary ACK failure retains observed and the current
typed acknowledgement error; do not write confirmed. After valid ACK, first
set the live observation to confirmed, then publish confirmed. If this second
publication fails, raise a recording error that distinguishes local storage
failure from ACK failure. The live facts remain confirmed; an offline reader
may still see unconfirmed. No normal status event or successful wait may be
invented for this opt-in storage failure.

Only after all required barriers succeed should the existing status/wait and
exec return-code behavior proceed. Timeout, output-limit and cancellation
remain failures even when the recorded child exit code is zero. No new retry,
new command, STOP, result discard or recovery operation is introduced.

KeyboardInterrupt, SystemExit and cancellation propagate as themselves, with
no blocking publication in cleanup and no replacement by a recording error.
Close is idempotent and cannot mask the active failure. A fork must not use the
original writer or session API. A deliberately started new inspector process
may read disk metadata; it gets no original-client authority. Filesystem calls
can block: initial support assumes the qualified local filesystem and must not
claim a hard wall-clock bound on storage I/O or relax the exec deadline.

## Filesystem and offline-reader boundaries

Use descriptor-relative operations, no-follow traversal and regular-file checks;
do not call resolve on an untrusted path and then assume symlinks were absent.
Require absolute paths without parent traversal; reject symlinks in every
component, unsafe writable ancestors, wrong owner/mode, named/default ACLs,
non-regular files, multi-link final files and changed directory/file bindings.
Root-owned sticky temporary ancestors may be accepted above the private parent;
the immediate parent itself must be current-user-owned and 0700. Existing
runtime-parent verification requires QEMU search and is not a private-record
validator: do not reuse its 0711 creation behavior or alter its policy globally.

Open with no-follow/nonblocking/close-on-exec flags and fstat before bounded
reads so a FIFO/device/oversized file cannot substitute for JSON. Validate
directory ancestry and held/visible entry identity, and do not treat a detached
descriptor as proof about a replacement path. Defend against other principals;
same-user/admin forgery remains explicitly outside provenance guarantees.

The reader accepts only a bounded recognized artifact set and explicitly
recognized temporary artifacts (without parsing/promoting temporary content).
Do not recursively scan or clean. Atomic publications are append-only, but a
reader can race phase progression: take a stable bounded snapshot or return
a fixed changed-record error. Never combine observations from different
directory instances. Check confirmed against observed, and observed against
pending; malformed later artifacts must not be silently ignored. Missing
later artifacts yield only the conservative earlier state, never proof of a
failed or unsent operation. Reading changes no product state (normal filesystem
access-time behavior is not a persistence promise).

Inspector JSON must label the result as local historical metadata and expose
only the validated schema/record ID, observation or null, conservative phase
and fixed guidance. It must not echo untrusted text or return an exec exit code
as its own inspection status. Exact field names should be pinned with CLI
contract tests in the implementation, not inferred by callers from this draft.

## Implementation slices and required evidence

Astra owns decisions, orchestration and verification; Sol authors source/tests.
Keep shared integration ownership explicit when independent slices run in
parallel. Do not widen generic ProcessSession or every adapter to carry a
filesystem feature; preserve existing runtime preflight and same-record gates.

1. Strict record codec, descriptor-bound writer and offline reader; new focused
   storage tests for mode/ACL/link/path/size/schema rejection, no-clobber races,
   partial writes, file/directory/parent fsync failure and bounded snapshots.
2. OCI-only opt-in routing and session barriers, with separate public parsing
   and routing tests. Prove no-option calls unchanged, unsupported runtimes
   produce no files/submit, literal argv unchanged, exactly one submit, no ACK
   on pre-ACK persistence failure, real mailbox uncertainty before/after ACK,
   distinct post-ACK storage failure and descriptor/interrupt cleanup.
3. Offline CLI read before root initialization; subprocess restart inspection
   after writer exit, stopped/removed VM and absent runtime roots. Prove zero
   monitor access, no result authority, fixed diagnostics and inspection exit
   semantics. Inject crashes around each barrier in isolated test processes;
   distinguish process-kill tests from power-loss qualification.
4. Independent code review and final focused local checks before commit/push;
   fetch exact pushed SHA on pieroot-server and run affected selections. Add a
   separate opt-in native record proof exercising public run-d, one recorded
   exec, new-process inspection and normal stop/rm with record/source retention.
   Do not rerun the unchanged full guest matrix or image build for this host-only
   feature. Native ACK fault injection is a separate claim from normal proof.

Register new tests explicitly in existing file/lane/shard selection. Do not
replace focused evidence with overlapping totals or call a documentation review
a code/native qualification. VM-root retention/reuse and multi-VM data-volume
sharing remain separate user requirements, not consequences of local records.

## Contract delivery (historical)

Commit `c44361f4eecefe5a914cee716fd5bb1ddb3d676f` delivered only the contract
and implementation sequencing. It changed no product code or tests. The earlier
398-test selections and native pass remain historical evidence, not validation
of the storage foundation or proposed CLI feature. Monitor authority recovery
and output-body storage still require a separate design and explicit scope
decision.

## Storage-only continuation

This implementation slice provides the internal codec, writer and offline reader
plus their focused tests. It does not connect a live OCI session or expose the
proposed CLI commands. There is no claim that actual exec results are written
before ACK until the later routing/session integration is implemented and
qualified. Standalone tests supply observations as local inputs, not guest
attestations, and storage state transitions do not perform monitor operations.

The writer takes an explicit managed-state exclusion root from its future
caller; it must not resolve or initialize runtime state itself. Offline reading
does not require that exclusion root or the original run to exist. The future
dispatcher remains responsible for full OCI argv validation, exact runtime
preflight and same-record entry checks before reserving a record. The no-option
exec route and generic ProcessSession/cloud adapter interfaces remain unchanged.

Linux no-clobber publication follows
[renameat2/RENAME_NOREPLACE](https://man7.org/linux/man-pages/man2/rename.2.html).
Missing support must fail rather than fall back to an overwriting rename.
The synchronization sequence follows [fsync(2)](https://man7.org/linux/man-pages/man2/fsync.2.html):
file synchronization alone does not establish directory-entry persistence.
Descriptor-based attribute checks use the Linux behavior described by
[Python's os.listxattr](https://docs.python.org/3/library/os.html#os.listxattr).

Qualification for this isolated foundation uses the new storage test file,
existing observation/session/client regressions, and lane validation. Real
Linux filesystem checks run against the exact pushed SHA on pieroot-server.
Since no runtime imports or calls the new module yet, a new VM boot, public
recorded-exec proof and full Gate 2 are not this slice's qualification. Those
remain required for the later public integration; earlier VM results cannot
be substituted for them.
