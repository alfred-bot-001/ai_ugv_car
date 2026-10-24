"""Sleep mode: thirty quiet minutes put the car to sleep; any interaction wakes it."""

from tvpet_pi.sleep import IDLE_SECONDS, SleepPolicy


def test_sleeps_after_thirty_quiet_minutes_and_not_before() -> None:
    policy = SleepPolicy()
    assert IDLE_SECONDS == 30 * 60
    assert policy.tick(0.0, busy=False) is None
    assert policy.tick(IDLE_SECONDS - 1, busy=False) is None
    assert policy.tick(IDLE_SECONDS, busy=False) == "sleep"
    assert policy.asleep
    assert policy.tick(IDLE_SECONDS + 5, busy=False) is None  # said once


def test_interaction_resets_the_clock_and_wakes() -> None:
    policy = SleepPolicy()
    policy.tick(0.0, busy=False)
    policy.touch(1000.0)
    assert policy.tick(1000.0 + IDLE_SECONDS - 1, busy=False) is None
    assert policy.tick(1000.0 + IDLE_SECONDS, busy=False) == "sleep"
    # touch() while asleep wakes at once and says so, exactly once.
    assert policy.touch(5000.0) == "wake"
    assert not policy.asleep
    assert policy.touch(5001.0) is None
    assert policy.tick(5001.0 + IDLE_SECONDS, busy=False) == "sleep"


def test_busy_work_counts_as_interaction() -> None:
    """Someone watching video, a running nav job or an unsaved map keeps the car awake."""
    policy = SleepPolicy()
    policy.tick(0.0, busy=False)
    assert policy.tick(IDLE_SECONDS, busy=True) is None
    assert policy.tick(2 * IDLE_SECONDS - 1, busy=False) is None
    assert policy.tick(2 * IDLE_SECONDS, busy=False) == "sleep"
    # Busy work that starts while asleep (an operator over ssh) wakes the car too.
    assert policy.tick(2 * IDLE_SECONDS + 10, busy=True) == "wake"


def test_status_reports_idle_time_and_state() -> None:
    policy = SleepPolicy(idle_seconds=60)
    policy.touch(100.0)
    assert policy.status(130.0) == {"state": "awake", "idle_seconds": 30}
    assert policy.tick(160.0, busy=False) == "sleep"
    assert policy.status(200.0) == {"state": "asleep", "idle_seconds": 100}
