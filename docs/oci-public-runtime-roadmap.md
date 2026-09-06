# Shortest safe path to public OCI run and Gate 2

The user prioritized public foreground `run`, Docker-like `run -d`, and the
local build-to-run Gate 2 over further isolated infrastructure slices. This
document records the execution order and the deliberately limited public surface.

## First public local OCI lifecycle (five-stage integration)

The CLI now connects explicit host admission, typed local intake, completed-run
removal, the native launch adapter and foreground/detached dispatch. Linux
x86_64 KVM with `qemu:///system` is the only initial target. Public OCI operations
are `run`, `run -d`, `ps`, `stop` and `rm`; additional guest `exec`, `start`,
`logs`, TTY/stdin and network attachment remain unsupported. Gate 2 is unchanged
and still pending real additional guest exec.

Host configuration requires absolute `PALIMPSEST_OCI_KERNEL`,
`PALIMPSEST_OCI_KERNEL_CONFIG`, `PALIMPSEST_OCI_PACKER` paths and canonical
`PALIMPSEST_OCI_KERNEL_DIGEST` / `PALIMPSEST_OCI_KERNEL_CONFIG_DIGEST` SHA-256 pins.
Use the qualified kernel/config pair, required built-in drivers, KVM/libvirt,
ACL tools, qemu-img, mkfs.ext4 and a supported mksquashfs (4.6+). The Python
environment must import libvirt without `PYTHONPATH`. Digest pins identify the
operator-qualified pair; they do not prove how a kernel was compiled.

Create a new short, dedicated runtime parent whose existing ancestors already
permit search (simple ACLs only); never change the user's home permissions:

```sh
palimpsest oci init-runtime /tmp/my-oci-runtime
export PALIMPSEST_STATE_HOME=/tmp/my-oci-runtime/state
palimpsest run app.oci.tar --name app -d
palimpsest stop app
palimpsest rm app
```

Set the five host variables before `run`. Existing parent paths are not adopted
or chmodded. Local layouts require `--runtime-kind oci-root`; `.oci.tar`/`.oci`
files select OCI automatically. `--manifest sha256:…` selects an explicit root
descriptor. Cloud-image layout directories keep their prior meaning. OCI
network defaults to none; explicit `--network none` is equivalent. Other OCI
network values and process overrides are not supported in this first surface.

Foreground returns the actual workload exit and combined VM-console output
(boot plus workload, not separated guest stdout/stderr). INT/TERM requests
authenticated STOP. Startup signals are queued outside lock/receipt mutations;
before activation they preserve an inactive exact run, and after READY they
request a bounded completed STOP. SIGKILL/crash is not an automatic cleanup
promise. `-d` returns the name only after READY; an already exited workload is
not reported as a live detached service. Closing a reader alone only detaches.

Normal `rm` verifies terminal completion, inactive exact domain and stale
monitor ownership before revoking access and releasing leases/root resources.
The default root is VM-exclusive and deleted with normal removal. The existing
retained-root API remains; public retention/reboot UX is deferred. Failed
pre-activation grants or ambiguous/stale terminal socket state are preserved
and may require recovery beyond normal `rm`; never manufacture terminal proof
or delete a guessed VM to make a failure appear clean.

`tests/kvm/test_oci_public_cli_live.py` is a separate opt-in public-command proof
(`PALIMPSEST_OCI_PUBLIC_CLI_LIVE=1`). It compiles a tiny syscall-only test image,
materializes actual OCI layers, checks guest OverlayFS `/` and an image-only
marker, finite exit, detached survival, STOP, foreground SIGINT and normal rm.
It does not replace the Palimpsest-build/additional-exec Gate 2. Run this exact
file during public adapter edits, not the entire native/portable suite.

Qualified on pieroot-server at `88a8e95b77dd0b6b6983af7ed1f00d2da10e0f6a`:
the public proof file passed both tests in 39.50 s, including three real VM
launches and public removal. Foreground exited 23; service STOP and foreground
SIGINT exited 42 after the guest observed PID 1's SIGTERM. No domain or monitor
remained. Eight selected host/request/adapter/CLI/lane modules passed 637 tests
in 30.07 s. These selected results are not a full suite or Gate 2 result.
The first attempt failed before VM creation because host tool discovery omitted
`/usr/sbin`; a fixed standard system search path and regression test corrected
it. Failed evidence `/tmp/p-pub-87cfaadb` remains; the successful owned fixture
was removed only after all three normal public `rm` operations completed.

## Connected prerequisites

Local OCI intake now selects a unique root descriptor within the same secure
snapshot when no pin is supplied. `palimpsest oci materialize IMAGE.oci.tar`
uses this path; `--manifest sha256:…` is still supported and is required for
multiple root entries. A single image index delegates its internal platform
selection to the existing verified image resolver. Discovery does not skip
descriptor hashes, size limits, or archive safety checks.

The run-owned lower provider connects sealed copies to domain planning,
fresh-monitor descriptors and exact read-only ACL grant/revocation. Logical
occurrence order and durable lease sets stay intact; repeated content is
copied once per run. This removes shared export registry/GC from the initial
launch dependency. It does not by itself provide the public adapter.
The private v10 launch envelope includes at most 24 additional distinct lower
file descriptors. Its existing 1 MiB encoded size cap is an admission limit:
oversized combinations of long/escaped paths are rejected before spawn.

The fresh coordinator now accepts pinned launch authority from a caller that
already imports libvirt or uses threads. Only its clean child invokes the
existing monitor spawn protocol; its return means launch accepted, not READY.
An explicit null terminal timeout permits a service lifetime while the default
45-second terminal timeout and bounded boot/STOP deadlines remain unchanged.

The monitor client now pins the exact run and monitor directory and separates
READY, STOP acceptance, durable TERMINAL and worker completion. A finite
stop-and-wait returns the validated guest exit result, not a fabricated success.
The noninteractive process session follows bounded, receipt-pinned VM-console
bytes and drains them before returning that result. This console combines boot
diagnostics and workload output; it does not provide separate guest stdout and
stderr, stdin, TTY or remote exec. INT/TERM enqueue lifecycle STOP outside run
locks; closing a reader only detaches it. The public adapter now uses this path.

The first public lifecycle connects host/runtime-root setup, request/adapter
wiring and normal stop/remove orchestration. Further recovery must cover pre-spawn grant
failure and stale terminal sockets without fabricating terminal evidence.

## Milestone 1: a complete public lifecycle

Initial scope: a local OCI archive/layout, Linux x86_64 KVM, an explicitly
qualified host kernel, and the packaged first-party initramfs. Registry intake
and other backends can follow without changing the OCI root semantics.

Connect these existing components in one vertical path:

1. Resolve a unique supported manifest from local input and pin its bytes,
   platform and digest. Reuse `LocalArchiveSource`/`LocalLayoutSource` and
   `materialize_image_hard`; reject ambiguous input instead of guessing. Add a
   typed OCI request rather than forcing it into the cloud-image `RunSpec`.
2. Prepare a runtime root whose ancestors already permit the selected QEMU
   principal to traverse them, with explicit setup/preflight. Never silently
   chmod the user's home directory. State/runs/root-volumes ACLs do not solve
   access to ancestors outside that namespace. Gate 2's temporary XDG root
   must also meet this condition through supported product setup.
3. Publish sealed BOOT and per-run lower exports before domain projection.
   Copy verified distinct lower images into owned inodes; keep ordered logical
   occurrences, hashes and OCIStore lease sets. No CAS hardlinks or CAS chmod.
   Revoke and reclaim exports only after exact inactive-domain/terminal cleanup.
   Shared physical exports can later implement the same logical contract.
4. Connect resource preparation, plan commit, definition and existing access
   grants. Retain the clean monitor spawn boundary: setup imports libvirt,
   whereas `spawn_monitor_exec` requires a clean single-threaded process.
   Use a typed fresh coordinator and revalidated pinned authority, not a
   test-only monkeypatch or relaxed spawn check.
5. Add `-d` to the OCI public path. Detached return requires authenticated
   READY and a monitor that survives launcher exit. Default foreground mode
   forwards workload output and exit status; interruption requests authenticated
   STOP. Do not return success merely because a STOP request was accepted.
6. Wire exact `stop` and `rm`: terminal/domain cleanup, BOOT/lower/stage1/root
   and runtime ACL revocation, shared traversal departure, root/lease release,
   then pinned run-tree removal. Ambiguous ownership remains fail-closed with
   actionable recovery information. Preserve VM-exclusive root generations
   and the existing retained-root API.

Enable only operations that have passed this vertical qualification. Existing
`resolve_run_request`, `ResolvedRunRequest`, `_adapter_for`, CLI parsing and
platform capability profiles all need deliberate OCI-specific integration;
removing one rejection is not a complete implementation.

Acceptance is public CLI execution without fixture ACL brokers, source-path
remapping or ownership-normalization adapters: OCI contents become the actual
guest `/`; foreground returns the workload result; `-d` survives caller exit;
a separate CLI can stop/remove only its exact VM; source archives remain.
Exercise failure and interruption cleanup as well as successful shutdown.

## Milestone 2: additional guest exec and the unchanged Gate 2

The current lifecycle protocol has READY/SNAPSHOT/STOP/TERMINAL handling but no
remote EXEC operation. A cgroup called `exec-00000001` currently represents the
main workload, not a separate command API.

Add the minimum real noninteractive exec: one additional argv-based process
while the main workload is alive, authenticated monitor-to-guest delivery,
bounded output, stdout/stderr/exit propagation, and teardown with the VM.
Bound request/output sizes, reject stale attempts and preserve process/user
isolation. Interactive TTY/stdin and parallel exec are not prerequisites.

Then require `tests/e2e/test_local_oci_build_run.py` without weakening it:

```text
Palimpsest build → immutable OCI archive + receipt
→ transfer to Docker-daemonless qualified KVM host
→ public run -d → separate public exec of the image probe
→ verify image marker and actual / / PID 1 root
→ stop → rm → no domain/run state, source archive preserved
```

The probe must execute inside the guest. A host-generated response, the initial
workload's output, or the existing boot-only fixture is not Gate 2 acceptance.
Only after this proof should the opt-in gate be enabled as a required product
qualification on an appropriately configured runner.

## Deferred optimization, not abandoned requirements

Shared lower-export membership/refcounts/GC, multiple VM data-volume sharing,
retained-root public UX, TTY and parallel exec, remote Docker Hub intake,
Compose and non-x86_64 backends need not block the first two milestones.
The user's eventual multi-VM volume requirement remains. VM root disks stay
exclusive and explicitly reusable after retention; a future shared data volume
is a different lifecycle from the VM root.

Every development change uses the [test lanes](testing.md) and its relevant
native/product proof. Complete portable shards and broad release regression
remain integration checks, not a reason to rerun all 4,000+ tests per small edit.
