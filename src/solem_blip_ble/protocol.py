"""Command packing and notification parsing for Solem BL-IP.

Commands follow https://github.com/pcman75/solem-blip-reverse-engineering (write +
commit on 108b0002). Status notifications (notify 108b0003, seq 0x02) are from
live BL-IP testing documented in docs/ble_protocol.md in this repository.
"""

from __future__ import annotations

import struct
from typing import Any, TypedDict

from .const import MAX_TURN_OFF_DAYS


class SolemStatus(TypedDict):
    controller_state: str
    is_watering: bool
    station_num: int | None
    remaining_seconds: int | None


def pack_commit() -> bytes:
    return struct.pack(">BB", 0x3B, 0x00)


def pack_turn_on() -> bytes:
    return struct.pack(">HBBBH", 0x3105, 0xA0, 0x00, 0x01, 0x0000)


def pack_turn_off_permanent() -> bytes:
    return struct.pack(">HBBBH", 0x3105, 0xC0, 0x00, 0x00, 0x0000)


def pack_turn_off_x_days(days: int) -> bytes:
    days = max(0, min(days, MAX_TURN_OFF_DAYS))
    return struct.pack(">HBBBH", 0x3105, 0xC0, 0x00, days & 0xFF, 0x0000)


def pack_sprinkle_station(station: int, minutes: int) -> bytes:
    station = max(1, min(station, 16))
    seconds = max(1, min(minutes, 240)) * 60
    return struct.pack(">HBBBH", 0x3105, 0x12, station & 0xFF, 0x00, seconds & 0xFFFF)


def pack_sprinkle_all_stations(minutes: int) -> bytes:
    seconds = max(1, min(minutes, 240)) * 60
    return struct.pack(">HBBBH", 0x3105, 0x11, 0x00, 0x00, seconds & 0xFFFF)


def pack_run_program(program: int) -> bytes:
    program = max(1, min(program, 3))
    return struct.pack(">HBBBH", 0x3105, 0x14, 0x00, program & 0xFF, 0x0000)


def pack_stop_manual_sprinkle() -> bytes:
    return struct.pack(">HBBBH", 0x3105, 0x15, 0x00, 0xFF, 0x0000)


def parse_status_notification(
    data: bytes | bytearray,
    *,
    max_station_num: int = 6,
) -> SolemStatus | None:
    """Parse seq=0x02 status notification; return None if not a status frame."""
    if len(data) < 18 or data[2] != 0x02 or data[3] == 0x10:
        return None

    status_byte = data[3]
    is_on = bool(status_byte & 0x40)
    is_watering = bool(status_byte & 0x02)
    station_num = data[9] if 1 <= data[9] <= max_station_num else None

    remaining_seconds = None
    if is_watering:
        seconds = struct.unpack(">H", data[14:16])[0]
        if 0 < seconds <= 240 * 60:
            remaining_seconds = seconds

    return {
        "controller_state": "On" if is_on else "Off",
        "is_watering": is_watering,
        "station_num": station_num,
        "remaining_seconds": remaining_seconds,
    }


def mock_status() -> dict[str, Any]:
    return {
        "controller_state": "Unknown",
        "is_watering": False,
        "station_num": None,
        "remaining_seconds": None,
    }
