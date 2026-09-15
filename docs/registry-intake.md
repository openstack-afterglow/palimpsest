# Anonymous TLS OCI registry intake

`palimpsest oci pull` turns one fully qualified anonymously accessible registry reference into
a verified local OCI archive. It does not make a registry or Docker's mutable
image store a runtime authority: `palimpsest run` continues to accept the
result only through its existing local OCI archive boundary.

```sh
palimpsest oci pull ghcr.io/nginx/nginx-unprivileged:stable-alpine \
  --output ./nginx.oci.tar
palimpsest oci pull quay.io/prometheus/busybox:latest \
  --output ./busybox.oci.tar
```

The command requires Skopeo 1.13 or newer on `PATH`. It selects exactly
`linux/amd64`, requests digest-preserving OCI archive output, snapshots the
selected graph through Palimpsest's descriptor-verifying private source CAS,
and publishes the archive at mode `0600` only after verification. The JSON
result distinguishes the copied archive's root descriptor from the selected
platform manifest digest.

## Security and compatibility limits

- The reference must include an explicit registry authority and repository.
  Scheme prefixes, embedded credentials, implicit Docker Hub names, and
  all-tags acquisition are rejected.
- Registry access is anonymous HTTPS with certificate verification forced on
  against system trust. The caller chooses the endpoint: it may resolve to the
  public Internet, a private network, or the local host. Palimpsest applies no
  endpoint allowlist and the command therefore carries the caller's normal
  network reachability.
  Ambient Docker, Podman, Skopeo, and registry credential files are not used.
  Private registry authentication and custom registry CAs are not supported by
  this initial command.
- Only `linux/amd64` is supported. Palimpsest's existing OCI media-type,
  descriptor, platform, process, layer, archive, and source-CAS checks still
  apply, so successful registry transfer alone is insufficient.
- The default deadline is 300 seconds. A polled 8 GiB soft limit covers the
  archive plus Skopeo temporary, cache, configuration, and runtime files in the
  command-owned staging tree; writes can briefly overshoot between polls.
  Descriptor-verified source-CAS copies require additional local storage.
  The deadline covers the external copy, not subsequent archive/CAS
  verification and publication.
  Standard output and error from the external client are discarded rather than
  accumulated without a bound. The client runs in a dedicated process group,
  which is terminated on success or failure; as with any local process,
  kernel uninterruptible sleep can delay actual task and resource reclamation.
- The output parent must already exist, be owned by the caller, be free of
  group/world write permission, and contain no symbolic-link traversal. The
  command never replaces an existing output path and cleans up only its own
  private staging directory on failure.

[Skopeo's official copy documentation](https://github.com/containers/skopeo/blob/main/docs/skopeo-copy.1.md)
defines `--preserve-digests` as a fail-if-preservation-is-impossible copy mode.
Its `--src-no-creds` and `--src-tls-verify=true` flags make this command's
anonymous authentication and TLS boundary explicit.

## Verified checkpoint

At `de304ea8da1b6f8323b893e0c9fbd6e2442e668a`, the exact Linux checkout passed
76 focused tests and both commands above using Skopeo 1.13.3. Both archives
passed descriptor/platform/process/source-CAS checks, were published at mode
`0600`, and produced four valid command-journal records. Pre-existing domain
identities/states/XML remained unchanged, with no active VM or QEMU before or
after acquisition. The test used a private Skopeo installation with its
distribution-provided signature policy; TLS verification and anonymous access
remained enabled. This is not publisher-signature proof.

| Registry image | Selected Linux amd64 manifest | Archive bytes |
| --- | --- | ---: |
| `ghcr.io/nginx/nginx-unprivileged:stable-alpine` | `sha256:b8c179cd3c2ae222a873dd59fbae240fadc03836cae5198afc9e9c19919c3880` | 25,555,968 |
| `quay.io/prometheus/busybox:latest` | `sha256:d86ce8f332fdb3b84f73d2fb0953f61bc77374668bac32a15a8a79ec2ed8f0a9` | 1,134,080 |

These are successful real acquisitions, not fresh VM boots of those newly
downloaded archives. Tags may change; preserve the selected digest for
reproducible follow-up runs.

### Separate current-image VM checks

Immediately before the registry-only change, the unchanged guest/runtime at
`cfb80157e66bff3933eba9196b5c0925c2e7c1c8` was checked with existing pinned
Docker Hub archives. A 160-test focused gate passed. Results were:

| Case | Native result | Observed scope |
| --- | --- | --- |
| PostgreSQL 17, default process | Failed | Permission refusal during launch/readiness; no service probe |
| Redis 7 Alpine, default process | Failed, 17.46 s | `setpriv` / permission-refusal markers |
| NGINX stable Alpine, default process | Failed, 20.22 s | `chown` / permission-refusal markers |
| Redis 7 Alpine, explicit `--user redis` | Passed, 27.76 s | `PONG`, root/PID 1 checks and normal stop/remove |
| NGINX unprivileged, original process | Passed, 31.41 s | Detached/exec/root/PID 1 lifecycle proof, not HTTP qualification |

No workload or PID 1 permissions were relaxed. The three new failed domains
were preserved inactive; successful test resources were removed normally.
All original twelve archives and twelve historical domains were preserved,
leaving fifteen inactive domains and no active QEMU. Two private-wrapper
precondition/accounting errors were corrected and audited before continuation;
they do not convert any default-image failure into a pass. PostgreSQL was not
rerun after its failure. These results are separate from the acquisitions above
and do not establish universal container-image compatibility.
