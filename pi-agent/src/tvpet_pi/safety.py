"""Pi-monotonic leases, the held stick states the Board keeps driving, audio challenges."""

import math
import secrets
import time
from collections.abc import Callable

from .protocol import Stick

# 2026-10-22, user decision: the web knobs stay where they are put and the car keeps
# moving until a knob is centred, a stop button is pressed, the page hides or the
# link drops. The page relays each held state about ten times a second; a held state
# older than this drives nothing more (the Board writes the stop commands) but the
# lease stays, so the next relayed state resumes without a new lease. There is no
# per-command challenge and no per-press time budget any more: both used to turn a
# slow public round trip into a rejected command, a toast and a stopped car.
HOLD_MAX_AGE = 0.5
# Mini heartbeats arrive every 0.2 s; three missed ones end the lease.
HEARTBEAT_MAX_AGE = 0.6


class LeaseError(ValueError):
    """The stick names a lease the Guard no longer holds; the browser leases again."""

    code = "lease"


def circle(x: float, y: float) -> tuple[float, float]:
    length = max(1.0, math.hypot(x, y))
    return x / length, y / length


def differential(x: float, y: float, maximum: float = 0.10) -> tuple[float, float]:
    """Mix a circle into a bounded factory T1 scalar; units depend on base firmware."""
    x, y = circle(x, y)
    left, right = y + x, y - x
    scale = max(1.0, abs(left), abs(right))
    return maximum * left / scale, maximum * right / scale


class Guard:
    """Revokes on epoch/disconnect, stale heartbeat, session deadline and emergency stop."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.epoch = secrets.token_hex(16)
        self.lease = ""
        self.lease_deadline = 0.0
        self.press_deadline = 0.0
        self.last_heartbeat = 0.0
        self.sequence = 0
        # Single-use challenges remain for audio playback only.
        self.challenges: dict[str, float] = {}
        # The last accepted stick per target and when it arrived.
        self.held: dict[str, tuple[Stick, float]] = {}
        self.estopped = False
        self.gimbal_verified = False
        self.wheels_verified = False

    def challenge(self) -> str:
        now = self.clock()
        self.challenges = {key: expiry for key, expiry in self.challenges.items() if expiry > now}
        value = secrets.token_urlsafe(18)
        self.challenges[value] = now + 0.45
        return value

    def acquire(self) -> str:
        if self.estopped or self.lease:
            raise ValueError("急停锁定或已有控制租约")
        self.lease = secrets.token_urlsafe(24)
        # No time budget for the web knobs; nav.py bounds its own arm session by
        # overwriting both deadlines.
        self.lease_deadline = math.inf
        self.press_deadline = math.inf
        self.sequence = 0
        self.challenges.clear()
        self.held.clear()
        return self.lease

    def heartbeat(self) -> None:
        self.last_heartbeat = self.clock()

    def accept(self, stick: Stick) -> tuple[float, float] | None:
        """Hold a new stick state; None means an older state arrived late and is ignored."""
        now = self.clock()
        if self.estopped:
            raise ValueError("急停已锁定，先解除急停")
        if stick.target == "wheels" and not self.wheels_verified:
            raise ValueError("底盘尚未完成实机验收")
        if stick.target == "gimbal" and not self.gimbal_verified:
            raise ValueError("云台尚未完成实机验收")
        if not self.lease or stick.lease != self.lease:
            raise LeaseError("控制租约已更换")
        if now >= min(self.lease_deadline, self.press_deadline):
            raise ValueError("控制会话已到期")
        if stick.sequence <= self.sequence:
            return None
        self.sequence = stick.sequence
        self.held[stick.target] = (stick, now)
        return circle(stick.x, stick.y)

    def holding(self, target: str) -> Stick | None:
        """The state the Board keeps driving: fresh, under the lease, with the link alive."""
        entry = self.held.get(target)
        if entry is None or not self.lease or self.expired():
            return None
        stick, accepted_at = entry
        if self.clock() - accepted_at >= HOLD_MAX_AGE:
            return None
        return stick

    def expired(self) -> bool:
        now = self.clock()
        return bool(self.lease) and (
            now >= min(self.press_deadline, self.lease_deadline)
            or now - self.last_heartbeat >= HEARTBEAT_MAX_AGE
        )

    def release(self, *, emergency: bool = False) -> None:
        self.lease = ""
        self.challenges.clear()
        self.held.clear()
        self.estopped = self.estopped or emergency

    def disconnect(self) -> None:
        self.release()
        self.epoch = secrets.token_hex(16)

    def reset(self) -> None:
        self.release()
        self.estopped = False
