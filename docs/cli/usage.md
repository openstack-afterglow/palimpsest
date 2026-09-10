# Palimpsest CLI usage guide

This guide explains what the commands do and where similarly named commands cross different trust and storage boundaries. The generated [syntax reference](reference.md) lists every parser-visible command, positional, option, choice, and default.

## Invocation and configuration

```sh
palimpsest [--url HUB_URL] COMMAND ...
palimpsest --version
palimpsest completion bash
```

`--url` is a global Hub URL override. Hub commands resolve the URL in this order: `--url`, `PALIMPSEST_URL`, then `config.toml` (`url` or `[hub].url`). They require `PALIMPSEST_TOKEN`; do not put tokens in examples or configuration committed to source control. Registry wrapper commands reuse Docker's existing config and credential directory: `DOCKER_CONFIG` when set, otherwise `$HOME/.docker`.

Options are parsed in the command position shown in the reference. `palimpsest docker ...` is special: everything after `docker` is passed to the Docker CLI. `palimpsest __complete` is an internal completion protocol, not a public command.

## Three image namespaces

Do not treat these similarly named surfaces as interchangeable:

| Surface | Examples | Backend and credentials |
| --- | --- | --- |
| Palimpsest Hub artifacts | `image ls`, `image pull`, `image push`, `layer *`, `bundle *` | Native Palimpsest Hub `/v1`; `PALIMPSEST_TOKEN` |
| Docker wrapper | `pull`, `push`, `tag`, `images`, `history`, `rmi`, `save`, `load`, and `image inspect/history/rm/save/load` | Installed Docker CLI and external OCI registries; Docker credentials |
| Local OCI root | `oci materialize`, `run ./image.oci.tar --runtime-kind oci-root` | Local OCI archive/layout only; private source CAS and Linux KVM runtime |

Palimpsest Hub is not an OCI Distribution `/v2` registry. A Docker Hub reference such as `docker.io/library/nginx:latest` cannot be passed directly to `palimpsest run`; first use an external tool to create a digest-preserving local OCI archive or layout, then run that local source.

## Hub cloud images, layers, and bundles

`image ls` queries cloud-image metadata and accepts server-side filters plus `--limit` (default 50, valid 1–200). `image pull DIGEST` verifies the Hub object is a complete cloud image, stores it locally, and optionally copies it beneath `--output`. `image verify PATH --digest DIGEST` checks local bytes. `image import` adds an existing qcow2/raw image to the local store; it does not upload it. `image push` uploads a local cloud image and requires `--name`; `--publish` controls Hub visibility.

```sh
palimpsest --url https://hub.example image ls --arch x86_64 --disk-format qcow2
palimpsest image pull "$IMAGE_DIGEST" --output ./downloads
palimpsest image import ./ubuntu.qcow2 --disk-format qcow2 --arch x86_64 --os-variant ubuntu24.04
palimpsest image push ./ubuntu.qcow2 --name ubuntu-dev --publish
```

`layer pack DIRECTORY --tag TAG` produces a local SquashFS layer with the fixed zstd recipe. `layer push VALUE` accepts a local tag/digest and optionally supplies Hub chain metadata; `--parent`, `--base-image`, and `--ubuntu-base` describe that chain. `layer ls` has the same 1–200 `--limit` restriction. `bundle pull` exports a Hub chain as an OCI image-layout directory and can include its base image. `bundle verify` verifies an extracted bundle directory.

```sh
palimpsest layer pack ./rootfs-overlay --tag app-v1
palimpsest layer push app-v1 --name app-v1 --base-image "$IMAGE_DIGEST"
palimpsest bundle pull "$LAYER_DIGEST" --output ./bundle --include-base
palimpsest bundle verify ./bundle
```

The Docker-compatible `image inspect`, `image history`, `image rm`, `image save`, and `image load` commands are aliases for Docker-backed operations; they do not inspect or mutate Hub `/v1` cloud-image records.

## Registry profiles and Docker wrapper commands

`registry add NAME ENDPOINT` records endpoint resolution, namespace, mirrors, CA files, transport flags, and BuildKit cache endpoints. The `docker` profile defaults its namespace to `library`; other names default to an empty namespace. `--default` selects the new profile, and `--force` replaces an existing profile. `registry buildkit-config --output FILE` refuses to overwrite unless `--force` is present and writes an owner-only file.

```sh
palimpsest registry add docker docker.io --namespace library --default
palimpsest registry add internal registry.example --ca /etc/company/company-ca.pem --mirror mirror.example
palimpsest registry ls --format json
palimpsest registry buildkit-config --output ./buildkitd.toml
```

`login`, `logout`, `pull`, `push`, and `tag` resolve short references through a selected registry profile. A positional login/logout server and `--registry` are mutually exclusive. `pull -a` and `push -a` require an untagged repository. `images`, `history`, `rmi`, `save`, and `load` preserve Docker CLI meanings. Repeat `--platform` where the reference permits multiple platform selections.

```sh
printf '%s\n' "$REGISTRY_PASSWORD" | palimpsest login --registry internal -u ci --password-stdin
palimpsest pull --registry docker nginx:1.27
palimpsest images --digests --filter dangling=false
palimpsest docker system df
```

The wrapper requires a working `docker` executable/daemon. `palimpsest docker` passes arguments through while pinning Docker's existing config directory. It rejects a competing Docker global `--config` and rejects `docker login --password`/`-p`; use `DOCKER_CONFIG` and `--password-stdin`.

## Build

`build` selects `palimpsestfile` or `dockerfile`; `auto` chooses the legacy frontend when `--base` or `--layer` is present, or when no context is given and the recipe is named `Palimpsestfile`; otherwise it chooses Dockerfile/BuildKit. Every build requires at least one `--tag`.

Legacy Palimpsestfile builds require exactly one tag and `--base`, reject a context positional, and accept `--layer` plus `--network`. They reject every Dockerfile/BuildKit-only option listed in the generated reference.

```sh
palimpsest build --frontend palimpsestfile -f Palimpsestfile \
  --base "$IMAGE_DIGEST" --layer "$LAYER_DIGEST" --tag app-layer
```

Dockerfile builds accept a context, multiple tags, `-f`, platform/target/build args, named local OCI contexts, registry and cache selection, output controls, and optional runtime-block production.

```sh
palimpsest build . -f Dockerfile -t example/app:dev \
  --platform linux/amd64 --build-arg MODE=dev \
  --local-image base=/srv/oci/base@"$MANIFEST_DIGEST" --load
palimpsest build . -t example/app:release --push \
  --runtime-tag app-runtime --runtime-base "$IMAGE_DIGEST" --runtime-block-size 131072 --runtime-push
```

Build restrictions enforced before execution:

- `--offline` requires `--network none` and rejects `--pull`, `--push`, `--runtime-push`, `--registry`, and external `--cache-from`/`--cache-to`.
- `--no-cache` is allowed only in offline mode; online builds must reuse Hub cache.
- `--runtime-tag` and `--runtime-base` must appear together; `--runtime-push` also requires both.
- `--local-image` uses `ALIAS=/absolute/layout@sha256:DIGEST`; the layout and pinned manifest graph are verified. `--runtime-block-size` defaults semantically to 131072 bytes and must be a power of two from 4096 through 1048576.
- `--output` is an OCI archive path, not arbitrary BuildKit exporter syntax; omission creates an invocation-unique archive beneath local build state.
- Dockerfile builds reject legacy `--base`/`--layer`; use `--runtime-base` and `--local-image` instead.
- `--progress` defaults to `plain`; build networking defaults to `none`.

## Run: cloud image versus OCI root

```sh
palimpsest run IMAGE_DIGEST --name dev --memory 4096 --vcpus 2
palimpsest run ./bundle --name bundled --layer "$LAYER_DIGEST"
palimpsest run ./app.oci.tar --runtime-kind oci-root --name app
palimpsest run ./app.oci.tar --runtime-kind oci-root --name app -d
```

Cloud-image runs accept a local/Hub image digest or a Palimpsest bundle directory. Extra `--layer` values must continue the verified SquashFS parent chain. Backend `auto` selects KVM on Linux and Lima/VZ on macOS; experimental `libvirt-hvf` must be selected explicitly. Conventional cloud runs attach the image and layers but do not pivot the guest root.

Local OCI-root runs accept a local OCI archive or image-layout path, not a registry reference. The public runtime is limited to Linux x86_64/amd64 KVM with `qemu:///system`, qualified absolute kernel/config/packer paths and digest proof, and `--network none`. It is capabilityless and has no network; it is not a general hostile-container sandbox.

Without `-d`, OCI `run` waits for the image workload, streams its output, and returns its exit status; it does not imply `palimpsest rm` or deletion of durable run state. `-d` returns the run name only after authenticated READY and leaves the VM running. Detach is OCI-root-only. `--manifest` pins an otherwise ambiguous root descriptor. `--user USER[:GROUP]` is OCI-root-only and changes the image user selection without adding capabilities, supplementary groups, ownership changes, or arbitrary argv/env/cwd overrides.

`--root-retention` defaults semantically to `delete`. `retain` preserves only the VM-exclusive writable OCI root volume during removal. It is not a shared data volume. Reuse requires its exact UUID through `--root-volume` and compatible lower graph/size/exclusive attachment.

Parser limits are 256–1,048,576 MiB for `--memory` (default 4096) and 1–256 for `--vcpus` (default 2).

## Runtime lifecycle and observation

`ps`, `inspect NAME`, `logs NAME [--follow]`, `start NAME`, `stop NAME`, and `rm NAME` act on managed runs. `inspect` emits the stable runtime record. `shell` requests an interactive runtime session; `exec NAME -- COMMAND...` is non-interactive and requires a command. `--completion-record PATH` asks OCI exec to persist its bounded completion record. Direct PID 1 access is refused.

```sh
palimpsest ps
palimpsest inspect app
palimpsest logs app --follow
palimpsest exec --completion-record ./exec.json app -- /usr/bin/id
palimpsest shell dev
palimpsest stop app
palimpsest rm app
```

`rm --volumes` is the conventional/project volume-removal request and is explicitly rejected for OCI-root runs; OCI root deletion/retention follows the owned run policy. OCI-root also rejects `start`, `shell`, and `commit`; use `exec` for additional processes and `stop`/`rm` for lifecycle control. `commit NAME --tag TAG` commits a conventional runtime layer.

OCI diagnostic commands emit JSON: `root-proof NAME`, `exec-status NAME`, `exec-record PATH`, `resource-status`, `root-volumes`, and `root-volume UUID`. `oci init-runtime PATH` securely creates a runtime parent and prints the `PALIMPSEST_STATE_HOME=...` assignment. `oci materialize SOURCE` snapshots and verifies a local archive/layout, converts layers with the selected SquashFS packer, and emits a receipt (or writes it to a new `--output` file). Its only platform is `linux/amd64`; timeout defaults to 300 seconds and must be positive and finite.

## Compose-shaped projects

Compose commands implement a strict `palimpsest.yml` subset, not arbitrary Docker Compose. Global project options precede the subcommand. Discovery checks `palimpsest.yml`, then `palimpsest.yaml`. `--project-directory` controls resource containment; `--env-file` is repeatable and must remain inside that directory. Project naming can come from `-p`, `PALIMPSEST_PROJECT_NAME`, or `COMPOSE_PROJECT_NAME`.

```sh
palimpsest compose -f palimpsest.yml config --services
palimpsest compose -p demo up -d api worker
palimpsest compose ps --format json
palimpsest compose logs -f api
palimpsest compose exec api -- /usr/bin/id
palimpsest compose port api 8080 --protocol tcp
palimpsest compose down --volumes
```

`up` rejects combining `--no-recreate` and `--force-recreate`. Without `-d`, it follows logs after reconciliation; with `-d`, it returns after startup. `exec` requires a command. Service filters are optional for `up`, `ps`, `logs`, and `stop`. Linux KVM project port publication and shared writable volumes are intentionally rejected by the current subset.

## Local store and UI

`store show` reports the selected roots and `store ls` inventories images/layers. `store rm DIGEST` always refuses referenced or leased objects; the accepted `--force` flag does not bypass those safety checks. `store move --to PATH` migrates the selected store; `--keep-source` retains the old bytes. `store set --to PATH` changes the configured location without moving existing data.

```sh
palimpsest store show --format json
palimpsest store ls --kind layer
palimpsest store move --to /srv/palimpsest/store --keep-source
palimpsest ui --port 8080 --no-browser
```

`ui --port 0` (the default) chooses an available port. Explicit ports must be 1024–65535. State roots are owner-only; Linux's default is `/var/lib/palimpsest` only when no env/config/XDG override is present. Existing legacy state is not silently migrated.
