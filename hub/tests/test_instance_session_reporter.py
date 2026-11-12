"""Portable cloud-init and local HTTP proof for the lifetime session reporter."""

import base64
import json
import shlex
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest
import yaml

from palimpsest_hub.services.instance_bootstrap import GUEST_RUNNER, SESSION_REPORTER, render_bootstrap
from palimpsest_hub.services.session_observer import GUEST_PROBE_COMMAND, parse_who

ROOT = "/var/lib/palimpsest-instance/"
CAPABILITY = "session-test-capability-not-a-real-credential"
PROBE_TEXT = "PALIMPSEST-UTMP-OK\nPALIMPSEST-SELF 42\n1 0\n42 1\nPALIMPSEST-END\n"


def spec():
    return {
        "origin": "https://hub.example.test",
        "instance_id": "instance-id",
        "capability": "separate-bootstrap-capability",
        "expires_at": 1,
    }


def rendered_files(payload):
    return {item["path"]: (item, base64.b64decode(item["content"]).decode("utf-8")) for item in payload["write_files"]}


def reporter_namespace():
    payload = json.loads(render_bootstrap({**spec(), "session_report": report_config()}).split("\n", 1)[1])
    source = rendered_files(payload)[ROOT + "session_reporter.py"][1]
    namespace = {"__name__": "session_reporter_under_test"}
    exec(compile(source, "session_reporter.py", "exec"), namespace)
    return namespace


def report_config():
    return {"capability": CAPABILITY, "interval_seconds": 30}


def test_cpu_bootstrap_without_session_report_keeps_previous_files_commands_and_bytes():
    original_spec = spec()
    rendered = render_bootstrap(original_spec)
    payload = json.loads(rendered.split("\n", 1)[1])
    assert [item["path"] for item in payload["write_files"]] == [
        ROOT + "spec.json",
        ROOT + "runner.py",
        "/etc/systemd/system/palimpsest-instance.service",
    ]
    assert payload["runcmd"] == [
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", "palimpsest-instance.service"],
    ]
    unit = """[Unit]
Description=Palimpsest verified instance runtime
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
ExecStart=/usr/bin/python3 /var/lib/palimpsest-instance/runner.py
PrivateMounts=yes
KillMode=control-group
Restart=no
[Install]
WantedBy=multi-user.target
"""
    previous_payload = {
        "write_files": [
            {
                "path": path,
                "permissions": permissions,
                "encoding": "b64",
                "content": base64.b64encode(content.encode()).decode(),
            }
            for path, content, permissions in [
                (ROOT + "spec.json", json.dumps(original_spec, separators=(",", ":")), "0600"),
                (ROOT + "runner.py", GUEST_RUNNER, "0600"),
                ("/etc/systemd/system/palimpsest-instance.service", unit, "0644"),
            ]
        ],
        "runcmd": [
            ["systemctl", "daemon-reload"],
            ["systemctl", "enable", "--now", "palimpsest-instance.service"],
        ],
    }
    assert rendered == "#cloud-config\n" + json.dumps(previous_payload, separators=(",", ":")) + "\n"
    assert "reporter" not in rendered
    assert yaml.safe_load(rendered) == payload


@pytest.mark.parametrize("session_report", [None, False, "not-a-config", []])
def test_only_dict_session_report_installs_reporter(session_report):
    payload = json.loads(render_bootstrap({**spec(), "session_report": session_report}).split("\n", 1)[1])
    assert len(payload["write_files"]) == 3
    assert len(payload["runcmd"]) == 2


def test_dev_cloud_config_keeps_reporter_private_and_separate_from_expiring_spec():
    original_spec = {**spec(), "session_report": report_config(), "ca_certificates": ["test-ca"]}
    rendered = render_bootstrap(original_spec)
    payload = json.loads(rendered.split("\n", 1)[1])
    assert yaml.safe_load(rendered) == payload
    assert payload["ca_certs"] == {"trusted": ["test-ca"]}
    files = rendered_files(payload)
    runner_spec_text = files[ROOT + "spec.json"][1]
    assert json.loads(runner_spec_text) == {**spec(), "ca_certificates": ["test-ca"]}
    assert CAPABILITY not in runner_spec_text
    assert original_spec["session_report"] == report_config()
    config_file, config_text = files[ROOT + "session.json"]
    assert config_file["permissions"] == "0600"
    assert json.loads(config_text) == {
        "origin": spec()["origin"],
        "instance_id": spec()["instance_id"],
        **report_config(),
    }
    reporter_file, source = files[ROOT + "session_reporter.py"]
    assert reporter_file["permissions"] == "0700"
    assert int(reporter_file["permissions"], 8) & 0o077 == 0
    assert source == SESSION_REPORTER and "spec.json" not in source and CAPABILITY not in source
    compile(source, "session_reporter.py", "exec")
    namespace = reporter_namespace()
    assert namespace["PROBE"] == GUEST_PROBE_COMMAND
    assert str(namespace["CONFIG"]) == ROOT + "session.json"
    unit = files["/etc/systemd/system/palimpsest-session-reporter.service"][1]
    for setting in (
        "Type=simple",
        "Restart=always",
        "RestartSec=10",
        "After=network-online.target",
        "WantedBy=multi-user.target",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
    ):
        assert setting in unit.splitlines()
    assert CAPABILITY not in unit and CAPABILITY not in json.dumps(payload["runcmd"])
    assert not any(line.startswith("ProtectProc=") for line in unit.splitlines())
    assert payload["runcmd"] == [
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", "palimpsest-instance.service"],
        ["systemctl", "enable", "--now", "palimpsest-session-reporter.service"],
    ]


@pytest.fixture
def local_receiver():
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_PUT(self):
            received.append(
                (
                    self.path,
                    self.headers["Authorization"],
                    self.headers["Content-Type"],
                    json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                )
            )
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            {"origin": f"http://127.0.0.1:{server.server_port}", "instance_id": "instance-id", **report_config()},
            received,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_reporter_one_iteration_puts_exact_probe_and_failed_probe_posts_nothing(local_receiver):
    config, received = local_receiver
    namespace = reporter_namespace()
    assert parse_who(PROBE_TEXT) == set()
    namespace["PROBE"] = "printf '%s' " + shlex.quote(PROBE_TEXT)
    namespace["run_once"](config)
    assert received == [
        (
            "/v1/instance-bootstrap/instance-id/sessions",
            "Bearer " + CAPABILITY,
            "application/json",
            {"probe": PROBE_TEXT},
        )
    ]
    namespace["PROBE"] = "exit 3"
    namespace["run_once"](config)
    assert len(received) == 1
    namespace["PROBE"] = "printf '\\377'"
    namespace["run_once"](config)
    assert len(received) == 1


@pytest.mark.parametrize("stdout", [b"x" * 65536, b"x" * 65537, b"\xff"])
def test_reporter_obeys_exact_probe_size_boundary_and_utf8(monkeypatch, local_receiver, stdout):
    config, received = local_receiver
    namespace = reporter_namespace()

    def run(args, **kwargs):
        assert args == ["/bin/sh", "-c", GUEST_PROBE_COMMAND]
        assert kwargs == {"stdin": subprocess.DEVNULL, "capture_output": True, "timeout": 10}
        return SimpleNamespace(returncode=0, stdout=stdout)

    monkeypatch.setattr(namespace["subprocess"], "run", run)
    namespace["run_once"](config)
    if len(stdout) == 65536:
        assert received == [
            (
                "/v1/instance-bootstrap/instance-id/sessions",
                "Bearer " + CAPABILITY,
                "application/json",
                {"probe": stdout.decode("utf-8")},
            )
        ]
    else:
        assert received == []


@pytest.mark.parametrize("failure_stage", ["probe", "http"])
def test_reporter_swallows_errors_without_logging_secrets(monkeypatch, capsys, failure_stage):
    namespace = reporter_namespace()
    namespace["PROBE"] = "printf '%s' " + shlex.quote(PROBE_TEXT)

    def fail(*args, **kwargs):
        raise OSError(CAPABILITY)

    if failure_stage == "probe":
        monkeypatch.setattr(namespace["subprocess"], "run", fail)
    else:
        monkeypatch.setattr(namespace["urllib"].request, "urlopen", fail)
    namespace["run_once"]({**spec(), **report_config()})
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize(("requested", "expected"), [(1, 10), (30, 30), (300, 120)])
def test_reporter_main_reads_only_persistent_session_config_and_clamps_interval(tmp_path, requested, expected):
    namespace = reporter_namespace()
    config = {**spec(), **report_config(), "interval_seconds": requested}
    config_path = tmp_path / "session.json"
    config_path.write_text(json.dumps(config))
    namespace["CONFIG"] = config_path
    calls = []
    namespace["run_once"] = calls.append

    class StopLoop(BaseException):
        pass

    def sleep(interval):
        assert interval == expected
        raise StopLoop

    namespace["time"] = SimpleNamespace(sleep=sleep)
    with pytest.raises(StopLoop):
        namespace["main"]()
    assert calls == [config]


@pytest.mark.parametrize("contents", [None, "{not-json"])
def test_reporter_survives_missing_or_corrupt_config_with_default_sleep(tmp_path, contents):
    namespace = reporter_namespace()
    config_path = tmp_path / "session.json"
    if contents is not None:
        config_path.write_text(contents)
    namespace["CONFIG"] = config_path
    calls = []
    sleeps = []
    namespace["run_once"] = calls.append

    class StopLoop(BaseException):
        pass

    def sleep(interval):
        sleeps.append(interval)
        if len(sleeps) == 2:
            raise StopLoop

    namespace["time"] = SimpleNamespace(sleep=sleep)
    with pytest.raises(StopLoop):
        namespace["main"]()
    assert calls == []
    assert sleeps == [30, 30]


def representative_spec(gpu=False, dev=False):
    identity = "11111111-1111-4111-8111-111111111111"
    config = {
        **spec(),
        "instance_id": identity,
        "capability": "b" * 43,
        "expires_at": 1900000000,
        "layers": [{"digest": "sha256:" + "a" * 64, "size_bytes": 1048576}],
        "mode": "command",
        "runtime": {"entrypoint": [], "cmd": ["/app/proof"], "env": [], "working_dir": "/", "user": "1000:1000"},
        "writable_bytes": 4 * 1024**3,
        "command": ["/app/proof"],
        "readiness_argv": [],
        "ca_certificates": [],
    }
    if gpu:
        # Match the rtx-qualified SQL/provider fixture in test_instances.py.
        config["gpu"] = {
            "profile": "rtx-qualified",
            "image_id": identity,
            "flavor_id": identity,
            "project_id": identity,
            "pci_alias": "RTX-3060-LHR:1,GA106-audio:1",
            "driver_version": "580.95.05",
            "device_count": 1,
        }
    if dev:
        config["session_report"] = {"capability": "s" * 43, "interval_seconds": 30}
    return config


@pytest.mark.parametrize("gpu", [False, True])
@pytest.mark.parametrize("dev", [False, True])
def test_representative_cloud_init_fits_nova_user_data_limit(gpu, dev):
    encoded = base64.b64encode(render_bootstrap(representative_spec(gpu=gpu, dev=dev)).encode("utf-8"))
    print(f"user_data_bytes gpu={gpu} dev={dev}: {len(encoded)}")
    assert len(encoded) <= 65535
