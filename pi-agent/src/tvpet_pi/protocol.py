"""Versioned, bounded input contracts shared by Mini and Pi."""

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_JPEG = 1_048_576
MAX_AUDIO = 640
AGENT_PROTOCOL = "agent.v1"

# Web joystick T1 ceiling band (m/s). 2026-10-22, user-approved: the page gets a
# speed slider up to 0.18, the navigation PWM_LIMIT approved on 2026-10-14, below the
# 0.3 m/s runaway stop. The floor is 0.07 like nav.WHEEL_FLOOR: the 2026-10-21 probes
# showed the firmware PID pausing wheels below about 0.06 m/s. The default stays 0.10.
JOYSTICK_SPEED_MIN = 0.07
JOYSTICK_SPEED_DEFAULT = 0.10
JOYSTICK_SPEED_MAX = 0.18


class Stick(BaseModel):
    """Normalized circular joystick; y is positive upwards."""

    model_config = ConfigDict(extra="forbid")
    target: Literal["wheels", "gimbal"]
    x: float = Field(ge=-1, le=1)
    y: float = Field(ge=-1, le=1)
    lease: str = Field(min_length=16, max_length=128)
    challenge: str = Field(min_length=16, max_length=128)
    sequence: int = Field(ge=1)
    # T1 ceiling for this command; only the wheels use it.
    speed: float = Field(
        default=JOYSTICK_SPEED_DEFAULT, ge=JOYSTICK_SPEED_MIN, le=JOYSTICK_SPEED_MAX
    )

    @field_validator("x", "y", "speed")
    @classmethod
    def finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("finite coordinate required")
        return value


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: Literal["admin"]
    password: str = Field(min_length=1, max_length=256)


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    old_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=400)
    volume: float = Field(default=0.5, ge=0, le=1)


class PosePerson(BaseModel):
    track_id: int = Field(ge=1)
    posture: Literal["standing", "sitting", "lying", "squatting", "curled", "unknown"]
    posture_label: str = Field(max_length=30)
    score: float = Field(ge=0, le=1, allow_inf_nan=False)
    action: Literal["waving", "raised_hand", "none"]
    action_label: str = Field(max_length=30)
    keypoints: list[tuple[float, float, float, float]] = Field(min_length=33, max_length=33)

    @field_validator("keypoints")
    @classmethod
    def valid_points(
        cls, points: list[tuple[float, float, float, float]]
    ) -> list[tuple[float, float, float, float]]:
        if any(
            not all(math.isfinite(v) and abs(v) <= 10 for v in p) or not 0 <= p[3] <= 1
            for p in points
        ):
            raise ValueError("invalid pose keypoints")
        return points


class PoseResult(BaseModel):
    state: Literal["active", "unavailable", "disabled"]
    people: list[PosePerson] = Field(default_factory=list, max_length=3)
    model: str = Field(default="", max_length=80)
    rules: str = Field(default="", max_length=80)
    inference_ms: float | None = Field(default=None, ge=0, le=60000, allow_inf_nan=False)
