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
