# Small stand-in for a library layer: used by the local proof run on any architecture.
apt-get update -q
apt-get install -y -q --no-install-recommends jq
mkdir -p /opt/blkstack && printf 'layer=poc-tools\n' >/opt/blkstack/layer-poc-tools
apt-get clean
rm -rf /var/lib/apt/lists/*
