# Declarative multi-VM projects

`palimpsest compose` runs several Palimpsest VMs from a strict `palimpsest.yml`. The command shape follows Docker Compose where the VM model permits it, while keeping boot images, block devices, and guest provisioning explicit.

The default discovery order is `palimpsest.yml`, then `palimpsest.yaml`, in the current or `--project-directory` directory. An explicit `-f/--file` wins; a relative file name is resolved from the directory where the command was invoked. The effective project directory is an explicit `--project-directory`, otherwise the first project file's parent. It supplies the default project name and is the base for project resources such as bundles, service `env_file`, typed cloud-init files, and the default `.env`.

## Example

```yaml
version: "1"
name: demo

volumes:
  database:
    driver: block
    size: 20GiB

services:
  db:
    image: sha256:1111111111111111111111111111111111111111111111111111111111111111
    memory: 2GiB
    vcpus: 2
    volumes:
      - database:/var/lib/postgresql
    environment:
      DATABASE_PASSWORD: ${DATABASE_PASSWORD:?set DATABASE_PASSWORD}
    cloud_init:
      inline:
        packages: [postgresql]
        write_files:
          - path: /etc/demo-db.conf
            permissions: "0640"
            content: |-
              listen=127.0.0.1
        runcmd:
          - [systemctl, enable, --now, postgresql]

  api:
    image: sha256:2222222222222222222222222222222222222222222222222222222222222222
    layers:
      - sha256:3333333333333333333333333333333333333333333333333333333333333333
    memory: ${API_MEMORY:-2GiB}
    vcpus: 2
    depends_on: [db]
    ports:
      - "127.0.0.1:${API_PORT:-18080}:8080/tcp"
    env_file: [runtime.env]
    environment:
      APP_ENV: development
```

Set runtime-only values and reconcile the project:

```sh
export DATABASE_PASSWORD='from-a-secret-store'

palimpsest compose config --quiet
palimpsest compose up -d
palimpsest compose ps
palimpsest compose logs api
palimpsest compose exec api -- systemctl status demo-api
palimpsest compose port api 8080
palimpsest compose stop api
palimpsest compose up -d api
palimpsest compose down
```

`up` waits for each selected VM to finish layer activation and provisioning, then returns. `-d` is accepted for Docker Compose muscle memory; project execution is VM-oriented and does not currently attach an aggregate foreground log stream. Dependencies are included automatically and start first.

## Supported schema

Unknown fields and unsupported values are errors. They are never silently ignored.

### Services

Each service accepts:

- exactly one of `image: sha256:...` or `bundle: project-relative-directory`;
- ordered `layers: [sha256:...]` on top of the boot source;
- `memory` as MiB/GiB and integer `vcpus`;
- one `networks` attachment;
- named block `volumes` in short `name:/guest/path[:ro]` or long form;
- TCP `ports` in `[host_ip:]host_port:guest_port[/tcp]` or long form;
- `environment` and one or more project-relative `env_file` files;
- typed `cloud_init` using `packages`, `write_files`, and argv-form `runcmd`;
- `depends_on` with dependency-started ordering.

An ordinary registry reference such as `nginx:latest` is not a bootable VM image and is rejected. Build or import a bootable Palimpsest cloud image/runtime block first. A missing boot image may be pulled from Palimpsest Hub; referenced SquashFS layer digests and local bundles must already be present and verified locally. The current bundle manifest does not carry a trusted boot-architecture field, so `bundle:` is explicitly x86_64/KVM-only; Apple Silicon projects must use an `image:` digest whose Hub metadata declares `aarch64`.

`depends_on` means “the dependency VM reached Palimpsest readiness.” It does not imply an application health check.

### Interpolation and environment

String values support `${VAR}`, `${VAR:-default}`, `${VAR:?message}`, and `$$`. Single-quoted YAML values are literal. Resolution sources, from lower to higher precedence, are repeated `--env-file` files (or the project `.env` by default) and the current process environment. As in Docker Compose, a relative explicit `--env-file` is interpreted from the invocation directory. Unlike Docker Compose, Palimpsest then requires it to remain inside the effective project directory and rejects symlinks; this preserves the project's path-containment boundary. Paths and all typed fields are validated after interpolation.

Service `env_file` has a different purpose: its files populate the guest environment in declaration order, and inline `environment` wins. Resolved environment values are not copied into the project ledger or `compose config` output. They are written to the owner-only VM seed/config needed to provision that VM and remain until the run is removed. Palimpsest therefore rejects obvious literal secret material and secret-shaped environment keys must refer to external variables; this is not yet a dedicated secrets subsystem.

### Networks and ports

The implicit `default` network is supported. One existing external network can be selected with:

```yaml
networks:
  project-net:
    external: true
    name: existing-network
services:
  api:
    image: sha256:...
    networks: [project-net]
```

On Linux, the external name is a libvirt network. On macOS it is a Lima network. Managed custom network creation, multiple NICs, aliases, and service DNS are not in schema v1.

Published ports default to `127.0.0.1`; use `0.0.0.0` explicitly to expose one. Static TCP forwarding is implemented by Lima. `compose port` reads the binding recorded on the live, owner-verified VM rather than the current desired YAML, so editing a project without recreating its VM cannot report an unapplied endpoint. The current Linux libvirt `type=network` path deliberately rejects `ports` before creating a VM: libvirt NAT does not provide per-domain inbound DNAT, while its supported per-domain forwarding path uses a different `passt` interface model. Palimpsest will not install hidden host firewall rules.

### Persistent block volumes

Top-level volumes use `driver: block` and default to 10 GiB:

```yaml
volumes:
  data:
    driver: block
    size: 50GiB
```

Linux creates an owner-only sparse raw ext4 image and attaches it directly as `virtio-blk`. Lima creates a persistent standalone disk and attaches it through `additionalDisks`. A new Palimpsest-owned Lima disk may be formatted on its first boot only; the VM definition is then stopped, changed to `format: false`, verified, and restarted before `up` succeeds. Existing volumes are verified and never automatically reformatted. Block attachments, including read-only ones, are exclusive to one service because ext4 is not a clustered filesystem. Host bind mounts and NFS are intentionally unsupported.

Project schema v1 executes only Palimpsest-managed named volumes. An `external: true` volume declaration is rejected during the mutation-free `up` preflight on both backends; arbitrary external Lima disks cannot be proven safe against Lima's implicit initialization contract. `compose down` removes per-run overlays and VMs but preserves managed named volumes. `compose down --volumes` deletes only volumes whose exact Palimpsest ownership receipt, filesystem label, backend identity, and attachment checks match.

### Typed cloud-init

Palimpsest accepts a backend-neutral subset rather than arbitrary raw cloud-config:

```yaml
cloud_init:
  inline:
    packages: [curl]
    write_files:
      - path: /etc/example.conf
        permissions: "0644"
        content: "mode=${MODE:-safe}"
    runcmd:
      - [systemctl, restart, example]
```

Use `cloud_init: {file: cloud-init.yml}` for a project-contained file containing the same typed keys. Shell-string commands, symlinks/path escape, runtime-owned paths, duplicate output paths, and raw MIME/user-data are rejected. KVM runs the subset through NoCloud and reports ready only after it completes. Lima translates it to idempotent provision steps; those steps run before Palimpsest's SquashFS overlay is attached, so they must not depend on `/opt/layers/merged`.

## Lifecycle and state

`up` resolves and preflights every selected service, dependency, port, network, image, and volume before the first VM mutation. A project lock serializes reconciliation. The ledger binds each logical service to a deterministic run name, owner UUID, backend, and configuration fingerprint without storing resolved secrets. The same running configuration is a no-op; a stopped VM is normally restarted; configuration drift recreates it; and `--force-recreate` always recreates it. `--no-recreate` preserves an already-running VM, but deliberately refuses a stopped VM because safely reconstructing all of its applied backend configuration from changed YAML is not yet supported.

Do not edit a Palimpsest-owned libvirt domain or Lima instance directly. Immutable owner UUIDs, disk formatting flags, volume references, and destructive operations are checked fail-closed, but full reconciliation of every externally edited CPU, memory, and network field is a later compatibility phase. Long-running `compose exec` and `compose logs --follow` hold the project lifecycle lock so a concurrent recreate cannot redirect the operation to a replacement VM.

Project state and block-volume receipts live below `${XDG_STATE_HOME:-~/.local/state}/palimpsest/projects/` and `volumes/`. Rollback removes only VMs created by the failed `up`; persistent named volumes remain recoverable by default.

## Command coverage and current differences

```text
palimpsest compose [-f FILE] [-p NAME] [--project-directory DIR]
                    [--env-file FILE ...] config|up|down|ps|logs|exec|stop|port
```

- `config --quiet|--services`, `up [SERVICE...]`, `down [-v]`, `ps [SERVICE...]`, `logs [SERVICE...]`, `exec SERVICE -- COMMAND...`, `stop [SERVICE...]`, and `port SERVICE PRIVATE_PORT` are supported.
- `logs --follow` currently accepts exactly one service, avoiding a misleading sequential multi-VM follow.
- Scale/replicas, build sections, restart policies, health checks, secrets/config objects, develop/watch, swarm deploy fields, and Compose extension/merge features are not supported.
- YAML anchors, aliases, tags, merge keys, duplicate keys, and unknown schema keys fail closed.

The upstream behavior used as the compatibility reference is the [Compose Specification](https://compose-spec.io/), especially Docker's documentation for [services](https://docs.docker.com/reference/compose-file/services/), [networks](https://docs.docker.com/reference/compose-file/networks/), [volumes](https://docs.docker.com/reference/compose-file/volumes/), and [variable interpolation](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/). Guest provisioning follows cloud-init's [NoCloud datasource](https://cloudinit.readthedocs.io/en/latest/reference/datasources/nocloud.html) and [module](https://cloudinit.readthedocs.io/en/latest/reference/modules.html) contracts. The Linux port limitation follows libvirt's documented [`passt` user-interface forwarding](https://libvirt.org/formatdomain.html#userspace-connection-using-a-passt-backend), not its outbound-only NAT source-port controls.
