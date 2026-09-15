# OCI-root guest networking

`palimpsest run` gives a local OCI-root VM one selectable virtual network:

```console
palimpsest run ./service.oci.tar --name api -d \
  --network nat --publish 127.0.0.1:18080:8080
```

| `--network` | Guest address | Outbound | Inbound |
| --- | --- | --- | --- |
| `nat` (default when omitted) | `10.0.2.15/24` via `10.0.2.2` | NAT to whatever the host can reach, DNS at `10.0.2.3` | only explicitly published ports |
| `host-only` | `10.0.2.15/24` | none; the guest cannot reach the host or any external address | only explicitly published loopback ports |
| `none` | none | none | none; the domain has no NIC at all |

**Omitting `--network` now means `nat`.** This is a deliberate breaking change
from the previous surface, where omission and `--network none` were both
no-NIC runs. An unchanged command therefore grants an untrusted image outbound
network access; use `--network none` to keep the historical isolation. Durable
state never reinterprets old runs: a domain plan written before this contract
is rejected and must be rebuilt, so a legacy run is never silently upgraded to
NAT.

## Publishing ports

`--publish [HOST_IP:]HOST_PORT:GUEST_PORT[/tcp|/udp]` is repeatable and
OCI-root-only; `-p` is the short form. Cloud-image runs reject it.

- The host address defaults to `127.0.0.1`, so publishing never exposes a
  service beyond the host by accident.
- `--publish 0.0.0.0:18080:8080` is the explicit opt-in to external exposure.
  Only then is the service reachable from the host's LAN address.
- Host ports must be 1024–65535. The VM process holds no privileged-port
  authority, so a privileged host port is refused instead of silently failing.
- `host-only` accepts published ports only on loopback addresses.
- `none` accepts no published ports at all.
- At most 32 publications per run. Duplicate `(host address, host port,
  protocol)` triples and wildcard/specific conflicts on the same port are
  refused before any host or state mutation.
- IPv6 is unsupported: host addresses must be IPv4 literals. `[::1]` and `::`
  are rejected rather than silently mapped, and a `0.0.0.0` listener does not
  bind IPv6.
- Port ranges, container-only ports (`--publish 8080`), and hostnames are
  rejected; every value is a literal address, integer, or protocol enum.

A guest service must listen on `0.0.0.0` (or `10.0.2.15`) to receive forwarded
traffic. A guest process bound only to `127.0.0.1` stays guest-local by design.

## How it works, and what owns what

OCI-root networking uses QEMU's built-in user-mode (SLIRP) backend authored
directly in the domain:

```text
-netdev user,id=pnet0,net=10.0.2.0/24,host=10.0.2.2[,dns=10.0.2.3|,restrict=on][,hostfwd=…]
-device virtio-net-pci,netdev=pnet0,mac=52:54:00:…,bus=pcie.0,addr=0x14
```

Consequences that are part of the contract:

- Palimpsest never creates, adopts, or mutates a libvirt network, bridge, tap
  device, firewall rule, or DNS service. `virsh dumpxml` shows no `<interface>`
  for an OCI-root domain: the NIC belongs to the authored QEMU argument vector.
- Every host listener is opened by the VM's own QEMU process. It disappears
  when the domain stops, so `stop`/`rm` need no separate listener revocation:
  proving the exact domain is inactive and then absent also proves the
  published endpoints are gone.
- The kernel `bind(2)` is the only port arbiter. Two runs cannot both own a
  host endpoint; the losing launch fails with its evidence preserved instead of
  a check-then-bind race.
- `host-only` uses `restrict=on`, which blocks guest-initiated traffic to the
  host and to external networks while still honoring the authored forwarding
  rules.
- There is no guest-to-guest L2 network. Each VM gets its own private
  `10.0.2.0/24`; inter-VM networking remains a separate future contract.
- The MAC is derived deterministically from the run ID inside QEMU's
  `52:54:00` range, so the guest can identify the exact authored NIC.

## Future contract decision

IPv6 host publication and VM-to-VM networking are **separate future
contracts**. Neither is an implicit extension of
`palimpsest.oci-root-network.v1`, and neither is implemented or qualified by
this decision.

### IPv6 host publication

This contract is limited to binding an IPv6 host listener and forwarding it to
the existing guest service. It does not imply guest IPv6 addressing, routing,
DNS, or egress; those would be another guest-network contract. Implementation
must use a new explicit publication grammar and durable schema rather than
widening the current colon-delimited IPv4 parser. It must define canonical
address syntax, loopback versus external classification (`::1` versus `::`),
IPv4/IPv6 wildcard collision behavior, and whether a listener is v6-only.

The exact supported QEMU version must prove TCP and UDP forwarding, safe
loopback defaults, real IPv6 host traffic, launch-time bind failure, and
listener removal. [QEMU's invocation reference](https://www.qemu.org/docs/master/system/invocation.html)
documents IPv4/IPv6 controls for its user-mode backend and a generic `hostfwd`
host address, but that documentation alone does not qualify the required
host-bind grammar or dual-stack socket behavior. Existing
v1 records therefore remain IPv4-only and are never reinterpreted.

### VM-to-VM networking

This requires a shared network resource and backend; it is not a fourth value
beside the current per-VM `nat`, `host-only`, and `none` modes. The contract
must define owner/tenant identity, network creation and removal authority,
membership, deterministic address and MAC allocation, duplicate prevention,
egress and host-reachability policy, optional name resolution, concurrent
attach/remove behavior, and recovery without adopting unrelated host network
objects.

QEMU exposes socket/stream and multicast backends that can connect virtual
machines, but those add shared listeners and topology state that the current
per-domain SLIRP ownership model intentionally lacks. Backend selection is
deferred until the shared-resource lifecycle is specified. Qualification must
use at least two real guests, bidirectional application traffic, negative
isolation cases, exact owned cleanup, and preservation of unrelated domains
and host network state.

The two workstreams are independently gated: IPv6 host publication neither
requires nor grants VM-to-VM reachability, and a future shared VM network does
not silently enable IPv6 or external exposure.

## Durable binding and guest verification

The complete intent — mode, backend, subnet, and the sorted publication tuple —
is bound into the OCI-root domain core (`palimpsest.oci-root-domain-core.v10`)
and domain plan (`palimpsest.oci-root-domain-plan.v16`). The domain core digest
travels on the kernel command line, which also carries the static address
configuration:

```text
ip=10.0.2.15::10.0.2.2:255.255.255.0::eth0:off[:10.0.2.3]
```

The stage-1 plan (`palimpsest.oci-stage1-plan.v16`,
`palimpsest.guest-stage1.v16`) carries the guest projection: mode, interface
name, address, gateway, netmask, expected MAC, and resolver policy. Host
publications are deliberately absent from the guest view.

Before the workload starts — while still privileged and before credential,
capability, and seccomp restriction — first-party PID 1:

1. verifies `eth0` exists, is `UP` and `RUNNING`, is not loopback, and matches
   the committed MAC, address, and netmask;
2. requires exactly one default route, on that interface, through the committed
   gateway;
3. requires the exact set of IPv4-configured interfaces (loopback alone without
   networking, loopback plus `eth0` with networking) so an extra active
   interface fails the boot;
4. writes `/etc/resolv.conf` with the committed nameserver for `nat` runs, using
   `O_NOFOLLOW` on a regular file, and writes no resolver at all for
   `host-only` or `none`.

Any mismatch rejects the workload launch instead of starting it with unverified
networking. With `--network none` the guest now additionally proves loopback is
the only configured address.

## Observing exposure

```console
palimpsest ps
palimpsest inspect api
palimpsest oci network api
```

`ps` includes a `PORTS` column for every committed publication, formatted as
`HOST_IP:HOST_PORT->GUEST_PORT/PROTOCOL`. `inspect` emits the same publications
as typed `detail.ports` fields together with `detail.network` and the fixed
guest address. Both commands read one durable ledger snapshot, perform no
backend call or state write, and report no publications before a domain plan is
committed.

These are configured endpoints, not a liveness claim. A stopped or failed run
retains its configured publications in durable inspection even though its QEMU
process owns no active listeners. Check lifecycle status and probe the service
when current reachability matters.

`oci network` performs the stronger OCI-specific committed-plan and stage-1
transport verification, then reports mode, backend, subnet, guest
address/gateway/netmask/MAC/resolver, every publication, and an explicit
`exposure.external` classification with its endpoints. A missing or invalid
plan is reported as a refusal, never as "no exposure".

## Host prerequisites

`--network nat` and `--network host-only` require a QEMU build with the
user-mode network backend (`qemu-system-x86_64 -netdev help` lists `user`).
The capability check runs before any state mutation and fails closed with
guidance to install such a QEMU or run with `--network none`. No other host
package, daemon, or privilege is required.

## Verification

Portable contracts live in
[`tests/unit/test_oci_network.py`](../tests/unit/test_oci_network.py),
[`tests/unit/test_oci_network_live_contract.py`](../tests/unit/test_oci_network_live_contract.py),
and the guest C harness
[`tests/unit/test_workload_network.py`](../tests/unit/test_workload_network.py).

The native proof is opt-in
([`tests/kvm/test_oci_network_live.py`](../tests/kvm/test_oci_network_live.py))
and needs `PALIMPSEST_OCI_NETWORK_LIVE=1`, a `0711` runtime parent in
`PALIMPSEST_OCI_NETWORK_PROOF_ROOT`, the ordinary native boot variables, and a
pinned original archive per node:

```sh
PALIMPSEST_OCI_NETWORK_PYTORCH_IMAGE=/path/pytorch.oci.tar
PALIMPSEST_OCI_NETWORK_PYTORCH_ARCHIVE_SHA256=sha256:…
PALIMPSEST_OCI_NETWORK_PYTORCH_MANIFEST_SHA256=sha256:…
PALIMPSEST_OCI_NETWORK_REDIS_IMAGE=/path/redis-7-alpine.oci.tar
PALIMPSEST_OCI_NETWORK_NGINX_IMAGE=/path/nginx-unprivileged.oci.tar
```

Its three nodes prove, with real traffic rather than status fields:

1. `nat` plus a published loopback port: the host performs one health request
   and two identical inference requests against the guest HTTP service, and the
   guest completes one real HTTPS API request through NAT after resolving the
   name through the virtual network's DNS.
2. `host-only` plus a published loopback port: the host completes a Redis
   `PING`/`+PONG` exchange while the guest proves egress is absent. The
   evidence is the two TCP negatives — an external address and the virtual
   gateway — made with a probe that must first succeed against a reachable
   in-guest listener. The resolver legs are policy checks (PID 1 wrote no
   nameserver, and a name lookup must not resolve), not independent egress
   evidence.
3. `nat` plus an explicit `0.0.0.0` publication: the service answers on the
   host's own LAN address, and the listener is gone after owned removal.

Each node additionally re-checks the authored QEMU vector, the absence of any
libvirt-owned network device, `oci network` exposure output, authenticated
root identity, PID 1 access denial, owned `stop`/`rm`, and unchanged source
archives.

## Native results

At exact checkout `9ada8ca2aba2c40aa932a35d04a8379930bcc7e8` on `pieroot-server`
(libvirt 10.0.0, QEMU 8.2.2), the changed stage-1 ELF passed the 43-boot /
44-QEMU matrix in 122.23 seconds and all three networking nodes passed in
513.02 seconds.

- **NAT with a published loopback port** (`net-nat-service-31d0ee9e`): the host
  received HTTP 200 from `/healthz` (`pytorch 2.8.0+cu126`, device `cpu`, CUDA
  false) and from two identical `/infer` requests through the forwarded port,
  with request counters 1 and 2, shape `[1,128,256]`, finite output, and the
  same SHA-256
  `73cf2a3cfaf2e95a0962b15c4eb8d259760ad5bf3a370562ec2c9296a38dc464`. The guest
  then completed a real outbound API call: `NET_EGRESS_OK verified 1 200 26` —
  one address resolved through the virtual network's DNS, a
  certificate-verified TLS session, HTTP 200 from `https://api.github.com/meta`,
  and 26 published API ranges parsed.
- **host-only with a published loopback port** (`net-host-only-60435d58`): the
  host completed a real Redis exchange (`+PONG`) through the forwarded port
  while the guest proved `NET_NO_EGRESS_OK`: the controlled TCP probe reached
  the in-guest listener and then failed against both an external address and
  the virtual gateway, and no nameserver was present.
- **NAT with an explicit wildcard publication** (`net-external-9c14c501`): the
  service answered HTTP 200 on the host's own LAN address `172.31.0.60:50711`,
  and that listener refused connections after the proof-owned removal.

Every node also verified the authored QEMU argument vector against the durable
plan, the absence of any libvirt-owned interface, hostdev or host filesystem,
the `oci network` exposure projection, authenticated root identity, PID 1
access denial, `stopped`/`removed` for its own run, and unchanged source
archives. Every named libvirt domain and all pinned archive hashes were
identical before and after; the separately preserved running
`ml-pytorch-ed03b448` was untouched. This qualifies these three exact images,
modes and publications on this host. It does not qualify IPv6, privileged host
ports, VM-to-VM networking, or any other image.

Three earlier native attempts failed and produced the fixes above: libvirt
aborted domain start because an unaddressed passthrough NIC claimed PCI slot
`0x1` ahead of libvirt's own root port; PID 1 then rejected the workload at the
link-state check because carrier was not yet reported; and it next rejected the
default-route check because the kernel prints `/proc/net/route` in uppercase
hexadecimal. Those runs are not networking qualification.

The first host-only node was also fail-open: a missing or unusable guest
`getent`/`nc` would have printed the isolation marker without testing
egress. The probe now refuses unless the tools exist, proves the TCP probe
against a reachable in-guest listener, rejects a present nameserver, fails
distinctly on any unexpected state, and requires `NET_NO_EGRESS_OK` as the
exact sole output. Its first fail-closed rerun passed in 40.37 seconds as
`net-host-only-e388c8bf` at `e5bc4f74ac144b03d45fbc9ebf50a0a7c439bc0c`, which
still used the `/etc/hosts`-only resolver control. That control was dropped
because it proved only that the binary runs, and the current form — including
the present-nameserver rejection — was re-run natively at exact
`ed4d7d18d205f8d00d2ec188ac1ef3d9c30cef55`: `net-host-only-f1dfd72f` passed in
38.66 seconds with a real `+PONG` through the published port,
`NET_NO_EGRESS_OK`, and owned stop/remove.
