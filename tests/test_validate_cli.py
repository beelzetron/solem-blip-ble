"""Tests for the packaged validation CLI helpers."""

import pytest
from pathlib import Path

from solem_blip_ble.validate import build_parser
from solem_blip_ble.validate_common import (
    action_capture_writes,
    action_listen_dwell_seconds,
    capture_probes,
    describe_notification,
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
