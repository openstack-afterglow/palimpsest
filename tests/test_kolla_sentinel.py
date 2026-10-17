"""Reject Redis discovery configurations that cannot serve controller HA."""

from __future__ import annotations

import pytest
from test_kolla_remote_build import _run_lifecycle

SENTINELS = [[f"192.0.2.{number}", 26379] for number in range(11, 14)]
CONTROLLERS = {f"controller{number}": ["palimpsest", "valkey"] for number in range(1, 4)}


@pytest.mark.parametrize(
    "addresses,master,db_index",
    [
        ([], "", 9),
        (SENTINELS[:2], "kolla", 9),
        ([SENTINELS[0]] * 3, "kolla", 9),
        ([SENTINELS[0], [SENTINELS[0][0], "26379"], SENTINELS[1]], "kolla", 9),
        (SENTINELS, "", 9),
        (SENTINELS, "kolla", 0),
    ],
    ids=[
        "standalone",
        "no-sentinel-majority-after-loss",
        "duplicate-endpoints",
        "coerced-duplicate-endpoints",
        "missing-master",
        "shared-db-zero",
    ],
)
def test_controller_ha_rejects_unsafe_cache_configuration(tmp_path, addresses, master, db_index):
    records, _ = _run_lifecycle(
        tmp_path,
        ["precheck_controller"],
        {
            "enable_palimpsest": True,
            "enable_palimpsest_hub_build_worker": False,
            "enable_valkey": True,
            "valkey_master_password": "controller-redis-secret",
            "palimpsest_redis_sentinel_addresses": addresses,
            "palimpsest_redis_sentinel_master": master,
            "palimpsest_redis_db_index": db_index,
            "palimpsest_database_password": "controller-db-secret",
            "palimpsest_keystone_password": "controller-keystone-secret",
            "palimpsest_reader_password": "controller-reader-secret",
            "palimpsest_package_forbidden_project_ids": ["protected-project"],
            "palimpsest_package_forbidden_user_ids": ["service-user"],
        },
        hosts=CONTROLLERS,
        expect_success=False,
    )
    assert not records
