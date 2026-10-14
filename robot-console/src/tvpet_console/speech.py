"""Mini-local speech services; temporary audio is removed after every request."""

import asyncio
import io
import os
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import Any


def synthesize(text: str, volume: float) -> bytes:
    """Generate bounded mono 16kHz PCM WAV using the installed macOS voice."""
    if sys.platform != "darwin":
        raise ValueError("当前主机未配置语音合成提供者")
    with tempfile.TemporaryDirectory(prefix="tvpet-speech-") as directory:
        root = Path(directory)
        source, aiff, output = root / "text.txt", root / "voice.aiff", root / "voice.wav"
        source.write_text(text)
        os.chmod(source, 0o600)
        subprocess.run(
            ["/usr/bin/say", "-v", "Tingting", "-f", str(source), "-o", str(aiff)],
            check=True,
            timeout=20,
            capture_output=True,
        )
        subprocess.run(
            [
                "/usr/bin/afconvert",
                "-f",
                "WAVE",
                "-d",
                "LEI16@16000",
                "-c",
                "1",
                str(aiff),
                str(output),
            ],
            check=True,
            timeout=10,
            capture_output=True,
        )
        with wave.open(str(output)) as wav:
            if wav.getnframes() > 960000 or wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                raise ValueError("合成音频超出 60 秒限制")
            frames = wav.readframes(wav.getnframes())
        samples = struct.unpack(f"<{len(frames) // 2}h", frames)
        scaled = struct.pack(f"<{len(samples)}h", *(int(sample * volume) for sample in samples))
        result = io.BytesIO()
        with wave.open(result, "wb") as wav_out:
            wav_out.setnchannels(1)
            wav_out.setsampwidth(2)
            wav_out.setframerate(16000)
            wav_out.writeframes(scaled)
        return result.getvalue()


class SpeechService:
    def __init__(self, model_path: str) -> None:
        self.model_path = model_path
        self.model: Any = None
        self.lock = asyncio.Lock()
        self.tts_lock = asyncio.Lock()

    @property
    def asr_ready(self) -> bool:
        root = Path(self.model_path)
        return bool(self.model_path) and all(
            (root / name).is_file()
            for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")
        )

    def transcribe(self, pcm: bytes) -> str:
        if not self.asr_ready:
            raise ValueError("尚未安装 Mini 本地转写模型")
        import numpy as np  # type: ignore[import-not-found]
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]

        if self.model is None:
            self.model = WhisperModel(self.model_path, device="cpu", compute_type="int8")
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
        if samples.size == 0 or float(np.sqrt(np.mean(samples * samples))) < 0.001:
            return ""
        segments, _ = self.model.transcribe(
            samples, language="zh", vad_filter=False, beam_size=5, condition_on_previous_text=False
        )
        return "".join(segment.text for segment in segments).strip()
