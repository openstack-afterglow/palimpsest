# Detailed installation and configuration guide

This guide covers end-user installation from PyPI or a GitHub VCS URL,
package selection, supported host/runtime combinations, external dependencies,
initial configuration, upgrades, and administrator-owned Linux deployment.
Released installations use pinned published bytes; trusted local installation below uses the actual checkout's bytes.

Palimpsest Local requires Python 3.11 or newer; Hub requires Python 3.12+.
This release source prepares0.3.10 on published0.3.9
(`5c0fef0604ca631bef40ed505a0d02d742ccba9a`). Root/Hub metadata, module versions,
generated locks and Kolla image defaults move together. After formal publication,
install exact client bytes with
`uv tool install --python 3.11 palimpsest-client==0.3.10`, or install a trusted
local checkout with `uv tool install --force --python 3.11 .`.
A root wheel upgrade neither deploys Hub nor qualifies actual build/run behavior.
Git/outbound GitHub HTTPS is needed only for VCS installation.

**0.3.5 release and production:** retains the latest HTTPS-only private build plane and authorization while integrating distributed export ownership and local/client work. PyPI/GHCR publication and the controller1 Hub plus dedicated build-worker VM rollout completed on 2026-10-14; see [CI, publication and production receipts](testing.md#full-source-035-integration--local-verification), including evidence-based `claim_pending` recovery. Native KVM proof was NOT performed (repository-policy skip, as for 0.3.4); idle polling does not prove live recipe execution. Historical approval, deployment, publication and authentication receipts below retain their dated ref/resource boundary. The API never runs recipes; the CPU-only service VM receives verified HTTPS, a private keyfile and local scratch only. Follow the offline drain/schema/paired-storage restore checklist before any separately authorized rollout.

Historical DMS Lab deployment receipts preserve private listen port `18021`, internal VIP port `8021` and public `https://palimpsest.dmslab.re.kr`; later0.3.7 receipts record the shared-CAS three-controller layout. Preserve actual current operator overrides rather than infer placement from old single-controller records or role defaults. Package pinning and image selection do not restart services. Verify public readiness and authenticated build/consumer behavior after a separately authorized Kolla rollout.

Package references require a separate trusted `palimpsest_package_public_origin`; it was empty in the inspected runtime. For the Afterglow key-gateway deployment, use the approved `https://cloud.dmslab.re.kr` origin rather than inferring it from Hub Host or the internal transport. Verify nonempty exact protected IDs, current requester service leaves and original-token `/v1/projects/current` through Afterglow. Do not use admin/service credentials for package acceptance, grant ordinary users automatically, or claim health 200 proves authenticated reads/writes.

### Unreleased GPU and fixed k3s runtime profiles

These candidate contracts are not enabled by installing the client wheel alone.
`palimpsest_instance_gpu_profiles` and `palimpsest_instance_cluster_profiles`
default to empty. Profiles require exact original project IDs and a separately
content-pinned guest image in `palimpsest_instance_base_images`; preserve existing
operator values rather than copying a historical image/flavor into production.

GPU entries pin image/flavor/PCI aliases, actual driver version and device count.
The guest base must already contain working NVIDIA driver and vendor CLI/library
1.18+. CUDA `base`/`runtime`/`devel` container tags describe application CUDA/cuDNN
components, not proof that this guest injection runtime or a compatible kernel
driver is installed. The tested CUDA12.6 devel image supplied nvcc12.6.85 and no
`nvidia-container-cli`. Do not install drivers/toolkits during measured workload
bootstrap; use a separately approved base-creation worker and immutable result.

Cluster entries pin image, permitted CPU flavor IDs and original projects. Only
the fixed two-node `cluster_profile` grants the whole-VM root/writable-cgroup
exception; normal CPU/GPU policy remains unchanged. Runtime inputs require the
fixed offline k3s image recipe and hash-pinned airgap archive. `cluster_server_id`
selects the same original user's unexpired server and exact retained stack, not an
arbitrary join URL/token or remote shell. See [precise execution/proof boundaries](benchmarks.md#fixed-api-k3s-qualification).

The prepared-root instance API requires a coordinated reviewed API/client source
cutover; installing a client wheel alone does not deploy it. Initial instance
creation allocates only one exact-project image-backed Cinder root and returns
while it is preparing. Pure GET observes `provider_phase=volume_ready` without
starting a VM. Explicit `POST /v1/instances/{id}/advance` with
`{"operation":"create-server","request_id":"<original UUID>","boot_volume_id":"<bound UUID>"}`
is the only first-Nova path. Nova attaches that existing volume, not an image
source. Preserve the original bootstrap expiration and all current Nova/Cinder/
GPU-host settings; this repair does not increase provider wait limits.

Deploy source-compatible callers together: record each explicit advancement
intent before sending once, then use GET-only settlement after an unknown reply.
Do not assume an older client which only creates and polls GET will start Nova.
Prepared-volume-only DELETE records cleanup intent without deleting Cinder;
when GET exposes `delete_volume_ready`, advance with `operation="delete-volume"`
and the same original request/boot identity. Unknown provider deletion remains
GET-only until actual404. `server_create_attempted=false` is a versioned no-call
certificate; null legacy/unknown state is never evidence that no server exists.
Existing server-backed DELETE may still remove Nova. This change adds private
versioned state to the existing instance spec JSON and uses `boot_volume_id`;
no new database column or raw-token persistence is required. Preserve existing
server-backed rows and uncertain journals rather than replaying old creates.
See the [instance state and ownership contract](../ARCHITECTURE.md#instance-effects-guest-boundary-and-result-semantics).

For the fixed k3s context, set explicit matching API/worker
`palimpsest_max_context_bytes` and `palimpsest_max_context_expanded_bytes` to at
least256MiB; keep worker RUN network `none`. Larger TensorFlow contexts require
their own explicit bounds and physically reserved scratch. Increasing a logical
limit does not create disk capacity. Never prune shared caches or resize/reformat
existing disks to hide insufficient capacity.

Operator storage administration uses a separate project-scoped application
credential, not workload auth or repeated global-admin password attempts. In the
current bounded worker preparation, the required private file onwireguardserver
is `/home/pieroot/.config/palimpsest/worker-storage-clouds.yaml`, ownerpieroot,
parent0700/file0600, cloud `palimpsest-worker-storage`, `v3applicationcredential`,
verified public Keystone `/v3` andRegionOne. The token's actual project must match
the exact approved worker project before Cinder or attachment effects. Do not put
secrets in chat, persist caller tokens, change IAM, reset administrator passwords,
or fall back to a different identity after authentication fails.


### Hub liveness and readiness (0.3.6)

`GET /health` and `GET /v1/health` remain dependency-free liveness endpoints
returning `200 {"status":"ok"}`. Keep them for process-alive diagnostics;
they must not admit traffic or certify SQL/Redis/storage availability.

The released API uses `GET /v1/ready` for container health. Role templates also
select it for internal/public HAProxy probes, requiring HTTP 200, but the limited
0.3.6 production rollout deliberately skipped shared HAProxy reconfiguration.
Do not infer that installing those templates changed active loadbalancer probes.
An external monitor should check readiness directly, never substitute liveness.
Readiness is unauthenticated and is not requester/Keystone/Glance authorization,
worker execution, backup/restore, CLI byte integrity or native-runtime proof.

For anonymous external monitoring with certificate verification:

```sh
curl --fail --silent --show-error --max-time 5 \
  https://palimpsest.dmslab.re.kr/v1/ready
```

Healthy responses are `200` with `status: ready` and `database`, `redis`,
`blob_store` checks all `ok`. Alert on HTTP 503, other non-200 responses,
connection/TLS failure or timeout. No token is required; responses are sanitized
and use `Cache-Control: no-store`. The shared asynchronous probe wait is two
seconds; it is not a kernel-I/O, shutdown or event-loop-starvation deadline.

The API-only Dockerfile healthcheck uses loopback port `8020`. Kolla overrides
the probe with `palimpsest_api_listen_address` and
`palimpsest_api_listen_port` (role default `18020`, recorded production override
`18021`). Both use a 5-second HTTP timeout, 30-second interval, 10-second
container-check timeout, three retries and 30-second start period. Export and
build-worker images do not inherit the API probe. HAProxy still expects status
200 and retains its existing timing defaults; applying the readiness path to
running shared loadbalancers requires separately scoped reconfiguration.

The canonical role starts containers with `state: started`; it has no existing
readiness-wait task. This change does not add a second deployment/wait mechanism
or equate successful container creation/reconfigure with readiness. Operators
must explicitly wait for container health and direct/public `/v1/ready` before
reopening traffic, then complete the stronger acceptance gates below. The
recorded 0.3.6 rollout separately observed container health and direct/public
readiness; neither replaces authenticated artifact or worker acceptance.


### Multi-controller Hub HA (0.3.7)

This source supports API and export-worker replicas on every approved controller.
It does **not** move recipe execution onto controllers or create extra build VMs:
the existing dedicated build worker keeps HTTPS-only access and private scratch.
Publication, storage provisioning and rollout require their own owner approval;
the dated 0.3.6 single-controller record below is not an HA deployment claim.

**Shared CAS is mandatory.** Put the intended API/export hosts in `[palimpsest]`
and explicitly configure `palimpsest_hub_storage_mode: shared`,
`palimpsest_hub_shared_path` (a pre-existing canonical absolute mountpoint),
`palimpsest_hub_shared_fstype` (`ceph`, `fuse.ceph` or `nfs4`) and
`palimpsest_hub_shared_source` (the exact actual `findmnt --json --mountpoint`
SOURCE). Keep the same container `palimpsest_hub_path` and SQL database everywhere.
The default `local` mode rejects a multi-host inventory before Docker mutation;
matching local volume names cannot substitute for shared storage.

Provision a dedicated least-privilege share/access identity outside this role.
Do not reuse another workload's share or broad backend key. Prepare numeric
UID/GID ownership for the pinned non-root image and a non-writable underlying
local mountpoint, then independently prove cross-controller byte visibility,
exclusive `flock`, atomic rename and file/directory `fsync`. Mount metadata and
readiness alone do not prove these properties, free capacity or runtime mount-loss
fencing. Keep the original local volume and writer-fenced paired backup intact.

The role never mounts a filesystem, creates the shared path or recursively
chowns it. It checks non-symlink canonical path and exact TARGET/FSTYPE/SOURCE
before bootstrap, again immediately before the bootstrap container, and before
each API/export writer start. Prepare shared ownership separately; the local-mode
volume-owner container is deliberately skipped in shared mode. Pull, stop and
destroy remain available while a shared mount is missing. Mount loss after the
last check is an operational failure, not a guaranteed race-free guard.

**Discover the Redis primary.** Kolla derives
`palimpsest_redis_sentinel_addresses` from its Valkey inventory/port and
`palimpsest_redis_sentinel_master` from the existing monitor name. These become
JSON `REDIS_SENTINEL_ADDRESSES` pairs and `REDIS_SENTINEL_MASTER` in API/export
containers. `REDIS_URL` retains the data-server username/password, database and
options, but its hostname is not a fallback. `palimpsest_redis_sentinel_password`
is a separate discovery credential; stock Kolla has no Sentinel client AUTH,
so its default is empty, never the Valkey master password. Do not reconfigure or
fault-inject the shared Valkey service to qualify this client.

Discovery/data socket waits default to two seconds in HA mode; URL overrides
must be finite and greater than zero, at most30seconds. `rediss://` enables
verified TLS for discovery and data with the URL's explicit TLS options.
Unavailable discovery/master fails closed. The next operation may reconnect
and discover a successor, but the client never replays an ambiguous write.
Redis asynchronous replication does not guarantee preservation of every token.
Standalone use requires empty Sentinel addresses/master/password. Hub requires
redis-py5.0.1 or newer for deterministic asynchronous connection cleanup.

**Cut over once, with all writers stopped.** Complete the existing protected
publication, original-requester baseline, all-writer drain, paired encrypted
SQL/CAS backup and isolated restore gates below. Copy the cold CAS into the
qualified share preserving numeric metadata and verify bytes before starting
new replicas. Bootstrap once through the new pinned API image. Use canonical
Kolla pull/reconfigure limited to the approved controller set, retaining
`--skip-tags haproxy`; use the packaged playbook separately for the unchanged
dedicated worker placement. Do not rerun Keystone/reader provisioning.

Changing active shared HAProxy requires separate narrowly scoped approval:
only Palimpsest backend membership/probes, all approved controller addresses,
`/v1/ready`, config validation and graceful reload. Do not reconfigure other
routes, Keepalived or ProxySQL. Before reopening require all replica readiness,
original-project authenticated upload/download byte equality, cross-replica
visibility, SQL ownership/fencing, API-service failover and sustained monitoring.
An idle worker is not a successful recipe execution or native qualification.

### Current production rollout procedure and gates

The latest recorded production release is **0.3.6**, deployed on 2026-10-17
from reviewed `v0.3.6` source `b8d0d49d587e0e86d36a6e1e99875930bba0fa2a`.
Its [publication and production receipts](testing.md#published-036-production-rollout)
record all three immutable image pins, writer-fenced paired SQL/CAS restore,
offline bootstrap, scoped Kolla pull/reconfigure, external readiness, original
requester CLI bytes and authenticated worker idle claims. Every Kolla action
retained `--skip-tags haproxy`; the running shared loadbalancers were not changed.
Publication and deployment remain separate owner decisions; these instructions
do not authorize another rollout or native qualification.

1. **Bind reviewed published bytes.** Complete the protected-dev PR/required
   exact-SHA CI gates and immutable release publication before installing a new
   operator wheel or pinning images. The canonical
   [Hub workflow](../.github/workflows/hub-docker.yml) builds API, export-worker
   and build-worker targets from repository-root context and
   `docker/hub/Dockerfile`; PR runs do not push. Repository release tags, not
   an independently installed Hub wheel version, determine image tags. Current
   publication is Linux amd64; local arm64 builds are not arm64 publication.
   Inspect each required manifest/revision against the same reviewed release
   commit and pin each `palimpsest_*_image_ref` to its immutable digest. A version
   default, mutable `dev`/`latest`, installed root wheel, or prior release image
   cannot contain unpublished working-tree changes.
2. **Preserve operator ownership and scope.** Record actual old image digests,
   container IDs/start times, configuration, secret inputs, numeric storage
   owners/modes and mounted paths. Preserve controller1 placement, ports
   `18021`/`8021`, the public HTTPS origin, worker key/hostname pins and
   `/etc/kolla/globals.d/99-palimpsest.yml` precedence over aggregate-owned
   `90-openstack-afterglow-globals.yml`. API/export need the same SQL and exact
   coherent CAS/locking view; identical host-local volume names are not shared
   storage. The remote build worker keeps private local scratch and HTTPS only.
3. **Fence all writers and prove recovery before schema cutover.** Close new
   ingress; stop/drain the remote build worker **while the API is still up**,
   settle any uncertain claim by server status and the preserved private journal,
   then drain/stop every old API/export/build/upload/GC writer and already-started
   blocking effect. Do not infer this from health, idle polling, lease expiry or
   a single container's exit. Capture paired SQL and CAS from one no-writers
   interval, plus private configuration and exact prior pins. Encrypt/restrict
   backups, record hashes, preserve numeric ownership and prove an isolated
   restore. Preserve unknown scratch/loops/jobs/Trusts; never replay them or
   delete volumes to force a clean start.
4. **Apply reviewed schema offline.** Run the new pinned API image's
   `palimpsest-hub-bootstrap` with approved private database/config inputs while
   every writer remains stopped. Current bootstrap creates missing tables
   (including `palimpsest_worker_leases`) and adds nullable build
   `lease_expires_at` when absent; there is no migration ledger, ownership
   backfill or legacy guest adoption. Empty-database smoke is not compatibility
   proof for the actual existing schema. `reconfigure.yml` does **not** bootstrap;
   `deploy`/`upgrade` do, but neither orchestrates the all-writer stop/backup.
   Migration into a separate empty initialized database is a distinct operation;
   it copies durable rows, not blob bytes or transient maintenance leases.
5. **Use the existing image-mode lifecycle.** After the approved operator wheel
   install and immutable pins, pull both placements. For the existing production
   placement, the controller path is:

   ```sh
   kolla-ansible pull -i /etc/kolla/multinode --tags palimpsest \
     --limit dms-controller1 --skip-tags haproxy
   kolla-ansible reconfigure -i /etc/kolla/multinode --tags palimpsest \
     --limit dms-controller1 --skip-tags haproxy
   ```

   Between pull and reconfigure, complete the offline schema gate above.
   The deployed aggregate does not target `palimpsest-build-worker`; use the
   [packaged worker playbook](#remote-build-worker-deployment-034-source-candidate)
   with `pull` then `reconfigure` for an existing worker (the recorded 0.3.5
   rollout used `pull` + `deploy`). Start only the new version; old recovery must
   not coexist with new live claims. Reconfigure includes precheck/config/start
   without rerunning Keystone/reader provisioning (`update_password: always`).
   Do not broaden controller limits, run global genconfig, or remove the
   historical HAProxy skip implicitly. Installing new role defaults alone does
   not update a running HAProxy: applying the revised internal/public probes
   through the separately owned aggregate/loadbalancer path needs its own scope
   approval and rendered-config/runtime evidence.
6. **Gate before reopening.** Require expected new container IDs, exact pinned
   images/revisions/versions, API container health, direct
   `http://<controller-address>:18021/v1/ready` and public verified-TLS
   `https://palimpsest.dmslab.re.kr/v1/ready` returning 200. Confirm actual SQL,
   Redis and CAS behavior, current original-requester authorization and CLI
   upload/download bytes, restored reads, restart persistence and worker
   claim/execution/cleanup separately. Health/readiness do not replace those
   acceptance gates. Compare the unrelated-container baseline. Rollback requires
   another all-writer stop, exact previous images/config and paired SQL/CAS
   restore, not older workers over new ownership state.

**Source-mode constraint:** `palimpsest_source_version` deliberately remains
`c4887f7806608e98f215abbd377d2eafe159ff76`; changing the image tag does not
advance it. Override only to the separately reviewed, published full commit
SHA. Source builds reject dirty or wrong-HEAD existing checkouts; use a separate
clean checkout rather than resetting operator work. Local image refs must use
`afterglow-local/palimpsest-hub-{api|worker|build-worker}:<12-hex-commit>`.
`pull` skips registry pulls in source mode and `reconfigure` does not build;
source builds occur in canonical `deploy`/`upgrade`, subject to the same offline
cutover gates. Never self-pin a dirty or not-yet-published release tree.


**Historical operations, 2026-10-13/14 (0.3.3, not current production):** canonical Kolla
deployment put API/export-worker 0.3.3 on `dms-controller1`. The observed immutable live refs were:

- API: `ghcr.io/openstack-afterglow/palimpsest-hub-api@sha256:cb5b32ef81f8db0380821146d50eed0f4eba9b26a4361a177fa0e1cef96ef979`
- Export worker: `ghcr.io/openstack-afterglow/palimpsest-hub-worker@sha256:99d6193e889c92b4512dece8cf2ce1c20c4372f1d2d64db301f1931a5bdd0af3`

Live token creation returned 201; `/v1/me` returned 200 with system-admin upload false under 0.3.3;
`POST /v1/builds` returned 503 `isolated build worker is not configured`.
Backups were empty-state logical SQL (14 tables, zero rows) and configuration
only, not writer-fenced CAS capture or isolated restore. The approved worker VM
`palimpsest-hub-build-worker-034` was created in `palimpsest-service` with
`cpu.2c_8g`, Ubuntu 24.04, a 40 GiB boot volume, no floating IP and SSH limited
to the operator. It has loop-control; verified-TLS Hub health returned 200
through explicit reachable IPs and GHCR/PyPI egress was observed. Its service
DNS lacks the Hub name. These are provisioning/connectivity observations, not
a deployed worker, completed build or native qualification.

### Historical 0.3.1 release and cutover record

The following pins and held-rollout record describe historical 0.3.1, not
current production or the released 0.3.5 integration. Their backup,
authority and recovery prerequisites remain applicable with the exceptions below.


The Hub 0.3.1 package registry and retained closed-transport pool recovery are
separate service code; update both reviewed Kolla API and worker image digests
to deploy it. A root wheel upgrade does not update running services.

The immutable release source is `1458db42a1449a25b664584d144d0a97086f8f6f`.
At that release, root/Hub/locks/modules/Kolla image tag were synchronized at
**0.3.1**. Published API/worker revisions matched that source; the observed
`0.3.1`, `v0.3.1`, `sha-1458db4` and `latest` tags resolved to these indexes:

- API: `ghcr.io/openstack-afterglow/palimpsest-hub-api@sha256:4ee1a42d14f4b0193877d1cd7d4f55187a987628097ec424eda2b0b7812d929a`
- Worker: `ghcr.io/openstack-afterglow/palimpsest-hub-worker@sha256:4564a89553132366f38fd23be2ee101262a4f99a9962afdae37a0975511814a6`

Both published images are **Linux amd64 only**. No arm64 publication or literal
`stable` alias exists in the observed release; `latest` is not a deployment pin.
The Dockerfile supports prior amd64/arm64 source builds, which do not establish
an arm64 release manifest. Runtime smoke executed the published amd64 digests
under cross-architecture Docker Desktop on arm64, not on a native amd64/KVM host.
See [qualification boundaries](testing.md#published-031-verification).

**The 0.3.1 rollout was held** in this record. For any new Kolla cutover, the operator must:

1. Preserve the previous **actual** API/worker digest pins, existing private
   Kolla secret/config inputs, mounts and service identities. Both images must
   come from the exact reviewed release ref. Source mode still defaults to
   `c4887f7806608e98f215abbd377d2eafe159ff76`, not this release; override it
   only with the separately reviewed `1458db42a1449a25b664584d144d0a97086f8f6f`. The source-build task rejects
   a dirty existing checkout or a different existing HEAD; prepare a separate
   clean checkout path rather than resetting an operator's checkout.
2. Stop new ingress and quiesce **all** API/export/build/upload/GC writers, then
   capture SQL and CAS from the same no-writers interval. Keep backup/restore
   evidence and verify an isolated restore before production cutover. Do not
   delete volumes, retained jobs, unknown guest state or Keystone Trusts.
3. Use the reviewed release API image's `palimpsest-hub-bootstrap` to create missing
   tables. It is additive `create_all`, not an ALTER migration. For a separate
   database, bootstrap an empty destination, run
   `palimpsest-hub-migrate-data --source-url "$SOURCE_DATABASE_URL" \
   --destination-url "$DESTINATION_DATABASE_URL" --dry-run`, then the same
   command without `--dry-run`, with credentials supplied privately and output
   restricted. Migration rejects a nonempty destination and incomplete native
   package schema; it copies all package tables, exports/delegations, builds,
   layers/grants/uploads, skipping optional legacy tables only when absent.
   Review private URI handling because this legacy CLI accepts URLs on argv;
   do not put a real password in shell history or diagnostic logs.
4. API and export worker must use the same SQL and exact absolute store path
   with a coherent filesystem/locking view. Default Docker named volumes are
   host-local: multi-host API/export placements do **not** become shared CAS
   merely because their SQL/volume names match. Supply reviewed shared storage
   or select one storage-owning placement. The 0.3.4 remote build worker instead
   uses HTTPS and private local scratch, never shared SQL/CAS/NFS. Preserve
   numeric owners/modes and allow for temporary double-sized finalization,
   bundle expansion and bounded worker scratch.
5. Supply trustee service-project credentials, the separate system-reader-only
   validator, nonempty exact protected user/project ID sets, current role graph,
   immutable namespace bindings and the trusted package HTTPS origin. Confirm
   actual Keystone Trust create/authenticate/impersonating-delete policy and
   finite least-role delegation (default member, 21600 seconds) with a real
   requester. Never add tenant grants or fall back to service tenant scoping.
   Pre-cutover undelegated queued exports fail `delegation_required`; requesters
   resubmit them. Existing manual tenant grants require a separate owner decision.
6. Align the separately owned Afterglow Kolla overlay and package-key gateway,
   then use its normal precheck/reconfigure path for API/export. The 0.3.4
   remote worker uses the separate Palimpsest-owned playbook below; the deployed
   Afterglow aggregate does not target its inventory group. Verify real
   SQL/Redis/auth/CLI bytes, cleanup, restart persistence and restored reads/
   writes before reopening ingress. Health 200 proves only liveness.

### Service-role provisioning prerequisite before reopening ingress

The 0.3.1 release gates every legacy artifact write and
`POST /v1/image-exports` on current `palimpsest-publish_editor`; the export
worker rechecks it before Glance I/O. Plain existing `member`/`reader` grants
are not service authority. Without an approved role-graph/assignment cutover,
existing export consumers (including Afterglow requesters) return 403. The
Palimpsest Kolla role registers the trustee and system reader only: it does
**not** create these service roles, inference rules or requester grants.

The 0.3.1 preset-only preparation created global roles and inference edges
without requester assignments; its read-only inventory found 106 ordinary
memberships without service grades across 43 users/41 projects (104 enabled).
This is historical cutover risk, not 106 exercised denied requests. On
2026-10-13/14 an approved grant of three Palimpsest leaves to `test` in project
`test` returned 204 for each; the owner later added Palimpsest roles to
`pieroot`/`test` themselves. These observations do not authorize blanket grants
or prove the 0.3.4 upload policy live. Review each requester's intended grade.

Before reopening ingress, the IAM/operator owner must explicitly approve and
provision unique **global** roles with these exact names, then verify their
immutable IDs and actual acyclic Keystone inference graph:

- Leaves: `palimpsest-inventory_reader`, `palimpsest-download_user`,
  `palimpsest-publish_editor`, `palimpsest-keys_editor`,
  `palimpsest-keys_admin`.
- Documented parent grades: `palimpsest_reader`, `palimpsest_user`,
  `palimpsest_editor`, `palimpsest_admin`.
- Preset grade edges: `palimpsest_admin` → `palimpsest_editor` →
  `palimpsest_user` → `palimpsest_reader`.
- Preset grade-to-leaf edges: reader → inventory_reader; user → download_user;
  editor → publish_editor and keys_editor; admin → keys_admin, using the full
  exact names above.
- Each non-reader leaf also implies `palimpsest_reader` through an actual
  Keystone inference rule, supplying inventory without inventing download or
  key-management authority. Role inference must not grant base membership.

Assign only the owner-approved grade or independent leaves to the intended
requesting users/groups in each exact project, retaining separate baseline
`member` (or inventory-only `reader`) authority. Existing export requesters need
current publish capability and the delegated member role; artifact downloads
separately need download capability. Do not grant every leaf to every tenant,
assign the trustee to tenant projects, weaken the Hub gate or silently remove
existing grants. If operators choose granular leaves or a different graph, it
must still provide exactly the approved effective capabilities; the Hub never
hardcodes parent expansion. Obtain a renewed original subject token after any
assignment upgrade not represented in that token.

The system reader must be allowed to read the complete current roles,
role-ID inference rules and effective assignments, including domain-role
records referenced by the inference listing. Confirm current original-token
and key-owner intersections, a real authorized export and denied plain-member
write before reopening ingress. Record exact role/edge/assignment receipts and
live Trust delete proof privately. This is an approval-requiring operator step,
not an IAM mutation performed by source preparation.

## Package catalog

| Distribution | Install selector | Entrypoints | Purpose |
| --- | --- | --- | --- |
| `palimpsest-client` | repository root | `palimpsest` | Local artifact, build, registry, VM, Compose, store, and UI CLI |
| `palimpsest-client[kvm]` | repository root plus `kvm` extra | `palimpsest` | Local CLI plus `libvirt-python>=10.0.0` for libvirt backends |
| `palimpsest-client[cloud]` | reviewed working source plus `cloud` extra | `palimpsest build --executor nova`, `palimpsest cloud` | Native Nova orchestration using OpenStack SDK and YAML; project resources, writable build storage, GPU flavor and SSH trust remain explicit operator inputs |
| `palimpsest-hub` | `#subdirectory=hub` | `palimpsest-hub`, `palimpsest-hub-worker`, `palimpsest-hub-build-worker`, `palimpsest-hub-bootstrap`, `palimpsest-hub-migrate-data` | Standalone Hub API and web console, separate export/build workers, schema bootstrap, migration |

The distribution rename does not change the Python module `palimpsest_local`
or the `palimpsest` CLI.

The base Local distribution has no required Python runtime dependencies. The
Hub distribution is separate and installs FastAPI, Uvicorn, SQLAlchemy/asyncmy,
Redis, OpenStack SDK/Keystone, Pydantic, multipart, and rate-limit dependencies.
Use the same reviewed Git ref for Local and Hub to prevent source skew.

For the unreleased Nova workflow, install the current trusted checkout with `uv tool install --force --python 3.11 '.[cloud]'`. This adds `openstacksdk>=3.3,<5` and PyYAML only to the optional client environment. Controller authentication comes from the selected SDK cloud profile, is TLS-verified and is never embedded in guest cloud-init. Install the same reviewed wheel on the internal acquisition worker before running its `cloud registry-worker` command; a local client upgrade does not deploy a Hub API/worker or authorize host GPU changes. See [Nova workflows](cli/workflows.md#9-native-nova-builds-and-internal-layer-workers).

The root wheel also installs the `palimpsest` Kolla-Ansible role as shared data
under `share/kolla-ansible/ansible/roles/palimpsest`. It does not install
Kolla-Ansible, Ansible, Hub, or their runtime dependencies; deployments pin
Kolla-Ansible independently. The released role defaults to Hub image tag `0.3.1`,
while source builds bind to the configured reviewed checkout commit SHA. Both
published image digests above must be inspected and pinned for the deployment
host; source metadata alone does not establish runtime readiness.

The role's release matches the Hub 0.3.1 source. Operators deploying
the package registry or closed-connection fix must verify reviewed API/worker
image digests in Kolla globals; a Python update does not change running containers.

### Install the Kolla role from PyPI

In the existing operator project's dependency array, pin
`palimpsest-client==0.3.3` and remove only the `palimpsest-client` Git entry from
`[tool.uv.sources]`. Keep Kolla and the other packages' existing pins unchanged.
Normal release installation does not require GitHub access to Palimpsest and
does not enable a source-image build.

```sh
uv --directory /etc/kolla lock
uv --directory /etc/kolla sync --locked --inexact --no-install-project \
  --reinstall-package palimpsest-client
/etc/kolla/.venv/bin/palimpsest --version
```

Explicit package-only reinstall is necessary when replacing a Git installation
with the same PyPI version: a plain sync can only audit the old installation.
Verify the lock's PyPI wheel hash, installed role bytes and unchanged unrelated
package versions. This installs role data; it does not pull or restart the Hub.


On deploy and upgrade, single-host `local` mode prepares only the root of its
named Hub volume for the pinned image's runtime user; it does not recursively
change blob children. Multi-host placement requires qualified `shared` mode,
which checks the existing mount and skips ownership changes entirely.
Database bootstrap remains a single operation on the first host. See the
[shared-CAS preparation and rollout gates](#multi-controller-hub-ha-037).

For an operator-owned public Hub URL, configure the HTTPS origin and explicitly
enable public HAProxy exposure:

```yaml
palimpsest_public_endpoint_url: "https://palimpsest.dmslab.re.kr"
palimpsest_public_haproxy_enabled: true
```

The role derives `palimpsest_public_haproxy_fqdn` only from an exact HTTPS
hostname origin, with an optional single trailing slash. Ports (even `:443`),
paths, queries, fragments, credentials, whitespace and non-HTTPS URLs derive
an empty hostname and disable public routing, including in HAProxy-only plays.
The default `https://<kolla_external_fqdn>:8020` cannot claim the main host.
Explicit hostname overrides remain supported and must match the origin at
precheck; they do not implicitly enable exposure. DNS and Kolla external
TLS/certificate setup remain operator prerequisites. The internal VIP endpoint
and private listen port are unchanged.

With the corresponding updated Afterglow role, this explicit endpoint defaults
`afterglow_service_palimpsest_internal_url` and is emitted as
`[services] palimpsest_internal_url` in its generated configuration. A separate
trusted HTTPS transport URL can override the Afterglow variable. Unset/empty
values preserve detailed Afterglow operator TOML, not a catalog fallback.
`palimpsest_package_public_origin` is still a separate package-key gateway origin
(the Afterglow origin in the DMS Lab example), not the Hub routing URL. For
Hub-only rollout, follow the [current production procedure](#current-production-rollout-procedure-and-gates),
including controller1 limits and `--skip-tags haproxy`. Applying Afterglow or
shared load-balancer changes is a separate approval-gated operation, not part of
the Hub-only command. A correct HAProxy route does not install new package APIs;
verify Hub version and authenticated `/v1/projects/current` separately.

Repository tags label both
`ghcr.io/openstack-afterglow/palimpsest-hub-api` and
`ghcr.io/openstack-afterglow/palimpsest-hub-worker` with tags derived from
the repository tag, not from the Hub wheel version. The root
[`v0.2.3` release workflow](https://github.com/openstack-afterglow/palimpsest/actions/runs/36081630018)
completed artifact verification and native stage-1 KVM proof, published through
the `palimpsest-client` trusted publisher, and created the GitHub Release. The
separate [Hub image workflow](https://github.com/openstack-afterglow/palimpsest/actions/runs/36081630008)
published tagged API and worker images. A PR check, development prerelease,
or skipped KVM job alone cannot substitute for the root release gate; neither
published artifact proves an operator deployment.

## Supported hosts and runtime scope

| Host/runtime | Status | Package | Runtime boundary |
| --- | --- | --- | --- |
| macOS Apple Silicon with Lima/VZ | Supported default | `palimpsest-client` | Lima 2.1+; conventional cloud-image VMs |
| macOS Apple Silicon with QEMU/libvirt HVF | Experimental | `palimpsest-client[kvm]` | `qemu:///session`; conventional cloud-image VMs only |
| macOS Apple Silicon with Lima/krunkit | Experimental | `palimpsest-client` | Lima 2.1+, krunkit 1.2.1+; guest Vulkan GPU remoted to host Metal |
| Linux `x86_64` with libvirt/KVM | Supported | `palimpsest-client[kvm]` | `qemu:///system`; conventional cloud-image VMs and the narrower OCI-root runtime |
| Linux `aarch64` with libvirt/KVM | Supported | `palimpsest-client[kvm]` | `virt` + EFI; conventional cloud-image VMs |
| Linux `x86_64`/`amd64` OCI-root | Supported narrow runtime | `palimpsest-client[kvm]` | OCI content becomes guest `/`; Linux KVM and qualified runtime assets required |
| Remote KVM or multi-host scheduling | Unsupported | — | Local-host runtime only |

The conventional cloud-image path and OCI-root path have different root
semantics. A conventional VM boots its cloud image and mounts Palimpsest layers
under `/opt/layers/merged`. OCI-root materializes a Linux `amd64` OCI artifact,
switches the guest root, and runs the workload as PID 1. Portable package
installation is not native runtime qualification.

The branch PR's native stage-1 proof checks OCI-root guest `/` switching and
PID 1; it is not a conventional cloud-image VM boot. The serial-bound first
boot and cloud-init-disabled reboot readiness changes still lack a post-merge
native conventional-cloud boot/reboot check, even if the stage-1 gate passed.

## Install directly from GitHub

Install the default branch into an active Python 3.11+ environment with pip:

```sh
python3.12 -m pip install \
  "palimpsest-client @ git+https://github.com/openstack-afterglow/palimpsest"
palimpsest --version
```

For a standalone CLI, prefer a uv tool environment. This path does not inherit
an unrelated project's Python support range and can provision Python 3.11 on a
host whose system interpreter is older:

```sh
uv python install 3.11
uv tool install --python 3.11 \
  "palimpsest-client @ git+https://github.com/openstack-afterglow/palimpsest"
palimpsest --version
```

For a uv-managed application dependency, declare a compatible project range
when creating the project:

```sh
uv python install 3.11
uv init --bare --python 3.11
uv python pin 3.11
uv add "palimpsest-client @ git+https://github.com/openstack-afterglow/palimpsest"
uv run python -c "import importlib.metadata as m; print(m.version('palimpsest-client'))"
uv run palimpsest --version
```

uv resolves every version admitted by the application's `requires-python`
value. Merely pinning Python 3.11 does not make a project declaring
`requires-python = ">=3.10"` compatible: raise the project floor to 3.11 or use
the isolated tool path. Do not use the suggested `--frozen` escape hatch; it
skips locking/syncing and does not make the package runnable on Python 3.10. If
`uv init` created a local application that also owns the `palimpsest` command,
the `importlib.metadata` probe above is the authoritative installation check.

The root distribution's installed version is reported under `palimpsest-client`;
for a 0.2.3 install this probe prints `0.2.3`.

uv records the dependency as a Git source and locks its resolved commit. For a
reproducible pip or uv installation, pin a reviewed full 40-character SHA:

```sh
PALIMPSEST_REF="FULL_40_CHARACTER_COMMIT_SHA"
python3.12 -m pip install \
  "palimpsest-client @ git+https://github.com/openstack-afterglow/palimpsest@${PALIMPSEST_REF}"
uv add \
  "palimpsest-client @ git+https://github.com/openstack-afterglow/palimpsest@${PALIMPSEST_REF}"
```

A branch or tag can replace the SHA after `@`, but moving refs are not
reproducible. Review the commit before use. A VCS installation validates the
fetched Python project through the normal build backend; it is not a
signed-release or native-KVM claim.

### Add libvirt support

When host libraries and the approved Python dependency source are ready,
replace or upgrade the base installation with the `kvm` extra:

```sh
"$HOME/.venvs/palimpsest/bin/python" -m pip install --upgrade \
  "palimpsest-client[kvm] @ git+https://github.com/openstack-afterglow/palimpsest.git@${PALIMPSEST_REF}"
```

Building `libvirt-python` may require platform libvirt development headers. The
extra does not configure libvirt, QEMU, firmware, networks, permissions, or
`/dev/kvm`.

### Install the standalone Hub

Keep Hub in its own environment and pin it to the same ref:

```sh
python3.12 -m venv "$HOME/.venvs/palimpsest-hub"
"$HOME/.venvs/palimpsest-hub/bin/python" -m pip install --upgrade pip
"$HOME/.venvs/palimpsest-hub/bin/python" -m pip install \
  "palimpsest-hub @ git+https://github.com/openstack-afterglow/palimpsest.git@${PALIMPSEST_REF}#subdirectory=hub"

"$HOME/.venvs/palimpsest-hub/bin/python" -m pip show palimpsest-hub
```

Do not start `palimpsest-hub` merely as an installation check: it starts the
service and requires its database, Redis, blob path, and OpenStack settings.

For server-side builds, use the dedicated CPU-only service VM described below,
not a controller, API container or nested KVM host. The canonical worker image
installs the exact same-source root wheel in the Hub venv with `qemu-utils`,
`bubblewrap`, `squashfs-tools`, `util-linux`, `mount` and `e2fsprogs`; it needs no
Local `[kvm]` extra or libvirt. API/export still need SQL/Redis/Keystone, and
export needs the reader/protected-ID settings. None of those credentials or
the Hub CAS path is passed to the build VM. Integrated ownership bootstrap is described below.

### Remote build worker deployment (0.3.4 source candidate)

This heading preserves the original 0.3.4 introduction/link target; the same
private HTTPS execution boundary is retained by 0.3.5, with API SQL ownership
and offline additive schema rollout described above/below. The original
introduction is not current publication or native acceptance evidence.

Provision a dedicated Linux x86_64 CPU-only Nova VM in `palimpsest-service`.
The API admits x86_64 bases only and the executor requires the VM machine to
match the input architecture. Do not place it on a controller or an active CI
runner. Enable `enable_palimpsest_hub_build_worker: "yes"` only with a nonempty
`palimpsest-build-worker` inventory group disjoint from `control`, `controller`,
`controllers`, `compute` and `palimpsest`. API/export/bootstrap remain on
`palimpsest` hosts; metadata-first `palimpsest_service_catalog` and host-local
`palimpsest_local_services` prevent worker-only hosts from templating SQL,
Redis, Keystone, reader values or `palimpsest.conf`.

Set these nonsecret operator inputs in the approved Kolla configuration;
keep `palimpsest_build_worker_key` in private `passwords.yml`/Vault, not this
example. Use 32–4096 printable non-space ASCII characters, matching the API's
`SecretStr` constraint. Kolla supplies it to API
`PALIMPSEST_HUB_BUILD_WORKER_KEY` and creates the worker's root-owned 0600
regular file `{{ palimpsest_config_dir }}/build-worker.key`, mounted read-only
at `/run/palimpsest/build-worker.key`. The worker reads the key only from that
file, never an environment value, and disables dumpability before reading it.

```yaml
enable_palimpsest_hub_build_worker: "yes"
palimpsest_build_worker_hub_origin: "https://palimpsest.dmslab.re.kr"
palimpsest_build_worker_scratch_volume: palimpsest_build_worker_scratch
palimpsest_build_worker_scratch_path: /var/lib/palimpsest/build-worker
palimpsest_build_worker_timeout_seconds: 3600
palimpsest_build_worker_max_blob_bytes: 107374182400
palimpsest_build_worker_cpus: 2
palimpsest_build_worker_memory: 6g
palimpsest_build_worker_pids_limit: 512
palimpsest_build_worker_etc_hosts: {}
```

`palimpsest_build_worker_etc_hosts` optionally maps the exact origin hostname
to an operator-verified reachable IP **inside the worker container only** when
service DNS lacks it. TLS still verifies the configured origin name through
the system CA; do not replace the origin with an IP or disable verification.
Transport uses stdlib `http.client`, a fresh connection per request and no
proxy, redirect or retry. The watchdog bounds header/body stalls, **not DNS or
connection establishment**; hostname pinning addresses missing DNS but does
not turn establishment into a hard wall-clock deadline.

The service `palimpsest-hub-build-worker` runs container
`palimpsest_hub_build_worker` as root, privileged, with bridge networking and
`init: true` (worker is not PID 1). Its only mounts are the read-only key and
private scratch; there is no `/dev/kvm`, Docker socket, libvirt or Hub CAS
mount. Defaults bound it to 2 CPUs, 6g memory with equal `memory_swap`, and
512 PIDs. Size local disk separately: the 100 GiB blob ceiling is not a disk
quota and cannot fit on the observed 40 GiB VM boot disk. The privileged
**service VM is the kernel isolation boundary**. Bubblewrap isolates recipe
namespaces, not a hostile kernel; recipes remain system-admin-only.

The root wheel installs the Palimpsest-owned lifecycle playbook alongside
Kolla's `site.yml`. After operator pinning and API configuration, invoke the
canonical worker path (replace `<venv>` with the operator Kolla environment):

```sh
kolla-ansible deploy -i /etc/kolla/multinode \
  --playbook <venv>/share/kolla-ansible/ansible/palimpsest-build-worker.yml
```

The same playbook supports `pull`, `reconfigure`, `upgrade`, `stop` and
`destroy`; choose only the approved lifecycle action and preserve retained
scratch until recovery is settled. The deployed Afterglow aggregate at
`/etc/kolla/plugins/openstack-afterglow-v1.30.12/deploy/kolla/site.yml` only
targets `palimpsest`. It does **not yet target the worker group/image**;
aggregate integration is a separate Afterglow follow-up, not implemented here.

For durable operator pins, use the Palimpsest-owned final override
`/etc/kolla/globals.d/99-palimpsest.yml` (0640) for reviewed Hub image refs and
origin. The aggregate-owned regular file `90-openstack-afterglow-globals.yml`
was rewritten by a concurrent Afterglow/Drover rollout and reverted refs to
0.3.2. Canonical argument assembly was observed to load `99-palimpsest.yml`
last; that check did not execute a playbook. Preserve these precedence rules
when pinning a subsequently published candidate, not a mutable `latest` tag.

#### Claim, execution and operational recovery

The worker authenticates `/v1/build-worker/*` with
`X-Palimpsest-Build-Worker-Key` (constant-time comparison); claimed-job routes
also require `X-Palimpsest-Build-Lease`, a UUID nonce in existing `lease_owner`.
`POST /v1/build-worker/claim` returns 202 with job ID/name/lease, UTC deadline,
timeout, recipe/digest, base and parent-first layer descriptors with sizes,
and max output bytes, or 204 when idle.
An API-local flock plus SQL `FOR UPDATE` enforces one globally active build.
The fixed deadline is `started_at + API timeout`; expired attempts become
`error/worker_interrupted` and are never requeued/replayed. Claim revalidates
visibility, chain/base identity, positive sizes and total input bytes within
the max-blob ceiling. Serialized timestamps use UTC ISO offsets.

Exact claimed inputs are fetched through
`GET /v1/build-worker/builds/{id}/inputs/{digest}` with fresh visibility,
CAS rehash and deadline bounds. The direct executor
`python -I -m palimpsest_local.hub_builder <manifest> <worker_pid>` verifies
recipe/chain/digests before mounts. It rejects qcow2 external backing/data
files, converts qcow2 to raw, journals explicit read-only loop ownership and
requires exactly one ext4 root with `etc` and `usr/bin` (`ro,noload`). Private
sysfs-derived partition nodes stay under the job tree; no shared `/dev` writes
or udev. Read-only parent SquashFS lowers are child-first; writable upper/work
use bounded tmpfs; the upper root inherits the effective lower root's numeric
uid/gid, mode, timestamps and xattrs. RUN uses private mount/PID/IPC/UTS/network
namespaces without user remapping, only CHOWN/DAC_OVERRIDE/FOWNER/FSETID/
SETUID/SETGID capabilities, `no_new_privs`, clean PATH/HOME/LANG/LC_ALL,
closed FDs and umask 022. WORKDIR is created inside the sandbox; host root,
sockets and credentials are absent and output is discarded. No-follow
`CLEAN_TARGETS_V1` transient cleanup precedes zstd SquashFS creation bounded
by `RLIMIT_FSIZE = max_output_bytes`; deadline exit is 124.

Before output transfer, executor cleanup must succeed. `PUT
/v1/build-worker/builds/{id}/output` requires exact Content-Length and canonical
`X-Content-Digest`, private 0600 staging, SquashFS magic and SHA-256. Digest
locking lasts through SQL commit; the private layer belongs only to the
original project/creator. A matching repeat after completion returns the same
receipt within a finite transfer deadline; conflicts return 409. `POST
/v1/build-worker/builds/{id}/fail` accepts only `build_failed`, `build_timeout`
or `cleanup_failed` and never downgrades complete. Recipe text is limited to
1 MiB UTF-8 and JSON envelopes to 6 MiB + 64 KiB.

During planned Hub maintenance, stop/drain the remote build worker before
stopping the Hub API to avoid an uncertain claim.

Keep scratch private and persistent across restarts. Its `worker.lock` flock
and `worker.json` journal are recovery authority; do not remove either merely
to unblock polling. The current source retains an `idle` checkpoint containing
the worker's private channel and monotonic claim sequence; an idle journal is
not an uncertain job and must not be deleted to reset that sequence. Operator
disposition and automatic settlement depend on the recorded phase:

- Identified `claim_pending`: the API durably binds the channel, protocol and
  sequence to an empty outcome or an exact job/lease under the claim SQL mutex.
  Authenticated claim recovery settles that identity; it never blindly retries
  the original claim POST. Recovery also fences an unseen request so a delayed
  original POST cannot assign a new job afterward. This recovery endpoint can
  write the claim cursor and is **not a read-only diagnostic**. A recovered job
  retains the no-reexecution rule and is settled through its exact lease.
- Legacy `claim_pending` without durable request identity remains fail-closed:
  preserve its journal and establish an explicitly authorized disposition.
  Counts of current nonterminal rows or unexpired leases alone cannot prove
  that no claim occurred; compare attempt/lease/status fields against the
  pre-worker baseline. Never clear a journal merely to resume polling.
- `claimed` / `executing`: restart never re-executes a recorded job. Preserve
  the tree while the cleanup helper verifies exact process groups and loops;
  a parent-death fence and `builder.pid` / `helper-UUID.pid` records bind
  PID/start ticks/boot ID. Stale records matching the current helper PID are
  previous lifetimes, not authority to kill that helper.
- Recipe `output_pending` and all `fail_pending` records settle through exact-
  lease status, not blind mutation replay. Image `output_pending` additionally
  uses a private authoritative publication receipt: bind the complete descriptor,
  rehash retained packed spans, skip exact committed digest/size stages and send
  only absent exact idempotent stages. Completion requires that same bound result
  and unchanged lease deadline. This never re-executes a build or extends an
  expired attempt. Deploy all API replicas before the compatible worker; pending
  descriptors stay hidden from public image-job results until completion.
- `terminal`: cleanup still must be established before deleting the job or
  claiming another. `preflight <worker_pid> <scratch_root>` and
  `cleanup <job_dir> <worker_pid>` are executor modes, not a license for broad
  host cleanup. Unverifiable cleanup halts and retains scratch. Preserve the
  journal and inspect exact owned processes/mounts/loops; never detach unknown
  loops, kill PID-only matches or erase the tree to manufacture recovery.

The identified-claim protocol requires the additive `palimpsest_worker_claims`
table and coordinated API/worker deployment. Bootstrap it only through an
explicit reviewed deployment after paired SQL/CAS backup and isolated restore;
preserve existing tables and legacy journals. Historical 0.3.5 recovery used
operator disposition, not this protocol, and is not current live qualification.
Record current-source tests, deployment and native workload proof separately.

### Hub API and worker logs

The API, Glance export worker, and separate build worker emit Hub-owned INFO
events by default. API events contain the HTTP method, **matched route
template** (or `<unmatched>`), status and elapsed milliseconds, not the URL.
Claimed export/build jobs emit start and terminal status; idle polling does not
emit a per-poll event. Set `PALIMPSEST_HUB_LOG_LEVEL=DEBUG` on the individual
process to opt in to bounded query presence/length and numeric job state/result
summaries. Other values retain INFO. DEBUG never includes query values, headers,
bearer/download tokens, SQL binds, recipe, disk bytes, paths, OpenStack payloads
or exception text. Hub disables Uvicorn's raw-target access logger at startup,
including when launched through the direct `uvicorn palimpsest_hub.main:app`
command. Do not enable third-party HTTP/SQL trace logging or an independent
server access logger that emits raw targets in production.

For image-build failures the worker retains one private owner-only
`last-image-failure.json` beside `worker.json`, after exact executor cleanup and
before deleting the disposable job tree. It contains the last64KiB from each
executor/BuildKit stream as base64 plus fixed category/errno metadata; final JSON
is limited to266240bytes. Treat these raw tool tails as potentially sensitive:
inspect only through approved operator access, never copy them into public API
errors, ordinary logs or reports. A later failure atomically replaces this one
artifact. It is not journal/claim authority; do not delete an uncertain journal
or adopt diagnostic staging to resume work. Executor stderr survives normal
failed-child finalization, not an arbitrary worker SIGKILL before the tail is
persisted. BuildKit uses a short socket in the existing per-executor private
`/run` tmpfs so persistent job path length cannot exceed its Unix-socket limit.

On a reused asyncmy connection whose uvloop TCP transport was already closed,
Hub marks the failed pool pre-ping as a disconnect and obtains a fresh connection
before the request's SQL operation. It does not retry transactions that lose
their connection after checkout; reconcile those requests before retrying.

### Historical KVM build-worker observations — 2026-09-24

The following dated probes used the superseded KVM executor, not the 0.3.4
remote worker. Their preflight, shared storage, guest sizing and libvirt
requirements are historical only and do not qualify the new direct executor.

2026-09-24 live proof on a dedicated Linux KVM host found two deployment
prerequisites that are not enforced by preflight and cause a `build_failed`
or `cleanup_failed` job with no further detail (the worker never logs
recipe/path content). First, the QEMU execution user must be able to access
the build worker's private job tree and overlay disk. The default QEMU user
could not access the worker-owned tree and guest creation failed with
`Permission denied`. In the live proof, QEMU and the worker ran as the same
dedicated unprivileged user; the QEMU group can differ from the worker's
primary group, and must retain `/dev/kvm` access. Second, keep
`PALIMPSEST_HUB_LOCAL_PATH` short: the guest control socket path is
`<PALIMPSEST_HUB_LOCAL_PATH>/builds/<build-uuid>/state/runs/builder-<id>/builder.sock`.
Linux `AF_UNIX` rejects socket paths that do not fit its 108-byte `sun_path`
(including the terminator); choose a short path such as `/srv/h` and account
for the actual builder ID length. The longer path used in the first live
attempt failed with `UNIX socket path ... too long`.

That probe ran API, worker, and QEMU under one UID and kept an administrator
credential copy in private staging; it did not verify production account or
credential separation. Sharing the UID was a probe-specific access choice,
not a production requirement.

Third, size the dedicated host above the builder guest's fixed 4096 MiB
(`src/palimpsest_local/build.py`) plus QEMU, Hub, database, Redis, and host
overhead. A separate Nova host with only 4,106,240,000 bytes of RAM passed
preflight and accepted a two-layer/two-RUN job, but QEMU could not allocate its
4,294,967,296-byte guest RAM; the job ended `build_failed` before boot. Do not
interpret a successful preflight or HTTP 202 as proof of guest execution.

A separately approved `cpu.2c_8g` Nova host (8,327,811,072 bytes visible in
the guest) subsequently completed one network-none two-layer/two-RUN build.
This qualifies that minimal host/configuration only, not a universal memory
minimum or production isolation. The exact job and cleanup evidence are in
[`development-handoff.md`](development-handoff.md).

The separately approved timeout/restart probe used a fresh 8GiB KVM host,
one private SQL/Redis/store, a 900-second build timeout, and two distinct
network-disabled guest recipes whose `RUN` commands printed a marker before
sleeping 1800 seconds. After observing each live guest and its marker, the
timeout case ended `error/build_failed` with no guest or scratch left; killing
only the recorded build worker main and starting a replacement ended the other
case `error/worker_interrupted` with the same cleanup boundary. The replacement
worker logged one interrupted build reconciled. These outcomes do not qualify
hard power loss, arbitrary recipes, production credential separation, or an
otherwise underprovisioned KVM host. Exact job IDs and cleanup evidence are in
[`development-handoff.md`](development-handoff.md).

## External prerequisites by feature

Python package installation deliberately does not install or mutate host tools.

### macOS Apple Silicon default runtime

- Lima 2.1 or newer with the VZ backend.
- Host virtualization permission and sufficient disk/memory.
- TCP publication only for Compose-shaped project ports.

The experimental libvirt/HVF backend additionally needs QEMU, libvirt,
`qemu:///session`, `hdiutil`, and the Local `[kvm]` extra. With Homebrew,
`brew install qemu libvirt pkgconf` supplies the host tools and the
`pkg-config` executable needed to build `libvirt-python`. Start the local
session daemon with `brew services start libvirt` if it is not already running.
In a source checkout, install and retain the optional binding when running uv:

```sh
uv sync --frozen --extra dev --extra kvm
virsh -c qemu:///session list --all
uv run --frozen --extra kvm palimpsest web --allow-control
```

Plain `uv run` can synchronize away an extra that was previously installed;
include `--extra kvm` for libvirt-backed commands, or use the prepared
`.venv/bin/palimpsest` directly. Verify the Python binding and session connection
in the same environment as the CLI, not just a separately installed `virsh`.
Select `--backend libvirt-hvf` explicitly for a conventional ARM64 cloud image.
Package installation or successful preflight alone does not prove a guest can
boot; Lima/VZ remains the macOS default. The latest handoff records separately
scoped live boot, SSH, console and measurement evidence.

### macOS Apple Silicon GPU acceleration via krunkit

- Lima 2.1 or newer with the `krunkit` driver (`limactl info | jq .vmTypesEx.krunkit`).
- Host `krunkit` 1.2.1 or newer (`brew tap slp/krun && brew install slp/krun/krunkit`).
- macOS 14 or newer on Apple Silicon (`arm64`).
- ARM64 Linux cloud image with Mesa Venus Vulkan support (e.g. Fedora with `slp/mesa-libkrun-vulkan` COPR).

Select `--backend lima-krunkit` explicitly. Default selection (`auto`) remains `lima-vz`. The krunkit driver exposes a paravirtualized `virtio-gpu` device, remoting guest Vulkan commands to host Metal. Direct Hypervisor.framework integration remains an optional alternative if libvirt is bypassed. This does not expose CUDA or the Apple Neural Engine to Linux guests.

### Linux conventional cloud-image runtime

- `/dev/kvm`, QEMU, libvirt, and access to `qemu:///system`.
- Architecture-appropriate EFI firmware and the libvirt `default` network.
- `qemu-img`, `cloud-localds`, SquashFS tools, OpenSSH, and filesystem tools.
- The Local `[kvm]` extra.

The installer does not grant group membership or create libvirt networks.
Linux Compose project port publication is currently rejected for the
conventional cloud-image path.

### Linux OCI-root runtime and registry acquisition

- Linux `x86_64`/`amd64`, KVM, system libvirt, and qualified kernel,
  initramfs/stage-1, packer, and domain-profile inputs.
- Skopeo 1.13 or newer on `PATH` for `palimpsest oci pull`; local OCI archive
  materialization does not require Skopeo.
- Only anonymous HTTPS `linux/amd64` registry acquisition is supported by the
  current `oci pull` contract.

OCI-root networking supports `nat`, `host-only`, and `none` under its explicit
per-VM network contract. This is separate from Compose project publication.

### Docker/OCI and Dockerfile builds

- An installed Docker CLI for OCI-profile `login`, `pull`, `push`, `tag` and Docker inventory/history/save/load/remove/passthrough. Native package push/pull do not use Docker's image store.
- An installed Docker credential helper and exact API-base/namespace `credHelpers` entry (or `credsStore`) for native login. Host-only Docker credentials, `auths` and plaintext fallback are not accepted. The native client also supports an ephemeral `PALIMPSEST_PACKAGE_KEY`, not a legacy Keystone token.
- A separately managed Buildx builder with an OCI exporter for Dockerfile builds. The default `docker` driver does not provide the required exporter.
- Every online build in default native cache mode requires a project-scoped key with both `cache:read` and `cache:write`; OCI output in that mode must pass `--cache-registry NATIVE_ALIAS --cache-package NAMESPACE/PACKAGE`, while native output defaults to its own profile/package. OCI output can instead use `--cache-mode local` with managed host cache and no Hub connection; this rejects native/external cache authorities and is not strict offline mode. CLI package publication additionally requires `packages:inventory` and `packages:write`, not content-download authority. Pull requires effective inventory plus `packages:read`.
- Strict offline Dockerfile builds require preloaded pinned inputs and a
  separately bootstrapped single-node `docker-container` builder using
  `--driver-opt network=none`.

Palimpsest validates the selected builder but does not create, reconfigure, or
grant access to Docker or BuildKit.

## Local CLI configuration

### State and configuration roots

The Local state root is resolved in this order:

1. `PALIMPSEST_STATE_HOME`.
2. `[storage].state_root` in
   `${XDG_CONFIG_HOME:-~/.config}/palimpsest/config.toml`.
3. An explicitly set `XDG_STATE_HOME`, with `/palimpsest` appended.
4. `~/.local/state/palimpsest` for ordinary Linux users and macOS; Linux root
   or the effective `palimpsest` account with home `/var/lib/palimpsest` uses
   `/var/lib/palimpsest`.

In this working source, `palimpsest store show` and `palimpsest store ls --kind
image` work for a normal user without state/log environment variables or Hub
credentials. Existing user state stays in place; explicit namespaces remain
authoritative and unsafe/corrupt state never triggers a fallback or migration.
`store show` reports the chosen state root and storage usage, not host readiness.

Linux normal-user journals use `logs/commands.jsonl` beneath the selected state
root and securely create only missing user-owned directories. Root/service
journals stay at `/var/log/palimpsest/commands.jsonl`, preprovisioned separately.
`PALIMPSEST_LOG_HOME` selects a pre-created absolute private directory on either
platform; Darwin otherwise keeps journaling disabled. Existing ownership/modes,
no-follow paths and corrupt records are never silently repaired. See the full
[storage/logging contract](linux-storage-logging.md).

Shell registration is also explicit: `palimpsest completion zsh --install`
(or `bash`/`fish`), then open a new interactive shell. See
[completion setup and preservation limits](../README.md#shell-completion).

### Hub client settings

Hub URL precedence is:

1. Global `--url HUB_URL` before the command.
2. `PALIMPSEST_URL`.
3. `url` or `[hub].url` in `config.toml`.

Legacy Hub artifact commands require the original project-scoped `PALIMPSEST_TOKEN`; keep it in approved secret handling, never `config.toml`, documentation, history or local state. Native package/cache profiles use their own HTTPS API base and package key, not `PALIMPSEST_URL`/`PALIMPSEST_TOKEN`. Package keys cannot queue privileged `/v1/builds`; those remain system-administrator operations. Legacy tokens cannot authorize BuildKit-cache writes.

```sh
export PALIMPSEST_URL="https://hub.example.invalid"
# Inject PALIMPSEST_TOKEN through the approved secret mechanism.
palimpsest image ls --limit 10
```

### Registry profiles and credentials

Profiles live at `${XDG_CONFIG_HOME:-~/.config}/palimpsest/registries.toml`, schema version 1, and contain no credentials. `registry add --protocol oci|palimpsest` defaults to OCI, as do old profiles with no protocol. For unqualified references, selection is `--registry`, `PALIMPSEST_REGISTRY`, then default. A fully qualified authority with no configured profile stays ordinary Docker/OCI, as before; this includes login/logout. A configured authority must map to one protocol, and an explicit `--registry` must match a qualified reference's authority. Strict offline builds never read this file.

Native profiles require `--api-base HTTPS_URL` on exactly the `endpoint` authority and optionally a one-component namespace. Native HTTPS verifies system trust plus absolute `--ca` files and refuses redirects; mirrors, plain HTTP, TLS-skip and Docker cache exporters are rejected. The helper entry is exactly `api_base.rstrip('/') + '/projects/' + namespace`; lookup checks that `credHelpers` entry then `credsStore`, never host-only entries or `auths`. Configure/install `docker-credential-HELPER` first. Native login verifies `/auth/me` before storing anything, uses the key's public UUID as `--username`, and accepts the secret only through stdin/prompt. Native `--namespace` selects the credential namespace. OCI profiles retain Docker's `DOCKER_CONFIG`/`~/.docker` credentials.

An ordinary member first registers the operator-bound namespace and issues a secret-once exact-package/action-bound key through the native control API. Follow the complete [registry flow](registries.md#native-authentication-build-and-transfer), including conditional `cloud.dmslab.re.kr/openstack-afterglow/test:v1` usage only after binding the alias to the actual immutable project UUID. Native `push --input`/`--manifest` and `pull --output` transfer verified OCI-layout archives without loading Docker; local typed receipts and frozen exports live under `state/package-references/` and `state/package-artifacts/`, separate from runtime-layer tags. These new source paths and written tests are not deployment or executed acceptance evidence.

### Compose project identity

Compose project names resolve from `-p`, then `PALIMPSEST_PROJECT_NAME`, then
`COMPOSE_PROJECT_NAME`, then the model/default naming rule. Global Compose
options must precede the Compose subcommand.

## Hub service settings

Hub uses case-insensitive Pydantic environment settings with no custom prefix.
The required settings are:

| Environment setting | Meaning |
| --- | --- |
| `DATABASE_URL` | Async MySQL-compatible SQLAlchemy URL |
| `REDIS_URL` | Redis connection URL |
| `PALIMPSEST_HUB_LOCAL_PATH` | Hub-owned local blob/cache directory |
| `OS_AUTH_URL` | Keystone authentication endpoint |
| `OS_USERNAME` / `OS_PASSWORD` | Export Trust trustee identity. Deferred export authenticates only to its own service project and requester-created Trusts, never a tenant password scope. Human-admin exclusion independently resolves the configured domain/user to exact native IDs through the read-only validator; it never logs in as the trustee. |
| `OS_PROJECT_NAME` | The trustee's own service project |
| `OS_READER_USERNAME` / `OS_READER_PASSWORD` | Separate read-only Keystone validation identity. It must authenticate system-scoped (`all`) with `reader` and no `admin`, `manager` or `service` role. Missing/unavailable or elevated validator authority fails token validation with 503. |
| `PALIMPSEST_HUB_PACKAGE_FORBIDDEN_PROJECT_IDS` | JSON array of exact protected project IDs, for example `'["<project-id>"]'`. A comma-separated list does not parse. Empty policy disables ordinary member/key authority with 503; independently verified human system management supersedes these mixed protected-owner exclusions. |
| `PALIMPSEST_HUB_PACKAGE_FORBIDDEN_USER_IDS` | JSON array of exact protected administrator/service principal IDs, for example `'["<user-id>"]'`. Empty policy disables ordinary member/key authority with 503. Human system management independently excludes actual validator/trustee IDs and service/delegated authentication. |

Human administration and admin-issued bounded key use freshly read the native user's home-domain ID and selected project's domain ID, verify each exact domain resource and require explicit boolean enabled=true. Disabled native requester/project/home/target domains deny with 403; missing/malformed/unavailable metadata is 503. The reader additionally needs complete native domain get/list, configured-domain filtered user lookup, user-group membership and raw user/group exact-system assignments, alongside existing subject/role/graph reads. Configure `OS_USER_DOMAIN_NAME`/`OS_USERNAME` to one existing native trustee domain/user; missing, ambiguous or partial lookup fails closed. Original token or saved key-provenance domain names never skip configured-service ID exclusion after a domain rename. These are read-only prerequisites, not permission to change IAM or fall back to service-password authentication.

Optional settings and defaults:

| Environment setting | Default / constraint |
| --- | --- |
| `DATABASE_POOL_SIZE` | `10`, valid 1–100 |
| `DATABASE_MAX_OVERFLOW` | `20`, valid 0–100 |
| `DATABASE_CONNECT_TIMEOUT` | `10` seconds, valid 1–120 |
| `DATABASE_READ_TIMEOUT` | `30` seconds, valid 1–120; asyncmy handshake/response read limit, separate from TCP connection and pool checkout timeouts; applies to API, export worker and offline bootstrap |
| `DATABASE_POOL_TIMEOUT` | `30` seconds, valid 1–120 |
| `DATABASE_UNHEALTHY_SECONDS` | `30` seconds, valid 1–3600 |
| `PALIMPSEST_HUB_MAX_BLOB_BYTES` | `107374182400` bytes (100 GiB), minimum 1 |
| `PALIMPSEST_HUB_MAX_BUNDLE_EXPANDED_BYTES` | `107374182400` bytes (100 GiB), minimum 1; total expansion allowed for one imported bundle |
| `PALIMPSEST_HUB_MAX_BLOCKING_OPERATIONS` | `2`, valid 1–16; process-wide concurrent hash/copy/parse workers |
| `PALIMPSEST_HUB_BUILD_WORKER_KEY` | Empty: build admission returns 503 `isolated build worker is not configured`. For remote builds, `SecretStr` of 32–4096 printable non-space ASCII characters; provision privately and match the worker keyfile. |
| `PALIMPSEST_HUB_BUILD_TIMEOUT_SECONDS` | `3600`, valid 60–3600; fixes the claim deadline at `started_at + timeout`; expiry is terminal `worker_interrupted`, never replay. |
| `PALIMPSEST_HUB_PACKAGE_NAMESPACE_BINDINGS` | `{}`; JSON object mapping canonical namespace aliases to exact immutable Keystone project IDs, for example `'{"openstack-afterglow":"<project-uuid>"}'`. One configured alias per project; an existing SQL namespace is never rebound. |
| `PALIMPSEST_HUB_PACKAGE_PUBLIC_ORIGIN` | Empty. Trusted HTTPS origin in canonical lower-case host/port form, with no credentials, path, query or fragment; other forms are rejected at startup. It is never inferred from the request Host. If empty, no package authority is advertised, and every native push fails closed with `503 HUB_UNAVAILABLE` before its tag is published. |
| `PALIMPSEST_HUB_EXPORT_DELEGATED_ROLES` | `'["member"]'`; JSON array of the least global roles a deferred Glance export Trust delegates. Glance's default `download_image` policy requires `member`, even for images the project owns. The requester must currently hold each listed role in the target project. `admin`, `manager` and `service` are rejected at startup. |
| `PALIMPSEST_HUB_EXPORT_DELEGATION_TTL_SECONDS` | `21600`, valid 900–86400; finite Trust lifetime. A queued export not started within it fails `delegation_expired`. |
| `OS_READER_USER_DOMAIN_NAME` | `Default`; domain of the separate validation identity |
| `OS_USER_DOMAIN_NAME` / `OS_PROJECT_DOMAIN_NAME` | `Default` |
| `OS_REGION_NAME` | `RegionOne` |
| `OS_INTERFACE` | `internal` |
| `SSL_VERIFY` | `true` |

`DATABASE_READ_TIMEOUT` limits each asyncmy wait for incoming protocol data,
including handshake/pre-ping/query responses; it is not an overall SQL transaction
deadline and does not bound a peer that continually supplies partial data. It
does not introduce retries. A response timeout after a mutation or COMMIT leaves
an uncertain outcome: preserve the original durable claim/publication receipt
and use its existing authoritative settlement, never assume rollback or replay.
The separate HTTPS-only build worker does not consume this database setting.

### Remote build worker settings

`BuildWorkerSettings` is a separate environment contract; it does not consume
`DATABASE_URL`, `REDIS_URL`, `PALIMPSEST_HUB_LOCAL_PATH` or `OS_*` secrets.

| Environment setting | Default / constraint |
| --- | --- |
| `PALIMPSEST_HUB_URL` | Required verified HTTPS origin; no credentials, non-root path, query or fragment |
| `PALIMPSEST_HUB_BUILD_WORKER_KEY_FILE` | Required absolute owner-only 0600 regular file; the key is never read from an environment value |
| `PALIMPSEST_HUB_BUILD_WORK_PATH` | Required absolute private local scratch; persist journals and cleanup state across restart |
| `PALIMPSEST_HUB_BUILD_TIMEOUT_SECONDS` | `3600`, valid 60–3600 |
| `PALIMPSEST_HUB_MAX_BLOB_BYTES` | `107374182400` (100 GiB), minimum 1; align with API limits and actual scratch capacity |

### Original token and package-owner prerequisites

The `OS_USERNAME`/`OS_PASSWORD` service identity remains separate from the read-only validator. The validator authenticates itself with `system_scope="all"` and validates the **presented subject token** through Keystone's token-read API; it never exchanges that token for a new default-project token and never falls back to service/admin credentials. `X-Project-Id`, if supplied, is only an exact assertion against the original scope, not a re-scope request. Downstream caller-scoped OpenStack requests reuse the validated original token and catalog. A missing reader identity/policy response fails closed (503), invalid/expired tokens fail 401, and scope assertions fail 403.

Operator-owned Keystone policy must let this reader call token validation (`tokens.validate`), `users.get`, `projects.get`, native domain/group reads and complete role-assignment collections. Every native package-key use freshly checks enabled owner/project, current authority and declared key ceilings. Ordinary member keys retain protected-owner and elevated `admin`/`manager`/`service` exclusions; independently issued human-admin keys retain explicit issuer provenance and fresh system-authority checks. A later owner promotion never upgrades an ordinary key. Missing/incomplete metadata fails closed; package keys cannot gain system management, key administration or OpenStack execution authority.

The separate human system-administrator gate resolves complete raw exact-`system:all` user and current-group grants through the trusted unique-global role-ID implication graph; project-oriented effective expansion may omit these grants and is not evidence of their absence. Qualify raw user/group reads and immediate revocation with the dedicated reader. Tenant administrative/service grades never substitute for native system authority. Original `X-Project-Id` asserts caller scope; separate `X-Target-Project-Id` selects a verified logical management target without exchanging/re-scoping the original token. See [current credential contracts](project-package-registry.md#3-identity-and-authorization).

**Historical legacy-publication policy (2026-10-14, before 0.3.10):** that
candidate treated a system administrator as an ordinary project publisher,
requiring current original-project membership and publication authority from
nonadministrative assignment roots. Preserve its dated qualification records;
do not apply that older rule as the current human-system management contract.
Current 0.3.10 additionally admits independently revalidated human system
administrators for policy-authorized target-bound publication without fabricated
target membership or service leaves. Original requester audit, native domain
enablement, delegated/service-identity vetoes and bounded key ceilings remain;
deferred Glance Trust-export admission retains its separate stricter boundary.

Keystone user/project IDs are exact bounded ASCII identifiers (up to 64 characters), including federated 64-hex user IDs. Do not parse them as UUIDs, case-fold or strip them. UUID formatting applies only to Hub-generated key/upload IDs. Protected sets and namespace binding values must preserve exact identity bytes. For a conventional project UUID, the conditional operator configuration is:

```text
PALIMPSEST_HUB_PACKAGE_FORBIDDEN_PROJECT_IDS='["<protected-project-id>"]'
PALIMPSEST_HUB_PACKAGE_FORBIDDEN_USER_IDS='["<protected-user-id>"]'
PALIMPSEST_HUB_PACKAGE_NAMESPACE_BINDINGS='{"openstack-afterglow":"<project-uuid>"}'
PALIMPSEST_HUB_PACKAGE_PUBLIC_ORIGIN=https://cloud.dmslab.re.kr
```

Replace each `<...>` marker with the operator-observed exact ID. A marker is not a display name, credential or authorization to change production. An ordinary member then registers the namespace for that original project. Without an alias, canonical lower-case 32-hex projects use `p-<project-id>`; other valid opaque IDs use `p-h-<56 hex characters of SHA-256(project-id)>`. Reserved deterministic names cannot bind another project. The SQL binding stays fixed even if configuration later changes. The native CLI reaches this Hub through the separate Afterglow key gateway at `https://cloud.dmslab.re.kr/api/v1/palimpsest/hub`; that gateway has not been deployed.

For federated owners, the reader must be able to see the owner's membership through a fresh effective role-assignment listing. That means a persistent direct user assignment or a currently persisted expiring group membership. [Keystone federation mapping combinations](https://docs.openstack.org/keystone/latest/admin/federation/mapping_combinations.html) distinguishes project mappings, which persist direct assignments, from group mappings, whose membership may be ephemeral. A user whose project role comes only from an ephemeral mapping group may not appear in that listing and then needs a persistent or direct assignment. A token-only ephemeral grant cannot stand in for fresh key-owner authority, because key use carries no caller Keystone token. If the cloud cannot expose that membership, key use fails closed. There is no automatic policy change or admin fallback, and no production mapping was changed here. This is an operator prerequisite, not a verified result.

These are operator prerequisites for a separately approved rollout, not a claim that Keystone policy, HTTPS origin, alias binding or package acceptance has been verified on the deployed service. The [2026-10-02 candidate checkpoint](development-handoff.md#candidate-integration--2026-10-02) records passed portable/Hub source gates and isolated HTTPS/BuildKit acceptance, with its identity/database limits.

The canonical Kolla role maps these inputs explicitly: `palimpsest_reader_user`, secret `palimpsest_reader_password` and `palimpsest_reader_user_domain_name` become `OS_READER_*`; `palimpsest_package_forbidden_project_ids` / `palimpsest_package_forbidden_user_ids` become the required protected JSON arrays; `palimpsest_package_namespace_bindings` becomes the alias map; and `palimpsest_package_public_origin` becomes the trusted HTTPS origin. The role precheck requires a separate reader username/password and both nonempty exact-ID protected sets. Secrets belong in approved private Kolla secret inputs, never globals/examples/logs. Rendering these settings is not evidence of Keystone policy or membership readiness.

Reader provisioning in the 0.3.4 source uses `openstack.cloud.identity_user` and `openstack.cloud.role_assignment` (`system: all`) **through `kolla_toolbox`**, following stock Kolla rather than requiring the operator host's collection. Inspect actual grants: `state: present` does not remove elevated assignments, and the validator rejects `admin`, `manager` or `service`. `update_password: always` resets the reader password whenever preconditions run. Observed 0.3.3 deployment failed to resolve `openstack.cloud.identity_user` on the operator host; its live cutover used the explicit bypass `-e palimpsest_run_preconditions=false`. That is a historical workaround, not the candidate provisioning contract or proof of current cloud policy.

### Deferred Glance export delegation

`POST /v1/image-exports` keeps the original-token admission: Glance visibility is checked with the presented token, and current `palimpsest-publish_editor` authority comes from the validator. When the request needs new Glance work (not byte reuse), the Hub creates a Keystone Trust **with the requester's validated token**: trustor = requester, trustee = this service identity, project = the token's project, `impersonation=true`, only `PALIMPSEST_HUB_EXPORT_DELEGATED_ROLES`, and a finite expiry. Keystone's response must match exactly or the Trust is deleted and the request fails. The Hub stores the Trust ID, scope, roles and expiry in `palimpsest_image_export_delegations`. It never stores the requester's token or password. Keystone refuses trust creation from application-credential, OAuth1 and EC2 tokens. Application credentials are exempt only when the insecure `allow_insecure_application_credential_trust_escalation` option is set. Exports requested with such tokens fail `delegation_denied` (403); requesters need a password, OIDC or federated token.

Before Glance I/O, the export worker re-reads the requester's current enabled user/project, publish authority and delegated role through the validator. It then authenticates `v3.Password(user_id=<trustee>, password, trust_id=...)` without project selectors. It verifies the trust token's ID, trustor, trustee, impersonated user, project, roles (no wider than current implications of the delegated roles) and expiry. Revoked, expired, swapped or legacy (pre-cutover, undelegated) exports fail terminally with `authorization_revoked`, `authorization_unavailable`, `delegation_revoked`, `delegation_expired`, `delegation_scope_mismatch` or `delegation_required`. There is no fallback to the service password scoped to the tenant. A new export always uses the current requester's own Trust. An earlier creator's delegation is retired, never reused.

Each Trust has a durable cleanup state. A terminal job, soft deletion, exhausted attempts or an abandoned admission queues its Trust for deletion. The worker deletes it through its own impersonating trust token, retries with backoff and records `deleted`. Admission deletes an unbound Trust immediately with the requester token when it can. Cleanup failure never reruns an export. A Trust that currently cannot issue tokens (trustor disabled or delegated role removed) cannot be deleted by the Hub. Keystone keeps that Trust, and restoring the role or user before `expires_at` makes it usable again. The finite expiry is therefore the real bound. After expiry the Hub records the Trust as `expired`; it stays in Keystone until the operator runs `keystone-manage trust_flush`. Keystone policy must allow trustors to create and delete their own trusts (`identity:create_trust`, `identity:delete_trust` defaults) and must allow trust-scoped authentication. These are operator prerequisites. No deployed Trust flow has been verified.

Cleanup completion is fenced by the claim's `cleanup_attempts` generation as
well as Trust ID/state: a stale worker returning after its five-minute lease
cannot overwrite the newer claimant's cleanup/backoff state. Only an update
that still owns the claim counts as finished. Export soft-delete compares
stored database DATETIME leases after UTC normalization; an unexpired claim
still returns busy rather than being deleted. New tests define these boundaries
and actual Base-model migration of all five delegation states, exact opaque
trustor IDs, roles JSON and expiry/retry metadata. No final regression or live
policy verification was run during source preparation.


The canonical Kolla role renders `palimpsest_export_delegated_roles` (default `["member"]`) and `palimpsest_export_delegation_ttl_seconds` (default `21600`) into the API environment. Its precheck rejects empty or administrative role lists and lifetimes outside 900–86400 seconds. The export worker container receives the same `OS_READER_*` and protected-ID inputs as the API. The service user's Kolla registration remains limited to its own `palimpsest_service_project_name`, where it still holds the existing `admin` grant. Do not add it to tenant projects. Existing manual tenant grants are no longer used by the Hub; removing them, or reducing the service-project grant, is a separate operator-approved change.

### Hub service-local authorization policy

Locally verified service-local policy: registered `oslo.policy` defaults replace required Keystone service-grade implication edges. Grade-only roles need no member/reader co-role; downstream provider credential prerequisites remain independent. Final Hub650, Kolla41, two real MariaDB/process cases and 370 actual multi-architecture Docker HTTP checks are recorded in [the scoped evidence checkpoint](testing.md#service-local-hub-policy-verification--2026-10-21). This is not production IAM/Kolla rollout qualification.

The Hub reads this nonsecret section from `PALIMPSEST_CONFIG` (default `/etc/palimpsest/palimpsest.conf`), not the secret-bearing `/app/palimpsest.conf`:

```toml
[oslo_policy]
policy_file = "/etc/palimpsest/policy.yaml"
policy_dirs = []
```

`OSLO_POLICY_POLICY_FILE` and JSON-array `OSLO_POLICY_POLICY_DIRS` override the TOML fields. Configuration/path changes need process restart. Native oslo.policy reload checks changed YAML files/directories on subsequent requests; use atomic replacement with a changed mtime and a directory mount (not a single bind-mounted inode). Remove loaded overrides by writing `{}`, not deleting the file. Missing default YAML uses registered defaults; explicitly configured missing non-default paths, invalid YAML/document types, native parser diagnostics and undefined/cyclic rule references return 503. Corrected files recover subsequent requests, never stale allow.

An existing file must map nonempty string rule names to string checks. Quote `"@"`, `"!"` and `""` for native allow/deny/empty-string semantics. Null/boolean/list values and empty/nonmapping documents return 503. The Enforcer reload hook validates document types and observes the real parser's error/exception reports in the loading thread, restoring its logger in `finally`; it never implements a grammar or evaluator. Native malformed checks under `not`/`or` therefore cannot turn parser failure into allow. Both main-file and policy.d recovery were exercised; unchanged files are not reread per action.

The packaged `palimpsest_hub/policy.yaml.sample` lists the defaults. From `hub/`, regenerate through the standard `palimpsest` namespace:

```sh
uv run --frozen oslopolicy-sample-generator --config-file etc/palimpsest-policy-generator.conf
```

The standard sample generator comments default rules. Before installing a newly generated comment-only sample, uncomment the desired string rules or use `{}` for defaults; empty YAML documents are deliberately invalid. The shipped active sample is regression-checked against every registered default.

Kolla mounts `{{ palimpsest_config_dir }}/policy` read-only at `/etc/palimpsest` into API and export worker. Its directory is 0755, nonsecret TOML/YAML 0644. The role initializes `{}` only when absent, preserving operator YAML; set `palimpsest_policy_source` only for deliberate managed replacement. Policy variables render the separate policy-only TOML. Put policy.d files beneath the mount or supply an explicit mount. Keep API/worker policy identical. Canonical images and nonroot policy reads were locally exercised; Kolla rollout remains unperformed.

`policy.conf.j2` renders `{{ palimpsest_config_dir }}/policy/palimpsest.conf` as root:root 0644. Both services explicitly set `PALIMPSEST_CONFIG=/etc/palimpsest/palimpsest.conf`. Original `/app/palimpsest.conf` remains root:root 0640 and is never read by the default bridge. All four canonical API/worker architecture images were exercised as UID:GID1000:1000 against actual root-owned files: secret reads failed with PermissionError while policy-only TOML/YAML/policy.d enforcement succeeded, without runtime user/group overrides or relaxed secrets. The full root Kolla contract selection passed41; see linked receipts.

Because standard oslo.policy role matching is case-insensitive, trusted global role binding uniqueness is checked by casefold before policy evaluation. Actual role names remain unchanged. Distinct `custom_policy_writer` and `Custom_Policy_Writer` IDs cannot borrow one another's YAML authority; the native real-engine HTTP regression covers the collision.

Authenticated `GET /v1/policy/capabilities` returns `service: "palimpsest"` and sorted `capabilities`/`actions` using original-token/current-owner decisions; empty arrays are legitimate. It never exchanges tokens or returns file paths/credentials. Project actions are `packages:inventory`, `packages:read`, `packages:write`, `cache:read`, `cache:write`, `keys:issue`, `keys:list`, `keys:revoke`. Separately human-system-verified actions are `packages:delete` and `system:manage` (existing build/delete/legacy administration). Leaf capability names are UI labels, not minted native roles. `@` cannot bypass project/key/package/user ownership, reduced-credential restrictions or fresh human system-only gates.

The following focused rerun commands cover actual loopback Uvicorn/Keystone SDK YAML reload, key ceilings and project/system constraints. The final complete Hub suite passed650 with two opt-in database skips; these subsets are not separately claimed as a final counted run:

```sh
# From hub/
uv run --frozen --extra dev pytest -q tests/test_auth.py -k 'native_policy or grade_and_exact_leaf or removed_service_edges'
uv run --frozen --extra dev pytest -q tests/test_auth.py tests/test_packages.py tests/test_hub_api.py tests/test_builds.py tests/test_image_exports.py tests/test_export_delegation.py
```

The standalone registered-app Docker driver uses zero service edges, grade-only identities and a read-only operator policy mount. Both architectures were built and exercised with task-owned `palimpsest-policy-019qotth-{api,worker}` tags, passing185 HTTP checks each. These are reproducible disposable-only commands; use fresh tags/evidence directories for another run:

```sh
for ARCH in arm64 amd64; do
  docker buildx build --platform "linux/$ARCH" -f docker/hub/Dockerfile --target palimpsest-hub-api --load -t "palimpsest-policy-api:$ARCH" .
  docker buildx build --platform "linux/$ARCH" -f docker/hub/Dockerfile --target palimpsest-hub-worker --load -t "palimpsest-policy-worker:$ARCH" .
  python3 scripts/smoke_package_capabilities.py run --api-image "palimpsest-policy-api:$ARCH" --worker-image "palimpsest-policy-worker:$ARCH" --platform "linux/$ARCH" --evidence-dir "build/service-local-policy-$ARCH"
done
```

Each evidence directory must be new or empty. The driver retains its owned SQL/CAS evidence volumes and never contacts production; its SQLite/lifespan-off adapter is not canonical MariaDB/deployment proof. Historical image/HTTP counts are not results for the new policy engine.

For a separately authorized disposable deployed Hub, using its trusted HTTPS origin and original grade-only human subject:

```sh
curl --fail-with-body --silent --show-error "$HUB/v1/policy/capabilities" -H "X-Auth-Token: $SUBJECT" -H "X-Project-Id: $PROJECT_ID"
curl --fail-with-body --silent --show-error "$HUB/v1/projects/current" -H "X-Auth-Token: $SUBJECT" -H "X-Project-Id: $PROJECT_ID"
```

In that disposable policy only, grant `packages:write` and `keys:issue` to `role:palimpsest_reader`, deny `packages:inventory` with `!`, and repeat capability/key/upload requests: allowed publication must not require an inventory leaf. Deny `packages:write` and retain `cache:write`; an already-issued key must immediately lose package publication without losing its independently allowed cache action. Request an undelegated download or another package/namespace, remove current membership, or call `/v1/builds` as project `admin`: each must deny. Replace every registered rule with `!`: a still-authenticated member sees empty capability/action arrays. Never edit production policy/IAM to run these probes without separate authorization.

### Deferred Glance export delegation

`POST /v1/image-exports` keeps the original-token admission: Glance visibility is checked with the presented token, and current `palimpsest-publish_editor` authority comes from the validator. When the request needs new Glance work (not byte reuse), the Hub creates a Keystone Trust **with the requester's validated token**: trustor = requester, trustee = this service identity, project = the token's project, `impersonation=true`, only `PALIMPSEST_HUB_EXPORT_DELEGATED_ROLES`, and a finite expiry. Keystone's response must match exactly or the Trust is deleted and the request fails. The Hub stores the Trust ID, scope, roles and expiry in `palimpsest_image_export_delegations`. It never stores the requester's token or password. Keystone refuses trust creation from application-credential, OAuth1 and EC2 tokens. Application credentials are exempt only when the insecure `allow_insecure_application_credential_trust_escalation` option is set. Exports requested with such tokens fail `delegation_denied` (403); requesters need a password, OIDC or federated token.

Before Glance I/O, the export worker re-reads the requester's current enabled user/project, publish authority and delegated role through the validator. It then authenticates `v3.Password(user_id=<trustee>, password, trust_id=...)` without project selectors. It verifies the trust token's ID, trustor, trustee, impersonated user, project, roles (no wider than current implications of the delegated roles) and expiry. Revoked, expired, swapped or legacy (pre-cutover, undelegated) exports fail terminally with `authorization_revoked`, `authorization_unavailable`, `delegation_revoked`, `delegation_expired`, `delegation_scope_mismatch` or `delegation_required`. There is no fallback to the service password scoped to the tenant. A new export always uses the current requester's own Trust. An earlier creator's delegation is retired, never reused.

Each Trust has a durable cleanup state. A terminal job, soft deletion, exhausted attempts or an abandoned admission queues its Trust for deletion. The worker deletes it through its own impersonating trust token, retries with backoff and records `deleted`. Admission deletes an unbound Trust immediately with the requester token when it can. Cleanup failure never reruns an export. A Trust that currently cannot issue tokens (trustor disabled or delegated role removed) cannot be deleted by the Hub. Keystone keeps that Trust, and restoring the role or user before `expires_at` makes it usable again. The finite expiry is therefore the real bound. After expiry the Hub records the Trust as `expired`; it stays in Keystone until the operator runs `keystone-manage trust_flush`. Keystone policy must allow trustors to create and delete their own trusts (`identity:create_trust`, `identity:delete_trust` defaults) and must allow trust-scoped authentication. These are operator prerequisites. No deployed Trust flow has been verified.

The canonical Kolla role renders `palimpsest_export_delegated_roles` (default `["member"]`) and `palimpsest_export_delegation_ttl_seconds` (default `21600`) into the API environment. Its precheck rejects empty or administrative role lists and lifetimes outside 900–86400 seconds. The export worker container receives the same `OS_READER_*` and protected-ID inputs as the API. The service user's Kolla registration remains limited to its own `palimpsest_service_project_name`, where it still holds the existing `admin` grant. Do not add it to tenant projects. Existing manual tenant grants are no longer used by the Hub; removing them, or reducing the service-project grant, is a separate operator-approved change.

After injecting dependencies/secrets, stop new ingress and drain **every** old
API/export/build/upload/GC writer and its already-started blocking effects.
Capture SQL and CAS from the same no-writers interval and prove isolated restore.
Run reviewed `palimpsest-hub-bootstrap` offline: additive tables include
`palimpsest_worker_leases`; existing build tables gain nullable
`lease_expires_at`. No automatic owner backfill or legacy guest adoption occurs.
The build worker remains HTTPS-only in its dedicated VM, never on shared SQL/CAS.
Start only the new version; old recovery must not coexist with new live claims.
Data migration into an empty initialized destination copies durable rows and
references, not transient maintenance leases; blob copying remains separate.
Undelegated queued exports fail `delegation_required` and require requester
resubmission. Unknown/ambiguous build scratch is retained, never replayed.
Rollback requires all writers stopped, exact previous image/config pins and
paired SQL/CAS restore; do not roll older workers over new live ownership state.

The `/app` web console uses the existing project-scoped `/v1/layers` and
resumable `/v1/uploads` endpoints, plus `POST /v1/builds`, `GET /v1/builds`,
and `GET /v1/builds/{id}`. A build request supplies `name`, full
`Palimpsestfile` text as `recipe`, `base_digest`, and optional ordered
`layer_digests`. The `FROM`/`LAYER` instructions must exactly match those
digests. Only a Keystone **system administrator** with a project-scoped token
can queue or inspect builds. Input visibility is checked on enqueue and again
when claimed; successful output appears as a private SquashFS layer under
`GET /v1/layers/{digest}/blob`. Raw recipes, user identities and worker paths
are not returned by the public build API. Recipes execute directly inside the
dedicated VM with private network namespaces, not per-build guest VMs. Queue
capacity remains four active jobs per project and six creations per hour. No
failed/interrupted attempt is automatically retried. Cleanup failures halt
the worker with private state retained; the journal phases and exact process/
loop recovery procedure above supersede the old guest-cleanup contract.

Uploads cap at four active sessions per project. Before creating a session,
24-hour-idle sessions for that project are removed under per-session locks;
interrupted PATCH bytes beyond the last acknowledged offset are discarded on
resume. The browser keeps the token in tab memory only; use HTTPS except on
localhost. Chromium streams large downloads directly to a chosen file; other
browsers support downloads up to 64 MiB in this console and can use the Hub
API/client for larger artifacts. Package installation or portable HTTP
contracts alone do not qualify the 0.3.4 direct executor; native qualification
is pending until the parent records it.

Lock waits never occupy a worker thread: each attempt is one non-blocking
`flock`, and a failed attempt sleeps on the event loop before retrying. Waiters
therefore cannot starve the current owner's unlock, its next lock, or its
filesystem worker, but acquisition order is not FIFO — per-project session and
build caps are what bound contention. Blob garbage collection skips a locked
blob instead of waiting for it. Cancellation while waiting for an upload or
project lock cannot retain its eventual file descriptor. A digest-verified
upload keeps an independent staging file until its layer registration commits:
retry the same PUT after a crash before commit using the acknowledged offset.
Concurrent registration of identical bytes and build queue capacity use
store-backed digest/project locks, so API replicas must share the same
filesystem view.

`PALIMPSEST_HUB_MAX_BLOB_BYTES` is a **per-blob** ceiling, not a per-project
or whole-store disk quota. `PALIMPSEST_HUB_MAX_BUNDLE_EXPANDED_BYTES` bounds
one bundle import's total expanded bytes; a bundle also refuses more than 4096
members and any member larger than the per-blob ceiling, and it stages and
verifies every blob before publishing any of them.
`PALIMPSEST_HUB_MAX_BLOCKING_OPERATIONS` bounds how many requests may occupy
hashing, copying, parsing, or fsync threads at once; raising it trades event
loop responsiveness and disk throughput for upload concurrency. A disconnected
client does not abort an in-flight worker, so its file lock is held until that
worker finishes. The canonical Kolla role exposes the three settings as
`palimpsest_hub_max_blob_bytes`, `palimpsest_hub_max_bundle_expanded_bytes`,
and `palimpsest_hub_max_blocking_operations`. Completed build rows and result
blobs have no
automatic retention/garbage-collection policy here; provision filesystem
capacity and operator monitoring separately before exposing uploads/builds.

During finalization, an unregistered upload and the promoted blob may briefly
consume twice that artifact's size. Include this peak in free-space planning.

## Administrator-owned Linux deployment

For a single-operator service host, keep installed code separate from mutable
state. Create an administrator-owned environment and install the same reviewed
VCS ref without invoking pip through `sudo` from an untrusted current directory:

```sh
cd /
sudo python3.12 -I -m venv /opt/palimpsest
sudo /opt/palimpsest/bin/python -I -m pip install \
  "palimpsest-client[kvm] @ git+https://github.com/openstack-afterglow/palimpsest.git@${PALIMPSEST_REF}"
```

Verify that the environment is administrator-owned and not writable by the
service identity. Package installation creates no account or state directory.
After reviewing the deployment identity, invoke the packaged provisioner as a
separate explicit operation:

```sh
cd /
sudo /opt/palimpsest/bin/python -I -m palimpsest_local.linux_install
```

It creates the no-login `palimpsest` account and owner-only (`0700`)
`/var/lib/palimpsest` and `/var/log/palimpsest` directories. Matching objects
are accepted; conflicts and symlinks are rejected. It does not recursively
change ownership, migrate/remove data, add privileged groups, configure KVM or
Docker, or write sudoers policy.

Run management commands with inherited root overrides removed:

```sh
sudo -H -u palimpsest env -u PALIMPSEST_STATE_HOME -u PALIMPSEST_LOG_HOME \
  -u XDG_STATE_HOME -u XDG_CONFIG_HOME \
  /opt/palimpsest/bin/python -I -m palimpsest_local.cli store show
```

## Isolated Hub candidate access and data safety

This is the **historical 2026-09-27 0.2.4 candidate** operator handoff, not the
current 0.3.4 deployment recipe or a claim that 0.2.4 was published. Its old
KVM/shared-store configuration does not apply to the remote build plane.
The private local workdir for that historical candidate is
`/Users/pieroot/.local/share/palimpsest-candidate-024/`; consult its nonsecret
`candidate-resources.json`, `artifact-receipt.json`, `reboot-receipt.json` and
the [dated evidence](development-handoff.md#격리-hub-후보-운영-인계--2026-09-27-production-미승격).
Do not paste its `api.env`, `build-worker.env`, `compose.env`, Keystone input,
SSH private key, or token into logs, tickets or commands. Root-owned candidate
configuration on the VM is `/etc/palimpsest-candidate/` (directory 0700,
files 0600). Its service state is `/srv/h` on the **separate mounted ext4 data
volume**, not the boot disk. API, export worker and bootstrap containers run
as UID/GID 2001 and mount `/srv/h:/srv/h`; the host KVM build worker is also
UID 2001. `/opt/palimpsest/local/bin/python` and
`/opt/palimpsest/hub/bin/python` are root-controlled interpreters. The candidate
uses `palimpsest-client 0.2.4` and Hub `0.2.1`, not the Kolla default Hub tag
`0.2.0` and not the already-published root `0.2.3`.

Candidate SSH config and alias are held in the private workdir. From an
authorized operator workstation, **after verifying the host key and access**:

```sh
WORKDIR="$HOME/.local/share/palimpsest-candidate-024"
ssh -F "$WORKDIR/ssh_config" -N -L 127.0.0.1:18020:127.0.0.1:8020 palimpsest-candidate-024
```

Leave that foreground process running for local `http://127.0.0.1:18020/app`
and `/v1` access; Ctrl-C closes only the tunnel. API port 8020 and SQL port
3306 are bound to **VM loopback**, Redis is not published, and local HTTP here
is inside the SSH forwarding boundary. Do not turn these into public listeners
or place tokens in URLs, command arguments, saved browser profiles or shell
history. Supply the project-scoped user token via the approved private input
path (browser tab memory or `PALIMPSEST_TOKEN` process environment), and the
CLI endpoint via `PALIMPSEST_URL=http://127.0.0.1:18020`; source Keystone
keys stay in protected input/env, never argv. Browser download links that
contain short-lived bearer tokens must not be shared or logged.

On the VM, service names are `palimpsest-candidate.service` (systemd oneshot
running `docker compose --env-file compose.env -f compose.yml up -d --wait
palimpsest-api palimpsest-worker` in `/etc/palimpsest-candidate`) and
`palimpsest-build-worker.service` (separate native KVM worker). Check active
units, mounted `/srv/h`, Compose bootstrap exit 0, API/export worker state,
MariaDB health, and the worker singleton before acting; use `docker compose
--env-file compose.env -f compose.yml ps` from that config directory without
printing resolved environment. `/v1/health` returning `{"status":"ok"}` is
**liveness only**, not SQL, Redis, auth, CAS, CLI or guest-build readiness.
The private `verify_readiness.py` documents stronger SQL `SELECT 1`, Redis
`PING`, UID/mount, loopback, disk and environment checks; inspect its scope,
do not output `/proc/*/environ`. The installed oneshot unit has **no
`ExecStop`**: stopping it alone leaves Compose containers running. Do not run
`docker compose down -v`; named/bind-mounted data must survive restart and
rollback.

### Consistent backup and isolated restore (procedure only; not executed)

These steps require an approved maintenance window and storage/SQL credentials;
there is **no** backup/restore proof in the candidate receipts. Restrict the
backup destination and transfer, encrypt it, record hashes and timestamps,
and test restoration on a separately isolated host before production use.

1. Verify `/srv/h` is the expected mounted data volume, not an empty mountpoint
   on the boot disk. Record source image IDs, package versions, current unit and
   container state, schema version, volume UUID and free space. Confirm **zero
   queued/building/exporting jobs and idle workers** (including any other
   writer, upload or garbage collector); do not merely trust `/v1/health`.
   Reject/defer new requests at the loopback/tunnel ingress, stop the native
   `palimpsest-build-worker.service` and stop API/export containers via Compose
   `stop` (not `down -v`). `systemctl stop palimpsest-candidate.service` is not
   a container stop. Recheck no writers remain and fail closed if a job or
   cleanup is ambiguous; never delete its private job tree.

   On the candidate host, once idleworker/no-new-ingress is established, the
   stop sequence is `sudo systemctl stop palimpsest-build-worker.service`,
   then, from `/etc/palimpsest-candidate`, `sudo docker compose --env-file
   compose.env -f compose.yml stop palimpsest-api palimpsest-worker`.
   Check the actual process/container states after each command; the still
   enabled units can restart services on reboot, so protect the maintenance
   window against an unexpected host restart. Do not stop MariaDB until after
   the logical dump (or before a deliberate cold full-volume snapshot).
2. With SQL still running and **no application writers**, take a
   transaction-consistent logical dump of the candidate `palimpsest` schema
   using credentials supplied by the protected Compose/container environment,
   **not** a password argument or saved shell history. Keep SQL credentials
   off stdout/stderr. Stop Redis cleanly after the workers are idle so its AOF
   is durable; copy mounted `/srv/h` (CAS blobs, metadata, private builds/
   uploads, Redis AOF/state) preserving numeric owners, permissions, symlinks
   and sparse files as appropriate, but **exclude `/srv/h/mysql` from this
   logical-dump restore set**. The SQL dump and CAS copy must span the **same
   no-writers interval**: a standalone SQL dump or live filesystem copy cannot
   prove referential consistency. `/srv/h/mysql` is MariaDB's live datadir and
   must not be copied as though it were a consistent cold snapshot. For a
   separate full-volume block snapshot, stop MariaDB and Redis cleanly first;
   restore that snapshot as a unit, not mixed with an unrelated SQL dump.
   Record dump/archive hashes, source volume identity and time, then verify no
   writer raced the snapshot.
3. On an isolated, empty replacement volume, restore the copied tree (without
   the old MariaDB datadir), preserving recorded numeric owners and exact modes.
   Hub CAS/build/upload files retain UID/GID 2001; Redis and database storage
   retain their own service identities, not a blanket recursive chown to 2001.
   Initialize a **new** MariaDB datadir/schema and import the matching SQL dump with credentials
   kept off argv/logs, and leave API/export/build workers stopped until SQL
   import and mounted CAS checks complete. Bring Redis up with its recovered
   AOF, check SQL and Redis, referenced blob digests and upload/job states,
   then start Compose API/export and the build-worker unit. If restoring a
   separately captured, consistently stopped whole-volume snapshot instead,
   do **not** import the logical dump on top of its MariaDB datadir; choose one
   database restore method. Inspect per-unit state, authenticated reads, blob
   digests and a disposable write/cleanup before reopening ingress. A health
   response alone does not certify restore. Retain the untouched backup and
   original volume until approval to retire.

For restart on the existing host after a verified backup, start the database
first if stopped; use `systemctl restart palimpsest-candidate.service` to
reissue its Compose `up` (a plain `start` of an already-active oneshot may do
nothing), then start the native build-worker unit. On reboot, the systemd
units require `/srv/h`; verify the mount and actual application readiness
again. Before any image rollback, record the **current candidate local** API
and worker image IDs in `artifact-receipt.json` and separately fetch the
**actual previous production immutable image digests** from production
deployment records. Those production previous digests are not in candidate
receipts and must never be guessed from candidate IDs, mutable tags or the
Kolla default. Retain volumes and SQL/CAS backup; switch both API and export
worker together only with schema compatibility reviewed, then verify the
restored path. No `down -v`, broad prune, destructive `install_host.py` rerun,
or production-ready claim follows from this candidate procedure.

## Upgrade and uninstall

Upgrade Local by reinstalling a newly reviewed SHA in the same environment:

```sh
PALIMPSEST_REF="NEW_FULL_40_CHARACTER_COMMIT_SHA"
"$HOME/.venvs/palimpsest/bin/python" -m pip install --upgrade \
  "palimpsest-client @ git+https://github.com/openstack-afterglow/palimpsest.git@${PALIMPSEST_REF}"
"$HOME/.venvs/palimpsest/bin/palimpsest" --version
```

Use the `[kvm]` selector again if that environment requires libvirt. Upgrade Hub
with the same ref and `#subdirectory=hub` selector only after its database and
worker compatibility have been reviewed.

```sh
"$HOME/.venvs/palimpsest/bin/python" -m pip uninstall palimpsest-client
"$HOME/.venvs/palimpsest-hub/bin/python" -m pip uninstall palimpsest-hub
```

Upgrading or removing Python code does not remove or migrate state, logs, VM
definitions, libvirt volumes, Docker data, BuildKit cache, or Lima disks. The
Linux provisioner has no uninstall mode; account and directory removal is an
explicit administrator operation.

## Contributor-only package build

Repository contributors can build the sdist/wheel and run the isolated package
smoke from a trusted checkout:

```sh
uv run python scripts/build_package.py --out-dir dist/package-0.3.1
```

This maintainer workflow is not the user installation path. Its local package
checks do not claim publication, signing, native KVM success, or arbitrary OCI
image compatibility.

See [platform compatibility](compatibility.md), [Linux storage and
logging](linux-storage-logging.md), [registry intake](registry-intake.md),
[Docker/OCI registry profiles](registries.md), [BuildKit workflow](buildkit-block-workflow.md),
and the [command workflows](cli/workflows.md) for operational details.
