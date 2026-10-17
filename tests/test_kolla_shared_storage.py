"""Exercise canonical Ansible writer gates, not shared-filesystem qualification.

Only external Docker mutations and mount-table responses are controlled. Real
Ansible conditionals, directory/symlink checks and canonicalization execute.
"""

from __future__ import annotations

import json
import os
import shutil
import sys

import pytest
from test_kolla_remote_build import _docker_records, _run_lifecycle

CONTROLLERS = {f"controller{number}": ["palimpsest", "valkey"] for number in range(1, 4)}


def _settings(**overrides):
    return {
        "enable_palimpsest": True,
        "enable_palimpsest_hub_build_worker": False,
        "palimpsest_run_preconditions": False,
        "enable_valkey": True,
        "valkey_master_password": "controller-redis-secret",
        "palimpsest_redis_sentinel_addresses": [[f"192.0.2.{number}", 26379] for number in range(11, 14)],
        "palimpsest_redis_sentinel_master": "kolla",
        "palimpsest_package_forbidden_project_ids": ["protected-project"],
        "palimpsest_package_forbidden_user_ids": ["service-user"],
        "palimpsest_api_listen_address": "127.0.0.1",
        "palimpsest_database_url": "mysql+asyncmy://hub:controller-db-secret@db/hub",
        "palimpsest_redis_url": "redis://:controller-redis-secret@redis:6379/9",
        "palimpsest_database_password": "controller-db-secret",
        "palimpsest_keystone_password": "controller-keystone-secret",
        "palimpsest_reader_password": "controller-reader-secret",
        "keystone_internal_url": "https://identity.example.test/v3",
        "default_project_domain_name": "Default",
        "default_user_domain_name": "Default",
        "openstack_region_name": "RegionOne",
        **overrides,
    }


def _shared_settings(path, **overrides):
    return (
        _settings(
            palimpsest_hub_storage_mode="shared",
            palimpsest_hub_shared_path=str(path),
            palimpsest_hub_shared_fstype="ceph",
            palimpsest_hub_shared_source="service@fsid.cephfs=/hub",
        )
        | overrides
    )


def _metadata(path, **overrides):
    return {"filesystems": [{"target": str(path), "fstype": "ceph", "source": "service@fsid.cephfs=/hub", **overrides}]}


def _findmnt_fixture(tmp_path, path, responses, *, lose_after=None):
    """Model mount answers; optional loss follows an observed external effect."""
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    calls = tmp_path / "findmnt-calls.jsonl"
    script = binary_dir / "findmnt"
    script.write_text(
        f"#!{sys.executable}\n"
        "import argparse, json, sys\n"
        "from pathlib import Path\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('-J', '--json', action='store_true')\n"
        "parser.add_argument('-M', '--mountpoint')\n"
        "parser.add_argument('-T', '--target')\n"
        "parser.add_argument('-o', '--output')\n"
        "query = parser.parse_args()\n"
        f"if (query.mountpoint or query.target) != {str(path)!r}:\n"
        "    sys.exit(1)\n"
        f"with open({str(calls)!r}, 'a') as stream:\n"
        "    stream.write('queried\\n')\n"
        f"response = {responses[0]!r}\n"
        f"lose_after = {lose_after!r}\n"
        f"for filename in Path({str(tmp_path / 'captures')!r}).glob('*.json'):\n"
        "    effect = json.loads(filename.read_text())\n"
        "    image_pull = lose_after == 'image-pull' and effect['kind'] == 'community.docker.docker_image'\n"
        "    writer = lose_after is not None and effect['arguments'].get('name') == lose_after\n"
        "    if image_pull or writer:\n"
        "        response = {'stdout': '', 'rc': 1}\n"
        "        break\n"
        "print(response['stdout'] if query.json else 'non-JSON mount table')\n"
        "sys.exit(response.get('rc', 0))\n"
    )
    script.chmod(0o700)
    return {"PATH": str(binary_dir) + os.pathsep + os.environ["PATH"]}, calls


def _response(metadata):
    return {"stdout": json.dumps(metadata)}


@pytest.mark.parametrize("action", ["precheck", "deploy", "upgrade", "reconfigure", "bootstrap_service", "start"])
def test_multi_controller_local_storage_fails_before_docker(tmp_path, action):
    records, _ = _run_lifecycle(tmp_path, [action], _settings(), hosts=CONTROLLERS, expect_success=False)
    assert not _docker_records(records)
    assert not _docker_records(records, "community.docker.docker_image")


@pytest.mark.parametrize("action", ["bootstrap_service", "start"])
@pytest.mark.parametrize("failure", ["unmounted", "target", "fstype", "source", "duplicate", "malformed"])
def test_shared_mount_metadata_fails_closed_before_any_docker(tmp_path, action, failure):
    mount = (tmp_path / "shared").resolve()
    mount.mkdir()
    metadata = _metadata(mount)
    if failure in ("target", "fstype", "source"):
        metadata["filesystems"][0][failure] = {"target": "/", "fstype": "ext4", "source": "wrong-share"}[failure]
    elif failure == "duplicate":
        metadata["filesystems"] *= 2
    response = _response(metadata)
    if failure == "unmounted":
        response = {"stdout": "", "rc": 1}
    elif failure == "malformed":
        response = {"stdout": "not mount metadata"}
    environment, _ = _findmnt_fixture(tmp_path, mount, [response])
    records, _ = _run_lifecycle(
        tmp_path,
        [action],
        _shared_settings(mount),
        hosts={"controller1": ["palimpsest"]},
        expect_success=False,
        environment=environment,
    )
    assert not _docker_records(records)
    assert not _docker_records(records, "community.docker.docker_image")
    assert not list(mount.iterdir()), "No fallback directory or storage content may be provisioned"


@pytest.mark.parametrize(
    "failure", ["missing", "symlink", "ancestor", "noncanonical", "relative", "empty", "fstype", "source"]
)
def test_shared_path_settings_and_real_filesystem_fail_before_mount_adoption(tmp_path, failure):
    mount = (tmp_path / "shared").resolve()
    mount.mkdir()
    path = str(mount)
    overrides = {}
    if failure == "missing":
        path = str(mount / "missing")
    elif failure == "symlink":
        link = mount.parent / "link"
        link.symlink_to(mount, target_is_directory=True)
        path = str(link)
    elif failure == "ancestor":
        link = mount.parent / "link"
        link.symlink_to(mount.parent, target_is_directory=True)
        path = str(link / mount.name)
    elif failure == "noncanonical":
        path += "/"
    elif failure == "relative":
        path = "shared"
    elif failure == "empty":
        path = ""
    elif failure == "fstype":
        overrides["palimpsest_hub_shared_fstype"] = "ext4"
    elif failure == "source":
        overrides["palimpsest_hub_shared_source"] = ""
    environment, calls = _findmnt_fixture(tmp_path, path, [_response(_metadata(path))])
    records, _ = _run_lifecycle(
        tmp_path,
        ["bootstrap_service"],
        _shared_settings(path, **overrides),
        hosts={"controller1": ["palimpsest"]},
        expect_success=False,
        environment=environment,
    )
    assert not calls.exists()
    assert not _docker_records(records)
    assert not _docker_records(records, "community.docker.docker_image")
    assert not (mount / "missing").exists()


@pytest.mark.parametrize("fstype", ["ceph", "fuse.ceph", "nfs4"])
def test_three_controller_verified_mount_reaches_bootstrap_and_api_export_writers(tmp_path, fstype):
    mount = (tmp_path / "shared").resolve()
    mount.mkdir()
    environment, _ = _findmnt_fixture(tmp_path, mount, [_response(_metadata(mount, fstype=fstype))])
    records, _ = _run_lifecycle(
        tmp_path,
        ["precheck_controller", "bootstrap_service", "start"],
        _shared_settings(mount, palimpsest_hub_shared_fstype=fstype),
        hosts=CONTROLLERS,
        environment=environment,
    )
    containers = _docker_records(records)
    assert {host for host, _ in containers} == set(CONTROLLERS)
    assert sum(args["name"] == "palimpsest_bootstrap" for _, args in containers) == 1
    for host in CONTROLLERS:
        assert {args["name"] for candidate, args in containers if candidate == host} >= {
            "palimpsest_hub_api",
            "palimpsest_hub_worker",
        }
    assert all(args["name"] != "palimpsest_hub_volume_owner" for _, args in containers)


@pytest.mark.parametrize("lost_before", ["bootstrap", "start", "export"])
def test_mount_loss_is_rechecked_at_each_writer_boundary(tmp_path, lost_before):
    mount = (tmp_path / "shared").resolve()
    mount.mkdir()
    lose_after = {
        "bootstrap": "image-pull",
        "start": "palimpsest_bootstrap",
        "export": "palimpsest_hub_api",
    }[lost_before]
    environment, _ = _findmnt_fixture(tmp_path, mount, [_response(_metadata(mount))], lose_after=lose_after)
    records, _ = _run_lifecycle(
        tmp_path,
        ["bootstrap_service", "start"],
        _shared_settings(mount),
        hosts={"controller1": ["palimpsest"]},
        expect_success=False,
        environment=environment,
    )
    names = {args["name"] for _, args in _docker_records(records)}
    assert ("palimpsest_hub_api" in names) is (lost_before == "export")
    assert "palimpsest_hub_worker" not in names
    assert "palimpsest_hub_volume_owner" not in names
    assert ("palimpsest_bootstrap" in names) is (lost_before != "bootstrap")


def test_mount_outage_does_not_gate_pull_stop_destroy(tmp_path):
    missing = (tmp_path / "offline-share").resolve()
    environment, calls = _findmnt_fixture(tmp_path, missing, [{"stdout": "", "rc": 1}])
    records, _ = _run_lifecycle(
        tmp_path,
        ["pull", "stop", "destroy"],
        _shared_settings(missing),
        hosts=CONTROLLERS,
        environment=environment,
    )
    assert not calls.exists()
    assert _docker_records(records, "community.docker.docker_image")
    containers = _docker_records(records)
    assert containers and all(args["state"] in ("stopped", "absent") for _, args in containers)


def test_export_only_start_rejects_multi_host_local_storage(tmp_path):
    records, _ = _run_lifecycle(
        tmp_path,
        ["start"],
        _settings(enable_palimpsest_hub_api=False),
        hosts=CONTROLLERS,
        expect_success=False,
    )
    assert not _docker_records(records)


@pytest.mark.skipif(shutil.which("findmnt") is None, reason="util-linux findmnt is unavailable on this host")
def test_real_findmnt_rejects_existing_unmounted_local_directory(tmp_path):
    mount = (tmp_path / "ordinary-local-directory").resolve()
    mount.mkdir()
    records, _ = _run_lifecycle(
        tmp_path,
        ["start"],
        _shared_settings(mount),
        hosts={"controller1": ["palimpsest"]},
        expect_success=False,
    )
    assert not _docker_records(records)
    assert not list(mount.iterdir())


def test_remote_build_worker_never_adopts_or_checks_shared_hub_storage(tmp_path):
    records, _ = _run_lifecycle(
        tmp_path,
        ["deploy", "bootstrap_service", "stop", "destroy"],
        {
            "palimpsest_hub_storage_mode": "shared",
            "palimpsest_hub_shared_path": "",
            "palimpsest_hub_shared_fstype": "",
            "palimpsest_hub_shared_source": "",
        },
    )
    # No control-plane credentials or usable Hub storage settings were supplied.
    containers = _docker_records(records)
    assert containers
    assert {args["name"] for _, args in containers} == {
        "palimpsest_build_worker_scratch_init",
        "palimpsest_hub_build_worker",
    }
    for _, args in containers:
        assert all("/var/lib/palimpsest/hub" not in volume for volume in args.get("volumes", []))
        assert not any(key.startswith(("OS_", "DATABASE", "REDIS")) for key in args.get("env", {}))
