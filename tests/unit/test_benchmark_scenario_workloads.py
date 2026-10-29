from __future__ import annotations

import json

import pytest

from palimpsest_local import benchmark_scenario_workloads as workloads
from palimpsest_local.errors import ArtifactValidationError

DIGEST = "a" * 64
IMAGE = "registry.example/workloads/python:stable@sha256:" + DIGEST
CUDA_IMAGE = "registry.example/nvidia/cuda:12.6.3-devel-ubuntu24.04@sha256:" + DIGEST


def config(scenario="torch-cpu", method="openstack-container"):
    return {
        "scenario": scenario,
        "method": method,
        "images": {
            scenario: CUDA_IMAGE if scenario == "cuda" else IMAGE,
            **({"k3s-probe": IMAGE} if scenario == "k3s" else {}),
        },
        "workload": {"iterations": 2, "matrix_size": 16, "timeout_seconds": 30},
        "cluster": {"servers": 1, "agents": 1, "timeout_seconds": 60},
    }


def tensor_result(scenario="torch-cpu"):
    gpu = scenario.endswith("-cuda")
    tensorflow = scenario.startswith("tensorflow-")
    operations = 2 * (2 * 16**3 + (18 * 14**2 if tensorflow and gpu else 0))
    result = {
        "schema": "palimpsest.workload-result.v1",
        "scenario": scenario,
        "success": True,
        "device": "cuda:0" if gpu else "cpu",
        "iterations": 2,
        "matrix_size": 16,
        "errors": 0,
        "checked_elements": 256,
        "completed_elements": 512,
        "framework": "tensorflow" if tensorflow else "torch",
        "framework_version": "2.0.1",
        "elapsed_seconds": 0.3,
        "iteration_seconds": [0.1, 0.2],
        "checksum": 8192.0,
        "expected_checksum": 8192,
        "max_abs_error": 0.0,
        "operations": operations,
        "operations_per_second": operations / 0.3,
        "shape": [16, 16],
        "dtype": "float32" if tensorflow else "torch.float32",
        "tensor_device": "/job:localhost/replica:0/task:0/device:" + ("GPU:0" if gpu else "CPU:0")
        if tensorflow
        else "cuda:0"
        if gpu
        else "cpu",
    }
    if tensorflow:
        result["soft_placement"] = False
    if gpu:
        result.update(
            cuda_available=True,
            device_index=0,
            device_count=1,
            device_name="NVIDIA Test GPU",
            compute_capability=[8, 6],
            cuda_runtime="12.6",
            total_memory_bytes=16 * 1024**3,
            cuda_allocated_bytes=3 * 256 * 4,
            synchronization_count=4 if tensorflow else 3,
            nvidia_smi=[
                {
                    "index": "0",
                    "uuid": "GPU-12345678-1234-1234-1234-123456789abc",
                    "name": "NVIDIA Test GPU",
                    "driver_version": "560.35.03",
                    "memory_mib": "16384",
                    "pci_bus_id": "00000000:01:00.0",
                }
            ],
        )
        if tensorflow:
            result.update(
                cudnn_version="9",
                convolution_device=result["tensor_device"],
                convolution_shape=[1, 14, 14, 1],
                convolution_checksum=1764.0,
                convolution_max_abs_error=0.0,
                convolution_checked_elements=196,
            )
    return result


def cuda_result():
    result = tensor_result("torch-cuda")
    for key in ("shape", "dtype", "tensor_device", "cuda_runtime"):
        result.pop(key)
    checksum = sum(index % 97 + 3 * (index % 31) for index in range(256))
    result.update(
        scenario="cuda",
        framework="cuda",
        framework_version="12060",
        checksum=checksum,
        expected_checksum=checksum,
        operations=1024,
        operations_per_second=1024 / 0.3,
        synchronization_count=2,
        kernel_launches=2,
        cuda_driver_version=12060,
        cuda_runtime_version=12060,
        compiled_cuda_version=12060,
        device_elapsed_seconds=0.2,
        pci_bus_id="0000:01:00.0",
        nvidia_smi_csv="0, GPU-12345678-1234-1234-1234-123456789abc, NVIDIA Test GPU, 560.35.03, 16384, 00000000:01:00.0\n",
    )
    return result


def marker(result):
    return json.dumps({"palimpsest_workload": result})


@pytest.mark.parametrize("scenario", ["torch-cpu", "torch-cuda", "tensorflow-cpu", "tensorflow-cuda"])
def test_real_counter_result_envelope_is_validated(scenario):
    result = tensor_result(scenario)
    assert workloads.validate_result(config(scenario), "framework startup\n" + marker(result)) == result


def test_cuda_result_requires_compiled_kernel_counters_and_independent_vector_sum():
    result = cuda_result()
    assert workloads.validate_result(config("cuda"), marker(result))["kernel_launches"] == 2
    for key, value in (
        ("kernel_launches", 0),
        ("cuda_driver_version", 0),
        ("cuda_runtime_version", 0),
        ("compiled_cuda_version", 0),
        ("device_elapsed_seconds", 0),
        ("device_elapsed_seconds", 1),
        ("checksum", result["checksum"] + 1),
        ("pci_bus_id", "0000:02:00.0"),
        ("nvidia_smi_csv", "not a GPU inventory"),
    ):
        broken = dict(result)
        broken[key] = value
        with pytest.raises(ArtifactValidationError):
            workloads.validate_result(config("cuda"), marker(broken))


@pytest.mark.parametrize("scenario", ["torch-cuda", "tensorflow-cuda", "cuda", "k3s"])
@pytest.mark.parametrize("method", ["api-import", "api-build", "api-reuse"])
def test_unsupported_api_gpu_and_cluster_fail_before_any_allocation(scenario, method):
    with pytest.raises(ArtifactValidationError, match="unsupported.*openstack-container"):
        workloads.validate_profile(config(scenario, method))
    assert workloads.PROFILES[scenario]["default_method"] == "openstack-container"


@pytest.mark.parametrize(
    "image",
    [
        "python:latest",
        "python@sha256:abc",
        "https://registry/image@sha256:" + DIGEST,
        "name@sha256:" + "A" * 64,
        "user:secret@registry/image@sha256:" + DIGEST,
        "name@sha256:" + DIGEST + "\n",
        "-option@sha256:" + DIGEST,
    ],
)
def test_image_identity_requires_exact_digest_without_credentials(image):
    value = config()
    value["images"]["torch-cpu"] = image
    with pytest.raises(ArtifactValidationError, match="digest-pinned"):
        workloads.validate_profile(value)


def test_cluster_requires_both_pinned_images_and_bounded_agents():
    value = config("k3s")
    value["images"].pop("k3s-probe")
    with pytest.raises(ArtifactValidationError, match="k3s-probe"):
        workloads.validate_profile(value)
    value = config("k3s")
    value["cluster"]["agents"] = 0
    with pytest.raises(ArtifactValidationError, match="agents"):
        workloads.validate_profile(value)


@pytest.mark.parametrize("servers", [0, 2, True])
def test_cluster_requires_exactly_one_integer_server(servers):
    value = config("k3s")
    value["cluster"]["servers"] = servers
    with pytest.raises(ArtifactValidationError, match="servers"):
        workloads.validate_profile(value)


def test_cluster_contract_explicit_single_server_is_supported():
    workloads.validate_profile(config("k3s"))


@pytest.mark.parametrize("missing", [None, "runtime", "inventory", "command"])
def test_gpu_host_preflight_checks_existing_prerequisites_without_installation(missing):
    result = {
        "success": True,
        "docker_nvidia_runtime": True,
        "docker_server_version": "28.3.0",
        "nvidia_smi": tensor_result("torch-cuda")["nvidia_smi"],
    }
    if missing == "runtime":
        result["docker_nvidia_runtime"] = False
    if missing == "inventory":
        result["nvidia_smi"] = []

    class Cloud:
        def run_guest(self, resource, argv, *, label, timeout):
            return {
                "returncode": 1 if missing == "command" else 0,
                "stdout": json.dumps({"palimpsest_gpu_host": result}),
            }

    if missing is None:
        assert workloads.gpu_preflight(Cloud(), {"server_id": "owned"}, config("torch-cuda")) == result
    else:
        with pytest.raises(ArtifactValidationError, match="GPU prerequisites absent"):
            workloads.gpu_preflight(Cloud(), {"server_id": "owned"}, config("torch-cuda"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("iterations", 0),
        ("iterations", True),
        ("iterations", 1001),
        ("matrix_size", 4096),
        ("timeout_seconds", 1801),
        ("command", "arbitrary command"),
    ],
)
def test_workload_options_have_finite_closed_bounds(field, value):
    value_config = config()
    value_config["workload"][field] = value
    with pytest.raises(ArtifactValidationError):
        workloads.validate_profile(value_config)


def test_cuda_requires_actual_devel_compiler_image():
    value = config("cuda")
    value["images"]["cuda"] = IMAGE
    with pytest.raises(ArtifactValidationError, match="devel"):
        workloads.validate_profile(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("success", False),
        ("success", 1),
        ("scenario", "torch-cuda"),
        ("errors", 1),
        ("checksum", 8191),
        ("expected_checksum", 8191),
        ("max_abs_error", 0.5),
        ("checked_elements", 1),
        ("completed_elements", 256),
        ("iterations", 1),
        ("operations", 1),
        ("operations_per_second", 1),
        ("elapsed_seconds", 0),
        ("elapsed_seconds", 31),
        ("elapsed_seconds", float("inf")),
        ("iteration_seconds", [0.3]),
        ("iteration_seconds", [0.1, -0.2]),
        ("iteration_seconds", [0.1, 0.3]),
        ("tensor_device", "cuda:0"),
        ("shape", [8, 32]),
    ],
)
def test_result_requires_independent_numerics_and_real_timing_counters(field, value):
    result = tensor_result()
    result[field] = value
    with pytest.raises(ArtifactValidationError):
        workloads.validate_result(config(), marker(result))


@pytest.mark.parametrize("scenario", ["torch-cuda", "tensorflow-cuda"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("device", "cpu"),
        ("cuda_available", False),
        ("device_count", 0),
        ("device_index", 1),
        ("synchronization_count", 0),
        ("nvidia_smi", []),
        ("compute_capability", [0, 0]),
        ("cuda_runtime", ""),
    ],
)
def test_gpu_label_alone_is_not_a_cuda_execution_proof(scenario, field, value):
    result = tensor_result(scenario)
    result[field] = value
    with pytest.raises(ArtifactValidationError):
        workloads.validate_result(config(scenario), marker(result))


@pytest.mark.parametrize(
    "field,value",
    [
        ("soft_placement", True),
        ("tensor_device", "/device:CPU:0"),
        ("convolution_device", "/device:CPU:0"),
        ("convolution_checked_elements", 0),
        ("convolution_checksum", 0),
        ("convolution_max_abs_error", 1),
    ],
)
def test_tensorflow_rejects_cpu_matmul_or_convolution_fallback(field, value):
    result = tensor_result("tensorflow-cuda")
    result[field] = value
    with pytest.raises(ArtifactValidationError):
        workloads.validate_result(config("tensorflow-cuda"), marker(result))


def test_result_marker_is_unique_strict_json_not_arbitrary_last_line():
    valid = marker(tensor_result())
    for stdout in (
        "",
        '{"device":"cuda"}',
        valid + "\n" + valid,
        '{"palimpsest_workload":NaN}',
        '{"palimpsest_workload":{},"extra":1}',
        valid.replace('"success": true', '"success": false, "success": true'),
    ):
        with pytest.raises(ArtifactValidationError):
            workloads.validate_result(config(), stdout)


class ClusterCloud:
    """Deterministic protocol fixture; never executes a guest or a cloud call."""

    def __init__(self):
        self.calls = []
        self.invalid_log = False
        self.invalid_node_ip = False
        self.extra_node = False
        self.fail_start = False

    def run_guest(self, resource, argv, *, label, timeout, input_bytes=None):
        self.calls.append(
            {"resource": resource, "argv": argv, "label": label, "timeout": timeout, "input": input_bytes}
        )
        stdout = ""
        if label.startswith("k3s-nodes-"):
            items = []
            for index, name in enumerate(("palimpsest-server", "palimpsest-agent-1")):
                items.append(
                    {
                        "metadata": {"name": name},
                        "status": {
                            "conditions": [{"type": "Ready", "status": "True"}],
                            "addresses": [
                                {
                                    "type": "InternalIP",
                                    "address": "10.0.0.99" if self.invalid_node_ip else f"10.0.0.{index + 2}",
                                }
                            ],
                            "nodeInfo": {
                                "kubeletVersion": "v1.32.1+k3s1",
                                "containerRuntimeVersion": "containerd://2.0.2",
                            },
                        },
                    }
                )
            if self.extra_node:
                items.append({"metadata": {"name": "foreign-node"}})
            stdout = json.dumps({"items": items})
        elif label == "k3s-probe-pods":
            stdout = json.dumps(
                {
                    "items": [
                        {
                            "metadata": {"name": f"palimpsest-probe-abc-{index}"},
                            "spec": {"nodeName": node, "containers": [{"image": IMAGE}]},
                            "status": {
                                "phase": "Running",
                                "conditions": [{"type": "Ready", "status": "True"}],
                                "containerStatuses": [
                                    {
                                        "name": "probe",
                                        "ready": True,
                                        "restartCount": 0,
                                        "state": {"running": {"startedAt": "2026-10-29T00:00:00Z"}},
                                        "imageID": "registry.example/workloads/python@sha256:" + DIGEST,
                                    }
                                ],
                            },
                        }
                        for index, node in enumerate(("palimpsest-server", "palimpsest-agent-1"))
                    ]
                }
            )
        elif label.startswith("k3s-probe-log-"):
            stdout = "not a workload result" if self.invalid_log else "palimpsest_k3s_probe:4950\n"
        return {
            "returncode": 1 if self.fail_start and label == "k3s-start-0" else 0,
            "stdout": stdout,
            "stderr": "",
            "elapsed_seconds": 0.01,
        }


def cluster_resources():
    return [{"server_id": "server-one", "private_ip": "10.0.0.2"}, {"server_id": "agent-one", "private_ip": "10.0.0.3"}]


def test_cluster_uses_private_token_files_real_readiness_and_pod_execution():
    cloud = ClusterCloud()
    events = []
    result = workloads.deploy_cluster(
        cloud, cluster_resources(), config("k3s"), lambda name, value: events.append((name, value))
    )
    assert result["success"] is True and result["node_count"] == 2
    assert len(result["probes"]) == 2
    private = [call for call in cloud.calls if call["label"].startswith("k3s-private-token")]
    assert len(private) == 2 and private[0]["input"] == private[1]["input"]
    token = private[0]["input"].decode()
    assert len(token) == 64
    assert token not in json.dumps(result) and token not in json.dumps(events)
    assert all(token not in json.dumps(call["argv"]) for call in cloud.calls)
    labels = [call["label"] for call in cloud.calls]
    assert len(labels) == len(set(labels))
    for call in cloud.calls:
        assert 0 < call["timeout"] <= 300
        assert "prune" not in call["argv"]
    assert all(proof["output"] == "palimpsest_k3s_probe:4950" for proof in result["probes"])


@pytest.mark.parametrize("problem", ["invalid_log", "invalid_node_ip", "extra_node", "fail_start"])
def test_cluster_does_not_promote_start_or_fake_ready_into_workload_success(problem):
    cloud = ClusterCloud()
    setattr(cloud, problem, True)
    with pytest.raises(ArtifactValidationError):
        workloads.deploy_cluster(cloud, cluster_resources(), config("k3s"), lambda *_: None)


@pytest.mark.parametrize(
    "resources",
    [
        [],
        [cluster_resources()[0]] * 2,
        [{"server_id": "one", "private_ip": "127.0.0.1"}, cluster_resources()[1]],
        [{"server_id": "one", "private_ip": "10.0.0.3"}, cluster_resources()[1]],
    ],
)
def test_cluster_invalid_resource_set_fails_before_guest_effect(resources):
    cloud = ClusterCloud()
    with pytest.raises(ArtifactValidationError):
        workloads.deploy_cluster(cloud, resources, config("k3s"), lambda *_: None)
    assert cloud.calls == []


@pytest.mark.parametrize("scenario", ["torch-cpu", "tensorflow-cpu"])
@pytest.mark.parametrize("method", ["api-import", "api-build", "api-reuse"])
def test_api_result_channel_rejects_unbounded_iteration_vectors_before_allocation(scenario, method):
    value = config(scenario, method)
    value["workload"]["iterations"] = 65
    with pytest.raises(ArtifactValidationError, match="iterations"):
        workloads.validate_profile(value)
