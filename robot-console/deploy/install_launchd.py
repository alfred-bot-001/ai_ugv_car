"""Install the packaged Mini core from an existing isolated deploy environment."""

import argparse
import os
import plistlib
import shlex
import subprocess
import time
from pathlib import Path

LABEL = "com.ai-tv-pet.console"


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="安装 Mini 主控台 launchd 服务")
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--cert", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--asr-model", type=Path)
    parser.add_argument("--face-service-url", default="")
    parser.add_argument("--pose-review-dir", type=Path)
    parser.add_argument("--pose-review-font", type=Path)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    executable = runtime / ".venv/bin/robot-console"
    if not executable.is_file() or not args.cert.is_file() or not args.key.is_file():
        parser.error("先安装运行环境并配置证书和私钥")
    if not args.origin.startswith("https://"):
        parser.error("对外入口必须使用 HTTPS")
    for directory in (runtime / "logs", runtime / "state"):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    environment = {
        "CONSOLE_DATA_DIR": str(runtime / "state"),
        "CONSOLE_ORIGIN": args.origin,
        "CONSOLE_DEV_HTTP": "0",
        "PYTHONUNBUFFERED": "1",
    }
    if args.asr_model:
        environment["CONSOLE_ASR_MODEL"] = str(args.asr_model.resolve())
    if args.face_service_url:
        environment["CONSOLE_FACE_SERVICE_URL"] = args.face_service_url
    if args.pose_review_dir:
        environment["CONSOLE_POSE_REVIEW_DIR"] = str(args.pose_review_dir.resolve())
    if args.pose_review_font:
        environment["CONSOLE_POSE_REVIEW_FONT"] = str(args.pose_review_font.resolve())
    configuration = {
        "Label": LABEL,
        "ProgramArguments": [
            str(executable),
            "serve",
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--cert",
            str(args.cert.resolve()),
            "--key",
            str(args.key.resolve()),
        ],
        "EnvironmentVariables": environment,
        "WorkingDirectory": str(runtime),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ExitTimeOut": 6,
        "Umask": 0o077,
        "StandardOutPath": str(runtime / "logs/console.out.log"),
        "StandardErrorPath": str(runtime / "logs/console.err.log"),
    }
    location = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
    location.parent.mkdir(parents=True, exist_ok=True)
    location.write_bytes(plistlib.dumps(configuration))
    os.chmod(location, 0o600)
    setup = runtime / "setup-admin.command"
    setup.write_text(
        "#!/bin/zsh\nset -eu\numask 077\n"
        + "export CONSOLE_DATA_DIR="
        + shlex.quote(str(runtime / "state"))
        + "\n"
        + "exec "
        + shlex.quote(str(executable))
        + " admin init\n"
    )
    os.chmod(setup, 0o700)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain + "/" + LABEL], capture_output=True)
    # bootout can return while launchd is still removing the previous job.
    for attempt in range(12):
        result = subprocess.run(
            ["launchctl", "bootstrap", domain, str(location)], capture_output=True, text=True
        )
        if result.returncode == 0:
            break
        if result.returncode != 5 or attempt == 11:
            result.check_returncode()
        time.sleep(0.5)
    print(f"服务已安装：{args.origin}")
    print(f"首次密码初始化：{setup}")


if __name__ == "__main__":
    main()
