# Docker Hub image-to-VM compatibility

## Scope and current public boundary

The public OCI-root runtime accepts a local uncompressed OCI image-layout
archive or directory, not a Docker Hub reference. `palimpsest pull` is currently
a Docker registry wrapper: it does not populate the OCI-root source store or
launch a VM. A legacy Docker-save archive is not the accepted OCI-layout
transport. Do not rename one and assume the format changed.

The compatibility workflow under test is:

```text
Docker Hub tag → unique linux/amd64 manifest digest
→ digest-preserving OCI archive copy → local public run / run -d
→ actual guest execution → public stop/rm
```

Use a preserving copier such as Skopeo, with the selected image manifest pinned:

```sh
skopeo --override-os linux --override-arch amd64 copy --preserve-digests \
  docker://docker.io/library/hello-world@sha256:SELECTED_AMD64_MANIFEST \
  oci-archive:hello-world.oci.tar
palimpsest run hello-world.oci.tar --name hello --memory 512 --vcpus 1
palimpsest rm hello
```

Replace the illustrative digest with the actual platform-specific digest and
configure the qualified host BOOT inputs and a dedicated runtime first; see
the [public runtime setup](oci-public-runtime-roadmap.md). Skopeo is an external
acquisition tool, not a newly implemented Palimpsest registry client. An
official digest-pinned Skopeo container may perform the copy on a Docker host;
the copied image's workload must still execute in the KVM VM, not in Docker.

The runtime verifies selected descriptors and layer DiffIDs. Preserving the
source manifest also binds the original image config and ordered compressed
layer descriptors. Archive SHA256 is a separate transport identity. None of
this is a publisher signature or a guarantee that arbitrary images are safe.

## Compatibility matrix and separately selected tests

Three independent opt-in checks target an unchanged foreground `hello-world`,
the default Redis Alpine service, and the non-root NGINX Alpine service.
The first proves output and terminal exit. Service proofs require a running
domain, default workload readiness, additional public exec, app root identity
matching authenticated PID 1 root evidence, direct PID 1 access refusal and
normal cleanup. They must preserve source archives and failure evidence.

### 2026-09-08 initial native results (`f606e32`)

All three independently selected checks ran on pushed commit
[`f606e32`](https://github.com/openstack-afterglow/palimpsest/commit/f606e32f497d835ffa04cf03655e920a22fa7917)
on the Linux x86_64 KVM host, using 512 MiB and one vCPU per requested VM.

| Original Docker Hub image | Result | Observed boundary |
| --- | --- | --- |
| `library/hello-world:latest` | **PASS**, 13.56 s | Default `/hello` executed in the VM, emitted `Hello from Docker!`, exited zero, and public `rm` removed the domain/run state. |
| `library/redis:7-alpine` | **FAIL**, 3.93 s | `oci-worker-pack` during layer conversion, before VM creation. Four layer occurrence receipts completed; the next layer contains only the `data/` directory. Exact packer cause requires a separate bounded diagnostic. |
| `nginxinc/nginx-unprivileged:stable-alpine` | **FAIL**, 1.03 s | The Linux image's `ArgsEscaped: true` is rejected by the existing process validator, before layer conversion/VM creation. |

The failures remain failures, not skips or expected-pass exclusions. No service
readiness, service exec/root-proof or service cleanup success is claimed from
them. Redis did not reach its entrypoint, so its observed failure is **not**
evidence of a guest capability or worker resource-limit problem. NGINX's
configured user is `101`, but its workload was never launched.

A proposed standalone Redis diagnostic was **not executed**: independent
review still rejected its interruption/descendant-cleanup guarantees and an
observation path that could replace the production error. Its rejected review
evidence is preserved separately from the native results. The precise packer
error remained unresolved at this checkpoint; zero-fragment superblock handling was only a
code-inspection hypothesis, not an observed diagnostic result or a fix.

These are new real-image compatibility checks, not a new image build or full
Gate 2. Existing local-build Gate 2 evidence remains separate. Initial portable
checks passed 117 cases locally and on the server; a fresh-runtime test setup
defect then failed the first hello attempt before any VM was created. The
proof-only correction isolated its source cache from lazily created product
state. Final focused checks passed 118 cases locally (2.67 s) and at the exact
server SHA (4.14 s); the three native checks skip unless individually opted in.
These overlapping runs are not an aggregate full-suite result.

### Immutable source identities

The tags were resolved once to the following unique `linux/amd64` manifests.
Copies used `--preserve-digests`; original archive hashes matched after each
native attempt. Both failed runtimes and their evidence remain preserved.

| Image | Selected manifest SHA256 | OCI archive SHA256 |
| --- | --- | --- |
| hello-world | `d1a8d0a4eeb63aff09f5f34d4d80505e0ba81905f36158cc3970d8e07179e59e` | `7e9f82b7d5203a3e0da8889915caeaed7b5e70cc3996de9c4cb471bdf3fa5aac` |
| Redis | `1db42ccef14898aa29bae778452d567534b59c107129cbc1163fb552de184d3c` | `90159972e37b9f02aed6b699361b7ab633d77323a056d811dc6684b8d06857d2` |
| NGINX | `b8c179cd3c2ae222a873dd59fbae240fadc03836cae5198afc9e9c19919c3880` | `64e3f9852293174971bbd81e54bf0093896fc0ee525c9b0494763d5208ffa029` |

All image Entrypoint/Cmd, configured user and environment remain unchanged.
There are no process overrides, baked probes or injected image files. VM
networking remains `none`; service readiness is not external port reachability.
The capabilityless workload policy may reject image entrypoints that perform
privileged operations such as switching user or changing ownership. Such a
failure must be reported, not hidden by weakening PID 1 protection or granting
privileges.

### 2026-09-08 zero-fragment follow-up

The in-repository, individually selected real-packer regressions at pushed
`d255fd2` reproduced the fragment-accounting rejection for directory-only and
empty layers (0.57 s and 0.37 s). The previously rejected standalone diagnostic
was not executed. These small production-packer component tests are not
worker-isolation or VM qualification; their limits are in [testing](testing.md).

At pushed `c95d948`, the host structural verifier v3 accepts an already-bounded
finite fragment-table offset for zero fragments, without changing the other
structural checks. Local focused checks passed 560 with seven Linux skips;
the same server selection passed 567 (169.94 s). Three real-packer cases,
including deterministic output, passed (0.71 s). These overlapping results
are not a full-suite aggregate.

The unchanged original Redis proof then passed layer conversion but failed
after VM boot (75.98 s). Its console reported stage-1 filesystem contract
rejection before mounting, not workload readiness. Read-only examination of
the seven generated lowers found the portable guest verifier rejected exactly
the two zero-fragment images, with finite offsets 188 and 136; the other five
passed. Both the portable guest verifier and C stage-1 verifier still contained
the old predicate. No Redis entrypoint/capability failure is established by
this attempt. The failed runtime and source archive remain preserved; no
domain remained in the read-only post-failure inventory.

Separately, the existing immutable Palimpsest-built image passed the cold
public run/exec/stop/rm proof at the same SHA (one test, 21.92 s), including
literal argv, separate streams, exit status and VM lifecycle. This is neither
a new image build nor full Gate 2. Guest verifier parity and a fresh original
Redis rerun remain separate follow-up work at this checkpoint.

### 2026-09-08 guest parity checkpoint (`a991912`)

The portable guest and real C stage-1 now apply the same v3 fragment rule.
A host/portable differential regression first failed only the zero-fragment
finite-offset case (one failed, six passed). After correction, focused local
Python/C/packaged-ELF checks passed 108 (19.26 s). The source and packaged ELF
provenance pins were updated after two identical offline pinned-toolchain
builds. No filesystem bounds, whole-lower digest check, protocol ABI, PID 1
protection or workload/resource policy was changed.

At exact pushed
[`a991912`](https://github.com/openstack-afterglow/palimpsest/commit/a9919123a02fbd79ca0ad43cb0f6911acc168c70),
server focused checks passed 106 with two skips (13.51 s). After acquiring the
missing pinned GCC toolchain image and enabling the Docker PID 1 harness,
the two skipped nodes alone passed (6.65 s), including two rebuilt ELFs
matching the packaged bytes. The initial skips were not counted as passes.

The unchanged Redis native proof still **FAILED** (75.60 s), now at
`root transition rejected; root state is indeterminate; workload disabled`.
The console confirms ext4 mounted. The C control flow reaches this rejection
only after lower structural/digest verification and staging-root assembly;
which internal root-transition check failed is not yet established. No
entrypoint, Redis readiness, service exec/root-proof or PID 1 denial proof
passed. Do not call this a capability failure or hide it with image edits,
privilege changes, or a success/xfail reclassification. The failed run remains
preserved, its domain was absent in post-failure inventory, and the original
Redis archive SHA256 still matched the immutable identity above.

The separate existing Palimpsest-built cold public exec proof **PASSED** at
the same SHA (20.47 s), including run/exec/root reports/stop/rm. Its original
archive hash remained unchanged. Redis requests 512 MiB / one vCPU; the
unchanged cold proof uses CLI defaults of 4096 MiB / two vCPUs. An earlier
operational-plan description incorrectly assigned the smaller budget to both;
source inspection and independent review corrected the description, not the
defaults or proof code. Both run with network `none`, one VM at a time.

This is a narrow guest compatibility correction and existing-build regression,
not a new application-image build, full native negative-control matrix, or
new Gate 2 qualification. The earlier `hello-world` success and NGINX failure
remain historical results from their recorded SHA, not reruns of this guest.

### 2026-09-08 root-transition diagnostic checkpoint (`162cebe`)

At exact pushed
[`162cebe`](https://github.com/openstack-afterglow/palimpsest/commit/162cebe0477d4b61262b455c688b623988d98245),
stage-1 adds one fixed target/reason marker only when initial proc/sys/dev
target preparation fails before any mount move. It does not change exact
`0755`, root ownership, emptiness, nofollow, identity checks, PID 1 protection,
or workload privileges. The original generic exit-71 fail-closed wait remains.
The console diagnostic is not authenticated READY or root-identity evidence.

The new actual-C fixture checks passed 11 cases, including root-owned empty
`0555` rejection as `mode`, unchanged metadata, wrong ownership, nonempty and
symlink targets, special mode bits and the `0755` positive. The final source
was built twice with the pinned offline compiler; identical final outputs
match the packaged ELF and provenance pins. Local focused checks passed 185
(23.50 s), plus architecture guard regressions 13 (7.51 s). Exact-SHA server
focused checks passed 198 without skips (39.42 s), including the explicit
Docker fixture opt-in and two packaged-ELF reproduction builds. These overlap
and must not be added together as a full-suite result.

The separate packaged-stage-1 native proof **PASSED** (123.45 s), executing
43 boots / 44 QEMU invocations across its existing positive and negative
controls, including root transition and PID 1/workload isolation. This is the
dedicated guest proof, not the entire native suite or a new Gate 2 result.

The unchanged Redis service proof still **FAILED** (76.03 s). Its console now
contains exactly one `target=proc; check=mode` diagnostic followed by the
existing indeterminate-root/workload-disabled rejection. Read-only listing
of the previously generated original-image lowers identified root-owned
`0555` for `/proc`, while `/dev` and `/sys` were `0755`. The instrumented VM
therefore confirms the exact mode predicate as the current rejection, before
mount moves and before the Redis entrypoint. No Redis readiness, service exec
or authenticated root/PID 1 service proof passed. No later compatibility
outcome is inferred from this diagnosis.

The failed runtime and console remain preserved; the post-failure domain
inventory was empty. The separate unchanged Palimpsest-built cold public
run/exec/root-report/stop/rm proof then **PASSED** (21.17 s), with its normal
4 GiB / two vCPU defaults. Redis used 512 MiB / one vCPU. Both were serial,
network-none VM runs. Original Hub archive hashes and the existing build
archive hash remained unchanged. No new application image was built, and
hello-world and NGINX were not rerun.

Allowing a root-owned empty `/proc` with `0555` in addition to `0755` is a
proposed next change only. User approval is pending; the current implementation
still rejects it. Do not chmod the image, broaden other target modes, relax
PID 1 protection or count the diagnostic failure as a compatibility pass.

### 2026-09-08 approved proc-mode checkpoint (`d9b3593`)

Following explicit user approval, exact pushed
[`d9b3593`](https://github.com/openstack-afterglow/palimpsest/commit/d9b3593774b11e62283a075e7df55838c6f0412a)
admits root-owned empty `proc` targets with either exact `0755` or `0555`.
Generic directory checks and `dev`/`sys` remain unchanged. Nofollow, type,
owner, mode and emptiness validation retain their original order. An immutable
initial device/inode/mode/UID/GID snapshot is compared against both retained
and reopened descriptors before each mount move; changes between the two
admitted modes still reject. Runtime readiness fixes OverlayFS magic. No
chmod, original-image edit, PID 1 protection or workload privilege change was
made. This approval supersedes the pending decision in the previous checkpoint.

A real C baseline test first failed with the original proc/mode diagnostic
(2.43 s). An intermediate candidate then failed both mode-change tests;
the initial snapshot correction addressed those failures rather than removing
the tests. A later inode-replacement fixture setup failure was corrected to
replace a child inside tmpfs, without increasing Docker privileges. Final
actual-C tests passed 22 (5.70 s). The final pinned offline ELF builds were
identical and matched the packaged binary/provenance pins. Local focused
checks passed 209 (32.93 s); the same selection passed 209 without skips on
the exact server SHA (43.16 s), including two packaged-ELF rebuilds. These
overlapping counts are not a full-suite aggregate.

The dedicated packaged-stage-1 native proof **PASSED** again (123.43 s):
43 boots / 44 QEMU invocations, including existing real root-transition and
PID 1/workload-isolation controls. This is not the entire native suite or a
new Gate 2 qualification.

The unchanged Redis service proof still **FAILED** (61.67 s), now waiting for
the application's `Ready to accept connections` message. Public `run -d`
returned successfully. Its console records completed root transition with
root as `/`, committed workload isolation, workload startup and lifecycle
READY. The previous proc/mode rejection is resolved on this real original
image. The entrypoint then reports
`setpriv: keep process capabilities failed: Operation not permitted`, and
PID 1 records main status 1 after terminal root synchronization and cgroup
cleanup. This is the observed next boundary, not a Redis readiness success.
Service exec, authenticated root-report comparison and direct PID 1 refusal
checks were not reached. Do not claim those service proofs passed or grant
additional privileges to conceal the entrypoint failure.

The failed runtime, root disk and console are preserved. An all-domain name
listing initially blocked the cold public exec preflight. A subsequent exact
domain query showed `shut off`: only its persistent definition remained, not
an active 512 MiB / one vCPU guest. The earlier description of a running VM
and request to stop it were corrected; no manual stop or deletion occurred.
An independently approved replacement plan preserved that exact inactive
definition, verifying its UUID, shut-off state, disabled autostart, zero active
domains and immutable archive hashes before and after the separate fresh-runtime
test. The existing Palimpsest-built cold public run/exec/root-report/PID 1
refusal/stop/rm proof then **PASSED** at the same product SHA (21.07 s), using
its normal 4 GiB / two vCPU defaults and network `none`. Neither the inactive
Redis definition nor its failure material was removed to satisfy a preflight.
The earlier empty-all-domain failure remains recorded rather than reclassified.
Original Hub and existing build archive hashes remain unchanged.
No new application image was built; hello-world and NGINX were not rerun.

### 2026-09-08 preserved Redis privilege-boundary checkpoint (`d9b3593`)

This documentation-only checkpoint uses analysis baseline
`e5cacb2891d6b5454c91274dfebdaf4ebfe0d676`; the product under observation
remains unchanged at
[`d9b3593`](https://github.com/openstack-afterglow/palimpsest/commit/d9b3593774b11e62283a075e7df55838c6f0412a).
No source, test, setting or image was changed. No test, VM, image build or
application execution was performed, and this is not a new Gate 2 result.

Main's read-only SSH inspection used `unsquashfs -cat` against the preserved
current Redis lower layers only; it did not extract or mount them. Layer
`d37f81ae91d2bb1de4ed0ad644f29339954ce4814d4ea24c327866dcd51185f4`
contains the original `usr/local/bin/docker-entrypoint.sh`. In the relevant
default path, when the first argument is `redis-server` and UID is zero, that
script finds current-directory entries not owned by `redis`, changes their
ownership, and then uses `/usr/bin/setpriv` to re-exec the original script and
original arguments as the `redis` UID/GID with supplementary groups cleared.
This describes the inspected preserved image; the current upstream Redis
master script uses `gosu` and is not evidence for these image bytes.

The preserved APK records in layers
`76eb1c41678f89618a08aa5c261ad876aa2827cb4f02bb38920a05b80a0046bf`
and `bd1c68e5be9fb53e974c3527804a35c69453e5956e9549b5eeef2f732907978f`
identify `setpriv` as util-linux `2.40.4-r1`. In official util-linux v2.40.4,
[`setpriv.c`](https://github.com/util-linux/util-linux/blob/v2.40.4/sys-utils/setpriv.c)
requests `PR_SET_KEEPCAPS(1)` before changing UID, GID and groups and emits the
same error text observed in the preserved console if that request fails.
Official Linux v6.6
[`commoncap.c`](https://github.com/torvalds/linux/blob/v6.6/security/commoncap.c)
rejects that request when `KEEP_CAPS_LOCKED` is set. Its
[`securebits.h`](https://github.com/torvalds/linux/blob/v6.6/include/uapi/linux/securebits.h)
definitions decode the locally verified securebits value 239 (`0xef`) as
`KEEP_CAPS` off and locked, together with locked `NOROOT`, locked
`NO_SETUID_FIXUP`, and locked `NO_CAP_AMBIENT_RAISE` settings.

This is a source inference that matches the observed application error, not a
traced-syscall proof. The package version matches the cited util-linux upstream
version, but the Alpine patch set and installed binary have not been audited
for equivalence. The citations use Linux v6.6 semantics; they do not claim
that the unavailable exact v6.6.71 source was verified.

Local source inspection independently confirms empty capability sets, locked
`NOROOT`, and `no_new_privs`; its seccomp filter does not directly deny
`prctl`. Unlocking `KEEP_CAPS` alone would neither supply `CAP_SETUID` or
`CAP_SETGID` nor authorize an arbitrary supplementary-group transition, so
that policy relaxation is not a compatibility fix and would weaken the
deliberate boundary. The existing public OCI `run` contract has no `--user`
override.

The preferred next decision is whether to separately approve an explicit,
bound effective-user override. Such a contract could let trusted stage-1
demote before exec while retaining the existing capability protections and
unchanged source-image bytes. It would still change process identity and its
binding, and Redis data paths could require compatible ownership or
permissions. It is not implemented, approved or validated, does not guarantee
service readiness, and can never convert the original-default Redis result
into a compatibility pass. Granting workload capabilities is not recommended.

The original default remains **FAILED**: Redis readiness, subsequent service
exec/root/PID 1 comparisons and Gate 2 qualification did not pass. The system
libvirt inventory shows the preserved Redis definition shut off; the empty
read-only session-libvirt inventory is not system inventory. The failed
definition, lower layers and earlier historical evidence remain preserved.

## Next public intake contract

### 2026-09-08 explicit Redis user qualification (`4cbc863`)

The user approved the separately proposed explicit `run --user` option and
Redis-user test. This supersedes only the preceding pending override decision;
it does not approve additional workload capabilities or relabel the original
default-process Redis failure. GPT 5.6 Sol implemented the host contract and
test slices, with independent Astra code/native-plan approval before push.

At product commit
[`4cbc863`](https://github.com/openstack-afterglow/palimpsest/commit/4cbc863fc73caab141c9d54001cbfe7edf769934),
the CLI accepts canonical `USER[:GROUP]`, rejects empty/oversized inputs and
cloud-image use, and preserves the request's old positional arguments. Default
runs retain boot-plan v2. Explicit overrides use v3 with original image
process, user override and recomputed user-only effective process bound into
the existing preparation/lease/domain/stage-1 chain. Image bytes, immutable
materialization receipts, lower graph, guest C/ELF and PID 1/capability/NNP/
securebits/seccomp policy are unchanged. No automatic ownership repair occurs.
See [the user contract](oci-run-user.md).

The final focused 13-module selection passed **1,057 locally** (26.45 s) and
**1,057 on the exact server SHA** (198.83 s), without skips. These are the same
selection on two hosts, not a full-suite aggregate. The independent review
first blocked oversized numeric inputs and an unstamped architecture marker;
both were corrected and reapproved. Intermediate consumer checks also caught
a stale test-lane selection assertion, which was updated without dropping the
native-only requirement. Two temporary Unix-socket fixtures initially failed
at sandbox bind, then passed in the scoped elevated rerun and final selection.
The explicit-user proof's original-UID expectation was corrected before native
execution. Working/staged architecture, lane inventory, Ruff and diff checks
passed. No full guest matrix was rerun for this host-only change.

The separately opted-in **original-archive Redis `--user redis` proof PASSED**
(28.05 s). It used a fresh 512 MiB / one-vCPU, network-none VM and the same
digest-pinned archive and manifest, without changing entrypoint, arguments or
environment. Redis reported version 7.4.11 and service readiness. Public exec
reported `redis`, UID 999/GID 1000 matching the image account, all five
capability sets zero, `NoNewPrivs=1`, and `Seccomp=2`. The proof compared the
authenticated original process with v3 provenance/effective process, matched
the app's actual `/` device/inode with two authenticated PID 1 root reports,
required direct `/proc/1/root` access to fail with permission denial, and
completed stop/rm with exact new-domain/run absence. Redis archive hashes
before/after remained identical. This qualifies only the explicit-user case,
not the original-default Redis execution or arbitrary Docker Hub images.

After inventory verification, the separate existing Palimpsest-built image's
cold default public run/exec/root/PID 1 refusal/stop/rm proof **PASSED**
(20.68 s), using unchanged 4 GiB / two-vCPU defaults and network none. No new
application image was built; this is not a new full Gate 2 qualification.
Both tests ran sequentially on `pieroot-server` at the same pushed product
SHA. The prior inactive Redis definition was preserved with exact UUID,
shut-off state and disabled autostart before/after; zero active domains and
unchanged original Hub/build archive hashes were rechecked. Earlier failed
runtimes, disks and recovery evidence were not removed.

The earlier assessment paragraph below is retained as historical context;
its user-override decision is now superseded by this checkpoint. Next work is
the separate Linux `ArgsEscaped` contract and direct registry-reference intake.

The approved proc-only mode correction is implemented and passed the original
Redis root transition, and the separate cold public exec regression passed
under the reviewed inactive-domain preservation plan. Next assess the
original entrypoint's `setpriv` capability-retention requirement against the
unchanged workload policy; the proc approval does not authorize broader
privileges, user/command overrides or image changes. Redis service compatibility
remains unqualified. Do not execute the previously
rejected standalone diagnostic. Separately review Linux handling of the
legacy `ArgsEscaped` field against the OCI/Moby contract before changing the
existing fail-closed parser. Neither change is justified by silently editing
the downloaded image or bypassing its validator.

Before enabling a registry reference directly in `run`, define an explicit
OCI-reference grammar that cannot fall into the existing cloud-image path,
tag-to-platform-digest resolution, acquisition/tool/authentication limits,
private crash-safe storage and retry, and provenance binding into the existing
local source validator. Registry acquisition must remain separate from guest
networking and must never fall back to running the application in Docker.
Do not infer broad image compatibility from one successful example. Keep
root-disk deletion and multi-VM data-volume sharing as separate follow-ups.

## Upstream references

- [Skopeo copy and preserve-digests](https://github.com/containers/skopeo/blob/main/docs/skopeo-copy.1.md)
- [Official Skopeo container distribution](https://github.com/containers/skopeo/blob/main/install.md#container-images)
- [Docker image-save transport](https://docs.docker.com/reference/cli/docker/image/save/)
- [OCI image config and legacy ArgsEscaped](https://github.com/opencontainers/image-spec/blob/main/config.md)
- [SquashFS 4.6.1 table writer](https://github.com/plougher/squashfs-tools/blob/4.6.1/squashfs-tools/mksquashfs.c)
- [Linux 6.6 SquashFS superblock reader](https://github.com/torvalds/linux/blob/v6.6/fs/squashfs/super.c)
