#!/usr/bin/env bash
# Build the layered ML stack and publish every stage to the OCI registry and to Ceph RBD.
#
#   layer 0  Ubuntu 22.04 cloud image (whole bootable disk)        -> TAG_PREFIX-base
#   layer 1  CUDA toolkit (userspace, no kernel driver)            -> TAG_PREFIX-cuda
#   layer 2  cuDNN                                                 -> TAG_PREFIX-cudnn
#   layer 3  PyTorch                                               -> TAG_PREFIX-torch
#
# Every stage is its own immutable stack, so a consumer can start from any of them, and a
# stage that already exists locally is not built again. Run on a disposable builder as root.
#
#   REGISTRY_REPO=registry.example.org/ml/ubuntu2204 LAYER_POOL=lib-layers \
#     pipeline/build-ml-stack.sh
#
# Environment:
#   REGISTRY_REPO   registry/repository to push to (log in first, as root: `sudo oras login REGISTRY`)
#   LAYER_POOL      RBD pool for layer images; empty skips the Ceph publication
#   BLKSTACK_RBD    rbd command with the publisher identity, e.g. "rbd --id blkstack-publisher"
#   SIZE_GB         virtual disk size of the stack (default 40)
#   STAGES          stages to build after the base (default "cuda cudnn torch")
#   PLAIN_HTTP=1    registry without TLS (test registries only)
#   ALLOW_UNVERIFIED_IMAGE=1   continue when the cloud image's signature cannot be checked
set -euo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)
B=$HERE/bin/blkstack
: "${REGISTRY_REPO:?set REGISTRY_REPO}"
LAYER_POOL=${LAYER_POOL:-}
SIZE_GB=${SIZE_GB:-40}
STAGES=${STAGES:-cuda cudnn torch}
NAME=${STACK_NAME:-ubuntu2204}
WORK=${WORK:-/var/cache/blkstack-build}
plain=(); [ "${PLAIN_HTTP:-0}" = 1 ] && plain=(--plain-http)

case $(uname -m) in x86_64) arch=amd64 ;; aarch64) arch=arm64 ;; *) echo "unsupported architecture" >&2; exit 1 ;; esac
image=jammy-server-cloudimg-$arch.img
url=https://cloud-images.ubuntu.com/jammy/current

mkdir -p "$WORK"; cd "$WORK"
if [ ! -f "$image.verified" ]; then
  curl -fsSL -O "$url/SHA256SUMS" -O "$url/SHA256SUMS.gpg" -o "$image" "$url/$image"
  # The checksum file is only as good as its signature. The key comes from the distribution's
  # own keyring package (ubuntu-cloudimage-keyring), not from a key server. A bad or missing
  # signature stops the build unless ALLOW_UNVERIFIED_IMAGE=1 says the risk is accepted.
  keyring=${CLOUDIMG_KEYRING:-/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg}
  if [ -f "$keyring" ] && gpgv --keyring "$keyring" SHA256SUMS.gpg SHA256SUMS 2>/dev/null; then
    echo "SHA256SUMS signature verified with $keyring"
  elif [ "${ALLOW_UNVERIFIED_IMAGE:-0}" = 1 ]; then
    echo "WARNING: SHA256SUMS signature NOT verified; continuing because ALLOW_UNVERIFIED_IMAGE=1" >&2
  else
    echo "SHA256SUMS signature could not be verified (install ubuntu-cloudimage-keyring, or set ALLOW_UNVERIFIED_IMAGE=1)" >&2
    exit 1
  fi
  grep " \*\?$image\$" SHA256SUMS | sha256sum -c -
  touch "$image.verified"
fi

publish() { # STACK TAG
  "$B" push "$1" "$REGISTRY_REPO:$2" "${plain[@]}"
  [ -z "$LAYER_POOL" ] || "$B" rbd-publish "$1" --pool "$LAYER_POOL"
}
exists() { "$B" inspect "$1" >/dev/null 2>&1; }
layers() { "$B" inspect "$1" | jq -c '[.layers[].digest]'; }
# A stage found from an earlier run is reused only if it sits exactly on the previous stage.
on_top_of() { [ "$(layers "$1" | jq -c '.[:-1]')" = "$(layers "$2")" ] || { echo "$1 exists but was built on other layers than $2; use another STACK_NAME" >&2; exit 1; }; }

"$B" init
exists "$NAME:base" || "$B" build-base --name "$NAME:base" --image "$WORK/$image" --size-gb "$SIZE_GB"
publish "$NAME:base" base

previous=base
for stage in $STAGES; do
  case $stage in
    cuda) recipe=cuda-toolkit.sh; title="CUDA toolkit" ;;
    cudnn) recipe=cudnn.sh; title="cuDNN 9 for CUDA 12" ;;
    torch) recipe=pytorch.sh; title="PyTorch" ;;
    *) echo "unknown stage: $stage" >&2; exit 1 ;;
  esac
  if exists "$NAME:$stage"; then on_top_of "$NAME:$stage" "$NAME:$previous"
  else "$B" build-layer --from "$NAME:$previous" --name "$NAME:$stage" --recipe "$HERE/recipes/$recipe" --title "$title"; fi
  publish "$NAME:$stage" "$stage"
  previous=$stage
done
"$B" ls
