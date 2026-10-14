"""Bootstrap secrets never use argv; speech conversion obeys PCM and volume limits."""

import io
import struct
import sys
import wave
from pathlib import Path
from unittest.mock import Mock

import pytest
from tvpet_console.cli import main
from tvpet_console.speech import SpeechService, synthesize


def test_admin_init_register_and_tls_cli(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSOLE_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        "tvpet_console.cli.getpass.getpass", lambda prompt: "test-only-long-password"
    )
    monkeypatch.setattr(sys, "argv", ["robot-console", "admin", "init"])
    main()
    token_file = tmp_path / "agent.token"
    monkeypatch.setattr(
        sys, "argv", ["robot-console", "agent", "register", "--token-file", str(token_file)]
    )
    main()
    assert token_file.stat().st_mode & 0o077 == 0
    assert len(token_file.read_text()) >= 32
    with pytest.raises(SystemExit):
        main()
    serve = Mock()
    monkeypatch.setattr("tvpet_console.cli.uvicorn.run", serve)
    monkeypatch.setattr(sys, "argv", ["robot-console", "serve"])
    main()
    assert serve.call_args.kwargs["host"] == "127.0.0.1"
    monkeypatch.setattr(sys, "argv", ["robot-console", "serve", "--host", "0.0.0.0"])
    with pytest.raises(SystemExit):
        main()


def test_local_tts_pcm_volume_and_cleanup(monkeypatch) -> None:
    files = []

    def run(args, **kwargs):
        if args[0].endswith("say"):
            source = Path(args[args.index("-f") + 1])
            files.append(source)
            assert source.read_text() == "测试声音"
            Path(args[args.index("-o") + 1]).write_bytes(b"AIFF")
        else:
            with wave.open(args[-1], "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(struct.pack("<hh", 1000, -1000))

    monkeypatch.setattr("tvpet_console.speech.subprocess.run", run)
    monkeypatch.setattr("tvpet_console.speech.sys.platform", "darwin")
    payload = synthesize("测试声音", 0.5)
    with wave.open(io.BytesIO(payload)) as output:
        assert output.getframerate() == 16000
        assert struct.unpack("<hh", output.readframes(2)) == (500, -500)
    assert all(not file.exists() for file in files)
    monkeypatch.setattr("tvpet_console.speech.sys.platform", "linux")
    with pytest.raises(ValueError):
        synthesize("测试", 0.5)
    speech = SpeechService("")
    assert not speech.asr_ready
    with pytest.raises(ValueError):
        speech.transcribe(bytes(640))
