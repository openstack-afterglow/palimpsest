# OCI run command override

`palimpsest run` accepts a literal command override for local OCI-root images:

```console
palimpsest run ./pytorch.oci.tar --runtime-kind oci-root \
  --name pytorch-dev --memory 8192 --vcpus 4 --network none -d -- \
  /bin/sh -c 'while :; do sleep 3600; done'
```

The first `--` terminates Palimpsest options. At least one command element must
follow it. Every following element is passed as literal `execve` argv: Palimpsest
does not split strings, expand variables, invoke a shell, or inherit host
environment. Shell behavior occurs only when the caller explicitly selects a
shell as shown above.

## Docker-compatible boundary

The override replaces only the image `Cmd`. The descriptor-verified image
`Entrypoint` is retained exactly and prepended to the supplied command. Images
whose original Entrypoint and Cmd are both empty remain rejected during current
image intake; a command override does not broaden that existing subset.

Command override is local OCI-root-only. Cloud-image runs reject it. It can be
combined with the existing `--user USER[:GROUP]`, but it does not change image
environment, working directory, stop signal, filesystem ownership, capabilities,
supplementary groups, networking, or VM XML. It adds no default keepalive,
interactive terminal, pseudo-TTY, stdin attachment, GPU access, or device
permission.

## Durable binding

Default runs retain boot-plan v2 and user-only runs retain v3. A command override
uses v4. During plan construction Palimpsest reopens the exact image config blob
from the trusted source CAS using the materialization receipt's config descriptor,
verifies its bounded bytes, reparses the original Entrypoint and Cmd, and requires
their reconstructed process to equal the materialized image process.

The v4 plan binds the original process, original Entrypoint and Cmd, literal
command override, optional user override, and effective process in the existing
lease/domain-core/authenticated guest-plan digest chain. Recovery recomputes the
effective process and rejects missing, unknown, inconsistent, or oversized
fields. Materialization receipt v2 and immutable lower/cache identity are
unchanged. These are local integrity properties, not external attestation.
