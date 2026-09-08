# Explicit OCI run users

`palimpsest run` accepts `--user USER[:GROUP]` for local OCI-root images only.
This is an explicit launch-identity override, not an image rewrite or a
privilege option. Omitting it preserves the image's configured process.

```sh
palimpsest run ./redis.oci.tar --runtime-kind oci-root \
  --name redis-demo --user redis --memory 512 --vcpus 1 --network none -d
```

The archive must already exist locally and satisfy the ordinary source and
host qualification checks. Registry references are not accepted directly.
The example is not a claim that every Redis image is compatible.

## Identity and isolation

Names and canonical unsigned numeric IDs use the existing bounded OCI user
parser. A group can be a name or numeric ID. The explicit value is limited to
65 characters before numeric conversion. Empty values, whitespace,
noncanonical numbers and malformed separators are rejected. Names resolve
against the guest image's account files, not the host's NSS or account files;
missing or ambiguous accounts fail closed. Supplementary groups remain empty.
Specifying a user without a group retains the existing image-root primary
group resolution semantics, not a promise of full Docker group behavior.

Stage-1 selects the effective UID/GID before executing the original
entrypoint. It then removes all workload capabilities and retains locked
securebits, no-new-privileges, seccomp and PID 1 protection. Selecting UID 0
does not grant capabilities. The option does not change argv, environment,
working directory, stop signal, image bytes or materialization receipts.

No automatic chown or chmod accompanies the override. A non-root process may
lack access to image or retained-root data paths. Retained roots remain
VM-exclusive; changing the launch user does not transfer file ownership or
enable multi-VM shared writers.

## Durable process binding

Default runs retain the existing boot-plan v2 representation. An explicit
override uses v3 with the original `image_process` and canonical
`user_override` in `process_provenance`, alongside the effective `process`.
The preparation decoder recomputes the user-only process and rejects
inconsistent or unknown fields. The complete boot-plan digest includes this
provenance and feeds the existing lease, domain-core and guest plan bindings.
Materialization receipts and lower-graph/cache identity stay unchanged.
These checks are local integrity contracts, not protection from a malicious
host rewriting all authority or an independent image-signature guarantee.

## Compatibility evidence

The original Redis default-process proof remains a separate test. Its
previous entrypoint failure is not reclassified by an explicit-user run.
The explicit-user proof separately checks service readiness, public exec,
effective identity, empty capabilities and active isolation, actual app root
against authenticated PID 1 root reports, direct PID 1 access refusal, normal
stop/removal and unchanged source hashes. See [test selection](testing.md).
Successful implementation or portable tests alone are not native service
qualification or a new full Gate 2 result.
