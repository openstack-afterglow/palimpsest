#!/usr/bin/env bash
# Proof run of the compute-node path without Nova: a libvirt domain whose root disk is
# /dev/blkstack/<volume-id> is started with `virsh start`; the qemu hook asks the node agent,
# the agent assembles the device from the delta image's recorded stack, the guest boots from
# it, and when the guest powers off the hook has the device torn down again.
#
# It stands in for what Nova would do with a Cinder volume of connection type `local`; Nova
# and Cinder themselves are not part of this run. Run it after poc/run-local-poc.sh passed on
# the same disposable host (it reuses that run's published layers and node state).
#
#   sudo poc/run-libvirt-hook-poc.sh [STACK]        # default ubuntu2204:dev1
set -euo pipefail

STACK=${1:-ubuntu2204:dev1}
HERE=$(cd "$(dirname "$0")/.." && pwd)
BASE=/var/lib/blkstack-poc
OUT=$BASE/out-hook
LAYER_POOL=${POC_LAYER_POOL:-lib-layers}
DELTA_POOL=${POC_DELTA_POOL:-user-delta}
NODE="rbd --id blkstack-node"
DOMAIN=blkstack-hook-poc
VOLUME=$(cat /proc/sys/kernel/random/uuid)
MISSING=$(cat /proc/sys/kernel/random/uuid)
MARKER=BLKSTACK_HOOK_POC_OK
CONSOLE=/var/lib/libvirt/qemu/$DOMAIN-console.log
SEED=/var/lib/libvirt/images/$DOMAIN-seed.img

mkdir -p "$OUT"
say() { printf '\n== %s\n' "$*"; }
cleanup() {
  virsh destroy "$DOMAIN" >/dev/null 2>&1 || true
  virsh undefine "$DOMAIN" --nvram >/dev/null 2>&1 || true
  virsh undefine "$DOMAIN-missing" --nvram >/dev/null 2>&1 || true
  rm -f "$SEED"
  BLKSTACK_ROOT=$BASE/node BLKSTACK_RBD=$NODE "$HERE/bin/blkstack" node-detach --missing-ok --instance "$VOLUME" >/dev/null 2>&1 || true
  rbd rm "$DELTA_POOL/volume-$VOLUME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

say "1. install libvirt, the node agent and the hook"
command -v virsh >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y -q libvirt-daemon-system libvirt-clients >/dev/null
install -m 0755 "$HERE/bin/blkstack" /usr/local/bin/blkstack
install -D -m 0755 "$HERE/compute/blkstack-agent-handler" /usr/local/libexec/blkstack-agent-handler
install -m 0644 "$HERE/compute/blkstack-agent.socket" "$HERE/compute/blkstack-agent@.service" /etc/systemd/system/
mkdir -p /etc/blkstack
cat >/etc/blkstack/node.env <<EOF
BLKSTACK_ROOT=$BASE/node
BLKSTACK_LAYER_POOL=$LAYER_POOL
BLKSTACK_DELTA_POOL=$DELTA_POOL
BLKSTACK_RBD="$NODE"
EOF
systemctl daemon-reload; systemctl enable --now blkstack-agent.socket >/dev/null
install -D -m 0755 "$HERE/compute/libvirt-hook-qemu" /etc/libvirt/hooks/qemu
systemctl restart libvirtd   # libvirt looks for hook scripts when the daemon starts

say "2. what the Cinder backend would have created: an empty delta image that records its stack"
# Created here by hand with the size and features the driver sketch aims for; the sketch
# itself (openstack/cinder_blkstack_driver.py) is not exercised by this run.
stack=$(BLKSTACK_ROOT=$BASE/node "$HERE/bin/blkstack" inspect "$STACK" | jq -cS 'del(.name, .source)')
size_gb=$(jq -r '.virtualSizeBytes / 1073741824 | floor' <<<"$stack")
$NODE create --size "$((size_gb + size_gb / 16 + 1))G" --image-feature layering,exclusive-lock,object-map,fast-diff "$DELTA_POOL/volume-$VOLUME"
$NODE image-meta set "$DELTA_POOL/volume-$VOLUME" blkstack.stack "$stack"

say "3. a domain whose root disk is /dev/blkstack/$VOLUME"
cat >"$OUT/user-data" <<EOF
#cloud-config
runcmd:
  - [sh, -c, 'echo "$MARKER \$(cat /opt/blkstack/layer-poc-tools) \$(lsblk -dno SIZE /dev/vda)" > /dev/console']
power_state: {mode: poweroff, delay: now, timeout: 60}
EOF
printf 'instance-id: %s\nlocal-hostname: hookpoc\n' "$VOLUME" >"$OUT/meta-data"
cloud-localds "$SEED" "$OUT/user-data" "$OUT/meta-data"
case $(uname -m) in aarch64) machine=virt; extra="<gic version='host'/>" ;; *) machine=q35; extra='' ;; esac
domain_xml() { # NAME VOLUME_ID
  cat <<EOF
<domain type='kvm'>
  <name>$1</name>
  <memory unit='MiB'>2048</memory>
  <vcpu>2</vcpu>
  <os firmware='efi'><type arch='$(uname -m)' machine='$machine'>hvm</type></os>   <!-- the image has an ESP on both architectures -->
  <features><acpi/>$extra</features>
  <cpu mode='host-passthrough'/>
  <on_poweroff>destroy</on_poweroff>
  <devices>
    <disk type='block' device='disk'>
      <driver name='qemu' type='raw' cache='none' io='native' discard='unmap'/>
      <source dev='/dev/blkstack/$2'/>
      <target dev='vda' bus='virtio'/>
      <serial>$2</serial>
    </disk>
    <disk type='file' device='disk'>
      <driver name='qemu' type='raw'/>
      <source file='$SEED'/>
      <target dev='vdb' bus='virtio'/>
      <readonly/>
    </disk>
    <interface type='user'><model type='virtio'/></interface>
    <serial type='file'><source path='$CONSOLE'/><target port='0'/></serial>
    <console type='file'><source path='$CONSOLE'/><target type='serial' port='0'/></console>
  </devices>
</domain>
EOF
}
domain_xml "$DOMAIN" "$VOLUME" >"$OUT/domain.xml"
virsh define "$OUT/domain.xml" >/dev/null
[ ! -e "/dev/blkstack/$VOLUME" ] || { echo "device exists before the domain starts" >&2; exit 1; }

say "4. virsh start: the prepare hook has the device assembled, the guest boots from it"
virsh start "$DOMAIN"
[ -b "/dev/blkstack/$VOLUME" ] || { echo "the hook did not assemble the device" >&2; exit 1; }
rbd device list | tee "$OUT/rbd-mapped-running.txt"
for _ in $(seq 150); do [ "$(virsh domstate "$DOMAIN")" = "shut off" ] && break; sleep 2; done
[ "$(virsh domstate "$DOMAIN")" = "shut off" ] || { echo "guest did not power off" >&2; tail -n 30 "$CONSOLE" >&2; exit 1; }
grep -a "$MARKER" "$CONSOLE" | tail -n 1 | tee "$OUT/guest-marker.txt"
grep -aq "$MARKER layer=poc-tools" "$CONSOLE" || { echo "guest did not report the layer content" >&2; exit 1; }

say "5. after the guest stopped: the release hook had the device torn down"
for _ in $(seq 30); do [ ! -e "/dev/blkstack/$VOLUME" ] && break; sleep 1; done
[ ! -e "/dev/blkstack/$VOLUME" ] || { echo "device still present after release" >&2; exit 1; }
[ -z "$(rbd device list --format json | jq -r '.[].name')" ] || { echo "an RBD mapping was left behind" >&2; exit 1; }
$NODE du --exact "$DELTA_POOL/volume-$VOLUME" | tee "$OUT/delta-usage.txt"

say "6. a volume without a delta image: prepare fails and libvirt refuses to start the guest"
domain_xml "$DOMAIN-missing" "$MISSING" >"$OUT/domain-missing.xml"
virsh define "$OUT/domain-missing.xml" >/dev/null
if virsh start "$DOMAIN-missing" 2>"$OUT/start-refused.txt"; then echo "a guest started without its disk" >&2; exit 1; fi
cat "$OUT/start-refused.txt"
journalctl -t blkstack-agent --since "-2 min" --no-pager | tail -n 3 | tee "$OUT/agent-journal.txt"
[ ! -e "$BASE/node/sessions/node-$MISSING" ] && [ ! -e "/dev/blkstack/$MISSING" ] || { echo "the refused start left state behind" >&2; exit 1; }
[ -z "$(rbd device list --format json | jq -r '.[].name')" ] || { echo "the refused start left an RBD mapping behind" >&2; exit 1; }

echo "HOOK PROOF RUN PASSED"
