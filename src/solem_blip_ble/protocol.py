"""Command packing and notification parsing for Solem BL-IP.

Commands follow https://github.com/pcman75/solem-blip-reverse-engineering (write +
commit on 108b0002). Status notifications (notify 108b0003, seq 0x02) are from
live BL-IP testing documented in docs/ble_protocol.md in this repository.
"""

from __future__ import annotations

import struct
from typing import Any, TypedDict

from .const import (
    BATTERY_LEVELS_9V,
    BATTERY_VOLTAGE_ALERT_9V,
    MAX_STATION_NUM,
    MAX_TURN_OFF_DAYS,
)


class SolemStatus(TypedDict):
    controller_state: str
    is_watering: bool
    station_num: int | None
    remaining_seconds: int | None
    battery_voltage: int | None
    battery_level: int | None
    battery_low: bool


def pack_commit() -> bytes:
    return struct.pack(">BB", 0x3B, 0x00)


def pack_turn_on() -> bytes:
    return struct.pack(">HBBBH", 0x3105, 0xA0, 0x00, 0x01, 0x0000)


def pack_turn_off_permanent() -> bytes:
    return struct.pack(">HBBBH", 0x3105, 0xC0, 0x00, 0x00, 0x0000)


def pack_turn_off_x_days(days: int) -> bytes:
    days = max(0, min(days, MAX_TURN_OFF_DAYS))
    return struct.pack(">HBBBH", 0x3105, 0xC0, 0x00, days & 0xFF, 0x0000)


def _pack_v5_duration_command(opcode: int, station: int, seconds: int) -> bytes:
    """Pack a V5 duration command (3-byte big-endian duration in seconds)."""
    seconds = max(1, min(seconds, 0xFFFFFF))
    return bytes(
        [
            0x31,
            0x05,
            opcode & 0xFF,
            station & 0xFF,
            (seconds >> 16) & 0xFF,
            (seconds >> 8) & 0xFF,
            seconds & 0xFF,
        ]
    )


def pack_sprinkle_station(station: int, minutes: int) -> bytes:
    station = max(1, min(station, MAX_STATION_NUM))
    seconds = max(1, min(minutes, 240)) * 60
    return _pack_v5_duration_command(0x12, station, seconds)


def pack_sprinkle_all_stations(minutes: int) -> bytes:
    seconds = max(1, min(minutes, 240)) * 60
    return _pack_v5_duration_command(0x11, 0, seconds)


def pack_run_program(program: int) -> bytes:
    program = max(1, min(program, 3))
    return struct.pack(">HBBBH", 0x3105, 0x14, 0x00, program & 0xFF, 0x0000)


def pack_stop_manual_sprinkle() -> bytes:
    return struct.pack(">HBBBH", 0x3105, 0x15, 0x00, 0xFF, 0x0000)


def battery_level_9v(voltage: int) -> int:
    """Map raw 9 V battery voltage to icon level 0–5."""
    for level, threshold in enumerate(BATTERY_LEVELS_9V):
        if voltage < threshold:
            return level
    return len(BATTERY_LEVELS_9V)


def parse_battery_9v(data: bytes | bytearray) -> tuple[int | None, int | None, bool]:
    """Parse byte 10 battery voltage; returns (voltage, level, low_alert)."""
    if len(data) <= 10:
        return None, None, False
    voltage = data[10]
    if voltage == 0:
        return None, None, False
    level = battery_level_9v(voltage)
    return voltage, level, voltage < BATTERY_VOLTAGE_ALERT_9V


def parse_status_notification(
    data: bytes | bytearray,
    *,
    max_station_num: int = MAX_STATION_NUM,
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
        seconds = struct.unpack(">H", data[13:15])[0]
        if 0 < seconds <= 240 * 60:
            remaining_seconds = seconds

    battery_voltage, battery_level, battery_low = parse_battery_9v(data)

    return {
        "controller_state": "On" if is_on else "Off",
        "is_watering": is_watering,
        "station_num": station_num,
        "remaining_seconds": remaining_seconds,
        "battery_voltage": battery_voltage,
        "battery_level": battery_level,
        "battery_low": battery_low,
    }


def is_command_notification(data: bytes | bytearray) -> bool:
    """True for seq 0x00/0x01/0x02 command-response notifications."""
    return len(data) >= 3 and data[2] in (0x00, 0x01, 0x02)


def mock_status() -> dict[str, Any]:
    return {
        "controller_state": "Unknown",
        "is_watering": False,
        "station_num": None,
        "remaining_seconds": None,
        "battery_voltage": None,
        "battery_level": None,
        "battery_low": False,
    }


class FirmwareVersion(TypedDict):
    major: int
    minor: int
    raw_hex: str


def pack_get_firmware_version() -> bytes:
    """Pack identification command to query firmware version (CMD_ID=0x01)."""
    return struct.pack(">BBB", 0x01, 0x00, 0x00)


def parse_firmware_version_response(data: bytes | bytearray) -> FirmwareVersion | None:
    """Parse identification response (CMD_ID=0x01) to extract firmware version.

    Response format:
    - Byte 0: Command code (0x01)
    - Byte 1: Subcommand (0x00)
    - Byte 2: Response type (0x00 = identification data)
    - Bytes 3-8: MAC address
    - Byte 9-10: Hardware revision
    - Byte 11: Hardware type code
    - Byte 12: Firmware major version
    - Byte 13: Firmware minor version
    - Bytes 14-15: Serial number components

    Returns dict with major, minor, and raw_hex version string, or None if invalid.
    """
    if len(data) < 16 or data[0] != 0x01 or data[2] != 0x00:
        return None

    major = data[12]
    minor = data[13]
    raw_hex = f"{major}.{minor}"

    return {
        "major": major,
        "minor": minor,
        "raw_hex": raw_hex,
    }
