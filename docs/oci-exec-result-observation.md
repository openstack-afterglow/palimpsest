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

Focused tests must distinguish failures before/after actual mailbox ACK,
malformed ACK responses, paused/chunked output, invalid terminal evidence,
non-success reasons, immutable metadata, same-process close versus fork,
interrupt propagation, real pinned-client binding/descriptor cleanup and
nonzero CLI error presentation. A fresh exact-SHA server public cold exec proof
checks unchanged normal run/exec/stop/rm behavior. Injected ACK uncertainty is
not a claim of a live dropped-reply or reconnect proof.

Disk retention, output-content storage and post-restart recovery require a
separate contract covering ownership, secret exposure, retention/deletion,
atomic persistence relative to ACK, and continuity of original-client authority.
They are not implied by this in-memory observation slice. Result takeover by a
different client, result discard and automatic replay remain out of scope.
