set -euo pipefail
mkdir -p /opt/layers/upper /opt/layers/work /opt/layers/merged
mkdir -p /mnt/palimpsest/lower0
DEV=/dev/disk/by-id/virtio-aaaaaaaaaaaaaaaaaaaa
for _ in $(seq 1 30); do [ -e "$DEV" ] && break; sleep 1; done
[ -e "$DEV" ] || { echo "layer disk missing: $DEV" >&2; exit 1; }
mount -t squashfs -o ro "$DEV" /mnt/palimpsest/lower0
mkdir -p /mnt/palimpsest/lower1
DEV=/dev/disk/by-id/virtio-bbbbbbbbbbbbbbbbbbbb
for _ in $(seq 1 30); do [ -e "$DEV" ] && break; sleep 1; done
[ -e "$DEV" ] || { echo "layer disk missing: $DEV" >&2; exit 1; }
mount -t squashfs -o ro "$DEV" /mnt/palimpsest/lower1
mkdir -p /mnt/palimpsest/lower2
DEV=/dev/disk/by-id/virtio-cccccccccccccccccccc
for _ in $(seq 1 30); do [ -e "$DEV" ] && break; sleep 1; done
[ -e "$DEV" ] || { echo "layer disk missing: $DEV" >&2; exit 1; }
mount -t squashfs -o ro "$DEV" /mnt/palimpsest/lower2
mount -t overlay overlay -o lowerdir=/mnt/palimpsest/lower2:/mnt/palimpsest/lower1:/mnt/palimpsest/lower0,upperdir=/opt/layers/upper,workdir=/opt/layers/work /opt/layers/merged
