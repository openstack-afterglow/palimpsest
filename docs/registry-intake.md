# Anonymous TLS OCI registry intake

`palimpsest oci pull` turns one fully qualified anonymously accessible registry reference into
a verified local OCI archive. It does not make a registry or Docker's mutable
image store a runtime authority: `palimpsest run` continues to accept the
result only through its existing local OCI archive boundary.

```sh
palimpsest oci pull quay.io/example/application:v1 \
  --output ./application.oci.tar
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
