# PyTorch from the official wheel index. The CUDA wheels carry their own CUDA runtime
# libraries, so this layer does not depend on the toolkit layer being present.
TORCH_INDEX=${TORCH_INDEX:-https://download.pytorch.org/whl/cu126}
apt-get update -q
apt-get install -y -q --no-install-recommends python3-pip python3-venv
python3 -m venv /opt/torch
/opt/torch/bin/pip install --no-cache-dir torch --index-url "$TORCH_INDEX"
apt-get clean
rm -rf /var/lib/apt/lists/* /root/.cache
