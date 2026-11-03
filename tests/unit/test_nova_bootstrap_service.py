from __future__ import annotations

import hashlib
import http.client
import json
import os
import selectors
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from palimpsest_local import nova_bootstrap_service as bootstrap

RUN = "cuda-root-proof"
NONCE = "a" * 32
SECRET = "private-ceph-fixture-secret-never-log"


def write_json(path, value):
    content = json.dumps(value, sort_keys=True).encode()
    temporary = path.with_name(path.name + ".new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    if shutil.which("openssl") is None:
        pytest.skip("real local TLS tests require OpenSSL CLI")
    root = tmp_path_factory.mktemp("nova-bootstrap-pki").resolve()
    root.chmod(0o700)
    ca = bootstrap.create_ca(root)
    server = bootstrap.issue_server_certificate(root, ca["cert_file"], ca["key_file"])
    client = bootstrap.issue_client_certificate(root, ca["cert_file"], ca["key_file"], run_id=RUN)
    other = bootstrap.issue_client_certificate(root, ca["cert_file"], ca["key_file"], run_id="other-run")
    foreign_dir = root / "foreign"
    foreign_dir.mkdir(mode=0o700)
    foreign_ca = bootstrap.create_ca(foreign_dir)
    foreign = bootstrap.issue_client_certificate(
        foreign_dir, foreign_ca["cert_file"], foreign_ca["key_file"], run_id=RUN
    )
    return {"root": root, "ca": ca, "server": server, "client": client, "other": other, "foreign": foreign}


@pytest.fixture
def plan():
    return {
        "run_id": RUN,
        "server_id": "11111111-2222-4333-8444-555555555555",
        "project_id": "12345678123456781234567812345678",
        "carrier_digest": "sha256:" + "c" * 64,
        "image": {
            "source": {
                "manifest_digest": "sha256:" + "d" * 64,
                "config": {"Entrypoint": ["/opt/nvidia/nvidia_entrypoint.sh"]},
            },
            "runtime": {
                "entrypoint": ["/opt/nvidia/nvidia_entrypoint.sh"],
                "env": ["PATH=/usr/bin:/bin"],
                "working_dir": "/",
                "user": "",
            },
            "layers": [{"digest": "sha256:" + "e" * 64, "size": 4096, "ordinal": 0}],
        },
        "network": {"address": "192.0.2.10", "cidr": "192.0.2.0/24", "gateway": "192.0.2.1", "dns": ["192.0.2.53"]},
        "upper": {"label": "PAL-UPPER"},
        "gpu": {
            "enabled": True,
            "kernel_release": "6.8.0-147-generic",
            "driver_version": "580.178.04",
            "expected_pci_alias": "GPU:1",
        },
        "command": ["/usr/bin/nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
    }


class LocalService:
    def __init__(self, root, pki, plan):
        self.root = root
        self.pki = pki
        self.enrollments = root / "enrollments"
        self.results = root / "results"
        self.enrollments.mkdir(mode=0o700)
        self.results.mkdir(mode=0o700)
        self.config_path = root / "service.json"
        self.config = {
            "schema": bootstrap.CONFIG_SCHEMA,
            "bind": "127.0.0.1",
            "port": 9443,
            "cert_file": pki["server"]["cert_file"],
            "key_file": pki["server"]["key_file"],
            "client_ca_file": pki["ca"]["cert_file"],
            "enrollments_dir": str(self.enrollments),
            "results_dir": str(self.results),
        }
        self.enrollment = {
            "schema": bootstrap.ENROLLMENT_SCHEMA,
            "run_id": RUN,
            "client_cert_sha256": pki["client"]["cert_sha256"],
            "expires_unix": int(time.time()) + 3600,
            "revoked": False,
            "plan": plan,
            "ceph_access": {
                "export": "192.0.2.20:6789:/volumes/_nogroup/fixture",
                "name": "client.fixture",
                "secret": SECRET,
            },
        }
        write_json(self.config_path, self.config)
        self.save_enrollment()
        self.server = None
        self.thread = None

    def save_enrollment(self):
        write_json(self.enrollments / (RUN + ".json"), self.enrollment)

    def start(self):
        self.server = bootstrap.BootstrapHTTPServer(self.config_path, _listen_port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()

    def close(self):
        if self.server is not None:
            self.server.shutdown()
            self.thread.join(timeout=10)
            self.server.server_close()
            assert not self.thread.is_alive()
            self.server = None

    def request(self, payload=None, *, endpoint="bootstrap", client="client", raw=None, path=None, headers=None):
        context = ssl.create_default_context(cafile=self.pki["ca"]["cert_file"])
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        if client is not None:
            material = self.pki[client]
            context.load_cert_chain(material["cert_file"], material["key_file"])
        connection = http.client.HTTPSConnection("127.0.0.1", self.port, context=context, timeout=5)
        try:
            body = raw if raw is not None else json.dumps(payload or {"nonce": NONCE}, separators=(",", ":")).encode()
            connection.request(
                "POST",
                path or f"/v1/{endpoint}/{RUN}",
                body=body,
                headers={"Content-Type": "application/json"} if headers is None else headers,
            )
            response = connection.getresponse()
            content = response.read()
            return response.status, json.loads(content), dict(response.getheaders())
        finally:
            connection.close()


@pytest.fixture
def service(tmp_path, pki, plan):
    root = tmp_path.resolve()
    root.chmod(0o700)
    running = LocalService(root, pki, plan)
    running.start()
    try:
        yield running
    finally:
        running.close()


def test_listener_does_not_resolve_loopback_hostname(tmp_path, pki, plan, monkeypatch):
    def unexpected_resolution(*args, **kwargs):
        raise AssertionError("numeric loopback listener must not resolve a hostname")

    monkeypatch.setattr(socket, "getfqdn", unexpected_resolution)
    root = tmp_path.resolve()
    root.chmod(0o700)
    local = LocalService(root, pki, plan)
    local.start()
    try:
        assert local.request()[0] == 200
    finally:
        local.close()


def test_real_mutual_tls_returns_exact_enrolled_plan_and_no_cmd_invention(service):
    status, response, headers = service.request()
    assert status == 200
    assert set(response) == {"schema", "run_id", "nonce", "expires_unix", "plan", "ceph_access"}
    assert response == {
        "schema": bootstrap.BOOTSTRAP_SCHEMA,
        "run_id": RUN,
        "nonce": NONCE,
        "expires_unix": service.enrollment["expires_unix"],
        "plan": service.enrollment["plan"],
        "ceph_access": service.enrollment["ceph_access"],
    }
    assert "cmd" not in response["plan"]["image"]["runtime"]
    assert headers["Cache-Control"] == "no-store"
    assert service.server.tls_context.verify_mode == ssl.CERT_REQUIRED
    assert service.server.tls_context.minimum_version == ssl.TLSVersion.TLSv1_2
    journal = service.results / (RUN + ".delivery.json")
    receipt = json.loads(journal.read_text())
    assert receipt["nonce"] == NONCE
    assert receipt["response_sha256"] == hashlib.sha256(bootstrap._encode(response)).hexdigest()
    assert SECRET not in journal.read_text()
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600


def test_same_ca_wrong_certificate_cannot_bootstrap_another_run(service):
    status, response, _ = service.request(client="other")
    assert status == 403
    assert response == {"error": "request rejected"}
    assert list(service.results.iterdir()) == []


@pytest.mark.parametrize("fingerprint", ["A" * 64, "a" * 63, "sha256:" + "a" * 64, "g" * 64])
def test_certificate_fingerprint_requires_exact_lowercase_der_hex(service, fingerprint):
    service.enrollment["client_cert_sha256"] = fingerprint
    service.save_enrollment()
    status, response, _ = service.request()
    assert status == 503
    assert response == {"error": "bootstrap unavailable"}
    assert not (service.results / (RUN + ".delivery.json")).exists()


@pytest.mark.parametrize("client", [None, "foreign"])
def test_tls_rejects_no_certificate_or_untrusted_ca(service, client):
    with pytest.raises((ssl.SSLError, ConnectionError, http.client.HTTPException, OSError)):
        service.request(client=client)
    assert not (service.results / (RUN + ".delivery.json")).exists()


@pytest.mark.parametrize("field,value", [("expires_unix", 1), ("revoked", True)])
def test_expiry_and_revocation_are_rechecked_even_for_identical_retry(service, field, value):
    assert service.request()[0] == 200
    service.enrollment[field] = value
    service.save_enrollment()
    assert service.request()[0] == 403
    assert service.request({"nonce": NONCE, "result": {"ok": True}}, endpoint="results")[0] == 403


@pytest.mark.parametrize("field,value", [("expires_unix", 1), ("revoked", True)])
def test_denied_initial_admission_creates_no_lock_or_other_state(service, field, value):
    service.enrollment[field] = value
    service.save_enrollment()
    assert service.request()[0] == 403
    assert list(service.results.iterdir()) == []


def test_unknown_run_names_cannot_allocate_per_run_lock_files(service):
    for index in range(8):
        assert service.request(path="/v1/bootstrap/unknown-run-" + str(index))[0] == 404
    assert list(service.results.iterdir()) == []


def test_missing_exact_enrollment_is_pending_then_same_nonce_can_bootstrap(service):
    path = service.enrollments / (RUN + ".json")
    path.unlink()
    status, response, _ = service.request()
    assert status == 404
    assert response == {"error": "request rejected"}
    assert list(service.results.iterdir()) == []
    service.save_enrollment()
    assert service.request()[0] == 200


def test_missing_enrollment_directory_is_not_retryable_pending(service):
    service.enrollments.rename(service.root / "saved-enrollments")
    assert service.request()[0] == 503
    assert list(service.results.iterdir()) == []


def test_changed_nonce_is_held_and_original_nonce_is_idempotent_across_restart(service):
    first = service.request()
    assert first[0] == 200
    before = (service.results / (RUN + ".delivery.json")).read_bytes()
    service.close()
    service.start()
    assert service.request({"nonce": "b" * 32})[0] == 409
    assert service.request()[1] == first[1]
    assert (service.results / (RUN + ".delivery.json")).read_bytes() == before


def test_enrollment_plan_change_cannot_reuse_an_issued_nonce(service):
    assert service.request()[0] == 200
    service.enrollment["plan"]["server_id"] = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    service.save_enrollment()
    assert service.request()[0] == 409


def test_concurrent_distinct_nonces_only_deliver_one(service):
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda nonce: service.request({"nonce": nonce})[0], [NONCE, "b" * 32]))
    assert sorted(outcomes) == [200, 409]


def test_results_require_delivery_and_are_durably_immutable_by_exact_body(service):
    raw = json.dumps(
        {"nonce": NONCE, "result": {"ok": True, "observation": "trusted workload, not attestation"}}
    ).encode()
    assert service.request(raw=raw, endpoint="results")[0] == 409
    assert service.request()[0] == 200
    assert service.request(raw=raw, endpoint="results")[0] == 200
    result_path = service.results / (RUN + ".result.json")
    before = result_path.read_bytes()
    assert json.loads(before)["result"]["ok"] is True
    assert stat.S_IMODE(result_path.stat().st_mode) == 0o600
    service.close()
    service.start()
    assert service.request(raw=raw, endpoint="results")[0] == 200
    # Even semantically identical JSON with changed bytes is not a new result.
    assert service.request(raw=raw + b" ", endpoint="results")[0] == 409
    assert service.request({"nonce": "c" * 32, "result": {"ok": True}}, endpoint="results")[0] == 409
    assert service.request({"nonce": NONCE, "result": {"ok": False}}, endpoint="results")[0] == 409
    assert result_path.read_bytes() == before


def test_large_raw_utf8_result_can_be_retried_without_storage_expansion(service):
    payload = {"nonce": NONCE, "result": {"part" + str(index): "é" * 60000 for index in range(8)}}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert len(raw) < bootstrap.MAX_JSON_BYTES
    assert service.request()[0] == 200
    assert service.request(raw=raw, endpoint="results")[0] == 200
    result_path = service.results / (RUN + ".result.json")
    before = result_path.read_bytes()
    assert len(before) < bootstrap.MAX_JSON_BYTES * 2
    assert service.request(raw=raw, endpoint="results")[0] == 200
    assert result_path.read_bytes() == before


@pytest.mark.parametrize(
    "payload",
    [
        {"nonce": "A" * 32},
        {"nonce": "0" * 31},
        {"nonce": "../other"},
        {"nonce": NONCE, "extra": SECRET},
        {},
        {"nonce": None},
    ],
)
def test_nonce_and_request_fields_are_exact(service, payload):
    status, response, _ = service.request(raw=json.dumps(payload).encode())
    assert status == 400
    assert SECRET not in json.dumps(response)
    assert not (service.results / (RUN + ".delivery.json")).exists()


@pytest.mark.parametrize(
    "raw",
    [b'{"nonce":"' + NONCE.encode() + b'","nonce":"' + NONCE.encode() + b'"}', b'{"nonce":NaN}', b'{"nonce":', b"\xff"],
)
def test_invalid_json_never_bypasses_admission(service, raw):
    status, response, _ = service.request(raw=raw)
    assert status == 503
    assert response == {"error": "bootstrap unavailable"}
    assert not (service.results / (RUN + ".delivery.json")).exists()


@pytest.mark.parametrize(
    "path",
    [
        "/v1/bootstrap/../other",
        "/v1/bootstrap/%2e%2e",
        "/v1/bootstrap/" + RUN + "?x=1",
        "/v1/bootstrap/" + "a" * 97,
        "/v1/results/other/extra",
    ],
)
def test_untrusted_path_is_not_used_as_enrollment_filename(service, path):
    assert service.request(path=path)[0] == 404
    assert list(service.results.iterdir()) == []


def test_body_bounds_encodings_and_result_shape(service):
    assert service.request(raw=b"x" * 4097)[0] == 413
    assert (
        service.request(
            raw=b"x",
            endpoint="results",
            headers={"Content-Type": "application/json", "Content-Length": str(bootstrap.MAX_JSON_BYTES + 1)},
        )[0]
        == 413
    )
    assert service.request(headers={"Content-Type": "text/plain"})[0] == 400
    assert service.request(headers={"Content-Type": "application/json", "Content-Encoding": "gzip"})[0] == 400
    assert service.request({"nonce": NONCE, "result": []}, endpoint="results")[0] == 400


@pytest.mark.parametrize(
    "field,value", [("server_id", "server-one"), ("project_id", ""), ("carrier_digest", "c" * 64), ("run_id", "other")]
)
def test_asserted_plan_identity_is_explicit_and_cannot_be_missing(service, field, value):
    service.enrollment["plan"][field] = value
    service.save_enrollment()
    assert service.request()[0] == 503
    assert not (service.results / (RUN + ".delivery.json")).exists()


def test_malformed_or_insecure_enrollment_is_unavailable_not_auth_bypass(service, capfd):
    path = service.enrollments / (RUN + ".json")
    path.write_text('{"secret":"' + SECRET + '",invalid}')
    assert service.request()[0] == 503
    service.save_enrollment()
    path.chmod(0o644)
    assert service.request()[0] == 503
    captured = capfd.readouterr()
    assert SECRET not in captured.out + captured.err


def test_config_is_reloaded_and_malformed_changes_fail_closed(service, capfd):
    assert service.request()[0] == 200
    service.config["unrecognized"] = SECRET
    write_json(service.config_path, service.config)
    status, response, _ = service.request()
    assert status == 503
    assert response == {"error": "bootstrap unavailable"}
    captured = capfd.readouterr()
    assert SECRET not in captured.out + captured.err


def test_replacing_private_state_directory_cannot_erase_nonce_history(service):
    assert service.request()[0] == 200
    service.results.rename(service.root / "old-results")
    service.results.mkdir(mode=0o700)
    assert service.request({"nonce": "b" * 32})[0] == 503
    assert list(service.results.iterdir()) == []


def test_symlinked_enrollment_or_journal_is_never_followed(service):
    path = service.enrollments / (RUN + ".json")
    real = service.enrollments / "saved.json"
    path.rename(real)
    path.symlink_to(real)
    assert service.request()[0] == 503
    path.unlink()
    real.rename(path)
    journal = service.results / (RUN + ".delivery.json")
    journal.symlink_to(service.root / "outside.json")
    assert service.request()[0] == 503
    assert not (service.root / "outside.json").exists()


def test_persistence_failure_never_returns_credentials_or_sensitive_exception(service, monkeypatch, capfd):
    original = bootstrap._publish

    def fail(*args, **kwargs):
        raise OSError(SECRET)

    monkeypatch.setattr(bootstrap, "_publish", fail)
    status, response, _ = service.request()
    assert status == 503
    assert response == {"error": "bootstrap unavailable"}
    assert not (service.results / (RUN + ".delivery.json")).exists()
    monkeypatch.setattr(bootstrap, "_publish", original)
    assert service.request({"nonce": "b" * 32})[0] == 200
    captured = capfd.readouterr()
    assert SECRET not in captured.out + captured.err


def test_delivery_is_committed_before_any_bootstrap_response(service, monkeypatch):
    original = bootstrap._publish

    def committed_then_interrupted(directory, name, content):
        original(directory, name, content)
        raise OSError(SECRET)

    monkeypatch.setattr(bootstrap, "_publish", committed_then_interrupted)
    assert service.request()[0] == 503
    monkeypatch.setattr(bootstrap, "_publish", original)
    assert service.request({"nonce": "b" * 32})[0] == 409
    assert service.request()[0] == 200


def test_corrupt_existing_result_cannot_be_replaced(service):
    assert service.request()[0] == 200
    assert service.request({"nonce": NONCE, "result": {"ok": True}}, endpoint="results")[0] == 200
    path = service.results / (RUN + ".result.json")
    path.write_text("{invalid}")
    assert service.request({"nonce": NONCE, "result": {"ok": False}}, endpoint="results")[0] == 503
    assert path.read_text() == "{invalid}"


def test_structurally_valid_but_inconsistent_result_is_not_acknowledged(service):
    request = {"nonce": NONCE, "result": {"ok": True}}
    assert service.request()[0] == 200
    assert service.request(request, endpoint="results")[0] == 200
    path = service.results / (RUN + ".result.json")
    stored = json.loads(path.read_text())
    stored["result"] = {"ok": False}
    write_json(path, stored)
    assert service.request(request, endpoint="results")[0] == 503
    assert json.loads(path.read_text())["result"] == {"ok": False}


@pytest.mark.parametrize(
    "mutation", ["mode", "uid", "symlink", "directory", "config-mode", "config-extra", "bind", "port"]
)
def test_server_rejects_unowned_or_insecure_configuration(tmp_path, pki, plan, monkeypatch, mutation):
    root = tmp_path.resolve()
    root.chmod(0o700)
    local = LocalService(root, pki, plan)
    if mutation == "mode":
        local.results.chmod(0o755)
    elif mutation == "uid":
        original = os.geteuid()
        monkeypatch.setattr(bootstrap.os, "geteuid", lambda: original + 1)
    elif mutation == "symlink":
        local.results.rename(root / "real-results")
        local.results.symlink_to(root / "real-results")
    elif mutation == "directory":
        local.config_path.unlink()
        local.config_path.mkdir(mode=0o700)
    elif mutation == "config-mode":
        local.config_path.chmod(0o644)
    else:
        if mutation == "config-extra":
            local.config["ceph_secret"] = SECRET
        elif mutation == "bind":
            local.config["bind"] = "0.0.0.0"
        else:
            local.config["port"] = 9444
        write_json(local.config_path, local.config)
    with pytest.raises(bootstrap.BootstrapError, match="bootstrap service initialization failed") as error:
        bootstrap.create_server(local.config_path)
    assert SECRET not in str(error.value)


def test_certificate_helpers_report_der_fingerprints_and_private_files(pki, capfd):
    for key in ("ca", "server", "client", "other", "foreign"):
        material = pki[key]
        der = ssl.PEM_cert_to_DER_cert(Path(material["cert_file"]).read_text())
        assert material["cert_sha256"] == hashlib.sha256(der).hexdigest()
        assert bootstrap.certificate_fingerprint(material["cert_file"]) == material["cert_sha256"]
        for field in ("cert_file", "key_file"):
            path = Path(material[field])
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert path.stat().st_uid == os.geteuid()
    captured = capfd.readouterr()
    assert "PRIVATE KEY" not in captured.out + captured.err


def test_certificate_helpers_reject_unsafe_names_and_do_not_replace_existing_keys(pki):
    ca = pki["ca"]
    before = Path(pki["client"]["key_file"]).read_bytes()
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.issue_client_certificate(pki["root"], ca["cert_file"], ca["key_file"], run_id="../escape")
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.issue_server_certificate(pki["root"], ca["cert_file"], ca["key_file"], server_name="host\nsecret")
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.issue_client_certificate(pki["root"], ca["cert_file"], ca["key_file"], run_id=RUN)
    assert Path(pki["client"]["key_file"]).read_bytes() == before


@pytest.mark.parametrize("reaped_after_term", [False, True])
def test_openssl_timeout_uses_only_term_and_retains_unreaped_child_artifacts(
    tmp_path, monkeypatch, capfd, reaped_after_term
):
    root = tmp_path.resolve()
    root.chmod(0o700)
    events = []

    class ControlledChild:
        returncode = None

        def wait(self, timeout):
            events.append(("wait", timeout))
            if len([item for item in events if item[0] == "wait"]) == 1 or not reaped_after_term:
                raise subprocess.TimeoutExpired("sensitive-facade-command-" + SECRET, timeout)
            self.returncode = -15
            return self.returncode

        def terminate(self):
            events.append(("TERM",))

        def kill(self):
            pytest.fail("certificate issuance must never escalate to SIGKILL")

    child = ControlledChild()

    def launch(arguments, **kwargs):
        assert arguments[0] == "openssl"
        assert SECRET not in json.dumps(arguments)
        assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["umask"] == 0o077
        key_path = Path(arguments[arguments.index("-keyout") + 1])
        descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(b"controlled-private-key-fixture")
        child.staging = key_path.parent
        return child

    monkeypatch.setattr(bootstrap.subprocess, "Popen", launch)
    expected = bootstrap.BootstrapError if reaped_after_term else bootstrap.CertificateProcessHold
    with pytest.raises(expected) as caught:
        bootstrap.create_ca(root)
    assert events == [("wait", 120), ("TERM",), ("wait", 10)]
    assert SECRET not in str(caught.value)
    assert not (root / "ca.key").exists()
    assert not (root / "ca.pem").exists()
    if reaped_after_term:
        assert not isinstance(caught.value, bootstrap.CertificateProcessHold)
        assert not child.staging.exists()
    else:
        hold = caught.value
        assert hold.process is child
        assert hold.staging_directory == child.staging
        try:
            assert stat.S_IMODE(child.staging.stat().st_mode) == 0o700
            assert (child.staging / "key.pem").read_bytes() == b"controlled-private-key-fixture"
            for descriptor in (hold.directory_fd, hold.staging_directory_fd):
                info = os.fstat(descriptor)
                assert stat.S_ISDIR(info.st_mode)
                assert info.st_uid == os.geteuid()
                assert stat.S_IMODE(info.st_mode) == 0o700
        finally:
            # This child is a facade, so no real process is left writing. Model
            # confirmed disposition before releasing the retained recovery FDs.
            child.returncode = -15
            os.close(hold.staging_directory_fd)
            os.close(hold.directory_fd)
    captured = capfd.readouterr()
    assert SECRET not in captured.out + captured.err


@pytest.mark.parametrize("slow_client", [False, True])
def test_cli_serve_term_is_an_ordinary_local_shutdown(service, slow_client):
    service.close()
    # Exercise the real CLI parser/serve/TERM path with only the listening-port
    # factory replaced; the server, certificates and TLS requests are genuine.
    script = (
        "import sys\n"
        "from palimpsest_local import nova_bootstrap_service as module\n"
        "def ephemeral(config):\n"
        "    server = module.BootstrapHTTPServer(config, _listen_port=0)\n"
        "    print(server.server_address[1], flush=True)\n"
        "    return server\n"
        "module.create_server = ephemeral\n"
        "raise SystemExit(module.main(['serve', '--config', sys.argv[1]]))\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(service.config_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=service.root,
        env={
            "PATH": os.defpath,
            "PYTHONPATH": str(Path(bootstrap.__file__).parents[1]),
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": str(service.root),
            "TMPDIR": str(service.root),
        },
    )
    slow_socket = None
    drip_thread = None
    stop_drip = threading.Event()
    try:
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            assert ready.select(timeout=15), "local bootstrap CLI did not publish its ephemeral listener"
            service.port = int(process.stdout.readline())
        deadline = time.monotonic() + 15
        while True:
            assert process.poll() is None, "local bootstrap CLI exited before readiness"
            try:
                status = service.request()[0]
            except OSError:
                status = None
            if status == 200:
                break
            assert time.monotonic() < deadline, "local bootstrap CLI did not become ready"
            time.sleep(0.05)
        if slow_client:
            context = ssl.create_default_context(cafile=service.pki["ca"]["cert_file"])
            context.load_cert_chain(service.pki["client"]["cert_file"], service.pki["client"]["key_file"])
            slow_socket = context.wrap_socket(
                socket.create_connection(("127.0.0.1", service.port), timeout=5), server_hostname="127.0.0.1"
            )
            slow_socket.sendall(("POST /v1/bootstrap/" + RUN + " HTTP/1.0\r\nX-Drip: ").encode())

            def drip():
                while not stop_drip.wait(0.02):
                    try:
                        slow_socket.sendall(b"x")
                    except OSError:
                        break

            drip_thread = threading.Thread(target=drip)
            drip_thread.start()
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0
        assert SECRET.encode() not in stdout + stderr
        assert b"PRIVATE KEY" not in stdout + stderr
    finally:
        stop_drip.set()
        if slow_socket is not None:
            slow_socket.close()
        if drip_thread is not None:
            drip_thread.join(timeout=5)
            assert not drip_thread.is_alive()
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=5)
