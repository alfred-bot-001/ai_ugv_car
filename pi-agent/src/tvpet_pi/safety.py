"""Pi-monotonic leases and single-use challenges, independent of Mini clocks."""

import math
import secrets
import time
from collections.abc import Callable

from .protocol import Stick


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
    """Revokes on epoch/disconnect, stale challenge, press budget and emergency stop."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.epoch = secrets.token_hex(16)
        self.lease = ""
        self.lease_deadline = 0.0
        self.press_deadline = 0.0
        self.motion_deadline = 0.0
        self.last_heartbeat = 0.0
        self.sequence = 0
        self.challenges: dict[str, float] = {}
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
        now = self.clock()
        self.lease = secrets.token_urlsafe(24)
        self.lease_deadline = now + 10
        self.press_deadline = now + 2
        self.sequence = 0
        self.challenges.clear()
        return self.lease

    def heartbeat(self) -> None:
        self.last_heartbeat = self.clock()

    def accept(self, stick: Stick) -> tuple[float, float]:
        now = self.clock()
        expiry = self.challenges.pop(stick.challenge, 0)
        ready = self.wheels_verified if stick.target == "wheels" else self.gimbal_verified
        if (
            self.estopped
            or not ready
            or not self.lease
            or stick.lease != self.lease
            or now >= min(expiry, self.lease_deadline, self.press_deadline)
            or now - self.last_heartbeat >= 0.6
            or stick.sequence <= self.sequence
        ):
            raise ValueError("控制被拒绝：设备未验收、租约或 challenge 已失效")
        self.sequence = stick.sequence
        self.motion_deadline = min(expiry, self.lease_deadline, self.press_deadline)
        return circle(stick.x, stick.y)

    def expired(self) -> bool:
        now = self.clock()
        return bool(self.lease) and (
            now >= min(self.press_deadline, self.lease_deadline)
            or now - self.last_heartbeat >= 0.6
            or (self.motion_deadline > 0 and now >= self.motion_deadline)
        )

    def release(self, *, emergency: bool = False) -> None:
        self.lease = ""
        self.challenges.clear()
        self.motion_deadline = 0.0
        self.estopped = self.estopped or emergency

    def disconnect(self) -> None:
        self.release()
        self.epoch = secrets.token_hex(16)

    def reset(self) -> None:
        self.release()
        self.estopped = False
