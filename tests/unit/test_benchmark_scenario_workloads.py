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
        compiled_compute_capability=[8, 6],
        device_compiled_arch=860,
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


@pytest.mark.parametrize("scenario", ["cuda", "torch-cuda", "tensorflow-cuda"])
def test_api_pair_requires_explicit_operator_gpu_profile(scenario):
    value = config(scenario, "api-cold-warm")
    with pytest.raises(ArtifactValidationError, match="gpu_profile"):
        workloads.validate_profile(value)
    value["gpu_profile"] = "qualified-gpu"
    if scenario == "tensorflow-cuda":
        value["images"][scenario] = workloads.TENSORFLOW_GPU_BASE
        value["hub"] = {"image_run_network": "default"}
        with pytest.raises(ArtifactValidationError, match="image_run_network=none"):
            workloads.validate_profile(value)
        value["hub"] = {"image_run_network": "none"}
    workloads.validate_profile(value)
    command = workloads.workload_argv(value)
    assert all(len(arg.encode()) <= 4096 for arg in command)
    assert len(command) <= 256


def test_tensorflow_fixed_recipe_uses_exact_historical_wheels_on_pinned_torch_base():
    import hashlib
    from pathlib import Path

    value = config("tensorflow-cuda", "api-cold-warm")
    value.update(gpu_profile="qualified-gpu", hub={"image_run_network": "none"})
    value["images"]["tensorflow-cuda"] = workloads.TENSORFLOW_GPU_BASE
    request, provenance = workloads.api_image_recipe(value)
    lock = Path(workloads.__file__).with_name("tensorflow_delta.lock").read_text()
    assert len(lock.splitlines()) == 18
    assert all("--hash=sha256:" in line for line in lock.splitlines())
    assert provenance["hash_pinned_wheels"] == 18 and provenance["supplemental_wheels"] == 17
    assert provenance["lock_sha256"] == hashlib.sha256(lock.encode()).hexdigest()
    assert provenance["tensorflow_version"] == "2.21.0"
    assert provenance["untouched_tensorflow_image"] is False
    assert provenance["guest_installation"] is False
    assert provenance["required_operator_image_run_network"] == "none"
    assert request["dockerfile"].startswith("FROM " + workloads.TENSORFLOW_GPU_BASE + "\n")
    assert "--mount=type=bind,source=wheels,target=/opt/palimpsest-wheels" in request["dockerfile"]
    assert "--mount=type=bind,source=tensorflow.lock,target=/tmp/palimpsest-tensorflow.lock" in request["dockerfile"]
    assert "--no-index --find-links=/opt/palimpsest-wheels" in request["dockerfile"]
    assert "--no-deps --require-hashes" in request["dockerfile"]
    assert "pip" not in " ".join(workloads.workload_argv(value))
    value["images"]["tensorflow-cuda"] = "docker.io/tensorflow/tensorflow:2.21.0@sha256:" + DIGEST
    with pytest.raises(ArtifactValidationError, match="original pinned Torch"):
        workloads.api_image_recipe(value)


@pytest.mark.parametrize("capability", [[5, 2], [6, 1], [8, 6], [8, 9], [10, 0]])
def test_cuda_result_requires_compilation_for_the_selected_gpu(capability):
    value = config("cuda")
    result = cuda_result()
    result.update(
        compute_capability=capability,
        compiled_compute_capability=capability,
        device_compiled_arch=capability[0] * 100 + capability[1] * 10,
    )
    assert workloads.validate_result(value, marker(result))["compute_capability"] == capability
    result["compiled_compute_capability"] = [9, 0] if capability != [9, 0] else [8, 6]
    with pytest.raises(ArtifactValidationError, match="compiled_compute_capability"):
        workloads.validate_result(value, marker(result))
    result["compiled_compute_capability"] = capability
    result["device_compiled_arch"] = 520 if capability != [5, 2] else 610
    with pytest.raises(ArtifactValidationError, match="device_compiled_arch"):
        workloads.validate_result(value, marker(result))


def api_cluster_config():
    value = config("k3s", "api-cluster")
    value.update(cluster_profile="qualified-k3s", k3s_version="v1.37.1+k3s1")
    from palimpsest_local.benchmark_cluster_context import K3S_IMAGE, PAUSE_IMAGE

    value["images"]["k3s"] = K3S_IMAGE

    value["images"]["k3s-probe"] = PAUSE_IMAGE
    return value


def test_api_cluster_is_explicit_fixed_offline_recipe_not_sdk_or_arbitrary_command():
    value = api_cluster_config()
    workloads.validate_profile(value)
    recipe, provenance = workloads.api_image_recipe(value)
    assert provenance["guest_installation"] is False
    assert provenance["airgap_bytes"] == 195939840
    assert provenance["required_operator_image_run_network"] == "none"
    assert "cp /bin/busybox /opt/palimpsest/probe-busybox" in recipe["dockerfile"]
    assert "COPY k3s-airgap-images-amd64.tar /var/lib/rancher/k3s/agent/images/" in recipe["dockerfile"]
    assert "/opt/palimpsest/pause-image" in recipe["dockerfile"]
    assert "/opt/palimpsest/k3s-version" in recipe["dockerfile"]
    assert all(word not in recipe["dockerfile"] for word in ("curl", "wget", "pip", "apt-get"))
    with pytest.raises(ArtifactValidationError, match="dedicated cluster"):
        workloads.workload_argv(value)
    with pytest.raises(ArtifactValidationError, match="Docker reference"):
        workloads.container_command(value, name="not-a-sdk-fallback")


@pytest.mark.parametrize("change", ["profile", "gpu", "agents", "replicas", "version", "network"])
def test_api_cluster_rejects_unqualified_or_inexact_profile(change):
    value = api_cluster_config()
    if change == "profile":
        value.pop("cluster_profile")
    elif change == "gpu":
        value["gpu_profile"] = "qualified-gpu"
    elif change == "agents":
        value["cluster"]["agents"] = 2
    elif change == "replicas":
        value["cluster"]["probe_replicas"] = 1
    elif change == "version":
        value["k3s_version"] = "v1.37.0+k3s1"
    else:
        value["hub"] = {"image_run_network": "default"}
    with pytest.raises(ArtifactValidationError):
        workloads.validate_profile(value)


def cluster_api_proof():
    import uuid

    value = api_cluster_config()
    members, observations = [], []
    for index in range(2):
        identity = str(uuid.UUID(int=index + 1))
        members.append(
            {
                "id": identity,
                "server_id": "server-" + str(index),
                "boot_volume_id": "boot-" + str(index),
                "cluster": {
                    "profile": value["cluster_profile"],
                    "role": "server" if index == 0 else "agent",
                    "server_instance_id": None if index == 0 else members[0]["id"],
                    "server_address": None if index == 0 else "10.0.0.2",
                    "node_name": "pal-" + uuid.UUID(identity).hex,
                },
            }
        )
        observations.append({"private_ip": "10.0.0." + str(index + 2)})
    proof = {
        "schema": "palimpsest.cluster-result.v1",
        "success": True,
        "k3s_version": value["k3s_version"],
        "checks": {"nodes": True, "numerical": True},
        "nodes": [
            {
                "name": member["cluster"]["node_name"],
                "internal_ip": observations[index]["private_ip"],
                "kubelet_version": value["k3s_version"],
                "container_runtime": "containerd://2.2.2",
            }
            for index, member in enumerate(members)
        ],
        "pods": [
            {
                "node": member["cluster"]["node_name"],
                "name": "probe-" + str(index),
                "image": value["images"]["k3s-probe"],
                "ready": True,
                "restarts": 0,
                "numerical_sum": 4950,
            }
            for index, member in enumerate(members)
        ],
    }
    return value, proof, members, observations


def test_api_cluster_controller_checks_real_two_node_wire_proof():
    value, proof, members, observations = cluster_api_proof()
    assert (
        workloads.validate_cluster_result(value, json.dumps({"palimpsest_cluster": proof}), members, observations)
        == proof
    )


@pytest.mark.parametrize(
    "problem",
    [
        "marker",
        "sum",
        "bool_sum",
        "restarts",
        "image",
        "extra_pod",
        "same_node",
        "extra_node",
        "version",
        "ip",
        "runtime",
        "checks",
        "node_name",
        "server",
        "boot",
        "oversized",
        "noise",
    ],
)
def test_api_cluster_controller_rejects_unqualified_proof(problem):
    value, proof, members, observations = cluster_api_proof()
    if problem == "marker":
        proof = {"success": True}
    elif problem in {"sum", "bool_sum", "restarts", "image"}:
        key, replacement = {
            "sum": ("numerical_sum", 4949),
            "bool_sum": ("numerical_sum", True),
            "restarts": ("restarts", 1),
            "image": ("image", "unqualified"),
        }[problem]
        proof["pods"][0][key] = replacement
    elif problem == "extra_pod":
        proof["pods"].append(dict(proof["pods"][0]))
    elif problem == "same_node":
        proof["pods"][1]["node"] = proof["pods"][0]["node"]
    elif problem == "extra_node":
        proof["nodes"].append(dict(proof["nodes"][0]))
    elif problem in {"version", "ip", "runtime"}:
        key, replacement = {
            "version": ("kubelet_version", "wrong"),
            "ip": ("internal_ip", "10.0.0.99"),
            "runtime": ("container_runtime", "docker://old"),
        }[problem]
        proof["nodes"][0][key] = replacement
    elif problem == "checks":
        proof["checks"]["numerical"] = 1
    elif problem == "node_name":
        members[1]["cluster"]["node_name"] = "not-the-admitted-node"
    elif problem == "server":
        members[1]["cluster"]["server_instance_id"] = members[1]["id"]
    elif problem == "boot":
        members[1]["boot_volume_id"] = members[0]["boot_volume_id"]
    stdout = json.dumps({"palimpsest_cluster": proof})
    if problem == "oversized":
        stdout += " " * 8193
    elif problem == "noise":
        stdout = "not a single sealed JSON document\n" + stdout
    with pytest.raises(ArtifactValidationError, match="exact two admitted"):
        workloads.validate_cluster_result(value, stdout, members, observations)


@pytest.mark.parametrize(
    "url,redirected",
    [
        ("https://user:credential@github.com/k3s-io/k3s/releases/download/anything", False),
        ("https://github.com/k3s-io/k3s/releases/download/latest/airgap.tar", False),
        ("http://github.com/k3s-io/k3s/releases/download/anything", False),
        ("https://example.com/github-production-release-asset/1", True),
        ("https://release-assets.githubusercontent.com/not-the-release-asset/1", True),
        ("https://user:credential@release-assets.githubusercontent.com/github-production-release-asset/1", True),
    ],
)
def test_cluster_airgap_allowlist_does_not_widen_tensorflow_hosts(url, redirected):
    from palimpsest_local import benchmark_cluster_context as context
    from palimpsest_local import benchmark_tensorflow_context as tensorflow

    with pytest.raises(ArtifactValidationError, match="allowlisted HTTPS"):
        context._url(url, redirected=redirected)
    with pytest.raises(ArtifactValidationError, match="allowlisted"):
        tensorflow._url(context.AIRGAP_URL)


def test_cluster_airgap_context_is_fresh_bounded_deterministic_and_external(tmp_path, monkeypatch):
    import hashlib
    import tarfile

    from palimpsest_local import benchmark_cluster_context as context

    payload = b"hash-authoritative-offline-airgap-fixture"
    monkeypatch.setattr(context, "AIRGAP_BYTES", len(payload))
    calls, checks = [], []

    def transfer(url, target, **kwargs):
        calls.append((url, kwargs))
        target.write_bytes(payload)
        return len(payload), None

    monkeypatch.setattr(context, "_transfer", transfer)
    root = tmp_path / "fresh"
    root.mkdir(mode=0o700)
    archive, receipt = context.acquire_context(
        root,
        deadline=100,
        check=lambda deadline: checks.append(deadline),
        event=lambda *_: None,
        max_context_bytes=1024**2,
    )
    assert calls[0][0] == context.AIRGAP_URL and len(calls) == 1
    assert receipt["context_digest"] == "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
    assert receipt["guest_acquisition"] is False and receipt["worker_run_network"] == "none"
    assert receipt["download_bytes"] == len(payload) and len(checks) >= 5
    with tarfile.open(archive) as stream:
        assert stream.getnames() == ["k3s-airgap-images-amd64.tar"]
        member = stream.getmembers()[0]
        assert (member.uid, member.gid, member.mtime, member.mode) == (0, 0, 0, 0o644)
        assert stream.extractfile(member).read() == payload
    with pytest.raises(ArtifactValidationError, match="empty fresh"):
        context.acquire_context(root, deadline=100, check=lambda _: None, event=lambda *_: None)


@pytest.mark.parametrize("problem", ["hash", "size", "redirect_loop", "deadline"])
def test_cluster_owned_curl_rejects_bad_bytes_and_reaps_on_expiry(tmp_path, monkeypatch, problem):
    import hashlib
    import subprocess
    import sys
    import time

    from palimpsest_local import benchmark_cluster_context as context

    payload = b"fixed-asset-bytes"
    monkeypatch.setattr(context, "AIRGAP_BYTES", len(payload))
    monkeypatch.setattr(context, "AIRGAP_SHA256", hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(context.shutil, "which", lambda _: "/fixture/curl")
    real_popen, commands, processes = subprocess.Popen, [], []

    def popen(command, **kwargs):
        commands.append(command)
        emitted = payload[:-1] if problem == "size" else b"x" * len(payload) if problem == "hash" else payload
        status = (
            "302\\nhttps://release-assets.githubusercontent.com/github-production-release-asset/1?signature=fixed"
            if problem == "redirect_loop"
            else "200\\n"
        )
        script = (
            "import sys,time; sys.stdout.buffer.write("
            + repr(emitted)
            + "); sys.stdout.flush(); sys.stderr.write("
            + repr(status.replace("\\n", "\n"))
            + "); sys.stderr.flush()"
        )
        if problem == "deadline":
            script += "; time.sleep(30)"
        process = real_popen([sys.executable, "-c", script], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(context.subprocess, "Popen", popen)
    check_count = [0]

    def check(_):
        check_count[0] += 1
        if problem == "deadline" and check_count[0] >= 3:
            raise ArtifactValidationError("approval expired")

    with pytest.raises(ArtifactValidationError):
        context._transfer(
            context.AIRGAP_URL
            if problem != "redirect_loop"
            else "https://release-assets.githubusercontent.com/github-production-release-asset/1?signature=fixed",
            tmp_path / "asset",
            deadline=time.monotonic() + 10,
            check=check,
            redirected=problem == "redirect_loop",
        )
    assert len(processes) == 1 and processes[0].poll() is not None
    command = commands[0]
    assert "--retry" not in command and "--location" not in command
    assert command[command.index("--max-redirs") + 1] == "0"
    assert command[command.index("--noproxy") + 1] == "*"


def test_api_cluster_accepts_hub_qualified_profile_name_alphabet():
    value = api_cluster_config()
    value["cluster_profile"] = "K3s-cpu.4c"
    workloads.validate_profile(value)


def test_api_cluster_rejects_same_tag_with_unqualified_base_digest():
    value = api_cluster_config()
    value["images"]["k3s"] = "docker.io/rancher/k3s:v1.37.1-k3s1@sha256:" + "b" * 64
    with pytest.raises(ArtifactValidationError, match="exact qualified.*digest"):
        workloads.validate_profile(value)
