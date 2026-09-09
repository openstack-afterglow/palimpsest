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
