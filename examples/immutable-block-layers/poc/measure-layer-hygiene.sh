#!/usr/bin/env bash
# Measures what trimming and journal handling are worth for one recipe: the same recipe is
# applied three times on the same base stack and committed (a) as the filesystem left it,
# (b) after fstrim, (c) after fstrim with the journal dropped during the build and re-created
# empty afterwards (what `blkstack build-layer` does). Prints the committed layer sizes.
# Disposable builder only; needs a stack built by poc/run-local-poc.sh or build-base.
#
#   sudo BLKSTACK_ROOT=/var/lib/blkstack-poc/builder poc/measure-layer-hygiene.sh STACK RECIPE
set -euo pipefail

STACK=${1:?stack}; RECIPE=${2:?recipe}
HERE=$(cd "$(dirname "$0")/.." && pwd)
B=$HERE/bin/blkstack
ROOT=${BLKSTACK_ROOT:-/var/lib/blkstack}
MNT=$(mktemp -d)

partition() { # the root partition of an attached session's device
  local kernel; kernel=$(basename "$(readlink -f "$1")")
  [ -n "$(ls "/sys/block/$kernel/holders")" ] || kpartx -as "$1" >/dev/null
  udevadm settle
  for h in "/sys/block/$kernel/holders"/*; do
    [ "$(blkid -p -o value -s LABEL "/dev/mapper/$(cat "$h/dm/name")" 2>/dev/null || true)" != cloudimg-rootfs ] || echo "/dev/mapper/$(cat "$h/dm/name")"
  done
}
apply() { # PARTITION: run the recipe in a chroot, as build-layer does
  mount -o noatime "$1" "$MNT"
  mount --bind /dev "$MNT/dev"; mount -t proc proc "$MNT/proc"; mount -t tmpfs tmpfs "$MNT/run"
  mkdir -p "$MNT/run/systemd/resolve"; cp -L /etc/resolv.conf "$MNT/run/systemd/resolve/stub-resolv.conf"
  install -m 0755 "$RECIPE" "$MNT/tmp/recipe.sh"
  chroot "$MNT" /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin DEBIAN_FRONTEND=noninteractive HOME=/root /bin/bash -euo pipefail /tmp/recipe.sh >/dev/null 2>&1
  rm -f "$MNT/tmp/recipe.sh"
}
finish() { umount "$MNT/run" "$MNT/proc" "$MNT/dev" "$MNT"; }

for variant in as-left fstrim fstrim+journal; do
  session=hygiene-$RANDOM; "$B" session-create "$STACK" "$session" >/dev/null 2>&1
  dev=$("$B" attach "$session"); part=$(partition "$dev"); journal=''
  if [ "$variant" = fstrim+journal ]; then
    journal=$(dumpe2fs -h "$part" 2>/dev/null | awk -F: '/^Total journal blocks/ { gsub(/[^0-9]/, "", $2); print $2 * 4096 / 1048576 }')
    tune2fs -O ^has_journal "$part" >/dev/null
  fi
  apply "$part"
  sync   # also commits the journal: ext4 does not discard blocks freed in a transaction that is still open
  [ "$variant" = as-left ] || fstrim "$MNT"
  sync; finish
  [ -z "$journal" ] || tune2fs -J "size=$journal" "$part" >/dev/null
  "$B" detach "$session"
  "$B" commit "$session" --name "hygiene:$session" >/dev/null 2>&1
  printf '%s\t%s bytes\n' "$variant" "$("$B" inspect "hygiene:$session" | jq '.layers[-1].size')"
  "$B" session-rm "$session"; rm -f "$ROOT/stacks/hygiene__$session.json"
done
rmdir "$MNT"
