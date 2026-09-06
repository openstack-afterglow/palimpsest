# Bounded additional guest exec

The first additional-command engine keeps the VM's main workload alive while
one command runs in its own cgroup leaf. It does not use Docker or execute the
command on the host. Public capability is enabled only after native engine
qualification; the unchanged Gate 2 is a separate product check.

## Request and process boundary

Requests contain literal argv (1–64 arguments, at most 8 KiB canonical UTF-8
JSON) and a finite timeout (1–30,000 ms). Environment, cwd and credentials come
from the immutable image process configuration. No shell is inserted, and no
environment/user/cwd overrides, stdin, TTY, per-command signal or parallel exec
are supported. A caller can explicitly request an image-provided shell as argv.

The additional fork occurs after boot authentication. PID 1 explicitly wipes
the inherited lifecycle key/session and control buffers in the child, closes
supervisor descriptors, applies the existing capability/seccomp/private-mount
isolation and verifies the child before releasing it. Its stdin is `/dev/null`.
The main workload retains its original leaf. Additional leaf generations are
separate and are removed only after the command and descendants are gone.

## Authentication and bounded delivery

The existing v2 authenticated channel gains `EXEC`, `EXEC_OUTPUT` and
`EXEC_EXIT`. All retain the original run/boot/generation/nonce/epoch bindings,
global wire ordering and HMAC separation. Exec request IDs share the host's
monotonic request counter; an exec result cannot substitute for VM `TERMINAL`.

Output frames identify stdout or stderr, a contiguous decoded-byte offset and
canonical lowercase hex (at most 1,024 decoded bytes per frame). Both streams
share a 65,536-byte total allowance. Completion binds the exact accepted stream
counts and a real reaped leader status after cgroup cleanup. Completed commands
require pipe EOF; output-limit failure can discard unread bytes. Reasons
are `completed`, `timeout`, `output-limit` or `cancelled`. Only a cancellation
before any child was started can have a null process status, with zero output;
it must never invent a signal or successful exit.

The local monitor IPC server only submits/polls/acknowledges a bounded mailbox;
it does not block on guest execution. The live lifecycle worker alone owns the
authenticated channel. A monotonic mailbox sequence plus UUID token makes an
exact retry idempotent and old acknowledged submissions permanently stale.
One result remains until acknowledged after output drain. A disconnected
reader does not automatically discard that result or rerun its command, and
the finite guest deadline still applies. Lost/abandoned result recovery UX is
not part of this first implementation.

STOP closes new exec admission immediately and is checked between complete
output frames, not only when the channel would block. It cancels/drains any
additional command before VM terminal completion. A command timeout or output
limit kills only its own leaf; following commands can run after result ACK.
Unexpected channel loss preserves uncertainty, never fabricates completion or
replays a possibly executed command. Active-exec reconnect is unsupported.

Command content and output are volatile, bounded mailbox data, not additions
to the durable boot lifecycle transcript. Public process sessions deliver
separate guest stdout/stderr and the actual completed exit code; truncation,
timeout and cancellation are explicit failures even if a leader exited zero.

## Refusal diagnostics, not result recovery

The exec client now distinguishes validated mailbox lifecycle states before
submitting a command. Refusal closes the client without submitting, polling,
acknowledging or taking over another command. Invalid status fields are rejected
as a typed state error, including a non-string lifecycle state.

| Reported condition | Meaning and safe next step |
|---|---|
| Not ready | Check the run and wait for authenticated READY. |
| Stopping | New exec admission is closed; wait for shutdown and examine the existing results. |
| Terminal | This run has ended; inspect its terminal result. |
| Control lost | Preserve run evidence and any original client output. A command's outcome can be unknown; do not rerun it on the assumption that it never executed. |
| Ready but occupied | The previous command may still be running **or** its result may be unacknowledged. Let the original client finish consuming its result if it is available. |

Occupied status does not prove that a client is abandoned. There is no new
result-list, takeover, discard or recovery command. A disconnected client does
not authorize a fresh client to acknowledge its result. Diagnostics do not
change the one-command mailbox, authentication, result retention or STOP policy.

## Host resource diagnostics

Worker/packer process creation failures with `EAGAIN` or `ENOMEM` have an
explicit resource diagnostic instead of an undifferentiated spawn error.
Missing executables and permission failures are not mislabeled as resource
exhaustion. The isolated worker still exports only a fixed error category,
not raw exception text, source paths or command contents.

The diagnostic suggests checking applicable process/thread and memory limits;
it does not claim which limit was reached or count available process slots.
Linux has multiple possible reasons for these errors, including UID-wide,
cgroup and system limits, and memory or PID-namespace conditions. See
[fork(2)](https://man7.org/linux/man-pages/man2/fork.2.html) and
[pthread_create(3)](https://man7.org/linux/man-pages/man3/pthread_create.3.html).

No resource limit is raised, no unrelated process is stopped and no automatic
retry is introduced. The worker's configured process ceiling remains 256
(or a lower inherited limit); this is not a dedicated process-tree quota.
Packer failures that only report a generic nonzero exit are not promoted to a
specific resource diagnosis by guessing from stderr.

Partial helper-thread startup now retains ownership of the already spawned
worker and attempts bounded termination. Confirmed worker/group exit permits
scratch cleanup; an uncertain exit or failed deferred reaper preserves scratch
evidence. An interrupted thread start is not treated as proof that no thread
exists, so cleanup does not close a pin already handed to a possible reaper.
This does not promise automatic recovery when the host cannot start a reaper.

## Qualification and Gate 2 boundary

### Resource/diagnostic follow-up verification (2026-09-07)

Implementation `a4903d0e24572058689c285c0b3326809dc628d8` was independently
reviewed, pushed and fetched on `pieroot-server`. Focused local tests passed
239 cases, with seven Linux-only skips. The same seven selected files passed
all 246 cases on the server in 14.63 seconds, including worker/helper/reaper
failure injection and pinned-packer errno cases. Ruff, formatting and test-lane
manifest checks passed; this is not a full-suite result.

The separate public CLI native test passed at that exact implementation SHA
(one test, 20.13 seconds). A new runtime state directory forced cold layer
materialization from the existing immutable Palimpsest-built archive. Real
run/exec/stop/rm preserved literal argv, split output, exit statuses, image root
marker, failure recovery and source archive contents. The original PID 1 probe
still produced the expected access denial in this isolation check; Gate 2 was
not rerun or declared passed. No guest policy, root-proof criterion, resource
limit, Docker service or source artifact was changed.

Server logs: `/tmp/palimpsest-34-selected-a4903d0.log` and
`/tmp/palimpsest-34-public-exec-a4903d0.log`. The successful public fixture
`/tmp/p-execcli-c40a2261` was removed by the test after normal VM removal.
Pre-existing failure evidence was not targeted.

`tests/kvm/test_oci_exec_live.py` is an explicit engine proof using public
run/stop/rm and the production exec process API. It requires
`PALIMPSEST_OCI_EXEC_LIVE=1`, `PALIMPSEST_OCI_EXEC_LIVE_IMAGE` pointing to a
bootable BusyBox-based OCI archive, its adjacent `acceptance.json` generated by
`tests/e2e/prepare_local_oci_build.py`, and the existing five host BOOT variables.
It checks independent streams/results, sequential commands, image root marker,
descendant cleanup, timeout/output-limit recovery and STOP during exec.

The engine proof passed on pieroot-server at
`02af2879bd79f19cdbfb02cd687d965e78283d55` (1 test, 22.02 s), after building the
archive locally with Palimpsest's unchanged offline/network-none preparation
script. The base preserved the original amd64 BusyBox layer; only its image
CMD was changed to a long-lived qualification service before the product build.
Docker container export is not equivalent: it added nonempty `/dev` entries,
which correctly failed guest mount-target admission before READY. That failed
input and evidence remain preserved.

Another pre-VM attempt failed in the hard materializer worker. Isolated packing
worked without worker limits and later with the same limits; the server's
ambient real-UID thread count was close to `RLIMIT_NPROC=256`. This is consistent
with transient UID-wide process contention, not an established layer metadata
defect. No limit was raised and no unrelated service was stopped. Better
admission diagnostics and per-worker process containment remain follow-up work.
Linux documents `RLIMIT_NPROC` as a real-UID-wide thread count, not a private
worker process-tree limit: [getrlimit(2)](https://man7.org/linux/man-pages/man2/getrlimit.2.html).

`tests/kvm/test_oci_exec_cli_live.py` separately exercises public dispatch with
`PALIMPSEST_OCI_EXEC_CLI_LIVE=1` and the same image/host settings. The engine
opt-in does not enable this public CLI proof, and neither proof enables Gate 2.

At `e2bdbf155941fa22370b747cca7a0867705531f5`, the public CLI proof passed on
pieroot-server (1 test, 19.47 s). Public run/exec/stop/rm preserved literal argv,
separate streams, exit 17, missing-command exit 127, recovery after that error,
the image marker at `/` and `/proc/self/root`, and normal domain/run removal.
The unchanged image-baked Gate 2 probe was separately executed inside that VM:
it returned nonzero with no success output and `Permission denied` at the exact
`/proc/1/root/palimpsest-e2e-root-marker` path. This is an isolation check and
evidence of an acceptance conflict, not a substituted Gate 2 pass.

The unchanged `tests/e2e/test_local_oci_build_run.py` was actually executed at
the same SHA: 1 failure in 0.03 s, at its Docker-socket prerequisite before any
VM launch. `/var/run/docker.sock` and `/run/docker.sock` existed. No daemon was
stopped or socket hidden. A separate public `oci init-runtime` parent was
provided through `PALIMPSEST_STATE_HOME`; that real root has no runs. This does
not repair the gate's hardcoded XDG cleanup assertion for a future successful
run: actual selected-root cleanup must also be verified when resolving the gate.

Server logs are `/tmp/palimpsest-32-exec-02af287.log`,
`/tmp/palimpsest-32-public-exec-e2bdbf1.log` and
`/tmp/palimpsest-32-gate2-e2bdbf1.log`. The immutable product-build artifact is
`/tmp/palimpsest-exec-image.NrprYU/image.oci.tar`, SHA-256
`3987bfb8f337ba5333b041b719abf3f377f5cfd623b0894a05a5dfc858d6d4ec`.
Its adjacent receipt and local independent rootfs proof remain preserved.
Failed image/worker evidence remains at `/tmp/p-exec-a2325ee5` and
`/tmp/p-exec-cfe70c49`; only successful, exactly owned proof fixtures were
removed after normal public VM removal.

At the same public code SHA, the existing public lifecycle file passed 2 tests
in 37.88 s (foreground, detached STOP, SIGINT and removal). Thirteen selected
exec/protocol/transport/guest/initramfs/platform/dispatch/CLI/lane unit modules
passed 585 tests in 14.42 s; this is not the full suite. The first selected run
had 584 passes and one asset-mode failure: checkout had used the server's
group-writable umask, yielding `0664`. The exact owned packaged guest binary's
SHA was verified and its required `0644` mode restored before the same tests
passed; no test or artifact-content check was weakened. Future server checkout
must set `umask 022` before Git, not only before pytest.
Logs: `/tmp/palimpsest-32-public-lifecycle-e2bdbf1.log`,
`/tmp/palimpsest-32-server-selected-e2bdbf1.log` (first failure), and
`/tmp/palimpsest-32-server-selected-mode-e2bdbf1.log` (verified rerun).

The first rejected-boot fixture retained a control-lost monitor after its exact
domain was already absent. Normal transport shutdown intentionally refuses that
state. After independent review, an operational cleanup revalidated its original
PID 789325/start 112093680, generation, owned directory FD, typed journal,
authenticated PING, and absence of both domain UUID and name. It sent SIGTERM
through a pinned pidfd to that helper only. No domain mutation or file deletion
was performed; the journal stayed byte-identical. Audit:
`/tmp/palimpsest-32-failed-monitor-retirement.log`. This is a one-fixture cleanup,
not implementation of the still-pending public control-lost recovery UX.

Notion section 17.84 records the implementation and verification summary and
was fetched again to confirm it. Detailed operational-path/process evidence
was not sent there: that payload was rejected by the safety review, so the
successful update omitted it. The detailed evidence remains in this workspace
and the original server logs.

The unchanged Gate 2 probe additionally reads `/proc/1/root`. That dereference
requires ptrace-style access to PID 1 and conflicts with its intentionally
non-dumpable, capability-protected supervisor namespace. Do not restore PID 1
dumpability or grant `CAP_SYS_PTRACE` merely to pass a test: it would also expose
the supervisor's unmasked filesystem/descriptor authority. Record the actual
Gate 2 result separately; changing that acceptance contract requires an explicit
decision, not a silent substitution with the engine proof.
The ptrace check and target process's mount-namespace view are documented in
[proc_pid_root(5)](https://man7.org/linux/man-pages/man5/proc_pid_root.5.html).
