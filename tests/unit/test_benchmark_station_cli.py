from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from palimpsest_local import benchmark_scenarios, benchmark_station_cloud, benchmark_station_measurements
from palimpsest_local import benchmark_station_cli as cli
from palimpsest_local.errors import ArtifactValidationError


def test_invalid_worker_window_cannot_authenticate_or_touch_resources(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid window cannot authenticate or construct a station")

    monkeypatch.setattr(benchmark_scenarios, "connection_from_stdin", forbidden)
    monkeypatch.setattr(benchmark_station_cloud, "StationCloud", forbidden)
    args = SimpleNamespace(station_action="run", duration_seconds=0, output=tmp_path, auth_stdin=True)
    with pytest.raises(ArtifactValidationError, match="duration"):
        cli.dispatch(args)


def test_station_status_is_read_only_and_excludes_bulk_probe_data(monkeypatch, tmp_path, capsys):
    (tmp_path / "station.json").write_text(
        json.dumps({"phase": "ready", "server_id": "retained-station", "data_volume_id": "owned500GiB"})
    )

    def forbidden(*args, **kwargs):
        pytest.fail("status cannot authenticate, allocate or execute probes")

    monkeypatch.setattr(benchmark_scenarios, "connection_from_stdin", forbidden)
    monkeypatch.setattr(benchmark_station_cloud, "StationCloud", forbidden)
    monkeypatch.setattr(benchmark_station_measurements, "run_measurement", forbidden)
    monkeypatch.setattr(
        benchmark_station_measurements,
        "latest_measurement",
        lambda *args, **kw: {
            "status": "partial",
            "attempt_id": "hour-cohort",
            "cache": {"reused": True, "usable": True},
            "environment": {
                "guest_logs": {"huge": "do-not-echo-bulk-data"},
                "network": {"external": {"combined": {"download": {"two_provider_mean_mib_per_second": None}}}},
            },
        },
    )
    assert cli.dispatch(SimpleNamespace(station_action="status", output=tmp_path)) == 0
    raw = capsys.readouterr().out
    value = json.loads(raw)
    assert value["station"]["server_id"] == "retained-station"
    assert value["retained"] is True
    assert value["latest_measurement"]["attempt_id"] == "hour-cohort"
    assert value["latest_measurement"]["provider_averages"]["download"]["two_provider_mean_mib_per_second"] is None
    assert "do-not-echo-bulk-data" not in raw


def test_worker_cannot_outlive_configured_adapter_deadline(monkeypatch, tmp_path):
    (tmp_path / "station-config.json").write_text(json.dumps({"deadline_seconds": 7200}))

    def forbidden(*args, **kwargs):
        pytest.fail("unsupported worker lifetime cannot touch provider resources")

    monkeypatch.setattr(benchmark_station_cloud, "StationCloud", forbidden)
    with pytest.raises(ArtifactValidationError, match="adapter deadline"):
        cli.dispatch(SimpleNamespace(station_action="run", duration_seconds=7201, output=tmp_path, auth_stdin=False))
