"""Authenticated additional command ordering, byte limits and VM separation."""

from dataclasses import replace

import pytest

from palimpsest_local.oci_control_protocol_v2 import (
    HostOCIControlV2Session,
    OCIControlProtocolV2Error,
    OCIControlV2Binding,
    OCIControlV2Message,
    decode_frame,
    encode_frame,
    sign_message,
    validate_exec_argv,
)

KEY = bytes(range(32))
BINDING = OCIControlV2Binding("11111111-1111-4111-8111-111111111111", "sha256:" + "a" * 64, "sha256:" + "b" * 64)
GENERATION = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def session():
    current = HostOCIControlV2Session(BINDING)
    hello = current.hello()
    current.accept(
        sign_message(
            OCIControlV2Message(
                "BOOTSTRAP",
                BINDING,
                current.boot_attempt_id,
                current.host_nonce,
                1,
                1,
                {"boot_key": KEY.hex()},
                boot_generation=GENERATION,
                reply_to=hello.body.request_id,
            ),
            KEY,
        )
    )
    ack = current.key_ack()
    current.accept(
        sign_message(
            OCIControlV2Message(
                "READY",
                BINDING,
                current.boot_attempt_id,
                current.host_nonce,
                1,
                2,
                {},
                boot_generation=GENERATION,
                reply_to=ack.body.wire_sequence,
            ),
            KEY,
        )
    )
    return current


def output(session, request, *, stream="stdout", offset=0, data=b"hello", wire=3):
    return sign_message(
        OCIControlV2Message(
            "EXEC_OUTPUT",
            BINDING,
            session.boot_attempt_id,
            session.host_nonce,
            1,
            wire,
            {"stream": stream, "offset": offset, "data_hex": data.hex()},
            boot_generation=GENERATION,
            reply_to=request.body.request_id,
        ),
        KEY,
    )


def completion(session, request, *, stdout=0, stderr=0, wire=4, reason="completed"):
    return sign_message(
        OCIControlV2Message(
            "EXEC_EXIT",
            BINDING,
            session.boot_attempt_id,
            session.host_nonce,
            1,
            wire,
            {
                "terminal": {"exit_code": 7, "signal": None},
                "stdout_bytes": stdout,
                "stderr_bytes": stderr,
                "reason": reason,
            },
            boot_generation=GENERATION,
            reply_to=request.body.request_id,
        ),
        KEY,
    )


@pytest.mark.parametrize("argv", [(), [], [""], ["a\0b"], [True], "echo", ["x"] * 65, ["x" * 8192], ["\ud800"]])
def test_invalid_argv_is_rejected_without_state_change(session, argv):
    before = (session._host_wire, session._request, session.state)
    with pytest.raises(OCIControlProtocolV2Error):
        session.exec(argv)
    assert before == (session._host_wire, session._request, session.state)


@pytest.mark.parametrize("timeout", [0, -1, 30001, True, 1.5, "30"])
def test_timeout_is_finite_strict_and_bounded(session, timeout):
    with pytest.raises(OCIControlProtocolV2Error):
        session.exec(("/bin/echo",), timeout_ms=timeout)


def test_request_is_immutable_and_authenticated(session):
    argv = ["/bin/echo", "", "line\nbreak", "한글"]
    request = session.exec(argv)
    argv[0] = "other"
    assert request.body.payload["argv"][0] == "/bin/echo"
    assert decode_frame(encode_frame(request)) == request
    assert request.body.direction == "host-to-guest"
    with pytest.raises(TypeError):
        request.body.payload["argv"][0] = "other"


def test_interleaved_streams_and_exact_completion_leave_main_ready(session):
    request = session.exec(("/bin/probe",))
    session.accept(output(session, request, data=b"\x00\xff"))
    session.accept(output(session, request, stream="stderr", data=b"error", wire=4))
    result = completion(session, request, stdout=2, stderr=5, wire=5)
    session.accept(result)
    assert session.state == "ready" and session._terminal is None
    subsequent = session.exec(("/bin/another",))
    assert subsequent.body.request_id > request.body.request_id
    with pytest.raises(OCIControlProtocolV2Error):
        session.accept(result)


def test_one_active_command_and_no_reconnect_adoption(session):
    session.exec(("/bin/probe",))
    with pytest.raises(OCIControlProtocolV2Error, match="outstanding"):
        session.exec(("/bin/again",))
    with pytest.raises(OCIControlProtocolV2Error, match="exec reconnect"):
        session.reconnect()
    with pytest.raises(OCIControlProtocolV2Error, match="exec reconnect"):
        session.admit_boundary(None, b"")


@pytest.mark.parametrize(
    "field,value", [("reply_to", 99), ("wire_sequence", 2), ("host_nonce", "f" * 64), ("epoch", 2)]
)
def test_stale_exec_reply_preserves_offsets_and_order(session, field, value):
    request = session.exec(("/bin/probe",))
    message = output(session, request)
    forged = sign_message(replace(message.body, **{field: value}), KEY)
    with pytest.raises(OCIControlProtocolV2Error):
        session.accept(forged)
    assert session._exec_counts == {"stdout": 0, "stderr": 0} and session._guest_wire == 2
    session.accept(message)


def test_gap_duplicate_and_inexact_result_do_not_advance(session):
    request = session.exec(("/bin/probe",))
    for message in (output(session, request, offset=1), completion(session, request, stdout=1)):
        with pytest.raises(OCIControlProtocolV2Error):
            session.accept(message)
    message = output(session, request)
    session.accept(message)
    with pytest.raises(OCIControlProtocolV2Error):
        session.accept(output(session, request, wire=4))
    assert session._exec_counts["stdout"] == 5 and session._guest_wire == 3


def test_combined_limit_is_not_two_independent_allowances(session):
    request = session.exec(("/bin/probe",))
    for index in range(64):
        session.accept(output(session, request, data=b"x" * 1024, offset=index * 1024, wire=index + 3))
    with pytest.raises(OCIControlProtocolV2Error, match="combined"):
        session.accept(output(session, request, stream="stderr", data=b"x", wire=67))
    session.accept(completion(session, request, stdout=65536, wire=67, reason="output-limit"))
    assert session.state == "ready"


def test_stop_is_allowed_but_vm_terminal_requires_exec_drain_first(session):
    request = session.exec(("/bin/probe",))
    stop = session.stop()
    terminal = sign_message(
        OCIControlV2Message(
            "TERMINAL",
            BINDING,
            session.boot_attempt_id,
            session.host_nonce,
            1,
            5,
            {"terminal": {"exit_code": 0, "signal": None}},
            boot_generation=GENERATION,
            reply_to=stop.body.request_id,
        ),
        KEY,
    )
    with pytest.raises(OCIControlProtocolV2Error, match="exec completion"):
        session.accept(terminal)
    session.accept(completion(session, request, reason="cancelled"))
    session.accept(terminal)
    assert session.state == "terminal"


@pytest.mark.parametrize(
    "payload",
    [
        {"stream": "stdin", "offset": 0, "data_hex": "ab"},
        {"stream": "stdout", "offset": True, "data_hex": "ab"},
        {"stream": "stdout", "offset": 0, "data_hex": "AB"},
        {"stream": "stdout", "offset": 0, "data_hex": "a"},
        {"stream": "stdout", "offset": 0, "data_hex": ""},
        {"stream": "stdout", "offset": 0, "data_hex": "ab" * 1025},
    ],
)
def test_closed_output_schema(session, payload):
    request = session.exec(("/bin/probe",))
    with pytest.raises(OCIControlProtocolV2Error):
        replace(output(session, request).body, payload=payload)


def test_exact_argv_byte_boundary():
    assert validate_exec_argv(["x" * 8188]) == ("x" * 8188,)
    with pytest.raises(OCIControlProtocolV2Error):
        validate_exec_argv(["x" * 8189])


@pytest.mark.parametrize(
    "reason,stdout,accepted",
    [
        ("cancelled", 0, True),
        ("cancelled", 1, False),
        ("completed", 0, False),
        ("timeout", 0, False),
        ("output-limit", 0, False),
    ],
)
def test_unstarted_cancellation_never_invents_a_process_exit(session, reason, stdout, accepted):
    request = session.exec(("/bin/probe",))
    payload = {"terminal": None, "reason": reason, "stdout_bytes": stdout, "stderr_bytes": 0}
    if accepted:
        response = sign_message(replace(completion(session, request).body, payload=payload), KEY)
        session.accept(response)
        assert session.state == "ready" and session._terminal is None
    else:
        with pytest.raises(OCIControlProtocolV2Error):
            replace(completion(session, request).body, payload=payload)
