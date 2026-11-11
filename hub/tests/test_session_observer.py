from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

from palimpsest_hub.services import session_observer
from palimpsest_hub.services.session_controller import ObservationError, Outcome, SessionController
from palimpsest_hub.services.session_observer import (
    GUEST_PROBE_COMMAND,
    WhoSessionObserver,
    parse_who,
)

G = 300.0
IID = "i-1"
PROBE_SHELL = 500
# The probe's own ssh session: its shell's parent. Excluded because it is an ancestor.
OWN_SESSION = (400, 1, "sshd-session: ubuntu@notty")


def _fetch_returning(text):
    async def fetch(_instance_id):
        return text

    return fetch


def _fetch_raising(exc):
    async def fetch(_instance_id):
        raise exc

    return fetch


def _listing(*who_lines, rows=()):
    """Build a probe transcript: utmp marker, `who` lines, shell pid, process table, end marker.

    Rows are `(pid, ppid, args)`. The probe shell and its own sshd session are always present
    unless the caller supplies a table of their own.
    """
    table = [(PROBE_SHELL, OWN_SESSION[0], "/bin/sh -c probe"), OWN_SESSION, *rows]
    body = [f"{pid} {ppid} {args}" for pid, ppid, args in table]
    return (
        "\n".join(("PALIMPSEST-UTMP-OK", *who_lines, f"PALIMPSEST-SELF {PROBE_SHELL}", *body, "PALIMPSEST-END")) + "\n"
    )


def test_parse_who_normalises_who_lines_and_ignores_blank_ones():
    text = _listing("pieroot   console  Nov 10 12:00 (:0)", "", "  pieroot ttys001 Nov 10 12:05 (host)")
    assert parse_who(text) == {
        "pieroot console Nov 10 12:00 (:0)",
        "pieroot ttys001 Nov 10 12:05 (host)",
    }


def test_non_pty_sshd_session_is_counted_by_its_pid():
    text = _listing(rows=[(700, 1, "sshd-session: ubuntu@notty")])
    assert parse_who(text) == {"sshd-session: ubuntu@notty (pid 700)"}


def test_two_sessions_of_one_user_stay_distinct_by_pid():
    text = _listing(rows=[(700, 1, "sshd-session: ubuntu@notty"), (701, 1, "sshd-session: ubuntu@notty")])
    assert parse_who(text) == {
        "sshd-session: ubuntu@notty (pid 700)",
        "sshd-session: ubuntu@notty (pid 701)",
    }


def test_probe_own_ssh_session_is_excluded_as_an_ancestor():
    # OWN_SESSION is the probe shell's parent and must not block shelve on its own account.
    assert parse_who(_listing()) == set()


def test_empty_listing_with_both_markers_is_a_valid_no_session_observation():
    observer = WhoSessionObserver(_fetch_returning(_listing()))
    assert asyncio.run(observer.sessions(IID)) == set()


def test_listener_and_preauth_titles_are_not_sessions():
    text = _listing(
        rows=[
            (2, 1, "sshd: /usr/sbin/sshd -D [listener] 0 of 10-100 startups"),
            (800, 2, "sshd: [accepted]"),
            (801, 2, "sshd: ubuntu [priv]"),
        ]
    )
    assert parse_who(text) == set()


def test_openssh_9_8_session_title_and_pty_session_are_counted():
    text = _listing(rows=[(900, 1, "sshd-session: ubuntu@notty"), (901, 1, "sshd: ubuntu@pts/3")])
    assert parse_who(text) == {
        "sshd-session: ubuntu@notty (pid 900)",
        "sshd: ubuntu@pts/3 (pid 901)",
    }


def test_listing_without_the_end_marker_is_unknown_not_empty():
    with pytest.raises(ObservationError):
        parse_who("PALIMPSEST-UTMP-OK\n")


def test_truncated_listing_is_unknown_even_with_sessions():
    text = _listing(rows=[(700, 1, "sshd-session: ubuntu@notty")]).replace("PALIMPSEST-END\n", "")
    with pytest.raises(ObservationError):
        parse_who(text)


def test_missing_utmp_marker_is_unknown_not_an_empty_room():
    # An OCI root without utmp prints no UTMP marker, but the end marker is still there.
    with pytest.raises(ObservationError, match="login database"):
        parse_who("PALIMPSEST-SELF 500\nPALIMPSEST-END\n")


def test_missing_probe_shell_identity_is_unknown_not_empty():
    text = _listing().replace(f"PALIMPSEST-SELF {PROBE_SHELL}\n", "")
    with pytest.raises(ObservationError, match="probe shell identity"):
        parse_who(text)


def test_probe_shell_absent_from_the_process_table_is_unknown():
    # Without the shell, its ancestors cannot be identified; refuse rather than count the probe's own ssh.
    text = _listing().replace(f"{PROBE_SHELL} {OWN_SESSION[0]} /bin/sh -c probe\n", "")
    with pytest.raises(ObservationError, match="missing from the process table"):
        parse_who(text)


def test_malformed_process_row_is_unknown():
    text = _listing().replace("400 1 sshd-session: ubuntu@notty", "garbage row")
    with pytest.raises(ObservationError, match="malformed process line"):
        parse_who(text)


def test_a_who_line_without_a_user_and_tty_is_unknown_not_empty():
    with pytest.raises(ObservationError):
        parse_who(_listing("orphan"))


def test_non_text_result_is_unknown():
    observer = WhoSessionObserver(_fetch_returning(None))
    with pytest.raises(ObservationError):
        asyncio.run(observer.sessions(IID))


def test_fetch_failure_is_unknown_and_does_not_leak_the_transport_text():
    observer = WhoSessionObserver(_fetch_raising(RuntimeError("token=SECRET-VALUE")))
    with pytest.raises(ObservationError) as caught:
        asyncio.run(observer.sessions(IID))
    assert "SECRET-VALUE" not in str(caught.value)


def test_hung_fetch_times_out_as_unknown():
    async def hang(_instance_id):
        await asyncio.sleep(5)

    observer = WhoSessionObserver(hang, timeout_seconds=0.01)
    with pytest.raises(ObservationError, match="timed out"):
        asyncio.run(observer.sessions(IID))


def test_non_positive_timeout_is_refused():
    with pytest.raises(ValueError):
        WhoSessionObserver(_fetch_returning(_listing()), timeout_seconds=0)


def test_probe_command_checks_utmp_before_who_and_prints_end_last():
    assert "PALIMPSEST-UTMP-OK" in GUEST_PROBE_COMMAND
    assert GUEST_PROBE_COMMAND.rstrip().endswith("PALIMPSEST-END")
    assert GUEST_PROBE_COMMAND.index("PALIMPSEST-UTMP-OK") < GUEST_PROBE_COMMAND.index("who")


def test_probe_refuses_to_print_the_end_marker_when_ps_fails():
    # `out=$(ps ...) || exit 1` must precede the end marker; a failed ps can never be "no sessions".
    guard = GUEST_PROBE_COMMAND.index("out=$(ps -eo pid=,ppid=,args=) || exit 1")
    assert guard < GUEST_PROBE_COMMAND.index("PALIMPSEST-END")


def test_real_probe_run_excludes_its_parent_session_and_counts_a_separate_one():
    # Local, real `sh` and `ps`: the outer shell's argv is an sshd session title and it is the
    # probe shell's parent (the probe's own session). A separate `cat` process is a second session.
    # The probe must count only the second one.
    env = {"PATH": os.environ["PATH"], "PROBE": session_observer._PROCESS_PART}
    other = subprocess.Popen(["sshd-session: ubuntu@notty"], executable="/bin/cat", stdin=subprocess.PIPE)
    try:
        outer = subprocess.Popen(
            ["sshd-session: ubuntu@notty"],
            executable="/bin/sh",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=env,
            text=True,
        )
        probe_output, _ = outer.communicate('/bin/sh -c "$PROBE"\n', timeout=30)
    finally:
        other.stdin.close()
        other.wait(timeout=30)
    transcript = "PALIMPSEST-UTMP-OK\n" + probe_output
    assert parse_who(transcript) == {f"sshd-session: ubuntu@notty (pid {other.pid})"}


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class _Provider:
    def __init__(self):
        self.calls = []

    async def shelve(self, instance_id):
        self.calls.append(("shelve", instance_id))

    async def unshelve(self, instance_id):
        self.calls.append(("unshelve", instance_id))


def test_controller_with_who_observer_shelves_only_after_a_clean_empty_listing():
    clock = _Clock()
    provider = _Provider()
    observer = WhoSessionObserver(_fetch_returning(_listing()))
    controller = SessionController(observer, provider, clock, G)
    controller.register(IID)
    clock.now = G
    assert asyncio.run(controller.tick(IID)) is Outcome.SHELVED
    assert provider.calls == [("shelve", IID)]


def test_controller_with_failing_who_observer_never_shelves():
    clock = _Clock()
    provider = _Provider()
    observer = WhoSessionObserver(_fetch_raising(OSError("ssh unreachable")))
    controller = SessionController(observer, provider, clock, G)
    controller.register(IID)
    clock.now = G
    assert asyncio.run(controller.tick(IID)) is Outcome.OBSERVATION_FAILED
    assert provider.calls == []
