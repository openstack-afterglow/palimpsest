# Linux OCI process metadata

Palimpsest's OCI intake supports exactly Linux amd64. The legacy image-config
field `ArgsEscaped` may be absent, null, `false` or `true`; a present non-null
value must be a JSON boolean. Numbers (including 0 and 1), strings, arrays
and objects are rejected.

On this Linux path, either boolean leaves the process unchanged:
`Entrypoint` followed by `Cmd` forms the literal argument vector. Palimpsest
does not join, split, unquote, unescape or insert a shell. Spaces, quotes,
backslashes and shell metacharacters remain ordinary argument bytes. An image
that explicitly names a shell still executes that image-supplied shell; this
rule does not prevent the image program from interpreting its own arguments.

## Basis and boundaries

The [OCI image specification v1.1.1](https://github.com/opencontainers/image-spec/blob/v1.1.1/config.md)
describes `ArgsEscaped` as a deprecated Windows compatibility field and permits
null for optional properties. Moby v28 marks the field
[Windows-specific](https://github.com/moby/moby/blob/v28.0.0/api/types/container/config.go)
and its [Linux process construction](https://github.com/moby/moby/blob/v28.0.0/daemon/oci_linux.go)
copies the executable and argument vector. The Linux interpretation above is
based on these contracts, not a Windows command-line implementation or a
promise of complete Docker compatibility.

All existing process type/size/bootability checks and the Linux amd64 image
platform gate remain. Windows and other architectures are not enabled.
The original config bytes, descriptor digest and source snapshot binding
remain authoritative and are not rewritten. Configs differing only in the
boolean can produce the same canonical process but different source identities.
There is no process schema, boot-plan version, materialization recipe or guest
binary change. Existing defaults and explicit `--user` provenance are retained.
PID 1 protection, capability removal, no-new-privileges, securebits, seccomp,
filesystem validation and VM-exclusive root ownership are unchanged.

## Focused verification

Use the process and source regressions first, then the affected image, request,
store and adapter consumers described in [testing](testing.md). The original
NGINX archive has an independently opted-in native proof. A parser pass is not
service qualification: require real default startup, readiness, public exec,
actual root comparison, PID 1 access refusal and normal stop/removal. The proof
uses network none and does not test external HTTP reachability. Preserve the
original failed qualification and unchanged archive even after a later pass.

## Verified parser / failed NGINX checkpoint — 2026-09-08

At [866d5e6](https://github.com/openstack-afterglow/palimpsest/commit/866d5e675b3c31f81f790468aa6d228251a13c88),
the focused nine-module selection passed 661 tests locally (22.77 s), with one
Linux-only openat2 skip. The same selection passed all 662 tests on the exact
pushed SHA on `pieroot-server` (191.59 s). Ruff, architecture and lane inventory
checks passed; code and native plan received independent Astra approval after
Sol implementation. This is not a full-suite result.

The unchanged original NGINX archive native test **failed** (20.71 s). It passed
the former ArgsEscaped intake rejection and console observations show actual
root transition, isolated workload startup and original entrypoint execution.
NGINX then reported permission denied opening `/var/log/nginx/error.log` and
exited with status 1 before detached startup returned. Readiness, subsequent
public exec/root-report comparison/PID 1 refusal and stop/rm did not pass.
Console root markers alone are not the missing authenticated public comparison.

Read-only listing of the preserved lower image shows `error.log -> /dev/stderr`
and `access.log -> /dev/stdout`. Source inspection of
`guest/stage1/init.c:prepare_workload_mount_boundary` and
`safe_workload_dev_entries` shows a private mode-0755 `/dev` with exactly six
devices (`null`, `zero`, `full`, `random`, `urandom`, `tty`) and no standard-stream
symlinks. The missing paths are a source-based explanation consistent with
the observed error, not a traced-syscall proof. Link creation alone has not
been qualified; inherited-FD reopening permissions may also matter. No image,
user, capability or guest device-policy change was made to hide the failure.

The new NGINX definition `hub-nginx-a6ccaf95` (UUID
`475bf9eb-fe11-45fb-8f27-d689d71b0586`) is shut off with autostart disabled.
Its runtime at `/tmp/p-hub-nginx-193990e3`, root and logs remain preserved,
alongside the prior inactive Redis definition and all earlier retained data.
The original NGINX archive SHA256 remains
`64e3f9852293174971bbd81e54bf0093896fc0ee525c9b0494763d5208ffa029`;
other original Hub and existing built-image hashes also remain unchanged.

Independent plan review first caught postflight checks conditional on test
success. The revised wrapper runs every preservation observation even after
failure, preserves the primary test exit code and passed 225 mocked-shell
scenarios. In the actual NGINX failure it correctly reported the extra inactive
definition while still verifying the old Redis UUID/state/autostart, all source
hashes, zero active VMs and exact clean product SHA. No failed VM was deleted
to satisfy inventory checks.

The separate existing built-image cold exec regression also **failed** at this
same SHA (5.51 s), during launch with
`OCI runtime ancestor changed during verification`. An independently approved
cold-only plan preserved both earlier inactive definitions. The new `exec-cli`
definition (UUID `415ba534-2eaf-4135-92dc-f033311e7b47`) is now also shut off
with autostart disabled, with failure evidence at `/tmp/p-execcli-9d99c203`.
Postflight retained the primary failure and checked all 17 observations: the
extra-definition inventory check failed as expected; all four archive hashes,
both previously preserved UUID/state/autostart checks, zero active domains,
exact SHA and clean tree passed. No retry or manual cleanup followed.

`oci_host.py:verify_runtime_parent` rechecks ancestor identity, ownership, mode
and ctime around ACL verification and again through held/visible paths. Which
ancestor or field changed in this attempt is not known; normal directory
activity is a candidate, not a confirmed cause. The parser patch did not change
this verifier. The failed cold test is not a new default-run pass, and earlier
successful cold results cannot substitute for it. A narrow diagnostic and a
reviewed retry plan preserving the existing `exec-cli` name/definition are
required before another native attempt.

The next guest task must define and independently review a narrow standard-I/O
path contract with positive/negative C and native tests before changing the
device allowlist or packaged ELF. Direct registry-reference intake remains a
separate unimplemented task. Neither the parser change nor this failed service
test constitutes a new application build or full Gate 2 qualification.

## Standard-I/O pathname review (2026-09-09, not implemented)

Inherited descriptors and path-based reopening are separate contracts. The
current workload can write its inherited stdout/stderr, but the private `/dev`
does not publish `/dev/stdin`, `/dev/stdout`, `/dev/stderr` or `/dev/fd`.
The [Linux proc FD documentation](https://www.man7.org/linux/man-pages/man5/proc_pid_fd.5.html)
also distinguishes permission to use an existing descriptor from permission
to reopen its underlying inode. Therefore merely adding links is not a proven
fix for the original NGINX failure, particularly after dropping credentials.

A candidate narrow contract is three fixed links to `/proc/self/fd/0`, `/1`
and `/2`, with an explicit decision on whether the more general `/dev/fd`
alias is needed. This is a proposal, not an expanded allowlist. The existing
six device nodes must remain exact, and extra entries, altered targets,
wrong types/owners and cross-PID targets must be rejected. Creation and
verification must fit the private tmpfs inode budget, including temporary
cgroup staging. PID 1's FD masks and capability/securebits/NNP/seccomp boundary
must remain unchanged.

Before implementation, qualify reopening separately for the main workload's
inherited console and additional exec's stdout/stderr pipes, for numeric root
and non-root identities. `exec_child` duplicates separate pipe endpoints onto
FDs 1/2; this is not evidence that their inode ownership permits reopening
after credential drop. Do not chmod the shared console or add capabilities
to bypass this distinction. Any stream transport change needs its own review.

Required evidence remains: real-C positive/negative allowlist tests, packaged
ELF reproducibility if C changes, original unchanged NGINX default-process
native proof, and a separate cold public lifecycle proof. Preserve the three
failed definitions and use a reviewed unique-name strategy for cold testing;
the existing fixed `exec-cli` test name now collides with retained evidence.
No new native test or application build is claimed by this review.

## Bounded stdout/stderr alias implementation (2026-09-11, test-defined)

The implemented guest source keeps the existing six private character devices
and adds only `stdout -> /proc/self/fd/1` and
`stderr -> /proc/self/fd/2` inside the workload child's private `/dev` mount.
It intentionally does not add `/dev/stdin`, `/dev/fd`, cross-PID aliases, new
devices, capabilities, groups, or a parent-side pathname open. Link creation
does not overwrite a pre-existing object. Each link must remain a root-owned,
single-link mode-0777 symlink with its exact bounded target under no-follow
metadata inspection. A bounded buffer larger than the exact target makes both
suffixes and truncation reject rather than compare as a prefix.

The directory allowlist now requires exactly the original six device names and
these two link names. The device entries are rechecked as root-owned,
single-link mode-0666 character devices with their exact major/minor pairs.
The whole policy runs immediately after construction and again after the
temporary cgroup staging entry is removed. These guarantees end at workload
release: a capabilityless numeric-UID-0 workload can still mutate its private
mode-0755 `/dev`, so the source does not claim immutable aliases after launch.

`tests/unit/test_workload_dev_aliases.py` compiles the production C helpers and
exercises the exact positive set plus pre-existing entry, wrong target, wrong
type, hard-link/extra-entry, missing-entry, and generic `/dev/fd` controls in a
bounded private tmpfs. Its portable callsite assertions keep creation after the
private tmpfs mount and before credential drop, and preserve the later cgroup
lifecycle revalidation. This is focused component evidence only. Until the
packaged ELF is reproducibly rebuilt and native probes pass, it is not an
original-NGINX compatibility success or a new Gate 2 qualification.

A small unprivileged Linux probe on `pieroot-server` confirmed the distinction:
writing through a newly created anonymous pipe's existing descriptor succeeded,
while reopening that same endpoint via `/proc/self/fd` after setting its inode
mode to zero returned EACCES. The probe closed both endpoints and touched no
VM or filesystem file. This demonstrates the general permission boundary, not
the actual guest console/pipe modes or the cause of NGINX's failed open.

## Host churn and standard-I/O probe checkpoint — 2026-09-09

The investigation started from clean baseline `3fca07d`. The original
read-only NGINX lower still has `error.log -> /dev/stderr` and
`access.log -> /dev/stdout`. Production `prepare_workload_mount_boundary`
creates exactly the six documented device nodes and no aliases. Upstream
NGINX `ngx_log_init` opens the log with `NGX_FILE_APPEND` and
`NGX_FILE_CREATE_OR_OPEN` in
[`ngx_log.c`](https://github.com/nginx/nginx/blob/master/src/core/ngx_log.c),
while
[`ngx_files.h`](https://github.com/nginx/nginx/blob/master/src/os/unix/ngx_files.h)
maps those flags to `O_WRONLY | O_APPEND | O_CREAT`. This is source-based
supporting evidence, not an exact syscall trace of the original binary.

The main investigation ran `/tmp/palimpsest-g56.BHPdp1/stdio-probe.py` in the
existing Python image `d7c79db7d957` as a disposable read-only, no-network
Docker container with no host mounts, a private 64 KiB tmpfs, 128 MiB memory,
0.25 CPU and 16 PIDs. The parent retained only `SETUID`/`SETGID` setup
capabilities before selecting UID/GID 101 with empty supplementary groups. In
a root-owned mode-0755 simulated device directory, opening a missing `stderr`
link without `O_CREAT` returned `ENOENT`, while using `O_CREAT` returned
`EACCES`. Writing through an inherited root-owned mode-0600 pipe succeeded,
but reopening the present self-FD alias returned `EACCES`. All four expected
results passed.

This was not an actual guest-console qualification and did not fix NGINX. The
read-only check `sudo -n true` required a password; passwordless sudo was not
available and no host privilege was relaxed. The current three VMs remain shut off and were not
rebooted or deleted. Ordinary direct-child churn on a checked host ancestor is
now a plausible mechanism, but the historical failure lacks phase, depth and
field diagnostics and remains unattributed. The ctime and ACL checks remain
intact.

The next concrete gate is a main-console and exec-pipe ownership/reopen matrix
under UID 0 and UID 101 with the existing security policy, independently
reviewed before any guest change. Do not chmod the shared console or add
capabilities. This checkpoint is not evidence of a root image build, NGINX
qualification, native proof or Gate 2 pass.

## Separate native stdio diagnostic

`tests/kvm/test_oci_stdio_cli_live.py` defines two separately selectable
UID 0/101 diagnostic cases under `PALIMPSEST_OCI_STDIO_CLI_LIVE=1`.
Each uses a fresh scratch OCI fixture with the test-only static
`tests/kvm/assets/stdio-fd-probe.c`, the existing public-CLI layout builder,
and explicit numeric `run --user UID:UID`. The fixture is not the original
NGINX image or a Palimpsest application-build acceptance artifact.

The main process and a public additional exec report their own FD 1/2
metadata and `/proc/self/fd/1` / `/proc/self/fd/2` reopening result. Reopening
uses write-only, append, nonblocking and no-controlling-terminal flags;
there is no create/truncate, stdio read, device creation or permission change.
Reports are written through both inherited output descriptors. The main
device's type/mode are observations rather than assumed compatibility facts;
the additional-exec pipe contract is checked separately. Missing standard
stream aliases, empty capabilities/groups, locked securebits, NNP/seccomp,
and denied direct PID 1 root access remain required observations.

Each case compares the main and exec root identities with the authenticated
public root report. Only successful cases perform normal stop/removal of
their own fresh VM; diagnostic artifacts remain, and failures preserve their
runtime. Execute sequentially with 512 MiB and one vCPU, exact pushed code,
and independent checks of retained domains and original archive hashes.
Portable parser/layout checks and a local cross-compilation are not native
evidence. No guest stage-1 source/ELF, production stream transport, device
allowlist or security policy changes are part of this diagnostic.

### Native diagnostic result — 2026-09-09

On exact pushed `cc1fca7`, the two sequential UID 0/101 cases passed on
`pieroot-server` in 30.54 seconds. Each fresh VM used 512 MiB, one vCPU and
no network. The pinned GCC static build succeeded. The preceding `8055f68`
attempt failed before VM boot on `-Werror=misleading-indentation` (1.64
seconds); only the final C control-flow statements were split into braced
blocks, without disabling warnings or changing probe semantics. Its evidence
at `/tmp/p-stdio-0-728b7dc5` remains preserved.

| Process | FD 1/2 type and owner/mode | UID 0 reopen | UID 101 reopen |
| --- | --- | --- | --- |
| Main | character console, 0:0, 0600 | success | EACCES |
| Additional exec | separate FIFO pipes, 0:0, 0600 | success | EACCES |

Both identities successfully wrote their reports through inherited FD 1/2.
Both lacked `/dev/stdout` and `/dev/stderr` (ENOENT). In all four reports,
all five capability sets and supplementary groups were empty, securebits
were 239, NNP was 1, seccomp was 2, and direct PID 1 root access returned
EACCES. Main and exec root device/inode matched the authenticated public
root reports before and after exec, with run/boot/domain identity unchanged.
Normal stop/removal and absence checks passed for both fresh diagnostic VMs.

Evidence remains at `/tmp/p-stdio-0-0ddf7126/evidence` and
`/tmp/p-stdio-101-b52f06e6/evidence`: compile output, CLI results, public
root reports and `diagnostic-receipt.json`. All 17 preflight and all 17
postflight observations passed, preserving the three old inactive domains
with their UUID/state/autostart and all four original archive hashes. No
active VM remained. Focused parser, fixture, lane and architecture checks
passed locally (113, 9.17 seconds) and on this exact server SHA (113, 7.46
seconds); these are separate from the two native cases and not a full suite.

This confirms the actual guest's inherited-write versus pathname-reopen
boundary, not the exact syscall cause of the original NGINX failure. Fixed
self-FD aliases alone would not resolve the observed UID 101 DAC failure.
The next design must review private workload-owned output transport and
strict fixed aliases, without chmod of the shared console or capability
additions. Production guest C/ELF and PID 1 protection remain unchanged.
Original NGINX qualification, a new application build and full Gate 2 remain
uncompleted by this diagnostic.

## Additional-exec output ownership boundary

The next implementation slice is intentionally smaller than the combined
transport-and-alias proposal. Additional exec alone now requires both endpoints
of its stdout and stderr pipes to begin as distinct root-owned FIFO inodes with
mode `0600`, changes only those output inodes to the already resolved process
UID/GID before fork, and verifies that inode identity, type and mode remain
unchanged. The isolation, child-error and release pipes stay root-owned and any
failure refuses the exec before a child is created.

This slice does not change the main character console, `/dev`, standard-stream
aliases, output limits, lifecycle protocol, security policy or terminal drain
semantics. The earlier native observations therefore remain historical
evidence for the unchanged implementation at that checkpoint, not a claim
about this new packaged guest. A later main-output design must use a separately
opened nonblocking console description, fixed bounded fair buffers, continued
lifecycle/STOP service, and require output EOF plus successful flush before
terminal publication. A deadline or permanent sink failure must report
incomplete delivery rather than silently discard output. That design remains
unimplemented and requires its own review.

### Exec-only qualification checkpoint — 2026-09-09

Exact pushed `bb04a5b` passed the final local focused selection (297 tests,
17.36 seconds) and packaged ELF checks (34 tests, 10.11 seconds, including two
pinned rebuilds). The same server SHA passed 297 focused tests in 24.92 seconds
and 34 binary tests in 16.69 seconds. The new callsite harness executes the
actual production helper and start function with syscall/cgroup doubles:
eleven faults require no fork, all ten endpoints closed and session cleanup;
its successful case checks exact control ownership, FD mapping and flags.
A separate real-pipe test verifies UID 101/GID 202 reopening after credential
drop. Neither kind of C harness substitutes for guest execution.

The first server focused run had 296 passes and one failure (25.09 seconds):
all provenance hashes matched, but checkout had created the packaged ELF as
`0664`, not the required `0644`. Read-only inspection found shell umask `002`
and no extended/default ACL. Fresh scratch checkouts reproduced `0664` under
`002` and `0644` under `022`, with identical content. After independent review,
only that exact no-follow, single-link, identity/hash-verified package file was
changed to `0644`; content and ownership remained unchanged. The strict test
was retained, the failed log and scratch experiment were preserved, and no VM
had been started. Future checkout commands set process umask before Git,
without changing global settings or normalizing VM/image permissions.

The subsequent required stage-1 proof passed all 43 boots / 44 QEMU invocations
in 122.20 seconds, with the private evidence retained. The two sequential
public stdio cases passed in 30.52 seconds, with these observed FD 1/2 results:

| Identity / process | Type, owner and mode | Self-FD pathname reopen |
| --- | --- | --- |
| UID 0 main | character console, 0:0, 0600 | success |
| UID 101 main | character console, 0:0, 0600 | EACCES |
| UID 0 additional exec | separate FIFO pipes, 0:0, 0600 | success |
| UID 101 additional exec | separate FIFO pipes, 101:101, 0600 | success |

All four reports retained capability sets/groups zero, securebits 239, NNP 1,
seccomp 2 and denied direct PID 1 root access. Main/exec root identities matched
the authenticated public root reports before and after exec. Standard stream
aliases remained absent. Both diagnostic VMs passed normal stop/removal, and
their private receipts remain retained.

The separate unchanged Palimpsest-built archive cold public lifecycle proof
passed in 21.43 seconds using a fresh runtime: literal argv, split streams,
exit status, root/PID 1 checks and normal stop/removal. Its successful
temporary runtime was removed by the existing proof. This reused an existing
immutable application artifact; it was not a new application build.
Each of the three native runs passed all 18 preflight and 18 postflight
observations, including the three historical inactive domains' UUID/state/
autostart, four archive hashes, no active VM and no remaining QEMU process.
The earlier failures remain preserved. Main-output transport, fixed aliases,
original NGINX qualification and full Gate 2 remain separate unfinished work.

## Main-output transport

Production PID 1 includes `guest/stage1/main_output_pump.h`. Before the main
fork it creates distinct stdout and stderr FIFOs, verifies their identity and
root-owned `0600` mode, changes both endpoints to the resolved workload UID and
GID, and verifies them again. The inherited child write ends are blocking and
become fd 1 and 2; only the parent read ends are nonblocking. Main-output FDs
are removed from additional exec children. Creation, ownership, flag, dup and
close faults fail closed.

Each stream has a fixed 4 KiB buffer and contributes at most one 1 KiB read per
pump tick. Chunks enter the same fixed 16 KiB queue as PID 1 diagnostics.
Workload enqueue is all-or-none backpressure, so a retry cannot duplicate
bytes; diagnostic overflow and permanent source or sink errors are sticky.
An actual console flush performs at most one nonblocking 1 KiB write per tick.
The design preserves each stream's byte order, diagnostic enqueue order and
the order in which PID 1 observes workload chunks. It does not claim a total
real-time ordering between independent stdout and stderr pipes.

The sink remains a distinct nonblocking, close-on-exec, write-only OFD reopened
through the fixed `/proc/self/fd/1` magic link and bound to the original
root-owned `0600` kernel console identity (`5:1`) across root transition. No
`/dev/console` pathname is reopened. Once this sink is acquired, diagnostics
are enqueued and never fall back to inherited blocking stdout or stderr.
Ordinary diagnostics retire after the last terminal diagnostic; authenticated
console `BOUNDARY_ACK` frames remain a distinct, non-discardable control path.
Post-close diagnostic attempts are discarded. Direct writes remain only for
pre-acquisition bootstrap handling and non-PID fixture execution.

Normal supervision, STOP handling and teardown continue pumping output. One
cleanup budget allows up to five seconds for graceful progress; after forced
cgroup cleanup, at most one additional second is available for final output
drain. Natural exit also gives descendant writers a bounded opportunity to
close their inherited endpoints. An early `ECHILD` observation is not success
until both sources reached EOF, their buffers are empty, and the shared queue
is empty and healthy. Deadline expiry, residual bytes or permanent I/O failure
prevents normal TERMINAL publication. These userspace bounds do not guarantee
a hard deadline for a task stuck in an uninterruptible kernel state.

On success the order is root quiescence, terminal diagnostic enqueue, bounded
drain and held-sink revalidation, then authenticated TERMINAL publication.
Main pipe ownership is retired, while PID 1 retains its one nonblocking
console descriptor for terminal reconnect. Closing it before TERMINAL would
disable the existing authenticated boundary protocol; adding another descriptor
to the same console would not isolate device backpressure. Terminal service
polls pending console writes and uses a separate five-second pending-boundary
delivery deadline, without extending the workload's five-plus-one-second
cleanup budget. A control failure wipes secrets and closes the sink before
waiting fail-closed; it does not rewrite the completed workload's exit cause.
Thus drain is output completeness evidence, not lifecycle authority by itself.

The build provenance uses versioned source-bundle framing over the named `init.c` and
`main_output_pump.h` inputs rather than treating `init.c` alone as the compiled
source. Portable/component evidence is distinct from the separate native VM,
UID stdio and public lifecycle proofs, which passed at the `9736132` checkpoint
below. No earlier compatibility checkpoint is rewritten as proof for this
output path.

Local integration verification (2026-09-10): the frozen source and rebuilt ELF
passed 239 focused output/initramfs/filesystem/stage-1/manifest/architecture
checks, 343 lifecycle/control/transport/qualification/proof checks, 64 actual
C guest-exec/ownership/transition checks and 34 packaged-ELF checks, including
two reproducible pinned-toolchain rebuilds. These disjoint selections total
680 passed; no full-suite or native result is implied. Independent review
also executed the new parser EINTR and 64-frame fairness harness.

Intermediate failures remain distinct: strict C fixture compilation exposed
unused-helper/indentation issues during concurrent edits, then passed after
correction; five manifest checks failed before the new control test was
registered, then the complete focused selection passed. Six local protocol
tests initially failed because the sandbox refused Unix socket bind; the
identical 343-test selection passed with that local socket restriction lifted.
The new ELF SHA-256 is
`3cad3fd4667d063d3689a9a9a82e93d1fe7406292c6d2a00d291f49d65822137`;
the canonical two-file source bundle SHA-256 is
`10ec84029efa76f36874ea63d851aaa98a1339b3549fc1be21664904c27d7152`.
GitHub publication and exact-SHA server/native qualification remain pending
at this local checkpoint; existing failure records are not replaced.

The integration was subsequently pushed as `81e0180` and fetched by the native
host at the exact SHA. Before any VM run, its focused checks exposed test-only
Linux GCC portability defects (missing `stdint.h` and misleading indentation).
The follow-up adds the explicit include and separates fixture statements;
production guest source, packaged ELF and all assertions are unchanged. Local
focused checks passed 28 cases, and the three affected generated fixtures
compiled under the pinned Linux GCC and passed all 15 runtime scenarios.
The first SSH pytest invocations also inherited umask `002`; unlike the Git
fetch, those invocations omitted the required `022`. No verifier was relaxed
and no existing artifact was chmodded. With process umask corrected, the same
server SHA passed the 372 protocol/initramfs checks and 98 actual-C/ELF checks.
Final output checks on the follow-up SHA and all native runs remain pending.

The exact follow-up `e585ce1` passed all 239 output-focused server checks and
the architecture guard. Its first native stage-1 positive workload failed
after STOP dispatch and the workload's stop-observed marker (14.32 seconds),
with lifecycle rejection stage 21 / errno 5; no remaining matrix, stdio or
cold native case was run. All 21 preflight and 21 postflight preservation
checks passed, with no active VM or QEMU and all prior domains/archives intact.
Source investigation traced the failure to `read_control_frame`: expiration
of the supervisor's graceful cleanup slice was treated as a protocol error,
even when no partial frame existed. The correction distinguishes caller
slice exhaustion (yield with parser state intact) from the actual frame's
five-second timeout (reject). Neither the 5+1 cleanup budget nor authenticated
protocol, output completeness or PID 1 protection is relaxed. The failed
native result remains evidence; the corrected guest requires a fresh rebuild
and exact-SHA native verification.

The corrected local guest passed 245 focused output/initramfs/filesystem/
stage-1/manifest/architecture checks, 343 protocol/proof checks, 64 actual C
checks and 34 ELF/reproducible-build checks (686 total). The actual reader
regression also reconstructs the previous conflated logic and requires its
empty-parser expiry case to fail; the corrected reader preserves partial
bytes on slice exhaustion, retains a full five-second frame deadline and
rejects actual frame expiry or clock failure. Independent review passed.
The rebuilt ELF SHA-256 is
`737d736319710de05d9318b7cbcf380849de47b99631717c96bc25498a2b6cea`;
source-bundle SHA-256 is
`9538af57001c1c968d1d9a7728ad7839cd289d3c387deca5015be9c665fbd331`.
These are local checks, not a replacement for the failed native proof.

On exact `d32328a`, the native host passed the same 686 selected checks.
The next stage-1 run reached final receipt construction but failed the retained
console ordering predicate (121.37 seconds). A separate line-number-only
diagnostic reproduced that rejection (122.19 seconds); all preceding marker
count checks had passed. A shorter two-boot observer then emitted only fixed
marker ordinals and deliberately stopped without qualification (21.51 seconds).
It confirmed that the first authenticated boundary preceded the workload's
signal-armed marker; the remaining expected order was intact. Each run passed
all 21 preflight and 21 postflight preservation checks, with no active VM/QEMU.

The proof host had closed composite connection one as soon as READY_COMMITTED
arrived, without waiting for the already-counted workload signal-armed marker.
The new pipe path permits that child marker to arrive later. The correction
waits for both markers before that one intentional disconnect. It does not
change guest source/ELF, authentication, negative reconnect controls or the
receipt's exact marker counts/order. The portable six-connection fixture now
also exercises delayed signal readiness; the original ordering case remains.
UID stdio and cold native qualification remain unrun until this gate passes.
The five-file synchronization follow-up passed 423 selected local protocol/
proof/manifest/architecture checks and independent review. The delayed variant
requires the connection to remain open before emitting signal readiness, then
completes the original six authenticated connections. No guest rebuild is
needed because this change only corrects host proof synchronization.

## Main-output native checkpoint: 9736132 (2026-09-10)

The synchronization follow-up was pushed as
`973613231ff0e57f0c11f5ed5c57fd673b924e71`; its exact server checkout passed
423 focused protocol/proof/manifest/architecture checks (44.59 seconds).
This follows the unchanged guest's 686 local and exact-server checks at
`d32328a`; these overlapping selections are not added into a full-suite count.

The ordinary, uninstrumented stage-1 matrix then passed all 43 boots / 44 QEMU
invocations and receipt validation (121.87 seconds). The two sequential public
UID 0/101 stdio probes passed (31.59 seconds). Main and additional-exec FD 1/2
were workload-owned `0600` FIFOs with successful self-FD reopening for both
identities. Capability sets and supplementary groups remained zero, securebits
239, NNP 1, seccomp 2 and PID 1 root access denial remained enforced; actual
main/exec roots matched authenticated reports. Stream aliases remain absent.

The same SHA's existing Palimpsest-built image passed the cold public
run/exec/root/PID1/stop/rm proof (21.14 seconds), including literal argv,
split stdout/stderr and exit status. This reused an immutable existing image,
not a new application build. Each native run passed all 21 preflight and
21 postflight checks: four preserved inactive domain identities/states/
autostart settings, four archive digests, clean exact-SHA checkout and no
active VM/QEMU. Successful proof VMs were removed only by their own existing
normal cleanup; prior failed domains/runtime evidence were preserved.

The stdio and cold runs each used a fresh private healthy journal directory;
their `commands.jsonl` files passed owner/0600/single-link/schema/sequence
checks with 28 records (14 start/end pairs) each. This does not install the
operational `/var/log/palimpsest` or `/var/lib/palimpsest` directories or the
service account. Those still require administrator setup. The main-output
path is now live-verified within these explicit proofs; original NGINX,
standard stream aliases, a new application build and full Gate 2 remain
separate unfinished work. Earlier failed runs and bounded diagnostics above
are retained as failures/diagnostics, not rewritten as passing qualifications.

## Bounded stdio-alias verification checkpoint: c563867 (2026-09-11)

The bounded stdout/stderr alias implementation and its synchronized native
proof fixtures were frozen at exact
`c563867c23c67f352e40774a8068cf11939fe0e7`. Local verification passed 17
actual-C helper cases, 34 packaged-ELF/reproducible-build cases, 346 guest
consumer cases, 101 output-focused cases with 12 explicit platform/opt-in
skips, and 113 alias/native-contract cases. These selections overlap and are
reported independently; they are not summed into a full-suite total.

The exact server checkout passed the combined core/qualification selection:
1,357 tests in 51.59 seconds. Its separate guest plus packaged-ELF selection
passed 623 tests in 70.46 seconds. The production stage-1 ELF SHA-256 is
`f8fe63805583a4b9bd33a2974f66f58f98c8c2b8589ee6ae748931c1081d67d5`;
the canonical source-bundle SHA-256 is
`9ec5562b88200e6290d227beb3b8e41785b348e4fe6e1a079d660362fbed2dab`.
The independent workload-proof ELF SHA-256 is
`0d01eeed6b695be965abeda6b7b6caefb4f4efa91228364ba9b2dbdcaf8a6cf4`,
and its source SHA-256 is
`519e458dec5cbe61de6bc0208c63d32c311e8596f99f3beb6a303d510c1d4b1e`.

Two preliminary failures remain part of the record. The first fixture-pin
update used the raw manifest-file hash where the verifier requires canonical
JSON digest and retained the previous proof-ELF size; the unchanged strict
verifier rejected both until the canonical digest and reproduced size were
synchronized. A separate local protocol selection initially failed because
the sandbox denied Unix-socket creation with `EPERM`; the same assertions
passed when rerun with that local socket restriction lifted. Neither failure
was converted to a skip or addressed by relaxing production validation.

At `2084133286201f738bd300498526968100d29d94`, the package workflow recorded
10 failures among 1,347 passes because a retained-root consumer still pinned
the stale workload-proof identity. The consumer pin was synchronized without
changing retained-root safety checks. The follow-up exact `c563867` package
workflow run `34500872305` succeeded.

The unchanged stage-1 native matrix then passed its one selected test in
121.81 seconds: all 43 boots / 44 QEMU invocations and receipt validation
passed. All 24 preflight and 24 postflight preservation observations passed.
The retained stage-1 evidence is
`/tmp/palimpsest-alias-stage1-0c3fdd1a`, with wrapper observations under
`/tmp/palimpsest-alias-wrapper-rzjlz72q`.

The two sequential UID 0/101 V2 stdio cases passed in 31.26 seconds. Main and
additional-exec FD 1/2 remained separate workload-owned `0600` FIFOs; each
process observed the exact root-owned `/dev/stdout -> /proc/self/fd/1` and
`/dev/stderr -> /proc/self/fd/2` aliases, reopened the intended FD identity,
and wrote exactly one marker through each pathname. The existing capability,
group, securebits, NNP, seccomp, authenticated-root and PID 1 denial assertions
also passed. All 24 preflight and 24 postflight observations passed, and the
private journal passed with 28 records. Runtime evidence remains at
`/tmp/p-stdio-0-f26b91dd` and `/tmp/p-stdio-101-ce52426f`; wrapper and journal
evidence remain at `/tmp/palimpsest-alias-wrapper-eus_sfxm` and
`/tmp/palimpsest-alias-stdio-journal-uz324nhz`.

The preserved original Docker Hub NGINX archive then passed its unchanged
default-process native test in 31.65 seconds. The image argv and configured
user were used without override; readiness, public exec, root identity, PID 1
refusal, normal stop and removal all passed. All 24 preflight and 24 postflight
observations passed. The private journal passed with 20 records, including the
one expected additional-exec error for PID 1 refusal. Runtime, wrapper and
journal evidence remain at `/tmp/p-hub-nginx-e3777da3`,
`/tmp/palimpsest-alias-wrapper-vrunrz2t` and
`/tmp/palimpsest-alias-nginx-journal-1gz7b22l`.

Finally, the unchanged existing `g35` Palimpsest-built image passed its cold
public lifecycle test in 22.08 seconds. Literal argv, split stdout/stderr,
exit status 17, image-root identity, PID 1 refusal, missing-command status 127,
continued exec availability, and normal stop/removal all passed. The successful
test removed only its fresh `/tmp/p-execcli-9ce6521b` runtime through its normal
cleanup. All 24 preflight and 24 postflight observations passed. Wrapper and
journal evidence remain at `/tmp/palimpsest-alias-wrapper-pbdlre_5` and
`/tmp/palimpsest-alias-cold-journal-87gahkr0`; the journal passed with 28
records and exactly the three expected additional-exec errors.

These four native selections were finite and sequential; their counts and
durations are not a full-suite total. The NGINX result is a preserved-archive,
default-process, network-disabled process/readiness proof, not HTTP serving.
The cold result reused an existing immutable built image, not a new application
build. This checkpoint does not claim direct registry `run`, HTTP
serving/networking, a new application build, or full Gate 2 qualification.
