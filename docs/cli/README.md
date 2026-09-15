# Palimpsest command-line reference

- [Usage guide](usage.md): command intent, examples, namespace boundaries, validation rules, and runtime restrictions.
- [Generated syntax reference](reference.md): every parser-visible command, option, positional, choice, and default.

## Detailed workflows

- [Installation and operating identity](../install.md)
- [Cloud-image VM workflow](../vm-workflow.md)
- [Docker/OCI registry profiles](../registries.md)
- [Dockerfile builds, local image contexts, and cache](../buildkit-block-workflow.md)
- [Compose-shaped project schema](../projects.md)
- [OCI-root host prerequisites and public lifecycle](../oci-public-runtime-roadmap.md) (qualification history; current source and limitations take precedence)
- [OCI users and permission limits](../oci-run-user.md)
- [Root verification](../oci-root-proof.md) and [retained root inventory](../oci-retained-root-inventory.md)
- [Additional exec](../oci-additional-exec.md) and [durable completion records](../oci-exec-durable-record.md)
- [Linux storage and logging](../linux-storage-logging.md)

After changing `palimpsest_local.cli.build_parser`, regenerate and verify the syntax inventory in the project environment:

```sh
uv run python scripts/generate_cli_reference.py
uv run python scripts/generate_cli_reference.py --check
```

The generator's `--check` mode is read-only and exits nonzero when `reference.md` is missing or stale.
