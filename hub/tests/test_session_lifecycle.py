from __future__ import annotations

import pytest

from palimpsest_hub.services.session_lifecycle import Decision, Phase, SessionMachine

G = 300.0


def idle_machine():
    machine = SessionMachine()
    machine.connect("a", 0.0)
    machine.disconnect("a", 100.0)
    return machine


def test_shelve_is_due_only_after_the_full_grace_period_with_no_session():
    machine = idle_machine()
    assert machine.decide(100.0 + G - 0.001) is Decision.WAIT
    assert machine.claim_shelve(100.0 + G - 0.001) is None
    assert machine.decide(100.0 + G) is Decision.SHELVE_DUE


def test_reconnect_before_the_deadline_cancels_the_timer_and_a_new_disconnect_restarts_it():
    machine = idle_machine()
    machine.connect("b", 399.0)
    assert machine.phase is Phase.ACTIVE
    assert machine.decide(10_000.0) is Decision.WAIT
    machine.disconnect("b", 1000.0)
    assert machine.decide(1000.0 + G - 1) is Decision.WAIT
    assert machine.decide(1000.0 + G) is Decision.SHELVE_DUE


def test_multiple_sessions_keep_the_instance_active_until_the_last_one_leaves():
    machine = SessionMachine()
    machine.connect("a", 0.0)
    machine.connect("b", 1.0)
    machine.disconnect("a", 2.0)
    assert machine.phase is Phase.ACTIVE
    assert machine.decide(10_000.0) is Decision.WAIT
    machine.disconnect("b", 50.0)
    assert machine.decide(50.0 + G) is Decision.SHELVE_DUE


def test_disconnecting_an_unknown_session_changes_nothing():
    machine = idle_machine()
    generation = machine.generation
    machine.disconnect("never-connected", 500.0)
    assert machine.generation == generation
    assert machine.grace_started == 100.0


def test_observation_failure_is_not_a_disconnect():
    machine = SessionMachine()
    machine.connect("a", 0.0)
    machine.observation_failed(10.0)
    machine.observation_failed(20.0)
    assert machine.phase is Phase.ACTIVE and machine.sessions == {"a"}
    assert machine.decide(10_000.0) is Decision.WAIT
    assert machine.observation_failures == 2
    gone = idle_machine()
    gone.observation_failed(150.0)
    assert gone.grace_started == 100.0


def test_a_new_instance_nobody_connected_to_still_becomes_due():
    machine = SessionMachine.created(50.0)
    assert machine.decide(50.0 + G - 1) is Decision.WAIT
    assert machine.decide(50.0 + G) is Decision.SHELVE_DUE


def test_only_one_of_two_concurrent_claims_wins():
    machine = idle_machine()
    first = machine.claim_shelve(100.0 + G)
    second = machine.claim_shelve(100.0 + G)
    assert first is not None and second is None
    assert machine.phase is Phase.SHELVING


def test_a_connect_between_decision_and_shelve_start_invalidates_the_claim():
    machine = idle_machine()
    claim = machine.claim_shelve(100.0 + G)
    assert machine.shelve_still_valid(claim)
    machine.connect("late", 100.0 + G + 0.5)
    assert not machine.shelve_still_valid(claim)


def test_reconnect_during_an_inflight_shelve_requires_unshelve_after_it_settles():
    machine = idle_machine()
    claim = machine.claim_shelve(100.0 + G)
    machine.connect("late", 100.0 + G + 1)
    assert machine.shelve_settled(claim, 500.0, ok=True) is True
    assert machine.phase is Phase.SHELVED
    machine.unshelved()
    assert machine.phase is Phase.ACTIVE


def test_uninterrupted_shelve_settles_without_requiring_unshelve():
    machine = idle_machine()
    claim = machine.claim_shelve(100.0 + G)
    assert machine.shelve_settled(claim, 450.0, ok=True) is False
    assert machine.phase is Phase.SHELVED
    machine.connect("user", 9000.0)
    assert machine.reconnected_during_shelve is True
    machine.unshelved()
    assert machine.phase is Phase.ACTIVE


def test_failed_shelve_keeps_the_instance_and_restarts_the_full_grace_period():
    machine = idle_machine()
    claim = machine.claim_shelve(100.0 + G)
    assert machine.shelve_settled(claim, 450.0, ok=False) is False
    assert machine.phase is Phase.GRACE
    assert machine.claim_shelve(450.0 + G - 1) is None
    assert machine.claim_shelve(450.0 + G) is not None


def test_failed_shelve_with_a_reconnected_user_returns_to_active():
    machine = idle_machine()
    claim = machine.claim_shelve(100.0 + G)
    machine.connect("late", 420.0)
    assert machine.shelve_settled(claim, 450.0, ok=False) is False
    assert machine.phase is Phase.ACTIVE and machine.grace_started is None


def test_settling_or_unshelving_in_the_wrong_phase_is_an_error():
    machine = idle_machine()
    with pytest.raises(ValueError):
        machine.unshelved()
    claim = machine.claim_shelve(100.0 + G)
    machine.shelve_settled(claim, 450.0, ok=True)
    with pytest.raises(ValueError):
        machine.shelve_settled(claim, 460.0, ok=True)


def test_nonpositive_grace_is_rejected():
    with pytest.raises(ValueError):
        SessionMachine(grace_seconds=0)


def test_a_machine_survives_a_json_round_trip_exactly():
    machine = SessionMachine(grace_seconds=120.0)
    machine.connect("a", 5.0)
    machine.connect("b", 6.0)
    machine.disconnect("a", 7.0)
    machine.observation_failed(8.0)
    restored = SessionMachine.from_state(machine.to_state())
    assert restored == machine and restored.sessions == {"b"} and restored.phase is Phase.ACTIVE
    idle = idle_machine()
    assert SessionMachine.from_state(idle.to_state()).decide(100.0 + G) is Decision.SHELVE_DUE


@pytest.mark.parametrize(
    "broken",
    [
        {},
        {"phase": "nonsense"},
        {"sessions": "a"},
        {"generation": True},
        {"grace_started": "soon"},
        {"grace_seconds": None},
        {"reconnected_during_shelve": 1},
    ],
)
def test_a_malformed_snapshot_is_refused_rather_than_guessed(broken):
    state = {**SessionMachine().to_state(), **broken} if broken else {}
    with pytest.raises(ValueError):
        SessionMachine.from_state(state)


def test_resume_of_a_userless_machine_starts_a_new_full_grace_only_when_given_the_time():
    machine = SessionMachine(phase=Phase.SHELVED)
    machine.unshelved(1000.0)
    assert machine.phase is Phase.GRACE and machine.grace_started == 1000.0
    assert machine.decide(1000.0 + G - 1) is Decision.WAIT and machine.decide(1000.0 + G) is Decision.SHELVE_DUE
    bare = SessionMachine(phase=Phase.SHELVED)
    bare.unshelved()
    assert bare.grace_started is None and bare.decide(10**9) is Decision.WAIT
