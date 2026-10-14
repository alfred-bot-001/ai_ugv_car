"""A still-unloading launchd job must not leave upgrades permanently stopped."""

import plistlib
import runpy
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock


def test_launchd_retries_transient_bootstrap(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "deploy"
    executable = runtime / ".venv/bin/robot-console"
    executable.parent.mkdir(parents=True)
    executable.touch()
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert.touch()
    key.touch()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "install_launchd.py",
            "--runtime",
            str(runtime),
            "--origin",
            "https://car.aimaker.vip",
            "--cert",
            str(cert),
            "--key",
            str(key),
        ],
    )
    execute = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 5, stderr="Previous job is still unloading"),
            subprocess.CompletedProcess([], 0),
        ]
    )
    wait = Mock()
    monkeypatch.setattr("subprocess.run", execute)
    monkeypatch.setattr("time.sleep", wait)
    runpy.run_path(
        str(Path(__file__).parents[1] / "deploy/install_launchd.py"), run_name="__main__"
    )
    assert execute.call_count == 3
    wait.assert_called_once_with(0.5)
    configuration = plistlib.loads(
        (tmp_path / "Library/LaunchAgents/com.ai-tv-pet.console.plist").read_bytes()
    )
    assert configuration["EnvironmentVariables"]["CONSOLE_ORIGIN"] == "https://car.aimaker.vip"
    assert configuration["ProgramArguments"][0] == str(executable)
