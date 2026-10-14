import unittest

from tvpet_pi.media import AudioChunk, VideoFrame
from tvpet_pi.session import PiSessionController, SessionRequest, SessionState


class FakePort:
    def __init__(self, frame=None):
        self.frame = frame
        self.opens = 0
        self.closes = 0
        self.plays = []

    def open(self):
        self.opens += 1

    def close(self):
        self.closes += 1

    def read(self):
        return self.frame

    def play(self, chunk):
        self.plays.append(chunk)


class FailingClosePort(FakePort):
    def close(self):
        super().close()
        raise RuntimeError("hardware close failed")


class SessionTests(unittest.TestCase):
    def test_no_capture_without_explicit_session_and_adapter(self):
        controller = PiSessionController()
        with self.assertRaises(RuntimeError):
            controller.read_video()
        with self.assertRaises(RuntimeError):
            controller.start(SessionRequest("session-1", video=True))
        self.assertEqual(controller.state, SessionState.IDLE)

    def test_only_requested_ports_open_and_close(self):
        frame = VideoFrame(1, 640, 480, "jpeg", b"image")
        camera, microphone, speaker = FakePort(frame), FakePort(), FakePort()
        controller = PiSessionController(camera, microphone, speaker)
        controller.start(SessionRequest("session-2", video=True, speaker=True))
        self.assertEqual(controller.read_video(), frame)
        with self.assertRaises(RuntimeError):
            controller.read_microphone()
        chunk = AudioChunk(2, 16000, 1, "pcm_s16le", b"voice")
        controller.play(chunk)
        self.assertEqual(speaker.plays, [chunk])
        self.assertEqual((camera.opens, microphone.opens, speaker.opens), (1, 0, 1))
        with self.assertRaises(ValueError):
            controller.stop("wrong-session")
        controller.stop("session-2")
        self.assertEqual((camera.closes, microphone.closes, speaker.closes), (1, 0, 1))
        self.assertEqual(controller.state, SessionState.IDLE)

    def test_close_failure_still_releases_other_ports(self):
        camera, speaker = FakePort(), FailingClosePort()
        controller = PiSessionController(camera=camera, speaker=speaker)
        controller.start(SessionRequest("session-3", video=True, speaker=True))
        with self.assertRaises(RuntimeError):
            controller.stop("session-3")
        self.assertEqual(camera.closes, 1)
        self.assertEqual(controller.state, SessionState.IDLE)


if __name__ == "__main__":
    unittest.main()
