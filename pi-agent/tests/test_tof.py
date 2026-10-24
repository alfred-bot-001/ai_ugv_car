"""ToF bridge frames, floor baseline and low-obstacle judgement never start motion."""

import json
import os
import threading
import time
from pathlib import Path

import pytest
from tvpet_pi import tof
from tvpet_pi.tof import (
    CELLS,
    CONSECUTIVE_FRAMES,
    FRAME_TIMEOUT,
    INVALID_MM,
    REASON_CLIFF,
    REASON_LOW_OBSTACLE,
    STARTUP_GRACE,
    Baseline,
    Detector,
    Frame,
    RateMeter,
    ToFLink,
    classify,
    median_baseline,
    parse_frame,
)


def line(values: list[int], tag: str = "T", seq: int = 7, millis: int = 1234) -> bytes:
    fields = [tag, str(seq), str(millis), *map(str, values), str(sum(values) & 0xFFFF)]
    return (",".join(fields) + "\n").encode()


def floor_frame(near: int = 150, far: int = INVALID_MM) -> list[int]:
    """Rows 0-3 look ahead (no return), rows 4-7 see the floor nearer and nearer."""
    values: list[int] = []
    for row in range(8):
        if row < 4:
            values += [far] * 8
        else:
            values += [near + (7 - row) * 80] * 8
    return values


def test_parse_accepts_bridge_frames_and_rejects_everything_else() -> None:
    values = list(range(64))
    frame = parse_frame(line(values))
    assert frame == Frame("T", 7, 1234, tuple(values))
    assert parse_frame(line(values, tag="H")).tag == "H"  # type: ignore[union-attr]
    assert parse_frame(line(values).rstrip(b"\n") + b"\r\n") == frame
    big = [60000] * 64  # the checksum is the low 16 bits of the sum
    assert parse_frame(line(big)) is not None
    assert parse_frame(b"# tof-bridge 1.0.0\n") is None
    assert parse_frame(b"ESP-ROM:esp32c5-eco2-20250121\r\n") is None
    assert parse_frame(line(values)[:-3] + b"9\n") is None  # wrong checksum
    assert parse_frame(line(values[:-1])) is None  # 63 cells
    assert parse_frame(line(values).replace(b",1,", b",x,", 1)) is None
    assert parse_frame(line([70000] * 64)) is None  # beyond uint16
    assert parse_frame(b"T," + b"1," * 2000) is None
    assert parse_frame(b"\n") is None and parse_frame(b"") is None


def test_median_baseline_round_trips_through_a_private_file(tmp_path: Path) -> None:
    frames = [tuple(floor_frame(150 + delta)) for delta in (-10, 0, 10, 300)]
    values = median_baseline(frames)
    assert values[56] == 155  # median of 140, 150, 160, 450 (bottom row, nearest)
    with pytest.raises(ValueError):
        median_baseline(frames[:2])
    with pytest.raises(ValueError):
        median_baseline([frames[0][:10]] * 3)
    baseline = Baseline(values, 1_700_000_000.0, len(frames))
    path = tmp_path / "data" / "tof-baseline.json"
    baseline.save(path)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    assert Baseline.load(path) == baseline
    assert sum(baseline.ground()) == 32  # four floor rows
    for broken in (
        {"schema": 2, "values": list(values)},
        {"schema": 1, "values": list(values)[:63]},
        {"schema": 1, "values": [1.5] * 64},
        {"schema": 1, "values": list(values), "created_at": "x"},
        [],
    ):
        with pytest.raises(ValueError):
            Baseline.from_json(broken)


@pytest.fixture
def baseline() -> Baseline:
    return Baseline(tuple(floor_frame()), 0.0, 5)


def test_classify_floor_cells_against_the_baseline(baseline: Baseline) -> None:
    clear = floor_frame()
    assert classify(tuple(clear), baseline) == ([], [])
    # Carpet edge or pitch: a 25 mm change on the 150-390 mm floor cells is not an obstacle.
    assert classify(tuple(v - 25 if v < 1000 else v for v in clear), baseline) == ([], [])
    # A sill under the bottom-right cells: readings drop well below 0.8 x baseline.
    sill = list(clear)
    for index in (61, 62, 63):
        sill[index] = 90
    assert classify(tuple(sill), baseline) == ([61, 62, 63], [])
    # The floor disappears under the bottom row: no return or much further.
    cliff = list(clear)
    for index in range(56, 60):
        cliff[index] = INVALID_MM
    for index in range(60, 64):
        cliff[index] = 900
    assert classify(tuple(cliff), baseline) == ([], list(range(56, 64)))
    assert classify(tuple(cliff), baseline, cliffs=False) == ([], [])


def test_classify_forward_cells_use_the_centre_columns_and_treat_4000_as_unknown(
    baseline: Baseline,
) -> None:
    ahead = floor_frame()
    for index in (2, 3, 4, 5):  # row 0, centre: a box 20 cm ahead
        ahead[index] = 200
    ahead[0] = ahead[7] = 100  # edge columns: a door frame beside the car, ignored
    assert classify(tuple(ahead), baseline) == ([2, 3, 4, 5], [])
    far = floor_frame()
    far[3] = 260  # just beyond the stop distance
    assert classify(tuple(far), baseline) == ([], [])
    assert classify(tuple(floor_frame(far=INVALID_MM)), baseline) == ([], [])


def test_detector_needs_agreeing_cells_over_consecutive_frames(baseline: Baseline) -> None:
    detector = Detector(baseline)
    sill = floor_frame()
    for index in (61, 62, 63):
        sill[index] = 90
    two_cells = floor_frame()
    two_cells[62] = two_cells[63] = 90
    for _ in range(CONSECUTIVE_FRAMES - 1):
        assert detector.update(tuple(sill)) == ""
    assert detector.update(tuple(floor_frame())) == ""  # one clean frame resets the count
    for _ in range(CONSECUTIVE_FRAMES - 1):
        assert detector.update(tuple(sill)) == ""
    assert detector.update(tuple(sill)) == REASON_LOW_OBSTACLE
    assert detector.update(tuple(sill)) == REASON_LOW_OBSTACLE  # held while it persists
    assert detector.obstacle_cells == [61, 62, 63]
    assert detector.update(tuple(floor_frame())) == ""
    for _ in range(CONSECUTIVE_FRAMES + 1):
        assert detector.update(tuple(two_cells)) == ""  # below OBSTACLE_MIN_CELLS
    cliff = floor_frame()
    for index in range(56, 64):
        cliff[index] = INVALID_MM
    for _ in range(CONSECUTIVE_FRAMES):
        verdict = detector.update(tuple(cliff))
    assert verdict == REASON_CLIFF
    Detector(baseline, cliffs=False)
    assert all(Detector(None).update(tuple(sill)) == "" for _ in range(5))


def test_rate_meter_counts_frames_in_its_window() -> None:
    meter = RateMeter(window=2.0)
    assert meter.hz(0.0) == 0.0
    for i in range(31):
        meter.add(10.0 + i / 15)
    assert meter.hz(12.0) == pytest.approx(15.0)
    assert meter.hz(20.0) == 0.0


def test_link_health_follows_the_port_and_the_last_frame(tmp_path: Path) -> None:
    now = [100.0]
    link = ToFLink("/dev/serial/by-id/usb-Espressif-if00", tmp_path / "b", clock=lambda: now[0])
    assert link.state() == "fault" and link.status()["error"]
    link.serial = object()  # type: ignore[assignment]  # opened: the XIAO is rebooting
    link.opened_at = now[0]
    assert link.state() == "starting"
    now[0] += STARTUP_GRACE - 1
    assert link.state() == "starting"
    now[0] += 2
    assert link.state() == "fault" and link.status()["error"] == "等待传感器数据帧"
    boot = b"ESP-ROM:esp32c5\r\n# tof-bridge 1.0.0\r\n# ready\r\n"
    assert link.feed(boot + line(floor_frame())[:20], now[0]) == []
    assert link.bad_lines == 1
    frames = link.feed(line(floor_frame())[20:] + line(floor_frame(), tag="H", seq=8), now[0])
    assert [f.tag for f in frames] == ["T", "H"] and link.frames == 2
    status = link.status()
    assert status["state"] == "active" and status["seq"] == 8 and len(status["values"]) == CELLS
    assert status["baseline"] is False and status["baseline_error"] == ""
    now[0] += FRAME_TIMEOUT + 0.01
    assert link.state() == "fault" and link.stop_reason() == ""
    link.feed(b"x" * 5000, now[0])  # garbage without newline is dropped
    assert link.buffer == b"" and link.bad_lines == 2
    assert ToFLink("").state() == "disabled"


def test_link_reloads_the_baseline_and_only_stops_while_healthy(tmp_path: Path) -> None:
    now = [100.0]
    path = tmp_path / "tof-baseline.json"
    link = ToFLink("port", path, clock=lambda: now[0])
    link.reload_baseline()
    assert link.detector.baseline is None and link.baseline_error == "no baseline recorded"
    path.write_text("{bad")
    link.reload_baseline()
    assert link.detector.baseline is None and "Expecting" in link.baseline_error
    Baseline(tuple(floor_frame()), 1.0, 5).save(path)
    os.utime(path, (2_000_000_000, 2_000_000_000))
    link.reload_baseline()
    assert link.detector.baseline is not None and link.baseline_error == ""
    assert link.status()["ground_cells"] == 32
    sill = floor_frame()
    for index in (61, 62, 63):
        sill[index] = 90
    for seq in range(CONSECUTIVE_FRAMES):
        link.feed(line(sill, seq=seq), now[0])
    assert link.stop_reason() == REASON_LOW_OBSTACLE
    status = link.status()
    assert status["reason_text"] == "tof: low obstacle" and status["obstacle_cells"] == [61, 62, 63]
    now[0] += FRAME_TIMEOUT + 0.1  # a silent sensor can neither clear nor cause a stop
    assert link.stop_reason() == "" and link.status()["reason"] == ""
    link.feed(line(floor_frame()), now[0])
    assert link.stop_reason() == ""
    path.unlink()
    link.reload_baseline()
    assert link.detector.baseline is None


def test_collect_gathers_frames_for_calibration_and_times_out() -> None:
    link = ToFLink("port", None)
    with pytest.raises(TimeoutError):
        link.collect(3, timeout=0.05)
    assert link.collector is None
    feeder = threading.Timer(0.05, lambda: link.feed(line(floor_frame()) * 3, time.monotonic()))
    feeder.start()
    try:
        assert link.collect(3, timeout=2.0) == [tuple(floor_frame())] * 3
    finally:
        feeder.join()
    assert link.collector is None


def test_serial_open_keeps_dtr_and_rts_low_and_retries(monkeypatch, tmp_path: Path) -> None:
    opened: list[dict[str, object]] = []

    class FakeSerial:
        def __init__(self, port, baudrate, timeout, exclusive):
            self.port = port
            self.dtr = self.rts = True
            self.in_waiting = 0
            self.closed = False

        def open(self):
            opened.append({"port": self.port, "dtr": self.dtr, "rts": self.rts})
            if len(opened) == 1:
                raise tof.serial.SerialException("busy")

        def read(self, n):
            raise tof.serial.SerialException("gone")

        def close(self):
            self.closed = True

    monkeypatch.setattr(tof.serial, "Serial", FakeSerial)
    monkeypatch.setattr(tof, "REOPEN_INTERVAL", 0.01)
    link = ToFLink("/dev/tof", tmp_path / "b.json")
    link.open()
    deadline = time.monotonic() + 2
    while len(opened) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    link.close()
    assert len(opened) >= 2
    assert all(entry["dtr"] is False and entry["rts"] is False for entry in opened)
    assert link.error in ("busy", "gone") and link.serial is None


def test_frame_from_the_bench_capture_parses() -> None:
    # Shape of a real tof-bridge 1.0.0 line (values abbreviated to a valid 64-cell row).
    values = [353, 371, 405, 457, 520, 622, 743, 925] * 8
    raw = line(values, seq=227, millis=24670)
    frame = parse_frame(raw)
    assert frame is not None and frame.values[:8] == (353, 371, 405, 457, 520, 622, 743, 925)
    assert json.dumps(Baseline(frame.values, 0.0, 1).to_json())


def test_board_sensor_check_stops_only_a_forward_hold_and_reports_the_lease() -> None:
    from unittest.mock import Mock

    from tvpet_pi.hardware import Board
    from tvpet_pi.protocol import Stick
    from tvpet_pi.safety import Guard

    guard = Guard()
    board = Board("test-port", guard, None)
    board.serial = Mock()
    guard.wheels_verified = True
    guard.heartbeat()
    lease = guard.acquire()
    reason = [""]
    ended: list[tuple[str, str]] = []
    board.stop_checks.append(lambda: reason[0])
    board.on_safety_stop = lambda why, which: ended.append((why, which))
    guard.accept(Stick(target="wheels", x=0, y=1, lease=lease, sequence=1))
    board._sensor_stop()
    assert guard.lease == lease and not ended  # nothing to report
    reason[0] = REASON_LOW_OBSTACLE
    guard.accept(Stick(target="wheels", x=1, y=0, lease=lease, sequence=2))
    board._sensor_stop()
    assert guard.lease == lease  # turning in place is not forward motion
    guard.accept(Stick(target="wheels", x=0, y=-1, lease=lease, sequence=3))
    board._sensor_stop()
    assert guard.lease == lease  # backing away from the obstacle stays possible
    guard.accept(Stick(target="wheels", x=0.3, y=0.4, lease=lease, sequence=4))
    board._sensor_stop()
    assert guard.lease == "" and ended == [(REASON_LOW_OBSTACLE, lease)]
    assert board.status()["safety_stop"]["reason"] == REASON_LOW_OBSTACLE
    assert not guard.estopped  # an ordinary stop, like a lost link, not an emergency stop
    commands = [json.loads(call.args[0]) for call in board.serial.write.call_args_list]
    assert {"T": 1, "L": 0.0, "R": 0.0} in commands
    board._sensor_stop()  # no hold any more: nothing happens
    assert ended == [(REASON_LOW_OBSTACLE, lease)]
    board.on_safety_stop = Mock(side_effect=RuntimeError("log failed"))
    guard.wheels_verified = True  # status() re-read the (absent) evidence file
    lease = guard.acquire()
    guard.accept(Stick(target="wheels", x=0, y=1, lease=lease, sequence=1))
    board._sensor_stop()  # a failing listener never keeps the car moving
    assert guard.lease == ""


def test_agent_wires_the_tof_link_only_when_a_port_is_configured(tmp_path: Path) -> None:
    from tvpet_pi.agent import AgentRuntime

    runtime = AgentRuntime({})
    assert runtime.tof is None and runtime.board.stop_checks == []
    assert runtime.board.on_safety_stop == runtime.safety_stopped
    runtime = AgentRuntime(
        {
            "tof_port": "/dev/serial/by-id/usb-Espressif-if00",
            "tof_baseline": str(tmp_path / "b.json"),
            "tof_cliff_stop": False,
            "nav_dir": str(tmp_path / "nav"),
        }
    )
    assert runtime.tof is not None and runtime.board.stop_checks == [runtime.tof.stop_reason]
    assert runtime.tof.baseline_path == tmp_path / "b.json" and not runtime.tof.detector.cliffs
    default = AgentRuntime({"tof_port": "x"}).tof
    assert default is not None and default.detector.cliffs
    assert str(default.baseline_path).startswith(str(Path.home()))
    logged: list[tuple[str, str]] = []
    assert runtime.nav is not None and runtime.nav_stream is not None
    runtime.nav_stream.log = lambda level, text: logged.append((level, text))  # type: ignore[method-assign]
    runtime.nav.lease = "nav-lease-0000000"
    runtime.safety_stopped(REASON_LOW_OBSTACLE, "nav-lease-0000000")
    assert runtime.nav.fault is not None and runtime.nav.fault["reason"] == REASON_LOW_OBSTACLE
    assert logged[-1][0] == "WARN" and "低矮障碍" in logged[-1][1]
    runtime.nav.fault = None
    runtime.safety_stopped(REASON_CLIFF, "joystick-lease-00")
    assert runtime.nav.fault is None and "悬崖" in logged[-1][1]


def test_cli_calibrates_from_live_frames_and_watches(tmp_path: Path, monkeypatch, capsys) -> None:
    from tvpet_pi import tof_cli

    frame = floor_frame()

    class FakeLink(ToFLink):
        def open(self) -> None:
            self.reload_baseline()
            threading.Timer(0.02, lambda: self.feed(line(frame) * 5, time.monotonic())).start()

    monkeypatch.setattr(tof_cli, "ToFLink", FakeLink)
    path = tmp_path / "data" / "tof-baseline.json"
    args = ["--port", "/dev/tof", "--baseline", str(path)]
    assert tof_cli.main([*args, "calibrate", "--frames", "3", "--timeout", "2"]) == 0
    assert Baseline.load(path).values == tuple(frame)
    out = capsys.readouterr().out
    assert "32 floor cells" in out and "   -    -" in out and " 150  150" in out
    assert tof_cli.main([*args, "watch", "--seconds", "0.3"]) == 0
    out = capsys.readouterr().out
    assert "seq 7" in out and "baseline=True" in out and "reason=-" in out

    class SilentLink(ToFLink):
        def open(self) -> None:
            self.error = "could not open port"

    monkeypatch.setattr(tof_cli, "ToFLink", SilentLink)
    assert tof_cli.main([*args, "calibrate", "--timeout", "0.05"]) == 1
    assert "could not open port" in capsys.readouterr().err
