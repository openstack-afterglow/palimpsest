# Environment benchmarks and experiment controller

Owner policy (2026-10-29): use one retained4vCPU/8192MiB OpenStack calibration VM
with a dedicated500GiB Cinder disk. During active experiments, measure Internet
download/upload at30–60minute intervals (default60minutes), not per workload.
Both providers run from that same VM in one timestamped cohort; keep each result
and their equal-weight mean. Missing directions remain N/A. Historical data is
never retroactively qualified by a later or differently located sample.

## Commands

| Command | Execution boundary |
| --- | --- |
| `palimpsest benchmark local --config CONFIG --output NEW_DIRECTORY` | Real fio/curl/iperf3 on the machine running this command; never implicitly SSH |
| `palimpsest benchmark openstack --config CONFIG --output NEW_DIRECTORY` | Verify explicit project, original Nova job, guest address, flavor snapshot and attachments through the API; run the probes inside that guest using pinned SSH |
| `palimpsest benchmark workflow --config CONFIG --output NEW_DIRECTORY` | Mandatory environment qualification, explicit hooks, then all five distinct method commands and job/method-bound runtime proofs |
| `palimpsest benchmark report RESULT_JSON... --output NEW_DIRECTORY` | Successful measurements as tables and standalone HTML, failures in `issues.md` |
| `palimpsest benchmark init --root NEW_DIRECTORY` | Create a private queue with no resource requests |
| `palimpsest benchmark submit --root QUEUE --config CONFIG` | Atomically submit an operator-owned workflow or standalone environment request |
| `palimpsest benchmark worker --root QUEUE` | One lock-bound executor; process submitted requests and persist status/evidence |
| `palimpsest benchmark status --root QUEUE JOB_ID` | Inspect queued/running/finished state; no command replay |

Long benchmarks run on a resident controller such as `mac-mini` (SSH alias
`ne-o@172.31.0.50`, observed `neoui-Macmini.local`, UID501), with actual
performance probes inside separately authorized cloud guests. The owner's
latest2026-10-28 clarification permits ordinary development and explicitly
approved deployment-server administration from the notebook; it supersedes
the earlier all-operations-on-Mini restriction, not historical evidence.
Do not forward an SSH agent or depend on the notebook staying awake. Install a
reviewed package/runtime on the controller, not an editable development checkout.
Credentials stay in its private configuration, never in cloud-init, report HTML
or job summaries. Runtime tool dependencies are **fio** (not the Python Fiona CLI),
**iperf3**, **curl**, Python3.11+, and the optional `cloud` package dependencies for
OpenStack mode. Provision tools outside measured install/consumer windows.

For cloud-product acceptance, this external controller is an API client, not a
replacement service orchestrator. Build and consumer execution must pass through
the Kolla-deployed Palimpsest/Afterglow APIs and server-owned workers. Direct
OpenStack SDK/SSH research remains a separately labelled lane; neither it nor
access to controller/compute management endpoints is a product prerequisite.
The2026-10-28 owner correction withdraws the earlier proposed node-SSH checklist;
retain its diagnostic evidence, but do not execute it as the next acceptance step.

## Reproducible scenario scripts (working source)

Do not generate a new benchmark driver for each run. The tracked scenario engine
owns a fixed program, input digest, approval-wide resource ledger, original SDK
receipts, append-only events and reports. Workload changes require source review;
configuration selects bounded resources and pinned images, not arbitrary hooks.
The implementation is not part of published0.3.10.

| Fixed script under `scripts/benchmarks/` | Real success condition |
| --- | --- |
| `environment.py` | Create the4CPU/8GiB station and500GiB data disk once, retain/reuse them, measure eligible hourly cohorts and report both provider means without recreating a VM |
| `torch_cpu.py` | Explicit CPU PyTorch matrix multiplication with complete numerical verification |
| `torch_cuda.py` | Actual CUDA allocation/matmul, synchronized iterations, GPU inventory and numerical verification; CPU fallback refused |
| `cuda.py` | NVIDIA inventory, real `nvcc` compilation and synchronized vector-add kernel with every output checked; requires a pinned **devel** image |
| `tensorflow_cpu.py` | Explicit TensorFlow CPU matmul with numerical verification |
| `tensorflow_cuda.py` | Explicit GPU matmul **and convolution**, device placement, cuDNN and numerical checks; CPU fallback refused |
| `k3s.py` | One server plus separately created agent VMs running digest-pinned `rancher/k3s`; all expected nodes Ready, matching private addresses, real pinned-image probe Deployment/pod results |

The environment script owns a persistent station; the other scripts own bounded
workload runs which reference its recent checkpoint:

```sh
python scripts/benchmarks/environment.py plan --config station.json
python scripts/benchmarks/environment.py create --config station.json --output results/station
python scripts/benchmarks/environment.py sample --output results/station
python scripts/benchmarks/environment.py run --output results/station --duration-seconds 7200
python scripts/benchmarks/environment.py status --output results/station
python scripts/benchmarks/environment.py report --output results/station
python scripts/benchmarks/torch_cpu.py plan --config torch-cpu.json
python scripts/benchmarks/torch_cpu.py run --config torch-cpu.json --output results/torch-001
```

Installed equivalents are `palimpsest benchmark station
{plan,create,sample,run,status,report,cleanup}` and `palimpsest benchmark scenario
{list,plan,run,status,report,cleanup}` for workloads. Station `run` is a finite
resident sampling window; only due cohorts perform probes. Repeated `sample`
within the interval returns recorded state without HTTP or guest probes, even
after failure. Station VM/data persist when a window finishes; `cleanup` is an
explicit destructive owner action, not an automatic benchmark epilogue.
Workload planning allocates no resources; API lanes no longer create a separate
SDK calibration VM. Failed/lost mutations never authorize a create/delete replay.

Station input schema is `palimpsest.benchmark-station.v1`: original project/actor,
exact4vCPU/8192MiB flavor,20GiB OS boot, separate `data_disk` of500GiB `ceph_hdd`
at `/mnt/palimpsest-benchmark`, original SSH route/pins, one-time approval ledger,
and `measurement`. Its provider list contains exact `provider_id` values
`cloudflare` and `ovh`, each with explicit verified HTTPS down/up URLs, bounded
byte counts and one repetition by default. `interval_seconds` is1800–3600,
default3600; a persisted pre-request claim consumes the interval on failure too.
The two directional provider means are averaged with equal weights only when
both are valid. No missing provider is zeroed or silently replaced.

The same cohort measures regular-file O_DIRECT fio on the500GiB data filesystem
(default1GiB scratch dataset,20seconds/profile,5repetitions,depth16), not all500GiB
of payload and not a separate workload's boot disk. It also records private TCP
send/reverse receive. Raw bounded probe logs are hash-checked before reports;
volume UUID/size/attachments, boot ID, source SHA and timestamps bind the result.
Both endpoint inputs, actual byte counts and failures are retained. A provider
403 is a partial context, not grounds to block unrelated CPU computation or
recreate the calibration VM. Disk/host identity failure still prevents use.

If disk-only requalification is needed, use the same retained VM and data disk:

```sh
palimpsest benchmark station repair-access --output results/station
palimpsest benchmark station disk --output results/station --checkpoint-output results/station/disk-checkpoint-001
```

`repair-access` is an explicit narrow repair for an original root-owned0711
mount that safe descriptor traversal cannot read: verify original500GiB volume,
boot/serial/ext4 UUID/root metadata/private child, then change only that mount
root to0755. Metadata0600 and measurement child0700 are unchanged. It cannot
format, mount, reattach, recursively chmod/chown or alter unknown filesystems.
The disk command performs direct fio only—no curl, Internet/private TCP or VM
creation. A distinct output preserves prior attempts/protocol versions. Its
guest probe start/finish and later read-only recovery time remain separate.
An explicit `environment_context.disk_checkpoint_root` joins a recent successful
disk checkpoint only when station/project/boot/data UUID/filesystem/source match
the original hourly network reference. Both references retain their own times;
the original partial cohort and its failure are never rewritten as success.

All probe and lifecycle commands share `station-active-operation.lock` in the
canonical station directory. It is acquired before metadata/output-specific
receipt locks and held through guest execution and evidence recovery. Selecting
a different disk checkpoint output does not permit concurrent fio; cleanup and
permission repair refuse while a probe owns the station. Busy sampling returns
recorded status with a bounded future wake-up, not a tight polling loop. CLI
mutators reject a busy station before private authentication, and backend APIs
hold the same fence across the complete operation. Read-only status is unaffected.


For unattended execution use the existing resident controller, not an AI polling
loop or a notebook-dependent SSH session:

```sh
palimpsest benchmark init --root scenario-queue
python scripts/benchmarks/torch_cpu.py submit --root scenario-queue --config torch-cpu.json
palimpsest benchmark worker --root scenario-queue
# In another terminal, or after reconnecting:
palimpsest benchmark status --root scenario-queue JOB_ID
palimpsest benchmark scenario status --output scenario-queue/results/JOB_ID
```

Install the reviewed package and its `cloud` extra on the resident controller.
Keep its SDK cloud credentials and SSH private key in that controller's private
configuration. Scripts use the original SDK-authenticated human/project; no
caller token is persisted in the queue, forwarded to a worker/guest, or printed.
The queue retains its existing single-executor/no-replay behavior and blocks
subsequent work after an interrupted/failed job pending explicit inspection.
No production worker, retired N5 controller or existing queue is upgraded or
replayed automatically by these scripts.

### Fresh private throughput peer and authentication

When no separately approved private iperf3 peer/bastion exists, the fixed helper
`scripts/benchmarks/peer.py` provides `create`, `status`, and `cleanup` for a
`palimpsest.benchmark-peer.v1` configuration. It requires its own explicit grant
and shared ledger: a20GiB/2-vCPU/8192-MiB peer, one new SG and one newly allocated
FIP on that peer only. The helper's bounded grant permits at most three cumulative
VMs/two live/80GiB, not an open-ended resource allowance. It cannot borrow an
existing SG/FIP or change shared network/IAM/production configuration.

```sh
python scripts/benchmarks/peer.py create --config peer.json --output results/peer-001
python scripts/benchmarks/peer.py status --output results/peer-001
# After every benchmark target has been ordinarily removed:
python scripts/benchmarks/peer.py cleanup --output results/peer-001
```

Only peer placement accepts `floating_network_id`; ordinary benchmark placement
must omit it. Peer output `connection.json` supplies its actual private iperf3
address, SG IDs, SSH jump and console-pinned SSH config/known-host references.
Copy those noncredential values into target `environment.network.internal`,
`ssh.jump` and `ssh.config_file`, and set target `ssh.known_hosts` to the peer's
`pinned_known_hosts`. The transport applies one known-host policy to both jump
and target: an empty seed omits the peer pin and fails before measurement, even
if the jump config points to a different known-host file. Never disable checking.
The target adds its own fresh console pin while excluding inherited keys for
that target alias. Port5201 is private to the new SG, not public. SSH ingress is limited to execution-host addresses
172.31.0.50/32 and172.30.0.50/32; egress permits same-group TCP22/5201 plus
HTTP/HTTPS/DNS/NTP. An existing qualified Docker engine is preserved. The helper
stops only an unmodified package-owned `iperf3.service` on its fresh VM before
starting its own private listener; unknown listeners/overrides fail closed.

For an execution host without a stored cloud profile, fixed `run`/`cleanup` and
peer `create`/`cleanup` also accept `--auth-stdin`. Feed a **private pipe**, not a
regular credential file. The bounded JSON accepts original-caller `v3token`
(token) or `v3password` (username/password and exactly one user-domain field),
plus `auth_url`, `project_id`, and `region_name`; the endpoint must be verified
HTTPS. YAML/OS-environment SDK configuration is disabled for this path, and
actual session TLS verification is checked before authenticating on the execution
host. Errors never print raw provider credential details. Queue execution uses
its controller's existing private SDK configuration and never persists these
inputs in a job. Original-receipt cleanup does not require local SSH files or a
still-admitted flavor; it still requires the original actor/project and verified
provider ownership. Unknown outcomes never become mutation retries.


### Execution lanes are explicit

`method` must be one of the following; no lane silently falls back to another:

| Method | Provisioning and execution boundary | Creates per repetition |
| --- | --- | --- |
| retained station | One4vCPU/8GiB VM with20GiB OS and500GiB data, reused across all sampling windows | 0 per measurement; 1 initial |
| `openstack-container`, ML | Create workload VM, reuse hourly station context without probes, pull pinned image outside computation, run container | 1 |
| `openstack-container`, k3s | Reuse station context; create server plus agents and prove real Kubernetes workload | 1 + agent count |
| `api-import` | Reuse station context; public Hub registry import and instance creation; SDK only observes provider identity | 1 |
| `api-build` | Reuse station context; public Dockerfile build and instance creation | 1 |
| `api-reuse` | Reuse station context; two sequential public consumers with identical ordered digests and separate writable disks | 2, sequential |
| `api-cold-warm` (unpublished candidate) | One external image job, then fresh sequential cold/warm consumers; warm selects the retained exact stack and never submits another image job | 2, sequential |
| `api-cluster` (unpublished candidate) | External fixed airgap-context build once; two simultaneously live API consumers form one exact k3s server/agent pair; later repetitions reuse the same completed job | 2, simultaneous |

API image/consumer timing and SDK reference timing remain different boundaries.
Deployed1afca684 has no GPU injection or privileged cluster profile. This
unpublished candidate adds explicit `api-cold-warm` GPU profiles and the separately
admitted fixed `api-cluster` path. Other unsupported methods fail before allocation,
without a Docker/SDK fallback disguised as API qualification.

An operator `palimpsest_instance_gpu_profiles` entry must pin the permitted project
IDs, exact qualified Glance image and content hash, flavor/PCI aliases, driver
version and actual GPU count. The base already supplies working NVIDIA drivers
and vendor CLI/library1.18+; no workload guest installs/resets drivers or builds
layers. API bootstrap uses fixed vendor compute/utility injection in its private
namespace and preserves the image UID/GID. Default profiles are empty/disabled.
Private flavor eligibility uses the same original caller's explicit
`is_public=True` listing (Nova public-or-project grants), not administrative
whole-tenant ACL enumeration. SDK3.3 omission sends `is_public=None`/all flavors
and is not a project-access fence. Actual CPU/GPU reads with the explicit filter
passed without creating a VM. Absent/revoked flavors still fail before allocation;
no IAM change or admin fallback is used.

SDK reference guests separately require their existing Docker NVIDIA runtime.

For `api-cold-warm`, one repetition consumes two cumulative VM/boot reservations
but at most one live consumer. Cold uses one continuous controller monotonic
interval from external dependency/context/image preparation through verified GPU
output. Warm begins immediately before the second consumer request, reuses the
same ordered Hub digests and profile, and requires distinct instance/Nova/boot
identities. Acquisition/build, consumer-only and synchronized inner-compute clocks
are reported separately, never summed. Warm still includes current guest HTTPS
layer transfer; it is not guest-local-cache warm, Manila or zero-copy. Shared
OS/CDN cache state is unknown and is never flushed. A failed pair yields no
successful comparison rows.

The fixed TensorFlow recipe is a derived runtime, not untouched original-image
success: pinned Torch2.8 CUDA12.6/cuDNN9 base plus TensorFlow2.21 and17 supplemental
hash-pinned wheels. The external resident client downloads the18 fixed wheels
under absolute time/byte/hash fences and uploads a bounded offline context. The
server builds with read-only BuildKit bind mounts and pip `--no-index --no-deps
--require-hashes`; wheel archives are not intentionally copied into runtime layers.
Worker RUN networking remains `none`. Cold includes this dependency preparation;
warm performs none of it. The streamed client default remains16MiB; larger
contexts require explicit `hub.max_context_bytes` and matching approval/server
limits, capped1GiB. The pre-send guard runs after hashing, immediately before POST.

The first3060/32GiB prerequisite failed Nova scheduling before boot; its ordinary
cleanup and spent budget remain historical. Under a new bounded approval, an
Ada8vCPU/16GiB prerequisite booted with driver535.309.01/capability8.9 but lacked
`nvidia-container-cli`. A separately approved same-image rebuild failed before
qualification; its VM later disappeared without an assistant DELETE, cause/actor
unknown, and its exact retained boot was ordinarily removed. The last approved
fresh base-builder installed signed/hash-verified Toolkit1.20.1 without changing
driver/kernel. Private image17085575 was captured, and that exact builder/boot
were ordinarily removed with absence proof; three prerequisite slots/240GiB stay
spent. CUDA12.6 devel supplies nvcc, not guest-host injection tooling. Actual API
GPU injection and numerical timings remain unqualified until those paths run.

For older/newer GPUs, CUDA compilation selects a validated actual device0 compute
capability and explicitly emits its `sm_xy` code; the program and result validator
require the capability written by the executed kernel's `__CUDA_ARCH__` to match
the selected GPU; a host compiler define is not device evidence. One trailing
float in the existing output buffer carries that observation, outside the checked
numerical elements/operation count. This avoids default-target PTX JIT against an
older driver. Real sm61/sm89 compilation is distinct from GPU execution proof.

### Fixed API k3s qualification

`cluster_profile` is disabled by default and separate from GPU privilege. It binds
original projects, exact content-qualified Linux guest base and permitted CPU
flavors. `cluster_server_id` connects only the same original user's live,
unexpired, identically qualified server to its one immutable agent, with the exact
ordered worker-built layer/runtime provenance. Both admission and effect fences
revalidate source eligibility; no caller-controlled command/readiness argv is
accepted for this fixed mode.

The image contains pinned k3s1.37.1, its exact hash/size-checked offline airgap
archive, static BusyBox and the exact sandbox-image digest. The guest mounts and
executes it; it does not acquire/build layers, download registry images or install
Palimpsest. Whole-VM root privilege, writable cgroups, native containerd snapshotter
and original-network server/agent communication are explicit exceptions, never a
relaxation of ordinary CPU/GPU chroot policy. A private expiry-bound capability
provides a domain-separated HMAC join token; no plaintext token is persisted in
SQL, public projection, argv, environment or logs. New dedicated SG ingress is
self-group only; no external ingress or FIP is required.

One cold repetition includes external airgap acquisition/context upload/image
build and a fresh two-node pair. A prepared repetition reuses the exact completed
job and ordered layers, creates two new writable guests, and performs no image
job/acquisition. Shared-ledger reservation atomically holds both live slots;
`cluster.timeout_seconds` separately bounds each pair's deployment/proof interval.
Hub callback readiness and Nova ACTIVE are independently observed before provider
identity checks. Cleanup deletes agent before server through API; ambiguous
effects are observed, not replayed, and cumulative consumption is never refunded.

The fixed proof checks the exact two Ready node identities/IPs/versions, one
digest-pinned numerical pod per node, successful4950 output and zero unexpected
restarts. It does **not** claim the older campaign's DNS/HTTP/PVC/service-restart
or VM-reboot gates. Old conductor RPC MessagingTimeout and Cinder attachment502
failed before k3s execution and remain provider evidence, not framework failures.

This study asks whether the intended build→layer→consumer→workload→cleanup path
works and whether measured boundaries are explainable—not whether a faster number
can be produced. Keep acquisition bytes/time, guest transfer, boot, cluster/GPU
proof and inner computation distinct; report exact numerical invariants, GPU/CPU/
RAM/source identities, clock agreement, failures and unavailable stages. One pair
does not establish dispersion or p90, and different GPU/memory combinations must
not be pooled as controlled hardware comparisons.
Reported `operations` are source-defined mathematical work, not hardware
instruction counters. TensorFlow's inner interval includes device-to-host
completion and convolution; its rate is not directly interchangeable with
PyTorch matmul or CUDA vector-add throughput.


### Saved configuration and immutable inputs

Schema: `palimpsest.benchmark-scenario.v1`. These fields are the reusable run input,
not values to invent at execution time:

| Field | Contract |
| --- | --- |
| `job_id` | Unique lower-case identifier, 1–32 characters; never reuse a consumed run ID |
| `scenario`, `method` | Fixed profile and supported explicit lane above |
| `cloud`, `project_id`, `project_name`, `project_domain_name` | SDK cloud alias and exact project realm; original authenticated user must match approval |
| `placement` | Exact `image_id`, `flavor_id`, `network_id`, unique `security_group_ids`, `boot_gib`, `writable_gib`; optional boot-volume type remains explicit |
| `ssh` | Existing private `identity_file`, corresponding `public_key_file`, pinned bastion `known_hosts`, `user`; optional `config_file`/`jump`; `direct_private=true` only for an explicitly routed private network. No FIP is automatically allocated or borrowed |
| `images` | Required profile keys use `repository[:tag]@sha256:DIGEST`; mutable tag-only inputs refused. k3s also requires `k3s-probe`. Station calibration is separate and needs no OCI image |
| `environment_context` | Absolute controller `measurement_root`, exact `station_id`, `project_id`, `data_volume_id`, and `max_age_seconds`≤3600. Read-only cached checkpoint; no per-experiment Internet probes and no attribution to workload-local hardware |
| `resources` | Per-run `max_creates`, `max_live`, cumulative `max_boot_gib`, sufficient for the entire planned scenario |
| `approval` | Exact `id`, `project_id`, `user_id`, `allowed_scenarios`, `allowed_flavor_ids`, per-VM `max_vcpus`/`max_ram_mib`, cumulative limits, timezone-aware `expires_at`, absolute private shared `ledger_root`; GPU/cluster additionally need `allow_gpu=true`/`allow_cluster=true` |
| `hub` | API lanes: verified HTTPS `url`, optional additional `ca_file`; original token comes from the SDK session, not configuration |
| `gpu_profile` | `api-cold-warm` GPU-only explicit operator profile ID; no caller-controlled library paths or device bypass |
| `cluster_profile`, `k3s_version` | `api-cluster`: exact operator privilege profile and fixed `v1.37.1+k3s1`; pinned k3s and sandbox references, one server/one agent, two proof replicas |
| `workload` | Bounded `iterations`, `matrix_size`, `timeout_seconds`; fixed actual numerical program, not caller shell |
| `cluster` | `servers=1`, `agents`, bounded deployment/probe timeout and probe replicas; network/SG must already permit required node traffic |
| `repetitions`, `deadline_seconds`, `timeouts` | Explicit finite repetition count, global bound and optional bounded ACTIVE/readiness/delete/guest windows |

The approval object is an operator declaration, **not** cloud authorization or
permission to exceed an approval granted outside the script. Its immutable shared
ledger is the same across runs under that grant. Reserve before allocation;
uncertain outcomes retain reservations. Proven deletion releases concurrent-live
capacity only: it never refunds consumed creations or cumulative boot capacity.
Use the original ledger, never a fresh path to manufacture replacement allowance.

New Debian/Ubuntu guests install Python/fio/curl/iperf3/mount tools during setup,
outside measured workload windows. Existing Docker engines/configuration are
preserved; CPU/reference setup installs `docker.io` only when Docker is absent.
GPU images must already provide their qualified Docker/NVIDIA runtime: missing
GPU Docker fails without replacing an engine or installing/resetting a driver.
Disk probes write only an exclusive regular scratch file on the dedicated data
filesystem, with no host-global cache flush. Station creation formats only its
newly created, metadata/serial/500GiB-size-matched volume once, with a predetermined
filesystem UUID and boot-device exclusion; existing unknown data is never formatted.
SDK SSH hosts are pinned from the exact newly created VM's serial-console identity,
not accepted with `StrictHostKeyChecking=no`. k3s joining uses private stdin and
root-owned token files; token contents/kubeconfig are never report/log material.

### Durable evidence and interpretation

Each new output directory records `config.json`, `runner-source.json` (actual
source hashes/Python/controller), `result.json`, ordered `events.jsonl`, original
SDK/API intents/acceptances/provider/flavor/volume receipts, bounded command
stdout/stderr and remote evidence references. Configuration uses credential
**references**, never literal passwords, tokens or signed URLs. Status polling,
state transitions, setup/acquisition, runtime, failures and ordinary cleanup are
separate recorded stages. Source changes or clock disagreement invalidate timing.

Read `overview.html`/`report.md` for successful scenario boundaries and
`environment.html`/`environment.md` for disk/network tables; failures and held
resources remain in `issues.md`, not successful timing rows. Missing data is N/A,
not zero or a synthetic fallback. CUDA/k3s definitions and unit tests are not live
GPU or cluster qualification; only a completed real run may claim those results.

`scripts/smoke_hub_execution.py` separately provides a **functional**, original-human
HTTP/SDK acceptance sequence: layer upload/query/conditional update/exact download,
real registry acquisition, safe-symlink multistage Dockerfile build, two sequential
consumer VMs, stop/delete, same-digest reuse and SDK absence checks. It publishes no
performance result and consumes its own exact remaining approval envelope; it is
not a substitute for the recent retained-station context required by benchmarks.

**Actual qualification (2026-10-29, unpublished):** the installed private CLI
created one real4vCPU/8GiB station and500GiB data disk, completed both providers'
down/up from that same VM, recorded arithmetic means, and returned the exact same
attempt on another within-hour sample without repeating probes. A finite resident
worker likewise reused the cohort and stopped while retaining the station.
Original first-cohort disk failure (root0711 descriptor access) remains preserved;
guarded access repair and a separate real disk-only checkpoint succeeded on the
same boot/data/filesystem with64 full hash-verified logs. Probe completion and
later recovery timestamps are separate; the network cohort was not rewritten.

Installed `benchmark scenario run` then created a real Torch CPU workload VM,
reused the hourly network and separate disk references without new speed tests,
pulled the exact pinned image and checked all65,536elements across five256×256
matmul iterations (PyTorch2.9.0+cu128, actual CPU, zero errors). Overall scenario
elapsed1034.474s includes VM/setup/image acquisition/transport/cleanup; inner
computation0.001534s and guest-command transport50.698s are different boundaries,
not VM startup latency or a steady-state performance/SLA result. The referenced
station checkpoint was admitted at run start; its exact original timestamps are
retained, not presented as a fresh probe at every later workload phase.

Exact SDK GET404 verified the workload VM/boot and both obsolete failed targets
were ordinarily removed. Station/data and its owned SSH/iperf peer remain ACTIVE
for reuse. The4preexisting servers/35volumes remain present, borrowed FIP stays
unbound, SG quota stays20 and this operation made zero quotaPUTs. No GPU/cluster,
native OCI-root, production rollout, or new deployed public execution endpoint is
proved by this SDK-container run. The named deployed Palimpsest OpenAPI still
lacks the unpublished image/instance routes; do not relabel this reference as
those product APIs. Earlier disposable attempts and consumed grants remain dated
failure evidence and are superseded by the retained-station owner policy.



## Public API execution contract (working source, not deployed0.3.10)

The external benchmark controller calls the Hub's native `/v1` APIs; it does not
replace those operations with client Skopeo/BuildKit or SDK/SSH provisioning.
SDK authentication/read-only provider observations and the separately labelled
retained-station/reference lanes above remain explicit. The dedicated
Linux worker owns registry acquisition, canonical OCI-to-SquashFS conversion and
Dockerfile execution. Instance allocation uses the original authenticated
requester's project-scoped OpenStack connection inside the API. No requester
Keystone token is persisted in a job or forwarded to the worker or guest.

| Operation | Public HTTP contract | Completion evidence |
| --- | --- | --- |
| Upload layer bytes | `POST /v1/uploads`, offset-bound `PATCH /v1/uploads/{id}`, metadata `PUT /v1/uploads/{id}` | Computed SHA-256 and registered descriptor |
| Query/download | `GET /v1/layers`, `GET /v1/layers/{digest}`, `GET /v1/layers/{digest}/blob` | Exact project visibility and downloaded byte hash |
| Update metadata | `PATCH /v1/layers/{digest}` with `If-Match: "config_digest"` | New descriptor digest; immutable blob bytes/architecture/ancestry |
| Server registry import | `POST /v1/registry-imports` | `GET /v1/image-jobs/{id}` reaches `complete` with ordered output digests and source manifest/config identities |
| Upload Dockerfile context | `POST /v1/build-contexts`, raw `application/x-tar`, `X-Content-SHA256`, exact `Content-Length` | Verified project-bound context digest |
| Direct Dockerfile build | `POST /v1/dockerfile-builds` | Same image-job completion contract; no client build |
| Create consumer | `POST /v1/instances` | `GET /v1/instances/{id}` includes guest result, timestamp, output and provider state |
| Reuse previously connected layers | `POST /v1/instances` with `source_instance_id` | New consumer binds the previous immutable ordered stack and runtime provenance |
| Observe/stop/remove consumer | `GET /v1/instances`, `GET /v1/instances/{id}`, `POST /v1/instances/{id}/stop`, `DELETE /v1/instances/{id}` | Exact-owned provider observation and terminal lifecycle state |

### Requests and retained identity

Registry import accepts `request_id` (UUID), `name`, `reference`, and
`platform="linux/amd64"`. Docker Hub shorthand such as `alpine:3` normalizes to
`docker.io/library/alpine:3`. External registries must be explicitly admitted by
`palimpsest_registry_hosts`; credentials and arbitrary URL transports are refused.
Acquisition verifies actual manifest, config and layer descriptors. The result
pins the image that was acquired, not a promise that a mutable tag never changes.

Dockerfile build accepts `request_id`, `name`, inline `dockerfile`, optional
`context_digest`, `target`, and `build_args`. Context files, multistage `COPY` and
safe internal relative symlinks are supported. Absolute/escaping/cyclic symlinks,
symlink-ancestor writes, hardlinks, devices, FIFOs, sparse members and duplicate
paths are rejected. Defaults bound context transfer to16MiB, expansion to256MiB
and members to10000. Dockerfile text is at most1MiB. Build arguments and original
Dockerfile text are not returned by job inventory/status. BuildKit has a private
per-job daemon, native snapshotter and context; no host Docker socket, forwarded
credentials, arbitrary remote frontend, remote ADD/Git source or insecure
entitlement is admitted. `palimpsest_image_run_network` defaults to `none`;
operator-selected `default` enables isolated worker RUN networking when the
workload actually requires it. The dedicated worker VM remains the privileged
kernel boundary, not hostile-code or arbitrary-network isolation.

Persist each `request_id` before POST. Repeating the identical project/request
returns its existing job/instance; changing that request returns409. A lost
response does not authorize a new request ID or a second provider allocation.
Image jobs have one fixed execution lease, bounded staging and atomic multi-layer
publication. Expired/uncertain attempts do not requeue. HTTP202 proves admission,
not worker execution. Image worker claims, inputs, staged outputs and completion
use a separate authenticated HTTPS protocol and durable no-replay local journal.

### Instance input and actual runtime

Instance requests select `request_id`, `name`, `image_id`, `flavor_id`,
`network_id`, optional `security_group_ids`, `boot_gib`, `writable_gib`, optional
`command` and `readiness_argv`, plus one source:

- `image_job_id` selects an exact completed project registry/Dockerfile job and
  its ordered layers, OCI entrypoint/CMD, environment, working directory and UID.
- `layer_digests` selects a complete visible application-layer chain with exact
  uploaded cloud-image provenance, or an unambiguous completed OCI job stack.
- `source_instance_id` reuses the previous instance's sealed stack/runtime even
  after that instance was deleted. It cannot be combined with another source.

The Glance boot image must be active, explicitly x86_64 Linux raw/qcow2, and
already provide cloud-init, Python3, systemd, mount/loop utilities, e2fsprogs,
SquashFS/OverlayFS and HTTPS trust. Network/security groups must belong to the
original request project; no service/admin credential fallback provisions access.
Application layers additionally require the Glance image's `palimpsest_digest`
property to match their exact Hub cloud-image base digest. Unknown or incompatible
architecture, base provenance and runtime identity fail before allocation.

The guest receives a fixed-expiry capability for only its exact layer downloads
and bounded result callback, never a Keystone token. It checks SHA-256, mounts
read-only SquashFS lowers, and creates a guest-private disk-backed ext4 writable
filesystem on its boot volume. `writable_gib` defaults to8, is smaller than
`boot_gib`, and is capped by `palimpsest_instance_max_writable_gib` (default64).
Guest free-space verification reserves capacity before formatting its exclusive
regular backing file; it never formats a discovered block device. Writable data
does not share RAM-only tmpfs, another VM's upper/work or Hub source bytes.

OCI image execution is explicitly **`oci-chroot`**: the cloud-image guest keeps
its kernel/systemd and executes the verified image root in a private chroot.
This is not native OCI-root stage-1/PID1 boot or Docker daemon compatibility.
Application mode retains its exact cloud base as the bottom lower. Commands are
actual argv execution under the declared OCI UID/GID and working directory.
With `readiness_argv`, a successful probe can attest a still-running workload;
otherwise command exit0 is required. API readiness is timestamped point-in-time
guest evidence, not continuous health, provider `ACTIVE`, reboot/SLA or GPU proof.
Stdout/stderr are independently bounded, and capability-equivalent bytes are
redacted from reported output.

### Owner-selected reuse and benchmark meaning

The2026-10-28 owner selected **same Hub digest reuse**, not shared-Cinder original
reattachment. A new VM downloads the same retained bytes, without rebuilding or
reacquiring the registry image, and obtains independent disk-backed writable
state. Do not label this a Cinder warm-cache/zero-copy/multiattach measurement or
mix its timing with the earlier Cinder benchmark. Global layer rows identify
physical bytes; ordered OCI occurrences and runtime metadata belong to the
completed image job. Repeated physical digests do not invent global parent links.

The later2026-10-29 owner architecture correction supersedes that storage target:
the external Palimpsest control plane owns acquisition/build/orchestration, layer
payloads belong on CephFS exposed through Manila, authoritative metadata belongs
in the Hub DB, and execution consumers mount read-only payloads. The HTTPS
download/`oci-chroot` route described here is the current source implementation,
not implementation or deployment evidence for that Manila target. Scenario
integration does not add guest-side Palimpsest control, manual provisioning, or
the older RW-Cinder-store/seal/RO-reattach prototype.

Names may be conditionally updated even on retained layers. Other descriptive
compatibility hints cannot retarget referenced descriptors. Publication changes
require fresh human system-management authority. Content replacement always means
uploading new bytes/new digest, never overwriting an old digest. Retained image
contexts, staging, outputs and instance sources have indexed SQL references;
source deletion/GC is refused even after consumer deletion so reuse remains valid.

The stdlib `HubClient` exposes `update_layer`, `upload_build_context`,
`import_registry_image`, `build_dockerfile`, `get_image_job`, `list_image_jobs`,
`create_instance`, `get_instance`, `list_instances`, `stop_instance` and
`delete_instance` without client-side SDK/SSH or automatic mutation retries.
Publication, Kolla rollout, existing worker quarantine disposition and real cloud
resource proofs remain separately scoped actions. The historical N5 private
SDK/SSH harness is not automatically migrated or reauthorized by these APIs.
New workload results reference their actual recent same-station time cohort and
preserve exact request-to-workload/result/cleanup evidence. Calibration source,
hardware/location and cache scope stay separate from workload-local claims.

## Ordinary VM create-to-SSH observation — 2026-10-29

One newly created OpenStack VM with **4 vCPU, 8 GiB RAM and a 500 GiB
`ceph_hdd` boot disk** reached the first observed, authenticated SSH probe
**144.149 seconds** after its Nova create intent; Nova ACTIVE was observed at
**23.727 seconds**. The existing Ubuntu 24.04 cloud image received only SSH
setup and a console host-key marker, with no added layer or application package.
The 500 GiB boot disk is not the retained station's separate measurement disk.
The console-pinned probe began after host-key publication and used the existing
Mini/peer path, so this n1 result is not the exact earliest possible SSH time,
variance, an SLA or a workload-readiness measurement. The new VM and boot disk
were ordinarily deleted and their absence verified. See the [handoff](development-handoff.md#ordinary-openstack-vm-request-to-ssh-measurement--2026-10-29) for the exact timings, source and evidence.

An independent **8 vCPU / 32 GiB** VM with the same image, network and 500 GiB
boot-disk type reached Nova ACTIVE in **24.180 seconds** and the first observed
authenticated SSH probe in **180.030 seconds**. Both results are n1 at different
times and cannot establish a causal effect of CPU/RAM size. The separately
created VM and its boot disk were deleted; their absence was verified. Full
measurements and the separate Notion entry are in the handoff above.


## Environment input

The local JSON schema is `palimpsest.benchmark-environment.v1`:

| Field | Meaning and bounds |
| --- | --- |
| `disk.directory` | Existing absolute, symlink-free directory on the intended filesystem; no raw block-device input |
| `disk.size_mib` | Scratch file size16–16384MiB, default1024; report this working-set size |
| `disk.runtime_seconds`, `repetitions`, `iodepth` | Per-profile window2–300s, actual repetitions1–10, queue depth1–128 |
| `network.external.download_url`, `upload_url` | Explicit credential-free HTTPS endpoints; TLS verified, redirects forbidden |
| `network.external.download_bytes`, `upload_bytes` | Exact expected transfer sizes1KiB–64MiB; oversized/chunked responses bounded |
| `network.external.repetitions` | Actual repetitions1–10, default3 |
| `network.internal.host`, `port` | Explicit private-address iperf3 peer, not loopback/link-local/multicast; TCP port1–65535 |
| `network.internal.duration_seconds`, `repetitions`, `parallel` | Bounded TCP send/reverse-receive tests; actual durations/counters checked |

Disk initialization precedes timed reads. Sequential read/write use1MiB blocks;
random read/write use4KiB. Linux uses libaio/O_DIRECT; Darwin posixaio/F_NOCACHE
is explicitly not the same physical-media isolation. Scratch cleanup removes
only the exact inode this invocation created. No formatting, cache flushing,
raw writes, existing-file overwrites or automatic installation is performed.

Results include bandwidth MiB/s, IOPS, completion-latency p95/p99, repetition
min/max/CV and interval gaps/stalls. Latency percentiles averaged over repetitions
are labelled as such, not falsely pooled. Device/controller caches are not
flushed; short repetitions do not prove long-term stability. Cinder/Ceph-backed
regular-file measurements describe the **guest-to-volume path**, not physical
Ceph OSD bandwidth, replication amplification or provider image-copy speed.

External HTTPS reports actual down/up payload rates and DNS/TCP/TLS/HTTP timing.
This is application-endpoint goodput, not line capacity. Internal TCP reports
send/receive bandwidth, interval variability and retransmits if supplied by that
iperf3 build. Missing counters are `not-measured`; UDP loss/jitter is not claimed.
No public or unrelated iperf server is started automatically.

Both disk and both network sections are required for overall success. Partial
measurements and failed raw outputs remain evidence but cannot qualify a workflow.
Controller wall/monotonic disagreement over15s invalidates qualification.

## OpenStack input and flavor evidence

Schema `palimpsest.benchmark-openstack.v1` requires `cloud`, `project_id`,
`server_id`, original `job_id`, private Nova `receipts` directory, `ssh` and nested `environment`.
The original receipt must match the server ID/name/spec marker; its `flavor_at_request`
is authoritative, not a caller-supplied flavor or a current catalog GET. Optional
`disk_volume_id` additionally binds the measured filesystem's device/serial to the
exact attached Cinder volume. The measured scratch inode device is compared with findmnt.
`ssh` contains `host`, `user`, `identity_file`, `known_hosts`, optional `jump` and
`config_file`. The address must belong to an exact-project port/floating-IP
binding on the original ACTIVE server; pinned host keys and the same guest boot
bind the returned measurement. Raw evidence remains in the guest's reported
private directory and the guest disk must be retained until collected.

`NovaController.create` now persists flavor ID/name/vCPU/RAM/disk/extra_specs
**before create**. ACTIVE inspection records Nova's embedded allocation flavor
separately. A current flavor GET is not retroactive proof of a deleted VM's
creation-time resources. Missing original snapshots stay unknown; do not replace
them with today's values. Consumer results preserve both snapshots.

`NovaController.create(..., boot_volume_type="ceph_hdd")` optionally selects an
explicit bounded volume-type name or UUID (Nova microversion2.67+). The selection
is included in the boot block-device request and original descriptor **before**
the durable intent/spec digest is written. When omitted, both fields stay absent
and the provider's existing default applies. Record the actual resulting Cinder
volume type separately; the requested type is not proof of physical media or a
particular Ceph OSD/backend. This does not change retention or cleanup authority.

Creating an internal peer is a resource action, not a read-only prerequisite.
This run separately approved two temporary CPU VMs for VM-to-VM measurement;
GPU experiments remain single-VM. Preserve existing read-only asset stores.
Use a new, dedicated scratch volume if writes are needed; explicit provisioning
and exact-owned cleanup remain outside the probe implementation.

## Five-method workflow input

Schema `palimpsest.benchmark-workflow.v1` requires a unique `job_id`, `mode`
(`local` or `openstack`), and exactly these five `cases`. Current cloud workflows
select the hourly `environment_context` described above. Explicit standalone
`environment` calibration remains available for separately approved diagnostic
work; it cannot be combined with `environment_context` and is not an instruction
to repeat Internet probes for each experiment:

| Method | Measurement scope |
| --- | --- |
| `direct-install` | Real installation and checks, with VM setup scope explicitly stated |
| `palimpsest-build` | Actual build/install/capture scope, not consumer boot labelled as a build |
| `palimpsest-import` | Actual original-image acquisition/materialization/registration; cold/warm distinct |
| `layer-allocate` | Allocate/activate/verify already built or loaded layers; preparation cost separately reported |
| `ansible` | Actual playbook installation and readiness, not a generated command or dry-run |

Each case supplies explicit `argv`, `proof_argv`, `timeout_seconds`,
`cache_state` and `preparation_scope`. Commands run only on the controller; use
explicit installed Palimpsest/Ansible/API/SSH hooks to target an approved guest.
A command's exit0 is insufficient: `proof_argv` must run real checks and emit
JSON `status=success`, matching `method` and `job_id`. OpenStack proof also needs
`flavor.requested.id` and actual allocation flavor. These are trusted operator
programs, not a remote arbitrary-command service for untrusted tenants.

`setup_hooks` may explicitly provision/install only qualification prerequisites
(maximum16 bounded hooks per group). For current station-context workflows they
must not create calibration VMs or repeat Internet tests. The controller reads
the saved cohort without probes, records its original reference and scope, and
allows partial provider context without zeroing missing means; unusable station
or disk identity blocks measured cases.

For explicit standalone calibration only, deferred `environment.config_file`
lets setup publish the exact newly owned target. `before_hooks`/`after_hooks`
are explicit argv+timeout objects; no automatic retry or cleanup inference.
Before-hooks run **after environment qualification**; standalone-only hooks can
release their disposable qualification peers. Failed qualification prevents
measured cases and before/after hooks; case/proof/clock failure stops advancement.
Optional `controller_hostname` rejects execution elsewhere.
All command evidence is private. `result.json`, `report.md`, `overview.html` and
`issues.md` are emitted; failures never become successful elapsed times.

**Historical opt-in only:** `scripts/benchmark_nova_gpu.py` retains its original
eight-case × five-repetition (40-attempt), serial GPU campaign contract. It still
requires an `environment_benchmark` OpenStack configuration and
`environment_release_argv`, then verifies that all project peers are gone. This
is superseded by the retained-station policy for current experiments: use the
fixed `torch_cuda.py`, `tensorflow_cuda.py` and `cuda.py` scenarios with hourly
`environment_context` instead. The old script is preserved for truthful source
and historical-plan provenance, not as a current default or permission to dispose
of the retained station/peer. Historical private plans are never upgraded or
replayed automatically; a historical campaign needs separate explicit authority.

## Always-on controller and recovery

Use a user-scoped macOS LaunchAgent to run the installed `benchmark worker` with
absolute runtime/config paths, private log directory, RunAtLoad, bounded restart
throttling, and caffeinate while active. Keep the private queue outside the
versioned runtime release. This run does not change OS power settings, login
permissions or install a public API/listening endpoint. The worker can wait24h
without resource allocations; new work starts only from an explicitly submitted
operator request. RunAtLoad/KeepAlive are not proof of operation after power loss,
logout, credential expiry or network outage.

The `mac-mini-katherine` controller uses the existing user LaunchAgent
`com.palimpsest.benchmark.controller` to supervise a **long-lived** private
`operations/controller-runtime.py` process. That supervisor creates or verifies
and adopts one detached tmux session, rather than repeatedly launching
`tmux new-session` under KeepAlive. With
`R=/Users/katherine/.local/share/palimpsest-benchmarks`, its dedicated socket is
`$R/tmux/controller.sock`, session `palimpsest-benchmark-controller`, and queue
root remains `$R/controller`. The pane launcher forwards HUP/TERM/INT as TERM
to the installed worker, waits for cleanup, and maintains caffeinate while the
worker lives. A private tmux config disables attach-time environment updates;
the worker receives an explicit allowlist containing the preserved PATH,
`OS_CLIENT_CONFIG_FILE=$R/config/clouds.yaml` and private log root, not an SSH
agent or ambient shell environment. Ambient `TMPDIR` is not inherited.

Read-only operator commands from the notebook (execution stays on the Mac mini):

```sh
ssh -t mac-mini-katherine '/opt/homebrew/bin/tmux -S /Users/katherine/.local/share/palimpsest-benchmarks/tmux/controller.sock attach-session -r -t palimpsest-benchmark-controller'
ssh mac-mini-katherine 'cat /Users/katherine/.local/share/palimpsest-benchmarks/controller/heartbeat.json /Users/katherine/.local/share/palimpsest-benchmarks/operations/supervisor.json /Users/katherine/.local/share/palimpsest-benchmarks/operations/pane.json'
```

Detach with **Ctrl-b d**; closing the SSH client does not stop the detached
session. The worker's output remains in the existing private controller logs,
not pane scrollback. Do not publish those raw logs or configuration files.
The separate `palimpsest-executor` session on `$R/tmux/executor.sock` retains
the completed read-only Codex review and scoped cutover job; it is not a second
queue executor and does not imply that an AI agent remains running.

Inspect the queue and current heartbeat before an operator stop/restart; never
interrupt an active or uncertain cloud job to perform maintenance. For an
approved **idle-only** service restart, use `launchctl bootout
gui/501/com.palimpsest.benchmark.controller`, wait for the pane worker and its
caffeinate helper to exit, then `launchctl bootstrap gui/501
~/Library/LaunchAgents/com.palimpsest.benchmark.controller.plist` on the Mac mini.
Do **not** use `kill-pane`, `kill-session`, `kill-server`, or SIGKILL as a worker
stop mechanism. The supervisor waits up to40s for TERM cleanup; the LaunchAgent
has ExitTimeOut60 and retains KeepAlive/RunAtLoad/ThrottleInterval30/umask077.
Heartbeat age over60s is an alert, not an automatic kill. An unexpected session,
held orphan-worker lock, or stale socket fails closed; `runtime-error.json` and
an increasing launchd runs count require operator inspection, not state deletion.
For a stale socket, first prove no server owns it (`lsof`), reconcile the exact
worker/session/lock, and only then perform separately reviewed recovery. The
original plist rollback copy is outside LaunchAgents at
`$R/proofs/tmux-controller-20261025/controller-original.plist`; do not bootstrap
it concurrently with the new supervisor. This is user-session persistence,
not a logout/reboot, credential-expiry or network-availability SLA.

Submit a complete workflow directly, or use a standalone envelope with schema
`palimpsest.benchmark-request.v1`, unique queue `job_id`, mode `local` or
`openstack`, and the complete probe input in `config`. The outer queue ID is
distinct from an inner OpenStack guest's original Nova `job_id`; changing a
measurement request never changes that ownership identity. Standalone jobs also
emit their report/HTML/issues and can be monitored without a notebook session.

Queue entries publish atomically. A started receipt is exclusive; worker lock
prevents concurrent executors. A started/interrupted/failed receipt makes
**all later jobs block** pending operator inspection, not just that one ID.
`heartbeat.json` reports waiting/blocked state. Never delete a state file to retry
an uncertain API create. Preserve the failed ID and evidence; reconcile the exact
owned resources and submit a new ID only after cleanup is known.

## Reporting and issues

### Half-hour operator progress

The Mac mini runs a separate detached tmux observer; it never submits requests,
replays experiments or calls OpenStack. Its dedicated socket is
`$R/tmux/monitoring.sock`, session `palimpsest-progress-monitor`, with
`$R/monitoring/collector.py` as the sole long-lived pane process. The separate
GUI user LaunchAgent `com.palimpsest.progress.monitor` runs
`$R/monitoring/supervisor.py` and starts/adopts the dedicated tmux observer via
`$R/monitoring/launch.py`. The collector also holds an exclusive `reporter.lock`;
a second collector exits75 rather than starting another reporting loop. The
controller and executor sockets/sessions remain separate and untouched. This is
GUI user-session supervision, not a logout/reboot, power, auth or network SLA.
Do not launch a replacement tmux server from an arbitrary SSH context: process
origin matters for access to the existing macOS Keychain login.
The launcher safely recovers only its exact owned stale socket after proving
no tmux server, no `lsof` owner and a free reporter lock; it can respawn only the
exact expected dead collector pane. The supervisor permits one such recovery
before requiring operator reconciliation, and failed-closed state holds rather
than creating a launchd restart storm. On orderly stop it waits for collector
exit and the free lock before removing only its own tmux session. Its180s
LaunchAgent exit timeout accommodates the bounded120s publisher invocation.
`lifecycle-final-proof.json` records the controlled load: one collector holding
the lock, unchanged collector/outbox/queue state, and preserved next due.

An immediate real observation is followed by periodic reports every1800 elapsed
seconds, scheduled with a monotonic clock. UTC/KST observation and next-due
timestamps are explicit; new terminal results are checked in the same loop
every30s when publication is not in flight (one adapter call is bounded120s).
`collector-state.json` preserves cadence and result deduplication across a
collector restart. Existing terminal results are baselined, not new experiments;
changed historical payloads raise a runtime count rather than silently replaying.
An idle ready queue is reported as idle/zero new work, never a progress percentage.
The periodic deadline advances only after report emission succeeds; a failed due
observation retries on the bounded30s poll instead of losing the scheduled slot.
The scoped timer correction/restart evidence is
`$R/monitoring/timer-correction-proof.json`; the first report ID and next-due
timestamp were preserved without touching the benchmark worker.

Private directories use0700 and files0600. Immutable allowlisted manifests live
in `monitoring/outbox/<report_id>.json`, companion text in `monitoring/reports/`,
the current observation in `latest.json`, and scheduling evidence in
`runtime.json`. Only fixed phases/reasons, counts, timestamps and bounded result
references cross the collector/publisher boundary; configuration, credentials,
raw logs, arbitrary command output and user-data do not. Publication uses only
the sibling `publish.py --manifest <absolute-path>` adapter after
`publisher.ready` is present, with an explicit minimal environment preserving
the Mac mini account's HOME, no credential argv, and suppressed adapter output.
The adapter's authenticated CLI login is separate from the benchmark worker.

`delivery/<report_id>.json` is separate mutable delivery state: exit0 means
adapter readback-confirmed delivery;20 is auth/config blocked;21 rejects an
unsafe manifest;22 is retryable/uncertain;23 requires manual reconciliation.
Pending reports remain durable during auth/network failures; blocked reports
are retried no more often than300s without altering their report IDs or
rerunning experiments. Uncertain remote writes must be reconciled by the adapter
before another append, not labelled sent by the collector.

The operator subsequently installed and logged in `~/.local/bin/ntn` on the
Mac mini. The adapter uses that existing CLI login directly; it does not extract
OAuth credentials, transfer tokens or require a new static-token file. The
initial SSH-origin monitoring context returned `KeychainAccessError`
(`notion-auth-proof.json`), but the same login worked in the existing genuine
GUI/launchd-origin context (`notion-gui-auth-proof.json`). A real GUI-context
publisher append/reconciliation/readback succeeded for the original first report
(`notion-gui-delivery-proof.json`). The operator chose to retain the existing
Keychain; only the monitor lifecycle was moved into a separate GUI LaunchAgent.
No unlock, access-control change, relogin or file-token migration was needed.
Preserve this origin during maintenance rather than silently altering credentials.

The initial live report `20261025T085018Z-000001` observed ready/idle with zero
approved unconsumed jobs and zero new results at17:50:18KST on2026-10-25, with
the next periodic report18:20:18KST (1800s later). The already-published historical
terminal result was baselined only. The scoped integration proof is
`$R/monitoring/smoke-proof.json`: duplicate collector exit75, launcher adoption,
one exact collector process, private modes, and unchanged pre-launch queue/state
JSON hashes. A later proof window additionally compares all regular
queue/state/result files by hash/size/mtime/inode and heartbeat phase;
`smoke-controller-before.json` and `smoke-controller-after.json` retain that
private inventory. No pre-launch result-file metadata baseline was collected,
so the broader comparison attests only that later proof window.
The scoped GUI migration initially encountered its own stale tmux socket after
the old server exited. Only that exact owned socket was removed after no-server,
no-`lsof`-owner and free-reporter-lock proofs; controller processes were untouched.
The18:20:18KST slot was therefore observed late, not backdated: real report
`20261025T092302Z-000002` records18:23:02KST,164.503s lateness and the original
scheduled due time. The next due remains anchored at18:50:18KST.
`gui-migration-proof.json` retains identity, immutable-first-report preservation,
deduplication preservation and matching queue/state hashes. Subsequent reports
carry actual observation time, scheduled due, lateness and the next anchored due.
The actual GUI monitoring context then automatically published this late report:
`autonomous-delivery-readback-proof.json` independently confirms native Notion
content, exactly one report toggle, matching receipt/payload hash, collector
delivery exit0 and the GUI process ancestry. Delivery was confirmed at
18:23:41KST; the later read-only verification ran inside the monitoring tmux
server, not an interactive SSH shell. The progress ledger is
<https://app.notion.com/p/404067c015d08161a99fea831b309c90>. This is observed
end-to-end automatic delivery, not a guarantee against future auth/network loss.

Read-only operator inspection:

```sh
ssh -t mac-mini-katherine '/opt/homebrew/bin/tmux -S /Users/katherine/.local/share/palimpsest-benchmarks/tmux/monitoring.sock attach-session -r -t palimpsest-progress-monitor'
ssh mac-mini-katherine 'cat /Users/katherine/.local/share/palimpsest-benchmarks/monitoring/latest.json /Users/katherine/.local/share/palimpsest-benchmarks/monitoring/runtime.json'
```

Detach with **Ctrl-b d**. Inspect the adapter's per-report receipt before claiming
Notion delivery; a local outbox, running timer or successful SSH exit alone is
not publication evidence. Never restart the benchmark worker to repair reporting.

The separately owned `k3s-comparison-002f8015c10d` campaign is not a legacy
controller queue entry. Its fixed private `progress.json` and `provenance.json`
are the only live external inputs; every path component is opened no-follow,
with owner/private-mode and size checks. Fixed phases, bounded counts, exact
version/hash fields and metadata-resolution status are projected into
`external_jobs`, independently of legacy queue/status counts. Unknown or unsafe
inputs report unavailable; zero legacy jobs never implies an inactive campaign.
Phase/count/provenance changes emit an additional `external-progress` manifest,
without advancing the existing monotonic1800s periodic deadline. Observation
timestamps alone do not trigger new reports. No workload is submitted by this path.

Only an executor-declared immutable terminal `publication.json` under that same
fixed root is eligible for result collection. Its bounded typed projection keeps
prepared/API/NodeReady/workload/end-to-end timings, cold/warm preparation cases,
functional proof flags and cleanup separate; absent values remain N/A. The first
terminal digest is retained. Changed or unreadable terminal output is alerted and
not replayed, without stopping legacy periodic observations. New campaign content
is appended under a dedicated management child page; failed outcomes link to the
existing issues structure. Existing reports/embeds are not rewritten. Publication
still requires the existing GUI/Keychain context, keyed no-replay journal and full
Notion readback before delivery is confirmed.

The live integration receipt `monitoring/k3s-integration-proof.json` records the
single idle-only collector reload and report `20261025T130945Z-000010`, delivered
with Notion readback at2026-10-25T13:10:03Z. The prior outbox, controller queue/state,
legacy result deduplication and13:20:18UTC periodic due were preserved. Only the
owned dead monitor pane was respawned; the same supervisor remained. Its internal
one-recovery allowance is not observable, so preservation is not established.
This observation had already advanced from preparation to reference
qualification (one campaign VM, zero completed clusters), not completed timings.
The dedicated page is
<https://app.notion.com/p/404067c015d081e7bbeccb34a9df6be7>. Provenance was reported
unresolved when its private-input checks failed, never substituted from publisher
source literals. This is one real delivery/status window, not a future-delivery
or controller-uptime guarantee.
Actual post-reload periodic report `20261025T132018Z-000014` fired at13:20:18UTC
with0.136s lateness, retained campaign fields separately from legacy queue zero,
and advanced next due to13:50:18UTC. Its standard receipt confirms Notion readback
at13:20:29.254737UTC. This is an exercised periodic slot, not future punctuality.

The user subsequently superseded the two-arm/eleven-cluster plan with exactly
five methods. Current `plan_revision=five-method-contract-pending` carries null
planned cluster/repetition counts, `primary_completed=0`, the fixed five-method
set, and `approval=current-existing-twoVM-qualification-only`. Historical
two-arm counts remain explicitly labelled historical, never current approval.
Method4 means a real root-Dockerfile source build followed by the canonical
package Dockerfile and layer connection, not published-binary capture. Requested
universal runtime semantics are read-only lower plus a separate consumer-writable
upper/workdir on boot/temp; this is not yet an implementation/qualification claim.

The parent-approved schema-only reload is recorded in
`monitoring/k3s-plan-schema-maintenance-proof.json`: exact supervisor PID/start
identity, independently armed15s SIGCONT watchdog, normal1.10s pause, same resumed
supervisor, single collector33846, preserved16:20:18UTC due and prior outbox/legacy
queue/state. Actual pending-plan report `20261025T161225Z-000042` was readback at
16:12:39.116235UTC. The additive plan/write-layer clarification preserves earlier
reports and does not classify raw read-only write failures as upstream image
incompatibility or assert a VM reboot from container/service restart evidence.

Settled reference/layer/D01 summaries use a separate fixed GUI-tmux `run-shell`
hook invoked by their execution owner after atomic immutable summary publication.
The hook uses the existing publisher lock/no-replay journals/readback and durable
`publication/stage-outbox`; blocked delivery never restarts install/create.
Its fixed operation receipt, not a collector report receipt, is
`monitoring/k3s-qualification-<stage>-publication-operation.json`. This bounded
hook needs neither an active chat agent nor another collector/publisher loop.

The subsequent real user choice is five methods×one fresh cluster each: n1 only,
variability/CV N/A, primary completed0 at selection. Machine null plan counts mean
final execution admission is pending, not that the user has not chosen n1.
Generic writable-runtime/source-build/artifact review and remote proof must
precede admission; no old n5 matrix or premature create follows from publication.
Separate immutable selection and cleanup-policy receipts were read back at
16:30:09UTC and16:30:18UTC respectively. Explicit owned-temporary-volume cleanup
permission requires durable verified evidence, clean quiescence, exact VM404,
available/detached fresh ownership and bounded deletion receipts, while needed
stores/artifacts/active upper-work and unrelated resources remain protected.
This is an operator-controlled experiment-volume exception, not an AssetCatalog
permanent-retention/pruning change; total creation limits remain11 VMs/13 new
volumes/412GiB and cleanup never authorizes extra resources.

Five-method **v2 progress and terminal reporting are now admitted and live**.
The parent approved the exact five-file source set in
`$R/monitoring/prepared-five-method-v2-metadata-r2-20261026/` after source-aligned
contract verification. The base caller contract `ed31e5a1…` remains immutable;
`source-alignment-addendum.json` binds the actual frozen executor result fields,
signed authorization `74ccc2f0…` and common inputs `63e5d793…`. All legacy v1
manifests and sequence47 payload wording remain supported unchanged. Primary
planned5 and settled0..5 count actual method success/failure outcomes; the earlier
untimed reference1 is outside that denominator. Each method has at most one
attempt. Final coverage lists all five, including not-attempted0/null, never pass.

The metadata-only adapter runs once inside the existing collector poll. It reads
only the five fixed authorized operation IDs and typed actual method/action/
execution results plus the owned ledger, using private no-follow/hash bindings.
It starts no workload, cloud request, extra service, credential access or reporter
loop. Actual signed admission and initial-pair disposal produce reporter-owned
admission/progress; missing or uncertain method evidence becomes a visible
projection HOLD, not a fabricated result. Settled summaries are immutable.

Actual settled results use five fixed private sanitized method-summary paths,
byte hashes and input/admission/source provenance bindings. They can feed the
existing collector's immutable outbox and native per-method tables automatically;
first method hashes and cumulative creation/deletion counters cannot regress or
be replayed as new outcomes. Timing scopes retain fresh cold/first-use/source-build,
package/API/NodeReady/workload/end-to-end endpoints and clock qualification;
old preparation timing is never substituted. n1 variation remains N/A. M4 success
requires actual paired source/package build and OCI/asset identities, not binary
capture; M3 requires its pinned Ansible commit and M5 its canonical source closure.

Future-matrix11 VM/13 volume/412GiB creation counters are cumulative and exclude
the initial pair/three campaign volumes; deleting a disk does not refund those
caps. Separate initial-boot and future deletion counts require per-volume receipt
hashes, while needed stores remain protected. Frozen actual wheel/generic-COW/
Docker-reference acceptance and real initial-pair release bind final admission.
Standalone connect timing is N/A when the executor has no independent stopwatch.
M4 first-use comes directly from actual
`source_build.observed_continuous_first_use_seconds`, explicitly not a sum of
phase durations; the adapter binds the same operation/child and stopped clocks,
checks the whole wall/monotonic interval, and labels original build-clock start
through workload readiness. Historical44.640s preparation is never substituted.

Actual cutover report `20261025T210859Z-000058` was completely read back in Notion
at21:09:24.632110UTC: primary attempted0/completed0, separate reference1, activeVM0,
newVM0. `monitoring/k3s-final-v2-cutover-proof.json` records one collector42812,
the same verified supervisor89720 resumed stateS after a0.090606s watchdog-guarded
pause, unchanged57 prior outbox/publication/delivery/controller records and the
original21:20:18UTC due. The executor's start witness is
`$R/k3s-comparison-002f8015c10d/reporter-admission-v2.json`; it is an actual
readback-confirmed reporting gate, not a claim a primary cluster has succeeded.

The initial submitted operation IDs were all rejected before execution by the
strict source-file mode0600 guard (actual0644); accepted methods, cloud attempts
and created resources stayed0. This was distribution rejection, not a Kubernetes
or image failure. A parent-approved exact amendment retained source bytes/caps/
logical n1 slots, corrected modes and introduced only fixed `-dist2` IDs. The
observer accepts the exact signed old74ccc→new5cfc chain and independently hashed
actual source-mode/queue-validator/zero-state evidence; pins are not disabled.
Only `k3s_five_method.py` and the metadata adapter changed. Old admission stays
immutable; the only additional admission filename is `admission-v2-dist2.json`.

`monitoring/k3s-dist2-observer-cutover-proof-r2.json` records one actual safe
reload after an imminent-slot precondition abort: same supervisor89720 stateS,
0.3994s pause, sole collector57950, unchanged history/old admission/controller/
publisher/progress source and22:20:18UTC due. Actual dist2-zero report000061 was
read back21:52:09.457859UTC; `reporter-admission-v2-dist2.json` is its start witness.

The first real M2 attempt then failed at agent create (HTTP400, shared RO source
volume reserved after server create accepted). Autonomous report000062 was read
back21:54:41.612828UTC: one attempted/settled failed method outcome, zero primary
successes, cleanup HOLD and unknown active count—not a completed cluster.
A later separate read-only reconciliation observed one active server/boot and no
agent VM/boot; its bounded issue projection was read back22:02:58.962104UTC at
<https://app.notion.com/p/404067c015d081e3b984f4868b030053>. Earlier uncertainty and
the immutable failed result remain intact. No workload/image incompatibility,
automatic retry, extra allocation or cleanup success is inferred; the other four
methods remain unattempted until their separate execution gates permit progress.

The user subsequently selected a **fresh valid M2 measurement**, not completion
of the partially created cluster with contaminated timing. This explicit exception
permits at most two raw M2 attempts (old failed plus one fresh replacement), while
the other four methods stay n1 and valid comparison slots stay5. Revised future
creation ceilings are12 VMs/14 new volumes/442GiB, max two CPU VMs concurrent;
there is no general retry loop or permission to erase the original failure.
Choice readback:2026-10-26T05:12:40.882911UTC.

Separate actual failed-consumer disposal was read back05:14:20.328944UTC: exact
server/boot404, needed source store read-only/available/zero attachments, activeVM0,
zero new resources. Source result `44b25d19…` records47.37448763847351s controller
intent→terminal wall-time (operational cleanup, not benchmark timing or the outer
tool-job duration). Guest unmount/graceful/foreign-holder attestation is not claimed.
That preparing state was subsequently superseded by the exact approved observer
revision: selected comparisons are separate from raw attempt history, using
`selection_revision=fresh-m2-valid-selection`, `admission-v3-fresh-m2.json`,
`method-results/prepared-layer-connect-fresh.json` and a new admission-scoped
guard namespace. Original failed summary/slug guard/counters and all old admissions
remain intact. Global spend includes the old1 VM/1 volume/30GiB; it is not refunded.
Actual selected-zero/raw-history-one Notion readback completed05:51:01.103315UTC,
report000080, before the separately owned fresh consumer launch. The sole collector
became28451; exact3-file cutover preserved cadence/history and unchanged publisher/
collector source with a0.6292s verified-supervisor pause and15s resume watchdog.

The fresh attempt then reached both consumer creates but failed at the server
cloud-init status command (exit2) before workload qualification. The original
execution verdict retained child uncertainty/cleanup false; clocks were closed
on failure. Autonomous report000083 was read back05:58:53.764523UTC with selected
attempted1/qualified-settled0, raw M2 total2, active count unknown and cleanup HOLD.
The292.722s failed interval is not a valid comparison or proof of clock drift.
The immutable failure issue was read back06:18:24.185487UTC at
<https://app.notion.com/p/405067c015d081b0a65ccdef9c29c0c3>.
The M2 raw-attempt allowance is exhausted; no third retry or other four method
launch is inferred. Later process/guest/cleanup reconciliation must be separate
evidence, never a rewrite of the failed attempt into success.

A separately authorized existing-pair recovery subsequently passed all six
functional gates: API, both nodes Ready, cross-node DNS/HTTP, PVC persistence and
controlled restart/recovery of both services. Actual terminal `b48ab0f8…` binds
result `aa7576ba…`; Notion readback07:02:43.547149UTC. It created zero resources
and is **functional qualification only**: comparison_timing_valid=false and
deployment time N/A because the recovery is contaminated for normal comparison.
The original failed records/raw limit2 remain unchanged; no third provisioning,
VM reboot or cleanup success follows from this qualification.

An owner-projected diagnostic correction was read back07:06:53.627338UTC:
empty packages[] schema rendering was the narrow issue; the earlier wrong-unit
inference is withdrawn. The R1 helper-mode predicate was overstrict; canonical
parent privacy was preserved, not a production privilege-guard weakening. Actual
binary/version, lower immutability/private upper and two private archives passed.
The NoMux78e02 source is a future-method candidate, not a silently deployed timing
fix. Resource/cleanup status remains a separately timestamped observation.

The final remaining admission now distinguishes **five research methods** from
**four executable methods**: M5→M3→M1→M4, n1 each. M2 is explicitly historical
functional PASS with normal timing N/A/raw2 exhausted, not a synthetic fifth
execution or future slot. `admission-v4-remaining-four.json` and
`publication-v4-remaining-four.json` bind the exact1090/2229 source-accounting
chain and actual M2-pair cleanupacdb. Per-method maps and terminal records contain
exactly four; old raw/selected guards, failures, admissions and payloads remain.
Future9 VM/11 volume/352GiB plus already spent3/3/90 stays within12/14/442/max2.

One exact five-file observer cutover left the collector source unchanged, resumed
the same supervisor89720 stateS after a0.8795s watchdog-guarded pause, and kept
all87 old histories/guard sets/cadence. Sole collector95457 emitted actual
four-zero/research-five report000088, Notion readback07:59:58.056624UTC, before
the executor's preapproved M5 launch. Actual running-M5 report000089 read back
08:03:28.723827UTC: attempted1/settled0, known active1, cumulative actual4 VM/
4 volume/120GiB—not the larger reserved budget and not a workload success.

The prior M2 pair disposal was separately read back08:07:10.081761UTC: both
server/boot404, source RO available/detached, no refunded spend. Its earlier
active0 is explicitly a timestamped cleanup snapshot, never current M5 inventory.
The same sole collector/publisher continues actual phase/result observations
and anchored30-minute reports without a notebook or parent per-step SSH loop.

Actual M5 full proof/cleanup and following M3-running report000093 were read
back08:24:43.717229UTC. Its direct request→full-workload measurement is
650.999556916s, not the651.200730s stopped-primary interval or616.564090s
cleanup interval. This uses canonical source-generated Drover cloud-init with
the experiment callback, not production Drover API acceptance; n1 has no CV.

M3's original normal attempt failed and remains N/A, autonomously read back
08:28:52.956574UTC with cleanup HOLD. An explicitly authorized existing-pair
recovery later passed all six functional gates, read back10:51:08.704271UTC.
The canonical pinned Ansible site was unmodified; R4 applied only the original
guest platform pod-reply prerequisite, with zero new resources. Both native
services restarted and recovered; no VM reboot was claimed. Historical R3
playbook-only API156.793617s/node188.730795s/site-complete190.561266s are partial
diagnostics, with workload time N/A. R4 setup1.824817s/CoreDNS4.037531s are
separate environment-repair diagnostics; never sum or compare them with M5's
whole-VM interval. M3 normal/full-VM request timing remains N/A; raw provisioning
count1 and all prior failures remain immutable. Cleanup is not implied.

At that actual recovery observation, research functional coverage is3 methods
(M2/M3/M5), but normal comparison timing count is only1 (M5). M1/M4 remain
unfinished future n1 work. The prepared final-two caller contract uses exact
two-operation maps/results and three nonexecuting historical bindings, not
fabricated M2/M3/M5 execution slots. No final-two runtime activation is implied.

Actual M3-pair provider disposal was later read back11:06:34.977970UTC:
both server/boot404, source16GiB RO available/detached, unrelated volume
unchanged, cumulative spend7VM/7vol/210GiB not refunded. Guest graceful,
pre-unmount and foreign-holder release were not attested by provider disposal.

The exact final-two observer revision is now active under grant50b6/amend09b4/
source002a: only M1 cold fresh→M4 actual upstream Dockerfile build, n1 each;
old three methods are immutable nonexecuting histories. One actual-source
contract proof validated/bound100 prior reports and showed candidate payloads
identical to the **current** producer, while preserving all historical stored
bytes; it does not regenerate history using an imagined old source version.
One guarded cutover preserved100 histories/old guards/controller/cadence and
left collector source/old remaining-four module unchanged. Sole collector73341
resumed supervisor89720 after0.8397s; actual two-zero report000102 read back
11:21:07.741182UTC before its start witness was issued to the execution owner.
`admission-v5-final-two.json`, `publication-v5-final-two.json` and
`reporter-admission-v5-final-two.json` isolate the new two-operation namespace.

This source/grant remains reviewed **sequential CPU/max2** execution. The user's
permission for future CPU parallelism is not an actual overlap claim; no extra
VM/coordinator was started by the reporter. Final M1/M4 outcomes still require
their own actual timers, full functional proof and cleanup. Prepared/started
state is never a successful measurement or five comparable normal timings.

M1 actually started after the final-two gate, report000104 readback11:24:22UTC,
then failed before acquisition/conversion, report000105 readback11:28:24UTC;
all normal timings stayed N/A and cleanup HOLD. A separately authorized first
real cold job on the existing pair acquired/materialized/registered in39.722367s
and reached provider RO, but subsequently failed both-node readiness. This
partial cold success/full-proof failure was read back12:41:19UTC; no continuous
cold→workload or full-VM timing was reconstructed from the failed interval.

Later M1 existing-pair R2 passed all six functional gates, readback13:24:23UTC,
with exact binary, private COW isolation/immutable lower, two strict archives,
owned masters closed and zero new resources. Normal full-VM and cold→workload
timings remain N/A. The typed one-service-start→both-Ready4.761960s and full
proof78.800002s are **recovery diagnostics only**; never add them to39.722367s.
An unchanged-source/unit/config service start recovered function, but the prior
agent exit cause remains unresolved. Raw M1 provision count1 and all original
failures remain immutable. Provider consumer cleanup was still separate.

That actual state is4/5 functional methods, only1 valid normal whole-VM timing
(M5), and M4's real upstream Dockerfile compile/build has not yet begun. The
prepared final-M4 reporting interface reserves only one new execution record,
with four nonexecuting historical bindings and no new M1 retry slot.

Actual M1 provider pair disposal was read back13:37:48UTC: both server/boot404,
both required16GiB CBC/prepared stores retained RO/available/detached, spend
9VM/10vol/286GiB not refunded. This does not attest guest graceful shutdown,
pre-unmount or foreign namespace holders. No source store was deleted.

The one-case final-M4 observer is now live under dadf/87de/source19e71e and the
separate append-only schema correction5f165/f783. The correction changes only
the request envelope schema to the actual approved-action schema, not recipe,
budget or old evidence. One actual-source proof preserved/bound111 prior reports
and current-producer-compatible payloads; one guarded cutover kept all old
modules/collector/controller/guards/cadence unchanged. Sole collector33772
resumed the same supervisor89720 after0.16599s, with actual one-zero Notion
report000112 read back14:00:32UTC before its immutable start witness.

Only `m4-source-n1-dist6` may execute; v6 admission/publication/witness and the
`-final-M4.json` projection isolate its single record from four historical
methods. The owner then actually created the first M4 builder and fresh artifact
store. Autonomous report000114 read back14:02:39UTC: active1, actual cumulative
10VM/12vol/382GiB—not reserved12/14/442. This is builder provisioning, not proof
that the upstream Dockerfile compilation, source OCI registration, consumer
activation, full six-gate qualification or cleanup has completed.

M4 subsequently completed the actual pristine upstream root Dockerfile/result
and original package/Dockerfile BuildKit solves, `source.kind=oci-build` asset
registration, full six-gate consumer proof and cleanup. The existing one-page
report's four native tables and uploaded self-contained HTML were read back
15:06:29UTC. Actual intervals: consumer request→full workload394.306962s;
artifact-store request→builder release1333.150253s; direct same-controller-clock
build-start→workload1727.583030s, **not** a phase-duration sum. Guest source
solve739.021872s/package solve150.393385s/registration52.700652s and operational
cleanup521.518570s remain separate scopes. All5 methods are functional, but
only M4/M5 have valid normal consumer timings; old M1/M2/M3 N/A remain.

The actual post-terminal snapshot had0 VMs, three RO16GiB source stores
available/detached, and one80GiB builder boot retained for actual source/.git/
assembly/raw BuildKit provenance. Three server404 and two disposable consumer
boot404 do not mean every volume was deleted. Total creations12/14/442 were
not refunded. Original package Alpine/APK/Git/chart inputs are online
non-hermetic; source-built and Hub-reference entrypoint bytes differed as an
observation. No VM reboot, OCI-container parity, variance or SLA is claimed.

Terminal report000118 was rejected after its already-confirmed method stage:
the native row presenter assumed progress-only fields on a terminal object.
A single display-expression repair accepts terminal status defaults without
changing validators, times or evidence. Actual terminal failed before/passed
after in a read-only regression; all eight existing v6 progress rows stayed
identical. Approved module b677 was atomically installed under the publication
lock; no collector restart/cadence change or replay of rejected000118 occurred.

The user then explicitly approved **three distinct fresh clean M1/M2/M3 timing
cases**, n1 each/sequential/max2: additional6VM/7vol/196GiB, cumulative new-revision
ceilings18VM/21vol/638GiB. The report/HTML marks all three **pending/unmeasured**;
this is not a rewrite of prior N/A, a failed-ID replay or retroactive expansion
of the completed M4 authority12/14/442. The same-campaign correction extension
binds the dated R3 environment qualification and its capture age explicitly,
never presenting old qualification as fresh or Mac-mini IO as Ceph-path IO.

The fixed clean-three observer is now live under actual grant8d3d/amend559d/
source20 01e2/user approval8ad. Only new IDs `m1-cold-clean-n1-r1`,
`m2-prepared-clean-n1-r1`, `m3-ansible-clean-n1-r1` are executable, in order;
v7 admission/publication/witness and `-clean-three.json` isolate their records.
Historical functional coverage remains5 methods, while selected normal timing
coverage is2 plus only actually qualified new successes, maximum5—not8 methods.
Old M1/M2/M3 N/A, old caps12/14/442 and every failed raw record remain unchanged.

PRIMARY origin is preserved, including M1 cold-store request. A separate
`direct_server_request_to_workload_seconds` is derived only from successful
same-primary-clock `server_request_start`→`workload_ready` controller endpoints,
with wall/monotonic guard and clock-bound checks. It is not phase summation or
guessed boot subtraction. Original M4/M5 primary394.306962/650.999557s stay as
published; their separately derived server-request spans are394.187716/650.945861s.
Qualification capture age is dated evidence, not an event that triggers a report
every30s; terminal age binds the last actual endpoint rather than mutable now.

One source-shaped proof preserved/bound122 old reports and current-renderer
payloads using each historical authority, plus all stored bytes and guard sets;
actual zero new cases/active0 retained actual12VM14vol442GiB. One guarded cutover
kept collector source/old modules/controller/cadence unchanged, sole collector75683
and same supervisor89720 after0.820726s pause. Actual v7 zero report000123 read
back15:55:42UTC before its witness was sent for the preapproved new-case launcher.
This admission/readback is not an actual new timing success.

Final selected comparison, n1 each, uses the same actual primary-clock
`server_request_start`→`workload_ready` endpoints. Original PRIMARY origins
remain separately preserved:

| Selected method | Direct server-request→workload seconds | Original PRIMARY→workload seconds |
| --- | ---: | ---: |
| M1 new cold fresh | 661.798054 | 664.457444, from cold-volume request |
| M2 new prepared layer | 445.843843 | 445.961766 |
| M3 new canonical Ansible* | 587.650274* | 587.704551* |
| M4 actual source build, consumer phase | 394.187716 | 394.306962 |
| M5 canonical Drover cloud-init | 650.945861 | 650.999557 |

*M3's **raw execution remains failed** after a later post-proof local secret
cleanup error. The original canonical playbook and all six application/service
restart/PVC gates completed and PRIMARY stopped before that error. Independent
provider read-only facts proved both VM/boot404 had already occurred before
the local shred error. The links were pinned Ansible collection test-fixture/
LICENSE TAR symlinks, not kubectl. Approved local-only unlink-self/secret shred
completed without cloud deletes, then formal Main adjudication accepted only
the already-closed workload window. Endpoint/whole wall-monotonic drift was
about0.0059s, below15s. No clock reset, replay, primary recovery or raw outcome
rewrite occurred: new clean-three raw outcomes stay **2 success /1 failure**.
This differs from the old pre-workload M3 recovery windows, which remain N/A.

The optional exact-hash-bound adjudication overlay changes measurement
eligibility and reconciled current cleanup, not method success or record bytes.
Its actual source proof preserved133 old reports/payloads and rejected false raw
success. One guarded two-module reload kept admission, source20, collector,
base/progress/publisher, all old history/guards/controller and cadence unchanged.
Fresh full terminal report000134 read back17:17:14UTC: raw phase failed/3 settled/
2 success/1 failure, **5 eligible workload measurements**, current active0.

Latest final one-page report and7944-byte HTML were read back17:19:00UTC with
four native tables, M3 asterisk and separate cold/prepared/source-build scopes.
M1 new guest cold33.494602s, historical cold39.722367s, M4 source739.021872s/
package150.393385s/build-to-builder-release1333.150253s and direct continuous
first-use1727.583030s are not summed into the comparison column.

Final ordinary cleanup confirmed0 experiment VMs/FIP unbound and the now-unneeded
80GiB builder boot **DELETE404**, after required typed BuildKit records/context/
spec/asset and private proof archives were retained. Raw plaintext compiler logs
were not exported; checkout/build caches/intermediates were intentionally
discarded with that boot, so no full bitwise rebuild claim is made. Four required
16GiB RO source stores remain available/detached; unrelated100GiB unchanged.
All20 recorded boot rows, including the separate reference history, are deleted;
approved cumulative18VM/21vol/638GiB creations are not refunded. No further
experiment execution is authorized by this completion record.

**Restart-policy methodology limit:** frozen `method_common.k3s_layer_unit`
lines1028–1033 emits `Restart=no` for layer methods M1/M2/M4. Pinned upstream
k3s356c0254 installer/service template and the retained observed installer specify
`Restart=always`/`RestartSec=5s`; pinned Ansible5f067 roles and original Drover
templates invoke that native installer. This bounded retained-source check does
not independently attest every rendered M3/M5 VM unit; Drover's separate
agent-join bootstrap retry unit is not the k3s daemon policy. Matching endpoints,
resources and version therefore does **not** establish service-resilience parity.
This is not proof of the earlier M1 exit1 cause (still unknown), a universal
unfairness/fix claim or permission to hotpatch measured units. Selected new
M1/M2/M4 n1 cases completed without manual recovery; earlier manual functional
recovery remains excluded. Numeric values, tables,7944-byte HTML and prior QA/
receipts were preserved; one appended report caveat carries exact source hashes.

### Independent N5/R5 campaign: operator-paused — 2026-10-27 UTC

The new `k3s-r5-74592e35e9cd` campaign is **not** the completed R3/n1
comparison above. Its fixed plan is five methods × five fresh attempts, in
cyclic Latin order across five rounds, with130 planned role-specific PRE/POST
baseline slots. No old n1 measurements are pooled into it. Each performance
point must be the median of three actual guest trials; method distributions
use five case points, not15 pseudo-independent trials. Failed, censored and
missing evidence stays visible. At the pause below, **all25 attempts remain
not-started, with0 fresh baselines,0 qualified windows and no measured plots**.

The user temporarily authorized notebook orchestration while the Mini was
unreachable. The isolated root is
`/Users/pieroot/.local/share/palimpsest-proofs/k3s-r5-74592e35e9cd-notebook`;
controller `pierootui-MacBookPro.local`, UID501, boot
`7457489F-1944-426A-8EF4-39C17433142B`. Later Mini SSH connectivity was restored
and checked separately; the N5 actor was **not** migrated or restarted there.
Measurements would still have to run in the actual authorized Linux guests,
not on notebook storage. Detached tmux is not a sleep/reboot/uptime guarantee.

The new independent report is
<https://app.notion.com/p/406067c015d08168aefce04541d4b3fd>.
One notebook-owned report-only collector/publisher runs in tmux session
`palimpsest-n5-progress-74592e35`, with a private socket under
`/tmp/palimpsest-n5-report-74592e35-501/`. Its monotonic1800-second cadence
retains the preparation anchor. Actual first periodic report000006 was read
back at06:23:24UTC; actual reference-creation report000007 was read back at
06:25:59UTC with0 method attempts and one provisional reference-ledger row.
The old Mini/R3 monitor and its history remain independent and untouched.
Private outbox, immutable write-started journals and readback receipts prevent
blind replay of uncertain writes. Legacy queue0 is not N5 experiment status.

Frozen source manifest `89d59dae809acd3bd9e03a3792a5e0271c08b740da0d1e21c2d6900544383271`,
resource scope `b4792cd95d31d1a3dcc7e62e325d3de3391f8769e5d7e5d53e487b0d641d03b8`
and the actual25-case zero state were checked against every fixed case ID.
The native Notion zero-state readback at06:21:06UTC produced witness
`6b331110d60fb1d6681cd29721d16700175f034f9a6a2524142ec5c0e6a12913` before
the final execution grant; the witness does not circularly bind that later
grant. Project is `SYSTEM` in domain `dmslab`, not Keystone system scope;
the user domain is `Accounts`. Approved ceilings were56 VM creations,
66 new volumes/2090GiB and at most3 concurrent guests, including a separate
reference. All31 pre-existing project volumes are protected.

The first accepted CPU reference
`6815ece6-697a-43c0-a4a3-7278463fc895` reached Nova `ERROR` with public fault
`500 / MessagingTimeout`, before Linux source qualification or any method
attempt. Original CREATE request
`req-3c34f99a-edbf-41e0-81a7-5bdf3edc0356` ran06:25:41→06:26:43UTC; the
tenant-visible `conductor_schedule_and_build_instances` event reported Error.
These are provider incident timestamps, **not performance measurements**.
Underlying messaging/broker, scheduler, storage or HDD causality is unknown.
The separate issue, with actual tenant-scoped evidence, is
<https://app.notion.com/p/406067c015d081338d4cc4d8be09b9c7>.

Ordinary exact-owned cleanup completed06:40:58UTC: that server404 and ports
released, only its new peer SG404, **0 Cinder DELETE calls**,0 new visible
volumes, all31 protected volumes present and borrowed FIP unbound/preserved.
Receipt `control/failed-reference-ordinary-cleanup/result.json` SHA256
`d726a392d4c709985694182d6a4a91e31bcfc1966e88330e471e3878597a93f7`
does not claim graceful guest unmount or listener-reap proof. The accepted
VM creation remains spent; reserved1VM/1volume/30GiB is not evidence that a
30GiB boot volume was physically created and is not refunded by deletion.

The user selected **cloud messaging checks before resumption**. Actual
mutable phase is `hold-failed-reference-ordinary-disposed-before25`; all
case outcomes, clocks, source and original grant remain unchanged. No
automatic retry, replacement reference,57th VM, rebuild, case driver or
Mini migration is authorized. Operator messaging-health confirmation and
bounded resumption/resource/controller approval must precede new execution.
The retained22-passed Nova behavior tests and synthetic25-case/130-slot
source fixtures are **source checks only**, not fresh cloud fio/HTTPS/TCP
qualification; the six-node Linux gate never ran. No benchmark completion,
performance ETA or measured boxplot is claimed.

Actual user-pause native publication was read back06:43:15UTC, receipt
`reporting/user-operator-pause-publication.json` SHA256
`384e266ae776fedc69dfc772a2185b632cc48ea67563928473df80d9d4b73dc2`.
The unchanged autonomous collector then published the correct disposed/HOLD
phase in periodic000009, read back06:53:14UTC, receipt SHA256
`d683f18ebcc6fb8a5c70e1ea862d8187e5f7b39d09b73124ee9f40388022c060`.
Its ledger retains one historical accepted server row but0 unreleased rows;
it does not recast deletion as an unspent allocation.

A separately authorized **private synthetic UI smoke**, visibly labelled
“SYNTHETIC UI TEST DATA – NOT EXPERIMENT RESULTS”, exercised66 SVGs, five
case points/method, MIN/MAX whiskers, role-separated PRE/POST panels and
actual desktop1000/mobile375 layouts without document overflow. Frozen
PNG export failed in Chrome155 because `createImageBitmap` rejected SVG.
Report-only candidate `reporting/renderer-fix-r1/n5_figures.py` SHA256
`2c43955276b7d1cc829368dfe20f341fefb30c499d324a4bffaa48331c893111`
changes only SVG object-URL/Image/canvas PNG export with finally-revoke;
all figure data/markup before that script stayed identical. Actual candidate
exported66 PNGs; two labelled previews were decoded/nonblank and visually
inspected. UI receipt `7262289d8f751ba30ea2b17a63224f909953e53d54d01aceede949c472b4477a`
and owned-browser/profile teardown receipt
`1600b5fff0f5688d84f0feee8e7e9c6b2df4baf150084ad0ba28de1608d2b3db`
remain private. No synthetic images were uploaded, no frozen source/grant or
active reporting code changed, and **actual N5 samples remain0**. This closes
render-only verification, not the paused experiment or future source admission.

Synthetic-smoke limit: the fixture uses identical values across methods and
PRE/POST groups. Counts, labels, box shapes and PNG export were exercised,
but this smoke does **not** discriminate swapped/duplicated per-method or
PRE/POST numeric mappings; no stronger mapping-correctness claim is made.

A subsequent bounded **distinct synthetic mapping check** closed that numeric
mapping gap without changing the original smoke or frozen source: independent
nonlinear per-method five-value inputs, distinct role/profile offsets and a17
POST offset passed raw fixture→`build_report`→figure specifications→SVG
geometry checks for51 figures,265 groups and1325 case points. Case IDs,
method/role/PRE–POST labels, units, min/Q1/median/Q3/max, whisker ends,
median/box positions and point coordinates matched independently specified
inputs. Private `reporting/renderer-fix-r1/distinct-group-mapping-proof.json`
SHA256 `8cd18fc1faf25a50158c57eb70363d94804d45e33694bdcd521583e54f39ead6`;
no new browser run, cloud request, Notion write or actual measurement.

### N5 resume-01: reporting admitted, replacement reference held — 2026-10-27 UTC

The subsequent explicit user choice supersedes the historical pause envelope:
**57 cumulative VM creations /67 volume reservations /2120GiB /max6 live VMs**,
with **two method cases** selected concurrently. One reference plus two guest
pairs normally bounds selected waves at5 live VMs; the sixth slot is headroom,
not an extra reference, FIP or third method case. Round1 waves are M1/M3,
M2/M4, then M5; later rounds retain cyclic-Latin adjacent pairs/singletons.
M2-r01 waits for successful, normally cleaned M1-r01 preparation. Whole-baseline
serialization permits at most3600s queue wait plus the unchanged3600s baseline
stage, included in action ceilings and raw case clocks. Peer activity samples,
UTC/KST windows and descriptive overlap are not causal-fairness or matched-idle
proof; no serial n1 measurements are pooled, and a serial work sum is not a
concurrent wall ETA.

The separate immutable source manifest is
`621ad8b775a29ad978c5fb3f4642c22a44ea85e9842a612543bf090356667e52`.
It adopts renderer bytes `2c43955276b7d1cc829368dfe20f341fefb30c499d324a4bffaa48331c893111`
unchanged: only the earlier line389 PNG decode repair differs from frozen
`b0ac5fdd…`. Retained66-PNG Chrome and distinct-group proof `8cd18fc1…` were
reused, not rerun. The original full report fixture **failed at207** because
its synthetic open-span case invalidated the M1-r01 preparation-source receipt;
the producer guard correctly rejected it. No frozen fixture was rewritten.
An outside-source targeted closure proved that rejection, a nonproducer open
span remains unknown/censored, and the previously unreached210–400 checks;
`resume-01/reporting/pure-checks-01/receipt.json` SHA256
`53c993567ec701add5131dc5a660c35e0ad6b36db057c91436be40006d33c5f0`.
This is not a full-fixture pass or measured-data qualification.

An exact approved report-only cutover gracefully replaced old producer12909
with sole Python producer76067, preserving the shared collector lock and1800s
anchor. Actual25-not-started source witness read back **11:45:32.659605UTC**,
SHA256 `68b17f46226ab46e0f82c82e9f936097027530f92e7ac3c93876027172d44ae9`;
it explicitly retained the then-original v1 state and1/1/30 reservation.
This witness did not sign execution authority or claim resource readiness.

The separately approved reference-only grant
`273a54d423683e425b4933740fbed1f1ca97e39f415194c38527f5d53bc6a6d8`
then migrated state to v2 and accepted replacement Nova server
`ae1f44c4-3294-44e6-9f28-c2256961fc9c`, with new peer SG
`bcf83bfd-def2-404b-8366-f1b856e4fe16` and six rules. That reference reached
Nova `ERROR`; the finite supervisor ended **HOLD-create-failed-no-next-step**
at11:49:49UTC, terminal SHA256
`70f525d4b519b83c8f3e186b9be19e2714b8c62f829b7334bb0bbe24d8268aa0`.
Linux qualification, SSH/FIP binding and the25-case driver did not run;
no retry or cleanup was inferred. The retained failed first reference remains
history, cumulative reservation is **2VM/2volumes/60GiB**, all25 cases remain
not-started, and actual method/baseline/qualified-window counts remain0.
The existing reporter automatically read back this v2 HOLD at
**11:50:18.063745UTC**, receipt `20261027T115010Z-000021` SHA256
`60c43c0975d744b03bbe05527b58db25ab71c79eda3cca1fd01cae105e021a1a`;
no manual re-zero publication or reporter restart followed the resource result.
Provider root cause, recovery and future cadence remain unproven.

A separate harness incident authenticated once to real Keystone before missing
fixture `serialization_root` stopped the baseline child. It made no
compute/volume/network request and no measurement; do not extend the earlier
batch's scoped zero-cloud-request claim to this incident. Private evidence is
`resume-01/fixtures-01/targeted-closure-receipt.json` SHA256
`dd363b99f956973158f56ec691b51a84abded5fa7648b2a8160d29a44c205bcf`;
it is an issue, not a performance case. Requested Astra review was unavailable;
independent review is not Astra approval. No synthetic figures were published.

Subsequent exact-owned diagnostics (14 GETs,0 mutations) distinguish this from
the first reference failure: public fault **500 /Bad Gateway (HTTP502)** at
11:49:47UTC, with a host-present `compute__do_build_and_run_instance` event
11:47:32→11:49:48UTC whose result field says `Success` despite final `ERROR`.
The30GiB boot `befe22fe-083d-4e26-ad1c-fe64e6ff440e` reports `ceph_hdd`,
bootable and available/unattached; Nova BDM still lists `/dev/vda`, DOTfalse.
No server/SG ports, borrowed FIP unbound and31/31 protected volumes were observed.
This identifies neither the HTTP502-dependent service nor a broker root cause,
and is not a guest mount, physical disk or performance proof. Allowlisted
summary SHA256 `987f8f92a44af6706413fbce4e2eba62f827902dda2b9704b483fd68e7a9f965`.
One authorized native append to the **existing** provider issue plus a concise
campaign HOLD link read back **11:53:53.905198UTC**, receipt
`resume-01/reporting/reference-r02-provider-hold-publication.json` SHA256
`651ec992d89aa984cb0d7e462692f530aa72a548c9967c5927a694971bf02b47`.
No new issue page, raw diagnostics, automatic retry or cleanup was published or
performed; the separate fixture Keystone-authentication incident is disclosed
there without counting it as resource creation or a performance case.

The later explicitly selected **ordinary r02 cleanup completed13:36:04.364446UTC**:
only VM `ae1f44c4-3294-44e6-9f28-c2256961fc9c` DELETE→404, then its original
30GiB boot `befe22fe-083d-4e26-ad1c-fe64e6ff440e` DELETE→404, then new SG
`bcf83bfd-def2-404b-8366-f1b856e4fe16` DELETE→404. The three-request disposal
preserved all31 protected volumes/3277GiB, left0 nonbaseline volumes, and kept
the borrowed FIP unbound without any FIP mutation. Result
`resume-01/failed-reference-r02-cleanup/run-01/result.json` SHA256
`e645a57add04fd5a51e4f9b1bf9db695bb4a3a68f9c58be6d19105d4212d7c78` binds
provider proof `c783feea1cee927f572693abeb7fbd59a41f9fa7169f22657a4744aa73ce6499`.
Accepted Nova creations2 and reserved2VM/2vol/60GiB remain **nonrefunded**;
all25 cases remain not-started/0 baselines and new launches remain HOLD.
No third reference/retry, force cleanup, guest pre-unmount/listener-reap,
Linux source qualification or live concurrent/performance run is implied.
Earlier fault/HOLD checkpoints above are historical, not current resource presence.

One authorized native cleanup-complete append to the **existing** issue plus
campaign link read back **13:38:36.871017UTC**, receipt
`resume-01/reporting/reference-r02-cleanup-publication.json` SHA256
`6dd58cd3a9c2c0d2758c8920b5f28ef245af77c41e825f6cd86fe18c34197012`;
payload SHA256 `c6b7d1b1ac0a97ebad02646b5f095a5bf3245ffc3b9c18e13e498ea58f576c6c`.
Existing error history was retained; no new issue page, synthetic-result upload,
reporter restart or measurement claim accompanied this cleanup publication.

### Measurement reports and issues

The one-page result contains run/flavor metadata, successful disk/network tables,
and distinct method/cache/preparation-scope timings. Put failures, collector
bugs, timeouts, clock-invalid attempts and unresolved causes on a separate issue
page, linked from the report. Do not hide incomplete coverage: use N/A and link
its evidence without mixing failure rows into the final success comparison.
HTML is a self-contained visual overview; measurements remain readable native
Notion tables. Preserve source manifest/config/asset IDs and local evidence
filenames separately from signed URLs, credentials and private user-data.
