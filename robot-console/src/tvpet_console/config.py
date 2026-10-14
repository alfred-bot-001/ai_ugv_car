"""Runtime configuration; secrets and state live outside the source checkout."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    origin: str = "https://localhost:8788"
    secure_cookie: bool = True
    agent_id: str = "laosan"
    asr_model: str = ""
    face_service_url: str = ""
    pose_review_dir: str = ""
    pose_review_font: str = ""
    sso_config_file: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(os.environ.get("CONSOLE_DATA_DIR", "private/console")),
            origin=os.environ.get("CONSOLE_ORIGIN", "https://localhost:8788").rstrip("/"),
            secure_cookie=os.environ.get("CONSOLE_DEV_HTTP", "0") != "1",
            asr_model=os.environ.get("CONSOLE_ASR_MODEL", ""),
            face_service_url=os.environ.get("CONSOLE_FACE_SERVICE_URL", ""),
            pose_review_dir=os.environ.get("CONSOLE_POSE_REVIEW_DIR", ""),
            pose_review_font=os.environ.get("CONSOLE_POSE_REVIEW_FONT", ""),
            sso_config_file=os.environ.get("CONSOLE_SSO_CONFIG_FILE", ""),
        )
