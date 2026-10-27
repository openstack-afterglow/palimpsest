from __future__ import annotations

import copy
from html.parser import HTMLParser

import pytest

from palimpsest_local.benchmark_reporting import render_report

_METHODS = ("direct-install", "palimpsest-build", "palimpsest-import", "layer-allocate", "ansible")


def _stat(mean, minimum=None, maximum=None, cv=0.05):
    return {
        "mean": mean,
        "min": mean if minimum is None else minimum,
        "max": mean if maximum is None else maximum,
        "cv": cv,
    }


def _environment():
    profiles = {}
    for name in ("sequential-read", "sequential-write", "random-read-4k", "random-write-4k"):
        profiles[name] = {
            "status": "success",
            "protocol": "fio-direct",
            "aggregate": {
                "bandwidth_mib_per_second": _stat(64, 60, 68),
                "iops": _stat(16384, 15000, 17000),
                "latency_p95_ms": _stat(0.25),
                "latency_p99_ms": _stat(0.75),
            },
            "repetitions": [
                {"status": "success", "interval_summary": {"cv": 0.12, "maximum_gap_seconds": 0.2, "stalls": 1}},
                {"status": "success", "interval_summary": {"cv": 0.08, "maximum_gap_seconds": 0.4, "stalls": 2}},
            ],
        }
    external = {}
    for direction, count, code in (("download", 16777216, 200), ("upload", 4194304, 201)):
        external[direction] = {
            "status": "success",
            "protocol": "https",
            "aggregate": {
                "bandwidth_mib_per_second": _stat(12.5, 12, 13),
                "dns_seconds": _stat(0.0002),
                "tls_seconds": _stat(0.012),
                "bytes": _stat(count, cv=0),
            },
            "repetitions": [
                {
                    "status": "success",
                    "http_status_code": code,
                    "connectivity": {"dns": "success", "tls": "success", "http": "success"},
                }
            ],
        }
    internal = {
        direction: {
            "status": "success",
            "protocol": "tcp",
            "aggregate": {"bandwidth_mib_per_second": _stat(256), "retransmits": _stat(3)},
            "repetitions": [{"interval_summary": {"cv": 0.02, "maximum_gap_seconds": 0, "stalls": 0}}],
        }
        for direction in ("send", "receive")
    }
    return {
        "schema": "palimpsest.environment-result.v1",
        "status": "success",
        "host": {"hostname": "benchmark-worker", "machine": "x86_64"},
        "started_unix_seconds": 1792800000,
        "duration_seconds": 75.25,
        "proof": "/proof/preflight/result.json",
        "disk": {"status": "success", "profiles": profiles},
        "network": {
            "status": "success",
            "external": {"status": "success", "directions": external},
            "internal": {"status": "success", "directions": internal},
        },
    }


def _workflow():
    return {
        "schema": "palimpsest.benchmark-workflow-result.v1",
        "mode": "openstack",
        "job_id": "experiment-42",
        "status": "success",
        "started_unix_seconds": 1792800000,
        "duration_seconds": 123.75,
        "source_root": "/workspace/palimpsest",
        "flavor": {
            "requested": {"id": "cpu-small", "name": "cpu.2c_8g", "vcpus": 2, "ram": 8192, "disk": 20},
            "actual": {"id": "cpu-observed", "name": "cpu.4c_16g", "vcpus": 4, "ram": 16384, "disk": 40},
        },
        "environment": _environment(),
        "cases": [
            {
                "method": method,
                "status": "success",
                "elapsed_seconds": 10.25 + index,
                "cache_state": "cold",
                "preparation_scope": "dependency preparation included; boot excluded",
                "proof": {"path": f"/proof/{method}/result.json"},
            }
            for index, method in enumerate(_METHODS)
        ],
        "proof": "/proof/workflow/result.json",
    }


def test_report_sources_values_flavor_boundaries_and_all_five_methods_without_mutation():
    workflow = _workflow()
    before = copy.deepcopy(workflow)
    report = render_report([workflow])
    assert set(report) == {"markdown", "html", "issues_markdown"}
    markdown = report["markdown"]
    assert "experiment-42" in markdown
    assert "cpu.2c&#95;8g" in markdown
    assert "cpu.4c&#95;16g" in markdown
    assert "vcpus=4" in markdown and "ram=16384" in markdown and "disk=40" in markdown
    assert "64 [60–68]; CV 0.05" not in markdown  # Markdown metacharacters are encoded.
    assert "64 &#91;60–68&#93;; CV 0.05" in markdown
    assert "16384 &#91;15000–17000&#93;; CV 0.05" in markdown
    assert "| 0.25 | 0.75 | 0.4 | 3 |" in markdown
    assert "0.02–0.02 | 0 | 0 | 3" in markdown
    assert "0.0002" in markdown  # Small measured durations must not round to zero.
    assert "16777216" in markdown and "4194304" in markdown
    assert "| 200 |" in markdown and "| 201 |" in markdown
    for index, method in enumerate(_METHODS):
        assert f"| {method} | {10.25 + index}" in markdown
        assert f"/proof/{method}/result.json" in markdown
    assert "dependency preparation included; boot excluded" in markdown
    assert "/workspace/palimpsest" in markdown
    assert "fio-direct" in markdown
    assert "not physical Ceph OSD throughput" in markdown
    assert "No issues reported." in report["issues_markdown"]
    assert len(markdown.splitlines()) < 100
    assert workflow == before


def test_failed_timing_is_only_in_separate_issue_ledger_with_real_evidence():
    workflow = _workflow()
    workflow["status"] = "failed"
    case = workflow["cases"][1]
    case.update(status="failed", elapsed_seconds=9876.125, issue="Builder exited with a nonzero status")
    report = render_report([workflow])
    assert "9876.125" not in report["markdown"]
    assert "9876.125" not in report["html"]
    assert "9876.125" in report["issues_markdown"]
    assert "Builder exited with a nonzero status" in report["issues_markdown"]
    assert "/proof/palimpsest-build/result.json" in report["issues_markdown"]
    assert "<strong>palimpsest-build</strong><span>0 successful measured attempts" in report["html"]


@pytest.mark.parametrize("elapsed", [None, float("nan"), float("inf"), -1, True, "42"])
def test_non_measurements_do_not_become_successful_elapsed_times(elapsed):
    workflow = _workflow()
    workflow["cases"] = [{"method": "ansible", "status": "success", "elapsed_seconds": elapsed}]
    report = render_report([workflow])
    assert "No successful measurements recorded." in report["markdown"]
    assert "Successful case has no finite nonnegative elapsed time" in report["issues_markdown"]
    assert "No attempt recorded" in report["issues_markdown"]
    assert "nan" not in report["markdown"] and "inf" not in report["markdown"]


def test_missing_metrics_are_na_and_missing_required_directions_and_profiles_are_issues():
    environment = _environment()
    environment["disk"]["profiles"] = {"sequential-read": {"status": "success"}}
    del environment["network"]["internal"]["directions"]["receive"]
    report = render_report([environment])
    assert "| sequential-read | N/A | N/A | N/A | N/A | N/A | N/A | N/A |" in report["markdown"]
    assert "disk/random-write-4k" in report["issues_markdown"]
    assert "internal/receive" in report["issues_markdown"]
    assert "Bandwidth or IOPS aggregate is missing" in report["issues_markdown"]
    assert "No direction recorded" in report["issues_markdown"]


def test_partial_environment_retains_successful_leaf_measurements_and_repetition_failures():
    environment = _environment()
    environment["status"] = "failed"
    profile = environment["disk"]["profiles"]["sequential-write"]
    profile.update(status="failed", issue="fio unavailable")
    environment["disk"]["profiles"]["sequential-read"]["repetitions"].append(
        {
            "status": "failed",
            "issue": "short I/O",
            "elapsed_seconds": 9.5,
            "interval_summary": {"cv": 9999, "maximum_gap_seconds": 9999, "stalls": 9999},
        }
    )
    report = render_report([environment])
    assert "| sequential-write |" not in report["markdown"]
    assert "| sequential-read |" in report["markdown"]
    assert "fio unavailable" in report["issues_markdown"]
    assert "repetition 3" in report["issues_markdown"] and "short I/O" in report["issues_markdown"]
    assert "9999" not in report["markdown"]


def test_secrets_and_cloud_config_are_not_serialized_and_markup_is_inert():
    workflow = _workflow()
    workflow["config"] = {"clouds": {"production": {"auth": {"password": "config-password"}}}}
    workflow["flavor"]["actual"]["extra_specs"] = {"token": "extra-spec-secret"}
    workflow["host"] = {"hostname": '<img src=x onerror="alert(1)">'}
    workflow["cases"][0]["preparation_scope"] = "[bad](javascript:alert(1)) | <script>alert(1)</script>"
    workflow["issues"] = [
        'password="private-password" token=private-token Authorization: Bearer private-bearer',
        "https://private-user:private-url-password@example.net/evidence?signature=private-signature#private-fragment",
        "clouds:\n  production:\n    auth:\n      username: private-config-user\n      password: private-config-password",
    ]
    report = render_report([workflow])
    rendered = "\n".join(report.values())
    for secret in (
        "config-password",
        "extra-spec-secret",
        "private-password",
        "private-token",
        "private-bearer",
        "private-user",
        "private-url-password",
        "private-signature",
        "private-fragment",
        "private-config-user",
    ):
        assert secret not in rendered
    assert '<img src=x onerror="alert(1)">' not in report["html"]
    assert "<script>alert(1)</script>" not in rendered
    assert "&lt;script&gt;" in report["html"]
    assert "[bad](javascript:" not in report["markdown"]
    assert "&#124;" in report["markdown"]
    assert "https://example.net/evidence" in report["issues_markdown"]


class _Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.external = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.external.extend(value for key, value in attrs if key in ("src", "href"))


def test_html_is_standalone_accessible_native_text_not_fake_interactivity_or_charts():
    report = render_report([_workflow()])
    parser = _Elements()
    parser.feed(report["html"])
    assert not parser.external
    assert not set(parser.tags) & {"script", "button", "canvas", "iframe", "svg"}
    assert "table" in parser.tags and "th" in parser.tags and "nav" in parser.tags
    assert 'scope="col"' in report["html"]
    assert 'lang="en"' in report["html"]
    assert "@media(max-width:760px)" in report["html"]
    assert "@media print" in report["html"]
    assert "1 / Environment" in report["html"]
    assert "2 / Workflow" in report["html"]
    assert "3 / Evidence" in report["html"]
    assert "10.25" in report["html"]


def test_empty_unknown_and_nonobject_records_are_not_fabricated_successes():
    for records in ([], [None], [{"schema": "unrecognized", "config": {"token": "hidden"}}]):
        report = render_report(records)
        assert "No recognized result records supplied" in report["issues_markdown"]
        assert "Not measured." in report["markdown"]
        assert "hidden" not in "\n".join(report.values())
        assert "/path/to" not in "\n".join(report.values())
        assert "TODO" not in "\n".join(report.values())


def test_zero_elapsed_is_measured_and_no_evidence_path_is_invented():
    workflow = _workflow()
    workflow["cases"][0]["elapsed_seconds"] = 0
    del workflow["cases"][0]["proof"]
    report = render_report([workflow])
    row = next(line for line in report["markdown"].splitlines() if "| direct-install |" in line)
    assert "| 0 | cold |" in row
    assert row.endswith("| N/A |")


def test_udp_loss_jitter_are_rendered_only_when_supplied_without_relabeling_tcp():
    environment = _environment()
    direction = environment["network"]["internal"]["directions"]["send"]
    direction["protocol"] = "udp"
    direction["aggregate"].update(loss_percent=_stat(1.25), jitter_ms=_stat(0.03))
    report = render_report([environment])
    send = next(line for line in report["markdown"].splitlines() if "| internal | send |" in line)
    receive = next(line for line in report["markdown"].splitlines() if "| internal | receive |" in line)
    assert "| udp |" in send and send.endswith("| 1.25 | 0.03 |")
    assert "| tcp |" in receive and receive.endswith("| N/A | N/A |")
