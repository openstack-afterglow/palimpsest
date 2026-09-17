"""Cross-platform pytest process isolation."""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolated_host_journal(tmp_path_factory: pytest.TempPathFactory):
    """Keep command-journal writes out of host-global locations."""

    journal_root = tmp_path_factory.mktemp("host-journal")
    journal_root.chmod(0o700)
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("PALIMPSEST_LOG_HOME", str(journal_root))
        yield
