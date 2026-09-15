# Opt-in durable exec observations: implementation contract

Status: public CLI/session implemented; focused checks and a fresh native proof
passed after the explicitly approved worker NPROC increase to 1024. Initial
resource and test-parser failures remain recorded below.
The original process-local baseline was `c2def266774bf2eb7c9edd246a4fb8e99722aa55`, including
the [process-local observation](oci-exec-result-observation.md). This contract
adds local historical inspection, **not recovery of monitor/client authority**.

## Product boundary

Keep ordinary `exec` unchanged: no default files, retention database, new
permission requirements or additional ACK retries. The explicit opt-in is:

```text
palimpsest exec --completion-record /absolute/private-parent/new-record NAME -- COMMAND ARG...
palimpsest oci exec-record /absolute/private-parent/new-record
```

The option must precede NAME because the existing command tail is literal
`argparse.REMAINDER`. Arguments after NAME must retain their existing meaning.
The first command supports only the existing Linux OCI-root runtime. Reject
other runtimes and invalid literal argv before record creation or submission;
do not silently ignore the option. Compose, shell, stdin/TTY and cloud adapters
are not extended. The current continuation implements these commands; it is
not qualified by the historical storage-only results below.

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

## Storage-only continuation (historical foundation)

The initial storage slice provided the internal codec, writer and offline reader
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

Creation-race checks compare directory inode bindings; they do not authenticate
an inode incarnation against the same user or administrator. In particular,
unlinking an unopened directory and immediately recreating it can reuse both
the inode and its timestamp values on ext4. The deterministic replacement test
keeps the original directory alive to establish a distinct inode, and must not
be described as proof against arbitrary same-user replacement. Private-parent
ownership and permissions are the boundary against other principals.

## Storage foundation delivery and qualification

Implementation `1bf84f15bb8a9f4d2e37cb5bd0af7f2cd2248bac` adds the internal
`OCIExecRecordWriter.reserve(..., managed_state=...)`, observed/confirmed
publication and `read_exec_record(...)` APIs. The caller supplies local
completion observations; no live session calls this module yet. Each file has
the exact versioned `palimpsest.oci-exec-record` schema and the reader labels its
snapshot as local historical metadata. Test-only correction
`b65d1b18296e14261a41a01f3211a1a4f019556f` leaves product source unchanged.

Independent source/test review and the correction review approved this isolated
slice. Final local selection of `test_oci_exec_record.py`,
`test_oci_exec_session.py`, `test_oci_exec_client.py` and `test_test_lanes.py`
passed **144 with 61 Linux-only skips** in 1.70 s. After GitHub push,
pieroot-server fetched exact SHA `b65d1b18296e14261a41a01f3211a1a4f019556f`;
the same selection passed **205, zero skips** in 6.24 s on ext4, including all
61 Linux cases and real ACL checks. These overlapping counts are not additive.
Changed-file lint/format, lane manifest and diff checks passed; server checkout
was clean after verification. No whole-suite, public recorded-exec, power-loss,
new image build or new full Gate 2 qualification is claimed.

Evidence is retained in `/tmp/palimpsest-g41.GtlvjG/` locally, including
`local-server-fix.log`, the three initial review results and
`review-server-fix-result.txt`. Server selected-test and lane logs are
`/tmp/palimpsest-g41-server-selected-b65d1b18296e14261a41a01f3211a1a4f019556f.log`
and `/tmp/palimpsest-g41-server-lanes-b65d1b18296e14261a41a01f3211a1a4f019556f.log`.

The first server selection at `1bf84f1` failed the inode-recycling-dependent
injection (204 passed, one failed, 6.51 s); its separate log remains retained.
The correction establishes a deterministic distinct-inode test, not a product
race fix. Earlier review failures led to publication cleanup/cancellation and
ACL/fault-test corrections before approval; they are not erased by the final
pass. An extra, unchanged macOS ProcessSession PTY check also failed outside the
selected scope (expected marker printed, exit status 1). Its cause was not
established or fixed; independent review deemed it nonblocking for this
unintegrated module. Failed delegated-runtime initialization and an unwritable
default test cache were environmental setup failures, with evidence preserved;
successful runs used the supported execution environment and a writable cache.

At this foundation delivery, the next step was OCI-only opt-in routing/session
publication barriers and the offline CLI, followed by a separate public
recorded-exec/native proof. The internal foundation alone neither preserved
actual exec results on disk nor changed ACK behavior. PID 1 protection, worker
limits, output handling and VM-root/volume ownership were unchanged.

## Public integration continuation

The opt-in dispatcher validates literal argv, requires the OCI-root runtime and
performs the existing exact-record preflight before reserving pending. The
original OCI session repeats its binding checks before submission and owns
the writer during execution. No-option adapter calls remain unchanged; generic
ProcessSession/ExecRequest and cloud/Lima interfaces are not extended.

An observed-publication failure raises the fixed pre-ACK recording error with
the live unconfirmed observation, without sending ACK. An ACK request/response
failure keeps the existing acknowledgement error. After a validated ACK, live
facts become confirmed before confirmed publication; failure to persist that
last phase raises a distinct post-ACK recording error, not an ACK error or a
successful child result. No output body or monitor authority is recovered.

`oci exec-record` reads only local metadata before root resolution or
initialization. Its JSON is the exact versioned snapshot with classification
and fixed no-authority guidance; successful inspection returns zero regardless
of the recorded child exit. Malformed/unreadable records fail with fixed
diagnostics. Both path arguments are kept raw until strict storage validation;
the parser must not erase dot components or symlinks through normalization.

The independent normal native proof is
`tests/kvm/test_oci_exec_record_cli_live.py`, enabled only by
`PALIMPSEST_OCI_EXEC_RECORD_CLI_LIVE=1` with the existing accepted exec image and
host BOOT configuration. It is separate from the engine/public-exec proofs and
full Gate 2. ACK faults belong to focused controlled tests, not a claimed live
reply-loss experiment. Code review and selected exact-SHA Linux checks have
passed; the separate native proof was initially incomplete and subsequently
passed in the authorized continuation below.

## Public integration evidence and initial native blocker

Implementation `7a6a59bc26739ac3988cf3c5c4169c9250caff1e` was independently
approved and pushed. Final local selection of the eight integration/storage/
session/client/public-routing/dispatcher/CLI-contract/lane files passed
**581 with 67 Linux-only skips** in 3.93 s. On pieroot-server, a clean checkout
of that exact pushed SHA passed the same selection: **648, zero skips**,
23.92 s. All 61 storage cases and six new real-writer/pinned-client Linux cases
executed. Counts overlap and are not additive. Changed-file lint/format, lane
manifest and diff checks passed; the original storage module and existing
no-option pinned-client regression test remained unchanged.

The separate cold native test was actually attempted at the same SHA and
**failed in 1.05 s before VM creation**. The materializer reported fixed stage
`toolchain version check`, errno `EAGAIN`, in the existing worker-resource
category. Neither public recorded exec nor its post-removal inspector ran;
no new completion-record directory or run was created. Successful read-only
domain enumeration and exact path checks confirmed absence. That failed attempt
did not qualify public recorded exec; no earlier successful VM proof was used
as its replacement.

Read-only resource observations before/after the attempt saw 443/474 visible
same-real-UID leader thread totals; the worker ceiling stayed 256. These are
non-atomic, visibility-dependent observations, not an admission verdict or a
proof of which process/thread, cgroup or memory limit caused EAGAIN. Separate
process-name/thread-count inspection showed other workloads sharing the user;
no unrelated process/service was stopped, no limit was raised, and no unchanged
native retry was attempted. The source archive's SHA256 remained
`862d4b9365f30e35a12ca48263223e4dfa11d00abb3ca68a428848e99e348458`.

Evidence is retained locally in `/tmp/palimpsest-g42.CCBDNM/`, including
`local-selected.log`, `review-integration-result.txt` and
`review-proof-final-result.txt`. Server evidence:

- `/tmp/palimpsest-g42-server-selected-7a6a59bc26739ac3988cf3c5c4169c9250caff1e.log`
- `/tmp/palimpsest-g42-native-7a6a59bc26739ac3988cf3c5c4169c9250caff1e.log`
- Failed runtime `/tmp/p-execrecord-runtime-81dbc815` and separate empty record
  parent `/tmp/p-execrecord-retained-81dbc815` (both preserved).

Initial tests exposed a default-client early-close regression, new-fixture
setup mistakes and transient lane drift before the parallel test file existed.
All were corrected before approval; default failure lifetime is caller-owned,
while recorded failures eagerly close their writer and monitor descriptors.
Two existing Unix-socket tests hit sandbox EPERM; the related four passed in
the socket-capable environment, followed by the full selected local pass.
Native-test review also corrected unsafe absence/retention cleanup gates and
inventory parsing against actual server LF formatting. Intermediate failed
logs and the previous turn's separate unresolved macOS PTY failure are kept;
that PTY test was neither rerun nor fixed here.

At that delivery the required native proof was blocked pending changed resource
availability or an explicitly authorized intervention. Do not stop unrelated
workloads or weaken limits implicitly. The subsequent authorized continuation
below resolves this qualification blocker by verifying recorded exit status
and streams, offline inspection, normal stop/rm, retained record bytes and
unchanged source. Public retained-root reuse and shared multi-VM volumes remain
separate follow-ups; no output replay or monitor-authority recovery was added.

## Authorized NPROC adjustment and fresh native qualification — 2026-09-08

The user explicitly approved raising the configured worker NPROC ceiling from
256 to 1024. Sol implemented the shared constant, fixed diagnostics and focused
boundary tests; Astra managed scope and independent approval. Commit
`23b8dd76dc469cdcee7ca98b83a9c8f4184a5519` preserves lower inherited soft/hard
bounds, NOFILE 256, AS/FSIZE/CPU/core limits, PID 1 protections and bounded retry
behavior. No unrelated workload was stopped or host-wide limit changed.
The cap is still real-UID-wide accounting, not a private worker-tree quota.

- Local worker/resource/converter/CLI selection: 348 passed, eight Linux skips
  (3.06 s). Exact pushed-SHA server selection: 356 passed, zero skips (16.90 s).
  The initial server run had 15 failures and 341 passes (17.51 s): the main
  orchestration script omitted the established umask 022, so SSH's umask 002
  created mode-664 fixtures rejected by existing safety checks. Only the test
  shell was corrected; product code and initial failure logs were preserved.
- First new native attempt reached boot, root proof/PID 1 denial, recorded exec
  exit 17, split output and confirmed offline inspection, then failed (14.05 s)
  because the test expected one LF from virsh domuuid. Actual domuuid and
  domname both returned the canonical value followed by two LFs, with no stderr.
  The exact failed VM was normally stopped; its definition, root/run, record
  and logs remain preserved, not counted as a successful full proof.
- Test-only correction `8c2b3daba714b1fee4c4b96618125b2b5107fbac` accepts one
  optional final blank LF but keeps canonical UUID/boot identity, exact name,
  bounded parsing and cleanup gates. New portable parser/lane checks passed
  81 locally (1.30 s) and 81 on that exact server SHA (2.48 s). An initial
  portable-test import error was fixed before final independent approval by
  separating a test-only parser module. Helper changes select only oci-monitor
  plus an explicit native-live suggestion. Production code is unchanged from
  the 356-pass worker-limit commit; counts overlap and are not additive.
- The fresh exact-node native proof at `8c2b3da` passed in 17.96 s. It verified
  run -d, actual root markers/PID 1 access denial, exit 17, 36 stdout bytes and
  22 stderr bytes, confirmed metadata, unchanged boot/root identity, normal
  stop/rm, and identical offline inspection after removal without BOOT or
  runtime initialization. Successful VM/run/runtime cleanup completed; the
  owner-private completion record remains. A separate post-check confirmed
  that only the earlier stopped failed-test domain remained.

Original image SHA256 remains
`862d4b9365f30e35a12ca48263223e4dfa11d00abb3ca68a428848e99e348458`.
Final advisory observations reported configured/projected NPROC 1024 and 485
visible same-UID threads (partial/racy, not admission or capacity guarantees).
Independent source/test reviews, changed-file lint/format, lane and diff checks
passed. The earlier unrelated macOS PTY failure remains outside this slice;
no new image build, full guest matrix, full Gate 2 or physical power-loss test
is claimed. ACK faults retain their separate controlled-test evidence.

Evidence:

- Local logs and approvals: `/tmp/palimpsest-g43.hCpkMP/`.
- Server initial selection: `/tmp/palimpsest-g43-selected-23b8dd76dc469cdcee7ca98b83a9c8f4184a5519.log`.
- Server corrected selection: `/tmp/palimpsest-g43-selected-umask022-23b8dd76dc469cdcee7ca98b83a9c8f4184a5519.log`.
- Parser/lane checks: `/tmp/palimpsest-g43-helpers-8c2b3daba714b1fee4c4b96618125b2b5107fbac.log`.
- Failed/successful native logs: `/tmp/palimpsest-g43-native-23b8dd76dc469cdcee7ca98b83a9c8f4184a5519.log`
  and `/tmp/palimpsest-g43-native-8c2b3daba714b1fee4c4b96618125b2b5107fbac.log`.
- Preserved failed runtime `/tmp/p-execrecord-runtime-5376e48a`, record parent
  `/tmp/p-execrecord-retained-5376e48a`, and shut-off domain
  `exec-record-cli-5376e48a` (UUID `49bd618f-1a3e-4cd8-b436-58c194efd791`).
- Successful retained record: `/tmp/p-execrecord-retained-d8f18b41/new-record`.
  Successful runtime `/tmp/p-execrecord-runtime-d8f18b41` was removed.

The public recorded-exec native blocker is resolved by this new proof. The
raised shared-UID cap is an explicit short-term tradeoff; dedicated worker
accounting via UID/cgroup remains a separate design, not an implemented feature.
