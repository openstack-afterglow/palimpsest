#!/bin/sh
set -eu

toolchain='docker.io/library/gcc@sha256:a689e29bc3adf4663ef9a141d23081252764d1319c63f591a027bd6fd676f4c1'
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
output=${1:-"$repo_root/src/palimpsest_local/assets/oci-stage1-init.x86_64"}
output_dir=$(dirname -- "$output")
mkdir -p "$output_dir"

tmp_output="$output.tmp"
rm -f "$tmp_output"
docker run --rm --platform linux/amd64 \
    --network none \
    --read-only \
    --user "$(id -u):$(id -g)" \
    --env HOME=/tmp --env LANG=C --env LC_ALL=C --env TZ=UTC --env SOURCE_DATE_EPOCH=0 \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777 \
    --mount "type=bind,src=$repo_root/guest/stage1,dst=/src,readonly" \
    --mount "type=bind,src=$output_dir,dst=/out" \
    --entrypoint /usr/local/bin/gcc \
    "$toolchain" \
    -std=c11 -Os -nostdlib -static -fno-builtin -fno-ident \
    -fno-stack-protector -fno-unwind-tables \
    -fno-pie -no-pie -ffreestanding -fno-tree-loop-distribute-patterns -mno-red-zone \
    -ffile-prefix-map=/src=. -fdebug-prefix-map=/src=. -Wall -Wextra -Werror \
    -Wl,--build-id=none,-z,noexecstack,-s \
    -o "/out/$(basename -- "$tmp_output")" /src/init.c
${PYTHON:-python3} "$repo_root/scripts/seal_static_elf.py" "$tmp_output"
chmod 0644 "$tmp_output"
mv -f "$tmp_output" "$output"
printf '%s\n' "$output"
