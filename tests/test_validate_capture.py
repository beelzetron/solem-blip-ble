"""Regression tests from live validate-solem-blip capture (2026-06-01).

Captured while program B (active_program=2, on-device name "Vasi") was running.
Status byte 8 uses 1-based slots (1=A, 2=B, 3=C); station 5 was the active zone.
"""

from __future__ import annotations

import json
from pathlib import Path

from solem_blip_ble import protocol

FIXTURE = (
    Path(__file__).parent / "fixtures" / "solem_validate_20260601_c8b961d44dcc8.jsonl"
)

CAPTURE_STATION_NAMES = {
    1: "Siepe",
    2: "Prato N",
    3: "Prato S",
    4: "Prato O",
    5: "Vasi",
    6: "Stazione 6",
}

CAPTURE_PROGRAMS = {
    0: {
        "name": "Siepe",
        "water_budget": 100,
        "cycle": 4,
        "week_days": 0x7F,
        "period_length": 2,
        "synchro_day": 1,
        "start_times": [1060, None, None, None, None, None, None, None],
        "station_durations": [1500, 0, 0, 0, 0, 0],
    },
    1: {
        "name": "Vasi",
        "water_budget": 100,
        "cycle": 4,
        "week_days": 0x7F,
        "period_length": 2,
        "synchro_day": 0,
        "start_times": [1080, None, None, None, None, None, None, None],
        "station_durations": [0, 0, 0, 0, 1800, 0],
    },
    2: {
        "name": "Prato",
        "water_budget": 100,
        "cycle": 4,
        "week_days": 0x11,
        "period_length": 3,
        "synchro_day": 0,
        "start_times": [270, None, None, None, None, None, None, None],
        "station_durations": [0, 1800, 1800, 1800, 0, 0],
    },
}


def load_capture_events(probe: str, direction: str = "RX") -> list[bytes]:
    payloads: list[bytes] = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        event = json.loads(line)
        if event["probe"] == probe and event["direction"] == direction:
            payloads.append(bytes.fromhex(event["payload_hex"]))
    return payloads


def assemble_station_names(
    payloads: list[bytes], *, max_station_num: int
) -> dict[int, str]:
    fragments: dict[int, dict[int, bytes]] = {}
    station_names: dict[int, str] = {}

    for payload in payloads:
        parsed = protocol.parse_station_name_fragment(payload)
        if parsed is None or not 1 <= parsed["station"] <= max_station_num:
            continue
        station = parsed["station"]
        fragments.setdefault(station, {})[parsed["sequence"]] = parsed["name_bytes"]
        if fragments[station].keys() >= {0, 1}:
            station_fragments = fragments[station]
            station_names[station] = (
                station_fragments[1] + station_fragments[0]
            ).decode("utf-8", errors="replace")

    return station_names


def load_irrigation_config_payloads() -> list[bytes]:
    """Return only 0x3a irrigation fragments from the config probe window."""
    return [
        payload
        for payload in load_capture_events("irrigation_config")
        if payload and payload[0] == 0x3A
    ]


def test_validate_capture_status_program_idle():
    """Program B (Vasi) reports active station 5 without status low bits set."""
    payloads = load_capture_events("status")
    parsed = [
        protocol.parse_status_notification(payload) for payload in payloads
    ]
    status = next(item for item in parsed if item is not None)

    assert status == {
        "controller_state": "On",
        "controller_off_mode": "on",
        "controller_off_days_remaining": 0,
        "is_watering": True,
        "station_num": 5,
        "remaining_seconds": None,
        "battery_voltage": 79,
        "battery_level": 4,
        "battery_low": False,
        "active_program": 2,
        "watering_origin": "program",
    }


def test_validate_capture_station_names():
    payloads = load_capture_events("output_names")
    names = assemble_station_names(payloads, max_station_num=6)
    assert names == CAPTURE_STATION_NAMES


def test_validate_capture_irrigation_programs():
    payloads = load_irrigation_config_payloads()
    programs = protocol.assemble_irrigation_programs(payloads, max_stations=6)

    assert set(programs) == {0, 1, 2}
    assert protocol.irrigation_config_complete(payloads)

    for program_index, expected in CAPTURE_PROGRAMS.items():
        program = programs[program_index]
        assert program["name"] == expected["name"]
        assert program["water_budget"] == expected["water_budget"]
        assert program["cycle"] == expected["cycle"]
        assert program["week_days"] == expected["week_days"]
        assert program["period_length"] == expected["period_length"]
        assert program["synchro_day"] == expected["synchro_day"]
        assert program["start_times"] == expected["start_times"]
        assert program["station_durations"] == expected["station_durations"]
