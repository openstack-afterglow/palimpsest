"""Unit resource queries must not inspect the developer's actual hypervisor."""

import pytest

from palimpsest_local import host_inventory


@pytest.fixture(autouse=True)
def _isolated_host_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_inventory, "observe", lambda: {"vms": [], "volumes": [], "networks": [], "warnings": []})
