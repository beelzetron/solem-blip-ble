"""Synthetic tests for V5 irrigation schedule/config parsing."""

from __future__ import annotations

import struct

from solem_blip_ble import protocol

PROGRAM_TAG = 0x10
FIRST_FRAGMENT_ID = 6


def _frame(fragment_id: int, payload: bytes) -> bytes:
    header = bytes([0x39, 0x00, fragment_id, PROGRAM_TAG])
    body = payload.ljust(16, b"\x00")[:16]
    return header + body


def _program_a_fragments() -> list[bytes]:
    name_part_1 = b"Morning Program"
    name_part_2 = b""
    header = struct.pack(
        ">HHBBB",
        30,
        100,
        0,
        0x7F,
        1,
    )
    start_times = struct.pack(
        ">8H",
        420,
        720,
        protocol.DISABLED_START_TIME,
        protocol.DISABLED_START_TIME,
        protocol.DISABLED_START_TIME,
        protocol.DISABLED_START_TIME,
        protocol.DISABLED_START_TIME,
        protocol.DISABLED_START_TIME,
    )
    durations_1_5 = b"".join(
        struct.pack(">I", seconds)[1:] for seconds in (600, 900, 0, 1200, 300)
    )
    durations_6_10 = b"".join(
        struct.pack(">I", seconds)[1:] for seconds in (450, 0, 0, 0, 0)
    )
    durations_11_12 = b"".join(
        struct.pack(">I", seconds)[1:] for seconds in (180, 240)
    )

    return [
        _frame(6, name_part_1[:16]),
        _frame(5, name_part_2[:16]),
        _frame(4, header),
        _frame(3, start_times),
        _frame(2, durations_1_5),
        _frame(1, durations_6_10),
        _frame(0, durations_11_12),
    ]


def _program_b_fragments() -> list[bytes]:
    fragments = []
    for fragment_id, payload in (
        (6, b"Evening"),
        (5, b""),
        (4, struct.pack(">HHBBB", 15, 80, 1, 0x3E, 2)),
        (3, struct.pack(">8H", 1080, 1320, 1440, 1440, 1440, 1440, 1440, 1440)),
        (2, b"".join(struct.pack(">I", seconds)[1:] for seconds in (500, 500, 500, 0, 0))),
        (1, b"".join(struct.pack(">I", seconds)[1:] for seconds in (0, 0, 0, 0, 0))),
        (0, b"".join(struct.pack(">I", seconds)[1:] for seconds in (0, 0))),
    ):
        frame = bytes([0x39, 0x00, fragment_id, 0x11]) + payload.ljust(16, b"\x00")[:16]
        fragments.append(frame)
    return fragments


def _program_c_fragments() -> list[bytes]:
    return [
        bytes([0x39, 0x00, fragment_id, 0x12]) + b"\x00" * 16
        for fragment_id in range(FIRST_FRAGMENT_ID, -1, -1)
    ]


def test_pack_get_irrigation_config():
    assert protocol.pack_get_irrigation_config() == bytes.fromhex("3900")


def test_normalize_config_notification_accepts_wrapped_frame():
    bare = bytes.fromhex("39061010") + b"x" * 16
    wrapped = bytes([0x10]) + bare
    assert protocol.normalize_config_notification(bare) == bare
    assert protocol.normalize_config_notification(wrapped) == bare


def test_normalize_config_notification_accepts_response_type_offset():
    payload = bytes.fromhex("3a061010") + b"x" * 16
    assert protocol.normalize_config_notification(payload) == payload


def test_parse_irrigation_config_fragment():
    payload = _program_a_fragments()[0]
    parsed = protocol.parse_irrigation_config_fragment(
        payload, first_fragment_id=FIRST_FRAGMENT_ID
    )
    assert parsed == {
        "program_index": 0,
        "fragment_id": 6,
        "logical_chunk": 0,
    }


def test_parse_irrigation_config_fragment_rejects_non_irrigation_class():
    payload = bytes([0x39, 0x00, 6, 0x20]) + b"x" * 16
    assert protocol.parse_irrigation_config_fragment(payload) is None


def test_assemble_irrigation_program_a():
    programs = protocol.assemble_irrigation_programs(
        _program_a_fragments(), max_stations=12
    )
    assert programs[0]["name"] == "Morning Program"
    assert programs[0]["inter_station_delay"] == 30
    assert programs[0]["water_budget"] == 100
    assert programs[0]["cycle"] == 0
    assert programs[0]["week_days"] == 0x7F
    assert programs[0]["period_length"] == 1
    assert programs[0]["start_times"][:2] == [420, 720]
    assert programs[0]["start_times"][2:] == [None] * 6
    assert programs[0]["station_durations"][:5] == [600, 900, 0, 1200, 300]
    assert programs[0]["station_durations"][5] == 450
    assert programs[0]["station_durations"][10:12] == [180, 240]


def test_assemble_irrigation_program_b():
    programs = protocol.assemble_irrigation_programs(
        _program_b_fragments(), max_stations=12
    )
    assert programs[1]["name"] == "Evening"
    assert programs[1]["water_budget"] == 80
    assert programs[1]["start_times"][:2] == [1080, 1320]
    assert programs[1]["start_times"][2:] == [None] * 6
    assert programs[1]["station_durations"][:3] == [500, 500, 500]


def test_irrigation_config_complete():
    payloads = _program_a_fragments() + _program_b_fragments()
    assert protocol.irrigation_program_has_final_chunk(payloads, 0)
    assert protocol.irrigation_program_has_final_chunk(payloads, 1)
    assert not protocol.irrigation_program_has_final_chunk(payloads, 2)
    assert not protocol.irrigation_config_complete(payloads)

    full_payloads = payloads + _program_c_fragments()
    assert protocol.irrigation_program_has_final_chunk(full_payloads, 2)
    assert protocol.irrigation_config_complete(full_payloads)


def test_irrigation_config_incomplete_when_middle_chunk_is_missing():
    payloads = _program_a_fragments() + _program_b_fragments() + _program_c_fragments()
    payloads.remove(_program_a_fragments()[3])

    assert not protocol.irrigation_program_has_final_chunk(payloads, 0)
    assert not protocol.irrigation_config_complete(payloads)


def test_irrigation_config_incomplete_when_chunk_is_undersized():
    payloads = _program_a_fragments() + _program_b_fragments() + _program_c_fragments()
    payloads[payloads.index(_program_a_fragments()[3])] = _program_a_fragments()[3][:-1]

    assert not protocol.irrigation_program_has_final_chunk(payloads, 0)
    assert not protocol.irrigation_config_complete(payloads)
