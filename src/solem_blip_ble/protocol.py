"""Command packing and notification parsing for Solem BL-IP.

Commands follow https://github.com/pcman75/solem-blip-reverse-engineering (write +
commit on 108b0002). Status notifications (notify 108b0003, seq 0x02) are from
live BL-IP testing documented in docs/ble_protocol.md in this repository.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, TypedDict

from .const import (
    BATTERY_LEVELS_9V,
    BATTERY_VOLTAGE_ALERT_9V,
    MAX_REMAINING_SECONDS,
    MAX_STATION_NUM,
    MAX_TURN_OFF_DAYS,
)


# Status byte 3 low bits describe the run origin/mode. Captures show byte 9
# (current station) is the authoritative active-valve indicator.
WATERING_STATUS_MASK = 0x06


def is_watering_status(status_byte: int) -> bool:
    """Return True when seq=0x02 status byte has manual/program activity bits."""
    return bool(status_byte & WATERING_STATUS_MASK)


class SolemStatus(TypedDict):
    controller_state: str
    controller_off_mode: str
    controller_off_days_remaining: int | None
    is_watering: bool
    station_num: int | None
    remaining_seconds: int | None
    battery_voltage: int | None
    battery_level: int | None
    battery_low: bool
    active_program: int | None
    watering_origin: str | None


def pack_commit() -> bytes:
    return struct.pack(">BB", 0x3B, 0x00)


def pack_turn_on() -> bytes:
    return struct.pack(">HBBBH", 0x3105, 0xA0, 0x00, 0x00, 0x0000)


def pack_turn_off_permanent() -> bytes:
    return struct.pack(">HBBBH", 0x3105, 0xC0, 0x00, 0x00, 0x0000)


def pack_turn_off_x_days(days: int) -> bytes:
    if not 0 <= days <= MAX_TURN_OFF_DAYS:
        raise ValueError(f"days must be between 0 and {MAX_TURN_OFF_DAYS}")
    return struct.pack(">HBBBH", 0x3105, 0xC0, 0x00, days & 0xFF, 0x0000)


def _pack_v5_duration_command(opcode: int, station: int, seconds: int) -> bytes:
    """Pack a V5 duration command (3-byte big-endian duration in seconds)."""
    if not 1 <= seconds <= 0xFFFFFF:
        raise ValueError("seconds must be between 1 and 16777215")
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
    if not 1 <= station <= MAX_STATION_NUM:
        raise ValueError(f"station must be between 1 and {MAX_STATION_NUM}")
    if not 1 <= minutes <= 240:
        raise ValueError("minutes must be between 1 and 240")
    seconds = minutes * 60
    return _pack_v5_duration_command(0x12, station, seconds)


def pack_sprinkle_all_stations(minutes: int) -> bytes:
    if not 1 <= minutes <= 240:
        raise ValueError("minutes must be between 1 and 240")
    seconds = minutes * 60
    return _pack_v5_duration_command(0x11, 0, seconds)


def pack_run_program(program: int) -> bytes:
    if not 1 <= program <= 3:
        raise ValueError("program must be between 1 and 3")
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


def _parse_int3(b0: int, b1: int, b2: int) -> int:
    """Parse a 3-byte big-endian duration."""
    return ((b0 & 0x0F) << 16) | ((b1 & 0xFF) << 8) | (b2 & 0xFF)


def _valid_remaining(seconds: int) -> int | None:
    if 0 < seconds <= MAX_REMAINING_SECONDS:
        return seconds
    return None


def parse_remaining_seconds(
    data: bytes | bytearray, station_num: int | None
) -> int | None:
    """Extract remaining sprinkle seconds from a seq=0x02 status notification.

    Stations 1–2 use a 3-byte slot at bytes 12–14 (station 1) or 15–17
    (station 2). The same 12–14 slot also carries the active duration for higher
    stations on many controllers. Fall back to the legacy 2-byte field at bytes
    13–14 validated on station 1 HCI captures.
    """
    if station_num is None or len(data) < 15:
        return None

    candidates: list[int] = []

    if len(data) >= 15:
        candidates.append(_parse_int3(data[12], data[13], data[14]))

    if station_num == 2 and len(data) >= 18:
        candidates.append(_parse_int3(data[15], data[16], data[17]))

    if len(data) >= 15:
        candidates.append(struct.unpack(">H", data[13:15])[0])

    for seconds in candidates:
        if remaining := _valid_remaining(seconds):
            return remaining
    return None


def parse_watering_origin(status_byte: int) -> str | None:
    """Return manual vs schedule origin from status byte low nibble when watering."""
    origin = status_byte & 0x0F
    if 0 < origin < 5:
        return "manual"
    if origin != 0:
        return "schedule"
    return None


def parse_active_program(
    data: bytes | bytearray, *, is_controller_on: bool
) -> int | None:
    """Parse active program index (1=A … 3=C) from status byte 8.

    Byte 8 remains set while a program run is in progress, including
    inter-station delays when the controller is ON but not actively watering.
    """
    if not is_controller_on or len(data) <= 8:
        return None
    program = data[8]
    if 1 <= program <= IRRIGATION_PROGRAM_COUNT:
        return program
    return None


def parse_controller_off_state(
    data: bytes | bytearray, *, is_controller_on: bool
) -> tuple[str, int]:
    """Parse V5 controller temporary/permanent off state from status byte 4."""
    if is_controller_on:
        return "on", 0
    off_days = data[4] & 0x3F
    if 1 <= off_days <= MAX_TURN_OFF_DAYS:
        return "temporary", off_days
    return "permanent", 0


def parse_intermediate_remaining(
    data: bytes | bytearray,
    station_num: int,
    *,
    max_station_num: int = MAX_STATION_NUM,
) -> int | None:
    """Parse remaining seconds from a seq=0x01 notification (stations 3+).

    Seq=0x01 carries remaining-time slots for stations 3 and higher.
    """
    if len(data) < 12 or data[2] != 0x01 or not (3 <= station_num <= max_station_num):
        return None
    offset = (station_num - 3) * 3 + 3
    if offset + 2 >= len(data):
        return None
    return _valid_remaining(
        _parse_int3(data[offset], data[offset + 1], data[offset + 2])
    )


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
    controller_off_mode, controller_off_days_remaining = parse_controller_off_state(
        data, is_controller_on=is_on
    )
    has_activity_bits = is_watering_status(status_byte)
    station_num = data[9] if 1 <= data[9] <= max_station_num else None
    is_watering = station_num is not None or has_activity_bits

    remaining_seconds = None
    if is_watering:
        remaining_seconds = parse_remaining_seconds(data, station_num)

    battery_voltage, battery_level, battery_low = parse_battery_9v(data)
    active_program = parse_active_program(data, is_controller_on=is_on)

    watering_origin: str | None
    if is_watering or active_program is not None:
        if active_program is not None or bool(status_byte & 0x04):
            watering_origin = "program"
        else:
            watering_origin = parse_watering_origin(status_byte)
    else:
        watering_origin = None

    return {
        "controller_state": "On" if is_on else "Off",
        "controller_off_mode": controller_off_mode,
        "controller_off_days_remaining": controller_off_days_remaining,
        "is_watering": is_watering,
        "station_num": station_num,
        "remaining_seconds": remaining_seconds,
        "battery_voltage": battery_voltage,
        "battery_level": battery_level,
        "battery_low": battery_low,
        "active_program": active_program,
        "watering_origin": watering_origin,
    }


def is_command_notification(data: bytes | bytearray) -> bool:
    """True for seq 0x00/0x01/0x02 command-response notifications."""
    return len(data) >= 3 and data[2] in (0x00, 0x01, 0x02)


def mock_status() -> dict[str, Any]:
    return {
        "controller_state": "Unknown",
        "controller_off_mode": "unknown",
        "controller_off_days_remaining": None,
        "is_watering": False,
        "station_num": None,
        "remaining_seconds": None,
        "battery_voltage": None,
        "battery_level": None,
        "battery_low": False,
        "active_program": None,
        "watering_origin": None,
    }


class FirmwareVersion(TypedDict):
    major: int
    minor: int
    patch: int
    raw_hex: str


class StationNameFragment(TypedDict):
    station: int
    sequence: int
    name_bytes: bytes


def pack_get_station_names() -> bytes:
    """Pack a V5 request for all output names."""
    return bytes([0x35, 0x00])


def parse_station_name_fragment(
    data: bytes | bytearray,
) -> StationNameFragment | None:
    """Parse one V5 output-name response fragment.

    Names are returned as two notifications per output. Each notification
    contains up to 16 UTF-8 bytes at offsets 4-19. Hardware uses response
    type 0x36 (0x35 in synthetic fixtures) with byte 1 = 0x12; the fragment
    index is the least significant bit of byte 2.
    """
    if (
        len(data) < 20
        or data[0] not in (0x35, 0x36)
        or data[1] != 0x12
    ):
        return None

    name_bytes = bytes(data[4:20]).split(b"\x00", 1)[0]
    return {
        "station": data[3] + 1,
        "sequence": data[2] & 1,
        "name_bytes": name_bytes,
    }


def pack_get_firmware_version() -> bytes:
    """Pack the V5 identification command used to query firmware version."""
    return bytes([0x0F, 0x00])


def pack_set_time(when: datetime | None = None) -> bytes:
    """Pack the V5 set-time command.

    Frame: ``03 06 00 YY MM DD hh mm ss`` where ``YY`` is year minus 1900 and
    month is ``1-12``. Does **not** use the ``3b00`` commit suffix.
    """
    moment = when or datetime.now().astimezone()
    return bytes(
        [
            0x03,
            0x06,
            0x00,
            (moment.year - 1900) & 0xFF,
            moment.month & 0xFF,
            moment.day & 0xFF,
            moment.hour & 0xFF,
            moment.minute & 0xFF,
            moment.second & 0xFF,
        ]
    )


def parse_firmware_version_response(data: bytes | bytearray) -> FirmwareVersion | None:
    """Parse a V5 identification response to extract firmware version.

    Accepts bare identification frames (command at byte 0) and wrapped
    notifications (0x10 prefix, command at byte 1). Version is always at
    bytes 12-14.

    Returns dict with major, minor, and raw_hex version string, or None if invalid.
    """
    if len(data) < 17:
        return None

    if data[0] == 0x0F and data[2] == 0x01:
        pass
    elif data[0] == 0x10 and data[1] == 0x0F and data[2] == 0x01:
        pass
    else:
        return None

    major = data[12]
    minor = data[13]
    patch = data[14]
    raw_hex = f"{major}.{minor}.{patch}"

    return {
        "major": major,
        "minor": minor,
        "patch": patch,
        "raw_hex": raw_hex,
    }


IRRIGATION_PROGRAM_CLASS = 0x1
IRRIGATION_PROGRAM_COUNT = 3
IRRIGATION_LOGICAL_CHUNKS = 7
IRRIGATION_CHUNK_MIN_LENGTHS = (20, 20, 11, 20, 19, 19, 10)
DISABLED_START_TIME = 1440
MAX_PROGRAM_NAME_BYTES = 31
MAX_START_TIMES = 8
MAX_PROGRAM_STATIONS = 12


class IrrigationProgram(TypedDict):
    name: str
    inter_station_delay: int
    water_budget: int
    cycle: int
    week_days: int
    period_length: int
    synchro_day: int
    period_start_date: date | None
    start_times: list[int | None]
    station_durations: list[int]


class IrrigationConfigFragment(TypedDict):
    program_index: int
    fragment_id: int
    logical_chunk: int


def _validate_u16(value: int, field: str) -> None:
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"{field} must be between 0 and 65535")


def _validate_u8(value: int, field: str) -> None:
    if not 0 <= value <= 0xFF:
        raise ValueError(f"{field} must be between 0 and 255")


def _validated_period_date(program: IrrigationProgram, today: date | None) -> date:
    period_start_date = program.get("period_start_date")
    if period_start_date is not None:
        return period_start_date
    return today or date.today()


def _normalized_start_times(program: IrrigationProgram) -> list[int]:
    start_times = list(program["start_times"])
    if len(start_times) > MAX_START_TIMES:
        raise ValueError("start_times must contain at most 8 entries")
    start_times.extend([None] * (MAX_START_TIMES - len(start_times)))

    normalized: list[int] = []
    for minutes in start_times:
        if minutes is None:
            normalized.append(DISABLED_START_TIME)
            continue
        if not 0 <= minutes < DISABLED_START_TIME:
            raise ValueError("start time minutes must be between 0 and 1439")
        normalized.append(minutes)
    return normalized


def _normalized_station_durations(
    program: IrrigationProgram, *, max_stations: int
) -> list[int]:
    if not 1 <= max_stations <= MAX_PROGRAM_STATIONS:
        raise ValueError("max_stations must be between 1 and 12")
    durations = list(program["station_durations"])
    if len(durations) > max_stations:
        raise ValueError(f"station_durations must contain at most {max_stations} entries")
    durations.extend([0] * (MAX_PROGRAM_STATIONS - len(durations)))

    normalized: list[int] = []
    for seconds in durations:
        if not 0 <= seconds <= 0xFFFFFF:
            raise ValueError("station durations must be between 0 and 16777215 seconds")
        normalized.append(seconds)
    return normalized


def normalize_irrigation_program_for_write(
    program: IrrigationProgram,
    *,
    today: date | None = None,
    max_stations: int = MAX_STATION_NUM,
) -> IrrigationProgram:
    """Return the program shape expected after a successful inferred V5 write."""
    _validate_u16(program["inter_station_delay"], "inter_station_delay")
    _validate_u16(program["water_budget"], "water_budget")
    _validate_u8(program["cycle"], "cycle")
    _validate_u8(program["week_days"], "week_days")
    _validate_u8(program["period_length"], "period_length")
    _validate_u8(program["synchro_day"], "synchro_day")

    name = program["name"].encode("utf-8")[:MAX_PROGRAM_NAME_BYTES].decode(
        "utf-8",
        errors="replace",
    )
    start_times = [
        None if minutes == DISABLED_START_TIME else minutes
        for minutes in _normalized_start_times(program)
    ]
    station_durations = _normalized_station_durations(
        program, max_stations=max_stations
    )[:max_stations]

    return {
        "name": name,
        "inter_station_delay": program["inter_station_delay"],
        "water_budget": program["water_budget"],
        "cycle": program["cycle"],
        "week_days": program["week_days"],
        "period_length": program["period_length"],
        "synchro_day": program["synchro_day"],
        "period_start_date": _validated_period_date(program, today),
        "start_times": start_times,
        "station_durations": station_durations,
    }


def pack_set_irrigation_program(
    program_index: int,
    program: IrrigationProgram,
    *,
    today: date | None = None,
    max_stations: int = MAX_STATION_NUM,
) -> list[bytes]:
    """Pack inferred V5 frames for writing one persisted irrigation program.

    This is the capture-inferred configuration path, not the manual-command
    path, so callers must not append the manual ``3b00`` commit frame unless
    hardware validation later proves it is required.
    """
    if not 0 <= program_index < IRRIGATION_PROGRAM_COUNT:
        raise ValueError("program_index must be between 0 and 2")

    normalized = normalize_irrigation_program_for_write(
        program,
        today=today,
        max_stations=max_stations,
    )

    key = 0x10 | (program_index & 0x0F)
    name = normalized["name"].encode("utf-8")[:MAX_PROGRAM_NAME_BYTES]
    name = name.ljust(MAX_PROGRAM_NAME_BYTES, b"\x00")
    period_start_date = normalized["period_start_date"]
    assert period_start_date is not None
    start_times = [
        DISABLED_START_TIME if minutes is None else minutes
        for minutes in normalized["start_times"]
    ]
    station_durations = normalized["station_durations"] + [0] * (
        MAX_PROGRAM_STATIONS - len(normalized["station_durations"])
    )

    frames: list[bytes] = [
        bytes([0x2F, 0x12, 0x00, key, *name[:16]]),
        bytes([0x2F, 0x12, 0x01, key, *name[16:31], 0x00]),
        bytes(
            [
                0x37,
                0x0E,
                0x00,
                key,
                (normalized["inter_station_delay"] >> 8) & 0xFF,
                normalized["inter_station_delay"] & 0xFF,
                (normalized["water_budget"] >> 8) & 0xFF,
                normalized["water_budget"] & 0xFF,
                normalized["cycle"] & 0xFF,
                normalized["week_days"] & 0xFF,
                normalized["period_length"] & 0xFF,
                normalized["synchro_day"] & 0xFF,
                period_start_date.day,
                period_start_date.month,
                (period_start_date.year >> 8) & 0xFF,
                period_start_date.year & 0xFF,
            ]
        ),
    ]

    start_frame = bytearray([0x37, 0x12, 0x01, key, *([0] * 16)])
    for slot, minutes in enumerate(start_times):
        offset = 4 + slot * 2
        start_frame[offset] = (minutes >> 8) & 0xFF
        start_frame[offset + 1] = minutes & 0xFF
    frames.append(bytes(start_frame))

    for chunk_id, station_range, frame_len in (
        (0x02, range(0, 5), 19),
        (0x03, range(5, 10), 19),
        (0x04, range(10, 12), 10),
    ):
        frame = bytearray([0x37, frame_len - 2, chunk_id, key, *([0] * (frame_len - 4))])
        for slot, station_index in enumerate(station_range):
            seconds = station_durations[station_index]
            offset = 4 + slot * 3
            frame[offset] = (seconds >> 16) & 0xFF
            frame[offset + 1] = (seconds >> 8) & 0xFF
            frame[offset + 2] = seconds & 0xFF
        frames.append(bytes(frame))

    return frames


def pack_get_irrigation_config() -> bytes:
    """Pack a V5 request for persisted irrigation program configuration."""
    return bytes([0x39, 0x00])


def normalize_config_notification(data: bytes | bytearray) -> bytes | None:
    """Normalize a V5 irrigation-config notification to canonical layout.

    Accepts bare read/write config frames (command at byte 0), response-type
    offsets (0x30, 0x38, 0x3a), and hardware-wrapped frames (0x10 prefix).
    """
    if len(data) < 4:
        return None

    if data[0] in (0x2F, 0x37, 0x39, 0x3A):
        return bytes(data)
    if data[0] == 0x30:
        return bytes([0x2F, *data[1:]])
    if data[0] == 0x38:
        return bytes([0x37, *data[1:]])
    if data[0] == 0x10 and len(data) >= 5 and data[1] in (0x2F, 0x37, 0x39, 0x3A):
        return bytes(data[1:])
    if data[0] == 0x10 and len(data) >= 5 and data[1] == 0x30:
        return bytes([0x2F, *data[2:]])
    if data[0] == 0x10 and len(data) >= 5 and data[1] == 0x38:
        return bytes([0x37, *data[2:]])
    return None


def _parse_config_int3(b0: int, b1: int, b2: int) -> int:
    return (b0 << 16) | (b1 << 8) | b2


def parse_period_start_date(normalized: bytes | bytearray) -> date | None:
    """Parse program period start date from header bytes 12-15 (day, month, year BE)."""
    if len(normalized) < 16:
        return None
    day = normalized[12]
    month = normalized[13]
    year = struct.unpack(">H", normalized[14:16])[0]
    if not (2000 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31):
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _empty_irrigation_program(*, max_stations: int) -> IrrigationProgram:
    return {
        "name": "",
        "inter_station_delay": 0,
        "water_budget": 0,
        "cycle": 0,
        "week_days": 0,
        "period_length": 0,
        "synchro_day": 0,
        "period_start_date": None,
        "start_times": [None] * 8,
        "station_durations": [0] * max_stations,
    }


def parse_irrigation_config_fragment(
    data: bytes | bytearray,
    *,
    first_fragment_id: int | None = None,
) -> IrrigationConfigFragment | None:
    """Parse one V5 irrigation-config notification fragment.

    Returns fragment metadata for irrigation programs (class 0x1). When
    ``first_fragment_id`` is supplied, ``logical_chunk`` is computed from it;
    otherwise only raw ``fragment_id`` is returned with ``logical_chunk=-1``.
    """
    normalized = normalize_config_notification(data)
    if normalized is None:
        return None

    program_class = normalized[3] >> 4
    if program_class != IRRIGATION_PROGRAM_CLASS:
        return None

    program_index = normalized[3] & 0x0F
    if program_index >= IRRIGATION_PROGRAM_COUNT:
        return None

    fragment_id = normalized[2]
    logical_chunk = (
        first_fragment_id - fragment_id
        if first_fragment_id is not None
        else -1
    )
    if first_fragment_id is not None and not 0 <= logical_chunk < IRRIGATION_LOGICAL_CHUNKS:
        return None
    if (
        first_fragment_id is not None
        and len(normalized) < IRRIGATION_CHUNK_MIN_LENGTHS[logical_chunk]
    ):
        return None

    return {
        "program_index": program_index,
        "fragment_id": fragment_id,
        "logical_chunk": logical_chunk,
    }


def _apply_irrigation_chunk(
    program: IrrigationProgram,
    normalized: bytes,
    logical_chunk: int,
    *,
    max_stations: int,
    name_part_1: bytes | None,
) -> bytes | None:
    """Apply one logical chunk to ``program``. Returns updated name part 1 if set."""
    if logical_chunk == 0:
        return bytes(normalized[4:20]).split(b"\x00", 1)[0]

    if logical_chunk == 1:
        part_2 = bytes(normalized[4:20]).split(b"\x00", 1)[0]
        program["name"] = ((name_part_1 or b"") + part_2).decode(
            "utf-8", errors="replace"
        )
        return name_part_1

    if logical_chunk == 2:
        program["inter_station_delay"] = struct.unpack(">H", normalized[4:6])[0]
        program["water_budget"] = struct.unpack(">H", normalized[6:8])[0]
        program["cycle"] = normalized[8]
        program["week_days"] = normalized[9]
        program["period_length"] = normalized[10]
        if len(normalized) > 11:
            program["synchro_day"] = normalized[11]
        program["period_start_date"] = parse_period_start_date(normalized)
        return name_part_1

    if logical_chunk == 3:
        offset = 4
        for slot in range(8):
            minutes = struct.unpack(">H", normalized[offset : offset + 2])[0]
            program["start_times"][slot] = (
                None if minutes >= DISABLED_START_TIME else minutes
            )
            offset += 2
        return name_part_1

    duration_slots = {
        4: range(0, 5),
        5: range(5, 10),
        6: range(10, min(12, max_stations)),
    }
    if logical_chunk in duration_slots:
        offset = 4
        for station_index in duration_slots[logical_chunk]:
            if station_index >= max_stations:
                break
            program["station_durations"][station_index] = _parse_config_int3(
                normalized[offset],
                normalized[offset + 1],
                normalized[offset + 2],
            )
            offset += 3
        return name_part_1

    return name_part_1


def assemble_irrigation_programs(
    payloads: Sequence[bytes | bytearray],
    *,
    max_stations: int = MAX_STATION_NUM,
) -> dict[int, IrrigationProgram]:
    """Assemble irrigation programs from V5 config notification payloads."""
    grouped: dict[int, list[bytes]] = {}
    for payload in payloads:
        normalized = normalize_config_notification(payload)
        if normalized is None:
            continue
        program_class = normalized[3] >> 4
        if program_class != IRRIGATION_PROGRAM_CLASS:
            continue
        program_index = normalized[3] & 0x0F
        if program_index >= IRRIGATION_PROGRAM_COUNT:
            continue
        grouped.setdefault(program_index, []).append(normalized)

    programs: dict[int, IrrigationProgram] = {}
    for program_index, fragments in grouped.items():
        first_fragment_id = max(fragment[2] for fragment in fragments)
        program = _empty_irrigation_program(max_stations=max_stations)
        name_part_1: bytes | None = None

        for normalized in sorted(fragments, key=lambda item: item[2], reverse=True):
            parsed = parse_irrigation_config_fragment(
                normalized, first_fragment_id=first_fragment_id
            )
            if parsed is None:
                continue
            name_part_1 = _apply_irrigation_chunk(
                program,
                normalized,
                parsed["logical_chunk"],
                max_stations=max_stations,
                name_part_1=name_part_1,
            )

        programs[program_index] = program

    return programs


def irrigation_program_complete(
    payloads: Sequence[bytes | bytearray],
    program_index: int,
) -> bool:
    """Return True when ``program_index`` includes every valid logical chunk."""
    fragments: list[bytes] = []
    for payload in payloads:
        normalized = normalize_config_notification(payload)
        if normalized is None:
            continue
        if (normalized[3] >> 4) != IRRIGATION_PROGRAM_CLASS:
            continue
        if (normalized[3] & 0x0F) != program_index:
            continue
        fragments.append(normalized)

    if not fragments:
        return False

    first_fragment_id = max(fragment[2] for fragment in fragments)
    logical_chunks = {
        parsed["logical_chunk"]
        for fragment in fragments
        if (
            parsed := parse_irrigation_config_fragment(
                fragment, first_fragment_id=first_fragment_id
            )
        )
        is not None
    }
    return logical_chunks == set(range(IRRIGATION_LOGICAL_CHUNKS))


def irrigation_program_has_final_chunk(
    payloads: Sequence[bytes | bytearray],
    program_index: int,
) -> bool:
    """Backward-compatible alias for complete irrigation-program validation."""
    return irrigation_program_complete(payloads, program_index)


def irrigation_config_complete(
    payloads: Sequence[bytes | bytearray],
    *,
    program_count: int = IRRIGATION_PROGRAM_COUNT,
) -> bool:
    """Return True when all expected irrigation programs are complete."""
    return all(
        irrigation_program_complete(payloads, program_index)
        for program_index in range(program_count)
    )
