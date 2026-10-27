"""Secret-conscious, dependency-free benchmark summaries.

The renderer reads aggregate results only; it never opens evidence files or
serializes arbitrary configuration. Missing measurements are not zeroes and
unsuccessful attempts are kept in the separate issue report.
"""

from __future__ import annotations

import math
import re
from html import escape
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_METHODS = ("direct-install", "palimpsest-build", "palimpsest-import", "layer-allocate", "ansible")
_ENVIRONMENT_SCHEMA = "palimpsest.environment-result.v1"
_WORKFLOW_SCHEMA = "palimpsest.benchmark-workflow-result.v1"
_NA = "N/A"
_BOUNDARY = (
    "Disk measures the selected guest/filesystem path, not physical Ceph OSD throughput. "
    "External HTTP transfers and internal iperf3 directions are separate measurements. "
    "Workflow elapsed times cover only the stated preparation/cache scope; they are not interchangeable. "
    "N/A means unmeasured. Failed attempts are excluded from success tables and retained in the issue report."
)
_SECRET = re.compile(
    r"(?i)\b(?:password|passwd|token|secret|authorization|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|credential|OS_PASSWORD|OS_TOKEN|PALIMPSEST_TOKEN)\b"
    r"[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _items(value: Any) -> list:
    return value if isinstance(value, (list, tuple)) else []


def _safe_text(value: Any) -> str:
    """Render scalars, redact common credential forms, and never dump mappings."""
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return _NA
    text = str(value)
    if re.search(r"(?im)(?:[\"']?(?:clouds|auth)[\"']?\s*:|\bOS_(?:AUTH_URL|USERNAME|PROJECT_NAME)\s*[:=])", text):
        return "[redacted cloud configuration]"
    text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*", "[redacted]", text, flags=re.DOTALL)
    text = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [redacted]", text)
    text = _SECRET.sub("[redacted]", text)

    def clean_url(match: re.Match) -> str:
        try:
            parsed = urlsplit(match.group())
            # Queries/fragments may contain signed credentials; they are never report material.
            host = parsed.hostname or ""
            if ":" in host:
                host = f"[{host}]"
            port = f":{parsed.port}" if parsed.port else ""
            return urlunsplit((parsed.scheme, host + port, parsed.path, "", ""))
        except ValueError:
            return "[redacted URL]"

    text = _URL.sub(clean_url, text)
    text = " ".join(text.split())
    return text or _NA


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _fmt(value: Any) -> str:
    number = _number(value)
    if number is None:
        return _NA
    if 0 < number < 0.001 or number >= 1e15:
        return f"{number:.3g}"
    return f"{number:.3f}".rstrip("0").rstrip(".")


def _first(data: dict, *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _mean(value: Any) -> Any:
    return _first(value, "mean", "mean_value") if isinstance(value, dict) else value


def _stat(value: Any) -> str:
    """Mean [min–max]; CV is a coefficient, not an inferred percentage."""
    if not isinstance(value, dict):
        return _fmt(value)
    return f"{_fmt(_mean(value))} [{_fmt(value.get('min'))}–{_fmt(value.get('max'))}]; CV {_fmt(value.get('cv'))}"


def _metric(data: dict, *keys: str) -> Any:
    # A leaf may carry its measurements directly or under its aggregate key.
    aggregate = _mapping(data.get("aggregate"))
    return _first(aggregate, *keys) if any(key in aggregate for key in keys) else _first(data, *keys)


def _intervals(data: dict) -> tuple[str, str, str]:
    """Reported interval CV range, worst gap, and total stalls across repetitions."""
    summaries = [
        _mapping(rep.get("interval_summary"))
        for rep in _items(data.get("repetitions"))
        if isinstance(rep, dict) and rep.get("status", "success") == "success"
    ]
    cvs = [number for item in summaries if (number := _number(item.get("cv"))) is not None]
    gaps = [number for item in summaries if (number := _number(item.get("maximum_gap_seconds"))) is not None]
    stalls = [number for item in summaries if (number := _number(item.get("stalls"))) is not None]
    cv = f"{_fmt(min(cvs))}–{_fmt(max(cvs))}" if cvs else _fmt(_mean(_metric(data, "interval_cv")))
    gap = _fmt(max(gaps)) if gaps else _fmt(_mean(_metric(data, "maximum_interval_gap_seconds")))
    stall = _fmt(sum(stalls)) if stalls else _fmt(_mean(_metric(data, "stall_count")))
    return cv, gap, stall


def _proof(data: dict) -> str:
    value = _first(data, "proof", "result_path", "evidence_path")
    if isinstance(value, dict):
        value = _first(value, "path", "result_path", "directory")
    return _safe_text(value)


def _flavor(value: Any) -> str:
    if not isinstance(value, dict):
        return _safe_text(value)
    parts = []
    for key in ("id", "name", "vcpus", "ram_mib", "ram", "disk_gib", "disk", "ephemeral_gib", "swap_mib"):
        if key in value and not isinstance(value[key], (dict, list, tuple)):
            parts.append(f"{key}={_safe_text(value[key])}")
    return ", ".join(parts) or _NA


def _flavors(record: dict) -> tuple[str, str]:
    flavor = _mapping(record.get("flavor"))
    requested = _first(flavor, "requested", "requested_flavor")
    actual = _first(flavor, "actual", "actual_flavor")
    if requested is None:
        requested = record.get("requested_flavor")
    if actual is None:
        actual = record.get("actual_flavor")
    # A flat flavor is observed only when the producer explicitly labels it actual.
    return _flavor(requested), _flavor(actual)


def _markdown_table(headers: tuple[str, ...], rows: list[list[str]]) -> str:
    def cell(value: str) -> str:
        value = escape(value, quote=False).replace("\n", " ")
        for character in "|[]`*_!\\":
            value = value.replace(character, f"&#{ord(character)};")
        return value

    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    output.extend("| " + " | ".join(cell(value) for value in row) + " |" for row in rows)
    return "\n".join(output)


def _html_table(headers: tuple[str, ...], rows: list[list[str]]) -> str:
    heading = "".join(f'<th scope="col">{escape(value)}</th>' for value in headers)
    body = "".join("<tr>" + "".join(f"<td>{escape(value)}</td>" for value in row) + "</tr>" for row in rows)
    return f'<div class="table-wrap"><table><thead><tr>{heading}</tr></thead><tbody>{body}</tbody></table></div>'


def render_report(results: list[dict]) -> dict[str, str]:
    """Return concise Markdown/standalone HTML and a separate issue ledger.

    Environment leaves use ``status=success`` and aggregate metrics (mean/min/max/cv).
    Workflow cases use ``elapsed_seconds``, ``cache_state``, ``preparation_scope``
    and ``proof``. Unknown schemas/statuses and absent fields remain visible as
    issues, never fabricated successful measurements.
    """
    metadata: list[list[str]] = []
    disks: list[list[str]] = []
    networks: list[list[str]] = []
    connectivity: list[list[str]] = []
    timings: list[list[str]] = []
    issues: list[list[str]] = []
    method_counts = dict.fromkeys(_METHODS, 0)
    environments: list[tuple[str, dict]] = []
    seen_environments: set[int] = set()

    def issue(
        run: str,
        component: str,
        status: Any,
        message: Any,
        proof: Any = None,
        elapsed: Any = None,
    ) -> None:
        issues.append([run, component, _safe_text(status), _safe_text(message), _fmt(elapsed), _safe_text(proof)])

    def supplied_issues(run: str, component: str, data: dict) -> None:
        entries = data.get("issues")
        if isinstance(entries, str):
            entries = [entries]
        for entry in _items(entries):
            if isinstance(entry, dict):
                issue(
                    run,
                    component,
                    entry.get("status", "issue"),
                    _first(entry, "message", "issue", "code"),
                    _proof(entry),
                )
            else:
                issue(run, component, "issue", entry, _proof(data))
        for index, rep in enumerate(_items(data.get("repetitions")), 1):
            if not isinstance(rep, dict):
                issue(run, f"{component}/repetition {index}", "unknown", "Repetition is not an object")
            elif rep.get("status", "success") != "success":
                issue(
                    run,
                    f"{component}/repetition {index}",
                    rep.get("status"),
                    _first(rep, "issue", "error") or "Repetition did not report success",
                    _proof(rep),
                    rep.get("elapsed_seconds"),
                )

    def add_environment(run: str, environment: dict) -> None:
        if id(environment) not in seen_environments:
            seen_environments.add(id(environment))
            environments.append((run, environment))

    for index, raw in enumerate(results, 1):
        if not isinstance(raw, dict):
            issue(f"record {index}", "input", "unknown", "Record is not an object")
            continue
        run = _safe_text(_first(raw, "job_id", "run_id", "id") or f"record {index}")
        schema = raw.get("schema")
        if schema not in (_ENVIRONMENT_SCHEMA, _WORKFLOW_SCHEMA):
            issue(run, "input", "unknown", "Unsupported or missing result schema", _proof(raw))
            continue
        requested, actual = _flavors(raw)
        host = _mapping(raw.get("host"))
        nested_environment = _mapping(raw.get("environment"))
        if not host:
            host = _mapping(nested_environment.get("host"))
        hostname = _safe_text(_first(host, "hostname", "node"))
        metadata.append(
            [
                run,
                _safe_text(raw.get("mode", "environment" if schema == _ENVIRONMENT_SCHEMA else None)),
                _safe_text(raw.get("status")),
                hostname,
                requested,
                actual,
                _fmt(raw.get("started_unix_seconds")),
                _fmt(raw.get("duration_seconds")),
                _safe_text(raw.get("source_root")),
                _proof(raw),
            ]
        )
        supplied_issues(run, "run", raw)
        if raw.get("status") != "success":
            issue(
                run,
                "run",
                raw.get("status", "unknown"),
                _first(raw, "issue", "error") or "Run did not report success",
                _proof(raw),
            )
        if schema == _ENVIRONMENT_SCHEMA:
            add_environment(run, raw)
            continue
        if nested_environment:
            add_environment(run, nested_environment)
        else:
            issue(run, "environment", "not-measured", "No environment preflight result", _proof(raw))
        case_flavors = [
            case.get("flavor")
            for case in _items(raw.get("cases"))
            if isinstance(case, dict) and isinstance(case.get("flavor"), dict)
        ]
        if (
            raw.get("mode") == "openstack"
            and actual == _NA
            and not case_flavors
            and not _mapping(nested_environment.get("flavor"))
        ):
            issue(run, "flavor", "unknown", "Actual flavor specifications were not recorded", _proof(raw))
        seen_methods = set()
        for case in _items(raw.get("cases")):
            if not isinstance(case, dict):
                issue(run, "workflow", "unknown", "Case is not an object", _proof(raw))
                continue
            method = case.get("method")
            if method not in _METHODS:
                issue(run, "workflow", "unknown", "Unsupported or missing method", _proof(case))
                continue
            seen_methods.add(method)
            supplied_issues(run, method, case)
            if case.get("status") != "success":
                issue(
                    run,
                    method,
                    case.get("status", "unknown"),
                    _first(case, "issue", "error") or "Attempt did not report success",
                    _proof(case),
                    case.get("elapsed_seconds"),
                )
                continue
            if _number(case.get("elapsed_seconds")) is None:
                issue(
                    run, method, "not-measured", "Successful case has no finite nonnegative elapsed time", _proof(case)
                )
                continue
            method_counts[method] += 1
            case_flavor = _mapping(case.get("flavor"))
            case_actual = _flavors(case)[1] if case_flavor else actual
            timings.append(
                [
                    run,
                    method,
                    _fmt(case.get("elapsed_seconds")),
                    _safe_text(case.get("cache_state")),
                    _safe_text(_first(case, "preparation_scope", "scope")),
                    case_actual,
                    _proof(case),
                ]
            )
            for field in ("cache_state", "preparation_scope"):
                if _first(case, field, "scope" if field == "preparation_scope" else field) is None:
                    issue(run, method, "unknown", f"{field} was not recorded", _proof(case))
        for method in _METHODS:
            if method not in seen_methods:
                issue(run, method, "not-measured", "No attempt recorded", _proof(raw))

    for run, environment in environments:
        supplied_issues(run, "environment", environment)
        if environment.get("status") != "success":
            issue(
                run,
                "environment",
                environment.get("status", "unknown"),
                "Environment preflight is incomplete",
                _proof(environment),
            )
        disk = _mapping(environment.get("disk"))
        supplied_issues(run, "disk", disk)
        if disk.get("status") != "success":
            issue(run, "disk", disk.get("status", "not-measured"), "Disk preflight is incomplete", _proof(disk))
        profiles = disk.get("profiles")
        if isinstance(profiles, dict):
            profiles = [dict(_mapping(value), profile=key) for key, value in profiles.items()]
        profiles = _items(profiles)
        if not profiles:
            issue(run, "disk", disk.get("status", "not-measured"), "No disk profiles measured", _proof(disk))
        profile_names = {_safe_text(_first(item, "profile", "name")) for item in profiles if isinstance(item, dict)}
        for required in ("sequential-read", "sequential-write", "random-read-4k", "random-write-4k"):
            if required not in profile_names:
                issue(run, f"disk/{required}", "not-measured", "No profile recorded", _proof(disk))
        for profile in profiles:
            if not isinstance(profile, dict):
                issue(run, "disk", "unknown", "Disk profile is not an object", _proof(disk))
                continue
            name = _safe_text(_first(profile, "profile", "name"))
            supplied_issues(run, f"disk/{name}", profile)
            if profile.get("status") != "success":
                issue(
                    run,
                    f"disk/{name}",
                    profile.get("status", "unknown"),
                    _first(profile, "issue", "error") or "Profile did not report success",
                    _proof(profile),
                )
                continue
            bandwidth = _metric(profile, "bandwidth_mib_per_second", "bandwidth_mib_s")
            iops = _metric(profile, "iops")
            _, gap, stalls = _intervals(profile)
            disks.append(
                [
                    run,
                    name,
                    _safe_text(profile.get("protocol")),
                    _stat(bandwidth),
                    _stat(iops),
                    _fmt(_mean(_metric(profile, "latency_p95_ms"))),
                    _fmt(_mean(_metric(profile, "latency_p99_ms"))),
                    gap,
                    stalls,
                ]
            )
            if _number(_mean(bandwidth)) is None or _number(_mean(iops)) is None:
                issue(run, f"disk/{name}", "not-measured", "Bandwidth or IOPS aggregate is missing", _proof(profile))
            missing = [
                name for name in ("latency_p95_ms", "latency_p99_ms") if _number(_mean(_metric(profile, name))) is None
            ]
            if gap == _NA or stalls == _NA:
                missing.append("interval gaps/stalls")
            if missing:
                issue(run, f"disk/{name}", "not-measured", "Missing " + ", ".join(missing), _proof(profile))
        network = _mapping(environment.get("network"))
        supplied_issues(run, "network", network)
        for section in ("external", "internal"):
            data = _mapping(network.get(section))
            supplied_issues(run, section, data)
            if not data:
                issue(run, section, "not-measured", "No network result", _proof(environment))
                continue
            if data.get("status") != "success":
                issue(
                    run,
                    section,
                    data.get("status", "unknown"),
                    _first(data, "issue", "error") or "Network component is incomplete",
                    _proof(data),
                )
            directions = data.get("directions")
            if isinstance(directions, dict):
                directions = [dict(_mapping(value), direction=key) for key, value in directions.items()]
            if not directions:
                directions = [
                    dict(_mapping(data[key]), direction=key)
                    for key in ("download", "upload", "send", "receive")
                    if isinstance(data.get(key), dict)
                ]
            if not directions:
                issue(run, section, "not-measured", "No directional network measurements", _proof(data))
            names = {_safe_text(item.get("direction")) for item in _items(directions) if isinstance(item, dict)}
            for required in ("download", "upload") if section == "external" else ("send", "receive"):
                if required not in names:
                    issue(run, f"{section}/{required}", "not-measured", "No direction recorded", _proof(data))
            for direction in _items(directions):
                if not isinstance(direction, dict):
                    continue
                name = _safe_text(direction.get("direction"))
                if section == "external":
                    repetitions = [rep for rep in _items(direction.get("repetitions")) if isinstance(rep, dict)]
                    checks = _mapping(direction.get("connectivity")) or _mapping(data.get("connectivity"))
                    check_values = []
                    for check in ("dns", "tls", "http"):
                        values = (
                            [checks[check]]
                            if check in checks
                            else [
                                _mapping(rep.get("connectivity"))[check]
                                for rep in repetitions
                                if check in _mapping(rep.get("connectivity"))
                            ]
                        )
                        check_values.append(", ".join(dict.fromkeys(_safe_text(value) for value in values)) or _NA)
                    codes = (
                        ", ".join(
                            dict.fromkeys(
                                _safe_text(rep["http_status_code"]) for rep in repetitions if "http_status_code" in rep
                            )
                        )
                        or _NA
                    )
                    connectivity.append(
                        [
                            run,
                            name,
                            *check_values,
                            codes,
                            _fmt(_mean(_metric(direction, "dns_seconds"))),
                            _fmt(_mean(_metric(direction, "tls_seconds"))),
                            _stat(_metric(direction, "bytes")),
                        ]
                    )
                    if _NA in check_values:
                        issue(
                            run,
                            f"external/{name}",
                            "unknown",
                            "DNS, TLS or HTTP connectivity status is missing",
                            _proof(direction),
                        )
                supplied_issues(run, f"{section}/{name}", direction)
                if direction.get("status") != "success":
                    issue(
                        run,
                        f"{section}/{name}",
                        direction.get("status", "unknown"),
                        _first(direction, "issue", "error") or "Direction did not report success",
                        _proof(direction),
                    )
                    continue
                bandwidth = _metric(direction, "bandwidth_mib_per_second", "bandwidth_mib_s")
                interval_cv, gap, stalls = _intervals(direction)
                networks.append(
                    [
                        run,
                        section,
                        name,
                        _safe_text(direction.get("protocol", data.get("protocol"))),
                        _stat(bandwidth),
                        interval_cv,
                        gap,
                        stalls,
                        _fmt(_mean(_metric(direction, "retransmits"))),
                        _fmt(_mean(_metric(direction, "loss_percent"))),
                        _fmt(_mean(_metric(direction, "jitter_ms"))),
                    ]
                )
                if _number(_mean(bandwidth)) is None:
                    issue(run, f"{section}/{name}", "not-measured", "Bandwidth aggregate is missing", _proof(direction))

    if not metadata:
        issue("report", "input", "not-measured", "No recognized result records supplied")
    # The same environment object may be supplied both independently and nested.
    issues = list(dict.fromkeys(tuple(row) for row in issues))
    sections = [
        (
            "Run and flavor evidence",
            (
                "Run",
                "Mode",
                "Status",
                "Host",
                "Requested flavor",
                "Actual flavor",
                "Started Unix s",
                "Duration s",
                "Source root",
                "Evidence",
            ),
            metadata,
        ),
        (
            "Disk · successful profiles",
            (
                "Run",
                "Profile",
                "Protocol",
                "BW MiB/s mean [min–max]; CV",
                "IOPS mean [min–max]; CV",
                "p95 ms rep mean",
                "p99 ms rep mean",
                "Max gap s",
                "Stalls",
            ),
            disks,
        ),
        (
            "External connectivity",
            (
                "Run",
                "Direction",
                "DNS",
                "TLS",
                "HTTP",
                "HTTP codes",
                "DNS s rep mean",
                "TLS s rep mean",
                "Bytes mean [min–max]; CV",
            ),
            connectivity,
        ),
        (
            "Network · successful directions",
            (
                "Run",
                "Boundary",
                "Direction",
                "Protocol",
                "MiB/s mean [min–max]; CV",
                "Interval CV min–max",
                "Max gap s",
                "Stalls",
                "Retransmits",
                "Loss %",
                "Jitter ms",
            ),
            networks,
        ),
        (
            "Workflow · successful attempts",
            ("Run", "Method", "Elapsed s", "Cache state", "Preparation / timing scope", "Actual flavor", "Evidence"),
            timings,
        ),
    ]
    markdown = ["# Benchmark report", _BOUNDARY]
    for title, headers, rows in sections:
        markdown.extend(
            [
                f"## {title}",
                _markdown_table(headers, rows)
                if rows
                else "No successful measurements recorded."
                if "successful" in title
                else "Not measured.",
            ]
        )
    markdown.extend(
        ["## Issues", f"{len(issues)} issue entries; failed and unknown measurements are recorded separately."]
    )
    issues_markdown = [
        "# Benchmark issues",
        "Failed, incomplete and unknown measurements; none is counted as a successful timing.",
    ]
    if issues:
        status_counts: dict[str, int] = {}
        for row in issues:
            status_counts[row[2]] = status_counts.get(row[2], 0) + 1
        issues_markdown.append(
            _markdown_table(
                ("Status", "Issue entries (not independent attempts)"),
                [[status, str(count)] for status, count in sorted(status_counts.items())],
            )
        )
        issues_markdown.append(
            _markdown_table(("Run", "Component", "Status", "Issue", "Attempt elapsed s", "Evidence"), issues)
        )
    else:
        issues_markdown.append("No issues reported.")
    html_sections = "".join(
        f"<section><h2>{escape(title)}</h2>{_html_table(headers, rows) if rows else '<p>Not measured.</p>'}</section>"
        for title, headers, rows in sections
    )
    method_html = "".join(
        f"<li><strong>{escape(method)}</strong><span>{method_counts[method]} successful measured attempts</span></li>"
        for method in _METHODS
    )
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Benchmark report</title><style>
:root{{color-scheme:light;--ink:#2f3437;--muted:#686762;--line:#eaeaea;--paper:#f7f6f3}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:14px/1.6 -apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif}}
main{{max-width:1400px;margin:auto;padding:48px 28px}}header{{border-bottom:1px solid var(--line);padding-bottom:24px}}
h1{{font:46px/1.1 Georgia,serif;letter-spacing:-.03em;margin:8px 0 16px}}h2{{font-size:18px;margin:0 0 16px}}p{{max-width:100ch}}.eyebrow{{font:12px monospace;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}}
.scope{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin:24px 0}}.scope article,section{{background:#fff;border:1px solid var(--line);border-radius:8px;padding:24px;min-width:0}}.scope h2{{margin-bottom:8px}}.scope p{{margin:0;color:var(--muted)}}section{{margin-top:20px}}
ol{{list-style:none;padding:0;display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px}}li{{border-top:2px solid #cbd9c8;padding:12px 0;overflow-wrap:anywhere}}li span{{display:block;color:var(--muted);font-size:12px}}
.table-wrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:12px}}th,td{{padding:10px 12px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line);overflow-wrap:anywhere}}th{{font-weight:600;color:var(--muted)}}td{{font-variant-numeric:tabular-nums}}.notice{{border-left:3px solid #c9aa64;padding-left:16px}}footer{{color:var(--muted);margin-top:24px}}
@media(max-width:760px){{main{{padding:24px 16px}}h1{{font-size:36px}}.scope{{grid-template-columns:1fr}}ol{{grid-template-columns:1fr 1fr}}section{{padding:16px}}}}
@media print{{body{{background:white}}main{{max-width:none;padding:0}}section,.scope article{{break-inside:avoid}}.table-wrap{{overflow:visible}}table{{font-size:9px}}}}
</style></head><body><main><header><div class="eyebrow">Palimpsest · measurement record</div>
<h1>Benchmark report</h1><p>{escape(_BOUNDARY)}</p></header>
<div class="scope" aria-label="Measurement boundaries"><article><h2>1 / Environment</h2><p>Filesystem I/O and external HTTP / internal iperf3 probes. These are preflight measurements, not workflow timings.</p></article>
<article><h2>2 / Workflow</h2><p>Five methods, with successful elapsed time shown alongside its recorded cache state, preparation scope and flavor.</p></article>
<article><h2>3 / Evidence</h2><p>Recorded paths identify supplied evidence. Missing values remain N/A; incomplete attempts remain in the separate issue ledger.</p></article></div>
<nav aria-label="Method coverage"><ol>{method_html}</ol></nav>{html_sections}
<section class="notice"><h2>Separate issue ledger</h2><p>{len(issues)} issue entries. Consult the separately emitted issues report for failures and unknown measurements.</p></section>
<footer>CV is the reported coefficient of variation (ratio). Values are not inferred from missing samples. No physical storage or cross-boundary equivalence is claimed.</footer>
</main></body></html>"""
    return {
        "markdown": "\n\n".join(markdown) + "\n",
        "html": html,
        "issues_markdown": "\n\n".join(issues_markdown) + "\n",
    }
