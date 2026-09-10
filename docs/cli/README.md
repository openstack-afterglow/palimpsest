# Palimpsest command-line reference

- [Usage guide](usage.md): command intent, examples, namespace boundaries, validation rules, and runtime restrictions.
- [Generated syntax reference](reference.md): every parser-visible command, option, positional, choice, and default.

After changing `palimpsest_local.cli.build_parser`, regenerate and verify the syntax inventory in the project environment:

```sh
uv run python scripts/generate_cli_reference.py
uv run python scripts/generate_cli_reference.py --check
```

The generator's `--check` mode is read-only and exits nonzero when `reference.md` is missing or stale.
