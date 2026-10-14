# Linux storage and logging rollout

## Install-and-use defaults

Regular Linux users select their existing `~/.local/state/palimpsest` directory
without setting state or logging environment variables. Fresh users initialize
that same private directory on the first state-using command; existing store
metadata, tags, run/project ledgers, volumes, and build records stay in place.
For example, `palimpsest store show` and `palimpsest store ls --kind image` are
local commands and need no Hub URL or token.

Explicit roots retain their precedence for every identity:

1. `PALIMPSEST_STATE_HOME`
2. `[storage].state_root` in `config.toml`
3. an explicitly set `XDG_STATE_HOME`, with `/palimpsest` appended
4. the identity-specific default

On Linux, effective UID 0 and the dedicated effective account named
`palimpsest` with home `/var/lib/palimpsest` retain the system state default.
Effective account lookup, not `USER`, `LOGNAME`, home-directory contents, low UID
heuristics, or a write-access probe determines that policy. An unknown account,
failed lookup, or conflicting named-service home refuses default selection
rather than silently choosing user storage. Explicit roots remain authoritative.
Other Linux accounts use the private home default; non-Linux state defaults are
unchanged. Root resolution only
selects paths and reads configuration: it creates no directories, changes no
permissions or ownership, and never migrates data or falls back to another
existing root when initialization fails.

Package installation with pip or uv creates no accounts, mutable state, shell
configuration, or hypervisor setup. The separate root-only
`python -I -m palimpsest_local.linux_install` installation step creates the
no-login `palimpsest` system account and primary group, with its home at
`/var/lib/palimpsest`, and both standard system directories owned by
`palimpsest:palimpsest` with mode `0700`. Use the administrator-owned Python
installation described in [installation](install.md). Existing explicit roots
remain valid without moving their assets.

The legacy-root guard applies only when a root or dedicated-account process
would select the system default. If its former home state path already exists,
including as a symlink, it must explicitly select that root with
`XDG_STATE_HOME=$HOME/.local/state` rather than silently hiding it behind the
system default. Ordinary users already select the home directory and need no
such override. This performs no migration; `store move` retains its own safety
preconditions and does not relocate roots containing runs or projects.

The managed state root contains the content store, source and derived OCI data,
BuildKit cache, run and project ledgers, writable project volumes, OCI root
volumes, locks, tags, transfers, and build records. Existing permission,
ownership, no-follow, digest, and inode checks remain unchanged.

This boundary covers only records and content owned by Palimpsest. It does not
move or take ownership of Docker's own image cache, layers, volumes, networks,
or credential store, nor libvirt's backend-internal network definitions.
Palimpsest-selected network metadata that is already part of run or project
ledgers remains under the managed state root. OCI-root execution continues to
support network `none` only, and a Palimpsest-managed shared-network asset
feature is not implemented by this storage change.

The installer accepts a preexisting account and directories only when they
match the required identity, ownership, and modes. It refuses partial or
conflicting identities and never recursively chowns, deletes, or migrates
existing data. A failed account creation can leave partial setup requiring
administrator inspection. Management commands run as the dedicated UID;
group ownership alone does not grant other UIDs direct write access. No sudoers,
Docker/libvirt/KVM group membership, or runtime daemon is installed.

## Chronological host command journal

On Linux, dispatched CLI commands record start/end observations in
`commands.jsonl` under the following directory:

- Regular users: `logs/` beneath the selected state root, normally
  `~/.local/state/palimpsest/logs`. State environment/config/XDG overrides also
  select this journal namespace.
- Root or the dedicated `palimpsest` account: `/var/log/palimpsest`, still
  preprovisioned by the administrator and never automatically created by the CLI.
- Explicit `PALIMPSEST_LOG_HOME`: that absolute owner-private directory, on
  Linux or Darwin. Explicit directories must already exist; invalid, unavailable,
  or unsafe overrides warn instead of silently choosing another path.

Only the regular-user Linux default provisions missing directory components,
using pinned no-follow directory descriptors, effective-UID ownership, and
`0700` modes. Existing directory modes/owners are not repaired. Foreign-owned,
symlinked, or group/world-writable ancestors are refused, except root-owned
sticky temporary-directory ancestors. The selected state root itself must be
owned by the effective UID. System paths and explicit log overrides retain the
preprovisioned-only policy. The journal itself remains `0600`.
Directory provisioning happens only at command start. If a command moves or
removes that selected state root, its end observation warns rather than
recreating the old root; the next invocation resolves the current configuration.

Darwin keeps the existing opt-in journal policy: no default log directory is
created and no journal is recorded unless `PALIMPSEST_LOG_HOME` is supplied.
This preserves its established side-effect boundary while removing the Linux
requirement for a manually provisioned system journal. Explicit Darwin logging
uses the same record and filesystem safety rules.

Records contain only a fixed schema, allowlisted command family (or `unknown`),
start/end phase, command success/error, random invocation ID, UTC observation
time, monotonic time, and an increasing sequence within the journal file.
Sequence and append order remain authoritative when wall time goes backwards;
timestamps describe host observation, not exact guest event time. Command
arguments, paths, environment values, names, credentials, guest output, and
exception text are not serialized. A successful `run -d` command is not evidence
that its workload later completed successfully.

The directory is `0700`; the single-link regular journal file is `0600` and owned
by the effective UID. Component-by-component no-follow opening, file/root
identity rechecks, and a nonblocking exclusive lock protect publication. Initial
file creation is exclusive; only an already-existing-file result permits a
second open without creation flags. Other open errors are not retried.
Lock acquisition uses a 50 ms wait budget, each record is at most 512 bytes, and the
file is capped at 64 MiB. These bounds do not impose a hard deadline on kernel
filesystem I/O. A partial tail, invalid sequence, unsafe path, full file, or
write/sync failure leaves existing bytes intact and produces a warning. A failed
partial append can leave an incomplete tail; it is not silently truncated.
There is no automatic rotation, deletion, or damaged-file repair. Administrators
must preserve and inspect records before arranging maintenance with writers
stopped; replacing the file starts a new sequence scope.

Logging failures do not replace a command's result or prevent dispatch. A fixed
warning goes to stderr, at most once per invocation, and is repeated on every
subsequent affected invocation until recording recovers. JSON stdout is not
changed. If stderr itself is unavailable, warning delivery is best-effort and
still does not abort the command. This is not a periodic background alert or a
web-UI health banner. Argument parsing/help and internal shell-completion exits
before dispatch are not journaled. Direct library calls and detached monitor
events are outside this CLI command journal.

Current raw VM console files remain pinned below each run's managed state. OCI startup,
monitoring, retained-console streaming, and QEMU access bind those files to
exact run-directory and inode identities. Moving or mirroring them before a
versioned log-routing contract would risk weakening lifecycle evidence or
making two copies appear authoritative.

A later logging slice should therefore introduce a versioned route for new
runs, retain legacy lookup for existing runs, distinguish the chronological
host journal from raw guest console bytes, and verify both KVM and OCI monitor
lifecycle behavior. It must not relocate or delete existing failed-run data
automatically.

## Verification checkpoint — 2026-09-09

### Dedicated installation and fail-open journal (`0456822`)

The new installation and command-journal implementation passed independent
review. Final local `core-cli` verification passed 1,061 tests (27.07 seconds),
and the focused OCI host/adapter tests passed 77 (2.02 seconds). A disposable
network-disabled Linux container verified real account/group creation, two
idempotent provisioning calls, unchanged UID/GID and directory inodes, no-login
home, exact directory ownership/modes, and state initialization and paired
journal writes as the dedicated UID. The test container was removed normally;
the host's accounts and standard directories were not changed.

Earlier local checks are retained as failures: restricted socket access caused
two failures and 28 setup errors; after permitting test sockets, a concurrent
journal test failed. A typed reproduction later identified `ENOENT` during
concurrent initial file opens, before the journal lock. Exclusive creation with
an `EEXIST`-only existing-file open fixed this path; it is not a lock-timeout fix
or a generalized retry. The production 50 ms budget remains unchanged, while
successful concurrency and timeout/fail-open behavior have separate tests.

GitHub publication was initially blocked by the automatic safety reviewer.
After explicit user approval of this payload and destination, implementation
commit `0456822baf8e5d963185148f57969e31479d7b64` was pushed to the existing
branch. The exact commit passed `core-cli` on the Linux server: 1,061 tests
(49.66 seconds), followed by 77 OCI host/adapter tests (15.72 seconds). The
architecture source guard also passed. No native VM rerun was required for this
host-only change; this is not a new Gate 2 or guest qualification.

Read-only inspection still found no dedicated account/group or either standard
directory, and noninteractive sudo required a password. Actual host provisioning
therefore remains unperformed. It requires administrator authentication and an
administrator-owned package installation; service-UID KVM/libvirt/Docker
permissions are separate. No host account, permission policy, or old VM data was
changed during these tests.

### Earlier storage-default and console diagnostic checkpoint

Implementation commit `1b9c5c6` passed 412 focused local tests plus 13
architecture-guard tests. The exact pushed commit passed the same 425 tests on
the Linux qualification host (34.30 seconds). Independent review, Ruff, lane
inventory, and working/staged architecture checks passed. This is focused
verification, not a full portable-suite or system-directory installation proof.

Read-only host inspection found both standard directories absent, and the
operator could not obtain noninteractive administrative access. No system
directory, ownership, global configuration, or existing asset was changed.
Actual provisioning remains pending, as does the operating-account choice.
Before integrating the chronological journal, its failure policy must be
settled: continue the VM operation with a warning, or refuse operations when
records cannot be written. The latter changes VM availability; neither policy
is implemented by this checkpoint.

An independent test-only console diagnostic also passed on the exact commit
(one KVM boot, 2.01 seconds, 128 MiB, one vCPU, no network). It verifies a
separate nonblocking console open description before root transition without
changing inherited console flags or ownership. All 18 preservation checks
passed both before and after the run: the three earlier inactive VM definitions
and four original archive hashes were preserved, and no active VM/QEMU remained.
Evidence is retained privately. This is a prerequisite for future main-output
transport, not its implementation, a new application build, NGINX qualification,
or a complete Gate 2 run. Production guest C/ELF and PID 1 protection are unchanged.
