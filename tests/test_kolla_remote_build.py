"""Exercise real Ansible selection/rendering; replace only external mutations.

The capture module records fully templated Docker arguments. It does not prove
container execution, image builds, remote infrastructure or a live Keystone flow.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "deploy/kolla/ansible/roles/palimpsest"
KEY = "test-only-build-admission-key-" + "a" * 40

CAPTURE_MODULE = """#!/usr/bin/python
import json
from pathlib import Path
from ansible.module_utils.basic import AnsibleModule

def main():
    module = AnsibleModule(argument_spec={
        "arguments": {"type": "dict", "required": True},
        "destination": {"type": "str", "required": True},
        "source_sha": {"type": "str", "required": True},
        "kind": {"type": "str", "required": True},
    })
    params = module.params
    Path(params["destination"]).write_text(json.dumps({
        "kind": params["kind"], "arguments": params["arguments"]
    }))
    args = params["arguments"]
    # Model immutable source checkout command results, not a real checkout.
    command = args.get("cmd", "")
    stdout = params["source_sha"] if "rev-parse HEAD" in command else ""
    module.exit_json(changed=False, stdout=stdout, rc=0, status=0)

if __name__ == "__main__":
    main()
"""


def _run_lifecycle(tmp_path, actions, variables, *, hosts=None, expect_success=True):
    hosts = hosts or {"worker1": ["palimpsest-build-worker"]}
    library = tmp_path / "library"
    library.mkdir()
    (library / "palimpsest_capture.py").write_text(CAPTURE_MODULE)
    filters = tmp_path / "filter_plugins"
    filters.mkdir()
    (filters / "kolla.py").write_text(
        "class FilterModule:\n"
        "    def filters(self):\n"
        "        return {'kolla_address': lambda network, host: '127.0.0.1'}\n"
    )
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    captures = tmp_path / "captures"
    captures.mkdir()
    # A nested role loop deliberately resolves project_services if entered.
    # Worker-only/API-disabled hosts must skip the dynamic role include first.
    nested_role = tmp_path / "roles/loadbalancer-config/tasks"
    nested_role.mkdir(parents=True)
    (nested_role / "main.yml").write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Model stock HAProxy service loop",
                    "ansible.builtin.debug": {"msg": "{{ item.key }}"},
                    "with_dict": "{{ project_services }}",
                }
            ]
        )
    )
    for path in (ROLE / "tasks").glob("*.yml"):
        tasks = yaml.safe_load(path.read_text()) or []
        for number, task in enumerate(tasks):
            for filesystem_module in ("ansible.builtin.file", "ansible.builtin.copy", "ansible.builtin.template"):
                if filesystem_module in task:
                    # Ownership is checked statically; local tests never require root.
                    task[filesystem_module].pop("owner", None)
                    task[filesystem_module].pop("group", None)

            task.pop("become", None)
            if "ansible.builtin.template" in task:
                source = task["ansible.builtin.template"]["src"]
                task["ansible.builtin.template"]["src"] = str(ROLE / "templates" / source)
            for module in (
                "community.docker.docker_container",
                "community.docker.docker_image",
                "ansible.builtin.git",
                "ansible.builtin.command",
                "kolla_toolbox",
            ):
                if module not in task:
                    continue
                args = task.pop(module)
                # Execute the actual image-ref validator; intercept Docker/git only.
                if module == "ansible.builtin.command" and "validate_image_ref.py" in str(args):
                    task[module] = args
                    continue
                task["palimpsest_capture"] = {
                    "arguments": args,
                    "kind": module,
                    "source_sha": "{{ palimpsest_source_version }}",
                    "destination": str(captures)
                    + "/{{ inventory_hostname }}-"
                    + path.stem
                    + f"-{number}-"
                    + "{{ item.key | default(item | default('single')) }}.json",
                }
            if "ansible.builtin.import_role" in task:
                task.pop("ansible.builtin.import_role")
                task["ansible.builtin.fail"] = {"msg": "Unexpected controller role on direct worker"}
        local_tasks = []
        for task in tasks:
            local_tasks.append(task)
            if task.get("register") == "palimpsest_build_worker_key_stat":
                # The harness cannot chown to root. Retain real type/mode/hash,
                # model only uid after the privileged production copy task.
                local_tasks.append(
                    {
                        "name": "Model production root ownership for local rendering",
                        "ansible.builtin.set_fact": {
                            "palimpsest_build_worker_key_stat": "{{ palimpsest_build_worker_key_stat | combine({'stat': palimpsest_build_worker_key_stat.stat | combine({'uid': 0})}) }}"
                        },
                        "when": task.get("when", True),
                        "no_log": True,
                    }
                )
        (task_dir / path.name).write_text(yaml.safe_dump(local_tasks, sort_keys=False))
    inventory = {"all": {"hosts": {host: {"ansible_connection": "local"} for host in hosts}, "children": {}}}
    for host, groups in hosts.items():
        for group in groups:
            inventory["all"]["children"].setdefault(group, {"hosts": {}})["hosts"][host] = {}
    inventory_file = tmp_path / "inventory.yml"
    inventory_file.write_text(yaml.safe_dump(inventory))
    settings = {
        "enable_palimpsest": False,
        "enable_palimpsest_hub_build_worker": True,
        "palimpsest_build_worker_key": KEY,
        "palimpsest_build_worker_hub_origin": "https://hub.example.test",
        "palimpsest_config_dir": str(tmp_path) + "/{{ inventory_hostname }}/config",
        "palimpsest_source_dir": str(tmp_path) + "/{{ inventory_hostname }}/source",
        "role_path": str(ROLE),
        **variables,
    }
    extra = tmp_path / "variables.json"
    extra.write_text(json.dumps(settings))
    playbook = tmp_path / "playbook.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "all",
                    "gather_facts": False,
                    "vars_files": [str(ROLE / "defaults/main.yml"), str(ROLE / "vars/main.yml")],
                    "tasks": [
                        {"ansible.builtin.include_tasks": str(task_dir / (action + ".yml"))} for action in actions
                    ],
                }
            ]
        )
    )
    result = subprocess.run(
        ["ansible-playbook", "--diff", "-i", str(inventory_file), str(playbook), "-e", "@" + str(extra)],
        capture_output=True,
        text=True,
        timeout=90,
        env={
            **os.environ,
            "ANSIBLE_LIBRARY": str(library),
            "ANSIBLE_FILTER_PLUGINS": str(filters),
            "ANSIBLE_LOCAL_TEMP": str(tmp_path / "local"),
            "ANSIBLE_REMOTE_TEMP": str(tmp_path / "remote"),
            "ANSIBLE_NOCOLOR": "1",
        },
    )
    transcript = result.stdout + result.stderr
    assert KEY not in transcript, "Admission key leaked into Ansible output"
    for secret in (
        "controller-db-secret",
        "controller-redis-secret",
        "controller-keystone-secret",
        "controller-reader-secret",
    ):
        assert secret not in transcript, "Control-plane credential leaked into Ansible output"
    if expect_success:
        assert result.returncode == 0, transcript
    else:
        assert result.returncode != 0, transcript
    recorded = [(path.name, json.loads(path.read_text())) for path in captures.glob("*.json")]
    return recorded, transcript


def _docker_records(records, kind="community.docker.docker_container"):
    return [(host.split("-", 1)[0], data["arguments"]) for host, data in records if data["kind"] == kind]


@pytest.mark.parametrize("source_mode", [False, True])
def test_worker_only_lifecycle_never_templates_controller_credentials(tmp_path, source_mode):
    records, _ = _run_lifecycle(
        tmp_path,
        ["deploy", "pull", "reconfigure", "upgrade", "loadbalancer", "stop", "destroy"],
        {
            "palimpsest_source_mode": source_mode,
            "palimpsest_build_worker_etc_hosts": {"hub.example.test": "192.0.2.10"},
        },
    )
    # No DB, Redis, Keystone, reader or controller inventory variables were supplied.
    containers = _docker_records(records)
    assert containers
    assert {args["name"] for _, args in containers} == {
        "palimpsest_hub_build_worker",
        "palimpsest_build_worker_scratch_init",
    }
    initializers = [args for _, args in containers if args["name"] == "palimpsest_build_worker_scratch_init"]
    assert initializers
    for args in initializers:
        assert args["command"][:2] == ["chmod", "0700"]
        assert args["privileged"] is False
        assert args["network_mode"] == "none"
        assert args["cleanup"] is True and args["detach"] is False
        assert len(args["volumes"]) == 1
        assert args["memory_swap"] == args["memory"]
        assert 0 < float(args["cpus"]) <= 2 and 0 < int(args["pids_limit"]) <= 512
    starts = [
        args
        for _, args in containers
        if args.get("state") == "started" and args["name"] == "palimpsest_hub_build_worker"
    ]
    assert starts
    for args in starts:
        assert args["privileged"] is True
        assert args["user"] in ("0", "0:0", "root")
        # The executor fences children to the worker; Docker init keeps the worker off PID 1.
        assert args["init"] is True
        assert args["etc_hosts"] == {"hub.example.test": "192.0.2.10"}
        assert int(args["pids_limit"]) > 0
        assert float(args["cpus"]) > 0
        assert args["memory"]
        assert args["memory_swap"] == args["memory"], "Worker must not acquire unbounded swap"
        assert all(not name.startswith(("OS_", "DATABASE", "REDIS")) for name in args["env"])
        assert set(args["env"]) == {
            "PALIMPSEST_HUB_URL",
            "PALIMPSEST_HUB_BUILD_WORKER_KEY_FILE",
            "PALIMPSEST_HUB_BUILD_WORK_PATH",
            "PALIMPSEST_HUB_BUILD_TIMEOUT_SECONDS",
            "PALIMPSEST_HUB_MAX_BLOB_BYTES",
        }
        assert args["env"]["PALIMPSEST_HUB_URL"] == "https://hub.example.test"
        assert args["env"]["PALIMPSEST_HUB_BUILD_WORKER_KEY_FILE"].startswith("/")
        assert args["env"]["PALIMPSEST_HUB_BUILD_WORK_PATH"].startswith("/")
        assert 60 <= int(args["env"]["PALIMPSEST_HUB_BUILD_TIMEOUT_SECONDS"]) <= 3600
        assert int(args["env"]["PALIMPSEST_HUB_MAX_BLOB_BYTES"]) > 0
        assert all(isinstance(value, str) for value in args["env"].values())

        mounts = str(args["volumes"])
        assert "palimpsest.conf" not in mounts
        assert not any(value in mounts for value in ("/dev/kvm", "docker.sock", "libvirt", "palimpsest_hub:"))
    config_dir = tmp_path / "worker1/config"
    assert not (config_dir / "palimpsest.conf").exists()
    keyfiles = [path for path in config_dir.rglob("*") if path.is_file() and path.read_text() == KEY]
    assert len(keyfiles) == 1
    assert keyfiles[0].stat().st_mode & 0o777 == 0o600
    images = _docker_records(records, "community.docker.docker_image")
    assert images
    assert all("palimpsest-hub-build-worker" in args["name"] for _, args in images)
    if source_mode:
        assert any(args.get("build", {}).get("target") == "palimpsest-hub-build-worker" for _, args in images)


@pytest.mark.parametrize(
    "override,hosts",
    [
        ({"palimpsest_build_worker_key": ""}, None),
        ({"palimpsest_build_worker_key": None}, None),
        ({"palimpsest_build_worker_key": "too-short"}, None),
        ({"palimpsest_build_worker_key": KEY + "\n"}, None),
        ({"palimpsest_build_worker_hub_origin": "http://hub.example.test"}, None),
        ({"palimpsest_build_worker_hub_origin": "https://user:pass@hub.example.test"}, None),
        ({"palimpsest_build_worker_hub_origin": "https://hub.example.test/v1"}, None),
        ({"palimpsest_build_worker_hub_origin": "https://hub.example.test?"}, None),
        ({"palimpsest_build_worker_timeout_seconds": 0}, None),
        ({"palimpsest_build_worker_max_blob_bytes": 0}, None),
        ({"palimpsest_build_worker_cpus": 0}, None),
        ({"palimpsest_build_worker_memory": "0g"}, None),
        ({"palimpsest_build_worker_pids_limit": 0}, None),
        ({"palimpsest_build_worker_scratch_volume": "palimpsest_hub"}, None),
        ({"palimpsest_build_worker_etc_hosts": {"hub.example.test": "not-an-address"}}, None),
        ({"palimpsest_build_worker_etc_hosts": {"Hub Example": "192.0.2.10"}}, None),
        ({"palimpsest_build_worker_etc_hosts": "hub.example.test=192.0.2.10"}, None),
        ({}, {"worker1": []}),
        ({}, {"controller1": ["palimpsest", "palimpsest-build-worker"]}),
    ],
)
def test_worker_deploy_fails_closed_before_mutation(tmp_path, override, hosts):
    records, _ = _run_lifecycle(tmp_path, ["deploy"], override, hosts=hosts, expect_success=False)
    assert not records
    assert not list(tmp_path.glob("*/config/*"))


def test_disabled_worker_does_not_require_origin_or_key(tmp_path):
    records, _ = _run_lifecycle(
        tmp_path,
        ["deploy", "pull", "destroy"],
        {
            "enable_palimpsest_hub_build_worker": False,
            "palimpsest_build_worker_key": "",
            "palimpsest_build_worker_hub_origin": "",
        },
    )
    assert not records


def test_controller_and_remote_worker_render_separate_service_arguments(tmp_path):
    records, _ = _run_lifecycle(
        tmp_path,
        ["config", "start", "pull", "image_precheck"],
        {
            "enable_palimpsest": True,
            "palimpsest_database_url": "mysql+asyncmy://hub:controller-db-secret@db/hub",
            "palimpsest_redis_url": "redis://:controller-redis-secret@redis:6379/9",
            "palimpsest_database_password": "controller-db-secret",
            "palimpsest_keystone_password": "controller-keystone-secret",
            "palimpsest_reader_password": "controller-reader-secret",
            "keystone_internal_url": "https://identity.example.test/v3",
            "default_project_domain_name": "Default",
            "default_user_domain_name": "Default",
            "openstack_region_name": "RegionOne",
        },
        hosts={"controller1": ["palimpsest", "control"], "worker1": ["palimpsest-build-worker"]},
    )
    containers = _docker_records(records)
    assert {(host, args["name"]) for host, args in containers} == {
        ("controller1", "palimpsest_hub_api"),
        ("controller1", "palimpsest_hub_worker"),
        ("worker1", "palimpsest_hub_build_worker"),
        ("worker1", "palimpsest_build_worker_scratch_init"),
    }
    api = next(args for _, args in containers if args["name"] == "palimpsest_hub_api")
    assert api["env"]["PALIMPSEST_HUB_BUILD_WORKER_KEY"] == KEY
    worker = next(args for _, args in containers if args["name"] == "palimpsest_hub_build_worker")
    serialized = json.dumps(worker)
    assert KEY not in serialized, "Worker must receive a mounted keyfile, not a key env value"
    assert "controller-db-secret" not in serialized
    assert "controller-redis-secret" not in serialized
    assert "controller-keystone-secret" not in serialized
    assert "controller-reader-secret" not in serialized
    assert not api.get("privileged", False)
    assert "etc_hosts" not in api and "init" not in api
    assert (tmp_path / "controller1/config/palimpsest.conf").exists()
    assert not (tmp_path / "worker1/config/palimpsest.conf").exists()


def test_all_three_hub_targets_share_canonical_workflow_policy():
    workflow = yaml.safe_load((ROOT / ".github/workflows/hub-docker.yml").read_text())
    steps = workflow["jobs"]["build-and-push"]["steps"]
    builds = [step["with"] for step in steps if step.get("uses") == "docker/build-push-action@v5"]
    targets = {"palimpsest-hub-api", "palimpsest-hub-worker", "palimpsest-hub-build-worker"}
    assert {build["target"] for build in builds} == targets
    for build in builds:
        assert build["context"] == "."
        assert build["file"] == "docker/hub/Dockerfile"
        assert build["push"] == "${{ github.event_name != 'pull_request' }}"
        assert "platforms" not in build, "Preserve the workflow's existing native platform policy"
    metadata = [step["with"] for step in steps if step.get("uses") == "docker/metadata-action@v5"]
    assert {entry["images"].rsplit("/", 1)[-1] for entry in metadata} == targets
    assert len({entry["tags"] for entry in metadata}) == 1
    assert workflow["jobs"]["build-and-push"]["needs"] == "test"


def test_worker_secret_is_root_owned_and_hidden_from_diffs():
    tasks = yaml.safe_load((ROLE / "tasks/config.yml").read_text())
    install = next(
        task
        for task in tasks
        if task.get("ansible.builtin.copy", {}).get("content") == "{{ palimpsest_build_worker_key }}"
    )
    args = install["ansible.builtin.copy"]
    assert args["owner"] == args["group"] == "root"
    assert args["mode"] == "0600"
    assert install["no_log"] is True and install["diff"] is False
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults["enable_palimpsest_hub_build_worker"] in (False, "no")
    assert defaults["palimpsest_service_catalog"]["palimpsest-hub-build-worker"]["group"] == "palimpsest-build-worker"
    assert "palimpsest_services" not in defaults, "Do not reintroduce eagerly rendered secret-bearing service maps"
