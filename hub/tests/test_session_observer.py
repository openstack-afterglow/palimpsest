from __future__ import annotations

import asyncio

import pytest

from palimpsest_hub.services.session_controller import ObservationError, Outcome, SessionController
from palimpsest_hub.services.session_observer import (
    GUEST_PROBE_COMMAND,
    WhoSessionObserver,
    parse_who,
)

G = 300.0
IID = "i-1"


def _fetch_returning(text):
    async def fetch(_instance_id):
        return text

    return fetch


def _fetch_raising(exc):
    async def fetch(_instance_id):
        raise exc

    return fetch


def _listing(*lines):
    return "\n".join(("PALIMPSEST-UTMP-OK", *lines, "PALIMPSEST-END")) + "\n"


def test_parse_who_normalises_lines_and_ignores_blank_ones():
    text = _listing("pieroot   console  Nov 10 12:00 (:0)", "", "  pieroot ttys001 Nov 10 12:05 (host)")
    assert parse_who(text) == {
        "pieroot console Nov 10 12:00 (:0)",
        "pieroot ttys001 Nov 10 12:05 (host)",
    }


def test_non_pty_sshd_session_is_counted_even_though_utmp_never_lists_it():
    text = _listing("sshd: ubuntu@notty")
    assert parse_who(text) == {"sshd: ubuntu@notty"}


def test_empty_listing_with_both_markers_is_a_valid_no_session_observation():
    observer = WhoSessionObserver(_fetch_returning(_listing()))
    assert asyncio.run(observer.sessions(IID)) == set()


def test_listing_without_the_end_marker_is_unknown_not_empty():
    with pytest.raises(ObservationError):
        parse_who("PALIMPSEST-UTMP-OK\n")


def test_truncated_listing_without_end_marker_is_unknown_even_with_sessions():
    with pytest.raises(ObservationError):
        parse_who("PALIMPSEST-UTMP-OK\npieroot console Nov 10 12:00 (:0)\n")


def test_missing_utmp_marker_is_unknown_not_an_empty_room():
    # An OCI root without utmp: nothing is printed for `who`, and the end marker is there.
    with pytest.raises(ObservationError, match="login database"):
        parse_who("PALIMPSEST-END\n")


def test_probe_command_prints_both_markers_and_checks_utmp_before_listing():
    assert "PALIMPSEST-UTMP-OK" in GUEST_PROBE_COMMAND
    assert GUEST_PROBE_COMMAND.rstrip().endswith("PALIMPSEST-END")
    assert GUEST_PROBE_COMMAND.index("PALIMPSEST-UTMP-OK") < GUEST_PROBE_COMMAND.index("who")


def test_a_line_without_a_user_and_tty_is_unknown_not_empty():
    with pytest.raises(ObservationError):
        parse_who("orphan\n")


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


def test_real_listener_and_preauth_titles_are_not_sessions():
    text = _listing(
        "sshd: /usr/sbin/sshd -D [listener] 0 of 10-100 startups",
        "sshd: [accepted]",
        "sshd: ubuntu [priv]",
    )
    assert parse_who(text) == set()


def test_openssh_9_8_session_title_is_counted():
    assert parse_who(_listing("sshd-session: ubuntu@notty")) == {"sshd-session: ubuntu@notty"}


def test_pty_sshd_session_is_counted():
    assert parse_who(_listing("sshd: ubuntu@pts/3")) == {"sshd: ubuntu@pts/3"}


def test_probe_refuses_to_print_the_end_marker_when_ps_fails():
    # `out=$(ps ...) || exit 1` must precede the end marker; a failed ps can never be "no sessions".
    guard = GUEST_PROBE_COMMAND.index("out=$(ps -eo args=) || exit 1")
    assert guard < GUEST_PROBE_COMMAND.index("PALIMPSEST-END")


def test_listing_cut_off_after_a_failed_ps_is_unknown_not_empty():
    with pytest.raises(ObservationError):
        parse_who("PALIMPSEST-UTMP-OK\n")
