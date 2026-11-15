# cuDNN 9 for CUDA 12. Needs the NVIDIA repository that recipes/cuda-toolkit.sh configures.
apt-get update -q
apt-get install -y -q --no-install-recommends cudnn9-cuda-12
apt-get clean
rm -rf /var/lib/apt/lists/*
