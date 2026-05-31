"""Tests for the packaged validation CLI helpers."""

from pathlib import Path

from solem_blip_ble.validate import build_parser
from solem_blip_ble.validate_common import capture_probes, selected_sections


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
    capture_args = parser.parse_args(["--capture"])
    assert capture_args.capture == "auto"
    replay_args = parser.parse_args(
        ["--replay", "capture.jsonl", "--only", "names"]
    )
    assert replay_args.replay == [Path("capture.jsonl")]
    assert replay_args.only == ["names"]
