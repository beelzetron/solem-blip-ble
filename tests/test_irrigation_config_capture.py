"""Capture-based tests for V5 irrigation schedule/config parsing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from solem_blip_ble import protocol

FIXTURE = (
    Path(__file__).parent / "fixtures" / "solem_irrigation_config_c8b961d44dcc8.jsonl"
)

CAPTURE_PROGRAMS = {
    0: {
        "name": "Programma A",
        "water_budget": 100,
        "cycle": 4,
        "week_days": 0x7F,
        "period_length": 2,
        "start_times": [1060, None, None, None, None, None, None, None],
        "station_durations": [1200, 0, 0, 0, 1800, 0],
    },
    1: {
        "name": "Programma B",
        "water_budget": 100,
        "cycle": 4,
        "week_days": 0x7F,
        "period_length": 2,
        "start_times": [None] * 8,
        "station_durations": [0] * 6,
    },
    2: {
        "name": "Programma C",
        "water_budget": 100,
        "cycle": 4,
        "week_days": 0x11,
        "period_length": 3,
        "start_times": [270, None, None, None, None, None, None, None],
        "station_durations": [0, 1500, 1500, 1500, 0, 0],
    },
}


def load_capture_events(probe: str = "irrigation_config", direction: str = "RX") -> list[bytes]:
    payloads: list[bytes] = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        event = json.loads(line)
        if event.get("probe") == probe and event.get("direction") == direction:
            payloads.append(bytes.fromhex(event["payload_hex"]))
    return payloads


@pytest.fixture(scope="module")
def capture_payloads() -> list[bytes]:
    if not FIXTURE.is_file():
        pytest.skip(f"Capture fixture not found: {FIXTURE}")
    payloads = load_capture_events()
    if not payloads:
        pytest.skip(f"No irrigation_config RX events in {FIXTURE}")
    return payloads


def test_capture_fragments_parse(capture_payloads: list[bytes]):
    parsed = [
        protocol.parse_irrigation_config_fragment(payload)
        for payload in capture_payloads
    ]
    assert sum(fragment is not None for fragment in parsed) >= 21


def test_capture_assembles_three_programs(capture_payloads: list[bytes]):
    programs = protocol.assemble_irrigation_programs(capture_payloads, max_stations=6)
    assert set(programs) == {0, 1, 2}
    assert protocol.irrigation_config_complete(capture_payloads)


def test_capture_programs_match_hardware(capture_payloads: list[bytes]):
    programs = protocol.assemble_irrigation_programs(capture_payloads, max_stations=6)
    for program_index, expected in CAPTURE_PROGRAMS.items():
        program = programs[program_index]
        assert program["name"] == expected["name"]
        assert program["water_budget"] == expected["water_budget"]
        assert program["cycle"] == expected["cycle"]
        assert program["week_days"] == expected["week_days"]
        assert program["period_length"] == expected["period_length"]
        assert program["start_times"] == expected["start_times"]
        assert program["station_durations"] == expected["station_durations"]
