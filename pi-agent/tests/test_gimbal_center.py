"""Pan/tilt servo zero calibration: pure checks plus a mocked UART run."""

import json
from typing import Any
from unittest.mock import Mock

import pytest
from tvpet_pi import gimbal_center
from tvpet_pi.gimbal_center import center, center_command, check_centered


def test_center_command_targets_the_factory_servo_ids() -> None:
    assert center_command("pan") == {"T": 502, "id": 2}
    assert center_command("tilt") == {"T": 502, "id": 1}
    with pytest.raises(KeyError):
        center_command("roll")


def test_check_centered_compares_feedback_against_tolerance() -> None:
    before = {"pan": -44.3, "tilt": 1.8}
    result = check_centered("pan", before, {"pan": 0.4, "tilt": 1.8})
    assert result == {
        "axis": "pan",
        "servo_id": 2,
        "before": -44.3,
        "after": 0.4,
        "centered": True,
    }
    assert check_centered("pan", before, {"pan": 5.0, "tilt": 1.8})["centered"] is False
    assert check_centered("tilt", before, {"pan": 0.0, "tilt": -1.5})["centered"] is True
    with pytest.raises(ValueError):
        check_centered("pan", {"tilt": 0.0}, {"pan": 0.0, "tilt": 0.0})
    with pytest.raises(ValueError):
        check_centered("pan", before, {"pan": float("nan"), "tilt": 0.0})


def test_center_writes_stop_feedback_then_set_middle(monkeypatch) -> None:
    uart = Mock()
    uart.__enter__ = Mock(return_value=uart)
    uart.__exit__ = Mock(return_value=False)
    pan = {"value": -44.0}

    def readline(_size: int = 0) -> bytes:
        if any(json.loads(c.args[0]).get("T") == 502 for c in uart.write.call_args_list):
            pan["value"] = 0.3
        return (
            json.dumps(
                {"T": 1001, "L": 0, "R": 0, "v": 12.1, "pan": pan["value"], "tilt": 2.0}
            ).encode()
            + b"\n"
        )

    uart.readline.side_effect = readline
    monkeypatch.setattr(gimbal_center.serial, "Serial", Mock(return_value=uart))
    monkeypatch.setattr(gimbal_center.time, "sleep", lambda _s: None)

    result = center("test-uart", "pan", settle=0.0, window=0.0)

    writes: list[dict[str, Any]] = [json.loads(c.args[0]) for c in uart.write.call_args_list]
    assert writes[0] == {"T": 1, "L": 0.0, "R": 0.0}
    assert {"T": 4, "cmd": 2} in writes
    assert writes.index({"T": 502, "id": 2}) > writes.index({"T": 4, "cmd": 2})
    assert result["before"] == -44.0 and result["after"] == 0.3 and result["centered"]
    assert result["port"] == "test-uart"
    gimbal_center.serial.Serial.assert_called_once_with(
        "test-uart", 115200, timeout=0.02, write_timeout=0.1, exclusive=True
    )


def test_center_refuses_without_fresh_feedback(monkeypatch) -> None:
    uart = Mock()
    uart.__enter__ = Mock(return_value=uart)
    uart.__exit__ = Mock(return_value=False)
    uart.readline.return_value = b'{"T":1001,"L":0,"R":0,"v":12.1}\n'
    monkeypatch.setattr(gimbal_center.serial, "Serial", Mock(return_value=uart))
    monkeypatch.setattr(gimbal_center.time, "sleep", lambda _s: None)
    with pytest.raises(RuntimeError):
        center("test-uart", "pan", settle=0.0, window=0.0)
    assert not any(json.loads(c.args[0]).get("T") == 502 for c in uart.write.call_args_list)


def test_main_requires_the_operator_confirmation(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.argv", ["tvpet-gimbal-center"])
    with pytest.raises(SystemExit):
        gimbal_center.main()
    monkeypatch.setattr("sys.argv", ["tvpet-gimbal-center", "--camera-facing-front"])
    monkeypatch.setattr(
        gimbal_center,
        "center",
        lambda port, axis, **_: {"port": port, "axis": axis, "centered": True},
    )
    gimbal_center.main()
    assert json.loads(capsys.readouterr().out) == {
        "port": "/dev/ttyAMA0",
        "axis": "pan",
        "centered": True,
    }
