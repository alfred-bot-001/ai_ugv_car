"""Sleep mode (2026-10-25, user request): the LiDAR motor runs whenever its USB port has
power and drains the battery while nobody uses the car. After thirty quiet minutes
pi-agent asks ugv-navd to cut that port's power (and stop the navigation container);
the next interaction wakes it. Pure timing logic here; the agent supplies the clock and
decides what counts as busy (viewers, a running nav job, an unsaved map...)."""

from typing import Any

IDLE_SECONDS = 30 * 60.0


class SleepPolicy:
    def __init__(self, idle_seconds: float = IDLE_SECONDS) -> None:
        self.idle_seconds = idle_seconds
        self.last_activity: float | None = None
        self.asleep = False

    def touch(self, now: float) -> str | None:
        """An interaction happened now; returns "wake" the first time it ends a sleep."""
        self.last_activity = now
        if self.asleep:
            self.asleep = False
            return "wake"
        return None

    def tick(self, now: float, *, busy: bool) -> str | None:
        """Periodic check: "sleep" once the quiet time is up, "wake" when busy work starts."""
        if busy:
            return self.touch(now)
        if self.last_activity is None:
            self.last_activity = now
            return None
        if not self.asleep and now - self.last_activity >= self.idle_seconds:
            self.asleep = True
            return "sleep"
        return None

    def status(self, now: float) -> dict[str, Any]:
        idle = 0 if self.last_activity is None else int(now - self.last_activity)
        return {"state": "asleep" if self.asleep else "awake", "idle_seconds": idle}
