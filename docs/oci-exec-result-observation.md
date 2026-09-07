# Original-client exec completion observation

## First slice: process-local facts, not durable recovery

The original OCI exec client can validate the guest's terminal report and
consume its output events before its acknowledgement is confirmed. These are
different facts: an ACK failure does not erase the observed terminal, prove
the command never ran, or prove the monitor still retains the result.

The first slice preserves a minimal immutable observation in that same client
process. It is not a disk receipt, restart/reconnect feature, result listing,
permission to acknowledge another client's result, or automatic replay.
No stdout/stderr content, command arguments, token, sequence, paths or control
secrets are retained in the observation. Existing streamed output is unchanged.

## Observation and acknowledgement contract

An OCI-specific `observed_completion` property is initially absent. It becomes
available only after the existing exact identity, offsets, output bounds and
terminal validation succeed and the generator resumes after yielding all
output events. It contains the validated terminal status (or the permitted
pre-child cancellation null), fixed completion reason, stream byte counts and
`unconfirmed` acknowledgement state. Consumed events are not proof of durable
storage or successful downstream output flushing.

The normal ACK request and its existing bounded identical-request retry policy
remain unchanged. A valid ACK response and expected next sequence change the
observation to `confirmed`. No new submission, polling, ACK retry, cancellation,
STOP or removal is introduced by this retention feature.

| What the original client can establish | Local observation | Public completion |
|---|---|---|
| No valid terminal report, or output events not fully consumed | Absent | No new completion claim |
| Valid terminal and consumed output, ACK not confirmed | Retained, `unconfirmed` | Typed failure; no normal result event |
| Valid terminal, consumed output and validated ACK | Retained, `confirmed` | Existing success/failure semantics |

An ACK may have reached the monitor even when its reply is lost. Both a failure
before ACK application and a lost reply after application therefore produce
the same `unconfirmed` local observation. Mailbox availability is unknown;
do not infer retention or release, or use a fresh status as authority to replay.

For ordinary ACK request/response errors, a typed state error carries the
observation and fixed diagnostic text. It identifies the observed terminal
facts and unconfirmed ACK without reflecting raw peer/transport exceptions.
KeyboardInterrupt/SystemExit continue to propagate as themselves. No normal
ProcessStatusEvent or successful `wait()` result is invented on these paths.
Timeout, output-limit and cancellation remain failures even if a child exited
zero; cancellation before child creation must not invent an exit status.

The observation remains accessible after session close in its original process.
A forked process cannot read it through this session API. It grants no monitor
authority and is not recoverable after process death. Generic ProcessSession,
CLI exit policy, guest protocol, monitor result retention and PID 1 protection
are unchanged.

## Verification and later decisions

Focused tests distinguish failures before/after actual mailbox ACK,
malformed ACK responses, paused/chunked output, invalid terminal evidence,
non-success reasons, immutable metadata, same-process close versus fork,
interrupt propagation, real pinned-client binding/descriptor cleanup and
nonzero CLI error presentation. A fresh exact-SHA server public cold exec proof
checked unchanged normal run/exec/stop/rm behavior. Injected ACK uncertainty is
not a claim of a live dropped-reply or reconnect proof.

### Completed qualification — 2026-09-07

Implementation: `36173fa4d9c9f00c5e1cb9faab0d91c9ac6537fa`. Sol authored
source/tests; Astra managed the contract and verification, with a separate
read-only Astra approval before commit and push.

- Local final nine-file selection: 398 passed in 7.87 s. Exact pushed-SHA
  Linux selection: 398 passed in 75.21 s. Author 126 and reviewer 107 focused
  passes overlap with these selections and are not additive suite coverage.
- Repository Ruff lint, changed-file formatting, lane classification and diff
  checks passed. A repository-wide format check reported nine unrelated,
  unchanged files needing formatting; those files were not modified.
- Fresh public cold exec proof: 1 passed in 21.43 s, including detached launch,
  exec-status before/after, literal argv, separate stdout/stderr, exit 17,
  missing-command exit 127, app root and same-boot authenticated root evidence,
  PID 1 direct-access denial, stop/rm and source-image preservation.
- The source archive SHA-256 remained
  `862d4b9365f30e35a12ca48263223e4dfa11d00abb3ca68a428848e99e348458`.
  Successful test cleanup removed its VM/run; the domain list was empty.
  The old failed runtime evidence remains preserved. Its original resource
  failure's exact cause is still unknown.

Local evidence: `/tmp/palimpsest-g39.QUsDEE/local-selected.log`,
`lane-check.log`, `sol-result.txt` and `review-result.txt` in the same directory.
Server evidence:
`/tmp/palimpsest-g39-server-selected-36173fa4d9c9f00c5e1cb9faab0d91c9ac6537fa.log`
and `/tmp/palimpsest-g39-public-exec-36173fa4d9c9f00c5e1cb9faab0d91c9ac6537fa.log`.
The successful cold runtime was `/tmp/p-execcli-b8f0b60b`; preserved historical
failure evidence includes `/tmp/p-execcli-0b710ea6/launch.stderr`.

Guest and builder were unchanged, so no new image build, complete guest boot
matrix or full Gate 2 was run. The native proof covers normal ACK behavior;
lost-ACK, interrupt and binding failures were controlled test injections.

Disk retention, output-content storage and post-restart recovery require a
separate contract covering ownership, secret exposure, retention/deletion,
atomic persistence relative to ACK, and continuity of original-client authority.
They are not implied by this in-memory observation slice. Result takeover by a
different client, result discard and automatic replay remain out of scope.

The follow-up [durable record contract](oci-exec-durable-record.md) now defines
an opt-in metadata-only design and implementation/test slices. It does not yet
add disk persistence to this shipped process-local API or recover authority
after process death; the future reader would inspect local historical claims.
