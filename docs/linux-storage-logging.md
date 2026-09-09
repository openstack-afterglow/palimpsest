# Linux storage and logging rollout

## Current first slice

On Linux, a fresh process with no `PALIMPSEST_STATE_HOME`, no configured
`storage.state_root`, and no explicitly set `XDG_STATE_HOME` resolves its managed
state and assets to `/var/lib/palimpsest`. Explicit roots keep their existing
precedence:

1. `PALIMPSEST_STATE_HOME`
2. `[storage].state_root` in `config.toml`
3. an explicitly set `XDG_STATE_HOME`, with `/palimpsest` appended
4. the Linux system default `/var/lib/palimpsest`

Root resolution itself does not create or change `/var/lib`, migrate an existing
XDG or configured root, or delete old state. Normal state initialization creates
the selected root only when operating-system permissions allow it; this change
does not install or preprovision system directories. A single-operator Linux
installation must preprovision `/var/lib/palimpsest` as an owner-only directory
for the account that runs Palimpsest. Existing explicit roots remain valid and
are the supported way to continue managing existing runs, failed VMs, volumes,
layers, and other assets without moving them.

If the former unconfigured path `~/.local/state/palimpsest` already exists,
including as a symlink, Palimpsest refuses to silently hide it behind the new
system default. Set `XDG_STATE_HOME=$HOME/.local/state` to select that legacy
root explicitly. This performs no migration. The existing `store move` command
has its own safety preconditions and will not relocate a root that still
contains runs or projects.

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

## Planned chronological host journal

The complete Linux layout will add an independently secured
`/var/log/palimpsest` root, also preprovisioned for the single operator. The
planned journal will record host-observed lifecycle events in UTC order with an
explicit sequence so equal timestamps and clock adjustments cannot reorder
events. Records must remain owner-only, bounded, free of credentials and secret
environment values, and protected against symlink and inode replacement.

That journal is not implemented in this slice. In particular, current raw VM
console files remain pinned below each run's managed state. OCI startup,
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
