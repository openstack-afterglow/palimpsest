# Official Docker Hub service compatibility

This matrix exercises unchanged, digest-pinned Linux amd64 OCI archives through
the public Palimpsest KVM runtime. It is separate from the older unprivileged
NGINX image proof and from local-build Gate 2. Image tags identify the intended
source; each actual run must record its selected manifest and archive digest.

| Case | Official source | Memory / vCPU | Process policy | Application check |
| --- | --- | --- | --- | --- |
| `postgres` | `docker.io/library/postgres:17` | 2048 MiB / 1 | Image default | Local Unix-socket `SELECT 1` |
| `redis` | `docker.io/library/redis:7-alpine` | 512 MiB / 1 | Image default | Guest-loopback Redis `PING` |
| `mysql` | `docker.io/library/mysql:8.4` | 2048 MiB / 1 | Image default | Local Unix-socket server ping |
| `nginx` | `docker.io/library/nginx:stable-alpine` | 512 MiB / 1 | Image default | Guest-loopback HTTP response |
| `redis_user` | Same pinned Redis archive | 512 MiB / 1 | Explicit `--user redis` | Guest-loopback Redis `PING` |

The Redis override is a separate result, never a correction of a failed default
run. No host port, network interface, capability, initialization secret, or
image environment is added by these tests. Application checks do not imply
external connectivity. Readiness, version, application response, authenticated
root identity, PID 1 access denial, and cleanup are distinct observations.

Fresh PostgreSQL and MySQL images need initialization configuration. Their
entrypoints can also require directory ownership changes and a user switch.
Palimpsest currently accepts a user override, but no OCI run environment or
command override. Therefore a default-run failure must retain the actual
console evidence and reached stage; it must not automatically be attributed to
a missing password or treated as a successful negative test. See the upstream
[PostgreSQL entrypoint](https://github.com/docker-library/postgres/blob/master/docker-entrypoint.sh)
and [MySQL entrypoint](https://github.com/docker-library/mysql/blob/master/docker-entrypoint.sh).
The acquired image bytes, rather than a mutable upstream branch, determine the
entrypoint actually tested.

## Execution and preservation

Use the independently opted-in cases in
`tests/kvm/test_oci_docker_hub_services_live.py`, one at a time on the qualified
server after pushing and checking out the exact tested commit. Each case needs
its own `PALIMPSEST_OCI_DOCKER_HUB_SERVICE_<CASE>_LIVE=1`, `IMAGE`,
`ARCHIVE_SHA256`, and `MANIFEST_SHA256` values, plus the existing five OCI host
BOOT/packer variables and a healthy private command journal.

Select one case without rerunning the entire native suite:

```sh
uv run pytest -q -s tests/kvm/test_oci_docker_hub_services_live.py -k postgres
```

Without its opt-in, a skipped case is not qualification. Portable contracts
belong to the ordinary lanes; VM cases stay in the explicit native lane.

Before and after each case, verify the exact preserved domain names, UUIDs,
states, autostart settings, source archive hashes, and absence of active QEMU.
Successful cases remove only their own run/domain through public commands.
Failed cases retain runtime, disk, and bounded evidence; only an exactly
owned active VM may receive public `stop`. Unknown ownership or failure to
prove inactivity blocks the next VM. Do not adopt an unrelated domain, use
force-destroy as fallback, or remove failure evidence to make the next case pass.

## Results

### Current network qualification contract

The Redis-user proof requires matching `/proc/net/dev` and sysfs interface
sets, unique positive indices, and exactly one `lo` (type 772, flags `0x9`
or `0x49`). Only optional `tunl0` (type 768) and `ip6tnl0` (type 769), each
with exact flags `0x80`, are accepted in addition. Unknown, renamed, UP,
duplicate, missing or inconsistent interface evidence is rejected. This is
a deliberate test-contract revision from literal only-`lo`, not a retrospective
pass for the earlier failed runs. A separate live domain XML check rejects
network interface devices before service probing. No guest networking,
capabilities or packaged ELF are changed. The existing UID/GID, capability,
NNP/seccomp, exact PONG, root/PID1 and public cleanup assertions remain.

### Redis diagnostic checkpoint — `bb879dd`, 2026-09-12

Exact `bb879dd645ba38463abc6f5a13e4f9be2567a926` passed the focused
service-contract and architecture selection locally (32 tests, 6.87 seconds)
and on the server (32 tests, 4.50 seconds). Only Redis explicit-user native
was rerun; stage-1 and stdio results at `f86e4da` are not new runs at this SHA.

The unchanged official Redis archive, launched with `--user redis`, returned
exact `PONG` with exit 0. The application's actual `/` matched both authenticated
root receipts (overlayfs, device 21, inode 2). Reading `/proc/1/root/etc/os-release`
was denied. The receipt shows UID 999/GID 1000, all capability sets zero,
NNP 1, seccomp 2, and loopback `127.0.0.1/8` up.

Raw interface evidence identifies `tunl0` (type 768, index 2) and `ip6tnl0`
(type 769, index 3), both with flags `0x80` and therefore no UP bit. They
explain the two extra entries; the retained domain XML has no NIC. The strict
only-`lo` assertion still fails (one failed test, 23.62 seconds). This records
working guest-local Redis communication, **not** a passing full compatibility
case, default-user Redis success, or external connectivity. No acceptance
criterion, production guest, packaged ELF, or privilege policy was changed.

Evidence: `/tmp/palimpsest-loopback-native-1gjx3ss_`, runtime
`/tmp/p-hub-svc-rdu-fbe72bb6`; wrapper SHA-256
`5769b05187ca645ee21a5263913859c2b53a9df81620e56bb4620a205eb25d37`.
The private journal `/tmp/palimpsest-loopback-journal-redis_user-g4qk_tjs`
passed validation (20 records, 10 invocations, 4671 bytes). Existing nine
inactive domains and twelve archive hashes were preserved, along with the
new failed domain `hub-service-redis-user-c99a1a55`, UUID
`6c5715b0-db35-4506-98d1-f5806eb98f5a`, shut off through public failure-stop.
Final preservation checks passed with ten inactive domains and no active
VM/QEMU; overall wrapper exit 1 retains the failed qualification. Evidence
was not deleted. These host temporary paths are not durable published artifacts.

Next review: distinguish known inactive kernel tunnel devices from external
NICs without relaxing workload protection or treating arbitrary extra devices
as safe. That contract remains pending; no revised network acceptance is
claimed by this diagnostic.

### Loopback checkpoint — `f86e4da`, 2026-09-12

Exact `f86e4daa8554fdd4b08f3c3c994263e486363d7a` passed 453 focused
tests on `pieroot-server`, including Linux x86_64 ioctl ABI and packaged ELF
checks (48.37 seconds). The packaged ELF digest is
`40f9553d12454bfeb7f0ebd73367b334dd1005fec729153ef9cd2a880cbf6f50`,
source-bundle digest
`ae299ca7616637c2b93015e29540910631684bbfc880d32030985f696482a339`.
The native stage-1 matrix passed (43 boots/44 QEMU; 122.02 seconds), as did
UID 0/101 stdio (two tests; 31.00 seconds). Original eight inactive domains
and twelve archives passed preservation checks through the stdio phase.

Redis-user then failed before PING because the new network receipt counted
two non-loopback interfaces. Nevertheless, its saved output directly shows
`lo` up with `127.0.0.1/8`, UID 999/GID 1000, all capability sets zero,
NNP 1 and seccomp 2. This is not yet a PING or completed root/PID1 proof.
The retained domain XML has no network interface device. Built-in IPIP and
IPv6 tunnel support in the qualified kernel suggests default tunnel devices,
but their actual names and states were not captured by this first probe and
must not be inferred as proven facts.

Evidence is `/tmp/palimpsest-loopback-native-qjqzxze3`; failed runtime
`/tmp/p-hub-svc-rdu-e8397910` retains domain
`hub-service-redis-user-b4b346bb`, UUID
`8e47605d-16b8-40c1-bc80-912e2a05db40`. The public test failure-stop path
left it shut off; a separate read confirmed inactivity. The wrapper's final
inventory rejected this additional retained domain, so its final preservation
gate failed and is not reported as passed. No failure evidence was removed.
The executed wrapper digest was
`50de59cf4fc321d523b1169c0ce0288a82d0c00e14ebe5541121ba91c5d9656e`.

The follow-up test retains raw net-device rows and each sysfs interface's
name, flags, type and index. It defers only the loopback-security assertion
until after the independent PING/root/PID1 observations, without accepting
the unexpected interfaces or converting a failed case into a pass. The
production guest and ELF are unchanged by this diagnostic-only follow-up.

### Native execution — `1752ba9`, 2026-09-11

All five independently selected cases were executed on `pieroot-server` at
exact `1752ba90ae604b5d1776c2e70ef8f9f28c3c3da3`. All five tests failed;
none is a successful application compatibility qualification. The same
focused portable selection passed locally (109 tests, 9.22 seconds) and on
that exact server checkout (109 tests, 7.80 seconds). Those passes qualify
the test contracts, not these services. The GitHub package build also
[completed successfully](https://github.com/openstack-afterglow/palimpsest/actions/runs/34558389044).

| Case | Observed stage and failure | Application response |
| --- | --- | --- |
| Postgres default | OCI root transition and workload start completed; directory `chmod` and switching to `postgres` failed with `Operation not permitted` | SQL probe not reached |
| Redis default | OCI root transition and workload start completed; `setpriv: keep process capabilities failed: Operation not permitted` | PING probe not reached |
| MySQL default | Stage-1 booted, then rejected root transition with `target=sys; check=mode`; workload was disabled fail-closed | MySQL entrypoint and socket probe not reached |
| NGINX default | Entry point reached NGINX, but `chown` of `client_temp` to UID 101 failed with `Operation not permitted` | HTTP probe not reached |
| Redis `--user redis` | Service readiness and version 7.4.11 succeeded; PING to `127.0.0.1:6379` failed with `Network unreachable` | Failed connectivity check, not a demonstrated server crash |

Only the explicit-user Redis case reached the independent root/PID 1 probes.
Its before/after authenticated root identity remained device 21, inode 2 and
matched the application's actual `/`; direct `/proc/1/root` reading was denied.
The other four cases did not reach those probes. Lifecycle READY is not
application readiness. Neither database reached a missing-password error;
initialization configuration remains a separate, untested dependency.

The next explicit-user Redis run is defined to retain a bounded guest-network
and security receipt before its application probe. It requires `/proc/net/dev`
to expose only `lo`, validates `127.0.0.1/8` when the unchanged image supplies
`ip`, records one non-root numeric UID/GID with all capability sets zero,
no-new-privileges 1 and seccomp mode 2, then requires exact `PONG`. This is a
test definition, not a later native success and does not revise the failed
`1752ba9` result above.

A read-only header scan of the pinned MySQL archive found a root-owned `sys`
directory with mode `0555` in its base layer and no later shallow replacement.
The current transition contract accepts exact `0755` for `/sys`, unlike the
separate `/proc` allowance. This explains the observed mode rejection; changing
the accepted input mode still needs a separately reviewed contract and native
proof, not a broad permission relaxation or archive mutation.

The retained matrix receipt and bounded command evidence are under
`/tmp/palimpsest-services-native-v563p6fq`, with `matrix-results.json` recording
each runtime and healthy private command journal. Failed runtimes are:

- Postgres: `/tmp/p-hub-svc-pg-a8da3859`
- Redis default: `/tmp/p-hub-svc-rd-bf8995a1`
- MySQL: `/tmp/p-hub-svc-my-44f35cad`
- NGINX: `/tmp/p-hub-svc-ng-4707394c`
- Redis explicit user: `/tmp/p-hub-svc-rdu-fde37c80`

The native wrapper SHA-256 was
`31cf7e19eb49679ba68f32fe836ba2135bae2790a29355877d467b5091ad85b5`.
All 29 initial and 41 final preservation observations passed, as did each
between-case check. The original four inactive domains and eight earlier
archives, plus all four newly acquired archives, were preserved. Four new
failed domains remain inactive; MySQL's failed launch left no registered
domain but its runtime remains. No active VM/QEMU remained. Public `stop`
was used for the owned running Redis-user failure; no force-destroy or manual
failure deletion was used. These temporary host paths are retained evidence,
not durable published artifacts. Production guest and security policy did not
change, and prior unprivileged NGINX/Gate 2 successes are not promoted to
official-image service successes.

### Follow-up boundaries

Prioritize a narrowly scoped guest-internal loopback design and focused Redis
connectivity test; this must not silently add a NIC, host port, external route,
or workload capability. Separately inspect the pinned MySQL `/sys` metadata
against stage-1's exact mode contract before proposing any allowance. Keep
Postgres/NGINX ownership and identity transitions distinct from database
environment configuration. Explicit environment support and non-root launch
configuration need their own contracts and verification; they cannot be
claimed to solve these observed permission failures. These are next-step
design boundaries, not implemented fixes or authorization to relax PID 1,
capability, no-new-privileges or seccomp protection.

### Fresh acquisition — 2026-09-11

On the exact clean `f77e251ff07c94392215c1aa09b87058449013fe` server,
external Skopeo acquisition completed for all four official sources. The
private selection receipt is
`/tmp/palimpsest-services-acquire-kytw4jez/selection-receipt.json`.
Each selected platform manifest, config and compressed layer was size/hash
checked; archives remain unmodified. All four configs have the default root
user and their original entrypoint plus default command.

| Image | Archive SHA-256 | Selected manifest SHA-256 | Archive bytes |
| --- | --- | --- | ---: |
| Postgres 17 | `ac62d2c7178f84938d23d45abcc6eeb17d7ac416f84742022b78737d39500174` | `d13db94ae661d517c5ed57c509a578d5ea64aae639871ba25294f4f42d83de28` | 161315328 |
| Redis 7 Alpine | `91155f4ab07ee60968fb00651769e650c78cf5515aa4e9d0e03731b8565ebed7` | `1db42ccef14898aa29bae778452d567534b59c107129cbc1163fb552de184d3c` | 16277504 |
| MySQL 8.4 | `84afe48b07fb60de7329f8200b10ad1f407b74ad0bb714d5ea73a5ad9bcfb3b5` | `d28300f0136cb6d4e24603b9da20460f216845ea778b3b9a4166825d78f0c7dd` | 239007232 |
| NGINX stable Alpine | `ece37c1755bb11644bbba93602821cbb0669a45dc76030b3c6d641c3f959c572` | `862dc06c359bfe5d3211e4106269f040d261e269e58ebf17060d8328c45067c0` | 28558336 |

The fixed Skopeo tool image ID was
`sha256:6427ae801eaa5e1b4579e20dce5940015352d99506fd9c5a8e8f8ca4fb202f68`.
It ran as the host user without capabilities, with no-new-privileges and a
read-only root. Only the fresh acquisition directory and its temporary child
were bind-mounted writable. No Docker socket was exposed inside the container.
The final acquisition wrapper digest was
`86215c2033a9b22ade2e8e7ecb47be9ef6bf854a8e4b0ef324c4271298409cf0`.
All 25 preflight and 25 postflight observations preserved the original four
inactive domains and eight earlier archives, with no active VM or QEMU.

Two acquisition-procedure failures remain separate from image compatibility:
`/tmp/palimpsest-services-acquire-j41fg6z0` stopped before container creation
because of an invalid bare `rw` option in Docker's `--mount` syntax;
`/tmp/palimpsest-services-acquire-s1bd9py1` resolved the Postgres manifest but
could not create Skopeo's temporary directory under the read-only `/var/tmp`.
Removing the unsupported mount option and binding a fresh owner-only temporary
child to `/var/tmp` fixed the procedure without broadening host write access
or container privileges. Both attempts passed all 25 pre/post preservation
observations. These are not failed VM boots or successful service proofs.
