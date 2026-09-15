#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd "$script_dir/../.." && pwd -P)"

tag="${1:-hello-example}"
rootfs_dir="$script_dir/rootfs"

echo "Packing SquashFS layer from: $rootfs_dir"
echo "Target tag: $tag"

digest="$(uv run --project "$repo_root" palimpsest layer pack "$rootfs_dir" --tag "$tag")"

echo "Layer packed successfully."
echo "Digest: $digest"
echo ""
echo "Listing local layer artifacts:"
uv run --project "$repo_root" palimpsest store ls --kind layer
