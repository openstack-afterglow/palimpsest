"""Public exec routing through real exact-record capability preflight."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
import test_runtime_dispatch as fixtures

from palimpsest_local import cli, oci_exec_session, platforms, runtime_dispatch, state
from palimpsest_local.errors import StateError
from palimpsest_local.runtime_types import (
    ExpectedRunIdentity,
    ProcessCapabilities,
    ProcessExit,
    ProcessExitCategory,
    ProcessOutputEvent,
    ProcessStatusEvent,
    ProcessStream,
    RuntimeOperation,
)


@pytest.fixture
def case(tmp_path, monkeypatch):
    roots = fixtures._roots(tmp_path)
    fixtures._write_ledger(
        roots, record={"schema_version": 2, "runtime_kind": "oci-root", "backend": "kvm", "status": "running"}
    )
    record = runtime_dispatch.resolve_existing_run("demo", roots=roots)
    value = SimpleNamespace(roots=roots, record=record, calls=[], code=17, closed=0)
    value.real_exec_session = oci_exec_session.exec_session
    monkeypatch.setenv("PALIMPSEST_STATE_HOME", str(roots.state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(roots.config.parent))

    # Observe, but do not bypass, issuance, exact-record validation, and
    # one-shot authentication/consumption of the real preflight report.
    require_preflight = runtime_dispatch.require_existing_preflight

    def preflight(current, operation, report, **kwargs):
        assert current == record and operation is RuntimeOperation.EXEC
        assert report.profile.requirements == () and report.checks == ()
        require_preflight(current, operation, report, **kwargs)
        value.calls.append("preflight")

    monkeypatch.setattr(runtime_dispatch, "require_existing_preflight", preflight)
    monkeypatch.setattr(
        platforms, "_check_capability", lambda *a, **k: pytest.fail("ambient or cloud capability probe entered")
    )
    monkeypatch.setattr(
        runtime_dispatch.cloud_runtime, "exec_session", lambda *a, **k: pytest.fail("cloud adapter entered")
    )

    class Session(fixtures._FakeProcessSession):
        capabilities = ProcessCapabilities(False, False, False, False)

        def events(self):
            yield ProcessOutputEvent(ProcessStream.STDOUT, b"only guest stdout\n")
            yield ProcessOutputEvent(ProcessStream.STDERR, b"only guest stderr\n")
            yield ProcessStatusEvent(self.wait())

        def wait(self):
            return ProcessExit(value.code, value.code, None, ProcessExitCategory.EXITED)

        def close(self):
            value.closed += 1

    value.session = Session()

    def execute(name, request, *, roots, _expected_record):
        assert name == record.name and roots == value.roots and _expected_record == record
        value.calls.append(request.argv)
        return value.session

    monkeypatch.setattr(oci_exec_session, "exec_session", execute)
    return value


def test_cli_exec_strips_separator_preserves_literal_argv_and_separates_guest_streams(case, capsys):
    assert cli.main(["exec", "demo", "--", "/bin/probe", "literal $HOME; $(uname)"]) == 17
    captured = capsys.readouterr()
    assert captured.out == "only guest stdout\n" and captured.err == "only guest stderr\n"
    assert case.calls == ["preflight", ("/bin/probe", "literal $HOME; $(uname)")]
    case.session.close()
    assert state.read_run_ledger_snapshot(case.roots, "demo").state["status"] == "running"


def test_dispatch_forwards_same_expected_run_identity_to_guest_adapter(case):
    expected = ExpectedRunIdentity(case.record.name, case.record.run_id, case.record.dispatch_key)
    assert runtime_dispatch.exec("demo", ["/bin/probe"], roots=case.roots, expected_identity=expected) is case.session
    assert runtime_dispatch._adapter_for(case.record, RuntimeOperation.EXEC) is oci_exec_session


def test_replaced_expected_identity_is_rejected_before_exec_preflight(case):
    expected = ExpectedRunIdentity(case.record.name, "00000000-0000-0000-0000-000000000001", case.record.dispatch_key)
    with pytest.raises(StateError, match="identity changed"):
        runtime_dispatch.exec("demo", ["/bin/probe"], roots=case.roots, expected_identity=expected)
    assert case.calls == []


def test_guest_adapter_also_rejects_a_rebound_record_before_session_creation(case, monkeypatch):
    foreign = replace(case.record, run_id="00000000-0000-0000-0000-000000000002")
    monkeypatch.setattr(oci_exec_session, "load_oci_run_binding", lambda *args: SimpleNamespace(record=foreign))
    monkeypatch.setattr(oci_exec_session, "OCIExecProcessSession", lambda *a, **k: pytest.fail("session created"))
    monkeypatch.setattr(oci_exec_session, "exec_session", case.real_exec_session)
    with pytest.raises(StateError, match="identity changed"):
        runtime_dispatch.exec("demo", ["/bin/probe"], roots=case.roots)
    assert case.closed == 0
