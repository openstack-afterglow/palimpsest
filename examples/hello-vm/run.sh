#!/usr/bin/env bash
set -euo pipefail

# Resolve this script's real path even when invoked through a symlink, then
# walk up to the repository root so the script works from any cwd.
resolve_script_dir() {
  local source="${BASH_SOURCE[0]}"
  while [[ -h "$source" ]]; do
    local dir
    dir="$(cd -P "$(dirname "$source")" && pwd)"
    source="$(readlink "$source")"
    [[ "$source" != /* ]] && source="$dir/$source"
  done
  cd -P "$(dirname "$source")" && pwd
}

script_dir="$(resolve_script_dir)"
repo_root="$(cd "$script_dir/../.." && pwd -P)"
program_name="$(basename "$0")"

log() {
  echo "==> $*"
}

die() {
  echo "error: $*" >&2
  exit 1
}

if [[ $# -lt 1 ]]; then
  echo "usage: $program_name IMAGE_PATH [RUN_NAME]" >&2
  echo "  IMAGE_PATH  path to a local Ubuntu cloud image in qcow2 format" >&2
  echo "  RUN_NAME    name for the VM run (default: hello-vm)" >&2
  exit 1
fi

image_path="$1"
run_name="${2:-hello-vm}"

# Early check: the image file must exist before we do anything else.
[[ -f "$image_path" ]] || die "image not found: $image_path"

# Early check: the run name must satisfy Palimpsest's run-name grammar.
[[ "$run_name" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] \
  || die "invalid run name '$run_name': must match ^[a-z0-9][a-z0-9-]{0,62}\$"

# Early check: only known-working host/architecture combinations are
# supported by the local runtime. Reject everything else up front instead of
# failing deep inside a build or boot.
host_system="$(uname -s)"
case "$(uname -m)" in
  arm64 | aarch64) host_machine="aarch64" ;;
  x86_64 | amd64) host_machine="x86_64" ;;
  *) host_machine="$(uname -m)" ;;
esac

case "$host_system:$host_machine" in
  Darwin:aarch64)
    arch="aarch64"
    backend="lima-vz"
    ;;
  Linux:x86_64)
    arch="x86_64"
    backend="kvm"
    ;;
  Linux:aarch64)
    arch="aarch64"
    backend="kvm"
    ;;
  *)
    die "no local runtime can boot a VM on ${host_system}/${host_machine}; supported combinations are Linux x86_64, Linux aarch64, and macOS arm64 (Lima)"
    ;;
esac

log "Host: $host_system/$host_machine -> arch=$arch backend=$backend"

recipe_dir="$(mktemp -d "${TMPDIR:-/tmp}/palimpsest-hello-vm.XXXXXX")"
trap 'rm -rf -- "$recipe_dir"' EXIT

# Derive a Palimpsest tag from the run name, bounded to the 64-character tag
# limit (tags match ^[a-z0-9][a-z0-9.+-]{0,63}$).
tag_suffix="-layer"
max_tag_len=64
max_name_len=$((max_tag_len - ${#tag_suffix}))
layer_tag="${run_name:0:max_name_len}${tag_suffix}"
[[ ${#layer_tag} -le $max_tag_len ]] \
  || die "internal error: derived tag exceeds ${max_tag_len} characters: $layer_tag"

log "Importing cloud image: $image_path"
base_digest="$(uv run --project "$repo_root" palimpsest image import "$image_path" --disk-format qcow2 --arch "$arch")"
log "Imported base image digest: $base_digest"

palimpsestfile="$recipe_dir/Palimpsestfile"
cat >"$palimpsestfile" <<EOF
FROM $base_digest
WORKDIR /opt/hello-vm
RUN echo "Hello from a real Palimpsest VM" > message.txt
RUN uname -m > arch.txt
EOF

log "Building SquashFS layer (tag: $layer_tag, network: none)"
layer_digest="$(uv run --project "$repo_root" palimpsest build \
  --frontend palimpsestfile \
  --base "$base_digest" \
  --tag "$layer_tag" \
  --file "$palimpsestfile" \
  --network none)"
log "Built layer digest: $layer_digest"

log "Starting VM '$run_name' (backend: $backend, 2048 MiB, 2 vCPUs, network: default)"
run_endpoint="$(uv run --project "$repo_root" palimpsest run "$base_digest" \
  --name "$run_name" \
  --layer "$layer_digest" \
  --memory 2048 \
  --vcpus 2 \
  --network default \
  --backend "$backend")"
log "VM is running: $run_endpoint"

log "Verifying live guest architecture (uname -m)"
uv run --project "$repo_root" palimpsest exec "$run_name" -- uname -m

log "Verifying built layer message (/opt/layers/merged/opt/hello-vm/message.txt)"
uv run --project "$repo_root" palimpsest exec "$run_name" -- cat /opt/layers/merged/opt/hello-vm/message.txt

log "Verifying build-time captured architecture (/opt/layers/merged/opt/hello-vm/arch.txt)"
uv run --project "$repo_root" palimpsest exec "$run_name" -- cat /opt/layers/merged/opt/hello-vm/arch.txt

log "palimpsest ps"
uv run --project "$repo_root" palimpsest ps

cat <<EOF

The VM '$run_name' is left running. Useful commands:
  uv run --project "$repo_root" palimpsest shell $run_name
  uv run --project "$repo_root" palimpsest exec $run_name -- <command>
  uv run --project "$repo_root" palimpsest inspect $run_name
  uv run --project "$repo_root" palimpsest logs $run_name

Cleanup when done:
  uv run --project "$repo_root" palimpsest stop $run_name
  uv run --project "$repo_root" palimpsest rm $run_name --volumes
EOF
