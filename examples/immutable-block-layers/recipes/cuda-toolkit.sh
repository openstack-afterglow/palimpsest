# CUDA toolkit (userspace only, no kernel driver) from NVIDIA's network repository.
# The driver is not part of an image layer: it has to match the kernel that runs the GPU.
CUDA_VERSION=${CUDA_VERSION:-12-6}
case $(dpkg --print-architecture) in
  amd64) repo=x86_64 ;;
  arm64) repo=sbsa ;;
  *) echo "unsupported architecture" >&2; exit 1 ;;
esac
apt-get update -q
apt-get install -y -q --no-install-recommends ca-certificates curl
curl -fsSL -o /tmp/cuda-keyring.deb "https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/${repo}/cuda-keyring_1.1-1_all.deb"
dpkg -i /tmp/cuda-keyring.deb
rm -f /tmp/cuda-keyring.deb
apt-get update -q
apt-get install -y -q --no-install-recommends "cuda-toolkit-${CUDA_VERSION}"
printf '/usr/local/cuda/lib64\n' >/etc/ld.so.conf.d/cuda.conf
ldconfig
printf 'export PATH=/usr/local/cuda/bin:$PATH\n' >/etc/profile.d/cuda.sh
apt-get clean
rm -rf /var/lib/apt/lists/*
