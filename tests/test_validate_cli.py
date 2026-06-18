"""Tests for the packaged validation CLI helpers."""

import json
import pytest
from pathlib import Path

from solem_blip_ble.validate import build_parser
from solem_blip_ble.validate_common import (
    action_capture_writes,
    action_listen_dwell_seconds,
    capture_probes,
    describe_notification,
    load_capture_events,
    off_days_capture_writes,
    schedule_write_capture_writes,
    selected_sections,
)
from solem_blip_ble import protocol

TEST_MAC = "AA:BB:CC:DD:EE:FF"


def test_build_parser_requires_mac():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
    args = parser.parse_args([TEST_MAC])
    assert args.mac == TEST_MAC


def test_selected_sections_defaults_to_all_reads():
    assert selected_sections(None) == (
        "status",
        "firmware",
        "names",
        "schedule",
        "gatt",
    )


def test_selected_sections_honours_only():
    assert selected_sections(["firmware", "schedule"]) == ("firmware", "schedule")


def test_capture_probes_for_schedule_only():
    probes = capture_probes(("schedule",))
    assert probes == [("irrigation_config", bytes.fromhex("3900"))]


def test_capture_probes_for_full_read_set():
    probes = capture_probes(selected_sections(None))
    assert [name for name, _payload in probes] == [
        "status",
        "firmware",
        "output_names",
        "irrigation_config",
    ]


def test_load_capture_events_can_include_tx_and_rx(tmp_path):
    path = tmp_path / "capture.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"direction": "TX", "probe": "p", "payload_hex": "3900"}),
                json.dumps({"direction": "RX", "probe": "p", "payload_hex": "3a00"}),
            ]
        ),
        encoding="utf-8",
    )

    events, used_paths = load_capture_events([path], direction=None)

    assert used_paths == [path]
    assert [event["direction"] for event in events] == ["TX", "RX"]


def test_build_parser_capture_and_replay():
    parser = build_parser()
    capture_args = parser.parse_args([TEST_MAC, "--capture"])
    assert capture_args.capture == "auto"
    replay_args = parser.parse_args(
        [TEST_MAC, "--replay", "capture.jsonl", "--only", "names"]
    )
    assert replay_args.replay == [Path("capture.jsonl")]
    assert replay_args.only == ["names"]


def test_action_capture_writes_program_a():
    writes = action_capture_writes(run_program=1, minutes=1)
    probe_names = [name for name, _payload in writes]
    assert probe_names == [
        "stop_manual_command",
        "stop_manual_commit",
        "run_program_a_command",
        "run_program_a_commit",
        "stop_after_command",
        "stop_after_commit",
    ]
    assert writes[2][1] == protocol.pack_run_program(1)


def test_action_capture_writes_station_sprinkle():
    writes = action_capture_writes(station=2, minutes=3)
    assert writes[2] == ("sprinkle_station_2_command", protocol.pack_sprinkle_station(2, 3))


def test_off_days_capture_writes():
    writes = off_days_capture_writes(3)
    assert writes == [
        ("turn_on_before_off_days_command", protocol.pack_turn_on()),
        ("turn_on_before_off_days_commit", protocol.pack_commit()),
        ("turn_off_days_3_command", protocol.pack_turn_off_x_days(3)),
        ("turn_off_days_3_commit", protocol.pack_commit()),
        ("turn_on_after_off_days_command", protocol.pack_turn_on()),
        ("turn_on_after_off_days_commit", protocol.pack_commit()),
    ]


@pytest.mark.parametrize("days", [0, 16])
def test_off_days_capture_writes_rejects_out_of_range(days):
    with pytest.raises(ValueError):
        off_days_capture_writes(days)


def test_schedule_write_capture_writes():
    expected, writes = schedule_write_capture_writes(
        program_index=1,
        program_name="Test",
        station=2,
        duration_seconds=90,
        start_minutes=360,
        max_stations=6,
    )

    assert expected["name"] == "Test"
    assert expected["start_times"] == [360, None, None, None, None, None, None, None]
    assert expected["station_durations"] == [0, 90, 0, 0, 0, 0]
    assert [name for name, _payload in writes] == [
        "set_program_b_frame_0",
        "set_program_b_frame_1",
        "set_program_b_frame_2",
        "set_program_b_frame_3",
        "set_program_b_frame_4",
        "set_program_b_frame_5",
        "set_program_b_frame_6",
        "readback_irrigation_config",
    ]
    assert writes[:-1] == [
        (f"set_program_b_frame_{index}", payload)
        for index, payload in enumerate(
            protocol.pack_set_irrigation_program(1, expected, max_stations=6)
        )
    ]
    assert writes[-1] == ("readback_irrigation_config", protocol.pack_get_irrigation_config())


def test_action_listen_dwell_after_start_commit():
    assert action_listen_dwell_seconds(
        "run_program_a_commit", minutes=1, capture_seconds=2.0
    ) == 65.0
    assert action_listen_dwell_seconds(
        "stop_manual_commit", minutes=1, capture_seconds=2.0
    ) == 2.0


def test_describe_notification_includes_active_program():
    data = bytearray(18)
    data[2] = 0x02
    data[3] = 0x42
    data[8] = 1
    data[9] = 1
    data[13] = 0x00
    data[14] = 0x3C
    note = describe_notification(bytes(data))
    assert "program=1" in note
    assert "watering=True" in note


def test_describe_notification_includes_off_days():
    data = bytearray(18)
    data[2] = 0x02
    data[3] = 0x00
    data[4] = 0x03
    note = describe_notification(bytes(data))
    assert "off_mode=temporary" in note
    assert "off_days=3" in note


def test_describe_notification_treats_schedule_write_echo_as_config():
    note = describe_notification(bytes.fromhex("38110211000000000000000000000000000078"))
    assert "config" in note
    assert "status" not in note


def test_build_parser_run_program_requires_actions():
    parser = build_parser()
    args = parser.parse_args(
        [TEST_MAC, "--actions", "--run-program", "1", "--minutes", "1"]
    )
    assert args.run_program == 1
    assert args.actions is True
    capture_args = parser.parse_args(
        [TEST_MAC, "--capture", "--actions", "--run-program", "1"]
    )
    assert capture_args.capture == "auto"
    assert capture_args.actions is True
    assert capture_args.run_program == 1


def test_build_parser_capture_off_days():
    parser = build_parser()
    args = parser.parse_args([TEST_MAC, "--capture-off-days", "3"])
    assert args.capture_off_days == 3
    assert args.capture is None


def test_build_parser_capture_write_schedule():
    parser = build_parser()
    args = parser.parse_args(
        [
            TEST_MAC,
            "--capture",
            "--write-schedule",
            "2",
            "--write-schedule-name",
            "Vasi",
            "--write-schedule-start",
            "06:30",
            "--write-schedule-station",
            "5",
            "--write-schedule-duration",
            "120",
            "--write-schedule-reconnect-seconds",
            "4",
            "--write-schedule-frame-attempts",
            "5",
        ]
    )
    assert args.capture == "auto"
    assert args.write_schedule_program == 2
    assert args.write_schedule_name == "Vasi"
    assert args.write_schedule_start == 390
    assert args.write_schedule_station == 5
    assert args.write_schedule_duration == 120
    assert args.write_schedule_reconnect_seconds == 4.0
    assert args.write_schedule_frame_attempts == 5


def test_build_parser_replay_schedule_write():
    parser = build_parser()
    args = parser.parse_args(
        [TEST_MAC, "--replay", "capture.jsonl", "--only", "schedule_write"]
    )
    assert args.replay == [Path("capture.jsonl")]
    assert args.only == ["schedule_write"]


@pytest.mark.parametrize("days", ["0", "16"])
def test_build_parser_capture_off_days_rejects_out_of_range(days):
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([TEST_MAC, "--capture-off-days", days])
