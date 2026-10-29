"""Tracked, bounded OCI computations and an exact-guest rancher/k3s deployment.

These functions never provision or delete cloud resources. GPU and privileged
cluster profiles deliberately use the SDK reference lane: the API OCI chroot
cannot inject host NVIDIA drivers or provide a writable privileged cgroup mount.
A workload result is evidence from a successful process, not remote attestation;
the runner must also retain the process return code and exact image identity.
"""

from __future__ import annotations

import csv
import ipaddress
import json
import math
import re
import secrets
import textwrap
import time
from collections.abc import Callable

from .errors import ArtifactValidationError

_API_METHODS = ("api-import", "api-build", "api-reuse")
PROFILES = {
    name: {
        "description": description,
        "gpu": gpu,
        "cluster": name == "k3s",
        "image_keys": ("k3s", "k3s-probe") if name == "k3s" else (name,),
        "methods": ("openstack-container",) if gpu or name == "k3s" else (*_API_METHODS, "openstack-container"),
        "default_method": "openstack-container",
        "timeout_seconds": 600 if name == "k3s" else 300,
        "iterations": 10,
    }
    for name, description, gpu in (
        ("torch-cpu", "PyTorch CPU float32 matrix multiplication with complete numerical checks", False),
        ("torch-cuda", "Explicit CUDA PyTorch matmul, synchronized iterations and device allocation", True),
        ("cuda", "CUDA devel-image nvcc compilation and synchronized vector-add kernel", True),
        ("tensorflow-cpu", "TensorFlow explicit CPU matrix multiplication with no soft placement", False),
        ("tensorflow-cuda", "TensorFlow explicit GPU matmul and convolution with no CPU fallback", True),
        ("k3s", "Digest-pinned privileged rancher/k3s server and agents with real pod proofs", False),
    )
}
_IMAGE = re.compile(
    r"(?:(?:localhost|[a-z0-9][a-z0-9.-]*)(?::[0-9]{1,5})/)?"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?@sha256:[0-9a-f]{64}\Z"
)


def _integer(value, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ArtifactValidationError(f"scenario {label} must be an integer in {minimum}..{maximum}")
    return value


def _positive(value, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ArtifactValidationError(f"scenario {label} must be finite and positive")
    return float(value)


def _options(config: dict) -> dict:
    options = config.get("workload", {})
    if not isinstance(options, dict) or set(options) - {"iterations", "matrix_size", "timeout_seconds"}:
        raise ArtifactValidationError("unsupported scenario workload options")
    return {
        "iterations": _integer(
            options.get("iterations", 10), "iterations", 1, 64 if config.get("method", "").startswith("api-") else 1000
        ),
        "matrix_size": _integer(options.get("matrix_size", 256), "matrix size", 16, 2048),
        "timeout_seconds": _integer(options.get("timeout_seconds", 300), "workload timeout", 10, 1800),
    }


def _cluster_options(config: dict) -> dict:
    options = config.get("cluster", {})
    if not isinstance(options, dict) or set(options) - {"servers", "agents", "timeout_seconds", "probe_replicas"}:
        raise ArtifactValidationError("unsupported scenario cluster options")
    _integer(options.get("servers", 1), "cluster servers", 1, 1)
    agents = _integer(options.get("agents", 1), "cluster agents", 1, 15)
    return {
        "agents": agents,
        "timeout_seconds": _integer(options.get("timeout_seconds", 600), "cluster timeout", 60, 1800),
        "probe_replicas": _integer(options.get("probe_replicas", agents + 1), "probe replicas", 1, agents + 1),
    }


def validate_profile(config: dict) -> None:
    """Validate unsupported execution methods before the runner allocates a VM."""
    if not isinstance(config, dict) or config.get("scenario") not in PROFILES:
        raise ArtifactValidationError("unknown benchmark scenario")
    scenario = config["scenario"]
    profile = PROFILES[scenario]
    method = config.get("method", profile["default_method"])
    if method not in profile["methods"]:
        raise ArtifactValidationError(
            f"unsupported {scenario} method: use openstack-container; API OCI has no NVIDIA driver injection or privileged cluster support"
        )
    images = config.get("images", {})
    if not isinstance(images, dict):
        raise ArtifactValidationError("scenario images must be a mapping")
    for key in profile["image_keys"]:
        image = images.get(key)
        if not isinstance(image, str) or not _IMAGE.fullmatch(image):
            raise ArtifactValidationError(f"scenario images.{key} requires a digest-pinned OCI reference")
    if scenario == "cuda":
        tag = images["cuda"].split("@", 1)[0].rsplit("/", 1)[-1].partition(":")[2]
        if not re.search(r"(?:^|-)devel(?:-|$)", tag):
            raise ArtifactValidationError("CUDA kernel compilation requires an explicitly tagged devel image with nvcc")
    if scenario == "k3s":
        _cluster_options(config)
    else:
        options = _options(config)
        if method.startswith("api-"):
            command = _python_argv(scenario, options)
            if len(command) > 256 or any(len(item.encode()) > 4096 for item in command):
                raise ArtifactValidationError("workload argv exceeds the public instance result contract")


_PYTHON_PREAMBLE = r"""
import csv, json, math, os, signal, subprocess, time
for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "TF_NUM_INTRAOP_THREADS", "TF_NUM_INTEROP_THREADS"):
    os.environ[name] = "1"
signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError("workload deadline")))
signal.alarm(options["timeout_seconds"])
n = options["matrix_size"]
iterations = options["iterations"]
gpu = scenario.endswith("-cuda")
def nvidia_details():
    output = subprocess.check_output([
        "nvidia-smi", "--query-gpu=index,uuid,name,driver_version,memory.total,pci.bus_id",
        "--format=csv,noheader,nounits"
    ], text=True, timeout=15)
    rows = list(csv.reader(output.splitlines(), skipinitialspace=True))
    assert rows and all(len(row) == 6 for row in rows), "NVIDIA inventory absent"
    return [dict(zip(("index", "uuid", "name", "driver_version", "memory_mib", "pci_bus_id"), row)) for row in rows]
def emit(result):
    result.update(schema="palimpsest.workload-result.v1", scenario=scenario, success=True,
                  iterations=iterations, matrix_size=n, errors=0)
    print(json.dumps({"palimpsest_workload": result}, allow_nan=False, separators=(",", ":")), flush=True)
"""

_TORCH = r"""
import torch
assert torch.__version__
torch.set_num_threads(1)
if gpu:
    assert torch.cuda.is_available() and torch.cuda.device_count() > 0, "CUDA unavailable; CPU fallback forbidden"
    torch.cuda.set_device(0)
    device = torch.device("cuda:0")
    inventory = nvidia_details()
    properties = torch.cuda.get_device_properties(0)
else:
    device = torch.device("cpu")
# Keep numerical and executed-operation accounting identical across lanes.
torch.backends.cuda.matmul.allow_tf32 = False
a = torch.ones((n, n), dtype=torch.float32, device=device)
b = torch.full((n, n), 2.0, dtype=torch.float32, device=device)
c = torch.matmul(a, b)
if gpu:
    torch.cuda.synchronize(device)
samples = []
for _ in range(iterations):
    start = time.perf_counter()
    c = torch.matmul(a, b)
    if gpu:
        torch.cuda.synchronize(device)
    samples.append(time.perf_counter() - start)
assert c.device == device or (not gpu and c.device.type == "cpu")
error = float(torch.max(torch.abs(c - 2 * n)).item())
checksum = float(c.sum(dtype=torch.float64).item())
assert error == 0.0 and checksum == 2 * n ** 3, "matmul numerical mismatch"
elapsed = sum(samples)
operations = iterations * 2 * n ** 3
result = dict(device="cuda:0" if gpu else "cpu", framework="torch", framework_version=torch.__version__,
              tensor_device=str(c.device), dtype=str(c.dtype), shape=list(c.shape), checksum=checksum,
              expected_checksum=2 * n ** 3, max_abs_error=error, checked_elements=c.numel(),
              completed_elements=iterations * n * n, elapsed_seconds=elapsed, iteration_seconds=samples,
              operations=operations, operations_per_second=operations / elapsed)
if gpu:
    result.update(cuda_available=True, device_count=torch.cuda.device_count(), device_index=0,
                  device_name=properties.name, total_memory_bytes=properties.total_memory,
                  compute_capability=list(torch.cuda.get_device_capability(0)), cuda_runtime=torch.version.cuda,
                  cuda_allocated_bytes=torch.cuda.memory_allocated(0), synchronization_count=iterations + 1,
                  nvidia_smi=inventory)
emit(result)
"""

_TENSORFLOW = r"""
import tensorflow as tf
tf.config.threading.set_intra_op_parallelism_threads(1)
tf.config.threading.set_inter_op_parallelism_threads(1)
tf.config.set_soft_device_placement(False)
tf.config.experimental.set_synchronous_execution(True)
physical = tf.config.list_physical_devices("GPU")
if gpu:
    assert physical, "TensorFlow GPU unavailable; CPU fallback forbidden"
    tf.config.set_visible_devices(physical[0], "GPU")
    inventory = nvidia_details()
    details = tf.config.experimental.get_device_details(physical[0])
    assert tf.test.is_built_with_cuda(), "TensorFlow not built with CUDA"
target = "/GPU:0" if gpu else "/CPU:0"
with tf.device(target):
    a = tf.ones((n, n), dtype=tf.float32)
    b = tf.fill((n, n), tf.constant(2.0, dtype=tf.float32))
    if gpu:
        pixels = tf.ones((1, n, n, 1), dtype=tf.float32)
        kernel = tf.ones((3, 3, 1, 1), dtype=tf.float32)
    samples = []
    for _ in range(iterations):
        start = time.perf_counter()
        c = tf.linalg.matmul(a, b)
        c.numpy()  # Device-to-host completion is a synchronization, not launch timing.
        if gpu:
            convolution = tf.nn.conv2d(pixels, kernel, strides=1, padding="VALID")
            convolution.numpy()
        samples.append(time.perf_counter() - start)
    checksum = float(tf.reduce_sum(tf.cast(c, tf.float64)).numpy())
    error = float(tf.reduce_max(tf.abs(c - 2 * n)).numpy())
assert error == 0.0 and checksum == 2 * n ** 3, "matmul numerical mismatch"
assert c.device.upper().endswith("/DEVICE:" + ("GPU:0" if gpu else "CPU:0")), "unexpected tensor placement"
elapsed = sum(samples)
operations = iterations * (2 * n ** 3 + (18 * (n - 2) ** 2 if gpu else 0))
result = dict(device="cuda:0" if gpu else "cpu", framework="tensorflow", framework_version=tf.__version__,
              tensor_device=c.device, dtype=c.dtype.name, shape=list(c.shape), checksum=checksum,
              expected_checksum=2 * n ** 3, max_abs_error=error, checked_elements=n * n,
              completed_elements=iterations * n * n, elapsed_seconds=elapsed, iteration_seconds=samples,
              operations=operations, operations_per_second=operations / elapsed, soft_placement=False)
if gpu:
    assert convolution.device.upper().endswith("/DEVICE:GPU:0"), "convolution CPU fallback forbidden"
    conv_checksum = float(tf.reduce_sum(tf.cast(convolution, tf.float64)).numpy())
    conv_error = float(tf.reduce_max(tf.abs(convolution - 9)).numpy())
    assert conv_error == 0.0 and conv_checksum == 9 * (n - 2) ** 2, "convolution numerical mismatch"
    build = tf.sysconfig.get_build_info()
    result.update(cuda_available=True, device_count=len(physical), device_index=0,
                  device_name=details["device_name"], compute_capability=list(details["compute_capability"]),
                  cuda_runtime=str(build["cuda_version"]), cudnn_version=str(build["cudnn_version"]),
                  synchronization_count=iterations * 2, nvidia_smi=inventory,
                  convolution_device=convolution.device, convolution_shape=list(convolution.shape),
                  convolution_checksum=conv_checksum, convolution_max_abs_error=conv_error,
                  convolution_checked_elements=(n - 2) ** 2)
emit(result)
"""

_CUDA_SOURCE = r"""
#include <cuda_runtime.h>
#include <chrono>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>
static void check(cudaError_t e) { if (e != cudaSuccess) throw std::runtime_error(cudaGetErrorString(e)); }
static std::string quote(const std::string& s) {
    std::ostringstream out; out << '"';
    for (unsigned char c : s) {
        if (c == '"' || c == '\\') out << '\\' << c;
        else if (c == '\n') out << "\\n";
        else if (c == '\r') out << "\\r";
        else if (c == '\t') out << "\\t";
        else if (c < 32) out << "?";
        else out << c;
    }
    out << '"'; return out.str();
}
__global__ void add(float* out, int count) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < count) out[i] = float(i % 97 + 3 * (i % 31));
}
int main(int argc, char** argv) {
    try {
        if (argc != 2) throw std::runtime_error("missing NVIDIA inventory");
        std::ifstream input(argv[1]); std::ostringstream inventory; inventory << input.rdbuf();
        if (!input || inventory.str().empty()) throw std::runtime_error("empty NVIDIA inventory");
        int devices = 0, driver = 0, runtime = 0;
        check(cudaGetDeviceCount(&devices)); if (devices < 1) throw std::runtime_error("no CUDA devices");
        check(cudaSetDevice(0)); check(cudaDriverGetVersion(&driver)); check(cudaRuntimeGetVersion(&runtime));
        cudaDeviceProp p; check(cudaGetDeviceProperties(&p, 0));
        char pci[64]; check(cudaDeviceGetPCIBusId(pci, sizeof(pci), 0));
        const int count = MATRIX_SIZE * MATRIX_SIZE;
        float* output = nullptr; check(cudaMalloc(&output, count * sizeof(float)));
        cudaEvent_t begin, end; check(cudaEventCreate(&begin)); check(cudaEventCreate(&end));
        std::vector<float> host(count); std::vector<double> samples; double device_ms = 0;
        for (int iteration = 0; iteration < ITERATIONS; ++iteration) {
            auto started = std::chrono::steady_clock::now();
            check(cudaEventRecord(begin)); add<<<(count + 255) / 256, 256>>>(output, count);
            check(cudaGetLastError()); check(cudaEventRecord(end)); check(cudaDeviceSynchronize());
            float milliseconds = 0; check(cudaEventElapsedTime(&milliseconds, begin, end));
            device_ms += milliseconds;
            samples.push_back(std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count());
        }
        check(cudaMemcpy(host.data(), output, count * sizeof(float), cudaMemcpyDeviceToHost));
        double checksum = 0, expected = 0; int errors = 0;
        for (int i = 0; i < count; ++i) {
            float value = float(i % 97 + 3 * (i % 31)); expected += value; checksum += host[i];
            if (host[i] != value) ++errors;
        }
        if (errors) throw std::runtime_error("kernel numerical mismatch");
        double elapsed = 0; for (double sample : samples) elapsed += sample;
        check(cudaFree(output)); check(cudaEventDestroy(begin)); check(cudaEventDestroy(end));
        std::cout << std::setprecision(17)
          << "{\"palimpsest_workload\":{\"schema\":\"palimpsest.workload-result.v1\",\"scenario\":\"cuda\",\"success\":true,"
          << "\"device\":\"cuda:0\",\"framework\":\"cuda\",\"framework_version\":" << quote(std::to_string(CUDART_VERSION))
          << ",\"matrix_size\":" << MATRIX_SIZE << ",\"iterations\":" << ITERATIONS << ",\"errors\":0,\"device_index\":0,"
          << "\"device_count\":" << devices << ",\"device_name\":" << quote(p.name)
          << ",\"pci_bus_id\":" << quote(pci) << ",\"total_memory_bytes\":" << p.totalGlobalMem
          << ",\"compute_capability\":[" << p.major << ',' << p.minor << "],\"cuda_available\":true,"
          << "\"cuda_driver_version\":" << driver << ",\"cuda_runtime_version\":" << runtime
          << ",\"compiled_cuda_version\":" << CUDART_VERSION << ",\"cuda_allocated_bytes\":" << count * sizeof(float)
          << ",\"synchronization_count\":" << ITERATIONS << ",\"kernel_launches\":" << ITERATIONS
          << ",\"checked_elements\":" << count << ",\"completed_elements\":" << static_cast<long long>(count) * ITERATIONS
          << ",\"checksum\":" << checksum << ",\"expected_checksum\":" << expected << ",\"max_abs_error\":0,"
          << "\"elapsed_seconds\":" << elapsed << ",\"device_elapsed_seconds\":" << device_ms / 1000
          << ",\"operations\":" << static_cast<long long>(count) * 2 * ITERATIONS
          << ",\"operations_per_second\":" << count * 2.0 * ITERATIONS / elapsed
          << ",\"nvidia_smi_csv\":" << quote(inventory.str()) << ",\"iteration_seconds\":[";
        for (size_t i = 0; i < samples.size(); ++i) { if (i) std::cout << ','; std::cout << samples[i]; }
        std::cout << "]}}\n";
    } catch (const std::exception& e) { std::cerr << "CUDA workload failed: " << e.what() << '\n'; return 1; }
}
"""


def _python_argv(scenario, options):
    import base64
    import zlib

    source = f"scenario={scenario!r}\noptions={options!r}\n" + textwrap.dedent(_PYTHON_PREAMBLE)
    source += textwrap.dedent(_TORCH if scenario.startswith("torch-") else _TENSORFLOW)
    encoded = base64.b64encode(zlib.compress(source.encode(), level=9)).decode()
    return [
        "python3",
        "-c",
        "exec(__import__('zlib').decompress(__import__('base64').b64decode(" + repr(encoded) + ")))",
    ]


def workload_argv(config: dict) -> list[str]:
    """The actual process argv used identically by API OCI and Docker CPU lanes.

    Tensor images need python3 and the selected framework. CUDA needs an actual
    devel image (nvcc, C++ toolchain, coreutils timeout and NVIDIA runtime tools).
    Compilation is setup, excluded from the measured kernel iteration samples.
    """
    validate_profile(config)
    scenario = config["scenario"]
    if scenario == "k3s":
        raise ArtifactValidationError("k3s uses its dedicated cluster deployment path")
    options = _options(config)
    if scenario == "cuda":
        script = (
            "umask 077\nwork=$(mktemp -d)\ntrap 'rm -rf -- \"$work\"' EXIT HUP INT TERM\n"
            'timeout --signal=TERM --kill-after=5s 15s nvidia-smi --query-gpu=index,uuid,name,driver_version,memory.total,pci.bus_id --format=csv,noheader,nounits > "$work/nvidia.csv"\n'
            "timeout --signal=TERM --kill-after=5s 10s nvcc --version\ncat > \"$work/workload.cu\" <<'PALIMPSEST_CUDA_SOURCE'\n"
            + _CUDA_SOURCE
            + "\nPALIMPSEST_CUDA_SOURCE\n"
            + f'timeout --signal=TERM --kill-after=5s {options["timeout_seconds"]}s nvcc -O2 -DMATRIX_SIZE={options["matrix_size"]} -DITERATIONS={options["iterations"]} "$work/workload.cu" -o "$work/kernel"\n'
            + f'timeout --signal=TERM --kill-after=5s {options["timeout_seconds"]}s "$work/kernel" "$work/nvidia.csv"\n'
        )
        return [
            "timeout",
            "--signal=TERM",
            "--kill-after=5s",
            f"{options['timeout_seconds']}s",
            "/bin/sh",
            "-ec",
            script,
        ]
    return _python_argv(scenario, options)


def container_command(config: dict, *, name: str) -> list[str]:
    """Run only the selected fixed workload; no caller-supplied shell or mounts."""
    validate_profile(config)
    scenario = config["scenario"]
    if config.get("method", "openstack-container") != "openstack-container":
        raise ArtifactValidationError("Docker reference commands require openstack-container")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,62}", name):
        raise ArtifactValidationError("invalid scenario container name")
    argv = [
        "sudo",
        "-n",
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--name",
        name,
        "--network=none",
        "--stop-timeout=10",
        "--env",
        "OMP_NUM_THREADS=1",
    ]
    if PROFILES[scenario]["gpu"]:
        argv += ["--gpus", "all"]
    # --entrypoint overrides an image's default wrapper so CPU/API argv agree.
    workload = workload_argv(config)
    return argv + ["--entrypoint", workload[0], config["images"][scenario], *workload[1:]]


def _nonempty(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise ArtifactValidationError(f"scenario result lacks {label}")
    return value


def _inventory(value) -> list[dict]:
    if not isinstance(value, list) or not value or len(value) > 64:
        raise ArtifactValidationError("scenario NVIDIA inventory is missing")
    uuids = set()
    for row in value:
        if not isinstance(row, dict):
            raise ArtifactValidationError("invalid NVIDIA inventory row")
        for field in ("index", "uuid", "name", "driver_version", "memory_mib", "pci_bus_id"):
            _nonempty(row.get(field), f"NVIDIA {field}")
        if not row["index"].isdigit() or not re.fullmatch(r"(?:GPU|MIG)-[0-9A-Fa-f-]+", row["uuid"]):
            raise ArtifactValidationError("invalid NVIDIA identity")
        if row["uuid"] in uuids or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", row["driver_version"]):
            raise ArtifactValidationError("invalid NVIDIA driver or duplicate device")
        uuids.add(row["uuid"])
        try:
            _positive(float(row["memory_mib"]), "NVIDIA memory")
        except ValueError:
            raise ArtifactValidationError("invalid NVIDIA memory") from None
        if not re.fullmatch(r"[0-9a-fA-F]{4,8}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", row["pci_bus_id"]):
            raise ArtifactValidationError("invalid NVIDIA PCI identity")
    return value


def _exact(result: dict, key: str, expected) -> None:
    if type(result.get(key)) is not type(expected) or result[key] != expected:
        raise ArtifactValidationError(f"scenario result {key} disagrees with the executed workload")


def validate_result(config: dict, stdout: str) -> dict:
    """Return the unique validated marker payload, never a guessed last JSON line."""
    validate_profile(config)
    scenario = config["scenario"]
    if scenario == "k3s":
        raise ArtifactValidationError("dedicated scenario results are not tensor workload markers")
    if not isinstance(stdout, str) or len(stdout) > 4 * 1024 * 1024:
        raise ArtifactValidationError("invalid scenario stdout bounds")
    markers = []

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON field")
            value[key] = item
        return value

    try:
        for line in stdout.splitlines():
            if not line.strip().startswith("{"):
                continue
            try:
                value = json.loads(
                    line, object_pairs_hook=unique_object, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
                )
            except ValueError:
                if "palimpsest_workload" in line:
                    raise ArtifactValidationError("invalid scenario marker JSON") from None
                continue
            if isinstance(value, dict) and "palimpsest_workload" in value:
                if set(value) != {"palimpsest_workload"} or not isinstance(value["palimpsest_workload"], dict):
                    raise ArtifactValidationError("invalid scenario marker envelope")
                markers.append(value["palimpsest_workload"])
        if len(markers) != 1:
            raise ArtifactValidationError("scenario requires exactly one palimpsest_workload marker")
        result = markers[0]
        options = _options(config)
        n, iterations = options["matrix_size"], options["iterations"]
        gpu = PROFILES[scenario]["gpu"]
        for key, expected in {
            "schema": "palimpsest.workload-result.v1",
            "scenario": scenario,
            "success": True,
            "device": "cuda:0" if gpu else "cpu",
            "iterations": iterations,
            "matrix_size": n,
            "errors": 0,
            "checked_elements": n * n,
            "completed_elements": iterations * n * n,
            "framework": "cuda" if scenario == "cuda" else "torch" if scenario.startswith("torch-") else "tensorflow",
        }.items():
            _exact(result, key, expected)
        _nonempty(result.get("framework_version"), "framework version")
        elapsed = _positive(result.get("elapsed_seconds"), "elapsed time")
        samples = result.get("iteration_seconds")
        if not isinstance(samples, list) or len(samples) != iterations:
            raise ArtifactValidationError("scenario iteration samples are incomplete")
        durations = [_positive(sample, "iteration duration") for sample in samples]
        if elapsed > options["timeout_seconds"] or not math.isclose(elapsed, sum(durations), rel_tol=1e-6):
            raise ArtifactValidationError("scenario time disagrees with actual iterations/deadline")
        expected_checksum = 2 * n**3
        operations = iterations * (2 * n**3)
        if scenario == "cuda":
            # Exact integer sum, independent of the result's claimed expectation.
            def residue_sum(count: int, modulus: int) -> int:
                cycles, remainder = divmod(count, modulus)
                return cycles * modulus * (modulus - 1) // 2 + remainder * (remainder - 1) // 2

            expected_checksum = residue_sum(n * n, 97) + 3 * residue_sum(n * n, 31)
            operations = iterations * n * n * 2
        elif scenario == "tensorflow-cuda":
            operations += iterations * 18 * (n - 2) ** 2
        for key in ("checksum", "expected_checksum"):
            value = result.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or value != expected_checksum:
                raise ArtifactValidationError("scenario numerical checksum differs from independent expectation")
        if type(result.get("max_abs_error")) not in (int, float) or result["max_abs_error"] != 0:
            raise ArtifactValidationError("scenario has numerical errors")
        _exact(result, "operations", operations)
        rate = _positive(result.get("operations_per_second"), "operation rate")
        if not math.isclose(rate, operations / elapsed, rel_tol=1e-6):
            raise ArtifactValidationError("scenario operation rate disagrees with measured counters")
        if scenario != "cuda":
            _exact(result, "shape", [n, n])
            tensor_device = _nonempty(result.get("tensor_device"), "actual tensor device")
            if scenario.startswith("torch-"):
                _exact(result, "tensor_device", "cuda:0" if gpu else "cpu")
                _exact(result, "dtype", "torch.float32")
            else:
                if not re.search(r"/device:(?:GPU:0|CPU:0)$", tensor_device, flags=re.IGNORECASE):
                    raise ArtifactValidationError("TensorFlow actual tensor placement is invalid")
                if not tensor_device.upper().endswith("/DEVICE:" + ("GPU:0" if gpu else "CPU:0")):
                    raise ArtifactValidationError("TensorFlow CPU/GPU fallback is forbidden")
                _exact(result, "dtype", "float32")
                _exact(result, "soft_placement", False)
        if gpu:
            _exact(result, "cuda_available", True)
            _exact(result, "device_index", 0)
            _integer(result.get("device_count"), "actual CUDA device count", 1, 64)
            _nonempty(result.get("device_name"), "actual CUDA device name")
            capability = result.get("compute_capability")
            if not isinstance(capability, list) or len(capability) != 2:
                raise ArtifactValidationError("missing CUDA compute capability")
            _integer(capability[0], "compute capability major", 1, 99)
            _integer(capability[1], "compute capability minor", 0, 99)
            if scenario == "cuda":
                raw = result.get("nvidia_smi_csv")
                if not isinstance(raw, str) or len(raw) > 65536:
                    raise ArtifactValidationError("CUDA NVIDIA inventory is missing")
                rows = list(csv.reader(raw.splitlines(), skipinitialspace=True))
                if any(len(row) != 6 for row in rows):
                    raise ArtifactValidationError("invalid CUDA NVIDIA inventory")
                result["nvidia_smi"] = [
                    dict(zip(("index", "uuid", "name", "driver_version", "memory_mib", "pci_bus_id"), row, strict=True))
                    for row in rows
                ]
            inventory = _inventory(result.get("nvidia_smi"))
            if not any(row["name"].strip() == result["device_name"].strip() for row in inventory):
                raise ArtifactValidationError("CUDA framework device is absent from NVIDIA inventory")
            if scenario != "cuda":
                _nonempty(result.get("cuda_runtime"), "CUDA framework runtime")
            else:
                for key in ("cuda_driver_version", "cuda_runtime_version", "compiled_cuda_version"):
                    _integer(result.get(key), key, 1000, 999999)
                _exact(result, "kernel_launches", iterations)
                device_elapsed = _positive(result.get("device_elapsed_seconds"), "CUDA event elapsed time")
                if device_elapsed > elapsed * 1.05:
                    raise ArtifactValidationError("CUDA device timing exceeds synchronized host timing")
                pci = _nonempty(result.get("pci_bus_id"), "CUDA PCI bus")
                if not any(row["pci_bus_id"].lower().lstrip("0") == pci.lower().lstrip("0") for row in inventory):
                    raise ArtifactValidationError("CUDA selected device is absent from NVIDIA inventory")
            _exact(
                result,
                "synchronization_count",
                iterations * 2
                if scenario == "tensorflow-cuda"
                else iterations + 1
                if scenario == "torch-cuda"
                else iterations,
            )
            if scenario in ("torch-cuda", "cuda"):
                _integer(result.get("total_memory_bytes"), "CUDA total memory", n * n * 4, 2**63 - 1)
                _integer(
                    result.get("cuda_allocated_bytes"), "CUDA allocated memory", n * n * 4, result["total_memory_bytes"]
                )
            if scenario == "tensorflow-cuda":
                _nonempty(result.get("cudnn_version"), "cuDNN version")
                if (
                    not _nonempty(result.get("convolution_device"), "convolution placement")
                    .upper()
                    .endswith("/DEVICE:GPU:0")
                ):
                    raise ArtifactValidationError("TensorFlow convolution CPU fallback is forbidden")
                _exact(result, "convolution_shape", [1, n - 2, n - 2, 1])
                _exact(result, "convolution_checked_elements", (n - 2) ** 2)
                if (
                    type(result.get("convolution_checksum")) not in (int, float)
                    or result["convolution_checksum"] != 9 * (n - 2) ** 2
                ):
                    raise ArtifactValidationError("TensorFlow convolution numerical mismatch")
                if (
                    type(result.get("convolution_max_abs_error")) not in (int, float)
                    or result["convolution_max_abs_error"] != 0
                ):
                    raise ArtifactValidationError("TensorFlow convolution numerical mismatch")
        return result
    except (KeyError, TypeError, AttributeError, OverflowError, csv.Error):
        raise ArtifactValidationError("invalid scenario workload result fields") from None


_GPU_PREFLIGHT = r"""
import csv, json, subprocess, sys
try:
    smi = subprocess.run(["nvidia-smi", "--query-gpu=index,uuid,name,driver_version,memory.total,pci.bus_id", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, timeout=15, check=True)
    info = subprocess.run(["sudo", "-n", "docker", "info", "--format", "{{json .}}"],
                          capture_output=True, text=True, timeout=15, check=True)
    document = json.loads(info.stdout)
    runtimes = document.get("Runtimes", {})
    # --gpus all is mandatory in this reference lane. A CDI spec directory by
    # itself is not proof that Docker can honor the NVIDIA DeviceRequest.
    assert isinstance(runtimes, dict) and "nvidia" in runtimes
    rows = list(csv.reader(smi.stdout.splitlines(), skipinitialspace=True))
    assert rows and all(len(row) == 6 for row in rows)
    inventory = [dict(zip(("index", "uuid", "name", "driver_version", "memory_mib", "pci_bus_id"), row)) for row in rows]
    print(json.dumps({"palimpsest_gpu_host": {"success": True, "nvidia_smi": inventory,
                     "docker_nvidia_runtime": True, "docker_server_version": document["ServerVersion"]}}))
except (OSError, ValueError, KeyError, AssertionError, subprocess.SubprocessError):
    print("GPU prerequisites absent: require working host nvidia-smi and Docker NVIDIA runtime for --gpus all; no driver/toolkit installation performed", file=sys.stderr)
    raise SystemExit(1)
"""


def gpu_preflight(cloud, resource: dict, config: dict) -> dict:
    """Check preinstalled host driver/runtime outside the measured workload.

    CDI-only configurations are not silently used as a different execution
    method: the required Docker --gpus all lane needs its NVIDIA runtime.
    """
    validate_profile(config)
    if not PROFILES[config["scenario"]]["gpu"]:
        raise ArtifactValidationError("GPU preflight requires a GPU profile")
    response = cloud.run_guest(
        resource, ["python3", "-c", textwrap.dedent(_GPU_PREFLIGHT)], label="gpu-host-preflight", timeout=40
    )
    message = "GPU prerequisites absent: working host NVIDIA driver and Docker NVIDIA runtime for --gpus all required; no installation attempted"
    if not isinstance(response, dict) or type(response.get("returncode")) is not int or response["returncode"] != 0:
        raise ArtifactValidationError(message)
    try:
        result = json.loads(response["stdout"])["palimpsest_gpu_host"]
        _exact(result, "success", True)
        _exact(result, "docker_nvidia_runtime", True)
        _inventory(result.get("nvidia_smi"))
        _nonempty(result.get("docker_server_version"), "Docker server version")
    except (ValueError, KeyError, TypeError, ArtifactValidationError):
        raise ArtifactValidationError(message) from None
    return result


_TOKEN_FILE = "/var/lib/palimpsest-scenarios/join-token"
_TOKEN_SETUP = r"""
import os, pathlib, re, sys
value = sys.stdin.buffer.read(129)
if not re.fullmatch(rb"[0-9a-f]{64}", value):
    raise SystemExit("invalid private cluster input")
directory = pathlib.Path("/var/lib/palimpsest-scenarios")
directory.mkdir(mode=0o700, parents=True, exist_ok=True)
fd = os.open(str(directory / "join-token"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "wb") as stream:
    stream.write(value)
"""
_PROBE_COMMAND = (
    'i=0; total=0; while [ "$i" -lt 100 ]; do total=$((total+i)); i=$((i+1)); done; '
    "[ \"$total\" -eq 4950 ]; printf 'palimpsest_k3s_probe:4950\\n'; printf ready > /tmp/palimpsest-probe-ready; exec sleep 1800"
)


def deploy_cluster(cloud, resources: list[dict], config: dict, event: Callable[[str, dict], None]) -> dict:
    """Deploy one new server plus agents; exact VM deletion owns all cleanup.

    run_guest must never persist stdin and must redact stdout/stderr/raw logs of
    every input_bytes call, including failures. Join tokens are private files,
    never Docker environment values, argv, events, kubeconfig or returned data.
    Resources must be supplied server-first, with exact-owned private_ip values.
    No effect is retried: polling is limited to read-only Kubernetes observations.
    """
    validate_profile(config)
    if config["scenario"] != "k3s":
        raise ArtifactValidationError("cluster deployment requires the k3s profile")
    options = _cluster_options(config)
    if not isinstance(resources, list) or len(resources) != options["agents"] + 1:
        raise ArtifactValidationError("k3s requires one separately created server plus every requested agent")
    addresses, server_ids = [], set()
    for resource in resources:
        if (
            not isinstance(resource, dict)
            or not isinstance(resource.get("server_id"), str)
            or not resource["server_id"]
            or resource["server_id"] in server_ids
        ):
            raise ArtifactValidationError("k3s requires distinct exact-owned VM receipts")
        server_ids.add(resource["server_id"])
        try:
            address = ipaddress.ip_address(resource["private_ip"])
        except (ValueError, KeyError, TypeError):
            raise ArtifactValidationError("k3s requires exact-owned private IPv4 node addresses") from None
        if (
            address.version != 4
            or not address.is_private
            or address.is_loopback
            or address.is_unspecified
            or address.is_link_local
            or str(address) in addresses
        ):
            raise ArtifactValidationError("k3s requires distinct routable private IPv4 node addresses")
        addresses.append(str(address))
    image = config["images"]["k3s"]
    probe_image = config["images"]["k3s-probe"]
    names = ["palimpsest-server", *[f"palimpsest-agent-{i}" for i in range(1, len(resources))]]
    container = "palimpsest-k3s"
    token = secrets.token_hex(32).encode("ascii")
    setup_started = time.perf_counter()

    def run(resource: dict, argv: list[str], label: str, timeout: float = 120, *, private: bytes | None = None) -> dict:
        response = cloud.run_guest(resource, argv, label=label, timeout=timeout, input_bytes=private)
        if not isinstance(response, dict) or type(response.get("returncode")) is not int or response["returncode"] != 0:
            raise ArtifactValidationError(f"k3s {label} failed; consult private guest evidence")
        return response

    for index, resource in enumerate(resources):
        # Pulling/setup is explicitly separate from measured deployment and probe timings.
        run(resource, ["sudo", "-n", "docker", "pull", image], f"k3s-pull-{index}", 300)
        run(
            resource,
            [
                "sudo",
                "-n",
                "docker",
                "run",
                "--rm",
                "--pull=never",
                "--privileged",
                "--cgroupns=host",
                "--network=none",
                "--entrypoint",
                "/bin/sh",
                image,
                "-ec",
                "test -d /sys/fs/cgroup; test -w /sys/fs/cgroup; mount -t tmpfs tmpfs /tmp; umount /tmp",
            ],
            f"k3s-capability-{index}",
            30,
        )
        run(
            resource,
            ["sudo", "-n", "python3", "-c", textwrap.dedent(_TOKEN_SETUP)],
            f"k3s-private-token-{index}",
            30,
            private=token,
        )
    del token
    setup_seconds = time.perf_counter() - setup_started
    started = time.perf_counter()
    deadline = time.monotonic() + options["timeout_seconds"]

    def remaining(limit: float = 60) -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise ArtifactValidationError("k3s deployment/probe exceeded its finite deadline")
        return min(left, limit)

    for index, resource in enumerate(resources):
        argv = [
            "sudo",
            "-n",
            "docker",
            "run",
            "--detach",
            "--pull=never",
            "--name",
            container,
            "--privileged",
            "--cgroupns=host",
            "--network=host",
            "--restart=no",
            "--stop-timeout=10",
            "--tmpfs",
            "/run",
            "--tmpfs",
            "/var/run",
            "--volume",
            "/var/lib/rancher/k3s:/var/lib/rancher/k3s",
            "--volume",
            _TOKEN_FILE + ":/run/secrets/palimpsest-token:ro",
            "--env",
            "K3S_TOKEN_FILE=/run/secrets/palimpsest-token",
            image,
            "server" if index == 0 else "agent",
            "--node-name",
            names[index],
            "--node-ip",
            addresses[index],
        ]
        if index == 0:
            argv += [
                "--advertise-address",
                addresses[0],
                "--tls-san",
                addresses[0],
                "--disable",
                "traefik",
                "--disable",
                "servicelb",
            ]
        else:
            argv += ["--server", "https://" + addresses[0] + ":6443"]
        # A failed start can contain implementation diagnostics; token contents
        # never enter this argv or any event, and container logs are not dumped.
        run(resource, argv, f"k3s-start-{index}", remaining())
        event(
            "cluster_node_started",
            {"server_id": resource["server_id"], "node": names[index], "role": "server" if index == 0 else "agent"},
        )

    kubectl = ["sudo", "-n", "docker", "exec", container, "kubectl", "--request-timeout=10s"]
    nodes = None
    poll_index = 0
    while True:
        remaining()
        poll_index += 1
        response = cloud.run_guest(
            resources[0],
            [*kubectl, "get", "nodes", "-o", "json"],
            label=f"k3s-nodes-{poll_index}",
            timeout=remaining(20),
        )
        if isinstance(response, dict) and response.get("returncode") == 0:
            try:
                document = json.loads(response["stdout"])
                items = document["items"]
                actual = {item["metadata"]["name"] for item in items}
                if actual - set(names):
                    raise ArtifactValidationError("k3s observed nodes outside this exact cluster")
                if (
                    len(items) == len(names)
                    and actual == set(names)
                    and all(
                        any(
                            condition.get("type") == "Ready" and condition.get("status") == "True"
                            for condition in item["status"].get("conditions", [])
                        )
                        for item in items
                    )
                ):
                    nodes = []
                    for item in items:
                        name = item["metadata"]["name"]
                        expected_ip = addresses[names.index(name)]
                        if not any(
                            address.get("type") == "InternalIP" and address.get("address") == expected_ip
                            for address in item["status"].get("addresses", [])
                        ):
                            raise ArtifactValidationError("k3s Ready node address differs from its exact guest receipt")
                        version = _nonempty(item["status"]["nodeInfo"]["kubeletVersion"], "k3s kubelet version")
                        runtime = _nonempty(
                            item["status"]["nodeInfo"]["containerRuntimeVersion"], "k3s container runtime"
                        )
                        if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+[^\s]*", version) or not runtime.startswith(
                            "containerd://"
                        ):
                            raise ArtifactValidationError("k3s Ready node runtime identity is invalid")
                        nodes.append(
                            {
                                "name": name,
                                "private_ip": expected_ip,
                                "ready": True,
                                "kubelet_version": version,
                                "container_runtime": runtime,
                            }
                        )
                    break
            except (ValueError, KeyError, TypeError):
                pass
        time.sleep(min(2.0, remaining(2)))
    ready_seconds = time.perf_counter() - started
    event("cluster_nodes_ready", {"count": len(nodes), "elapsed_seconds": ready_seconds})
    probe = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "palimpsest-probe", "namespace": "default"},
        "spec": {
            "replicas": options["probe_replicas"],
            "selector": {"matchLabels": {"app": "palimpsest-probe"}},
            "template": {
                "metadata": {"labels": {"app": "palimpsest-probe"}},
                "spec": {
                    "affinity": {
                        "podAntiAffinity": {
                            "requiredDuringSchedulingIgnoredDuringExecution": [
                                {
                                    "labelSelector": {"matchLabels": {"app": "palimpsest-probe"}},
                                    "topologyKey": "kubernetes.io/hostname",
                                }
                            ]
                        }
                    },
                    "containers": [
                        {
                            "name": "probe",
                            "image": probe_image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["/bin/sh", "-ec", _PROBE_COMMAND],
                            "readinessProbe": {
                                "exec": {
                                    "command": ["/bin/sh", "-ec", 'test "$(cat /tmp/palimpsest-probe-ready)" = ready']
                                },
                                "periodSeconds": 1,
                                "failureThreshold": 1800,
                            },
                            "resources": {
                                "requests": {"cpu": "10m", "memory": "16Mi"},
                                "limits": {"cpu": "100m", "memory": "64Mi"},
                            },
                        }
                    ],
                },
            },
        },
    }
    probe_started = time.perf_counter()
    # Manifest is fixed structured data and contains only a public pinned image;
    # adapter also redacts this stdin call, as required for all private inputs.
    run(
        resources[0],
        [*kubectl[:4], "-i", *kubectl[4:], "create", "-f", "-"],
        "k3s-probe-create",
        remaining(),
        private=json.dumps(probe).encode(),
    )
    run(
        resources[0],
        [
            *kubectl,
            "rollout",
            "status",
            "deployment/palimpsest-probe",
            "--timeout=" + str(max(1, int(remaining(1800)))) + "s",
        ],
        "k3s-probe-rollout",
        remaining(1800),
    )
    response = run(
        resources[0],
        [*kubectl, "get", "pods", "-l", "app=palimpsest-probe", "-o", "json"],
        "k3s-probe-pods",
        remaining(20),
    )
    try:
        pods = json.loads(response["stdout"])["items"]
        if len(pods) != options["probe_replicas"]:
            raise ValueError("replica count")
        proofs = []
        scheduled = set()
        for pod in pods:
            name = pod["metadata"]["name"]
            if not isinstance(name, str) or not re.fullmatch(r"palimpsest-probe-[a-z0-9-]+", name):
                raise ValueError("pod identity")
            node = pod["spec"]["nodeName"]
            if node not in names or node in scheduled or pod["status"]["phase"] != "Running":
                raise ValueError("pod placement")
            scheduled.add(node)
            containers = pod["status"]["containerStatuses"]
            if (
                len(containers) != 1
                or containers[0]["ready"] is not True
                or type(containers[0].get("restartCount")) is not int
                or containers[0]["restartCount"] != 0
            ):
                raise ValueError("pod readiness")
            status = containers[0]
            if status.get("name") != "probe" or not status.get("state", {}).get("running", {}).get("startedAt"):
                raise ValueError("pod process state")
            if not any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in pod["status"].get("conditions", [])
            ):
                raise ValueError("pod Ready condition")
            if (
                len(pod["spec"]["containers"]) != 1
                or pod["spec"]["containers"][0]["image"] != probe_image
                or not re.search(r"sha256:[0-9a-f]{64}$", status["imageID"])
            ):
                raise ValueError("pod image identity")
            log_index = 0
            while True:
                log_index += 1
                logs = run(
                    resources[0],
                    [*kubectl, "logs", name, "--container=probe", "--tail=5"],
                    "k3s-probe-log-" + name + "-" + str(log_index),
                    remaining(20),
                )["stdout"]
                if logs.strip():
                    break
                time.sleep(min(1.0, remaining(1)))
            if logs.strip() != "palimpsest_k3s_probe:4950":
                raise ValueError("pod execution output")
            proofs.append(
                {
                    "pod": name,
                    "node": node,
                    "ready": True,
                    "image": probe_image,
                    "image_id": status["imageID"],
                    "output": "palimpsest_k3s_probe:4950",
                    "expected_sum": 4950,
                }
            )
    except (ValueError, KeyError, TypeError):
        raise ArtifactValidationError("k3s probe did not prove pinned-image execution on its Ready nodes") from None
    elapsed = time.perf_counter() - started
    remaining()
    return {
        "schema": "palimpsest.scenario-cluster-result.v1",
        "scenario": "k3s",
        "success": True,
        "device": "cluster",
        "image": image,
        "setup_seconds": setup_seconds,
        "elapsed_seconds": elapsed,
        "nodes_ready_seconds": ready_seconds,
        "probe_seconds": time.perf_counter() - probe_started,
        "node_count": len(nodes),
        "nodes": nodes,
        "probes": proofs,
    }
