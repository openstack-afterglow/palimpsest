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

Native results for this new official-image matrix are pending. Existing
NGINX-unprivileged, Redis explicit-user, and Gate 2 passes do not qualify these
new images or application probes.
